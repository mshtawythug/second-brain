---
name: ingest-brain
description: >
  Add new content to the user's local second-brain knowledge base using the
  `brain` CLI's ingestion commands. Use this skill whenever the user asks to
  ingest, save, pull, or capture content into the brain — including files
  (PDFs / DOCX / Markdown / TXT), directories of files, Gmail threads, Krisp
  meeting transcripts, Slack threads, the user's own Jira issues, a Notion
  export, or arbitrary stdin snippets. For Krisp/Slack/Jira the brain does NOT have a direct
  integration; instead orchestrate
  the relevant MCP server and pipe the fetched content into `brain
  ingest-stdin`. Always pick a sensible scope flag — never bulk-ingest the
  Gmail inbox.
  MANDATORY TRIGGERS: ingest, ingest last week, ingest this file, ingest this
  directory, pull this email, pull last week's Krisp calls, save this Slack
  thread, save this transcript, add this to my brain, capture this in my
  brain, import this into the brain, brain ingest, brain ingest-stdin, brain
  ingest-gmail, ingest my jira tickets, ingest my apple notes, ingest my notion export.
---

# Ingest Brain

Use the `brain` CLI to add content to the user's hybrid-search corpus. Pick
the right ingestion path based on where the content lives. **Never invent
content** — ingest only what the user has actually pointed you at or what an
MCP returned.

For searching/synthesis, see `consult-brain`. For authoring brand-new vault
notes, see `brain-authoring`.

## Choosing the right command

Each source with a recipe has a reference file in [`sources/`](sources/). **Before ingesting
from that source, read its file in full.** For Jira and GitHub the recipe *is* the scope, so
never run the command from its table row alone.

| Source | Command | Notes |
|---|---|---|
| One local file (PDF / DOCX / MD / TXT) | `brain ingest <path>` | Dedup by `source_path`. `--force` reapplies. Scanned / image-only PDFs are OCR'd when `tesseract` is on PATH (`brew install tesseract`; `brain doctor`'s `ocr` line); without it, a hint plus "skipped (empty document)". Knobs: `BRAIN_OCR` / `BRAIN_OCR_LANG` / `BRAIN_OCR_MAX_PAGES`. |
| A directory of files | `brain ingest-dir <dir>` | Recursive. `--ext md,pdf` to scope. `--dry-run` first. |
| Gmail messages | `brain ingest-gmail` | **REFUSES without a scope flag.** Read [sources/gmail.md](sources/gmail.md). |
| Google Calendar events | `brain ingest-calendar --since YYYY-MM-DD --until YYYY-MM-DD` | **REFUSES without both dates.** At most 92 days, ending at most 14 days ahead. `--dry-run` first. |
| Krisp transcripts | `brain ingest-stdin --source krisp` | Krisp MCP→stdin. Read [sources/krisp.md](sources/krisp.md). |
| Slack threads | `brain ingest-stdin --source slack` | Slack MCP→stdin. Read [sources/slack.md](sources/slack.md). |
| Arbitrary text | `brain ingest-stdin` | The same MCP→stdin chain; [sources/krisp.md](sources/krisp.md) shows the flags. |
| The user's own Jira issues | `brain ingest-stdin --source jira` | Atlassian MCP→stdin, **confidential**. Read [sources/jira.md](sources/jira.md). |
| Krisp action items as a separate doc | `brain enrich --krisp-action-items` (handoff) + `brain ingest-stdin --content-type krisp_action_items` | See `brain-todo` for the full flow. |
| The user's own GitHub PRs, issues, READMEs | `brain ingest-github --mine --since YYYY-MM-DD` | **REFUSES without `--repo`/`--mine` and `--since`.** Read [sources/github.md](sources/github.md). |
| Notes in named Apple Notes folders (macOS) | `brain ingest-apple-notes --folder NAME` | **REFUSES without `--folder`** (repeatable; never all notes). Stored **confidential** unless `--sensitivity normal`. At most `--max` (200) a run; `--dry-run` first. Locked notes and attachments are skipped. The first run triggers macOS's Automation prompt for Notes: the user must allow it (`docs/guides/apple-notes.md` in the brain repo). |
| A Notion export (folder or `.zip`) | `brain ingest-notion <export-path>` | No API: the user exports "Markdown & CSV" first. Pages by id, updated in place on re-export. `--with-databases` for rows; `--max` (default 200) a run. |
| Screenshots / a drop folder | `brain ingest-folder --dir PATH --since 7d` | **Moves the files** into `imported/`. **REFUSES without `--dir`/`--configured` and `--since Nd`.** `--dry-run` first. Read [sources/drop-folders.md](sources/drop-folders.md). |
| Voice memos / a `kind = "voice"` drop folder | `brain ingest-folder --dir PATH --since 7d` | Transcribed locally (whisper.cpp); **needs `BRAIN_WHISPER_MODEL`, never download a model for the user.** Same move-first rules. Read [sources/drop-folders.md](sources/drop-folders.md). |

## Files and directories

