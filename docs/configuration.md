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

### Claude Code (consult-brain skill)

For Claude Code (the CLI), this repo ships skills under `skills/` that teach
Claude when and how to use each brain feature. Install the ones you want with
symlinks so live edits to the repo update the skills:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/consult-brain"  ~/.claude/skills/consult-brain   # hybrid search + Q&A
ln -s "$(pwd)/skills/brain-graph"    ~/.claude/skills/brain-graph      # GraphRAG themes/patterns
ln -s "$(pwd)/skills/elicit-brain"   ~/.claude/skills/elicit-brain     # tacit-knowledge elicitation
ln -s "$(pwd)/skills/brain-todo"     ~/.claude/skills/brain-todo       # action-item view
ln -s "$(pwd)/skills/ingest-brain"   ~/.claude/skills/ingest-brain     # Krisp/Slack ingest
```

The `consult-brain` skill (plain search + Q&A) is the one you most likely want
first — it triggers on phrases like "what did I say to X", "summarize my
conversations about Y", "write this in my voice". The `elicit-brain` skill
triggers on elicitation phrases like "what do I know that I haven't written
down", "surface my knowledge gaps", "interview me about X", or "brain elicit".
The MCP server above covers Claude Desktop; these skills cover Claude Code.

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
pipx uninstall second-brain
```
