# Drop folders: make screenshots searchable

> Part of the [Second Brain](../../README.md) docs. See [docs/README.md](../README.md) for the full index.

A drop folder is a folder you save files into. `brain ingest-folder` moves each new file
into the folder's `imported/` sub-folder and ingests it from there. The first use is
screenshots: point macOS's screenshot location at a folder, and what was on screen becomes
searchable within minutes. Images are read by the local `tesseract` OCR engine; nothing
leaves the machine. Roadmap ticket RM-33.

## 1. Install OCR

```bash
brew install tesseract
brain doctor          # the `ocr` line should be OK
```

`BRAIN_OCR` must be `auto` (the default). With OCR off or no `tesseract`, image folders are
skipped, their files are not moved, and the command prints this fix.

## 2. Configure a folder

Create `$BRAIN_HOME/drop-folders.toml` (`~/.brain/drop-folders.toml` for a normal install):

```toml
[[folder]]
path = "~/Pictures/Screenshots"
```

That folder gets the defaults: kind `screenshot`, files `.png`, `.jpg` and `.jpeg`, stored
**confidential** (a screenshot can show anything, including a credential the secret guard
does not recognise), and run automatically. Every key is in
[configuration](../configuration.md#drop-folderstoml--drop-folders-rm-33). To make macOS save
screenshots there, run `defaults write com.apple.screencapture location ~/Pictures/Screenshots`,
or pick the folder under Options in the Screenshot app (Shift-Command-5).

Check it, then run it once by hand:

```bash
brain doctor                                         # the `drop folders` line
brain ingest-folder --configured --since 7d --dry-run
brain ingest-folder --configured --since 7d
```

## 3. Run it every 10 minutes (optional)

```bash
brain-install-launchd --with-dropfolders
```

This installs the `com.brain.dropfolders` LaunchAgent, which runs
`brain ingest-folder --configured --since 2d` every 10 minutes. It is kept by every later
`brain-install-launchd` and every `brain-up`, and removed only by
`brain-uninstall-launchd`. Its logs are `$BRAIN_HOME/logs/com.brain.dropfolders.{out,err}.log`;
`brain doctor`'s `daemons` line reports its last exit. A run stops after 200 files a folder or
8 minutes, and the rest wait for the next run.

## What happens to your files

- Each new file is moved to `imported/<stem>-<sha8><ext>`, where `sha8` is the first 8 hex
  digits of its SHA-256. The document keeps the original name as its title, and its stored
  path is the `imported/` one, so do not move files out of `imported/`.
- The same file saved twice: the second copy is deleted, only after its full SHA-256 matches
  the first, and nothing new is ingested. Two different files with one name become two
  documents.
- A failure that may pass next time (the database was down) moves the file back, to be tried
  again next run. A failure that would repeat (an unreadable or oversized image, an OCR
  timeout, the secret guard) leaves it in `imported/`, reported `rejected`, and it is not
  retried. An image whose OCR reads no text also stays in `imported/` and adds no document.
- A move never overwrites a file. Symlinks, sub-folders, hidden files, iCloud files that are not
  downloaded, and files modified in the last 5 seconds are left alone.

To read a file again (a `rejected` one included), delete its document if it has one
(`brain rm`), then `brain ingest <folder>/imported/<file>`; an image is stored
confidential unless you pass `--sensitivity normal`.
