<!-- mcp-name: io.github.mshtawythug/second-brain -->

# Second Brain

Local, queryable knowledge base and note vault with hybrid search and an entity-graph layer — searchable by any AI coding agent or assistant from any conversation.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml/badge.svg)](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Stores career docs, interview prep, Krisp transcripts, Slack threads, Gmail, and authored Markdown notes in Postgres + pgvector. Any agent reaches all of it through the `brain` CLI or the bundled `brain-mcp` MCP server — no re-pasting context into every chat.

The everyday loop — capture a note, search it back, read the top hit, check the corpus:

![brain daily workflow](docs/assets/usage.gif)

## Token savings

Querying `brain` returns a ranked snippet instead of dumping whole threads and files into an agent's context, so a lookup costs a fraction of the tokens a direct MCP or file read would:

| Source | Direct read (MCP / `Read` tool) | Via `brain` (search + optional `show`) | Savings |
|---|---|---|---|
| **Gmail thread** | 15–50k (metadata + full threads with quoted replies / headers) | ~2–4k (snippets + one targeted `brain show`) | **~5–15×** |
| **Krisp transcript** | 25–75k (search + full transcripts across candidates) | ~6–16k (search + one relevant transcript) | **~4–10×** |
| **Long PDF / DOCX (30+ pages)** | 15–25k (read the whole file) | ~1–9k (one snippet + targeted `show`) | **~5–15×** |

Brain stores pre-extracted, quote-stripped bodies and hybrid-ranks *before* fetching, so only the passage that matched enters context. See the [full per-source breakdown](docs/configuration.md#token-economics).

## See it in 60 seconds

Install the CLI, then run the offline demo — a throwaway Postgres seeded with a synthetic *Larkspur* compliance corpus. No Ollama, no personal data, no model downloads:

```bash
pipx install secondbrain-py
# or, with uv:
uv tool install secondbrain-py
# or, pin the git tag (no PyPI needed):
pipx install git+https://github.com/mshtawythug/second-brain.git@v0.2.1

brain demo        # spins up a sandbox Postgres, seeds 22 docs, runs a hero query
```

![brain demo](docs/assets/demo.gif)

_(IDs and scores are per-run; the seeded corpus and its ranking are deterministic — the top hit is always the "Compliance Horror Stories" note.)_

Try these next:

```bash
brain demo query "SOC 2 evidence request"      # a targeted follow-up query
brain demo query "vendor risk" --source gmail  # narrow by source
brain show <id>                                # read the top hit in full (in your own brain)
brain demo teardown                            # remove the sandbox when done
```

`brain demo` needs only Docker; `brain demo teardown` destroys the sandbox.

Already ran the demo? You have `brain` installed — jump straight to `brain setup --profile …` below.

## Quick start

Full install — the `brain` CLI plus the runtime (Postgres, and optionally Ollama / graph / wiki):

```bash
curl -fsSL https://raw.githubusercontent.com/mshtawythug/second-brain/v0.2.1/install.sh | bash
```

The installer pipx-installs `brain` from the `v0.2.1` tag, then runs `brain setup` to provision `$BRAIN_HOME`, start the Postgres container, and (optionally) install the wiki + Claude Code skill. Choose how much to stand up with `--profile`:

| Profile | Search | Extra dependencies beyond the core |
|---|---|---|
| `minimal` | FTS-only (`BRAIN_EMBEDDER=none`) | None — core only (no Ollama, no models). |
| `standard` *(default)* | Hybrid (FTS + vector) | + Ollama + one ~1 GB embedding model (`snowflake-arctic-embed2`). |
| `full` | Hybrid + GraphRAG + wiki | + Apache AGE image, concept-extraction LLM, Quartz/Caddy wiki, and (opt-in) launchd daemons via `--daemons`. |

```bash
brain setup --profile minimal    # Docker-only, FTS search — fastest to stand up
brain setup --profile standard   # default: local hybrid search
brain setup --profile full --daemons
```

**Prerequisites (core):** Git, Python 3.11+, and a running Docker (Desktop or Engine) — the Postgres + pgvector database runs in a container on port 55432.

**Optional extras** (only if you want more than FTS-only search):

- **[Ollama](https://ollama.com/)** — local, free embeddings + LLM enrichment. To upgrade a `minimal` (FTS-only) brain to hybrid search: install Ollama, then `ollama pull snowflake-arctic-embed2`, set `BRAIN_EMBEDDER=arctic` in `.env`, and run `brain init && brain reembed`.
- **Node.js 18+ and [Caddy](https://caddyserver.com/)** — only for the rendered [wiki](docs/vault-and-wiki.md).
- **`gws` CLI** — only for Gmail ingest and Google-backed directory linking.

Prefer to drive every step yourself, or hacking on the code? See [Installing from source](docs/configuration.md#installing-from-source-manual).

## Core usage

```bash
brain ingest ~/Documents/resume.pdf --tag career          # ingest a file (TXT / MD / PDF / DOCX)
brain ingest-dir ~/Documents/career                       # recursive, idempotent by content hash
brain search "what did I tell my manager about the migration"   # hybrid FTS + vector search
brain show <id-prefix>                                    # full document body (6+ hex prefix)
brain list --source gmail --limit 20                      # browse by source
brain tag <id-prefix> +interview +career -old-tag         # add (+name) / remove (-name) tags
```

Add `--json` to `search` / `show` / `list` for machine-readable output, and `--fts-only` to `search` to skip the embedding call. The full command surface — Gmail ingest, enrichment, tacit-knowledge elicitation, GraphRAG, the proactivity/synthesis commands, and vault authoring — lives in the [CLI reference](docs/cli-reference.md).

## Claude integrations

`brain` and the bundled `brain-mcp` server are harness-agnostic — any agent that runs a shell command or speaks MCP can query the corpus.

Register the MCP server once, then just ask — the agent searches the brain and answers from it, no context pasted in:

![Claude answering a question from the brain over the brain-mcp MCP server](docs/assets/mcp.gif)

**Claude Code** — register the MCP server (or symlink the [skills](docs/configuration.md#claude-code-consult-brain-skill)):

```bash
claude mcp add brain -- brain-mcp
```

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json` (replace `/Users/you` with your home directory):

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/you/workspace/second-brain/.venv/bin/brain-mcp",
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:55432/second_brain",
        "BRAIN_EMBEDDER": "arctic"
      }
    }
  }
}
```

`uvx secondbrain-py` also launches the MCP server. Full walkthrough (symlink, smoke-test, troubleshooting): [docs/guides/claude-desktop-setup.md](docs/guides/claude-desktop-setup.md).

## How it works

**Hybrid search.** Every document is chunked, embedded, and indexed for Postgres full-text search. A query runs both legs — lexical `tsvector` ranking and vector cosine similarity — and fuses them with [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60), then applies a recency boost and metadata filters. Lexical alone misses paraphrases ("what did I say about X"); vector alone misses exact names (a coworker, a former employer); RRF gets both in one ranked list without tuning weights. Set `BRAIN_EMBEDDER=none` for an FTS-only brain with no embedding dependency at all.

**GraphRAG.** Alongside search, brain builds an entity graph of the people and concepts that co-occur across the corpus ([Apache AGE](https://age.apache.org/) inside the same Postgres) and retrieves over that structure — answering "what themes come up in my conversations with X" or "which clusters of people and topics dominate my notes" by traversing relationships instead of matching text. It runs alongside plain search, not instead of it. See [docs/graphrag.md](docs/graphrag.md).

## Documentation

- [docs/](docs/README.md) — full documentation index
- [CLI reference](docs/cli-reference.md) — every command beyond the core five
- [GraphRAG](docs/graphrag.md) — entity-graph retrieval (themes, patterns, connections)
- [Vault and Wiki](docs/vault-and-wiki.md) — the two-tier note vault + optional rendered wiki
- [Configuration](docs/configuration.md) — tech stack, tuning knobs, integrations, embedder backends, uninstall
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, codebase layout, running the tests
- [CHANGELOG.md](CHANGELOG.md) — release history
- [LICENSE](LICENSE) — MIT
