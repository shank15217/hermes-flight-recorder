"""Hermes plugin entry point (roadmap step 2).

Registers exactly one slash command, ``/statefile``. No hooks and no tools are
registered yet: mirroring a live session into the statefile is step 3, so an
installed plugin cannot affect an untagged session's turns today.
"""

from __future__ import annotations

PLUGIN_ID = "hermes-flight-recorder"
COMMAND_NAME = "statefile"


def register(ctx):
    """Register only ``/statefile``. Kept small so loading cannot break a chat."""

    def _handler(raw_args: str, *, session_id: str = "", session_key: str = "",
                 platform: str = "", source=None) -> str:
        # A slash-command handler runs in the gateway. Resolve the data root on
        # EVERY call: plugin_data_dir() follows the active profile and must not
        # be cached across profile switches. Any failure becomes a reply, never
        # an exception that could take down the session.
        try:
            from plugins.plugin_storage import plugin_data_dir

            from .commands import handle_statefile
            from .registry import Registry

            registry = Registry(plugin_data_dir(PLUGIN_ID))
            return handle_statefile(raw_args, session_id=session_id,
                                    registry=registry)
        # A slash-command boundary is the one place a blind catch is correct:
        # any failure must become a chat reply, never a broken session.
        except Exception as exc:  # noqa: BLE001
            return f"/statefile: error: {type(exc).__name__}: {exc}"

    ctx.register_command(
        COMMAND_NAME,
        handler=_handler,
        description="Opt in to Mainplane-style append-only statefile recording for this session",
        args_hint="on|off|status",
    )
