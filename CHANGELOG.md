# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Two version axes

This repository ships two independently versioned artifacts, each with its own
git tag scheme:

- **`v*` tags** — the **brain CLI** (the Python package `secondbrain-py`). These
  entries below track that axis. A `v*` tag cuts a GitHub Release and publishes
  the sdist+wheel to PyPI (`.github/workflows/release.yml`).
- **`age-image-*` tags** — the prebuilt **Apache AGE Postgres image**
  (`ghcr.io/mshtawythug/second-brain-age`, tag
  `pg16-v1.5.0-rc0-pgv0.8.6`). Its version string is pinned to the AGE +
  pgvector versions baked into the Dockerfile, not to the CLI version, and it is
  published by `.github/workflows/publish-age-image.yml`. Bumping the CLI does
  not rebuild the image; rebuilding the image does not bump the CLI.

## [Unreleased]

### Added — `brain ingest-code`: your own files from a local git repository (RM-43, PR #113)

- `brain ingest-code <repo-dir> --author-email E [--author-email E2 …] --since YYYY-MM-DD`
  stores the tracked files that a commit by one of your addresses touched since that day, one
  document each (source `code`, content type `code`), chunked one definition per chunk by the
  RM-42 code chunker. Both scope flags are required (exit 2 without them). `--ext` and a
  subdirectory narrow the run. Each address matches the whole address, case-insensitively.
- Always skipped and counted: git-ignored files (a force-added ignored file included),
  `node_modules`, `.venv`, `vendor`, `dist`, `build` and `third_party` directories, lockfiles,
  minified files, files over 200 KB, binary or non-UTF-8 files, empty files, and anything that is
  not a regular file inside the work tree.
- The secret guard always runs in `reject` mode for this command, whatever `BRAIN_SECRET_GUARD`
  says, and there is no `--allow-secrets`. A refused file is named with its finding kinds and
  line numbers, never the match, and counted on its own summary line.
- `confidential` by default (`--sensitivity normal` for a personal repository). Summaries are off
  unless `--enrich`. `--max-files` (default 2,000) caps the files written a run; unchanged files
  are free, and a re-run updates changed files in place.
- `git` runs by its absolute path with a minimal environment, a timeout and an output cap. The
  scanned repository is treated as untrusted (security review sec-113). Its config cannot make
  `git` run a command: lazy fetch and every transport are off, and hooks, fsmonitor and
  signature display are forced off. Global config is not read (`GIT_CONFIG_GLOBAL=/dev/null`). A
  partial clone, or a `core.worktree` outside the named directory, is refused (exit 1), and
  only files that resolve inside that directory are read. `brain doctor` gains a `git` row (a
  WARN when `git` is not on `PATH`).
- **Concept-skip policy `v2` → `v3`:** `code` joins `CONCEPT_SKIP_CONTENT_TYPES`. As with any
  change to the set, the next `brain graphrag build --backfill` re-reconciles every existing
  `calendar_event` and `screenshot` document once, with an empty extraction and no model call.
- **Fixed (BL-35):** `brain vault sync` re-chunks an edited `_ingested/` mirror through the
  chunker its content type calls for, so an edited `code` mirror keeps its definition-level
  chunks instead of being re-chunked as prose. A `code` mirror also materializes no wiki links:
  a `[[Name]]` in code would otherwise have linked the file to a note titled "Name".
- **Fixed (shared runner, macOS):** a CLI that wrote past its output cap and had already exited
  was reported as failing to start (`PermissionError`), because macOS answers `killpg` on a group
  of unreaped children with `EPERM`. The runner behind `gh`, `osascript`, the voice tools and now
  `git` treats that as a group already gone.

### Added — a code-aware chunker for the `code` content type (RM-42, PR #109)

- Documents with `content_type="code"` are chunked at definition boundaries,
  one definition per chunk. Python is split with the stdlib `ast` module at
  top-level functions and classes; a class over the budget is split by method,
  and the module docstring and imports form a header chunk. Python that does not
  parse (Python 2, syntax errors) and JavaScript/TypeScript, Go, Rust, Java and
  shell use a table of top-level declaration patterns. A comment, attribute or
  decorator block directly above a declaration goes with it. Every chunk starts
  with a `path:start-end symbol` line, and the path is put on one line without
  control characters and capped in length.
- Every code chunk, header included, stays within the prose chunker's ceiling
  (`INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS`). A definition over
  it is packed by blank-line blocks, then lines, then characters.
- Every other content type chunks exactly as before. No extractor produces `code`
  yet (RM-43 adds it), so no stored chunk changes. New leaves are
  `brain/ingest/chunk_dispatch.py` (`chunk_for`) and
  `brain/ingest/code_chunker.py`. `ingest/__init__.py`'s two chunking calls now
  go through `chunk_for`, at net zero lines. The path and language hints are
  read from the document's `metadata` (`path`, `language`). Tree-sitter is not
  used (RM-42.D1).

### Added — share from the phone into the capture inbox (RM-39)

- A "Save to Brain" iOS Shortcut (step by step in `docs/guides/mobile-capture.md`) writes what
  you share, plus an optional comment, as a `.md` file into an iCloud Drive folder. On the Mac,
  that folder is a drop folder with the new `mode = "capture"`. Each file becomes a
  capture-inbox note (`capture/`, tagged `inbox`, titled as `brain capture` titles one), and the
  file is then **deleted**, because the folder is in iCloud. There is no HTTP endpoint and no
  network surface on the Mac (RM-39.D1).
- `brain capture import --dir PATH --since Nd [--dry-run]` imports one capture folder.
  `brain ingest-folder --configured` (and the drop-folder LaunchAgent) runs capture folders with
  the others. Both go through `brain.capture_import.import_file`, so they cannot diverge.
- **The shared file is untrusted.** No YAML parser reads it: only a leading `---` block's
  `sensitivity:` line is honoured, and every other key is dropped. A share can raise the
  folder's level to `confidential`, never lower it. An unknown level, or a block that does not
  close within 40 lines and 4 KiB, means `confidential`. A file over 1 MiB, or one that is not
  UTF-8, is `rejected` and stays in `imported/`. Shared text is not scanned for secrets (as
  with `brain capture`).
- **Content is never lost or written twice.** A failure before the note is written moves the
  file back for the next run. Once the note file exists, an indexing failure is reported
  `written` (the next vault sync indexes it), and the file is deleted rather than retried. The
  same file (name and bytes) is one note: its key is kept as `capture_import` in the note's
  frontmatter.
- A capture folder takes `.md`/`.txt` and defaults to `normal` sensitivity; naming a `kind`
  for it is an error. A capture folder that is the vault, lies inside it or contains it is
  refused (RM-37's `import_sync.is_within` rule, applied both ways), because it would take
  the vault's own notes as shares. A share whose only text is inside its `---` block keeps the
  whole text. An ingest step's `unchanged` is now reported `duplicate` on every drop-folder
  path. Evicted iCloud files and `.icloud` placeholders wait until they download.
  The guide states that shares pass through Apple's iCloud and recommends Advanced Data
  Protection.
- `brain.vault.note_builder.create_vault_note` is now `write_vault_note` followed by
  `index_vault_note`, with no change in behaviour. `write_vault_note` takes an
  `extra_frontmatter` mapping, which can never set a brain-managed field. The note file is now created exclusively
  (`O_EXCL`, retrying `-2`, `-3`… on a taken name), so two concurrent writers can no longer
  overwrite each other, and a failed write leaves no partial file.

### Added — the Ingest tab's inbox folder (RM-40)

- `brain ui`'s Ingest tab gains **From the inbox**: pick, by name, a file already sitting in
  the inbox folder and ingest it. The inbox is the first `drop-folders.toml` entry with
  `kind = "manual"` and `auto = false`, so the `--configured` LaunchAgent never ingests it
  first; it takes the uploadable types (`.md`, `.markdown`, `.txt`, `.pdf`, `.docx`) its entry
  lists. The file is moved into the inbox's `imported/` folder first (RM-33's move-first step,
  through the new `brain.drop_folder.run_one`), ingested from there under source `manual`
  with the folder's sensitivity (confidential unless set), and so appears in the tree.
- `GET /api/ingest/inbox` lists names and sizes only (never a path), and only on a loopback,
  writable server; `POST /api/ingest/inbox {name, ...}` queues a job of kind `ingest.inbox`.
- A new traversal surface, guarded three times: the name must be one plain file name
  (no `/`, `..`, NUL, control character or leading dot) and is checked with `vault/paths.py`'s
  resolve-then-compare rule and `lstat` at submit; the job checks it again before anything
  moves; and the move opens it `O_NOFOLLOW` against the folder's descriptor. A symlink, a
  folder, a pipe, an iCloud-only file or a file over the upload caps is refused, and a file
  swapped after it was listed or submitted is never read.
- Every failure (database, embedder, secret guard, unreadable file, cancel) puts the file
  back in the inbox; a file with no text stays in `imported/`. Job errors name the cause by
  class or fixed code only. Guide: `docs/guides/drop-folders.md#the-ingest-tabs-inbox-rm-40`.
- Hardening from the PR #110 review: the job ingests the exact file the submit checked or
  nothing (its identity, one hard link, and the size cap are checked on the opened file, and
  the hash reads at most the cap plus a byte, so a file that grew or was swapped is refused
  unread); a symlink loop or an inbox that cannot be searched is a 4xx, never a 500; a file
  with a second hard link is refused; the listing reads at most 5,000 entries off the event
  loop and is fetched only when the user picks Inbox mode. A file already in `imported/`
  under the same content-addressed name is held to the same checks before it counts as the
  same file, and the copy used on a volume without hard links is bounded by the cap too. A
  result that cannot be read back after the commit is reported as saved, never as a failure
  that would put the file back in the inbox.

### Added — voice memos, transcribed on this machine (RM-38)

