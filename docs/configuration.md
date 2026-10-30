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

# 8. Sanity check. Optional integrations (gws) print warnings when absent.
brain doctor
```

If `brain doctor` complains, the usual suspects are: Docker isn't running, the
container hasn't finished starting yet (give it ~10 seconds and retry), Ollama
isn't running (`brew services start ollama`), the configured embedding model
isn't pulled (`ollama pull snowflake-arctic-embed2`), or — only when
`BRAIN_EMBEDDER=voyage` — `VOYAGE_API_KEY` is missing from `.env`. Missing
`gws` only disables Gmail ingest and Google directory refreshes. GraphRAG health (AGE extension presence,
graph-drift counters, community-materialization staleness) is a soft check — a
stock pgvector DB is reported as a warning, not a failure, and doesn't flip the
exit code.

## Running `brain` from any directory

By default, `brain` and `brain-mcp` only work inside the repo folder with the
venv activated, and the `bin/brain-up` / `bin/brain-down` / `bin/brain-rebuild`
/ `bin/brain-status` scripts have to be invoked by full path. `brain setup`
installs shims automatically; if you set up from source, two small one-time
edits fix the CLI and `bin/` scripts (the MCP symlink is optional but useful for
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
brain-status         # → watcher status (stopped initially)
```

Once both are on PATH, the daily flow is just `brain-up` / `brain-down` —
no need to remember watcher invocations.

## Feature config knobs

