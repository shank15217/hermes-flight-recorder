"""Real tests for step 1: record format, I/O and context rebuild.

The chain-building tests construct Records directly so ``n``, ``upto``, ``id``
and ``step`` are exact; file tests exercise the actual bytes on disk.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from hermes_flight_recorder import (
    Record,
    RecordFormatError,
    StateFile,
    append_record,
    build_context,
    read_records,
)


def rec(n, kind, *, id="", step="", upto=0, body=b"", **extra):
    return Record(n=n, kind=kind, body=body, id=id, step=step,
                  upto=upto, extra=extra)


# --- on-disk format -------------------------------------------------------

def test_append_assigns_monotonic_n_and_step_upto_is_explicit(tmp_path):
    p = tmp_path / "s.state"
    r1 = append_record(p, "message", "hello")
    r2 = append_record(p, "text", b"assistant", step=r1.id)
    with pytest.raises(ValueError, match="explicit upto"):
        append_record(p, "step", id="s1")
    r3 = append_record(p, "step", id="s1", upto=2)
    assert (r1.n, r2.n, r3.n) == (1, 2, 3)
    assert (r1.upto, r2.upto, r3.upto) == (0, 0, 2)
    assert [r.kind for r in read_records(p)] == ["message", "text", "step"]
    assert read_records(p)[2].id == "s1"


def test_roundtrip_raw_body_with_newlines_nul_and_invalid_utf8(tmp_path):
    p = tmp_path / "s.state"
    body = b"line one\n\x00\xff\xfe not utf8\nlast line\n"
    append_record(p, "result", body, step="s1")
    append_record(p, "message", "after")
    got = read_records(p)
    assert len(got) == 2
    assert got[0].body == body
    assert got[1].body == b"after"


def test_unknown_header_fields_are_preserved(tmp_path):
    p = tmp_path / "s.state"
    append_record(p, "message", "hi", via="cli", vendor={"cache_read": 88000})
    got = read_records(p)[0]
    assert got.extra == {"via": "cli", "vendor": {"cache_read": 88000}}
    assert got.to_header()["vendor"] == {"cache_read": 88000}
    # and they survive another append (the file is re-read and re-encoded)
    append_record(p, "message", "bye")
    header_line = p.read_bytes().split(b"\n", 1)[0]
    assert json.loads(header_line)["vendor"] == {"cache_read": 88000}


def test_missing_file_reads_as_empty_chain(tmp_path):
    assert read_records(tmp_path / "nope.state") == []


def test_truncated_body_is_rejected(tmp_path):
    p = tmp_path / "bad.state"
    p.write_bytes(b'{"n":1,"kind":"result","len":10}\nabc')
    with pytest.raises(RecordFormatError, match="truncated body"):
        read_records(p)


def test_missing_body_terminator_is_rejected(tmp_path):
    p = tmp_path / "bad.state"
    p.write_bytes(b'{"n":1,"kind":"result","len":3}\nabc')
    with pytest.raises(RecordFormatError, match="missing body terminator"):
        read_records(p)


def test_truncated_header_is_rejected(tmp_path):
    p = tmp_path / "bad.state"
    p.write_bytes(b'{"n":1,"kind":"result"')
    with pytest.raises(RecordFormatError, match="truncated header"):
        read_records(p)


def test_non_monotonic_n_is_rejected(tmp_path):
    p = tmp_path / "bad.state"
    p.write_bytes(
        b'{"n":1,"kind":"message","len":1}\na\n'
        b'{"n":1,"kind":"message","len":1}\nb\n'
    )
    with pytest.raises(RecordFormatError, match="non-monotonic"):
        read_records(p)


def test_unknown_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown record kind"):
        append_record(tmp_path / "s.state", "wat")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_statefile_is_private_0600(tmp_path):
    p = tmp_path / "s.state"
    append_record(p, "message", "secret")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# --- context rebuild ------------------------------------------------------

def test_empty_chain_returns_zero_upto():
    assert build_context([]) == ([], 0)


def test_metadata_kinds_never_enter_context():
    chain = [
        rec(1, "start"), rec(2, "config"), rec(3, "name"),
        rec(4, "message", body=b"hi"),
        rec(5, "step", id="s1", upto=4),
    ]
    ctx, upto = build_context(chain)
    assert upto == 5
    assert [r.kind for r in ctx] == ["message", "step"]


def test_results_land_after_their_step_and_before_next_assistant_turn():
    # blocks stream in BEFORE their step record; results follow it.
    chain = [
        rec(1, "message", body=b"question"),
        rec(2, "text", body=b"thinking out loud", step="s1"),
        rec(3, "call", body=b'{"name":"run"}', step="s1"),
        rec(4, "step", id="s1", upto=1),
        rec(5, "result", body=b"done", step="s1"),
        rec(6, "text", body=b"answer", step="s2"),
        rec(7, "step", id="s2", upto=5),
    ]
    ctx, upto = build_context(chain)
    assert upto == 7
    assert [r.n for r in ctx] == [1, 2, 3, 4, 5, 6, 7]


def test_upto_defers_records_the_step_did_not_see():
    chain = [
        rec(1, "message", body=b"m"),
        rec(2, "result", body=b"late"),
        rec(3, "step", id="s1", upto=1),   # did not see n=2
        rec(4, "step", id="s2", upto=3),   # did see n=2
    ]
    ctx, _ = build_context(chain)
    assert [r.n for r in ctx] == [1, 3, 2, 4]


def test_unclosed_blocks_are_dropped():
    chain = [
        rec(1, "message", body=b"m"),
        rec(2, "text", body=b"interrupted", step="never"),
        rec(3, "step", id="s1", upto=1),
    ]
    ctx, _ = build_context(chain)
    assert [r.n for r in ctx] == [1, 3]


def test_trailing_pending_records_are_flushed_results_first():
    chain = [
        rec(1, "message", body=b"m"),
        rec(2, "step", id="s1", upto=1),
        rec(3, "message", body=b"later"),
        rec(4, "result", body=b"r"),
    ]
    ctx, upto = build_context(chain)
    assert upto == 4
    assert [r.n for r in ctx] == [1, 2, 4, 3]  # result before message


def test_build_is_deterministic():
    chain = [
        rec(1, "message", body=b"m"),
        rec(2, "text", body=b"t", step="s1"),
        rec(3, "step", id="s1", upto=1),
        rec(4, "result", body=b"r"),
    ]
    assert build_context(chain) == build_context(chain)


def test_statefile_end_to_end_context(tmp_path):
    sf = StateFile(tmp_path / "s.state")
    sf.append("start", "session begun")
    sf.append("config", b'{"model":"x"}')
    sf.append("message", "what is 2+2?")
    sf.append("text", "four", step="s1")
    sf.append("step", id="s1", upto=4)
    sf.append("result", "four", step="s1")
    ctx, upto = sf.context()
    assert upto == 6
    assert [r.kind for r in ctx] == ["message", "text", "step", "result"]
    assert len(sf) == 6
    assert sf.last_n == 6


def test_corrupt_header_shapes_raise_recordformaterror(tmp_path):
    p = tmp_path / "bad.state"
    for raw in (
        b'{"n":1,"kind":"wat","len":1}\na\n',
        b'{"n":"abc","kind":"message","len":1}\na\n',
        b'{"kind":"message","len":1}\na\n',
        b'{"n":1,"kind":"message","len":true}\na\n',
    ):
        p.write_bytes(raw)
        with pytest.raises(RecordFormatError):
            read_records(p)
