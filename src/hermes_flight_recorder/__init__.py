"""hermes-flight-recorder — opt-in append-only statefile sessions for Hermes Agent.

Roadmap step 1 (done): record format, on-disk I/O and deterministic context
rebuild in :mod:`records` / :mod:`statefile`. The plugin entry point is still
deliberately inert -- nothing is installed, nothing is hooked, no session is
touched until a later step.

Design and attribution: see README, NOTICE and docs/design.md.
"""

__version__ = "0.1.0.dev1"

# The internal session-format tag. See docs/prior-art.md "Note on naming".
TAGGED_SESSION_TYPE = "mainplane"

from .records import (
    BLOCK_KINDS,
    CONTEXT_EXCLUDED,
    KINDS,
    Record,
    RecordFormatError,
    append_record,
    build_context,
    new_id,
    read_records,
)
from .statefile import StateFile

__all__ = [
    "BLOCK_KINDS",
    "CONTEXT_EXCLUDED",
    "KINDS",
    "TAGGED_SESSION_TYPE",
    "Record",
    "RecordFormatError",
    "StateFile",
    "__version__",
    "append_record",
    "build_context",
    "new_id",
    "read_records",
]
