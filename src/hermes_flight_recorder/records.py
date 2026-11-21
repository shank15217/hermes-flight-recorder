"""Record vocabulary and on-disk shape (specification only, no implementation yet).

One record = one JSON header line + a body of exactly ``header["len"]`` bytes +
a newline. The body is never escaped and never re-encoded: "when an upstream
shape is uncertain, keep the bytes" (Mainplane AGENTS.md, credited in NOTICE).
"""

from __future__ import annotations

# Record kinds. ``start``, ``config`` and ``name`` are metadata and never enter
# rebuild context. The rest mirror the upstream vocabulary deliberately so the
# format is legible to anyone who has read Mainplane's statefile docs.
KINDS = (
    "start", "config", "system", "message", "text", "thinking",
    "call", "step", "result", "error", "name",
)
CONTEXT_EXCLUDED = frozenset({"start", "config", "name"})
BLOCK_KINDS = frozenset({"text", "thinking", "call"})


def append_record(path, kind: str, body: bytes, **header):
    """Append one record. Roadmap step 1."""
    raise NotImplementedError("record I/O lands in roadmap step 1")


def read_records(path):
    """Yield records in file (time) order. Roadmap step 1."""
    raise NotImplementedError("record I/O lands in roadmap step 1")


def build_context(records):
    """Reorder time-ordered records into API context order. Roadmap step 1.

    Results directly after their step; messages as one user turn; blocks with
    their step; unclosed blocks dropped; ``CONTEXT_EXCLUDED`` never emitted.
    """
    raise NotImplementedError("Build() lands in roadmap step 1")
