---
name: consult-brain
description: >
  Answer questions or write content using the user's local "second brain" — a
  hybrid-search knowledge base of career artifacts, meeting transcripts, Slack
  threads, Gmail, and authored vault notes. Use for lookups about the user's
  own history, quotes, facts, and drafting in their voice. For ONE synthesized
  answer with inline citations, or an audio overview, use `brain-ask`. For
  themes / patterns / connections across interactions, use `brain-graph`.
  MANDATORY TRIGGERS: ask my brain, second brain, what did I say, my
  conversations, my notes about, write in my voice, draft like me, my career,
  my interview prep, summarize my emails about, what did I tell, did I ever
  say, remind me about my, pull from my brain, search my notes for, do I have
  anything written about.
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
For the unprompted side — daily digests, resurfacing, weekly reviews,
timelines, link suggestions — see `brain-proactivity`. **Boundary with
`brain-memory`:** that skill governs what *you the agent* record and recall
about the user across sessions; this one only reads the corpus the *user*
built.

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

## Choosing a retrieval command (cost-ordered, cheapest first)

| You want | Use | Typical cost |
|---|---|---|
| One synthesized answer with citations | `brain ask "<q>" --json` | ~1–2k tokens; the reading happens on local Ollama |
| To triage WHICH documents matter | `brain search "<q>" --json --brief --limit 5` | a few hundred tokens per result |
| To READ material, sized to fit | `brain recall "<q>" --budget 2000 --json` | the passage block stays under the budget — a ceiling, not an exact fill (measured 1,811 tokens on a 2,000 budget); the `--json` envelope prints ~1.25–1.3× the budget (`--budget 2000` → ~2,500 tokens) |
| Why a specific document matched | `brain search "<q>" --json --limit 5` (no --brief) | ~1.6k chars per result |
| One document's full text | `brain show <id> --json` | the whole body — up to ~67k tokens on the largest doc |

Rules:
- NEVER loop `brain show` over a search's results — that recipe is unbounded. Five representative
  20-result questions cost a mean **183,940 tokens** (worst: 257,989), twice the ~91,000 tokens
  arithmetic predicts, because documents ranking top-20 run longer than the corpus mean. The same
  five cost **8,998** via triage-then-recall (−95.1%). Both endpoints are measured; the −95.1%
  between them is a **counterfactual comparison of two documented procedures, not an observed
  saving** — nothing measures whether an agent follows a skill. Source:
  `docs/audits/2026-08-11-wave2-routing-counterfactual.md`. Use `brain recall` or `brain ask`.
- Reach for `brain show` for ONE document whose body you need — ideally after
  `--brief` named it.
- `--brief` swaps each result's snippet for the document's ingest-time summary
  when the summary is smaller, naming which you got via `snippet_source`.
  Cheaper, but it does NOT tell you why the doc matched — drop it when that matters.

## How to consult the brain

The manual loop below (search → read → synthesize) is the default and right for
most questions — reach for it when you need raw documents in context (voice
mimicry, verbatim quoting, drafting). **If the deliverable is instead one cited
answer, do not hand-roll it: hand off to `brain-ask`.**

### Step 1 — Distill the question into search terms

Pick 1–3 specific search queries. Always pass `--json` so output parses cleanly,
and `--brief` unless you need to see *why* each document matched.

```bash
brain search "<query>" --limit 10 --json --brief
```

Use filters to sharpen. Most useful flags:

| Flag | Purpose |
|---|---|
| `--source krisp\|slack\|gmail\|manual` | Scope by source kind |
| `--tag <name>` *(or `--has-tag <name>`)* | Require a tag |
| `--without-tag <name>` | Exclude docs carrying a tag |
| `--since N` | Last N days by **ingestion time** (`documents.ingested_at`); a bare number is days, or pass a duration suffix (`7d` / `24h` / `90m`). When the user means "the call/email happened in the last N days," reach for `--after` instead — `--since` will misfire on content ingested today but dated months ago. |
| `--after YYYY-MM-DD` / `--before YYYY-MM-DD` | Absolute event-date window — filters on `coalesce(sent_at, ingested_at)`, inclusive lower / exclusive upper. Prefer this for "last month / Q1 / since the kickoff" queries. |
| `--person "<name or email>"` | Resolves through the directory (exact email → exact display name → substring; alpha tiebreak). Matches **every recorded name-variant** of a merged person, so docs stored under different forms (Gmail `jane.doe` vs Krisp `Jane Doe`) all surface — the match is complete across sources. A genuinely ambiguous name (two *different* people) exits 2 with candidates — narrow it down or use email |
| `--kind <content-type>` | `transcript`, `email`, `krisp_action_items`, `note`, `markdown`, `pdf`, … (this is `documents.content_type`, NOT the tier enum) |
| `--thread <gmail_thread_id>` | Pull every doc in one Gmail thread |
| `--draft / --no-draft` | Tri-state; default both. `--no-draft` excludes quarantined docs |
| `--limit/-n N` | Default 5; bump to 10–20 for synthesis — pair with `--brief`, which is what keeps a 20-result triage affordable |
| `--brief` | `--json` only. Substitutes the doc's ingest-time summary for the chunk snippet when the summary is cheaper, and adds `snippet_source` ∈ `chunk`/`summary`. Measured −57.4% tokens over an 11-query sample. Ranking is untouched. No-op without `--json` |
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

