# Contributing

This repo is at step 0 (scaffold). Before writing implementation code, read
[docs/design.md](docs/design.md) and [docs/prior-art.md](docs/prior-art.md).

Ground rules:

- Original Python only. Do not copy Mainplane's Go source. Describe borrowed ideas in
  `docs/prior-art.md` and keep the attribution in `NOTICE` accurate.
- Keep the plugin **opt-in**. No hook may touch a session that has not been tagged.
- Do not replace Hermes' context engine; use `pre_llm_call` context injection instead.
- Statefiles contain full transcripts and tool output. Default to local, per-profile storage,
  restrictive permissions, and no network egress.
- Verify hook signatures against the current Hermes plugin docs before wiring them; the tables
  in `docs/design.md` are a plan, not a guarantee.
- A native plugin directory needs `plugin.yaml` + `__init__.py` with `register(ctx)` at the
  repo root; the library lives under `src/`. Check the load with `hermes plugins doctor .`
  before pushing.
