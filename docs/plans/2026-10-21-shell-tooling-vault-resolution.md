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
| `main` prints the `$BRAIN_HOME/.env` vault, expands a `~` `BRAIN_HOME`, prints the default; bad argv → 2; runs as `python -m` | `tests/test_install_config.py` | the shell entry point |
| `brain setup --vault ~/x` writes an absolute path (fresh render + append to existing `.env`) | `tests/test_setup_vault_tilde.py` | fix 2 at the write boundary |
| `brain-monitor` reads the vault from `$BRAIN_HOME/.env`, expanded | `tests/test_bin_monitor.py` | `_vault` |
| `brain-status` inspects the `$BRAIN_HOME/.env` vault with no env export; keeps the daemon rows when the resolver fails or prints nothing | `tests/test_bin_scripts.py` | the shim |
| `_brain-watcher-fg` passes the `$BRAIN_HOME/.env` vault with no env export; aborts (no pid file, no `brain` call) when the resolver fails or prints nothing | `tests/test_bin_scripts.py` | the shim |

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

The repo's five-clause definition (memory `feedback_prove_the_check_can_fail`):
**(a)** the case passes on the unmutated source; **(b)** the mutation lands
exactly its declared count, replacing all occurrences; **(c)** the case then
goes red; **(d)** it fails AT the assertion it targets, with an
`AssertionError`, not in a helper; **(e)** every independent claim has its own
entry — one mutation per assertion — and any assertion no mutation reaches is
listed as unfired, with a reason.

*(Corrected after the audit of `f46e594`: the first two passes of this section
used a homegrown clause list whose fifth clause was "restored → green", so (e)
was never checked, and seven assertions had no mutation reaching them. The
restore check is kept below as hygiene, not as a clause.)*

**Method.** Run 2026-10-21 against `568ecf1`, sequentially in this worktree with
nothing else running in it. Each run executes all **seventeen** new test cases,
so (c) is an exact-set claim: the declared red set must equal the measured one.
(d) reads the JUnit failure: its message must be an `AssertionError` and the
first `>`-marked line must be the targeted assertion. (e) is computed: the
harness carries an inventory of every assertion each case makes (41), checks
each still exists in its test file, and fails if any is neither fired nor
listed as unfired. After each mutation the file is restored with `git checkout`
and the tree must be clean with all seventeen green (hygiene). The harness is
reproduced at the end of this file.

`[fails]` / `[prints-nothing]` are the two parametrizations of the
unresolved-vault tests: a stub interpreter that exits 1, and one that exits 0
and prints nothing (the case the empty-output guards exist for).

Result: **(a)** 17/17. **(b)–(d)** OK for all 26 mutations. **(e)** 41/41
assertions fired, 0 unfired. Restored-green OK after every mutation.

