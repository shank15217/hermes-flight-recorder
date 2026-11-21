# hermes-flight-recorder

> **Status: step 2 of 7, review fixes landed.** The record format, deterministic context
> rebuild, a sticky per-session registry and the `/statefile on|off|status` command are
> implemented and tested. Append is O(1) via one cached open handle per statefile, and statefile
> paths are derived from the session id (both from Mainplane's `statefile` design). Both review
> fix passes are in: domain errors, strict `enabled` parsing, explicit step `upto`, mypy + extended
> ruff CI gates. **No hooks are registered yet** — a tagged session does not yet mirror its turns
> into the `.state` file (that is step 3). Untagged sessions are untouched. See
> [docs/design.md](docs/design.md) for the plan and §12 for the review resolution.

Opt-in, [Mainplane](https://github.com/mainplane-ai/mainplane)-style **append-only statefile
sessions** for [Hermes Agent](https://hermes-agent.nousresearch.com/).

A Hermes session normally lives in Hermes' SQLite store and is rebuilt into a prompt by the
core. *hermes-flight-recorder* adds a second, opt-in representation for sessions you explicitly
tag: an append-only `.state` file of header+raw-body records, a deterministic context rebuild,
per-step prompt-cache accounting, and a **cache-aware handoff** to a fresh session when the
current one gets expensive. Think of it as a flight recorder: a black box you can replay, hand
to the next crew, and audit — not a replacement for the aircraft.

The motivation is the same one that makes persistent KV cache worth having on local models:
don't pay to re-warm what you can retrieve. Mainplane's `cold_limit` rollover is a scheduling
policy built on that idea; bringing it to Hermes is the point of this project.

## Prior art and credit

**This project exists because of Mainplane.** The design is an independent reimplementation of
ideas first published in [mainplane-ai/mainplane](https://github.com/mainplane-ai/mainplane)
(MIT), authored by **@Alezander9** and the Mainplane contributors. In particular:

- append-only `.state` record files as the single source of truth for a session
- deterministic context rebuild from time-ordered records (`statefile.Build`)
- **agents as durable identities over a chain of sessions**, not one context window
- `cold_limit`: roll over when the prompt cache is cold and the session is past a token budget
- **handoff**: the new session is told where the old session's log lives, and retrieves what it
  needs rather than inheriting everything
- a deliberately tiny tool/capability surface, with skills as the extension point

See [NOTICE](NOTICE) and [docs/prior-art.md](docs/prior-art.md) for the full attribution.
No Mainplane source code is copied into this repository; the implementation will be original
Python against Hermes' documented plugin API. Both projects are MIT-licensed. This project is
not affiliated with, endorsed by, or a fork of Mainplane.

## How it will work (planned)

1. `/statefile on` tags the **current session** as `type: mainplane` in a per-profile registry.
   `/statefile off`, `/statefile status`, `/statefile chain` and `/statefile handoff` follow.
   Untagged sessions are untouched — the plugin is inert until you ask for it.
2. Plugin hooks mirror tagged sessions into an append-only `.state` file under the plugin's
   data directory: messages, assistant text/thinking, tool calls and results, per-step usage.
3. `pre_llm_call` context injection is the hook that pulls a prior session's log into a fresh
   one — the handoff.
4. Rollover maps onto Hermes' `/branch` (which keeps the warm prompt cache and the exact system
   prompt) and `/new`, with `cold_limit` computed from real `cache_read` / `cache_write` numbers.

The full hook mapping, record format, registry schema, rollover math, constraints and open
questions are in **[docs/design.md](docs/design.md)**.

## Usage (step 2)

Install as a **user plugin** (`~/.hermes/plugins/hermes-flight-recorder/`) or via a pip entry
point, enable it in `plugins.enabled`, then in any session:

```
/statefile on      # enable Mainplane-style recording for THIS session only
/statefile status  # show type, statefile path, record count, chain, limits
/statefile off     # disable; the entry and .state file are kept, re-enabling resumes it
```

The tag and its `.state` file live under `plugin_data_dir()` (per Hermes profile) and persist
across turns, restarts and plugin updates. Nothing is written for sessions you never enabled.
Hooks are not wired yet, so recording is currently limited to the `start` record created when
you opt in.

## Non-goals

No WireGuard mesh, no remote workers, no shared NFS/SMB drives, no provider proxy, no web UI,
no fork of Hermes core, no replacement of Hermes' context engine. Those are Mainplane's job and
they are excellent; this project borrows one pattern, not the product.

## Roadmap

| Step | Deliverable | Status |
| --- | --- | --- |
| 0 | Repo, prior-art credit, license, design doc, package + test skeleton | **done (this repo)** |
| 1 | Record format + `Build()` reorder, with tests | **done** |
| 2 | Registry + `/statefile on\|off\|status` command | **done** |
| 3 | Hook wiring → live `.state` mirroring for tagged sessions | planned |
| 4 | `post_api_request` usage capture; verify cache fields on local llama.cpp / vLLM | planned |
| 5 | `cold_limit` policy + handoff via context injection (advisory first) | planned |
| 6 | Skill + docs for install under `~/.hermes/plugins/` | planned |

## Install

Not yet. When v0.1 lands, this will be a user plugin (`~/.hermes/plugins/`) or a pip entry
point — project-local plugins are not loaded by the Hermes gateway.

## License

MIT — see [LICENSE](LICENSE). Attribution for the borrowed design is in [NOTICE](NOTICE).
