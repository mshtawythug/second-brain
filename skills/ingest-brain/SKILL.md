---
name: ingest-brain
description: >
  Add new content to the user's local second-brain knowledge base using the
  `brain` CLI's ingestion commands. Use this skill whenever the user asks to
  ingest, save, pull, or capture content into the brain — including files
  (PDFs / DOCX / Markdown / TXT), directories of files, Gmail threads, Krisp
  meeting transcripts, Slack threads, the user's own Jira issues, or arbitrary
  stdin snippets. For Krisp/Slack/Jira the brain does NOT have a direct
  integration; instead orchestrate
  the relevant MCP server and pipe the fetched content into `brain
  ingest-stdin`. Always pick a sensible scope flag — never bulk-ingest the
  Gmail inbox.
  MANDATORY TRIGGERS: ingest, ingest last week, ingest this file, ingest this
  directory, pull this email, pull last week's Krisp calls, save this Slack
  thread, save this transcript, add this to my brain, capture this in my
  brain, import this into the brain, brain ingest, brain ingest-stdin, brain
  ingest-gmail, ingest my jira tickets.
---

# Ingest Brain

Use the `brain` CLI to add content to the user's hybrid-search corpus. Pick
the right ingestion path based on where the content lives. **Never invent
content** — ingest only what the user has actually pointed you at or what an
MCP returned.

For searching/synthesis, see `consult-brain`. For authoring brand-new vault
notes, see `brain-authoring`.

## Choosing the right command

| Source | Command | Notes |
|---|---|---|
| One local file (PDF / DOCX / MD / TXT) | `brain ingest <path>` | Dedup by `source_path`. `--force` reapplies. Scanned / image-only PDFs are OCR'd when `tesseract` is on PATH (`brew install tesseract`; `brain doctor`'s `ocr` line); without it, a hint plus "skipped (empty document)". Knobs: `BRAIN_OCR` / `BRAIN_OCR_LANG` / `BRAIN_OCR_MAX_PAGES`. |
| A directory of files | `brain ingest-dir <dir>` | Recursive. `--ext md,pdf` to scope. `--dry-run` first. |
| Gmail messages | `brain ingest-gmail` | **REFUSES without a scope flag.** See Gmail section. |
| Google Calendar events | `brain ingest-calendar --since YYYY-MM-DD --until YYYY-MM-DD` | **REFUSES without both dates.** At most 92 days, ending at most 14 days ahead. `--dry-run` first. |
| Krisp transcript / Slack thread / arbitrary text | `brain ingest-stdin` | The MCP→stdin chain. See Krisp + Slack sections. |
| The user's own Jira issues | `brain ingest-stdin --source jira` | Atlassian MCP→stdin, **confidential**. See Jira section. |
| Krisp action items as a separate doc | `brain enrich --krisp-action-items` (handoff) + `brain ingest-stdin --content-type krisp_action_items` | See `brain-todo` for the full flow. |
| The user's own GitHub PRs, issues, READMEs | `brain ingest-github --mine --since YYYY-MM-DD` | **REFUSES without `--repo`/`--mine` and `--since`.** See GitHub section. |

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

## Gmail — `brain ingest-gmail`

This command **refuses to run without a scope flag** by design. Pick the
narrowest scope that satisfies the user's ask:

| User ask | Flag |
|---|---|
| "Ingest emails from `<person>`" | `--from someone@example.com` |
| "Ingest my Linear tickets / GitHub PR notifications" | `--label "linear/notifications"` or whatever the user uses |
| "Pull emails about <topic>" | `--query "subject:<topic> OR body:<topic>"` (Gmail query syntax) |
| "Pull last 30 days" | `--since YYYY/MM/DD` (concrete date 30 days ago — Gmail's native `after:` syntax; relative strings like `30d` build a broken `after:30d` and return nothing) |
| "Pull this specific thread" | `--query "rfc822msgid:<msg-id>"` — `--query` takes **raw Gmail search syntax** only (e.g. `from:alice@example.com subject:invoice`). If the user gives you a Gmail search URL, extract the underlying query string from it before passing — don't paste the URL. |

Always run with `--dry-run` first when the scope might be broader than the
user expected. Cap with `--max 100` for safety. Note: **drafts are now
included** (Q1-A change) — they ingest with `documents.draft=TRUE` and are
hidden from `brain ui`'s browseable views but visible to `brain search` / `brain recall` /
`brain show`.

The summary line is `N ingested (M draft)` when drafts are present.

## The context cost of the MCP→stdin chain

Read this before running the Krisp or Slack chains below.

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

## Krisp transcripts — MCP→stdin chain

The brain has no direct Krisp integration. Claude orchestrates:

1. Call the Krisp MCP to search/list meetings, fetch transcripts. Use
   whichever Krisp MCP tool fits — your harness exposes them as
   `mcp__claude_ai_Krisp__*`. Don't hardcode parameter names; read your live
   MCP schema for the available tools and parameters.
2. For each meeting, pipe the transcript body into `brain ingest-stdin`:

```bash
echo "<transcript content>" | brain ingest-stdin \
  --source krisp \
  --external-id "<meeting_id>" \
  --title "<short title — usually the meeting topic + date>" \
  --content-type transcript \
  --date YYYY-MM-DD \
  --metadata '{"participants": ["Alice", "Bob"], "duration_min": 42}'
```

Dedup is by `(source_kind='krisp', external_id=<meeting_id>)` — re-ingesting
the same meeting UPDATEs in place. UUID + tags survive.

3. Report counts back to the user: "Ingested N new transcripts, updated M
   existing ones."

## Slack threads — MCP→stdin chain

Same pattern with the Slack MCP. Fetch a thread, then:

```bash
echo "<thread content>" | brain ingest-stdin \
  --source slack \
  --external-id "<channel-id>/<thread-ts>" \
  --title "Slack — <#channel> — <first message snippet>" \
  --content-type thread \
  --date YYYY-MM-DD \
  --metadata '{"channel": "#engineering", "participants": ["U1", "U2"]}'
```

Pick `--external-id` so it's stable across re-ingests. Channel-id + thread-ts
is the canonical Slack identifier; thread-ts alone is also unique within a
workspace.

## Jira issues — MCP→stdin chain

The user's own tickets, through the Atlassian MCP connector (`searchJiraIssuesUsingJql`,
then `getJiraIssue`). There is no Jira CLI and no flag to enforce scope, so **this recipe
is the scope** — follow it exactly.

