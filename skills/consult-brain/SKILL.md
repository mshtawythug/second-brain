---
name: consult-brain
description: >
  Answer questions and write content using the user's local "second brain" — a
  hybrid-search knowledge base of their career artifacts, meeting transcripts,
  Slack threads, Gmail conversations, and authored notes. Use this skill
  whenever the user asks about their own history, past meetings, past Slack
  threads or emails, interview prep, specific people they have worked with, or
  asks Claude to write/draft anything in their voice. Trigger on phrases like
  "what did I say to X", "summarize my conversations about Y", "what's in my
  brain about Z", "pull my thoughts on W", "write a [thing] in my voice",
  "draft this like I would", or any question whose answer plausibly lives in
  the user's personal corpus. Also trigger on indirect signals like "do I have
  anything written about ..." or "search my notes for ...". Prefer this skill
  over guessing or inventing biographical content.
  MANDATORY TRIGGERS: ask my brain, second brain, what did I say, my
  conversations, my notes about, write in my voice, draft like me, my career,
  my interview prep, summarize my emails about, what did I tell, did I ever
  say, remind me about my, pull from my brain.
---

# Consult Brain

Use the local `brain` CLI to search the user's hybrid FTS+vector knowledge
base, then synthesize a grounded answer or draft from the retrieved content.
Stay strictly grounded in retrieved material — never invent biographical
claims or specific quotes.

## When this fires

Any of these patterns:

1. **Q&A about the user's history**: "What did I say to X about Y?", "Did I
   ever discuss [topic] with [person]?", "What was my Q3 plan for the team?"
2. **Synthesis across sources**: "Summarize my conversations about pricing",
   "What action items did I get out of my January meetings?", "Pull every
   interview I did about [topic]."
3. **Writing in the user's voice**: "Draft a LinkedIn post about [topic] in
   my voice", "Help me reply to this recruiter", "Write a cover letter for
   [role] using my actual examples and phrasing."
4. **Lookup**: "What's my elevator pitch?", "Summarize my [prior role]
   experience."

If the user's question doesn't fit any of these — e.g., a generic technical
question, a Krisp/Gmail action that should go through MCPs first — defer.

## How to consult the brain

### Step 1 — Distill the question into search terms

Pick 1–3 specific search queries. Examples:

- "What did I say to <person> last month?" →
  `brain search "<person>" --since 60 --limit 10`
- "Pull every interview about scaling teams" →
  `brain search "scaling teams" --tag interview --limit 8`
- "Action items from Q1 meetings" →
  `brain search "action item" --since 90 --limit 10`
- "Write in my voice about engineering culture" →
  `brain search "engineering culture" --limit 5` (then read a few authored
  notes for tone)

Use filters when they sharpen the query:

- `--source krisp|slack|gmail|manual` to scope by source
- `--tag <tag>` to scope by whatever tag conventions the user has adopted
- `--since N` for "last N days" recency filter
- `--limit N` (default 5; bump for synthesis tasks)
- `--fts-only` for keyword-only matches when vector retrieval feels off

Always use `--json` so the output parses cleanly:

```bash
brain search "..." --since 60 --limit 10 --json
```

### Step 2 — Read full text on the most relevant hits

`brain search` returns short snippets. For real synthesis, fetch the full
document:

```bash
brain show <id-prefix> --json
```

ID prefixes are 6+ hex chars from the search result. Read the full content of
the top 2–4 hits before synthesizing. Don't synthesize from snippets alone —
they're previews, not source.

### Step 3 — Synthesize, grounded

When answering or drafting:

- **Cite by title** at minimum, by id-prefix when useful: "From your
  YYYY-MM-DD Krisp call (`<id>`) you said …"
- **Quote sparingly** but when you do, quote verbatim from `brain show`
  content. Don't paraphrase quotations.
- **Don't invent biographical claims**. If retrieved content doesn't cover the
  question cleanly, say so and ask the user for the missing input rather than
  fabricate. Examples of things to NEVER make up: company names, dates,
  headcounts, revenue numbers, deal sizes, named people, specific technical
  decisions, salary/comp figures.
- **Voice mimicry**: when drafting in the user's voice, mirror retrieved
  phrasing, sentence rhythm, and example density. Don't shift to a more
  formal/marketing register than what the retrieved docs use.

### Step 4 — Surface the source

End the response with a short "Sources" list — titles + id-prefixes of the
documents you used. This lets the user run `brain show <id>` to see the
original.

## Filtering tips

- **Recent**: `--since 7` for this week, `--since 30` for last month,
  `--since 90` for last quarter, `--since 365` for this year.
- **Conversations vs documents**: `--source krisp` is meeting transcripts,
  `--source gmail` is email threads, `--source slack` is Slack threads,
  `--source manual` is manually-ingested files (PDFs / DOCX / Markdown / TXT).
  Authored vault notes have `kind='vault'` and surface without a source
  filter.

## Don't reach for the brain when …

- The user asks about something obviously NOT in the brain (a generic coding
  question, weather, news, anything outside their personal corpus).
- The brain is empty for the topic and the user just needs to be told. Don't
  keep searching with broader terms hoping to scrape something — say "no
  relevant content found, want to share more context?"
- The user explicitly says "don't search my brain" or similar.

## Examples — full flow

**Example A — Q&A**
> User: "What did I say to <person> about <topic>?"

```
brain search "<person> <topic>" --limit 5 --json
brain show <top-id> --json
# (read 1–2 docs in full)
```

Answer: "From your YYYY-MM-DD Krisp call (`<id>`), you framed it around three
things: [retrieved bullet 1], [retrieved bullet 2], [retrieved bullet 3]. The
transcript also captures [counterparty] pushing back on [point], to which you
replied [verbatim quote].

Sources:
- YYYY-MM-DD Krisp — <title> (`<id>`)
- Email thread — <title> (`<id>`)"

**Example B — Voice writing**
> User: "Help me write a LinkedIn post about engineering hiring."

```
brain search "engineering hiring" --limit 5 --json
brain show <top-id> --json
brain show <second-id> --json
```

Then write the post mirroring the retrieved phrasing density and tone. Cite
the source docs at the bottom.

**Example C — Summary across many sources**
> User: "Summarize every conversation I had about pricing this quarter."

```
brain search "pricing" --since 90 --limit 20 --json
# group by source/title, read top 5–8 in full
brain show <id> --json    # x5–8
```

Synthesize: write a 4–6 paragraph summary structured as themes (not
chronological), grouped by counterparty when relevant. Cite each theme with
the supporting docs.

## Operational notes

- The `brain` CLI works from any cwd. No need to `cd` into the second-brain
  repo.
- `brain doctor` will confirm the DB is healthy if a query fails.
- If `brain search` returns zero results, try broadening (drop a filter, drop
  a tag), then if still zero, tell the user rather than make something up.
- The brain is updated by the user running `brain ingest-dir`, `brain
  ingest-gmail`, and Claude-driven Krisp/Slack ingestion. If a recent event
  isn't in the brain yet, say so.
