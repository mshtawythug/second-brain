# `brain-install-launchd` vault resolution — design

**Date:** 2026-10-16 · **Branch:** `fix/install-launchd-vault-path` · **Base:** `1481e1e`
**Plan:** `docs/plans/2026-10-16-install-launchd-vault.md`
**Found in:** the PR #39 sweep (`docs/specs/2026-10-16-mcp-log-level-dotenv-design.md` §3).

## 1. The bug

`bin/launchd.install_main` (the `brain-install-launchd` console script) called
`install_plists(brain_home, launchd_dir, launchctl)` with no `vault_path`, and
`install_plists` fell back to the literal `Path.home() / "brain-vault"`. That
default read no configuration at all: not an exported `BRAIN_VAULT_PATH`, and
not `$BRAIN_HOME/.env`.

`brain-up` runs `brain-install-launchd` on every start
(`templates/bin/brain-up.sh`, `ensure_supervised`). That command rewrites both
plists and re-bootstraps them. So for an install with a custom vault, `brain
setup` wrote correct plists (it passes its own resolved vault), and the next
`brain-up` repointed the vault-sync watcher and the daily brief at
`~/brain-vault`. It doesn't bite an install whose vault is the default.

## 2. Decision

### Precedence: the same as `brain setup`

`--vault` (an explicit `vault_path`) > `BRAIN_VAULT_PATH` in the process env >
`BRAIN_VAULT_PATH` in `$BRAIN_HOME/.env` > the default vault.

- **No flag is added to `brain-install-launchd`.** It takes no arguments today,
  and `brain-up` calls it bare. The explicit leg is the `vault_path` argument
  `brain setup` already passes to `install_plists`, and it keeps winning.
- **Why `$BRAIN_HOME/.env` and not `Config.load_minimal().vault_path`:**
  - The full dotenv chain includes the cwd walk-up, and `brain-up` runs from
    any directory. That is the ambient-cwd read the plists deliberately opt out
    of (`BRAIN_IGNORE_CWD_DOTENV=1`).
  - `load_minimal` also raises on unrelated invalid knobs, so a typo elsewhere
    would stop the daemons being supervised at all.
  - `$BRAIN_HOME/.env` is the file the daemons themselves resolve, and in a dev
    checkout it is the repo `.env`.
  - The reasoning is the same as PR #39's for setup.

### One definition (DRY): `brain/install_config.py`

PR #39 left the `.env` lookup as a private helper in `setup.py`
(`_brain_home_dotenv_value`), with the vault precedence written inline in
`run_setup`. Importing that into `bin/launchd.py` would pull in the whole Typer
installer, and copying it would give two precedences that can drift. Both move
into a new module, `brain/install_config.py` (45 lines):

- `brain_home_dotenv_value(brain_home, name)` is moved unchanged. Setup still
  uses it for `VOYAGE_API_KEY`.
- `resolve_install_vault(brain_home, override=None)` is the precedence above.
  It reads `brain.config.DEFAULT_VAULT_PATH` at call time, not at import, so
  tests can fence the default into `tmp_path`.

### Where the resolver is called: `install_plists`'s default

`install_plists` now resolves `vault_path=None` through `resolve_install_vault`.
`install_main` itself is unchanged. This is the pattern the same function
already uses for `launchctl`: its docstring says that argument "defaults to
`resolve_launchctl`, never a literal", because a literal default ignored the
environment for every direct caller. Fixing the default rather than the one
caller covers any future direct caller too. `brain setup` keeps passing its own
resolved vault explicitly, so the two installers cannot disagree.

### `uninstall_main`: no change

`uninstall_plists` boots out and deletes plists by label. It renders nothing
and never needs a vault.

## 3. Scope left alone

- **The shell tooling still reads only the exported value — FOLLOW-UP.**
  `brain-up.sh` sets its own `VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"`
  (`templates/bin/brain-up.sh:51`) for the opt-in `BRAIN_WIKI_BUILD=1` overlay
  and cold-start build, and `brain-status.sh` and `brain-monitor` do the same.
  After this change, a `.env`-only custom vault therefore gives a real split: the
  plists (and so the daemons) get the custom vault, while
  `BRAIN_WIKI_BUILD=1 brain-up` builds `~/brain-vault` and `brain-status`
  reports on it. The `_brain-*-fg` wrappers are not affected: they run under the
  plist's env, which now carries the right vault. The workaround is to export
  `BRAIN_VAULT_PATH`, and `docs/vault-and-wiki.md` says so at the env-overrides
  row. The fix is the "shell-tooling family" follow-up from PR #39's spec §3 —
  the shell scripts would need to ask Python for the vault, e.g. through
  `resolve_install_vault` — and it stays out of this change.
- **No `--vault` flag on `brain-install-launchd`**, as above.

## 4. Tests

`tests/test_install_launchd_vault.py`. Everything is on `tmp_path`: `HOME`,
`BRAIN_HOME`, the vaults, `BRAIN_LAUNCHD_DIR`, `BRAIN_LAUNCHCTL=/usr/bin/true`,
and `brain.config.DEFAULT_VAULT_PATH`.

- `test_install_main_takes_vault_from_brain_home_dotenv` is the regression. It
  was red first: both plists got `brain-vault` under the fenced `HOME`.
- `test_install_main_process_env_beats_brain_home_dotenv` covers precedence. It
  was also red first, because the literal ignored the process env too.
- `test_install_main_without_any_vault_config_uses_the_default` checks that the
  default is still the default.
- `test_explicit_vault_path_beats_env_and_dotenv` checks that setup's explicit
  vault still wins.
- `test_a_tilde_vault_is_expanded_before_it_reaches_the_plist[dotenv|process-env]`
  checks that `~/…` is expanded against the fenced `HOME` before it is baked in
  (added in review). launchd does not expand `~`, so a literal `~/Notes` in a
  plist makes the watcher exit 2 ("not a directory") and respawn every 10s.

The tests read `BRAIN_VAULT_PATH` from each installed plist with `plistlib`.
`tests/test_setup_dotenv_knobs.py` is repointed at the moved helper and the
`brain.config` default; all of its assertions are unchanged.

## 5. Ceiling records

`src/brain/setup.py` is in CLAUDE.md's over-ceiling table. It shrinks here, from
1,389 to 1,363 lines, so it owes no growth record. Its trailing descriptive hop
(the PR #39 format fix) is promoted to `c2c04a6` (1,389), and a descriptive hop
is added for this change. `bin/launchd.py` (324) and the new
`install_config.py` (45) are under the ceiling.
