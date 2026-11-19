# `--help` runs nothing on the daemon scripts; clean reranker error (F18, F37, F19, F20)

Branch `fix/shims-help`, PR-B of the 0.4.0 QA-fix program. Source of truth: the QA report's F18
(MED-HIGH), F37, F19 and F20; evidence rows C38, C39, C40, C44 and B37.

## 1. Findings and root causes

### F18 / F37: the daemon scripts ignore argv, so `--help` runs them

| Entry point | Code path | What `--help` did (evidence) |
|---|---|---|
| `brain-up` | `src/brain/bin/up.py:8` `exec_shim("brain-up", sys.argv[1:])` → `_launcher.py:104-107` `ensure_shim()` (writes `$BRAIN_HOME/.shims/brain-up`) → `execvpe` of `templates/bin/brain-up.sh`, which never reads `$@` | ran `brain-install-launchd`: bootout + bootstrap of every agent (C39) |
| `brain-down` | `down.py:8` → same launcher → `brain-down.sh` (no `$@` handling) | `launchctl print`/`bootout`, pid-file kills, `pkill -f` sweeps (C40) |
| `brain-status` | `status.py:8` → same launcher → `brain-status.sh` (no `$@` handling) | installed the shim, ran the status report (B37 = F37) |
| `brain-uninstall-launchd` | `launchd.py:490` `uninstall_main()` takes no argv; never parses `sys.argv` | booted out and deleted every `com.brain.*` plist (C38) |

Root cause: `exec_shim` forwards argv to a shell script that ignores it, and `uninstall_main`
never builds a parser. `_launcher.exec_shim` also installs or rewrites the shim before anything
looks at argv, so even a correct shell-side guard would leave a shim write behind.

The other entry points checked, already correct (they become guard tests, see §4):

- `brain-install-launchd` (`launchd.py:447`): `argparse` runs before anything else.
- `brain-rebuild` (`bin/rebuild.py` → `maintenance.main` → `_parse_args` first, `maintenance.py:310`).
- `brain-monitor` (`monitor.py:278`): `parse_known_args` first, and leftovers are rejected with exit 2.
- The dev-checkout `bin/brain-*` wrappers only `exec "$SCRIPT_DIR/../.venv/bin/brain-<name>" "$@"`,
  so they inherit whatever the console script does. No change needed.
- `bin/brain-ci`, `bin/brain-skills-sync` and `bin/*-gif` already have `usage()` +
  `case … -h|--help) … *) exit 2`. This is the repo's shell idiom, and the fix reuses it.

These shell scripts, run by hand, still act on `--help` (the console-script fix does not reach them):

- `src/brain/templates/bin/brain-up.sh`, `brain-down.sh`, `brain-status.sh` (installed into
  `$BRAIN_HOME/.shims/`).
- `src/brain/templates/bin/_brain-watcher-fg.sh` (writes the pid file, execs the watcher),
  `_brain-brief-fg.sh` (dedup scan + brief), `_brain-dropfolders-fg.sh` (ingest-folder). launchd
  runs these with no arguments: every plist's `ProgramArguments` is the shim path alone
  (`templates/launchd/*.plist.j2`).
- `bin/_brain-watcher-fg` (a byte-identical dev copy of the template, pinned by
  `tests/test_packaging_templates.py::test_fg_helpers_match_packaged_templates`).

### F20: `bin/gws-reauth`

`bin/gws-reauth:39` runs `rm -f "${HOME}/.config/gws/token_cache.json"` before it looks at
anything, then `exec gws auth login --scopes … "$@"`. So `gws-reauth --help` deletes the token
cache first, then hands `--help` to gws.

### F19: `brain-install-launchd --with-reranker` with a missing GGUF prints a traceback

