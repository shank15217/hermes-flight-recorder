"""Mirror one tagged session's turns into its append-only statefile.

Hermes owns context construction; this module only OBSERVES hook payloads and
appends records. Untagged sessions are a no-op -- no file is created, nothing
is read or written beyond the registry lookup.

The record shape follows Mainplane: a user ``message``, assistant ``call`` /
``text`` blocks tagged with a step id, a closing ``step`` record carrying
``upto``, and ``result`` records for tool output. ``upto`` is captured AFTER
the user message is appended but BEFORE any block/result of the turn, which is
exactly what Mainplane's ``step()`` gets from ``Build(chain)`` (F6).
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from pathlib import Path

from .records import new_id
from .registry import Registry
from .statefile import get_statefile

logger = logging.getLogger(__name__)

# Tool results and transcripts can be huge; cap what we persist. The marker
# makes truncation visible rather than silently corrupting a record.
MAX_TEXT_BYTES = 256 * 1024


def _guard(fn):
    """A hook must never break a session: log and return None on any failure."""

    @functools.wraps(fn)
    def wrapper(self, **kwargs):
        try:
            return fn(self, **kwargs)
        except Exception:
            logger.exception("hermes-flight-recorder hook %s failed", fn.__name__)
            return None

    return wrapper


class Recorder:
    """Turn state for all tagged sessions in this process.

    ``data_dir`` is a callable resolved on every use (never cached): it follows
    the active Hermes profile. ``redact`` is ``ctx.redact_text`` captured at
    registration; without it the recording hooks are not registered (F7).
    """

    def __init__(self, data_dir, redact):
        self._data_dir = data_dir
        self._redact = redact
        self._lock = threading.RLock()
        self._turns: dict[str, dict] = {}
        # One-shot advisory for the first turn after a tagged session is reset.
        self._pending_handoff: dict | None = None
        self._handoff_target: str | None = None

    def _text(self, value) -> bytes:
        if isinstance(value, str):
            s = value
        elif value is None:
            s = ""
        else:
            try:
                s = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                s = str(value)
        if self._redact is None:
            return b"[redaction unavailable]"
        try:
            s = self._redact(s, force=True)
        except Exception:
            logger.exception("redaction failed; storing a placeholder")
            return b"[redaction unavailable]"
        body = s.encode("utf-8", errors="replace")
        if len(body) > MAX_TEXT_BYTES:
            body = body[:MAX_TEXT_BYTES] + b"\n[truncated by hermes-flight-recorder]"
        return body

    def _sf(self, session_id: str):
        """One registry read (W5), then the cached open handle."""
        if not session_id:
            return None
        registry = Registry(self._data_dir())
        entry = registry.get(session_id)
        if entry is None or not entry.enabled:
            return None
        return get_statefile(entry.statefile)

    def _sf_for(self, *ids):
        """First tagged statefile among several candidate session ids (W3)."""
        for sid in ids:
            sid = str(sid or "")
            if not sid:
                continue
            sf = self._sf(sid)
            if sf is not None:
                return sf
        return None

    def _new_turn(self, sf, session_id: str) -> dict:
        # build_context() returns (ctx, chain[-1].n); we only need the index.
        # sf.last_n IS that value, straight from the cached handle -- no full
        # read+rebuild per turn (review C1).
        turn = {"step_id": new_id(), "upto": sf.last_n}
        self._turns[session_id] = turn
        return turn

    def _turn(self, sf, session_id: str) -> dict:
        with self._lock:
            return self._turns.get(session_id) or self._new_turn(sf, session_id)

    def _close_turn(self, sf, session_id: str, turn: dict, *, reason: str) -> None:
        """Close an unfinished turn so Build() does not drop its blocks (W1).

        Mirrors Mainplane's interrupted-step path: an error record plus a
        closing step record. Without this, an interrupted /stop leaves orphan
        ``result`` records whose ``call`` blocks were discarded.
        """
        sf.append("error", self._text(f"turn incomplete: {reason}"),
                  step=turn["step_id"], via="statefile")
        sf.append("step", b"", id=turn["step_id"], upto=turn["upto"],
                  via="statefile")
        with self._lock:
            self._turns.pop(session_id, None)

    def _take_handoff(self, session_id: str):
        """Return a one-shot advisory for the first turn after a tagged reset (W4)."""
        if not session_id or session_id != self._handoff_target:
            return None
        handoff = self._pending_handoff
        self._handoff_target = None
        self._pending_handoff = None
        if not handoff:
            return None
        return (
            "hermes-flight-recorder: the previous session "
            f"({handoff['old_session_id']}) was recorded to "
            f"{handoff['statefile']}. Recording is NOT enabled for this new "
            "session; run /statefile on to continue, or /statefile status to check."
        )

    @_guard
    def on_pre_llm(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        advisory = self._take_handoff(session_id)
        sf = self._sf(session_id)
        if sf is None:
            return advisory
        # A pre_llm_call without a prior post_llm_call means the last turn was
        # interrupted/retried. Close it before the new message so its blocks
        # are not orphaned (W1).
        with self._lock:
            stale = self._turns.get(session_id)
        if stale is not None:
            self._close_turn(sf, session_id, stale,
                             reason="new turn began before the prior turn closed")
        # The user message is part of THIS turn's prompt, so it must be in the
        # chain before we capture upto (otherwise the step "sees" nothing).
        sf.append("message", self._text(kwargs.get("user_message")),
                  via="pre_llm_call")
        with self._lock:
            self._turns[session_id] = self._new_turn(sf, session_id)
        return advisory

    @_guard
    def on_post_tool(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
        if sf is None:
            return
        turn = self._turn(sf, session_id)
        call = sf.append(
            "call",
            self._text({"name": kwargs.get("tool_name") or "",
                        "arguments": kwargs.get("args") or {}}),
            step=turn["step_id"], via="post_tool_call",
        )
        sf.append("result", self._text(kwargs.get("result")),
                  step=turn["step_id"], via="post_tool_call",
                  **{"for": call.id})
        return

    @_guard
    def on_post_llm(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
        if sf is None:
            return
        with self._lock:
            turn = self._turns.pop(session_id, None) or self._new_turn(sf, session_id)
        sf.append("text", self._text(kwargs.get("assistant_response")),
                  step=turn["step_id"], via="post_llm_call")
        sf.append("step", b"", id=turn["step_id"], upto=turn["upto"],
                  via="post_llm_call")
        return

    @_guard
    def on_session_end(self, **kwargs):
        """Close an unfinished turn when a turn ends interrupted or failed (W1)."""
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        with self._lock:
            turn = self._turns.get(session_id)
        if turn is None:  # normal turns were already closed by on_post_llm
            return
        sf = self._sf(session_id)
        if sf is None:
            return
        reason = ("interrupted" if kwargs.get("interrupted") else
                  "failed" if kwargs.get("failed") else "session end")
        self._close_turn(sf, session_id, turn, reason=reason)
        return

    @_guard
    def on_session_open(self, **kwargs):
        """Bind a pending reset handoff to the newly opened session (W4)."""
        session_id = str(kwargs.get("session_id") or "")
        with self._lock:
            pending = self._pending_handoff
            if (pending and session_id and
                    session_id != pending["old_session_id"] and
                    self._handoff_target is None):
                self._handoff_target = session_id

    @_guard
    def on_reset(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
        if sf is None:
            return
        sf.append("system", self._text(
            "session reset; recording stops here until /statefile on is run "
            "in the new session"), via="on_session_reset")
        with self._lock:
            self._pending_handoff = {"old_session_id": session_id,
                                     "statefile": str(sf.path)}
        return

    @_guard
    def on_compaction(self, **kwargs):
        # Compaction may report the NEW (untagged) session id; fall back to the
        # old one so the boundary is not silently lost (W3).
        sf = self._sf_for(kwargs.get("session_id"), kwargs.get("old_session_id"))
        if sf is None:
            return
        note = {k: kwargs.get(k) for k in (
            "kind", "reason", "before_tokens", "after_tokens",
            "messages_removed", "old_session_id")}
        sf.append("system", self._text(note), via="post_compaction")
        return

    @_guard
    def on_delete(self, **kwargs):
        """Archive tagged sessions Hermes deleted (or hid).

        Keeps each .state file for audit but drops its registry entry, so the
        registry cannot grow without bound (review W2). Bulk deletes carry
        ``deleted_ids``; a single delete carries only ``session_id``. This hook
        stores no transcript text, only Hermes-provided enum fields.
        """
        ids = kwargs.get("deleted_ids")
        if not isinstance(ids, (list, tuple)) or not ids:
            primary = str(kwargs.get("session_id") or "")
            ids = [primary] if primary else []
        if not ids:
            return
        registry = Registry(self._data_dir())
        note = json.dumps({"reason": kwargs.get("reason"),
                           "profile": kwargs.get("profile")},
                          sort_keys=True, ensure_ascii=False).encode()
        for entry in registry.archive_many(ids):
            path = Path(entry.statefile)
            if path.exists():
                get_statefile(path).append("system", note,
                                           via="on_session_delete")
        return
