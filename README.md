# Second Brain

Local personal knowledge base with hybrid search, designed to be queried by Claude from any conversation.

Stores career documents, interview prep, Krisp call transcripts, Slack threads, and Gmail messages in Postgres + pgvector. Searches use Reciprocal Rank Fusion of FTS rank and vector cosine similarity (`voyage-4`).

## Setup

```bash
git clone <repo> ~/workspace/second-brain
cd ~/workspace/second-brain
cp .env.example .env
# Edit .env: paste your VOYAGE_API_KEY (free tier covers personal use)

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

docker compose up -d
brain init
brain doctor   # should print all OK
```

### Make `brain` available globally (no venv activation needed)

`pip install -e ".[dev]"` produces `.venv/bin/brain` with an absolute-path shebang
pointing at the venv's Python, so the launcher is self-contained — running it
imports from the venv's `site-packages` without `source .venv/bin/activate`.

To call `brain` from anywhere, symlink it onto a directory already on your `$PATH`:

```bash
ln -s ~/workspace/second-brain/.venv/bin/brain /opt/homebrew/bin/brain
```

Verify with `which brain` (should resolve to the symlink) and `brain doctor`.

## Usage

```bash
# Ingest your career corpus
brain ingest-dir ~/Documents/career

# Tag a document
brain tag <id-prefix> +interview +company-id

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
- When to invoke `brain search` and `brain show` (career topics, interviews, past meetings, COMPANY_REDACTED, deals)
- How to orchestrate Krisp/Slack ingestion via MCP → `brain ingest-stdin`
- That `--json` output is available for programmatic parsing

## Tests

```bash
pytest                      # full suite (uses second_brain_test DB)
pytest --cov=brain          # with coverage
```

## License

Personal — not published.
