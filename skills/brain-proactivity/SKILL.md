---
name: brain-proactivity
description: >
  The proactive half of the user's local second-brain — digest what just
  landed, surface older notes due for another look, synthesize a week,
  flag contradictions and stale notes, work the review queue, show how a
  theme moved over time, propose links between notes that should be
  connected, and mine failed searches. Use this skill when the user asks
  what they should look at, what happened recently, what they should
  revisit, what has gone stale, which notes should be linked, or what
  their brain keeps failing to answer. Backs `brain brief`,
  `brain resurface`, `brain review`, `brain timeline`, `brain connect`,
  and `brain gaps`. To answer a specific question use `consult-brain`;
  for one composed cited answer use `brain-ask`; for themes across a
  relationship use `brain-graph`; for open action items use
  `brain-todo`; to jot a thought into the inbox use `brain-capture`.
  MANDATORY TRIGGERS: what should I look at, catch me up, my daily
  brief, today's digest, what did I miss, what's new in my brain, what
  should I revisit, what should I review, resurface old notes, spaced
  repetition, weekly review, my week in review, review my week, what
  contradicts, contradictions in my notes, what's gone stale, stale
  notes, snooze this finding, timeline of, how did it change over time,
  evolution over time, suggest links, what should be linked, related
  notes I haven't linked, failed searches, what am I not finding,
  searches that came up empty.
---

# Brain Proactivity

Hybrid search and GraphRAG are **reactive** — the user asks, the brain
answers. This skill is the **proactive** half: six commands that surface
things the user did not think to ask for.

All of these run from any working directory and are read-only unless
noted. The commands that write (`brain review weekly` emits a vault
page, `brain connect accept --write` appends a wikilink, `review
snooze` / `resolve` / `dismiss` move queue state) are called out in
"Safety rules" below.

For answering a specific question, quotes, or voice writing, use
`consult-brain`; when the deliverable is one composed cited answer, use
`brain-ask`. For themes / patterns / connections across a relationship,
use `brain-graph`. For open action items, use `brain-todo`. For jotting
a thought into the inbox or triaging it later, use `brain-capture`. For
ingesting existing content (a file, an email, a transcript), use
`ingest-brain`; for authoring a deliberate titled note, use
`brain-authoring`. **Boundary with `brain-memory`:** that skill owns
what *you the agent* record and recall about the user across sessions —
this skill only reads and triages what the *user* already put in their
corpus.

## Which command answers which question

| The user asks… | Reach for |
|---|---|
| "What should I look at today?" / "Catch me up" | `brain brief` |
| "What am I forgetting?" / "Show me old notes worth rereading" | `brain resurface` |
| "How did my week go?" / "Synthesize last week" | `brain review weekly` |
| "What in my notes contradicts itself / has gone stale?" | `brain review scan` → `review list` |
| "Not this one, not now" / "I fixed that one" | `brain review snooze` / `resolve` |
| "How did X change over time?" / "Timeline of X" | `brain timeline "X"` |
| "Which of my notes should be linked but aren't?" | `brain connect` |
| "What does my brain keep failing to answer?" | `brain gaps` |

## Daily digest — `brain brief`

Recent captures, open todos, pins, and best-effort LLM next-step
suggestions. Surfaces **titles and todo texts only — never document
bodies**.

```bash
brain brief                              # today's digest to the terminal
brain brief --since 48 --todo-since 14   # widen both windows
brain brief --wiki                       # also write the dated vault page
brain brief --no-enrich                  # skip the LLM next-step suggestions
brain brief --date 2026-06-09 --json
```

| Flag | Purpose |
|---|---|
| `--since <str>` | Capture window. **A bare number is HOURS** (suffixes `7d` / `24h` / `90m`). |
| `--todo-since <str>` | Open-todo window. **A bare number is DAYS** — the units differ from `--since`; do not assume they match. |
| `--date YYYY-MM-DD` | ISO date for the header (default: today). |
| `--no-enrich` | Skip LLM next-step suggestions. |
| `--wiki` / `--no-wiki` | Write `<vault>/daily/<YYYY>/<date>-brief.md` (default `--no-wiki`). |
| `--json` | Machine-readable instead of the table. |