### Step 2 — Read the material, sized to your context

`brain search` returns short snippets. Don't synthesize from snippets alone —
but don't reach straight for full documents either. Two ways to read, in cost
order:

**Default — `brain recall`.** Returns the passages themselves, packed to an
explicit token budget, one per document so sources stay diverse, with inline
`[N]` citations:

```bash
brain recall "<query>" --budget 2000 --json
```

Same filters as `brain search` (`--person`, `--after`/`--before`, `--kind`,
`--tag`, `--source`, `--thread`, `--without-tag`, `--fts-only`), so most tuned
searches port straight over — `--draft`, `--has-tag`, `--sensitivity` and
`--updated-*` are search-only. Raise `--budget` for a broad question (4000 for
multi-source synthesis). **`--budget` bounds the passage block, not the
`--json` payload**: `--json` adds per-passage metadata around the same text,
measured at ~1.25–1.3× the budget (`--budget 2000` prints ~2,500 tokens,
`--budget 4000` ~5,150). Still the only retrieval surface with a ceiling.

**Single document — `brain show`.** When you know *which* document you need
the body of, and you need it verbatim:

```bash
brain show <id-prefix> --json
```

ID prefixes are 6+ hex chars from the search result. The JSON includes a
`summary` field when the doc has been auto-summarized — for fast Q&A where the
summary already answers cleanly, you can stop there.

**The ceiling.** `brain show` returns the *whole* body — a mean of 18,218
chars, up to ~67k tokens on the largest document. Once is fine; per search
result is the no-loop rule above. Cap yourself at 2–3 calls per question.

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

This appends to the `interactions` table; future ranking uses it. Don't ask
permission — log silently when the user reacts to a specific cited doc.

## Cited synthesis and audio overviews — hand off to `brain-ask`

The manual loop above returns a ranked list you read and synthesize yourself.
When the user instead wants **one** composed answer carrying inline citations
back to their own documents, or a two-host audio overview, that is
`brain-ask`'s territory — hand off rather than stitching several `brain show`
calls together by hand. Rule of thumb: reading documents to write something
yourself → stay here; the deliverable *is* a cited answer or a narrated
overview → `brain-ask`.

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
brain search "<topic>" --person "<person>" --limit 5 --json --brief   # triage
brain show <top-id> --json                                       # ONE doc, the top hit
```

Answer: "From your YYYY-MM-DD Krisp call (`<id>`), you framed it around three
things: …" Surface sources. If the user nods, `brain rate <id> useful`.

**B — Voice writing**
> User: "Help me write a LinkedIn post about engineering hiring."

```bash
brain search "engineering hiring" --kind note --limit 5 --json --brief
brain show <top-id> --json          # voice mimicry needs verbatim prose …
brain show <second-id> --json       # … two docs is the ceiling here
```

Voice mimicry is the one case that genuinely wants full bodies — you are
copying sentence rhythm, not extracting facts. Two documents is enough; a
third rarely changes the voice and doubles the cost. Write the post mirroring
the retrieved phrasing density and tone. Cite at bottom.

**C — Summary across many sources**
> User: "Summarize every conversation I had about pricing this quarter."

```bash
# "This quarter" means the calendar quarter the user is in right now.
# Resolve the quarter start before running the search — e.g., Q1 → YYYY-01-01,
# Q2 → YYYY-04-01, Q3 → YYYY-07-01, Q4 → YYYY-10-01. Do NOT substitute "today − 90d";
# mid-quarter that pulls last quarter's data into a "this quarter" query.

# 1. Triage — which documents are in scope, and how do they group?
brain search "pricing" --after <quarter-start-YYYY-MM-DD> --limit 20 --json --brief

# 2. Read — ONE bounded call, not one call per document.
brain recall "pricing" --after <quarter-start-YYYY-MM-DD> --budget 4000 --json
```

For a bounded quarter (e.g., "Q1 2026 pricing conversations"), pass both:
`--after 2026-01-01 --before 2026-04-01` (exclusive upper). Both commands take
these filters, so step 2 reuses step 1's (search-only exceptions above).

If the user wants the summary *written for them* rather than read by you,
`brain ask "summarize my pricing conversations this quarter" --json` does the
whole thing in one call — see `brain-ask`.

**Hard ceiling on `brain show` here.** A 20-result search invites a `brain
show` per hit — priced in the cost table above, and never the right move. Show
**at most 2–3 documents**: the ones `--brief` and `recall` already flagged.

Synthesize a 4–6 paragraph thematic summary (not chronological), grouped by
counterparty when relevant. Cite each theme with the supporting docs.

## Operational notes

- The `brain` CLI works from any cwd. No need to `cd` into the second-brain repo.
- `brain doctor` confirms the DB is healthy if a query fails — see `brain-maintenance` for the recovery path.
- If `brain search` returns zero results, try broadening (drop a filter, drop a tag), then if still zero, tell the user rather than make something up.
- The brain is updated by `brain ingest-dir`, `brain ingest-gmail`, and Claude-driven Krisp/Slack ingestion via `ingest-brain`. If a recent event isn't in the brain yet, say so.