All have sensible defaults; set them in `.env` (see `.env.example`). The
`brain review` knobs cover both the weekly report and the scan engine. These
tune the [Proactivity and synthesis](cli-reference.md#proactivity-and-synthesis)
commands.

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_OLLAMA_NUM_CTX` | `8192` (grows to fit raised caps when unset) | Context window every Ollama chat call requests (`options.num_ctx`) — `brain ask`, `brief`, `audio`, `review`, `timeline --synthesize`, `gaps`, enrichment and GraphRAG extraction all share it, so the model loads once. Set explicitly, it must hold every capped call (`BRAIN_ENRICH_MAX_INPUT_TOKENS`, `BRAIN_AUDIO_MAX_INPUT_TOKENS`, the extract chunk) or the brain refuses to start, naming the knob. Full description in the [MCP env table](#environment-variables). |
| `BRAIN_OLLAMA_EMBED_NUM_CTX` | `2048` | Context window the Ollama embedders (`arctic`, `qwen3`) request on every `/api/embed` call. It is separate from `BRAIN_OLLAMA_NUM_CTX` because the embedder is a different model with its own runner. It must be at least `1400` (the 700-token chunk ceiling ×2 for the model's tokenizer) or the brain refuses to start. An input that does not fit is refused by Ollama (`OllamaEmbedContextOverflow`), never silently embedded as its prefix. No effect under `voyage` / `none`, but still validated at startup. Full description in the [MCP env table](#environment-variables). |
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
| `BRAIN_AUDIO_MAX_INPUT_TOKENS` | `3000` | Max grounding tokens fed to the script model. Raising it past `3072` needs a larger context window: automatic when `BRAIN_OLLAMA_NUM_CTX` is unset, a startup error naming the knob when it is set explicitly too small. |
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

### `BRAIN_RERANKER` — the cross-encoder reranker (off by default)

A second ranking stage that reorders the head of every search (`brain search`,
`brain explain`, MCP `brain_search` and `brain_explain`, `brain ui`, `brain
recall`, `brain ask`) by a cross-encoder's score. It ships **off**, and stays
off until `brain eval` on your own corpus shows it helps (roadmap ticket RM-14c).
These knobs live in the feature settings leaf `brain/settings/rerank.py`.

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_RERANKER` | `none` | `none`, `llamacpp` (a local `llama-server` from llama.cpp, serving `bge-reranker-v2-m3`), or `voyage` (hosted). |
| `BRAIN_RERANK_DEPTH` | `20` | How many top results are rescored, 2 to 50. The rest keep their order after them. |
| `BRAIN_RERANK_TIMEOUT_S` | `5.0` | Per-call timeout, 0.1 to 120 seconds. On a timeout the results stay in hybrid order. |
| `BRAIN_RERANK_URL` | `http://127.0.0.1:8081` | Where `llama-server` listens. **Loopback only** (`127.0.0.1`, `::1`, `localhost`): the passages are your documents' text, so any other host is a `ConfigError`. The opt-in `com.brain.reranker` job binds `127.0.0.1` at this URL's port; for it the URL must be `http://127.0.0.1:PORT` or `http://localhost:PORT`, PORT in 1024–65535 (read by `brain-install-launchd`). |
| `BRAIN_RERANK_MODEL` | `bge-reranker-v2-m3` (llamacpp), `rerank-3` (voyage) | The model name sent with each request. |
| `BRAIN_RERANK_MAX_CHARS` | `1500` | Each passage is the title, a newline and the snippet, cut to this many characters (100 to 20,000). |
| `BRAIN_RERANK_GGUF` | `$BRAIN_HOME/models/bge-reranker-v2-m3-Q8_0.gguf` | The model file the opt-in `com.brain.reranker` LaunchAgent serves; an absolute path (or `~/…`). Read by `brain-install-launchd` (from the shell, else `$BRAIN_HOME/.env`), not by searches; re-run it after a change. |

`voyage` uses `VOYAGE_API_KEY`, the same key the Voyage embedder reads, and is a
`ConfigError` without it. It never receives a **confidential** document: those
rows are not sent and keep their places in the list. The local backend sees them,
because nothing leaves the machine.

`llama-server` must be started with `--embedding --pooling rank --reranking`
(the request is `POST {BRAIN_RERANK_URL}/v1/rerank`). Installing it, the tested
version and model, its measured memory, and the opt-in launchd job
(`brain-install-launchd --with-reranker`) are in
[docs/guides/local-reranker.md](guides/local-reranker.md). `brain doctor`'s
`reranker` line probes `{BRAIN_RERANK_URL}/health` while the backend is
`llamacpp`.

A bad value in any of these is a `ConfigError` the first time a search runs,
even while `BRAIN_RERANKER=none`, as with every other knob. `brain eval` records
the backend, model and depth in its `config_signature` only while the reranker is
on, so a baseline recorded with it off stays valid, and each row of `brain eval
--json` carries the stage's time in `stage_ms` (not written into baselines). If
the reranker skips for a query during `brain eval` (a timeout, or it is not
running), that row names it in `stages_skipped`, and the run is refused like one
with a failed embed: `--record-baseline` writes nothing and `--fail-below` exits
1. Start the reranker and run it again.

### `BRAIN_WATCH_MIN_COSINE` — saved searches matched by meaning

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_WATCH_MIN_COSINE` | `0.45` | How similar a document must be (cosine, 0 to 1) for a `--semantic` watch to report it (`brain watch`, roadmap ticket RM-15). Watches that match by words, the default, ignore it. |

It lives in the feature settings leaf `brain/settings/watch.py`. A value outside
0 to 1 is a `ConfigError` naming the variable, raised only when a semantic watch is
checked: it skips those watches and never stops `brain brief`.

### `BRAIN_OCR` — OCR for image-only PDFs

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_OCR` | `auto` | `auto` OCRs an image-only PDF when the `tesseract` binary is on `PATH`; `off` never does. Roadmap ticket RM-32. |
| `BRAIN_OCR_LANG` | `eng` | tesseract's `-l` argument: one or more language codes joined by `+` (`eng+deu`). Each code is letters, digits and underscores, so the value can never be read as an option. Codes other than `eng` and `osd` need `brew install tesseract-lang`. |
| `BRAIN_OCR_MAX_PAGES` | `50` | How many pages of one PDF are rendered and recognised, 1 to 1,000. Later pages are not read. |

They live in the feature settings leaf `brain/settings/ocr.py`. A PDF is OCR'd
only when its text layer averages under 25 characters a page **and** a page
within the cap (the first `BRAIN_OCR_MAX_PAGES`) carries an image object. So a
PDF with a real text layer (25 or more characters a page on average), or with no
image within the cap, is never OCR'd; a sparse one that carries an image is, and
the OCR text replaces its text layer. One with a full text layer never reads
these knobs, and with
`BRAIN_OCR=off` the file is never opened by the second PDF parser. Each page is
rendered at 200 dpi in grayscale (`pypdfium2`, PNG via Pillow) and passed to `tesseract stdin
stdout -l <lang>`, with a 60-second limit per page; a page that times out or
fails is skipped with a class-only warning.

A PDF is untrusted input, so two more bounds are fixed in code. A page renders to
at most 70 million pixels, counted as pdfium allocates them (each side rounded
up), with no side longer than 32,767 pixels (tesseract's limit); A0 at 200 dpi is
about 62 million. A larger page is rendered at a lower resolution, and one that
would fit only below 50 dpi, whose long side would not fit, or whose short side
would be under one pixel, is skipped, so a forged page size (a huge or a very thin
MediaBox) cannot exhaust memory. Embedded images are bounded too, because pdfium
decodes each at its own size. Before the file reaches pdfium, every image stream in
the document is sized from its header, without decoding: masks (with or without
`/Subtype /Image`), annotation appearances, patterns and Type3 glyphs included.
If any one image declares more than 280 million pixels, or one cannot be sized,
the document is not OCR'd, with one warning. The scan itself is bounded: a
cross-reference whose entries alias one file offset is refused, and so is a walk
past 100,000 objects, 512 MiB of objects or 10 seconds. The bound is per image, so a long scan with one page-sized image a
page is unaffected. Each page's drawn images are also summed against the same
bound, and annotations and form fields are not drawn.

**Known residuals.** An inline image (`BI ... EI`) is not a stream object, so the
document scan does not see it. On a page, pdfium counts it; inside a Type3 glyph or
a tiling pattern it is in neither count. A `/JPXDecode` codestream carries its own
size, which pdfium may decode instead of the declared one; that was not measured.
And pypdf's own parse of a hostile file (deep arrays, for one) is not bounded by
any of this; it predates OCR. The systemic fix is a process-level memory and time
guard (backlog BL-32). One document gets at most
10 minutes of OCR in total; at the deadline OCR stops, keeps the pages read so
far, and logs a count. This matters because `brain ui` ingests on one job worker
that cannot be cancelled. The text is stored
as `[page N]` blocks, and the document's `metadata.ocr` records
`{"pages": n, "engine": "tesseract"}`. OCR runs locally, and the secret guard
scans the recognised text like any other body.

Without `tesseract`, an image-only PDF still ingests as an empty document
(`skipped (empty document)`) and one warning says how to install it. `brain
doctor`'s `ocr` line reports the tesseract version and checks that every
`BRAIN_OCR_LANG` language is installed; a missing binary is OK, not a warning,
because OCR is optional. Measured with tesseract 5.5.3 (leptonica 1.87.0) from
Homebrew on 2026-10-30.

### `drop-folders.toml` — drop folders (RM-33)

No environment variable: the folders `brain ingest-folder --configured` reads are
`[[folder]]` tables in `$BRAIN_HOME/drop-folders.toml`, read by
`brain/settings/drop_folders.py`. A missing file means no folders.

| Key | Default | Purpose |
|---|---|---|
| `path` | (required) | The folder. Absolute or starting with `~`; stored resolved. A folder that does not exist (an unplugged volume) is skipped with a warning naming its entry; the others still run. |
| `kind` | `screenshot` | The source kind its documents get; any kind `brain ingest-stdin --source` accepts. |
| `extensions` | `[".png", ".jpg", ".jpeg"]` | Which files are taken; each must be one an extractor reads. |
| `sensitivity` | `confidential` | `normal` or `confidential`. A screenshot can show anything, so the default is confidential. |
| `auto` | `true` | `false`: only `brain ingest-folder --dir PATH` uses the folder, never `--configured`. |
| `mode` | `ingest` | The only mode; `capture` arrives with RM-39 and is refused until then. |

An unknown key, a bad value, or two entries for one folder is a `ConfigError`
naming the entry and key (`folder[1].kind`), never the value. Images are read
with the `BRAIN_OCR*` knobs above. The opt-in LaunchAgent is
`brain-install-launchd --with-dropfolders`; see
[guides/drop-folders.md](guides/drop-folders.md).

### `BRAIN_BRIEF_DELIVERY` — send the daily brief to yourself

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_BRIEF_DELIVERY` | `none` | Where `brain brief --wiki` (the 07:00 job) sends the brief once the page is written: `none`, `email` (through `gws gmail +send`, to `BRAIN_USER_EMAIL` only) or `slack` (an incoming webhook). Roadmap ticket RM-16. |
| `BRAIN_BRIEF_SLACK_WEBHOOK_URL` | unset | The Slack incoming-webhook URL, needed by `slack`. **A secret** (its path is the credential): it must start `https://hooks.slack.com/` (that exact host, no port, no credentials, no whitespace or control characters, at most 65,536 characters), it is never logged, and `brain doctor` says only that it is set. Bind it to a private channel or a DM, never a shared channel. |
| `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS` | `false` | Also send the open todo items and the suggested next steps. Both come from document text, so they are a second opt-in; by default the message carries the todo count only. |

They live in the feature settings leaf `brain/settings/brief_delivery.py`.
`email` needs `BRAIN_USER_EMAIL` (one address, no list, not starting with `-`)
and `gws` on `PATH`; the subject is always `[brain brief] YYYY-MM-DD`, so a mail
filter can label it, and `brain ingest-gmail` never ingests your own copy back.
What is sent is the published page's content, built with confidential documents
excluded, and with draft documents removed: the date, counts, capture and pinned
titles, the watch hits, and the "New this week" entity names. The Slack post
turns link previews off. With
`BRAIN_BRIEF_DELIVERY` unset or `none`, the other two knobs are not read by the
brief at all. A bad value with delivery on (an unknown channel, a non-Slack URL,
`email` without one `BRAIN_USER_EMAIL`) does not stop the brief: it logs one
warning, and `brain doctor`'s `brief delivery` row names the variable.
A failed send is one warning too, and the watch hits it carried stay unnotified,
so the next brief sends them again. If `gws` is not authorized to send, run
`bin/gws-reauth`.

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
| Generated vault pages | gated (People Hub, `brief --wiki`, the weekly review) | The vault is plain files that sync and get shared; a confidential body or title should not land in a page brain writes unprompted. |
| `brain ui` | serves everything on loopback; withholds bodies on a LAN bind unless started with `--include-confidential` | A LAN bind can be reached by other devices. |

`brain search --sensitivity confidential` is therefore a **lens, not an access
control** — it answers "show me only what I've marked", the same way `--kind`
answers "show me only transcripts". `search_predicate.py` says so at the filter
itself.

> If you are about to make CLI `recall` withhold bodies "for consistency with
> MCP": don't. That is a regression dressed as hardening. The consistent thing
> is the *rule* — content leaves the machine only on an explicit opt-in — and
> the CLI is not a way off the machine.

### `BRAIN_AGENT_ID` — who is doing the work

Records the **actor**, as opposed to `source` (`cli` / `mcp` / `ui`), which
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
frontmatter flags that `brain ui`'s browseable views gate on, so they look
interchangeable at that boundary. They are not. `draft` is a *publish
quarantine* — the document is still fully readable everywhere else, including
over MCP and in `brain ui` search. `confidential` is an *egress control*: it additionally keeps the
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

## Adding a knob: feature settings leaves

New knobs do not go in `brain/config.py`. That file is over the 800-line
ceiling, and every knob added there needs a growth record. A new knob lives in a
**feature settings leaf** instead: one module per feature under
`src/brain/settings/` (for example `settings/rerank.py`), holding a frozen
dataclass and one loader, `load_<feature>_settings(env)`. Existing knobs stay
where they are. The policy and an example leaf are in the
`brain/settings/__init__.py` docstring; the design is §2.3 of
`docs/specs/2026-10-23-roadmap-completion-design.md`.

**You set these knobs exactly like every other one**: in the same `.env` files,
resolved in the order above, with the process environment still winning. A leaf
is read only after `Config.load()` has run, and that call publishes the dotenv
chain into the process environment, so a leaf sees the same values `Config`
does.

The parsers in `brain/settings/_env.py` (`env_bool`, `env_choice`, `env_int`,
`env_float`, `env_str`) all behave the same way:

| Input | Result |
|---|---|
| Unset, empty or whitespace only | The knob's default |
| A valid value | Parsed after stripping surrounding whitespace. Booleans accept `1/true/yes/on` and `0/false/no/off`, as `config.py` does. Choices ignore case. Numbers must lie inside the knob's stated bounds, and `nan` and `inf` are refused. |
| An invalid value | `brain` refuses to start with a `ConfigError` naming the variable and what it must be, for example `BRAIN_RERANK_DEPTH must be an integer in [2, 50]` |

The error **never quotes the value you set**, unlike the older knobs in
`config.py`, because a leaf may hold a secret. A secret (an API token, a webhook
URL) is also hidden from the settings object's `repr`, and `brain doctor` reports
only whether it is set.

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
| `brain_search` | Hybrid search with `source`, `tag`, `since_days`, and `fts_only` filters, and `entity` / `entity_type` (RM-24: only docs that mention that graph entity; needs `BRAIN_GRAPH_ENABLED=true` and `brain graphrag build`, else `INVALID_PARAMS`). Falls back to text-only matching when the query embed fails, and says so in the `fallback*` keys. |
| `brain_recall` | Passages packed to a token budget, with `[N]` citations. Falls back to text-only matching when the query embed fails, and says so in the `fallback*` keys and in the context block's header. |
| `brain_show` | Return one full document by 6+ character id prefix. |
| `brain_related` | Documents related to one document, best first: `brain ui`'s related-notes panel. Params: `id` (6+ char prefix), `limit` (default 10, at most 20), `include_confidential`. Without `include_confidential`, confidential neighbours are left out, and asking about a confidential document is `INVALID_PARAMS`. An empty list means no candidate cleared the ranking floors; on a brain of under about 40 documents that often happens even when similar documents exist. |
| `brain_explain` | Why a search ranked what it did: `brain explain --json`'s `explain` object per result, without snippets. Params: `query`, `limit` (at most `BRAIN_SEARCH_MAX_LIMIT`), `source`, `tag`, `since_days` (at least 1), `include_confidential`. Carries the `fallback*` and `stages` keys `brain_search` does. |
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
| `brain_brief` | Daily digest: recent captures, open todos, pins, best-effort next steps. Params: `since_hours`, `todo_since_days`, `no_enrich`. Titles + todo texts only. Also carries `watch_hits` (saved-search hits first seen in the capture window) and `watch_notes`; it runs no watch check. `new_entities`: people, orgs and projects first mentioned this week, under the same lens. `possible_duplicates`: the count of open near-duplicate pairs, under the same lens on both documents of a pair. |
| `brain_watch_list` | The saved searches (`brain watch`): name, query, mode, enabled, filters, timestamps. Names no document. |
| `brain_watch_add` | Save a search. Params: `name`, `query`, `source`, `tag`, `person`, `kind`, `without_tag`, `semantic`. Current matches are recorded as already seen; no count is returned. A taken name or a bad filter is `INVALID_PARAMS`. |
| `brain_watch_check` | What is new for a watch (or every enabled one) right now. **Peek only**: nothing is recorded and the brief still reports the same documents. Params: `name`, `include_confidential`. A check looks at 50 documents at most: `unrecorded` counts further full-text matches, and `capped` says a semantic check reached 50. |
| `brain_watch_remove` | Remove a watch and its recorded hits. Refused unless `confirm=true`. |
| `brain_dedup_list` | Near-duplicate document pairs (`brain dedup`), most similar first. **Read-only**: it never scans or labels. Params: `status` (`open`\|`dismissed`\|`confirmed`, default `open`), `limit` (1 to 200, default 50), `include_confidential`. Without `include_confidential`, a pair is listed only when neither document is confidential. |
| `brain_tags_list` | Every tag and its document count, most used first (`brain tags list`). **Read-only.** Params: `include_confidential`, `limit` (default 200, at most 1000); `truncated` says more tags exist. Without `include_confidential`, confidential documents are left out of the counts and a tag found only on them is not listed. |
| `brain_tags_suggest_merges` | Pairs of tags that are probably one tag, ranked, with evidence (`keep`, `merge`, counts, `reasons`, `ratio`, `overlap`, `shared`): close spelling, singular/plural, or co-occurrence. **Read-only**: merging is `brain tags merge` at the terminal. Params: `include_confidential`, `limit` (default 25, at most 100). |
| `brain_review_weekly` | Synthesize a week's activity into a review page. Params: `week` (ISO `YYYY-Www`), `no_graph`, `emit`. |
| `brain_review_scan` | Run a contradiction / staleness scan into the review queue. Params: `scan_type` (`conflicts`\|`stale`\|`all`), `dry_run`, `limit`. |
| `brain_review_findings_list` | Read the contradiction + staleness queue without scanning. Params: `kind` (`all`\|`conflicts`\|`stale`), `limit`. |
| `brain_timeline` | How a theme/entity evolved over time. Params: `query`, `person`, `granularity`, `since`, `until`, `limit`, `synthesize`, `tenant`. Needs the graph layer. |
| `brain_ask` | Agentic multi-hop cited answer synthesis. Params: `question`, `mode` (`hybrid`\|`auto`\|`fuse`\|`local`), `no_loop`, `limit`, `max_iterations`. Snippets only. Retrieves text-only when the query embed fails, and says so in the `fallback*` keys; every other `ask` failure is `INTERNAL_ERROR` with one fixed, class-only sentence. |
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
| `BRAIN_OLLAMA_NUM_CTX` | `8192` | Context window the brain requests on every Ollama chat call (`options.num_ctx`), instead of inheriting the server's `OLLAMA_CONTEXT_LENGTH`. The KV cache scales with it (llama3.1:8b, q8_0: 544 MiB at 8192 vs 4352 MiB at 65536). Unset, it grows past 8192 only as far as a raised `BRAIN_ENRICH_MAX_INPUT_TOKENS` / `BRAIN_AUDIO_MAX_INPUT_TOKENS` needs; an explicit value too small for a capped call (those two caps, or the GraphRAG extract chunk) fails at startup; an over-window prompt is refused by Ollama, never silently truncated. |
| `BRAIN_OLLAMA_EMBED_NUM_CTX` | `2048` | Context window the Ollama embedders (`arctic`, `qwen3`) request on every `/api/embed` call (`options.num_ctx`), instead of inheriting `OLLAMA_CONTEXT_LENGTH`. It is independent of `BRAIN_OLLAMA_NUM_CTX` because the embedder is a different model and runner. The default `2048` holds the largest stored chunk (1,689 model tokens, measured) and equals Ollama's embedding batch size, so arctic keeps its per-input capacity. arctic has no KV cache, so the window costs it nothing. On a server whose `OLLAMA_CONTEXT_LENGTH` is ≥ 40,960, qwen3-embedding:8b loaded at its trained 40,960, a ~3 GiB q8_0 KV cache; at `2048` it is ~153 MiB. Values below `1400` fail at startup, under every backend (the knob has no effect under `voyage` / `none`). An over-window input is refused by Ollama, never silently truncated. |
| `VOYAGE_API_KEY` | (required for `voyage`) | Voyage AI key. Ignored by the local backends. |
| `BRAIN_VAULT_PATH` | `~/brain-vault` | Vault folder for authored notes, ingested mirrors, and MCP note tools. |
| `BRAIN_USER_EMAIL` | unset | Owner email read by `brain ui`'s email-thread mode for its "Only my replies" filter. Unset, the filter is not offered. Takes effect on the next page load. |
| `BRAIN_MCP_LOG_LEVEL` | `INFO` | Stderr log level. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`. Unknown values fall back to `INFO`. Set it in the `env` block above or in any `.env` the CLI reads (see [Where config lives](#where-config-lives-env-resolution-order)). The `env` block wins. |
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
