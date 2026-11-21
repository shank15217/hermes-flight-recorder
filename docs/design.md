# Design (v0 — plan, not implementation)

> Status: **draft**. Hook signatures below come from the Hermes plugin docs
> (<https://hermes-agent.nousresearch.com/docs/developer-guide/plugins>) and must be re-verified
> against the running Hermes version at implementation time.
>
> Step 1 (record format / `Build()`) is implemented — see
> `src/hermes_flight_recorder/records.py` and `tests/test_records.py`. Everything from §4
> onward that touches Hermes is still a plan.

## 1. Goal

Give a Hermes user an **opt-in per-session** second representation of a conversation:

- `type: mainplane` tag on the session, stored in a per-profile registry
- an append-only `.state` file that is the source of truth for that tagged session
- deterministic rebuild of API context from the record log
- per-step prompt-cache accounting, and a `cold_limit` rollover that hands off to a fresh
  session by pointing at the previous log

Untagged sessions see **zero** plugin behaviour. Nothing is global, nothing is automatic by
default.

**Tagging is sticky.** Once a session is enabled, the tag and its `.state` file persist across
turns, Hermes restarts and plugin updates — for that session only — until `/statefile off`.
The registry and every `.state` file live under `plugin_data_dir()`, which follows the active
Hermes profile and survives `hermes plugins update`. No file is created for an untagged
session.

## 2. Non-goals

No mesh/workers/drives/provider proxy/web UI. No fork of Hermes core. No replacement of Hermes'
context engine (it is single-select and owns compression). No network calls. No model-facing
tools — the plugin's only model-visible surface is the slash command and a bundled skill.

## 3. Why this is a plugin, not a context engine

Hermes stores sessions in SQLite (FTS5, lineage tracking, atomic writes) and follows a
*prompt-stability* principle: the system prompt does not change mid-conversation. A context
engine plugin is single-select and would displace Hermes' compressor. We therefore **observe and
inject** instead of replacing:

- `ctx.register_hook(...)` — lifecycle observation into the `.state` file
- `ctx.register_command(name, handler, description)` — `/statefile ...`
- `pre_llm_call` **returns context injection**, which is the supported way to add retrieved
  context to a turn
- `ctx.inject_message(...)` / `ctx.dispatch_tool(...)` for advisory handoff messages
- `plugin_data_dir()` for durable, profile-scoped state that survives plugin updates

Plugins are opt-in via the allow-list, and project-local `./.hermes/plugins/` are not loaded by
the gateway — install as a user plugin (`~/.hermes/plugins/`) or a pip entry point. Hook errors
are caught and logged by Hermes; the plugin must never break a session.

## 4. Opt-in and the registry

`/statefile on|off|status|chain|handoff` is a registered slash command. Handlers run in the
gateway and may declare `session_id`, `session_key`, `platform`, `source` keywords; they must
**return** their text (not `print`). Use `agent.runtime_cwd.resolve_agent_cwd()` for paths, not
`os.getcwd()`.

Registry lives at `plugin_data_dir()/registry.json` (per HERMES_HOME / profile):

```json
{
  "version": 1,
  "sessions": {
    "<session_id>": {
      "type": "mainplane",
      "statefile": "/.../plugin-data/sessions/<session_id>.state",
      "agent_id": "<uuid>",
      "chain": ["<session_id>", "..."],
      "parent": null,
      "created_at": "2026-11-20T00:00:00Z",
      "context_limit": 250000,
      "cold_limit": 100000,
      "instructions": ""
    }
  }
}
```

`type: mainplane` is a local format tag (see `docs/prior-art.md` § Note on naming). A chain is the
ordered list of sessions belonging to one logical agent; `parent` links a handoff.

## 5. Record format

One record = one JSON header line, then a body of exactly `header["len"]` bytes (never escaped,
never re-encoded), then `\n`. File order is **time order**; API context is rebuilt from it.

Header fields (minimum): `n` (monotonic int), `kind`, `id`, `step`, `upto`, `len`, `ts`.
Kinds mirror the upstream vocabulary: `start, config, system, message, text, thinking, call,
step, result, error, name`. `start`, `config` and `name` are metadata and never enter context.

```json
{"n":12,"kind":"result","id":"r_9f2","step":"s_8","upto":11,"len":437,"ts":"..."}
```

Raw bodies are kept because tool output and vendor payloads have shapes we cannot predict; a
guessed parse loses information. Usage is recorded per `step`:

```json
{"input":1200,"output":340,"cache_read":88000,"cache_write":0,"vendor":{}}
```

`prompt = input + cache_read + cache_write`.

**Append order within a step.** Blocks (`text`, `thinking`, `call`) stream in *before* the
`step` record that closes them; results follow it. A step record's `upto` is the highest `n`
in the chain when that step's prompt was built (upstream: `hdr.Upto = upto`), so results
appended after the step are not attributed to it — they are seen by the *next* step. That is
why `Build()` defers records with `n > step.upto` rather than dropping them.

## 6. Context rebuild (`Build`)

Given the chain of records, produce API context order:

1. For each `step`, emit the `result` records that step saw, immediately after it.
2. Emit `message` records seen since the previous step as one user turn.
3. Emit `text` / `thinking` / `call` blocks belonging to that step.
4. Emit the `step` record itself.
5. Drop blocks no step closed (interrupted turns). Never emit `start`, `config`, `name`.

`upto` on a step records the highest `n` visible when that step was built, so `seen(r, s)` is
`r.n <= s.upto`. This is the deterministic part: the same file always yields the same prompt.

## 7. Hook mapping

| Hook | Payload (relevant fields) | Use |
| --- | --- | --- |
| `on_session_start` | `session_id, model, platform` | if tagged, append `start` + `config` records |
| `pre_llm_call` | `session_id, user_message, conversation_history, is_first_turn, model, platform` | append `message`; **return** handoff context injection on a fresh chain member |
| `post_llm_call` | `session_id, user_message, assistant_response, conversation_history, model, platform, side_question_token, review_signals` | append `text` / `thinking` blocks and the `step` record |
| `post_tool_call` | `tool_name, args, result, task_id, duration_ms` | append `call` + `result` records (raw result bytes) |
| `pre_api_request` | `session_id, model, provider, base_url, api_mode, api_call_count, message_count, tool_count, approx_input_tokens, max_tokens, request` | correlate a provider call with a step |
| `post_api_request` | above + `api_duration, finish_reason, response, usage` | append usage; **keep vendor usage raw** |
| `api_request_error` | correlation fields + `status_code, reason, error, request` | append `error` record |
| `post_compaction` | `session_id, old_session_id, kind, reason, before_tokens, after_tokens, messages_removed` | record a compaction boundary in the chain |
| `on_session_reset` | `session_id, platform` | detect `/new` / `/reset` as the handoff point |
| `on_session_delete` | `session_id, reason, profile, deleted_ids, sessions_dir` | archive, don't silently delete, the `.state` file |
| `on_session_end` | `session_id, completed, interrupted, model, platform` | flush / fsync |

Privacy: `post_tool_call` results can contain secrets. Open question (§10) — redaction policy
must be decided before v0.1 ships.

## 8. `cold_limit` rollover

Two triggers:

- **`context_limit`** — cumulative prompt tokens exceed the model's window (hard).
- **`cold_limit`** — the *last* step shows a cold cache (`cache_read` ≈ 0 while the session is
  past `cold_limit` tokens), so continuing pays full input price every step. Cheaper to start a
  fresh session and hand it the old log path than to keep re-warming.

`cold_limit` is **not** a guess: it is computed from recorded `usage` per step. Caveat —
OpenAI-compatible local servers (llama.cpp, vLLM, SGLang on hwrouter) may not report
`cache_read` / `cache_write` in the same shape as Anthropic or OpenAI. Step 4 of the roadmap is
an explicit measurement task on the real endpoints before the policy is trusted; if the fields
are absent, `cold_limit` must degrade to a no-op rather than guess.

## 9. Handoff

Mainplane's new session is told *where the old session's log is on its workers*. Our analogue:

- `/branch` (Hermes) creates a child session id, copies the full transcript, keeps the parent's
  exact system prompt and **keeps the warm prompt cache** — this is the cheap rollover, and the
  preferred mapping for `cold_limit`.
- `/new` + a handoff message is the `context_limit` path.
- `on_session_reset` / `post_compaction` detect when the user or Hermes has already rolled over.
- `pre_llm_call` on the new chain member injects a short note naming the prior `.state` path and
  how to read it, rather than copying the whole log into context.

**Honest limitation:** hooks have no in-process slash-command bridge; `ctx.dispatch_tool(
"terminal", {"command": "hermes ..."})` is the supported way to drive Hermes from a hook, and
that is awkward inside an interactive session. v0.1 handoff will therefore be **advisory** —
the plugin posts a note recommending `/branch` or `/new` and prepares the injection — with
automatic rollover deferred to v0.2 pending testing on real sessions.

## 10. Open questions

1. Does `post_api_request`'s `usage` carry cache fields on our local providers? (measure first)
2. How do we detect "cache is cold" from a single step without false positives on small prompts?
3. Redaction of tool results / secrets in `.state` files — opt-in masking, or user-managed?
4. Interaction between `pre_llm_call` injection and Hermes' prompt-stability principle.
5. Should the registry be JSON or SQLite? (Start JSON; move if chains get large.)
6. `instructions` hot-reload semantics (v0.2+) and the 4 KiB cap.
7. Exactly how `/branch` lineage appears in `post_compaction` / session ids on this build.

## 11. Storage & permissions

All state under `plugin_data_dir()` (follows the active Hermes profile). Files mode 0600,
directory 0700, fsync on flush. `.state` files and `registry.json` are gitignored. No egress.
Deleting a tagged session archives its `.state` rather than dropping it.