The suggestions leg is best-effort — it is skipped silently when Ollama
is down or `--no-enrich` is passed. A brief with no suggestions is not
an error.

## Spaced repetition — `brain resurface`

Scores every non-draft, non-action-item document from **age**,
**last-access staleness**, and **importance** (tags + summary), then
lists the highest scorers. Re-scored fresh on each run, so a doc the
user just opened drops out on its own.

```bash
brain resurface                      # top 7 due for review
brain resurface -n 15                # surface more
brain resurface --min-age-days 30    # only docs older than 30 days
brain resurface --source krisp       # krisp | gmail | manual | slack
brain resurface --json
```

Defaults: `--limit/-n 7`, `--min-age-days 14`.

## Weekly synthesis and the review queue — `brain review`

Six subcommands. `weekly` writes a synthesis page; `scan` fills a
findings queue that `list` reads and `snooze` / `resolve` / `dismiss`
work through.

```bash
# Weekly synthesis page → <vault>/reviews/<week>.md
brain review weekly                          # current ISO week
brain review weekly --week 2026-W23          # a specific week
brain review weekly --no-graph               # tag-cluster themes (no AGE)
brain review weekly --no-emit --json         # stdout only, writes nothing

# Contradiction + staleness scan, then read the queue
brain review scan                            # both scans
brain review scan --conflicts --dry-run      # conflicts only, no writes
brain review scan --stale
brain review list --kind conflicts -n 10     # kind: conflicts | stale | all
```

### Working the queue

Three verbs move a finding out of the open queue. Pick the one that
matches what actually happened — they are not interchangeable, and the
queue is only useful while they stay honest.

```bash
brain review snooze <id-prefix>              # hide for 7 days (default)
brain review snooze <id-prefix> --days 3     # hide for 3
brain review resolve <id-prefix>             # "I acted on this"
brain review dismiss <id-prefix>             # "this was never real"
```

| Verb | Means | Reversible? |
|---|---|---|
| `snooze` | "Not now." Drops out of `review list` and **returns on its own** once the deadline passes. Re-snoozing just moves the deadline. | Yes — it comes back by itself |
| `resolve` | "I fixed the contradiction / updated the stale note." A later scan may legitimately re-surface the same target if it goes stale again. | Idempotent; re-resolving is a no-op |
| `dismiss` | "This finding was noise." Never re-adjudicated. | **No, not from the CLI** |

All three take the short id prefix that `review list` prints. An unknown
or ambiguous prefix exits 1 with a clear message — read it rather than
retrying with a longer guess.

Notes that matter:

- `--json` on `weekly` **implies `--no-emit`** — asking for JSON never
  writes the vault page.
- `scan` with neither `--conflicts` nor `--stale` runs **both**.
- `scan --json` is newline-delimited JSON, one finding per line — not a
  JSON array. `review list --json` *is* an array.
- Findings land in `elicitation_gaps`, shared with `brain elicit`. For
  eliciting unwritten knowledge rather than reviewing existing notes,
  hand off to `elicit-brain`.

## Evolution over time — `brain timeline`

Buckets the documents mentioning an entity by document date
(`COALESCE(sent_at, ingested_at)`) and reports per-period doc counts,
co-topics, and representative titles.

```bash
brain timeline "platform migration"
brain timeline "hiring" --granularity year
brain timeline "Project Phoenix" --person "Jane Doe"
brain timeline "pricing" --since 2026-01 --until 2026-06
brain timeline "pricing" --synthesize            # best-effort Ollama narrative
brain timeline "pricing" --json
```

| Flag | Purpose |
|---|---|
| `--person "<name>"` | Scope to docs where this person co-appears as a participant. |
| `--granularity` | `auto` (default) \| `month` \| `quarter` \| `year`. `auto` picks the coarsest width yielding ≥3 buckets. |
| `--since` / `--until` | ISO **month** cutoffs (`YYYY-MM`), both inclusive. |
| `--limit/-n N` | Max buckets. |
| `--synthesize` | Opt-in Ollama narrative on the densest buckets; never required. |
| `--tenant T` | Default `BRAIN_GRAPH_TENANT`. |
| `--json` | Machine-readable. |

Requires the graph layer (`BRAIN_GRAPH_ENABLED` + `brain graphrag
build`). An unknown entity prints a friendly note and **exits 0** — that
is not a failure, so do not retry it with variations. Report "nothing in
the graph under that name" and offer `brain search` instead.

