# Drop folders and screenshots

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules apply to this recipe too.

`brain ingest-folder` takes the files saved into a drop folder (a screenshot folder, for
one), **moves** each into the folder's `imported/` sub-folder, and ingests it from there.
Images (`.png`, `.jpg`, `.jpeg`) are OCR'd by the local `tesseract` into `screenshot`
documents, stored **confidential** by default. It **refuses without `--dir PATH` or
`--configured`, and `--since Nd`** (1d to 365d).

The command moves the user's files, so:

- Run `--dry-run` first and show the user the list before the real run.
- Only use a folder the user named, or one already in `$BRAIN_HOME/drop-folders.toml`
  (`--configured`). Never point `--dir` at a folder the user did not ask for: their Desktop,
  Downloads, or any folder of photos.
- Never edit `drop-folders.toml` or install the LaunchAgent unless the user asks; the guide
  `docs/guides/drop-folders.md` has both steps.

```bash
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d --dry-run
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d
brain ingest-folder --configured --since 2d
```

Per-file verbs: `ingested`, `updated`, `unchanged`, `empty` (no text was read: the file stays
in `imported/`), `duplicate` (the same file was imported before: the drop copy is deleted),
`vanished`, `rejected` (a failure every run would repeat, such as an unreadable image, an
OCR timeout or the secret guard: the file stays in `imported/` and is not retried), `failed`
(a transient failure: the file is moved back; the reason is a class name). Exit 1 means a
file failed or a folder was skipped. The usual cause is that OCR is off or `tesseract` is
missing, and the command prints the fix.
