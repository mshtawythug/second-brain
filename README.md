# Second Brain

Local personal knowledge base with hybrid search, designed to be queried by Claude from any conversation.

Stores career documents, interview prep, Krisp call transcripts, Slack threads, and Gmail messages in Postgres + pgvector. Searches use Reciprocal Rank Fusion of FTS rank and vector cosine similarity (`voyage-4`).

## What this is

A `brain` CLI backed by a local Postgres database that I can query from any Claude Code session. I ingest my own career artifacts — resumes, interview prep, COMPANY_REDACTED docs, Krisp call transcripts, Slack threads, selected Gmail — and Claude searches them via `brain search` whenever a conversation touches my work history. Instead of copy-pasting context into every chat, Claude pulls the real source material on demand.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| CLI | Python 3.11 + [Typer](https://typer.tiangolo.com/) | Fast to write, good ergonomics, easy to test. |
| Storage | PostgreSQL 16 + [`pgvector`](https://github.com/pgvector/pgvector) | One database for both lexical (`tsvector`) and semantic (vector) search — no separate vector store to operate. Runs in Docker on port 5433. |
| Embeddings | Voyage AI [`voyage-4`](https://docs.voyageai.com/) (1024-dim) | Strong retrieval quality on long-form personal text; free tier covers personal use. |
| Search | Hybrid: Postgres FTS + vector cosine, fused via [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60) | Lexical alone misses paraphrases ("what did I say about X"); vector alone misses exact names ("person-x", "COMPANY_REDACTED"). RRF combines both ranks without tuning weights. |
| Extraction | `pypdf`, `pdfplumber`, `python-docx`, `markdown-it-py` | Covers the file types I actually have. |
| Chunking | Paragraph-aware, budgeted with `tiktoken` | Keeps semantic boundaries intact while staying under the embedder's token limit. |
| Output | [Rich](https://rich.readthedocs.io/) tables + `--json` mode | Human-readable in a terminal, machine-parsable when Claude shells out. |
| Tests | `pytest` against a real Postgres test DB, fake embedder fixture | Real-DB integration catches schema/migration drift that mocks would hide. |
| Lint / type | `ruff`, `mypy` | Cheap to run, catches real bugs. |

## Why I built this

- **Claude has no memory across conversations.** A queryable second brain gives it durable, personal context (past meetings, prior writings, decisions) without me re-pasting it every time.
- **Local-only by design.** My Krisp transcripts and Slack history don't leave my machine — no SaaS account, no vendor indexing my comms.
- **Hybrid search beats either half.** My queries split roughly 50/50 between paraphrase-heavy ("compliance horror stories") and exact-name ("person-x", "COMPANY_REDACTED"). RRF gives me both in one ranked list.
- **Works from any cwd.** A symlinked launcher means Claude Code in any project can call `brain search` — the knowledge base isn't tied to one repo.
- **Idempotent ingest.** `documents.content_hash` is `UNIQUE`, so re-running `brain ingest-dir` is a no-op. I can rerun without thinking about duplicates.

## Setup

### Prerequisites

- **Python 3.11+** — `python3.11 --version` should work. On macOS: `brew install python@3.11`.
- **Docker Desktop** (or Docker Engine) — running. The Postgres + pgvector database lives in a container on port 5433. Get it from [docker.com](https://www.docker.com/products/docker-desktop/).
- **A Voyage AI API key** — free tier is plenty for personal use. Sign up at [voyageai.com](https://www.voyageai.com/) and grab a key from the dashboard.
- **Claude Code** (optional but the whole point) — install from [claude.com/claude-code](https://claude.com/claude-code) so Claude can call `brain` for you.

### Install

```bash
# 1. Clone the repo and enter it
git clone <repo> ~/workspace/second-brain
cd ~/workspace/second-brain

# 2. Drop your Voyage API key into a local .env (gitignored)
cp .env.example .env
# then open .env and paste your key after VOYAGE_API_KEY=

# 3. Create an isolated Python environment so this project's deps
#    don't clash with anything else on your system
python3.11 -m venv .venv
source .venv/bin/activate

# 4. Install the brain CLI and its dependencies into that venv
pip install -e ".[dev]"

# 5. Start the Postgres + pgvector container in the background
docker compose up -d

# 6. Apply database migrations (creates tables, indexes, extensions)
brain init

# 7. Sanity check — should print "all OK"
brain doctor
```

If `brain doctor` complains, the usual suspects are: Docker isn't running, the
container hasn't finished starting yet (give it ~10 seconds and retry), or
`VOYAGE_API_KEY` is missing from `.env`.

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
brain tag <id-prefix> +interview +company-id

# Edit a document in place (title / metadata / body)
brain edit <id-prefix> --title "New title"
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}'   # shallow-merges
brain edit <id-prefix> --content-file ./fixed.md            # re-embeds
brain edit <id-prefix>                                      # opens $EDITOR

# Search
brain search "what did I tell person-x about COMPANY_REDACTED"
brain search "compliance horror stories" --limit 10
brain search "interview prep" --tag interview --since 30

# Drill into a result
brain show <id-prefix>

# Browse
brain list --source gmail --limit 20
brain list --tag company-id

# Gmail (requires at least one scope flag)
brain ingest-gmail --label interviews --since 30d
brain ingest-gmail --from person-a@example.com

# Krisp / Slack — Claude orchestrates this:
# (Claude calls Krisp MCP, then pipes to brain ingest-stdin)
echo "<transcript>" | brain ingest-stdin \
  --source krisp --external-id meeting-42 \
  --title "person-x sync — Apr 24" --content-type transcript \
  --metadata '{"participants":["person-x","Ali"]}'

# Admin
brain status   # counts and last-ingest time
brain doctor   # health check
```

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
