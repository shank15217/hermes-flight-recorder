"""Step 3: hook wiring mirrors tagged sessions into their .state files."""

from __future__ import annotations

import sys
import types

from hermes_flight_recorder import Registry, build_context, read_records
from hermes_flight_recorder.plugin import HOOK_NAMES, register
from hermes_flight_recorder.recording import MAX_TEXT_BYTES

SECRET = "***"


class FakeCtx:
    def __init__(self, *, redact=True, redact_error=False):
        self.commands = {}
        self.hooks = {}
        self.tools = []
        self._redact_error = redact_error
        if redact:
            self.redact_text = self._redact

    def _redact(self, text, *, force=False):
        if self._redact_error:
            raise RuntimeError("scrubber unavailable")
        return text.replace(SECRET, "\u00abredacted\u00bb")

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))


def fake_storage(monkeypatch, tmp_path):
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_data_dir = lambda name: tmp_path / name
    pkg.plugin_storage = mod
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    return tmp_path / "hermes-flight-recorder"


def statefile_path(data_dir, session_id="s1"):
    return Registry(data_dir).get(session_id).statefile


def fire_turn(ctx, session_id="s1", *, user="hello", result="ok",
              assistant="done"):
    ctx.hooks["pre_llm_call"](
        session_id=session_id, user_message=user, conversation_history=[],
        is_first_turn=True, model="test-model", platform="local",
        task_id="task", turn_id="turn",
    )
    ctx.hooks["post_tool_call"](
        session_id=session_id, tool_name="terminal",
        args={"command": "echo hi"}, result=result, task_id="task",
        duration_ms=5,
    )
    ctx.hooks["post_llm_call"](
        session_id=session_id, user_message=user,
        assistant_response=assistant, conversation_history=[],
        model="test-model", platform="local",
    )


def test_register_wires_command_and_exactly_the_five_hooks(tmp_path, monkeypatch):
    fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    assert list(ctx.commands) == ["statefile"]
    assert set(ctx.hooks) == set(HOOK_NAMES)
    assert ctx.tools == []


def test_missing_redact_text_registers_no_hooks(tmp_path, monkeypatch):
    """F7 fail-closed: never record unredacted transcript text."""
    fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx(redact=False)
    register(ctx)
    assert list(ctx.commands) == ["statefile"]
    assert ctx.hooks == {}


def test_full_turn_mirrors_all_records(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    assert "enabled" in ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx)

    records = read_records(statefile_path(data_dir))
    kinds = [r.kind for r in records]
    assert kinds == ["start", "message", "call", "result", "text", "step"]
    step = records[-1]
    assert step.kind == "step"
    assert step.upto == 2, "upto includes start + this turn's user message"
    assert step.id == records[2].step == records[4].step
    assert records[3].extra["for"] == records[2].id
    assert all(r.extra.get("via") for r in records[1:])


def test_context_rebuild_after_a_turn(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx)
    rebuilt, upto = build_context(read_records(statefile_path(data_dir)))
    assert [r.kind for r in rebuilt] == [
        "message", "call", "text", "step", "result"]
    assert upto == 6


def test_untagged_sessions_are_untouched(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    fire_turn(ctx, session_id="untagged")  # never tagged
    assert not (data_dir / "registry.json").exists()
    assert not (data_dir / "sessions").exists()

    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx, session_id="s1")
    assert Registry(data_dir).tagged()[0].session_id == "s1"
    assert len(list((data_dir / "sessions").glob("*.state"))) == 1


def test_redaction_is_applied_to_every_stored_text(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx, user=f"key {SECRET} here", result=f"out {SECRET}",
              assistant=f"reply {SECRET}")
    bodies = b"".join(r.body for r in read_records(statefile_path(data_dir)))
    assert SECRET.encode() not in bodies
    assert "\u00abredacted\u00bb".encode() in bodies


def test_redaction_failure_stores_a_placeholder(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx(redact_error=True)
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx, user="sensitive payload")
    bodies = b"".join(r.body for r in read_records(statefile_path(data_dir)))
    assert b"sensitive payload" not in bodies
    assert b"[redaction unavailable]" in bodies


def test_hook_failure_is_isolated(tmp_path, monkeypatch):
    fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")

    def boom(_name):
        raise OSError("disk gone")

    monkeypatch.setattr(sys.modules["plugins.plugin_storage"],
                        "plugin_data_dir", boom)
    assert ctx.hooks["pre_llm_call"](
        session_id="s1", user_message="hello") is None


def test_system_markers_for_reset_and_compaction(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    ctx.hooks["on_session_reset"](session_id="s1", platform="local")
    ctx.hooks["post_compaction"](
        session_id="s1", old_session_id="s0", kind="auto", reason="limit",
        before_tokens=1000, after_tokens=400, messages_removed=12)
    records = read_records(statefile_path(data_dir))
    assert [r.kind for r in records] == ["start", "system", "system"]
    assert records[1].extra["via"] == "on_session_reset"
    assert records[2].extra["via"] == "post_compaction"


def test_two_turns_have_distinct_step_ids(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx)
    fire_turn(ctx, user="again", result="ok2", assistant="done2")
    records = read_records(statefile_path(data_dir))
    steps = [r for r in records if r.kind == "step"]
    assert len(steps) == 2
    assert steps[0].id != steps[1].id
    assert steps[0].upto < steps[1].upto


def test_oversized_tool_result_is_truncated(tmp_path, monkeypatch):
    data_dir = fake_storage(monkeypatch, tmp_path)
    ctx = FakeCtx()
    register(ctx)
    ctx.commands["statefile"][0]("on", session_id="s1")
    fire_turn(ctx, result="x" * (MAX_TEXT_BYTES + 500))
    results = [r for r in read_records(statefile_path(data_dir))
               if r.kind == "result"]
    assert len(results) == 1
    assert len(results[0].body) <= MAX_TEXT_BYTES + 100
    assert b"truncated by hermes-flight-recorder" in results[0].body
