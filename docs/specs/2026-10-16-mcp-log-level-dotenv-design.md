# `BRAIN_MCP_LOG_LEVEL` from `.env` — design

**Date:** 2026-10-16 · **Branch:** `fix/mcp-log-level-dotenv` · **Base:** `a967685`
**Plan:** `docs/plans/2026-10-16-mcp-log-level-dotenv.md`

## 1. The bug

`brain-mcp` ignores `BRAIN_MCP_LOG_LEVEL` when the knob is set in a `.env`.

`mcp_server.main()` called `_configure_logging()` and then `Config.load()`.
`_configure_logging` reads `os.environ["BRAIN_MCP_LOG_LEVEL"]`. The only thing
that applies the dotenv chain (repo `.env`, cwd walk-up, `$BRAIN_HOME/.env`) is
`Config._load_field_dict`, which `os.environ.setdefault`s every merged key.
Logging was therefore configured from a process env that did not yet contain
any `.env` value. The knob worked only from the real process env, e.g. Claude
Desktop's MCP `env` block.

What the docs promised: `docs/configuration.md` lists the knob in the MCP
"Environment variables" table and in the example Desktop `env` block. It does
not restrict it to that block, and the same section tells readers that the
other knobs come from `.env`. `.env.example` does not mention the knob.

## 2. Decision: load first, then configure logging (option a)

`main()` now starts through a new `_load_config()`:

```python
def _load_config() -> Config:
    cfg = Config.load()
    _configure_logging()
    return cfg
```

**Why (a) and not (b).** Option (b) — have `_configure_logging` read the level
from the loaded `Config` or from the dotenv chain explicitly — either adds a
`Config` field for a knob only one process reads (config.py is over the 800-line
ceiling), or re-implements the chain's precedence a second time. (a) reuses the
one precedence implementation (`process env > repo .env > cwd .env >
$BRAIN_HOME/.env`) by construction, and is the smallest change.

**Records emitted during `Config.load`.** `config.py` has no logger. The only
record that can fire during the load is python-dotenv's WARNING for an
unparseable `.env` line (`dotenv.main`). Before `basicConfig` runs, that record
still reaches stderr through `logging.lastResort` (WARNING and above, bare
message; verified). Nothing at DEBUG/INFO is emitted during the load, so no
record the configured level would have shown is lost. Buffering records across
the load was considered and rejected as machinery for an empty set.

**Failure path.** A `ConfigError` (e.g. no `DATABASE_URL` anywhere) was never
routed through logging: it propagates out of `main()` as an uncaught traceback
on stderr. That is unchanged; the new order does not swallow it.
`test_load_config_missing_database_url_still_raises` pins it.

**Precedence is unchanged.** The process env still beats every `.env`
(`os.environ.setdefault`). `test_load_config_process_env_log_level_beats_dotenv`
pins it.

## 3. Sweep: other entry points of the same class

The class: an entry point reads a knob from `os.environ` before (or without)
`Config.load` publishing the dotenv chain, so a `.env` value is silently
ignored where the rest of the system honours it.