- A drop folder with `kind = "voice"` takes `.m4a`, `.mp3` and `.wav` memos (shared or
  exported from the Voice Memos app; brain never reads the app's own store). Each is
  transcribed locally: macOS's `afinfo` reads its length (10 s timeout), `afconvert` makes a
  16 kHz mono WAV in a private temporary folder (60 s), and `whisper-cli` from whisper.cpp
  (`brew install whisper-cpp`) transcribes it, with a timeout of twice the memo's length plus a
  minute. Only the transcript enters the brain, as a `voice_memo` document under the new
  `voice` source kind (🎙️ in `brain ui`, month headers under `_ingested/voice`), stored
  **confidential** by default on every path; the folder's `sensitivity` or `--sensitivity`
  overrides it. The tools get no stdin, only `PATH`, `HOME`, `LANG` and `TMPDIR` from the
  environment, and their stderr is never read.
- `BRAIN_WHISPER_MODEL` names a whisper.cpp model file; unset means voice memos are off, and
  brain never downloads one. `BRAIN_WHISPER_MAX_MINUTES` (default 60, up to 600) is the longest
  memo transcribed. The converted WAV's size is checked against the cap too, so a file whose
  header understates its length is still caught.
- A memo over the cap, one that is not audio, or any timeout is `rejected` and stays in
  `imported/`; `brain ingest <imported file>` tries it again. Whisper failing on the converted
  audio points at the model or the binary, so the memo is moved back for the next run, with a
  warning naming its `imported/` name and the error class. A memo's transcription runs on its
  own timeout, not the run's 8-minute limit, which only stops new files from starting. With
  voice memos off or `whisper-cli` missing, voice folders are skipped with the fix printed,
  and their files are not moved.
- `brain ingest <memo>.m4a` works too (source `manual`, confidential by default); with voice
  memos off it is a one-line refusal. Audio stays out of `brain ingest-dir`'s default walk and
  is not uploadable in `brain ui`.
- `brain doctor` gains a `voice memos` line, and its `drop folders` line warns when a voice
  folder cannot be transcribed. No real whisper model is run by the test suite; one
  `live_whisper` test runs the real tools when `BRAIN_LIVE_WHISPER_MODEL` names a model.
  Guide: `docs/guides/drop-folders.md#voice-memos-rm-38`.
- Hardening from the PR #107 review:
  - The memo is opened once, without following a symlink, and `afinfo` and `afconvert`
    both read that open file (`/dev/fd/N`). A file swapped in after the check is never read,
    and neither tool sees the memo's name. Before this, a name holding a newline and a fake
    `estimated duration` line could set the duration.
  - `afconvert` runs under a file-size limit of the cap plus a second, so a memo whose
    header understates its length cannot fill the disk. Hitting the limit is `rejected`
    as too long, whether `afconvert` is killed by SIGXFSZ or stops with the WAV cut off.
    A memo of exactly the cap is still transcribed. The stored duration is measured from
    the WAV, not read from the header.
  - Whisper printing more than 4 MiB is `rejected`; it is not retried.
  - A memo whisper fails on three times is `rejected` instead of being retried every run.
    The count is kept per memo content in `$BRAIN_HOME/state/voice-failures.json`, and a
    success clears it. A tool that cannot start is not counted.
  - `whisper-cli` found through a relative `PATH` entry is refused.
  - The model file must be owned by you (or root), not group- or world-writable, and not
    inside a drop folder. Every directory above it must also be owned by you or root and
    not group- or world-writable (a sticky one such as `/tmp` is fine), since whisper
    opens it by path after the check.
    Otherwise voice memos are off, and `brain doctor` says why.
  - In `brain ingest-folder`, stopping a run (Ctrl-C, or SIGTERM from `launchctl bootout`)
    kills whisper's whole process group and deletes the temporary WAV. The Apple Notes
    and GitHub readers share that runner, so a Ctrl-C or an exception there also kills
    their tool's process group; SIGTERM is turned into a clean exit only around
    `ingest-folder`. Image OCR runs `tesseract` through its own runner and is unchanged.
  - A run that was killed outright leaves its temporary folder behind. The next run with a
    voice folder deletes this user's such folders once they are older than 3 hours, or
    longer when `BRAIN_WHISPER_MAX_MINUTES` allows longer runs (about 20 hours at 600).
    A folder another run removes during the sweep is skipped.
  - The failure count is best-effort: if `$BRAIN_HOME/state` cannot be written, the run
    prints a class-only warning and carries on. A memo's count is cleared only once it
    is stored.

### Added — `brain ingest-dir --sync`: an imported directory's deletions are mirrored (RM-37)

- `brain ingest-dir <dir> --sync` lists, after the walk, the documents imported from files under
  `<dir>` that are no longer on disk; `--apply` deletes them through the same path as `brain rm`
  (row, chunks, graph entry, vault mirror). Dry run by default. Only `manual` documents with a
  `source_path` under `<dir>` (by path components) and in this run's extensions are candidates;
  vault notes and stdin documents never are, documents under a nested brain vault are skipped,
  and a file that cannot be checked is kept. On a case- or normalization-insensitive volume a
  case-variant `<dir>` matches only when the file system confirms it is the same directory.
- A file whose folder may be on an unmounted volume is kept, not pruned: its nearest existing
  folder is (or sits below) a mount point, is on another device, is an empty folder below
  `<dir>`, or holds a mount point. `--apply` re-reads each row and skips one that changed since
  the listing (`kept (row changed)`).
- Refusals (exit 1, nothing written): a missing `<dir>`, a `<dir>` inside the brain vault, an
  empty walk while documents were imported from `<dir>`, and, with `--apply`, more than half of
  the documents under `<dir>` and more than 5. `--allow-empty` overrides the last two. The
  mass-deletion refusal is stricter than the ticket and was added at plan approval.
- New leaves `brain/ingest/import_sync.py` (the rules) and `brain/cli_import_sync.py` (the
  output); `cli_ingest.py` gains the three options and the wiring. No migration, no new kind.

### Added — screenshots from a drop folder become searchable (RM-33)

- `brain ingest-folder --dir PATH --since 7d` (one folder) or
  `brain ingest-folder --configured --since 2d` (every folder in
  `$BRAIN_HOME/drop-folders.toml` with `auto = true`). Each new file is moved to
  the folder's `imported/` as `<stem>-<sha8><ext>`, then ingested from there and
  titled by its original name, so two different files that arrive with one name
  stay two documents. The same file dropped twice deletes the drop copy, only
  after its full SHA-256 matches the earlier import, and re-ingests that import:
  nothing is added when its document exists, and a file a crash left in
  `imported/` without one is ingested. A move never overwrites a file, and a
  transient failed ingest returns the file to the folder. A configured folder
  that is missing (an unplugged volume) is skipped with a warning naming its
  entry; the other folders still run. A locked file (Finder's "Locked") is reported
  `failed` and left where it is; it never stops the files after it. Symlinks,
  sub-folders, hidden files (iCloud stubs and in-progress screenshots), iCloud
  files evicted from the disk, and files modified in the last 5 seconds are left
  alone. A run stops after 200 files a folder or 8 minutes, and the rest wait for
  the next run. Without `--dir` or `--configured`, and `--since`, the command exits 2.
  A failure every run would repeat (an unreadable or oversized image, an OCR timeout,
  the secret guard) leaves the file in `imported/`, reported `rejected`, so it is not
  retried every 10 minutes; only a transient failure (the database) moves it back.
  Files are moved by identity, not by name: the folders are held open, and a file
  swapped for a symlink or a pipe mid-run is refused. File names are printed, and
  titles stored, on one line with control characters removed.
- A new image extractor OCRs `.png`, `.jpg` and `.jpeg` with the local
  `tesseract` (RM-32's engine) into a `screenshot` document, stored
  **confidential** by default on every path, `brain ingest x.png` and
  `brain ingest-dir --ext png` included (a screenshot can show anything; the secret
  guard still runs). An explicit `--sensitivity normal` is honoured.
  `brain ingest` of a symlinked or unreadable image is a one-line refusal (exit 1),
  not a traceback. Only Pillow's PNG and JPEG decoders are reachable. A file over 50 MiB,
  an image over 70 million pixels or with a side over 32,767 is refused from its
  header before it is decoded, and `tesseract` reads a grayscale PNG that brain
  re-encodes, never the original file. Images stay out of `brain ingest-dir`'s
  default walk. With OCR off or no `tesseract`, image folders are skipped with
  the fix printed, and their files are not moved.
- A new `screenshot` source kind, with a 📸 glyph in `brain ui` and month
  headers under `_ingested/screenshot`. `screenshot` skips concept extraction.
  That changed the concept-skip set, so its policy version went from `v1` to
  `v2`: the next graph build or backfill re-reconciles every `calendar_event`
  document once, with an empty extraction and no model call.
- `brain-install-launchd --with-dropfolders` installs the opt-in
  `com.brain.dropfolders` LaunchAgent, which runs
  `brain ingest-folder --configured --since 2d` every 10 minutes. Like the
  reranker it is kept by every later install (including `brain-up`'s) and removed
  only by `brain-uninstall-launchd`. `brain doctor` gains a `drop folders` line,
  and its `daemons` line checks the job only once it is installed. Guide:
  `docs/guides/drop-folders.md`.

### Added — Notion pages from Notion's Markdown export (RM-35)

- **`brain ingest-notion <export-path>`** reads a Notion "Export → Markdown & CSV" folder or
  `.zip` and stores each page as a `notion_page` document under the new `notion` source kind. No
  Notion API, no token: the export you chose is the scope. Titles lose Notion's 32-hex id suffix.
- Each page is deduplicated by its page id, parsed from the export's file names, not by its path:
  re-export to a new folder or zip and re-run, and every page updates in place. A file whose
  name carries no id, and every copy of an id that appears twice, is skipped and counted, never
  guessed.
- Images and attachments are skipped and counted; so are nested archives (unzip the outer one and
  pass the inner one). Database rows are ingested, one document per row page, only with
  `--with-databases`; a row's last-edited property dates it. `--since YYYY-MM-DD` keeps pages
  last edited on or after that day (pages without the property are kept and counted).
- `--max` (default 200) caps the pages written a run and prints how many remain, so a large
  workspace drains over several runs; unchanged pages do not count. `--sensitivity`, `--tag`,
  `--no-enrich` and `--allow-secrets` as for the other ingest commands.
- The export is read as untrusted input: a zip is read member by member in memory and never
  extracted; a member name that is absolute or leaves the archive, a symbolic-link, encrypted or
  duplicate member, more than 100,000 entries, or more than 512 MiB of pages and tables refuses
  the whole archive; a page over 8 MiB is skipped. The central directory is found as `zipfile`
  will find it (ZIP64 included) and its entries are counted header by header, in constant memory,
  before `zipfile` parses it; a ZIP64 layout that Python releases with and without the
  CVE-2025-8291 zipfile fix would read differently is refused. A
  folder is walked without following links; hard-linked files are skipped, and a file replaced
  between listing and reading (by a link, a FIFO, a folder or another file) is refused.
- A new page whose text matches an existing document's, and an empty page, are reported by id
  and counted as not stored, never as unchanged or as remaining under `--max`. Files are read
  as UTF-8 with a leading byte-order mark dropped.
- Titles have control and format characters removed and are cut at 300 characters. Output names
  pages by id, never by title, including the secret guard's notices (`refused <id>: N
  finding(s)`).

### Fixed — a duplicated calendar invite no longer rewrites the other event (BL-34)

- `brain ingest-calendar` and `brain ingest-github`: a new event or item whose
  text was identical to an already-stored document (the same meeting on two
  calendars, a duplicated invite) matched that document through the ingest
  pipeline's content-hash dedup. The run then overwrote the OTHER document's
  occurrences, date and attendees (GitHub: state, labels and times), reported
  it "refreshed", and never stored the new one. Now only an item's own row is
  ever refreshed: the new one is reported as `same body as another event: <id>`
  (`… another item: <id>` for GitHub) and counted, and the other document is
  left untouched. A result with no document is reported `empty <id>` instead of
  failing an assertion. The summary line gains these counts only when they are
  non-zero. One rule, `brain.ingest.volatile_patch.settle`, now serves calendar,
  GitHub and Apple Notes.

### Added — `brain ingest-apple-notes`: named Apple Notes folders (RM-36)

- `brain ingest-apple-notes --folder NAME` (repeatable, required; macOS only)
  stores the notes of the named folders as documents of the new `applenotes`
  source, read through `osascript -l JavaScript`. The program sent to Notes is
  fixed and read-only; folder names reach it as JSON data only. Bodies are
  converted from Notes' HTML to Markdown by a stdlib converter (headings,
  lists, checklists, links); attachments are skipped and locked notes are never
  read (they are counted). Notes are stored `confidential` unless
  `--sensitivity normal`. `--since`, `--max` (default 200, the rest reported),
  `--dry-run`, `--tag` and `--no-enrich` are offered. Dedup is by the Notes id;
  a moved note updates only its metadata. Without `--folder` it exits 2; off
  macOS, 1. `/usr/bin/osascript` (never a `PATH` lookup) is stopped after 120 s;
  the converter is linear, nesting and output are capped, and a body over
  2,000,000 characters is skipped. `brain doctor` gains an
  `apple notes` row, and `docs/guides/apple-notes.md` documents macOS's
  Automation permission prompt.
- `brain.ingest.capped_process` holds the timeout-and-stdout-cap runner that
  `gh` (RM-31) used, now shared with the `osascript` reader; `gh`'s behaviour
  and messages are unchanged.
- Shared leaves, behaviour unchanged: `brain.ingest.volatile_patch` (the
  stored-row lookup and the metadata-only refresh that `ingest-calendar`,
  `ingest-github` and `ingest-apple-notes` each carried) and
  `brain.ingest.text_safety.one_line` (moved from `github_docs`, which
  re-exports it).

### Added — image-only PDFs are OCR'd (RM-32)

- A scanned PDF or a photographed receipt used to ingest as an empty document
  ("skipped (empty document)"). When a PDF's text layer averages under 25
  characters a page and a page within the cap carries an image object, `brain` now renders up to
  `BRAIN_OCR_MAX_PAGES` (default 50) pages at 200 dpi and reads them with the
  local `tesseract` binary, 60 seconds at most per page. The text is stored as
  `[page N]` blocks, and `metadata.ocr` records the page count and engine.
  A PDF with a real text layer (25 or more characters a page on average), or
  with no image within the cap, is never OCR'd; a sparse one that carries an
  image is, and the OCR text replaces its text layer. A page renders to at most 70 million
  pixels, counted as pdfium rounds them, with no side over 32,767 (a page that
  fits only below 50 dpi is skipped); a document declaring any one image over
  280 million pixels (masks and annotation appearances included) is not OCR'd,
  checked from the headers before anything is decoded; and one document gets at
  most 10 minutes of OCR, so a forged page size or a long scan cannot exhaust
  memory or hold the `brain ui` job worker.
- New knobs `BRAIN_OCR` (`auto` or `off`), `BRAIN_OCR_LANG` (default `eng`) and
  `BRAIN_OCR_MAX_PAGES`, in `brain/settings/ocr.py`; `brain doctor` gains an
  `ocr` line. Without `tesseract` (`brew install tesseract`), an image-only PDF
  is still skipped as empty, with a one-line hint.
- `pypdfium2` and `Pillow` are now explicit base dependencies. `pdfplumber`
  already required both, so no new package is installed.

### Changed — the `ingest-brain` skill keeps its per-source recipes in `sources/`

- The Gmail, Krisp, Slack, Jira and GitHub recipes moved out of
  `skills/ingest-brain/SKILL.md` into `skills/ingest-brain/sources/<source>.md`,
  with the text unchanged. SKILL.md keeps the routing table (each row links its
  recipe), the shared commands, the context-cost section and the safety rules,
  and is down from 298 lines to 165 of the 300-line ceiling, so later sources
  add a file instead of growing it. `bin/brain-skills-sync` already copies whole
  skill directories; new tests pin that nested files install and that drift in
  one is caught, and the skill-text guards now scan the reference files too.
  Re-run `bin/brain-skills-sync` after pulling.

### Added — your pull requests, issues and READMEs as documents (RM-31)

- `brain ingest-github` reads your code-host activity through the `gh` CLI and
  its own login: only items you authored, are assigned or reviewed. It refuses
  to run without `--repo OWNER/REPO` or `--mine`, and `--since` (exit 2);
  `--repo` narrows to those repositories and never reads other contributors'
  items there. It stops at 500 documents a run, saying so. One document
  per PR, issue and README, deduplicated by `<owner>/<repo>#<n>` and
  `<owner>/<repo>:README` under the new `github` source.
- The body keeps only your own comments; `--with-thread` adds everyone's. State,
  counts and labels live in metadata, so a merged PR is a metadata-only update
  (no re-chunk, no re-embed), and an unchanged item is not read again.
- Items from private or internal repositories, or from a repository whose
  visibility could not be read, are stored `confidential`. No `gh` output or
  stderr is printed or stored: every error names only the command and its exit
  code.
- `brain doctor` has a `github` row (is `gh` on `PATH`?), and the `ingest-brain`
  skill has a recipe for the GitHub MCP connector when `gh` is not installed.
- The ingest secret guard (and the pre-commit hook) now also catches the code
  host's `gho_`, `ghu_`, `ghs_` and `ghr_` tokens, not only `ghp_` and
  `github_pat_`.

### Added — calendar events as documents (RM-30)

- **`brain ingest-calendar --since YYYY-MM-DD --until YYYY-MM-DD`** stores Google Calendar events
  as documents through the `gws` CLI: one `calendar_event` document per event, and one per
  recurring series, under the new `calendar` source kind. Both dates are required; the window is at
  most 92 days and ends at most 14 days ahead. `--calendar` (default `primary`), `--dry-run`,
  `--tag`, `--enrich` (no summary by default) and `--allow-secrets`.
- Meetings without a transcript are now searchable, filterable with `--source calendar` and
  `--person`, and dated by their start time. Attendee addresses are stored only as participants,
  never in the body. One cleaner treats the title, location, description and attendee names: it
  removes every link and address (obfuscated forms included) and drops any line with a dial-in
  number, PIN, passcode or meeting ID. A title keeps the words before the secret, so it stays
  findable by name. A date is told apart from a phone number by being a whole, valid date
  ("2026.10.06" stays; "01.70.37.22.46" is a phone number and goes). The rules prefer privacy to
  recall, so an ordinary line that looks like a dial-in is dropped too. Declined, untitled,
  cancelled, working-location and out-of-office entries are skipped, and private events (or
  Google's legacy `confidential` visibility) are stored as `confidential`. The `gws` read gets a
  120-second timeout and a 16 MiB output cap, and errors are printed by class only.
- A series' occurrence dates live in its metadata, so a re-run that only finds new occurrences
  updates the metadata without re-chunking or re-embedding. Each run is merged into what is
  stored (the latest ten start times known, and the latest date), so backfilling an earlier window
  never moves a series backwards, and an occurrence that is no longer in the calendar stops
  dating the series. A series whose own record cannot be read keeps its stored text.
- The graph's people aspect now resolves a calendar event's attendees
  (`people._doc_participant_keys` reads its participants). Because `brain review weekly` tallies
  its "key people" through the same function, a week's meetings now count their attendees'
  addresses there too, as its Gmail threads already did. That is intended: a meeting is key-people
  signal. Confidential documents stay out, as before.
- `brain ui` shows a calendar glyph for the new kind, and groups `_ingested/calendar` by month.
- **Concept-skip policy.** `graph_rag/extract_policy.py` names the content types whose concept
  aspect is never extracted (`calendar_event` for now). Such a document is reconciled with an
  empty extraction and a policy watermark, so the next `brain graphrag build --backfill` counts it
  as skipped, and its people aspect still runs. Adding a type and bumping
  `CONCEPT_SKIP_POLICY_VERSION` strips that type's earlier concept mentions on the next backfill.

### Added — Jira issues as a source, through an agent recipe (RM-34)

- **A new source kind, `jira`.** `brain ingest-stdin --source jira` is accepted, and the facet
  panel, the source filter and `[[jira:<host>/<KEY>]]` links pick it up. In `brain ui` a Jira row
  gets its own glyph. `_ingested/jira/` has no month headers in the tree, which is intended.
- **The recipe lives in the `ingest-brain` skill, not in code.** The Atlassian MCP connector
  fetches the issues, and the skill pipes each one into `brain ingest-stdin --source jira
  --content-type jira_issue --sensitivity confidential`. There is no Jira API client. The scope is
  in the recipe: by default the JQL is
  `(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d`, with the
  parentheses. The agent refuses a JQL with no time bound, and project-wide pulls are not part of
  the recipe. Issues ingest as `confidential`, because work-tracker content is the employer's.
  Status and assignee go in metadata, never in the body. Other people's comments go in only on
  request.
- **`brain ingest-stdin --help` lists the accepted kinds from the one definition.** It had listed
  four kinds as a literal, so it would have called `jira` invalid.

### Fixed — the vault watcher no longer loses a delete across a shutdown or a lost connection (RM-26)

- A file deleted (or renamed away) just as the watcher shut down, or while it
  could not reach the database, kept its row until `brain vault sync --prune`:
  the startup sync never prunes. The watcher now writes each such delete to
  `$BRAIN_HOME/state/watcher-owed.jsonl` and applies it at its next start,
  after the startup sync. A path whose file is back on disk is never deleted.
- A delete whose database write landed just before the connection dropped no
  longer leaves the document's graph vertex behind (it used to need
  `brain graphrag remove`). Graph removes the watcher could not apply at
  shutdown are written to the same journal and applied at the next start.
- The journal is mode 0600 and holds vault-relative paths and document ids,
  never content. A line it cannot read is skipped with a warning; the rest is
  applied. Only `brain vault sync --watch` keeps it.

### Added — the local reranker runtime: a guide, a doctor line and an opt-in launchd job (RM-14b)

- **`docs/guides/local-reranker.md`** installs `llama-server` (llama.cpp 0.7.0, from Homebrew)
  and the `bge-reranker-v2-m3` Q8_0 GGUF (635,676,416 bytes, with its sha256). It measured the
  server's resident memory at about 804 MiB, and it covers port 8081 already being in use.
- **`brain-install-launchd --with-reranker`** installs `com.brain.reranker`, a `KeepAlive`
  LaunchAgent that runs `llama-server --embedding --pooling rank --reranking` on
  `127.0.0.1`. Its arguments are fixed in the template; only the model path
  (`BRAIN_RERANK_GGUF`) and the port vary. The port is `BRAIN_RERANK_URL`'s, so the job listens
  where searches look: 8081 by default. The host is always `127.0.0.1`. The flag fails before
  touching anything when the model path is relative or names no file, or when
  `BRAIN_RERANK_URL` is not `http://127.0.0.1:PORT` / `http://localhost:PORT` with PORT in
  1024–65535. On `brain-up`'s flagless run, a value that has since gone bad leaves the installed
  job as it was. That run prints one WARNING naming the variable and installs the other jobs. The agent stays installed: the flagless install `brain-up` runs re-renders it and
  never removes it, and `brain-uninstall-launchd` removes it.
- **`brain doctor`** gains a `reranker` line. With `BRAIN_RERANKER=llamacpp` it calls
  `GET {BRAIN_RERANK_URL}/health` with a 2 s timeout, and it reports `not running` or
  `not ready` as a WARN, never a FAIL. The `daemons` line checks the reranker agent only when
  it is installed.
- Nothing changes until you opt in: `BRAIN_RERANKER` still defaults to `none`, and `brain
  setup` does not offer the reranker.

### Changed — the log-privacy guard covers its three recorded blind spots (RM-28)

- The test guard that keeps exception text out of `src/brain`'s log lines now
  also catches a value derived from an exception and logged after its `except`
  block, an exception stored on an attribute or appended to an attribute's
  container, and a nested `def` or `lambda` (its defaults included) that logs
  the handler's exception. It follows every binding form (`for`, `with … as`,
  comprehensions, `match` captures, `except*`), sees `traceback.format_exc()`
  and `sys.exc_info()`, and fails when an allowlist entry goes stale. Its rules
  moved to `tests/log_privacy_rules.py`, its allowlists to
  `tests/log_privacy_allowlists.py`. It found no new leak in `src/brain`; no
  runtime behaviour changes.
- What it still cannot see is listed in its module docstring; backlog BL-28
  tracks the two big classes, exception text interpolated into a new
  exception's message and exception text echoed to stderr.

### Fixed — the graph build survives a concurrent delete; a reconcile re-checks the participants (RM-27)

- **`brain graphrag build` no longer stops when a document is deleted while it runs.** A document
  deleted after the build listed it but before the build read it is now skipped, and the build
  report (`BuildResult`) counts it as `skipped_deleted`. One deleted later in its reconcile
  (during the model call, or while waiting on the graph write lock) is dropped and counted as
  `skipped`, as before. Before, the whole build stopped with a "not found" error. The
  `brain graphrag build` footer and the MCP build tool do not show `skipped_deleted` yet
  (follow-up BL-26).
- **A reconcile no longer commits people from participants that were just edited.** The graph
  write used to check only that the document's body was unchanged. An edit that changed only the
  participants (a recipient added to a thread) could be overtaken by an older reconcile, which then
  wrote the older people. The write now checks every document input the plan read: the body hash,
  the content type, the source kind, and the participants. If any of them changed, it writes
  nothing, and the next reconcile indexes the edit.
- **The short cross-type collapse window is accepted, not fixed.** For a moment between its two
  steps, a merged concept can show a lower-cased display name. Only the display name is affected,
  and the second step restores it. The reason it is not worth closing is recorded in
  `graph_rag/cross_type.py`.

### Added — `--entity` search filter (RM-24)

- **`brain search --entity "<name>" [--entity-type TYPE]`** returns only documents
  that mention that graph entity (`graph_entity_mentions`, tenant-scoped). The name
  resolves by canonical key (plus the target of any curated alias rule), then by
  display name. Every leg, the total and `--facets` see the filter.
- **MCP `brain_search`** takes `entity` and `entity_type`.
- Not found, ambiguous, the graph off or absent, or an alias file
  (`BRAIN_GRAPH_ALIASES_PATH`) that cannot be loaded: exit 2 / `INVALID_PARAMS`.
  The not-found message points at `brain graphrag entities`; the alias-file
  message names the failure's class only, never the file's text.
- Built on the coordinator's ruling adopting the roadmap spec's §5 U4
  recommendation; no user answer yet.

### Fixed — two terminal error reports no longer quote exception text (RM-29)

- **`brain vault export`** reports a file it could not write as `<path>: <class>`,
  for example `OSError[ENOSPC]` for a full disk. It used to print the exception's
  message.
- **`brain backfill scan-secrets --action redact`** reports a document it could
  not rewrite by the failure's class, in the text report and in `--json`. The
  usual case, a redacted body identical to another document's, reads "redacted
  body collides with another document's content hash"; it used to name the
  other document's id.
- **`brain backfill scan-secrets --action mark-confidential`** and
  **`brain mark-confidential` / `mark-normal`** report a vault file they could
  not update by the failure's class (for example `OSError[ENOSPC]`) instead of
  the exception's message.

### Fixed — a replacement note renamed at once no longer inherits the old note's identity (RM-25)

- When a note was replaced by an unrelated file that had no frontmatter `id`,
  and the file was renamed within one watcher debounce window, the vault
  watcher gave it the old note's id and every backlink pointing at it (H-1,
  the residue PR #52 recorded). An id-less rename destination is now compared
  by content: the word 5-gram shingles of the old row's stored content and of
  the file's body. Below a Jaccard overlap of 0.2 it is a different document,
  so the old row is deleted and the file is indexed fresh with its own id.
- A note with fewer than 8 shingles on either side (short or empty) keeps the
  in-place rename, as before, because an overlap that small means nothing. A
  file that carries an `id` is still decided by the id alone.
- The accepted trade: a long note rewritten almost entirely and renamed within
  one debounce window is treated as new, and loses its backlinks.

### Added — the engagement readout: `brain usage --engagement` (RM-22a)

- **`brain usage --engagement [--json]` says whether there is enough engagement
  data to learn from.** It counts document opens and clicks by surface, the
  distinct documents opened, the `rated useful` and `rated irrelevant` ratings,
  and the share of reads that came from a search session, over `--days`. It
  also reports the `interactions` table's rows and bytes, which the future
  pruning ticket (RM-58) needs.
- **The gate line is always judged over 90 days**, whatever `--days` says. It
  needs 500 reads across 100 distinct documents and 30 explicit ratings. These
  are named constants in `brain.engagement`, with their rationale. The
  interactions-aware ranking stage (RM-22b) is built only once the gate is met.
- Reads include the `opened` row `brain ask` records for each citation in an
  answer. The search-session share tells them apart from reads after a search.
- Counts only: no title, query, document id or agent. `brain usage` without
  the flag is byte-identical to before (a golden test captured before the flag
  landed). `--raw-queries` with `--engagement` exits 2.

### Added — `brain ui`: Copy path (RM-23)

- **The note pane has a Copy path button**, so a note can be opened in any editor.
  On a loopback server it copies the absolute path of the note's file, and
  otherwise its path inside the vault. The status beside it says **Copied**, or
  why the copy failed (a denied clipboard permission, for example).
- **It is absent over `--lan`.** The browser's clipboard API exists only in a
  secure context, and a plain `http://` LAN page is not one.
- **`GET /api/notes/{id}` gains `abs_path`, only on a loopback bind** (the same
  bind classification as the Ingest tab's gate), and never for a note whose body
  is withheld or whose vault file is missing. There is no `obsidian://` link, and
  the server never starts an editor.

### Added — tag rename, merge and suggested merges (RM-21)

- **`brain tags`**: `list [--json]`, `rename OLD NEW`, `merge A B --into C` and
  `suggest-merges [--limit N] [--json]`. `rename` and `merge` are a dry run
  unless `--apply`: they print the mapping and every document and saved search
  they would change.
- `--apply` rewrites every place a tag lives: the document row, its chunks'
  tag text (so `brain search --tag NEW` and a text query for the new tag find
  it at once), the frontmatter of its vault file, and saved searches' `tag` /
  `without_tag` filters. It runs per document, like
  `brain backfill normalize-tags`, so a re-run finishes an interrupted one and
  is otherwise a no-op. `updated_at` is not bumped.
- A vault file that cannot be rewritten (unreadable, broken frontmatter, or a
  path outside the vault) no longer stops the run, in `brain tags` and in
  `brain backfill normalize-tags` alike: it is reported by error class, counted
  in the summary, and the command exits 1 after finishing everything else. The
  database row is already updated, so a re-run does not retry the file: fix it
  by hand. Every `vault_path` is checked to stay inside the vault first.
- `suggest-merges` ranks pairs by close spelling, singular/plural and
  co-occurrence on the same documents, each with its evidence.
- MCP: `brain_tags_list` and `brain_tags_suggest_merges`, read-only, with
  confidential documents left out by default. There is no MCP rename or merge.
- `brain backfill normalize-tags` now runs on the same engine
  (`brain.tag_mapping`); its output is unchanged, byte for byte.

### Added — near-duplicate detection: `brain dedup` (RM-19)

- **`brain dedup scan` finds pairs of near-copies** (a forwarded email, a re-sent
  draft, one file ingested twice by two paths): word 5-gram MinHash signatures,
  verified at an estimated Jaccard similarity of at least 0.85. Documents under
  50 words are skipped, and two documents of one Gmail thread are never paired.
  A scan signs only new and edited documents (`--all` re-signs everything,
  `--limit N` caps a run). No new dependency.
- **It flags; it never refuses, merges or deletes.** `brain dedup list` shows the
  pairs; `dismiss A B` and `confirm A B` label one. A dismissed pair is never
  re-opened by a later scan, an edit that breaks an open pair drops it, and
  deleting a document deletes its pairs.
- The daily brief job runs `brain dedup scan --limit 500` on its own line before
  the brief, so a failed scan never stops it, and the brief gains a read-only
  **Possible duplicates (N)** count. The `--wiki` page and the delivered message
  count only pairs whose two documents are neither confidential nor drafts.
- MCP **`brain_dedup_list`** (read-only) lists a pair only when neither document
  is confidential, unless `include_confidential=true`. The Ingest tab's job result
  fills `possible_duplicates`, with a confidential near-copy's title, id and path
  withheld unless `brain ui` serves confidential titles.
- **Upgrade:** migration 030 (`brain init`), and reinstall the launchd daemons,
  because the brief job's script changed.

### Added — follow-up questions in the Agent tab (RM-18c)

- **Follow up** on an answer in `brain ui`'s Agent tab asks the next question
  with the earlier turns as context, so "and when?" plans searches for the
  thing you were asking about. A chain of follow-ups carries at most the last
  three questions and answers.
- Only the search plan sees the earlier turns. The new answer is written from
  the current question and what the searches find now, so a claim from an
  earlier answer cannot reach it without a citation.
- The earlier turns come from the answers the page is showing. Nothing new is
  stored in the browser: a reload ends the conversation, and only ask ids are
  remembered, as before. A question asked without pressing **Follow up** sends
  no history. A follow-up always plans, so *Quick answer* is off for it.
- `POST /api/ask` accepts `history: [{question, answer}]`. The server checks its
  shape (400 `invalid_history`, `history_turn_too_long`; `history_requires_loop`
  with `no_loop`) and caps it itself, at three turns and 2,000 tokens, oldest
  dropped first. `GET /api/ask/config` reports `max_history_turns`. `brain ask`
  and MCP `brain_ask` are unchanged.

### Added — the brief lists who and what is new this week (RM-17)

- **`brain brief` gains a "New this week" section**: the people, organisations
  and projects whose first mention in a non-draft document was ingested in the
  last 7 days, most-mentioned first, at most 10. It is read from the GraphRAG
  entity tables, so it is absent when `BRAIN_GRAPH_ENABLED=false` or the tables
  do not exist.
- Each surface uses its own lens: the terminal and `--json` list every such
  name; the `--wiki` page, the delivered message and MCP `brain_brief` (by
  default) leave out an entity known only from confidential documents. An
  entity known only from drafts is listed nowhere. `brain_brief` and
  `brain brief --json` carry it as `new_entities`.

### Added — the Agent tab: ask your brain from the browser

`brain ui`'s **Agent** tab is no longer a placeholder (roadmap ticket RM-18b, the
front end of RM-18a's backend, and the model-output hardening pass the UI spec
requires before model text reaches the page).

- Ask a question, optionally as a quick answer with no planning loop, and watch
  each plan, retrieve, reflect and synthesize step as it runs. **Cancel** works
  until the answer is ready; a question waiting behind an ingest job says so.
- The answer cites its notes: each in-range `[n]` marker is a link that opens the
  cited note in the Notes tab, and the citation list beside it gives each note's
  title, source and snippet. A citation withheld by the confidential lens shows
  no title and no link.
- Model output is text, never markup. The answer, the sub-queries and every
  server string are inserted with `textContent`; a marker becomes a link only when
  its number is a returned citation whose id is a canonical UUID; no model string
  becomes an attribute; and the Content-Security-Policy is unchanged (no inline
  script). A static test fails the build if the tab's modules gain any HTML sink.
- The tab remembers the asks it submitted by id only (in their own scope, so they
  never appear in the Ingest tab), and is disabled, with the reason, on a
  `--read-only` or LAN-bound server.

### Added — the daily brief can be delivered to you (RM-16)

- **`BRAIN_BRIEF_DELIVERY`** (`none` by default, `email` or `slack`): once
  `brain brief --wiki` (the 07:00 job) has written the page, it sends the same
  content to the owner, by email through `gws gmail +send` to `BRAIN_USER_EMAIL`
  only, or to a Slack incoming webhook (`BRAIN_BRIEF_SLACK_WEBHOOK_URL`, a secret,
  `https://hooks.slack.com/` only). The subject is always `[brain brief]
  YYYY-MM-DD`.
- What is sent is the published page's payload: confidential documents never
  leave, and by default only titles and counts.
  `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true` adds the todo text and the
  suggestions built from it.
- A delivery problem never fails the brief: one class-only warning, exit 0
  (a `gws` authorization failure names `bin/gws-reauth`). When delivery was
  configured and did not happen, the watch hits stay unnotified, so the next
  brief sends them again.
- `brain doctor` gains a `brief delivery` row (the channel and its
  prerequisite, never the webhook URL).
- `brain ingest-gmail` never ingests your own delivered brief: a fetched message
  is skipped when it is from `BRAIN_USER_EMAIL` and its subject starts with
  `[brain brief]` (after any `Re:` / `Fwd:` / `Fw:`, so your replies and
  forwards of a brief are skipped too). Mail from anyone else with that subject,
  and your other mail, are still ingested. The Gmail query itself is unchanged.
- Draft documents are never delivered (they stay on the local page), and the
  Slack post turns link previews off.

### Added — the Agent tab's backend: ask over HTTP

`brain ui` can now answer a question as a job (roadmap ticket RM-18a, the server
half of the Agent tab; the tab itself is RM-18b, above).

- `POST /api/ask` with `{question, no_loop?}` queues an ask (202) on the Ingest
  tab's single worker, so an ask and an ingest never run at once. It runs the same
  plan, retrieve, reflect and synthesize loop as `brain ask`, in `hybrid` mode.
  `GET /api/ask/config` reports whether asking is available and why not.
- Progress, result and cancel reuse `/api/jobs/{id}`: each step is an event, the
  result is `brain ask --json`'s shape plus each citation's stored sensitivity and
  `elapsed_ms`, and a cancel is honoured at every step, chat call and embed until
  the answer is recorded. A cancelled ask records nothing.
- The whole ask is capped at `BRAIN_ASK_MAX_ITERATIONS` × 3 ×
  `BRAIN_ASK_TIMEOUT_SECONDS` (`timeout`). Failures carry fixed, class-only
  messages (`chat_unavailable`, `context_overflow`, `output_truncated`,
  `synthesis_failed`, `embedder_unavailable`, `database_error`, `internal_error`).
- Privacy: answers draw on confidential documents only where the server serves
  confidential bodies; a citation of a confidential or unknown-level document is
  withheld, with the answer, where it does not. Ask results quote document text,
  so they are never listed by `GET /api/jobs` and are kept for 10 minutes.
- Asking is refused on `--read-only` and over `--host` / `--lan`, like ingest.

### Fixed — a finished UI job outlived its retention

`GET /api/jobs/{id}` and its cancel now drop a job past its retention. Before,
expiry ran only when a job was submitted or the list was read, so a finished job
stayed readable by id for as long as neither happened.

### Added — saved searches, reported in the daily brief (RM-15)

- **`brain watch`** saves a search: `add NAME QUERY` (with `--source`, `--tag`,
  `--person`, `--kind`, `--without-tag`, and `--semantic` to match by meaning),
  `list`, `check [--peek]`, `hits`, `pause`, `resume` and `rm`. A watch reports
  documents that are new, or edited, since its last check, each once.
- **The daily brief** checks every enabled watch first and lists the new hits in a
  **Watch hits** section. The `--wiki` page lists at most 5 per watch, leaves out
  confidential documents, and marks what it found as notified, so the next page
  does not repeat it; the terminal brief and `brain watch hits` still show them.
- **MCP**: `brain_watch_list`, `brain_watch_add`, `brain_watch_check` (peek only:
  an agent never consumes the brief's notifications) and `brain_watch_remove`
  (needs `confirm=true`). `brain_brief` carries `watch_hits`.
- **`BRAIN_WATCH_MIN_COSINE`** (0.45) for semantic watches.
- **Migration 029** (`saved_searches`, `saved_search_hits`): run `brain init`.
  Until it is applied, the brief builds as before, without the section.

### Changed — `brain brief`'s build moved to `brain.brief_publish` (RM-15)

- The two-audience build (terminal payload, gated `--wiki` page) moved out of
  `cli.py` into `brain.brief_publish.build_brief`, unchanged in behaviour.

### Added — the Ingest tab: upload a file

The **Ingest** tab can now upload one file per job (roadmap ticket RM-13, phase 3
of the Ingest tab design): `.md`, `.markdown`, `.txt`, `.pdf` or `.docx`.

- **Upload a file** beside **Paste text**. Choose a file or drop it on the zone,
  which is also a button, so the keyboard path is the same. The file is uploaded
  as soon as it is chosen, shown with **Remove**, and ingested as a job on the same
  panel when you press **Ingest**, with optional tags, sensitivity and title.
- Refused before anything is kept: a type outside the list, a name that reduces to
  nothing, a file over its cap (5 MiB for text and markdown, 25 MiB for PDF and
  Word, 64 MiB staged in total), or bytes that do not match the extension (a PDF
  must start `%PDF-`; a `.docx` must be a zip holding `word/document.xml` and pass
  zip-bomb bounds; text must be UTF-8 without NUL). Caps are enforced on the
  stream, so an oversize body is never held in memory.
- New routes: `POST /api/ingest/uploads` (the raw bytes, `application/octet-stream`,
  name in `X-Brain-Filename`), `POST /api/ingest/file` (queue the job) and
  `DELETE /api/ingest/uploads/{id}`. The upload route is the one place the server
  accepts a body that is not JSON; a test over every mutating route proves it is
  exactly that one route and method. No multipart parser is used.
- Staged files live in a private (`0700`) per-process temporary directory and are
  deleted when their job ends however it ends (a queued job cancelled before it
  ran included), when removed, once 15 minutes old and unsubmitted (checked at the
  next upload or submit), and at shutdown; a dead server's leftovers are swept at
  the next start. Filenames are never logged.
- An uploaded document dedups on its extracted text, not a path: the same file
  uploaded twice is **Already in your brain**, but a file once ingested with
  `brain ingest <path>` becomes a second document.
- `GET /api/ingest/config` now reports the uploadable `extensions` (an explicit
  allow-list, checked against the extractor registry) and their caps. A job
  result carries `original_filename`, withheld with the title for a confidential
  document.

### Added — a cross-encoder reranker stage, off by default (RM-14a)

- **`BRAIN_RERANKER`** (`none` by default, `llamacpp` or `voyage`) adds a
  reranking stage to the search pipeline, so `brain search`, `brain explain`,
  MCP `brain_search` and `brain_explain`, `brain ui`, `brain recall` and `brain
  ask` reorder their top `BRAIN_RERANK_DEPTH` (20) results by a cross-encoder's
  score. With the default nothing changes. Knobs: `BRAIN_RERANK_DEPTH`,
  `BRAIN_RERANK_TIMEOUT_S`, `BRAIN_RERANK_URL` (loopback only),
  `BRAIN_RERANK_MODEL`, `BRAIN_RERANK_MAX_CHARS` (`docs/configuration.md`).
- **`llamacpp`** talks to a local `llama-server` over loopback HTTP; **`voyage`**
  uses the hosted `rerank-3` and never receives a confidential document.
- **`brain search --rerank/--no-rerank`** and **`brain explain --rerank/--no-rerank`**
  override the setting for one run.
- **Never fails a search.** A timeout or an unreachable backend leaves the
  results in hybrid order, exit 0, with one stderr line: `reranker unavailable;
  results are in hybrid order`.
- `score` is not overwritten; `brain explain --json` shows
  `explain.reranker_score`. The footer and `brain ui`'s meta line show the
  `rerank` time.
- **`brain eval`** applies the configured stages itself (it still never falls
  back), adds the reranker's backend, model and depth to `config_signature` only
  while it is on, and records each row's `stage_ms` in `brain eval --json`. A
  query for which the reranker skipped (it timed out) is marked `stages_skipped`
  and treated like a degraded row: `--record-baseline` refuses the run (exit 1),
  and `--fail-below` refuses to judge it (exit 1). The committed `ci.json` is
  unchanged.

### Added — the Ingest tab: paste from the browser

`brain ui`'s **Ingest** tab is no longer a "deferred" placeholder (roadmap ticket
RM-11, phase 2 of the Ingest tab design). It pastes text or markdown into the
brain through the RM-10 backend below.

- A form with a title, the body, tags, the type (`note` or `transcript`), the
  sensitivity (`normal` or `confidential`) and "skip the summary". The size limit
  and the choices come from the server, and an over-size body is refused before
  anything is sent.
- A job panel: queued, then embedding with a progress bar, then the outcome.
  **Added** links to the new document in the Notes tab; a body pasted before says
  **Already in your brain**. It shows the summary and graph status, masked secret
  findings, and the note when a confidential paste stays off a hosted embedder.
  Cancel works until writing starts, and a refused or too-late cancel says so.
- The panel remembers this browser tab's jobs across a reload, without
  re-announcing them. A job the server no longer knows is reported as "outcome
  unknown, re-submitting is safe" only if this tab never saw it finish; a finished
  job the server later evicted (after an hour, or past fifty jobs) just drops off.
- A finished row stays finished: a poll or a cancel answer that was already in
  flight cannot put it back to running or offer Cancel again.
- A confidential document's title is never shown unless
  `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` is on: the panel says the title is withheld
  and offers no link. It never puts back the title you typed.
- On a `--read-only`, `--host` or `--lan` server the tab is disabled and names the
  reason. Keyboard-only use, both themes and reduced motion (a still progress
  bar) are covered by two new browser suites, `tests/test_ui_browser_ingest.py`
  and `tests/test_ui_browser_ingest_lifecycle.py`.

### Added — MCP `brain_related` and `brain_explain`

Two read-only MCP tools give an agent what `brain ui` and `brain explain` already
show a person (roadmap ticket RM-12).

- `brain_related(id, limit=10, include_confidential=false)` returns the documents
  related to one document, best first: the same ranking, in the same order, as
  `brain ui`'s related-notes panel. Each row is `{id, title, vault_path, source,
  score, snippet}`, and the cosine floor used is echoed as `vector_sim_floor`.
  `limit` may not exceed 20. An empty list means no candidate cleared the
  ranking floors; on a brain of under about 40 documents that often happens
  even when similar documents exist (backlog BL-16).
- `brain_explain(query, limit=10, source, tag, since_days,
  include_confidential=false)` returns, for each result, the same `explain`
  object `brain explain --json` prints (FTS and vector ranks, each leg's RRF
  contribution, the recency boost, the best chunk, the filters applied), without
  snippets. It carries the `fallback*` and `stages` keys `brain_search` does.
  `limit` may not exceed `BRAIN_SEARCH_MAX_LIMIT`, and `since_days` must be at
  least 1.
- Both leave confidential documents out unless `include_confidential` is true,
  like every MCP retrieval tool. `brain_related` also refuses (`INVALID_PARAMS`)
  to list the neighbours of a confidential document without that opt-in, because
  that list would describe what is withheld.
- They are the first tools in `src/brain/mcp_tools/`, so `mcp_server.py` did not
  grow. `brain explain --json` now builds its `explain` object with the
  projection the MCP tool uses; its output is unchanged.

### Added — the Ingest tab's backend: background jobs and paste ingest

`brain ui` can now ingest pasted text over HTTP (roadmap ticket RM-10, phase 1 of
the Ingest tab design). The browser panel is the next ticket, RM-11.

- `POST /api/ingest/paste` takes a title, a body, tags, a content type (`note` or
  `transcript`), a sensitivity (`normal` or `confidential`) and `enrich`. It
  answers 202 with a job. The document is an ordinary ingested-tier document from
  source `manual`, written through the same pipeline as `brain ingest*`. It is
  deduplicated by content: pasting the same body again reports "skipped" with the
  existing document's id. It never syncs the graph; the vault watcher does that.
- `GET /api/jobs`, `GET /api/jobs/{id}?after=<seq>` and
  `POST /api/jobs/{id}/cancel` let a client poll a job's progress and cancel it.
  A cancel accepted before writing starts always writes nothing. Once writing
  has started, cancel answers 409. A result whose document is confidential has
  its title, vault path and document id withheld unless
  `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` is on, the same rule the tree follows. That covers a paste that lands, as a
  duplicate, on an existing confidential document. The job list carries no
  results at all.
- `GET /api/ingest/config` says whether this server may ingest and why not, and
  `GET /api/health` gains `ingest_enabled`. Ingest is offered only on a loopback,
  writable server: `--read-only` refuses it, and so does any `--host` / `--lan`
  bind (403 `ingest_requires_loopback`).
- Jobs run one at a time on a background thread, with at most four waiting.
  They are kept in memory only: after a restart, re-submitting is always safe,
  because identical content is a no-op. Paste bodies are capped at 2 MiB, and
  the cap is enforced while the request streams in.
- `brain.ingest.outcome.classify` is now the one place that turns an ingest
  result into "ingested / updated / skipped / empty / mirror repaired". The
  CLI's wording is unchanged.

### Added — one search pipeline for every search surface

`brain search`, `brain explain`, MCP `brain_search`, `brain ui`'s search,
`brain recall` and the retrieval behind `brain ask` now all search through one
function, `brain.search_pipeline.run_search` (roadmap ticket RM-09). It runs the
search, the text-only fallback, and then any configured ranking stages, in that
order, so a later stage (a reranker, a document-level leg) is wired in one place
and reaches every surface at once. No stage ships yet, so results are unchanged.
`brain search --json --meta` and MCP `brain_search` gain one always-present key,
`stages`, after the four `fallback*` keys: `[]` today, and one
`{name, applied, skipped_reason, ms}` object per stage once one is configured. A
stage that fails is reported as skipped (class name only) and costs that stage,
never the search. The bare `--json` list, `brain explain --json` and the
`brain ui` response are unchanged. A new test fails if any other module calls
the search directly.

### Added — feature settings leaves, where new knobs go

New configuration knobs now live in small per-feature modules under
`brain.settings` instead of `config.py`, which is over the 800-line ceiling
(roadmap ticket RM-08). A leaf is a frozen dataclass and one loader that reads
`os.environ` after `Config.load()` has published the `.env` chain into it, so
a leaf knob is set in the same `.env` files as every other knob. The shared
parsers in `brain.settings._env` (`env_bool`, `env_choice`, `env_int`,
`env_float`, `env_str`) treat an unset or blank variable as the default, and
refuse a bad value with a `ConfigError` that names the variable and never quotes
the value, so a secret cannot leak through an error message. No existing knob
moved, and nothing changes for any current setting. How to add a knob is in
`docs/configuration.md`, "Adding a knob: feature settings leaves".

### Added — an extractor registry, and one definition of the source kinds

File extractors now register in a new leaf module, `brain.ingest.dispatch`,
instead of a table inside `ingest/__init__.py`, which is over the 800-line
ceiling (roadmap ticket RM-07). An `ExtractorSpec` names its extensions, loads
its extractor and its parser's error types lazily, and can stay out of
`brain ingest-dir`'s default walk (`in_directory_walks=False`), so a later image
or audio extractor does not change what an existing `ingest-dir` run picks up.
`brain.ingest` still exports `extract_path` and `supported_extensions`.
`brain.vault.links` now re-exports `brain.source_kinds.VALID_SOURCE_KINDS`
instead of keeping its own copy, so a new source kind is defined in one place.
Nothing changes for any existing file type or source kind: the same five
extensions map to the same four extractors, in the same order, with the same
error messages. How to add either is in `CONTRIBUTING.md`, "Adding a file
extractor or a source kind".

### Fixed — `ingest_document`'s docstring says when the summary is written

It said the auto-summary was written "inside the same transaction". It has run
after the commit, as its own autocommit `UPDATE`, since Wave perf-T3 (backlog
BL-12). Documentation only.

### Added — registration seams for new CLI commands and MCP tools

New commands and MCP tools can now be added without editing `cli.py` or
`mcp_server.py`, both of which are over the 800-line ceiling (roadmap ticket
RM-06; the smallest scope of backlog BL-2, whose full split is RM-48). A new
command module adds one entry to `cli_registry.EXTENSION_REGISTRARS`, which
`cli.py` runs once after its last built-in registration. A new MCP tool is a
module in the new `brain.mcp_tools` package: it receives the server's helpers
as a `ToolContext` through `register(app, ctx)` instead of importing
`brain.mcp_server`, and a module refuses to be bound to a second, different
context. `python -m brain.mcp_server` now serves the imported module, so an
extension tool reads the state `main()` set. No command or tool is added,
renamed or changed: the CLI command tree (110 command paths, with their options)
and the MCP tool list (42 tools, with their schemas) are identical before and
after. How to add either is in `CONTRIBUTING.md`, "Adding a command or an MCP
tool".

### Added — the offline test resolves every CSS `url()` in `brain ui`

A new test module, `tests/test_ui_static_css_urls.py`, checks every `url()` in
every shipped stylesheet, as `tests/test_ui_static_assets.py` already checked
`index.html`'s `href`/`src` (roadmap ticket RM-05, backlog BL-3). The test
fails, naming the stylesheet and the target, when a target does not exist, is
empty or query-only (has no path), names a directory, leaves `static/` (through
`..`, a root path outside `/static/`, another origin or a symlink), or names a
file that no `package-data` glob ships. `url()`s inside comments are ignored,
`data:` and `#fragment` targets pass (including a `data:` SVG that nests its own
`url(%23id)`), and a `url(` the test cannot parse fails instead of going
unchecked. Test only: no `brain` command changes.

### Added — CI fails when the file-size ceiling table and the tree disagree

`scripts/check_ceiling_table.py` compares CLAUDE.md's file-size ceiling table
with the modules under `src/brain/` that are over 800 lines (roadmap ticket
RM-04, backlog BL-1). It exits 1, naming the file and what to do, when a file
crosses 800 lines without a row, when a row's file is deleted or back under the
ceiling, or when a row is malformed (no Lines cell, a Lines cell without
`wc -l src/brain/<path>` or naming the wrong file, a duplicate row); it exits 2
when the table's header row is missing or appears more than once, or when
`CLAUDE.md` or `src/brain/` is missing. It runs as a step in `ci.yml`'s `lint`
job and in `bin/brain-ci`'s lint stage, and `tests/test_bin_brain_ci.py` now
fails when a lint step exists in one of the two and not the other. Development
tooling only: no `brain` command changes.

### Changed — `brain ui` search falls back to text-only matching, with a notice

When the query embedding cannot be computed, the Notes tab in `brain ui` now
shows full-text matches instead of an error, with a notice above the results
saying they are text-only and why (by failure class only), and the meta line
still reads `fts-only` (roadmap ticket RM-03). `GET /api/search` answers 200
with the four always-present `fallback*` keys `brain search --json --meta` and
MCP `brain_search` carry (`null` on a normal search); it used to answer `503
embedding_unavailable`. The next search retries the embedder, and the page size
is the same with and without a fallback.

### Changed — `brain ask` answers from text-only retrieval when the embedder is down

`brain ask` and MCP `brain_ask` no longer fail when the query embedding cannot
be computed: they retrieve through the same text-only fallback `brain search`
uses, and still answer (roadmap ticket RM-02). The answer text is never edited.
The CLI prints `Retrieval: text-only (semantic ranking unavailable)` under it
and the one-line notice on stderr (exit code 0). `--json` and `brain_ask` carry
the four always-present `fallback*` keys (`null` on a normal run), separate
from `fallback_used`, which still means the planning loop was skipped or degraded.
`brain eval --answer` does not fall back: an embed failure still stops it with
exit 1, as in every `brain eval` mode.

Fixed in the same change: every other `ask` failure is now one fixed
`ask: <sentence>` line and exit 1 (MCP: `INTERNAL_ERROR` with the same
sentence). Ollama down, a prompt over the chat model's context window, an answer
cut off at its output budget, a synthesis error and a graph-leg failure each get
their own sentence. None of them prints the exception's message any more (the
old CLI text did), and a graph mode's own embed failure, which used to escape
as a traceback, is mapped too.

