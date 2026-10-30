# RM-17 — The brief's "new this week" entities section

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-17 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(roadmap 4.1, "new entities or projects newly mentioned"). The ticket is the spec; this is the task
list, the rulings and the record of what changed from the ticket.
**Base:** `second-brain/master` `2f22d13` (RM-15 and RM-16 merged).
**Branch:** `feat/rm-17-brief-new-entities`.

## Goal

The daily brief names the people, organisations and projects that first appeared in the corpus in
the last 7 days, on every brief surface, each under its own confidential lens.

## Files

| File | Change |
|---|---|
| `src/brain/brief_entities.py` | **New leaf.** `NewEntity`, `new_entities(conn, *, tenant_id, since, exclude_confidential, limit=10)`, `entity_tables_present`, `for_brief` (the 7-day window, the graph-disabled and tables-absent gates), `section_lines`, `SECTION_TITLE`. |
| `src/brain/brief.py` | `BriefData.new_entities` (default empty) and its `to_dict` key; `assemble_brief` fills it under its lens (the fifth gated read); the page renders `## New this week`; `terminal_sections` gives the terminal its optional sections. **+18 lines** (386 → 404; budget +25). |
| `src/brain/cli.py` | `_print_brief` prints the optional sections through `brief.terminal_sections`, replacing the watch block line for line. **Net-zero** (see Deviations). |
| `src/brain/brief_delivery.py` | `render_delivery` adds the section after the watch hits, one line per name. |
| `src/brain/brief_publish.py` | The suggestion re-derivation check (`_suggestion_inputs`, was `_without_watches`) ignores `new_entities`. |
| `tests/graph_entity_fakes.py` | Shared synthetic seeders (documents, entities, mentions). |
| `tests/test_brief_entities.py` | The leaf: the four acceptance criteria, the lens, drafts, types, tenant, prefilter, order, limit, tables absent, rendering. |
| `tests/test_brief_new_entities.py` | Every surface: payloads, page, terminal, `build_brief`'s two audiences, delivery, `without_drafts`, the suggestion check. |
| `CLAUDE.md` | The `cli.py` ceiling row gains its net-zero hop. |
| Docs | `CHANGELOG.md`, `docs/cli-reference.md`, `docs/configuration.md`, `skills/brain-proactivity/SKILL.md`, the backlog row (4.1), Appendix B. |

## The query

```sql
SELECT e.id, e.name, e.entity_type, MIN(d.ingested_at), COUNT(*)
FROM graph_entities e
JOIN graph_entity_mentions m ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
JOIN documents d ON d.id = m.document_id
WHERE e.tenant_id = %s
  AND e.created_at >= %s                 -- prefilter only
  AND e.entity_type = ANY(%s)            -- person, org, project
  AND d.draft = FALSE
  [AND d.sensitivity <> 'confidential']  -- the lens, before the aggregate
GROUP BY e.id, e.name, e.entity_type
HAVING MIN(d.ingested_at) >= %s          -- the decider
ORDER BY document_count DESC, first_ingested_at DESC, e.name, e.id
LIMIT %s
```

The two lens variants are frozen strings chosen by `exclude_confidential`, the house pattern; no
level is bound as a parameter.

## Rulings

- **D1 (the ticket's RM-17.D1): "first appeared" is `ingested_at`.** An old thread imported this
  week is new to the brain, which is what the owner has not seen yet.
- **D2, order and size.** Most-mentioned first, then the most recent first mention, then name and
  id (a total order). At most 10 names.
- **D3, delivery: the names are delivered.** Delivery sends the gated payload, and entity names are
  title-class (spec §2.5 rule 3: titles and counts by default), so the section goes into the email
  and the Slack post after the watch hits, one line per name, each through `_one_line` and Slack
  escaping. The aggregate never counts a draft, in either lens, so no entity known only from drafts
  reaches the page, the terminal or the message, and `without_drafts` has nothing of this section
  to remove (it keeps `new_entities` as it is). Tests:
  `test_build_brief_gates_the_page_and_delivery_and_never_lists_drafts` and
  `test_without_drafts_keeps_the_section`.
- **D4, the window.** `for_brief` uses the wall clock: `now − 7 days`. It does not follow `--since`
  (the capture window, in hours) or `--date` (the header only, as for every other section). `now`
  is a keyword seam for tests.
- **D5, the gates.** "Graph disabled" is `cfg.graph_enabled` false (`BRAIN_GRAPH_ENABLED`).
  "Tables absent" is two `to_regclass` probes (`graph_entities`, `graph_entity_mentions`), the
  shape of `saved_search.watch_tables_present`. Either one gives an empty list and no query.
- **D6, the line.** `[type] name`, with any whitespace run in a name collapsed to one space, so an
  extracted name cannot break a Markdown or terminal line.
- **D7, suggestions.** `build_brief` re-derives the published page's suggestions only when the two
  payloads differ in what the prompt reads. The section is not in the prompt, so it is left out of
  that comparison, as the watch hits are: a confidential-only entity costs no second Ollama call.
- **D8, the lens sits inside the aggregate.** Gated, an entity mentioned by an old confidential
  document and a new normal one IS listed (its first visible mention is new), and the permissive
  payload does not list it (its first mention is old). Filtering after the aggregate would drop
  it from the gated payload, and that absence would say a confidential mention exists.

## Tests (RED first)

1. `tests/test_brief_entities.py` written first; RED: `ModuleNotFoundError: brain.brief_entities`.
2. `tests/test_brief_new_entities.py` written next; RED: `ImportError: cannot import name
   'terminal_sections'`. With `brief.py` done and the other three files untouched, four tests stayed
   RED (the CLI print, `build_brief`'s delivery, the suggestion count, the delivery render), and
   each went GREEN with its own file's change.
3. Mutation proofs (in a `git archive` copy only): the lens moved after the aggregate; drafts not
   excluded; `created_at` deciding instead of prefiltering; the type filter dropped; the
   tables-absent check dropped; the graph-disabled check dropped; the prefilter dropped; the tenant
   filter dropped. Each is killed by at least one test.

## Risks

- The prefilter misses an entity created before the window whose older mentioning documents were
  all deleted since (the ticket's accepted risk). It only delays a name; it never leaks one. A test
  pins the behaviour.
- `graph_entities` has no index on `created_at`. The table is a personal corpus's entity set, and
  the prefilter runs once per brief per lens.

## Deviations

1. **`cli.py` is touched**, though the ticket's scope names only the leaf and `brief.py`. The
   terminal must show the section, and `cli._print_brief` prints the brief. Its four-line watch
   block became a four-line loop over `brief.terminal_sections`, so the file is **net-zero**
   (8,786 → 8,786) and owes no growth record. Its ceiling row gains the hop.
2. **`brief_delivery.py` and `brief_publish.py` are touched** (both shared with RM-15 and RM-16,
   spec §4.2): the delivered message carries the section (D3), and the suggestion check ignores it
   (D7).
3. **`brain_brief`'s MCP docstring does not name `new_entities`.** `mcp_server.py` is over the
   ceiling, and the tool needs no code change: it returns `assemble_brief(...).to_dict()` under its
   own lens (`exclude_confidential=not include_confidential`), so the key is gated like the others.
   `docs/configuration.md`'s MCP table names the key.
