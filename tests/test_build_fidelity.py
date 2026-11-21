"""build_context must match Mainplane's Go Build() byte-for-byte in record order.

The reference below is a literal port of pkg/statefile/context.go. It is kept
in the test suite so a future refactor cannot silently drift from the upstream
algorithm we credit in NOTICE.
"""

from __future__ import annotations

import random

from hermes_flight_recorder import Record, build_context

KINDS = (
    "start", "config", "name", "system", "message",
    "text", "thinking", "call", "step", "result", "error",
)


def _mainplane_build(chain):
    ctx, pending, blocks = [], [], {}

    def flush(s):
        rest = []
        for p in pending:
            if s is not None and not (p.n <= s.upto):
                rest.append(p)
            elif p.kind == "result":
                ctx.append(p)
        for p in pending:
            if p.kind != "result" and (s is None or p.n <= s.upto):
                ctx.append(p)
        pending[:] = rest

    for r in chain:
        if r.kind in ("start", "config", "name"):
            continue
        if r.kind in ("text", "thinking", "call"):
            blocks.setdefault(r.step, []).append(r)
        elif r.kind == "step":
            flush(r)
            ctx.extend(blocks.pop(r.id, []))
            ctx.append(r)
        else:
            pending.append(r)
    flush(None)
    return ctx, (chain[-1].n if chain else 0)


def test_build_context_matches_mainplane_on_random_chains():
    rng = random.Random(20261121)
    for _ in range(1000):
        n = rng.randint(1, 30)
        chain = [
            Record(
                n=i,
                kind=rng.choice(KINDS),
                id=rng.choice(["s1", "s2", "s3", ""]),
                step=rng.choice(["s1", "s2", "s3", ""]),
                upto=rng.randint(0, n),
            )
            for i in range(1, n + 1)
        ]
        ours, our_upto = build_context(chain)
        ref, ref_upto = _mainplane_build(chain)
        assert [r.n for r in ours] == [r.n for r in ref]
        assert our_upto == ref_upto
