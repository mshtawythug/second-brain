# AGENTS.md

Codex instructions for this repository.

## Hard Rules

1. Write automated tests for code changes. Prefer focused unit tests plus real Postgres integration tests when database behavior is involved.
2. Run verification before claiming work complete: `ruff check`, `mypy src/`, and `pytest` unless the user explicitly narrows the scope or the environment blocks a command.
3. Never commit or push without explicit user permission.
4. For multi-file or high-risk implementation work, produce a written plan first and get approval before editing.
5. Before referencing existing modules, functions, schemas, fields, or command behavior, read the actual source first.
6. Bug fixes require regression tests that reproduce the original failure.
7. Do not revert unrelated user changes. Work with the current dirty tree.
8. Plans, specs, and new Markdown docs created during work belong under `docs/`: specs in `docs/specs/YYYY-MM-DD-<topic>-design.md`, plans in `docs/plans/YYYY-MM-DD-<topic>.md`.

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
- Keep migrations as raw SQL, pure and frozen in time.
- Schema changes require new numbered migrations; do not edit shipped migrations casually.

## Testing Standards

- Keep tests explicit: setup, exercise, verify, teardown.
- Prefer production seams and dependency injection over patch-heavy tests.
- `pytest.monkeypatch`, `unittest.mock.patch`, and `mocker.patch` are acceptable test doubles with cleanup.
- Do not mutate production modules/classes directly in tests without cleanup.
- Treat unexplained failures as regressions until investigated.

## Memory And Docs

Codex memory is separate from Claude memory. Codex agents must use the Codex-owned project memory under:

```text
/Users/mshtawythug/.codex/memories/second-brain/
```

Claude memory under `/Users/mshtawythug/.claude/projects/-Users-mshtawythug-workspace-second-brain/memory/` is Claude-owned. Do not read or update Claude memory as the target for a Codex memory update unless the user explicitly asks to inspect Claude memory.

When a task changes CLI commands, database schema, Python value types, extractors, search behavior, vault/wiki behavior, or operating rules, update the corresponding Codex memory file and `/Users/mshtawythug/.codex/memories/second-brain/MEMORY.md` index. When asked to "update memory", audit the Codex memory files against the current codebase and update drifted content plus the Codex memory index.

## Operational Notes

- Postgres data is a host bind mount at `./data/postgres`; `docker compose down -v` alone does not wipe it.
- Switching embedders is destructive because embeddings cannot be re-projected across models.
- `brain init` applies migrations and reconciles `chunks.embedding` dimensions with the active embedder.
- For `qwen3`, pgvector HNSW is skipped because 4096 dimensions exceed pgvector's vector index cap.

## Lessons

1. Read source before naming fields or imports.
2. Run the relevant command before saying it works.
3. Grep call sites after signature changes.
4. Prefer shared helpers after repeated patterns emerge.
5. Leave unrelated changes alone.
