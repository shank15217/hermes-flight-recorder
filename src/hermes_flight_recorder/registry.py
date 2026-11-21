"""Per-profile registry of opted-in sessions.

Pure library code: it knows nothing about Hermes. The plugin layer resolves
``plugin_data_dir()`` at call time and hands the resulting directory in, which
keeps this testable and follows the active Hermes profile.

A tag is sticky: it lives in ``registry.json`` under the plugin data root and
survives turns, restarts and ``hermes plugins update``. Untagged sessions have
no entry and no statefile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

try:  # advisory lock is POSIX-only; Windows falls back to unlocked writes
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

from .records import new_id
from .statefile import StateFile, append_record, forget_statefile, get_statefile

# One owner for this fact: re-exported from the package facade.
TAGGED_SESSION_TYPE = "mainplane"
REGISTRY_VERSION = 1
DEFAULT_CONTEXT_LIMIT = 250000
DEFAULT_COLD_LIMIT = 100000

# Process-wide read cache. Keyed by resolved registry.json path and validated
# by (mtime_ns, size, inode), so a write from ANY process invalidates it. A
# hook used to pay two full JSON parses + four chmods per tool call; now the
# steady-state read is one stat (W5).
_registry_cache: dict[str, dict] = {}
_registry_cache_lock = threading.RLock()


def _cache_key(p: Path) -> str:
    return str(p.resolve())


def _snapshot(data: dict) -> dict:
    """Cheap copy of a parsed registry.

    Callers mutate ``data["sessions"]`` (pop/assign) but never an entry dict
    in place, so a shallow copy of that mapping is enough. deepcopy here cost
    ~11 ms per read on a 1000-entry registry -- worse than no cache at all.
    """
    return {**data, "sessions": dict(data["sessions"])}


class RegistryError(Exception):
    """The registry is unreadable or has an unsupported shape. Never reset it."""


@dataclass(frozen=True)
class SessionEntry:
    """One opted-in session. See docs/design.md section 4 for the schema."""

    session_id: str
    type: str = TAGGED_SESSION_TYPE
    enabled: bool = True
    statefile: str = ""
    agent_id: str = ""
    chain: tuple[str, ...] = ()
    parent: str | None = None
    created_at: str = ""
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    cold_limit: int = DEFAULT_COLD_LIMIT
    instructions: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "type": self.type,
            "enabled": self.enabled,
            "statefile": self.statefile,
            "agent_id": self.agent_id,
            "chain": list(self.chain),
            "parent": self.parent,
            "created_at": self.created_at,
            "context_limit": self.context_limit,
            "cold_limit": self.cold_limit,
            "instructions": self.instructions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SessionEntry:
        enabled = d.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RegistryError("enabled must be a boolean")
        return cls(
            session_id=str(d["session_id"]),
            type=str(d.get("type", TAGGED_SESSION_TYPE)),
            enabled=enabled,
            statefile=str(d.get("statefile", "")),
            agent_id=str(d.get("agent_id", "")),
            chain=tuple(str(x) for x in d.get("chain", ())),
            parent=d.get("parent"),
            created_at=str(d.get("created_at", "")),
            context_limit=int(d.get("context_limit", DEFAULT_CONTEXT_LIMIT)),
            cold_limit=int(d.get("cold_limit", DEFAULT_COLD_LIMIT)),
            instructions=str(d.get("instructions", "")),
        )


def _entry_from(raw) -> SessionEntry:
    """Parse one registry value; malformed data is a RegistryError, not a raw TypeError (F3)."""
    try:
        return SessionEntry.from_dict(raw)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RegistryError(f"malformed session entry: {exc}") from exc


def _check_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id:
        raise RegistryError("session id is empty")
    if "\x00" in session_id:
        raise RegistryError("session id contains a NUL byte")
    if len(session_id) > 512:
        raise RegistryError("session id is too long")


def _statefile_name(session_id: str) -> str:
    """A filesystem-safe name that cannot escape the sessions directory."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:40].strip("._") or "session"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}.state"


