# Prior art: what we borrow from Mainplane

Repository: <https://github.com/mainplane-ai/mainplane> · MIT · authored by @Alezander9 and
contributors. Read upstream at the pinned tag/commit when implementing; the mapping below was
made against `main` at `de84d77` (2026-11-20).

This document exists so the credit is specific and durable rather than a one-line "inspired
by". **No code in this repository is copied from Mainplane.**

| Idea | Where Mainplane shows it | What we reimplement |
| --- | --- | --- |
| Session = append-only record file | `pkg/statefile/record.go` — "A record is one JSON header line, a body of exactly `len` bytes, and a newline. The body is never escaped." | `records.py` / `statefile.py`: same on-disk shape, original Python |
| Record kinds | `pkg/statefile/record.go`: `start, config, system, message, text, thinking, call, step, result, error, name` | same vocabulary, documented as an interchange format |
| Deterministic context rebuild | `pkg/statefile/context.go` — `Build()` reorders file (time) order into API (context) order: results directly after their step, messages merged into one user turn, blocks no step closed are dropped, `start`/`config`/`name` never enter context | `build.py` equivalent with tests |
| Keep the raw bytes | `AGENTS.md`: "When an upstream shape is uncertain, keep the bytes. Raw event in the error, raw block in the record. Guessed fields lose information." | every record keeps an unparsed body plus a typed header |
| Usage / prompt-cache accounting | `pkg/statefile/record.go` — `Usage{Input, Output, CacheRead, CacheWrite, Vendor json.RawMessage}`; `Prompt() = Input + CacheRead + CacheWrite` | per-step usage recorded from `post_api_request`, vendor payload kept raw |
| Agent ≠ session | `docs/self-hosting.md` "Agents and apps": "A session ends at its context limit. An agent does not: it is an id, a config and a list of apps over a chain of sessions." | registry entry with `agent_id` and `chain[]` |
| `context_limit` / `cold_limit` rollover | `docs/self-hosting.md`: a new session when config changed, at `context_limit`, or "its prompt cache is cold and it is past `cold_limit`" | rollover policy in the design doc |
| Handoff | `pkg/harness/agent.go` `handoff()` and the doc line "The new session is told where the old session's log is on its workers" | `pre_llm_call` injection pointing at the prior `.state` path |
| Hot-reloadable instructions | `docs/self-hosting.md`: agent `instructions` ≤4 KiB read by every session, change makes no new session | planned (v0.2+) |
| Tiny tool surface | `pkg/harness/tools.go`: `run, read, write, edit` (or `run, read, patch`); everything else is code the model writes | we keep Hermes' tools and add no model-facing tools at all |
| Skills as capability files | `docs/self-hosting.md` "Agents and apps": `apps/<name>/skill.md` + `app.json` | a bundled Hermes skill that explains `/statefile` |
| O(1) append via an open handle | `pkg/statefile/file.go` `File{f, n}` + `Append()`: "One writer per file"; `pkg/harness/session.go`: "A step holds the file open for its whole run, so a message posted mid-step appends to the same handle" | `statefile.py`: process-wide LRU of open `StateFile` handles caching `n` + byte offset; `append_record` is a single write, one `scan_records` on first open |
| Path derived from the session id | `pkg/statefile/session.go` `Sessions.Path(id)` — "The filesystem is the registry": `<dir>/<id>.state`, never a stored path | `registry.py`: `_statefile_path()` derives and containment-checks; a stored `statefile` value is ignored |
| Torn-tail recovery on open | `pkg/statefile/record.go` `ErrTorn` / `Decode()` ("EOF anywhere inside a record is ErrTorn"); `pkg/statefile/file.go` `scan()` returns `good` offset and `Open()` calls `f.Truncate(good)` | `records.py` `scan_records()` + `read_records(tolerate_torn_tail=True)`; `statefile.py` truncates a torn tail before appending |

## What we deliberately do NOT take

Mainplane's control plane, WireGuard mesh, CPace join, cloudflared tunnel + pointer, shared
NFS/SMB drives, provider proxying, apps/webhooks with per-session keys, projects and dashboards,
and the `claude-code` / `chatgpt` subscription shapes. Those are a product, not a pattern; Hermes
already has its own equivalents for most of them.

## Note on naming

The repository name `hermes-flight-recorder` is an aviation nod — a black box that records a
flight and can be handed to the next crew — chosen precisely so the upstream project's name is
not appropriated. The internal session tag `type: mainplane` (see design doc) is a local string
describing the *session format*, not a product name; it can be renamed without changing the
format. This project is not affiliated with or endorsed by Mainplane.
