# Design (v0 — plan, not implementation)

> Status: **draft**. Hook signatures below come from the Hermes plugin docs
> (<https://hermes-agent.nousresearch.com/docs/developer-guide/plugins>) and must be re-verified
> against the running Hermes version at implementation time.
>
> Steps 1-3 are implemented: record format / `Build()` (`records.py`, `statefile.py`), the
> sticky registry + `/statefile` command (`registry.py`, `commands.py`, `plugin.py`), and hook
> mirroring (`recording.py`). Step 4 (usage/cache accounting) onward is still a plan.

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
      "enabled": true,
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

**Implemented in step 2.** `Registry` (`src/hermes_flight_recorder/registry.py`) owns this file:
`tag()` (idempotent), `untag()` (sets `enabled: false` but keeps the entry and
the `.state` file), `get` / `is_tagged` / `tagged`. `is_tagged` and `tagged` count only
enabled entries; `get` still returns a disabled one so `/statefile status` can report it.
Re-enabling resumes the same `agent_id`, `statefile` and `chain`.
Writes are serialized with `flock` and published atomically (`registry.json.tmp` →
`os.replace`), 0600 file / 0700 dirs. A corrupt or wrong-version registry raises
`RegistryError` rather than being silently reset. `tag()` also creates the session's `.state`
file with one `start` record; a later `on` after `off` resumes the same file and chain.

`/statefile on|off|status` is registered with `ctx.register_command(name, handler, description,
args_hint)`. The handler resolves `plugin_data_dir("hermes-flight-recorder")` on **every call**
(it follows the active profile and must not be cached) and always returns text. With no
`session_id` it fails closed. **No hooks and no tools are registered at this step.**
The `statefile` field in `registry.json` is **display data only**: F2 of the code review showed a
tampered stored path could redirect transcript writes outside `sessions/`. `Registry._statefile_path()`
now derives the location from the session id and checks containment, and `_bind()` overwrites the
stored value on every read — Mainplane's "The filesystem is the registry" (`Sessions.Path(id)`).

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

**Append cost (F1).** The first implementation re-read the whole file on every append — O(n²),
worse than the compaction this project is meant to avoid. `statefile.py` now keeps one open
handle per statefile, cached process-wide in an LRU, with `n` and the byte offset in memory;
`append_record` is a single seek+write+fsync. Rebuilding context re-reads the file (as Mainplane's
`Sessions.Load` does), once per rebuild rather than once per append. A torn final record is dropped
and truncated on open, mirroring `ErrTorn` / `Open`; `read_records(tolerate_torn_tail=True)` offers
the same behaviour to direct callers, while the default stays strict.

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

**Implemented in step 3.** `recording.py` registers five observer hooks (all `**kwargs`,
so they receive the full additive payload) and appends Mainplane-shaped records for tagged
sessions only:

| Hook | Records |
| --- | --- |
| `pre_llm_call` | `message` (user turn), then captures `upto` from `build_context()` **after** the message is appended, and opens a turn with a fresh `step_id` |
| `post_tool_call` | `call` (tool name + args) and `result`, both tagged with that `step_id`; `result` carries `for: <call id>` |
| `post_llm_call` | `text` (assistant response) then the `step` record with the captured `upto` |
| `on_session_reset` | `system` marker |
| `post_compaction` | `system` marker with kind/reason/token counts |
| `on_session_delete` | `system` marker (reason/profile only), then **archives** the registry entry: the `.state` file is kept for audit, the entry is dropped so the registry cannot grow without bound (review W2) |

`pre_api_request` / `post_api_request` are deliberately **not** wired yet: usage and cache
accounting are step 4. A single `step` per Hermes turn (rather than per provider API request)
is a documented simplification.

**F7 — hard gate for step 3.** `post_tool_call` results and transcripts can contain secrets.
Hermes' plugin guide requires running saved text through `ctx.redact_text(text, force=True)`
before storing it; step 3 MUST do this at the point where a hook's payload becomes a record
body, before `append_record`. Two further facts from the live docs (verified against this
build) constrain the design:

- `post_api_request` / `post_llm_call` `request` and `response` kwargs are **sanitized,
  size-capped JSON views** (sensitive keys redacted, long strings truncated), and `usage` is a
  *plain token-summary dict* — not a raw provider payload. So "keep the raw bytes" records a
  redacted, truncated view, and cache fields are not guaranteed to be present.
- Auxiliary LLM calls (titling, compression, MoA, vision) fire `pre_auxiliary_call` /
  `post_auxiliary_call`, **not** `pre/post_api_request`. Step 4's accounting must not mistake
  them for session steps.

**F7 resolved in step 3.** `Recorder._text()` is the single redaction/serialization boundary:
every stored body passes through `ctx.redact_text(..., force=True)` before `append_record`, and
a redaction failure stores `[redaction unavailable]` rather than raw text. If
`ctx.redact_text` is unavailable at registration, **no hooks are registered at all** (fail
closed). Bodies are also capped at 256 KiB with a visible truncation marker. Verified
end-to-end through Hermes' real hook dispatch with an `sk-` shaped credential: the raw key
does not reach the `.state` file. The plugin records what the hooks actually deliver, which
is the sanitized/size-capped view described above -- not a raw provider payload.

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

**Deletion / uninstall.** `on_session_delete` archives: it appends one `system` record and drops
the registry entry but **keeps** the `.state` file, so session deletion does not make
`registry.json` grow. Explicit removal is `/statefile purge` (one session: entry + file +
cached handle) and `/statefile purge-all` (all entries, all statefiles, `registry.json` and
`registry.lock`). `hermes plugins remove` deletes only the install tree — Hermes deliberately
keeps `plugin-data/` across `remove`/`update`. The full procedure, per-profile paths and file
inspection commands are in [UNINSTALL.md](../UNINSTALL.md).

## 12. Review resolution (pre-step-3 gate)

A comprehensive review at `3922f4f` found the append path was O(n²) — 1.619 s / 4.052 s /
13.651 s for 400 / 800 / 1600 appends — which would cost more than the compaction the project
exists to avoid. Re-reading Mainplane's `pkg/statefile/file.go`, `pkg/harness/session.go` and
`pkg/statefile/session.go` showed the intended shape: one open handle + cached `n`, held for the run.

Resolved in this pass:

| Finding | Resolution |
| --- | --- |
| F1 O(n²) append | `statefile.py`: process-wide LRU of open handles caching `n` + byte offset; O(1) append. Same benchmark is now 0.661 s / 1.327 s / 2.925 s / 5.993 s for 400 / 800 / 1600 / 3200 appends (~1.7-1.9 ms each, flat) |
| F2 registry path traversal | `_statefile_path()` derives from the session id, containment-checked; stored `statefile` ignored on every read |
| F5 torn final write (came with Mainplane's `Open`) | `scan_records()` drops a torn tail; `statefile.py` truncates it on open; `read_records(tolerate_torn_tail=True)` for direct callers; default stays strict |

Regression tests live in `tests/test_statefile.py`, `tests/test_registry.py`,
`tests/test_records.py` and `tests/test_build_fidelity.py`. `build_context`
still matches a literal port of Mainplane's Go `Build()` on 3,000 randomized chains (0 mismatches),
and 8-thread concurrent appends still produce a clean 1..400 sequence.

### Second fix pass

| Finding | Resolution |
| --- | --- |
| F3 corrupt inputs leak raw exceptions | `_decode_one` wraps `Record.from_header`, `_entry_from` wraps `SessionEntry.from_dict`; bad headers/entries now raise `RecordFormatError` / `RegistryError` |
| F4 `bool("false")` re-enables a session | `enabled` must be a real `bool`; a string is rejected instead of silently enabling |
| F6 `upto` captured at the wrong time | `step` records now **require** an explicit `upto` (`build_context()`'s second return value, captured at prompt-build time); non-step records carry no `upto` at all |
| F7 secret-bearing / sanitized hook payloads | documented as a **hard gate for step 3** in §7: `ctx.redact_text(..., force=True)` at the serialization boundary, plus the facts that `request`/`response` are sanitized + size-capped and `usage` is a plain token-summary dict. No code can fix this before hooks exist. |
| F8 profile/host resolution | unit test now proves the handler calls `plugin_data_dir()` on **every** invocation (no caching across profile switches). Remaining gate: execute the handler inside a real plugin-host / named-profile session — still unverified, do not rely on it until tested. |
| mypy / PERF401 / CI | the two `fcntl = None` errors fixed; `_flush` uses `list.extend`; mypy and `ruff --select B,S,SIM,C4,PERF` added to CI |
| Test gaps | the differential `Build()` port and the corrupt-input, `enabled`, step-`upto` and concurrency probes are now committed tests |

### Step 3 (hook mirroring)

`recording.py` + five hooks in `plugin.yaml`. Opt-in enforced at the top of every hook:
`Registry(self._data_dir()).is_tagged(session_id)` -- untagged sessions create no files and
write no records. `data_dir()` resolves `plugin_data_dir()` on **every call** (F8, no caching
across profile switches). `step` records carry the `upto` captured at prompt-build time (F6).
Hook failures are logged and swallowed, so a plugin can never break a session.

Verified: `hermes plugins doctor --ci` reports **6 hooks / 0 tools**; `hermes plugins
validate` passes incl. security scan; a real `HERMES_HOME` with the plugin installed and
enabled fired `pre_llm_call` / `post_tool_call` / `post_llm_call` / `on_session_reset`
through Hermes' own `invoke_hook` and produced
`['start','message','call','result','text','step','system']` with a sane `build_context()`
rebuild and real redaction of an `sk-` key.

**Still open:**
- **F8's last mile** -- executing the hooks inside the live plugin-host child under
  `plugins.isolation: host` / multiplex profiles. `doctor` and `validate` confirm
  registration under host isolation, and in-process dispatch is fully verified, but an
  ad-hoc probe cannot launch the PM-managed host child. Needs a live gateway session.
- The design decision on whether `read_records` should default to tolerating a torn tail.
- Step 4's cache-field measurement (the endpoint to measure against must be supplied).

### Cleanup pass (review W2 + uninstall traceability)

`UNINSTALL.md` documents the two locations (install tree vs `<hermes home>/plugin-data/<name>/`,
including per-profile copies), how to inspect `registry.json` and `.state` files, and the
full `hermes plugins remove` + `rm -rf` procedure.

Added `/statefile purge` (one session) and `/statefile purge-all` (all data), backed by
`Registry.archive_many` / `archive` / `purge` / `purge_all`, and `forget_statefile()` so a
deleted file's cached open handle cannot keep writing to an unlinked inode.

`on_session_delete` closes **W2**: Hermes fires it with `session_id`, `reason`, `profile` and
optional `deleted_ids`, dispatched under the owning profile's home. The hook appends one
`system` marker (no transcript text, so it is registered even without `ctx.redact_text`) and
drops the registry entries in a single `archive_many` pass, keeping the `.state` files for
audit. Registry growth on session deletion is thereby bounded; `purge-all` clears the
archived files.
