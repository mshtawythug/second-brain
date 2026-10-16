# `BRAIN_MCP_LOG_LEVEL` from `.env` — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-16-mcp-log-level-dotenv-design.md`
**Branch:** `fix/mcp-log-level-dotenv` from `second-brain/master` @ `a967685` (master `1cd136a` merged in afterwards)

**Goal:** make `brain-mcp` honour `BRAIN_MCP_LOG_LEVEL` from the dotenv chain,
and fix the same-class reads in `brain setup`, without changing precedence or
the startup-failure path.

## Task 1: `brain-mcp` loads config before configuring logging

**Files:** `src/brain/mcp_server.py`, `tests/test_mcp_server.py`

1. Extract `_load_config()` from `main()`, keeping the OLD order (a pure
   refactor), and route `main()` through it.
2. RED: `test_load_config_honours_log_level_from_dotenv`. A `.env` holds
   `BRAIN_MCP_LOG_LEVEL=DEBUG`, the process env does not, and the test expects
   an effective level of DEBUG. It must fail with `assert 20 == 10`.
3. GREEN: swap to `Config.load()` then `_configure_logging()`, with the reason
   in `_load_config`'s docstring.
4. Add the guards: process env beats `.env`; `main()` goes through
   `_load_config` with nothing ahead of it; a missing `DATABASE_URL` still
   raises `ConfigError`.

## Task 2: `brain setup` re-runs read `$BRAIN_HOME/.env`

**Files:** `src/brain/setup.py`, `tests/test_setup_dotenv_knobs.py` (new)

1. RED (dry runs): an existing `$BRAIN_HOME/.env` holds `BRAIN_VAULT_PATH` and
   the vault resolves to the default. An existing `.env` holds
   `VOYAGE_API_KEY` and `--non-interactive --embedder voyage` exits 1.
2. GREEN: add `_brain_home_dotenv_value(brain_home, name)` and use it as the
   fallback below the process env at both reads. `--vault` stays on top.
3. Guards: the process env beats the file; `--vault` beats the file; a key
   missing everywhere still refuses.

## Task 3: records and docs

1. Module-docstring pointers in `mcp_server.py` and `setup.py`, and inline
   reasons at each growth site.
2. CLAUDE.md over-ceiling rows: check for a trailing descriptive hop to promote
   (there was none: both rows were already SHA-bound), then add a descriptive
   hop and a growth note to each.
3. CHANGELOG `[Unreleased]` → Fixed: two entries.
4. `docs/configuration.md`: the `BRAIN_MCP_LOG_LEVEL` row says where the knob
   can be set and that the Desktop `env` block wins.

## Task 4: verification

1. `ruff check`, `mypy src/`.
2. The affected files under brain-ci's environment exports (`CI`,
   `GITHUB_ACTIONS`, colour vars scrubbed), against a sibling test DB:
   `tests/test_mcp*.py tests/test_config*.py tests/test_setup*.py
   tests/test_cli_setup.py`. The full suite is CI's job.
3. A five-clause mutation catalog over every new guard.
4. Commit, but do not push. The coordinator pushes.

## Out of scope (recorded in the spec, §3)

`bin/monitor.py` and the shell-tooling env-override family (`brain-up`,
`brain-status`, the `_brain-*-fg` shims) read `BRAIN_VAULT_PATH` /
`BRAIN_WIKI_PORT` from the process env only. Whether that family should read
`.env` is a separate decision.

## Mutation catalog

Every new guard passes all five clauses: green unmutated; the mutation lands its
declared count; red; the needle appears on a `>`/`E` line; restored → green,
with the file's sha256 unchanged. Run with `PYTHONDONTWRITEBYTECODE=1`.
M10–M14 were added in the PR #39 review.

Tests: **mcp** = `tests/test_mcp_server.py`, **setup** =
`tests/test_setup_dotenv_knobs.py`.

| # | Target | Mutation | Count | Expected red | Needle |
|---|---|---|---|---|---|
| M1 | `mcp_server._load_config` | `_configure_logging()` before `Config.load()` (the original bug) | 1 | mcp `test_load_config_honours_log_level_from_dotenv` | `assert 20 == 10` |
| M2 | `mcp_server.main` | `cfg = _load_config()` → `_configure_logging(); cfg = Config.load()` | 1 | mcp `test_main_loads_config_before_configuring_logging` | `configure_logging` |
| M3 | `mcp_server._configure_logging` | read the dotenv chain's value ahead of `os.environ` | 1 | mcp `test_load_config_process_env_log_level_beats_dotenv` | `assert 10 == 40` |
| M4 | `mcp_server._load_config` | `except Exception: cfg = Config.load_minimal()` around the load | 1 | mcp `test_load_config_missing_database_url_still_raises` | `DID NOT RAISE` |
| M5 | `setup.run_setup` vault | drop the `$BRAIN_HOME/.env` leg | 1 | setup `test_rerun_takes_vault_from_existing_brain_home_dotenv` | `would: export BRAIN_VAULT_PATH=` |
| M6 | `setup.run_setup` vault | `.env` leg ahead of the process env | 1 | setup `test_vault_process_env_beats_brain_home_dotenv` | `from-env` |
| M7 | `setup.run_setup` vault | `.env` value ahead of `--vault` | 1 | setup `test_vault_override_beats_brain_home_dotenv` | `override` |
| M8 | `setup.run_setup` Voyage | drop the `$BRAIN_HOME/.env` leg | 1 | setup `test_rerun_accepts_voyage_key_from_existing_brain_home_dotenv` | `Exit` |
| M9 | `setup._brain_home_dotenv_value` | `or None` → `or name` (a missing key reads as set) | 1 | setup `test_voyage_key_missing_everywhere_still_refuses` | `DID NOT RAISE` |
| M10 | `setup._brain_home_dotenv_value` | `except (OSError, UnicodeDecodeError)` → `except FileNotFoundError` | 1 | setup `test_unusable_brain_home_dotenv_reads_as_unset[directory]` | `IsADirectoryError` |
| M11 | `setup._brain_home_dotenv_value` | → `except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError)` | 1 | setup `test_unusable_brain_home_dotenv_reads_as_unset[unreadable]` | `PermissionError` |
| M12 | `setup._brain_home_dotenv_value` | → `except OSError` | 1 | setup `test_unusable_brain_home_dotenv_reads_as_unset[not-utf8]` | `UnicodeDecodeError` |
| M13 | `setup.run_setup` Voyage | evaluate the key lookup eagerly on every run | 1 | setup `test_voyage_key_is_not_looked_up_for_other_embedders` | `assert 2 == 1` |
| M14 | `setup.run_setup` `--reset` | re-resolve `vault_override or DEFAULT_VAULT_PATH` inside the reset branch | 1 | setup `test_reset_keeps_the_vault_named_in_the_old_brain_home_dotenv` | `custom-vault` |

M4's first draft caught `ConfigError`, which `mcp_server` does not import, so it
went red on a `NameError`. Clause 4 (the needle) caught that; the row above
is the corrected mutation.
