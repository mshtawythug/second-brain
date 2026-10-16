# `BRAIN_MCP_LOG_LEVEL` from `.env` — design

**Date:** 2026-10-16 · **Branch:** `fix/mcp-log-level-dotenv` · **Base:** `a967685` (master `1cd136a` merged in afterwards)
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

**Records emitted during `Config.load`, and how the level takes effect.**
`config.py` has no logger. The only record that can fire during the load is
python-dotenv's WARNING for an unparseable `.env` line (`dotenv.main`).
Importing `brain.mcp_server` builds the FastMCP server at module scope
(`make_server`), and that installs a stderr `RichHandler` on the root logger at
INFO before `main()` runs. That handler is therefore in place during the load,
so the WARNING reaches stderr under either order, formatted by Rich. The same
fact makes `_configure_logging`'s `basicConfig` a no-op in production: the level
takes effect through `logger.setLevel` on `brain.mcp`, whose records propagate
to that handler. This was verified end to end: with `brain.mcp` at DEBUG, stdout
stays empty and both the dotenv WARNING and a DEBUG line reach stderr. Nothing at
DEBUG/INFO is emitted during the load, so no record the configured level would
have shown is lost, and buffering records across the load was rejected as
machinery for an empty set.

*(Corrected in review. The first draft, and the message of the pushed commit
`8669e8b`, said the WARNING reached stderr through `logging.lastResort` and that
`basicConfig` configured the stream. Both were true only of a bare interpreter
that had not imported `brain.mcp_server`, which is never the case in
`brain-mcp`. The behaviour was correct; the stated mechanism was not.)*

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
| `bin/launchd.resolve_brain_py` (`BRAIN_PY`) | left | An interpreter pin for rendering plists against a different install (docstring example: `BRAIN_PY=… brain-install-launchd`). It is a per-invocation override, and the plists it renders carry `BRAIN_PY` themselves. |
| `doctor_runtime._plist_brain_py` (`BRAIN_PY`) | not an env read | It reads `EnvironmentVariables.BRAIN_PY` out of an installed plist file, not `os.environ`. |
| `bin/launchd.install_main` / `uninstall_main` (`brain-install-launchd` / `brain-uninstall-launchd` in `[project.scripts]`; read `BRAIN_PY`, `BRAIN_LAUNCHCTL`, `BRAIN_LAUNCHD_DIR`) | left | Test seams and shell-tooling overrides, documented as such in `docs/vault-and-wiki.md` (the "Env overrides" table). None of them is a `.env` knob. (These entry points ignoring `BRAIN_VAULT_PATH` altogether is the separate follow-up below.) |
| `bin/launchd.resolve_launchctl` (`BRAIN_LAUNCHCTL`) | left | A test seam that keeps the real `launchctl` unreachable from the suite (phase 8 T0). It is not a user knob. |
| `bin/monitor.py` (`BRAIN_WATCH_PID`, `BRAIN_BUILD_PID`, read at import) | left | Test seams and parity with the shell shims' PID paths (module comment at the constants); same shell-tooling family as the monitor row above. |
| `claude_hook.load_thresholds` (`BRAIN_HOOK_*` and `BRAIN_VAULT_PATH`, read through name constants from an injected mapping; `cli_claude` passes `os.environ`) | left | Deliberate and documented at the function: the Stop hook must survive the absence of `DATABASE_URL` and must not pay dotenv resolution on every event, so these are real environment variables, not `.env` entries. |

The check ran over every `os.environ.get` / `os.getenv` / `os.environ[...]`
read outside `config.py`, including reads through a name constant
(`MAX_BYTES_ENV`, `BUILD_TIMEOUT_ENV_VAR`) or through an injected mapping that
a caller fills from `os.environ` (`claude_hook`), plus every `main()`,
`cli_main()` and `__main__` block under `src/brain`. *(Corrected in review: the
first draft claimed completeness while missing the `BRAIN_PY`,
`BRAIN_LAUNCHCTL`, `BRAIN_*_PID` and `BRAIN_HOOK_*` rows above. Its grep matched
only string-literal names, so reads through constants and injected mappings
slipped past it.)*

**Follow-up found during review, not fixed here: `brain-install-launchd`
ignores `BRAIN_VAULT_PATH` entirely.** `bin/launchd.install_main` calls
`install_plists(brain_home, launchd_dir, launchctl)` without a `vault_path`,
and `install_plists` then falls back to `~/brain-vault`. It reads neither the
process env nor any `.env`, and every `brain-up` runs it. So an install with a
custom vault gets correct plists from `brain setup`, and they are rewritten to
point at `~/brain-vault` on the next `brain-up`. This is the same hazard this
change fixes in setup, but it is not the same class: no read happens too early,
because no read happens at all. It needs its own change and test, so it is
raised with the coordinator rather than folded in here. **Coordinator decision:** kept out of this PR; it runs as its own track after #39 merges.

### `--reset` keeps the old `.env`'s vault (decided in review)

`run_setup` resolves the vault (and now reads `$BRAIN_HOME/.env` for it) BEFORE
the `--reset` branch deletes `$BRAIN_HOME`. So `--reset` without `--vault` now
keeps the vault the old `.env` named; it used to fall back to `~/brain-vault`.
**Kept deliberately:** reset deletes `$BRAIN_HOME` (config, Postgres data,
shims) but never the vault, so pointing the fresh config back at the user's
existing vault is the right outcome. The alternative silently orphans their
notes behind a default path. `--vault` and the process env still override it.
Pinned by `test_reset_keeps_the_vault_named_in_the_old_brain_home_dotenv`, which
is a dry run on tmp paths.

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
- `test_voyage_key_is_not_looked_up_for_other_embedders`: only a voyage run
  reads the key. A malformed line makes each parse observable as one
  python-dotenv warning, and a non-voyage run must parse exactly once.
- `test_reset_keeps_the_vault_named_in_the_old_brain_home_dotenv`: §3's
  `--reset` decision.
- `test_unusable_brain_home_dotenv_reads_as_unset` (a directory, mode 000, or
  not UTF-8): the helper falls back to `None` and never raises.

They isolate config with `isolate_config_dotenv(..., project_env=...)` and
pytest's `monkeypatch`. The setup tests also use `unittest.mock.patch` to stub
`shutil.which` / `subprocess.run` (and `typer.prompt` for the reset
confirmation), as `tests/test_setup.py` does. All of these are standard test
doubles with automatic cleanup. The tests run under the suite-wide `os.environ` restore
(`tests/env_isolation.py`, PR #38), so a key `Config.load` setdefaults in from
a test's `.env` cannot outlive the test. There is no monkey-patching of production code.

## 5. Ceiling records

`mcp_server.py` and `setup.py` are both in CLAUDE.md's over-ceiling table and
both grew. Each growth carries an inline reason (the `_load_config` docstring;
the helper docstring and a comment at each read) and a module-docstring
pointer. Neither row had a trailing descriptive hop left to promote: both were
already SHA-bound through `2475d11` / `120934a`, and nothing touched either file
from there through `1cd136a`. Each row gains a SHA-bound hop at `8669e8b`, the
commit that grew both files (4,960 and 1,390 lines, measured with `git show`).
The merge of master changes neither file. The PR #39 review-fix commit after it
touches both, and each row gets a descriptive hop for it: `mcp_server.py` grows
by the corrected `_load_config` docstring (§2), and `setup.py` changes at net
zero (the Voyage short-circuit).
