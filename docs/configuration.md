# Configuration and administration

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index. Post-install operations: the tech stack, installing from source, running `brain` from any directory, feature tuning knobs, Claude integrations, choosing or switching the embedder backend, cleaning up legacy data, and removing brain from the machine.

## Token economics

The condensed version of this table is in the [README](../README.md#token-savings). Querying `brain` burns far less context than having an agent read the source directly via MCP or a file-read tool. Rough per-query estimates (yours will vary with thread/file size):

| Source | Direct (MCP / `Read` tool) | `brain search` (+ optional `brain show`) | Savings |
|---|---|---|---|
| **Gmail** | `search_threads` (~2–5k of metadata) + `get_thread` × 3–5 hits, each 3–10k with quoted replies / signatures / headers (long threads 20k+) → **15–50k** | 5 × 400-char snippets + IDs ≈ **~1k**; one targeted `brain show` of the cleaned body ≈ 1–3k → **~2–4k** | **~5–15×** |
| **Krisp transcript** | `search_meetings` (~500–1k per match) + `get_multiple_documents` for full transcripts (~6k for 30 min, 10–20k for 60 min) across 5 candidates → **25–75k** | Search ~1k; load just the one relevant transcript via `brain show` (~5–15k) → **~6–16k** | **~4–10×** |
| **Slack thread** | `slack_search_*` + `slack_read_thread` per hit (3–8k each with user names / timestamps / reactions) → **10–30k** | Search ~1k; `brain show` on one cleaned thread ~2–5k → **~3–6k** | **~3–8×** |
| **PDF / DOCX (resume, 1-pager)** | `Read` whole file: 5-page resume ~2–4k | One snippet ~100 tokens; usually no `show` needed → **~1k** | **~2–4×** |
| **Long PDF / DOCX (interview prep, 30+ pages)** | `Read` whole file: ~15–25k | Search ~1k; targeted `brain show` of just that doc when needed ~3–8k → **~1–9k** | **~5–15×** |
| **Long Markdown notes (~4k words)** | `Read` whole file: ~6k | One snippet ~100 tokens for the matching passage → **~1k** | **~6–10×** |

Why the gap is so large:

- **Pre-extracted bodies.** Brain stores HTML-stripped, quote-removed, signature-free text. Gmail/Slack MCP returns full thread structure, headers, MIME parts, and quoted replies that bloat every hit.
- **Hybrid retrieval ranks before fetching.** RRF returns the top 5 *actually-relevant* docs in one call. The MCP equivalent is a keyword search that often pulls 20+ unrelated threads and forces a refining round-trip.
- **Chunking returns just the relevant passage.** A 30-page interview prep doc reduces to a ~100-token snippet of the section that matched — the rest of the doc never enters context unless you ask for it.
- **Ingest tokens are paid once, off-conversation.** Embedding + extraction happen during `brain ingest`, never against your chat context. With the default local Arctic backend, ingest is also free in dollar terms.

Caveats:

- Numbers are rough — long, chatty threads or very large docs widen the gap; short ones narrow it.
- Brain only knows what's been ingested. For "search anything in my inbox right now," Gmail MCP is still the only option; brain is for the slice you've curated in.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| CLI | Python 3.11 + [Typer](https://typer.tiangolo.com/) | Fast to write, good ergonomics, easy to test. |
| Storage | PostgreSQL 16 + [`pgvector`](https://github.com/pgvector/pgvector) | One database for both lexical (`tsvector`) and semantic (vector) search — no separate vector store to operate. Runs in Docker on port 55432. |
| Embeddings | Pluggable via `BRAIN_EMBEDDER` — default [Snowflake Arctic Embed v2](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0) over local [Ollama](https://ollama.com/) (1024-dim, Apache 2.0, free). [Voyage AI](https://docs.voyageai.com/) (`voyage-3.5`, paid SaaS) and [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) (4096-dim, local Ollama) are alternates behind the same `Embedder` Protocol. `none` disables embeddings for an FTS-only brain. | Local-by-default keeps the corpus off vendor servers; the abstraction lets the user upgrade or downgrade backends without touching ingest/search code. |
| Search | Hybrid: Postgres FTS + vector cosine, fused via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60) | Lexical alone misses paraphrases ("what did I say about X"); vector alone misses exact names ("a coworker", "a former employer"). RRF combines both ranks without tuning weights. |
| Graph retrieval | [Apache AGE](https://age.apache.org/) (openCypher inside Postgres) + [`networkx`](https://networkx.org/) for Louvain community detection | Entity-centric retrieval for themes, patterns, and connections that hybrid search misses. Graph sync defaults on; on stock pgvector, ingest-time graph sync is a no-op and `brain graphrag …` commands return an AGE-not-available guard. |
| Extraction | `pypdf`, `pdfplumber`, `python-docx`, `markdown-it-py` | Covers the file types I actually have. |
| Chunking | Paragraph-aware, budgeted with `tiktoken` | Keeps semantic boundaries intact while staying under the embedder's token limit. |
| LLM enrichment | Local-by-default [Ollama](https://ollama.com/) (`llama3.1:8b` defaults for `BRAIN_ENRICH_MODEL` and `BRAIN_GRAPH_EXTRACT_MODEL`) | Auto-summary writes `documents.summary` after ingest; GraphRAG concept extraction uses a separate Ollama extractor. `--no-enrich` disables auto-summary for an ingest run; `BRAIN_GRAPH_CONCEPTS=false` disables the concept aspect. |
| Output | [Rich](https://rich.readthedocs.io/) tables + `--json` mode | Human-readable in a terminal, machine-parsable when an agent shells out. |
| Wiki rendering | [Quartz](https://quartz.jzhao.xyz/) (static site from Markdown + `[[wiki-links]]`) + [Caddy](https://caddyserver.com/) (blue/green serve from the `current` symlink) | Optional rendered vault view: graph view, backlinks, full-text search, dark mode. Body-only edits take the per-file fastpath, reaching the UI in ~2s including reload. Start it with `brain-up` in dev; installer users opt out with `--skip-wiki`. |
| Tests | `pytest` against a real Postgres test DB, fake embedder fixture | Real-DB integration catches schema/migration drift that mocks would hide. |
| Lint / type | `ruff`, `mypy` | Cheap to run, catches real bugs. |

## Installing from source (manual)

`brain setup` (see the [README quick start](../README.md#quick-start)) handles
all of this for you. Drive each step yourself only if you're hacking on the
code:

```bash
# 1. Clone the repo and enter it
git clone <repo> ~/workspace/second-brain
cd ~/workspace/second-brain

# 2. Set up the environment file (gitignored). The default BRAIN_EMBEDDER=arctic
#    needs no API keys. If you choose voyage, paste VOYAGE_API_KEY here.
#    Optional: set BRAIN_VAULT_PATH and BRAIN_USER_EMAIL here too.
cp .env.example .env

# 3. Create an isolated Python environment so this project's deps
#    don't clash with anything else on your system
python3.11 -m venv .venv
source .venv/bin/activate

# 4. Install the brain CLI and its dependencies into that venv
pip install -e ".[dev]"

# 5. Start the Postgres + pgvector container in the background
docker compose up -d

# 6. Apply database migrations and align the embedding column with the
#    active backend's native dim (1024 for arctic/voyage, 4096 for qwen3).
brain init

# 7. Backfill embeddings + finalize the column (NOT NULL + index).
#    On a fresh DB this just finalizes the column (NOT NULL + HNSW
#    index when applicable); on a re-ingest it backfills any NULL rows
#    first, then finalizes.
brain reembed

# 8. Sanity check. Optional integrations (gws, npx) print warnings when absent.
brain doctor
```

If `brain doctor` complains, the usual suspects are: Docker isn't running, the
container hasn't finished starting yet (give it ~10 seconds and retry), Ollama
isn't running (`brew services start ollama`), the configured embedding model
isn't pulled (`ollama pull snowflake-arctic-embed2`), or — only when
`BRAIN_EMBEDDER=voyage` — `VOYAGE_API_KEY` is missing from `.env`. Missing
`gws` only disables Gmail ingest and Google directory refreshes; missing `npx`
only disables `brain vault render`. GraphRAG health (AGE extension presence,
graph-drift counters, community-materialization staleness) is a soft check — a
stock pgvector DB is reported as a warning, not a failure, and doesn't flip the
exit code.

## Running `brain` from any directory

By default, `brain` and `brain-mcp` only work inside the repo folder with the
venv activated, and the `bin/brain-up` / `bin/brain-down` / `bin/brain-rebuild`
/ `bin/brain-status` scripts have to be invoked by full path. `brain setup`
installs shims automatically; if you set up from source, two small one-time
edits fix the CLI and wiki scripts (the MCP symlink is optional but useful for
debugging).

**1. Symlink the `brain` launcher onto your PATH** so `brain` works from any
shell — including any agent running in any project:

```bash
# macOS (Homebrew):
ln -s ~/workspace/second-brain/.venv/bin/brain /opt/homebrew/bin/brain

# Linux (or non-Homebrew macOS):
ln -s ~/workspace/second-brain/.venv/bin/brain ~/.local/bin/brain
```

The symlink works without `source .venv/bin/activate` because `pip install -e`
gives the launcher an absolute-path shebang pointing at the venv's Python.

**2. Optional: symlink `brain-mcp` too** so you can start the MCP server from
any terminal while debugging an MCP client (e.g. Claude Desktop):

```bash
# macOS (Homebrew):
ln -s ~/workspace/second-brain/.venv/bin/brain-mcp /opt/homebrew/bin/brain-mcp

# Linux (or non-Homebrew macOS):
ln -s ~/workspace/second-brain/.venv/bin/brain-mcp ~/.local/bin/brain-mcp
```

Your MCP client can still use the absolute venv path shown below; this symlink
is only for convenience.

**3. Add `bin/` to your shell's PATH** so `brain-up` / `brain-down` /
`brain-rebuild` / `brain-status` work from anywhere. Pick the snippet for
your shell:

```bash
# zsh (macOS default since Catalina) → ~/.zshrc
echo 'export PATH="$HOME/workspace/second-brain/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# bash → ~/.bashrc (Linux) or ~/.bash_profile (macOS bash users)
echo 'export PATH="$HOME/workspace/second-brain/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# fish → ~/.config/fish/config.fish
fish_add_path ~/workspace/second-brain/bin
```

If you cloned the repo somewhere other than `~/workspace/second-brain/`, swap
that path in (or use `$(pwd)/bin` while sitting in the repo root).

**4. Verify everything:**

```bash
which brain          # → /opt/homebrew/bin/brain (or ~/.local/bin/brain)
which brain-mcp      # optional; same prefix if you created the symlink
which brain-up       # → /Users/<you>/workspace/second-brain/bin/brain-up
brain doctor         # → required components OK; optional ones may warn
brain-status         # → wiki/watcher status (both stopped initially)
```

Once both are on PATH, the daily flow is just `brain-up` / `brain-down` —
no need to remember Quartz or watcher invocations.

## Feature config knobs

All have sensible defaults; set them in `.env` (see `.env.example`). The
`brain review` knobs cover both the weekly report and the scan engine. These
tune the [Proactivity and synthesis](cli-reference.md#proactivity-and-synthesis)
commands.

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_RESURFACE_LIMIT` | `7` | Default docs surfaced per `brain resurface` run. |
| `BRAIN_RESURFACE_MIN_AGE_DAYS` | `14` | Exclude docs younger than N days from resurfacing. |
| `BRAIN_RESURFACE_AGE_HALFLIFE_DAYS` | `180` | Half-life for the age component of the resurface score. |
| `BRAIN_RESURFACE_ACCESS_HALFLIFE_DAYS` | `90` | Half-life for the last-access staleness component. |
| `BRAIN_BRIEF_SINCE_HOURS` | `24` | Recent-captures window for `brain brief`. |
| `BRAIN_BRIEF_TODO_SINCE_DAYS` | `7` | Open-todo window for `brain brief`. |
| `BRAIN_BRIEF_CAPTURE_LIMIT` | `20` | Max captures listed in the brief. |
| `BRAIN_BRIEF_PIN_LIMIT` | `10` | Max pinned docs listed in the brief. |
| `BRAIN_REVIEW_ACTIVITY_LIMIT` | `20` | Max activity rows in the weekly review. |
| `BRAIN_REVIEW_THEME_LIMIT` | `5` | Max themes in the weekly review. |
| `BRAIN_REVIEW_OPEN_LOOP_LIMIT` | `20` | Max open loops in the weekly review. |
| `BRAIN_REVIEW_CONFLICT_LIMIT` | `30` | Max entity candidates examined by the conflict scan. |
| `BRAIN_REVIEW_CONFLICT_PAIRS_PER_ENTITY` | `3` | Max doc pairs compared per entity in the conflict scan. |
| `BRAIN_REVIEW_EMBED_SIM_FLOOR` | `0.40` | Min embedding similarity to treat two docs as on-topic for conflict. |
| `BRAIN_REVIEW_STALE_AGE_DAYS` | `365` | A doc must be older than this to be a staleness candidate. |
| `BRAIN_REVIEW_STALE_SUPERSEDE_WINDOW_DAYS` | `90` | A newer doc within this window can mark an older one stale. |
| `BRAIN_REVIEW_STALE_SIM_FLOOR` | `0.60` | Min similarity for a newer doc to supersede an older one. |
| `BRAIN_REVIEW_STALE_LIMIT` | `200` | Max staleness candidates examined per scan. |
| `BRAIN_TIMELINE_GRANULARITY` | `quarter` | Default bucket width: `month` \| `quarter` \| `year`. |
| `BRAIN_TIMELINE_LIMIT` | `20` | Max timeline buckets returned. |
| `BRAIN_TIMELINE_SYNTH_LIMIT` | `5` | Densest buckets given an Ollama narrative under `--synthesize`. |
| `BRAIN_TIMELINE_TRIM` | `oldest` | Which buckets to drop when over the limit. |
| `BRAIN_CONNECT_MIN_SCORE` | `0.60` | Min blended (RRF) score to keep a link suggestion. |
| `BRAIN_CONNECT_CANDIDATE_LIMIT` | `50` | Max candidate pairs considered per source doc. |
| `BRAIN_CONNECT_MAX_PER_DOC` | `5` | Max suggestions persisted per source doc. |
| `BRAIN_ASK_MAX_ITERATIONS` | `3` | Hard cap on plan/reflect loop iterations. |
| `BRAIN_ASK_DOCS_PER_ITER` | `5` | Max documents retrieved per iteration. |
| `BRAIN_ASK_MODEL` | `llama3.1:8b` | Ollama model for the plan/reflect/synthesize steps. |
| `BRAIN_ASK_TIMEOUT_SECONDS` | `90` | Per-LLM-call timeout for `brain ask`. |
| `BRAIN_AUDIO_SCRIPT_MODEL` | `llama3.1:8b` | Ollama model for the two-host script. |
| `BRAIN_AUDIO_MAX_TURNS` | `12` | Default dialogue turn cap. |
| `BRAIN_AUDIO_MAX_INPUT_TOKENS` | `3000` | Max grounding tokens fed to the script model. |
| `BRAIN_AUDIO_THEME_LIMIT` | `4` | Max themes/communities folded into the overview. |
| `BRAIN_GAPS_LOOKBACK_DAYS` | `30` | Lookback window for `brain gaps` surfaces. |
| `BRAIN_GAPS_MIN_CLUSTER_SIZE` | `2` | Min failed-query count before a cluster becomes a gap. |
| `BRAIN_RECALL_BUDGET_TOKENS` | `2000` | Default token budget for the whole `brain recall` block, header included. |
| `BRAIN_RECALL_PASSAGE_TOKENS` | `120` | Context window stitched around each document's best chunk. |
| `BRAIN_RECALL_MAX_CANDIDATES` | `25` | Upper bound on documents considered before packing. |
| `BRAIN_SNIPPET_CONTEXT_TOKENS` | `200` | Context tokens stitched around a `brain search` snippet. |
| `BRAIN_SNIPPET_MAX_CHARS` | `1600` | Hard character cap on a stitched search snippet — currently the **dominant** constraint on snippet size (see the note below). Positive integer; **`0` is rejected** with a `ConfigError` at startup, unlike `BRAIN_SHOW_MAX_CONTENT_TOKENS`. |
| `BRAIN_VECTOR_SIM_FLOOR` | `0.25` | Min cosine similarity for a vector-leg candidate to count. See the note below — it trades recall for precision. |
| `BRAIN_RECENCY_HALFLIFE_DAYS` | `180` | Half-life of the recency boost applied to search scores. |
| `BRAIN_BACKUP_DIR` | `$BRAIN_HOME/backups` | Where `brain backup` writes archives and `brain restore` discovers them. Absolute or `~`-relative. Resolved lazily, so relocating the brain home relocates its backups; never created until a backup actually runs. |
| `BRAIN_SHOW_MAX_CONTENT_TOKENS` | `25000` | MCP `brain_show` body **and summary** cap. A cut body gains `content_truncated` + `content_tokens` + `content_truncated_recovery`; a cut summary gains `summary_truncated` + `summary_tokens` + `summary_truncated_recovery`. `content` and `summary` are **each** bounded by this value, so the two fields together total at most **2×** it (the serialized payload is larger — fixed metadata; see the note below). **`0` = unlimited** (the only knob in this family that accepts it), and it opts both fields out. |
| `BRAIN_SEARCH_MAX_LIMIT` | `50` | MCP `brain_search` `limit` ceiling; equals the candidate-chunk limit, above which a larger `limit` cannot surface more documents. Must be **≥ 5**, the tool's own default `limit` (cross-validated at startup, so a lower ceiling cannot fail every default call). |
| `BRAIN_RECALL_MAX_BUDGET_TOKENS` | `13000` | MCP `brain_recall` `budget_tokens` ceiling. See the note below — this is **not** 32000, and the difference is the point. |
| `BRAIN_GRAPH_ENTITIES_MAX_LIMIT` | `500` | MCP `brain_graphrag_entities` ceiling. `limit=0` now means *this*, no longer "all". Must be **≥ 50**, the tool's own default `limit` (cross-validated at startup). |
| `BRAIN_MCP_ROWS_MAX_LIMIT` | `200` | Row cap for the bare-list MCP tools (`brain_backlinks` / `brain_links` / `brain_orphans`). A cut list flags `more_available` on its last element. |
| `BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT` | `25` | MCP `brain_graphrag_communities` listing cap (was "all"). Distinct from `BRAIN_GRAPH_COMMUNITY_LIMIT`, which governs retrieval-time global theme selection. |

### MCP payload ceilings — every default is a judgement call

The six knobs above bound what a single MCP tool call can return, so no one
call can eat a large fraction of an agent's context window. They are sized off
live-corpus percentiles, not derived from anything — which is exactly why they
are env vars, and why exceeding one raises `INVALID_PARAMS` **naming the
ceiling** instead of trimming quietly. A ceiling that silently truncates is
worse than no ceiling: the caller reads a partial answer as a complete one.

### `BRAIN_SHOW_MAX_CONTENT_TOKENS` — one knob, two fields, each bounded

`brain_show` can return a body and a `summary`, and this knob now caps **both**,
at the same value, on **every** path — not only under `summary_only=true`.

**Why the summary is capped at all.** `summary_only=true` is the documented
escape hatch *from* the body ceiling, and it used to hand back a field with no
ceiling of its own: the cheap mode carried the unbounded payload while the
expensive one did not. `documents.summary` is short *in practice* only because
`OllamaEnricher` writes it that way — migration 011 declares it `TEXT` with no
length constraint. "Only the generator keeps it small, not the schema" is not a
bound, and a ceiling module whose escape hatch is unbounded does not have a
ceiling.

**Why every path, not just the escape hatch.** `brain_show` returns `summary`
alongside a full body too, so capping it only under `summary_only` would leave
exactly the same hole on the path an ordinary open takes.

**The consequence, stated rather than implied away: `content` and `summary` are
*each* bounded by this value, so the two fields together total at most `2 ×`
it — not `1 ×`.** That is the price of keeping one knob instead of two. It is a
bound where there was none — but if you are sizing a context window against this
number, size it against twice this number. The same honesty applies as to
`payload_tokens` elsewhere in this branch: a knob whose name promises one bound
and whose behaviour delivers another is a defect, so the doubling is documented
at the knob, in `apply_content_ceiling`'s docstring, and beside the default in
`config.py`.

**The bound is on the two fields, not on the serialized payload.** The response
also carries `title`, `tags`, `source_path`, the ids, and — only when a cut
happened — the recovery-marker prose. Measured end-to-end at
`max_content_tokens=500` (2026-08-20):

| | tokens |
|---|---|
| `content` | 500 |
| `summary` | 500 |
| **both fields** | **1,000** (= `2 ×` the cap, exactly on the bound) |
| whole serialized payload | **1,226** |
| fixed overhead | ~226 |

That overhead is roughly constant, so it is a *larger* share at smaller caps and
a negligible one at the 25,000 default. An agent sizing purely against
`2 × BRAIN_SHOW_MAX_CONTENT_TOKENS` under-counts by it.

Markers are emitted **only when a cut actually happens**, so an ordinary payload
whose body and summary both fit comes back byte-identical. `summary_truncated_recovery`
points at the CLI (`brain show <id>`) deliberately: once the summary itself is
over the ceiling there is no smaller MCP mode left, and pointing back at
`summary_only` would be a loop.

### `BRAIN_RECALL_MAX_BUDGET_TOKENS` — why 13000 and not 32000

`brain_recall`'s MCP response ships every passage **twice**: once structured in
`passages[].text`, and again rendered inside `context_block`. Measured across
11 live queries at `budget_tokens=2000`, the delivered payload cost 4,025–4,726
tokens — **2.01×–2.36× the budget requested**. That range is scoped to *that
budget*; see "the ratio is a function of the budget" below before quoting it at
another one.

So `budget_tokens` sizes the content *selected*, not the response *returned*. A
ceiling of 32000 would deliver ~70–76k tokens, larger than the `brain_show`
tail these ceilings exist to cap. The intended bound is ~32k **delivered**, so
the accepted budget is `32000 ÷ 2.36 ≈ 13,500` → `13000`.

**The 2.36 divisor is an empirical constant from an 11-query sample, not a
law.** It comes from `docs/audits/2026-08-10-token-payload-baseline.json`,
produced by `scripts/token_payload_report.py` over
`scripts/token_payload_queries.txt`. The response also now returns
`payload_tokens` — the true serialized cost — so the overshoot is observable
rather than a document. When the duplication is removed, re-derive the divisor
or delete it and restore 32000; a stale divisor would then under-bound by half.

**Re-measured after the ceilings landed** (2026-08-13,
`docs/audits/2026-08-13-token-payload-after-wave3.json`, same 11 queries, same
live corpus): every query came in exactly **+8 tokens** — the additive
`payload_tokens` key and nothing else — moving the worst case from 2.3630 to
**2.3670**, a hair above the 2.36 the ceiling is derived from. The bound still
holds with margin: `13000 × 2.3670 = 30,771 ≤ 32,000`. It stops holding only if
a future measurement exceeds `32000 ÷ 13000 = 2.4615`.

**The ratio is a function of the budget, so the range is scoped to one.** End-to-end
QA (2026-08-20) measured **2.44×** — 1,466 delivered tokens at `budget_tokens=600`
on short synthetic passages — and read it as the documented range being wrong. It
is not: delivered ≈ `2 × used + overhead`, so the *ratio* is `2 + overhead ÷ budget`
and climbs as the budget shrinks. Same code, same mechanism: `r = 2.3670` at 2000,
`r = 2.4433` at 600.

The ceiling holds either way you do the arithmetic:

| check | value | verdict |
|---|---|---|
| substitute QA's ratio | `13000 × 2.4433 = 31,763` | ≤ 32,000 ✔ |
| adopt it as the divisor | `32000 ÷ 2.4433 = 13,097` | ≥ 13,000 ✔ |
| against the break point | `2.4433 < 2.4615` | ✔ |

The first row's margin looks thin (237 tokens, 0.7%) but it is the wrong sum:
applying a 600-budget ratio at a 13,000 budget over-states the fixed-overhead
term by 21.7×. **The range was scoped, not widened** — widening it to 2.44×
would imply that figure was measured under the same conditions and would move
the input the divisor is derived from for nothing. Re-derive only from a
measurement taken *at* the ceiling.

**It was measured at 2000 and is applied at 13000 — 6.5× away.** That
extrapolation errs safe: the overshoot is ~2× structural duplication plus a
roughly *fixed* JSON envelope, and a fixed envelope is a larger fraction of a
small payload, so the ratio should fall toward the ~2.0 duplication floor as
the budget rises. It has not been re-measured at 13000. If you change how a
passage renders, re-run the harness at the ceiling itself rather than trusting
that reasoning.

### `BRAIN_VECTOR_SIM_FLOOR` — why a good semantic match can rank last

The floor discards vector-leg candidates below `0.25` cosine similarity. It
buys precision: without it, the vector leg always returns *something*, so every
query gets nearest-neighbour filler whether or not anything is actually
relevant.

The cost is real and you will meet it. **Very short documents embed weakly**,
so a genuine semantic match can fall under the floor and score `0.0` on the
vector leg — surviving only on its lexical rank, or ranking first with no
vector contribution at all. Observed in QA: the query *"watering the plants"*
against a short *Garden Irrigation Plan* note. Nothing is broken; the document
simply had too little text to embed strongly.

If your corpus is mostly short notes and search feels blunt, lower the floor
(`0.15` is a reasonable next stop) and accept more filler. Raise it if you get
too many loosely-related hits. There is no universally right value — it is a
recall-versus-precision dial, and the default is tuned for a corpus of mostly
long-form documents.

### `BRAIN_SNIPPET_MAX_CHARS` — the constraint that actually decides snippet size

`BRAIN_SNIPPET_CONTEXT_TOKENS` looks like the knob that controls how much
snippet you get. On the live corpus it usually is not. Measured 2026-08-13 over
11 seeded queries x 5 results = 55 results:

- On **47 of 55 results (85.5%)** the **matched chunk alone** already reached
  `BRAIN_SNIPPET_MAX_CHARS`. The cap truncates the matched chunk itself, before
  any neighbouring-chunk context is consulted.
- Only **3 of 55** admitted any neighbour at all: the live median chunk is
  ~2,281 chars / ~570 tokens against the default 200-token context budget, so
  there is usually nothing that fits.
- Across those 55 results the expansion produced **30,727** tokens of snippet
  and delivered **19,213** — the cap discards **11,514 tokens (37.5%)** unread.

So if snippets feel truncated mid-thought, raise **this** knob first; raising
`BRAIN_SNIPPET_CONTEXT_TOKENS` alone will usually change nothing. Raising it
lengthens every snippet in every search result, which is a direct cost to an
agent's context window — the 37.5% discarded above is the headroom, not free
space. Lower it to shrink payloads at the cost of reading context.

`0` is **rejected** with a `ConfigError` at startup (it is parsed as a positive
integer), so unlike `BRAIN_SHOW_MAX_CONTENT_TOKENS` there is no "0 = unlimited"
opt-out; set a large value instead. `scripts/token_payload_report.py
--snippet-constraints` re-measures the numbers above on demand, and the full
write-up — including a removed mechanism that tried to attack the wrong
constraint — is in `brain.snippet_context`'s module docstring.

## Search and retrieval trust boundaries

Two of the variables above decide what leaves this machine, and one design
decision governs which surface shows what. All three need more than a
one-line table cell.

### `BRAIN_SECRET_GUARD` — ingest-time credential scanning

| Value | Behaviour |
|---|---|
| `warn` *(default)* | Findings are printed to **stderr**; the document is stored unchanged. `stdout` stays byte-identical, so scripts parsing it are unaffected. |
| `redact` | The matched span is replaced with `[REDACTED:<pattern-name>]` before storage. |
| `reject` | The ingest is refused and **nothing is written** — the guard raises before the content hash and before the write transaction opens. |
| `off` | No scanning. |

`--allow-secrets` bypasses the *action* for one invocation while still printing
findings, so you can force through a known false positive without disabling the
guard globally.

**`brain vault sync` is the exception: it defaults to `off`, not `warn`.** That
is deliberate. Sync has seven call sites (`brain vault sync`, the `--watch`
daemon, MCP, and others), and defaulting them to an active mode would have
silently changed all of their behaviour at once. Callers opt in by passing the
configured mode explicitly.

When sync *is* guarded, a refusal is **per-file, not fatal**: the offending
note is skipped, the walk continues, and the count surfaces as
`SyncReport.secrets_refused`. One credential-bearing note therefore cannot
abort a whole-corpus sync — and a report showing `3 files refused` is a
correct outcome, not a malfunction.

**`redact` on a vault-tier note refuses instead of redacting.** This looks
inconsistent and is the only correct behaviour. A vault note's file is the
source of truth, so redacting would store a clean body in the database while
your file on disk keeps the secret — and the file wins on the next sync, which
means the redaction silently undoes itself. Worse, you would believe the
credential was scrubbed. The fix is to edit the file, remove the secret, and
sync again; the refusal message says so.

### The CLI and MCP treat confidential documents differently, on purpose

`brain search`, `brain show` and `brain recall` all return confidential bodies
in full. The MCP tools of the same names withhold them unless the caller passes
`include_confidential=true`. **That asymmetry is the design, not an oversight
on the CLI side.**

The trust boundary runs between *your machine* and *a hosted model*, not
between you and your own corpus:

| Surface | Behaviour | Why |
|---|---|---|
| CLI | serves everything | You are reading your own notes on your own machine. Hiding them from you protects nobody and makes the tier unusable. |
| MCP | withholds by default | It feeds a model that may be hosted. This is the egress point F6 exists for. |
| Wiki | drops from the published index | The rendered site can be served to others. |

`brain search --sensitivity confidential` is therefore a **lens, not an access
control** — it answers "show me only what I've marked", the same way `--kind`
answers "show me only transcripts". `search_predicate.py` says so at the filter
itself.

> If you are about to make CLI `recall` withhold bodies "for consistency with
> MCP": don't. That is a regression dressed as hardening. The consistent thing
> is the *rule* — content leaves the machine only on an explicit opt-in — and
> the CLI is not a way off the machine.

### `BRAIN_AGENT_ID` — who is doing the work

Records the **actor**, as opposed to `source` (`cli` / `mcp` / `wiki`), which
records the **surface**. Two agents both working over MCP are indistinguishable
by `source`; this is what tells them apart in `brain usage`.

Must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$` — one alphanumeric character,
then up to 63 more alphanumerics, dots, underscores, colons or hyphens. Leading
punctuation is rejected so an id can never be mistaken for a CLI flag. A
malformed value fails at startup rather than being silently dropped, because a
silently dropped id shows up only as a permanently empty `brain usage` bucket.

Unset means **unattributed**, which is a first-class answer rather than a
failure: every row written before migration 027 is genuinely unattributed, and
`brain usage` renders that as `(unattributed)` instead of inventing an agent.

> **Read this before exporting it in a shell profile.** `BRAIN_AGENT_ID`
> attributes **everything** that process writes — including documents you
> ingest by hand with `brain ingest` and `brain ingest-dir`. Setting it
> globally will therefore mark manually-added documents as authored by that
> agent. `search_queries.agent_id` is ephemeral telemetry, but
> `documents.agent_id` is durable provenance on your own corpus, so wrong
> provenance there is the more expensive mistake. Prefer setting it per-process
> (`BRAIN_AGENT_ID=research-agent brain search …`) or in the agent's own
> environment rather than in `~/.zshrc`.

Per-invocation `--agent` overrides it on `search`, `recall`, `rate` and
`ingest-stdin`. Those four are the agent-facing surfaces; `brain ingest` /
`ingest-dir` deliberately have **no** `--agent` flag, because attaching an
explicit agent to a hand-run ingest would be a fabricated fact. The ambient
env var still applies to them — see the warning above.

## Design notes worth knowing before you change something

Four decisions that look arbitrary from the outside, are load-bearing, and
have each already cost someone an afternoon to rediscover.

**`regenerate_vault_file` refuses vault-tier rows, by design.** Ingested-tier
mirrors under `_ingested/` are generated *from* the database, so regenerating
one is safe. A vault-tier note is the opposite — the file is the source of
truth, and regenerating it from the DB could discard authored edits that have
not been re-synced. Any future "update a field, then re-mirror" command hits
this immediately. The pattern to copy is `cli_docs._set_sensitivity`: pre-check
`kind` from the database and branch — rewrite one frontmatter field in place
for vault tier, regenerate for ingested tier. Pre-check the tier rather than
catching the `ValueError` and matching its message; the message can be
rephrased, the tier cannot.

**`brain ui`'s assets live in a flat `static/`, superseding spec §2.1.** The
design document describes a nested tree; the shipped layout is flat
(`app.css`, `app.js`, `index.html`, `theme.js`). The code is correct and the
spec is stale.

**`MarkdownIt("commonmark")` does *not* default to `html=False`.** Verified on
markdown-it-py 4.2.0: the CommonMark preset turns raw HTML **on**, so the bare
preset renders a literal `<script>` tag straight through. `html=False` is
passed explicitly in `ui/render.py` and `tests/test_ui_render.py` asserts the
escaping rather than trusting the preset. The F14 design document states the
opposite — the document is wrong and the code follows the measurement. If you
construct a `MarkdownIt` anywhere else, pass the option.

**`confidential` and `draft` share a seam but not a guarantee.** Both are
frontmatter flags that the Quartz `contentIndex` emitter drops, so they look
interchangeable at the publish boundary. They are not. `draft` is a *publish
quarantine* — the document is still fully readable everywhere else, including
over MCP. `confidential` is an *egress control*: it additionally keeps the
body off a hosted embedder and withholds it from every MCP retrieval surface.
Treating one as a substitute for the other is a security mistake in one
direction and a usability annoyance in the other.

## Where config lives (`.env` resolution order)

`$BRAIN_HOME/.env` (default `~/.brain/.env`) is the **canonical** config
location. It is the only one a `pip` / `uvx` install can see: those users have
no checkout, and the `brain` console script on your `PATH` loads no environment
of its own.

`Config.load()` layers four sources, highest precedence first:

| # | Source | Notes |
|---|---|---|
| 1 | process environment | Never overwritten by any file. |
| 2 | `<repo>/.env` | Source checkouts only — resolved relative to the installed `brain/config.py`. |
| 3 | `<cwd>/.env` | Walk-up from the current directory. Only works while you stand in the right place. Drop it with `BRAIN_IGNORE_CWD_DOTENV=1`. |
| 4 | `$BRAIN_HOME/.env` | The canonical location. Honors `$BRAIN_HOME`; defaults to `~/.brain`. |

### `<vault>/.brain-fastpath-ignore` — the one config file that is not `.env`

Not an environment variable, and the only per-vault config file `brain` reads,
which is why it is easy to miss.

Body-only edits normally take the per-file fastpath (~2s to the UI). Whether an
edit qualifies is decided by a fingerprint over the document's *structural*
frontmatter keys; every other key has to be classified first. A key `brain`
does not recognise **fails closed** — the edit is forced down the full-build
path, which is correct but slow. Generic keys ship in the package. Keys
specific to your own vault do not, deliberately: they used to, and shipping one
vault's namespaced identifiers inside a public PyPI wheel served nobody else.

So a vault with its own frontmatter conventions silently loses the fastpath
until its owner declares those keys. Add a `.brain-fastpath-ignore` file at the
**vault root** — not in `.quartz/`, which `brain wiki install --force` deletes:

```
# one frontmatter key per line; '#' starts a comment
acme_ledger_id          # a literal key
acme_sweep_*            # a glob covers a whole namespaced family
```

Globs (`*`, `?`, `[...]`) match case-sensitively. The file **adds to** the
shipped defaults and never replaces them. An absent file is the documented
zero-config state, not an error; a present-but-unreadable one forces a full
build rather than trusting a stale fingerprint. Only list keys that genuinely
cannot change rendered HTML — that is the promise the fastpath is keeping on
your behalf.

Implementation: `src/brain/wiki/ignored_fields.py`. Design rationale:
`docs/specs/2026-05-09-fastpath-fingerprint.md`.

### `BRAIN_IGNORE_CWD_DOTENV`

The cwd walk-up climbs to the filesystem root, so a process started in an
arbitrary directory can pick up an unrelated `.env` and silently talk to the
wrong database. That is fine for interactive use — it is what makes a checkout
work from any subdirectory — but it is a liability for anything long-running
and unattended, where the failure is invisible rather than loud.

Set `BRAIN_IGNORE_CWD_DOTENV=1` (accepted: `1`, `true`, `yes`, `on`) to remove
link 3 from the chain entirely. **Background daemons should set it**, alongside
an explicit `BRAIN_HOME`, so their config resolution does not depend on where
they happened to be started.

The link is *removed*, not merely deprioritized: it disappears from
`dotenv_chain()` too, so `brain doctor` and the error message report exactly the
files that were really consulted. Resolution is never auto-detected from
context (`isatty`, "am I a daemon?") — that would make which database you talk
to depend on whether stdout is a pipe, which is the same class of invisible,
environment-dependent divergence this section exists to prevent.

`brain setup` provisions link 4 for you, and `brain init` repairs it when it can:

* **Fresh install** — a real `.env` is written at `$BRAIN_HOME/.env`.
* **Dev checkout** — `$BRAIN_HOME/.env` becomes a **symlink** to the repo
  `.env`. Deliberately not a copy: a copy would put `VOYAGE_API_KEY` /
  `DATABASE_URL` on disk twice and the two would silently drift. The trade-off
  is that moving the checkout breaks the link — `brain doctor` reports that
  explicitly as a dangling symlink rather than as "missing".
* **Re-running either command never overwrites** an existing `$BRAIN_HOME/.env`,
  file or symlink.

When `DATABASE_URL` cannot be resolved, the error prints every path that was
searched with its state, so you can see which link of the chain is dead:

```text
DATABASE_URL is not set.
No .env file was found in any of:
  /Users/you/src/second-brain/.env  (missing)
  /Users/you/somewhere/.env         (missing)
  /Users/you/.brain/.env            (missing)
Run `brain setup` to create /Users/you/.brain/.env (or export DATABASE_URL).
```

A file that *was* found and loaded but lacks the key reports that instead —
a different fault with a different fix. `brain doctor` checks the same chain
via `brain.config.dotenv_chain()`, so the two can never disagree.

## Session-end capture hook (`BRAIN_HOOK_*`)

> **These four are real environment variables, not `.env` entries.** Putting
> them in `.env` has no effect.

The Claude Code Stop hook runs as a short-lived subprocess on every session
end, and it must work when the database is down, when `DATABASE_URL` is unset,
and before any `.env` exists. So it reads the process environment directly:
`Config.load()` would demand `DATABASE_URL`, and even the minimal loader would
pay dotenv resolution on every Stop event. Both would couple the hook to
infrastructure it is specifically designed to survive the absence of.

Set them where the hook process will actually see them — your shell profile,
or the `env` block of the hook entry in `settings.json`.

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_HOOK_ENABLED` | enabled | Set to a falsey value (`0`, `false`, `no`, `off`) to disable the hook without unregistering it. |
| `BRAIN_HOOK_MIN_TOOL_CALLS` | `12` | Sessions below this floor with no file mutation read as a lookup rather than work, and are skipped. The one threshold here with no principled derivation — it is overridable precisely so someone who finds it noisy raises it instead of turning the hook off entirely. |
| `BRAIN_HOOK_TRANSCRIPT_MAX_BYTES` | `8388608` (8 MiB) | Above this, only the tail window is scanned, so one enormous transcript cannot stall session exit. |
| `BRAIN_HOOK_SENTINEL_TTL_DAYS` | `7` | How long the per-session sentinel file is kept before cleanup. |

## Claude integrations

The `brain` CLI and the `brain-mcp` MCP server are harness-agnostic — any agent
that can run a shell command or speak MCP can use the corpus. The two worked
integrations below are the ones I run: an MCP server (Claude Desktop, or any
MCP-compatible client) and a skill (Claude Code). Both call the same underlying
`brain` CLI.

### Claude Desktop (MCP server)

#### Configuration

The `brain-mcp` binary exposes Brain as an [MCP](https://modelcontextprotocol.io/)
server so Claude Desktop — or any other MCP-compatible client — can search,
save, author notes, inspect links, and edit entries during a chat — no
terminal required. For the full step-by-step (symlink, boot smoke-test, and
troubleshooting), see
[docs/guides/claude-desktop-setup.md](guides/claude-desktop-setup.md).

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`. The `env` block must match whichever backend you set in `.env` — pass `BRAIN_EMBEDDER` and the backend-specific knobs (Ollama host for `arctic`/`qwen3`, `VOYAGE_API_KEY` for `voyage`). `BRAIN_VAULT_PATH` is optional when you use the default `~/brain-vault`, but setting it explicitly makes Desktop behavior match the CLI even if the repo moves later (replace `/Users/you` with your home directory):

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/you/workspace/second-brain/.venv/bin/brain-mcp",
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:55432/second_brain",
        "BRAIN_EMBEDDER": "arctic",
        "OLLAMA_HOST": "http://localhost:11434",
        "BRAIN_VAULT_PATH": "/Users/you/brain-vault",
        "BRAIN_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

For the Voyage backend, swap the embedder-specific keys: `"BRAIN_EMBEDDER": "voyage"` and `"VOYAGE_API_KEY": "<paste here>"`.

#### MCP tools

| Tool | What it does |
|---|---|
| `brain_search` | Hybrid search with `source`, `tag`, `since_days`, and `fts_only` filters. |
| `brain_show` | Return one full document by 6+ character id prefix. |
| `brain_list` | Browse recent documents, optionally filtered by `source` or `tag`. |
| `brain_status` | Counts, last-ingest timestamp, and by-source breakdown. |
| `brain_resurface` | Spaced-repetition review queue: older, unrevisited docs scored by age, access staleness, and importance. Params: `limit`, `min_age_days`, `source_kind`. |
| `brain_ingest_stdin` | Save text from a chat or another MCP result; auto-tags `source-mcp`. |
| `brain_tag` | Add/remove tags on an existing document and rewrite mirror frontmatter when present. |
| `brain_edit` | Update title, content type, body, or metadata; body edits re-embed. |
| `brain_backlinks` | List documents that link to a document. |
| `brain_links` | List outgoing links, optionally including unresolved refs. |
| `brain_orphans` | List docs with no incoming or outgoing links. |
| `brain_note_new` | Create a vault note from chat content without opening `$EDITOR`; auto-tags `source-mcp`. |
| `brain_daily` | Resolve or create a daily note for a date. |
| `brain_link_proposal` | Propose a `[[link]]` from one vault note to another without writing files. |
| `brain_brief` | Daily digest: recent captures, open todos, pins, best-effort next steps. Params: `since_hours`, `todo_since_days`, `no_enrich`. Titles + todo texts only. |
| `brain_review_weekly` | Synthesize a week's activity into a review page. Params: `week` (ISO `YYYY-Www`), `no_graph`, `emit`. |
| `brain_review_scan` | Run a contradiction / staleness scan into the review queue. Params: `scan_type` (`conflicts`\|`stale`\|`all`), `dry_run`, `limit`. |
| `brain_review_findings_list` | Read the contradiction + staleness queue without scanning. Params: `kind` (`all`\|`conflicts`\|`stale`), `limit`. |
| `brain_timeline` | How a theme/entity evolved over time. Params: `query`, `person`, `granularity`, `since`, `until`, `limit`, `synthesize`, `tenant`. Needs the graph layer. |
| `brain_ask` | Agentic multi-hop cited answer synthesis. Params: `question`, `mode` (`hybrid`\|`auto`\|`fuse`\|`local`), `no_loop`, `limit`, `max_iterations`. Snippets only. |
| `brain_connect_list` | List auto-link suggestions. Params: `status` (`pending`\|`accepted`\|`rejected`\|`all`), `limit`. |
| `brain_connect_accept` | Accept a suggestion; with `write=True` append the wikilink. Params: `id` (6+ char prefix), `write`. |
| `brain_connect_reject` | Reject a suggestion; frozen and never re-proposed. Param: `id` (6+ char prefix). |
| `brain_gaps` | Surface knowledge gaps from repeated search failures. Params: `since_days`, `limit`, `push` (upsert into the elicitation queue). |
| `brain_graphrag_search` | Graph retrieval over the entity graph. Modes: `auto` (default) / `local` / `themes` / `global` / `fuse`. Returns a `GraphContext` with entities + scored docs. |
| `brain_graphrag_themes` | "Themes in my conversations with X" — required `person` arg. Returns ranked theme groups. |
| `brain_graphrag_entity` | One entity's co-occurrence neighbourhood. |
| `brain_graphrag_build` | Backfill or force-rebuild the graph from existing documents. Idempotent + resumable. |
| `brain_graphrag_communities_build` | Detect + summarize communities (Louvain over the entity graph). Required for `--mode global`. |

#### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | (required) | Postgres connection string. Same value used by the CLI. |
| `BRAIN_EMBEDDER` | `arctic` | Embedder backend: `arctic`, `voyage`, or `qwen3`. Must match the dim baked into the database by `brain init`. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL. Used by `arctic` and `qwen3`; ignored by `voyage`. |
| `QWEN3_MODEL` | `qwen3-embedding:8b` | Ollama model tag for the qwen3 backend. |
| `VOYAGE_API_KEY` | (required for `voyage`) | Voyage AI key. Ignored by the local backends. |
| `BRAIN_VAULT_PATH` | `~/brain-vault` | Vault folder for authored notes, ingested mirrors, wiki rendering, and MCP note tools. |
| `BRAIN_USER_EMAIL` | unset | Owner email used by the rendered Gmail thread view's "Show only my replies" filter. Set it before wiki builds if you use that filter. |
| `BRAIN_MCP_LOG_LEVEL` | `INFO` | Stderr log level. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`. Unknown values fall back to `INFO`. |
| `BRAIN_GRAPH_ENABLED` | `true` | Enable people-aspect graph sync at ingest. On a stock pgvector DB the sync is a best-effort no-op (never raises). |
| `BRAIN_GRAPH_CONCEPTS` | `true` | Enable concept-aspect extraction (LLM entity extraction over topics/projects/orgs/tools). Requires `BRAIN_GRAPH_EXTRACT_MODEL` to be pullable via Ollama. |
| `BRAIN_GRAPH_TENANT` | `default` | Tenant id stamped on every graph row, vertex, edge, and query. Single-user local deployments leave it at the default. |
| `BRAIN_GRAPH_EXTRACT_MODEL` | `llama3.1:8b` | Ollama model used by the concept extractor. Any JSON-mode-capable model pullable via `ollama pull <name>`. |

(See `.env.example` for the full ~18-knob `BRAIN_GRAPH_*` set covering traversal caps, community detection, and concept-extraction tuning — all with sensible defaults.)

#### What to expect

After saving the config and fully quitting/reopening Claude Desktop, the
`brain_*` tools become callable in any chat. Ask "search my brain for the Q1
review with [person]" and Desktop can call `brain_search`; ask "make a daily
note for today with these bullets" and it can call `brain_daily` /
`brain_edit`. Server startup is ~0.5–1.5s; the first search may also pay the
embedder cold start cost (Ollama loading the model, or the Voyage
SDK/network path warming up). Logs go to stderr and are surfaced by Claude
Desktop if a tool call fails.

### Claude Code (bundled skills)

For Claude Code (the CLI), this repo ships eight skills under `skills/` that
teach Claude when and how to reach for each brain feature. Install them with
the bundled sync script, which enumerates `skills/` directly rather than
carrying a hardcoded list — so a skill added to the repo is never missed:

```bash
bin/brain-skills-sync                # copy-install every skill into ~/.claude/skills
bin/brain-skills-sync --check        # report drift, copy nothing (exit 1 if any are stale)
bin/brain-skills-sync --dest <dir>   # install somewhere else ($BRAIN_SKILLS_DEST also works)
```

It touches only the brain-family skills, never anything else in
`~/.claude/skills/`, and is idempotent — each skill reports `installed`,
`updated`, or `unchanged`. The install is a **copy**, not a symlink, so it is
predictable across upgrades; the trade-off is that editing a skill in the repo
does not take effect until you re-run the script. `--check` is the drift guard:
after pulling, it exits non-zero and names any skill the repo has moved ahead on.

| Skill | Reach for it when the user… |
|---|---|
| `consult-brain` | asks about their own history, wants a quote or writing in their voice, or wants one cited multi-hop answer (`brain ask`) or an audio overview (`brain audio`) |
| `brain-graph` | asks about themes, patterns, or connections across interactions, or wants to enumerate the entity graph (also owns `brain owner`) |
| `brain-proactivity` | asks what to look at, what they missed, what has gone stale, what should be linked, how a theme changed over time, or wants to quick-capture a thought |
| `brain-authoring` | creates, renames, edits, or tags a note, or asks about the wikilink graph (`backlinks` / `links` / `orphans` / `graph`) |
| `brain-maintenance` | asks whether the brain is healthy, or needs `setup`, `demo`, re-embedding, backfills, or the `eval` harness |
| `brain-todo` | asks what is on their plate, or wants Krisp action items pulled in |
| `ingest-brain` | wants a file, directory, Gmail thread, Krisp transcript, or Slack thread added to the brain |
| `elicit-brain` | wants tacit knowledge surfaced — "what do I know that I haven't written down" |

`consult-brain` is the one you most likely want first. The MCP server above
covers Claude Desktop; these skills cover Claude Code. For the full map — and
the difference between this script and `brain claude install-skill` — see
[Agent skills](agent-skills.md).

### Example prompts

A snippet in `~/.claude/CLAUDE.md` tells every Claude Code conversation:
- When to invoke `brain search` and `brain show` (career topics, interviews, past meetings, prior roles, deals)
- When to reach for GraphRAG instead (`brain graphrag themes --person X`, `brain graphrag search ... --mode global|fuse`) — questions about themes, patterns, or connections across the corpus, where the answer lives in the *relationships* between docs rather than any single doc
- How to orchestrate Krisp/Slack ingestion via MCP → `brain ingest-stdin`
- That `--json` output is available for programmatic parsing

Once your corpus is ingested, you can ask your agent things like:

**Recall past conversations**
- "What did I tell the design team about the new onboarding flow?"
- "Summarize my last three 1:1s with my manager."
- "Did I ever discuss pricing with the Acme account? Pull the relevant threads."

**Find decisions and rationale**
- "When did we decide to drop the legacy mobile client, and why?"
- "What was the argument for picking Postgres over DynamoDB on the platform team?"
- "Find the meeting where we agreed on the Q3 hiring plan."

**Meeting and interview prep**
- "I have a call with Acme tomorrow — brief me on everything I've discussed with them."
- "Pull stories from my notes about cross-functional leadership for an interview."
- "What examples do I have of resolving production incidents?"

**Draft in your voice**
- "Draft a follow-up email to the candidate I interviewed last Tuesday, in my voice."
- "Write a Slack update about the migration status, matching how I usually write."
- "Help me outline a talk on hybrid search using examples from my own work."

**Cross-source synthesis**
- "Pull every mention of the data warehouse migration across Slack, Krisp, and email, then summarize where it stands."
- "What's the through-line in my notes about engineering culture over the past year?"

**Themes and connections (GraphRAG)**
- "What themes keep coming up in my conversations with [person]?" → `brain graphrag themes --person ...`
- "What connects the platform-migration thread to the hiring-plan thread?" → `brain graphrag search "..." --mode fuse`
- "Map out everyone connected to [project] and the topics that pull them together." → `brain graphrag search "[project]" --mode local` then `brain graphrag communities list`
- "Which clusters of people and topics dominate my notes overall?" → `brain graphrag communities build` then `brain graphrag search "..." --mode global`
- "Show me the entity graph around [topic] at depth 2." → `brain graphrag entity "[topic]"`

**Elicit tacit knowledge** (`brain elicit` — needs Ollama)
- "What do I know that I haven't written down?" → `brain elicit list` then `brain elicit`
- "Surface my knowledge gaps." → `brain elicit list --json`
- "Interview me about my engineering culture principles." → `brain elicit --target "engineering culture"`
- "Draft the unwritten rules I keep referencing in meetings." → `brain elicit --signal delta`
- "What implicit knowledge do I have about [person/project]?" → `brain elicit --target "[name]"`

**Ingest on demand** (the agent orchestrates the MCP calls)
- "Ingest last week's Krisp calls."
- "Pull the Slack thread about the auth incident into my brain."
- "Ingest emails from the recruiting@ alias from the past 30 days."

The pattern: ask the question naturally — the agent decides whether to call `brain search` (single-doc lookup, ranked by lexical + semantic similarity) or `brain graphrag …` (themes / patterns / connections traversed via the entity graph), which filters to apply (`--source`, `--tag`, `--since`, `--person`, `--mode`), and when to follow up with `brain show` for full context.

## Choosing an embedder backend

Set `BRAIN_EMBEDDER` in `.env` (or the shell). Three vector backends are supported, plus `none` for an FTS-only brain:

| Value | Model | Dim | Cost | Setup | Notes |
|---|---|---|---|---|---|
| `arctic` *(default)* | Snowflake Arctic Embed v2 (Apache 2.0) | 1024 | Free | Ollama + `ollama pull snowflake-arctic-embed2` (Ollama packages `Snowflake/snowflake-arctic-embed-l-v2.0` from Hugging Face under this shorter tag — same model) | Recommended. Strong retrieval quality on personal text; HNSW-indexable; fully local. |
| `voyage` | Voyage AI `voyage-3.5` | 1024 | ~$0.06/M tokens | `VOYAGE_API_KEY` in `.env` | Highest quality on long-form text; corpus leaves your machine. |
| `qwen3` | Qwen3-Embedding-8B (Alibaba) | 4096 | Free | Ollama + `ollama pull qwen3-embedding:8b` | Local. Native 4096 dims exceeds pgvector's HNSW cap (2000 for `vector`) so search uses sequential scan — fine at <100K chunks but slower than `arctic`. China-origin model — judge accordingly. |
| `none` | — (no vector leg) | — | Free | No Ollama, no API key | FTS-only brain. `brain search` runs pure Postgres full-text ranking; no embeddings are computed. Fastest to stand up (see the [README quick start](../README.md#quick-start) `minimal` profile). |

The active backend is reflected in `brain init` ("embedder arctic (dim=1024)") and `brain doctor` (the embedding-column line shows the column type and whether `[hnsw]` is present).

## Switching embedder backends

**Switching is destructive.** The chosen backend's native dim is baked into the `chunks.embedding` column on the first `brain init`, and existing embeddings cannot be re-projected to a different model — the chunks must be re-embedded from their original text. The CLI refuses to swap dims silently when chunks already exist; instead, do a full reset:

```bash
# 1. Stop the database and delete the data directory (chunks are wiped).
docker compose down
rm -rf data/postgres

# 2. Pick the new backend in .env (or via shell env var).
#    BRAIN_EMBEDDER=qwen3   # for example

# 3. Start fresh and re-ingest.
docker compose up -d
brain init                       # column shaped to the new backend's dim
brain ingest-dir ~/Documents/career   # or whatever your ingest sources are
brain reembed                    # finalizes NOT NULL + (for dim ≤ 2000) HNSW index
brain doctor
```

`docker compose down -v` is **not** sufficient — Postgres data lives in `./data/postgres` (a host bind-mount), not a Docker-managed volume. The `rm -rf data/postgres` step is what actually wipes the corpus.

## Data hygiene backfills

These commands are for cleanup after older imports or tag taxonomy changes:

```bash
brain backfill normalize-tags --dry-run
brain backfill normalize-tags
brain backfill normalize-tags --mapping ./tag-map.json

brain backfill source-rows --dry-run
brain backfill source-rows
brain vault export --to ~/brain-vault --force   # after source-rows
```

`normalize-tags` lowercases, hyphenates, dedupes, and rewrites both DB tags and
frontmatter tags when a mirror file exists. The optional mapping file is a JSON
object like `{"recruiters": "recruiter", "artificial-intelligence": "ai"}`.
`source-rows` is only for legacy Markdown rows with `source_id IS NULL`; fresh
installs should not need it.

## Uninstall

```bash
# 1. Tear down the runtime (launchd plists, $BRAIN_HOME files, Docker compose).
#    By default this KEEPS the database at $BRAIN_HOME/data/postgres/ and the
#    vault at $BRAIN_VAULT_PATH — both are user data.
brain uninstall

# 2. (Destructive — opt in explicitly.) Also remove the DB and/or vault:
brain uninstall --remove-db --remove-vault   # --remove-db requires a typed
                                             # confirmation: "yes, delete my data"

# 3. Remove the pipx-installed CLI itself (must be a separate command —
#    a Python CLI can't safely uninstall its own running process).
pipx uninstall secondbrain-py
```
