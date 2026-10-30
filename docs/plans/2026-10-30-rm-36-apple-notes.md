# RM-36 — Apple Notes (`brain ingest-apple-notes`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-36". Protocol: §2.4.
Budgets: §2.2 (`doctor_runtime.py`). Shared files: §4.2.

## Goal

Notes in the Apple Notes folders the owner names become searchable, read through
`osascript -l JavaScript` (path (b)), stored `confidential` by default.

## Files

| File | Change |
|---|---|
| `src/brain/ingest/capped_process.py` | **New leaf.** The capped `Popen` core lifted out of `github.gh_runner`: own process group, a timer that kills the group at the timeout, stdout read in chunks and the group killed past a byte cap, stderr discarded unread. Returns a `CappedRun`; raises only `OSError`. The second capped runner would otherwise copy RM-31's ~40 lines. |
| `src/brain/ingest/github.py` | `gh_runner` keeps its `which`/label/error mapping and calls `run_capped`; messages, seams and behaviour unchanged (net negative). |
| `src/brain/ingest/apple_notes.py` | **New.** `SOURCE_KIND = "applenotes"`; the fixed JXA script; `osascript_runner` (120 s timeout, 64 MiB stdout cap); `read_notes(request, runner)` → `NotesBatch`, every field validated; `AppleNotesError`. |
| `src/brain/ingest/notes_html.py` | **New.** Stdlib `html.parser` converter, Notes HTML → Markdown: headings, bold/italic, ordered/unordered/nested lists, checklists, links (http/https/mailto only), blockquotes, `pre`, tables as `a | b` rows; `img`/`object`/`script`/`style` dropped. |
| `src/brain/cli_ingest_apple_notes.py` | **New.** The command: scope check, platform check, cap, ingest loop, report. |
| `src/brain/cli_registry.py` | One import + one `EXTENSION_REGISTRARS` entry. |
| `src/brain/source_kinds.py` | `applenotes` added, with its comment line. |
| `src/brain/doctor_apple_notes.py` | **New leaf**: the `apple notes` row. |
| `src/brain/doctor_runtime.py` | Compose the leaf in (import + one line + docstring entry, ≈ +4 of the +60 budget). |
| `src/brain/ui/static/js/results.js`, `discovery.js` | `applenotes: "🗒️"`. `tree.js`: no entry (no month grouping, as the ticket states). |
| `skills/ingest-brain/SKILL.md` | One routing-table row. No `sources/applenotes.md`: the command carries its own scope, so the row is the whole recipe. |
| Tests | `test_notes_html.py`, `test_apple_notes_reader.py`, `test_capped_process.py`, `test_cli_ingest_apple_notes.py` (5434 DB), `test_doctor_apple_notes.py`, `test_apple_notes_source.py`; updates to `test_source_kinds_identity.py` (`ADDED_KINDS`), `test_demo_corpus.py` (`DEMO_EXEMPT_KINDS` + reason), `test_cli_registry_extensions.py`, `test_doctor_runtime.py` (row order). |
| Docs | `docs/guides/apple-notes.md` (the Automation prompt), `docs/cli-reference.md`, `CHANGELOG.md` [Unreleased], spec Appendix B row, backlog roadmap-2.4 row. |

`cli.py`, `cli_ingest.py`, `config.py`, `ingest/__init__.py`: untouched. No migration.

## Protocol (§2.4)

1. **Path (b).** `osascript -l JavaScript -e <JXA_SCRIPT> <request-json>`. The script is a module
   constant and never formatted. The folder names, `--since` and the limit travel as ONE JSON
   argv element that the script reads with `JSON.parse(argv[0])`, so a name holding quotes, `);`
   or a JS expression is data, never code. The element always starts with `{`, so osascript
   cannot read it as an option. The script only reads (no `make`, `delete`, `move`, `save`,
   `doShellScript`, no StandardAdditions); a test pins that. Runner injectable
   (`apple_notes_runner` module attribute in the CLI, swapped with `monkeypatch.setattr`), 120 s
   timeout, the whole process group killed on timeout or past the stdout cap.
