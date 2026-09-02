---
description: Query the user's local second-brain knowledge base (career, past meetings, Slack/Gmail/Krisp transcripts) via the `brain` CLI. Use when the user asks about their background, past meetings, past Slack threads, past emails, interview prep, or specific people / projects they've mentioned.
when_to_use: Trigger on phrases like "what did I say about X", "my thoughts on Y", "meetings about Z", "interview prep", "tell me about my work on W", or any question requiring personal context the user has stored in their second brain.
---

# Second Brain — `brain` CLI

The user has a personal knowledge base at their vault (default
`~/brain-vault`). Use it whenever a conversation touches their career,
past meetings, past Slack threads, past emails, interview prep, or
specific people / projects they've mentioned.

**Search** — `brain search "..."` returns the top 5 matching documents
with snippets and IDs. Use `--json` if you need to parse output. Add
`--brief` (with `--json`) to triage on the cheaper ingest-time summary
instead of the chunk snippet.
Basic filters: `--source krisp|slack|gmail|manual`, `--tag X`,
`--has-tag X`, `--without-tag X`, `--since N` (days), `--limit N`,
`--fts-only`.
Metadata filters: `--person "Name"` (resolved via the directory, same
as `brain people`), `--after YYYY-MM-DD`, `--before YYYY-MM-DD`,
`--kind transcript|email|email_thread|note|markdown|pdf|...`,
`--thread <gmail-thread-id>`, `--draft` (only drafts) or `--no-draft`
(only published).
Prefer narrower queries — e.g. `brain search "interview prep"
--person "person-x" --after 2026-03-01 --kind transcript` is much better
signal than the same query unfiltered.

**Explain a search** — `brain explain "..."` shows per-result FTS rank,
vector cosine, RRF contributions, and recency boost so you can debug
why a result ranked where it did. `--verbose` also shows active filters.

**Read, budgeted** — `brain recall "<question>" --budget 2000 --json`
returns the matching passages themselves, packed to a hard token
ceiling, one per document, with inline `[N]` citations. It takes the
same filters as `brain search` (`--person`, `--after`/`--before`,
`--kind`, `--tag`, `--source`, `--thread`, `--without-tag`,
`--fts-only`), so those parts of a search you already tuned port over —
but `--draft`/`--no-draft`, `--has-tag`, `--sensitivity` and
`--updated-after`/`--updated-before` are search-only, and `brain recall`
rejects them. **This is the only retrieval surface with a token
ceiling** — reach for it whenever the next step is reading more than
one document. (`--budget` bounds the
passage block; the `--json` payload adds per-passage metadata and
measures ~1.25–1.3× the budget — `--budget 2000` prints ~2,500 tokens.)

**NEVER loop `brain show` over a search's results.** That recipe is
unbounded: bodies run to a mean ~18,000 characters and ~67k tokens at
the largest, so a 20-result read is six figures of tokens. Use `brain
recall --budget N` for several documents, `brain ask` for one
synthesized answer, and `brain show` only for a single document you
have already identified.

**Read full** — `brain show <id-prefix>` (6+ chars). Returns the
document body — the whole thing, for ONE document. `brain show <id>
--json` may also include a `summary` field if the document has been
auto-summarized; the key is omitted on docs that haven't been
summarized yet.

**Browse people** — `brain people` lists people with at least
`BRAIN_PEOPLE_HUB_MIN_DOCS` appearances in the corpus (default 3, owner
filtered out via `BRAIN_OWNER_PARTICIPANTS`). `brain people "Name"`
takes a case-insensitive substring of a display name and lists that
person's docs.

**Action items** — `brain todo` lists open Krisp-meeting action items.
Flags: `--source krisp` (only source supported today), `--since N`
(only items from docs ingested in the last N days), `--closed` (include
closed items, default open only), `--limit/-n N`, `--json`.

**Record a rating** — when the user explicitly says a search result was
useful or irrelevant, call `brain rate <id-prefix> useful` (or
`irrelevant`) so the interaction is logged. Don't volunteer this — only
when the user asks.

**Ingest Gmail** — `brain ingest-gmail` (requires one or more scope
flags — never bulk-ingest the inbox). Scope flags: `--query/-q` (raw
Gmail search), `--label/-l`, `--from`, `--since YYYY/MM/DD`,
`--until YYYY/MM/DD`. Optional: `--tag/-t`, `--max N` (default 50),
`--dry-run`, `--no-enrich`.

**Ingest from Krisp / Slack** — these don't have CLIs; pipe content via
`brain ingest-stdin --source krisp|slack --external-id … --title … --date
…` after fetching from the relevant MCP.

**When in doubt:** `brain doctor` confirms it's installed and healthy.
`brain --help` lists every subcommand.
