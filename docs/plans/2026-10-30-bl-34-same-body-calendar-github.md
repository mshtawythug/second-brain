# BL-34 — same-body collision and empty result in `ingest-calendar` / `ingest-github`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Found in the #103 review (sec-103b INFO 3, and rev-104's Notion finding). Base: master `ef626d1`.

## The defect

`ingest_document` looks a sourced document up by `(kind, external_id)` and, when that
finds nothing, falls back to the stdin content-hash index (`ingest/__init__.py`,
`_plan_action`). A NEW external id whose body equals another stdin document's
therefore gets `skip` back, with **that other document's id**: `created=False`,
`body_changed=False`.

- `cli_ingest_calendar._ingest_one` and `cli_ingest_github._write` then hand
  `result.document_id` to `refresh_volatile` with `stored={}`. That patches the
  OTHER document's volatile metadata (calendar: occurrences, date, participants;
  github: state, labels, timestamps, …) and reports `refreshed`. The new item is
  never stored.
- An empty result (`document_id=None`: no chunks) hits
  `assert result.document_id is not None`.

**Calendar is live.** Its body is title, `When:`, location, attendees and
description, with no event id, so the same meeting on two calendars, or a
duplicated invite, collides. **GitHub is latent**, because every body opens with
`<owner>/<repo>#<n>` or `<repo> README (<path>)`. Neither can produce an empty
body today (calendar always has a `When:` line, github its header), so the empty
branch is a backstop. Prod holds 0 calendar and 0 github sources: no repair.

## Fix: one rule, one home

`ingest/volatile_patch.py` gains one function that settles an `IngestResult`
for an item whose own row was looked up before the call:

```python
Settled = Literal["ingested", "updated", "refreshed", "unchanged", "empty", "same_body"]

def settle(conn, result, *, stored, metadata, keys, vault_root, graph_syncer) -> Settled:
    if result.created or result.body_changed:
        return "ingested" if result.created else "updated"
    if result.document_id is None:
        return "empty"
    if stored is None or result.document_id != stored[0]:
        return "same_body"          # a foreign row: never patched, never forced
    return refresh_volatile(conn, document_id=result.document_id, stored=stored[1], ...)
```

`refresh_volatile` keeps its signature and stays the primitive. Calendar's
master-fallback path calls it on `stored[0]`, which is the item's own row by
construction, so it is unchanged.

| Caller | Change |
|---|---|
| `cli_ingest_calendar._ingest_one` | After `ingest_document`, `settle(...)`. The assert goes. On `ingested`/`updated` it still records attendees when not yet recorded, exactly as now. |
| `cli_ingest_github._write` | `settle(...)`. The assert goes. |
| `cli_ingest_apple_notes._write` | Its inline rule (#103) is replaced by `settle(...)`, so the rule has one home. Its output is unchanged. |

### Reporting

- **Calendar:** prints `  same body as another event: <external id>` and
  `  empty <external id>`.
- **GitHub:** prints `  same body as another item: <external id>` and `  empty <external id>`.
- **Apple Notes** is unchanged (`same body as another note: <id>`, `empty <id>`).
- **The two counts** are appended to each summary line **only when non-zero**
  (`, N same body as another event`, `, N empty`). A run without either prints
  exactly what it prints today, so existing output and its tests stay
  byte-identical. Neither outcome feeds any remaining or due count: the calendar
  window and the github cap count items read, not rows written.

## Decisions for approval

- **D1 (APPROVED; pinned by a test), summary format:** append the counts only when non-zero, as above, rather than
  always (Apple Notes always prints its counts; its line stays as it is).
- **D2 (APPROVED as a recorded residual), calendar attendee recording:** for an event with no row of its own,
  attendees are recorded in the people directory BEFORE `ingest_document` (so
  the graph reconcile resolves them). A same-body event therefore still records
  its attendees, and because it never gets a row, every re-run records them again
  and bumps their occurrence counts. That is today's behaviour too (the event
  never had a row before this fix either). Proposal: **leave it**, record it in the
  BL-34 row as a residual, and do not reorder the record in this fix ("behaviour
  otherwise identical"). The alternative, recording after the write and only
  for `ingested`, changes when a new event's attendees resolve in the graph.
- **D3 (APPROVED), the backlog row:** #104 also adds BL-33 and BL-34; whichever lands second keeps ONE BL-34 row, marked SHIPPED. master has rows through BL-32 and no BL-34. I add a
  `BL-34` row (marked SHIPPED) after BL-32. If #104 adds BL-33, this is a
  one-line union at merge.

## Tests (TDD)

- `tests/test_volatile_patch.py`: `settle` on all six outcomes against the 5434 DB,
  including a foreign row (its metadata is unchanged) and `document_id=None`.
- `tests/test_cli_ingest_calendar.py`: two events, two ids, one body, run twice.
  Only the first is stored, the second is reported by id on both runs, and the
  first row's occurrences, date and participants are untouched. Also: an empty
  result (an `ingest_document` double returning `document_id=None`) is reported
  `empty`, with no assert.
- `tests/test_cli_ingest_github.py`: the same two cases. A natural same-body
  collision cannot be built (the header line differs), so it uses a second item
  whose rendered body equals the first's through an `ingest_document` double, or
  a fixture pair that renders identically if one exists.
- Every existing calendar, github and apple-notes test passes unchanged.
- Mutation probes in git-archive copies: each `settle` branch, and each CLI's
  reporting of both outcomes. 100% line and branch coverage on `volatile_patch`
  and the touched CLI functions.

## Files

`src/brain/ingest/volatile_patch.py`, `src/brain/cli_ingest_calendar.py`,
`src/brain/cli_ingest_github.py`, `src/brain/cli_ingest_apple_notes.py`, the
three CLI test files plus `tests/test_volatile_patch.py`, `CHANGELOG.md`, the
backlog doc's BL-34 row, and memory (`extractors.md`, `types.md`). None of these
files is in the CLAUDE.md ceiling table.