`install_plists` → `_reranker_inputs` (`launchd.py:181-188`) raises `RerankerConfigError` (a
`LaunchdError`). `install_main` (`launchd.py:480`) does not catch it, so the console script prints
a raw traceback and exits 1 (C38). The message is already safe: it names `BRAIN_RERANK_GGUF` and
never its value. The other `RerankerConfigError` causes hit the same unhandled path: a relative
`BRAIN_RERANK_GGUF` (`launchd.py:168`) and a bad `BRAIN_RERANK_URL` (`launchd.py:209`). So do the
other `LaunchdError`s, a failed `launchctl bootstrap` and a bad render.

## 2. Behaviour change

### One shared Python guard (DRY)

Add to `src/brain/bin/_launcher.py`:

```python
def parse_no_args(prog: str, description: str, argv: Sequence[str]) -> None:
    """Accept only -h/--help, before any action: argparse prints usage and exits 0 for
    -h/--help, and exits 2 ("unrecognized arguments") for anything else."""
```

It is an `argparse.ArgumentParser(prog=prog, description=description)` with no arguments and
`parse_args(list(argv))`. Callers:

- `up.py`, `down.py`, `status.py`: `main()` becomes `parse_no_args(<name>, _DESCRIPTION, sys.argv[1:])`
  and then `exec_shim(<name>, [])`. The guard runs before `exec_shim`, so before `_brain_home_root()`,
  `ensure_shim()` and the exec. `exec_shim` keeps its contract (install, then exec with the given
  args). Its unit tests stay valid.
- `launchd.py`: `uninstall_main(argv: Sequence[str] | None = None)` calls
  `parse_no_args("brain-uninstall-launchd", …, sys.argv[1:] if argv is None else argv)` before
  `resolve_launchctl()`. This mirrors `install_main`'s `argv` seam.
- `src/brain/uninstall.py:121`: `uninstall_main()` → `uninstall_main([])`. `brain uninstall` calls it
  in-process, where `sys.argv[1:]` is `["uninstall", "--yes", …]`, so without the explicit `[]` the
  new parser would `SystemExit(2)` in the middle of `brain uninstall`. `except Exception` does not
  catch `SystemExit`. This is a one-line change in a file PR-A (F16) also edits. PR-A's hunks are
  at lines 73 and 167, so the two should merge cleanly. See risks.

Descriptions (the help text):

- brain-up: "Start the brain vault-sync watcher and the daily-brief job under launchd (runs
  brain-install-launchd). Idempotent. Takes no arguments."
- brain-down: "Stop the brain vault-sync watcher: boot out its LaunchAgents and stop the processes
  in its pid files (and legacy wiki leftovers). The plists stay. Idempotent. Takes no arguments."
- brain-status: "Show whether the brain vault-sync watcher is running, and the vault it watches.
  Read-only. Takes no arguments."
- brain-uninstall-launchd: "Boot out and delete every brain LaunchAgent (com.brain.*). Idempotent.
  Takes no arguments."

### Shell guard, inline in each script (the repo's existing idiom)

Each of the 6 templates, plus `bin/_brain-watcher-fg` (re-copied byte-identical), gets this block
right after `set -euo pipefail`, before `SCRIPT_DIR` and any other line:

```bash
# Arguments (F18): none. -h/--help prints usage and exits 0 before ANY action;
# anything else is a usage error (exit 2), never a silent run.
usage() {
  echo "Usage: brain-down [-h|--help]  — <one line>. Takes no arguments."
}
case "$#:${1:-}" in
  0:) ;;
  1:-h|1:--help) usage; exit 0 ;;
  *) echo "error: unknown argument(s): $*" >&2; usage >&2; exit 2 ;;
esac
```

This is the same shape as `bin/brain-demo-gif` and the other `*-gif` scripts. It also checks `$#`,
so `x "" extra` cannot slip through as `""` the way `case "${1:-}"` alone lets it. It works on
macOS `/bin/bash` 3.2.

