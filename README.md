# Second Brain

Local, queryable knowledge base and note vault with hybrid search and an entity-graph layer — designed to be searched by any AI coding agent or assistant from any conversation.

Stores career documents, interview prep, Krisp call transcripts, Slack threads, Gmail messages, and authored Markdown notes in Postgres + pgvector. Hybrid search ranks results with Reciprocal Rank Fusion over full-text rank and vector cosine similarity, plus recency weighting and metadata filters. The embedding backend is pluggable — defaults to local Snowflake Arctic Embed v2 over Ollama (free, no cloud dependency); Voyage AI and Qwen3-Embedding-8B are also supported.

On top of plain search it adds: a **GraphRAG** layer (an entity graph of people, orgs, and concepts in Apache AGE) for themes, patterns, and connections across interactions; optional **LLM enrichment** that auto-summarizes and tags ingested documents; and an Obsidian-style **vault** of wiki-linked notes that can be published as a browsable **Quartz wiki**. Any agent reaches all of this through the `brain` CLI or the bundled `brain-mcp` MCP server — with ready-made skills for Claude Code, and the MCP server dropping into Claude Desktop or any MCP-compatible client.

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
- **Docker Desktop** (or Docker Engine) — running. The Postgres + pgvector database lives in a container on port 55432. Get it from [docker.com](https://www.docker.com/products/docker-desktop/).
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
  - 8 pre-flight checks (Docker daemon, Ollama, port 55432 + 8080 free, Caddy installed, `~/.claude/skills/` writable, pinned Quartz commit reachable on GitHub).
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
| Storage | PostgreSQL 16 + [`pgvector`](https://github.com/pgvector/pgvector) | One database for both lexical (`tsvector`) and semantic (vector) search — no separate vector store to operate. Runs in Docker on port 55432. |
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

# Rank diagnostics: why did these results surface, and in what order?
brain explain "platform migration tradeoffs"
brain explain "platform migration tradeoffs" --verbose --json
```

ID prefixes must be at least 6 hex characters and must uniquely identify one
document. `--json` is the machine-readable path for agents or scripts.

`brain explain` runs the same query as `brain search` but prints per-result
ranking diagnostics — FTS rank, vector cosine, RRF contributions, and the
recency boost — so you can see *why* a result surfaced and in what order. Add
`--verbose` to show which filters were active, or `--json` for the full
`SearchExplanation` payload. It accepts the same filter flags as `search`
(`--source`, `--tag`, `--since`, `--person`, `--after`/`--before`, `--kind`, …).

`--person` resolves the name or email you pass against the directory and
matches docs recorded under **any** known variant of that person — space-form
(`jane doe`), dot-form (`jane.doe`), bare email, and every `Name <email>`
combination the sources emitted — so Gmail-header and Krisp-label spellings of
the same person all count as one identity.

`--since` takes a bare number or a duration suffix — `7d` (days), `24h`
(hours), `90m` (minutes) — on the commands that share this parser: `search`,
`explain`, `todo`, `enrich`, and `gaps` (bare number = **days**), plus `brain
brief` (`--since` bare = **hours**; its `--todo-since` bare = days). Bare
numbers keep each command's existing unit, so old scripts are unaffected. The
remaining `--since` flags are unchanged and do **not** take suffixes:
`ingest-gmail` expects a Gmail `YYYY/MM/DD` date, `timeline` an ISO `YYYY-MM`
month, and `gaps push` a plain integer day count.

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

## License

[MIT](LICENSE)
