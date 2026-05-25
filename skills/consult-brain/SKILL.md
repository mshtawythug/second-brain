---
name: consult-brain
description: >
  Answer questions and write content using the user's local "second brain" — a
  hybrid-search knowledge base of their career artifacts, meeting transcripts,
  Slack threads, Gmail conversations, and authored vault notes. Use this skill
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

For THEMES, PATTERNS, or CONNECTIONS **across** interactions — recurring
themes, "what connects A and B", "themes with X", "how my thinking on Z has
evolved", "map out …" — use `brain-graph` instead. That skill walks the entity
graph (people/concepts + their relationships + clusters); this one stays for
lookups, facts, quotes, and writing in the user's voice. Rule of thumb: a flat
answer about content → stay here; relationships, themes, or clustering →
`brain-graph`.

For ingesting new content into the brain, see `ingest-brain`. For authoring
new notes, see `brain-authoring`. For action-item / TODO queries, see
`brain-todo`. For health / re-embedding / maintenance, see `brain-maintenance`.

## When this fires

1. **Q&A about the user's history** — "What did I say to X about Y?", "Did I
   ever discuss [topic] with [person]?", "What was my Q3 plan for the team?"
2. **Synthesis across sources** — "Summarize my conversations about pricing",
   "What action items did I get out of my January meetings?", "Pull every
   interview I did about [topic]."
3. **Writing in the user's voice** — "Draft a LinkedIn post about [topic] in
   my voice", "Help me reply to this recruiter", "Write a cover letter for
   [role] using my actual examples and phrasing."
4. **Lookup** — "What's my elevator pitch?", "Summarize my [prior role]
   experience."

If the question is a generic technical/coding/news question, defer — that's
not what the brain is for.

## How to consult the brain

### Step 1 — Distill the question into search terms

Pick 1–3 specific search queries. Always pass `--json` so output parses cleanly.

```bash
brain search "<query>" --limit 10 --json
```

Use filters to sharpen. Most useful flags:

| Flag | Purpose |
|---|---|
| `--source krisp\|slack\|gmail\|manual` | Scope by source kind |
| `--tag <name>` *(or `--has-tag <name>`)* | Require a tag |
| `--without-tag <name>` | Exclude docs carrying a tag |
| `--since N` | Last N days by **ingestion time** (`documents.ingested_at`). When the user means "the call/email happened in the last N days," reach for `--after` instead — `--since` will misfire on content ingested today but dated months ago. |
| `--after YYYY-MM-DD` / `--before YYYY-MM-DD` | Absolute event-date window — filters on `coalesce(sent_at, ingested_at)`, inclusive lower / exclusive upper. Prefer this for "last month / Q1 / since the kickoff" queries. |
| `--person "<name or email>"` | Resolves through the directory (exact email → exact display name → substring; alpha tiebreak). Ambiguous match exits 2 with candidates — narrow it down or use email |
| `--kind <content-type>` | `transcript`, `email`, `krisp_action_items`, `note`, `markdown`, `pdf`, … (this is `documents.content_type`, NOT the tier enum) |
| `--thread <gmail_thread_id>` | Pull every doc in one Gmail thread |
| `--draft / --no-draft` | Tri-state; default both. `--no-draft` excludes quarantined docs |
| `--limit/-n N` | Default 5; bump to 10–20 for synthesis |
| `--fts-only` | Keyword-only fallback when vector retrieval feels off |

Query-shape tips:

- "What did I say to <person> last month?" →
  `brain search "<topic>" --person "<name>" --after <date-30d-ago> --limit 10 --json`
  *(Use `--after` for event-date queries. `--since` only narrows by ingestion time — fine when the corpus was ingested as events happened, misleading after a bulk backfill.)*
- "Pull every interview about scaling teams" →
  `brain search "scaling teams" --tag interview --limit 8 --json`
- "Action items from Q1 meetings" → use `brain-todo` instead.
- "Write in my voice about engineering culture" →
  `brain search "engineering culture" --kind note --limit 5 --json`
  (vault notes carry tone better than transcripts).

**People shortcut.** Before searching, if the user names a person you don't
know yet, run `brain people "<name>"` first to confirm the canonical display
name + primary email + recent docs. Then pivot into `brain search --person`.

### Step 2 — Read full text on the most relevant hits

`brain search` returns short snippets. For real synthesis, fetch the full doc:

```bash
brain show <id-prefix> --json
```

ID prefixes are 6+ hex chars from the search result. The JSON includes a
`summary` field when the doc has been auto-summarized — for fast Q&A where the
summary already answers cleanly, you can stop there. For voice mimicry,
quoting, or synthesis across multiple docs, read the full `content`. Don't
synthesize from snippets alone.

