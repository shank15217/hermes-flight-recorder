"""Step 2: /statefile command behaviour, without any Hermes runtime."""

from __future__ import annotations

import os

from hermes_flight_recorder import USAGE, Registry, handle_statefile, read_records


def reg(tmp_path):
    return Registry(tmp_path / "data")


def test_status_on_untagged_session(tmp_path):
    out = handle_statefile("status", session_id="s1", registry=reg(tmp_path))
    assert "not enabled" in out


def test_on_then_status(tmp_path):
    r = reg(tmp_path)
    out = handle_statefile("on", session_id="s1", registry=r)
    assert "enabled" in out
    assert "type: mainplane" in out
    assert "records: 1" in out
    assert r.is_tagged("s1")
    assert "records: 1" in handle_statefile("", session_id="s1", registry=r)


def test_on_twice_reports_already_enabled_without_duplicating(tmp_path):
    r = reg(tmp_path)
    handle_statefile("on", session_id="s1", registry=r)
    out = handle_statefile("on", session_id="s1", registry=r)
    assert "already enabled" in out
    assert len(read_records(r.get("s1").statefile)) == 1


def test_off_disables_but_retains_statefile(tmp_path):
    r = reg(tmp_path)
    handle_statefile("on", session_id="s1", registry=r)
    path = r.get("s1").statefile
    out = handle_statefile("off", session_id="s1", registry=r)
    assert "disabled" in out
    assert not r.is_tagged("s1")
    assert os.path.isfile(path)
    assert "not enabled" in handle_statefile("status", session_id="s1", registry=r)


def test_off_on_untagged_is_a_noop(tmp_path):
    r = reg(tmp_path)
    out = handle_statefile("off", session_id="s1", registry=r)
    assert "not enabled" in out
    assert not r.path.exists()


def test_off_on_resumes_the_same_chain(tmp_path):
    r = reg(tmp_path)
    handle_statefile("on", session_id="s1", registry=r)
    first = r.get("s1").statefile
    handle_statefile("off", session_id="s1", registry=r)
    handle_statefile("on", session_id="s1", registry=r)
    assert r.get("s1").statefile == first
    assert len(read_records(first)) == 1


def test_help_and_unknown_verb(tmp_path):
    r = reg(tmp_path)
    assert handle_statefile("help", session_id="s1", registry=r) == USAGE
    assert "Unknown /statefile argument" in handle_statefile("wat", session_id="s1", registry=r)


def test_no_session_id_fails_closed(tmp_path):
    r = reg(tmp_path)
    out = handle_statefile("on", session_id="", registry=r)
    assert "no session id" in out
    assert not r.path.exists()
    assert r.tagged() == []


def test_untagged_sessions_are_untouched(tmp_path):
    r = reg(tmp_path)
    handle_statefile("on", session_id="A", registry=r)
    assert r.is_tagged("A")
    assert not r.is_tagged("B")
    assert "not enabled" in handle_statefile("status", session_id="B", registry=r)
    assert all(e.session_id != "B" for e in r.tagged())


def test_corrupt_registry_returns_error_text(tmp_path):
    r = reg(tmp_path)
    r.tag("s1")
    r.path.write_text("{broken", encoding="utf-8")
    out = handle_statefile("status", session_id="s1", registry=r)
    assert out.startswith("/statefile: error:")
