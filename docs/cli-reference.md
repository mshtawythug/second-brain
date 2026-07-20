# CLI reference

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index. Core `ingest` / `search` / `show` / `list` / `tag` usage lives in the [README](../README.md#core-usage); this file covers the rest of the command surface.

## Gmail ingest

`brain ingest-gmail` uses the `gws` CLI and requires at least one scope flag so
you do not accidentally ingest your entire mailbox. It excludes Gmail drafts,
groups messages by `threadId`, stores each thread as one `email_thread`
document, and updates a stable row when a thread grows.

```bash
brain ingest-gmail --label interviews --since 2026/04/01 --tag interview
brain ingest-gmail --from alice@example.com --max 25
brain ingest-gmail --query 'from:recruiting@example.com newer_than:30d' --dry-run
brain ingest-gmail --until 2026/05/01 --tag archive
```

Dates passed to `--since` / `--until` are converted into Gmail `after:` /
`before:` query terms, so use Gmail's `YYYY/MM/DD` shape. Raw `--query` is
passed through and can use any Gmail search syntax.

## Status and health

```bash
brain status          # counts and last-ingest time
brain status --json   # machine-readable: {documents, chunks, sources, by_source, last_ingest}
brain doctor          # env, Postgres/pgvector, embedder, gws, npx, mirror drift, AGE + graph health
brain doctor --json   # machine-readable [{check, status, detail, remedy}]; still exits non-zero on FAIL

# Refresh Postgres planner statistics. Fixes the "chunks stats — never analyzed"
# warning doctor reports after a pg_restore (a restore bulk-loads rows via COPY
# but never runs ANALYZE, so the planner uses bad row estimates until autovacuum).
brain analyze         # ANALYZE the chunks table (the one doctor warns about)
brain analyze --all   # ANALYZE every table in the database
```

`brain doctor` exits non-zero only for required failures: config, database, or
active embedder. Optional integrations (`gws`, `npx`, the AGE extension, the
concept-extractor model, community materialization staleness) are warnings.
`brain status --json` and `brain doctor --json` are the scripting-friendly
paths; `doctor --json` preserves the same non-zero exit on any FAIL check.

## brain capture

```bash
# Jot a thought straight into the brain — always tagged `inbox`.
brain capture --text "Follow up with the platform team about the migration cutover"
echo "Idea: batch the nightly re-embed to cut Ollama warmups" | brain capture --title "re-embed idea" -t ideas

# Review the inbox later: promote, tag, or discard each item; or just list it.
brain capture list
brain capture review
```

`brain capture` is a zero-friction inbox: pipe text on stdin or pass `--text`,
optionally adding `-t/--tag` alongside the always-on `inbox` tag. `brain capture
list` shows what's waiting; `brain capture review` walks each item so you can
promote it into a real note, retag, or discard it.

## brain enrich

```bash
# Backfill LLM auto-summaries for documents that don't have one yet (idempotent).
brain enrich --backfill
brain enrich --backfill --limit 50
brain enrich --backfill --remodel        # also re-summarize rows whose summary_model is stale

# Print the Krisp MCP request Claude should run to pull action items (the CLI
# never calls MCP itself — Claude pipes results back via ingest-stdin).
brain enrich --krisp-action-items --since 7
```

`brain enrich` has two mutually-exclusive modes. `--backfill` runs the Ollama
enricher over rows where `summary IS NULL` (add `--remodel` to also refresh
summaries written by an older `BRAIN_ENRICH_MODEL`; honors `--limit/-n`).
`--krisp-action-items` prints the MCP + `ingest-stdin` commands for Claude to
execute, then exits without contacting MCP itself.

## brain rate

```bash
# Thumbs up / down a document (verdict is `useful` or `irrelevant`).
brain rate 3f9a2b useful
brain rate 3f9a2b irrelevant

# Rate a graph target instead of a document, and/or mark it as graph-surfaced.
brain rate <entity-uuid> useful --target-type entity
brain rate <community-key> useful --graph-retrieved
```

`brain rate` records relevance feedback into the `interactions` table. Ratings
append — re-rating a target creates a fresh row and the full history is
preserved. Pass `--target-type entity|community|theme` to rate a GraphRAG
target by its durable id instead of a document; `--graph-retrieved` marks the
rating as produced by a graph surface.

## brain people

```bash
brain people                 # alphabetised People Hub roster (everyone above the doc threshold)
brain people "Jane"          # one person's full document list (case-insensitive substring)
brain people --json          # machine-readable aggregation
```

`brain people` is a read-only view of the same People Hub aggregation that
renders the `<vault>/people/` pages and drives the derived-link participant
filter. The visibility threshold (`BRAIN_PEOPLE_HUB_MIN_DOCS`, default 3) and
owner filter (`BRAIN_OWNER_PARTICIPANTS`) flow through the normal config, so
flipping either env var changes the roster on the next invocation.

## Tacit-knowledge elicitation

### How it works

`brain elicit` is the part that **asks**. The brain already indexes what you
have written and said; this feature mines that corpus for knowledge that is
*implied everywhere but written nowhere* — entities referenced constantly in
transcripts, decisions never captured as a note, positions that shifted across
time. It drafts a confident guess at the unwritten rule, opens your `$EDITOR`
so you can correct it, and codifies the result as a vault note (tagged `tacit`
+ the signal kind) with a `## Source` section wikilinking the evidence docs.
Codified gaps are marked resolved and never resurface.

Four gap signals feed the queue:

| Signal | What it detects |
|---|---|
| `delta` | Entities heavily referenced in ingested docs (transcripts, Slack, mail) but never authored into a vault note. The cleanest tacit-knowledge proxy. |
| `orphan` | Graph entities with high mention-count but no written description. The graph knows *who* matters; this surfaces *why* it doesn't. |
| `contradiction` | Opposing positions across docs — flag-gated; requires Ollama + non-null summaries (`BRAIN_ELICIT_CONTRADICTION_ENABLED=true`). |
| `user_flagged` | A topic you name with `--target`; always enters the queue regardless of corpus signals. |

The interactive loop (`brain elicit`) requires **Ollama** (used to draft the
rule text). Viewing the gap queue (`brain elicit list`) is read-only and
Ollama-free (unless `--signal contradiction` is enabled).

The `list` table shows each gap's **entity name** (e.g. `"Acme Corp"`) and a
**rationale** column explaining why it was surfaced. JSON output includes the
same data as `target_name` and `rationale` fields.

### Commands

```bash
# Show the ranked gap queue — refresh detectors, list open gaps.
brain elicit list
brain elicit list --json
brain elicit list --limit 10             # show at most 10 gaps
brain elicit list --low-confidence       # include gaps below the score floor
brain elicit list --type person          # only person-type gaps
brain elicit list --type project --type org   # multiple types (repeatable)

# Run the interactive draft-then-correct loop (needs Ollama).
brain elicit
brain elicit --target "engineering culture"   # user-flagged: jump straight to this topic
brain elicit --signal delta                   # only surface delta gaps this run
brain elicit --signal orphan
brain elicit --signal contradiction           # needs BRAIN_ELICIT_CONTRADICTION_ENABLED=true
brain elicit --include-low-confidence         # include below-score-floor gaps in the loop
brain elicit --type project --type tool       # filter to specific entity types (repeatable)
```

Interactive keymap (one gap at a time):

| Key | Action |
|---|---|
| `e` | Open draft in `$EDITOR`. Save a non-empty, changed body to codify as a vault note. |
| `s` | Skip (dismiss) this gap. |
| `n` | Snooze — prompt for a number of days, suppress until then. |
| `q` | Quit the loop. |

The editor buffer opens with the drafted rule text and a comment block listing
the evidence doc IDs. The body must be **non-empty and changed from the draft**;
re-saving the draft unchanged re-prompts rather than codifying.

### Config knobs

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_ELICIT_MIN_EVIDENCE_DOCS` | `3` | Minimum distinct source docs required before a gap is draftable. |
| `BRAIN_ELICIT_MIN_GAP_SCORE` | `0.3` | Post-normalization score floor (0–1). Below-floor gaps appear in `list` but are skipped in the loop unless `--include-low-confidence`. |
| `BRAIN_ELICIT_QUEUE_LIMIT` | `20` | Max gaps surfaced per `list` run (`-n 0` uses this limit). |
| `BRAIN_ELICIT_CONTRADICTION_ENABLED` | `false` | Enable the contradiction detector (requires Ollama + non-null summaries). |
| `BRAIN_ELICIT_CONTRADICTION_MIN_DOCS` | `5` | Min entity doc-count before contradiction scan runs on that entity. |

## Proactivity and synthesis

Hybrid search and GraphRAG are reactive — you ask, the brain answers. This set
of commands is the proactive half: it surfaces what's due for review, digests
what just landed, synthesizes across time, flags contradictions, suggests
links, and answers multi-hop questions with citations. All eight are read-side
features over the corpus you've already ingested; none change ingest or search
ranking. The LLM-backed ones (`brief` suggestions, `ask`, `audio`, `timeline
--synthesize`, conflict `scan`) use the local Ollama and degrade gracefully
when it's unavailable.

Their tuning env vars are collected under
[Feature config knobs](configuration.md#feature-config-knobs) in the
configuration docs.

### brain resurface

Spaced-repetition resurfacing — the brain picks older notes you haven't
revisited recently and ranks them for review. Every non-draft, non-action-item
document is scored fresh on each run from its **age**, **last-access
staleness** (via the interactions log), and **importance** (tags + summary
presence), so a doc you opened yesterday drops out automatically.

```bash
brain resurface                      # top 7 docs due for review
brain resurface -n 15                # surface more
brain resurface --min-age-days 30    # only docs older than 30 days
brain resurface --source krisp       # filter by source kind
brain resurface --json
```

### brain brief

A proactive daily digest of recent captures, open todos, pins, and best-effort
LLM next-step suggestions. Surfaces **titles and todo texts only — never
document bodies**. On macOS the installer wires a launchd agent
(`com.brain.brief`) that runs `brain brief --wiki` once at **07:00 local** each
day and writes the digest to `<vault>/daily/<YYYY>/<date>-brief.md`.

```bash
brain brief                          # today's digest to the terminal
brain brief --since 48 --todo-since 14   # widen the capture / todo windows
brain brief --wiki                   # also write the dated vault page
brain brief --no-enrich              # skip the LLM next-step suggestions
brain brief --date 2026-06-09 --json
```

### brain review

Periodic synthesis over the corpus — a weekly review page plus a
contradiction/staleness scan queue. `weekly` assembles themes (graph
communities, or tag clusters with `--no-graph`), activity, open loops, new
captures, and key people for an ISO week and writes
`<vault>/reviews/<week>.md`. `scan` surfaces findings into the same review
queue that `list` reads and `dismiss` clears.

```bash
# Weekly synthesis page.
brain review weekly                          # current ISO week → vault page
brain review weekly --week 2026-W23          # a specific week
brain review weekly --no-graph               # tag-cluster themes (no AGE)
brain review weekly --no-emit --json         # stdout only

# Contradiction + staleness scan.
brain review scan                            # run both scans
brain review scan --conflicts --dry-run      # conflicts only, no writes
brain review scan --stale                    # staleness only
brain review list --kind conflicts -n 10     # read the queue
brain review dismiss <id-prefix>             # dismiss one finding
```

The **contradiction** leg is gated on `BRAIN_ELICIT_CONTRADICTION_ENABLED`
(default off) and needs Ollama; the **staleness** leg needs neither. Findings
land in `elicitation_gaps` alongside the `brain elicit` signals (migration 018
widens the `signal_kind` CHECK to include `stale` + `search_failure`).

### brain timeline

How a theme or entity evolved over **time**. Buckets the documents that mention
a query by document date (`COALESCE(sent_at, ingested_at)`) into month /
quarter / year periods, showing per-period doc counts, co-topics, and
representative titles. Requires the graph layer (`BRAIN_GRAPH_ENABLED` +
`brain graphrag build`); the query is ILIKE-resolved to graph entities and an
unknown entity prints a friendly note and exits 0.

```bash
brain timeline "platform migration"                    # default quarter buckets
brain timeline "hiring" --granularity year
brain timeline "Project Phoenix" --person "Jane Doe"   # scope to a participant
brain timeline "pricing" --since 2026-01 --until 2026-06
brain timeline "pricing" --synthesize                  # best-effort Ollama narrative
brain timeline "pricing" --json
```

### brain connect

Proactive auto-link suggestions — surfaces note pairs that share entities or
semantics but aren't linked yet, then lets you accept (optionally writing the
wikilink) or reject each one. `refresh` blends an entity-graph affinity leg
with an embedding affinity leg via RRF, drops already-linked and below-threshold
pairs, and upserts the top suggestions per source doc; accepted/rejected rows
are frozen and never re-proposed. The `connect refresh` stage also runs
non-fatally inside `brain-rebuild`.

```bash
brain connect refresh                  # recompute + upsert pending suggestions
brain connect refresh --doc <id> --dry-run
brain connect list                     # pending review queue
brain connect list --all --json        # every status
brain connect accept <suggestion-id> --write   # flip to accepted + append wikilink
brain connect reject <suggestion-id>           # freeze; never re-proposed
brain connect stats                    # pending / accepted / rejected counts
```

`accept --write` appends a path-form wikilink under a `## See Also` section at
the end of the source doc's vault file (idempotent — a repeated accept never
duplicates). The default minimum blended score is **0.60**
(`BRAIN_CONNECT_MIN_SCORE`, retuned up from 0.30 on live-corpus evidence).

