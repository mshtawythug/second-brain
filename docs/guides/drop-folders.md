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
  the first, and the first is ingested again, which adds nothing when its document exists
  (and recovers it if a crash left it without one). Two different files with one name
  become two documents.
- A configured folder that is missing (an unplugged drive) is skipped with a warning naming
  its entry (`folder[0]`); the others still run, and `brain doctor` warns until it is back.
- A failure that may pass next time (the database was down) moves the file back, to be tried
  again next run. A failure that would repeat (an unreadable or oversized image, an OCR
  timeout, the secret guard) leaves it in `imported/`, reported `rejected`, and it is not
  retried. An image whose OCR reads no text also stays in `imported/` and adds no document.
- A move never overwrites a file. Symlinks, sub-folders, hidden files, iCloud files that are not
  downloaded, and files modified in the last 5 seconds are left alone.

To read a file again (a `rejected` one included), delete its document if it has one
(`brain rm`), then `brain ingest <folder>/imported/<file>`; an image is stored
confidential unless you pass `--sensitivity normal`.

## Voice memos (RM-38)

A drop folder can also take voice memos (`.m4a`, `.mp3`, `.wav`). Each memo is transcribed
on this machine by whisper.cpp, and only the transcript enters the brain: the audio stays in
the folder's `imported/`, and the 16 kHz WAV made for whisper is deleted straight after.
Nothing leaves the machine.

### 1. Install whisper.cpp and a model

```bash
brew install whisper-cpp        # provides whisper-cli
```

brain never downloads a model. Download one yourself from the `ggerganov/whisper.cpp`
repository on Hugging Face and put it anywhere, for example `~/models/`. The recommended
model is `ggml-base.en.bin` (English only, about 148 MB). **No real model was run by the
tests that shipped this feature:** they use a fake transcriber. To check a model on your
machine, run the one live test with `BRAIN_LIVE_WHISPER_MODEL` naming the file:
`BRAIN_LIVE_WHISPER_MODEL=~/models/ggml-base.en.bin pytest tests/test_ingest_voice.py -m live_whisper`.

Then name the model in `$BRAIN_HOME/.env`:

```bash
BRAIN_WHISPER_MODEL=~/models/ggml-base.en.bin
BRAIN_WHISPER_MAX_MINUTES=60    # the default; longer memos are skipped
brain doctor                    # the `voice memos` line should be OK
```

`afinfo` and `afconvert`, which read and convert the audio, are built into macOS.

### 2. Configure a folder

```toml
[[folder]]
path = "~/VoiceMemos"
kind = "voice"
```

`kind = "voice"` gives the folder the audio extensions and stores each memo
**confidential** (a memo is private by nature); set `sensitivity = "normal"` on the entry to
change that. Get memos in by sharing or exporting them from the Voice Memos app into the
folder. brain never reads the Voice Memos app's own store.

### What happens to a memo

- `afinfo` reads its length (10 s timeout). A memo longer than `BRAIN_WHISPER_MAX_MINUTES` is
  `rejected` and left in `imported/`.
- `afconvert` makes a 16 kHz mono WAV (60 s timeout); the WAV's size is checked against the
  cap again.
- `whisper-cli` transcribes it, with a timeout of twice the memo's length plus a minute. The
  run's own 8-minute limit stops *new* files from starting, never a memo already being
  transcribed, so at the 60-minute cap one run can last about **two hours** (at most 2 × 60 +
  1 minutes of transcription, plus the run's 8 minutes). The LaunchAgent never starts a second
  run while one is still going.
- A memo of silence adds no document and stays in `imported/`.
- **Rejected, never retried:** too long, not readable as audio, or any timeout. To try one
  again, `brain ingest <folder>/imported/<file>`. It is stored confidential, under source
  `manual`, unless you pass `--sensitivity normal`.
- **Moved back, retried next run:** whisper failing on the converted WAV, which points at the
  model or the binary rather than the memo. Each time, the run prints a warning naming the
  memo's `imported/` name (it carries the content hash) and the error class. A memo that
  shows up in that warning run after run is the one to look at: check `brain doctor`, then
  try `brain ingest` on it by hand.
- With voice memos off (no `BRAIN_WHISPER_MODEL`), no model file, or no `whisper-cli`, voice
  folders are skipped with the fix printed, and their files are not moved.