2. **Kind** `applenotes`; glyph in both tables; no month grouping.
3. **Dedup** `(applenotes, <note id>)` through `ingest_document`; an edited note updates in
   place. Volatile keys (`folder`, `modified`) are metadata; a re-run whose body is unchanged
   patches them with `update_document(metadata_patch=…)` (no re-chunk). `metadata.date` (promoted
   to `sent_at`) is the note's creation day, which never moves.
4. **Credentials:** none. First run → macOS Automation prompt (guide). A nonzero osascript exit
   prints a fixed hint naming System Settings → Privacy & Security → Automation; osascript's own
   stderr is discarded unread.
5. **Scope:** `--folder NAME` (repeatable) required; refusal, exit 2:
   `ingest-apple-notes requires --folder NAME (repeatable); "all notes" is not offered`.
   `--since YYYY-MM-DD` optional (malformed or future → exit 2). Folders matched by exact name in
   every account; a name that matches no folder is warned; if none matches, exit 1.
6. **PII/sensitivity:** body = the stdlib converter's Markdown; attachments dropped (the script
   also strips `<img>` so inline image data never crosses stdout); locked notes are never read
   (`passwordProtected`) and are counted. Default `confidential`; `--sensitivity normal`
   overrides. Secret guard as every ingest.
7. **Tests:** synthetic JXA JSON through the fake runner; converter ≥ 95% (target 100%); scope
   refusal; non-macOS exit 1 (`host_platform` module attribute, swapped in tests, so the suite runs
   on Linux CI); the adversarial injection test; an end-to-end run through the CLI with a fake
   `osascript` script on a scratch `PATH` (never the real one).
8. **Dependencies:** none (RM-36.D1: no `markdownify`).
9. **Cost:** summaries and concepts on (`--no-enrich` skips the summary); `--max` default 200
   (min 1). The script returns bodies only for the newest `--max` eligible notes and the count of
   the rest, so the run reports "stopped at the --max cap: N more note(s) match". Worst case one
   capped run: 200 × 8–10 min ≈ 27–33 h of graph-lane time; the guide says to start with `--since`.

## Decisions taken here (ticket silent)

- **R1 Shared capped runner.** Lift RM-31's runner into `ingest/capped_process.py` rather than
  copy it (CLAUDE.md lessons 3); github.py re-pointed, its tests unchanged.
- **R2 Sort and cap in the script.** Bulk-read ids/dates/locked flags, sort newest `modified`
  first, read bodies only for the first `limit`. Notes scripting is slow per note, so this bounds
  the run (ticket risk). Python re-applies the cap (the cost bound must not rest on the script
  alone); `--since` is a scope narrowing, not a security boundary, so it is applied once, in the
  script.
- **R3 Duplicate folder names** (one per account) are all read; a note is listed once.
- **R4 `--dry-run`** lists `folder / title` and writes nothing (parity with RM-30/31).
- **R5 Output validation.** A note entry missing a string `id`/`name`/`body`/`modified`, or with
  an id over 512 characters or holding control characters, is skipped and counted, never fatal.
- **R6 Doctor row `apple notes`:** non-macOS → OK "not macOS; brain ingest-apple-notes is
  unavailable here" (not a WARN: nothing is broken); macOS without `osascript` → WARN; macOS with
  it → OK, naming the first-run permission prompt. Never runs `osascript`.

## Acceptance criteria → tests

1. No `--folder` → exit 2, runner never called.
2. Synthetic notes land `confidential`; locked ones counted in the report.
3. Converter keeps headings, lists, links, checklists.
4. `host_platform = "linux"` → exit 1 with the macOS message, runner never called.
5. `--max 2` with 5 eligible → 2 ingested, "3 more" reported.

## Order (TDD)

1. `capped_process` + github re-point (github tests green before and after).
2. `notes_html` converter.
3. `apple_notes` reader + adversarial injection test.
4. CLI + registry + kind + DB tests + fake-osascript end-to-end.
5. Doctor leaf + composition.
6. Front end, skill, docs, memory.
7. `ruff check && mypy src/`, named tests under the lock, `test_log_privacy_rules.py`; mutation
   probes in a `git archive` copy.