### brain ask

Agentic multi-hop **cited** answer synthesis. Where `brain search` returns a
ranked list you read yourself, `brain ask` plans sub-queries, retrieves across
iterations, optionally reflects to fill coverage gaps, and composes a single
answer with inline `[N]` citations into your documents. Requires a local Ollama
for the plan/reflect/synthesize steps; if Ollama is down the command exits
non-zero with a clear message (no partial answer). Only document **snippets**
are sent to the LLM — never full bodies.

```bash
brain ask "what did I learn negotiating across my job searches?"
brain ask "what did we decide about the data pipeline?" --explain
brain ask "..." --no-loop              # single retrieve+synthesize pass (faster)
brain ask "..." --mode fuse            # RRF of graph + hybrid retrieval
brain ask "..." --mode auto            # graph router  | local | hybrid (default)
brain ask "..." -n 8 --max-iter 4      # -n is shorthand for --limit (docs retrieved per iteration)
brain ask "..." --json
```

Retrieval `--mode` is `hybrid` (vector/FTS only, default) `| auto | fuse |
local`; the three graph modes require the Apache AGE image. An answer-quality
eval harness ships alongside it: `brain eval --answer` loads
`tests/eval/answer_corpus.yaml`, runs `brain ask --no-loop` per case, and
reports citation-grounding metrics (live-model gated, no baselines).

