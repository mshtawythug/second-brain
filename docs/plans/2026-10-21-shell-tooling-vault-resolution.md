# Shell tooling vault resolution — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/shell-tooling-vault-resolution`, base `second-brain/master` @ `6948c6f`
(PR #40, which added `brain.install_config.resolve_install_vault`).

## Problem

PR #40 gave the installers one vault precedence — `--vault` > exported
`BRAIN_VAULT_PATH` > `BRAIN_VAULT_PATH` in `$BRAIN_HOME/.env` > the packaged
default — and baked the result into the launchd plists. The read-only tooling
did not follow:

1. **`brain-status`** (`src/brain/templates/bin/brain-status.sh`) and
   **`brain-monitor`** (`src/brain/bin/monitor.py::_vault`) read only an
   exported `BRAIN_VAULT_PATH`, else `~/brain-vault`. With the vault configured
   only in `$BRAIN_HOME/.env` — the normal case after `brain setup --vault X`,
   since nothing exports it into an interactive shell — both inspect the
   default vault while the daemons watch the configured one. `brain-monitor`
   also never expands `~`.
2. **`brain setup --vault '~/x'`** (quoted, so the shell does not expand it):
   `resolve_install_vault` returns the override verbatim, so a literal `~`
   reaches `.env`, `os.environ` and the plists. The env and dotenv legs were
   already expanded; only the explicit leg was not.

## Inventory — every `BRAIN_VAULT_PATH` reader in `bin/`, `scripts/` and the shipped shims

Re-derive with `grep -rn 'BRAIN_VAULT_PATH\|brain-vault' bin scripts src/brain/templates/bin src/brain/bin`.

| Reader | Same bug? | Action |
|---|---|---|
| `templates/bin/brain-status.sh` (installed shim behind `bin/brain-status` → `.venv/bin/brain-status`) | yes — env-only, reads `<vault>/.quartz/current` | **fix**: ask the resolver; print the vault it inspected |
| `src/brain/bin/monitor.py::_vault` (behind `bin/brain-monitor`) | yes — env-only, no `~` expansion | **fix**: `resolve_install_vault(_brain_home_root())` |
| `templates/bin/_brain-watcher-fg.sh` + its dev copy `bin/_brain-watcher-fg` | yes, on a manual run only — under launchd the plist always exports the resolved vault | **fix**: env (plist-baked) used as-is; unset → ask the resolver. Keeps the daemon start path free of an extra interpreter call. |
| `templates/bin/brain-up.sh` | `VAULT` feeds only the `BRAIN_WIKI_BUILD=1` overlay + cold-start build and the wiki URL warning | **leave** — wiki-only, deleted by the parallel phase-9 PR. No non-wiki vault read exists in it. |
| `templates/bin/_brain-build-fg.sh` + `bin/_brain-build-fg` | wiki build daemon | **leave** — wiki (phase 9) |
| `bin/brain-verify-fastpath` | Quartz fast-path verifier | **leave** — wiki (phase 9) |
| `scripts/smoke-macos.sh`, `scripts/smoke-linux.sh` | default-only, but run on a fresh VM right after `install.sh` (no custom vault exists), and the vault is consumed by the wiki-rebuild observation step; a pipx install puts no brain-importable interpreter on `PATH` for a shell helper | **leave**, reported |
| `templates/bin/_brain-brief-fg.sh` | no — passes through to `brain brief`, which uses `Config.load` | none |
| `bin/brain-*-gif` | no — they EXPORT a sandbox vault, they do not read one | none |
| `src/brain/claude_hook.py::load_thresholds` (outside bin/scripts) | env-only **by documented design** ("never the .env chain" — the Stop hook must survive missing infra) | **leave**, reported |

## Design

- `brain.install_config` stays the single definition of the precedence.
  - `resolve_install_vault`: the explicit leg becomes `override.expanduser()`.
    Every installer write (`.env` render, `.env` append, `os.environ`, plists)
    derives from this one value, so this is the setup write boundary and it
    keeps `setup.py` (over the 800-line ceiling) from growing.
  - New `main(argv)` + `python -m brain.install_config BRAIN_HOME`: prints the
    resolved vault. This is the shell entry point; the bash shims carry **no**
    precedence logic of their own. The shim passes its own resolved
    `$BRAIN_HOME` (the same one it derives pid paths from).
- `brain-status.sh`: pick `PY` the way `brain-up.sh` does (`BRAIN_PY`, else
  `python3`); resolve after the daemon rows so a failed resolution still
  reports the daemons; print a `vault` row; on failure say so instead of
  silently inspecting a wrong path.
- `monitor.py`: `_vault()` delegates; the epilog names the `.env` leg.
- `_brain-watcher-fg`: resolve after the venv autoload so `python3` is the
  venv's in a dev checkout; abort (no pid file) if resolution fails.

## Tests (TDD — each written red first)

| Test | File | Targets |
|---|---|---|
| explicit `~` override is expanded | `tests/test_install_config.py` | `resolve_install_vault` override leg |
| `main` prints the `$BRAIN_HOME/.env` vault; bad argv → 2 | `tests/test_install_config.py` | the shell entry point |
| `brain setup --vault ~/x` writes an absolute path (fresh render + append to existing `.env`) | `tests/test_setup_vault_tilde.py` | fix 2 at the write boundary |
| `brain-monitor` reads the vault from `$BRAIN_HOME/.env`, expanded | `tests/test_bin_monitor.py` | `_vault` |
| `brain-status` inspects the `$BRAIN_HOME/.env` vault with no env export | `tests/test_bin_scripts.py` | the shim |
| `_brain-watcher-fg` passes the `$BRAIN_HOME/.env` vault with no env export | `tests/test_bin_scripts.py` | the shim |

## Risks

- **Live daemon wrapper.** `_brain-watcher-fg` runs under launchd. The change
  is inert there (the plist exports `BRAIN_VAULT_PATH`); the resolver is only
  reached on a manual run. The installed shim updates on the next launcher run
  via `ensure_shim` drift protection — nothing on this branch touches the live
  install.
- **Phase-9 overlap.** No wiki-build code is edited, so the phase-9 deletion
  PR should not conflict beyond `brain-status.sh`'s Quartz `current` block,
  which this change only moves below the new resolution.
- **Behaviour change** for `brain-status` / `brain-monitor`: they now report
  the vault the daemons watch rather than `~/brain-vault`. That is the fix.

## Mutation proofs (five clauses)

Filled in after implementation: (1) passes unmutated, (2) the mutation lands,
(3) the test goes red, (4) the failing assertion is the targeted one,
(5) restored → green.