| Site | Verdict | Reason |
|---|---|---|
| `mcp_server.main` → `_configure_logging` (`BRAIN_MCP_LOG_LEVEL`) | **FIXED** | §2. |
| `setup.run_setup` → `VOYAGE_API_KEY` | **FIXED** | Setup never calls `Config.load`. On a re-run the key the previous run appended to `$BRAIN_HOME/.env` was invisible: `--non-interactive` refused with "requires VOYAGE_API_KEY", an interactive run re-prompted and appended a duplicate line. |
| `setup.run_setup` → `BRAIN_VAULT_PATH` | **FIXED** | Same cause, worse effect: a re-run without `--vault` resolved `~/brain-vault`, exported it, and handed it to the wiki install and the launchd plists while `$BRAIN_HOME/.env` named another vault. |
| `setup.run_setup` → `BRAIN_HOME` | left | It anchors the chain: `$BRAIN_HOME/.env` cannot supply the variable that locates it. |
| `maintenance.main` (`BRAIN_WIKI_KEEP_BUILDS`) | not a bug | `Config.load()` runs on the line before the read. |
| `log_rotation` (`BRAIN_LOG_MAX_BYTES`) | left | Deliberately avoids `Config` (module comment at the constant): rotation must work while `Config.load` is the thing crash-looping. |
| `wiki/build_swap.resolve_build_timeout_s` (`BRAIN_WIKI_BUILD_TIMEOUT_S`) | left | Dormant wiki path since R2. The docstring defines it as a caller's-environment override, and it is not in any `.env` doc. |
| `wiki/build_watcher._FASTPATH_ENABLED` | left | Dormant since R2 (no build watcher runs). Documented as an "at process startup" override in the shell-tooling env table. |
| `_compose.compose_project_name` (`BRAIN_COMPOSE_PROJECT`) | left | Documented as `export BRAIN_COMPOSE_PROJECT=…` (`docs/cli-reference.md`), a per-invocation isolation switch for a second stack. |
| `bin/monitor.py` (`BRAIN_VAULT_PATH`, `BRAIN_WIKI_PORT`, no `Config.load`) | left, **follow-up** | Part of the shell-tooling family documented as "Env overrides" (`docs/vault-and-wiki.md`). `brain-up` / `brain-status` and the `_brain-*-fg` shims read the same variables from the process env only, and the daemons get `BRAIN_VAULT_PATH` from their plist. Fixing monitor alone would make it disagree with the rest of that family. Whether the shell tooling should read `.env` is a separate decision. `BRAIN_WIKI_PORT` is not a `Config` knob at all. |
| `editor.py` (`EDITOR`/`VISUAL`), `BRAIN_LAUNCHD_DIR` | left | Shell convention / test seam, never `.env` knobs. |

The check ran over every `os.environ.get` / `os.getenv` / `os.environ[...]`
read outside `config.py`, plus every `main()`, `cli_main()` and `__main__` block
under `src/brain`.

### Setup fix shape

A helper, `_brain_home_dotenv_value(brain_home, name)`, returns the value from
an existing `$BRAIN_HOME/.env` or `None`. It returns `None` for a missing,
unreadable or undecodable file, and for an empty value. Both reads now use
`process env > $BRAIN_HOME/.env` (and the vault keeps `--vault` above both).

**Why only `$BRAIN_HOME/.env` and not the whole chain:** it is the file setup
provisions and appends to, and it is the file the launchd daemons resolve
(their plists set `BRAIN_HOME` and turn the cwd walk-up off). In a dev checkout
it is the repo `.env`, by symlink or by identity (`_is_repo_checkout`). The
full chain would also need `BRAIN_HOME` to be exported, and setup only exports
it after preflight (never in a dry run), so the chain could consult a
different home than the one setup is installing into.

## 4. Tests (all red first, then green)

`tests/test_mcp_server.py`:
- `test_load_config_honours_log_level_from_dotenv`: the regression. Red before
  the swap (`assert 20 == 10`).
- `test_load_config_process_env_log_level_beats_dotenv`: precedence.
- `test_main_loads_config_before_configuring_logging`: pins `main()`'s call
  site, so a revert there is caught even with the helper intact.
- `test_load_config_missing_database_url_still_raises`: the failure path.

`tests/test_setup_dotenv_knobs.py` (dry runs, no side effects):
- `test_rerun_takes_vault_from_existing_brain_home_dotenv`: regression (red).
- `test_vault_process_env_beats_brain_home_dotenv`,
  `test_vault_override_beats_brain_home_dotenv`: precedence.
- `test_rerun_accepts_voyage_key_from_existing_brain_home_dotenv`: regression
  (red).
- `test_voyage_key_missing_everywhere_still_refuses`: the fallback must not
  turn a missing key into a pass.

They use `isolate_config_dotenv(..., project_env=...)` and pytest's
`monkeypatch` only. There is no monkey-patching of production code.

## 5. Ceiling records

`mcp_server.py` and `setup.py` are both in CLAUDE.md's over-ceiling table and
both grew. Each growth carries an inline reason (the `_load_config` docstring;
the helper docstring and a comment at each read) and a module-docstring
pointer. Neither row had a trailing descriptive hop left to promote: both were
already SHA-bound through `2475d11` / `120934a`, and nothing touched either file
from there through `a967685`. Each row gains a descriptive hop for this change.
