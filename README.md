# Second Brain

Local, queryable knowledge base and note vault with hybrid search and an entity-graph layer — designed to be searched by any AI coding agent or assistant from any conversation.

Stores career documents, interview prep, Krisp call transcripts, Slack threads, Gmail messages, and authored Markdown notes in Postgres + pgvector. Hybrid search ranks results with Reciprocal Rank Fusion over full-text rank and vector cosine similarity, plus recency weighting and metadata filters. The embedding backend is pluggable — defaults to local Snowflake Arctic Embed v2 over Ollama (free, no cloud dependency); Voyage AI and Qwen3-Embedding-8B are also supported.

On top of plain search it adds: a **GraphRAG** layer (an entity graph of people, orgs, and concepts in Apache AGE) for themes, patterns, and connections across interactions; optional **LLM enrichment** that auto-summarizes and tags ingested documents; and an Obsidian-style **vault** of wiki-linked notes that can be published as a browsable **Quartz wiki**. Any agent reaches all of this through the `brain` CLI or the bundled `brain-mcp` MCP server — with ready-made skills for Claude Code, and the MCP server dropping into Claude Desktop or any MCP-compatible client.

## Table of contents

- [What this is](#what-this-is)
- [Why this exists](#why-this-exists)
  - [Why I built it](#why-i-built-it)
  - [Token cost vs. direct fetch](#token-cost-vs-direct-fetch)
- [Quick start](#quick-start)
  - [Prerequisites](#prerequisites)
    - [Required](#required)
    - [Optional](#optional)
  - [Install](#install)
  - [Make `brain`, `brain-mcp`, and the `bin/` scripts available from any directory](#make-brain-brain-mcp-and-the-bin-scripts-available-from-any-directory)
- [Tech stack](#tech-stack)
- [Core usage](#core-usage)
  - [Ingest files and pasted text](#ingest-files-and-pasted-text)
  - [Search, browse, and inspect](#search-browse-and-inspect)
  - [Tags, edits, draft hiding, and deletes](#tags-edits-draft-hiding-and-deletes)
  - [Gmail ingest](#gmail-ingest)
  - [Status and health](#status-and-health)
- [GraphRAG (graph retrieval)](#graphrag-graph-retrieval)
  - [How it works](#how-it-works)
  - [Enabling GraphRAG](#enabling-graphrag)
  - [Query modes](#query-modes)
  - [Upgrading an existing brain to the AGE image](#upgrading-an-existing-brain-to-the-age-image)
- [Tacit-knowledge elicitation](#tacit-knowledge-elicitation)
  - [How it works](#how-it-works-1)
  - [Commands](#commands)
  - [Config knobs](#config-knobs)
- [Vault model](#vault-model)
  - [One-time vault setup](#one-time-vault-setup)
  - [Authoring commands](#authoring-commands)
  - [Link graph](#link-graph)
  - [Watcher mode](#watcher-mode)
  - [Vault maintenance](#vault-maintenance)
- [Wiki (rendered view, optional)](#wiki-rendered-view-optional)
  - [Quick start (Wiki)](#quick-start-wiki)
  - [One-time setup](#one-time-setup)
  - [Render](#render)
  - [Customizing the graph](#customizing-the-graph)
  - [Serve locally](#serve-locally)
  - [Daily use — `bin/` scripts](#daily-use--bin-scripts)
  - [Deploy (optional)](#deploy-optional)
  - [When Quartz isn't a fit](#when-quartz-isnt-a-fit)
- [Claude integrations](#claude-integrations)
  - [Claude Desktop (MCP server)](#claude-desktop-mcp-server)
  - [Claude Code (consult-brain skill)](#claude-code-consult-brain-skill)
  - [Example prompts](#example-prompts)
- [Configuration and administration](#configuration-and-administration)
  - [Choosing an embedder backend](#choosing-an-embedder-backend)
  - [Switching embedder backends](#switching-embedder-backends)
  - [Data hygiene backfills](#data-hygiene-backfills)
  - [Uninstall](#uninstall)
- [Codebase layout](#codebase-layout)
- [Tests](#tests)
- [License](#license)

## What this is

A `brain` CLI backed by a local Postgres database that any AI agent can query from any session — Claude Code, Claude Desktop, or any other CLI-capable or MCP-compatible harness. I ingest my own career artifacts — resumes, interview prep, Krisp call transcripts, Slack threads, selected Gmail — and the agent searches them via `brain search` whenever a conversation touches my work history. Instead of copy-pasting context into every chat, the agent pulls the real source material on demand.

## Why this exists

### Why I built it

- **AI assistants have no memory across conversations.** A queryable second brain gives them durable, personal context (past meetings, prior writings, decisions) without me re-pasting it every time.
- **Local-only by design.** My Krisp transcripts and Slack history don't leave my machine — no SaaS account, no vendor indexing my comms.
- **Hybrid search beats either half.** My queries split roughly 50/50 between paraphrase-heavy ("compliance horror stories") and exact-name (a coworker, a former employer). RRF gives me both in one ranked list.
- **Works from any cwd.** A symlinked launcher means any agent in any project can call `brain search` — the knowledge base isn't tied to one repo.
- **Idempotent ingest.** `documents.content_hash` is `UNIQUE`, so re-running `brain ingest-dir` is a no-op. I can rerun without thinking about duplicates.

### Token cost vs. direct fetch

The other big reason this is worth building: querying `brain` burns far less context than having an agent read the source directly via MCP or a file-read tool. Rough per-query estimates (yours will vary with thread/file size):

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

## Quick start

### Prerequisites

#### Required

- **Git** — used to clone this repo and the optional Quartz wiki renderer.
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

#### Optional

- **Node.js 18+ and npm** (only if you want the rendered wiki). On macOS: `brew install node`.
- **[Caddy](https://caddyserver.com/)** (only if you want the live wiki at `brain.test` or `localhost:8080`). On macOS:
  ```bash
  brew install caddy
  brew services start caddy
  ```
  See [Wiki rendering → Serve locally](#serve-locally) for the Caddyfile recipe and the `brain.test` /etc/hosts entry. Skip Caddy if you only ever query the brain through `brain search` from your agent — the wiki view is optional.
- **`gws` CLI** (only for Gmail ingest and Google-backed directory linking). Brain shells out to `gws gmail users messages list/get` for `brain ingest-gmail`, and uses `gws` best-effort for Calendar/Contacts directory refreshes. Install and authenticate it separately, make sure `which gws` resolves, then `brain doctor` will report `gws CLI OK`.
- **Apache AGE Postgres image** — shipped automatically. `brain setup` builds and runs the custom `second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2` image (PostgreSQL 16 + pgvector 0.8.2 + pgcrypto + AGE 1.5.0-rc0) from the packaged Dockerfile. The step-by-step path stays on stock pgvector for backwards-compat; see [Upgrading an existing brain to the AGE image](#upgrading-an-existing-brain-to-the-age-image).
- **An Ollama model for GraphRAG concept extraction** (only if `BRAIN_GRAPH_CONCEPTS=true`, which is the default). `ollama pull llama3.1:8b` (~4.7 GB) covers the default model; set `BRAIN_GRAPH_EXTRACT_MODEL` to override. Setting `BRAIN_GRAPH_CONCEPTS=false` disables concept extraction (people aspect still works without an LLM).
- **Graphviz** (optional) — only needed if you want to render `brain graph --format dot` output locally with `dot -Tsvg`. On macOS: `brew install graphviz`.
- **An AI agent or assistant** (optional but the whole point) — any CLI-capable agent (e.g. Claude Code) can call the `brain` CLI from any project; any MCP-compatible client (e.g. Claude Desktop) can call the `brain-mcp` server once configured.

### Install

#### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/<your-github-username>/second-brain/v0.2.0/install.sh | bash
```

Replace `<your-github-username>` with your GitHub user/org. The script:
- Refuses Windows (use WSL2 + Ubuntu and re-run inside WSL).
- Checks that Python ≥ 3.11 is on PATH; refuses with a clear remediation if not.
- Installs `pipx` if missing (`brew install pipx` on macOS, `pip install --user pipx` on Linux), then `pipx ensurepath`.
- `pipx install`s the `brain` CLI from the tagged release (default `v0.2.0`; override with `BRAIN_INSTALL_REF=...`). Refuses non-tag refs unless `BRAIN_INSECURE=1` is set.
- Resolves the pipx bin directory explicitly (doesn't rely on PATH) and execs `brain setup`, which runs:
  - 8 pre-flight checks (Docker daemon, Ollama, port 5433 + 8080 free, Caddy installed, `~/.claude/skills/` writable, pinned Quartz commit reachable on GitHub).
  - Creates `$BRAIN_HOME` (default `~/.brain`) with `data/postgres/`, `logs/`, `.shims/`.
  - Renders `docker-compose.yml` + `.env` from packaged templates, brings up the Postgres + pgvector container, waits for `pg_isready`, and runs `brain init` + `brain doctor`.
  - Prompts (default Y) to install the wiki UI (`brain wiki install` — clones Quartz at a pinned commit + applies the overlay + `npm install`) and the Claude Code skill (`~/.claude/skills/brain/SKILL.md`).
  - On macOS, installs the launchd LaunchAgents for the watcher + build daemons LAST, after every prerequisite has passed.

Pass `--non-interactive` (default Y for every prompt) or `--skip-wiki` / `--skip-skill` through to setup:

```bash
curl -fsSL https://raw.githubusercontent.com/<your-github-username>/second-brain/v0.2.0/install.sh | bash -s -- --non-interactive
```

#### Step by step (if you'd rather)

If you prefer to drive each step yourself or you're hacking on the code:

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

### Make `brain`, `brain-mcp`, and the `bin/` scripts available from any directory

By default, `brain` and `brain-mcp` only work inside this folder with the venv
activated, and the `bin/brain-up` / `bin/brain-down` / `bin/brain-rebuild` /
`bin/brain-status` scripts have to be invoked by full path. Two small one-time
edits fix the CLI and wiki scripts; the MCP symlink is optional but useful for
debugging.

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

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| CLI | Python 3.11 + [Typer](https://typer.tiangolo.com/) | Fast to write, good ergonomics, easy to test. |
| Storage | PostgreSQL 16 + [`pgvector`](https://github.com/pgvector/pgvector) | One database for both lexical (`tsvector`) and semantic (vector) search — no separate vector store to operate. Runs in Docker on port 5433. |
| Embeddings | Pluggable via `BRAIN_EMBEDDER` — default [Snowflake Arctic Embed v2](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0) over local [Ollama](https://ollama.com/) (1024-dim, Apache 2.0, free). [Voyage AI](https://docs.voyageai.com/) (`voyage-3.5`, paid SaaS) and [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) (4096-dim, local Ollama) are alternates behind the same `Embedder` Protocol. | Local-by-default keeps the corpus off vendor servers; the abstraction lets the user upgrade or downgrade backends without touching ingest/search code. |
| Search | Hybrid: Postgres FTS + vector cosine, fused via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60) | Lexical alone misses paraphrases ("what did I say about X"); vector alone misses exact names ("a coworker", "a former employer"). RRF combines both ranks without tuning weights. |
| Graph retrieval | [Apache AGE](https://age.apache.org/) (openCypher inside Postgres) + [`networkx`](https://networkx.org/) for Louvain community detection | Entity-centric retrieval for themes, patterns, and connections that hybrid search misses. Graph sync defaults on; on stock pgvector, ingest-time graph sync is a no-op and `brain graphrag …` commands return an AGE-not-available guard. |
| Extraction | `pypdf`, `pdfplumber`, `python-docx`, `markdown-it-py` | Covers the file types I actually have. |
| Chunking | Paragraph-aware, budgeted with `tiktoken` | Keeps semantic boundaries intact while staying under the embedder's token limit. |
| LLM enrichment | Local-by-default [Ollama](https://ollama.com/) (`llama3.1:8b` defaults for `BRAIN_ENRICH_MODEL` and `BRAIN_GRAPH_EXTRACT_MODEL`) | Auto-summary writes `documents.summary` after ingest; GraphRAG concept extraction uses a separate Ollama extractor. `--no-enrich` disables auto-summary for an ingest run; `BRAIN_GRAPH_CONCEPTS=false` disables the concept aspect. |
| Output | [Rich](https://rich.readthedocs.io/) tables + `--json` mode | Human-readable in a terminal, machine-parsable when an agent shells out. |
| Wiki rendering | [Quartz](https://quartz.jzhao.xyz/) (static site from Markdown + `[[wiki-links]]`) + [Caddy](https://caddyserver.com/) (blue/green serve from the `current` symlink) | Optional rendered vault view: graph view, backlinks, full-text search, dark mode. Body-only edits take the per-file fastpath, reaching the UI in ~2s including reload. Start it with `brain-up` in dev; installer users opt out with `--skip-wiki`. |
| Tests | `pytest` against a real Postgres test DB, fake embedder fixture | Real-DB integration catches schema/migration drift that mocks would hide. |
| Lint / type | `ruff`, `mypy` | Cheap to run, catches real bugs. |

## Core usage

### Ingest files and pasted text

```bash
# Single files: TXT, Markdown, PDF, DOCX.
brain ingest ~/Documents/resume.pdf --tag career --tag resume
brain ingest ./notes.md --force       # re-ingest even if the content already exists

# Directories: recursive, idempotent by content hash.
brain ingest-dir ~/Documents/career
brain ingest-dir ~/Documents/career --tag career --ext pdf,md,docx
brain ingest-dir ~/Documents/career --dry-run

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
exists, mirror ingested documents into `_ingested/<source>/` so the wiki can
show them. Re-running unchanged content is a no-op unless you pass `--force`.

### Search, browse, and inspect

```bash
brain search "what did I tell my manager about the platform migration"
brain search "compliance horror stories" --limit 10
brain search "interview prep" --tag interview --since 30
brain search "person-x onboarding" --source gmail --json
brain search "exact product name" --fts-only   # skip embedding call

brain show <id-prefix>
brain show <id-prefix> --json

brain list --source gmail --limit 20
brain list --tag career
brain list --json
```

ID prefixes must be at least 6 hex characters and must uniquely identify one
document. `--json` is the machine-readable path for agents or scripts.

### Tags, edits, draft hiding, and deletes

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

# Hide a doc from the rendered wiki without removing it from local CLI search.
brain mark-draft <id-prefix>
brain mark-published <id-prefix>

# Delete a document and its chunks. If a vault mirror exists, it is unlinked too.
brain rm <id-prefix>
brain rm <id-prefix> --yes
```

### Gmail ingest

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

### Status and health

```bash
brain status   # counts and last-ingest time
brain doctor   # env, Postgres/pgvector, embedder, gws, npx, mirror drift, AGE + graph health
```

`brain doctor` exits non-zero only for required failures: config, database, or
active embedder. Optional integrations (`gws`, `npx`, the AGE extension, the
concept-extractor model, community materialization staleness) are warnings.

## GraphRAG (graph retrieval)

### How it works

GraphRAG adds **entity-centric graph retrieval alongside** the existing
hybrid vector + FTS search — it does not replace `brain search`. Where hybrid
search ranks documents by lexical + semantic similarity, GraphRAG builds a
graph of the people and concepts that co-occur across your corpus and
retrieves over that structure. It answers questions hybrid search struggles
with — "what themes come up in my conversations with X", "which clusters of
people and topics dominate my notes" — by traversing relationships instead of
matching text.

It exposes a `brain graphrag …` CLI command group (with full MCP parity via
the `brain_graphrag_*` tools), with five retrieval modes:

| Mode | What it does |
|---|---|
| `local` | Entity-centric — seeds on an entity and traverses its co-occurrence neighbourhood. |
| `themes` | "Themes with X" — scopes to a person and returns ranked theme groups (the headline use case; requires `--person`). |
| `global` | Community-level — RRF over detected communities (run `brain graphrag communities build` first). |
| `fuse` | RRF-merges the local-graph document leg with the vector/FTS hybrid leg into one ranked list. |
| `auto` *(default)* | Heuristic router — picks `themes` / `global` / `local` based on the query and whether a person resolves. |

The graph runs on [Apache AGE](https://age.apache.org/) (an openCypher graph
extension) inside the same Postgres. Both aspects — **people** (always on) and
**concepts** (LLM entity extraction over topics/projects/orgs/tools) — are
default-on; flip `BRAIN_GRAPH_CONCEPTS=false` to disable the concept aspect if
you don't have an LLM available locally. No raw Cypher is ever accepted or
shown; every command takes structured params and the backend injects the
tenant + traversal caps.

### Enabling GraphRAG

GraphRAG is on by default after `brain setup` (the one-liner installer). The
packaged `docker-compose.yml` template provisions a custom Postgres image —
`second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2` (PostgreSQL 16 + pgvector 0.8.2 +
pgcrypto + Apache AGE 1.5.0-rc0), built from
`src/brain/templates/docker/age/Dockerfile` — and `brain init` bootstraps the
AGE extension and the recomputable graph mirror automatically.

If you installed by hand (the "Step by step" path above) on a stock pgvector
image, GraphRAG will run in soft-degraded mode: ingest succeeds, but graph
sync is a silent no-op and `brain graphrag …` commands return a friendly "AGE
not available" guard. To enable the full graph stack, follow the
[Upgrading an existing brain to the AGE image](#upgrading-an-existing-brain-to-the-age-image)
recipe below.

After install, backfill the graph from your existing documents and (optionally)
detect communities for `--mode global`:

```bash
brain graphrag build --backfill          # idempotent + resumable
brain graphrag communities build         # required only for --mode global
brain doctor                             # AGE + graph-drift + communities health
```

The relevant `.env` knobs are documented in `.env.example` (search for
`BRAIN_GRAPH_`): `BRAIN_GRAPH_ENABLED` (default `true`), `BRAIN_GRAPH_CONCEPTS`
(default `true`), `BRAIN_GRAPH_TENANT`, `BRAIN_GRAPH_DEPTH`,
`BRAIN_GRAPH_EXTRACT_MODEL`, the community-detection tuning
(`BRAIN_GRAPH_COMMUNITY_*`), and more — all with sensible defaults.

### Query modes

```bash
# Graph retrieval. --mode defaults to auto (the heuristic router).
brain graphrag search "platform migration tradeoffs"
brain graphrag search "platform migration tradeoffs" --mode local
brain graphrag search "hiring plans" --mode fuse        # graph leg ⊕ vector/FTS leg
brain graphrag search "..." --json                      # machine-readable

# "Themes in my conversations with X" — the headline. --person is required.
brain graphrag themes --person "Jane Doe"
brain graphrag themes --person "Jane Doe" --limit 5 --synthesize

# Inspect one entity's co-occurrence neighbourhood.
brain graphrag entity "Project Phoenix"

# Community admin (global mode).
brain graphrag communities build        # detect + summarize (skips if graph unchanged)
brain graphrag communities refresh      # force a rebuild regardless of the dirty gate
brain graphrag communities list         # admin view of materialized communities

# Index maintenance. build (with concepts) + refresh also auto-collapse cross-type
# duplicate concept entities — the same name extracted as `org` in one doc and
# `project` in another merges into one row (highest-precedence type wins), keeping
# the best surface form (e.g. `AcmePlatform`, not `acmeplatform`).
brain graphrag build --backfill         # reconcile every existing doc into the graph
brain graphrag build --force            # authoritative full rebuild (recover a dropped mirror)
brain graphrag refresh                  # recompute edge weights + collapse cross-type duplicates
```

The same surface is available to any MCP client (Claude Desktop, Claude Code, and others) through the
`brain_graphrag_search`, `brain_graphrag_themes`, `brain_graphrag_entity`,
`brain_graphrag_build`, and `brain_graphrag_communities_build` MCP tools.

### Upgrading an existing brain to the AGE image

If your brain was installed before 2026-05-22 (or you used the step-by-step
path on the committed `docker-compose.yml`, which intentionally stays on stock
pgvector), the graph stack runs in soft-degraded mode until you flip the
container image. The cutover is about the container, not the data — the AGE
graph itself is a recomputable mirror you'll rebuild with
`brain graphrag build --force` after the swap.

```bash
# 1. Back up your database before any image change (host bind-mount; mandatory).
mkdir -p ~/brain-backups
docker exec second-brain-postgres pg_dump -U brain -Fc -d second_brain \
  > ~/brain-backups/second_brain-precutover-$(date +%Y%m%d-%H%M%S).dump

# 2. Build the AGE image locally. The Dockerfile is packaged in this repo.
docker build -t second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2 \
  -f src/brain/templates/docker/age/Dockerfile src/brain/templates/docker/age/

# 3. Create a gitignored docker-compose.override.yml that pins the AGE image
#    only on your machine — the committed docker-compose.yml stays on stock
#    pgvector as the repo default.
cat > docker-compose.override.yml <<'YAML'
services:
  postgres:
    image: second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2
YAML

# 4. Restart the container so Compose picks up the new image.
docker compose down
docker compose up -d

# 5. Bootstrap AGE + recompute the graph mirror.
brain init                               # creates the age extension + graph mirror
brain graphrag build --force             # authoritative full rebuild
brain graphrag communities build         # optional, needed for --mode global
brain doctor                             # AGE + graph-drift should now report OK
```

If something goes wrong, `docker compose down && docker compose up -d` with
the override removed returns you to stock pgvector (the data on disk is
unchanged — graph rows live in their own tables and AGE catalogue, both of
which are safely re-derivable from the backup).

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

## Vault model

Brain has two storage tiers, both searchable through the same hybrid index:

- **Ingested tier** (`kind='ingested'`) — Krisp transcripts, Slack threads, Gmail, raw files. Read-only by convention; the DB is authoritative. Files are mirrored under `_ingested/<source>/` in the vault folder so they show up in the wiki.
- **Vault tier** (`kind='vault'`) — notes you author. The `.md` file on disk is the source of truth; the DB is a derived index that `brain vault sync` rebuilds from the file. Edit in any text editor.

Both tiers live in a single vault folder (default `~/brain-vault/`, override with `BRAIN_VAULT_PATH`). Wiki-links (`[[Title]]`, `[[brain:<id>]]`, `[[krisp:<external_id>]]`) cross both tiers — vault notes can link to ingested artifacts and back.

### One-time vault setup

```bash
# 1. Scaffold the vault folder + templates + _ingested/ subdirs.
brain vault init                         # creates ~/brain-vault/

# 2. Dump the existing DB into the vault as Markdown files. One-shot,
#    safe to re-run, idempotent.
brain vault export --to ~/brain-vault    # produces _ingested/<source>/*.md

# 3. Establish the round-trip baseline so future sync runs are no-ops
#    until you actually edit something.
brain vault sync
```

After this, your DB and vault are in lockstep. Edit any `.md` file in `~/brain-vault/` and the next `brain vault sync` (or the watcher — see below) will re-chunk + re-embed it.

### Authoring commands

```bash
# Create a note from _templates/note.md, opens $EDITOR.
brain note new "person-x conversation"

# Today's daily note at daily/<YYYY>/<YYYY-MM-DD>.md.
brain daily

# Rename a note safely — rewrites every [[old-title]] reference across
# the vault. Atomic snapshot/restore on failure.
brain note rename <id-prefix> "New title"
brain note rename <id-prefix> "New title" --dry-run   # preview the diff

# Edit an existing doc. For vault-tier docs, opens the file in $EDITOR
# directly. For ingested-tier (Krisp/Slack/Gmail), uses the JSON-header
# editor flow.
brain edit <id-prefix>
```

### Link graph

```bash
brain backlinks <id-prefix>              # what links TO this doc
brain links <id-prefix>                  # what this doc links to
brain links <id-prefix> --unresolved     # plus dangling [[refs]]
brain orphans                            # vault notes with no links
brain orphans --all                      # include ingested-tier
brain graph --format json                # full link graph as JSON
brain graph --format dot | dot -Tsvg > graph.svg && open graph.svg
brain graph --format mermaid             # paste into mermaid.live
brain graph --root <id> --depth 2 --format dot | dot -Tsvg > focus.svg
```

### Watcher mode

```bash
brain vault sync --watch
```

Runs as a daemon (Ctrl-C to stop). Filesystem events trigger debounced (500ms) per-file syncs — edit a note, save, and within a beat the chunk + embedding update in the DB. Skips `_templates/`, `_attachments/`, hidden directories. The `bin/brain-up` script kicks this off alongside the wiki [build watcher](#serve-locally).

### Vault maintenance

```bash
# Preview or prune vault-tier DB rows whose source files vanished.
brain vault sync --dry-run
brain vault sync --prune

# Remove stale _ingested/ mirror files whose DB rows no longer exist.
brain vault prune-orphans
brain vault prune-orphans --include-stale
brain vault prune-orphans --apply

# Rebuild the name/email directory used by metadata-derived links.
brain vault directory refresh
brain vault directory show
brain vault directory show --source gmail

# Rebuild Gmail/Krisp metadata-derived graph edges and rewrite the
# "Related" fences in affected _ingested/ files.
brain vault relink-derived
```

Use `brain vault directory refresh` after a large Gmail ingest, after changing
Google contacts/calendar access, or after editing any people metadata consumed
by the linker. Use `brain vault relink-derived` when you want the graph and
rendered "Related" sections to reflect the latest Gmail/Krisp corpus. Both
commands are idempotent; missing `gws` degrades to warnings, with Gmail-derived
directory entries still refreshed from already-ingested mail.

**Excluding the corpus owner from derived edges.** By default the
participant-overlap rules (R2 `shared_participant`, R3 `same_day_participant`)
treat every participant as graph-worthy — including yourself, which can make
every meeting/email link to every other doc you're on. Set
`BRAIN_OWNER_PARTICIPANTS` in `.env` to a comma-separated list of identifiers
to strip before the rules evaluate. Both emails AND display names are
accepted, matching is case-insensitive, and entries are trimmed +
lowercased at load time.

For full coverage **list every form your name appears under in your
corpus** — Gmail headers contribute both the email AND a normalized
display-name key for each participant, so listing only the email leaves
the display-name key behind and the rules still match. The recommended
shape is `<Display Name>,<email>[,<other-email>...]`:

```
BRAIN_OWNER_PARTICIPANTS="Pat Morgan,redacted@example.com,redacted@example.com"
```

After changing the value, run `brain vault relink-derived` to rebuild the
derived-links table, then `brain vault sync` to refresh the in-body
"Related" fences. Unset / blank disables the exclusion (existing behavior).

The `brain owner` subcommand group manages this list without hand-editing
`.env`: `brain owner show` prints the active list, `brain owner set
"<csv>"` replaces it, and `brain owner add <id>` / `brain owner remove
<id>` adjust one entry at a time (idempotent, case-insensitive). Each
mutation rewrites `.env` atomically and reminds you to run
`brain vault relink-derived` + `brain vault sync` afterward.

## Wiki (rendered view, optional)

The vault is plain Markdown plus `[[wiki-links]]` plus YAML frontmatter — readable in any editor. When you want a polished wiki view of the vault (graph view, backlinks panel, full-text search, dark mode), `brain vault render` shells out to [Quartz](https://quartz.jzhao.xyz/), a static-site generator built specifically for Obsidian-style vaults. Brain orchestrates Quartz; it doesn't bundle it.

### Quick start (Wiki)

If you want a live wiki view of your vault at `brain.test` (Obsidian-style
graph, backlinks, full-text search, dark mode), wire up Caddy + the Quartz
workspace. This is the procedural "I want it working" path — the
how-it-works details (architecture, atomic build swap, auto-reload
mechanism) live under [Wiki rendering → Serve locally](#serve-locally)
below.

```bash
# 1. Install Caddy (skip if it's already running on your machine).
brew install caddy
brew services start caddy

# 2. Map brain.test to localhost (one-time, system-wide).
echo '127.0.0.1 brain.test' | sudo tee -a /etc/hosts

# 3. Clone Quartz into your vault as .quartz/.
git clone https://github.com/jackyzha0/quartz.git ~/brain-vault/.quartz
cd ~/brain-vault/.quartz
npm install

# 4. Drop in the brain-tuned Quartz config (graph extensions, ignore patterns,
#    reload-signal transformer registration).
cp ~/workspace/second-brain/quartz.config.ts ./quartz.config.ts

# 5. Configure Caddy to serve the live build symlink. Paste the Caddyfile
#    recipe from "Serve locally" below into /opt/homebrew/etc/Caddyfile,
#    replacing /Users/<you>/brain-vault with your actual vault path
#    (Caddy does NOT expand ~). Then reload:
brew services reload caddy

# 6. Light the wiki up. Cold start is ~40s for a ~450-doc vault.
#    (Run `~/workspace/second-brain/bin/brain-up` if `bin/` isn't on your
#    PATH yet — see the section below for the one-time PATH setup.)
brain-up
```

`brain-up` starts the vault sync watcher, applies the brain Quartz overlay,
runs the cold-start build (if `current/` is empty or unhealthy), starts the
build watcher, and opens the browser. After it returns, every save in
`~/brain-vault/` triggers a fresh background rebuild, and open tabs
auto-reload the moment the new build is swapped in. See [Daily use —
`bin/` scripts](#daily-use--bin-scripts) for the full daily workflow.

### One-time setup

Quartz is a Node.js project, so this assumes Node 18+ is on your PATH. `brain doctor` prints a `quartz/npx` line (`OK` / `not installed`) so you can tell at a glance whether you're set up.

```bash
# 1. Clone Quartz into your vault as `.quartz/`. (Quartz isn't published
#    to npm — there's no `npx quartz create`; the canonical install is
#    a git clone of the upstream repo. `brain vault render` looks for
#    the workspace at <vault>/.quartz/ by default; override with
#    --quartz-dir if you want it elsewhere.)
git clone https://github.com/jackyzha0/quartz.git ~/brain-vault/.quartz
cd ~/brain-vault/.quartz
npm install

# 2. Drop in the brain-tuned config. The sample at the brain repo root
#    has the right plugin set for vault notes (graph view, Obsidian
#    flavored markdown, ignore patterns for _templates / _attachments /
#    .quartz / .git).
cp ~/workspace/second-brain/quartz.config.ts ./quartz.config.ts
```

### Render

```bash
brain vault render
# → rendered to /…/dist (open dist/index.html or serve with `python -m http.server` from there)
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--to PATH` | `./dist` | Where to write the rendered site. Must stay under the cwd (no `..` traversal). |
| `--vault PATH` | `cfg.vault_path` | Render a different vault than the configured one. |
| `--quartz-dir PATH` | `<vault>/.quartz` | Point at a Quartz workspace elsewhere on disk. |
| `--no-build` | off | Verify the Quartz workspace is wired up correctly without running the build. |
| `--overlay` / `--no-overlay` | on | Copy `quartz_overrides/` over the workspace before building. See [Customizing the graph](#customizing-the-graph) below. |
| `--print-overlay` | off | Print the overlay plan (file pairs + rename status) and exit without copying or building. Takes precedence over `--overlay/--no-overlay`. |

The build inherits stdout/stderr so you see Quartz's progress live. Builds longer than 5 minutes are killed (assume your config is wedged); if you hit that, check `quartz.config.ts` for a runaway plugin.

### Customizing the graph

Stock Quartz ships a serviceable graph view, but brain extends it with tier coloring (vault vs. ingested), per-source coloring (krisp / slack / gmail / manual), recency-based node sizing, dashed styling for derived edges, and search-driven filter chips. These extensions live under `quartz_overrides/` in this repo and are mirrored into the user's Quartz workspace at build time by `brain vault render`.

**What the overlay does.** Right before invoking `npx quartz build`, the render command runs an *overlay* pass that copies every file under `quartz_overrides/` over the corresponding path in `<vault>/.quartz/`. The directory tree is a 1:1 mirror of its destination — `quartz_overrides/quartz.layout.ts` lands at the workspace root, and `quartz_overrides/quartz/<subdir>/<file>` lands at `<quartz_dir>/quartz/<subdir>/<file>`. One special case: stock Quartz's `quartz/plugins/emitters/contentIndex.tsx` is renamed to `_upstreamContentIndex.tsx` first, so the brain wrapper at `quartz_overrides/quartz/plugins/emitters/contentIndex.ts` can `import { ContentIndex as UpstreamContentIndex } from "./_upstreamContentIndex"`. The rename is idempotent — re-running the overlay on a workspace that already has it does nothing.

**Opt out.** Pass `--no-overlay` to skip the copy entirely and use whatever the workspace currently has. Useful for testing stock Quartz behavior or when you've hand-edited the workspace and don't want it clobbered.

```bash
brain vault render --no-overlay
```

**Inspect.** Pass `--print-overlay` to see the planned rename + copy operations without applying them or building.

```bash
brain vault render --print-overlay
# overlay plan for /Users/mshtawythug/brain-vault/.quartz:
#   rename: …/contentIndex.tsx → …/_upstreamContentIndex.tsx
#   copy:   …/quartz_overrides/quartz.layout.ts → …/.quartz/quartz.layout.ts
#   …
```

**Upgrading Quartz.** When upstream Quartz cuts a release that touches a file we override, the brain repo's vendored copy needs to be re-rebased on top of the new upstream. The brain delta is anchored by two markers: `// brain:` (value/structural choices on upstream-supported logic) and `// brain-extension:` (keys/types that don't exist in stock Quartz). The combined regex `grep -nE "brain[-:]" <file>` enumerates every change in a file. Each `quartz_overrides/` file's header comment also documents its own upgrade notes inline.

The recipe (run from the brain repo root):

```bash
cd ~/workspace/second-brain     # or wherever you cloned the repo

# 1. Pull the latest upstream copy of the file we override.
curl -L -o /tmp/upstream-Graph.tsx \
  https://raw.githubusercontent.com/jackyzha0/quartz/v4/quartz/components/Graph.tsx

# 2. Diff against the vendored copy to see the brain delta.
diff -u /tmp/upstream-Graph.tsx \
  quartz_overrides/quartz/components/Graph.tsx

# 3. Replace the vendored file with the new upstream and re-apply each
#    `// brain:` / `// brain-extension:` block from the diff.
cp /tmp/upstream-Graph.tsx quartz_overrides/quartz/components/Graph.tsx
# … hand-port the brain markers …

# 4. Smoke-test: brain vault render → open the site, verify the graph
#    still loads and the brain-specific visuals (tier colors, derived
#    edges, recency sizing) all behave.
```

### Serve locally

Brain serves the wiki as a **blue/green static site**: [Caddy](https://caddyserver.com/) serves a `current` symlink under the vault, the build watcher renders each new build into a fresh sibling directory, and an atomic symlink swap flips traffic to the new build the instant it's ready. Every request between rebuilds hits a complete, self-consistent build — no half-written window, no missing assets, no CSS that doesn't match the HTML. Quartz's own `--serve` dev server is no longer used.

```
~/brain-vault/.quartz/
  builds/
    20260501-153912-ab12cd/    ← one dir per build
    20260501-154430-ef34gh/
    20260501-155102-ij56kl/    ← active
  current → builds/20260501-155102-ij56kl/   ← Caddy serves this
```

**Caddyfile.** Paste the recipe below into `/opt/homebrew/etc/Caddyfile` (system-wide, absolute paths only — Caddy does *not* expand `~`). Replace `/Users/<you>/brain-vault` with your actual vault path:

```caddy
http://brain.test, http://localhost:8080 {
    root * /Users/<you>/brain-vault/.quartz/current
    file_server
    @build_id path /.build-id
    header @build_id Cache-Control "max-age=2, must-revalidate"
    try_files {path} {path}/ {path}.html /404.html
    encode gzip
}
```

Then `brew services reload caddy`. `localhost:8080` stays as a backwards-compat alias for anything that hardcodes the port. The `try_files` chain handles Quartz's three slug shapes (`/foo`, `/foo/`, `/foo.html`) and falls back to Quartz's own `404.html`. The `Cache-Control` override on `/.build-id` is what lets the auto-reload poller actually see new build IDs instead of a stale cached one.

**`/etc/hosts`.** One-time entry so `brain.test` resolves locally:

```bash
echo '127.0.0.1 brain.test' | sudo tee -a /etc/hosts
```

**Auto-reload.** When the build watcher swaps `current/` to a new build, every open tab reloads within ~1-2 seconds. The mechanism: `bin/brain-up` exports `BRAIN_WIKI_RELOAD=1` for the build watcher, which makes the brain Quartz overlay's `Plugin.ReloadSignal()` transformer inject a `<script src="/static/reload.js" defer>` into every page. That script polls `/.build-id` every 1 second while the tab is foregrounded, sends `If-None-Match` after the first ETag-bearing response so unchanged builds return `304 Not Modified`, pauses while the tab is backgrounded (so an idle tab in the background doesn't generate traffic), and calls `location.reload()` when the build ID changes. `brain vault render` (the one-shot prod build path) leaves `BRAIN_WIKI_RELOAD` unset, so production builds ship without the polling script — only the dev daily-use flow gates it on.

**First-build cost.** Cold start (`brain-up` against an empty `.quartz/current`) takes ~40s for a ~450-doc vault — the user sees a "first build, ~40s" message in the foreground before the script returns. Full rebuilds (rename, frontmatter change, structural edit) are also ~40s, but they happen entirely in the background under `builds/<ts>-<hash>/`; open tabs keep seeing the previous build right up to the atomic swap. The build watcher coalesces rapid edits with a 1.5s debounce, so a flurry of saves produces one rebuild rather than one per save. Old build dirs are GC'd after each swap (default keep=3, tunable via `BRAIN_WIKI_KEEP_BUILDS`); the build that `current` points at is never deleted, even if it's beyond the keep window.

**Per-file fastpath (trivial edits).** When you edit one Markdown file's body without changing structural fields (title, tags, slug, etc.), the watcher routes the edit through a per-file partial emit instead of a full rebuild. Warm fastpath builds land in ~700ms-1.8s on a ~1100-doc vault; combined with the 1s reload poll, the total edit-to-UI latency is ~2 seconds. The mechanism: a full build writes a `manifest.json` + `contentmap.json` envelope under `<vault>/.quartz/.cache/fastpath/` keyed by canonical structural fingerprints; `brain.wiki.edit_classifier` recomputes the fingerprint after each edit and routes TRIVIAL edits to a Quartz `build-partial` subcommand that re-emits only the changed file's HTML (cross-file emitters like backlinks/graph see a synthesized full corpus so they don't break). Anything non-trivial (rename, tag change, slug collision, manifest miss) falls back to the full build path. Set `BRAIN_FASTPATH_ENABLED=false` to disable the fastpath if you ever need to.

Backlinks, graph view, and full-text search all work out of the box — that's Quartz's job, not brain's.

### Daily use — `bin/` scripts

Four convenience scripts under `bin/` cover the daily flow. They assume `bin/` is on your PATH — see [Make `brain`, `brain-mcp`, and the `bin/` scripts available from any directory](#make-brain-brain-mcp-and-the-bin-scripts-available-from-any-directory) above for the one-time setup.

```bash
brain-up       # start vault sync watcher → apply Quartz overlay → cold-start
               # build (if needed) → start build watcher → open browser.
               # Idempotent.
brain-down     # stop both watchers. Caddy is left running so brain.test keeps
               # serving the last good build.
brain-rebuild  # full-corpus rebuild: embeddings → summaries → search →
               # graph → graph-weights → communities → wiki (atomic swap).
               # Runs all 7 derived-layer stages in dependency order, then
               # swaps the wiki build atomically.  Common flags:
               #   --wiki-only     run only the wiki stage (old fast path)
               #   --only STAGES   comma-separated stage ids to run
               #   --skip STAGES   comma-separated stage ids to skip
               #   --dry-run       print the plan; run nothing, take no lock
               #   --force         bypass the in-flight-ingest guard
               #   --clean-cache   wipe <vault>/.quartz/.cache/parser/ before
               #                   the wiki build (cold-build baseline)
brain-status   # show watcher state, the active build dir, the build-id pinned
               # by current/, and whether the wiki URL is reachable.
```

`brain-up` is idempotent: re-running it skips the cold-start build when `current/` is healthy and re-uses any already-running watchers. `brain-down` deliberately leaves Caddy alone — the previous build keeps serving while you iterate, and `brain.test` survives `brain-down && brain-up` cleanly. The blue/green swap means open tabs survive every rebuild — no broken assets, no half-written CSS, no flicker.

Env overrides:

| Variable | Default | Purpose |
|---|---|---|
| `BRAIN_VAULT_PATH` | `~/brain-vault` | Vault directory the watchers + builds operate against. |
| `BRAIN_WIKI_PORT` | `8080` | Port `brain-up` opens / `brain-status` curls. Caddy must be configured to listen on it (see [Serve locally](#serve-locally)). |
| `BRAIN_OPEN_BROWSER` | `1` | Set `0` to skip the auto-`open` after `brain-up`. |
| `BRAIN_WIKI_KEEP_BUILDS` | `3` | How many old build dirs under `builds/` to retain after each swap. Lets you `git diff`-style inspect prior builds. |
| `BRAIN_NO_OVERLAY` | `0` | Set `1` to skip the Quartz overlay step at startup. Useful when iterating on stock Quartz behavior. |
| `BRAIN_NO_BUILD_WATCHER` | `0` | Set `1` to skip starting the build watcher (used by the bin-script tests; also handy when debugging the sync watcher in isolation). |
| `BRAIN_FASTPATH_ENABLED` | `true` | Set `false`/`0`/`no` to disable the per-file partial-emit fastpath and force every vault edit through a full rebuild. See [Serve locally → Per-file fastpath](#serve-locally) for the trade-off. |
| `BRAIN_PY` | (unset) | Test/CI knob — overrides the Python interpreter `bin/brain-up` invokes for the watcher + build subprocesses. (`brain-rebuild` is now a Python console-script entry point that uses its venv's `sys.executable` directly; `BRAIN_PY` does not affect it.) Defaults to `<repo>/.venv/bin/python`. |

PIDs are tracked at `$BRAIN_HOME/run/brain-{watch,build}.pid` (moved off `/tmp` in `f5e551a` because macOS's `tmp_cleaner` reaped them and `brain-status` then falsely reported the daemon as stopped; override via `BRAIN_WATCH_PID` / `BRAIN_BUILD_PID`). Logs still default to `/tmp/brain-{watch,build}.log` (override `BRAIN_WATCH_LOG` / `BRAIN_BUILD_LOG`). The legacy `$BRAIN_HOME/run/brain-wiki.pid` from the old `quartz --serve` setup is still cleaned up by `brain-down` for backward compat — fresh installs won't see it.

### Deploy (optional)

`dist/` is plain HTML/CSS/JS — drop it on GitHub Pages, Netlify, S3, Cloudflare Pages, or anywhere else. Quartz's docs cover the deployment recipes; brain doesn't take a position. Note that the default config at the brain repo root has analytics turned off and `baseUrl: "localhost:8080"`; flip both before publishing the site somewhere public.

### When Quartz isn't a fit

If Quartz drifts incompatibly, gets archived, or you just want a different look: the vault format (Markdown + frontmatter + `[[wiki-links]]`) is generic enough that any Obsidian-aware renderer (MkDocs Material with the right plugins, Hugo with an Obsidian theme, your own exporter) can replace it without touching the vault folder. Brain doesn't lock you in.

## Claude integrations

The `brain` CLI and the `brain-mcp` MCP server are harness-agnostic — any agent that can run a shell command or speak MCP can use the corpus. The two worked integrations below are the ones I run: an MCP server (Claude Desktop, or any MCP-compatible client) and a skill (Claude Code). Both call the same underlying `brain` CLI.

### Claude Desktop (MCP server)

#### Configuration

The `brain-mcp` binary exposes Brain as an [MCP](https://modelcontextprotocol.io/)
server so Claude Desktop — or any other MCP-compatible client — can search,
save, author notes, inspect links, and edit entries during a chat — no
terminal required.

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`. The `env` block must match whichever backend you set in `.env` — pass `BRAIN_EMBEDDER` and the backend-specific knobs (Ollama host for `arctic`/`qwen3`, `VOYAGE_API_KEY` for `voyage`). `BRAIN_VAULT_PATH` is optional when you use the default `~/brain-vault`, but setting it explicitly makes Desktop behavior match the CLI even if the repo moves later:

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/mshtawythug/workspace/second-brain/.venv/bin/brain-mcp",
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5433/second_brain",
        "BRAIN_EMBEDDER": "arctic",
        "OLLAMA_HOST": "http://localhost:11434",
        "BRAIN_VAULT_PATH": "/Users/<you>/brain-vault",
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
| `brain_ingest_stdin` | Save text from a chat or another MCP result; auto-tags `source-mcp`. |
| `brain_tag` | Add/remove tags on an existing document and rewrite mirror frontmatter when present. |
| `brain_edit` | Update title, content type, body, or metadata; body edits re-embed. |
| `brain_backlinks` | List documents that link to a document. |
| `brain_links` | List outgoing links, optionally including unresolved refs. |
| `brain_orphans` | List docs with no incoming or outgoing links. |
| `brain_note_new` | Create a vault note from chat content without opening `$EDITOR`; auto-tags `source-mcp`. |
| `brain_daily` | Resolve or create a daily note for a date. |
| `brain_link_proposal` | Propose a `[[link]]` from one vault note to another without writing files. |
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

## Configuration and administration

Post-install operations: choosing or switching the embedder backend, cleaning up legacy data, and removing brain from the machine.

### Choosing an embedder backend

Set `BRAIN_EMBEDDER` in `.env` (or the shell). Three values are supported:

| Value | Model | Dim | Cost | Setup | Notes |
|---|---|---|---|---|---|
| `arctic` *(default)* | Snowflake Arctic Embed v2 (Apache 2.0) | 1024 | Free | Ollama + `ollama pull snowflake-arctic-embed2` (Ollama packages `Snowflake/snowflake-arctic-embed-l-v2.0` from Hugging Face under this shorter tag — same model) | Recommended. Strong retrieval quality on personal text; HNSW-indexable; fully local. |
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

### Data hygiene backfills

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

### Uninstall

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

## Codebase layout

```
src/brain/
  cli.py              — Typer app, every `brain ...` subcommand
  config.py           — env loading; selects BRAIN_EMBEDDER ∈ {arctic, voyage, qwen3}
  db.py               — psycopg connection + migration runner (schema_migrations tracked)
  embeddings.py       — three concrete embedders behind a shared Protocol
  embedding_targets.py — allowlist + identifier-safety helpers for pgvector embedding columns
  errors.py           — BrainError hierarchy
  queries.py          — read-side SQL helpers shared by CLI + MCP
  search.py           — hybrid FTS + vector via RRF
  rank_fusion.py      — shared RRF helper (search + graph retrieval both use it)
  set_similarity.py   — Jaccard helper (community membership matching)
  tags.py             — canonical casefold-lowercase + hyphenated tag normaliser
  interactions.py     — append-only feedback log (search clicks, ratings, pins) — supports both document + graph targets
  enrichment.py       — Ollama-backed summariser + tag-proposal helpers
  todo.py             — parses krisp_action_items checkboxes for `brain todo`
  format.py           — human + JSON output
  edit_session.py     — JSON-header + body editor flow
  editor.py           — $EDITOR / $VISUAL subprocess wrapper
  mcp_server.py       — FastMCP stdio server (brain_* + brain_graphrag_* tools)
  setup.py            — `brain setup` orchestration (preflight, compose render, init, doctor)
  uninstall.py        — `brain uninstall` (removes launchd plists + runtime state)
  cli_claude.py       — `brain claude install-skill` (installs the Claude Code skill)
  templates/          — packaged docker-compose.yml.j2, env.example, AGE Dockerfile, skill, Caddyfile
  ingest/             — extractors per file type + chunker + Embedder Protocol + graph sync hook
  vault/              — vault-model modules (slug, templates, frontmatter, export, links, resolver, sync, rename, graph, watch, paths)
  wiki/               — wiki rendering + serve (build_swap, build_watcher, build_partial, edit_classifier, fastpath_manifest, fastpath_state, build_homepage, build_people, build_related, slug)
  quartz_overrides/   — Quartz overlay applied to <vault>/.quartz at render time
  graph_rag/          — GraphRAG package: backend (AGE), schema (frozen value objects), extractor (Ollama), cooccur, weighting, traversal, router, communities, sync, reconcile
migrations/           — numbered SQL files (001..016) + schema_migrations tracking
bin/                  — brain-up / brain-down / brain-rebuild / brain-status convenience scripts
quartz.config.ts      — sample Quartz v4 config (copy into <vault>/.quartz/)
skills/               — claude code skills (consult-brain, brain-graph, elicit-brain, brain-todo, ingest-brain)
tests/                — real-DB pattern, fake embedder, ~3560 tests
```

## Tests

```bash
pytest                      # full suite (uses second_brain_test DB)
pytest --cov=brain          # with coverage
```

## License

[MIT](LICENSE)
