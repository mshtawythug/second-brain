# RM-37 — External markdown vault, with deletions tracked

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-37". The ticket is the
spec; this is the task list. Base: `second-brain/master` at `4c4c0a7`.

## What ships

`brain ingest-dir <dir> --sync [--apply] [--allow-empty]`. After the existing walk, a prune pass
lists (default) or deletes (`--apply`) the documents a previous import of `<dir>` made from files
that are no longer on disk. Kind stays `manual` (RM-37.D1). No migration, no new knob, no front-end
change, no MCP tool.

## Files

| File | Change |
|---|---|
| `src/brain/ingest/import_sync.py` (new leaf, < 300 lines) | Root resolution, the three refusals, scope, the "gone" test, the plan and the apply. Imports no Typer and no CLI. |
| `src/brain/cli_import_sync.py` (new, < 150 lines) | The echo / exit mapping for the two phases, so `cli_ingest.py` grows only by the options and two calls (the `cli_extract.py` precedent). |
| `src/brain/cli_ingest.py` (over the ceiling) | Three options on `ingest_dir`, a preflight call before the walk and a prune call after it. Growth recorded inline + module docstring; CLAUDE.md row: promote the trailing descriptive hop (RM-33 round 3) to its SHA, add a descriptive hop for this change. |
| `tests/test_import_sync.py` (new) | Leaf unit + DB tests (5434, `tmp_path` trees). |
| `tests/test_cli_ingest_dir_sync.py` (new) | CLI tests: each refusal, dry-run lists, `--apply` deletes, option misuse. |
| `CHANGELOG.md`, `docs/cli-reference.md`, `skills/ingest-brain/SKILL.md` (one paragraph), spec Appendix B row → DONE, backlog row 2.4 (external half) → SHIPPED, CLAUDE.md ceiling row | Docs. |
| memory `cli.md`, `types.md` | "verify merged". |

## Behaviour

**Order inside `ingest_dir` when `--sync` is set:**

