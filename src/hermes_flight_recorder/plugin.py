"""Hermes plugin entry point (roadmap step 3 + cleanup).

Registers the ``/statefile`` command, five observer hooks that mirror tagged
sessions, and one lifecycle hook that archives entries when Hermes deletes a
session. Recording hooks are inert for untagged sessions. If
``ctx.redact_text`` is unavailable the recording hooks are NOT registered at
all (F7, fail closed) — the cleanup hook still is, since it stores no user
text.
"""

from __future__ import annotations

PLUGIN_ID = "hermes-flight-recorder"
COMMAND_NAME = "statefile"

RECORDING_HOOK_NAMES = (
    "pre_llm_call",
    "post_tool_call",
    "post_llm_call",
    "on_session_end",
    "on_session_open",
    "on_session_reset",
    "post_compaction",
)
CLEANUP_HOOK_NAMES = ("on_session_delete",)
HOOK_NAMES = RECORDING_HOOK_NAMES + CLEANUP_HOOK_NAMES


def register(ctx):
    """Wire the command, cleanup hook, and (if redaction exists) recording hooks."""

    def _command(raw_args: str, *, session_id: str = "", session_key: str = "",
                 platform: str = "", source=None) -> str:
        # A slash-command boundary is the one place a blind catch is correct:
        # any failure must become a chat reply, never a broken session.
        try:
            from plugins.plugin_storage import plugin_data_dir

            from .commands import handle_statefile
            from .registry import Registry

            registry = Registry(plugin_data_dir(PLUGIN_ID))
            return handle_statefile(raw_args, session_id=session_id,
                                    registry=registry)
        except Exception as exc:  # noqa: BLE001
            return f"/statefile: error: {type(exc).__name__}: {exc}"

    ctx.register_command(
        COMMAND_NAME,
        handler=_command,
        description="Opt in to Mainplane-style append-only statefile recording for this session",
        args_hint="on|off|status|purge|purge-all",
    )

    try:
        redact = ctx.redact_text
    except AttributeError:
        redact = None

    def data_dir():
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_ID)

    from .recording import Recorder

    recorder = Recorder(data_dir, redact)

    # Cleanup must run even when redaction is unavailable: it stores only
    # Hermes-provided enum fields, not transcript text.
    ctx.register_hook("on_session_delete", recorder.on_delete)

    if redact is None:
        # Fail closed: F7 is a hard gate. No recording hooks means no
        # unredacted writes.
        return

    handlers = {
        "pre_llm_call": recorder.on_pre_llm,
        "post_tool_call": recorder.on_post_tool,
        "post_llm_call": recorder.on_post_llm,
        "on_session_end": recorder.on_session_end,
        "on_session_open": recorder.on_session_open,
        "on_session_reset": recorder.on_reset,
        "post_compaction": recorder.on_compaction,
    }
    for name in RECORDING_HOOK_NAMES:
        ctx.register_hook(name, handlers[name])
