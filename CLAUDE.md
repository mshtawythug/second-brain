# CLAUDE.md

IMPORTANT:
1. You must write automated tests for all code (pytest). Aim for both unit tests and integration tests against a real Postgres test database.
2. You must pass ALL tests before committing.
3. Maintain a minimum test coverage threshold of 85%. Per-module targets: pure logic (chunker, search, format) 95%, ingest pipeline 90%, CLI commands 85%.
4. **NEVER commit or push without explicit user permission.** No exceptions — even in bypass permissions mode.
5. USE Team mode (TeamCreate + teammates) for any multi-task work to keep the main context window clear. **Always use team-driven execution** (not inline) when executing implementation plans. See "Team Mode Override" section below.
6. After edits, run the full test suite (`pytest`) and lint (`ruff check`). Fix bugs and update tests before claiming work complete.
7. NEVER jump straight to code. Produce a written plan FIRST for any multi-file task. Get explicit approval before writing code.
8. When referencing existing modules/functions, READ the actual source file first. Never guess field names, import paths, or function signatures.
9. All Python code MUST pass `ruff check` with zero warnings AND `mypy src/` with zero errors. See "Linting" below.
10. **Pre-commit check:** Run `ruff check && mypy src/ && pytest` before committing.
11. **Memory updates:** After completing a task that adds/changes any of the following, update the corresponding memory file in the auto-memory directory: CLI commands → `cli.md`, database schema → `schema.md`, Python types/dataclasses → `types.md`, ingest extractors → `extractors.md`, search algorithm changes → `search.md`.
11a. **Memory audits:** When asked to "update memory", audit ALL memory files against the current codebase using parallel Explore agents (one per file). Update any drifted content and the MEMORY.md index.
11b. **Plans, specs & docs:** ALL plans, specs, design docs, and any `.md` files created as part of work MUST be stored under `docs/` — specs in `docs/specs/YYYY-MM-DD-<topic>-design.md`, plans in `docs/plans/YYYY-MM-DD-<topic>.md`. Never leave planning/spec documents in the repo root.
12. **MANDATORY regression tests for bug fixes:** Every bug fix MUST include a test that reproduces the original bug and verifies the fix. No bug fix is complete without a regression test.
13. **NO monkey-patching in tests. NO EXCEPTIONS.**
    - **BANNED:** Reopening production modules/classes to inject constants, methods, or attributes.
    - **BANNED:** Direct attribute assignment on imported modules without restoration.
    - **If a test needs monkey-patching to pass, the production code has a bug.** Fix the production code, not the test.
    - `monkeypatch` (pytest), `unittest.mock.patch`, and `mocker.patch` (pytest-mock) are NOT monkey-patching — they are standard test doubles with automatic cleanup. These are fine.
14. **Post-phase code review + completion audit LOOP (MANDATORY):** After completing each phase/wave of a plan, run a **review + audit loop** that repeats until BOTH pass with zero issues:
    - **Code review:** Dispatch `superpowers:code-reviewer` on ALL changes in the phase against the plan spec.
    - **Completion audit:** Dispatch a separate completion auditor checking: (a) every task implemented — nothing skipped, (b) every fix matches spec exactly, (c) every required test exists and passes, (d) no regressions, (e) no dead code — no orphaned modules, unused functions, or untested branches.
    - **THE LOOP:** If EITHER review finds issues → fix ALL issues → re-run BOTH → repeat. Exit condition: code reviewer says "APPROVED" AND auditor says "AUDIT PASSED". No phase is complete until the loop exits clean.
15. **No PII in checked-in code. NO EXCEPTIONS.** The repo has been explicitly PII-scrubbed across tests, fixtures, docs, and comments — never reintroduce real personal data. Use synthetic / redacted values for names, email addresses, phone numbers, postal addresses, meeting attendees, calendar specifics, company names, customer references, and internal project codenames. When fixing or extending tests, copy the existing synthetic pattern; never paste production data (e.g. real meeting transcripts, real email bodies, real chat threads, employer / coworker names) even temporarily. If a real value seems required for a test to be meaningful, the test is wrong — parameterize the fixture or refactor the production code so it's testable with synthetic data. This rule covers commit messages and PR descriptions too.

## Team Mode Override (MANDATORY — overrides superpowers skill routing)

**ALL agent dispatching MUST use Team mode** (TeamCreate + Agent with team_name) instead of standalone Agent subagents. This applies to every superpowers skill that spawns agents:

