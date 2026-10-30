# RM-24 — `--entity` search filter

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-24 in `docs/specs/2026-10-23-roadmap-completion-design.md` (Wave 5; roadmap 3.3;
backlog rank 17). The ticket is the spec; this plan is the task list.

**U4.** The spec marks RM-24 NEEDS USER CONFIRMATION (§5 U4). It is **built on the coordinator's
ruling adopting §5 U4's recommendation; no user answer yet.** The ticket text is not edited.

## Goal

`brain search "q" --entity "<name>"` (and MCP `brain_search(entity=...)`) returns only documents
that mention that entity in the graph. Every leg, the total count and the facets see the filter.

## How the filter reaches the surfaces

The filter is two predicate keyword arguments, `entity_ids` and `entity_tenant`, built by
`brain.entity_filter.search_kwargs` and splatted into two places on each named surface:

1. `run_search(hybrid_search, …, **entity_kw)`. `run_search` passes unknown keyword arguments to
   `hybrid_search` unchanged (RM-09), and `hybrid_search` forwards them to its one
   `build_predicate` call. The FTS leg, the vector leg and the total count all read that one
   predicate.
2. The facet predicate. `hybrid_search` does not return its predicate, so `brain search --facets`
   and MCP `brain_search(facets=true)` each rebuild it from the same keyword arguments. Both
   rebuild sites get `**entity_kw` too.

Surfaces: `brain search` (CLI) and MCP `brain_search`, which the ticket names. **Not** `brain
explain`, `brain ui`'s `/api/search`, `brain recall`, `brain ask`, saved searches or MCP
`brain_related`. They pass no entity arguments, so `build_predicate`'s default (`None`) leaves
their SQL byte-identical.

## Files

| File | Change |
|---|---|
| `src/brain/entity_filter.py` (new leaf) | `resolve_entity_ids`, `graph_tables_present`, `search_kwargs`, `GRAPH_REQUIRED_MESSAGE`. Graph modules are imported inside functions, so a search without `--entity` does not import `brain.graph_rag`. |
| `src/brain/errors.py` | `EntityFilterError(BrainError)`, with `EntityNotFound`, `EntityAmbiguous` and `EntityGraphUnavailable` beside `PersonNotFound` / `PersonAmbiguous`. |
| `src/brain/search_predicate.py` | `entity_ids`, `entity_tenant`; the ticket's `d.id IN (SELECT gem.document_id …)` clause. `entity_ids` without `entity_tenant` raises `ValueError` (a caller bug). |
| `src/brain/search.py` (over the ceiling) | `hybrid_search` gains the two keyword arguments and forwards them. Recorded inline and in the module docstring. |
| `src/brain/cli_search.py` (budget ≤ 15) | `--entity`, `--entity-type`; one resolution call mapped to `typer.BadParameter` (exit 2); `**entity_kw` at the two sites. |
| `src/brain/mcp_server.py` (over the ceiling) | `brain_search` gains `entity` and `entity_type`; the error maps to `INVALID_PARAMS`; `**entity_kw` at the two sites. Recorded inline and in the module docstring. |
| `CLAUDE.md` | The `search.py` and `mcp_server.py` rows each gain a SHA-bound hop, in a later commit that does not touch either file (the `bc3b868` precedent). Neither row has a descriptive last hop to promote: `search.py` ends at `737247c`, `mcp_server.py` at `39fef78`, and no commit touched either file after them. |
| Docs | `CHANGELOG.md`, `docs/cli-reference.md`, `docs/configuration.md`, the skill that documents search filters, the backlog row, Appendix B. |

## Resolver

`resolve_entity_ids(conn, *, tenant_id, name, entity_type, rules=()) -> list[str]`:

