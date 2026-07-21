# Contributing to Second Brain

Thanks for your interest in improving Second Brain. This guide covers local
setup, the quality gate every change must clear, and the project conventions
(testing discipline, commit format, and the no-PII / no-monkey-patching rules).

Second Brain is a local-first personal knowledge base CLI: hybrid full-text +
`pgvector` search, a GraphRAG entity graph, and LLM enrichment over your own
notes, transcripts, Slack, and Gmail — all running locally on Postgres +
Ollama, and queryable by any MCP client through the bundled `brain-mcp` server.

## Prerequisites

- **Python 3.11+** (3.11 or 3.12).
- **Docker** (for the Postgres + `pgvector` test database).
- **Ollama** (optional for most work — only needed for tests/commands that
  actually embed or enrich; the suite uses a fake embedder fixture, so you can
  run the full test suite without Ollama).

## Local development setup

```bash
# 1. Clone
git clone https://github.com/mshtawythug/second-brain.git
cd second-brain

# 2. Create and activate a virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install the package with dev extras (pytest, ruff, mypy, ...)
pip install -e ".[dev]"
```

### Test database

The test suite runs against a **real Postgres** — a dedicated AGE-enabled
instance, separate from any production database, on **port 5434**
(database `second_brain_test`, user/password `brain`/`brain`):

```bash
docker compose -f docker-compose.age-test.yml up -d
```

This pulls the prebuilt multi-arch Apache AGE image from GHCR
(`ghcr.io/mshtawythug/second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2`). If that image
is unavailable, add `--build` to compile AGE locally from the pinned Dockerfile
(`src/brain/templates/docker/age/`) — slower, but produces the identical
pinned image:

```bash
docker compose -f docker-compose.age-test.yml up -d --build
```

The suite connects via `TEST_DATABASE_URL`, which defaults to
`postgresql://brain:brain@localhost:5434/second_brain_test`. Data lives in a
**named** Docker volume (`second-brain-age-test-postgres`) — `docker compose
-f docker-compose.age-test.yml down` stops the container and keeps the data;
`down -v` wipes the test data (safe — it never touches any production corpus).

### Production stack (optional, for exercising the CLI end-to-end)

The production Postgres is a **separate** container on **port 55432**
(stock `pgvector`, host bind-mount at `./data/postgres`):

```bash
docker compose up -d      # Postgres + pgvector on 55432
brain init                # apply migrations + reconcile the embedding column
brain reembed             # backfill embeddings (needs an embedder — e.g. Ollama)
brain doctor              # health check
```

Note: the production database is a host bind-mount, so `docker compose down -v`
alone does **not** wipe it — a full local reset is `docker compose down && rm
-rf data/postgres && docker compose up -d && brain init`.

## Quality gate

Every change must pass all three, in this order, before you open a PR:

```bash
ruff check        # lint (auto-fix with: ruff check --fix)
mypy src/         # static types — zero errors
pytest            # full suite against the port-5434 test DB
```

`pytest` runs with coverage enforcement built in (`--cov-fail-under=85`).

### Coverage floors

| Area | Minimum |
|------|---------|
| Overall | **85%** (enforced by `--cov-fail-under=85`) |
| Pure logic (`chunker`, `search`, `format`) | **95%** |
| Ingest pipeline | **90%** |
| CLI commands | **85%** |

## Testing discipline

- **Test-driven, red-first.** Write the test before the implementation, watch
  it fail (RED), write the minimum code to pass (GREEN), then refactor. New
  behavior lands with tests, not after.
- **Every bug fix ships a regression test.** A fix is not complete without a
  test that reproduces the original bug and verifies the fix. No exceptions.
- **Real-DB integration tests.** Prefer exercising real Postgres over mocking it
  — real-DB tests catch schema and migration drift that mocks hide. A fake
  embedder fixture stands in for the embedding backend so tests stay fast and
  offline.
- **AAA structure.** Arrange → Act → Assert, with explicit test data (no
  invisible factory defaults).

### No PII — synthetic fixtures only

This repository has been explicitly scrubbed of personal data. **Never**
introduce real names, email addresses, phone numbers, postal addresses, meeting
attendees, company names, customer references, or internal project codenames —
in tests, fixtures, docs, comments, commit messages, or PR descriptions. Use
synthetic / redacted values only. If a test seems to need a real value to be
meaningful, the test is wrong — parameterize the fixture or refactor the code so
it is testable with synthetic data. Copy the existing synthetic patterns when
extending tests.

### No monkey-patching of production modules

- **Banned:** reopening production modules/classes to inject constants, methods,
  or attributes; direct attribute assignment on imported modules without
  restoration. If a test needs this to pass, the production code has a bug — fix
  the production code, not the test.
- **Allowed:** `monkeypatch` (pytest), `unittest.mock.patch`, and `mocker.patch`
  (pytest-mock). These are standard test doubles with automatic cleanup, not
  monkey-patching.

## Commit and PR conventions

Commits follow **Conventional Commits**:

```
<type>: <description>

<optional body>
```

