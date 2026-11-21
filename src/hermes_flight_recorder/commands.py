"""Slash-command logic for ``/statefile``.

Hermes-free on purpose: the plugin layer supplies a :class:`Registry` and the
calling session id, and this module only formats replies. Nothing here writes
for an untagged session.
"""

from __future__ import annotations

from .records import RecordFormatError
from .registry import TAGGED_SESSION_TYPE, Registry, RegistryError
from .statefile import get_statefile

USAGE = (
    "/statefile on      enable Mainplane-style statefile recording for this session\n"
    "/statefile off     disable it (the .state file is kept, not deleted)\n"
    "/statefile status  show this session's recording state\n"
    "/statefile help    show this help\n"
    "\n"
    "Only the session where you run /statefile on is affected. Untagged "
    "sessions are untouched."
)


def _describe(registry: Registry, session_id: str) -> str:
    entry = registry.get(session_id)
    if entry is None or not entry.enabled:
        return (f"/statefile: session {session_id} is not enabled "
                f"(type={TAGGED_SESSION_TYPE} recording off).")
    sf = get_statefile(entry.statefile)
    try:
        records = sf.read()
    except (RecordFormatError, OSError) as exc:
        return f"/statefile: statefile is unreadable: {exc}"
    last = records[-1].n if records else 0
    return (
        f"/statefile: session {session_id}\n"
        f"  type: {entry.type}\n"
        f"  statefile: {entry.statefile}\n"
        f"  records: {len(records)} (last n={last})\n"
        f"  chain: {len(entry.chain)} session(s)\n"
        f"  context_limit: {entry.context_limit}  cold_limit: {entry.cold_limit}\n"
        f"  created: {entry.created_at}"
    )


def handle_statefile(raw_args: str, *, session_id: str = "",
                     registry: Registry) -> str:
    """Handle one ``/statefile`` invocation. Always returns text, never raises.

    ``session_id`` comes from the calling-session keywords Hermes passes to a
    registered command handler. With no id we fail closed rather than guessing
    which session to tag.
    """
    parts = (raw_args or "").strip().split()
    verb = parts[0].lower() if parts else "status"
    if verb in ("help", "?", "-h", "--help"):
        return USAGE
    if verb not in ("on", "off", "status"):
        return f"Unknown /statefile argument {parts[0]!r}.\n\n{USAGE}"
    if not session_id:
        return ("/statefile: no session id in this chat; nothing to tag. "
                "Run it inside a session.")

    try:
        if verb == "on":
            already = registry.is_tagged(session_id)
            registry.tag(session_id)
            head = "already enabled" if already else "enabled"
            return f"/statefile: {head} for this session.\n\n{_describe(registry, session_id)}"

        if verb == "off":
            if not registry.untag(session_id):
                return "/statefile: this session is not enabled; nothing changed."
            return ("/statefile: disabled for this session. The .state file is kept "
                    "on disk; enable again to resume the same chain.")

        return _describe(registry, session_id)
    except (RegistryError, OSError, RecordFormatError) as exc:
        return f"/statefile: error: {exc}"
