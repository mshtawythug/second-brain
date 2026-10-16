# `BRAIN_MCP_LOG_LEVEL` from `.env` — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-16-mcp-log-level-dotenv-design.md`
**Branch:** `fix/mcp-log-level-dotenv` from `second-brain/master` @ `a967685`

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