| Superpowers skill | Use instead | What changes |
|---|---|---|
| `superpowers:subagent-driven-development` | `team-driven-development` skill | TeamCreate at start, teammates with worktree isolation, SendMessage for coordination |
| `superpowers:dispatching-parallel-agents` | `team-parallel-dispatch` skill | TeamCreate at start, parallel teammates with worktree isolation |
| `superpowers:requesting-code-review` | Dispatch reviewer as teammate on existing team (if one exists), otherwise standalone Agent | Add team_name if team active |
| `superpowers:executing-plans` | Use `team-driven-development` instead of `subagent-driven-development` when it suggests subagents | Same redirect |
| `superpowers:writing-plans` execution handoff | **NEVER offer a choice.** Skip the two-option prompt and proceed directly with `team-driven-development`. | No "Inline Execution" option, no question asked |

**Non-agent superpowers skills are unchanged:** brainstorming, verification-before-completion, using-git-worktrees, finishing-a-development-branch, test-driven-development, systematic-debugging, receiving-code-review, writing-skills.

**Why:** Team mode provides shared task lists, inter-teammate communication via SendMessage, and better coordination visibility than isolated subagents.

**NEVER** fall back to standalone Agent subagents for execution or parallel dispatch.

**Plan header override:** When writing plan documents, use `> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.`

## Architecture Principles

### DRY
- Search for existing shared code before creating new modules/utilities.
- Reusable patterns: `src/brain/` for shared utilities. Extract anything used in 2+ places.
- Prefer composition over copy-paste.

### SOLID Principles (MANDATORY)
- **Single Responsibility**: Each module has ONE reason to change. Extractors only extract. The chunker only chunks. The embedder only embeds. The search module only searches. The CLI only orchestrates.
- **Open/Closed**: Open for extension, closed for modification — adding a new ingest source (e.g., `notion.py`) means adding a new extractor module + dispatcher entry, not editing existing extractors.
- **Liskov Substitution**: All extractors return `ExtractedDoc`. All search backends conform to the same result shape. Callers don't care which subclass.
- **Interface Segregation**: Modules expose narrow, focused public APIs. Don't force a consumer to import a kitchen-sink module to get one helper.
- **Dependency Inversion**: High-level modules accept dependencies via constructor / function args (e.g., `count_tokens` callable on the chunker, `client` on the embedder). Tests pass fakes; production passes real ones.

### Coding Conventions
- PEP 8, `snake_case` everywhere.
- Type hints on every function signature. `mypy src/` must pass.
- f-strings for string formatting (no `%` or `.format()`).
- `pathlib.Path` for filesystem paths, never raw strings.
- Dataclasses for value objects (`ExtractedDoc`, `Chunk`, `SearchResult`).
- Custom exceptions inherit from a project-specific base (`BrainError` if needed); never `raise Exception(...)`.
- **Every module has a one-line docstring** at the top describing its purpose.
- **Use parameterized SQL** (`%s` placeholders + tuple). NEVER concatenate user input into SQL strings.
- **Every external HTTP/DB client MUST have explicit timeouts.** Voyage SDK retries handled in `embeddings.py`; Postgres connections set `connect_timeout`.
- **No bare `except:`** — always catch specific exceptions (`psycopg.OperationalError`, `voyageai.error.RateLimitError`, etc.).

### Linting — Ruff + mypy
Run after every change: `ruff check` (lint) or `ruff check --fix` (auto-fix), then `mypy src/`. Config in `pyproject.toml` under `[tool.ruff]` and `[tool.mypy]`.

**Key rules:** Line length 100, target Python 3.11, `from __future__ import annotations` not needed (3.11+ has native PEP 604 union syntax). Sort imports with `ruff check --select I --fix`.

### Eval gate (CI)
The eval-marker harness (`tests/test_eval_harness_live.py`) is **excluded from the default pytest invocation** (`pyproject.toml` → `addopts = "... -m 'not eval'"`). Rationale: it requires a live Postgres + Ollama, would slow every local `pytest` run, and the threshold assertions assume the live brain corpus.

**CI enforces it separately.** `.github/workflows/eval.yml` runs on every PR + every push to `master`:

1. Spins up Postgres 16 + pgvector as a service container on port 5433.
2. Installs the package with `pip install -e ".[dev]"`.
3. Runs `pytest -m eval --no-cov -v` — gate fails on any harness regression.
4. Conditionally runs `brain eval --baseline ci --diff --fail-below` when `tests/eval/baselines/ci.json` exists (added by Wave A.4).

**Decision recorded (Wave A.1):** the eval marker stays OFF in the default `pytest` invocation. Gate lives in CI only. Local devs run `pytest -m eval` manually when needed.