### brain audio

A NotebookLM-style **two-host audio overview** of a theme or a person.
`--person` builds a themes overview (graph `themes` mode); `--topic` builds a
community-level overview (graph `global` mode — run
`brain graphrag communities build` first). Exactly one of the two is required.
The script is grounded **only** in entity names + document summaries (never raw
bodies). Writes `<out>.json` + `<out>.md`; with `--tts` it also synthesizes
audio via a pluggable backend (artifacts are written *before* synthesis, so
they survive a TTS failure). There is no MCP tool for `brain audio` — it's
CLI-only.

```bash
brain audio --person "Jane Doe"                  # themes overview script
brain audio --topic "platform migration"         # community-level (needs communities)
brain audio --person "Jane Doe" --turns 16
brain audio --topic "hiring" --tts 'shell:/path/to/tts.sh'   # synthesize audio
brain audio --person "Jane Doe" --json           # print script JSON, write no files
```

### brain gaps

Search-failure-driven knowledge-gap detection. Mines the `search_queries` log
for queries the brain failed to answer — `zero_results` (nothing matched) and
`no_click` (results returned but never opened) — over a lookback window, then
clusters them into knowledge gaps. The read view shows the **normalized**
canonical query label (raw query strings stay server-side); `push` upserts the
resulting `search_failure` gaps into the `brain elicit` queue.

```bash
brain gaps                            # failed-query clusters over the window
brain gaps --since 60 -n 30
brain gaps --json
brain gaps push                       # upsert search_failure gaps into the queue
brain gaps push --dry-run
```

Search-failure logging is **best-effort** — it never blocks or breaks a search.
On a brain whose database predates migration 019 (the `search_queries` table),
`brain gaps` prints a clean warning to run `brain init`, and searches keep
working.