### Changed — `brain recall` falls back to text-only when the embedder is down

`brain recall` and MCP `brain_recall` no longer fail when the query embedding
cannot be computed: they pack passages from full-text matching alone, through
the same fallback `brain search` uses (roadmap ticket RM-01). The context
block's header ends `, text-only`, so an agent pasting the block can see the
ranking was lexical only. The CLI also prints the one-line notice on stderr
(exit code 0). `--json` and `brain_recall` carry the four always-present
`fallback*` keys (`null` on a normal run). A database error still fails as
before.

Fixed in the same change: the whole block now honours `--budget` for a long
query too. The header echoes the query, and the fixed reserve held back for it
could not cover a long one. The query the header echoes is now cut to 16 tokens,
with a trailing `…`. The JSON `query` field keeps the full text, and a shorter
query is echoed exactly as before.

### Changed — search falls back to text-only when the embedder is down

`brain search`, `brain explain` and MCP `brain_search` no longer fail when the
query embedding cannot be computed (Ollama down, a timeout, a Voyage error, a
model not pulled, a bad API key). They answer from full-text matching alone and
say so: a one-line stderr notice on the CLI (exit code 0), and four new,
always-present keys on `brain search --json --meta` and `brain_search` —
`fallback` (`"fts_only"` or `null`), `fallback_reason` (the failure's class,
never its message), `fallback_transient` and `fallback_note`. A permanent
failure falls back too; its notice says a retry will not fix it and points at
`brain doctor`, except for a query longer than the embedder's context window
(`OllamaEmbedContextOverflow`), whose notice says to shorten the query.
Non-embedding errors (a database failure) still fail as before. The bare
`brain search --json` list keeps its shape. `brain eval` does **not** fall
back, so its degraded-run semantics are unchanged. `brain ui` is unchanged
too (rulings and follow-ups in
`docs/plans/2026-10-23-search-fts-fallback.md`); `brain recall` and `brain ask`
now fall back as well (entries above).