Why the guard is inline and not a sourced helper: each script is installed on its own by
`ensure_shim(name)` and run on its own by launchd. A sourced `_brain-args.sh` would need its own
shim-install step in every launcher, a dev-checkout twin and a parity test. It would also add a
runtime failure mode (a missing or stale helper under launchd) to the daemons' start path, and all
of that to save about 8 lines. A glob-parametrized conformance test (§4 C/D) runs every
`templates/bin/*.sh`, so a new template without the guard fails the suite. That is the drift
protection.

Exit 2 for other arguments, script by script:

- `brain-up` / `brain-down` / `brain-status` / `brain-uninstall-launchd`: none accept an argument
  today. The `.sh` ignored whatever the launcher forwarded, and `uninstall_main` never parsed.
  `brain-up.sh` calls `brain-install-launchd` without arguments, and nothing in `src/` or the docs
  passes one. An argument is therefore always a mistake, and running anyway is the bug.
- The `_brain-*-fg` wrappers: launchd passes none (`ProgramArguments` = the shim path only).
  `setup.py` and `launchd.py` only install them and never run them.
- `BRAIN_PY` / the test stubs are env vars, not argv, so the guard does not touch them.
  `tests/test_bin_scripts.py` runs every script with no arguments.

### `bin/gws-reauth` (F20)

Same idiom. Arguments for `gws auth login` now go after `--`:

```bash
usage() { echo "Usage: gws-reauth [-h|--help] [-- <gws auth login args>]  — delete gws's cached access token (~/.config/gws/token_cache.json) and re-run gws auth login with the full second-brain scope set; arguments after -- go to gws auth login."; }
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --) shift ;;
  "") if (($#)); then <error>; exit 2; fi ;;
  *) echo "error: unknown argument: $1 (pass gws auth login arguments after --)" >&2; usage >&2; exit 2 ;;
esac
```

All of this runs before the `rm`. The trade-off: `gws-reauth --some-gws-flag` used to reach gws
and now exits 2. I accept that. The script's first action is destructive, so an unrecognized token
(a typo, `-help`, `help`) must never reach the `rm`. `--` keeps pass-through explicit. Nothing in
the repo or docs passes arguments to it. The docs only say "run `bin/gws-reauth`".

### F19: one clean line

In `install_main`:

```python
try:
    install_plists(...)
except LaunchdError as exc:
    print(f"brain-install-launchd: error: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
```

This catches `LaunchdError`, the module's own base class, which covers `RerankerConfigError`, a
failed bootstrap and a bad render. It does not catch `BrainError` broadly. A reranker config error
names the variable, never its value (a documented invariant in `launchd.py:156-159, 177-179,
197-199`). A bootstrap failure carries launchctl's own stderr, which holds no secret and is usually
two lines, so the handler collapses whitespace to print exactly one line (review round 1). Exit 1, not 2:
this is a config problem, not a usage problem. `brain-up.sh` already treats any nonzero exit as
"brain-install-launchd failed — daemons NOT supervised", so its behaviour does not change. The
`--with-reranker` path already fails closed before any shim, plist or launchctl call (inputs are
read at `launchd.py:314-315`, before `ensure_shim` at 336). The test pins that.

Not in scope (noted for fix-lead): a `ConfigError` from `resolve_install_vault` would still print a
traceback. It is a different finding class with no QA row.

## 3. Files

| File | Change |
|---|---|
| `src/brain/bin/_launcher.py` | + `parse_no_args`; module docstring names it |
| `src/brain/bin/up.py`, `down.py`, `status.py` | guard before `exec_shim(name, [])` |
| `src/brain/bin/launchd.py` (519 lines, under the ceiling) | `uninstall_main(argv=None)` guard; `install_main` catches `LaunchdError`; module docstring entry-point lines |
| `src/brain/uninstall.py` | `uninstall_main()` → `uninstall_main([])` (one line) |
| `src/brain/templates/bin/{brain-up,brain-down,brain-status,_brain-watcher-fg,_brain-brief-fg,_brain-dropfolders-fg}.sh` | inline guard |
| `bin/_brain-watcher-fg` | re-copied from the template (parity test) |
| `bin/gws-reauth` | guard + `--` pass-through |
| `tests/test_bin_help_guard.py` (new) | §4 A–E, H |
| `tests/test_bin_gws_reauth.py` (new) | §4 F |
| `tests/test_bin_launchd_reranker.py` (613 → ~680 lines) | §4 G |
| `tests/test_bin_launcher.py` | the two dispatch tests that encoded F18 (see §4) |
| `CHANGELOG.md` | §6 |

