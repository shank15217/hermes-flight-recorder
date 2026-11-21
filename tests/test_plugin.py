"""Step 2: the plugin surface itself.

A fake ctx checks register() wires exactly one command and no hooks/tools.
A fake `plugins.plugin_storage` module lets us exercise the real handler
end-to-end without touching the machine's HERMES_HOME.
"""

from __future__ import annotations

import sys
import types

from hermes_flight_recorder.plugin import register


class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.hooks = []
        self.tools = []
        self.skills = []

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_hook(self, *args, **kwargs):
        self.hooks.append((args, kwargs))

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))

    def register_skill(self, *args, **kwargs):
        self.skills.append((args, kwargs))


def _fake_storage(monkeypatch, tmp_path):
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_data_dir = lambda name: tmp_path / name
    pkg.plugin_storage = mod
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    return mod, tmp_path / "hermes-flight-recorder"


def test_register_registers_only_the_command():
    ctx = FakeCtx()
    register(ctx)
    assert list(ctx.commands) == ["statefile"]
    assert ctx.commands["statefile"][2] == "on|off|status"
    assert ctx.hooks == []
    assert ctx.tools == []
    assert ctx.skills == []


def test_registered_handler_roundtrips_on_off_status(tmp_path, monkeypatch):
    _, data_dir = _fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    handler = ctx.commands["statefile"][0]

    on = handler("on", session_id="sess-1", session_key="***",
                 platform="local", source=None)
    assert "enabled" in on
    assert (data_dir / "registry.json").is_file()
    assert "records: 1" in handler("status", session_id="sess-1")
    assert "disabled" in handler("off", session_id="sess-1")
    assert "not enabled" in handler("status", session_id="sess-1")


def test_handler_failure_becomes_reply_not_exception(tmp_path, monkeypatch):
    mod, _ = _fake_storage(monkeypatch, tmp_path)

    def boom(_name):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "plugin_data_dir", boom)
    ctx = FakeCtx()
    register(ctx)
    out = ctx.commands["statefile"][0]("on", session_id="s1")
    assert isinstance(out, str)
    assert "/statefile: error:" in out
    assert "disk full" in out
