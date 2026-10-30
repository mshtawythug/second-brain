# CLI reference

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index. The [README](../README.md#core-usage) shows the everyday `ingest` / `search` / `show` / `list` / `tag` commands; this file covers everything else — first-run `setup` and the offline `demo`, the advanced flags and diagnostics for those core commands, plus the full command surface beyond them.

## brain setup

`brain setup` is the one-command installer for the runtime: it creates
`$BRAIN_HOME`, installs the `brain` shims, starts the Postgres container via
Docker, and optionally installs the Claude Code skill. How much it stands up
is chosen with `--profile` — `minimal` (Postgres + FTS only,
`BRAIN_EMBEDDER=none`), `standard` (default; adds Ollama hybrid search), or
`full` (adds GraphRAG and opt-in daemons). The
[README profiles table](../README.md#quick-start) is the canonical description
of what each profile pulls in.

```bash
brain setup                              # standard profile, interactive prompts
brain setup --profile minimal            # Postgres + FTS only (no Ollama, no models)
brain setup --profile full --daemons     # + graph + launchd background daemons
brain setup --non-interactive            # take the default for every prompt
brain setup --dry-run                    # print planned actions; touch nothing
brain setup --embedder none              # non-interactive backend: arctic|voyage|qwen3|none
brain setup --reset                      # destructive wipe of $BRAIN_HOME (typed confirmation)
```

`--daemons` installs the launchd background agents and applies only to the
`full` profile (default `--no-daemons`). `--embedder` picks the backend
non-interactively — `arctic` (default hybrid), `voyage` (SaaS), `qwen3`, or
`none` (FTS-only, the `minimal` default). `--non-interactive` uses defaults for
every prompt; `--dry-run` prints every planned action without touching the
filesystem. `--reset` requires a typed confirmation that cannot be bypassed even
with `--non-interactive`.

To stand up a second, isolated stack (QA, a throwaway `$BRAIN_HOME`) alongside
the real one, export `BRAIN_COMPOSE_PROJECT=<name>` — every `docker compose`
call and the rendered `container_name` switch to that project so it never
collides with the default `brain` stack.

## brain demo

`brain demo` is a zero-Ollama taste test: it provisions a throwaway Postgres
sandbox (its own compose project `brain-demo`, container, and volume — never the
prod stack), seeds a 22-doc synthetic *Larkspur* compliance corpus with a
deterministic offline embedder, and runs a hero query inline — ranked results in
under two minutes with no personal data and no model downloads. It needs only
Docker, or an existing empty database via `--database-url`.

```bash
brain demo                               # provision + seed + run the hero query
brain demo --with-embeddings             # also build the vector leg + HNSW index (default: FTS-only)
brain demo --port 55444                  # host port for the sandbox (auto-bumps if busy; default 55433)
brain demo --database-url postgresql://…  # seed an existing empty DB instead of Docker
brain demo --json                        # emit the hero-query results as JSON

brain demo query "PCI scope creep"              # search the running sandbox
brain demo query "vendor risk" --source gmail   # filters: --source/--tag/--person/--after
brain demo status                        # is the sandbox up, and how many docs?
brain demo teardown                      # destroy the sandbox (docker compose down -v)
```

The default flow is FTS-only (no Ollama); pass `--with-embeddings` to exercise
the vector leg too. `query` accepts the same corpus filters as real search
(`--source`, `--tag`, `--person`, `--after`) plus `--json`. `--port` and
`--database-url` are shared by the sub-commands so they target the same sandbox.
`teardown` removes both the container and its volume, leaving no demo state
behind.

## Ingest options

```bash
# Single files: TXT, Markdown, PDF, DOCX.
brain ingest ~/Documents/resume.pdf --tag career --tag resume
brain ingest ./notes.md --force       # re-ingest even if the content already exists

# Directories: recursive, idempotent by content hash.
brain ingest-dir ~/Documents/career
brain ingest-dir ~/Documents/career --tag career --ext pdf,md,docx
brain ingest-dir ~/Documents/career --dry-run
brain ingest-dir ~/Documents/career --sync          # also list documents whose files are gone
brain ingest-dir ~/Documents/career --sync --apply  # ...and delete them

# Piped content: used for Krisp / Slack / arbitrary text from agents.
echo "<transcript>" | brain ingest-stdin \
  --source krisp \
  --external-id meeting-42 \
  --title "1:1 - 2026-04-24" \
  --content-type transcript \
  --date 2026-04-24 \
  --tag one-on-one \
  --metadata '{"participants":["Alice","Bob"],"duration_min":30}'
```

All ingest paths write rows + chunks to Postgres and, when `BRAIN_VAULT_PATH`
exists, mirror ingested documents into `_ingested/<source>/` so the vault holds the
whole corpus as Markdown. Re-running unchanged content is a no-op unless you pass `--force`.

**Re-syncing an imported directory (`--sync`).** Re-running `ingest-dir` picks up new and
changed files, but a file you deleted from the tree leaves its document behind. `--sync` adds a
prune pass after the walk: it finds the `manual` documents imported from files under `<dir>`
(compared by path components, so `notes` never matches `notes-old`) whose files are gone, and
lists them, `would delete: <id8>  <path>`, with a `N of M documents under <dir> would be removed`
summary. Nothing is deleted without `--apply`, which re-checks each file just before deleting
its document (row, chunks, graph entry and vault mirror, the same path as `brain rm`). The scope
is this run's extensions (`--ext`). A file that exists but cannot be checked (an unreadable
folder, an I/O error) is kept and counted, never deleted. Vault notes and stdin documents are
never candidates, and documents under a brain vault nested inside `<dir>` are skipped.

A missing file on a missing volume is not a deleted file. For each gone file, `--sync` looks at
its nearest folder that still exists, at or below `<dir>`, and keeps the document (counted as
"could not check") when that folder, or one between it and `<dir>`, is a mount point; when it is
on another disk than `<dir>`; when it is an empty folder below `<dir>` (a dropped network mount
leaves an empty mount point); or when it holds a mount point (`/Volumes` with one drive
unplugged). A folder you deleted outright still prunes; a folder you emptied but kept does not.
To prune files on a nested mount, run `--sync` on the mount itself. `--apply` also skips, as
`kept (row changed)`, a document whose row changed since the listing.

`--sync` refuses, exit 1, before anything is written:

- when `<dir>` does not exist or is not a directory;
- when `<dir>` is the brain vault or inside it (the vault watcher owns that tree);
- when the walk finds no files while documents were imported from `<dir>` (an unmounted drive);
- with `--apply`, when more than half of the documents under `<dir>` and more than 5 would go
  (a partly unmounted tree or a moved folder).

`--allow-empty` overrides the last two. `--apply` or `--allow-empty` without `--sync`, and
`--apply` with `--dry-run`, are usage errors (exit 2). A delete that fails is reported by error
class and the run exits 1.

**Scanned PDFs.** A PDF with no real text layer (a scan, a photographed receipt)
is read with the local `tesseract` binary when it is installed (`brew install
tesseract`): up to 50 pages at 200 dpi, stored as `[page N]` blocks. Without
`tesseract` it is reported as `skipped (empty document)` with a hint line. Knobs
and limits: [configuration](configuration.md#brain_ocr--ocr-for-image-only-pdfs).

## Search diagnostics and filters

```bash
brain search "exact product name" --fts-only   # skip the embedding call

# Rank diagnostics: why did these results surface, and in what order?
brain explain "platform migration tradeoffs"
brain explain "platform migration tradeoffs" --verbose --json
```

ID prefixes must be at least 6 hex characters and must uniquely identify one
document. `--json` is the machine-readable path for agents or scripts.

**When the embedder is unreachable, search falls back to text-only matching.**
If the query embedding cannot be computed (Ollama not running, a timeout, a
Voyage error, a model that was never pulled), `brain search` and `brain explain`
still answer from full-text matching alone and print one line on stderr saying
so; the exit code stays 0. The line names the failure by class only and tells a
transient outage ("comes back when the embedder does") from a configuration
problem a retry will not fix ("run 'brain doctor'"), and a query longer than the
embedder's context window (`BRAIN_OLLAMA_EMBED_NUM_CTX`) from both ("shorten the
query"). The bare `--json` list keeps
its shape; `--json --meta` adds `"fallback": "fts_only"`, `fallback_reason`,
`fallback_transient` and `fallback_note` (all `null` on a normal search), and
`brain explain` shows `matched_filters.fts_only: true`. MCP `brain_search`
carries the same four keys. `brain eval` never falls back: a failed embed there
is still a degraded row or an aborted run, so a baseline is never scored on a
text-only ranking.

**One search pipeline.** `brain search`, `brain explain`, MCP `brain_search`,
`brain ui`'s search, `brain recall` and the retrieval of `brain ask` all search
through `brain.search_pipeline.run_search`: the search, the text-only fallback,
then any configured ranking stages, in that order. The one stage so far is the
cross-encoder reranker, which is **off** unless `BRAIN_RERANKER` names a backend
(see below), so by default results are unchanged. `--json --meta` and MCP
`brain_search` carry a fifth always-present key after the four `fallback*` keys,
`stages`: one `{name, applied, skipped_reason, ms}` object per stage, `[]` when
none is configured. A stage that fails is reported with `applied: false` and the
failure's class in `skipped_reason`, and the results are the ones it was given.
The bare `--json` list and `brain explain --json` are unchanged.

**The reranker (off by default).** With `BRAIN_RERANKER=llamacpp` (a local
`llama-server`) or `voyage` (hosted), every surface above reorders the first
`BRAIN_RERANK_DEPTH` results (default 20) by a cross-encoder's score; the rest
keep their order after them. Knobs and the local runtime:
[configuration](configuration.md#brain_reranker--the-cross-encoder-reranker-off-by-default).

```bash
brain search "platform migration" --no-rerank   # hybrid order for this run
brain search "platform migration" --rerank      # needs BRAIN_RERANKER set; else exit 2
```

- `score` stays the hybrid (RRF plus recency) score, so a reranked list can be
  out of order by `score`. `brain explain --json` shows the reranker's own score
  as `explain.reranker_score` (`null` for rows it did not score).
- The footer gains a `rerank …ms` segment, and `brain ui`'s meta line a `rerank`
  one; both totals include it. (`--json --meta`'s `timing_ms.total` is the
  search's own time; the stage's is in `stages[].ms`.)
- If the reranker times out or is not running, the results are in hybrid order,
  the exit code is 0, and one line says so on stderr: `reranker unavailable;
  results are in hybrid order`. `--json --meta` and MCP report the stage with
  `applied: false` and the failure's class.
- MCP and `brain ui` follow the setting; they have no per-call switch.

`brain explain` runs the same query as `brain search` but prints per-result
ranking diagnostics — FTS rank, vector cosine, RRF contributions, and the
recency boost — so you can see *why* a result surfaced and in what order. Add
`--verbose` to show which filters were active, or `--json` for the full
`SearchExplanation` payload. It accepts the same filter flags as `search`
(`--source`, `--tag`, `--since`, `--person`, `--after`/`--before`, `--kind`, …).
Its MCP twin is `brain_explain` (`query`, `limit`, `source`, `tag`, `since_days`,
`include_confidential`): per result, the same `explain` object `--json` prints,
without the snippet. Unlike the CLI, it leaves confidential documents out of the
match set unless `include_confidential` is true. `brain ui`'s related-notes
panel has an MCP twin too, `brain_related` (`id`, `limit` up to 20,
`include_confidential`), which also refuses a confidential document's own
related list without that opt-in.

`--person` resolves the name or email you pass against the directory and
matches docs recorded under **any** known variant of that person — space-form
(`jane doe`), dot-form (`jane.doe`), bare email, and every `Name <email>`
combination the sources emitted — so Gmail-header and Krisp-label spellings of
the same person all count as one identity.

`--entity "<name>"` (`brain search` only) keeps documents that mention that
graph entity. The name is matched against the entity's canonical key, and only
when no key matches, against its display name, case-insensitively. A curated
alias rule (`BRAIN_GRAPH_ALIASES_PATH`) adds its target entity and keeps the
named one: rules are merged only by `brain graphrag build` / `refresh`, so an
unmerged name can still have documents of its own. The FTS leg, the vector leg, the total and `--facets` all
see the filter. A name that matches entities of several types exits 2 and asks
for `--entity-type org|person|project|tool|topic`. A name that matches nothing
exits 2 and points at `brain graphrag entities`. It needs the graph: with
`BRAIN_GRAPH_ENABLED` off, or no graph tables, it exits 2 with "`--entity` needs
the graph: set BRAIN_GRAPH_ENABLED=true and run `brain graphrag build`". MCP
`brain_search` takes `entity` and `entity_type` and raises `INVALID_PARAMS` in
the same cases.

`--since` takes a bare number or a duration suffix — `7d` (days), `24h`
(hours), `90m` (minutes) — on the commands that share this parser: `search`,
`explain`, `todo`, `enrich`, and `gaps` (bare number = **days**), plus `brain
brief` (`--since` bare = **hours**; its `--todo-since` bare = days). Bare
numbers keep each command's existing unit, so old scripts are unaffected. The
remaining `--since` flags are unchanged and do **not** take suffixes:
`ingest-gmail` expects a Gmail `YYYY/MM/DD` date, `timeline` an ISO `YYYY-MM`
month, and `gaps push` a plain integer day count.

### Search transparency and facets

```bash
brain search "platform migration"                 # footer on stderr by default
brain search "platform migration" --no-meta       # suppress the footer
brain search "platform migration" --facets        # add a facet panel
brain search "platform migration" --json          # bare list — UNCHANGED shape
brain search "platform migration" --json --meta   # envelope with counts + timings
brain search "platform migration" --json --brief  # cheaper snippets + snippet_source
```

The footer (`544 matched · 3 shown · embed 5820ms · sql 214ms`) goes to
**stderr**, so `--json` stays pipeable and `> file` stays clean. `--meta` moves
the same numbers into a JSON envelope; **without `--brief`, `--json` on its own
is still a bare list of 7-key objects and always will be**, because the list is
consumed positionally by skills and shell scripts and there is no deprecation
channel for a personal tool. `--facets --json` implies `--meta`, since facets
have nowhere else to live.

`--brief` is the fourth member of this matrix and the only one that changes the
**result** shape:

- **JSON-only.** It is a documented **no-op** without `--json` — the human
  table path ignores it entirely.
- It composes with `--meta` / `--facets`: brief applies to the envelope's
  `results` list as well as to the bare list, so the two never describe the
  same search two different ways.
- Each result's `snippet` carries the **cheaper of** the document's ingest-time
  `summary` and its query-conditioned chunk snippet, priced in `cl100k_base`
  tokens (not characters). The seven key *names* are unchanged; one additive
  key, `snippet_source` ∈ `{"chunk", "summary"}`, names which artifact won —
  so brief output is 8 keys, not 7.
- Documents with no summary (a 7.4% tail on the live corpus) fall back to the
  chunk snippet with `snippet_source="chunk"`. Brief mode never omits a result.

**The honest tradeoff:** the chunk snippet is query-conditioned and tells you
*why* this document matched; the summary is a query-independent whole-document
gist. A result that comes back with `snippet_source="summary"` therefore no
longer answers "why did this match?". `snippet_source` makes that visible per
result, and the fix is to re-call the same query **without** `--brief` to get
the query-conditioned snippet back.

`total_documents` is **lexical-only** by construction: the vector leg may
surface near-neighbours it does not count. A vector-inclusive total would be
capped at the candidate limit and therefore meaningless as a "total".

**`results_tokens` — the canonical serialized cost of this call's results.**
The `--meta` envelope carries one more key, `results_tokens`: the exact
`cl100k_base` count of the *canonical* serialization
(`json.dumps(…, ensure_ascii=False)`) of the `results` array this call
produced. Not an estimate and not
`chars / 4` — the same counter the chunker and the recall budgeter spend, and
the same number persisted to `search_queries.payload_tokens`, so `brain usage`
and an agent reading its own budget can never disagree about one call. It
counts the **array only**, not the envelope around it: the envelope carries the
number, so counting the envelope would change what it is counting.

**It is the canonical form, not the rendered one.** `brain search --json`
prints through Rich's `console.print_json`, which re-serializes with
`indent=2` — so what lands in your terminal runs roughly **10% larger** than
this number (re-derived on a 5-result payload: 876 canonical vs 957 emitted).
That is deliberate: counting the canonical form is what keeps a CLI row, an
MCP row and the Wave-0 harness (`scripts/token_payload_report.py`) all
comparable. Read `results_tokens` as "the cost of the payload", not "the byte
count of your terminal".

It is `null` when nothing was measured. That is the normal state of a *human*
search: the table path delivers a Rich preview, not a payload, so pricing the
JSON it never emitted would file a counterfactual under a measured name. Read
`null` as "not measured", never as zero.

Under `--brief` the call **also** records a `baseline_tokens` — what these same
results would have cost in the default projection. It is written only when a
cheaper mode was genuinely in effect; a default search had no alternative and
gets `NULL` rather than an invented saving. Both columns arrive with migration
028, so run `brain init` after upgrading; until you do, search keeps working
and warns once per call.

`--limit` here is **not** bounded by `BRAIN_SEARCH_MAX_LIMIT`. That ceiling
applies to the MCP `brain_search` tool only — see [MCP payload
ceilings](#mcp-payload-ceilings--and-why-they-do-not-apply-to-this-cli).

### Filtering by when a document changed

```bash
brain search "runway" --updated-after 2026-07-01
brain search "runway" --updated-before 2026-07-31
```

Distinct from `--after` / `--before`, which filter on when a document was
**authored or received** (`coalesce(sent_at, ingested_at)`). These filter on
when it was last **changed**. Maintenance jobs — enrichment backfills, tag
normalization, `vault_path` bookkeeping — deliberately do *not* bump
`updated_at`, so a `brain enrich --backfill` across the corpus will not make
every document look edited today.

## brain recall

Retrieval sized for an agent's context window. Where `brain search` ranks
pointers for a human to read, `recall` returns the **material itself**, packed
to an explicit token budget and cited.

```bash
brain recall "platform migration runway"
brain recall "platform migration runway" --budget 800
brain recall "platform migration runway" --json
brain recall "platform migration runway" --agent research-agent
```

| Flag | Default | Meaning |
|---|---|---|
| `--budget`, `-b` | `BRAIN_RECALL_BUDGET_TOKENS` (2000) | Token budget for the **whole** emitted block, header included. |
| `--max-candidates` | `BRAIN_RECALL_MAX_CANDIDATES` (25) | Documents considered before packing. |
| `--json` | off | Machine-readable projection instead of the block. |
| `--agent` | `BRAIN_AGENT_ID` | Attribute this recall to an agent id. |

It accepts the same metadata filters as `search` (`--source`, `--tag`,
`--since`, `--person`, `--after`/`--before`, `--kind`, `--thread`,
`--without-tag`, `--fts-only`).

Output is a `# recall:` header followed by `[N]`-cited passages, one per
document — the same citation convention `brain ask` uses, so an agent that
pastes a block and cites `[2]` is speaking a vocabulary the rest of the system
understands. One passage per document keeps source diversity high, which is
what a limited budget most wants.

If the budget cannot hold even the top passage, you get **one truncated
passage** rather than nothing: an agent asked for context, and silence is a
worse answer than a shortened excerpt.

If the embedding model is unavailable, `recall` falls back to text-only (FTS)
matching, as `brain search` does. The header ends `, text-only`, a one-line
notice goes to stderr (exit code 0), and `--json` carries `fallback`
(`"fts_only"` or `null`), `fallback_reason` (the failure's class, never its
message), `fallback_transient` and `fallback_note`. As with `brain search`, the
notice names the failure by class only and tells a transient outage ("comes back
when the embedder does") from a configuration problem a retry will not fix ("run
'brain doctor'"), and a query longer than the embedder's context window
(`BRAIN_OLLAMA_EMBED_NUM_CTX`) from both ("shorten the query").

## MCP payload ceilings — and why they do not apply to this CLI

Six ceilings bound how much a single response can cost an agent's context
window (`BRAIN_SEARCH_MAX_LIMIT`, `BRAIN_SHOW_MAX_CONTENT_TOKENS`,
`BRAIN_RECALL_MAX_BUDGET_TOKENS`, `BRAIN_GRAPH_ENTITIES_MAX_LIMIT`,
`BRAIN_MCP_ROWS_MAX_LIMIT`, `BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT` — all
documented in [configuration.md](configuration.md)).

**They are enforced on the MCP tool surface only. No `brain` CLI command is
capped by them, and that is deliberate**, not an oversight: the ceilings exist
because an agent cannot see a payload's size before it asks and pays for the
whole thing in context. You are at a terminal — you can pipe, page, and stop
reading. Capping the CLI would buy nothing and break scripts.

So the CLI's limit semantics are **unchanged**, and in three places they now
differ from the MCP twin:

| | CLI | MCP tool |
|---|---|---|
| `graphrag entities --limit 0` (and any `limit < 1`) | still means **all entities** | re-mapped to `BRAIN_GRAPH_ENTITIES_MAX_LIMIT` (500) |
| `graphrag communities` with no `--limit` | still means **all communities** | finite default: `BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT` (25) |
| `search --limit`, `recall --budget`, `show` body | unbounded | rejected above the ceiling with the ceiling named |

The MCP side never truncates silently. It either raises with the ceiling in the
message so the agent can re-ask smaller, or returns the cut alongside an
explicit marker (`truncated` + `limit_applied` on the graph envelopes,
`more_available` on the last element of the bare-list tools, `content_truncated`
+ `content_tokens` on `brain_show`). If you are comparing a CLI result against
an MCP one and the counts differ, this table is why.

## brain usage

```bash
brain usage --days 30
brain usage --json                  # normalized query labels
brain usage --json --raw-queries    # opt in to the raw strings
brain usage --limit 20
```

Searches, opens, feedback events and ingests over the trailing window, broken
down by day, by surface (`cli` / `mcp` / `ui`) and by agent, plus
search-latency p50/p95.

**`--json` withholds raw query strings by default.** A query log records what
you were *looking for*, including the searches that found nothing — often more
revealing than any document it returned. Counts are identical either way; only
the label changes. Human output is deliberately **not** redacted: the terminal
is inside the trust boundary, and "what do I search for most" is unactionable
if you are shown normalized labels.

A row with no `agent_id` reports as `(unattributed)`, never folded into a
surface. Every row written before migration 027 is genuinely unattributed, and
saying so is more useful than guessing.

### Token cost — measured and counterfactual, never blended

Since migration 028 the report also answers "what does retrieval cost me?", on
one line with **two clauses and two denominators**:

```
tokens served 148,203 (measured, 412 of 517 calls) · counterfactual savings 61,880 over 210 brief calls (−29.4%)
```

The first clause is a **measurement**: canonically-serialized payload tokens,
over the calls that really priced a payload. (Canonical, not rendered — see
`results_tokens` above; a `--json` terminal spend runs ~10% higher.) Calls
that delivered no payload —
every human terminal search — are excluded from both the sum and its
denominator, which is why `412 of 517`.

The second clause is a **counterfactual**, and the word is on the line
deliberately. It compares the calls that used a cheaper mode against what those
*same* calls would have cost in the default projection — a call that never
happened. It is computed over a strictly smaller row set (`210`, not `412`), so
the two clauses are never merged into one headline "we save N%": a percentage
over calls that never had an alternative is marketing, not measurement. If
nothing used a cheaper mode the clause is **absent**, and
`counterfactual_savings_tokens` is `null` rather than `0` — a brain with no
savings figure is not a brain with a savings figure of zero. A cheaper mode
that turned out *dearer* reports a signed increase (`+5.3%`) rather than being
laundered into a saving.

`--json` carries the parts, not a pre-blended rate: `payload_tokens_total`,
`measured_calls`, `baseline_tokens_total`, `counterfactual_payload_tokens`,
`counterfactual_calls` and the derived `counterfactual_savings_tokens`. Note
the two payload sums are deliberately different populations — the total spans
every measured call, the counterfactual one only the rows that also carry a
baseline, which is the only pair a difference may be taken over.

On a database missing migration 019/023/024/027/028 the command **fails** with a
`brain init` hint rather than reporting confident zeroes — the opposite of the
telemetry *write* path, which swallows so that search keeps working. A silently
incomplete report is worse than no report.

### Engagement readout — `--engagement`

```bash
brain usage --engagement              # counts over the last 30 days
brain usage --engagement --days 7
brain usage --engagement --json
```

Is there enough engagement data for an interactions-aware ranking stage to
learn from? The readout counts **document** interactions over `--days`:

- reads, split by surface (`cli` / `mcp` / `ui` / `wiki`) into opens
  (`opened`) and clicks (`clicked`)
- distinct documents opened
- `rated useful` and `rated irrelevant`
- how many reads came from a search session, and what share of all reads that
  is: the row's session id names a logged search. (`brain ask` logs its
  citations under the ask's own session, which is not a search.)
- the size of the whole `interactions` table: rows, and bytes as
  `pg_total_relation_size`.

A rating of a graph target (an entity, community or theme) is not counted,
because a document ranking stage cannot learn from it. It is still included in
the table size.

The last line is the **gate**: met when, over the last 90 days, there are at
least **500 reads (opens + clicks) across at least 100 distinct documents and
30 explicit ratings**. The gate is **always** judged over 90 days, whatever
`--days` says, so widening the window cannot meet it. Reads include the
`opened` row `brain ask` records for each citation in an answer; the
search-session share is how to tell those apart from reads after a search.
Each count is printed beside its threshold:

```
Gate (always the last 90 days): NOT MET — reads 40 of 500 · documents 40 of 100 · ratings 5 of 30
```

The gate's `reads` equals what the report above calls "opens" (`opened` +
`clicked`). The readout's `opens` and `clicks` split that total: `opens` counts
`opened` only (the JSON key too), `clicks` counts `clicked`. The output is
counts only. No title, query, document id or agent appears in either
form. `--json` adds a `gate` object (`met`, each count, each `min_*`).
`--raw-queries` is refused (exit 2), because the readout has no queries, and
`--limit` has no effect on it. Without `--engagement`, `brain usage` prints
exactly what it printed before the flag existed.

## Confidentiality

```bash
brain mark-confidential <id-prefix>
brain mark-normal <id-prefix>
brain list --sensitivity confidential
brain ingest notes.md --sensitivity confidential
```

`confidential` is an **egress** control, not an access control. The document
stays fully readable from the local CLI — that is inside the trust boundary.
What changes is what leaves the machine: the body is kept off a hosted
embedder, withheld from MCP `brain_show` / `brain_search` / `brain_recall` /
`brain_resurface` unless `include_confidential=true`, kept out of the pages
brain generates into the vault (People Hub, `brief --wiki`, the weekly review),
and withheld from `brain ui` on a LAN bind unless it is started with
`--include-confidential`.

Both commands are idempotent. **`mark-normal` is the only sanctioned
downgrade**: re-ingest is escalate-only, so it can raise a document's tier but
never lower it — otherwise a background `vault sync --watch` pass would quietly
reset anything you had marked. That also means marking a document confidential
writes the tier into its frontmatter for vault-tier notes, because for those
the file is the source of truth.

An unrecognized `--sensitivity` value is a **usage error (exit 2)**, never a
silent empty list. For a confidentiality filter, "nothing is marked
confidential" is the most dangerous possible wrong answer. The same applies to
`brain search --sensitivity` and `brain list --sensitivity`.

### `--sensitivity` on `search` is a lens, not a filter you can rely on

```bash
brain search "roadmap" --sensitivity confidential   # only confidential hits
brain search "roadmap"                              # BOTH tiers — the default
```

`brain search` accepts `--sensitivity`, but **there is no sensitivity filter on
the ranked results by default**: an unfiltered `brain search` still returns
confidential documents and their bodies. That is deliberate — the local CLI is
inside the trust boundary, and the tier governs *egress* (hosted embedders, MCP
responses, generated vault pages, a LAN-bound `brain ui`), not local reads.

The practical consequence: `--sensitivity normal` is a convenience lens for
narrowing what you are looking at, **not** a way to make a local session safe to
screen-share. If you need output that provably excludes confidential bodies, the
MCP surfaces are where that guarantee lives.

## Agent attribution

```bash
brain search "x" --agent research-agent
brain recall "x" --agent research-agent
brain rate <id> useful --agent research-agent
brain ingest-stdin --source slack --external-id t1 --title T --agent capture-bot
BRAIN_AGENT_ID=research-agent brain search "x"     # ambient, no flag
```

Precedence is **flag > `BRAIN_AGENT_ID` > unattributed**. `--agent` exists on
the four agent-facing surfaces above; `brain ingest` / `ingest-dir` have **no**
`--agent` flag, because attaching an explicit agent to a hand-run ingest would
be a fabricated fact. The ambient env var does still attribute them — see the
warning in [configuration.md](configuration.md#brain_agent_id-who-is-doing-the-work)
before exporting it in a shell profile.

On `ingest-stdin` the flag **wins** over an `agent_id` inside `--metadata`: an
explicit flag is the more specific and more recent statement of intent.

## Tags, edits, draft hiding, and deletes

```bash
# Tags accept +name and -name modifiers. Existing vault mirrors are rewritten
# so frontmatter stays aligned with the DB.
brain tag <id-prefix> +interview +career -old-tag
brain tag <id-prefix> +career --regenerate-file   # recover a missing ingested mirror

# Ingested-tier docs: targeted updates or JSON-header editor flow.
brain edit <id-prefix> --title "New title"
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}'   # shallow merge
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}' --replace-metadata
brain edit <id-prefix> --content-file ./fixed.md            # re-chunks + re-embeds
cat ./fixed.md | brain edit <id-prefix> --content-stdin
brain edit <id-prefix>                                      # opens $EDITOR

# Vault-tier docs: the file is source of truth. `brain edit <id>` opens the
# Markdown file directly; mutating flags are rejected for vault docs.
brain edit <vault-id-prefix>

# Hide a doc from brain ui's browseable views without removing it from local CLI search.
brain mark-draft <id-prefix>
brain mark-published <id-prefix>

# Delete a document and its chunks. If a vault mirror exists, it is unlinked too.
brain rm <id-prefix>
brain rm <id-prefix> --yes
```

## brain tags

Tag maintenance across the whole corpus: list tags, rename one, merge several,
and find the pairs that are probably one tag.

```bash
brain tags list [--json]                             # every tag and its count
brain tags rename old-tag new-tag                    # dry run: prints what would change
brain tags rename old-tag new-tag --apply            # write it
brain tags merge widgets widget-ui --into widget     # dry run
brain tags merge widgets widget-ui --into widget --apply
brain tags suggest-merges [--limit 50] [--json]      # ranked, with evidence
```

- **Dry run by default.** Without `--apply`, `rename` and `merge` print the
  mapping (`mapping: old → new`), each document they would retag, and each saved
  search whose filter they would rewrite, and write nothing. A merge into an
  existing tag cannot be undone without a mapping back, so read the plan first.
- **`--apply` writes every place a tag lives**: the `documents.tags` row, its
  chunks' `tags_text` (what tag text search reads), the frontmatter of the file
  at the document's `vault_path` (vault notes and `_ingested/` mirrors alike),
  and the `tag` / `without_tag` filters of saved searches (`brain watch`), so a
  watch keeps matching. A file missing on disk is warned about on stderr; the
  row is still updated.
- It runs per document, each one committed on its own, the same loop as
  `brain backfill normalize-tags`. A run cut short leaves some documents done;
  running it again finishes the rest, and a run with nothing left to do is a
  no-op. Only documents carrying a mapped tag are touched. `updated_at` is not
  bumped: tag maintenance is not new knowledge.
- **A file that cannot be rewritten** (unreadable, broken frontmatter, or a
  `vault_path` outside the vault) does not stop the run: it is reported on
  stderr by error class, with the tags it should have, the summary counts it
  (`N file(s) failed`), and the command exits 1 once everything else is done.
  Its database row is already updated, so **a re-run will not retry that
  file**: fix the file by hand. `brain backfill normalize-tags` behaves the
  same way.
- **`merge`** keeps one copy of the merged tag per document, in first-seen
  order. `--into` may be one of the sources (`merge a b --into a`).
- **`suggest-merges`** reports pairs whose spelling is close (`difflib` ratio
  ≥ 0.85), singular/plural pairs (`widget` / `widgets`, `policy` / `policies`),
  and pairs that sit on the same documents (Jaccard ≥ 0.8 over at least 2
  shared documents). Each line names the tag to keep (the more used one), the
  reasons, the counts, the ratio and the overlap, and the command to run. It
  writes nothing.
- The MCP twins, `brain_tags_list` and `brain_tags_suggest_merges`, are
  read-only and leave confidential documents out by default. There is no MCP
  rename or merge: a corpus-wide rewrite is done here.

## Gmail ingest

`brain ingest-gmail` uses the `gws` CLI and requires at least one scope flag so
you do not accidentally ingest your entire mailbox. It excludes Gmail drafts,
groups messages by `threadId`, stores each thread as one `email_thread`
document, and updates a stable row when a thread grows. It never ingests the
briefs `brain brief` emails to you (RM-16): a message is skipped when it is from
`BRAIN_USER_EMAIL` and its subject starts with `[brain brief]`, after any `Re:`,
`Fwd:` or `Fw:`, so your replies to and forwards of a brief are skipped too. A
message from anyone else with that subject, and your other mail, are ingested.
`--dry-run` marks a thread of nothing but your briefs as skipped. With
`BRAIN_USER_EMAIL` unset, nothing is skipped.

```bash
brain ingest-gmail --label interviews --since 2026/04/01 --tag interview
brain ingest-gmail --from alice@example.com --max 25
brain ingest-gmail --query 'from:recruiting@example.com newer_than:30d' --dry-run
brain ingest-gmail --until 2026/05/01 --tag archive
```

Dates passed to `--since` / `--until` are converted into Gmail `after:` /
`before:` query terms, so use Gmail's `YYYY/MM/DD` shape. Raw `--query` is
passed through and can use any Gmail search syntax.

## Calendar ingest

`brain ingest-calendar` (RM-30) stores Google Calendar events as documents
through the `gws` CLI and its own login. Both `--since` and `--until`
(`YYYY-MM-DD`; `--since` inclusive, `--until` exclusive) are required. The
window is at most 92 days, and `--until` is at most 14 days after today (UTC).
Otherwise the command exits 2 with "ingest-calendar requires --since and
--until (at most 92 days, ending at most 14 days ahead)".

```bash
brain ingest-calendar --since 2026-09-01 --until 2026-10-01 --dry-run
brain ingest-calendar --since 2026-10-01 --until 2026-10-31 --calendar primary --tag meetings
```

- **One document per event, and one per recurring series** (source `calendar`,
  content type `calendar_event`, deduplicated by the event id or the series id).
  A series takes its title, description and attendees from the series itself,
  so a re-run over a different window does not change its text.
- **What the body holds:** the title, the time (or, for a series, its
  recurrence rule and time of day), the location, the attendees' names, and
  the description, all with every link and address removed. Attendee addresses
  are stored only as participants, which is what `--person` matches. Google's
  joining block and any line carrying a dial-in number, a PIN, a passcode or a
  meeting ID are dropped. A title keeps the words before the secret: "Weekly
  sync, passcode is 7Hk2pQ" is stored as "Weekly sync", and a keyword with no
  value ("Password manager rollout") stays. The rules prefer privacy to
  recall: an ordinary description line that looks like a dial-in ("Discuss the
  access code rollout") is dropped too.
- **What metadata holds:** the start time, which dates the document (`sent_at`,
  used by date filters and the recency boost; for a series, its latest known
  occurrence), and for a series the latest ten start times known and how many
  occurrences the most recent run's window held. Each run is merged into what is
  stored, so backfilling an earlier window never moves a series backwards. When
  only these change, a re-run updates the metadata without re-chunking or
  re-embedding. If a series' own record cannot be read, a stored series keeps its
  text and only its dates are refreshed; the command prints how many.
- **Skipped:** events you declined, events with no title, cancelled events, and
  working-location and out-of-office entries. **Private** events (and Google's legacy
  `confidential` visibility) are stored as `confidential`; the rest as `normal`.
- **Future events.** The recency boost treats a future date as today, so an
  upcoming event ranks as the newest document. The 14-day limit on `--until`
  bounds that.
- **Cost.** No summary unless `--enrich` is passed, and when the graph is on,
  no concept extraction: `calendar_event` is in
  `graph_rag.extract_policy.CONCEPT_SKIP_CONTENT_TYPES` (the people aspect still
  runs). Attendee names and addresses are recorded in the people directory, so
  `brain search --source calendar --person "<name>"` finds the event.
- **Identical events.** An event whose stored text would be identical to
  another stored event's (the same meeting on two calendars, a duplicated
  invite) is reported by id as `same body as another event: <id>` and not
  stored; the other event's document, its occurrences, date and attendees
  included, is left untouched. A result with no document is reported as
  `empty <id>`. The summary line gains `, N same body as another event` and
  `, N empty` only when those counts are non-zero.
- `--dry-run` lists the titles and dates and writes nothing. There is no daemon
  run; schedule the command yourself if you want one.

## GitHub ingest

`brain ingest-github` (RM-31) stores your own pull requests, issues and
repository READMEs as documents, through the `gh` CLI and its own login
(`gh auth login`); brain stores no token. It requires `--repo OWNER/REPO`
(repeatable) or `--mine`, **and** `--since YYYY-MM-DD`. Otherwise it exits 2
with "ingest-github requires --repo or --mine, and --since".

```bash
brain ingest-github --mine --since 2026-10-01 --dry-run
brain ingest-github --mine --since 2026-10-01 --tag github
brain ingest-github --repo example-org/example-repo --since 2026-09-01 --with-thread
```

- **Scope.** Only your own items are read: PRs you authored, are assigned or
  reviewed, and issues you authored or are assigned, updated since `--since`.
  `--mine` reads them everywhere; `--repo` narrows them to the named
  repositories and never reads other contributors' items there.
  Organisation-wide pulls and commits are not read.
- **One document per PR or issue** (source `github`, content types `github_pr`
  and `github_issue`, deduplicated by `<owner>/<repo>#<number>`), and one per
  README (`github_readme`, `<owner>/<repo>:README`). READMEs are read for each
  `--repo` repository and, with `--mine`, for the result repositories you own;
  `--no-readmes` skips them.
- **What the body holds:** the title line, the description and **your own**
  comments (issue comments, review bodies and inline review comments).
  Other people's comments are added only with `--with-thread`; their logins are
  not stored in the body otherwise.
- **What metadata holds:** the state (`open`, `closed` or `merged`), the draft
  flag, the review and comment counts, the labels, and the update, close and
  merge times. When only these change, a re-run updates the metadata without
  re-chunking or re-embedding. An item whose update time has not moved is not
  read again. The creation time dates the document (`sent_at`).
- **Sensitivity.** Items and READMEs from a repository that is not public
  (private, or an enterprise `internal` one) are stored as `confidential`, and
  so is anything from a repository whose visibility could not be read.
- **Terminal safety.** Titles are stored and printed on one line with control
  and format characters removed, so a title cannot drive your terminal. `gh`'s
  output is read up to 32 MiB per call and `gh` is stopped past that; its
  error output is never shown.
- **Cap and cost.** At most 500 documents a run: PRs and issues first, newest
  first, then READMEs in the budget left. The command says when it stopped at
  the cap. Every new document gets a summary (`--no-enrich` skips it), and when
  the graph is on, concept extraction takes minutes per document in the
  watcher's graph lane, so a full 500-document run keeps the lane busy for days.
  Start with a short `--since`.
- **Identical items.** An item whose stored text would be identical to another
  stored document's is reported by id as `same body as another item: <id>` and
  not stored; the other document is left untouched. (Every item's text opens
  with its `<owner>/<repo>#<number>`, so this is a safeguard rather than a case
  you should meet.) A result with no document is reported as `empty <id>`. The
  summary line gains `, N same body as another item` and `, N empty` only when
  those counts are non-zero.
- `--dry-run` lists what would be read and writes nothing. Without `gh` on
  `PATH` the command exits 1 with an install hint, and `brain doctor`'s `github`
  row warns.

## Notion export ingest

`brain ingest-notion` (RM-35) stores the pages of a Notion "Export → Markdown & CSV" export,
given as the unzipped folder or the `.zip` itself. It calls no Notion API and needs no token:
what you exported is the scope.

```bash
brain ingest-notion ~/Downloads/Export-abc123.zip
brain ingest-notion ~/Downloads/Export-abc123 --with-databases --since 2026-09-01
brain ingest-notion ~/Downloads/Export-abc123.zip --max 50 --tag notion
```

- **One document per page** (source `notion`, content type `notion_page`), deduplicated by the
  32-hex page id at the end of each file name (`Title <id>.md`), not by its path. Re-export to a
  new folder and re-run: every page updates in place, and unchanged pages are left alone.
- **Titles** come from the page's `# ` first line, else the file name without its id.
- **Skipped and counted:** images and other attachments, `.md` files with no parseable id (never
  guessed), every copy of an id that appears twice, nested archives (Notion splits large exports
  into `Part-N.zip` files: unzip the outer archive and pass each inner one), links, and pages over
  8 MiB. The database CSVs are counted and never stored.
- **Databases.** With `--with-databases`, each database row is ingested from the row page the
  export writes for it (`<Database> <id>/<Row> <id>.md`), with its properties; a row's
  `Last edited time` dates it. If a database's CSV lists rows but the export has no row pages,
  the command says so.
- **`--since YYYY-MM-DD`** keeps pages last edited on or after that day. Pages with no readable
  last-edited property (ordinary pages have none) are kept and counted. Notion writes that time
  in your local zone with no offset and it is stored as if it were UTC, so it serves ordering and
  the `--since` day window only and may be off by your UTC offset.
- **Not stored, and said so:** a new page whose text is identical to an existing document's
  (`skipped <id>: same text as an existing document`) and an empty page (`skipped <id>: empty`).
  Neither counts as unchanged or toward what remains under `--max`.
- **Cap and cost.** `--max` (default 200) caps the pages written a run; when it stops, it prints
  how many still need writing, and re-running continues, since unchanged pages are free. Every new
  page gets a summary (`--no-enrich` skips it), and with the graph on, concept extraction takes
  minutes per page: 200 new pages can keep the graph lane busy for about 30 hours.
- **Sensitivity** is `normal` unless `--sensitivity confidential`. The secret guard runs on every
  page (`--allow-secrets` skips it for one run).
- **Untrusted input.** A zip is read member by member in memory and never extracted. It is refused
  whole (exit 1) when a member name is absolute or leaves the archive, a member is a symbolic
  link, encrypted or duplicated, it holds more than 100,000 entries (counted header by header
  before the archive is parsed), its ZIP64 records could be read differently by different Python
  patch releases, or its pages and tables add up to more than 512 MiB.
  A folder is walked without following links; files with more than one hard link are skipped,
  and a file replaced between listing and reading is refused.
- **Terminal safety.** Titles are stored on one line with control and format characters removed,
  at most 300 characters. Output names pages by id, never by title; a page the secret guard
  refuses prints `refused <page id>: N finding(s)`, and its warn and redact notices are labelled
  with the page id.

## Drop folders (screenshots, voice memos)

`brain ingest-folder` (RM-33) ingests the files saved into a drop folder: a
screenshot folder, for one. It requires `--dir PATH` (one folder) or
`--configured` (every folder in `$BRAIN_HOME/drop-folders.toml` with
`auto = true`), **and** `--since Nd` (1d to 365d, by modification time).
Otherwise it exits 2 with "ingest-folder requires --dir PATH or --configured,
and --since Nd".

```bash
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d --dry-run
brain ingest-folder --dir ~/Pictures/Screenshots --since 7d
brain ingest-folder --configured --since 2d          # what the LaunchAgent runs
```

- **Move first, then ingest.** Each new file is moved to the folder's
  `imported/` as `<stem>-<sha8><ext>` (`sha8`: the first 8 hex digits of its
  SHA-256) and ingested from there, titled by its original stem. Two different
  files with one name stay two documents. The same file dropped twice: the drop
  copy is deleted (only after its full SHA-256 matches the earlier import) and
  that import is ingested again, which adds nothing when its document exists
  (reported `duplicate`) and recovers a file a crash left in `imported/`.
- **A missing folder** (an unplugged volume, a folder deleted) is skipped with a
  warning naming its `drop-folders.toml` entry by index; the other folders still
  run, and it alone does not make the run exit 1. `brain doctor` warns too.
- **Your files are safe.** A move never overwrites (a hard link, or an exclusive
  copy on a volume without hard links), and files are moved by identity: the
  folders are held open, and a file swapped for a symlink or a pipe mid-run is
  refused. A transient failure (the database) moves the file back, unless a new
  file took its name meanwhile; then it stays in `imported/` and the failure says
  so. A failure every run would repeat (an unreadable or oversized image, an OCR
  timeout, the secret guard) leaves it in `imported/` as `rejected`, never retried. A file that already sat in `imported/` before the run is
  never moved or deleted. `imported/` must be a real directory: a symlink there
  refuses the folder.
- **What is skipped:** sub-folders (`imported/` among them), symlinks, hidden
  files (iCloud's `.<name>.icloud` stubs and macOS's in-progress screenshot
  files), iCloud files evicted from the disk, other extensions, and files
  modified in the last 5 seconds (still being written; the next run takes them).
- **Images** (`.png`, `.jpg`, `.jpeg`) are OCR'd by the local `tesseract`
  into a `screenshot` document (source `screenshot`, stored **confidential**
  unless the folder or `--sensitivity` says otherwise). An image with no
  recognised text stays in `imported/` and adds no document. With OCR off or no
  `tesseract`, image folders are skipped with the fix printed, and exit 1.
- **Voice memos** (`.m4a`, `.mp3`, `.wav`, RM-38) in a `kind = "voice"` folder
  are transcribed locally by whisper.cpp (`afinfo`, `afconvert`, `whisper-cli`)
  into a `voice_memo` document (source `voice`, stored **confidential** unless the
  folder or `--sensitivity` says otherwise); only the transcript enters the brain.
  A memo over `BRAIN_WHISPER_MAX_MINUTES`, one that is not audio, and any timeout
  are `rejected`, and so is one whose WAV reaches the file-size limit (`afconvert`
  runs under one), or whose transcript is over 4 MiB. Whisper failing on the converted
  audio moves it back, with a warning naming its `imported/` name, the error class, and
  `failure N of 3`. The third failure on the same memo content is `rejected` instead
  (the count is in `$BRAIN_HOME/state/voice-failures.json`). A memo's transcription
  runs on its own timeout (twice its length plus a minute), not the run's 8
  minutes. With voice memos off or `whisper-cli` missing, voice folders are
  skipped with the fix printed, and exit 1.
- **Bounds.** At most 200 files a folder and 8 minutes a run; the rest are left
  where they are for the next run, and the report says how many. `screenshot`
  skips concept extraction; summaries follow `BRAIN_ENRICH_MIN_TOKENS`, which
  most screenshots are under.
- `--dir` uses the folder's configured settings when it is in
  `drop-folders.toml` (even with `auto = false`), else the screenshot defaults.
  `--dry-run` lists what would be ingested and touches nothing.
  `--no-enrich` and `--allow-secrets` work as for `brain ingest`.
- Exit 0 when nothing failed; 1 when a file failed or a folder could not be
  used. Failures are printed by class only.

`brain ingest <file>.png` works too (source `manual`, stored **confidential**
unless `--sensitivity normal`; `brain ingest-dir --ext png` likewise), and so does
`brain ingest <memo>.m4a` (RM-38; the same default; with voice memos off it is a
one-line refusal, exit 1). Audio is not in `brain ingest-dir`'s default walk either. File names
are printed, and titles stored, on one line with control characters removed. Images are not in `brain ingest-dir`'s default walk; `--ext png` adds
them. The folder file and the `com.brain.dropfolders` LaunchAgent
(`brain-install-launchd --with-dropfolders`) are in
[guides/drop-folders.md](guides/drop-folders.md).

## Apple Notes ingest

`brain ingest-apple-notes` (RM-36, macOS only) stores the notes of the Apple
Notes folders you name, read through `osascript -l JavaScript` (a macOS
built-in); brain stores no credential. It requires `--folder NAME`
(repeatable); "all notes" is not offered. Otherwise it exits 2 with
`ingest-apple-notes requires --folder NAME (repeatable); "all notes" is not
offered`. On any other platform it exits 1. The first run triggers macOS's
Automation permission prompt for Notes; see
[guides/apple-notes.md](guides/apple-notes.md).

```bash
brain ingest-apple-notes --folder Reading --dry-run
brain ingest-apple-notes --folder Reading --folder Projects --since 2026-10-01
brain ingest-apple-notes --folder Recipes --sensitivity normal --tag cooking
```

- **Scope.** Folders are matched by exact name in every account. A name that
  matches no folder is warned about; if none matches, the command exits 1.
  `--since YYYY-MM-DD` keeps notes modified on or after that day (local time).
- **One document per note** (source `applenotes`, content type `apple_note`,
  deduplicated by the note's Notes id). An edited note updates in place; a
  note moved to another folder, or touched without a text change, updates
  only its metadata (`folder`, `modified`) without re-chunking. The creation
  time dates the document (`sent_at`).
- **What the body holds:** the note converted from Notes' HTML to Markdown
  (headings, lists, checklists as `- [ ]` / `- [x]`, links). Attachments are
  not ingested, and **locked notes are never read**: the command counts them.
  A note with no text left is counted as `empty` and not stored. A note whose
  converted body is identical to another stored note's (two copies of one
  template) is reported by id as `same body as another note` and not stored;
  the other note's document is left untouched.
- **Sensitivity.** `confidential` by default (personal notes are private by
  nature); `--sensitivity normal` overrides it.
- **Cap and cost.** At most `--max` notes a run (default 200), newest
  modification first; the command says how many more matched. Every new note
  gets a summary (`--no-enrich` skips it), and when the graph is on, concept
  extraction takes minutes per note in the watcher's graph lane, so a full
  200-note run keeps it busy for more than a day. Start with `--since`.
- **Safety.** The `osascript` program is fixed; folder names reach it only as
  JSON data, never as code, and it only reads. It always runs
  `/usr/bin/osascript` (never a `PATH` lookup), stopped after 120 s; its error
  output is never shown. A body over 2,000,000 characters is skipped (counted
  as `skipped`), the Markdown kept from a note stops at 1,000,000 characters,
  and a note that fails to convert costs that note only. `brain doctor`'s
  `apple notes` row says whether `/usr/bin/osascript` exists.
- `--dry-run` lists `folder / title` for each note and writes nothing.

## Status and health

```bash
brain status          # counts and last-ingest time
brain status --json   # machine-readable: {documents, chunks, sources, by_source, last_ingest}
brain doctor          # env, Postgres/pgvector, embedder, gws, mirror drift, AGE + graph health
brain doctor --json   # machine-readable [{check, status, detail, remedy}]; still exits non-zero on FAIL

# Refresh Postgres planner statistics. Fixes the "chunks stats — never analyzed"
# warning doctor reports after a pg_restore (a restore bulk-loads rows via COPY
# but never runs ANALYZE, so the planner uses bad row estimates until autovacuum).
brain analyze         # ANALYZE the chunks table (the one doctor warns about)
brain analyze --all   # ANALYZE every table in the database
```

`brain doctor` exits non-zero only for required failures: config, database, or
active embedder. Optional integrations (`gws`, the AGE extension, the
concept-extractor model, community materialization staleness, the local
reranker, the `tesseract` OCR probe) are warnings; a missing `tesseract` is OK.
`brain status --json` and `brain doctor --json` are the scripting-friendly
paths; `doctor --json` preserves the same non-zero exit on any FAIL check.

## brain capture

```bash
# Jot a thought straight into the brain — always tagged `inbox`.
brain capture --text "Follow up with the platform team about the migration cutover"
echo "Idea: batch the nightly re-embed to cut Ollama warmups" | brain capture --title "re-embed idea" -t ideas

# Review the inbox later: promote, tag, or discard each item; or just list it.
brain capture list
brain capture list --json     # machine-readable; the safe surface for an agent
brain capture review
brain capture review --auto   # non-interactive: LLM-routes items out by tag
```

`brain capture` is a zero-friction inbox: pipe text on stdin or pass `--text`,
optionally adding `-t/--tag` alongside the always-on `inbox` tag. `brain capture
list` shows what's waiting; `brain capture review` walks each item so you can
promote it into a real note, retag, or discard it.

`capture review` is interactive and its discard path deletes a document, so it
is deliberately not something an agent should drive unattended — the
[`brain-proactivity` skill](agent-skills.md) covers it with that prohibition
attached.

## brain claude install-hooks

```bash
brain claude install-hooks                 # install the session-end capture hook
brain claude install-hooks --dry-run       # show what would change, write nothing
brain claude install-hooks --target ~/.claude-alt   # non-default config root
brain claude install-hooks --force         # overwrite a differing hook script
brain claude install-hooks --uninstall     # remove the hook entry and script
```

Installs a Claude Code **Stop** hook that nudges exactly one dedupe-then-capture
pass after a session that did real work and wrote nothing back to the brain. It
writes `<target>/hooks/brain-capture-hook.sh` and merges an entry into
`<target>/settings.json`; `--target` moves both (default `~/.claude`).

Opt-in and separately reversible: `--uninstall` removes the entry *and* the
script. `--force` overwrites a hook script that differs from the shipped one,
but it **never** bypasses the refusal to touch a malformed `settings.json` —
merging into a file we cannot parse risks destroying unrelated configuration,
so that refusal has no override.

Run `--dry-run` first if you have hand-edited your Claude Code settings.

## brain backfill scan-secrets

```bash
brain backfill scan-secrets                                  # READ-ONLY report
brain backfill scan-secrets --json                           # same, machine-readable
brain backfill scan-secrets --limit 200                      # stop after N documents
brain backfill scan-secrets --apply --action mark-confidential
brain backfill scan-secrets --apply --action redact          # slow; rewrites bodies
```

Scans every stored document for credential-shaped strings — the retroactive half
of the `F4` ingest guard, which only protects documents from the moment it
shipped.

**Read-only by default, behind two independent gates.** `--apply` is required to
write anything, *and* the default `--action report` cannot write even when
`--apply` is passed. Both exist because the destructive action rewrites document
bodies across the whole corpus. To actually change anything you must name a
non-`report` action **and** pass `--apply`; either alone is a no-op report.

| `--action` | With `--apply` |
|---|---|
| `report` (default) | never writes, regardless of `--apply` |
| `mark-confidential` | raises the tier on each hit |
| `redact` | rewrites bodies — re-chunks, re-embeds, re-hashes, regenerates vault mirrors |

`redact` is by far the slowest option for that reason. An unrecognized
`--action` is a usage error (**exit 2**).

A document that `redact` could not rewrite is listed under `error(s)` (and in
`--json`'s `errors`) and keeps its secret. A `mark-confidential` change that
could not reach a vault file is listed the same way. Every reason names the
failure's class only, never the exception's text. The usual case, where the redacted body is
identical to another document's, reads "redacted body collides with another
document's content hash".

One limit worth stating plainly: this finds what is *already stored*. It cannot
un-send anything that was already transmitted to a hosted embedder before the
scan ran.

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

## brain todo

```bash
brain todo                          # every open action item
brain todo --since 30               # from action-item docs ingested in the last 30 days
brain todo --closed                 # include checked-off items
brain todo --limit 100 --json
```

`brain todo` reads the `content_type='krisp_action_items'` documents that
`brain enrich --krisp-action-items` produces (stored separately from the meeting
transcript), parses `- [ ]` / `- [x]` checklist lines out of each body, and
prints one row per item — so you see what you owe without opening N transcripts.
Default is open items only.

`--since` takes a bare number as **days** (suffixes `7d` / `24h` / `90m` also
work) and filters on `documents.ingested_at`, not the meeting date: a call from
six months ago that you ingested yesterday still shows under `--since 7`.
`--source` accepts only `krisp` today and exists for a future Slack/Gmail
extension. `--json` emits a flat list of `{document_id, document_title,
ingested_at, state, text}`.

There is no `brain todo close`. To check an item off, edit the source
action-item document (`brain edit <id-prefix>`) and change `- [ ]` to `- [x]`;
the next run reflects it. Agents drive this through the `brain-todo`
[skill](agent-skills.md).

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

## brain note move

```bash
brain note move <id-prefix> projects/atlas --dry-run
brain note move <id-prefix> projects/atlas
brain note move <id-prefix> "" --yes            # "" or "." = vault root
brain note move <id-prefix> archive --no-link-refactor
```

Relocates a vault note to another folder, keeping its title **and its
document id** — which is why incoming backlinks survive. A move is a rename to
the same title in a different folder, so it reuses the whole rename machinery:
the plan phase scans the vault for path-form `[[…]]` references, and the apply
phase snapshots every file it touches and restores them on any failure.

`NEW-FOLDER` is vault-root-relative and created if missing. A leading slash is
stripped, so `/projects/atlas` means `projects/atlas` — never the filesystem
root. Paths that escape the vault are rejected (exit 2).

**Safe to run with `brain vault sync --watch` and `brain-mcp` live.** The file
is relocated with an atomic rename (same inode) and `documents.vault_path` is
repointed before the follow-up sync, so the watcher observes a *move* rather
than a delete-then-create. That distinction matters: the delete branch would
cascade every incoming link away.

There is **no `--force`**. A note already at the destination is a hard error,
because silently clobbering one is unrecoverable. Moving a note into the folder
it already occupies prints `already in <folder> — nothing to do` and exits 0.

Confirmed by default (a move rewrites links across the whole vault and you
cannot see that blast radius from the command line); `--yes` skips the prompt,
`--dry-run` skips it too since nothing will be written.

## Maintenance odds and ends

```bash
brain reembed                       # backfill NULL chunk embeddings
brain orphans                       # vault notes with no links either way
brain orphans --all                 # include ingested-tier mirrors
brain uninstall                     # remove runtime state + daemons
```

`brain reembed` backfills `chunks.embedding` for rows that have none. After
`brain init` every chunk starts NULL, so this is the second half of first-run
setup. Idempotent — only still-NULL rows are touched, so it is safe to re-run
after a crash. It also finalizes the column (`NOT NULL`, plus an HNSW index
when the active backend's dimension allows one).

`brain orphans` lists documents with **zero** incoming and zero outgoing links.
It defaults to vault-tier only: ingested-tier mirrors legitimately have no
`[[…]]` links of their own, so including them buries the signal. `--all` opts
them in.

`brain uninstall` removes launchd plists, stops the Docker compose stack and
deletes the `$BRAIN_HOME` runtime files (`.env`, `.shims/`, and a legacy
`Caddyfile` an older install may have left).
**Your database and vault are kept by default** — read its `--help` before
passing anything that widens the blast radius.

## brain graphrag remove

```bash
brain graphrag remove <document-uuid> [<document-uuid> ...] [--tenant T]
```

The targeted repair for graph removes the vault watcher could not apply. When
the watcher's shutdown times out behind a long graph reconcile, or its graph
lane loses its database connection, it logs the document ids whose graph
removes were not applied, and it names this command. Those documents' rows are
already gone, but their AGE `Document` vertices and `MENTIONED_IN` edges stay.
`brain graphrag remove` removes exactly those. Then, once for the whole batch,
it garbage-collects any entities left with no mentions and refreshes the
aggregate edges. It is much cheaper than `brain graphrag build --force`, which
re-extracts every document's concepts. Like `refresh`, it holds the tenant's
graph write lock for that pass, so the watcher's graph sync waits for it.

- **Full UUIDs only.** A deleted document has no row to resolve a prefix
  against, so a prefix exits 2 and nothing is removed. The same UUID given
  twice, in any spelling, counts once.
- **Deleted documents only.** If any id's document still exists, it exits 1 and
  nothing is removed. That includes a document restored while the command
  waited for the lock. To delete a document, use `brain rm`.
- **Idempotent.** An id whose vertex is already gone prints
  `<id>: no document vertex, nothing to remove` and exits 0.
- **Counts only.** It prints each id with the vertex count removed, then a
  summary line. It never prints a title or an entity name.

The MCP twin is `brain_graphrag_remove(document_ids, tenant=None)`.

## brain backup / brain restore

```bash
brain backup                        # DB + vault, into BRAIN_BACKUP_DIR
brain backup --no-vault --label pre-migration
brain backup --json                 # manifest as JSON
brain restore <archive>             # y/N confirmation
brain restore <archive> --db-only --yes
```

`pg_dump` runs **inside the container** by default. This is not a preference:
the production Postgres is 16.x while a typical host Homebrew `pg_dump` is
14.x, and the version mismatch makes a host-side dump abort outright. A host
binary is used only after an explicit major-version check.

The vault is archived whole except for `<vault>/.quartz/`, the leftover
workspace of the retired rendered wiki (see [docs/vault.md](vault.md)). Nothing
reads it, it can run to many GB, and the backup does not walk into it; the
report says when it was left out. Every other dotted directory (`.obsidian/`, a
vault `.git/`) is user data and is backed up. A restore renames the existing
vault aside whole, so a `.quartz/` on disk moves with it to
`<vault>.replaced-<timestamp>` and is never deleted.

`--yes` skips the y/N prompt but **never** skips the pre-flight compatibility
checks — those are what stop a restore from half-applying.

## brain ui

```bash
brain ui                                  # loopback only, opens a browser
brain ui --read-only
brain ui --lan --read-only                # serve to the LAN (e.g. a phone), token minted
brain ui --host <LAN address> --token <secret>   # the manual form of --lan
```

The primary reading surface: a local, single-user web app over the same corpus. Adds **no** runtime
dependency — `starlette` and `uvicorn` already ship as transitive deps of the
MCP SDK — and the front end is hand-written static assets with no bundler and
no CDN, so it works fully offline.

`--token` is **required** whenever `--host` is not loopback. The only hostname that counts as
loopback is `localhost` (and only when it resolves to loopback alone); any other name — including
`ip6-localhost` and `localhost.` — needs a token even if it resolves to `127.0.0.1`. The address
literals `127.0.0.1` and `::1` need none.

`--lan` binds this machine's LAN address. It uses `--token` if given and otherwise mints one,
then prints `http://<address>:<port>/#t=<token>` to open on the other device. It cannot be
combined with `--host`. Do not use `--host 0.0.0.0` to reach other devices: a wildcard bind
gets the loopback `Host` allowlist, so a browser on another device is refused with 400. The
port is still reachable by programs that set `Host` themselves — the token, not the bind, is
what keeps other clients out. The server prints a warning when you use it. Full walkthrough and caveats:
[Reading your brain from a phone](guides/brain-ui-on-your-phone.md).

Confidential bodies follow the bind. On a **loopback address** (`127.0.0.1`,
`::1`), or on **`localhost` when it resolves only to loopback**, they are always
served — the UI is exactly as inside the trust boundary as `brain show` is —
and `--include-confidential` has no
effect. (A `localhost` that also resolves to a non-loopback address is a
non-loopback bind.) On a **non-loopback** bind they are withheld unless you pass
`--include-confidential`, since the material is then crossing the wire. A
**loopback alias** — any other name that resolves to loopback — is guarded like
a network bind: a token is required, and bodies are withheld unless you pass
`--include-confidential`. The startup banner states which of the three you got.

**Copy path.** The note pane's **Copy path** button puts the note's path on the
clipboard, so you can open the file in any editor. On a loopback server it copies
the absolute path of the note's file; elsewhere it copies the path inside the vault
(`vault_path`). The status beside the button says **Copied**, or why the copy
failed (for example, a denied clipboard permission). The button needs the
browser's clipboard API, which a page offers only in a secure context: an
`http://127.0.0.1` page is one, and a `--lan` `http://` page is not, so the
button is absent over `--lan`. `GET /api/notes/{id}` carries the absolute path as
`abs_path` only on a loopback bind (a loopback address, or `localhost` resolving
only to loopback), and never for a note whose body is withheld or whose vault
file is missing. There is no `obsidian://` link, and the server never starts an
editor.

**When the embedder is unreachable, the Notes tab searches text-only.** If the
query embedding cannot be computed, `GET /api/search` still answers 200 with
full-text matches, and a notice above the results says so: the failure by class
only, and whether it is a transient outage, a configuration problem (`brain
doctor`) or a query longer than the embedder's context window. The meta line
reads `fts-only`, and the next search tries the embedder again. The response
carries the same four `fallback*` keys as `brain search --json --meta` (all
`null` on a normal search). It used to answer `503 embedding_unavailable` and
show nothing.

Each page request tries the embedder afresh, so with an embedder that comes and
goes between two requests, one page can be text-only-ranked and the next
hybrid-ranked, and a note can repeat or be skipped across the two. Each page's
own notice says which ranking it shows.

**Ingesting from the browser.** The **Ingest** tab pastes text or markdown into
the brain. Give it a title, paste the body, and optionally add tags, pick the
type (`note` or `transcript`) and the sensitivity, or skip the summary. The job
panel beside the form shows the job queued, then embedding (`Embedding 3 of 20
chunks`, with a progress bar), then the outcome: **Added** with an *Open in
Notes* link, **Already in your brain** for a body you pasted before, or the
reason it failed. It also shows the summary and graph status, any credential-like
strings the secret guard found (masked), and a note when a confidential paste
was kept off a hosted embedder. **Cancel** works until writing starts. The panel
remembers the jobs this browser tab submitted, so after a reload it shows how
they ended; if the server restarted in between, it says the outcome is unknown
and that re-submitting is safe. A confidential document's title is shown only
when `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` is on; otherwise the panel says the
title is withheld and offers no link. On a `--read-only` or `--host` / `--lan`
server the tab is disabled and says why.

**Uploading a file.** Switch the tab to **Upload a file**, then choose a file or
drop one on the zone (the zone is a button, so the keyboard works too). One
`.md`, `.markdown`, `.txt`, `.pdf` or `.docx` file per job: text and markdown up
to 5 MiB, PDF and Word up to 25 MiB. The file is uploaded as soon as you choose
it, so a wrong type, an oversize file or a file whose contents do not match its
extension is refused at once; **Remove** discards it. Add tags, a sensitivity and
an optional title (otherwise the document is titled as the extractor titles it,
from the file's name for a PDF or a text file), then **Ingest**. The job runs on
the same panel as a paste, and an identical file uploaded again is **Already in
your brain**. A file ingested once with `brain ingest <path>` and once by upload
becomes two documents: the upload has no path to dedup on. An uploaded file you
never submit is deleted once it is 15 minutes old, at the next upload or submit,
or when the server stops; a submitted one when its job ends.

**Asking from the browser.** The **Agent** tab answers a question from your
brain, as `brain ask` does. Type a question (at most 2,000 characters), tick
*Quick answer* to skip the planning loop, and press **Ask**. The answer card shows
each step as it runs (*Planning the search*, *Retrieving notes (round 1 of 3)*,
*Checking whether that is enough*, *Writing the answer*), then the answer, the
notes it cites and what it searched for. A number in brackets, such as `[1]`, is a
link that opens the cited note in the Notes tab; so is *Open in Notes* beside each
citation. **Cancel** works until the answer is ready. A question waits while an
ingest job runs (the card says *Waiting for the ingest job to finish*), and the
tab takes one question at a time. Answers are plain text: the model's output is
never rendered as markup, so formatting characters and HTML in it show as typed.
Where the server does not serve confidential bodies, an answer that draws on a
confidential note is withheld, and the card says so with no link. The tab
remembers the questions this browser tab asked, by id only, for as long as the
server keeps the answer (10 minutes); after a reload a card is labelled by the
time it was asked. **Follow up** on an answer asks the next question in context:
the search plan sees the earlier questions and answers (at most the last three),
so a question such as *and when?* is planned as a search for the thing you were
asking about, while the answer itself is written only from what the searches find
now. Those turns come from the cards on the page and are never stored, so a reload
ends the conversation and a card restored after one offers no **Follow up**. A
follow-up always plans, so *Quick answer* is off for it; **New question** drops
the context. On a `--read-only` or `--host` / `--lan` server the tab is
disabled and says why: use `brain ask` from a terminal.

The tab sits on these routes. On a
loopback, writable server, `POST /api/ingest/paste` ingests pasted text as an
ordinary ingested document from source `manual`, through the same pipeline as
`brain ingest*`. The request carries `title`, `body` (at most 2 MiB), and the
optional `tags`, `content_type` (`note` or `transcript`), `sensitivity` (`normal`
or `confidential`) and `enrich` (default true). It answers 202 with a job, which
a client polls with `GET /api/jobs/{id}?after=<seq>` and can cancel with
`POST /api/jobs/{id}/cancel` (body `{}`) until writing starts; after that cancel
answers 409 `not_cancellable`. Pasting the same body again is a no-op that names
the existing document. A result whose document is confidential (or whose level is
unknown) has its title, vault path and document id withheld
(`title_withheld: true`) unless `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` is on, as in
the tree. Jobs run one at a time, at most four wait (a fifth gets
429), and they live in memory only, so after a restart re-submitting is the
recovery. `GET /api/ingest/config` reports whether ingest is available and why
not (`read_only`, `requires_loopback` or `jobs_unavailable`), the uploadable
`extensions` and their per-type caps (`limits.upload_bytes`). Ingest is never
offered over `--host` / `--lan`: from another device, use `+ New` or
`brain ingest`.

An upload is two requests. `POST /api/ingest/uploads` takes the file's raw bytes
as `application/octet-stream`, the one route whose body is not JSON, with its
name percent-encoded in `X-Brain-Filename`; the server keeps only the name's last
component and extension, streams the body to a private staging file (at most
25 MiB for `.pdf` / `.docx`, 5 MiB for text, 64 MiB staged in total), checks the
bytes match the extension, and answers 201 `{upload_id, size, kind}`. Refusals:
400 `bad_filename`, 413 `upload_too_large`, 415 `unsupported_file`, 429
`staging_full`. `POST /api/ingest/file` with JSON `{upload_id, tags?, sensitivity?,
enrich?, title?}` queues the job (202), or answers 404 `upload_unknown` for an
unknown or expired upload. `DELETE /api/ingest/uploads/{upload_id}` (body `{}`)
discards an unsubmitted file. An upload's result also carries
`original_filename`, withheld with the title.

The inbox (RM-40) is the first `drop-folders.toml` entry with `kind = "manual"` and
`auto = false`, which the `--configured` LaunchAgent never ingests
([guide](guides/drop-folders.md#the-ingest-tabs-inbox-rm-40)). `GET /api/ingest/inbox`
answers 200 `{available, reason, extensions, files: [{name, size}], truncated,
default_sensitivity}`: names only, never a path, newest first, at most 200 from the first
5,000 directory entries, and an empty list unless ingest is available. The names are not
lensed by `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` (the tab asks only on demand, and only a
loopback, writable server answers) (`reason` is also `inbox_unconfigured`,
`inbox_misconfigured` or `inbox_unreadable`). `POST /api/ingest/inbox` with JSON
`{name, tags?, sensitivity?, enrich?, title?}` queues a job of kind `ingest.inbox` (202);
`sensitivity` defaults to the folder's (confidential unless set). Refusals: 400
`invalid_name` (a `/`, `..`, NUL, control character or leading dot), `outside_inbox`,
`not_a_regular_file` (a symlink, folder or pipe), `hard_linked_file` or `unknown_field`; 404
`inbox_file_unknown` or `inbox_unconfigured`; 409 `inbox_misconfigured`,
`inbox_unreadable` (the inbox cannot be searched) or `file_not_on_disk`; 413 `inbox_file_too_large`; 415 `unsupported_file`. The job checks the
name again, moves the file into the inbox's `imported/` folder and ingests it from there
(source `manual`, deduplicated by that path); every failure puts it back, by error code
(`inbox_file_changed`, `inbox_file_gone`, `inbox_file_too_large`, `file_not_on_disk`,
`inbox_unreadable`, `inbox_move_failed`, `inbox_unusable`, or the ingest codes above). The
job ingests the file the submit checked or nothing: the opened file must have that identity,
one link, and a size within the cap, and it is hashed reading at most the cap plus a byte.
If the result cannot be read back after the commit, the job still succeeds, with
`details_unavailable` (an error class) and no counts; the file stays in `imported/`.

The Agent tab's backend asks a question as a job
on the same worker, one at a time with ingest. `POST /api/ask` with JSON
`{question, no_loop?, history?}` (a question of at most 2,000 characters; `no_loop`
defaults to false) answers 202 with a job of kind `ask`, which runs `brain ask`'s
plan, retrieve, reflect and synthesize loop in `hybrid` mode. Poll it with
`GET /api/jobs/{id}?after=<seq>`: each step is an event (`plan`, `retrieve`,
`reflect`, `synthesize`, with the iteration as `done`), and the result is
`brain ask --json`'s shape plus each citation's `sensitivity` and `withheld`, and
`elapsed_ms`. Cancel it with `POST /api/jobs/{id}/cancel` at any point until it
finishes; a cancelled ask records nothing. An ask that runs past
`BRAIN_ASK_MAX_ITERATIONS` × 3 × `BRAIN_ASK_TIMEOUT_SECONDS` fails with
`timeout`; the other error codes are `chat_unavailable`, `context_overflow`,
`output_truncated`, `synthesis_failed`, `embedder_unavailable`, `database_error`
and `internal_error`. Answers draw on confidential documents only where the
server serves confidential bodies (a loopback bind, or `--include-confidential`),
and a citation of a confidential or unknown-level document is withheld, with the
answer, where it does not. Ask jobs are never listed by `GET /api/jobs`; a result
is readable only by its id, and only for 10 minutes. `GET /api/ask/config`
reports whether asking is available and why not (`read_only`,
`requires_loopback` or `jobs_unavailable`), the question cap, the follow-up turn
cap (`max_history_turns`) and the modes. A follow-up sends `history`, a list of
earlier `{question, answer}` turns, oldest first. Each must be non-empty, the
question at most 2,000 characters and the answer at most 8,000 (otherwise 400
`invalid_history` or `history_turn_too_long`), and history with `no_loop: true` is
400 `history_requires_loop`. The server keeps the newest three turns, then the
newest that fit in 2,000 tokens, and shows them to the plan step only. Like
ingest, asking is refused on `--read-only` (403 `read_only`) and over `--host` /
`--lan` (403 `ask_requires_loopback`).

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
configuration docs. Agents reach this whole set through the
[`brain-proactivity` skill](agent-skills.md) (`brief` / `resurface` / `review` /
`timeline` / `connect` / `gaps` / `capture`); `ask` and `audio` live in
`consult-brain`.

`brain brief` and `brain resurface` in action — the day's digest, then the
older notes ranked for another look (recorded against a synthetic corpus):

![brain brief's daily digest followed by brain resurface's spaced-repetition table, over a synthetic compliance corpus](assets/proactivity.gif)

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

Every `brain brief` first checks your saved searches (`brain watch`, below) and
adds a **Watch hits** section when one has new matches. The terminal lists every
hit first seen in the capture window; the `--wiki` page lists the hits no page
has listed yet, at most 5 per watch ("and N more"), and leaves out confidential
documents. Once the page is written, those hits are marked as notified, so the
next page does not repeat them. The section is absent when you have no watches.

A **New this week** section names the people, organisations and projects whose
first mention in a non-draft document was ingested in the last 7 days
(most-mentioned first, at most 10; `[person] Name`). The terminal lists every
such name; the `--wiki` page, and anything delivered, leaves out an entity
known only from confidential documents. The section comes from the GraphRAG
entity tables, so it is absent when `BRAIN_GRAPH_ENABLED=false` or those tables
do not exist. `--json` carries it as `new_entities`.

A **Possible duplicates (N)** line appears when near-duplicate pairs (`brain
dedup`, below) are open. It is a count, never a title. The terminal counts every
open pair; the `--wiki` page, and anything delivered, counts only pairs whose
two documents are neither confidential nor drafts. The brief never scans; the
daily brief job runs `brain dedup scan --limit 500` just before it. `--json`
carries it as `possible_duplicates`.

**Delivery (opt-in, off by default).** With `BRAIN_BRIEF_DELIVERY=email` or
`slack` (see [configuration](configuration.md#brain_brief_delivery--send-the-daily-brief-to-yourself)),
`brain brief --wiki` also sends the page's content to you once it is written:
an email to `BRAIN_USER_EMAIL` through `gws gmail +send`, with the fixed subject
`[brain brief] YYYY-MM-DD`, or a post to a Slack incoming webhook (link previews
off). Only what the page carries is sent (never a confidential title), minus any
draft document, and by default only titles and counts: todo text and the
suggestions built from it go out only with
`BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true`. A delivery problem never fails the
brief: it prints one warning and exits 0, and the watch hits stay unnotified so
the next brief sends them again. A `gws` authorization failure names the fix,
`bin/gws-reauth`. `brain doctor` has a `brief delivery` row. `brain ingest-gmail`
skips your own delivered briefs (see [Gmail ingest](#gmail-ingest)).

### brain watch

Saved searches ("watch lists"). Save a query once, and each morning's brief lists
the documents that **newly** match it: new documents, and older ones you have
since edited. A watch matches **by words** (full-text, works with the embedder
down) unless you pass `--semantic`, which matches by meaning and keeps a match
whose similarity reaches `BRAIN_WATCH_MIN_COSINE`.

```bash
brain watch add roadmap "quarterly roadmap"        # by words
brain watch add calls "pricing" --source krisp     # with filters
brain watch add ideas "agent memory" --semantic    # by meaning
brain watch list [--json]
brain watch check [NAME] [--json]                  # check now and record new hits
brain watch check --peek                           # report without recording
brain watch hits [NAME] --since 7d [--json]        # recent hits, listed or not
brain watch pause NAME | resume NAME | rm NAME
```

- **Filters** (`add`): `--source`, `--tag`, `--person`, `--kind`,
  `--without-tag`, validated as `brain search` validates them. Tags are stored
  normalised; the person is resolved again at each check.
- **`add` records the current matches as already seen**, so the first brief
  is not a flood and an immediate `check` reports nothing. A `--semantic` add
  while the embedder is unavailable is refused, with nothing saved.
- **A check looks at what changed since the last one** (documents created or
  edited since an hour before the previous check), so a document is reported
  once per watch, and an old match is never reported again.
- **`check NAME` runs a paused watch too**; `check` with no name, and the brief,
  check enabled watches only.
- A semantic check that cannot embed is reported as `skipped, embedder
  unavailable`; it records nothing and retries the same window next time. A bad
  `BRAIN_WATCH_MIN_COSINE` skips semantic watches the same way and never stops
  the brief.
- **A check records at most 50 documents.** When more match at once (a bulk
  import), `check` and the terminal brief say how many more matched than it could record
  (a semantic check says "50 checked, more may exist"); `brain search` lists them.
- Saved searches need migration 029: run `brain init` after upgrading.

### brain dedup

Near-duplicate detection: pairs of documents that are near-copies of each other
(a forwarded email, a re-sent draft, one file ingested twice by two paths). It
**flags only**: nothing is ever refused, merged or deleted. Deleting one of a
pair stays `brain rm`.

```bash
brain dedup scan [--all] [--limit N]         # sign new/edited documents, record pairs
brain dedup list [--status open|dismissed|confirmed] [--limit N] [--json]
brain dedup dismiss A B                      # not duplicates; never re-opened
brain dedup confirm A B                      # duplicates (a label only)
```

- **How a pair is found.** Each body is lowercased and split into words; every
  run of 5 words is hashed, and a 128-value MinHash signature with 32 bands of 4
  rows proposes candidates. A candidate is recorded only when its estimated
  word-5-gram Jaccard similarity is **at least 0.85**. Documents under **50
  words** are skipped, and two documents of one Gmail thread are never paired.
- **`scan` signs only what is new or edited** (a document whose content hash
  changed since its last signature); `--all` re-signs everything, and `--limit`
  caps one run, reporting how many are left. The daily brief job runs
  `brain dedup scan --limit 500` on its own line before `brain brief --wiki`, so
  a failed scan never stops the brief.
- **An edit is re-checked**: an open pair that no longer verifies is dropped.
  A pair you **dismissed** or **confirmed** keeps its label through every later
  scan, so a dismissed pair is never re-opened. Deleting a document deletes its
  pairs.
- `dismiss` and `confirm` take id prefixes (6+ characters), in either order.
- `list` is the terminal, so it lists confidential documents too; MCP
  `brain_dedup_list` does not, unless asked (see
  [configuration](configuration.md#mcp-tools)).
- The Ingest tab's job result carries `possible_duplicates`: the new document's
  near-copies among documents already signed. A confidential near-copy's title,
  id and path are withheld unless `brain ui` serves confidential titles.
- Needs migration 030: run `brain init` after upgrading.

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

# Three distinct verbs for closing a finding — they are not interchangeable.
brain review dismiss <id-prefix>             # "this was never real" (noise)
brain review resolve <id-prefix>             # "I acted on it" → status='resolved'
brain review snooze  <id-prefix>             # "not now" → hidden, returns later
brain review snooze  <id-prefix> --days 30   # default is 7
```

**Pick the verb by what actually happened**, because the queue's usefulness
degrades if everything is dismissed. `dismiss` is for findings that were never
real — a false contradiction between two notes that do not in fact disagree.
`resolve` is for findings you acted on: the contradiction was reconciled, the
stale note was updated. `snooze` hides the finding for `--days` (default 7) and
then brings it back, which is the honest option for "real, but not today" —
dismissing those instead is how a review queue quietly stops reflecting reality.

A snoozed finding disappears from `brain review list` until its snooze expires.
That is intended, but it does mean a quiet queue is not proof of a clean corpus.

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

If only the embedding model is unavailable, `ask` still answers, from text-only
(FTS) retrieval, as `brain search` does. The answer text is not changed: a line
`Retrieval: text-only (semantic ranking unavailable)` follows it, the one-line
notice goes to stderr (exit code 0), and `--json` carries `fallback`
(`"fts_only"` or `null`), `fallback_reason` (the failure's class, never its
message), `fallback_transient` and `fallback_note`. These are separate from
`fallback_used`, which still means the planning loop was skipped or degraded. Every other
failure is one `ask: <sentence>` line on stderr and exit 1: Ollama down, a
prompt over the chat model's context window, an answer cut off at its output
budget, a graph-leg failure. The sentence names the class at most, never the
exception's message.

![brain ask plans sub-queries over the corpus, then synthesizes one answer with inline citations back to the source documents](assets/ask.gif)

_Recorded against a throwaway synthetic corpus; regenerate with `bin/brain-ask-gif`._

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
reports citation-grounding metrics (live-model gated, no baselines). Unlike
`brain ask`, the harness does **not** fall back to text-only retrieval: an
embedder failure stops the run with exit 1, as every `brain eval` mode does.

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
