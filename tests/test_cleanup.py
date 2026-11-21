"""Cleanup path: archive / purge / purge-all and the on_session_delete hook."""

from __future__ import annotations

import os
import sys
import types

from hermes_flight_recorder import (
    Registry,
    append_record,
    read_records,
)
from hermes_flight_recorder.commands import handle_statefile
from hermes_flight_recorder.plugin import HOOK_NAMES, RECORDING_HOOK_NAMES, register


class FakeCtx:
    def __init__(self, *, redact=True):
        self.commands = {}
        self.hooks = {}
        self.tools = []
        if redact:
            self.redact_text = lambda text, *, force=False: text

    def register_command(self, name, handler=None, description="", args_hint=""):
        self.commands[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))


def setup(tmp_path, monkeypatch, *, redact=True):
    home = tmp_path / "hermes-flight-recorder"
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_data_dir = lambda name: home
    pkg.plugin_storage = mod
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    ctx = FakeCtx(redact=redact)
    register(ctx)
    return home, ctx


def test_archive_keeps_statefile_but_drops_entry(tmp_path):
    r = Registry(tmp_path)
    entry = r.tag("s1")
    got = r.archive("s1")
    assert got.session_id == "s1"
    assert r.get("s1") is None
    assert r.tagged() == []
    assert os.path.isfile(entry.statefile), "statefile is archived, not deleted"


def test_archive_many_handles_a_bulk_delete(tmp_path):
    r = Registry(tmp_path)
    for i in range(5):
        r.tag(f"s{i}")
    entries = r.archive_many(["s1", "s3", "nonexistent"])
    assert sorted(e.session_id for e in entries) == ["s1", "s3"]
    assert sorted(e.session_id for e in r.tagged()) == ["s0", "s2", "s4"]


def test_purge_removes_entry_statefile_and_cached_handle(tmp_path):
    r = Registry(tmp_path)
    entry = r.tag("s1")
    append_record(entry.statefile, "message", b"x")
    result = r.purge("s1")
    assert result["found"] is True
    assert result["statefile_removed"] is True
    assert r.get("s1") is None
    assert not os.path.exists(entry.statefile)

    # The cached open handle must have been dropped: re-tagging writes a
    # fresh file starting at n=1, not into the unlinked inode.
    r.tag("s1")
    assert [rec.n for rec in read_records(entry.statefile)] == [1]


def test_purge_on_unknown_session_is_a_noop(tmp_path):
    r = Registry(tmp_path)
    assert r.purge("nope")["found"] is False


def test_purge_all_removes_every_file(tmp_path):
    r = Registry(tmp_path)
    for i in range(3):
        e = r.tag(f"s{i}")
        append_record(e.statefile, "message", b"x")
    result = r.purge_all()
    assert result == {"entries": 3, "statefiles": 3}
    assert not (tmp_path / "registry.json").exists()
    assert not (tmp_path / "registry.lock").exists()
    assert list((tmp_path / "sessions").glob("*.state")) == []
    assert r.tagged() == []
    # idempotent
    assert r.purge_all() == {"entries": 0, "statefiles": 0}


def test_command_purge(tmp_path, monkeypatch):
    home, ctx = setup(tmp_path, monkeypatch)
    ctx.commands["statefile"]("on", session_id="s1")
    out = handle_statefile("purge", session_id="s1", registry=Registry(home))
    assert "purged this session" in out
    assert Registry(home).get("s1") is None
    assert "nothing to purge" in handle_statefile(
        "purge", session_id="s1", registry=Registry(home))


def test_command_purge_all(tmp_path, monkeypatch):
    home, ctx = setup(tmp_path, monkeypatch)
    for i in range(2):
        ctx.commands["statefile"]("on", session_id=f"s{i}")
    out = handle_statefile("purge-all", session_id="s1", registry=Registry(home))
    assert "2 session(s)" in out and "2 statefile(s)" in out
    assert "UNINSTALL.md" in out
    assert not (home / "registry.json").exists()


def test_on_delete_archives_and_marks(tmp_path, monkeypatch):
    home, ctx = setup(tmp_path, monkeypatch)
    ctx.commands["statefile"]("on", session_id="s1")
    ctx.commands["statefile"]("on", session_id="s2")
    p1 = Registry(home).get("s1").statefile
    p2 = Registry(home).get("s2").statefile

    ctx.hooks["on_session_delete"](
        session_id="s1", reason="prune", profile="default",
        deleted_ids=["s1", "s2", "untagged"])

    assert Registry(home).tagged() == []
    assert os.path.isfile(p1) and os.path.isfile(p2)
    for path in (p1, p2):
        records = read_records(path)
        assert records[-1].kind == "system"
        assert records[-1].extra["via"] == "on_session_delete"
        assert b"prune" in records[-1].body


def test_single_delete_without_deleted_ids(tmp_path, monkeypatch):
    home, ctx = setup(tmp_path, monkeypatch)
    ctx.commands["statefile"]("on", session_id="s1")
    ctx.hooks["on_session_delete"](session_id="s1", reason="delete",
                                   profile="default")
    assert Registry(home).get("s1") is None


def test_cleanup_hook_registered_even_without_redaction(tmp_path, monkeypatch):
    _, ctx = setup(tmp_path, monkeypatch, redact=False)
    assert list(ctx.commands) == ["statefile"]
    assert set(ctx.hooks) == {"on_session_delete"}
    assert not any(name in ctx.hooks for name in RECORDING_HOOK_NAMES)


def test_full_hook_set_matches_manifest_names(tmp_path, monkeypatch):
    _, ctx = setup(tmp_path, monkeypatch)
    assert set(ctx.hooks) == set(HOOK_NAMES)
    assert ctx.tools == []
