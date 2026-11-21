"""hermes-flight-recorder — opt-in append-only statefile sessions for Hermes Agent.

Roadmap steps 1-2 (done): record format / deterministic context rebuild, and a
sticky per-session registry behind ``/statefile on|off|status``. Step 3 (hook
mirroring into a live session) is NOT implemented: the plugin registers one
command, no hooks, no tools.

Design and attribution: see README, NOTICE and docs/design.md.
"""

__version__ = "0.3.0.dev3"

from .commands import USAGE, handle_statefile
from .records import (
    BLOCK_KINDS,
    CONTEXT_EXCLUDED,
    KINDS,
    Record,
    RecordFormatError,
    build_context,
    new_id,
    read_records,
    scan_records,
)
from .registry import (
    DEFAULT_COLD_LIMIT,
    DEFAULT_CONTEXT_LIMIT,
    REGISTRY_VERSION,
    TAGGED_SESSION_TYPE,
    Registry,
    RegistryError,
    SessionEntry,
)
from .statefile import StateFile, append_record, get_statefile

__all__ = [
    "BLOCK_KINDS",
    "CONTEXT_EXCLUDED",
    "DEFAULT_COLD_LIMIT",
    "DEFAULT_CONTEXT_LIMIT",
    "KINDS",
    "REGISTRY_VERSION",
    "TAGGED_SESSION_TYPE",
    "USAGE",
    "Record",
    "RecordFormatError",
    "Registry",
    "RegistryError",
    "SessionEntry",
    "StateFile",
    "__version__",
    "append_record",
    "build_context",
    "get_statefile",
    "handle_statefile",
    "new_id",
    "read_records",
    "scan_records",
]