### Removed — the Quartz wiki (wiki-to-ui phase 9, R3)

`brain ui` is the reading surface; the rendered Quartz wiki, dormant since the
previous release, is deleted. The vault is untouched and still readable in
Obsidian or any editor, which is also what to use while Postgres is down (the
wiki-as-fallback option, RA-8, was declined).

- **Commands removed:** `brain vault render`, `brain wiki install`,
  `brain setup --skip-wiki` / `--wiki-port` (the `full` profile no longer offers
  a wiki or preflights Caddy/Quartz), `brain-rebuild --only quartz` and
  `--clean-cache` (both now exit 2; `--wiki-only` still exits 2 and names
  `--only mirror`), and `brain-monitor probe`.
- **`brain-up` no longer builds, serves or opens a wiki.** `BRAIN_WIKI_BUILD`,
  `BRAIN_OPEN_BROWSER`, `BRAIN_NO_OVERLAY`, `BRAIN_WIKI_PORT`,
  `BRAIN_WIKI_KEEP_BUILDS`, `BRAIN_WIKI_RELOAD` and `BRAIN_WIKI_BUILD_TIMEOUT_S`
  are no longer read. `brain-status` and `brain-monitor` report only the
  vault-sync watcher. `brain doctor` drops its `wiki build` and `quartz/npx`
  lines; a non-zero daemon exit is now reported the same way whatever its code.
