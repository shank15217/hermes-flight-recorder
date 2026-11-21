"""Append-only record format and deterministic context rebuild.

One record = one JSON header line + a body of exactly ``header["len"]`` bytes
+ a newline. The body is never escaped, never decoded, never re-encoded: "when
an upstream shape is uncertain, keep the bytes" (Mainplane AGENTS.md; credited
in NOTICE). File order is time order; :func:`build_context` rebuilds API order.

This module is pure format + parsing. Opening a file for appending, keeping
its handle and its last record number in memory, is :mod:`statefile`'s job --
that is what makes an append O(1) instead of a full rescan.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Record kinds. ``start``, ``config`` and ``name`` are metadata and never enter
# rebuilt context. The rest deliberately mirror the upstream vocabulary so the
# format is legible to anyone who has read Mainplane's statefile docs.
KINDS = (
    "start", "config", "system", "message", "text", "thinking",
    "call", "step", "result", "error", "name",
)
CONTEXT_EXCLUDED = frozenset({"start", "config", "name"})
BLOCK_KINDS = frozenset({"text", "thinking", "call"})

_HEADER_CANONICAL = frozenset({"n", "kind", "len", "id", "step", "upto", "ts"})


class RecordFormatError(ValueError):
    """The statefile is corrupt or truncated. Never guess, never partial-read."""


def new_id() -> str:
    """A short random id for a record or a step."""
    return uuid.uuid4().hex[:16]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Record:
    """One record: a canonical header plus an opaque raw body.

    ``step`` on a block (text/thinking/call) names the step record it belongs
    to. ``step`` on other records is free-form provenance. ``upto`` on a step
    record is the highest ``n`` that was in context when that step was built.
    Unknown header fields are kept verbatim in ``extra``.
    """

    n: int
    kind: str
    body: bytes = b""
    id: str = ""
    step: str = ""
    upto: int = 0
    ts: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown record kind: {self.kind!r}")
        if self.n < 1:
            raise ValueError(f"record n must be >= 1, got {self.n}")
        if self.upto < 0:
            raise ValueError(f"record upto must be >= 0, got {self.upto}")
        if not isinstance(self.body, bytes):
            object.__setattr__(self, "body", bytes(self.body))

    def to_header(self) -> dict:
        h = {"n": self.n, "kind": self.kind, "len": len(self.body)}
        if self.id:
            h["id"] = self.id
        if self.step:
            h["step"] = self.step
        if self.upto:
            h["upto"] = self.upto
        if self.ts:
            h["ts"] = self.ts
        for k, v in self.extra.items():
            h.setdefault(k, v)  # canonical fields always win
        return h

    @classmethod
    def from_header(cls, header: dict, body: bytes) -> Record:
        extra = {k: v for k, v in header.items() if k not in _HEADER_CANONICAL}
        return cls(
            n=int(header["n"]),
            kind=str(header["kind"]),
            body=bytes(body),
            id=str(header.get("id", "") or ""),
            step=str(header.get("step", "") or ""),
            upto=int(header.get("upto", 0) or 0),
            ts=str(header.get("ts", "") or ""),
            extra=extra,
        )


def encode_record(rec: Record) -> bytes:
    line = json.dumps(rec.to_header(), separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    # A newline terminates the header; a second one terminates the body. The
    # body may itself contain any byte, including newlines, NUL and invalid
    # UTF-8 -- which is why ``len`` lives in the header.
    return line + b"\n" + rec.body + b"\n"


def _decode_one(f, *, tolerate_torn: bool = False) -> Record | None:
    """Decode one record; return None at EOF or on a torn final record.

    Mirrors Mainplane's ``Decode``: EOF *inside* a record is a torn tail,
    while a malformed header is corruption and always raises.
    """
    line = f.readline()
    if line == b"":
        return None
    if not line.endswith(b"\n"):
        if tolerate_torn:
            return None
        raise RecordFormatError("truncated header line: no terminating newline")
    try:
        header = json.loads(line[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordFormatError(f"invalid header JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise RecordFormatError("header is not a JSON object")
    for key in ("n", "kind", "len"):
        if key not in header:
            raise RecordFormatError(f"header missing {key!r}")
    length = header["len"]
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise RecordFormatError(f"invalid body length: {length!r}")
    body = f.read(length)
    if len(body) != length:
        if tolerate_torn:
            return None
        raise RecordFormatError(
            f"truncated body: expected {length} bytes, got {len(body)}")
    if f.read(1) != b"\n":
        if tolerate_torn:
            return None
        raise RecordFormatError("missing body terminator after raw body")
    try:
        return Record.from_header(header, body)
    except (ValueError, KeyError, TypeError) as exc:
        # A bad header shape is corruption, not a torn tail. Keep the raw bytes
        # in the message rather than letting a bare ValueError escape (F3).
        raise RecordFormatError(f"invalid record header: {exc}") from exc


def iter_records(f, *, tolerate_torn: bool = False) -> Iterator[Record]:
    """Yield records from an open binary file object, in file (time) order."""
    while True:
        rec = _decode_one(f, tolerate_torn=tolerate_torn)
        if rec is None:
            return
        yield rec


def _check_monotonic(records: list[Record]) -> None:
    for expected, rec in enumerate(records, start=1):
        if rec.n != expected:
            raise RecordFormatError(
                f"non-monotonic record numbers: expected n={expected}, got {rec.n}")


def scan_records(path) -> tuple[list[Record], int]:
    """Scan a file ONCE. Return ``(whole_records, good_offset)``.

    ``good_offset`` is the byte offset where the first torn tail begins. A torn
    *final* record is dropped rather than raised -- Mainplane ``file.go``'s
    ``scan`` / ``Open`` behaviour. Corruption in the middle still raises.
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    records: list[Record] = []
    good = 0
    with p.open("rb") as f:
        while True:
            rec = _decode_one(f, tolerate_torn=True)
            if rec is None:
                return records, good
            records.append(rec)
            good = f.tell()
    return records, good


