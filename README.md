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
#    On a fresh DB this just finalizes the column (NOT NULL + HNSW
#    index when applicable); on a re-ingest it backfills any NULL rows
#    first, then finalizes.
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

### Make `brain` and the `bin/` scripts available from any directory

By default, `brain` only works inside this folder with the venv activated, and
the `bin/brain-up` / `bin/brain-down` / `bin/brain-status` scripts have to be
invoked by full path. Two small one-time edits fix both.

**1. Symlink the `brain` launcher onto your PATH** so `brain` works from any
shell — including Claude Code in any project:

```bash
# macOS (Homebrew):
ln -s ~/workspace/second-brain/.venv/bin/brain /opt/homebrew/bin/brain

# Linux (or non-Homebrew macOS):
ln -s ~/workspace/second-brain/.venv/bin/brain ~/.local/bin/brain
```

The symlink works without `source .venv/bin/activate` because `pip install -e`
gives the launcher an absolute-path shebang pointing at the venv's Python.

**2. Add `bin/` to your shell's PATH** so `brain-up` / `brain-down` /
`brain-status` work from anywhere. Pick the snippet for your shell:

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

**3. Verify both:**

```bash
which brain          # → /opt/homebrew/bin/brain (or ~/.local/bin/brain)
which brain-up       # → /Users/<you>/workspace/second-brain/bin/brain-up
brain doctor         # → all OK
brain-status         # → wiki/watcher status (both stopped initially)
```

Once both are on PATH, the daily flow is just `brain-up` / `brain-down` —
no need to remember Quartz or watcher invocations.

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

Runs as a daemon (Ctrl-C to stop). Filesystem events trigger debounced (500ms) per-file syncs — edit a note, save, and within a beat the chunk + embedding update in the DB. Skips `_templates/`, `_attachments/`, hidden directories. The `bin/brain-up` script kicks this off alongside the wiki dev server.

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

## Wiki rendering (Quartz)

