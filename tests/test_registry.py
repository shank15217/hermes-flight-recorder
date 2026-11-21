"""Step 2: sticky per-session registry."""

from __future__ import annotations

import json
import os
import stat

import pytest

from hermes_flight_recorder import (
    TAGGED_SESSION_TYPE,
    Registry,
    RegistryError,
    SessionEntry,
    read_records,
)


def make_reg(tmp_path):
    return Registry(tmp_path / "plugin-data" / "hermes-flight-recorder")


def test_tag_creates_entry_and_start_record(tmp_path):
    r = make_reg(tmp_path)
    e = r.tag("sess-A")
    assert e.type == TAGGED_SESSION_TYPE
    assert e.chain == ("sess-A",)
    assert e.context_limit == 250000
    assert e.cold_limit == 100000
    assert e.agent_id
    assert os.path.isfile(e.statefile)
    recs = read_records(e.statefile)
    assert len(recs) == 1
    assert recs[0].kind == "start"
    assert recs[0].extra["via"] == "/statefile on"


def test_tag_is_idempotent(tmp_path):
    r = make_reg(tmp_path)
    a = r.tag("s1")
    b = r.tag("s1")
    assert a == b
    assert len(read_records(a.statefile)) == 1
    assert len(r.tagged()) == 1


def test_untag_keeps_file_and_off_on_resumes_same_chain(tmp_path):
    r = make_reg(tmp_path)
    e1 = r.tag("s1")
    assert r.untag("s1") is True
    assert r.is_tagged("s1") is False
    assert os.path.isfile(e1.statefile)
    e2 = r.tag("s1")
    assert e2.statefile == e1.statefile
    assert e2.agent_id == e1.agent_id
    assert e2.chain == ("s1",)
    assert len(read_records(e2.statefile)) == 1
    assert r.get("s1").enabled is True


def test_disabled_entry_is_retained_but_not_tagged(tmp_path):
    r = make_reg(tmp_path)
    e = r.tag("s1")
    assert r.untag("s1") is True
    assert r.get("s1").enabled is False
    assert r.get("s1").statefile == e.statefile
    assert r.tagged() == []
    assert r.untag("s1") is False  # already disabled


def test_untag_untagged_returns_false(tmp_path):
    assert make_reg(tmp_path).untag("nope") is False


def test_untagged_session_creates_nothing(tmp_path):
    r = make_reg(tmp_path)
    assert r.is_tagged("x") is False
    assert r.get("x") is None
    assert r.tagged() == []
    assert r.statefile("x") is None
    assert not r.path.exists()
    assert not r.sessions_dir.exists()


def test_tagged_lists_all_sessions(tmp_path):
    r = make_reg(tmp_path)
    r.tag("a")
    r.tag("b")
    assert {e.session_id for e in r.tagged()} == {"a", "b"}


def test_unsafe_session_id_cannot_escape_sessions_dir(tmp_path):
    r = make_reg(tmp_path)
    e = r.tag("../../etc/passwd:chat/1")
    assert os.path.commonpath([e.statefile, str(r.sessions_dir)]) == str(r.sessions_dir)
    assert os.path.isfile(e.statefile)
    assert r.get("../../etc/passwd:chat/1") is not None


@pytest.mark.parametrize("bad", ["", None, "x\x00y", "a" * 513])
def test_invalid_session_ids_rejected(tmp_path, bad):
    with pytest.raises(RegistryError):
        make_reg(tmp_path).tag(bad)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_registry_and_dirs_are_private(tmp_path):
    r = make_reg(tmp_path)
    r.tag("s1")
    assert stat.S_IMODE(r.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(r.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(r.sessions_dir.stat().st_mode) == 0o700


def test_atomic_save_leaves_no_tmp_file(tmp_path):
    r = make_reg(tmp_path)
    r.tag("s1")
    assert not (r.data_dir / "registry.json.tmp").exists()


def test_corrupt_registry_raises_instead_of_resetting(tmp_path):
    r = make_reg(tmp_path)
    r.tag("s1")
    r.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="unreadable"):
        r.get("s1")


def test_unsupported_version_raises(tmp_path):
    r = make_reg(tmp_path)
    r.tag("s1")
    r.path.write_text(json.dumps({"version": 99, "sessions": {}}), encoding="utf-8")
    with pytest.raises(RegistryError, match="unsupported registry version"):
        r.get("s1")


def test_entry_roundtrips_through_dict():
    e = SessionEntry(session_id="s", chain=("s", "s2"), parent="p",
                     instructions="be brief")
    assert SessionEntry.from_dict(e.to_dict()) == e
