"""Regression tests for the third review pass: C1, W1, W3, W4, W5."""

from __future__ import annotations

import itertools
import json
import os
import sys
import types

from hermes_flight_recorder import (
    Registry,
    StateFile,
    build_context,
    read_records,
)
from hermes_flight_recorder.plugin import HOOK_NAMES, register


class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.hooks = {}
        self.redact_text = lambda text, *, force=False: text

    def register_command(self, name, handler=None, description="", args_hint=""):
        self.commands[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *args, **kwargs):
        pass


def setup(tmp_path, monkeypatch):
    home = tmp_path / "hermes-flight-recorder"
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_data_dir = lambda name: home
    pkg.plugin_storage = mod
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    ctx = FakeCtx()
    register(ctx)
    return home, ctx


def pre(c, sid="s1", msg="hello"):
    return c.hooks["pre_llm_call"](
        session_id=sid, user_message=msg, conversation_history=[],
        is_first_turn=True, model="m", platform="local")


def tool(c, sid="s1"):
    c.hooks["post_tool_call"](
        session_id=sid, tool_name="t", args={"x": 1}, result="r",
        task_id="t", duration_ms=1)


def post(c, sid="s1"):
    c.hooks["post_llm_call"](
        session_id=sid, user_message="hello", assistant_response="world",
        conversation_history=[], model="m", platform="local")


# --- C1: no full-history read/rebuild per turn ----------------------------

def test_c1_no_statefile_read_during_a_turn(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")

    def forbidden(self):
        raise AssertionError("StateFile.read() must not run per turn")

    monkeypatch.setattr(StateFile, "read", forbidden)
    pre(c)
    tool(c)
    post(c)
    records = read_records(Registry(home).get("s1").statefile)
    assert [r.kind for r in records] == [
        "start", "message", "call", "result", "text", "step"]
    assert records[-1].upto == 2, "step upto equals the chain tip"


def test_c1_upto_equals_last_n(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    sf = Registry(home).statefile("s1")
    for i in range(3):
        pre(c, msg=f"u{i}")
        post(c)
    records = read_records(sf.path)
    steps = [r for r in records if r.kind == "step"]
    messages = [r for r in records if r.kind == "message"]
    assert len(steps) == 3
    assert [s.upto for s in steps] == [m.n for m in messages]
    assert all(a.upto < b.upto for a, b in itertools.pairwise(steps))


# --- W1: interrupted turns are closed, no orphan results -----------------

def test_w1_interrupted_turn_is_closed_by_on_session_end(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    pre(c)
    tool(c)  # no post_llm_call -> turn left open
    c.hooks["on_session_end"](session_id="s1", completed=False, failed=False,
                              interrupted=True, turn_exit_reason="stop")
    records = read_records(Registry(home).get("s1").statefile)
    kinds = [r.kind for r in records]
    assert kinds == ["start", "message", "call", "result", "error", "step"]
    ctx, _ = build_context(records)
    assert "call" in [r.kind for r in ctx], "call block must not be dropped"
    assert "error" in [r.kind for r in ctx]


def test_w1_retried_pre_llm_closes_the_stale_turn(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    pre(c, msg="first")
    tool(c)
    pre(c, msg="retry")  # second pre_llm_call without a post_llm_call
    post(c)
    records = read_records(Registry(home).get("s1").statefile)
    steps = [r for r in records if r.kind == "step"]
    assert len(steps) == 2, "the stale turn was closed, the retry got its own"
    assert steps[0].id != steps[1].id


def test_w1_normal_turn_closed_once_by_post_llm(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    pre(c)
    post(c)
    c.hooks["on_session_end"](session_id="s1", completed=True, failed=False,
                              interrupted=False)
    records = read_records(Registry(home).get("s1").statefile)
    assert [r.kind for r in records] == ["start", "message", "text", "step"]


# --- W3: compaction marker falls back to old_session_id -------------------

def test_w3_compaction_marker_uses_old_session_id(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    path = Registry(home).get("s1").statefile
    before = len(read_records(path))
    c.hooks["post_compaction"](
        session_id="NEW-untagged", old_session_id="s1", kind="auto",
        reason="limit", before_tokens=100, after_tokens=50, messages_removed=3)
    after = read_records(path)
    assert len(after) == before + 1
    assert after[-1].kind == "system"
    assert after[-1].extra["via"] == "post_compaction"


# --- W4: advisory handoff after a reset -----------------------------------

def test_w4_advisory_handoff_is_one_shot_and_opt_in(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    c.hooks["on_session_reset"](session_id="s1")
    c.hooks["on_session_open"](session_id="s2", platform="local",
                               reason="create")

    first = c.hooks["pre_llm_call"](
        session_id="s2", user_message="hi", conversation_history=[],
        is_first_turn=True, model="m", platform="local")
    assert first is not None
    assert "Recording is NOT enabled" in first
    assert "/statefile on" in first

    second = c.hooks["pre_llm_call"](
        session_id="s2", user_message="again", conversation_history=[],
        is_first_turn=False, model="m", platform="local")
    assert second is None
    assert Registry(home).get("s2") is None, "handoff must not auto-tag"


def test_w4_reset_marker_records_the_stop(tmp_path, monkeypatch):
    home, c = setup(tmp_path, monkeypatch)
    c.commands["statefile"]("on", session_id="s1")
    c.hooks["on_session_reset"](session_id="s1")
    records = read_records(Registry(home).get("s1").statefile)
    assert records[-1].kind == "system"
    assert b"recording stops" in records[-1].body


# --- W5: registry read cache ---------------------------------------------

def test_w5_reads_do_not_chmod_directories(tmp_path, monkeypatch):
    home = tmp_path / "reg"
    Registry(home).tag("s1")
    calls = []
    original = Registry._ensure_dirs

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Registry, "_ensure_dirs", counting)
    Registry(home).get("s1")
    Registry(home).is_tagged("s1")
    Registry(home).tagged()
    assert calls == [], "read paths must not mkdir/chmod"


def test_w5_cache_invalidated_by_external_write(tmp_path):
    home = tmp_path / "reg"
    r = Registry(home)
    r.tag("s1")
    assert r.get("s1") is not None  # populated the cache
    data = json.loads(r.path.read_text())
    data["sessions"] = {}
    tmp = r.path.with_name("registry.json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, r.path)  # new inode + mtime, as _save does
    assert Registry(home).get("s1") is None, "external write must invalidate"


def test_w5_cache_shared_across_instances_and_cleared_by_purge(tmp_path):
    home = tmp_path / "reg"
    Registry(home).tag("s1")
    assert Registry(home).is_tagged("s1")
    Registry(home).purge_all()
    assert Registry(home).get("s1") is None
    assert Registry(home).tagged() == []


def test_all_hooks_registered(tmp_path, monkeypatch):
    _, c = setup(tmp_path, monkeypatch)
    assert set(c.hooks) == set(HOOK_NAMES)
    assert len(HOOK_NAMES) == 8