| # | Mutation | Red set (exact) → assertion fired |
|---|---|---|
| M1 | `install_config`: explicit leg `return override` | `ic_tilde` → `resolved ==`; `st_fresh`, `st_append` → `_vault_lines(…) ==` |
| M2 | `main` prints `_config.DEFAULT_VAULT_PATH` | `ic_main_dotenv`, `ic_tilde_home` → `out ==`; `ic_subproc` → `stdout ==`; `bs_status` → vault row; `bs_watch` → `brain.calls == [...]` |
| M3 | `main` argc check `if not args:` | `ic_argc2` → `code == 2` |
| M4 | `monitor._vault` env-only (the original bug) | `mon_vault` → `_vault() ==`; `mon_snap` → `vault=…` banner |
| M5 | `brain-status.sh` env-only (the original bug) | `bs_status` → vault row; `bs_unres[both]` → `unresolved` row |
| M6 | `brain-status.sh` `exit 1` after the unresolved row | `bs_unres[both]` → `returncode == 0` |
| M7 | `_brain-watcher-fg` resolver replaced by `$HOME/brain-vault` | `bs_watch` → `brain.calls == [...]`; `bs_abort[both]` → `returncode != 0` |
| M8 | `_brain-watcher-fg` abort branch loses `exit 1` | `bs_abort[both]` → `returncode != 0` |
| M9 | resolver default `Path.home() / "brain-vault"` | `ic_main_default` → `out ==` |
| M10 | usage line dropped | `ic_argc[both]` → `usage … in captured.err` |
| M11 | `brain-status.sh` drops `&& [[ -n "$VAULT" ]]` | `bs_unres[prints-nothing]` only → `unresolved` row |
| M12 | `_brain-watcher-fg` drops `\|\| [[ -z "$VAULT" ]]` | `bs_abort[prints-nothing]` only → `returncode != 0` |
| M13 | `main` stops expanding its `BRAIN_HOME` argument | `ic_tilde_home` → `out ==` |
| M14 | `main` returns 1 on success | `ic_main_dotenv`, `ic_tilde_home`, `ic_main_default` → `code == 0`; `ic_subproc` → `returncode == 0`; `bs_status` → vault row; `bs_watch` → `returncode == 0` |
| M15 | usage error returns 1, not 2 | `ic_argc[both]` → `code == 2` |
| M16 | usage line printed to stdout | `ic_argc[both]` → `captured.out == ""` |
| M17 | `__main__` exits `main() + 1` | `ic_subproc` → `returncode == 0`; `bs_status` → vault row; `bs_watch` → `returncode == 0` |
| M18 | `_snapshot` returns 1 | `mon_snap` → `main([]) == 0` |
| M19 | `brain-status.sh` `exit 1` after the vault row | `bs_status` → `returncode == 0` |
| M20 | `brain-status.sh` reads `current/` from `$HOME/brain-vault` | `bs_status` → `build-id` line |
| M21 | `_brain-watcher-fg` `exit 1` after resolving | `bs_watch` → `returncode == 0` |
| M22 | `_brain-watcher-fg` abort message reworded | `bs_abort[both]` → `could not resolve the vault in stderr` |
| M23 | `_brain-watcher-fg` resolves AFTER writing the pid file (audit F2-i) | `bs_abort[both]` → `not watch_pid.exists()` only |
| M24 | `_brain-watcher-fg` runs the watcher before aborting (synthetic) | `bs_abort[both]` → `brain.calls == []` |
| M25 | `brain-status.sh` `current` block moved outside `if VAULT=…` (audit F2-ii) | `bs_unres[both]` → `"  current " not in stdout` |
| M26 | `brain-status.sh` resolves before the daemon rows and stops (`exit 0`) when unresolved | `bs_unres[both]` → `"watcher:" in stdout` |

**Read with care:**
- **M14 and M17 redden the shell cases too, by design.** The shims consume the
  resolver's exit status, so a non-zero exit IS "unresolved": `brain-status`
  prints the unresolved row (its vault-row assertion fires), the wrapper aborts
  (its `returncode == 0` fires). The first harness run under-declared both red
  sets and failed (c); the sets were corrected, not the mutations.
- **M24 is synthetic.** No plausible single edit makes the wrapper both print
  the abort message, exit non-zero and invoke `brain`: the real route to a
  `brain` call is the `exec`, which replaces the process, so its exit status
  is the stub's 0 and `returncode != 0` fires first (M8). M24 inserts a call
  before the `exit` to show the `brain.calls == []` assertion can fire; it
  stays as a diagnostic witness, not as the guard for a realistic regression.
- **M11 and M12 are red ONLY for `[prints-nothing]`.** A failing interpreter is
  already caught by the exit status; the empty-output guard is load-bearing
  exactly for an interpreter that exits 0 and says nothing.
- **Mutated copies.** The shell tests run the dev copy `bin/_brain-watcher-fg`,
  so its mutations target that file (its template twin is held identical by
  `tests/test_packaging_templates.py`). `brain-status` runs the package-data
  template (editable install → `ensure_shim` copies it into a tmp
  `$BRAIN_HOME`), so its mutations target the template.
