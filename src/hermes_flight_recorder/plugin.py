"""Hermes plugin entry point (roadmap step 3).

Registers the ``/statefile`` command plus five observer hooks. Hooks are inert
for untagged sessions. If ``ctx.redact_text`` is unavailable the hooks are NOT
registered at all: recording unredacted transcript text would violate F7.
"""

from __future__ import annotations

PLUGIN_ID = "hermes-flight-recorder"
COMMAND_NAME = "statefile"

HOOK_NAMES = (
    "pre_llm_call",
    "post_tool_call",
    "post_llm_call",
    "on_session_reset",
    "post_compaction",
)


def register(ctx):
    """Wire the command and (only if redaction is available) the hooks."""

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
        args_hint="on|off|status",
    )

    try:
        redact = ctx.redact_text
    except AttributeError:
        redact = None
    if redact is None:
        # Fail closed: F7 is a hard gate. No hooks means no unredacted writes.
        return

    def data_dir():
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_ID)

    from .recording import Recorder

    recorder = Recorder(data_dir, redact)
    handlers = {
        "pre_llm_call": recorder.on_pre_llm,
        "post_tool_call": recorder.on_post_tool,
        "post_llm_call": recorder.on_post_llm,
        "on_session_reset": recorder.on_reset,
        "post_compaction": recorder.on_compaction,
    }
    for name in HOOK_NAMES:
        ctx.register_hook(name, handlers[name])
