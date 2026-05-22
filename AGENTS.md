# AGENTS.md

Meta-instructions for the Codex coding agent, peer to `CLAUDE.md`. Both
files live at the repo root because they are agent-runtime configuration,
not project documentation — `CLAUDE.md` configures Anthropic's Claude
Code; `AGENTS.md` configures OpenAI's Codex. Do NOT move either file
under `docs/` (rule 8 below covers `.md` artifacts produced *as part of
work*; these two files configure the workers themselves).

## Hard Rules

1. Write automated tests for code changes. Prefer focused unit tests plus real Postgres integration tests when database behavior is involved.
2. Run verification before claiming work complete AND before any commit: `ruff check`, `mypy src/`, and `pytest` unless the user explicitly narrows the scope or the environment blocks a command. All three must pass; do not commit on a red gate.
3. Never commit or push without explicit user permission.
4. For multi-file or high-risk implementation work, produce a written plan first and get approval before editing.
5. Before referencing existing modules, functions, schemas, fields, or command behavior, read the actual source first.
6. Bug fixes require regression tests that reproduce the original failure.
7. Do not revert unrelated user changes. Work with the current dirty tree.
8. Plans, specs, and new Markdown docs created during work belong under `docs/`: specs in `docs/specs/YYYY-MM-DD-<topic>-design.md`, plans in `docs/plans/YYYY-MM-DD-<topic>.md`.
9. No real personal data in checked-in code, tests, fixtures, docs, comments, commit messages, or PR descriptions. The repo has been explicitly PII-scrubbed — use synthetic / redacted values for names, email addresses, phone numbers, postal addresses, meeting attendees, calendar specifics, company names, customer references, and internal codenames. If a test seems to need real data, the test is wrong: parameterize the fixture or refactor the production code so it's testable with synthetic values.

## Codex Agenting

Codex does not provide Claude's `TeamCreate` tool in this session. The closest equivalent is Codex sub-agents:

- Use `explorer` agents for bounded codebase questions.
- Use `worker` agents for scoped implementation work with clear file ownership.
- Use parallel agents only when the user explicitly asks for agentic/parallel/delegated work or when future session policy allows it.
- Do not describe Microsoft Teams integration as team-agent orchestration; it is unrelated.

The `superpowers` plugin is enabled for Codex when available. Prefer its workflow skills for planning, TDD, debugging, code review, verification, and subagent-driven development. If the plugin is unavailable in a session, follow the equivalent manual workflow in this file.

## Workflow

1. Plan: identify affected files, tests, risks, and verification commands.
2. Approve: wait for user approval before multi-file edits.
3. Implement: keep changes scoped and follow existing project patterns.
4. Verify: run `ruff check`, `mypy src/`, and `pytest`; for CLI-facing changes, also run a relevant `brain ...` command.
5. Review: inspect the diff for regressions, dead code, missing tests, and doc drift before final response.

## Project Overview

Second Brain is a local personal knowledge base with hybrid search, designed to be queried from coding-agent sessions. It stores selected documents, transcripts, Slack/Gmail-derived text, and notes in Postgres + pgvector, then searches with Reciprocal Rank Fusion over full-text rank and vector cosine similarity.

## Tech Stack

- Python 3.11+
- Typer CLI
- PostgreSQL 16 + pgvector in Docker on port `5433`
- Pluggable embedders via `BRAIN_EMBEDDER`: `arctic` default, `voyage`, or `qwen3`
- Extraction: `pypdf`, `pdfplumber`, `python-docx`, `markdown-it-py`
- Graph retrieval (experimental): Apache AGE (openCypher graph in-Postgres) + `networkx` (Louvain community detection) for entity-centric GraphRAG alongside the vector/FTS search; default-OFF via `BRAIN_GRAPH_ENABLED`; needs the custom AGE Postgres image
- Tests: `pytest` with real Postgres fixtures and fake embedders
- Lint/type: `ruff`, `mypy`

## Common Commands

