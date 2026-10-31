# RM-39 — Mobile share-sheet capture: plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Spec: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-39" (AC1–AC6), built
on RM-33's runner (`src/brain/drop_folder.py`). Base: master `88260b9`.

## Shape

An iOS Shortcut writes one `.md` file per share into an iCloud Drive folder. That folder
is a drop folder with `mode = "capture"`. Each file is moved into `imported/` by RM-33's
move-first step, turned into a capture-inbox note by `capture_import.import_file`, and
then its `imported/` copy is **deleted** (RM-39.D2).

```
brain capture import --dir F --since 7d ─┐
                                         ├─ capture_import.run_capture_folder(conn, folder, …)
brain ingest-folder --configured ────────┘        └─ drop_folder.run(…, ingest=importer)
  (mode = "capture" folders)                             └─ importer → capture_import.import_file
```

There is **one code path**. Both CLIs build their `IngestFn` with
`capture_import.importer(...)`, which calls `import_file` by module-global lookup.
`tests/test_cli_capture_import.py::test_both_commands_make_the_same_notes_through_import_file`
puts a spy on `capture_import.import_file`
(pytest `monkeypatch`, a test double) and asserts that both commands reach it.

## Files

| File | Change |
|---|---|
| `src/brain/capture_import.py` (new leaf, ~200 lines) | `parse_share(raw) -> Share` (pure); `import_file(conn, path, *, title, timeout, ctx) -> str`; `importer(ctx) -> IngestFn`; constants |
| `src/brain/drop_folder.py` (557) | Minimal: `run` accepts `mode == "capture"`, and on a capture folder a successful outcome deletes the `imported/` copy by identity (`_unlink_if_same`, same fd discipline). Both `_one` and `_duplicate` do this. A failed delete is a detail, never a failure. |
| `src/brain/settings/drop_folders.py` (155) | Lift the `capture` refusal. In a capture folder `kind` must be absent; `extensions` defaults to `{.md, .txt}` and may only be a subset of that; `sensitivity` defaults to `normal` (mode-aware defaults, like RM-38.D2's kind-aware ones). |
| `src/brain/vault/note_builder.py` (238) | `create_vault_note` gains `extra_frontmatter: Mapping[str, str] = {}`, added after the brain-managed fields and never overriding them. It carries `sensitivity` and the `capture_import` key. It also splits into `write_vault_note` (render + write, returns `(path, id)`) and the existing sync. `create_vault_note` = write + sync, so its behaviour is unchanged. |
| `src/brain/_capture_command.py` (469) | `brain capture import --dir PATH --since Nd [--dry-run]`; a scope refusal exits 2, like `ingest-folder` |
| `src/brain/cli_ingest_folder.py` (478) | `_ingest_step` dispatches on `folder.mode`; a capture folder gets `capture_import.importer`. `--dir` on an unconfigured folder keeps the screenshot defaults. |
| docs | `docs/guides/mobile-capture.md` (new); `drop-folders.md` (capture mode); CHANGELOG; cli-reference; spec Appendix B; backlog; `skills/brain-capture/SKILL.md` routing line |

No migration and no new dependency. No file crosses 800 lines, so the ceiling table is
unchanged.

## `import_file` — what it does to one file

1. **Read** `path` with `O_NOFOLLOW`. `fstat` must show a regular file of at most
   `MAX_SHARE_BYTES` (1 MiB). The bytes must decode as strict UTF-8, with NUL stripped.
   Too big or not UTF-8 raises `RejectedFileError`: the file stays in `imported/`, not
   retried and not deleted.
2. **Parse** with `parse_share`, a line-based parser. **No YAML runs on untrusted input.**
   - An optional leading `---` block of at most 40 lines / 4 KiB.
   - Only `sensitivity: <level>` is honoured. Every other key is dropped, `allow_secrets`
     included, and is never written to the note.
   - An invalid level, or an opening `---` with no close, fails closed to `confidential`.
     In the second case the whole text becomes the body.
   - Sensitivity is **escalate-only** against the folder's level: a `normal` line in a
     confidential folder stays confidential.
3. **Empty body** (whitespace only) returns `"empty"`.
4. **Idempotency key** = `sha256(title + "\0" + raw bytes)`, where title is the runner's
   one-lined original stem. It is written to the note's frontmatter as
   `capture_import: <hex>`, which lands in `documents.metadata`. If a vault-tier row
   already carries it, the result is `"unchanged"` (reported `duplicate`).
   - Two different files with one name differ in bytes, so they are two notes (AC2).
   - The same file seen again is one note.
5. **Pre-checks** (vault configured, template present). A failure raises a plain
   `CaptureImportError`, which is transient: the file is moved back and retried next run.
6. **Write** with `write_vault_note`:
   - folder `capture/`, title `make_capture_title(body)` (as `brain capture`), tags
     `["inbox"]`, plus the extra frontmatter;
   - a write failure (`OSError`) is transient: nothing was written.
7. **Index** with `sync_one_file`, the same as `create_vault_note`.
   - **After the write, the content is safe in the vault.** A sync failure must not move
     the file back, because a retry would author a second note.
   - So a failure here returns `"written"`. The note is on disk and unindexed until the
     next vault sync (the watcher or `brain vault sync`). A class-only warning is logged.
   - It counts as a success, so the `imported/` copy is deleted.

**Content is never lost.** Every failure leaves it in exactly one of these places:
- the drop folder (transient, before the write);
- `imported/` (rejected);
- the vault (after the write).

The source is deleted only after the vault file exists.

## Open decisions (recommendation first)

1. **Secret guard.** (a) Match `brain capture`: no guard on authored notes. (b) Run
   `cfg.secret_guard`; a refusal leaves the note file unindexed. **Recommend (a).** A
   share is something the owner chose to send. The guard would also need a
   `note_builder` change, and a refusal there strands a vault file.
2. **Tags.** `inbox` only. A `mobile` provenance tag is possible, but the
   `capture_import` metadata key already marks provenance. **Recommend `inbox` only.**
3. **Title.** `make_capture_title(body)`, the same as `brain capture`, rather than the
   Shortcut's file stem (a timestamp). **Recommend `make_capture_title`.**
4. **`"empty"` in capture mode** deletes the `imported/` copy too, so nothing lingers in
   iCloud; RM-33 keeps it. **Recommend delete.**
5. **Malformed frontmatter** (no closing `---`) → a confidential note with the whole text
   as body, rather than a rejection. **Recommend confidential** (fail closed, the file
   leaves iCloud).
6. **The `note_builder` split** (`write_vault_note` + sync) is the only way to tell a
   failure before the write from one after it. The alternative is duplicating
   `create_vault_note`'s pre-write checks in `import_file`. **Recommend the split.**
7. **Overlap with RM-40.** I edit `settings/drop_folders.py` (mode validation) and
   `cli_ingest_folder.py` (`_ingest_step`). If impl-rm40 touches either one, whichever
   merges second takes the conflict.

## Tests (real Postgres `second_brain_test_rm39`, `tmp_path` vault + `BRAIN_HOME`)

- `test_capture_import.py`: `parse_share` unit cases:
  - frontmatter allowlist, hostile keys dropped, invalid level → confidential, unclosed
    block, over-limit block, CRLF;
  - escalate-only.

  `import_file` against the DB:
  - AC1 (comment + URL in the body, `inbox` tag, `kind='vault'` under `capture/`);
  - AC4 (confidential);
  - idempotent re-run;
  - too big / not UTF-8 → rejected;
  - empty;
  - no vault / no template → transient;
  - sync failure → `"written"` and no second note;
  - a symlink at the path → refused.
- `test_drop_folder.py` (extended):
  - a capture folder deletes the `imported/` copy after success (AC5), keeps it after a
    rejection, moves it back after a transient failure;
  - duplicate path in capture mode;
  - delete EPERM → detail, not a failure.
- `test_settings_drop_folders.py`: capture-mode defaults and refusals (`kind` given,
  non-text extensions).
- `test_cli_capture_import.py`:
  - scope refusals;
  - AC1–AC5 end to end;
  - AC3 (`.icloud` stub + dataless file via the `lstat` seam on `scan`, plus a real
    dot-file);
  - `--dry-run`;
  - every printed name goes through `one_line`.
- `test_cli_capture_import.py` (AC6, `test_both_commands_make_the_same_notes_through_import_file`): the same files through
  `ingest-folder --configured` and `capture import` give the same notes, and the spy sees
  both calls.
- 100% line + branch on `capture_import.py` and the touched hunks.
- Mutation-prove each guard (size cap, allowlist, escalate-only, delete-only-on-success,
  identity check on delete, written-not-moved-back) in `git archive` copies, batches ≤5.

## Review round 1 (rev-111)

- **M1:** `capture_import.importer` refuses a capture folder that is the vault, lies
  inside it or contains it (`DropFolderError`, so `ingest-folder` skips it with exit 1).
  `brain capture import` refuses it with exit 2 before anything moves, `--dry-run`
  included. The rule is RM-37's: `ingest.import_sync.is_within`, extracted from
  `resolve_sync_root` and applied both ways.
- **2:** `note_builder._write_unique` replaces `_unique_target` plus `write_text`: an
  exclusive (`"x"`) create that retries `-N` on `FileExistsError` and unlinks a partial
  file when the write fails.
- **3a:** `drop_folder._reported` maps `unchanged` to `duplicate` on both `_one` and
  `_duplicate`.
- **3b:** a share whose only text is inside a closed `---` block keeps the whole text as
  its body.
- **4:** the AC6 test's real location is named above.
- **5:** `create_vault_note` does not take `extra_frontmatter` (no production caller
  passed it); only `write_vault_note` does, which is what `import_file` calls.

