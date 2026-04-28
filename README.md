# Second Brain

Local personal knowledge base with hybrid search, designed to be queried by Claude from any conversation.

Stores career documents, interview prep, Krisp call transcripts, Slack threads, and Gmail messages in Postgres + pgvector. Searches use Reciprocal Rank Fusion of FTS rank and vector cosine similarity. The embedding backend is pluggable — defaults to local Snowflake Arctic Embed v2 over Ollama (free, no cloud dependency); Voyage AI and Qwen3-Embedding-8B are also supported.

## What this is

A `brain` CLI backed by a local Postgres database that I can query from any Claude Code session. I ingest my own career artifacts — resumes, interview prep, Krisp call transcripts, Slack threads, selected Gmail — and Claude searches them via `brain search` whenever a conversation touches my work history. Instead of copy-pasting context into every chat, Claude pulls the real source material on demand.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| CLI | Python 3.11 + [Typer](https://typer.tiangolo.com/) | Fast to write, good ergonomics, easy to test. |
| Storage | PostgreSQL 16 + [`pgvector`](https://github.com/pgvector/pgvector) | One database for both lexical (`tsvector`) and semantic (vector) search — no separate vector store to operate. Runs in Docker on port 5433. |
| Embeddings | Pluggable via `BRAIN_EMBEDDER` — default [Snowflake Arctic Embed v2](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0) over local [Ollama](https://ollama.com/) (1024-dim, Apache 2.0, free). [Voyage AI](https://docs.voyageai.com/) (`voyage-3.5`, paid SaaS) and [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) (4096-dim, local Ollama) are alternates behind the same `Embedder` Protocol. | Local-by-default keeps the corpus off vendor servers; the abstraction lets the user upgrade or downgrade backends without touching ingest/search code. |
| Search | Hybrid: Postgres FTS + vector cosine, fused via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60) | Lexical alone misses paraphrases ("what did I say about X"); vector alone misses exact names ("a coworker", "a former employer"). RRF combines both ranks without tuning weights. |
| Extraction | `pypdf`, `pdfplumber`, `python-docx`, `markdown-it-py` | Covers the file types I actually have. |
| Chunking | Paragraph-aware, budgeted with `tiktoken` | Keeps semantic boundaries intact while staying under the embedder's token limit. |
| Output | [Rich](https://rich.readthedocs.io/) tables + `--json` mode | Human-readable in a terminal, machine-parsable when Claude shells out. |
| Tests | `pytest` against a real Postgres test DB, fake embedder fixture | Real-DB integration catches schema/migration drift that mocks would hide. |
| Lint / type | `ruff`, `mypy` | Cheap to run, catches real bugs. |

## Why I built this

- **Claude has no memory across conversations.** A queryable second brain gives it durable, personal context (past meetings, prior writings, decisions) without me re-pasting it every time.
- **Local-only by design.** My Krisp transcripts and Slack history don't leave my machine — no SaaS account, no vendor indexing my comms.
- **Hybrid search beats either half.** My queries split roughly 50/50 between paraphrase-heavy ("compliance horror stories") and exact-name (a coworker, a former employer). RRF gives me both in one ranked list.
- **Works from any cwd.** A symlinked launcher means Claude Code in any project can call `brain search` — the knowledge base isn't tied to one repo.
- **Idempotent ingest.** `documents.content_hash` is `UNIQUE`, so re-running `brain ingest-dir` is a no-op. I can rerun without thinking about duplicates.

## Token cost vs. direct fetch

The other big reason this is worth building: querying `brain` burns far less context than having Claude read the source directly via MCP or the `Read` tool. Rough per-query estimates (yours will vary with thread/file size):

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

## Setup

### Prerequisites

- **Python 3.11+** — `python3.11 --version` should work. On macOS: `brew install python@3.11`.
- **Docker Desktop** (or Docker Engine) — running. The Postgres + pgvector database lives in a container on port 5433. Get it from [docker.com](https://www.docker.com/products/docker-desktop/).
- **[Ollama](https://ollama.com/)** (for the default `arctic` backend, and for `qwen3`). On macOS:
  ```bash
  brew install ollama
  brew services start ollama
  ollama pull snowflake-arctic-embed2     # default — 1.2 GB
  # ollama pull qwen3-embedding:8b        # only if you want BRAIN_EMBEDDER=qwen3 — 4.7 GB
  ```
  Skip Ollama entirely if you plan to use `BRAIN_EMBEDDER=voyage` exclusively.
- **A Voyage AI API key** (only if `BRAIN_EMBEDDER=voyage`) — free tier covers personal use. Sign up at [voyageai.com](https://www.voyageai.com/) and grab a key.
- **Claude Code** (optional but the whole point) — install from [claude.com/claude-code](https://claude.com/claude-code) so Claude can call `brain` for you.

### Install

```bash
# 1. Clone the repo and enter it
git clone <repo> ~/workspace/second-brain
cd ~/workspace/second-brain

# 2. Set up the environment file (gitignored). The default BRAIN_EMBEDDER=arctic
#    needs no API keys; if you choose voyage, paste your VOYAGE_API_KEY here.
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
#    On a fresh DB with no chunks yet this is a no-op-then-finalize;
#    after a re-ingest it backfills any NULL rows.
brain reembed

# 8. Sanity check — should print "all OK" lines for each component.
brain doctor
```

If `brain doctor` complains, the usual suspects are: Docker isn't running, the
container hasn't finished starting yet (give it ~10 seconds and retry), Ollama
isn't running (`brew services start ollama`), the configured embedding model
isn't pulled (`ollama pull snowflake-arctic-embed2`), or — only when
`BRAIN_EMBEDDER=voyage` — `VOYAGE_API_KEY` is missing from `.env`.

### Choosing an embedder backend

Set `BRAIN_EMBEDDER` in `.env` (or the shell). Three values are supported:

| Value | Model | Dim | Cost | Setup | Notes |
|---|---|---|---|---|---|
| `arctic` *(default)* | Snowflake Arctic Embed v2 (Apache 2.0) | 1024 | Free | Ollama + `ollama pull snowflake-arctic-embed2` | Recommended. Strong retrieval quality on personal text; HNSW-indexable; fully local. |
| `voyage` | Voyage AI `voyage-3.5` | 1024 | ~$0.06/M tokens | `VOYAGE_API_KEY` in `.env` | Highest quality on long-form text; corpus leaves your machine. |
| `qwen3` | Qwen3-Embedding-8B (Alibaba) | 4096 | Free | Ollama + `ollama pull qwen3-embedding:8b` | Local. Native 4096 dims exceeds pgvector's HNSW cap (2000 for `vector`) so search uses sequential scan — fine at <100K chunks but slower than `arctic`. China-origin model — judge accordingly. |

The active backend is reflected in `brain init` ("embedder arctic (dim=1024)") and `brain doctor` (the embedding-column line shows the column type and whether `[hnsw]` is present).

### Switching embedder backends

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

### Make `brain` available from any directory

By default, `brain` only works inside this folder with the venv activated. To
call it from anywhere — including from Claude Code in any project — symlink the
launcher onto a directory already on your `$PATH`:

```bash
# macOS (Homebrew):
ln -s ~/workspace/second-brain/.venv/bin/brain /opt/homebrew/bin/brain

# Linux (or non-Homebrew macOS):
ln -s ~/workspace/second-brain/.venv/bin/brain ~/.local/bin/brain
```

The symlink works without `source .venv/bin/activate` because `pip install -e`
gives the launcher an absolute-path shebang pointing at the venv's Python.

Verify with `which brain` (should resolve to the symlink) and `brain doctor`.

## Usage

```bash
# Ingest your career corpus
brain ingest-dir ~/Documents/career

# Tag a document
brain tag <id-prefix> +interview +career

# Edit a document in place (title / metadata / body)
brain edit <id-prefix> --title "New title"
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}'   # shallow-merges
brain edit <id-prefix> --content-file ./fixed.md            # re-embeds
brain edit <id-prefix>                                      # opens $EDITOR

# Search
brain search "what did I tell my manager about the platform migration"
brain search "compliance horror stories" --limit 10
brain search "interview prep" --tag interview --since 30

# Drill into a result
brain show <id-prefix>

# Browse
brain list --source gmail --limit 20
brain list --tag career

# Gmail (requires at least one scope flag)
brain ingest-gmail --label interviews --since 30d
brain ingest-gmail --from alice@example.com

# Krisp / Slack — Claude orchestrates this:
# (Claude calls Krisp MCP, then pipes to brain ingest-stdin)
echo "<transcript>" | brain ingest-stdin \
  --source krisp --external-id meeting-42 \
  --title "1:1 — Apr 24" --content-type transcript \
  --metadata '{"participants":["Alice","Bob"]}'

# Admin
brain status   # counts and last-ingest time
brain doctor   # health check
```

## Use from Claude Desktop

The `brain-mcp` binary exposes the brain as an [MCP](https://modelcontextprotocol.io/) server so Claude Desktop can search, save, and edit entries during a chat — no terminal required. Seven tools are advertised:

- **Read:** `brain_search`, `brain_show`, `brain_list`, `brain_status`
- **Write:** `brain_ingest_stdin`, `brain_tag`, `brain_edit`

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`. The `env` block must match whichever backend you set in `.env` — pass `BRAIN_EMBEDDER` and the backend-specific knobs (Ollama host for `arctic`/`qwen3`, `VOYAGE_API_KEY` for `voyage`):

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/mshtawythug/workspace/second-brain/.venv/bin/brain-mcp",
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5433/second_brain",
        "BRAIN_EMBEDDER": "arctic",
        "OLLAMA_HOST": "http://localhost:11434"
      }
    }
  }
}
```

For the Voyage backend, swap the embedder-specific keys: `"BRAIN_EMBEDDER": "voyage"` and `"VOYAGE_API_KEY": "<paste here>"`.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | (required) | Postgres connection string. Same value used by the CLI. |
| `BRAIN_EMBEDDER` | `arctic` | Embedder backend: `arctic`, `voyage`, or `qwen3`. Must match the dim baked into the database by `brain init`. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL. Used by `arctic` and `qwen3`; ignored by `voyage`. |
| `QWEN3_MODEL` | `qwen3-embedding:8b` | Ollama model tag for the qwen3 backend. |
| `VOYAGE_API_KEY` | (required for `voyage`) | Voyage AI key. Ignored by the local backends. |
| `BRAIN_MCP_LOG_LEVEL` | `INFO` | Stderr log level. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`. Unknown values fall back to `INFO`. |

### What to expect

After saving the config and restarting Claude Desktop, the seven tools become callable in any chat — ask "search my brain for the Q1 review with person-x" and Claude Desktop calls `brain_search` directly. Server startup is ~0.5–1.5s; the cold start is the embedder warming up on the first search (Ollama loading the model into memory, or the Voyage SDK initializing). Logs go to stderr and are surfaced by Claude Desktop if a tool call fails.

## Architecture

See [`docs/specs/2026-04-24-second-brain-design.md`](docs/specs/2026-04-24-second-brain-design.md).

## How Claude uses this

A snippet in `~/.claude/CLAUDE.md` tells every Claude Code conversation:
- When to invoke `brain search` and `brain show` (career topics, interviews, past meetings, prior roles, deals)
- How to orchestrate Krisp/Slack ingestion via MCP → `brain ingest-stdin`
- That `--json` output is available for programmatic parsing

### Example prompts

Once your corpus is ingested, you can ask Claude things like:

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

**Ingest on demand** (Claude orchestrates the MCP calls)
- "Ingest last week's Krisp calls."
- "Pull the Slack thread about the auth incident into my brain."
- "Ingest emails from the recruiting@ alias from the past 30 days."

The pattern: ask the question naturally — Claude decides whether to call `brain search`, which filters to apply (`--source`, `--tag`, `--since`), and when to follow up with `brain show` for full context.

## Tests

```bash
pytest                      # full suite (uses second_brain_test DB)
pytest --cov=brain          # with coverage
```

## License

[MIT](LICENSE)