### Step 3 — Synthesize, grounded

- **Cite by title** at minimum, by id-prefix when useful: "From your
  YYYY-MM-DD Krisp call (`<id>`) you said …"
- **Quote sparingly** but when you do, quote verbatim from `brain show`
  content. Don't paraphrase quotations.
- **Don't invent biographical claims.** If retrieved content doesn't cover the
  question cleanly, say so and ask the user for the missing input rather than
  fabricate. NEVER make up: company names, dates, headcounts, revenue numbers,
  deal sizes, named people, technical decisions, salary/comp figures.
- **Voice mimicry**: mirror retrieved phrasing, sentence rhythm, and example
  density. Don't shift to a more formal/marketing register than the source.

### Step 4 — Surface the source + record feedback

End the response with a short "Sources" list — titles + id-prefixes you used.

If the user signals the answer was on-target or off-target, log it:

```bash
brain rate <id-prefix> useful        # answer drew on this doc and helped
brain rate <id-prefix> irrelevant    # this doc was a false positive
```

This appends to the `interactions` table; future ranking iterations use it.
Don't ask for permission — log silently when the user reacts positively to a
specific cited doc, or when they tell you a doc was off-base.

## Debugging weird results — `brain explain`

If a query returns docs that look wrong, use `brain explain` to see the
ranking math:

```bash
brain explain "<query>" --limit 10 --verbose --json
```

It returns per-result FTS rank, vector cosine, RRF contributions, recency
boost, best-chunk index, and matched filters. Two things to look for:

- **All-low Vec-cos under 0.3** — vector retrieval drifted; consider
  `--fts-only` for the actual query.
- **`matched_filters` doesn't include a filter you passed** — your filter
  silently didn't apply (typo, unresolved person name, etc.).

## When NOT to reach for the brain

- The user asks about something obviously NOT in the brain (generic coding
  question, weather, news, public facts).
- The brain is empty for the topic. Don't broaden-and-broaden hoping to
  scrape something — say "no relevant content found in your brain for X,
  want to share more context?" and stop.
- The user explicitly says "don't search my brain" / "skip the brain".
- The ask is about **themes, patterns, or connections across** interactions
  (recurring themes, "what connects A and B", "themes with X", "how my thinking
  evolved", "map out …") — hand off to `brain-graph`, which walks the entity
  graph instead of returning a flat ranked doc list.

## Examples — full flow

**A — Q&A**
> User: "What did I say to <person> about <topic>?"

```bash
brain people "<person>"                                          # confirm canonical name
brain search "<topic>" --person "<person>" --limit 5 --json
brain show <top-id> --json
```

Answer: "From your YYYY-MM-DD Krisp call (`<id>`), you framed it around three
things: …" Surface sources. If the user nods, `brain rate <id> useful`.

**B — Voice writing**
> User: "Help me write a LinkedIn post about engineering hiring."

```bash
brain search "engineering hiring" --kind note --limit 5 --json
brain show <top-id> --json
brain show <second-id> --json
```

Write the post mirroring the retrieved phrasing density and tone. Cite at
bottom.

**C — Summary across many sources**
> User: "Summarize every conversation I had about pricing this quarter."

```bash
# "This quarter" means the calendar quarter the user is in right now.
# Resolve the quarter start before running the search — e.g., Q1 → YYYY-01-01,
# Q2 → YYYY-04-01, Q3 → YYYY-07-01, Q4 → YYYY-10-01. Do NOT substitute "today − 90d";
# mid-quarter that pulls last quarter's data into a "this quarter" query.
brain search "pricing" --after <quarter-start-YYYY-MM-DD> --limit 20 --json
# group by source/title; for each top doc:
brain show <id> --json
```

For a bounded quarter (e.g., "Q1 2026 pricing conversations"), pass both:
`--after 2026-01-01 --before 2026-04-01` (exclusive upper).

Synthesize a 4–6 paragraph thematic summary (not chronological), grouped by
counterparty when relevant. Cite each theme with the supporting docs.

## Operational notes

- The `brain` CLI works from any cwd. No need to `cd` into the second-brain repo.
- `brain doctor` confirms the DB is healthy if a query fails — see `brain-maintenance` for the recovery path.
- If `brain search` returns zero results, try broadening (drop a filter, drop a tag), then if still zero, tell the user rather than make something up.
- The brain is updated by `brain ingest-dir`, `brain ingest-gmail`, and Claude-driven Krisp/Slack ingestion via `ingest-brain`. If a recent event isn't in the brain yet, say so.
