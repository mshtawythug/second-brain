# `brain uninstall --remove-vault` targets the configured vault (F16, F41)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Source:** 0.4.0 QA report, findings **F16** (HIGH) and **F41**, row C04
(evidence `brain-qa/logs/c/C04.txt`).
**Branch:** `fix/uninstall-vault-path` off `second-brain/master` (`ef824ed1`).

## 1. What happens today (read from source)

- `run_uninstall` (`src/brain/uninstall.py:44`) picks the vault at line 73:
  `vault = vault_path if vault_path is not None else DEFAULT_VAULT_PATH`. The CLI
  (`cli.py:8194` `uninstall_cmd`, call at 8225) never passes `vault_path`, so production always
  uses `DEFAULT_VAULT_PATH` (`config.py:169`, `Path.home() / "brain-vault"`, bound at import).
  `BRAIN_VAULT_PATH`, from the environment or from `$BRAIN_HOME/.env`, is never consulted, though
  the `--remove-vault` help text promises `$BRAIN_VAULT_PATH`.
- Step 8 (`uninstall.py:170-175`) does `shutil.rmtree(vault)` on that default. A user whose
  vault lives elsewhere keeps it and loses whatever is at `~/brain-vault`. The C04 log shows the
  wrong path in both the summary (`… fakehome/brain-vault ← YOUR NOTES`) and the "vault kept at"
  line.
- The only gate on the vault is the generic `Proceed? [y/N]` (`uninstall.py:97-98`), and `--yes`
  skips it. `--remove-db` alone has a typed confirmation that `--yes` cannot bypass (lines 103-109).
- **Ordering hazard:** step 6 (`uninstall.py:146-154`) deletes `$BRAIN_HOME/.env` before step 8.
  A vault path that lives only in that file must be read before step 6, or it is gone.
- **F41:** step 5 (`uninstall.py:131-141`) runs `docker compose down` with `check=False` and
  prints `[ok] docker compose down` whatever the return code. Only an exception reaches `[warn]`.

### The resolver to reuse

`brain.install_config.resolve_install_vault(brain_home)` (`install_config.py:37-58`) already
defines the install-side vault: `BRAIN_VAULT_PATH` from the process env, then from
`$BRAIN_HOME/.env`, then `config.DEFAULT_VAULT_PATH` (read at call time, not bound at import).
Every leg is `expanduser`-ed. `brain setup`, `brain-install-launchd`, `brain-monitor` and the bash
shims all call it, so it is the vault the daemons watch.

**Decision: uninstall uses `resolve_install_vault(home)`, not `Config.load_minimal()`.**
- It is the inverse of install, so it should resolve exactly what install resolved. The
  resolver exists so that "`brain setup` and the `brain-install-launchd` … cannot disagree about
  which vault the daemons watch", and uninstall joins that set.
- It needs no `DATABASE_URL`. It never raises `ConfigError`: an unrelated typo'd knob cannot
  break an uninstall. It publishes nothing into `os.environ`, so step 5's compose project does not
  change as a side effect.
- It reads `$BRAIN_HOME/.env` for the `home` that `run_uninstall` is about to clean, the same
  file step 6 deletes. It does not read the cwd walk-up or the repo `.env`. A destructive
  operation must not depend on the directory it is run from (config.py's `_cwd_dotenv_enabled`
  records the same concern). In a dev checkout `$BRAIN_HOME` is the repo root, so the repo
  `.env` is still the file read.
- It is a function in an 80-line module, so `config.py` (over the ceiling) does not grow.

## 2. Behaviour change

All checks and prompts run **before step 4**: nothing is removed until every gate has passed.

1. **Resolve up front.** At the top of `run_uninstall`, before the summary:
   `configured = (vault_path if given else resolve_install_vault(home)).expanduser().absolute()`.
   The test-only `vault_path` override stays and goes through the same guards and confirmation.
   The **target** is `configured.resolve()`, the canonical absolute path. It is what is printed,
   what must be typed, and what `rmtree` receives.
2. **Refusal guards** (only with `--remove-vault` and only when the path exists). Each prints
   `Refusing --remove-vault: <reason>. Nothing was removed.` and raises `typer.Abort`:
   - `configured.is_symlink()`: rmtree would refuse it mid-run (after the runtime files are
     gone), and following it could reach a directory the user never configured.
   - The target is not a directory: rmtree would crash mid-run.
   - The target is `$HOME` or an ancestor of it (this covers `/`). A blank or `~` value must
     never wipe a home directory.
   - The target is `$BRAIN_HOME` or an ancestor of it. That would delete `data/postgres`
     without `--remove-db`'s confirmation.
   Comparisons use `resolve()` on both sides. Guards I am **not** adding, to keep this minimal:
   a vault inside `$BRAIN_HOME` (legitimate), or a "looks like a vault" content check (heuristic,
   and it would refuse a legitimately empty vault).
