"""F1/F2 fix: one open handle per statefile, O(1) appends, torn-tail recovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_flight_recorder.statefile as sfmod
from hermes_flight_recorder import (
    Record,
    RecordFormatError,
    append_record,
    get_statefile,
    read_records,
)
from hermes_flight_recorder.records import encode_record


def test_append_is_constant_time_with_one_scan(tmp_path):
    p = tmp_path / "s.state"
    n = 500
    for _ in range(n):
        append_record(p, "message", b"x")
    sf = get_statefile(p)
    assert sf.resync_count == 1, "every append must reuse the open handle"
    assert len(sf.read()) == n
    assert sf.last_n == n
    assert len(read_records(p)) == n  # disk agrees with the in-memory cache


def test_handle_is_cached_across_calls(tmp_path):
    p = tmp_path / "s.state"
    assert get_statefile(p) is get_statefile(p)
    append_record(p, "message", b"a")
    append_record(p, "message", b"b")
    assert get_statefile(p).resync_count == 1


def test_external_writer_forces_one_resync(tmp_path):
    p = tmp_path / "s.state"
    append_record(p, "message", b"a")
    sf = get_statefile(p)
    with p.open("ab") as f:
        f.write(encode_record(Record(n=2, kind="message", body=b"b")))
    rec = append_record(p, "message", b"c")
    assert rec.n == 3
    assert sf.resync_count == 2
    assert [r.body for r in read_records(p)] == [b"a", b"b", b"c"]


def test_external_truncation_is_resynced(tmp_path):
    p = tmp_path / "s.state"
    for _ in range(5):
        append_record(p, "message", b"x")
    sf = get_statefile(p)
    p.write_bytes(b"")
    rec = append_record(p, "message", b"y")
    assert rec.n == 1
    assert sf.resync_count == 2
    assert len(read_records(p)) == 1


def test_torn_final_record_is_recovered(tmp_path):
    p = tmp_path / "s.state"
    p.write_bytes(
        encode_record(Record(n=1, kind="message", body=b"ok"))
        + b'{"n":2,"kind":"message","len":10}\nabc'
    )
    with pytest.raises(RecordFormatError):
        read_records(p)  # strict contract unchanged
    assert len(read_records(p, tolerate_torn_tail=True)) == 1
    rec = append_record(p, "message", b"next")
    assert rec.n == 2  # torn tail was truncated on open
    assert len(read_records(p)) == 2


def test_concurrent_appends_are_serialized(tmp_path):
    p = tmp_path / "c.state"
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: append_record(p, "message", b"x"), range(400)))
    assert [r.n for r in read_records(p)] == list(range(1, 401))


def test_lru_eviction_closes_handles(tmp_path, monkeypatch):
    monkeypatch.setattr(sfmod, "MAX_OPEN_STATEFILES", 2)
    with sfmod._cache_lock:
        for old in sfmod._cache.values():
            old.close()
        sfmod._cache.clear()
    paths = [tmp_path / f"a{i}.state" for i in range(3)]
    first = get_statefile(paths[0])
    append_record(paths[0], "message", b"x")
    assert first._fh is not None
    append_record(paths[1], "message", b"x")
    append_record(paths[2], "message", b"x")  # evicts the oldest
    with sfmod._cache_lock:
        assert len(sfmod._cache) == 2
    assert first._fh is None, "an evicted handle must release its fd"
    scans = first.resync_count
    first.append("message", b"y")  # a held reference still works
    assert first.resync_count == scans, "reopening must not rescan"
    assert [r.n for r in read_records(paths[0])] == [1, 2]


def test_step_records_require_an_explicit_upto(tmp_path):
    p = tmp_path / "s.state"
    append_record(p, "message", b"a")
    with pytest.raises(ValueError, match="explicit upto"):
        append_record(p, "step", id="s1")
    r = append_record(p, "step", id="s1", upto=1)
    assert r.upto == 1
    assert read_records(p)[1].upto == 1
    assert read_records(p)[0].upto == 0  # non-step records carry no upto
