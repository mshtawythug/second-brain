# RM-35 — Notion pages from Notion's Markdown export

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-35" (roadmap 2.8, the
Notion half; the Jira half shipped as RM-34). Protocol §2.4, ceiling §2.2 (RM-35 touches no
over-ceiling file and no near-ceiling budget), shared files §4.2.

## Goal

`brain ingest-notion <export-path>` reads a Notion "Export → Markdown & CSV" folder or `.zip`
and ingests each page as a `notion` document keyed by its 32-hex page id, so a re-export to a new
folder updates in place. No Notion API, no images, no attachments.

## Files

| File | What |
|---|---|
| `src/brain/ingest/notion_archive.py` (new) | Reads the export, folder or zip, as untrusted input: the path, link and size guards. Yields `ExportFile`s and reads one at a time. Writes nothing to disk. |
| `src/brain/ingest/notion_export.py` (new, the spec's named module) | Pure: names → page ids, the export plan (pages, database rows, skip counts), page text → `ExtractedDoc`, the last-edited property, the CSV row count. |
| `src/brain/cli_ingest_notion.py` (new) | The command: options, `--since`, `--max`, the ingest loop, the report. |
| `src/brain/cli_registry.py` | One import, one `EXTENSION_REGISTRARS` entry. |
| `src/brain/source_kinds.py` | `notion` added, one comment line. |
| `src/brain/ui/static/js/results.js`, `discovery.js` | `notion: "📓"`. No `MONTH_GROUPED_PATHS` entry (spec: no month grouping). |
| `skills/ingest-brain/SKILL.md` | One routing-table row (no recipe file: the CLI is the recipe). |
| tests | `tests/notion_fixtures.py` (synthetic export builder), `tests/test_notion_archive.py`, `tests/test_notion_export.py`, `tests/test_cli_ingest_notion.py`, `tests/test_notion_source.py`; `ADDED_KINDS`, `DEMO_EXEMPT_KINDS`, the registrar expected list. Five tests used `notion` as their example of an UNKNOWN kind (`test_ui_schemas`, `test_ui_source_filter_none`, `test_ui_browser_lede`, `test_wiki_link_parser`, `test_vault_sync_source_kind_validation`); they now use a kind that will never exist. |
| docs | CHANGELOG, `docs/cli-reference.md`, spec Appendix B, backlog roadmap-2.8 row. |

Why not an `ingest/dispatch.py` entry (protocol point 1, path (c)): dispatch maps one file
extension to a `(Path) -> ExtractedDoc` extractor with `source_path` dedup. An export is a tree or
an archive, its `.md` extension is the markdown extractor's, and its documents must carry
`source_path=None` plus an external id (spec point 3). The dispatch docstring already names this
case ("its own command"), so the export reader is its own module behind its own command.

## Protocol (as built)

1. **Path (c)**, a user-made local export.
2. **Kind** `notion`, `content_type` `notion_page`; glyph `📓` in both `SOURCE_ICONS`; no month
   grouping.
3. **Dedup** `(notion, <page id>)`, `source_path=None`. The id is the 32-hex suffix of the file
   stem (`Title <id>.md`), lowercased. A re-export to a new folder or zip hits the same sources row.
4. **Credentials** none.
5. **Scope** the export path (accepted deviation). `--since YYYY-MM-DD` keeps a page whose
   last-edited property is on or after that day; a page without the property, or with one that
   does not parse, is kept and counted ("undated").
6. **PII / sensitivity** page text only. Default `normal`, `--sensitivity` overrides (validated
   once, up front). The secret guard runs per page (`--allow-secrets`).
7. **Tests** synthetic trees and zips built in `tmp_path` from named constants.
8. **Dependencies** none (`zipfile`, `csv` are stdlib).
9. **Cost** summaries and concepts on (`--no-enrich` skips the summary). `--max` (default 200)
   caps the pages *written* (ingested or updated) in a run; unchanged pages are free, so re-runs
   drain a large workspace. 200 new pages is about 30 hours of graph lane in the worst case.

## Deviations from the spec (approved by the coordinator, 2026-10-30)

1. **Database rows come from the row pages, not the CSV cells (ruling R1).** The spec says
   database CSVs are ingested "one row per document". A CSV row carries no page id, so keying it
   would mean keying by title or row order: a guess (AC4) that breaks in-place updates (AC2). The
   export writes each row as its own `Row <id>.md` page, which carries a stable id, so rows come
   from those pages behind `--with-databases` (AC3 holds). The CSVs are counted and never stored,
   and a CSV that lists rows when the export has no row pages is reported.
2. **The reader is its own module and command, not an `ingest/dispatch.py` entry.** The
   spec's protocol point 1 names path (c), "an extractor registered with `ingest/dispatch.py`".
   `.md` already belongs to the markdown extractor, dispatch maps one file to one document deduped
   by `source_path`, and these pages must carry `source_path=None` plus a page id (spec point 3).
   See "Why not an `ingest/dispatch.py` entry" above.

## Rulings

- **R1 — database rows come from the row pages, not the CSV cells.** A CSV row carries no page id,
  so keying it would mean keying by title or row order: a guess (AC4) that also breaks AC2 (a
  rename or re-sort would duplicate or overwrite). The export writes every row as
  `Row <id>.md` with its properties, inside a folder named like the CSV (`DB <id>/` beside
  `DB <id>.csv` or `DB <id>_all.csv`). So a database is recognised by that pair, its row pages are
  "database rows", and `--with-databases` gates them (AC3). The CSV files themselves are counted
  and never stored. With the flag, a database whose CSV lists rows but whose folder holds no row
  page is reported, so a change in Notion's layout is loud, not silent.
- **R2 — the title** is the page's first line when it is a `# ` heading (Notion writes the full
  title there; the file name may be truncated), else the file name without the id (AC1), else
  `Untitled`.