**The JQL.** Use this one unless the user narrows it further:

`(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d`

Keep the parentheses: without them `AND` binds tighter and the time bound applies to
`reporter` only. A narrower JQL is fine (a shorter window, one project, one key), but it
must still name the user and carry a time bound. **Refuse a JQL with no time bound**, and
tell the user why. Project-wide pulls (every issue in a project) are not part of this recipe.

**What goes in the body:** the key and summary, the description, and the user's own
comments. Other people's comments go in only when the user asks for them. Status and
assignee change often, so they go in `--metadata`, never in the body:

```bash
echo "<KEY>: <summary>

<description>

<the user's own comments>" | brain ingest-stdin \
  --source jira \
  --external-id "<site-host>/<ISSUE-KEY>" \
  --title "<ISSUE-KEY> — <summary>" \
  --content-type jira_issue \
  --sensitivity confidential \
  --date YYYY-MM-DD \
  --tag jira \
  --metadata '{"site": "<site-host>", "issue_key": "<ISSUE-KEY>", "status": "<status>", "assignee": "<name>"}'
```

- **Always `--sensitivity confidential`.** Work-tracker content belongs to the employer:
  confidential keeps it off a hosted embedder and out of generated pages and unprompted
  listings. Only the user can ask for `normal`.
- **Dedup is `(jira, <site-host>/<ISSUE-KEY>)`**, so the same key on two sites stays two
  documents. Re-ingesting a changed issue updates it in place.
- **An unchanged body prints `skipped (already ingested): <title> → <document-id>` and
  leaves metadata alone.** If only the status or assignee moved, patch it without a
  re-chunk, passing the id after `→`:
  `brain edit <document-id> --metadata '{"status": "Done"}'`.

The 30-day bound keeps a run to tens of issues; batch small, as for Krisp.

## GitHub PRs, issues and READMEs

**Prefer the CLI**: `brain ingest-github` reads through the user's own `gh` login, and only
the user's own items (author, assignee or reviewer). It **refuses without `--repo OWNER/REPO`
or `--mine`, and `--since`**; `--repo` narrows to those repositories, never to everyone's items
there. It stops at 500 documents a run, keeps only the user's own comments unless
`--with-thread`, and stores any repository that is not public as **confidential**. Start with
a short `--since` and a `--dry-run`: each new document costs a summary and concept extraction.

**Without `gh`, use the GitHub MCP connector** (`search_pull_requests` / `search_issues`, then
`pull_request_read` / `issue_read`). The recipe is the scope: only the user's own items
(`author:@me`, `assignee:@me` or `reviewed-by:@me`, never an organisation) with an
`updated:>=YYYY-MM-DD` bound; refuse a search with no date bound. Body: the title line, the
description and the user's own comments; state goes in `--metadata`, never the body. Pass
`--sensitivity confidential` for any repository that is not public (private, internal, or
visibility unknown), and `--content-type github_issue` for an issue:

```bash
echo "<owner>/<repo>#<n>: <title>

<description>

<the user's own comments>" | brain ingest-stdin --source github \
  --external-id "<owner>/<repo>#<n>" --title "<owner>/<repo>#<n> — <title>" \
  --content-type github_pr --date YYYY-MM-DD --tag github \
  --metadata '{"repo": "<owner>/<repo>", "state": "<state>"}'
```

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