- **Unchanged:** the vault mirror (`brain-rebuild --only mirror`), the People
  Hub pages, `brain vault relink-derived`, `brain daily`, `brain brief --wiki`
  (it writes to `<vault>/daily/`), and the `com.brain.watcher` /
  `com.brain.brief` daemons. `brain-install-launchd` still removes a leftover
  `com.brain.build`, and `brain doctor` still warns while one is installed.
- **Leftovers you can delete by hand.** Nothing on your machine is removed for
  you: `<vault>/.quartz/` (the Quartz workspace and its builds),
  `$BRAIN_HOME/Caddyfile` and any `caddy` you started to serve it, and
  `$BRAIN_HOME/build/`. The vault watcher keeps skipping `.quartz/` until it is
  gone, and `brain backup` leaves it out (see Fixed).

### Changed — the brain sizes its own Ollama context window

- **Every Ollama chat call now sends `options.num_ctx`** (new knob
  `BRAIN_OLLAMA_NUM_CTX`, default `8192`) instead of inheriting the server's
  `OLLAMA_CONTEXT_LENGTH`. On a server tuned to 65536, each brain-loaded
  `llama3.1:8b` runner used to allocate a 4,352 MiB (~4.25 GiB) KV cache
  (q8_0); at 8192 it is 544 MiB, **3,808 MiB (~3.7 GiB) less wired memory**,
  for prompts that never
  exceeded ~4k tokens. Embeddings have their own window (next section).