3. **Summary** prints the resolved absolute target: `• <target>  ← YOUR NOTES`. The
   end-of-run `[skipped] vault kept at <target>` line also uses the resolved path. That fixes the
   misleading line in C04 even without `--remove-vault`.
4. **Typed confirmation**, after `Proceed?` and after `--remove-db`'s phrase, only when
   `--remove-vault` is set and the target exists:
   ```
   WARNING: --remove-vault will permanently delete your notes at:
     <target>
   Type the vault path exactly as shown above to confirm:
   ```
   The answer must equal `str(target)` exactly. A mismatch prints
   `Aborted — the vault was NOT removed (nothing was removed).` and raises `typer.Abort`.
   `--yes` skips only `Proceed?`. It never skips this prompt, which matches `--remove-db`.
   If the target does not exist, there is no prompt and the existing `[skipped] vault not found`
   line is printed.
5. **`--yes` without a TTY.** The prompt reads stdin, as `--remove-db`'s does. At EOF (no
   input, `</dev/null`, cron), click raises `Abort` and the run stops with nothing removed (the
   CLI prints `Aborted.` and exits 1). A pipe that supplies a wrong answer, such as `yes |` or
   `printf 'y\n'`, also refuses. A pipe can proceed **only by naming the exact resolved path**,
   and that is the property F16 needs: the caller has named the directory that will be deleted.
   I am deliberately **not** adding an `isatty()` refusal. It would make `--remove-vault`
   impossible to script, it would differ from `--remove-db`, and it would make behaviour depend
   on whether stdin is a terminal. `config.py` refuses that kind of context sniffing for the
   same reason. *(fix-lead: if you prefer a hard refusal on a non-TTY stdin, it is one
   `sys.stdin.isatty()` check plus one test. Say so and I will add it.)*
6. **Step 8** calls `shutil.rmtree(target)` on the target validated in steps 1 and 2. rmtree
   refuses a symlink, so a swap between check and delete cannot follow a link.
7. **F41, step 5.** `subprocess.run(..., capture_output=True, text=True, errors="replace",
   timeout=30)`. `[ok]     docker compose down` only when `returncode == 0`. Otherwise
   `[warn]   docker compose down exited <rc>: <tail>`, where the tail is the last 3 non-empty
   stderr lines joined with ` | ` and capped at 300 characters (omitted when stderr is empty). An
   exception (`FileNotFoundError` for a missing docker, `TimeoutExpired`) keeps today's `[warn]`.
   A new `_warn()` helper replaces the step-5 inline `typer.secho(... "[warn]" ...)` call. The
   step-4 launchd `except` branch keeps its inline call on purpose, see §9.

## 3. Files

| File | Change |
|---|---|
| `src/brain/uninstall.py` (185 lines → about 270) | Resolve via `resolve_install_vault`; a `_vault_refusal(configured, target, home) -> str \| None` helper; the summary path; the typed vault confirmation; step 8 on the target; F41 return-code handling; `_warn`. Drop the `DEFAULT_VAULT_PATH` import. Update the docstring. |
| `src/brain/cli.py` (over the ceiling) | Only the `--yes` and `--remove-vault` help strings are reworded **in place on their existing lines**, for example `--remove-vault`: `"Also remove the vault (YOUR NOTES; typed confirmation of its path, even with --yes)"`. **Net-zero lines**, so no growth record is owed. A ceiling-table trail hop **is** still owed, because touching a tabled file means promoting its trailing descriptive hop and adding one for this change (see §9). If review wants docstring prose that cannot fit net-zero, it goes in `docs/` instead. |
| `tests/test_uninstall.py` (236 → about 450) | New tests (§4). The existing `test_uninstall_remove_vault_takes_explicit_flag` gains a `typer.prompt` answer of `str(vault.resolve())`, a deliberate behaviour change. An autouse fixture points `HOME` and `DEFAULT_VAULT_PATH` (in both `brain.config` and `brain.uninstall`, `raising=False`) at `tmp_path`, so no test in the file can reach a real home, on master or on the branch. |
| `docs/configuration.md` § Uninstall (line ~1202) | Say how the vault is resolved (env > `$BRAIN_HOME/.env` > `~/brain-vault`, the same as `brain setup`). Say that the resolved path is printed and must be typed, and that `--yes` does not skip it. |
| `docs/cli-reference.md` (line ~1548) | One sentence with the same facts. |
| `CHANGELOG.md` | §6, inserted immediately before `### Fixed — the reranker's launchd job no longer refuses long passages (RM-14c)`. |

