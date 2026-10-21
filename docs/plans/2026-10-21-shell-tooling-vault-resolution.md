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
  the vault the installers resolve — the one `brain-up` bakes into the plists —
  rather than `~/brain-vault`. That is the fix. It is resolved at the moment
  they run, so after a config change and before the next `brain-up` it can
  differ from the value the running daemons were started with.

## Mutation proofs (five clauses)

Run 2026-10-21 against `72376af` (the fix `963d915` plus the review round),
sequentially in this worktree
with nothing else running in it. Each mutation is a literal `str.replace` on
the production file, asserted to match **exactly once**; it is restored with
`git checkout -- <file>` and the tree is asserted clean before the next one.
Every run executes **all seventeen** new test cases, so clause 3 is an exact-set
claim, not "the target went red". Clause 4 reads the JUnit failure text and
requires the `>`-marked failing line to be the targeted assertion.

Clauses: (1) all seventeen pass unmutated; (2) the mutation landed (one match,
tree dirty); (3) the red set equals the declared set exactly; (4) each red
test failed AT its targeted assertion; (5) restored → tree clean and all
seventeen green.

Baseline: 17/17 pass — clause 1 OK. `[fails]` / `[prints-nothing]` are the two
parametrizations of the unresolved-vault tests: a stub interpreter that exits 1,
and one that exits 0 and prints nothing (what the empty-output guards are for).

| # | Mutation (production file) | Red set — exact | Failing assertion | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|---|
| M1 | `install_config`: explicit leg `return override` (no `expanduser`) | `test_an_explicit_tilde_override_is_expanded`, both `test_setup_vault_tilde` tests | `assert resolved ==` / `assert _vault_lines(` | ✓ | ✓ | ✓ | ✓ |
| M2 | `install_config.main` prints `_config.DEFAULT_VAULT_PATH` | `test_main_prints_the_brain_home_dotenv_vault`, `test_main_expands_a_tilde_brain_home`, `test_module_entry_point_runs_as_a_subprocess`, `test_brain_status_inspects_the_brain_home_dotenv_vault`, `test_brain_watcher_fg_resolves_…_when_unexported` | `assert capsys.readouterr().out ==` / `assert result.stdout ==` / `assert f"  vault     {vault}…"` / `assert _read_log(` | ✓ | ✓ | ✓ | ✓ |
| M3 | `main` argc check `if not args:` | `test_main_rejects_a_wrong_argument_count[argv1]` | `assert code == 2` | ✓ | ✓ | ✓ | ✓ |
| M4 | `monitor._vault` back to the env-only original | `test_vault_reads_brain_home_dotenv_when_nothing_is_exported`, `test_snapshot_reports_the_dotenv_vault` | `assert _vault() ==` / `assert f"vault={…}"` | ✓ | ✓ | ✓ | ✓ |
| M5 | `brain-status.sh` back to `${BRAIN_VAULT_PATH:-$HOME/brain-vault}` | `test_brain_status_inspects_…`, `test_brain_status_still_reports_daemons_…[fails]`, `[prints-nothing]` | `assert f"  vault     {vault}…"` / `assert "  vault     ⚠️  unresolved"` | ✓ | ✓ | ✓ | ✓ |
| M6 | `brain-status.sh` `exit 1` after the unresolved row | `test_brain_status_still_reports_daemons_…[fails]`, `[prints-nothing]` | `assert result.returncode == 0` | ✓ | ✓ | ✓ | ✓ |
| M7 | `_brain-watcher-fg` resolver replaced by `$HOME/brain-vault` | `test_brain_watcher_fg_resolves_…`, `test_brain_watcher_fg_aborts_…[fails]`, `[prints-nothing]` | `assert _read_log(` / `assert result.returncode != 0` | ✓ | ✓ | ✓ | ✓ |
| M8 | `_brain-watcher-fg` abort branch loses its `exit 1` | `test_brain_watcher_fg_aborts_…[fails]`, `[prints-nothing]` | `assert result.returncode != 0` | ✓ | ✓ | ✓ | ✓ |
| M9 | resolver default bound as `Path.home() / "brain-vault"` | `test_main_prints_the_default_when_nothing_is_configured` | `assert capsys.readouterr().out ==` | ✓ | ✓ | ✓ | ✓ |
| M10 | `main` drops the usage line on stderr | `test_main_rejects_a_wrong_argument_count[argv0]`, `[argv1]` | `assert "usage: python -m brain.install_config BRAIN_HOME" in captured.err` | ✓ | ✓ | ✓ | ✓ |

| M11 | `brain-status.sh` drops `&& [[ -n "$VAULT" ]]` | `test_brain_status_still_reports_daemons_…[prints-nothing]` | `assert "  vault     ⚠️  unresolved"` | ✓ | ✓ | ✓ | ✓ |
| M12 | `_brain-watcher-fg` drops `\|\| [[ -z "$VAULT" ]]` | `test_brain_watcher_fg_aborts_…[prints-nothing]` | `assert result.returncode != 0` | ✓ | ✓ | ✓ | ✓ |
| M13 | `main` stops expanding its `BRAIN_HOME` argument | `test_main_expands_a_tilde_brain_home` | `assert capsys.readouterr().out ==` | ✓ | ✓ | ✓ | ✓ |

Every one of the seventeen new test cases is red under at least one mutation.
M11 and M12 are red ONLY for `[prints-nothing]`: an interpreter that fails is
already caught by the exit status, so the empty-output guard is load-bearing
exactly for the exit-0-and-silent case.

**Mutated copy, stated:** the shell tests run the dev copy `bin/_brain-watcher-fg`,
so M7/M8 mutate that file; its template twin is held identical by
`tests/test_packaging_templates.py`. `brain-status` runs the package-data
template (editable install → `ensure_shim` copies it into a tmp `$BRAIN_HOME`),
so M5/M6 mutate the template.

**Previously unpinned, now pinned:** the `.expanduser()` on `main`'s
`BRAIN_HOME` argument (M13) and both empty-output guards (M11, M12) — review
round on `f46e594`.