- **The first brain call after upgrading reloads the resident chat model once.**
  Another tool that uses the same model without its own `num_ctx` reloads it
  back, so the two alternate reloads while both are active.
- **A prompt that does not fit is now an error, never a silent truncation.**
  The brain sends `truncate: false` / `shift: false`, so an over-window prompt
  raises `OllamaContextOverflow`. With `BRAIN_OLLAMA_NUM_CTX` unset, the window
  grows past 8192 only as far as a raised `BRAIN_ENRICH_MAX_INPUT_TOKENS` /
  `BRAIN_AUDIO_MAX_INPUT_TOKENS` needs. An explicit value too small for those
  caps or the GraphRAG extract chunk fails at startup, naming the knob to raise.

### Changed — the embedder sizes its own Ollama context window

- **Every Ollama `/api/embed` call now sends `options.num_ctx`** (a new knob,
  `BRAIN_OLLAMA_EMBED_NUM_CTX`, default `2048`) and `truncate: false`. Before,
  the embedder inherited the server's `OLLAMA_CONTEXT_LENGTH`. The knob is
  separate from `BRAIN_OLLAMA_NUM_CTX` because the embedder is a different
  model with its own runner.
- **The default `arctic` backend loses no capacity and saves no memory.**
  It is a non-causal model with no KV cache, and `2048` is already its
  effective per-input limit, because Ollama's embedding batch size is 2048.
- **`qwen3` gains the most.** On a server whose `OLLAMA_CONTEXT_LENGTH` is
  ≥ 40,960, qwen3-embedding:8b used to load at its trained 40,960 window, a
  q8_0 KV cache of about 3 GiB. At `2048` the cache is about 153 MiB.
- **An input that does not fit is now an error, never a silent prefix.** The
  brain raises `OllamaEmbedContextOverflow`, which is an `OllamaEmbedError`, so
  existing handlers still catch it. The message names the knob. `brain eval`
  treats it as permanent: the run stops with exit `1` and that message instead
  of scoring the query `0.0` (see Fixed). Every stored
  chunk fits at `2048`: the measured maximum is 1,689 model tokens. A value
  below `1400` (the 700-token chunk ceiling ×2) fails at startup.
- **The first embed after upgrading reloads the embedding model once.** That
  takes seconds for arctic. Stored vectors are unchanged, so no re-embed is
  needed.

### Changed — the wiki is dormant (R2)

- **The next `brain-up` after upgrading removes the `com.brain.build`
  LaunchAgent.** `brain-up` runs `brain-install-launchd` on every start, and it
  now installs only `com.brain.watcher` (vault sync) and `com.brain.brief`,
  booting out and deleting a leftover `com.brain.build`. Nothing rebuilds the
  wiki in the background any more; Caddy keeps serving its last build.
  **The way back:** `brain-rebuild --only quartz` rebuilds the wiki once, and
  `BRAIN_WIKI_BUILD=1 brain-up` runs the old overlay + cold-start build. To get
  the rebuild daemon itself back, run `brain wiki install`, then
  `brain-install-launchd` from a release before this one. This release's next
  `brain-up` removes it again, so keeping the daemon means staying on the
  earlier release; on this release, rebuild on request with
  `brain-rebuild --only quartz`.
- **`brain-rebuild`:** the `wiki` stage is split. `mirror` (vault export,
  sync-summaries, prune-orphans) runs by default; the Quartz overlay and build
  are the opt-in `quartz` stage (`--only quartz`). `--wiki-only` is removed (it
  exits 2 and names `--only mirror` / `--only quartz`), and `--clean-cache` now
  requires `--only quartz`.
- **`brain-up`** builds the wiki only with `BRAIN_WIKI_BUILD=1`. Its banner and
  `brain-status` say the wiki is dormant, and `brain-monitor probe` explains why
  there is no rebuild to time.
- **`brain setup`** offers the launchd daemons whether or not the wiki is
  installed, since they are the vault-sync watcher and the daily brief. The
  offer is still default-No interactively and never made under
  `--non-interactive`. Setup no longer installs the `_brain-build-fg` shim.
- **`brain doctor`:**
  - the `wiki build` line reports the wiki dormant and never fails on its age
    (it used to FAIL after 3 days);
  - a new `vault watcher` line warns when the vault-sync watcher is not
    installed (macOS) or not running (elsewhere), because external edits will
    not reach search until `brain vault sync`. Off macOS, a process list that
    cannot be read (`ps` failed) is a separate "status unknown" warning, not
    "not running", and its remedy is to check with `ps` and start
    `brain vault sync --watch` only if none is running — the watcher may be
    up, and nothing stops a second one from starting;
  - a new `retired daemon` line warns while a `com.brain.build` plist is still
    installed.
- **`brain ui`:** the Publish tab is gone.

### Changed — `brain ui` is the reading surface; the wiki is demoted

- **`brain-up` no longer opens the wiki in a browser.** `BRAIN_OPEN_BROWSER`
  now defaults to `0` (it was `1`); set it to `1` to get the old behaviour.
  The banner leads with `read: brain ui`, and the wiki line is labelled legacy.
  The wiki kept building and serving — nothing was removed. (R2, above, then
  made it dormant.)
- **`brain setup`'s closing report** names `brain ui` ahead of the legacy wiki
  URL, and the MCP `brain_capture` description points at the `brain ui` tree
  instead of the Quartz UI.

### Added

- **`brain ui` reports the vault-sync watcher in its top bar.** When
  `brain vault sync --watch` is not running it says "Watcher not running —
  external edits will not appear in search" ("Watcher off" on a narrow
  screen), and "Watcher status unknown" when the process list cannot be read.
  It says nothing while the watcher runs. The page asks
  `GET /api/watcher` — one enum, no process details, behind the same token
  and Host checks as every other `/api/` route — at load, every minute while the tab
  is visible, and when you return to it. On macOS it can differ from
  `brain doctor`, which checks the `com.brain.watcher` plist rather than the
  process; see `docs/vault-and-wiki.md`.
- **`brain ui --lan`** — read from a phone or tablet on the same network. It
  binds this machine's LAN address and uses `--token` if given, otherwise mints
  one, then prints the URL to open. It cannot be combined with `--host`, and
  `brain ui` still never binds a non-loopback address without a token. Guide:
  `docs/guides/brain-ui-on-your-phone.md`.
- **A startup notice on wildcard binds** (`--host 0.0.0.0` or `::`): a browser
  on another device is refused, because a wildcard bind accepts only loopback
  Host names. The notice also says the token, not the bind, keeps other
  clients out: a program can send a loopback Host header by hand. The docs
  previously recommended `--host 0.0.0.0 --token` for remote reading, which
  never worked from another device's browser.
