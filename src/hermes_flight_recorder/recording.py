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

from .records import build_context, new_id
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
    registration; without it no hook is registered at all (F7, fail closed).
    """

    def __init__(self, data_dir, redact):
        self._data_dir = data_dir
        self._redact = redact
        self._lock = threading.RLock()
        self._turns: dict[str, dict] = {}

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
        if not session_id:
            return None
        registry = Registry(self._data_dir())
        if not registry.is_tagged(session_id):
            return None
        return registry.statefile(session_id)

    def _new_turn(self, sf, session_id: str) -> dict:
        _, upto = build_context(sf.read())
        turn = {"step_id": new_id(), "upto": upto}
        self._turns[session_id] = turn
        return turn

    def _turn(self, sf, session_id: str) -> dict:
        with self._lock:
            return self._turns.get(session_id) or self._new_turn(sf, session_id)

    @_guard
    def on_pre_llm(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
        if sf is None:
            return
        # The user message is part of THIS turn's prompt, so it must be in the
        # chain before we capture upto (otherwise the step "sees" nothing).
        sf.append("message", self._text(kwargs.get("user_message")),
                  via="pre_llm_call")
        with self._lock:
            self._turns[session_id] = self._new_turn(sf, session_id)
        return

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
    def on_reset(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
        if sf is None:
            return
        sf.append("system", self._text("session reset"),
                  via="on_session_reset")
        return

    @_guard
    def on_compaction(self, **kwargs):
        session_id = str(kwargs.get("session_id") or "")
        sf = self._sf(session_id)
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
