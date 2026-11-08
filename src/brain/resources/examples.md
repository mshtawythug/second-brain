# brain examples

## search: Find what you already stored

```bash
# Hybrid search across everything
brain search "vendor renewal"

# Only Slack threads from the last 30 days
brain search "pricing decision" --source slack --since 30d

# Keywords only, when no embedder is running
brain search "invoice 1042" --fts-only

# More results, as JSON for a script
brain search "vendor renewal" --limit 10 --json

# A vague query: also search paraphrases from the chat model at OLLAMA_HOST, this run only
brain search "that thing about renewing" --rewrite

# Why a result ranked where it did
brain explain "vendor renewal"

# Read one result in full, by the id prefix search printed
brain show 1a2b3c4d

# Browse the newest Gmail documents
brain list --source gmail --limit 20

# One answer with citations, from a local model
brain ask "What did we decide about the vendor renewal?"

# What one tag's most recent documents say, with citations, from a local model
brain tags summarize vendor
```

## ingest: Add new content

```bash
# Preview a folder; nothing is stored
brain ingest-dir ./notes --tag planning --dry-run

# Writes: stores one file; running it again changes nothing
brain ingest ./notes/vendor-renewal.pdf --tag vendor

# Reads your real Gmail and prints real subjects, storing nothing; a scope flag is always required
brain ingest-gmail --from someone@example.com --since 2026/10/01 --max 20 --dry-run

# The same, for one Gmail label
brain ingest-gmail --label receipts --max 20 --dry-run

# Writes: stores a Slack thread you saved to a file
brain ingest-stdin --source slack --external-id thread-0001 --title "Design thread" < thread.txt

# Reads your real calendar and prints real titles, storing nothing; --until is exclusive
brain ingest-calendar --since 2026-10-01 --until 2026-11-01 --dry-run

# Reads your real GitHub activity in every org the account can see and prints real titles, storing nothing
brain ingest-github --mine --since 2026-10-01 --dry-run
```

## graph: See themes, people and connections

```bash
# Graph retrieval; the mode is chosen for you
brain graphrag search "vendor renewal"

# Themes across your conversations with one person
brain graphrag themes --person "Jane Doe"

# One entity and what it connects to
brain graphrag entity "Jane Doe" --depth 2

# The people and concepts in the most documents
brain graphrag entities --limit 20

# The clusters the graph has found
brain graphrag communities list

# How a topic moved over time
brain timeline "vendor renewal" --since 2026-01

# Everything about one person
brain people "Jane Doe"

# Graph size and health
brain graphrag stats
```

## brief-review: Catch up on the day and the week

```bash
# Writes: prints today's digest; also checks saved watches and records new hits
brain brief --no-enrich

# Open action items from the last week
brain todo --since 7d

# Findings waiting in the review queue
brain review list

# Preview a staleness scan; the queue is not changed
brain review scan --stale --dry-run

# This week's review, printed; no vault page is written
brain review weekly --no-emit

# Older documents worth another look
brain resurface --limit 5

# Searches that found nothing in the last 30 days
brain gaps --since 30d
```

## maintenance: Keep the brain healthy

```bash
# Is everything installed and answering?
brain doctor

# Document and chunk counts
brain status

# How many chunks still need an embedding; nothing is embedded
brain reembed --dry-run

# Preview a vault sync; no file or row changes
brain vault sync --dry-run

# Who has been using this brain, and how
brain usage --days 30

# Writes: a plaintext archive of the whole database and vault; it can be large
brain backup --label before-upgrade
```