- **`--host` is judged by the address it will actually bind**, not by how it
  is spelled or named. An IP address is checked numerically (no DNS); a
  hostname is resolved ONCE, judged by EVERY address it resolves to, and the
  server binds exactly those addresses — the name is never looked up again,
  so a second DNS answer cannot move the bind away from what was judged. Any
  unspecified address makes it a wildcard (with the notice, and the URL of the
  wildcard's own loopback). `localhost` is the only loopback NAME, and only
  when every address it resolves to is loopback — so a name that points at
  `0.0.0.0` gets the notice, `localhost` mapped elsewhere needs a token like
  any other bind, and any OTHER name needs a token even when it resolves to
  `127.0.0.1` (`127.0.0.1.nip.io`, `ip6-localhost`, `localhost.` with its
  trailing dot): whoever controls that name's DNS could re-point it at the
  server and pass its Host check. Every spelling of the wildcard gets the
  notice and every loopback address literal is loopback (`127.0.0.2` and
  `::1` included).
  Spellings that bound something unexpected are refused, each with a usage
  error that says why:
  - an empty value, or leading/trailing whitespace: asyncio bound `0.0.0.0`
    and `::` together for `""`, with no notice;
  - any IPv6 zone (`%…`), link-local included: a browser cannot open a zoned
    URL, and an empty zone (`::%`) was read as the wildcard `::`;
  - a numeric spelling the system reads differently from its canonical form,
    such as `0`, `127.1` and `010.0.0.1`. The system reads `0` as `0.0.0.0`,
    and platforms disagree on `010.0.0.1` (8.0.0.1 or 10.0.0.1). The error
    names what the system would have bound;
  - an IPv4-mapped address such as `::ffff:127.0.0.1`, which the IPv6-only
    socket cannot bind;
  - a link-local IPv6 address without a zone (`fe80::1`): it binds only with
    a zone, and zones are refused;
  - a non-ASCII value (`localhoſt`, the Kelvin sign), and a name with an empty
    or over-long label (`.`, `a..b`, 64 characters), which escaped as a raw
    traceback;
  - anything that is neither an address nor a hostname, such as
    `127.0.0.1:8080`, `http://127.0.0.1` or `0.0.0.0/0` (a port goes in
    `--port`);
  - brackets that are unbalanced, repeated, or around anything but an IPv6
    literal: `[::1` used to fail later as "port already in use", and
    `[127.0.0.1]` is not URL syntax.
  This class of bug was first reported as closed after review round 8; it was
  not, because each fix refused one more SPELLING. Classifying by the resolved
  address closes it.
- **`brain eval` golden-corpus entries can pin `after:` / `before:` dates**
  (`YYYY-MM-DD`) in place of `since_days:`. They are `hybrid_search`'s own
  bounds on `coalesce(sent_at, ingested_at)`: `after` inclusive, `before`
  exclusive, both at UTC midnight. A `since_days` window moves with the clock,
  so a `recency` query drifted whenever a competing document was ingested (a
  daily brief, on most days) and `--fail-below` exited 3 with no change to
  search; a closed range in the past does not move. `load_corpus` refuses
  `after` without `before`, `after >= before`, a value that is not a plain
  date, and either bound mixed with `since_days`. Existing corpora keep
  `since_days` until you convert them, and converting one needs a baseline
  re-record. How to choose the dates: `tests/eval/baselines/README.md` (2).

### Fixed — the `full` profile pulled the old AGE image

- **`brain setup --profile full` named the AGE image tag `pgv0.8.2`.** The
  0.3.0 bump moved the image to `pgvector/pgvector:0.8.6-pg16` but left the
  old tag in the compose template that `brain setup` renders into
  `$BRAIN_HOME/docker-compose.yml`. That tag still exists on GHCR, so a full
  install pulled the 0.8.2 image. If the pull failed, the local build compiled
  0.8.6 and labelled it `pgv0.8.2`. The template now names
  `pg16-v1.5.0-rc0-pgv0.8.6`, matching the Dockerfile. `bin/brain-graphrag-gif`
  had the same stale tag and is fixed too.
- **What this does to an existing full install.** Nothing changes until you
  re-run `brain setup --profile full`. Run it with the stack stopped: while
  Postgres is up, the port preflight stops setup before it writes anything.
  The re-run rewrites `$BRAIN_HOME/docker-compose.yml`, then runs
  `docker compose pull` and `up -d` itself. So in that same run it pulls the
  0.8.6 image and recreates the Postgres container. The major version stays
  PostgreSQL 16, so the data directory is used as it is. The `vector`
  extension in each database stays at 0.8.2 until you run
  `ALTER EXTENSION vector UPDATE;` there. Check with
  `SELECT extversion FROM pg_extension WHERE extname = 'vector';`.
- This supersedes the 0.3.0 note that "existing local deployments keep running
  their current image until you rebuild deliberately". It still holds for a
  deployment whose compose file `brain setup` does not render, such as a
  repo-root `docker-compose.override.yml`.

### Fixed

- **`brain backup` archived the leftover Quartz workspace.** Every full backup
  carried `<vault>/.quartz/`, the deleted wiki's `node_modules`, builds and
  cache, which on an upgraded vault is about 11 GB that nothing reads.
  `brain restore` paid that cost twice, because it takes a pre-restore backup
  first. The backup now leaves out `<vault>/.quartz/` without walking into it,
  and says so in its report. It skips only that top-level directory: other
  dotted directories, such as `.obsidian/` and a vault `.git/`, are user data
  and are still backed up. `vault_file_count` counts what the archive holds.
  A restore still renames the existing vault aside whole, so a `.quartz/` on
  disk moves with it and is never deleted.
- **`brain-up` repointed the background daemons at `~/brain-vault`.**
  `brain-install-launchd`, which every `brain-up` runs, wrote the launchd
  plists with `~/brain-vault` no matter what vault was configured. It ignored
  both an exported `BRAIN_VAULT_PATH` and the one in `$BRAIN_HOME/.env`. So an
  install with a custom vault got correct daemons from `brain setup`, and the
  next `brain-up` switched the vault-sync watcher and the daily brief to the
  default. It now resolves the vault the way `brain setup` does: an exported
  `BRAIN_VAULT_PATH`, then `$BRAIN_HOME/.env`, then `~/brain-vault`.
- **`BRAIN_MCP_LOG_LEVEL` was ignored when set in a `.env`.** `brain-mcp`
  configured logging before it loaded the config, and loading the config is
  what applies the `.env` files. So the knob worked only from the real process
  environment, such as Claude Desktop's `env` block, and never from the repo
  `.env` or `$BRAIN_HOME/.env`. The config now loads first. A value in the
  process environment still beats the `.env`. A missing `DATABASE_URL` still
  stops the server with the same error on stderr.
- **Re-running `brain setup` ignored `BRAIN_VAULT_PATH` and `VOYAGE_API_KEY`
  in an existing `$BRAIN_HOME/.env`.** Setup read both from the process
  environment only. Without `--vault`, a re-run fell back to `~/brain-vault`
  and used it for the wiki install and the launchd daemons, even though the
  `.env` named another vault. With `--embedder voyage` it refused under
  `--non-interactive`, or asked for the key again and added a second copy to
  the `.env`. Setup now falls back to `$BRAIN_HOME/.env` for both. `--vault`
  and a value in the process environment still win. This also applies to
  `--reset` without `--vault`: the fresh config keeps the vault the old `.env`
  named, because reset deletes `$BRAIN_HOME` but never the vault.
- **`brain eval --record-baseline` could save a poisoned baseline and exit 0.**
  When a query's embed call failed (an Ollama timeout while another process
  held a large model), the runner scored that query `0.0` and carried on, and
  recording wrote the `0.0` as a real score. Every later `--fail-below` run
  would then have compared against it. Such a row is now marked `degraded`, and
  recording refuses: it writes nothing, leaves any existing baseline untouched,
  names how many queries were degraded (not which), and exits `1`. Under
  `--diff`, degraded queries the baseline also has are left out of the deltas
  and listed as `degraded_queries`. `--fail-below` exits `1` instead of judging
  a diff with any `degraded_queries`, because a fully degraded run would
  otherwise compare nothing and pass. Every run with a degraded query warns on
  stderr with the count, and ad-hoc runs still exit `0`. Exit codes
  `1`/`2`/`3` keep their meanings. See `tests/eval/baselines/README.md`.
- **`brain eval` under `BRAIN_EMBEDDER=voyage` lost the whole run to one rate
  limit.** The degraded-row tolerance above caught only Ollama's embed error, so
  a Voyage timeout, rate limit or 5xx escaped as a traceback. Each backend now
  marks its embed failures *transient* (a timeout, a dropped connection, HTTP
  408/429/5xx) or *permanent*, and a transient failure on any backend degrades
  its row and gets the same recording and `--fail-below` refusals.
- **`brain eval` treated a missing Ollama model like a timeout.** An HTTP 404
  (model not pulled) degraded every row, exited `0` and advised a retry. A
  permanent embed failure — a model not pulled, a bad Voyage API key, an
  unusable response — now stops the run with exit `1` and a message naming the
  error, before any report or baseline is written.
- **`brain eval --answer` printed a traceback when the embedder failed.** It
  now exits `1` with a one-line message, as it already did when the chat model
  was unavailable.
- **A tokened `brain ui` opened a page that could not load anything.** The
  opened URL lacked the `#t=` fragment, and the panel printed it only for
  non-loopback binds. Both now carry the token, percent-encoded — a `%` in a
  token used to stop the page from booting.
- **`brain ui --host ::` refused every request**, its own machine's included,
  and died at startup with "no free port found": the port probe was IPv4-only,
  and the IPv6 wildcard's Host allowlist held a value no browser sends. `::`
  now gets the same loopback-only Host allowlist as `0.0.0.0` and serves
  `http://[::1]:<port>/`. It stays IPv6-only, so IPv4 clients — including
  `127.0.0.1` — cannot connect to it. `--host [::]` and `--host [::1]` now bind
  instead of failing in `getaddrinfo`.
- **A wildcard bind printed and opened `http://0.0.0.0:<port>/`** (refused with
  400) and expected a write Origin no browser sends. It now prints and opens
  the loopback URL, and accepts writes from it.
- **`brain ui --host <an IPv6 address>` works.** Before, the port probe was
  IPv4-only and refused the bind; an IPv6 address now gets a Host allowlist in
  the bracketed, canonical form the browser sends (`[fd00::5]`), so its own URL
  is served and every other Host is refused. Non-canonical spellings such as
  `0:0:0:0:0:0:0:1` are normalised (that one is loopback).
- **A non-ASCII `--token` made every API call fail with 500**
  (`secrets.compare_digest` raises on a non-ASCII string). `brain ui` now
  refuses a token that is not printable ASCII, or that has leading or trailing
  whitespace, with a usage error; the server also compares the token as bytes.
- **A host without IPv6 support** no longer prints a traceback for
  `--host ::`; it exits with a message naming the missing address family.
- **Every bind failure was reported as a busy port.** `brain ui` said "port
  … already in use" or "no free port found" whatever the cause. Now only a
  port in use is busy: an address this machine does not own, a name that does
  not resolve, and a privileged port below 1024 each get their own message.
- **The free port could be taken before the server bound it.** `brain ui`
  probed the port, released it, and let the server bind it again; another
  process could take it in between. The check is now the bind: the sockets it
  opens are the ones served, and a port in use is reported by `brain ui`
  itself rather than by the web server's startup error.
- **A wildcard bind could hand the token to another program.** On macOS,
  `brain ui --host 0.0.0.0` (or `::`) bound its port even when another
  program already listened on that port's loopback address, and the
  loopback URL it printed and opened — token included — reached that
  program instead — and a program could take that loopback port after
  `brain ui` had bound it, too. A wildcard bind no longer shares its port in
  either order: it will not bind over another program's loopback listener
  (`brain ui` moves to the next free port, or with `--no-auto-port` says the
  port is in use), and it also holds its own loopback address, so nothing can
  bind there afterwards — except in the instant between those two binds, a
  window of microseconds that remains open. One side effect, on every
  platform: restarting a wildcard bind while the last run's connections are
  still closing may land on the next port.
- **`brain ui --port` outside 1–65535 printed a traceback**, as did an
  auto-port search walking past 65535; `--port 0` bound a different random
  port on each address. Each is now a usage error, and the search stops at
  65535 with a message.
- **`brain ui --host LOCALHOST` refused its own URL with 400** — as did any
  mixed-case hostname. A browser lower-cases the host it sends and the Host
  check is case-sensitive, so the allowlist and URL kept a spelling no browser
  uses. Hostnames are now lower-cased at the CLI.
- **A golden-corpus `since_days` could run a window other than the one
  written.** `brain eval` converted it with `int()`, so `true` ran as 1 day and
  `40.7` and `'40'` as 40; `0` passed through as no window at all, and a
  negative value as a window starting in the future. `since_days` must now be
  a positive integer. **A corpus carrying `true`, `40.7`, `'40'`, `0` or a
  negative value now exits 1** with the query number, instead of loading.
  `forty` and `[40]`, which used to escape as a traceback, exit 1 the same way.
- **An unquoted impossible date anywhere in a golden corpus crashed
  `brain eval` with a traceback.** PyYAML builds an unquoted `2026-02-30` as a
  date inside `yaml.safe_load`, in any field (`notes:` included), and raises a
  bare `ValueError` that `load_corpus` did not catch. It now exits 1 with a
  corpus parse error naming the file. A quoted `"2026-02-30"` stays a string
  and, in `after:` / `before:`, is refused as not a calendar date.