```bash
brain ingest ~/Downloads/2026-Q1-OKRs.pdf --tag okrs
brain ingest-dir ~/career-docs --tag career --dry-run    # preview first
brain ingest-dir ~/career-docs --tag career --ext md,pdf
```

Verbs in output:

- `ingested:` — new row inserted
- `updated:` — existing row replaced in-place (content or tags changed, or `--force`)
- `skipped:` — content unchanged

`--force` on `brain ingest` re-applies even when content is byte-identical.
Useful after a chunker / embedder upgrade.

`brain ingest-dir` has no `--force` flag — it always UPDATEs in-place when the
file changed and skips otherwise.

To mirror deletions from a directory imported earlier, add `--sync`: it lists the
documents whose files are gone (`would delete: …`, then `N of M documents under
<dir> would be removed`). Show the user that list and run `--sync --apply` only
when they confirm it. Never add `--allow-empty` on your own: it overrides the
refusals for an empty tree (an unmounted drive) and a mass deletion.

## The context cost of the MCP→stdin chain

Read this before running any MCP→stdin recipe in `sources/`: Krisp, Slack, Jira, and GitHub
through the connector.

Ingest is the **most expensive thing this skill does to your context**, and
it is expensive in a way the other brain skills are not. In `consult-brain`
you choose how much text to pull in (`--brief`, `brain recall --budget N`).
Here you have no such control: the MCP returns the full transcript or thread,
that text lands in your context, and you echo it straight back out to
`brain ingest-stdin`. **Every ingested document transits your context twice —
once inbound from the MCP, once outbound in the `echo`.**

Measured on this corpus, a Krisp transcript is a **mean of ~7,900 tokens**
(median 7,400; p90 15,300; largest 33,600 — n=261; reproducible from
`docs/audits/2026-08-11-wave2-routing-counterfactual.md`).
Doubled by the echo, that is
~16k tokens of context per meeting. Ten meetings in one pass is ~160k — a
context window. Consequences to plan around:

- **Batch small.** "Ingest last week's Krisp calls" is 5–15 meetings. Do them
  a few at a time and report counts as you go, rather than fetching all of
  them before the first `ingest-stdin`.
- **Never re-read what you just wrote.** After `ingest-stdin` succeeds, the
  content is in the brain. Verify with `brain status` / `brain list` (titles
  and counts), **not** `brain show` on each new document — that would be a
  third pass over the same text for no new information.
- **Don't summarize the transcript yourself on the way through.** The
  post-ingest hook already summarizes on local Ollama, off your context.
  Pipe the raw body and let it run.
- **If the user only wants to know what was discussed** — not to store it —
  ingest is the wrong tool. Ingest once, then query with `consult-brain`
  (`brain search --brief`, `brain recall --budget N`), where the read is
  bounded.

Note that no *ingest* command takes `--json`; they report counts, not payloads,
so there is nothing here to parse and nothing to trim. (The verification
commands below — `brain list`, `brain show` — do take it.) The cost is entirely
in the content you carry to reach `ingest-stdin`.

## Tags on ingest

Every ingest command takes one or more `--tag` flags. Tags are
auto-normalized at write time (casefold-lowercase, hyphen-separated, deduped)
— any case/separator at the CLI is fine.

```bash
brain ingest <file> --tag interview-prep --tag career-2026
```

## Disabling the post-ingest summary

All four ingest commands run an LLM auto-summary post-hook by default (Q1-D).
Skip with `--no-enrich` when:

- The doc is tiny and the summary would be noisy.
- Ollama is known down and you want the ingest to complete fast.
- You're scripting a bulk import and will run `brain enrich --backfill`
  afterwards.

```bash
brain ingest-stdin --source slack --external-id ... --title ... --no-enrich
```

## Verifying after ingest

After any non-trivial ingest, sanity-check:

```bash
brain status                                   # totals + breakdown by source
brain list --source krisp --limit 5            # see the freshest rows
```

Those two are titles and counts — cheap, and enough to confirm the rows
landed. Only if something looks wrong, spot-check **one** document with
`brain show <id-prefix>`. Do not `brain show` each thing you just ingested:
you already had that text in context on the way in, and re-reading a batch of
transcripts costs more than the ingest did.

If `brain doctor` flags an Ollama / Postgres issue, route to `brain-maintenance`.

## Safety rules

- **Never bulk-ingest the inbox.** `brain ingest-gmail` enforces this with the
  scope-flag requirement; respect the spirit when picking scopes.
- **Jira: the user's own issues, time-bounded, confidential.** No JQL without a
  time bound, no project-wide pulls, always `--sensitivity confidential`.
- **GitHub: the user's own activity, date-bounded.** Never an organisation-wide
  pull; a repository that is not public is always confidential.
- **Don't invent content for stdin ingests.** If the MCP returned nothing,
  tell the user — don't generate a placeholder transcript.
- **No untagged dumps.** Always pass `--tag` so the user can find/clean the
  batch later.
- **Mind external_id stability.** A Krisp meeting that gets re-ingested with
  a different `--external-id` becomes a duplicate row. Match what the MCP
  returns as the canonical meeting id.