**Updating a committed baseline:**
1. Locally with a populated brain + Ollama: `brain eval --record-baseline ci`
2. Inspect the diff: `git diff tests/eval/baselines/ci.json`
3. Commit the new baseline JSON alongside the change that justifies the new numbers.

The `--fail-below` flag exits with code `3` (distinct from `1` = generic error and `2` = Typer BadParameter) when any mean metric — nDCG@5, MRR, or Recall@20 — regresses by more than `1e-4` (one unit at the baseline's 4-decimal serialization precision). Uniform threshold across all three metrics — no per-metric overrides (kept simple; the one downstream consumer is the CI workflow). `--fail-below` requires `--diff`; passing it alone exits `2` (Typer BadParameter).

`tests/eval/baselines/.gitignore` ignores `*.json` by default; explicitly-named committed baselines (currently `ci.json`) are allowlisted with `!ci.json`. Add new committed baselines the same way — do not blanket-allow.

**Wave A.1 source:** audit `docs/audits/2026-05-14-q1-codex-cumulative-review.md`, plan `docs/plans/2026-05-14-plan-audit-gap-remediation.md`. The plan tracks the remaining waves (A.2 person-variant key expansion, A.3 EXEC tracker reconciliation, A.4 first committed `ci.json` baseline).

### Migration Safety
- Migrations are raw SQL files in `migrations/`, applied in name order by `brain init`.
- **Never reference Python code in migrations** — they are pure SQL, frozen in time.
- **Every migration must be idempotent or applied to a fresh schema.** During development, `docker compose down && rm -rf data/postgres && docker compose up -d && brain init` resets cleanly. (`docker compose down -v` alone won't wipe the data — Postgres is mounted from a host bind-mount at `./data/postgres`, not a Docker-managed volume.)
- **Schema changes** = new numbered migration file. Never edit `001_init.sql` once shipped.

### Security Standards
- UUID primary keys everywhere.
- Parameterized SQL queries only.
- Never hardcode secrets — `VOYAGE_API_KEY` and `DATABASE_URL` come from `.env` (gitignored). Never commit `.env`.
- Never log full document content at INFO level — it can contain sensitive transcripts/emails. Log titles + IDs only.

## Workflow

### Plan → Approve → Implement → Verify
1. **Plan** — List affected files, modules, tests, risks.
2. **Approve** — Present plan to user. Do NOT write code until approved.
3. **Implement** — File by file. Use teammates for large tasks (per Team Mode Override above).
4. **Verify** — Run `ruff check && mypy src/ && pytest`. Then exercise the CLI end-to-end (`brain doctor`, `brain status`, a sample `brain search`).

### Teammate Usage
Use teammates for: parallel test creation, codebase exploration, tasks consuming >30% of context window, implementation when plan is approved. See "Team Mode Override" — always Team mode, never standalone subagents.

## Project Overview

Local personal knowledge base ("second brain") with hybrid search, designed to be queried by Claude from any conversation. Stores career documents, interview prep, Krisp call transcripts, Slack threads, and selected Gmail in Postgres + pgvector. Searches use Reciprocal Rank Fusion of FTS rank + vector cosine similarity.

**Embeddings:** Pluggable via the `BRAIN_EMBEDDER` env var. Default `arctic` =
Snowflake Arctic Embed v2 (1024-dim, local Ollama, free, Apache 2.0).
Alternatives: `voyage` (Voyage AI `voyage-3.5` SaaS, paid, 1024-dim) and
`qwen3` (Qwen3-Embedding-8B, local Ollama, free, China-origin, 4096-dim — no
HNSW index because pgvector caps at 2000 dims for `vector`).

Full design in `docs/specs/2026-04-24-second-brain-design.md`. Implementation plan in `docs/plans/2026-04-24-second-brain.md`. Pluggable-embedder retrofit plan in `docs/plans/2026-04-28-local-embeddings-qwen3-8b.md`.

## Tech Stack

- **Language:** Python 3.11+
- **CLI framework:** Typer
- **Database:** PostgreSQL 16 + pgvector (Docker, port 5433)
- **Embeddings:** Pluggable backends behind a single `Embedder` Protocol.
  Default `arctic` (Snowflake Arctic Embed v2 over local Ollama, 1024-dim).
  Alternates: `voyage` (Voyage AI SaaS, 1024-dim) and `qwen3` (Qwen3-Embedding-8B
  over local Ollama, 4096-dim). Selected at setup time via `BRAIN_EMBEDDER`.
- **PDF/DOCX:** pypdf, pdfplumber, python-docx
- **Markdown:** markdown-it-py
- **Tokenization:** tiktoken (offline `cl100k_base`, used by every backend for chunker budgeting)
- **Output:** Rich (colored tables, JSON)
- **Graph retrieval (experimental):** Apache AGE (openCypher graph in-Postgres) +
  networkx (Louvain community detection). Entity-centric GraphRAG alongside the
  vector/FTS search. Needs the custom AGE Postgres image; default-OFF via
  `BRAIN_GRAPH_ENABLED`.
- **Tests:** pytest, real Postgres test DB, fake embedder fixture
- **Lint/Type:** ruff, mypy

## Build & Run Commands

```bash
# Setup (default arctic backend — local Ollama, free, no API key)
brew install ollama                      # macOS; on Linux follow ollama.com/install
brew services start ollama
ollama pull snowflake-arctic-embed2

cp .env.example .env                     # BRAIN_EMBEDDER=arctic by default
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d                     # Postgres + pgvector on port 5433
brain init                               # applies migrations + aligns chunks.embedding dim
brain reembed                            # backfill any NULL embeddings + finalize column
brain doctor                             # health check (env, Postgres, embedder, gws CLI)

# Daily use
brain ingest <file>                      # ingest a single file
brain ingest-dir <dir>                   # recursive ingest
brain search "..."                       # hybrid search
brain show <id-prefix>                   # full document
brain reembed                            # backfill missing embeddings; idempotent

# Switching backends (destructive — requires data wipe + re-ingest)
docker compose down && rm -rf data/postgres
# edit .env (BRAIN_EMBEDDER=qwen3 / voyage / arctic)
docker compose up -d && brain init && brain ingest-dir <…> && brain reembed

# GraphRAG (experimental — entity graph alongside vector/FTS search)
# Requires the custom Apache AGE Postgres image
#   (second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2), NOT the stock pgvector prod
#   image. `pip install -e ".[dev]"` pulls the networkx dep. Set
#   BRAIN_GRAPH_ENABLED=true in .env to enable the ingest-time graph sync.
brain init                               # also bootstraps AGE + applies graph migrations when the image ships AGE
brain graphrag build --backfill          # backfill the people graph from existing docs
brain graphrag communities build         # detect + summarize communities (needed for --mode global)
brain graphrag search "..."              # graph retrieval (modes: auto|local|themes|global|fuse)
brain graphrag themes --person "Jane Doe"  # "themes in my conversations with X"
brain graphrag search "..." --mode fuse  # RRF of the graph leg + vector/FTS hybrid leg
brain graphrag communities list          # admin view of materialized communities
brain doctor                             # also reports AGE + graph health (soft check)
# Usage skill (Claude Code): skills/brain-graph/SKILL.md (brain-graph) — graph
#   retrieval for themes / patterns / connections across interactions (themes
#   with a person, what connects A and B, recurring themes), alongside plain
#   hybrid search.

# Testing
pytest                                   # full suite
pytest --cov=brain --cov-report=term     # with coverage
pytest tests/test_chunker.py -v          # single file

# Linting
ruff check                               # lint
ruff check --fix                         # auto-fix
mypy src/                                # type check
```

## Architecture

### Layout
```
src/brain/
  cli.py            — Typer app, all commands (init / doctor / status /
                      ingest* / search / show / list / tag / edit / rm / reembed)
  config.py         — env loading; selects BRAIN_EMBEDDER ∈ {arctic, voyage, qwen3}
  db.py             — psycopg connection + migration runner;
                      ensure_embedding_column() reconciles chunks.embedding
                      dim against the active embedder
  embeddings.py     — `_OllamaEmbedderBase` shared transport; concrete
                      `ArcticEmbedder`, `Qwen3Embedder`, `VoyageEmbedder`;
                      `make_embedder(cfg)` factory dispatched on cfg.embedder
  errors.py         — BrainError base + id-prefix exceptions, OllamaEmbedError
  queries.py        — read helpers (resolve_document_prefix, fetch_document,
                      list_documents, summary_counts) + reembed helpers
                      (iter_chunks_missing_embedding, finalize_embedding_index,
                      embedding_column_state)
  search.py         — hybrid FTS + vector via RRF
  format.py         — human + JSON output
  edit_session.py   — JSON-header + body editor flow used by `brain edit`
  mcp_server.py     — stdio MCP server (brain-mcp entry point)
  ingest/
    __init__.py     — Embedder Protocol (declares `dim`); dispatcher +
                      ingest_document() / update_document() pipelines
    chunker.py      — paragraph-aware chunking (uses embedder.count_tokens)
    text.py / markdown.py / pdf.py / docx.py — file extractors
    gmail.py        — shells out to gws CLI
    stdin.py        — generic stdin ingester (Krisp, Slack)

migrations/         — numbered SQL files
  001_init.sql                    — base schema (chunks.embedding starts as vector(1024))
  002_qwen3_embedding.sql         — drops + re-adds chunks.embedding as vector(4096), nullable
                                    (ensure_embedding_column then resizes for the active backend)

scripts/embedding_smoke.py        — retrieval sanity check for a corpus +
                                    seeded queries (used during backend swaps)

tests/              — real-DB fixture, fake embedder, one test file per module
docs/specs/         — design specs
docs/plans/         — implementation plans
```

### Key Patterns
- **Idempotent ingest:** `documents.content_hash` is UNIQUE on `documents`. Re-ingesting the same file is a no-op unless `--force`.
- **Source dedup:** `sources(kind, external_id)` is UNIQUE. Re-ingesting the same Krisp/Gmail message updates the existing row instead of duplicating.
- **Hybrid search:** Reciprocal Rank Fusion (k=60) combines FTS rank and vector cosine similarity. Ranks chunks; groups by document; returns top N docs with best matching chunk as snippet.
- **Claude-orchestrated ingestion:** Krisp and Slack don't have CLIs. Claude calls the MCP, then pipes content into `brain ingest-stdin --source krisp/slack ...`.
- **Pluggable embedders behind one Protocol.** All three backends conform to `brain.ingest.Embedder` (`dim: int`, `embed(texts, *, input_type)`, `count_tokens`). The factory `make_embedder(cfg)` is the only place that knows about concrete backends; ingest, search, and reembed depend only on the Protocol.
- **Dim-aware finalize.** `queries.finalize_embedding_index` applies `NOT NULL` on `chunks.embedding` for every backend, then conditionally creates an HNSW cosine index when `embedder.dim ≤ 2000` (arctic, voyage). For Qwen3 (4096) the index is skipped — pgvector 0.8.x caps HNSW/IVFFlat at 2000 for `vector`; sequential cosine scan is acceptable at personal-corpus scale.
- **Backend-aware `brain init`.** `init` runs migrations, then `db.ensure_embedding_column` reconciles `chunks.embedding`'s declared dim against `embedder.dim` — drop + re-add on a fresh DB, error with a destructive-reset hint if chunks already exist at a different dim.
- **Switching backends is destructive.** Embeddings cannot be re-projected across models; switching requires `docker compose down && rm -rf data/postgres && brain init && brain ingest-dir … && brain reembed`. Postgres data is a host bind-mount (`./data/postgres`), not a Docker volume — `docker compose down -v` alone is *not* sufficient.

## Lessons Learned

These are universal mistakes — never repeat them.

1. **Never guess fields/paths** — Read the actual source file before referencing any field, function, or import path.
2. **Never declare "done" without running it** — Run the CLI command end-to-end before claiming a task complete. Tests passing ≠ feature working.
3. **Never copy-paste code** — Extract shared patterns into reusable functions. If you write something twice, refactor on the third write.
4. **Test mocks must match signatures** — After changing a function's signature, grep ALL call sites and update them. Run `mypy src/` to catch drift.
5. **Four Phase Tests** — Every test follows setup → exercise → verify → teardown. Clear separation between phases. No mystery guests — make test data explicit, don't rely on invisible factory defaults.
6. **No test failures are pre-existing** — Every failure is a regression until proven otherwise. Investigate immediately, never dismiss as "pre-existing" or "flaky" without evidence.
7. **The Scout Law — leave the code better than you found it** — Before completing any task, ALL tests in the suite must pass — not just the ones related to your changes. If you encounter broken tests from other areas, fix them.

## Default Design Skill: open-design

For any design work — UI/UX, components, slides/decks, branding, motion, design reviews, image/video generation, design systems — prefer skills from the **open-design** collection (installed in `~/.claude/skills/`).

Examples: `frontend-design`, `design-review`, `design-brief`, `creative-director`, `ui-ux-pro-max`, `web-design-guidelines`, `color-expert`, `brand-guidelines`, `theme-factory`, `pptx-generator`, `slides`, `apple-hig`, plus 70+ design-system templates.

The Anthropic `frontend-design` plugin has been disabled in favor of this collection. The local open-design app also runs at http://localhost:7456 (Docker).