class Registry:
    """Sticky, per-profile map of tagged sessions.

    All writes are serialized with an advisory lock and published atomically
    (temp file + ``os.replace``) so a crash can never leave a half registry.
    """

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "registry.json"
        self.sessions_dir = self.data_dir / "sessions"
        self.lock_path = self.data_dir / "registry.lock"

    def _ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.sessions_dir, 0o700)

    def _statefile_path(self, session_id: str) -> Path:
        """Derive the statefile from the id; never trust a stored path.

        Mainplane's ``Sessions.Path(id)`` calls "the filesystem is the
        registry": a statefile's location is a function of its id, not data
        that a hand-edited registry can redirect.
        """
        p = self.sessions_dir / _statefile_name(session_id)
        if not p.resolve().is_relative_to(self.sessions_dir.resolve()):
            raise RegistryError("statefile path escapes the sessions directory")
        return p

    def _bind(self, entry: SessionEntry) -> SessionEntry:
        """Overwrite any stored statefile with the derived, contained one."""
        return replace(entry, statefile=str(self._statefile_path(entry.session_id)))

    @contextmanager
    def _lock(self, ensure_dirs: bool = True):
        if ensure_dirs:
            self._ensure_dirs()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict:
        p = self.path
        if not p.exists():
            with _registry_cache_lock:
                _registry_cache.pop(_cache_key(p), None)
            return {"version": REGISTRY_VERSION, "sessions": {}}
        try:
            st = p.stat()
        except OSError as exc:
            raise RegistryError(f"registry is unreadable: {exc}") from exc
        key = _cache_key(p)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with _registry_cache_lock:
            hit = _registry_cache.get(key)
            if hit is not None and hit["stamp"] == stamp:
                return _snapshot(hit["data"])
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"registry is unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
            raise RegistryError(
                f"unsupported registry version: {data.get('version')!r}"
                if isinstance(data, dict) else "registry is not an object")
        if not isinstance(data.get("sessions"), dict):
            raise RegistryError("registry has no sessions map")
        # Re-stat after the read: a concurrent writer changed the file, so the
        # next _load must not trust this stamp.
        st2 = p.stat()
        with _registry_cache_lock:
            _registry_cache[key] = {
                "stamp": (st2.st_mtime_ns, st2.st_size, st2.st_ino),
                "data": _snapshot(data),
            }
        return data

    def _save(self, data: dict) -> None:
        self._ensure_dirs()
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        if os.name == "posix":
            dfd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        st = self.path.stat()
        with _registry_cache_lock:
            _registry_cache[_cache_key(self.path)] = {
                "stamp": (st.st_mtime_ns, st.st_size, st.st_ino),
                "data": _snapshot(data),
            }

    def get(self, session_id: str) -> SessionEntry | None:
        if not session_id or not self.path.exists():
            return None
        with self._lock(ensure_dirs=False):
            raw = self._load()["sessions"].get(session_id)
        return self._bind(_entry_from(raw)) if raw is not None else None

    def is_tagged(self, session_id: str) -> bool:
        entry = self.get(session_id)
        return entry is not None and entry.enabled

    def tagged(self) -> list[SessionEntry]:
        if not self.path.exists():
            return []
        with self._lock(ensure_dirs=False):
            sessions = self._load()["sessions"]
        entries = [self._bind(_entry_from(v)) for v in sessions.values()]
        return sorted((e for e in entries if e.enabled),
                      key=lambda e: e.created_at)

    def tag(self, session_id: str, *, context_limit: int = DEFAULT_CONTEXT_LIMIT,
            cold_limit: int = DEFAULT_COLD_LIMIT, instructions: str = "",
            parent: str | None = None, agent_id: str | None = None) -> SessionEntry:
        """Enable a session. Idempotent, and re-enabling resumes the same chain."""
        _check_session_id(session_id)
        with self._lock():
            data = self._load()
            raw = data["sessions"].get(session_id)
            if raw is not None:
                entry = self._bind(_entry_from(raw))
                if not entry.enabled:
                    entry = replace(entry, enabled=True)
                    data["sessions"][session_id] = entry.to_dict()
                    self._save(data)
                return entry
            sf_path = self._statefile_path(session_id)
            entry = SessionEntry(
                session_id=session_id,
                statefile=str(sf_path),
                agent_id=agent_id or new_id(),
                chain=(session_id,),
                parent=parent,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                context_limit=int(context_limit),
                cold_limit=int(cold_limit),
                instructions=instructions,
            )
            self._ensure_dirs()
            if not sf_path.exists():
                append_record(sf_path, "start", f"mainplane session {session_id}",
                              via="/statefile on")
            data["sessions"][session_id] = entry.to_dict()
            self._save(data)
            return entry

    def untag(self, session_id: str) -> bool:
        """Disable a session. Keep the entry and the .state file for resume."""
        _check_session_id(session_id)
        with self._lock():
            data = self._load()
            raw = data["sessions"].get(session_id)
            if raw is None:
                return False
            entry = SessionEntry.from_dict(raw)
            if not entry.enabled:
                return False
            data["sessions"][session_id] = replace(entry, enabled=False).to_dict()
            self._save(data)
            return True

    def statefile(self, session_id: str) -> StateFile | None:
        entry = self.get(session_id)
        if entry is None or not entry.enabled:
            return None
        return get_statefile(entry.statefile)

    # --- cleanup ----------------------------------------------------------

    def archive_many(self, session_ids) -> list[SessionEntry]:
        """Drop registry entries but KEEP their .state files (design §11).

        One lock/read/write for a whole bulk delete, so `on_session_delete`
        does not re-read the registry once per id.
        """
        entries = []
        with self._lock():
            data = self._load()
            for sid in session_ids:
                sid = str(sid)
                raw = data["sessions"].pop(sid, None)
                if raw is not None:
                    entries.append(self._bind(_entry_from(raw)))
            if entries:
                self._save(data)
        return entries

    def archive(self, session_id: str) -> SessionEntry | None:
        """Drop one registry entry, keeping its .state file for audit."""
        _check_session_id(session_id)
        found = self.archive_many([session_id])
        return found[0] if found else None

    def purge(self, session_id: str) -> dict:
        """Remove one session entirely: registry entry + .state file + cached handle."""
        entry = self.archive(session_id)
        if entry is None:
            return {"found": False, "statefile_removed": False}
        sf_path = Path(entry.statefile)
        removed = False
        if sf_path.exists() and sf_path.resolve().is_relative_to(
                self.sessions_dir.resolve()):
            forget_statefile(sf_path)
            sf_path.unlink()
            removed = True
        return {"found": True, "statefile_removed": removed,
                "statefile": str(sf_path)}

    def purge_all(self) -> dict:
        """Remove ALL plugin state: entries, every .state file, registry files.

        Irreversible. The empty data directories remain; UNINSTALL.md shows how
        to delete them after `hermes plugins remove`.
        """
        with self._lock():
            data = self._load()
            count = len(data["sessions"])
            data["sessions"] = {}
            self._save(data)
        removed = 0
        if self.sessions_dir.exists():
            for p in self.sessions_dir.glob("*.state"):
                if p.resolve().is_relative_to(self.sessions_dir.resolve()):
                    forget_statefile(p)
                    p.unlink()
                    removed += 1
        for name in ("registry.json", "registry.lock"):
            f = self.data_dir / name
            if f.exists():
                f.unlink()
        with _registry_cache_lock:
            _registry_cache.pop(_cache_key(self.path), None)
        return {"entries": count, "statefiles": removed}