- **Related documents missed candidates by meaning.** The vector half of the
  related-docs ranking (the `brain ui` related panel and `related.json`) asked
  the index for 50 nearest chunks and got at most 40. Draft, vault-less and
  confidential chunks were dropped only after that, so they used up slots. On a
  21k-chunk corpus that left 25–39 candidates where 50 existed. It now runs the
  same scoped HNSW scan as `brain search`'s vector leg and filters before
  counting, and it found all 50 for every document sampled. Related lists can
  change, mostly in their lower half.
- **`brain connect` embedding scores were documented as exact and were not.**
  The nearest-neighbour scan ran at pgvector's default `ef_search` of 40 and
  agreed with an exhaustive scan for 106 of 200 sampled documents. It now runs
  at 1000 (199 of 200), and a scan that comes back short falls back to the
  exhaustive query rather than being taken as complete. Suggestions from the
  next `brain connect refresh` can differ, mostly near the tail.

### Dependencies

- **`starlette>=1.7`** (was `>=0.47`). Earlier releases parse the IPv6 Host
  `[::1]:8765` as `[` and refuse it, which breaks `brain ui --host ::`.
  Measured: 1.6.0 fails the two `::` security tests; 1.7.0 and 1.8.0 pass.

## [0.3.0] - 2026-08-07

### Fixed — configuration resolution and the background daemons

- **`brain` now works from any directory.** Configuration is resolved through a
  dotenv chain whose last link is `$BRAIN_HOME/.env`, but nothing ever created
  that file, so a `brain` invoked outside the source checkout found no
  configuration at all. `brain setup` and `brain init` now provision it: a real
  file on a fresh install, or — in a development checkout — a symlink to the
  repository's `.env`, so credentials are not duplicated onto disk twice and
  cannot drift. Both paths are idempotent and never clobber an existing file.
- **The error message named the wrong cause.** A missing configuration file
  reported `DATABASE_URL is not set (see .env.example)`, which sends people to
  debug a database that is working fine. It now prints every path that was
  searched with its resolution state, distinguishes "no configuration found"
  from "configuration loaded but this key is absent", and reports a dangling
  symlink as such rather than as a missing file.
- **The launchd daemons failed silently for twelve days.** launchd starts agents
  with a minimal environment and no useful working directory, so all three hit
  the same dead configuration chain. The generated property lists now export
  `BRAIN_HOME` and set an explicit `WorkingDirectory`, and the daemon entry
  points resolve configuration explicitly rather than inheriting it. The
  interpreter the plists pin is now overridable via `BRAIN_PY`, matching the
  contract `brain.bin._launcher` already documented.
- **The daemons opt out of the working-directory `.env` walk-up**
  (`BRAIN_IGNORE_CWD_DOTENV`). That leg climbs to the filesystem root, so a
  stray `.env` in any ancestor directory could silently repoint a daemon at a
  different database. The walk-up remains the default for interactive use.
- **A failed configuration load no longer publishes a stale wiki.** The build
  caught the failure, logged a warning, and carried on — republishing from the
  vault mirror with no database refresh, so the site looked healthy while
  serving twelve-day-old content. It now aborts before the blue/green swap, so
  the last good build stays live, and exits with a distinct status (`3`) meaning
  "misconfigured, a human must act" as opposed to `1`, "the build broke, a retry
  may help". The watcher logs such faults once rather than per file event.
- **Log files are now size-capped.** A crash-looping watcher produced a 496 MB
  error log with no rotation of any kind; on a smaller disk that fills the
  volume. Rotation covers the standard-error path that launchd writes to
  directly, not only Python's logging handlers.

### Fixed — `brain doctor` can now see the failure it missed

- Three new checks, all of which stayed silent through the outage above: whether
  `$BRAIN_HOME/.env` resolves *and* loads (distinguishing missing, unreadable,
  dangling-symlink and present-but-incomplete); whether each installed LaunchAgent
  is loaded and last exited zero; and whether the newest *completed* wiki build is
  recent. A signal-killed daemon that also left the wiki stale is reported as a
  failure rather than a routine restart, and these conditions move the exit code
  instead of printing a red line and exiting `0`. A fourth check warns when the
  daemons and the CLI import different installations of the package.
- **`chunks stats` reported a row count three orders of magnitude wrong.** It
  read `pg_stat_user_tables`, whose counters PostgreSQL discards on crash
  recovery, and so announced "never analyzed" for a table whose planner
  statistics were present and current. It now reads the crash-durable catalogs.
- **The vault-drift remedy could destroy data.** A mirror file whose front-matter
  id did not match any document was classed as an orphan without checking whether
  the *path* belonged to a live row — so `brain vault prune-orphans --apply`
  would have deleted the only mirror of a real document. Clobbered mirrors are now
  a distinct category with `brain vault export --force` as the remedy.

### Fixed — safety of the tooling itself

- **The secret-scanning pre-commit hook could be walked past.** The false-positive
  allowlist contained the standard PEM header line that opens every RSA private
  key. Because the hook extracts matches rather than merely detecting them, a
  genuine key reduced to exactly that header, which was then subtracted as an
  allowlisted false positive — so a real private key would have committed
  silently. `EC` and `OPENSSH` headers were unaffected, since only the RSA form
  was listed. (This changelog entry deliberately does not reproduce the header:
  the repaired gate correctly rejects it, and the first draft of this very entry
  was blocked for containing it.)
- **Four privacy tests never ran.** Their guard compared a Typer `CommandInfo.name`
  that is `None` for commands registered without an explicit name, so the
  predicate was permanently false — including the only executed check that
  `brain usage --json` withholds raw search-query strings. The contract itself
  held; its verification was absent.
- Schema migrations now serialise on an advisory lock taken inside
  `run_migrations`, so concurrent runners — `brain init`, `brain demo`, a restore,
  or a test session — can no longer interleave a read-then-apply sequence against
  the same database.
- The two competing Quartz build timeouts (300 s from one entry point, 600 s from
  another) are unified into one overridable constant.

### Added

- **Continuous-integration quality gate** (`.github/workflows/ci.yml`) — every
  pull request and every push to `main`/`master` now runs `ruff check`,
  `mypy src/`, and the full `pytest` suite (with its 85% coverage floor) against
  the pinned Apache AGE Postgres test instance. Two jobs: a Docker-free
  lint/types job that fails fast, and the test job. The README's **CI** badge now
  reports this gate; previously it reported the marker-gated eval harness, which
  runs neither the linter, the type checker, nor the coverage floor.
- `SECURITY.md` — supported versions, private vulnerability reporting through
  GitHub's Security tab (no email address published), response expectations, and
  the security model of a local-first tool: the loopback-bound local database,
  the **unauthenticated** optional wiki port, the stdio MCP server, and what does
  and does not leave the machine per embedder backend.
- `.github/dependabot.yml` — weekly grouped updates for the Python dependencies
  in `pyproject.toml` and for GitHub Actions, monthly for the Apache AGE base
  image. Nothing auto-merges; every Dependabot PR runs the new CI gate.
- Daily repository-traffic snapshot workflow and its committed history
  (`.github/workflows/traffic-stats.yml`), plus live PyPI version, PyPI download,
  and GitHub star badges in the README.

### Changed

- **`mcp` now supports both major versions: `mcp>=1.2,<3.0`** (was `>=1.0,<2.0`).
  mcp 2.0 renamed the two names this project imports, so a compatibility layer
  resolves them at import time; 1.2.0, 1.29.0 and 2.0.0 are all exercised. Note
  the floor moved up: `mcp.server.fastmcp` did not exist before 1.2.0, so the old
  `>=1.0` claimed support for versions where `brain-mcp` could not import.
- The bundled Apache AGE image now builds on `pgvector/pgvector:0.8.6-pg16`.
  Existing local deployments keep running their current image until you rebuild
  deliberately — see `docs/graphrag.md`.

- **The `typer` dependency is now bounded on both sides: `typer>=0.16,<0.28`**
  (previously unbounded above). This is a user-visible install constraint: in a
  shared environment, an upper bound can conflict with another package that
  requires a newer Typer, and `pip install --upgrade typer` past the cap will no
  longer resolve alongside this release. The bound exists because Typer renders
  the entire CLI surface — Typer 0.26 vendored Click wholesale, which changed
  the exit code and traceback behaviour of a hand-raised usage error, and Typer
  below 0.16 pairs with a current Click in a combination that fails the CLI
  test suite. Both ends are exercised in CI; the cap is a requirement that the
  next minor be tested before users get it, not a claim that it is broken.
  Users who need a Typer outside this range should stay on 0.2.1 until the cap
  moves.

### Fixed

- `brain vault render` now invokes `npx --no -- quartz build`. Without `--no`,
  a workspace where npm cannot resolve `quartz` locally made npx fall back to
  installing a package of that name from the registry and building the vault
  with it — and such a workspace still passed the setup check, so the
  substitution of an unpinned package for the overlaid local Quartz was
  silent. It now exits non-zero instead. The `--` separator keeps npx from
  swallowing Quartz's own flags.
- `brain vault render` no longer derives its output directory from the current
  working directory. The default was `./dist`, so running the command from an
  unrelated project wrote a complete Quartz site into that project's source tree
  (and a scheduled run, which has no meaningful cwd, would do it every time).
  The default is now `<quartz-dir>/dist` — i.e. `<vault>/.quartz/dist` — derived
  from the configured vault, so the site lands in the same place from any
  directory. An explicit `--to` still overrides it and still keeps ordinary
  cwd-relative semantics. Because the Quartz build deletes its output directory
  before emitting, render now also refuses an output path that contains the
  vault, the Quartz workspace, or the current directory; in particular `--to .`
  (previously accepted, and equivalent to wiping the working directory) is
  rejected.
- The repository-traffic snapshot is now genuinely best-effort: it rebases before
  pushing and picks the latest entry by date, so a transient API failure or a
  concurrent push no longer fails the workflow.

## [0.2.1] - 2026-07-20

### Fixed

- **`brain init` on a `pip`/`pipx` install** — the SQL migrations were not
  packaged into the wheel, so a non-editable install had no migrations to apply
  and `brain init` could not build the schema. The migrations now live inside the
  `brain` package (`src/brain/migrations/`) and ship with the wheel. This is the
  fix that makes a clean `pipx install secondbrain-py` usable at all.
- `brain init` now fails loudly when no migrations are packaged, instead of
  silently reporting success against an empty schema.

### Changed

- **Distribution renamed to `secondbrain-py`** (was `second-brain`). PyPI's
  anti-typosquat rule blocks `second-brain` — its separator-collapsed form
  collides with the unrelated existing `secondbrain` project — so the published
  package name is now `secondbrain-py`. The import package stays `brain` and the
  human CLI stays `brain`; only the distribution name and the uvx convenience
  alias changed.
- The uvx alias is now `uvx secondbrain-py` (was `uvx second-brain`), matching the
  renamed distribution and the MCP-registry `server.json` package identifier.
- PyPI Trusted Publishing is now enabled in `release.yml`, so `v*` tags publish
  the sdist+wheel to PyPI (`pipx install secondbrain-py`). A `workflow_dispatch`
  trigger allows re-running a publish manually.

No functional changes to the CLI, search, ingest, or graph behavior.

## [0.2.0] - 2026-07-19

### Added

- `brain demo` taste test — a zero-config, throwaway sample corpus so a new user
  can try hybrid search in seconds before wiring up their own data.
- `BRAIN_EMBEDDER=none` FTS-only mode — run without any embedding backend
  (no Ollama / no API key), full-text search only, for the lightest install.
- `brain setup --profile minimal|standard|full` — tiered setup that provisions
  only what each profile needs.
- Prebuilt multi-arch (linux/amd64 + linux/arm64) Apache AGE Postgres image on
  GHCR, so fresh installs pull instead of compiling AGE from C source (slow and
  fragile on Apple Silicon). The compose files keep a local `build:` fallback.
- README restructure and docs split for a clearer first-run path.

### Fixed

- Install one-liner: broken placeholder/tag reference (the advertised
  `v0.2.0` install path now resolves).
- launchd default-install behavior.
- Caddy preflight abort.

## 0.1.0

Initial public release. Unbracketed on purpose: no `v0.1.0` tag was ever cut, so
there is no release page to link to, and a bracketed heading with no definition
renders as the literal text `[0.1.0]`.

### Added

- Hybrid search over Postgres + pgvector (Reciprocal Rank Fusion of full-text
  rank and vector cosine similarity, with recency weighting and metadata
  filters).
- GraphRAG entity-graph layer (people, orgs, and concepts in Apache AGE) for
  themes, patterns, and connections across interactions.
- Obsidian-style vault of wiki-linked notes, publishable as a browsable Quartz
  wiki.
- `brain-mcp` MCP server for querying the brain from any MCP-compatible client.

[Unreleased]: https://github.com/mshtawythug/second-brain/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.3.0
[0.2.1]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.0
