# RM-33 — Image extractor and the drop-folder runner (plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-33" (the spec), with
§2.1 (DoD), §2.2 (budgets: `doctor_runtime.py`, `bin/launchd.py`), §2.3 (settings leaves), §2.4
(new-source protocol) and §4.2 (shared files). Branch `feat/rm-33-images-drop-folder` off
`second-brain/master` `2525b03`.

**Size and split.** M, as the ticket says. One PR: the extractor alone ships nothing a user can
reach (no folder feeds it), and the runner without the extractor has nothing to ingest. If the
coordinator prefers two PRs, the cut is (A) image extractor + `screenshot` kind + front-end tables
+ extract policy, then (B) runner + settings + `brain ingest-folder` + launchd + doctor.

## Files

New (all under 800; the estimates are lines):

| File | Purpose |
|---|---|
| `src/brain/ingest/image.py` (~170) | `extract_image(path, *, ocr=None, open_image=…, deadline_s=…)`: Pillow decode with an explicit pixel and side cap, grayscale PNG re-encode, OCR through RM-32's `OcrEngine`. `content_type="screenshot"`. Errors `ImageError(ValueError)` → `ImageTooLargeError`, `UnreadableImageError`, `OcrUnavailableError`, all content-free. |
| `src/brain/drop_folder.py` (~300) | The spec's `DropFolder`, `scan`, `run`, `DropReport`, `IngestFn`; move-first; the duplicate no-op; move-back. Filesystem only: no DB, no extractor import. |
| `src/brain/settings/drop_folders.py` (~170) | Reads `$BRAIN_HOME/drop-folders.toml` (`[[folder]]` tables) with `tomllib`; validates every key; `ConfigError` messages name `folder[i].key`, never a value. |
| `src/brain/cli_ingest_folder.py` (~220) | `brain ingest-folder --dir PATH \| --configured --since Nd [--sensitivity L] [--no-enrich] [--allow-secrets] [--dry-run]`, registered through `cli_registry.EXTENSION_REGISTRARS`. Builds the real `IngestFn` (extract → retitle → `ingest_document`). |
| `src/brain/doctor_drop_folders.py` (~80) | The `drop folders` doctor row: not configured / N folders / config error / images configured but OCR unavailable. |
| `src/brain/templates/launchd/com.brain.dropfolders.plist.j2` | `StartInterval` 600, no `RunAtLoad`, no `KeepAlive`; env block as the brief's. |
| `src/brain/templates/bin/_brain-dropfolders-fg.sh` | Log rotation, then `exec "$PY" -m brain ingest-folder --configured --since 2d`. |
| `docs/guides/drop-folders.md` | Configure a folder, run once, `--with-dropfolders`, uninstall. |
| `skills/ingest-brain/sources/drop-folders.md` | The recipe; one routing row in `SKILL.md` (stays ≤ 300 lines). |

Touched:

| File | Change |
|---|---|
| `src/brain/ingest/dispatch.py` | One `ExtractorSpec(name="image", extensions=(".png", ".jpg", ".jpeg"), in_directory_walks=False)` + its lazy loader. |
| `src/brain/source_kinds.py` | `screenshot` in `VALID_SOURCE_KINDS`, one docstring line. |
| `src/brain/graph_rag/extract_policy.py` | `screenshot` in `CONCEPT_SKIP_CONTENT_TYPES`; `CONCEPT_SKIP_POLICY_VERSION` `v1` → `v2` (the module's own rule). |
| `src/brain/cli_registry.py` | One `("ingest-folder", register_ingest_folder)` entry. |
| `src/brain/bin/launchd.py` (491, +~30) | `_DROPFOLDERS_LABEL` joins `_OPTIONAL_LABELS`; `install_plists(..., with_dropfolders=False)` with the same sticky rule as the reranker (plist exists → re-render + re-bootstrap; never removed); `_brain-dropfolders-fg` in the wrapper tuple; `--with-dropfolders` in `install_main`. No inputs to validate, so no warn-and-skip branch is needed. |
| `src/brain/doctor_runtime.py` (583, +3 of the +60 budget) | Composes `drop_folders_doctor_check()`; one docstring entry. `daemons`/`daemon code` need no change: `DAEMON_LABELS` already includes `_OPTIONAL_LABELS` and checks each only when its plist exists. |
| `ui/static/js/results.js`, `discovery.js` | `screenshot: "📸"` in both `SOURCE_ICONS`. |
| `ui/static/js/tree.js` | `"_ingested/screenshot"` in `MONTH_GROUPED_PATHS`. |
| tests' kind tables | `ADDED_KINDS` (`test_source_kinds_identity.py`), `DEMO_EXEMPT_KINDS` (`test_demo_corpus.py`). |
| docs | `CHANGELOG.md` [Unreleased]; `docs/cli-reference.md`; `docs/configuration.md` (the TOML file; no env knob is added); spec Appendix B RM-33 → DONE (#pr); backlog `2026-10-23-backlog-reconciliation.md` rows 5.1 and 3.5's "Images outside PDFs" pointer. |

No file in the CLAUDE.md ceiling table is touched. No migration (the kind is a TEXT value).

## Design decisions the ticket leaves open (my rulings; flag any you disagree with)

1. **`scan` is top-level only.** Sub-directories are never entered, `imported/` included. Also
   skipped: names starting with `.` (covers `.<name>.icloud` and macOS's in-progress
   `.Screenshot …` temp file), non-regular files per `lstat` (symlinks never followed), the
   dataless bit (`getattr(st, "st_flags", 0) & SF_DATALESS`, so Linux CI works), extensions not
   in the folder's set, `mtime < since`, and **`mtime` within the last 5 s** (a file still being
   written is picked up next run).
2. **The move never overwrites.** `os.rename` replaces an existing destination silently, so the
   move is `os.link(src, dst)` (atomic, `FileExistsError` when `dst` exists) then
   `os.unlink(src)`; on a filesystem without hard links (`EPERM`, `ENOTSUP`, `EXDEV`, `EMLINK`) it
   falls back to an `O_EXCL` copy + `copystat` + unlink. The move back uses the same primitive.
3. **The duplicate no-op deletes the drop copy only when the bytes are identical.** `sha8` is 32
   bits; a name match is checked against the full SHA-256 of the existing `imported/` file, which
   must be a regular file (`lstat`). A mismatch, or a symlink there, is a reported failure that
   leaves both files alone.
4. **Move-back cannot overwrite either.** If a new file took the original name while the ingest
   ran, the moved file stays in `imported/` and the failure says so.
5. **`imported/` must be a real directory.** Created if missing; a symlink (or a non-directory) at
   that name refuses the whole folder, so nothing is ever moved outside it.
6. **Time and size bounds.** Per file: the OCR timeout is `min(PAGE_TIMEOUT_S, remaining)` and
   the file is refused above 50 MiB before Pillow opens it. Per run: a 480 s deadline (under the
   600 s interval) and at most 200 files per folder per run; files past either bound are left in
   place for the next run and counted as `deferred`. launchd never overlaps one label's runs, and
   two concurrent manual runs are safe by (2): the loser's `link` sees `ENOENT` or `EEXIST`.
7. **Image hardening.** `Image.open(path, formats=("PNG", "JPEG"))` (no other decoder is reachable
   from a `.png`); Pillow's own decompression-bomb check runs inside `open` and its
   `DecompressionBombError`/`Warning` map to `ImageTooLargeError`; then the explicit cap from the
   header, before `load()`: at most 70 Mpx, each side 1–32,767 px (pdf.py's values). Decode →
   grayscale → PNG re-encode, so tesseract never parses the untrusted file and EXIF is dropped.
   The global `Image.MAX_IMAGE_PIXELS` is not mutated. Every Pillow failure (`OSError`,
   `SyntaxError`, `ValueError`, `EOFError`, `struct.error`, `MemoryError`, `Image.UnidentifiedImageError`)
   becomes `UnreadableImageError` with a fixed message.
8. **OCR unavailable is a failure, not an empty document.** With `BRAIN_OCR=off` or no
   `tesseract`, `extract_image` raises `OcrUnavailableError`, so the runner moves the file back
   (otherwise every screenshot would be consumed into `imported/` as an empty document). The
   runner checks first and skips an image folder with the tesseract hint, so the launchd job does
   not churn. OCR that ran and read nothing is the spec's empty document: it **stays in
   `imported/`**, reported `empty`, with no document row (tested in `test_drop_folder.py` and
   end to end in `test_cli_ingest_folder.py`). It is never moved back, because retrying it would
   read nothing again. Approved by team-lead 2026-10-30.
9. **`--dir` uses the folder's configuration when it is configured** (including `auto = false`,
   which is what "only an explicit run uses it" needs), else the defaults: kind `screenshot`,
   `.png/.jpg/.jpeg`, `confidential`. `--sensitivity` overrides for one run. `--dir` and
   `--configured` are mutually exclusive; `--since` is `Nd` with 1 ≤ N ≤ 365, mandatory. The
   refusal exits 2: `ingest-folder requires --dir PATH or --configured, and --since Nd`.
10. **`mode = "capture"` is refused by the settings loader for now** ("not available until
    RM-39"). The dataclass keeps the spec's `Literal["ingest", "capture"]` so RM-39 only adds the
    branch.
11. **Settings validation.** `path` required, `~` expanded, absolute, an existing directory,
    stored resolved; `kind` in `VALID_SOURCE_KINDS` (default `screenshot`); `extensions` lowercase
    dotted suffixes each served by `dispatch` (default the image three); `sensitivity` a valid
    level (default `confidential`); `auto` a bool (default true); unknown keys and two entries
    resolving to the same path are errors. A missing file means "no folders".
12. **Title.** The runner passes the original stem; the `IngestFn` replaces the extractor's title
    (`dataclasses.replace`). `source_kind` is the folder's kind and `source_external_id` defaults
    to the `imported/` path (dedup rule 2).
13. **Exit codes.** 0 when nothing failed (deferred files included); 1 when any file failed or a
    configured folder could not be used; 2 for the scope refusal. Errors print by class only
    (`describe_failure`). File names are printed (the owner's own terminal and launchd log).
14. **`brain ingest x.png`** works through dispatch (kind `manual`, the CLI's own sensitivity
    default). `cli_ingest.py` is over the ceiling and is not touched.

**Known residual (recorded, not fixed):** a crash between the move and the ingest leaves a file
in `imported/` with no document, and a later identical drop is the spec's no-op, so it is never
ingested; `brain ingest <imported path>` recovers it. An image whose OCR times out is moved back
and retried every run inside `--since`.

## Tasks (TDD, RED first, each guard mutation-proven in a `git archive` copy)

1. **Kind + policy + front end.** Tests: kind identity/demo tables; extract-policy skip for
   `screenshot` and the version bump; JS tables (the existing table tests, plus one asserting the
   three entries).
2. **`ingest/image.py` + dispatch.** Synthetic PNG/JPEG rendered with Pillow; a fake `OcrEngine`;
   adversarial: header-only PNG declaring 100,000² px (refused before decode, asserted by a
   `load` that must not run), a JPEG with patched SOF dimensions, a 0-width/over-side image, a
   `.png` that is a GIF, truncated PNG, file over the byte cap, OCR off / binary missing, OCR
   timeout capped by the deadline, empty OCR text, no recognised text leaks into any error. One
   real-tesseract test, skipped when the binary is absent. Dispatch: registered, not in
   directory walks, `UPLOAD_EXTENSIONS` still a subset.
3. **`settings/drop_folders.py`.** Every validation branch; messages name keys only.
4. **`drop_folder.py`.** `tmp_path` trees: symlink (file and `imported/`), `imported/` contents,
   `.icloud` stub, dot-temp file, dataless via injected `lstat`, mtimes around `since` and the
   settle window, two same-name different files in two runs (two `imported/` names), same file
   twice (drop copy removed, destination untouched, ingest not called), `sha8` collision (both
   left alone), failed ingest → moved back, failed ingest with the original name re-taken → left
   in `imported/`, failed ingest on a pre-existing destination never touches it, link fallback
   path, deadline and file cap → deferred.
5. **`cli_ingest_folder.py`.** Scope refusals (none, both, bad `--since`); `--configured` skips
   `auto = false`; `--dir` picks up a configured folder; dry run; the end-to-end test through
   `ingest_document` on the 5434 DB: a synthetic PNG with rendered text (fake OCR engine) becomes
   a `screenshot` document titled by its original stem, confidential, `source_path` in
   `imported/` (AC1); AC3/AC4 against the DB.
6. **launchd + doctor.** Plist renders (StartInterval 600, the shim path); first flagless install
   does not install it; `--with-dropfolders` does; a later flagless install re-renders and
   re-bootstraps it; uninstall removes it; doctor does not report it when never installed; the
   doctor row's four states. Recording `launchctl` stub as in `test_bin_launchd_reranker.py`.
7. **Docs, skill, memory, CHANGELOG, Appendix B, backlog.** Then `ruff check && mypy src/`, the
   named tests plus `test_log_privacy_rules.py` under the lock, the CLI end-to-end on the 5434 DB
   (transcript in the PR), PR, report.

## Risks

- **Moving the owner's files.** Bounded by (1)–(5): only regular, non-symlink, top-level files of
  the configured extensions inside a configured folder, only into its own real `imported/`, never
  overwriting, and deletion only of a byte-identical drop copy. Tests run only in `tmp_path`.
- **A screenshot of a credential the secret guard misses.** The `confidential` default.
- **Hostile image files.** (7); a process-level memory guard is BL-32's, shared with PDFs.
- **The policy bump re-reconciles every `calendar_event`** once with an empty extraction (cheap:
  no model call).

## Review round 1 (#105, sec-105 at `f51488d`), rulings applied

- **M1, terminal-safe names.** Every file name and folder path the CLI prints (the
  report, `--dry-run`, the skip lines) goes through `brain.ingest.text_safety.one_line`,
  and so does the stored title: the original stem on one line, capped at 300 characters
  (`drop_folder.MAX_TITLE_CHARS`), falling back to the `imported/` stem when nothing is
  left. Tested with an ESC/OSC/newline name and a hostile folder path.
- **M2, screenshots confidential on every path (supersedes ruling 14).** Spec point 6
  wins: `ExtractorSpec.default_sensitivity` (the image spec: `confidential`) and
  `dispatch.default_sensitivity(path)`. `brain ingest` and `brain ingest-dir` take
  `--sensitivity` as optional and fall back to it, so `brain ingest x.png` is
  confidential and an explicit `--sensitivity normal` is honoured. `cli_ingest.py` is
  in the ceiling table: the change is net-zero (858 → 858) and its row gains a hop.
- **L1, identity, not names.** The folder and `imported/` are held open as
  `O_DIRECTORY | O_NOFOLLOW` fds and every step names files relative to them. A file is
  hashed from one fd opened `O_NOFOLLOW | O_NONBLOCK` and checked regular with `fstat`.
  After the link or copy, the new name must be a regular file with that fd's
  `(st_dev, st_ino)` (the copy's own after a copy, whose bytes must hash to the digest),
  else it is removed. The old name is unlinked only while it is still that file, and
  before the ingest the path it reads must still resolve to the moved file.
  `extract_image` opens the file `O_NOFOLLOW | O_NONBLOCK` and hands Pillow the open
  file. Tested through the injected `link` and `lstat` seams: a symlink swap, a FIFO
  swap, a source replaced before its unlink, `imported/` swapped for a symlink, and
  bytes changed before the copy fallback.
- **L2, deterministic failures stay in `imported/`, not in a `failed/` sibling.** The
  ingest step raises `drop_folder.RejectedFile` for a failure every run would repeat:
  an `ImageError` other than `OcrUnavailableError` (unreadable, too large, an OCR
  timeout or error), another extractor's `ValueError`, or the secret guard. The file
  stays in `imported/`, reported `rejected` with its cause's class, and the run exits 1.
  It is never retried; `brain ingest <imported path>` (with `--allow-secrets` for the
  guard) recovers it. Any other failure (the database, the embedder) and
  `OcrUnavailableError` still move the file back.