def read_records(path, *, strict: bool = True,
                 tolerate_torn_tail: bool = False) -> list[Record]:
    """Read a statefile completely. A missing file is an empty chain.

    With ``strict`` (default) the ``n`` sequence must be 1, 2, 3, ...; a gap or
    a duplicate is corruption, not something to paper over. With
    ``tolerate_torn_tail`` a partially-written final record is dropped (see
    :func:`scan_records`); the default keeps the strict contract.
    """
    p = Path(path)
    if not p.exists():
        return []
    if tolerate_torn_tail:
        records, _ = scan_records(p)
    else:
        with p.open("rb") as f:
            records = list(iter_records(f))
    if strict:
        _check_monotonic(records)
    return records


def _flush(pending: list[Record], step: Record | None,
           ctx: list[Record]) -> list[Record]:
    """Emit what ``step`` saw, deferring the rest.

    Results are emitted before messages/system/error records, then any record
    with ``n > step.upto`` is carried forward to the next step. With no step
    (end of chain) everything pending is emitted, results first.
    """
    seen = (lambda _r: True) if step is None else (lambda r: r.n <= step.upto)
    rest: list[Record] = []
    for r in pending:
        if not seen(r):
            rest.append(r)
        elif r.kind == "result":
            ctx.append(r)
    ctx.extend(r for r in pending if r.kind != "result" and seen(r))
    return rest


def build_context(records: Iterable[Record]) -> tuple[list[Record], int]:
    """Rebuild API context order from time-ordered records.

    For each step: the results and messages it saw, then its assistant blocks,
    then the step record itself. Blocks are streamed *before* their step record
    in the file, and blocks whose step never closed are dropped (an interrupted
    turn). ``start``, ``config`` and ``name`` never enter context.

    Returns ``(context_records, upto)`` where ``upto`` is the highest ``n`` in
    the chain -- the ``upto`` the next step record should carry.
    """
    chain = list(records)
    if not chain:
        return [], 0
    ctx: list[Record] = []
    pending: list[Record] = []
    blocks: dict[str, list[Record]] = {}
    for r in chain:
        if r.kind in CONTEXT_EXCLUDED:
            continue
        if r.kind in BLOCK_KINDS:
            blocks.setdefault(r.step, []).append(r)
        elif r.kind == "step":
            pending = _flush(pending, r, ctx)
            ctx.extend(blocks.pop(r.id, []))
            ctx.append(r)
        else:
            pending.append(r)
    _flush(pending, None, ctx)
    return ctx, chain[-1].n