No migration, no schema change, and no new module. `install_config` is already imported by
`setup` and `bin/launchd`, so `test_import_hygiene` should be unaffected. I will run it to
confirm.

## 4. Tests (all in `tests/test_uninstall.py`; pure, `tmp_path` only, no DB)

Every test calls `run_uninstall` directly. Each one injects `_launchd_uninstall=` a recording
fake and patches `subprocess.run` with `mock.patch` (the file's existing pattern), so no real
launchctl or docker runs. `HOME` is `tmp_path/"home"`, and the decoy is
`tmp_path/"home"/"brain-vault"/note.md`, which is where `DEFAULT_VAULT_PATH` points. Where the
fixed code needs an answer, `typer.prompt` is patched to return `str(configured.resolve())`, so
the only thing that can stop a deletion is the behaviour under test. On master that patch is
unused (master has no vault prompt), and master deletes the decoy. That is why the decoy must be
a tmp dir.

| # | Test | What it proves | Why it fails on master |
|---|---|---|---|
| 1 | `test_remove_vault_targets_brain_vault_path_from_env` | `BRAIN_VAULT_PATH` (env) → that dir is removed; the decoy is untouched | master rmtrees the decoy and keeps the configured vault |
| 2 | `test_remove_vault_targets_brain_vault_path_from_brain_home_dotenv` | `BRAIN_VAULT_PATH` is unset in env and present only in `<home>/.env` → that dir is removed and the decoy is untouched. `.env` is also gone afterwards, which proves the path was read before step 6 deleted it | the same: master deletes the decoy |
| 3 | `test_summary_and_kept_line_print_resolved_absolute_vault` | A relative `BRAIN_VAULT_PATH` (`rel-vault`, with `chdir(tmp_path)`) is printed as the absolute resolved path, both in the summary (`--remove-vault`) and in the "vault kept at" line (no flag) | master prints the default path |
| 4 | `test_remove_vault_wrong_typed_path_removes_nothing` | Answer `"y"` → `typer.Abort`; vault intact; `.env` and `.shims` still present; launchd fake not called; `subprocess.run` not called | master has no vault prompt and deletes the vault |
| 5 | `test_yes_does_not_bypass_vault_confirmation_at_eof` | `yes=True` with `sys.stdin` set to `io.StringIO("")` (real click prompt, real EOF, **no** `typer.prompt` patch) → `Abort`, vault intact, nothing removed | master skips the prompt under `--yes` and deletes |
| 6 | `test_remove_vault_refuses_unsafe_target[...]`, parametrized: `home`, `parent_of_home`, `brain_home`, `parent_of_brain_home`, `symlink`, `not_a_directory` | Even with the correct typed answer, the guard refuses: `Abort`, target intact, launchd not called | master rmtrees the home or brain-home dir, or crashes in rmtree after deleting the runtime files |
| 7 | `test_compose_down_nonzero_exit_warns_with_code_and_stderr_tail` | rc=1 with stderr `"…\nno such project\n"` → output has `[warn]`, `exited 1` and `no such project`, and no `[ok]     docker compose down` | master prints `[ok]` |
| 8 | `test_compose_down_ok_only_on_zero_and_warns_on_exception` | rc=0 → `[ok]`; `FileNotFoundError` → `[warn]` | **Pin, not RED.** Master already does both. It guards against the fix over-correcting. |
| 9 | `test_remove_vault_missing_target_skips_without_prompt` | Configured vault absent → `[skipped] vault not found at <abs>`, `typer.prompt` never called | **Pin**: on master the run also skips, but it prints the default path. If you want it RED, the path assertion makes it RED. |

The RED run happens on master code in the worktree before the fix. I will record each failure
message in the PR and check that it fails for the stated reason, not on an import or fixture
error. A safety note for that run: only the autouse fixture's tmp `DEFAULT_VAULT_PATH` and `HOME`
stand between master's code and a real `~/brain-vault`. Before the first RED run I will check
that `brain.uninstall.DEFAULT_VAULT_PATH` is the tmp decoy (an assertion inside the fixture).

Targeted runs, under the suite lock with the disk check:
`pytest tests/test_uninstall.py tests/test_log_privacy_rules.py tests/test_log_privacy.py
tests/test_import_hygiene.py tests/test_repo_hygiene_files.py tests/test_examples_rot.py -p no:cacheprovider`,
plus `ruff check` and `mypy src/`.

## 5. Risks

- **Scripted `--yes --remove-vault` now stops.** This is intended. The CHANGELOG says so and
  gives the way through: type, or pipe, the exact path.
- **Uninstall and `Config.load` can now disagree** when `BRAIN_VAULT_PATH` comes only from a
  cwd or repo `.env`. Uninstall then targets the install-side vault, which is the one the daemons
  watch. That is deliberate (§1), and the user sees the printed path and types it before
  anything is deleted.
- **A symlinked parent directory** (for example macOS `/var` → `/private/var`): the printed
  path is the resolved one, and that is what the user must type. It is the directory that will
  actually be deleted, though it can differ from the path the user configured.
- **`--remove-db` and `--remove-vault` together** now need two typed answers, both before any
  removal. A failed vault answer after a correct DB phrase still removes nothing, and test 4's
  variant covers that.

## 6. CHANGELOG text

```markdown
### Fixed — `brain uninstall --remove-vault` deletes the configured vault, and only once you type its path (F16, F41)

- `--remove-vault` always deleted `~/brain-vault`, whatever `BRAIN_VAULT_PATH` said, so a vault
  kept elsewhere survived and whatever sat at `~/brain-vault` was deleted instead. The vault is
  now resolved the way `brain setup` and `brain-install-launchd` resolve it: `BRAIN_VAULT_PATH`
  from the environment, then from `$BRAIN_HOME/.env`, then `~/brain-vault`. It is resolved before
  anything is removed, because uninstall deletes `$BRAIN_HOME/.env` along the way.
- The summary prints the vault's absolute path, and the vault is deleted only after you type
  that path exactly. `--yes` skips only the `Proceed?` prompt, not this one. With no input (a
  script, `</dev/null`), the prompt reads end-of-file and the uninstall stops with nothing
  removed. **A script that ran `brain uninstall --yes --remove-vault` now stops there.** It has to
  supply the exact path on stdin.
- `--remove-vault` refuses, before anything is removed, a vault path that is a symlink, is not a
  directory, or is your home directory, `$BRAIN_HOME`, or a parent of either.
- `brain uninstall` printed `[ok] docker compose down` even when the command failed. It now
  prints `[ok]` only on exit 0, and otherwise a `[warn]` with the exit code and the end of
  compose's error output.
```

## 7. Not in scope

- `DEFAULT_VAULT_PATH` stays bound at import in `config.py`. `resolve_install_vault` reads it at
  call time, which is all uninstall needs.
- Auto-memory `cli.md`: the `uninstall` flag semantics changed. The coordinator owns the memory
  directory, so I will flag the update in my final report instead of editing it.

## 8. Review additions (approved by team-lead via fix-lead, 2026-11-19)

- **(a) The source is printed with the path.** The new helper
  `install_config.resolve_install_vault_with_source(brain_home) -> tuple[Path, str]` returns the
  path and one of three labels: `"BRAIN_VAULT_PATH env"`, `"$BRAIN_HOME/.env"` or
  `"default ~/brain-vault"`. `resolve_install_vault` keeps its signature. Without an override it
  returns the helper's path (one source of truth), so its callers and behaviour are unchanged.
  The summary bullet and the typed-confirmation prompt both print `<path>  (from <source>)`. A
  user whose vault is set only in a repo or cwd `.env` sees "default" and can decline. When the
  test-only `vault_path` override is used, the label is `"explicit override"`. New test 10, which
  is parametrized over the three sources, checks the printed label. Its `default` case leaves
  `BRAIN_VAULT_PATH` unset in both the environment and `$BRAIN_HOME/.env`.
- **(b) A vault that does not exist.** The output is `vault not found at <abs path>, nothing to
  remove`. There is no prompt and the step is skipped. Test 9 asserts that exact text with the
  absolute path, so it is RED on master.
- **(c)** Item 4 is unchanged: EOF aborts, and there is no `isatty()` refusal.
- **(d)** The autouse decoy fixture uses only `monkeypatch.setenv` / `monkeypatch.setattr`.
- PR-B owns the step-4 `uninstall_main()` call line, so this diff leaves that line untouched.
  The launchd `except` branch next to it also keeps its inline `[warn]` call, so `_warn()`
  replaces only step 5's inline call (§9 item 8).

## 9. Review round 1 (PR #135 @ `7784c4c1`, NOT APPROVED, fixed in follow-up commits)

1. **The guards compare file identity.** `_is_at_or_above` uses `os.path.samefile` between the
   target and each existing path in a guarded path's chain. Paths that do not exist are skipped.
   On a case-insensitive volume `…/HOME` is `…/home`, and the old string comparison let it
   through. The new test is `test_remove_vault_refuses_case_variant_of_home`. It skips when the
   tmp filesystem is case-sensitive.
2. **The database and runtime entries are guarded.** A target is refused if it is
   `$BRAIN_HOME/data/postgres`, anything inside it, or any parent of it. A target is also refused
   if it is at or inside any `_BRAIN_HOME_SAFE_REMOVES` entry: step 6 deletes that entry first,
   so step 8 would crash mid-run. New parametrized cases: `database`, `parent_of_database`,
   `inside_database`, `runtime_entry`, `inside_runtime_entry`.
3. `test_remove_db_then_wrong_vault_path_removes_nothing` kills the mutation
   `if removing_vault and not remove_db:`. The mutation was verified in a scratch copy: it
   survived every earlier test, and this test fails on it.
4. **Ceiling table.** The `cli.py` trail promotes `0f32826d` (8,704, the empty-daily-brief
   commit) and adds a hop for `7784c4c1` (8,704, net-zero). That hop is bound to its SHA by the
   later commit that edits the table and does not touch `cli.py` (the `bc3b868` precedent).
5. The wrong-answer test is parametrized with near misses: `y`, trailing space, trailing
   slash, and the unresolved spelling through a symlinked parent.
6. The autouse fixture also `delenv`s `BRAIN_VAULT_PATH`.
7. The `run_uninstall` docstrings for `yes` and `remove_vault` are corrected.
8. **The step-4 launchd `except` branch keeps its inline `typer.secho("[warn]…")`.** It sits
   beside the `uninstall_main()` line that PR-B changes to `uninstall_main([])`. Routing it
   through `_warn` would turn a clean merge into a conflict, so only step 5 uses `_warn`.

## 10. Review round 2 (PR #135 @ `7f7a53fe`, NOT APPROVED, fixed in follow-up commits)

1. **`_is_at_or_above` walks both chains.** It compares, by `samefile`, against
   `lower.absolute()` and its parents **and** `lower.resolve()` and its parents
   (`_lineage`). Before this, when HOME or `$BRAIN_HOME` (or a parent of either) was a symlink,
   the real parent of the resolved directory was never compared. Three new cases cover it:
   `parent_of_symlinked_brain_home`, `symlinked_home_real_parent` and
   `symlinked_brain_home_real_parent`. All three were RED (`DID NOT RAISE Abort`) at
   `7f7a53fe`.
2. **Each unsafe-target case asserts its own refusal reason.** A later guard can no longer
   cover for a disabled one. An `if False:` mutation of every guard was run in a git-archive
   copy, and every mutation was killed.
   - The runtime-entry message now reads `is $BRAIN_HOME/<name> or inside it`, because it fires
     for the entry itself as well as for paths inside it.
3. The CHANGELOG and `docs/configuration.md` now describe the identity comparison over both
   chains.

## 11. Review round 3 (PR #135 @ `4dc43987`, NOT APPROVED, fixed in follow-up commits)

- **Master `6c078c1f` (PR #136) is merged in. There was no rebase.** The merge was clean, and
  step 4 now reads `uninstall_main([])`.
- **`_resolve_vault` no longer lets a resolve failure escape.** On Python 3.11 a symlink-loop
  `BRAIN_VAULT_PATH` raised `RuntimeError` from `configured.resolve()`, so even a plain
  `brain uninstall` crashed. Master never resolved the path, so this was a regression. The
  function now catches `(OSError, RuntimeError)` and sets `_Vault.resolvable=False`, and
  `target` falls back to the configured path.
  - Without `--remove-vault`, the kept line shows that configured path.
  - With `--remove-vault`, the run refuses with `<path> cannot be resolved (symlink loop)` before
    anything is removed. This check runs before `exists()`, because pathlib reports a loop as
    not existing.
- **New file `tests/test_uninstall_symlink_loop.py`.** `test_uninstall.py` is near the
  ceiling, so the tests go in a separate file. They cover both branches above, plus `_lineage`'s
  fallback to the spelled chain, which kills mutant M19 (an unguarded `resolve()`). The fixture
  uses the real 3.11 `RuntimeError`. On an interpreter that resolves a loop without raising,
  the fixture makes `Path.resolve` raise for the two links instead.
