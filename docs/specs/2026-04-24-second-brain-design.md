# Second Brain — Design Spec

**Date:** 2026-04-24
**Owner:** Pat Morgan
**Status:** Draft — pending approval

## Goal

A local, personal knowledge base that Claude can query from any conversation. Seeds with `~/Documents/career/` (career artifacts, interview prep, Acme reference, meeting preps) and supports ongoing ingestion from Krisp calls, Gmail, and Slack from day one. Claude stays the reasoning layer; the brain is purely storage + retrieval.

## In scope (v1)

- File ingestion: PDF, DOCX, Markdown, plain text
- Gmail ingestion (via `gws` CLI, explicit scope required — no bulk-inbox ingest)
- Krisp ingestion (Claude-orchestrated via MCP → piped into brain)
- Slack ingestion (Claude-orchestrated via MCP → piped into brain)
- Hybrid search (FTS + vector via RRF)
- Manual tags and filters

## Non-goals (v1)

- Web UI or GUI
- Multi-user / multi-tenant
- Cloud hosting or sync
- Automatic file-watcher or scheduled/cron ingestion
- Reranking models
- Direct Krisp / Slack REST API integration (we rely on MCP orchestration instead)

## Architecture

```
┌────────────────────────────────────────┐
│  Claude conversation (Code / Desktop)  │
│             │                          │
│             │  Bash: brain search "…"  │
│             ▼                          │
│     ┌──────────────────────┐           │
│     │  brain CLI (Python)  │           │
│     └─────┬──────────┬─────┘           │
│           │          │                  │
│     ingest│          │query             │
│           │          │                  │
│           ▼          ▼                  │
│    ┌──────────────────────┐            │
│    │ Voyage API (embed)   │            │
│    └──────────────────────┘            │
│           │                             │
│           ▼                             │
│  ┌─────────────────────────┐           │
│  │ Postgres 16 + pgvector  │           │
│  │ (Docker, port 5433)     │           │
│  │ - sources               │           │
│  │ - documents             │           │
│  │ - chunks (embedding)    │           │
│  └─────────────────────────┘           │
└────────────────────────────────────────┘
```

**Flow — ingest:** `brain ingest foo.pdf` → extract text → paragraph-aware chunk → embed chunks via Voyage `voyage-3-large` → write `sources` + `documents` + `chunks` rows → done. Idempotent on content hash.

**Flow — query:** `brain search "..."` → embed query → hybrid search (FTS + cosine via pgvector) → Reciprocal Rank Fusion (RRF) ranking → return top N documents with best-matching snippets → Claude reads snippets, calls `brain show <id>` if deeper context needed.

## Storage — Postgres + pgvector

**Host:** Docker container, Postgres 16 image with `pgvector` extension, port **5433** (avoids conflict with another local project on 5432). Data volume persists under `./data/postgres` in the project directory.

**Schema** (migration `001_init.sql`):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid

CREATE TABLE sources (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind         TEXT NOT NULL,            -- 'manual' | 'krisp' | 'gmail' | 'slack' | ...
  external_id  TEXT,                      -- krisp meeting id, gmail message id, etc.
  metadata     JSONB NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(kind, external_id)
);

CREATE TABLE documents (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id     UUID REFERENCES sources(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  content_hash  TEXT NOT NULL UNIQUE,     -- sha256 of content, prevents dup ingestion
  content_type  TEXT NOT NULL,            -- 'pdf' | 'markdown' | 'txt' | 'transcript' | 'email'
  source_path   TEXT,                      -- original file path or URL
  tags          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  metadata      JSONB NOT NULL DEFAULT '{}',
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tsv           tsvector GENERATED ALWAYS AS
                  (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))) STORED
);
CREATE INDEX documents_tsv_idx   ON documents USING GIN(tsv);
CREATE INDEX documents_tags_idx  ON documents USING GIN(tags);
CREATE INDEX documents_source_idx ON documents(source_id);