```bash
# Setup
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d
brain init
brain reembed
brain doctor

# Daily use
brain ingest <file>
brain ingest-dir <dir>
brain search "query"
brain show <id-prefix>
brain status

# GraphRAG (experimental — entity graph alongside vector/FTS search).
# Requires the custom Apache AGE image (second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2),
# NOT the stock pgvector prod image. `pip install -e ".[dev]"` pulls networkx.
# Set BRAIN_GRAPH_ENABLED=true in .env to enable the ingest-time graph sync.
brain init                               # also bootstraps AGE + graph migrations on an AGE image
brain graphrag build --backfill          # backfill the people graph from existing docs
brain graphrag communities build         # detect + summarize communities (needed for --mode global)
brain graphrag search "query"            # graph retrieval (modes: auto|local|themes|global|fuse)
brain graphrag themes --person "Jane Doe"  # "themes in my conversations with X"
brain graphrag communities list          # admin view of materialized communities
brain doctor                             # also reports AGE + graph health (soft check)
# Usage skill (Claude Code): skills/brain-graph/SKILL.md (brain-graph) — graph
#   retrieval for themes / patterns / connections across interactions (themes
#   with a person, what connects A and B, recurring themes), alongside plain
#   hybrid search.

# Verification
ruff check
mypy src/
pytest
pytest --cov=brain --cov-report=term
```

## Architecture

```text
src/brain/
  cli.py            Typer app and command orchestration
  config.py         Environment loading and embedder selection
  db.py             Postgres connection, migrations, embedding-column alignment
  embeddings.py     Arctic, Qwen3, and Voyage embedder implementations
  errors.py         Project exceptions
  queries.py        Read helpers and reembed helpers
  search.py         Hybrid FTS + vector search via RRF
  format.py         Human and JSON output
  edit_session.py   Editor flow for `brain edit`
  mcp_server.py     stdio MCP server
  ingest/           Extractors, chunking, and ingest/update pipeline
  vault/            Vault sync, links, graph, rendering, and derived links
  wiki/             Quartz build watcher and atomic build swap

migrations/         Numbered SQL migrations
tests/              Unit and integration tests
docs/specs/         Design specs
docs/plans/         Implementation plans
```

## Coding Standards

- Use `pathlib.Path` for filesystem paths.
- Use dataclasses for value objects.
- Type every function signature.
- Keep module APIs narrow and focused.
- Use parameterized SQL only; never concatenate user input into SQL.
- External HTTP and DB clients must have explicit timeouts.
- Avoid bare `except:`; catch specific exceptions.
- Add succinct comments only where code is not self-explanatory.
- Keep migrations as raw SQL, pure and frozen in time. Never reference Python in a migration.
- Every migration must be idempotent or apply to a fresh schema. Local resets go through `docker compose down && rm -rf data/postgres && docker compose up -d && brain init` because Postgres is a host bind mount, not a Docker volume.
- Schema changes require a new numbered migration. Do not edit shipped migrations (e.g., `001_init.sql`) once they have landed on `master`.

## Testing Standards

- Keep tests explicit: setup, exercise, verify, teardown.
- Prefer production seams and dependency injection over patch-heavy tests.
- `pytest.monkeypatch`, `unittest.mock.patch`, and `mocker.patch` are acceptable test doubles — they have automatic cleanup and do not count as monkey-patching.
- Do not reopen production modules/classes in tests to inject constants, methods, or attributes, and do not assign attributes onto imported modules without restoration. If a test needs that kind of monkey-patching to pass, the production code has a bug — fix the production code, not the test.
- Treat unexplained failures as regressions until investigated.

## Eval Gate (CI)

The eval-marker harness (`tests/test_eval_harness_live.py`) is excluded from the default `pytest` invocation via `pyproject.toml` → `addopts = "... -m 'not eval'"`. It requires a live Postgres + Ollama and assumes the live brain corpus, so it would slow every local run and fail without that environment. Local devs run `pytest -m eval` manually when needed.

CI enforces it separately. `.github/workflows/eval.yml` runs on every PR and every push to `master`:

1. Spins up Postgres 16 + pgvector as a service container on port `5433`.
2. Installs the package with `pip install -e ".[dev]"`.
3. Runs `pytest -m eval --no-cov -v` — gate fails on any harness regression.
4. Conditionally runs `brain eval --baseline ci --diff --fail-below` when `tests/eval/baselines/ci.json` exists (added by Wave A.4).

Decision recorded (Wave A.1): the eval marker stays OFF in the default `pytest` invocation. Gate lives in CI only.

Updating a committed baseline:

1. Locally with a populated brain + Ollama: `brain eval --record-baseline ci`.
2. Inspect the diff: `git diff tests/eval/baselines/ci.json`.
3. Commit the new baseline JSON alongside the change that justifies the new numbers.

