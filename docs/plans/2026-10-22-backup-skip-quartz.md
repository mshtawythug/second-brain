# `brain backup` skips `<vault>/.quartz/` — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/backup-skip-quartz`, base `second-brain/master` @ `58e27c6` (PR #45).

## Problem

`tar_vault` (`src/brain/backup/create.py`) walks the vault with
`sorted(vault_path.rglob("*"))` and adds every entry to `vault.tar`. It has no
skip list. The Quartz wiki was deleted at R3 (phase 9, PR #43), but an upgraded
vault still holds `<vault>/.quartz/`: the old Quartz workspace with its
`node_modules`, builds, `current` and `.cache`. Nothing reads it any more, and
the user deletes it by hand (`docs/vault.md`). Vault sync, the vault watcher and
`brain-monitor` already skip it (ruling R9-8). Until it is deleted, every full
`brain backup` carries about 11 GB of dead build output (measured 2026-10-21,
handoff `docs/handoff/2026-10-13-merge-all-handoff.md`, "Follow-ups still open").
That cost is paid twice, because `brain restore` takes a pre-restore backup first.

## Rulings

| # | Question | Ruling |
|---|---|---|
| **B1** | Skip only `.quartz`, or more (every dotted dir, like sync; or `.quartz` + `.git`, like `brain-monitor`)? | **Only `.quartz`.** A backup is a backup of user data, so its question is not the one sync asks ("is this an authored note?"). `.obsidian/` holds the user's editor config, plugins and workspace layout, and a vault `.git/` holds the user's own history. Both are user data a restore must bring back. `.quartz/` is the one tree this project wrote, that nothing reads since R3, and that the docs tell the user to delete. |
| **B2** | At any depth, or only `<vault>/.quartz`? | **Only the top-level entry.** The Quartz workspace always lived at `<vault>/.quartz` (`git grep -n '"\.quartz"' 885b5a6^ -- src`). A `.quartz` further down was not written by this project, so it is the user's and stays in the backup. Matching by name at any depth would drop user data to save nothing. |
| **B3** | What if `<vault>/.quartz` is a file or a symlink? | **Skipped whatever its type.** The name is the Quartz tombstone. A symlink is never followed (it was not followed before either: `rglob` in 3.11 and `os.walk(followlinks=False)` both leave symlinked dirs alone). `os.walk` lists a regular file under `filenames` and a symlink to a directory under `dirnames`, so both filters are needed. Test 9 covers both cases, and M9 proves the `filenames` filter. The report line prints the name with no trailing `/`, so it is correct for every type. *(Both were added after the audit of `b8d5e30`. The `filenames` filter had no test, and the line read `left out .quartz/` even for a file.)* |
| **B4** | Filter `rglob`'s output, or prune the walk? | **Prune the walk with `os.walk`.** Filtering after `rglob` still lists and `stat`s every entry under `node_modules` (tens of thousands) on each backup. `os.walk` with `dirs[:]` pruning never enters the tree. This is the idiom `bin/monitor.py::_recent_vault_files` uses. The entry set is otherwise unchanged: every name in `dirnames` and `filenames` at every level, which is what `rglob("*")` yields (symlinked dirs appear in `dirnames` and are not descended). The order is unchanged, because the list is still `sorted()`. |
| **B5** | Share a constant with `bin/monitor.py` or `vault/sync.py` (DRY)? | **No existing constant fits, so a new one lives in `create.py`.** The three consumers have three different policies: sync skips every dotted component, `brain-monitor` skips `{".quartz", ".git"}` at any depth, and backup skips `{".quartz"}` at the top only (B1, B2). `monitor._VAULT_SKIP_DIRS` cannot be reused, because it contains `.git`. A shared `".quartz"` name constant would join three modules (one of them a `bin/` script that imports nothing from `brain.backup` or `brain.vault`) to deduplicate one string literal, and that coupling costs more than it saves. The new constant is `VAULT_BACKUP_SKIP_DIRS`, public, so a test and a future reader can name the policy. |
| **B6** | Does `file_count` / the manifest change? | **`vault_file_count` counts what is in `vault.tar`, as it did before.** Skipped files are not in the tar, so they are not counted. No manifest field is added. A field would change the manifest schema and `from_dict` compatibility for every older archive, and restore has no use for it. The `vault.tar` checksum in `files` is computed over the tar as written, so `verify` stays consistent. |
| **B7** | Does the backup say it skipped something? | **Yes, but only when the tree exists.** `tar_vault` returns the skipped top-level names. `create_backup` reports one `on_step` line, which `brain backup` prints with the other steps: `left out .quartz (leftover Quartz wiki output, not backed up; safe to delete by hand)`. A vault with no `.quartz` gets no line. |
| **B8** | Restore: does a backup without `.quartz` delete or touch an existing `.quartz`? | **No code change; it is pinned by a test instead.** `restore_backup` renames the whole existing vault aside to `<vault>.replaced-<ts>` (`_move_vault_aside`, never `rmtree`, §7) and extracts the archive into a fresh directory. So an existing `.quartz` moves with its vault, with its contents intact, and it is never deleted or rewritten. The restored vault has no `.quartz`, which is correct, because the backup is the user's data and `.quartz` is not. A new test in `tests/test_restore_swap.py` pins this, and the mutation proof covers it (M8). |
| **B9** | Restore preflight's `_count_vault_files` counts `.quartz` files in the target vault. Change it? | **No.** That number is printed under "WILL BE OVERWRITTEN" as what the restore would move aside, and the rename-aside does move the `.quartz` files. Counting them is accurate. It is slow on an upgraded vault (it walks `node_modules`), but it is a read-only count, and B8 means it stays correct. Out of scope. |
| **B10** | Error handling of the walk | **Parity with `rglob`.** Both silently pass over a directory they cannot list (`os.walk`'s default `onerror=None`, `rglob`'s internal `PermissionError` handling). An unreadable *file* still fails `tar.add` loudly, as before. Making an unlistable directory fatal would be a behaviour change and is out of scope. It is recorded here so the parity was a decision, not an accident. |

## Affected files

| File | Change |
|---|---|
| `src/brain/backup/create.py` (314 lines, not in CLAUDE.md's over-800 table; it stays well under 800) | `VAULT_BACKUP_SKIP_DIRS`, a `VaultTar` result (`file_count`, `skipped`), the pruned walk in `tar_vault`, and the B7 report line in `create_backup`. |
| `tests/test_backup_create.py` | Red-first regression tests (below). |
| `tests/test_restore_swap.py` | B8 test. |
| `docs/cli-reference.md` | One sentence in `## brain backup / brain restore`. |
| `docs/vault.md` | The retired-wiki note says `brain backup` skips `.quartz/` too. |
| `CHANGELOG.md` | `[Unreleased]`: the phase-9 leftovers bullet says the backup skips it too, plus a `### Fixed` entry. |
| `docs/handoff/2026-10-13-merge-all-handoff.md` | The "Follow-ups still open" bullet says the follow-up is fixed. |

No file in CLAUDE.md's over-800 table is touched, so no trail needs promoting.
No migration, no schema change, and no CLI flag change. `docs/plans/2026-10-21-wiki-to-ui-phase9.md` §6 says
"leaving it costs only disk". That is a dated record (R9-10) and stays as written.

## Tests (TDD, red first)

In `tests/test_backup_create.py`, a fixture vault gets a `.quartz/` subtree
(`node_modules/pkg/index.js`, `builds/<id>/index.html`, `.cache/fastpath/x`), a
sibling `.obsidian/app.json`, and a nested `notes/.quartz/keep.md`:

1. `test_vault_tar_leaves_out_the_quartz_workspace`: no member of the inner
   `vault.tar` is `.quartz` or starts with `.quartz/`.
2. `test_vault_file_count_excludes_the_quartz_workspace`: `vault_file_count`
   equals the number of regular files in the inner tar, and equals the files
   outside `.quartz` (2 base files + `.obsidian/app.json` + `notes/.quartz/keep.md` = 4).
3. `test_vault_tar_keeps_other_dotted_user_config`: `.obsidian/app.json` is a member.
4. `test_vault_tar_keeps_a_nested_quartz_directory`: `notes/.quartz/keep.md` is a member (B2).
5. `test_quartz_workspace_skip_is_reported`: exactly one `on_step` line names
   `.quartz`, and it is the B7 text.
6. `test_no_skip_is_reported_without_a_quartz_workspace`: the plain vault gets
   no such line.
7. `test_quartz_workspace_is_not_walked`: records every directory `os.scandir`
   opens (via pytest `monkeypatch`, a standard test double with automatic
   cleanup, so it is allowed under rule 13) and asserts none is `.quartz` or
   under it. This proves the pruning, which the membership tests cannot tell
   apart from a post-filter. A second assertion (`notes/` was opened) proves the
   probe observes the walk at all.

9. `test_top_level_quartz_is_skipped_whatever_its_type`, parametrized (B3):
   `<vault>/.quartz` is (a) a regular file or (b) a symlink to a directory
   outside the vault. In both cases no tar member is `.quartz` or under it, and
   `tar_vault(...).skipped == (".quartz",)`. *(Added after the audit of
   `b8d5e30`, so it is not part of the red-first run below.)*

Tests 1, 3, 4, 7 and 9 call `tar_vault` directly and need no database. Tests 2,
5 and 6 go through `create_backup` against the test DB on 5434 with the faked
`pg_dump`.

In `tests/test_restore_swap.py`:

8. `test_restore_moves_an_existing_quartz_workspace_aside_intact` (B8). It backs
   up a vault that holds `.quartz/`, restores the archive, and asserts that the
   workspace file is intact in `<vault>.replaced-<ts>` and that the restored
   vault has no `.quartz`. The "intact aside" half holds with or without the fix
   (it pins the restore contract this PR relies on). The "restored vault has no
   `.quartz`" half is red without the fix, because the archive then carries it.

Red-first result (production code unchanged, new tests only): 5 failed, 3 passed.
Red: 1, 2, 5, 7, 8. Green: 3, 4, 6, which guard against over-skipping, so they
are expected to pass before the fix as well.

## Mutation proof (five clauses)

Re-run on 2026-10-22 against commit `1ac45d4`, the audit-fix commit that added
test 9 and M9. The first run, at `7b3b6a5`, covered M1–M8 with the same
outcomes. A scratchpad harness (not checked in) ran sequentially in the
worktree, with no other suite running. For each mutation it read the pristine
file, required the `old` text to occur exactly the declared number of times,
wrote the mutant, recorded `git diff --numstat`, ran the new tests (eight, plus
test 9's two cases, so ten items), and restored the pristine bytes in a
`finally`. After the run, `git diff --stat -- src` was empty. A mutation counts
only if its target test fails, and fails at the `>` line of the assertion it
targets, not at an earlier line or an exception.

1. **Passes unmutated:** 10 passed.
2. **Mutation lands the declared count:** every row below was applied 1/1, with numstat `1 1`.
3. **Guard goes red / 4. at the targeted assertion / 5. one entry per claim:**

| # | Claim | Mutation (1 line) | Red | Target fails at |
|---|---|---|---|---|
| M1 | top-level `.quartz` left out | `VAULT_BACKUP_SKIP_DIRS` → `frozenset()` | 7 (leaves_out, not_walked, file_count, skip_reported, restore, both test-9 cases) | `assert leaked == []` |
| M2 | pruned, not listed then filtered (B4) | `tar_vault` takes an `rglob` + post-filter entry list (membership unchanged) | 1 (not_walked only) | `assert entered == []` |
| M3 | top level only (B2) | `if here == vault_path:` → `if True:` | 4 (nested, file_count, both test-9 cases, whose `skipped` is overwritten by the deeper levels) | `assert "notes/.quartz/keep.md" in names` |
| M4 | other dotted dirs kept (B1) | skip set `{".quartz", ".obsidian"}` | 2 (dotted, file_count) | `assert ".obsidian/app.json" in names` |
| M5 | `file_count` = what the tar holds (B6) | `file_count` from an unfiltered `rglob` count | 2 (file_count, not_walked) | `assert result.manifest.vault_file_count == len(_FILES_KEPT)`. The preceding `regular == _FILES_KEPT` passed, so the test reached the count. |
| M6 | skip is reported (B7) | `for name in vault_skipped:` → `for name in ():` | 1 | `assert [step for step in steps if ".quartz" in step] == [_QUARTZ_SKIP_STEP]` |
| M7 | no report without a workspace (B7) | `for name in vault_skipped:` → `for name in (".quartz",):` | 1 | `assert _QUARTZ_SKIP_STEP not in steps` |
| M8 | restore never deletes an existing `.quartz` (B8) | `_move_vault_aside`: `vault_path.rename(aside)` → `shutil.rmtree(vault_path)` | 2 (new restore test, plus the existing `test_vault_moved_aside_not_deleted`) | `assert aside.is_file()` |
| M9 | a top-level `.quartz` **file** is skipped (B3) | `filenames = [name for name in filenames if name not in VAULT_BACKUP_SKIP_DIRS]` → `filenames = list(filenames)` | 1 (test 9 `[regular-file]` only: `skipped` is computed before the filter, so the report would still claim the skip) | `assert [name for name in names if name.split("/")[0] == ".quartz"] == []` |

Test 9's `[symlink-to-outside-dir]` case goes through the `dirnames` filter, the
same line M1 and M2 already exercise, so it has no mutation row of its own.

What this does not prove (written down rather than guarded): the probe in M2
observes `os.scandir`. A future walk built on `os.listdir` would bypass it.
`os.walk` and `pathlib` (3.11–3.13) both list through `os.scandir`, and the
probe's second assertion (`notes/` was opened) would go red if the walk stopped
calling it, so a probe that went blind would fail rather than pass silently.

## Verification

- `ruff check` (src + tests): clean. `mypy src/`: no issues in 218 source files.
- `ruff format --check`: **the repository is not format-clean, and this PR does
  not try to make it so.** `ruff format --check src tests` on this branch reports 689 of 726 files would be reformatted,
  and CI does not run the formatter. All three touched Python files already had
  formatter hunks at the base. Each still has the same number of hunks, and
  none of them is in a line this PR added: 3 in `create.py`, 3 in
  `test_backup_create.py`, 8 in `test_restore_swap.py`. Reformatting the files
  would bury a fix of under 70 lines in unrelated churn.
- Targeted tests, run serially on the test DB (port 5434), with no full suite
  locally (CI is the full-suite evidence): every `tests/test_backup_*.py`,
  `tests/test_cli_backup.py` and `tests/test_restore_*.py` module: 181 passed at `b8d5e30`, and 183 at `1ac45d4` (the two test-9 cases added).
- `create.py` was 314 lines and is 376 at `1ac45d4`, under the 800 ceiling (re-derive with `wc -l src/brain/backup/create.py`). It is not in
  CLAUDE.md's over-800 table, and neither is any other touched file.

## Risks

- **Restore of an old archive that contains `.quartz`.** It is unaffected: the
  restore extracts whatever the archive holds, and the safe-member filter is
  unchanged.
- **Symlinks.** A symlink inside the vault was a symlink member before, and it is
  one now. `_safe_members` still refuses link members at restore (unchanged,
  pre-existing behaviour).
- **Ordering.** `sorted()` over `Path`s, as before, so a `vault.tar` from an
  unchanged vault without `.quartz` has the same members in the same order.