CREATE TABLE chunks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index  INT NOT NULL,
  content      TEXT NOT NULL,
  embedding    vector(1024) NOT NULL,     -- voyage-3-large dims
  metadata     JSONB NOT NULL DEFAULT '{}',
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  UNIQUE(document_id, chunk_index)
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx       ON chunks USING GIN(tsv);
CREATE INDEX chunks_document_idx  ON chunks(document_id);
```

**Why chunks are a separate table:** embeddings want paragraph-level granularity for precision; documents keep the full text together for reading and FTS headlines. Search ranks chunks, groups by document, returns top-N documents with their top matching chunks as snippets.

## Ingestion pipeline

**Extractors (`src/brain/ingest/`):** one module per content type.
- `pdf.py` — `pypdf` for simple PDFs, fallback to `pdfplumber` for layout-heavy docs. Strips page headers/footers with a simple heuristic (lines repeated across >50% of pages).
- `docx.py` — `python-docx` for Word documents. Preserves paragraphs, unwraps tables into tab-separated rows, includes lists.
- `markdown.py` — `markdown-it-py` → plain text, preserves heading hierarchy in a small metadata structure.
- `text.py` — pass-through for `.txt` source.
- `gmail.py` — invokes `gws gmail` CLI via subprocess, parses message list + body per message.
- `stdin.py` — accepts arbitrary text via stdin for Claude-orchestrated sources (Krisp, Slack, any future MCP-backed source).

**Chunker (`chunker.py`):** paragraph-aware with token-count target.
- Split on blank lines first (paragraphs).
- Target: **~600 tokens per chunk**, **100-token overlap** between consecutive chunks within the same document.
- Never split inside a paragraph unless the paragraph itself exceeds 1000 tokens (then split on sentence boundaries).
- Token counting via Voyage's tokenizer (SDK exposes it).

**Embedder (`embeddings.py`):** thin wrapper around the `voyageai` SDK.
- Batches up to 128 chunks per API call (Voyage's limit), with a max of 120K tokens per request.
- Retries 3 times on transient errors with exponential backoff.
- `VOYAGE_API_KEY` read from `.env` via `python-dotenv`.

**Idempotency:** `content_hash` (sha256 of extracted text) is UNIQUE on `documents`. Re-ingesting the same file is a no-op. `brain ingest --force` overrides by deleting the old row first.

## Search — hybrid algorithm

Hybrid FTS + vector via **Reciprocal Rank Fusion (RRF)**, which is provably robust and avoids score-normalization headaches.

```
1. Embed the query via Voyage (with input_type='query').
2. Query A — vector:
     SELECT document_id, id, content, 1 - (embedding <=> $1) AS score
     FROM chunks
     ORDER BY embedding <=> $1
     LIMIT 50;
3. Query B — FTS:
     SELECT document_id, id, content,
            ts_rank(tsv, plainto_tsquery('english', $2)) AS score
     FROM chunks
     WHERE tsv @@ plainto_tsquery('english', $2)
     ORDER BY score DESC
     LIMIT 50;
4. RRF combine: for each chunk, rrf_score = Σ 1 / (k + rank_in_list), k=60.
5. Group by document_id, take that doc's best chunk as snippet, sort docs by max rrf_score.
6. Return top N (default 5) documents: { id, title, source_kind, snippet, score }.
```

**Filters:** `--source <kind>`, `--tag <name>`, `--since <relative>` (e.g., `7d`) narrow the search before ranking. `--limit N` overrides default 5.

## CLI surface

Written with **Typer** (Click-based, first-class type hints, auto-help, clean subcommand structure). Installed as `brain` via `pyproject.toml` entry point.

```
# File ingestion
brain ingest <path> [--tag TAG]... [--force]
brain ingest-dir <path> [--tag TAG]... [--ext pdf,docx,md,txt] [--dry-run]

# Gmail — at least one scope flag is REQUIRED (refuses bulk inbox ingest)
brain ingest-gmail (--query Q | --label L | --from EMAIL | --since DATE | --until DATE)...
                   [--tag TAG]... [--max N] [--dry-run]

# Stdin — for Claude-orchestrated sources (Krisp, Slack, or ad-hoc)
brain ingest-stdin --source KIND --external-id ID --title TITLE
                   [--content-type TYPE] [--tag TAG]... [--metadata JSON] [--date DATE]

