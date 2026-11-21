"""One open handle per statefile, cached in memory.

This is the part that makes an append O(1). Mainplane's ``statefile.File``
keeps ``f *os.File`` plus ``n int`` and its harness holds it in ``session.f``
for the whole step ("a cache of the file, never the other way round"). A Hermes
plugin has no session object, but it does live in one long-lived gateway /
plugin-host process, so a process-wide LRU of open handles is the equivalent.

Only the handle, ``n`` and the byte offset are cached -- not the transcript.
Rebuilding context re-reads the file (as Mainplane's ``Sessions.Load`` does);
that is once per rebuild, not once per append. One scan on first open (or after
another writer changed the file); every later append is a single write at the
cached offset. A torn final record is truncated on open, as in
``statefile.Open``.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

try:  # advisory lock is POSIX-only; Windows falls back to unlocked appends
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

from .records import (
    KINDS,
    Record,
    RecordFormatError,
    _check_monotonic,
    build_context,
    encode_record,
    new_id,
    read_records,
    scan_records,
    utc_now,
)

# One process serves many sessions; cap the descriptors we keep open.
MAX_OPEN_STATEFILES = 128
_cache_lock = threading.RLock()
_cache: OrderedDict[str, StateFile] = OrderedDict()


def _as_body(body) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return bytes(body)


def _lock(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class StateFile:
    """A session's append-only log held open with its last ``n`` cached.

    ``append`` is O(1). ``read`` / ``context`` rebuild from disk. A closed
    handle (LRU eviction) reopens at the cached offset without rescanning,
    because the file is append-only.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None
        self._n = 0
        self._offset = 0
        self._opened = False
        self._lock = threading.RLock()
        self.resync_count = 0  # full scans; 1 in steady state

    def _resync(self) -> None:
        """Scan once, truncate any torn tail, and keep the handle at its tip."""
        self.close()
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        records, good = scan_records(p)
        _check_monotonic(records)
        fh = p.open("r+b" if p.exists() else "w+b")
        fh.seek(0, os.SEEK_END)
        if fh.tell() != good:
            fh.truncate(good)
        fh.seek(good)
        self._fh = fh
        self._n = records[-1].n if records else 0
        self._offset = good
        self._opened = True
        self.resync_count += 1
        os.chmod(p, 0o600)

    def _reopen(self) -> None:
        """Reopen at the cached offset, O(1), after an LRU eviction."""
        self.close()
        if not self.path.exists():
            self._resync()
            return
        fh = self.path.open("r+b")
        if fh.seek(0, os.SEEK_END) != self._offset:
            fh.close()
            self._resync()  # someone else wrote; rescan once
            return
        fh.seek(self._offset)
        self._fh = fh

    def _ensure(self) -> None:
        if self._fh is None:
            if self._opened:
                self._reopen()
            else:
                self._resync()
            return
        try:
            if os.fstat(self._fh.fileno()).st_size == self._offset:
                return
        except OSError:
            pass
        self._resync()  # another writer changed the file under us

    def append(self, kind: str, body=b"", *, id: str | None = None,
               step: str = "", upto: int | None = None,
               ts: str | None = None, **extra) -> Record:
        """Append one record. O(1) in the steady state.

        ``upto`` is required for ``step`` records: it must be the highest ``n``
        visible when that step's *prompt was built* (``build_context()``'s
        second return value), captured before the step's blocks and results
        are appended. Auto-filling it at append time would include them and
        mis-order context (review finding F6).
        """
        if kind not in KINDS:
            raise ValueError(f"unknown record kind: {kind!r}")
        if kind == "step" and upto is None:
            raise ValueError(
                "step records require an explicit upto (capture it from "
                "build_context() at prompt-build time)")
        upto_val = 0 if upto is None else int(upto)
        raw = _as_body(body)
        with self._lock:
            for _attempt in range(3):
                self._ensure()
                fh = self._fh
                _lock(fh)
                try:
                    if os.fstat(fh.fileno()).st_size != self._offset:
                        continue  # lost the race; resync on the next attempt
                    rec = Record(
                        n=self._n + 1,
                        kind=kind,
                        body=raw,
                        id=id or new_id(),
                        step=step,
                        upto=upto_val,
                        ts=ts or utc_now(),
                        extra=extra,
                    )
                    data = encode_record(rec)
                    fh.seek(self._offset)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                    self._offset += len(data)
                    self._n = rec.n
                    return rec
                finally:
                    _unlock(fh)
            raise RecordFormatError("statefile kept changing under concurrent writers")

    def read(self) -> list[Record]:
        with self._lock:
            self._ensure()
            return read_records(self.path)

    def context(self) -> tuple[list[Record], int]:
        return build_context(self.read())

    @property
    def last_n(self) -> int:
        with self._lock:
            self._ensure()
            return self._n

    def __len__(self) -> int:
        return len(self.read())

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                fh, self._fh = self._fh, None
                fh.close()

    def __repr__(self) -> str:
        return f"<StateFile {self.path} n={self._n}>"


def get_statefile(path) -> StateFile:
    """Return the process-wide open handle for ``path`` (LRU-cached)."""
    key = str(Path(path).resolve())
    with _cache_lock:
        sf = _cache.get(key)
        if sf is None:
            sf = StateFile(key)
            _cache[key] = sf
        else:
            _cache.move_to_end(key)
        evicted = []
        while len(_cache) > MAX_OPEN_STATEFILES:
            _, old = _cache.popitem(last=False)
            evicted.append(old)
    for old in evicted:  # close outside _cache_lock to avoid lock inversion
        old.close()
    return sf


def append_record(path, kind: str, body=b"", *, id: str | None = None,
                  step: str = "", upto: int | None = None,
                  ts: str | None = None, **extra) -> Record:
    """Append one record via the cached open handle (O(1)).

    ``step`` records must pass ``upto`` explicitly -- see ``StateFile.append``.
    """
    return get_statefile(path).append(
        kind, body, id=id, step=step, upto=upto, ts=ts, **extra)
