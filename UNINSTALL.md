# Uninstalling / inspecting hermes-flight-recorder

The plugin has **two** separate locations. Hermes deletes the first on
`hermes plugins remove`, and deliberately **keeps** the second. A complete
removal needs both.

## Where things live

| | Path | Contents | Deleted by `hermes plugins remove`? |
| --- | --- | --- | --- |
| Install tree | `~/.hermes/plugins/hermes-flight-recorder/` | `plugin.yaml`, `__init__.py`, `src/hermes_flight_recorder/*.py` | **Yes** (also removes the `plugins.enabled` entry) |
| Runtime data | `<hermes home>/plugin-data/hermes-flight-recorder/` | `registry.json`, `registry.lock`, `sessions/*.state` | **No** |

`<hermes home>` is `~/.hermes` by default. `plugin_data_dir()` resolves the
active profile on every call, so under multiplex each profile home gets its
**own** copy, e.g. `~/.hermes/profiles/<name>/plugin-data/hermes-flight-recorder/`.
Hermes' storage convention intentionally keeps `plugin-data/` across
`remove` and `update` so an update cannot destroy transcripts — there is no
`--purge-data` flag on `hermes plugins remove`, and
`hermes plugins show <name>` does not print the data path.

All runtime files are `0600`, directories `0700`. `registry.json` contains
session ids and metadata; `.state` files contain **redacted** transcripts
(`ctx.redact_text(..., force=True)` was applied before storing).

## Inspecting the files directly

```
# every tagged session
cat ~/.hermes/plugin-data/hermes-flight-recorder/registry.json

# the append-only records of one session (JSON header line + raw body)
python3 -c "from hermes_flight_recorder import read_records; \
[print(r.n, r.kind, r.body[:200]) for r in read_records('.../<file>.state')]"
```

`.state` files are append-only: one JSON header line, then a body of exactly
`len` bytes, then a newline. Bodies are opaque raw bytes and may contain
newlines or non-UTF-8 data — use `read_records`, not a line-based parser.

## Clean up while the plugin is still installed

In any session:

```
/statefile purge      # this session only: drop its tag, delete its .state file, close its handle
/statefile purge-all  # ALL plugin data: every entry, every .state file, registry.json + lock
```

Both are **irreversible**. `purge-all` reports the counts it removed. The empty
`plugin-data/hermes-flight-recorder/` directories remain — delete them in the
next step. Hooks are inert after a purge, because no session is tagged.

Note: when Hermes deletes or archives a session, `on_session_delete` keeps that
session's `.state` file for audit but drops its registry entry, so the registry
does not grow without bound. Those archived `.state` files are what
`purge-all` clears.

## Full uninstall (works after the plugin is gone)

1. Remove the install tree and config entry:

   ```
   hermes plugins remove hermes-flight-recorder
   ```

   If you also installed the Python package via pip, `pip uninstall
   hermes-flight-recorder`.

2. Find **every** data directory (default home + all profiles):

   ```
   find ~/.hermes -type d -path '*/plugin-data/hermes-flight-recorder' -print
   ```

   Also check any custom `HERMES_HOME` you have used, e.g. `find "$HERMES_HOME"
   -type d -path '*/plugin-data/hermes-flight-recorder' -print`.

3. Review, then delete each one:

   ```
   # review first
   find ~/.hermes -type d -path '*/plugin-data/hermes-flight-recorder' -print
   # then remove (substitute each path printed above)
   rm -rf ~/.hermes/plugin-data/hermes-flight-recorder
   ```

4. Verify nothing remains:

   ```
   grep -n hermes-flight-recorder ~/.hermes/config.yaml   # expect no output
   find ~/.hermes -type d -name hermes-flight-recorder -print  # expect no output
   ```

`registry.lock` is a zero-byte advisory-lock file; it is harmless if left
behind. There are no cron jobs, services, env vars, network endpoints or
system-wide files created by this plugin.
