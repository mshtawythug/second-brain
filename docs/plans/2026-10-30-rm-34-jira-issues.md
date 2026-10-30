# RM-34 — Jira issues (plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-34 — Jira issues", and
§2.4 (the new-source protocol). Size S. No Jira API client and no CLI wrapper (RM-34.D1).

## Scope

1. **The `jira` kind.** `brain.source_kinds.VALID_SOURCE_KINDS` gains `"jira"`. This is the only
   place a kind is defined in Python (RM-07). `ui.schemas`, `cli._VALID_SOURCE_KINDS` and
   `vault.links._SOURCE_KINDS` re-export it, so validation, the facet panel, the source dropdown and
   `[[jira:<host>/<KEY>]]` links all pick it up with no further edit.
2. **`--source` help derives the list.** The `ingest-stdin --source` help text in `cli_ingest.py`
   hardcoded `gmail|krisp|manual|slack`, so it would have told an agent that `jira` is invalid. It
   now reads `source_kinds_hint()`. The change is **net-zero** in lines, so it owes no growth record;
   `cli_ingest.py` is in the CLAUDE.md ceiling table, so its trail gets a descriptive hop.
3. **Front-end glyphs.** `SOURCE_ICONS` in `ui/static/js/results.js` and `ui/static/js/discovery.js`
   gain `jira: "🎫"`. `MONTH_GROUPED_PATHS` in `tree.js` is **not** changed: the ticket states that
   no month grouping is the intended fallback.
4. **The recipe** in `skills/ingest-brain/SKILL.md`: the Atlassian MCP connector
   (`searchJiraIssuesUsingJql`, `getJiraIssue`) piped into
   `brain ingest-stdin --source jira --content-type jira_issue --sensitivity confidential`.
   - Default JQL, verbatim and parenthesized:
     `(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d`. Without the
     parentheses `AND` binds tighter and the bound applies to `reporter` only.
   - The agent refuses a JQL with no time bound. Project-wide pulls are not part of the recipe.
   - Dedup key `--external-id "<site-host>/<ISSUE-KEY>"`.
   - The body holds the summary, the description and the owner's comments. Others' comments go in
     only on request. Status and assignee are volatile and go in `--metadata`. A re-run whose body is
     unchanged reports `skipped` and does not touch metadata, so the recipe applies a changed status
     with `brain edit <id> --metadata '{...}'`, which patches without re-chunking.
   - Default `confidential`, because work-tracker content is the employer's.
   - The skill file stays inside `tests/test_skill_frontmatter.py`'s 100–300 line band.

## Two existing tests the new kind breaks (not named in the ticket)

- `tests/test_source_kinds_identity.py::test_source_kind_set_unchanged` pins the pre-RM-07 set.
  The pin becomes `PRE_RM07_KINDS | {"jira"}`, still restated as a literal on purpose.
- `tests/test_demo_corpus.py::test_corpus_covers_all_four_source_kinds` requires the 22-doc demo
  corpus to contain every kind. The demo manifest has no sensitivity field, and a Jira document
  defaults to confidential, so a demo Jira doc would showcase the wrong tier. Instead the guard gets
  a named `DEMO_EXEMPT_KINDS = {"jira"}`, with the reason recorded. The anti-drift property stays:
  the exemptions must be a subset of `VALID_SOURCE_KINDS`, and every other kind must still be
  covered. So a kind added later still fails the guard until it is added to the demo or exempted.

## Tests (synthetic only; made-up keys such as `example.atlassian.net/DEMO-1`)

New file `tests/test_jira_source.py`:

- `validate_source_kind("jira")` passes; `ui.schemas.parse_search_params` accepts `source=jira`.
- `ingest-stdin --help` names `jira`.
- End-to-end through the CLI against the 5434 database: a synthetic issue body, the recipe's
  metadata shape, `--sensitivity confidential`. The row has `sources.kind = 'jira'` and
  `sensitivity = 'confidential'`, its vault mirror lands under `_ingested/jira/`, and the metadata
  carries status and assignee.
- The facet panel buckets it: `compute_facets` returns a `jira` source bucket.
- Front-end: both `SOURCE_ICONS` tables have a `jira` entry, and `MONTH_GROUPED_PATHS` does not
  name `_ingested/jira` (the fallback is intended, so it is pinned).
- Skill text: the recipe contains the parenthesized JQL verbatim, `--sensitivity confidential`,
  `--source jira`, `--content-type jira_issue`, and the no-time-bound refusal.

Existing guards re-run: `test_source_kinds_identity`, `test_demo_corpus`,
`test_ingest_source_kind_validation`, `test_ui_schemas`, `test_ui_source_filter_none`,
`test_skill_frontmatter`, `test_new_skills`, `test_skills_token_routing`, `test_cli_ingest_stdin`.

## Protocol point 9 (cost)

Summaries and concept extraction stay on. The 30-day bound keeps one run to tens of issues.

## Known limitation, recorded and not fixed here

`vault/export._short_id` takes the first 8 characters of the external id for the mirror filename.
For `<host>/<KEY>` that is the host prefix (`example.`), so the segment carries no information about
the issue. The date and the slug keep filenames distinct, and the collision suffix handles the rest.
Atlassian cloud hosts are longer than 8 characters, so the segment never contains the `/`.

## Bookkeeping

CHANGELOG `[Unreleased]`; Appendix B RM-34 → `DONE (#<pr>)`; the CLAUDE.md ceiling-table hop for
`cli_ingest.py`; memory `extractors.md` / `cli.md` plus the MEMORY.md index lines, marked
"verify merged".