`<type>` is one of: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`,
`ci`. Keep the subject imperative and under ~72 characters. The same no-PII rule
applies to commit messages and PR descriptions.

When you open a pull request, the PR template checklist walks through what a
reviewer expects: tests added/updated (red-first for fixes), the `ruff check &&
mypy src/ && pytest` gate green, no PII, and docs updated (README / `docs/` /
`CHANGELOG.md`) whenever the change is user-facing.

## Docs assets

The docs embed **seven** generated GIFs, each rebuilt by its own script under
`bin/`. Six are [VHS](https://github.com/charmbracelet/vhs) terminal recordings
driven by a tape in `docs/assets/*.tape`; the seventh (the wiki clip) is a
Playwright **browser** capture of the rendered Quartz site, not a VHS tape. The
GIFs live across `README.md`, `docs/graphrag.md`, `docs/cli-reference.md`, and
`docs/vault-and-wiki.md`. Every regenerator needs Docker plus either
`brew install vhs` (the six VHS ones) or Node (the wiki one); a few need more, as
noted below (Ollama, the Apache AGE image, ffmpeg).

- **Hero / demo** — `bin/brain-demo-gif` → `docs/assets/demo.gif` (README, "See
  it in 60 seconds"), from `docs/assets/demo.tape`. Records the `brain demo`
  sandbox flow; the script provisions and tears down the isolated `brain demo`
  sandbox.
- **Daily workflow** — `bin/brain-usage-gif` → `docs/assets/usage.gif` (README
  hero), from `docs/assets/usage.tape`. Records the regular `brain` CLI (ingest →
  search → show → status) against a **throwaway, fully-isolated** Postgres it
  spins up just for the recording (compose project `brain-usage-gif`, port 55440,
  named volume) and destroys afterward — never the prod, demo, or test databases,
  and every seeded doc is synthetic. Uses the local Ollama `arctic` embedder when
  available, else FTS-only (`BRAIN_EMBEDDER=none`).
- **Claude integrations** — `bin/brain-mcp-gif` → `docs/assets/mcp.gif` (README,
  "Claude integrations"), from `docs/assets/mcp.tape`. Drives the `claude` CLI
  answering a question over the bundled `brain-mcp` MCP server.
- **Entity-graph retrieval** — `bin/brain-graphrag-gif` →
  `docs/assets/graphrag.gif` (`docs/graphrag.md`), from
  `docs/assets/graphrag.tape`. Records `brain graphrag` against a throwaway,
  isolated Apache AGE Postgres; additionally needs the AGE image and Ollama (for
  the graph build).
- **Ask your corpus** — `bin/brain-ask-gif` → `docs/assets/ask.gif`
  (`docs/cli-reference.md`), from `docs/assets/ask.tape`. Records the `brain ask`
  plan → retrieve → synthesize loop; additionally needs Ollama and ffmpeg (to
  trim).
- **Proactive commands** — `bin/brain-proactivity-gif` →
  `docs/assets/proactivity.gif` (`docs/cli-reference.md`), from
  `docs/assets/proactivity.tape`. Records `brain brief` + `brain resurface`;
  additionally needs Ollama.
- **Rendered wiki** — `bin/brain-wiki-gif` (with
  `bin/brain-wiki-gif-capture.cjs`) → `docs/assets/wiki.gif`
  (`docs/vault-and-wiki.md`). A Playwright **browser** capture of the built
  Quartz wiki (graph view, backlinks, People Hub) — **not** a VHS tape. Needs
  Node + Playwright (auto-installed) and Docker.

If a CLI or UI change makes any GIF stale, regenerate it with the matching
script.

## Codebase layout

```
src/brain/
  cli.py              — Typer app, every `brain ...` subcommand
  config.py           — env loading; selects BRAIN_EMBEDDER ∈ {arctic, voyage, qwen3, none}
  db.py               — psycopg connection + migration runner (schema_migrations tracked)
  embeddings.py       — concrete embedders behind a shared Protocol
  embedding_targets.py — allowlist + identifier-safety helpers for pgvector embedding columns
  errors.py           — BrainError hierarchy
  queries.py          — read-side SQL helpers shared by CLI + MCP
  search.py           — hybrid FTS + vector via RRF
  rank_fusion.py      — shared RRF helper (search + graph retrieval both use it)
  set_similarity.py   — Jaccard helper (community membership matching)
  tags.py             — canonical casefold-lowercase + hyphenated tag normaliser
  interactions.py     — append-only feedback log (search clicks, ratings, pins), document + graph targets
  enrichment.py       — Ollama-backed summariser + tag-proposal helpers
  chat.py             — shared public `chat_json()` Ollama call (brief / ask / audio)
  activity.py         — shared time-windowed activity reader (brief + weekly review)
  resurface.py        — `brain resurface` spaced-repetition scoring
  brief.py            — `brain brief` daily-digest assembly + next-step suggestions
  review/             — `brain review` package: scans (contradiction/staleness), weekly synthesis, queue queries, emit/render
  timeline.py         — `brain timeline` temporal bucketing over graph entities
  connect.py          — `brain connect` auto-link scoring core
  cli_connect.py      — `brain connect` CLI sub-app (list/refresh/accept/reject/stats)
  ask.py              — `brain ask` agentic plan/reflect/synthesize loop
  audio.py            — `brain audio` two-host script generation + TTS Protocol
  gaps.py             — `brain gaps` search-failure clustering + detector
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
  eval/               — retrieval + answer eval harness (metrics, baselines, corpus, runner, answer_eval for `brain eval --answer`)
src/brain/migrations/ — numbered SQL files (001..023) packaged inside the brain package + schema_migrations tracking
bin/                  — brain-up / brain-down / brain-rebuild / brain-status convenience scripts
quartz.config.ts      — sample Quartz v4 config (copy into <vault>/.quartz/)
skills/               — Claude Code skills (consult-brain, brain-graph, elicit-brain, brain-todo, ingest-brain)
tests/                — real-DB pattern, fake embedder fixture, one test module per source module (~290 modules)
```

## Tests

```bash
pytest                      # full suite (uses the second_brain_test DB on port 5434)
pytest --cov=brain          # with coverage
pytest tests/test_chunker.py -v   # a single module
```

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