## Auto-link suggestions — `brain connect`

Surfaces note pairs that share entities or semantics but are not linked
yet. `refresh` blends an entity-graph affinity leg with an embedding leg
via RRF, drops already-linked and below-threshold pairs, and upserts the
top suggestions per source doc. Accepted and rejected rows are frozen
and never re-proposed.

```bash
brain connect                          # bare = the pending queue
brain connect list --all --json        # every status
brain connect refresh                  # recompute + upsert pending
brain connect refresh --doc <id-prefix> --dry-run
brain connect accept <suggestion-id> --write   # accept + append the wikilink
brain connect accept <suggestion-id>           # accept, write nothing
brain connect reject <suggestion-id>
brain connect stats                    # pending / accepted / rejected counts
```

Suggestion ids are prefixes of ≥6 chars, same convention as document
ids. `accept --write` appends a path-form wikilink under a `## See Also`
section at the end of the source doc's vault file; the write is
idempotent, so a repeated accept never duplicates the link.

## Search-failure gaps — `brain gaps`

Mines the `search_queries` log for queries the brain failed to answer —
`zero_results` (nothing matched) and `no_click` (results returned, never
opened) — then clusters them.

```bash
brain gaps                            # failed-query clusters over the window
brain gaps --since 60 -n 30           # bare number is DAYS (0 = config default)
brain gaps --json
brain gaps push                       # upsert search_failure gaps into the queue
brain gaps push --dry-run
```

This answers "what does my brain keep failing to answer" — a retrieval
observation. For "what do I know but have never written down", that is
tacit-knowledge elicitation: use `elicit-brain` instead. `gaps push`
feeds the same `elicitation_gaps` queue, which is where the two meet.

On a database predating migration 019 (`search_queries`), `brain gaps`
prints a clean warning to run `brain init` and searches keep working.

## Prerequisites and graceful degradation

| Command | Needs | Without it |
|---|---|---|
| `brain brief` suggestions | Ollama | Digest still prints; suggestions silently skipped. |
| `brain resurface` | Nothing beyond Postgres | — |
| `brain review weekly` graph themes | AGE + built graph | Pass `--no-graph` for tag-cluster themes. |
| `brain review scan --conflicts` | `BRAIN_ELICIT_CONTRADICTION_ENABLED` (**default off**) + Ollama | Conflict leg no-ops; `--stale` needs neither. |
| `brain timeline` | `BRAIN_GRAPH_ENABLED` + `brain graphrag build` | Unknown entity → friendly note, exit 0. |
| `brain connect refresh` | Embeddings; graph leg optional | Score leans on the embedding leg. |
| `brain gaps` | Migration 019 (`search_queries`) | Warning to run `brain init`; search unaffected. |
| `brain review snooze` / `resolve` | Nothing beyond Postgres | — |

If a command errors outright rather than degrading, route to
`brain-maintenance` (`brain doctor` first) instead of retrying.

## Safety rules

- **`brain connect accept --write` mutates a vault file.** Confirm with
  the user before passing `--write`; `accept` without it only flips the
  status.
- **`brain review dismiss` is not undoable from the CLI.** Confirm the
  finding is really noise first. When the user means "not right now",
  `snooze` is the correct verb — it is self-reversing. When they mean
  "I dealt with it", use `resolve`. Reaching for `dismiss` because it is
  the one you remember silently destroys the queue's signal.
- **Never clear the review queue on the user's behalf.** Snoozing or
  resolving findings in bulk to make a list look tidy is a data-quality
  loss, not housekeeping. Adjudicate one at a time, with the user.
- **`brain review weekly` writes `<vault>/reviews/<week>.md` by
  default.** Use `--no-emit` (or `--json`, which implies it) when the
  user only wants to read the synthesis.
- **`brain brief` prints titles and todo texts, never bodies.** Never
  paste a document body the command did not print — fetch it explicitly
  with `brain show` via `consult-brain` if the user asks for it.
- **Exit 0 with an empty result is a valid answer.** An empty brief,
  zero resurfaced docs, or an unresolved timeline entity means "nothing
  to surface" — say so rather than broadening until something appears.