- **R3 — the body** is the page text verbatim, NUL characters removed (Postgres `TEXT` rejects
  them). Image and link references stay as text; the files they point at are never read.
- **R4 — skipped and counted, never guessed (AC4):** a `.md` with no parseable id; every copy of
  an id that appears more than once in one export; every non-page file (images, PDFs, other
  attachments); nested archives (reported with "unzip it and pass the inner archive"); files over
  the per-file cap; folder entries that are symbolic links.
- **R5 — the untrusted archive.** A zip is opened with `zipfile` and each member is read in place,
  streamed with a bounded `read`. Nothing is extracted to disk. The whole archive is refused
  (exit 1, class-only message, no member name echoed) when: it holds more than
  `MAX_EXPORT_FILES` members; a member name is absolute or drive-absolute (`C:/`, `C:\`), or has
  an empty, `.` or `..` segment, with `\` read as a separator as well as `/` (so `a\b.md` is
  kept, and `..\x.md` is refused: a backslash alone is not refused); a member is a symbolic link
  (Unix mode in `external_attr`); two members share a name; a member is encrypted; the bytes read
  in one run pass `MAX_READ_BYTES`; or `zipfile` reports corruption, which includes a header
  that understates a member's size once the CRC of the shortened content mismatches (a read
  never returns more than the declared size). *(Corrected for aud-104 F3: this sentence said a
  backslash was refused and that an over-long read was refused; neither is what the code does.)* Only `.md` and
  `.csv` members are ever decompressed, so attachments cannot be a bomb. A folder is walked with
  `os.walk(followlinks=False)`; symlinked files and directories are skipped and counted, and each
  file is opened with `O_NOFOLLOW` so a swap between listing and reading cannot follow a link.
- **R6 — the cap** counts written pages. Once `--max` pages are written, the rest are compared with
  what is stored (content hash, by page id) and the remainder that would still be written is
  printed with "run again to continue".
- **R7 — output** names pages by id, never by title (a title is someone else's text). Failures
  are printed by class (`describe_failure`). The secret guard's notices are re-rendered with the
  page id as their label, a refused page prints `refused <id>: N finding(s)`, and the stored
  title has control and format characters removed and is cut at 300 characters (sec-104 M1).
- **R8 — the sec-104 hardening round.** A folder read records each file's `(st_dev, st_ino)` at
  listing, skips files with more than one hard link, opens with `O_NOFOLLOW | O_NONBLOCK`, and
  requires a regular file with the recorded identity (L1); an entry that vanishes while listed is
  skipped and counted, never echoed (L2); a zip's end record (and its ZIP64 form) is read from the
  file's tail and an archive declaring more than `MAX_EXPORT_FILES` entries, or a larger central
  directory than that many can need, is refused before `zipfile` parses it (L3).

## Tasks (TDD, each red first)

1. `notion_export`: id parsing, plan building, page building, last-edited parsing, CSV row count.
2. `notion_archive`: folder and zip readers and every guard; adversarial tests for traversal,
   absolute names, symlinks (zip mode and folder links), duplicate names, member count, total
   bytes, lying headers, encryption, corruption; mutation-prove each guard in a `git archive` copy.
3. `cli_ingest_notion` + registration: refusal of a missing or non-export path, ingest end to end
   on 5434, re-export to a new folder updates in place (same document id), `--with-databases`,
   `--since`, `--max` with the remainder, `--sensitivity`, zip input, the secret guard.
4. Kind, glyph and table wiring; skill row; docs; memory.

## Verification (as run)

- 100% line and branch coverage on the three new modules.
- 22 guard mutations, each in a `git archive` copy with `PYTHONPATH` pinned to it, are all
  killed, and each copy passes again when restored: the zip name, link, encryption, duplicate,
  count and size guards, the folder `followlinks`, regular-file, resolve and `O_NOFOLLOW` guards,
  the read caps, the corrupt-member mapping, and the plan's id, duplicate-id, resource-fork and
  database-row rules.
- After the sec-104 round (R8), 40 mutations: 37 killed (the round adds `O_NONBLOCK`, the
  identity check, the hard-link skip, the vanished-entry skip, the read-error mapping, the
  end-record entry and size caps and the call that runs them, the ZIP64 parse and signature
  checks, the missing end record, the title strip and cap, and the guard output by id) and 3
  **equivalent** mutants, recorded rather than claimed: removing `O_NOFOLLOW` or the read-time
  `S_ISREG` is still refused by the inode-identity check (a link, FIFO or folder swapped in is
  another inode), and removing the ZIP64 record's room check is still refused as corrupt
  because a negative seek raises `OSError`, which maps to the same sentence. Both read-time
  checks stay as defence in depth.
- After review round 2, 48 mutations: 45 killed (round 2 adds the own-document check, the
  empty/same-text split, the past-cap empty, taken-text and own-hash rules and skip lines, the
  LZMA mapping and the `utf-8-sig` decode) and the same 3 equivalents.
- After the sec-104 L3 rework, 54 mutations: 52 killed, including the walk's cap, signature and
  short-header checks, the locator followed whatever the plain record says, the locator-first
  read, the prepended-data fallback, the locator and record consistency checks, the
  directory-before-the-file check, the size cap, the guard call and the reason mapping. 2 are
  equivalent on APFS, where the harness runs: `O_NOFOLLOW` (a link swapped in is another inode)
  and the read-time `S_ISREG`, which on ext4 is what refuses a FIFO or folder that reused the
  inode number. The ZIP64 "room" mutant is gone with the code it mutated.
- After the residual fix, the harness still holds 54 mutations: the locator-first read, the
  prepended fallback and the old record checks were replaced by the parsed-count backstop, the
  record required before the locator, the fixed/unfixed divergence refusal and the new record
  consistency check. Those, with the six other directory rules, were re-run as one batch of 10
  (the disk rule caps batches): all 10 killed.
- `tests/test_skills_token_routing.py::test_skills_sync_is_not_drifted` fails locally until
  `bin/brain-skills-sync` runs after merge, because SKILL.md gained the Notion row (CI skips it).

## Review round 2 (rev-104, aud-104)

- **Pages that are not stored.** A new page whose text equals another stdin document's falls
  through `ingest_document`'s content-hash dedup and comes back with THAT document's id; an
  empty page has no chunks and comes back with none. Both used to be reported `unchanged` and
  counted in `--max`'s remainder forever. A page is now `unchanged` only when the returned
  document is the one keyed to `(notion, <page id>)`; otherwise it prints
  `skipped <id>: same text as an existing document` or `skipped <id>: empty`, the summary
  counts each, and the remainder leaves both out. Pages past the `--max` cap are classified the
  same way without writing (unchanged, empty, same text, or remaining), so the summary counts
  every page. Nothing is forced (`--force` would replace
  the other document). A stored page re-exported with another page's text fails loudly
  (`failed <id>: UniqueViolation`, the stdin content-hash index) and keeps its old body;
  the calendar and GitHub commands share the first shape and are worse there (BL-34).
- A corrupt LZMA member maps to the corrupt sentence; files are decoded `utf-8-sig`;
  `NotionPage.has_last_edited` and `last_edited()`'s bool are gone; the naive last-edited time
  stored as UTC is documented (ruling). The shared run loop is BL-33 (ruling: not in this PR).

## Re-review (rev-104 L1, sec-104 L3 rework)

The first end-record check could be steered apart from `zipfile` (sec-104, proven with 300k-entry
archives at about 186 MB peak): (a) it read ZIP64 only on a `0xFFFF`/`0xFFFFFFFF` sentinel, while
`zipfile._EndRecData64` follows any locator; (b) it trusted the declared count, which `zipfile`
ignores (it parses the declared size); (c) it read the ZIP64 record just before the locator, while
`zipfile` reads it at the locator's offset first. And it refused a plain 65,535-entry archive
(rev-104 L1). The new leaf `ingest/zip_directory.py` replaces it:

- `locate` copies `_EndRecData`/`_EndRecData64`: the exact tail, else the last end-record
  signature in the final 64 KiB; any locator is followed, its record read at the locator's
  offset, then just before the locator (prepended data); disk, offset and size checks as
  `zipfile`'s; with no locator the plain values stand, maximal or not. The directory starts where
  `zipfile` puts it (the record's location minus its size); a size larger than what precedes the
  record is corrupt.
- `count_entries` walks that directory one 46-byte header at a time for its declared size,
  requiring `PK\x01\x02`, skipping name, extra and comment, refusing at `max_files + 1`.
- `open_export` runs both, plus the directory-size cap, before calling `zipfile`; tests prove it
  through an injected `open_zip` factory that fails if called. `ZipExport`'s own count check was
  removed: it could no longer fire.

**Residual: zipfile versions that disagree (sec-104, at `53df8d8`).** `requires-python >= 3.11`
admits patch releases without the CVE-2025-8291 zipfile fix, which read the ZIP64 record just
before the locator and ignore the locator's offset. sec built an archive whose small record at the
offset passed the mirror (1 entry) while an unfixed zipfile parsed all 300k entries. So
`_zip64` now requires the record just before the locator (what an unfixed zipfile reads) and
refuses an archive that also has one at a different locator offset (where a fixed one reads
first): that is the only shape on which they diverge. Prepended data (no record at the offset)
reads alike and is kept. This reverses the earlier "(c): extensible data accepted": ZIP64
extensible data is now refused, as no Notion export carries it. `ZipExport`'s parsed-count check
is back as a backstop for a zipfile that reads another directory, tested through a fake
`open_zip`. Tests: sec's shape (`notion_fixtures.old_zipfile_bypass`), a record both before the
locator and at its offset, extensible data, and prepended data still accepted.

**Inode reuse (found by CI on Linux).** ext4 reuses a freed inode number at once, so an
unlink-then-create at the same path can carry the listed `(st_dev, st_ino)`. A regular file
recreated that way is read, as the export's own content inside the root; a FIFO or folder is
refused by `S_ISREG`, which is therefore not redundant there. The test now renames another file
over the path (two inodes exist at once), which is the swap the identity check is for.

## Risks

- Notion changes its file naming: every page then lands in the "no id" count, which is printed.
- Notion stops writing row pages: R1's empty-folder warning fires.