`brain eval --fail-below` exits with code `3` (distinct from `1` = generic error and `2` = Typer `BadParameter`) when any mean metric — nDCG@5, MRR, or Recall@20 — regresses by more than `1e-4` versus the baseline. The threshold matches the 4-decimal serialization precision used by `brain.eval.baseline._round_floats`. `--fail-below` requires `--diff`; passing it alone exits `2`.

`tests/eval/baselines/.gitignore` ignores `*.json` by default and allowlists named baselines with `!ci.json`. Add new committed baselines the same way — never blanket-allow.

Wave A.1 source: audit `docs/audits/2026-05-14-q1-codex-cumulative-review.md`, plan `docs/plans/2026-05-14-plan-audit-gap-remediation.md`. Remaining waves: A.2 (person-variant key expansion), A.3 (EXEC tracker reconciliation), A.4 (first committed `ci.json` baseline).

## Memory And Docs

Codex memory is separate from Claude memory. Codex agents must use the Codex-owned project memory under:

```text
/Users/mshtawythug/.codex/memories/second-brain/
```

Claude memory under `/Users/mshtawythug/.claude/projects/-Users-mshtawythug-workspace-second-brain/memory/` is Claude-owned. Do not read or update Claude memory as the target for a Codex memory update unless the user explicitly asks to inspect Claude memory.

When a task changes CLI commands, database schema, Python value types, extractors, search behavior, vault/wiki behavior, or operating rules, update the corresponding Codex memory file and `/Users/mshtawythug/.codex/memories/second-brain/MEMORY.md` index. When asked to "update memory", audit the Codex memory files against the current codebase and update drifted content plus the Codex memory index.

Topic → file map (Codex memory):

| Change touches… | Update file |
|---|---|
| `brain` subcommands, flags, exit codes | `cli.md` |
| SQL migrations, table/column shape, indexes | `schema.md` |
| Public dataclasses, Protocols, exception classes, ingest/update kwargs | `ingest-and-types.md` |
| File extractors, ingest pipeline, source hooks, dedup rules | `ingest-and-types.md` |
| Search ranking, RRF weights, filter surface, `SearchExplanation`, eval harness | `search.md` |
| Auto-summary, auto-tag, action items, `brain rate` / `brain todo` | `enrichment-and-feedback.md` |
| Vault mirror writeback, wiki build, Quartz overlays, fences, People Hub | `vault-and-wiki.md` |
| `quartz_overrides/` build/reload pipeline + edit-to-UI latency work | `quartz-latency.md` |
| `_people.yml`, People Hub emit thresholds, `brain people` | `people-hub.md` |
| Hard rules, workflow contracts, banned actions | `operational-rules.md` |
| Shipped-wave summaries, project-level decisions, plan/audit references | `project.md` |

## Operational Notes

- Postgres data is a host bind mount at `./data/postgres`; `docker compose down -v` alone does not wipe it.
- Switching embedders is destructive because embeddings cannot be re-projected across models.
- `brain init` applies migrations and reconciles `chunks.embedding` dimensions with the active embedder.
- For `qwen3`, pgvector HNSW is skipped because 4096 dimensions exceed pgvector's vector index cap.
- GraphRAG (experimental) needs the custom Apache AGE image `second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2` (built from `src/brain/templates/docker/age/Dockerfile`); the stock pgvector prod image does not ship AGE. The concept aspect (LLM extraction) is default-OFF behind `BRAIN_GRAPH_CONCEPTS`; the people aspect needs no model. The graph is a recomputable mirror — rebuild it with `brain graphrag build --force`. Switching an existing brain to AGE is a separate, deliberate cutover — back up the database first; until then GraphRAG runs against the AGE-backed test instance only (`docker-compose.age-test.yml`, port 5434) while prod stays on stock pgvector.
- GraphRAG retrieval/admin surfaces have full CLI↔MCP parity: `brain graphrag {search,themes,entity,build,refresh,communities {build,refresh,list}}` and the matching `brain_graphrag_*` MCP tools. Never accept or emit raw Cypher — every command takes structured params and the backend injects the tenant + traversal caps.

## Lessons

1. Read source before naming fields or imports.
2. Run the relevant command before saying it works.
3. Grep call sites after signature changes.
4. Prefer shared helpers after repeated patterns emerge.
5. Leave unrelated changes alone.
