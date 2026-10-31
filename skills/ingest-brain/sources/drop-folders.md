# Drop folders and screenshots

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules apply to this recipe too.

`brain ingest-folder` takes the files saved into a drop folder (a screenshot folder, for
one), **moves** each into the folder's `imported/` sub-folder, and ingests it from there.
Images (`.png`, `.jpg`, `.jpeg`) are OCR'd by the local `tesseract` into `screenshot`
documents, stored **confidential** by default, on every path: `brain ingest x.png` and
`brain ingest-dir --ext png` too, unless `--sensitivity normal` is passed. Voice memos
(`.m4a`, `.mp3`, `.wav`) in a folder configured with `kind = "voice"` are transcribed on this
machine by whisper.cpp into `voice` documents, also **confidential** by default; only the
transcript enters the brain. They need `BRAIN_WHISPER_MODEL` (a model file the user downloads
themselves; never download one for them) and `whisper-cli`. It **refuses without `--dir PATH` or
`--configured`, and `--since Nd`** (1d to 365d).

A folder with `mode = "capture"` (RM-39, the phone's "Save to Brain" share sheet) makes each
`.md`/`.txt` file a capture-inbox note and then **deletes** the file; that is the
`brain-capture` skill's `brain capture import`, and
`--configured` runs it too.

The command moves the user's files, so:

- Run `--dry-run` first and show the user the list before the real run.
- Only use a folder the user named, or one already in `$BRAIN_HOME/drop-folders.toml`
  (`--configured`). Never point `--dir` at a folder the user did not ask for: their Desktop,
  Downloads, or any folder of photos.
- Never edit `drop-folders.toml` or install the LaunchAgent unless the user asks; the guide
  `docs/guides/drop-folders.md` has both steps.

**The inbox (RM-40).** The first entry with `kind = "manual"` and `auto = false` is the
`brain ui` Ingest tab's inbox: the user picks a file there by name (**From the inbox**), and
`--configured` never touches it. Do not run `--dir` on the inbox to "help": the files there
are waiting for the user's own pick.

```bash
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d --dry-run
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d
brain ingest-folder --configured --since 2d
```

Per-file verbs: `ingested`, `updated`, `empty` (no text was read: the file stays
in `imported/`), `duplicate` (the same file was imported before: the drop copy is deleted and the import
re-checked, adding nothing),
`vanished`, `rejected` (a failure every run would repeat, such as an unreadable image, an
OCR timeout or the secret guard: the file stays in `imported/` and is not retried), `failed`
(a transient failure: the file is moved back; the reason is a class name). Exit 1 means a
file failed or a folder was skipped. The usual cause is that OCR is off or `tesseract` is
missing (or, for a voice folder, `BRAIN_WHISPER_MODEL` is unset or `whisper-cli` is missing), and
the command prints the fix. A memo longer than `BRAIN_WHISPER_MAX_MINUTES` is `rejected`; a
`warning: … was not transcribed (…, failure N of 3)` line means whisper failed and the memo goes back to be retried; the third failure on the same memo rejects it.
