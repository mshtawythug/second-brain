---
name: brain-todo
description: >
  Show open action items the user has captured in their local second-brain,
  and orchestrate the deferred-fetch flow that ingests action items from
  Krisp meetings into the brain. Use this skill whenever the user asks
  what's on their plate, what action items they owe, what follow-ups came
  out of recent meetings, or asks to pull Krisp action items. Backed by
  `brain todo` (parser/view) and `brain enrich --krisp-action-items`
  (deferred-fetch handoff that the agent completes via the Krisp MCP).
  MANDATORY TRIGGERS: what's on my plate, my action items, my todos, my
  TODOs, open action items, follow-ups, what do I owe, what do I need to
  do, krisp action items, pull action items, ingest krisp action items,
  brain todo, action items from my meetings, what are my next steps.
---

# Brain TODO

The brain stores Krisp action items as their own `content_type='krisp_action_items'`
documents (separate from the meeting transcript). `brain todo` parses
`- [ ]` / `- [x]` checklist lines out of those documents and prints one
row per item, so the user can see open work without opening N transcripts.

For searching meeting content, see `consult-brain`. For ingesting other
content, see `ingest-brain`.

## Showing open items — `brain todo`

```bash
brain todo                                  # all open items, all sources
brain todo --source krisp                   # Krisp-derived only
brain todo --since 30                       # items from action-item docs ingested in last 30d
brain todo --closed                         # include checked-off items
brain todo --limit 50 --json                # JSON output (for piping / summaries)
```

Flags:

| Flag | Purpose |
|---|---|
| `--source krisp` | Currently the only supported source. Stays as a flag for future Slack/Gmail extension. |
| `--since N` | Only items from action-item docs **ingested** in the last N days. Filters on `documents.ingested_at` — not the Krisp meeting date. A meeting from six months ago that you ingested yesterday still surfaces under `--since 7`. |
| `--closed` | Include `- [x]` items. Default is open-only. |
| `--limit N` | Cap rows (default 50). |
| `--json` | Machine-readable. Each row carries `document_id`, `document_title`, `ingested_at` (ISO 8601 string or `null`), `state` (`"open"` or `"done"`), `text`. |

The parser is `brain.todo.parse_action_items` — it walks the body for
`- [ ]` / `- [x]` lines, dedented, leading-bullet-tolerant. Items embedded
in nested lists are picked up.

## Pulling new action items from Krisp

Krisp action items aren't fetched by the brain CLI directly — the CLI prints
a *deferred-fetch handoff* that the agent fulfills via the Krisp MCP. This
pattern keeps the CLI free of live MCP knowledge.

### Step 1 — Get the handoff

```bash
brain enrich --krisp-action-items                       # all meetings the CLI knows about
brain enrich --krisp-action-items --since 14            # last 14 days
brain enrich --krisp-action-items --source-id MEET123   # one specific meeting
```

The CLI prints:

- The Krisp MCP tool names it expects the agent to use:
  `mcp__claude_ai_Krisp__search_meetings`,
  `mcp__claude_ai_Krisp__list_activities`,
  `mcp__claude_ai_Krisp__get_multiple_documents`.
- The window/scope in plain English with a concrete ISO 8601 date.
- The exact `brain ingest-stdin` invocation to pipe each fetched action-item
  blob into.

**Do NOT make up MCP parameter syntax** based on the CLI's prose. Read your
live MCP schema for the actual parameters each Krisp tool accepts. The CLI
deliberately names the tools and describes scope in English; the agent
parameterizes them from the live schema. This rule has been violated and
corrected multiple times — never speculate about kwargs.

### Step 2 — Call the Krisp MCP

Use the tools the CLI named, parameterized for the scope it described. Fetch
each meeting's action-item content (Krisp surfaces this as one of the
document kinds returned by `get_multiple_documents`).

### Step 3 — Pipe each result into `brain ingest-stdin`

This part of the recipe is literal (the brain CLI owns this flag shape):

```bash
echo "<action items markdown>" | brain ingest-stdin \
  --source krisp \
  --external-id "<meeting_id>--action-items" \
  --content-type krisp_action_items \
  --title "Action items — <meeting title> — YYYY-MM-DD" \
  --date YYYY-MM-DD \
  --metadata '{"parent_meeting_external_id": "<meeting_id>"}'
```

Notes:

- `--external-id` MUST be distinct from the transcript's external-id —
  conventionally suffix with `--action-items`.
- `--metadata.parent_meeting_external_id` is REQUIRED for `--content-type
  krisp_action_items` (BadParameter otherwise). Use the meeting's stable
  Krisp meeting id.
- The `action-items` tag is auto-applied via `_maybe_autotag_action_items`
  — no need to pass `--tag action-items` yourself.

### Step 4 — Report back

```bash
brain todo --since 7        # confirm the items are now visible
```

Tell the user: "Pulled N meetings (M action items, K still open)."

## Closing an item

Brain doesn't have a `brain todo close <id>` command. To check off an item,
edit the source action-item document directly:

```bash
brain edit <action-item-doc-id-prefix>
# In the editor, change `- [ ]` to `- [x]` on the line you want to close.
```

The body change re-chunks + re-embeds; the next `brain todo` run reflects it.

## Operational notes

- `brain todo` is read-only. It never modifies docs.
- Items are matched by line — a single action-item doc can yield many rows.
- The parser does NOT promote items into the wiki's "Related" fence or the
  graph — those are derived from `[[wiki-links]]` and metadata, not
  checklist markers.
- If the user asks "what action items came out of yesterday's calls" and
  `brain todo --since 1` returns nothing, propose Step 1 to ingest first
  (don't fabricate items).