No file crosses 800 lines, and no `docs/ceiling-table.md` row changes. No pyproject entry-point
change. No migration.

## 4. Tests (all `nodb`, hermetic)

Shared harness (new file): `PATH=<tmp stubs>:/usr/bin:/bin` with recording stubs for `launchctl`,
`pkill`, `docker`, `brain`, `python` and `gws`. Each appends its argv to `<name>.calls`, and none
exists before the run. The env also sets `HOME=tmp`, `BRAIN_HOME=tmp/brain_home`,
`BRAIN_LAUNCHCTL` / `BRAIN_PKILL` to the stubs, `BRAIN_LAUNCHD_DIR=tmp/LaunchAgents` (seeded with a
decoy `com.brain.watcher.plist`), `BRAIN_INSTALL_LAUNCHD=<stub>`, `BRAIN_PY=<python stub>`,
`BRAIN_WATCH_PID` / `BRAIN_BUILD_PID` / `BRAIN_WIKI_PID` under tmp (the watch one holding the pid
of a live `/bin/sleep` the test owns), `BRAIN_VAULT_PATH=tmp/vault`, `BRAIN_SKIP_VENV_AUTOLOAD=1`
(so `bin/_brain-watcher-fg` cannot pick up the checkout's real `brain`), `BRAIN_IGNORE_CWD_DOTENV=1`,
and `DATABASE_URL` / `BRAIN_PROD_DATABASE_URL=postgresql://localhost:1/none`.

The "zero side effects" assertion is: no `*.calls` file anywhere; a snapshot of every path under
`tmp_path` (path, size, mtime_ns) is identical before and after, so no `.shims/` dir, no pid file
and an untouched decoy plist; and the `sleep` process is still alive.

Console scripts run through the dev-checkout `bin/<name>` wrapper, which goes through the console
script and the Python entry, the user's real path.

| # | Test | Proves | On master |
|---|---|---|---|
| A | `test_console_script_help_runs_nothing[name × {-h,--help}]`, name ∈ brain-up, brain-down, brain-status, brain-uninstall-launchd | exit 0, `usage: <name>` on stdout, zero side effects | RED: each runs (stub calls / `.shims` written / decoy plist deleted / sleep killed) |
| A' | same, name ∈ brain-install-launchd, brain-rebuild, brain-monitor | already correct; pinned so a refactor cannot regress | GREEN (guard; mutation-proved, §5) |
| B | `test_console_script_rejects_arguments[name × {--bogus, extra}]`, all 7 names | exit 2, `unrecognized arguments` on stderr, zero side effects | RED for the 4 F18 names; GREEN guard for the other 3 |
| C | `test_shell_script_help_runs_nothing[script × {-h,--help}]`: glob of `src/brain/templates/bin/*.sh` + `bin/_brain-watcher-fg`, run as `bash <path>` | exit 0, `usage: <script name>` (case-insensitive), zero side effects; a future template is covered automatically | RED for all 7 (e.g. watcher-fg writes the pid file and execs the stub `brain`; brief-fg runs the `python` stub) |
| D | `test_shell_script_rejects_arguments[script × {--bogus, extra}]` | exit 2, zero side effects | RED for all 7 |
| E | `test_brain_uninstall_launchd_step_ignores_brain_argv`: `run_uninstall(yes=True, _launchd_uninstall=None, …)` on `sys.platform="darwin"` with `sys.argv=["brain","uninstall","--yes"]`, `subprocess.run` patched to a recording no-op, `BRAIN_LAUNCHD_DIR` tmp | `brain uninstall`'s in-process call does not parse brain's own argv (no `SystemExit`; "launchd plists removed" printed) | GREEN guard against this PR's own regression (mutation: drop the `[]`) |
| F | `tests/test_bin_gws_reauth.py`: `HOME=tmp` with a decoy `~/.config/gws/token_cache.json`; a fake `gws` on PATH (never the real one) | `--help`/`-h`: exit 0, usage, decoy survives, gws never called. `--bogus` / `extra`: exit 2, decoy survives, gws never called. No args: decoy removed, gws argv == `auth login --scopes <csv>`. `-- --flag x`: decoy removed, gws argv ends `--flag x` with no literal `--` | help/bogus/extra/`--` RED; no-args GREEN baseline |
| G | `test_bin_launchd_reranker.py`: `test_install_main_reranker_config_error_is_one_line[case]` in-process, for case ∈ default GGUF missing / relative `BRAIN_RERANK_GGUF` / non-loopback `BRAIN_RERANK_URL` (with an empty placeholder GGUF) | `SystemExit(1)`; stderr is exactly one line starting `brain-install-launchd: error:` and naming the variable; a synthetic marker in the value never appears; zero launchctl calls; no plist, no `.shims` | RED: `RerankerConfigError` propagates |
| G' | `test_install_launchd_missing_gguf_prints_no_traceback` (subprocess via `bin/brain-install-launchd --with-reranker`, the exact QA repro) | rc 1, `Traceback` absent, one stderr line naming `BRAIN_RERANK_GGUF`, zero side effects | RED: traceback |
| H | in-process unit test for `parse_no_args`: `--help` → `SystemExit(0)` + `usage: x` on stdout; `--x` → `SystemExit(2)` | the shared helper's contract | RED (ImportError: the helper does not exist; acceptable for a new helper, and A/B carry the behavioural RED) |

Existing tests that encoded the bug, to be updated (named in the PR body):
`tests/test_bin_launcher.py::test_brain_down_main_dispatches_to_shim` and
`::test_brain_status_main_dispatches_to_shim` asserted that `--verbose` / `--json` are forwarded to
the shim. They become `sys.argv=["brain-down"]` → `exec_shim("brain-down", [])`, plus a check that
`--verbose` raises `SystemExit(2)` without calling the fake `exec_shim`.

Found during implementation: `tests/test_launchctl_isolation.py::test_install_main_lands_on_the_env_stub`
expected `install_main` to let a failed bootstrap escape as `LaunchdError`. It now expects the F19
contract (`SystemExit(1)` plus the one `brain-install-launchd: error: launchctl bootstrap failed`
line), and still asserts that only the env stub was called.

Existing suites to run (targeted, under the lock): `tests/test_bin_help_guard.py`,
`tests/test_bin_gws_reauth.py`, `tests/test_bin_launcher.py`, `tests/test_bin_scripts.py`,
`tests/test_bin_launchd.py`, `tests/test_bin_launchd_reranker.py`,
`tests/test_bin_launchd_dropfolders.py`, `tests/test_bin_launchd_retired.py`,
`tests/test_install_launchd_vault.py`, `tests/test_uninstall.py`,
`tests/test_packaging_templates.py`, `tests/test_packaging_entrypoints.py`,
`tests/test_setup_daemons.py`, `tests/test_launchctl_isolation.py`, plus the hygiene scans
`tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`, `tests/test_import_hygiene.py`,
`tests/test_repo_hygiene_files.py`. All with `--no-cov` (a subset cannot meet the 85% gate). Also
`ruff check` and `mypy src/`.

## 5. Proving the green guards can fail

Mutations run in a `git archive` copy of the branch, never the worktree, and the copy is deleted
afterwards:

1. Delete the `[]` in `uninstall.py` → E fails with `SystemExit(2)`.
2. In `install_main`, call `resolve_launchctl()` and run `launchctl print` before `parse_args` → A'
   (`brain-install-launchd`) fails on the stub call.
3. Delete the guard from one template (`_brain-dropfolders-fg.sh`) → its C/D rows fail.
4. Drop the `except LaunchdError` → G/G' fail with the traceback.

Each mutation is checked as applied (grep) before its run.

## 6. CHANGELOG (inserted just before the BL-39 `### Fixed — \`brain ingest-gmail\` …` anchor)

```markdown
### Fixed — `--help` no longer runs the daemon scripts; a bad reranker setting is one line (F18, F19, F20, F37)

- `brain-up`, `brain-down`, `brain-status` and `brain-uninstall-launchd` ignored their arguments,
  so `--help` ran them: `brain-down --help` booted out the watcher and swept its processes, and
  `brain-uninstall-launchd --help` deleted every brain LaunchAgent. `-h`/`--help` now prints the
  usage and exits 0 before the command installs a shim or calls `launchctl`, `pkill` or anything
  else. Any other argument is a usage error (exit 2), where it used to be silently ignored.
- The scripts behind them do the same when run by hand: `$BRAIN_HOME/.shims/brain-{up,down,status}`
  and launchd's `_brain-{watcher,brief,dropfolders}-fg` (and `bin/_brain-watcher-fg` in a checkout).
- `bin/gws-reauth --help` prints its usage. It used to delete `~/.config/gws/token_cache.json`
  first. Arguments for `gws auth login` now go after `--`. Any other argument exits 2 and deletes
  nothing.
- `brain-install-launchd --with-reranker` with no model file, a relative `BRAIN_RERANK_GGUF` or an
  unusable `BRAIN_RERANK_URL` prints one line that names the variable (never its value) and exits
  1, where it printed a `RerankerConfigError` traceback. Nothing is installed when a reranker input
  is unusable. A failed `launchctl bootstrap` stops the install at that agent, and its error is one
  line too (launchctl's own message, joined).
```

Review round 1 (PR #136 @ c445a899): the bootstrap error was two lines (launchctl's stderr), the
handler comment claimed every `LaunchdError` names only variables, and the CHANGELOG said nothing is
installed after a failed bootstrap. Fixed: whitespace collapsed in the handler, pinned by
`test_install_main_multi_line_bootstrap_failure_is_one_line` (RED first), the comment and both
texts corrected.

## 7. Risks

- **Overlap with PR-A on `src/brain/uninstall.py`** (one line, 121; PR-A edits about 73 and 167).
  The hunks do not touch each other, so the merge should be clean. Test E lives in my new file, not
  in `tests/test_uninstall.py`, which PR-A may edit. It calls `run_uninstall(vault_path=…)`. If
  PR-A renames that keyword, E needs the same rename at merge.
- **Shim sha changes.** Every template's bytes change, so the next `brain-up` /
  `brain-install-launchd` prints `shim updated: … (sha mismatch)` once and rewrites
  `$BRAIN_HOME/.shims/*`. That is the drift protection doing its job. The daemon install
  (non-editable uv tool) picks it up only after the usual post-merge reinstall.
- **Behaviour change.** `brain-status --json` and the like used to "work" by ignoring the flag; they
  now exit 2. That is the point of the fix, and it is in the CHANGELOG.
- **`gws-reauth` pass-through** now needs `--` (§2).
- **Running the RED tests on master is safe by construction:** every acting binary is a stub, the
  pid files and the LaunchAgents dir are under tmp, `BRAIN_SKIP_VENV_AUTOLOAD=1` keeps the
  watcher-fg off the real `brain`, and the DB URLs point at `localhost:1`.
- **Memory.** CLAUDE.md rule 11 wants `cli.md` in auto-memory updated for CLI changes. It is a
  shared file outside the worktree, so I leave it to fix-lead (one line: these scripts take only
  `-h/--help`; `gws-reauth` pass-through after `--`).