The vault is plain Markdown plus `[[wiki-links]]` plus YAML frontmatter — readable in any editor. When you want a polished wiki view of the vault (graph view, backlinks panel, full-text search, dark mode), `brain vault render` shells out to [Quartz](https://quartz.jzhao.xyz/), a static-site generator built specifically for Obsidian-style vaults. Brain orchestrates Quartz; it doesn't bundle it.

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

Use Quartz's own dev server — it handles slug-to-file resolution and SPA routing correctly (Python's `http.server` 404s on Quartz's clean URLs and would need a separate SPA-fallback config):

```bash
cd ~/brain-vault/.quartz
npx quartz build --serve --port 8080 --directory ~/brain-vault
open http://localhost:8080
```

The dev server hot-reloads when vault files change — pair it with `brain vault sync --watch` and edits flow disk → DB → wiki without manual rebuilds.

Backlinks, graph view, and full-text search all work out of the box — that's Quartz's job, not brain's.

### Daily use — `bin/` scripts

To avoid memorizing the watcher + dev-server invocations, three convenience scripts live under `bin/`:

```bash
brain-up      # starts watcher + Quartz dev server, opens browser. Idempotent.
brain-down    # stops both.
brain-status  # shows pids, log paths, and whether the wiki is reachable.
```

These assume `bin/` is on your PATH — see [Make `brain` and the `bin/` scripts available from any directory](#make-brain-and-the-bin-scripts-available-from-any-directory) above for the one-time setup.

Env overrides honored by the scripts: `BRAIN_VAULT_PATH` (default `~/brain-vault`), `BRAIN_WIKI_PORT` (default `8080`), `BRAIN_OPEN_BROWSER` (default `1`; set `0` to skip auto-open). PIDs are tracked at `/tmp/brain-{wiki,watch}.pid`; logs at `/tmp/brain-{wiki,watch}.log`.

### Deploy (optional)

`dist/` is plain HTML/CSS/JS — drop it on GitHub Pages, Netlify, S3, Cloudflare Pages, or anywhere else. Quartz's docs cover the deployment recipes; brain doesn't take a position. Note that the default config at the brain repo root has analytics turned off and `baseUrl: "localhost:8080"`; flip both before publishing the site somewhere public.

### When Quartz isn't a fit

If Quartz drifts incompatibly, gets archived, or you just want a different look: the vault format (Markdown + frontmatter + `[[wiki-links]]`) is generic enough that any Obsidian-aware renderer (MkDocs Material with the right plugins, Hugo with an Obsidian theme, your own exporter) can replace it without touching the vault folder. Brain doesn't lock you in.

## Architecture

The design is captured across three specs and one set of phase-by-phase implementation plans. Read the specs for *why* and *what*; read the plans for the task-by-task breakdown that actually shipped.

### Specs (`docs/specs/`)

| Spec | What it covers |
|---|---|
| [`2026-04-24-second-brain-design.md`](docs/specs/2026-04-24-second-brain-design.md) | Original v1 — Postgres + pgvector schema, hybrid FTS+vector search via Reciprocal Rank Fusion, ingestion pipeline (PDF/DOCX/MD/TXT, Gmail, Krisp/Slack via stdin), CLI surface. The foundation everything else builds on. |
| [`2026-04-27-mcp-server-design.md`](docs/specs/2026-04-27-mcp-server-design.md) | FastMCP server exposing brain tools (`brain_search`, `brain_show`, `brain_list`, `brain_status`, `brain_ingest_stdin`, `brain_tag`, `brain_edit`) so Claude Desktop can call them in any conversation. Stdio transport, error wrapping, warmup embed. |
| [`2026-04-28-vault-model-design.md`](docs/specs/2026-04-28-vault-model-design.md) | The current model — vault folder of `.md` files as source of truth for authored notes, sync engine + watcher, `[[wiki-links]]` graph, Quartz-rendered wiki. Two-tier corpus (vault + ingested). |

### Plans (`docs/plans/`)

| Plan | What it shipped |
|---|---|
| [`2026-04-24-second-brain.md`](docs/plans/2026-04-24-second-brain.md) | v1 build-out — schema, ingest extractors, hybrid search, CLI. |
| [`2026-04-26-brain-edit.md`](docs/plans/2026-04-26-brain-edit.md) | `brain edit` JSON-header + body editor flow for in-place updates. |
| [`2026-04-27-mcp-server.md`](docs/plans/2026-04-27-mcp-server.md) | MCP server implementation per the design above. |
| [`2026-04-28-local-embeddings-qwen3-8b.md`](docs/plans/2026-04-28-local-embeddings-qwen3-8b.md) | Pluggable embedder backends (arctic / voyage / qwen3) behind a single `Embedder` Protocol. |
| `2026-04-{28,29}-vault-model-phase-{1..7}.md` | Vault model rollout: schema + export, sync engine + wiki-link parser, authoring CLI, link graph queries, watcher, Quartz render integration, MCP additions. Each phase shipped independently with its own review + audit loop. |

### Codebase layout

```
src/brain/
  cli.py              — Typer app, every `brain ...` subcommand
  config.py           — env loading; selects BRAIN_EMBEDDER ∈ {arctic, voyage, qwen3}
  db.py               — psycopg connection + migration runner (schema_migrations tracked)
  embeddings.py       — three concrete embedders behind a shared Protocol
  errors.py           — BrainError hierarchy
  queries.py          — read-side SQL helpers shared by CLI + MCP
  search.py           — hybrid FTS + vector via RRF
  format.py           — human + JSON output
  edit_session.py     — JSON-header + body editor flow
  editor.py           — $EDITOR / $VISUAL subprocess wrapper
  mcp_server.py       — FastMCP stdio server
  ingest/             — extractors per file type + chunker + Embedder Protocol
  vault/              — vault-model modules
    slug.py           — deterministic ASCII slugifier
    templates.py      — _templates/ rendering ({{title}}, {{date}}, ...)
    frontmatter.py    — YAML frontmatter parse / dump / body_hash
    export.py         — DB → vault one-shot dump
    links.py          — wiki-link parser ([[X]], [[X|Y]], ![[X]], [[brain:id]])
    resolver.py       — title / alias / id / source-external resolution
    sync.py           — vault → DB reconciliation; sync_one_file helper
    rename.py         — note rename with [[]] reference rewrite + atomic restore
    graph.py          — backlinks / outgoing / orphans / graph queries
    graph_format.py   — JSON / DOT / Mermaid emitters
    watch.py          — fsnotify watcher with debounce + drain on shutdown
migrations/           — numbered SQL files (001..004) + schema_migrations tracking
bin/                  — brain-up / brain-down / brain-status convenience scripts
quartz.config.ts      — sample Quartz v4 config (copy into <vault>/.quartz/)
docs/specs/           — design specs (above)
docs/plans/           — implementation plans (above)
tests/                — real-DB pattern, fake embedder, ~825 tests
```

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