# Search & browse
brain search <query> [--limit N] [--source KIND] [--tag TAG] [--since DUR] [--json] [--fts-only]
brain show <id> [--json]
brain list [--source KIND] [--tag TAG] [--limit N] [--json]
brain tag <id> <+tag|-tag>...
brain rm <id>

# Admin
brain status                      # counts, DB size, last ingest time
brain init                        # creates DB schema (runs migrations)
brain doctor                      # checks Postgres reachable, Voyage key set, gws CLI available, extension installed
```

**Gmail scope enforcement:** `brain ingest-gmail` exits with error if invoked with zero scope flags. At least one of `--query`, `--label`, `--from`, `--since`, or `--until` must be provided. `--since` and `--until` accept `2026-04-01` or relative (`7d`, `30d`). Combine flags freely (e.g., `--from jane.doe@example.com --since 30d`).

**Output:**
- Default: human-readable. Color-coded with `rich`. Snippets show matched terms highlighted.
- `--json`: machine-readable, for when Claude parses results programmatically.

**IDs:** UUIDs are unwieldy to type. `brain` accepts a UUID prefix (min 6 chars) and disambiguates on collision.

## File layout

```
~/workspace/second-brain/
├── docker-compose.yml           # Postgres 16 + pgvector, port 5433, volume ./data/postgres
├── .env.example                  # VOYAGE_API_KEY=, DATABASE_URL=postgres://...:5433/second_brain
├── .gitignore                    # .env, data/, __pycache__, .pytest_cache
├── pyproject.toml                # deps + `brain` entry point
├── README.md                     # setup, first-ingest, how Claude uses it
├── docs/specs/
│   └── 2026-04-24-second-brain-design.md   # this file
├── migrations/
│   ├── 001_init.sql
│   └── README.md                 # how to apply (brain init or manual psql)
├── src/brain/
│   ├── __init__.py
│   ├── cli.py                    # Typer app, all commands
│   ├── config.py                 # env loading, DB URL, paths
│   ├── db.py                     # psycopg connection pool, migration runner
│   ├── embeddings.py             # Voyage wrapper (batching, retries)
│   ├── ingest/
│   │   ├── __init__.py           # dispatch by extension
│   │   ├── pdf.py
│   │   ├── docx.py
│   │   ├── markdown.py
│   │   ├── text.py
│   │   ├── gmail.py              # shells out to gws gmail CLI
│   │   ├── stdin.py              # generic stdin ingester for Claude-orchestrated sources
│   │   └── chunker.py
│   ├── search.py                 # hybrid RRF logic
│   └── format.py                 # human + JSON output formatters
└── tests/
    ├── conftest.py               # real Postgres (second_brain_test DB), fake embedder, mocked gws subprocess
    ├── fixtures/                 # tiny sample PDFs, DOCXs, MDs, txts, gmail json
    ├── test_chunker.py
    ├── test_ingest_pdf.py
    ├── test_ingest_docx.py
    ├── test_ingest_markdown.py
    ├── test_ingest_gmail.py      # mocks gws CLI subprocess, asserts scope-flag enforcement
    ├── test_ingest_stdin.py
    ├── test_search.py
    └── test_cli.py               # full end-to-end via CliRunner