1. Walk listing as today (`files`).
2. **Preflight, before anything is written** (so a refused run ingests nothing either):
   - **R1a** `<dir>` does not exist or is not a directory at the time of the call → exit 1. (Typer's
     `exists=True` already exits 2 for a path missing at parse time; the leaf re-checks with
     `Path.resolve(strict=True)` + `is_dir()` because that is the check the prune relies on.)
   - **R2** the resolved `<dir>` is the resolved vault root or inside it → exit 1 ("belongs to the
     vault watcher").
   - **R1b** zero supported files in the walk while the database holds **in-scope** documents
     (step 4's scope, existing or not) → exit 1 naming the count and `--allow-empty`, unless
     `--allow-empty`.
3. The ingest walk, unchanged (skipped under `--dry-run`, as today).
4. **Prune plan** (read-only: one `SELECT` + `lstat` per in-scope row). Candidates are rows with:
   - `documents.kind = 'ingested'` (**R3**, in the SQL; vault-tier rows can never be read here),
   - `sources.kind = 'manual'` (open decision D1),
   - `source_path IS NOT NULL` (stdin docs are never candidates) and absolute,
   - `source_path` **under the resolved root by path components** (see "Containment"),
   - not under the resolved vault root (open decision D3),
   - suffix in this run's extension set (open decision D2),
   - and **gone**: `os.lstat(source_path)` raises `FileNotFoundError` or `NotADirectoryError`.
     Any other `OSError` (`PermissionError` from an unreadable parent, `ELOOP`, `EIO`, a timeout
     from a network volume) means *unknown* → kept, counted, and reported as "could not check N".
     A file that exists but is unreadable is therefore never deleted. A dangling symlink at the
     stored path counts as present (`lstat` succeeds) → kept.
5. **Dry run (default):** print `would delete: <id8>  <source_path>` per candidate (through
   `one_line`) and a summary naming `--apply`. **`--apply`:** for each candidate, re-`lstat`
   immediately before the delete (a file restored since the plan is kept), then
   `vault.delete.delete_document(conn, document_id=…, vault_root=cfg.vault_path,
   graph_syncer=_build_graph_syncer(cfg))`. A `ValueError` (row already gone) is reported as
   skipped; a `psycopg.Error` is reported class-only (`describe_failure`) and the run exits 1 after
   the loop. `ANALYZE` after the batch only if something was deleted (the existing helper).

**Mass-deletion guard — a deviation beyond the spec (a stricter refusal), added at plan
approval.** R1b catches a tree that vanished whole; it misses a partially-unmounted tree or a
moved subfolder, which is the realistic disaster. So with `--apply`, the run refuses (exit 1,
nothing deleted) when the candidates are **more than half** of the in-scope rows **and more than
5**, unless `--allow-empty`. The dry run always prints `N of M documents under <dir> would be
removed`. Both sides of each threshold are tested (5 vs 6 candidates; exactly half vs more than
half).

**Output.** `--apply` prints one line per document (`deleted <id8>  <relative path>`, the path
relative to the root and sanitised with `one_line`) and a final count.

**Containment** (`is_under(stored, root)`), never a string prefix:

- `PurePath(stored).parts[:len(root.parts)] == root.parts` → in scope. So `/a/notes` never matches
  `/a/notes-old/x.md`; trailing slashes vanish in `Path`.
- Otherwise, if those parts match **case-folded and NFC-normalized**, the stored prefix exists, and
  `os.path.samefile(prefix, root)` → in scope. This covers APFS's case- and normalization-
  insensitivity (a file imported as `/Volumes/Archive/Notes/…` pruned by `--sync /volumes/archive/notes`, an NFD
  name typed as NFC) without widening scope on a case-sensitive volume, where the two prefixes are
  different directories and `samefile` is false.
- Anything else → out of scope. A relative `source_path` (none should exist; extractors resolve)
  is out of scope.

Symlinks: `<dir>` is resolved (strict), and stored paths are already resolved at ingest, so a
symlinked `<dir>` (`~/Notes -> /Volumes/D/Notes`) matches its rows. A symlinked *file* inside
`<dir>` whose target is outside it was stored by its target, so it is out of scope and never
pruned (safe direction; documented).

**Dry-run never writes:** no `DELETE`, no unlink, no graph call; `--dry-run --sync` lists both the
walk and the prune and connects read-only in effect (only `SELECT`). Tested by asserting row
counts, mirror files and a recording graph-syncer fake are untouched.

**Option misuse** (exit 2, Typer `BadParameter`): `--apply` or `--allow-empty` without `--sync`;
`--apply` with `--dry-run`.

## Types (leaf)

- `ImportSyncRefused(BrainError)` — refusal with a user-facing, path-free-of-content message.
- `PruneCandidate(document_id, title, source_path)` — frozen.
- `PrunePlan(root, in_scope, candidates: tuple[PruneCandidate, ...], unverifiable: int)` — frozen.
- `PruneReport(deleted: tuple[str, ...], restored: int, missing_rows: int, failed: int)` — frozen.
- `resolve_sync_root(directory, *, vault_root) -> Path` (R1a, R2);
  `check_not_empty(conn, *, root, vault_root, extensions, supported_files, allow_empty)` (R1b);
  `plan_prune(conn, *, root, vault_root, extensions, lstat=os.lstat) -> PrunePlan`;
  `apply_prune(conn, plan, *, vault_root, graph_syncer, lstat=os.lstat) -> PruneReport`.
  `lstat` is injectable (DI, no monkey-patching) for `EIO`/`ELOOP`; `PermissionError` is also
  tested for real with a `chmod 000` parent in `tmp_path`.

## Tests (RED first, in this order)

1. Refusals R1a, R1b (and `--allow-empty` override), R2 — leaf and CLI.
2. **A vault-tier row is never touched:** a `kind='vault'` row whose `source_path` is set to a
   missing file under `<dir>` survives `--sync --apply` (inserted with a bounded SQL `UPDATE` in
   the test, since no ingest path writes one).
3. Acceptance 1: a deleted file's document is listed in dry run and removed with `--apply`; its
   mirror file under the `tmp_path` vault is unlinked; a present file's document survives.
4. Edge cases: sibling prefix `/notes` vs `/notes-old`; symlinked `<dir>`; trailing slash;
   case-variant root on a case-insensitive volume (skipped when `tmp_path` is case-sensitive) and
   out-of-scope on a case-sensitive one via a fake `samefile`-false layout; NFD/NFC root; NULL
   `source_path` stdin doc; unreadable (`chmod 000` parent) → kept and counted; injected `EIO`;
   non-`manual` source kind (D1); file restored between plan and apply; extension filter (D2);
   rows under the vault (D3).
5. 100% line + branch coverage on `import_sync.py`, `cli_import_sync.py` and the touched
   `ingest_dir` lines; mutation proof (git-archive copies, `PYTHONPATH` pinned, batches ≤ 10) of
   R1a, R1b, R2, R3, the component containment, the samefile gate and the "gone" errno set.

All against `second_brain_test_rm37` on 5434 and `tmp_path` trees only.

## Risks

- A mistyped or unmounted `<dir>`: R1a/R1b plus dry-run by default.
- A partially-synced network volume (some files visible, others not yet): not detectable from the
  file system; dry-run by default is the guard, and the docs say to inspect the list.
- `samefile` on a stalled network mount can block; it is only reached for a case/normalization
  mismatch, and dry-run shows the result first.

## Decisions (approved 2026-10-30: D1–D4 yes, as recommended)

- **D1** Restrict candidates to `sources.kind = 'manual'` as well as `documents.kind='ingested'`?
  **Recommend yes**: `ingest-dir` only writes `manual`, so a drop-folder row of its default kind
  (`screenshot` or `voice`, RM-33/RM-38) whose `imported/` copy lies under `<dir>` is never a
  candidate. *(Qualified in review round 1, L1: a drop folder configured with `kind = "manual"`
  stores manual rows, so those are candidates; a prune of one removes the owner's own deleted
  file, which is what `--sync` is for.)*
- **D2** Restrict candidates to this run's extension set (`--ext`, default the walk's
  supported set)? **Recommend yes**: `--ext pdf --sync` prunes only PDFs, so the scope of a prune
  is exactly what the run would re-ingest. (R1b counts in-scope rows with the same filter.)
- **D3** When the vault is *inside* `<dir>` (e.g. `<dir>` = a parent of the vault), refuse, or skip
  rows under the vault? **Recommend skip** (and say how many were skipped); R2 refuses only the
  spec's case, `<dir>` inside the vault.
- **D4** Option misuse is exit 2 (`--apply`/`--allow-empty` without `--sync`, `--apply` with
  `--dry-run`). **Recommend yes.**

## Results (2026-10-30)

- `tests/test_import_sync.py` + `tests/test_cli_ingest_dir_sync.py`: 83 tests, red first (import
  error, then 17 behavioural failures), green after. Line + branch coverage 100% on
  `ingest/import_sync.py` and `cli_import_sync.py`; every new line in `ingest_dir` covered.
- Mutation proof, git-archive copies with `PYTHONPATH` pinned, two batches of 10 plus one re-run:
  all 21 mutants killed (R1a strict + is_dir, R2, R1b, R3, D1, component containment, samefile
  gate, the fold check, the gone-errno set, the mass guard and both of its thresholds, the apply
  re-check, the nested-vault skip, the extension filter, the `..` and relative-path guards, and
  the two call sites in `cli_ingest.py`). The relative-path guard first survived, because an
  absolute root never matches a relative path anyway; a test against a relative root now pins it.
- `tests/log_privacy_rules.py` registers `ImportSyncRefused` (an exception class whose name does
  not end in `Error`); no allowlist entry.
- `tests/test_skills_token_routing.py::test_installed_skills_are_in_sync` reports drift locally
  until `bin/brain-skills-sync` runs after merge (the `ingest-brain` skill changed); it skips in CI.

## Review round 1 (rev-108, at `5f981b2`)

- **M1 (partial unmount below the root).** R1b catches only a fully empty walk and the mass guard
  only more than half and more than 5, so a minority of files on a dropped mount would prune. For
  each gone file, `plan_prune` (and `apply_prune`, again) now takes its nearest existing ancestor
  `A` at or below the root and keeps the row, counted as "could not check", when:
  (a) `A`, or a directory between `A` and the root, is a mount point (the root itself may be one);
  (b) `A` is on another device than the root;
  (c) `A` is below the root and is an empty directory (a dropped SMB/autofs mount leaves its empty
  mount point; an empty ROOT is R1b's case, which `--allow-empty` overrides);
  (d) `A` holds a mount point. **(d) goes beyond rev's three arms**: `/Volumes` with one volume
  unplugged has `A = /Volumes = root`, so (a)–(c) all miss it, and that is scenario 1 of the
  finding. Any error checking an ancestor, the root's device or `A`'s listing also keeps the row.
  A folder deleted outright still prunes; a folder emptied in place is kept (the safe direction),
  and a file on a nested mount is pruned only by running `--sync` on that mount.
  The seam is `FsOps(lstat, ismount, listdir)` on both functions (no monkey-patching).
- **L1.** D1's wording, and the module docstring, are qualified (above).
- **L2.** `apply_prune` re-reads each row right before `delete_document` and skips it as
  `changed` (printed `kept (row changed)`) unless it is still `ingested` / `manual` with the
  planned `source_path`.
- Tests: one per arm and per error path, both sides where a control exists (the same layout
  without the staged mount prunes), and the L2 cases (moved `source_path`, vault tier, another
  source kind). 101 tests; 100% line + branch coverage on both modules. Mutation, batches ≤ 5:
  12 of 12 killed (each arm, arm (a)'s root exclusion, arm (c)'s root exclusion, the three
  error paths, the L2 match, and both classifier call sites).
