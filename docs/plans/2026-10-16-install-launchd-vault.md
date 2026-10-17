# `brain-install-launchd` vault resolution — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-16-install-launchd-vault-design.md`
**Branch:** `fix/install-launchd-vault-path` from `second-brain/master` @ `1481e1e`

**Goal:** `brain-install-launchd`, which every `brain-up` runs, bakes the
configured vault into the daemon plists, using the same precedence as
`brain setup`.

**Hard rule for every task:** tmp paths only. Never touch the real
`~/Library/LaunchAgents`, `launchctl`, `~/.brain` or the vault. The seams are
`BRAIN_LAUNCHD_DIR`, `BRAIN_LAUNCHCTL=/usr/bin/true`, `HOME`, `BRAIN_HOME` and
`brain.config.DEFAULT_VAULT_PATH`.

## Task 1: regression test, red first

**File:** `tests/test_install_launchd_vault.py` (new)

1. Write a fixture that fences `HOME`, `BRAIN_HOME`, `BRAIN_LAUNCHD_DIR`,
   `BRAIN_LAUNCHCTL` and the default vault into `tmp_path`, and unsets
   `BRAIN_VAULT_PATH`.
2. RED: `$BRAIN_HOME/.env` names a custom vault and `install_main()` runs. Both
   plists' `EnvironmentVariables.BRAIN_VAULT_PATH` must be the custom vault.
   It fails with `brain-vault` under the fenced `HOME`, not the custom vault.
3. Precedence tests: the process env beats `.env`; no config gives the default;
   an explicit `vault_path` beats both.

## Task 2: shared resolver

**Files:** `src/brain/install_config.py` (new), `src/brain/setup.py`,
`src/brain/bin/launchd.py`, `tests/test_setup_dotenv_knobs.py`

1. Move `setup._brain_home_dotenv_value` to
   `install_config.brain_home_dotenv_value`, unchanged.
2. Add `install_config.resolve_install_vault(brain_home, override=None)`:
   override > env > `$BRAIN_HOME/.env` > `brain.config.DEFAULT_VAULT_PATH`, read
   at call time.
3. `setup.run_setup` calls `resolve_install_vault(brain_home,
   override=vault_override)`; the inline block goes.
4. `install_plists` resolves a `None` vault through the resolver, not the
   literal, and its docstring gives the reason.
5. Repoint the setup tests at the moved helper and the `brain.config` default.
6. GREEN.

## Task 3: records and docs

1. CLAUDE.md `setup.py` row: promote the trailing descriptive hop to `c2c04a6`,
   add a descriptive hop for this change (net-negative), and note where the
   helper moved.
2. CHANGELOG `[Unreleased]` → Fixed.
3. `docs/vault-and-wiki.md` env-overrides table: the `BRAIN_VAULT_PATH` row
   says what `brain-install-launchd` bakes in.

## Task 4: verification

1. `ruff check`, `mypy src/`, and `ruff format --check` on new files (no new
   drift on touched files).
2. The affected test files, under brain-ci's env exports, against a sibling DB.
3. The mutation catalog below, with the home snapshot guard.
4. Commit without pushing.

## Mutation catalog

Five clauses per row: green unmutated; lands its declared count; red; the needle
appears on a `>`/`E` line; restored → green, sha256 unchanged. The harness also
snapshots the top-level mtimes of the real `~/.brain`, `~/brain-vault` and
`~/Library/LaunchAgents` before and after the run, and requires them unchanged.

Tests: **L** = `tests/test_install_launchd_vault.py`, **S** =
`tests/test_setup_dotenv_knobs.py`. S1–S3 re-prove that PR #39's setup guards
still bite after the helper moved.

| # | Target | Mutation | Count | Expected red | Needle |
|---|---|---|---|---|---|
| L1 | `bin/launchd.install_plists` | back to `vault_path if … else Path.home() / "brain-vault"` (the bug) | 1 | L `test_install_main_takes_vault_from_brain_home_dotenv` | `custom-vault` |
| L2 | `install_config.resolve_install_vault` | drop the `$BRAIN_HOME/.env` leg | 1 | L `test_install_main_takes_vault_from_brain_home_dotenv` | `custom-vault` |
| L3 | `install_config.resolve_install_vault` | `.env` leg ahead of the process env | 1 | L `test_install_main_process_env_beats_brain_home_dotenv` | `from-file` |
| L4 | `install_config.resolve_install_vault` | drop `if override is not None: return override` | 1 | L `test_explicit_vault_path_beats_env_and_dotenv` | `explicit` |
| L5 | `install_config.resolve_install_vault` | default → literal `Path.home() / "brain-vault"` instead of `brain.config.DEFAULT_VAULT_PATH` | 1 | L `test_install_main_without_any_vault_config_uses_the_default` | `default-vault` |
| R1 | `install_config.resolve_install_vault` | `Path(raw).expanduser()` → `Path(raw)` (added in review) | 1 | L `test_a_tilde_vault_is_expanded_before_it_reaches_the_plist[dotenv]` | `~/custom-vault` |
| R1 | same | same, proved separately on the process-env leg | 1 | L `test_a_tilde_vault_is_expanded_before_it_reaches_the_plist[process-env]` | `~/custom-vault` |
| S1 | `setup.run_setup` | stop passing `override=vault_override` | 1 | S `test_vault_override_beats_brain_home_dotenv` | `override` |
| S2 | `setup.run_setup` | MOVE the resolve line (and its comment) below the reset block (ORD) | 1 | S `test_real_reset_writes_the_old_dotenv_vault_into_the_fresh_dotenv` | `default-vault` |
| S3 | `install_config.brain_home_dotenv_value` | `except (OSError, UnicodeDecodeError)` → `except FileNotFoundError` | 1 | S `test_unusable_brain_home_dotenv_reads_as_unset[directory]` | `IsADirectoryError` |