1. Keys: `name` lowercased with its whitespace collapsed (the ticket's `lower(name)`, and the
   alias rules' key form), and the strip-all concept key (`graph_rag.extract._canonical_key`).
2. Alias redirects (`graph_rag.aliases` rules, loaded from `BRAIN_GRAPH_ALIASES_PATH`): a rule whose
   source `(type, key)` matches the name, or matches an entity the name resolved to, ADDS its
   target entity; the source is kept. Rules are merged only by `brain graphrag build` / `refresh`,
   and ingest and the watcher apply none, so an unmerged source can still carry mentions, and its
   target may not exist yet. A merged source has no mentions, so keeping it costs nothing, and a
   source already deleted by the orphan GC still reaches its target. (Round 1, rev-91: the first
   cut REPLACED the source with the target, which lost an unmerged source's documents and raised
   `EntityNotFound` when the target did not exist yet.)
3. `canonical_key` match, tenant-scoped, narrowed by `entity_type` when given.
4. Only when 2 and 3 found nothing: a case-insensitive `name` match.
5. No entity → `EntityNotFound`, whose message points at `brain graphrag entities`. Entities of
   more than one type and no `entity_type` → `EntityAmbiguous`, which lists the types. Types are
   judged after the redirect: a source whose target was found counts as the target's type.

`search_kwargs(conn, cfg, name, entity_type)` returns `{}` when `name` is `None`. Otherwise it checks
`cfg.graph_enabled` and that `graph_entities` and `graph_entity_mentions` exist (`to_regclass`). It
raises `EntityGraphUnavailable` with the ticket's message if either check fails. Then it resolves the
tenant (`graph_rag.tenancy.resolve_tenant`), loads the alias rules, and resolves. `entity_type`
without `name` is an `EntityFilterError`, and so is an unknown type.

## Tests (RED first)

- `tests/test_entity_filter.py` (DB on 5434): key match; strip-all key match; name fallback; alias
  redirect (source present with no mentions, and source deleted); tenant scoping; not found;
  ambiguous; `entity_type` narrows away the ambiguity; graph off; tables absent; `entity_type`
  without `name`; unknown type.
- `tests/test_search_predicate_entity.py`: the clause and its params; no filter leaves `where_sql`
  as `"TRUE"`; ids without a tenant raise `ValueError`.
- `tests/test_search_entity_filter.py` (DB): `hybrid_search` returns only the entity's documents
  on both legs (AC 1), and `total_documents` and `compute_facets` agree with the filtered set (AC 3).
- `tests/test_cli_search_entity.py`: AC 1, AC 3 through `--meta --facets`, AC 2 (exit 2 for not
  found and ambiguous), AC 4 (graph off → exit 2 with the message).
- `tests/test_mcp_search_entity.py`: the same four ACs through `brain_search`, mapped to
  `INVALID_PARAMS`.

Mutation proofs, in `git archive` copies only: the filter dropped from the FTS leg, from the vector
leg, and from the total or the facets; the tenant dropped; the alias step dropped; the ambiguity
check dropped; the graph-off check dropped; MCP mapping to `INTERNAL_ERROR`.

## Risks

- An alias the resolver misses gives an empty result; `EntityNotFound`'s message names `brain
  graphrag entities`.
- Budgets: `search.py` about +6, `cli_search.py` ≤ 15, `mcp_server.py` "one parameter". Measured at
  the end and stated in the PR.

## Deviations

1. **MCP takes `entity_type` as well as `entity`.** The ticket names only `entity`. Without
   `entity_type`, an MCP caller has no way to settle an `EntityAmbiguous` answer, which tells it to
   pick a type. One optional parameter beside the first keeps to the "one parameter" spirit.
2. **The resolver also tries the strip-all concept key.** The ticket says `canonical_key =
   lower(name)`. Concept entities have been keyed by strip-all since concepts-v6 (`Acme Platform`
   → `acmeplatform`), so `lower(name)` alone misses every multi-word concept by key. Such a name
   would then resolve only through step 4's display-name match, which runs only when no key matched.
3. **The facet predicate gets the filter at its rebuild sites, not through a shared object.** The
   ticket says the facets see the filter "because they share the predicate". In fact the CLI and
   MCP each rebuild the facet predicate from the same keyword arguments, so both rebuild sites take
   `**entity_kw`. AC 3 is tested on both surfaces.
4. **`brain explain` does not get `--entity`.** The ticket names `brain search` only, and the
   `cli_search.py` budget (≤ 15) has no room for a second command.
5. **Two refusals the ticket does not name.** `--entity-type` without `--entity`, and an
   `--entity-type` outside the graph's five types, are both exit 2 / `INVALID_PARAMS`
   (`EntityFilterError`). Without them, the first would be silently ignored, and the second would
   match nothing and report "not found".

## Round 3 (aud-91)

- aud-91's mutant M20 drops the `in present` guard in `resolve_entity_ids`' `found_types`. A source
  whose alias target is absent then counts as the target's type, and every earlier test passed.
  `test_a_cross_type_alias_with_an_absent_target_is_still_ambiguous` pins the guard. It seeds topic
  `sam` and person `sam`, plus a `topic sam → person "sam rivera"` rule with no `sam rivera` row.
  "Sam" must raise `EntityAmbiguous` naming both types. Under M20 the topic counts as a person,
  `found_types` collapses to `{person}`, and nothing raises.
- The red-first check is static, because 5434 was down, and CI is the run evidence. No `src/`
  change.
- `second-brain/master` (#88, #90) was merged in as its own commit, with both sides kept in the
  CHANGELOG, the backlog and Appendix B.

## Round 4 (chk-91)

- `search_kwargs` loaded the alias file unguarded, so a bad `BRAIN_GRAPH_ALIASES_PATH`
  (`GraphReconcileError`, `yaml.YAMLError`, `KeyError` / `TypeError`, and `OSError` /
  `ValueError` / `AttributeError` for an unreadable, undecodable or wrongly shaped file) became
  a CLI traceback or a generic MCP error. Search never read that file before RM-24.
  `entity_filter._load_rules` maps each to `EntityFilterError`, so the existing CLI exit 2 and
  MCP `INVALID_PARAMS` mappings apply.
- The message is `ALIAS_FILE_MESSAGE` plus `describe_failure(exc)`, never `str(exc)`. A YAML error
  quotes the bad line. `GraphReconcileError` redacts keys, but its invalid-type message prints the
  file's `type` field unredacted (measured: `alias rule has invalid type: <sentinel>:ha…`).
- Tests: `test_a_bad_alias_file_is_a_filter_error_that_quotes_nothing` (self-merge, invalid type,
  malformed YAML, missing key) plus one CLI and one MCP test for the malformed file. Each carries
  a sentinel name the message must not contain. RED on a `git archive` copy of `6e2c014`: 6 failed,
  each with the escaped loader error. GREEN: the five RM-24 test files pass 55.
- Round 5 (aud-91): every class in `_ALIAS_LOAD_FAILURES` is now tested. `TypeError` (a rule that
  is a bare string) and `AttributeError` (a top-level YAML list) are two more `BAD_ALIAS_FILES`
  entries, each carrying the sentinel. `ValueError` is covered by
  `test_a_non_utf8_alias_file_is_a_filter_error` (`UnicodeDecodeError`), and `OSError` by
  `test_an_alias_path_that_is_a_directory_is_a_filter_error` (`IsADirectoryError`). Mutation
  proofs ran in `git archive` copies with `PYTHONPATH` pinned to the copy. Dropping each of the
  four classes from the tuple fails its test: 4 of 4 killed.

## Found, not fixed

MCP `brain_search`'s facet predicate omits `updated_after` / `updated_before`, so with `facets=true`
and an F9 edit range, the facet counts describe a wider set than the results. The CLI passes both.
This predates RM-24 and is outside its scope; it is reported, not changed here, and tracked as
BL-24 in `docs/plans/2026-10-23-backlog-reconciliation.md`.