```

## Dependencies (pyproject.toml)

- `psycopg[binary]` (v3) — Postgres driver
- `pgvector` — Python adapter for `vector` type
- `voyageai` — official SDK
- `pypdf` — primary PDF extractor
- `pdfplumber` — fallback for layout-heavy PDFs
- `python-docx` — DOCX extractor
- `markdown-it-py` — MD → text
- `typer` — CLI
- `rich` — colored output + snippet highlighting
- `python-dotenv` — env loading
- **Dev:** `pytest`, `pytest-cov`, `ruff`, `mypy`

**External CLI dependency:** `gws` (Google Workspace CLI) — already installed on the system, invoked via subprocess by `brain ingest-gmail`.

## Testing

**Approach:** real Postgres (a separate `second_brain_test` database in the same Docker container), fake embedder (deterministic hash-based vectors). No DB mocks.

**Coverage targets:**
- `chunker.py`: 100% (pure function)
- `search.py`: 95%
- `ingest/*`: 90%
- `cli.py`: 85% via `CliRunner`

**Key test cases:**
- Chunker splits on paragraphs, respects 600/100 token budget, handles oversized paragraphs via sentence splitting.
- PDF extractor handles single-page, multi-page, header/footer stripping.
- Ingest is idempotent: ingest same file twice → one row. `--force` replaces.
- Search: FTS-only match, vector-only match, hybrid match, filters (source, tag, since).
- CLI: all commands exercised, `--json` schema stable.

## Setup flow (first run)

1. `cp .env.example .env` → paste Voyage key
2. `docker compose up -d` → Postgres boots on 5433
3. `pip install -e .` (or `uv sync`)
4. `brain init` → applies migrations, enables `vector` extension
5. `brain doctor` → confirms healthy
6. `brain ingest-dir ~/Documents/career` → seeds the brain
7. In any future Claude conversation, Claude runs `brain search "..."` via Bash when it needs context

## How Claude uses this

New Claude conversations need to be told the brain exists. We add a block to `~/.claude/CLAUDE.md` (loads into every Claude Code conversation globally) covering:

- **Retrieval** — when and how to invoke `brain search "..."` / `brain show <id>`, trigger topics (Pat's career, Acme, interviews, past meetings, deals, relationships), JSON output flag for parsing.
- **Ingestion orchestration** — when the user asks to "ingest Krisp calls" or "pull recent Slack threads," the pattern: Claude calls the relevant MCP tools, pipes content into `brain ingest-stdin` with proper `--source`, `--external-id`, `--title`, and `--metadata`. Sample commands included.
- **Gmail** — `brain ingest-gmail` refuses bulk ingest; Claude must pick sensible scope flags (query, label, from, date range) based on the user's ask.

A `reference` auto-memory entry provides a belt-and-suspenders pointer.

## Claude-orchestrated ingestion (Krisp, Slack)

Krisp and Slack don't have CLIs — they expose Claude-side MCP tools. The `brain` CLI can't invoke MCP directly, so Claude is the orchestrator:

**Krisp flow** (user says "ingest last week's Krisp calls"):
1. Claude calls `mcp__claude_ai_Krisp__search_meetings` / `list_activities` to enumerate meetings.
2. For each meeting, Claude calls `mcp__claude_ai_Krisp__get_multiple_documents` to pull the transcript.
3. Claude pipes the transcript into:
   ```bash
   echo "<transcript text>" | brain ingest-stdin \
     --source krisp --external-id <meeting_id> \
     --title "<meeting title>" --content-type transcript \
     --date <meeting_date> --metadata '{"participants":[...],"duration_min":N}'
   ```
4. `brain` dedups on `(source_kind, external_id)` so repeated runs are safe.

**Slack flow** is symmetric — `mcp__claude_ai_Slack__slack_read_thread` / `slack_search_public_and_private` → `brain ingest-stdin --source slack --external-id <ts> --title "<channel> — <first line>"`.

**Instructions for Claude** live in `~/.claude/CLAUDE.md` so every conversation knows the pattern.

## Future additions — architecturally accommodated

- **Direct Krisp / Slack REST API** — replaces Claude orchestration with a fully autonomous `brain ingest-krisp --since 7d`. Requires API keys. Deferred until we see how often it's needed.
- **Reranking** — if hybrid RRF isn't good enough, add `voyage-rerank-2` as a post-processing step on top-50 candidates.
- **File watcher** — daemon that auto-ingests new files in watched directories (e.g., `~/Documents/career/`).
- **Scheduled ingestion** — cron / launchd job that runs `brain ingest-gmail --label inbox --since 1d` nightly.

All additive — no schema changes, no CLI rewrites.

## Risks & open questions

- **PDF extraction quality** — some PDFs (scanned, complex layouts) will extract poorly. v1 accepts this; if it's a problem, we add OCR (`pytesseract`) later.
- **Voyage API availability** — hard dependency. If Voyage is down, ingestion fails but querying still works (FTS only). `brain search --fts-only` flag provided as escape hatch.
- **Security of `.env`** — contains Voyage key. `.gitignore` covers it; repo should stay private (this is a local-only tool, no plans to publish).
- **Data sensitivity** — Krisp transcripts and emails are sensitive. Voyage's stated policy is no training on API data, but the content does leave your machine during embedding. Acknowledged tradeoff.