- **`main`'s `BRAIN_HOME` `expanduser` IS load-bearing** (M13). An earlier
  draft of this section said "the shims always pass an absolute path"; that was
  false — both shims pass `${BRAIN_HOME:-…}` verbatim, so
  `export BRAIN_HOME="~/.brain"` reaches `main` as a literal `~`.

## Appendix — the harness

Run from the worktree root with `TEST_DATABASE_URL` pointing at a sibling test
DB, on a clean, committed tree (it restores with `git checkout`):
`.venv/bin/python harness.py <scratch-dir>`.

```python
"""Five-clause mutation harness (repo definition (a)-(e)) for the vault-resolution tests.

(a) every case passes unmutated; (b) every mutation pair lands exactly its
declared count, replacing all occurrences; (c) the declared cases go red, and
ONLY those; (d) each red case fails with an AssertionError AT its targeted
assertion line; (e) every assertion in the inventory is fired by at least one
mutation, and any unfired one is listed with a reason. Restored -> clean tree
and all green is checked as hygiene after each mutation.
"""
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

WT = Path.cwd()
OUT = Path(sys.argv[1])
IC = "tests/test_install_config.py"
ST = "tests/test_setup_vault_tilde.py"
MON = "tests/test_bin_monitor.py"
BS = "tests/test_bin_scripts.py"
UNRES = "test_brain_status_still_reports_daemons_when_the_vault_cannot_be_resolved"
ABORT = "test_brain_watcher_fg_aborts_when_the_vault_cannot_be_resolved"
TESTS = {
    "ic_tilde": f"{IC}::test_an_explicit_tilde_override_is_expanded",
    "ic_main_dotenv": f"{IC}::test_main_prints_the_brain_home_dotenv_vault",
    "ic_tilde_home": f"{IC}::test_main_expands_a_tilde_brain_home",
    "ic_main_default": f"{IC}::test_main_prints_the_default_when_nothing_is_configured",
    "ic_argc0": f"{IC}::test_main_rejects_a_wrong_argument_count[argv0]",
    "ic_argc2": f"{IC}::test_main_rejects_a_wrong_argument_count[argv1]",
    "ic_subproc": f"{IC}::test_module_entry_point_runs_as_a_subprocess",
    "st_fresh": f"{ST}::test_a_quoted_tilde_vault_is_rendered_absolute_into_a_fresh_env",
    "st_append": f"{ST}::test_a_quoted_tilde_vault_is_appended_absolute_to_an_existing_env",
    "mon_vault": f"{MON}::test_vault_reads_brain_home_dotenv_when_nothing_is_exported",
    "mon_snap": f"{MON}::test_snapshot_reports_the_dotenv_vault",
    "bs_status": f"{BS}::test_brain_status_inspects_the_brain_home_dotenv_vault",
    "bs_unres_fails": f"{BS}::{UNRES}[fails]",
    "bs_unres_empty": f"{BS}::{UNRES}[prints-nothing]",
    "bs_watch": f"{BS}::test_brain_watcher_fg_resolves_the_brain_home_dotenv_vault_when_unexported",
    "bs_abort_fails": f"{BS}::{ABORT}[fails]",
    "bs_abort_empty": f"{BS}::{ABORT}[prints-nothing]",
}
# (e): every assertion each case makes, as its source snippet.
A_UNRES = ["assert result.returncode == 0", 'assert "watcher:" in result.stdout',
           'assert "  vault     ⚠️  unresolved" in result.stdout',
           'assert "  current " not in result.stdout']
A_ABORT = ["assert result.returncode != 0",
           'assert "could not resolve the vault" in result.stderr',
           'assert _read_log(stub_dir / "brain.calls") == []',
           'assert not hermetic_pids.paths["watch_pid"].exists()']
A_ARGC = ["assert code == 2", 'assert captured.out == ""',
          'assert "usage: python -m brain.install_config BRAIN_HOME" in captured.err']
INVENTORY = {
    "ic_tilde": ["assert resolved =="],
    "ic_main_dotenv": ["assert code == 0", "assert capsys.readouterr().out =="],
    "ic_tilde_home": ["assert code == 0", "assert capsys.readouterr().out =="],
    "ic_main_default": ["assert code == 0", "assert capsys.readouterr().out =="],
    "ic_argc0": A_ARGC, "ic_argc2": A_ARGC,
    "ic_subproc": ["assert result.returncode == 0", "assert result.stdout =="],
    "st_fresh": ["assert _vault_lines("], "st_append": ["assert _vault_lines("],
    "mon_vault": ["assert _vault() =="],
    "mon_snap": ["assert main([]) == 0", 'assert f"vault={dotenv_only_vault}'],
    "bs_status": ["assert result.returncode == 0", 'assert f"  vault     {vault}',
                  'assert f"  build-id  {build_dir.name}'],
    "bs_unres_fails": A_UNRES, "bs_unres_empty": A_UNRES,
    "bs_watch": ["assert result.returncode == 0", "assert _read_log(stub_dir / \"brain.calls\") == ["],
    "bs_abort_fails": A_ABORT, "bs_abort_empty": A_ABORT,
}
UNFIRED: dict[tuple[str, str], str] = {}

CFG = "src/brain/install_config.py"
MONF = "src/brain/bin/monitor.py"
STATUS = "src/brain/templates/bin/brain-status.sh"
WATCH = "bin/_brain-watcher-fg"
UNRES_PRINTF = "    printf '  vault     ⚠️  unresolved — %s -m brain.install_config failed\\n' \"$PY\"\n"


def between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def status_cur(t: str) -> str:
    return between(t, "    # Current build pointer", "else\n" + UNRES_PRINTF)


def status_if(t: str) -> str:
    return between(t, 'if VAULT="$("$PY" -m brain.install_config', "\n# Quick health check")


def watch_res(t: str) -> str:
    return between(t, 'if [[ -n "${BRAIN_VAULT_PATH:-}" ]]; then\n', "\n# Cap oversized")


# Each mutation: (label, file, pairs(text) -> [(old, new, declared_count)], {case: snippet}).
M = [
    ("M1 explicit leg not expanded", CFG,
     lambda t: [("        return override.expanduser()\n", "        return override\n", 1)],
     {"ic_tilde": "assert resolved ==", "st_fresh": "assert _vault_lines(",
      "st_append": "assert _vault_lines("}),
    ("M2 entry point ignores the resolver", CFG,
     lambda t: [("    print(resolve_install_vault(Path(args[0]).expanduser()))\n",
                 "    print(_config.DEFAULT_VAULT_PATH)\n", 1)],
     {"ic_main_dotenv": "assert capsys.readouterr().out ==",
      "ic_tilde_home": "assert capsys.readouterr().out ==",
      "ic_subproc": "assert result.stdout ==", "bs_status": 'assert f"  vault     {vault}',
      "bs_watch": 'assert _read_log(stub_dir / "brain.calls") == ['}),
    ("M3 argc check only rejects zero args", CFG,
     lambda t: [("    if len(args) != 1:\n", "    if not args:\n", 1)],
     {"ic_argc2": "assert code == 2"}),
    ("M4 monitor back to env-only", MONF,
     lambda t: [("    return resolve_install_vault(_brain_home_root())\n",
                 '    return Path(os.environ.get("BRAIN_VAULT_PATH", str(Path.home() / "brain-vault")))\n', 1)],
     {"mon_vault": "assert _vault() ==", "mon_snap": 'assert f"vault={dotenv_only_vault}'}),
    ("M5 brain-status back to env-only", STATUS,
     lambda t: [('if VAULT="$("$PY" -m brain.install_config "$BRAIN_HOME_RESOLVED")" && [[ -n "$VAULT" ]]; then\n',
                 'if VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}" && [[ -n "$VAULT" ]]; then\n', 1)],
     {"bs_status": 'assert f"  vault     {vault}',
      "bs_unres_fails": 'assert "  vault     ⚠️  unresolved" in result.stdout',
      "bs_unres_empty": 'assert "  vault     ⚠️  unresolved" in result.stdout'}),
    ("M6 brain-status exits non-zero when unresolved", STATUS,
     lambda t: [(UNRES_PRINTF, UNRES_PRINTF + "    exit 1\n", 1)],
     {"bs_unres_fails": "assert result.returncode == 0",
      "bs_unres_empty": "assert result.returncode == 0"}),
    ("M7 watcher-fg guesses the default", WATCH,
     lambda t: [('elif ! VAULT="$("${BRAIN_PY:-python3}" -m brain.install_config "${BRAIN_HOME:-$PROJECT_ROOT}")" \\\n',
                 'elif ! VAULT="$HOME/brain-vault" \\\n', 1)],
     {"bs_watch": 'assert _read_log(stub_dir / "brain.calls") == [',
      "bs_abort_fails": "assert result.returncode != 0",
      "bs_abort_empty": "assert result.returncode != 0"}),
    ("M8 watcher-fg abort branch loses its exit", WATCH,
     lambda t: [("    exit 1\nfi\n\n# Cap oversized", "fi\n\n# Cap oversized", 1)],
     {"bs_abort_fails": "assert result.returncode != 0",
      "bs_abort_empty": "assert result.returncode != 0"}),
    ("M9 default bound as Path.home()", CFG,
     lambda t: [("    return Path(raw).expanduser() if raw else _config.DEFAULT_VAULT_PATH\n",
                 '    return Path(raw).expanduser() if raw else Path.home() / "brain-vault"\n', 1)],
     {"ic_main_default": "assert capsys.readouterr().out =="}),
    ("M10 usage line dropped", CFG,
     lambda t: [("        print(_USAGE, file=sys.stderr)\n        return 2\n", "        return 2\n", 1)],
     {"ic_argc0": 'assert "usage: python -m brain.install_config BRAIN_HOME" in captured.err',
      "ic_argc2": 'assert "usage: python -m brain.install_config BRAIN_HOME" in captured.err'}),
    ("M11 brain-status drops its empty-output guard", STATUS,
     lambda t: [(' && [[ -n "$VAULT" ]]; then\n', "; then\n", 1)],
     {"bs_unres_empty": 'assert "  vault     ⚠️  unresolved" in result.stdout'}),
    ("M12 watcher-fg drops its empty-output guard", WATCH,
     lambda t: [(')" \\\n        || [[ -z "$VAULT" ]]; then\n', ')"; then\n', 1)],
     {"bs_abort_empty": "assert result.returncode != 0"}),
    ("M13 main does not expand BRAIN_HOME", CFG,
     lambda t: [("    print(resolve_install_vault(Path(args[0]).expanduser()))\n",
                 "    print(resolve_install_vault(Path(args[0])))\n", 1)],
     {"ic_tilde_home": "assert capsys.readouterr().out =="}),
    ("M14 main reports failure on success", CFG,
     lambda t: [("    return 0\n", "    return 1\n", 1)],
     {"ic_main_dotenv": "assert code == 0", "ic_tilde_home": "assert code == 0",
      "ic_main_default": "assert code == 0", "ic_subproc": "assert result.returncode == 0",
      # The shims consume the exit status: non-zero means "unresolved".
      "bs_status": 'assert f"  vault     {vault}', "bs_watch": "assert result.returncode == 0"}),
    ("M15 usage error exits 1, not 2", CFG,
     lambda t: [("        return 2\n", "        return 1\n", 1)],
     {"ic_argc0": "assert code == 2", "ic_argc2": "assert code == 2"}),
    ("M16 usage line goes to stdout", CFG,
     lambda t: [("        print(_USAGE, file=sys.stderr)\n", "        print(_USAGE)\n", 1)],
     {"ic_argc0": 'assert captured.out == ""', "ic_argc2": 'assert captured.out == ""'}),
    ("M17 module entry point exits non-zero", CFG,
     lambda t: [("    raise SystemExit(main())\n", "    raise SystemExit(main() + 1)\n", 1)],
     {"ic_subproc": "assert result.returncode == 0",
      "bs_status": 'assert f"  vault     {vault}', "bs_watch": "assert result.returncode == 0"}),
    ("M18 snapshot returns non-zero", MONF,
     lambda t: [('        print("    (empty)")\n\n    return 0\n', '        print("    (empty)")\n\n    return 1\n', 1)],
     {"mon_snap": "assert main([]) == 0"}),
    ("M19 brain-status exits non-zero after the vault row", STATUS,
     lambda t: [("    printf '  vault     %s\\n' \"$VAULT\"\n",
                 "    printf '  vault     %s\\n' \"$VAULT\"\n    exit 1\n", 1)],
     {"bs_status": "assert result.returncode == 0"}),
    ("M20 brain-status reads current/ from the default vault", STATUS,
     lambda t: [('    CURRENT="$VAULT/.quartz/current"\n', '    CURRENT="$HOME/brain-vault/.quartz/current"\n', 1)],
     {"bs_status": 'assert f"  build-id  {build_dir.name}'}),
    ("M21 watcher-fg exits non-zero after resolving", WATCH,
     lambda t: [("fi\n\n# Cap oversized", "fi\nexit 1\n\n# Cap oversized", 1)],
     {"bs_watch": "assert result.returncode == 0"}),
    ("M22 watcher-fg abort message reworded", WATCH,
     lambda t: [('    echo "could not resolve the vault (', '    echo "vault lookup failed (', 1)],
     {"bs_abort_fails": 'assert "could not resolve the vault" in result.stderr',
      "bs_abort_empty": 'assert "could not resolve the vault" in result.stderr'}),
    ("M23 watcher-fg resolves the vault AFTER writing the pid file", WATCH,
     lambda t: [(watch_res(t) + "\n", "", 1),
                ('echo "$$" >"$WATCH_PID"\n', 'echo "$$" >"$WATCH_PID"\n' + watch_res(t), 1)],
     {"bs_abort_fails": 'assert not hermetic_pids.paths["watch_pid"].exists()',
      "bs_abort_empty": 'assert not hermetic_pids.paths["watch_pid"].exists()'}),
    ("M24 watcher-fg runs the watcher before aborting (synthetic)", WATCH,
     lambda t: [("    exit 1\nfi\n\n# Cap oversized",
                 '    brain vault sync --watch --vault "$VAULT" || true\n    exit 1\nfi\n\n# Cap oversized', 1)],
     {"bs_abort_fails": 'assert _read_log(stub_dir / "brain.calls") == []',
      "bs_abort_empty": 'assert _read_log(stub_dir / "brain.calls") == []'}),
    ("M25 brain-status current block moved outside the vault check", STATUS,
     lambda t: [(status_cur(t), "", 1),
                (UNRES_PRINTF + "fi\n", UNRES_PRINTF + "fi\n" + status_cur(t), 1)],
     {"bs_unres_fails": 'assert "  current " not in result.stdout',
      "bs_unres_empty": 'assert "  current " not in result.stdout'}),
    ("M26 brain-status resolves before the daemon rows and stops when unresolved", STATUS,
     lambda t: [(status_if(t), "", 1),
                ("echo '🧠 brain status'\n",
                 status_if(t).replace(UNRES_PRINTF, UNRES_PRINTF + "    exit 0\n")
                 + "\necho '🧠 brain status'\n", 1)],
     {"bs_unres_fails": 'assert "watcher:" in result.stdout',
      "bs_unres_empty": 'assert "watcher:" in result.stdout'}),
]


def run(tag: str) -> dict[str, tuple[str, str] | None]:
    xml = OUT / f"{tag}.xml"
    subprocess.run([".venv/bin/pytest", "-q", "--no-cov", "--tb=long", "-p", "no:cacheprovider",
                    f"--junitxml={xml}", *TESTS.values()], capture_output=True, text=True, check=False)
    res: dict[str, tuple[str, str] | None] = {}
    for tc in ET.parse(xml).iter("testcase"):
        key = next(k for k, v in TESTS.items() if v.endswith("::" + tc.get("name", "")))
        fail = tc.find("failure")
        if fail is None:
            fail = tc.find("error")
        res[key] = None if fail is None else (fail.get("message", ""), fail.text or "")
    assert set(res) == set(TESTS), set(TESTS) ^ set(res)
    return res


def failing_line(text: str) -> str:
    lines = [ln[1:].strip() for ln in text.splitlines() if ln.startswith(">")]
    return lines[0] if lines else ""


def clean() -> bool:
    return subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip() == ""


ok = True
# (e) precondition: every inventory snippet still exists in its test file.
for key, snippets in INVENTORY.items():
    src = (WT / TESTS[key].split("::")[0]).read_text()
    for sn in snippets:
        if sn not in src:
            print(f"inventory snippet missing from source: {key}: {sn}")
            ok = False
for label, _, _, targets in M:
    for k, sn in targets.items():
        if sn not in INVENTORY[k]:
            print(f"target not in inventory: {label}: {k}: {sn}")
            ok = False
base = run("baseline")
a = all(v is None for v in base.values())
print(f"(a) baseline {sum(v is None for v in base.values())}/{len(base)} pass: {'OK' if a else 'FAIL'}")
ok &= a
fired: set[tuple[str, str]] = set()
for label, path, pairs_of, targets in M:
    assert clean(), "dirty tree before mutation"
    p = WT / path
    text = p.read_text()
    b = True
    for old, new, n in pairs_of(text):
        if text.count(old) != n:
            b = False
        text = text.replace(old, new)
    p.write_text(text)
    b = b and not clean()
    res = run(label.split()[0])
    red = {k for k, v in res.items() if v is not None}
    c = red == set(targets)
    d = True
    for k in targets:
        if res.get(k) is None:
            d = False
            continue
        msg, tb = res[k]
        line = failing_line(tb)
        hit = msg.startswith(("assert", "AssertionError")) and targets[k] in line
        d &= hit
        if hit:
            fired.add((k, targets[k]))
        else:
            print(f"    (d) miss {k}: msg={msg[:60]!r} line={line[:90]!r}")
    subprocess.run(["git", "checkout", "--", path], check=True)
    after = run(label.split()[0] + "-restored")
    hyg = clean() and all(v is None for v in after.values())
    ok &= b and c and d and hyg
    print(f"{label}: (b) {'OK' if b else 'FAIL'} (c) {'OK' if c else 'FAIL'} red={sorted(red)} "
          f"(d) {'OK' if d else 'FAIL'} restored-green {'OK' if hyg else 'FAIL'}")
    if not c:
        print(f"    expected={sorted(targets)}")
inventory = {(k, sn) for k, sns in INVENTORY.items() for sn in sns}
unfired = inventory - fired - set(UNFIRED)
stale_exempt = set(UNFIRED) & fired
e = not unfired and not stale_exempt
print(f"(e) {len(fired & inventory)}/{len(inventory)} assertions fired, {len(UNFIRED)} exempt: {'OK' if e else 'FAIL'}")
for k, sn in sorted(unfired):
    print(f"    unfired: {k}: {sn}")
ok &= e
print("ALL OK" if ok else "SOME FAILED")
```
