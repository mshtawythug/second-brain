# F5-F8 — Search transparency and brain note move

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Search transparency — result counts, latency, facets — and `brain note move`

---

### 1. Goal

Two user-visible outcomes, both about *knowing what the brain just did*.

**(a) Search transparency.** Today `brain search` prints a bare table and nothing else. The user cannot tell whether the 5 rows are 5 of 5 or 5 of 544, cannot tell that the 6.1-second wall clock was almost entirely one Ollama embed call rather than a slow database, and has no way to see the *shape* of the match set (which sources, which content types, which tags). After this work, every human `brain search` ends with a one-line stderr footer (`544 matched · 5 shown · embed 5820ms · sql 214ms · total 6042ms`), `--facets` prints a grouped breakdown of the whole match set, and machine consumers can opt into the same metadata without a single existing byte of `--json` output changing shape.

**(b) `brain note move`.** A vault note can be renamed but cannot be moved. `brain note rename` (cli.py:8073) relocates a note only as a *side effect* of a slug change, always within its current folder (`old_relative.with_name(...)`, rename.py:164). Reorganising a vault — "these six notes belong under `projects/`" — currently requires moving files in Finder and hoping `brain vault sync --watch` reconciles, which is precisely the path that historically destroyed incoming backlinks. `brain note move` makes relocation a first-class, atomic, link-refactoring, dry-runnable operation on the same machinery that already backs rename.

---

### 2. Current state

#### 2.1 Search

| Fact | Evidence |
|---|---|
| `hybrid_search` returns `list[SearchResult]` and nothing else | search.py:231-255, 615 |
| `SearchResult` has 7 public fields + optional `explain` | search.py:88-99 |
| `SearchDiagnostics` already exists as a mutable out-parameter, carrying exactly one field `fts_count: int \| None` | search.py:65-85 |
| `fts_count` is **explicitly documented as NOT a total** — it is `len(fts_rows)`, capped by `CANDIDATE_LIMIT = 50` and `PER_DOC_CHUNK_CAP = 3`; only the zero case is exact | search.py:76-82, 103, 110, 429-430 |
| The WHERE predicate is built inline inside `hybrid_search` across ~45 lines and reused by *three* call sites in the same function (FTS leg, vector leg, and the `has_filters` fast-path decision) | search.py:325-388, 405-406, 437-442 |
| Timing is measured nowhere in the search path | no `perf_counter` / `time` import in search.py:27-36 |
| Query embedding is LRU-cached in-process, so embed latency is bimodal (cold ~5.8 s on this machine vs ~0 ms warm) | search.py:159, 184-213 |
| `brain search --json` emits a **bare top-level list** of 7-key objects | cli.py:4215-4230 |
| MCP `brain_search` returns `{"session_id": …, "results": [ …7-key objects… ]}` and its docstring explicitly warns that the top-level shape already changed once in Q1-C | mcp_server.py:346-361, 461-474 |
| Precedent for keeping stdout clean: the degraded-embedder hint is written to **stderr** specifically "so it never pollutes `--json` stdout" | cli.py:4084-4098 (docstring at 4090) |
| `search_table` renders the Rich table with no footer/caption | format.py:40-61 |
| `search.py` is 704 lines; `format.py` is 783 lines — both close to the 800-line ceiling | measured |
| `hybrid_search` itself is ~385 lines — already far over the 50-line function guideline (pre-existing) | search.py:231-615 |

Measured on the live corpus (read-only, prod DB port 55432, 1376 documents / 13079 chunks):

```
SELECT count(DISTINCT c.document_id) FROM chunks c
  WHERE c.tsv @@ to_tsquery('english','meeting');
  →   544   cold 2749 ms   warm 34-42 ms
EXPLAIN: Seq Scan on chunks (1981 of 13079 rows pass the filter),
         Sort, Aggregate. Planner declines the GIN index at 15% selectivity.
Narrow query ('hybrid & search') → 9 docs, 28 ms.
Three-way facet rollup over the same match set → 126 ms warm, 161 rows.
```

**Missing:** exact total match count; any latency instrumentation; any facet surface; any metadata channel on either output surface.

#### 2.2 `brain note move`

| Fact | Evidence |
|---|---|
| `plan_rename` computes the destination as `old_relative.with_name(f"{new_slug}.md")` — folder is structurally fixed | rename.py:163-165 |
| `apply_rename` already owns snapshot → write → restore-on-failure, plus the whole-vault `[[wikilink]]` reference refactor | rename.py:199-324 |
| `collect_references` matches **both** title form (`[[Old Title]]`, case-insensitive) and **path form** (`[[folder/old-slug\|Old Title]]`, case-sensitive POSIX) — path form is what `link_rewrite` writes for Quartz | rename.py:332-432, esp. 356-363, 407-409 |
| `apply_rename` step 3 does `new_path.write_text(...)` then `old_path.unlink()` — **not** `os.rename` | rename.py:274-278 |
| The watcher's `delete` handler re-stats and, if the file really is gone, runs `_handle_delete` → `DELETE FROM documents WHERE vault_path = …`, whose `ON DELETE CASCADE` wipes incoming `links` rows | watch.py:730-773, 916, and the post-mortem docstring at watch.py:987-1003 |
| The watcher's `move` handler is the *safe* path: `UPDATE documents SET vault_path=%s WHERE kind='vault' AND vault_path=%s`, preserving the document id and every incoming backlink, then a scoped `sync_one_file` | watch.py:977-1052, esp. 1027-1031 |
| Vault-escape guard already exists and already follows symlinks on both sides | cli.py:7726-7745 |
| `RenameError` inherits plain `Exception`, **not** `BrainError` — a pre-existing violation of the project rule | rename.py:46 vs errors.py:16 |
| No `move` command exists anywhere | `grep -n "note_app.command" src/brain/cli.py` → only `new` (7819) and `rename` (8073) |

**Missing:** the ability to specify a destination folder, at any layer.

#### 2.3 Migration

**This section requires no migration.** No schema change: the total count, timings and facets are all computed at query time from existing columns (`chunks.tsv`, `chunks.document_id`, `documents.content_type`, `documents.tags`, `sources.kind`); `brain note move` writes only to the existing `documents.vault_path`. `024_agent_attribution.sql` and `025_document_sensitivity.sql` belong to other sections and are not touched here.

---

### 3. User-visible surface

#### 3.1 `brain search` — new flags

Added to the existing `search()` signature (cli.py:4102-4154), after `--fts-only`, before the Q1-C filter block:

| Flag | Type / default | Help text |
|---|---|---|
| `--facets` | `bool = False` | `Group the full match set by source, content type, and top tags.` |
| `--no-meta` | `bool = False` (i.e. `meta` defaults on) | `Suppress the match-count / latency footer (human output only).` |
| `--meta` | `bool = False` | `With --json, wrap results in an envelope carrying counts, timings, and facets. Without --json this flag is a no-op (the footer is already on).` |

No existing flag changes type, default, or help text.

#### 3.2 Human output — literal sample

```
$ brain search "meeting notes" --limit 3
                                  Search results
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ID       ┃ Title                      ┃ Source ┃ Score ┃ Snippet                     ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 3f2a1c9d │ Weekly sync — platform     │ krisp  │ 0.031 │ We walked the backlog and…  │
│ 88be04a1 │ Roadmap review notes       │ manual │ 0.028 │ Three themes came out of…   │
│ c1d4e770 │ Re: agenda for Thursday    │ gmail  │ 0.026 │ Attaching the deck we…      │
└──────────┴────────────────────────────┴────────┴───────┴─────────────────────────────┘
544 matched · 3 shown · embed 5820ms · sql 214ms · total 6042ms          ← STDERR
```

Cache-warm second invocation in the same process (MCP / `brain ask`):

```
544 matched · 3 shown · embed 0ms (cached) · sql 198ms · total 201ms
```

FTS-only mode (the `NullEmbedder`, or `--fts-only`) omits the embed segment entirely:

```
544 matched · 3 shown · sql 187ms · total 190ms
```

The footer goes to **stderr**, following the precedent set by `_warn_if_fts_only_degraded` (cli.py:4090). `brain search q --json | jq` therefore stays byte-identical to today, and `brain search q > out.txt` still captures only the table.

`--facets` adds a second stderr block after the footer:

```
$ brain search "meeting notes" --facets --limit 3
  … table as above …
544 matched · 3 shown · embed 5820ms · sql 214ms · facets 126ms · total 6168ms

Source          Content type            Tags
  krisp    311    transcript    311       1:1              84
  gmail    198    email_thread  142       planning         61
  manual    31    email          56       hiring           40
  slack      4    note           31       roadmap          33
                  markdown        4       platform         22
                                          retro            14
                                          budget            9
                                          offsite           6
                                          (+153 more)
```

`(no results)` (cli.py:4232) is unchanged; when there are zero results the footer still prints (`0 matched · 0 shown · …`) because "your query matched nothing and here is how long that took" is exactly the case the user most wants explained. `--facets` with zero matches prints `no facets (0 documents matched)`.

#### 3.3 JSON — backward compatibility

**Recommendation, and the one I am specifying: keep `--json` a bare list forever; put every new field behind the opt-in `--meta` envelope; let the MCP object grow keys inline.**

*Default `brain search --json` — unchanged, bare list of 7-key objects:*

```json
[
  {"id": "3f2a…", "title": "Weekly sync — platform", "source_kind": "krisp",
   "snippet": "We walked…", "score": 0.031, "content_type": "transcript",
   "tags": ["1:1", "platform"]}
]
```

*`brain search --json --meta` — new envelope:*

```json
{
  "query": "meeting notes",
  "total_documents": 544,
  "returned": 3,
  "fts_count": 50,
  "timing_ms": {"embed": 5820.4, "sql": 214.1, "facets": null, "total": 6041.7},
  "embed_cached": false,
  "fts_only": false,
  "facets": null,
  "results": [ /* the SAME 7-key objects, byte-identical */ ]
}
```

With `--facets`, `"facets"` becomes:

```json
{"source": [{"value": "krisp", "count": 311}, {"value": "gmail", "count": 198}],
 "content_type": [{"value": "transcript", "count": 311}],
 "tag": [{"value": "1:1", "count": 84}], "tag_truncated": 153}
```

`--facets --json` **implies** `--meta` (there is nowhere else for facets to go); this is stated in the `--facets` help text and is not a shape change for anyone, because nobody passes `--facets` today.

*MCP `brain_search` — additive keys on the existing dict:*

```json
{"session_id": "…", "results": [ …unchanged… ],
 "total_documents": 544, "returned": 3, "fts_count": 50,
 "timing_ms": {"embed": 5820.4, "sql": 214.1, "facets": null, "total": 6041.7},
 "embed_cached": false, "fts_only": false, "facets": null}
```

**Why this split rather than "just make `--json` an object".** The CLI `--json` list is consumed positionally: the `consult-brain` skill, `~/.claude/CLAUDE.md` ("Use `--json` if you need to parse output"), and any user shell script all do `jq '.[0].id'` or `payload[0]["id"]`. Turning that into `{"results": …}` is a silent, unversioned break in a personal tool with no deprecation channel — the exact mistake mcp_server.py:358-360 documents having made once already. The MCP surface is different in kind: it is a *dict today*, MCP clients read named keys, and adding keys to a JSON object is the canonical non-breaking evolution. So: object grows, list stays a list, and the CLI gets a flag for people who want the object. The cost is one extra flag; the benefit is that no existing invocation anywhere changes behaviour.

#### 3.4 `brain note move`

```
brain note move <ID-PREFIX> <NEW-FOLDER> [--dry-run] [--yes] [--no-link-refactor] [--vault PATH]
```

| Argument / flag | Type / default | Help text |
|---|---|---|
| `id` | `str`, required positional | `Document id (or 6+ char prefix).` |
| `new_folder` | `str`, required positional | `Destination subdirectory under the vault root. Use "" or "." for the vault root. Created if missing.` |
| `--dry-run` | `bool = False` | `Print the plan without changing anything.` |
| `--yes`, `-y` | `bool = False` | `Skip the confirmation prompt.` |
| `--no-link-refactor` | `bool = False` | `Skip rewriting [[…]] references in other notes. The file still moves.` |
| `--vault` | `Path \| None = None` | `Override the configured vault path.` |

Dry run:

```
$ brain note move 3f2a1c9d projects/atlas --dry-run
would move inbox/weekly-sync-platform.md → projects/atlas/weekly-sync-platform.md
would rewrite 4 reference(s) in 3 file(s):
  daily/2026/2026-07-14.md:12    [[inbox/weekly-sync-platform|Weekly sync — platform]] → [[Weekly sync — platform]]
  daily/2026/2026-07-21.md:7     [[inbox/weekly-sync-platform|Weekly sync — platform]] → [[Weekly sync — platform]]
  projects/atlas/index.md:31     [[inbox/weekly-sync-platform|Weekly sync — platform]] → [[Weekly sync — platform]]
  projects/atlas/index.md:44     [[inbox/weekly-sync-platform#Decisions|…]] → [[Weekly sync — platform#Decisions]]
(dry run — nothing written)
```

Real run:

```
$ brain note move 3f2a1c9d projects/atlas
Move inbox/weekly-sync-platform.md → projects/atlas/weekly-sync-platform.md,
rewriting 4 reference(s) in 3 file(s)? [y/N]: y
rewrote 4 reference(s) in 3 file(s)
moved inbox/weekly-sync-platform.md → projects/atlas/weekly-sync-platform.md (id=3f2a1c9d)
```

Collision:

```
$ brain note move 3f2a1c9d projects/atlas
target path already exists: projects/atlas/weekly-sync-platform.md — rename this
note first (`brain note rename 3f2a1c9d "<new title>"`) or pick another folder
$ echo $?
1
```

`brain note move` prints no JSON (neither does `brain note rename`) and adds no MCP tool. Backward-compat risk: **none** — every string above is new output from a new command; `brain note rename`'s output strings (cli.py:8145-8158) and `_print_rename_plan` (cli.py:8046-8070) are reused verbatim for the reference-rewrite lines but the move/rename headline lines are branched, so rename's existing golden strings are untouched.

---

### 4. Module layout

| Path | New/changed | Purpose | Lines |
|---|---|---|---|
| `src/brain/search_predicate.py` | **new** | `SearchPredicate` frozen dataclass + `build_predicate()`. The WHERE/JOIN/prepare construction extracted verbatim out of `hybrid_search` so the count query, the facet query, the FTS leg and the vector leg all share **one** predicate. | ~140 |
| `src/brain/facets.py` | **new** | `FacetBucket`, `SearchFacets` dataclasses + `compute_facets(conn, *, predicate, tsquery, top_tags)`. Pure read; no embedder dependency. | ~150 |
| `src/brain/format_search.py` | **new** | `search_meta_line(diag, *, returned) -> str`, `facets_renderable(facets) -> Table`, `search_meta_json(...) -> dict[str, Any]`, `search_envelope_json(...) -> dict[str, Any]`. Lives outside `format.py` because that file is already 783/800 lines. | ~120 |
| `src/brain/search.py` | changed | Loses the inline predicate block (−45), gains `perf_counter` phases, a `total_count: bool = False` kwarg, and the new diagnostics writes (+55). **704 → ~714.** | ~714 |
| `src/brain/cli.py` | changed | `search()` gains 3 flags + meta/facet plumbing (+50); new `note_move()` command + `_print_move_plan()` (+75). Already 9760 lines — a pre-existing, separately-tracked violation (see the GraphRAG G0–G4 deferral note in `docs/plans/2026-05-20-graphrag.md`); this section does not create it and does not attempt the split. | +125 |
| `src/brain/mcp_server.py` | changed | `brain_search` passes `total_count=True`, accepts `facets: bool = False`, and merges `search_meta_json(...)` into its return dict. Docstring updated to list the new keys as additive. | +18 |
| `src/brain/vault/rename.py` | changed | `plan_rename(..., new_folder: str \| None = None)`; `apply_rename` step 3 switches to `Path.replace` + in-place `vault_path` UPDATE; no-op reference filter; `RenameError` re-parented to `BrainError`. **591 → ~665.** | ~665 |
| `src/brain/vault/paths.py` | changed | New `assert_within_vault(target: Path, vault_root: Path) -> None` raising `VaultPathEscape`. | +25 |
| `src/brain/errors.py` | changed | New `VaultPathEscape(BrainError)`. | +10 |
| `src/brain/format.py` | **unchanged** | Deliberately: adding here would push it past 800. | 783 |

---

### 5. Design detail

#### 5.1 `search_predicate.py`

```python
"""Shared WHERE/JOIN construction for every leg of hybrid search."""

@dataclass(frozen=True)
class SearchPredicate:
    """Immutable, fully-built SQL predicate shared by all search legs.

    ``where_sql`` is a parameterized fragment (``%s`` placeholders only —
    no user text is ever interpolated); ``where_params`` are its bound
    values in positional order.
    """
    where_sql: str                 # e.g. "TRUE AND d.content_type = %s"
    where_params: tuple[Any, ...]  # immutable; callers splat into a list
    has_filters: bool              # where_sql != "TRUE"
    join_clause: str               # "" or "JOIN documents d ON d.id = c.document_id"
    fts_filter: str                # "" or f" AND {where_sql}"
    prepare_flag: bool | None      # True on the no-filter fast path, else None


def build_predicate(
    *,
    source_kind: str | None,
    tag: str | None,
    since_days: int | None,
    person_keys: list[str] | None,
    after: datetime | None,
    before: datetime | None,
    content_type: str | None,
    thread_id: str | None,
    draft: bool | None,
    without_tag: str | None,
) -> SearchPredicate: ...
```

The body is search.py:325-388 moved verbatim, including `_ensure_utc` (which moves with it and is re-exported from `search` for the existing `tests/test_search_metadata_filters.py` import path). **This is how predicate duplication is avoided**: the count query and the facet query take a `SearchPredicate` and cannot drift from the FTS/vector legs, because there is exactly one construction site.

The count query joins `documents` **whenever `has_filters`**, using the identical `join_clause`. On the no-filter fast path it omits the join, exactly as the FTS leg does (search.py:384-387), and for the same reason (the FK guarantees the join is row-preserving).

#### 5.2 Total match count

New in `search.py`:

```python
_TOTAL_COUNT_SQL = """
    SELECT count(DISTINCT c.document_id)
    FROM chunks c
    {join_clause}
    WHERE c.tsv @@ to_tsquery('english', %s){fts_filter}
"""


def _count_matching_documents(
    conn: psycopg.Connection,
    *,
    predicate: SearchPredicate,
    tsquery: str,
) -> int:
    """Exact count of DISTINCT documents whose chunks match the lexical query.

    Reuses ``predicate`` — the same object the FTS and vector legs use — so
    the count can never diverge from the ranked set's filters.
    """
    sql = _TOTAL_COUNT_SQL.format(
        join_clause=predicate.join_clause, fts_filter=predicate.fts_filter
    )
    row = conn.execute(
        sql, [tsquery, *predicate.where_params], prepare=predicate.prepare_flag
    ).fetchone()
    return int(row[0]) if row else 0
```

`{join_clause}` / `{fts_filter}` are f-string slots filled from `SearchPredicate` fields that are themselves built only from **literals plus `%s`** — no user input reaches the SQL text. Every user value travels in `where_params` / `tsquery` as a bound parameter. `tsquery` is itself produced by `_build_tsquery` (search.py:118-145), which round-trips the raw query through a **parameterized** `plainto_tsquery()` call, so it is a Postgres-sanitised tsquery string, never raw user text.

**Semantics, and why this is a sibling rather than an extension of `fts_count`.** `fts_count` is `len(fts_rows)` — capped at 50 by `CANDIDATE_LIMIT` and further shaped by `PER_DOC_CHUNK_CAP`, and its documented contract (search.py:76-82) is "only the zero case is exact". `brain gaps` keys off `fts_count == 0`. Redefining it to an uncapped total would silently break the gap detector's calibration and every `search_queries.fts_count` row already in the DB (migration 023). So `fts_count` is left **byte-identical in meaning**, and a new sibling field is added:

```python
@dataclass
class SearchDiagnostics:
    fts_count: int | None = None          # UNCHANGED semantics
    total_documents: int | None = None    # exact DISTINCT-document count; None unless total_count=True
    embed_ms: float | None = None         # None when fts_only
    embed_cached: bool | None = None
    sql_ms: float | None = None           # FTS leg + vector leg + count + doc-meta fetch
    total_ms: float | None = None
    facets_ms: float | None = None        # populated by the caller after compute_facets
```

Note the count is a **document** count, matching the CLI's document-granular result rows — not a chunk count. It counts documents with ≥1 *lexically* matching chunk; it deliberately does not include vector-only near-neighbours, because "how many documents actually mention this" is the number a user reading `544 matched` expects, and a vector-inclusive total would be `min(50, …)`-shaped noise. This is stated in the field docstring and in `--meta`'s help.

**Cost.** Measured above: 28-42 ms warm on a 13k-chunk corpus, 2.7 s cold (page-cache miss). Because that is real and unbounded in corpus size, the search layer keeps it **opt-in**: `hybrid_search(..., total_count: bool = False)`. The CLI passes `total_count=not no_meta`; MCP passes `total_count=True`; every other internal caller (`brain ask`, `brain review`, the eval harness, graph `--mode fuse`) is unchanged and pays nothing. `--no-meta` is therefore also the escape hatch for anyone who finds the count too slow.

#### 5.3 Latency phases

```python
    t_start = perf_counter()
    ...
    t_embed_start = perf_counter()
    q_emb = _query_embed(embedder, query)
    embed_ms = (perf_counter() - t_embed_start) * 1000.0
```

`embed_cached` is derived without touching `_cached_query_embed`'s internals by snapshotting `_cached_query_embed.cache_info().hits` before and after the call and comparing — `functools.lru_cache` exposes this publicly, so no production module is reopened.

`sql_ms` accumulates the FTS leg, vector leg, optional count, and the `doc_rows` metadata fetch. `total_ms` is `perf_counter() - t_start` measured at the single `return` (search.py:615). All three land in `diagnostics` only when a holder was passed — no behaviour change for the `diagnostics=None` default, and `perf_counter()` calls cost nanoseconds so they run unconditionally (no branch).

`facets_ms` is written by the **caller** after `compute_facets`, because faceting is a separate module invoked outside `hybrid_search` (SRP: the search module ranks; the facet module aggregates).

#### 5.4 Facets

```python
@dataclass(frozen=True)
class FacetBucket:
    value: str
    count: int


@dataclass(frozen=True)
class SearchFacets:
    source: tuple[FacetBucket, ...]
    content_type: tuple[FacetBucket, ...]
    tag: tuple[FacetBucket, ...]
    tag_truncated: int      # tags beyond top_tags, not shown
    total_documents: int


DEFAULT_TOP_TAGS = 8


def compute_facets(
    conn: psycopg.Connection,
    *,
    predicate: SearchPredicate,
    tsquery: str,
    top_tags: int = DEFAULT_TOP_TAGS,
) -> SearchFacets: ...
```

One round trip, one CTE, three grouped legs — the exact query benchmarked above at 126 ms:

```sql
WITH matched AS (
    SELECT DISTINCT c.document_id AS id
    FROM chunks c
    {join_clause}
    WHERE c.tsv @@ to_tsquery('english', %s){fts_filter}
)
SELECT 'source' AS facet, coalesce(s.kind, 'manual') AS value, count(*)::int AS n
FROM matched m
JOIN documents d ON d.id = m.id
LEFT JOIN sources s ON s.id = d.source_id
GROUP BY 2
UNION ALL
SELECT 'content_type', coalesce(d.content_type, 'unknown'), count(*)::int
FROM matched m JOIN documents d ON d.id = m.id
GROUP BY 2
UNION ALL
SELECT 'tag', t, count(*)::int
FROM matched m JOIN documents d ON d.id = m.id, unnest(d.tags) AS t
GROUP BY 2
ORDER BY 1, 3 DESC, 2
```

`coalesce(s.kind, 'manual')` mirrors the display fallback `search_table` already applies (`r.source_kind or "manual"`, format.py:57) so the facet labels and the table's Source column agree. Bind order is `[tsquery, *predicate.where_params]` — the CTE is the only place the predicate appears, so the params bind exactly once regardless of how many legs read `matched`. Tags are truncated in Python (`buckets[:top_tags]`, remainder counted) rather than in SQL, so the total-tag cardinality is known for the `(+153 more)` line.

`compute_facets` is called from the CLI/MCP only when `--facets` / `facets=True`, and `total_documents` for the facet path is taken from the diagnostics count already computed (facets imply `total_count=True`), not recomputed.

#### 5.5 CLI wiring (`brain search`)

```python
    show_meta = not no_meta
    want_total = show_meta or facets or (json_output and meta)
    ...
    results = hybrid_search(..., diagnostics=diagnostics, total_count=want_total)
    facet_data: SearchFacets | None = None
    if facets:
        t0 = perf_counter()
        facet_data = compute_facets(conn, predicate=..., tsquery=...)
        diagnostics.facets_ms = (perf_counter() - t0) * 1000.0
```

`hybrid_search` does not return its `SearchPredicate`. Rather than widen its return type, the CLI calls `build_predicate(...)` itself with the same kwargs and passes the resulting object to `compute_facets` — cheap (pure string assembly), and it keeps `hybrid_search`'s signature contract untouched. `_build_tsquery` is already public-enough within the package (`search._build_tsquery`); the facet path calls it through a thin re-export `brain.search.build_tsquery` (a new public alias; the private name stays for the existing tests that import it).

Output ordering, human path:
1. `console.print(search_table(results))` → **stdout** (unchanged)
2. `typer.echo(search_meta_line(...), err=True)` → **stderr**
3. if `--facets`: `Console(stderr=True).print(facets_renderable(facet_data))` → **stderr**

JSON path: `emit_json(list_payload)` when `--meta` is absent (unchanged code path, cli.py:4216-4229 kept verbatim), else `emit_json(search_envelope_json(...))`. The `--meta` branch builds the envelope by **calling the same list comprehension** so the `results` array cannot drift from the bare-list form (DRY: the comprehension moves into `format_search.search_results_json(results) -> list[dict[str, Any]]`, used by both branches and by MCP — replacing the three copies that exist today at cli.py:4218-4227, cli.py:4332-4340 and mcp_server.py:464-472).

#### 5.6 `brain note move` — signature change

**Decision: an optional `new_folder` parameter on `plan_rename`, not a new `plan_move` function.**

```python
def plan_rename(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    document_id: str,
    new_title: str,
    new_folder: str | None = None,   # NEW — None keeps the current folder
) -> RenameOp:
```

with the destination computation at rename.py:163-165 becoming:

```python
    new_slug = slugify(new_title)
    if new_folder is None:
        new_relative = old_relative.with_name(f"{new_slug}.md")
    else:
        folder = Path(new_folder.strip().strip("/")) if new_folder.strip() not in {"", "."} else Path()
        new_relative = folder / f"{new_slug}.md"
    new_abs = vault_path / new_relative
    assert_within_vault(new_abs, vault_path)   # raises VaultPathEscape
```

Why a parameter rather than a parallel function:

* `RenameOp` (rename.py:74-94) already carries `old_path` / `new_path` as **absolute paths**. A folder change is fully expressible in `new_path`. A `plan_move` would have to duplicate the doc-row lookup, the vault-tier check, the missing-file check, the collision check, and the whole `collect_references` call — five blocks, ~55 lines, verbatim. That is the copy-paste the DRY rule forbids.
* `apply_rename` needs **zero** changes to handle a folder change: it already branches on `op.new_path.resolve() != op.old_path.resolve()` (rename.py:274) and already does `op.new_path.parent.mkdir(parents=True, exist_ok=True)` (rename.py:275), which is exactly "create the destination folder".
* `RenameOp` gains **no new field**. "Was this a move or a rename?" is answered by `op.old_title != op.new_title` and `op.old_path.parent != op.new_path.parent`; the CLI branches on those for its headline line. Adding a field would break `frozen=True` construction sites in `scripts/collapse_gmail_threads.py` and cli.py:8118-8125.
* Default `None` is a strict superset of today's behaviour, so `brain note rename` and every existing test call `plan_rename` unchanged.

`brain note move <id> <folder>` then resolves the note's **current** title from the DB and calls `plan_rename(new_title=<current title>, new_folder=<folder>)`. A move is a rename to the same title in a different folder.

#### 5.7 What happens to each artifact

| Artifact | Behaviour |
|---|---|
| **The file on disk** | `Path.replace(new_path)` (`os.rename`) after the frontmatter rewrite is written **to the old path**. Byte-for-byte identical content, same inode, atomic within the filesystem. |
| **`documents.vault_path`** | Updated in place by a single parameterized `UPDATE documents SET vault_path=%s WHERE id=%s`, issued by `apply_rename` immediately after the on-disk rename, before `sync_one_file`. Mirrors the watcher's own idiom at watch.py:1027-1031. |
| **`documents.id`** | **Preserved.** This is the whole point — the id is never re-minted, so nothing cascades. |
| **Incoming links** (`links` rows whose target is this doc) | Untouched. They reference `documents.id`, which survives; `ON DELETE CASCADE` never fires because no `DELETE` is issued. |
| **Outgoing links** | Re-materialized by `sync_one_file` (rename.py:318-323), which the existing apply path already calls with `op.new_path`. |
| **`[[…]]` references in other notes** | Path-form references (`[[old/folder/slug\|Title]]`) are rewritten by `collect_references` + `apply_matches_to_text`. Because `new_title == old_title`, `_rewrite_link_text`'s synthetic-display drop (rename.py:483-489) collapses them to bare `[[Title]]`. Bare title-form references produce an identical `new_text` and are now **filtered out** (see below) so untouched files stay untouched. The next `link_rewrite` pass re-expands `[[Title]]` to `[[new/folder/slug\|Title]]`; in the interim the resolver's title rule (`LOWER(title) = LOWER(?)`, rename.py:357-359) resolves them, so no link is ever broken. |
| **Derived-links fence** | Preserved verbatim. The fence is a marker-delimited block inside the body (`FENCE_START_MARKER` / `FENCE_END_MARKER`, derived_links/fence.py:45-46); `_rewrite_source_frontmatter` re-dumps `dump_frontmatter(fields, body)` with the body untouched, and the rename is a file move, not a re-render. `sync_one_file`'s body hash excludes the fence (`strip_fence`, fence.py:102), so a pure move is a content no-op and the scoped derived-linker pass re-runs against the same doc id. |
| **Quartz / wiki** | The moved file appears at its new URL on the next build; the doc id is stable so backlink panels and the graph view keep their edges. |
| **`documents.source_path`** | Untouched — vault-tier notes dedup by `vault_path`, not `source_path` (migration 006). |

#### 5.8 The no-op-reference filter

Added at the tail of `collect_references` (rename.py:423-431):

```python
            if new_text == old_text:
                # A move keeps the title, so bare ``[[Title]]`` refs rewrite to
                # themselves. Emitting them would rewrite (and re-mtime, and
                # re-trigger the watcher on) every file in the vault that merely
                # mentions the note. Only real changes belong in the plan.
                continue
```

This also makes `brain note rename`'s `rewrote N reference(s)` count honest — today a self-referential no-op is counted. That is a behaviour change to an existing command, so it ships with a dedicated regression test (§8) rather than as an unremarked side effect.

#### 5.9 Collision handling

`plan_rename`'s existing guard (rename.py:170-174) already covers it — `new_abs.exists() and new_abs.resolve() != old_abs.resolve()`. Only the message becomes destination-aware:

```python
        raise RenameError(
            f"target path already exists: {new_relative.as_posix()} — rename this "
            f'note first (`brain note rename {document_id[:8]} "<new title>"`) '
            "or pick another folder"
        )
```

There is **no `--force`, no overwrite path, ever**. Two notes with the same slug in one folder is a user decision the tool must not make silently, and silently clobbering a note is unrecoverable (the snapshot dir is deleted on success, rename.py:314). Moving a note onto itself (`new_folder` equal to the current folder) is a no-op: `new_abs.resolve() == old_abs.resolve()`, the guard does not fire, `apply_rename` takes the in-place branch (rename.py:279-280), and the CLI prints `already in projects/atlas — nothing to do` and exits 0.

#### 5.10 Vault-escape protection

New in `brain/vault/paths.py` (the DRY extraction of cli.py:7726-7745):

```python
def assert_within_vault(target: Path, vault_root: Path) -> None:
    """Raise :class:`VaultPathEscape` if ``target`` resolves outside ``vault_root``.

    Both sides are ``.resolve()``d, so symlinks are followed on the target AND
    on the vault root — a vault symlinked into iCloud still validates, while a
    symlink pointing out of the vault is rejected.
    """
    try:
        target.resolve().relative_to(vault_root.resolve())
    except ValueError as e:
        raise VaultPathEscape(
            f"path must stay within the vault; {target} resolves outside {vault_root}"
        ) from e
```

```python
class VaultPathEscape(BrainError):
    """A user-supplied path resolves outside the vault root."""
```

`cli.py::_assert_within_vault` is rewritten to a 4-line wrapper that catches `VaultPathEscape` and re-raises `typer.BadParameter(f"{label} must stay within the vault; …")`, preserving its current message contract and its existing callers (`brain note new --folder`, `brain daily --date`). `brain note move` calls the wrapper for the friendly Typer message; `plan_rename` calls the pure function so **library** callers are guarded too — defence in depth, one implementation.

This rejects, for `--folder`: `../../etc`, `/etc`, `a/../../..`, `~/elsewhere` (via `expanduser` in `_resolve_vault`, cli.py:7716-7723), and any subpath that resolves through a symlink out of the vault.

#### 5.11 Watcher race — the actual fix

**The race, precisely.** `apply_rename` today writes the new file then `unlink()`s the old (rename.py:274-278). Watchdog therefore emits `created(new)` + `deleted(old)`, not a move. The watcher's delete branch re-stats `job.abs_path` (watch.py:744); the old path really is gone, so it falls through to `_handle_delete` → `DELETE FROM documents WHERE vault_path = <old_rel>` → `ON DELETE CASCADE` wipes every incoming `links` row. If that worker job wins the race against `apply_rename`'s own `sync_one_file` (which is what repoints `vault_path`), the note's backlinks are destroyed. This is the identical failure class the watcher's own docstring records as a shipped bug (watch.py:987-1003) and that the memory note *"HIGH: watcher rename destroyed incoming backlinks"* records from the 2026-07-12 overhaul.

**Fix, in three layers:**

1. **Emit a move event, not a delete.** `apply_rename` step 3 becomes: write the rewritten text **to `op.old_path`**, then `op.old_path.replace(op.new_path)`. Watchdog now emits `modified(old)` + `moved(old → new)`. `_classify_event` (watch.py:482) maps that to `action="move"`, routed to `_handle_move` (watch.py:977) — the branch that does an **in-place `UPDATE vault_path`** and never deletes. The destructive branch becomes unreachable for a brain-initiated move. `Path.replace` is `os.replace`: atomic on the same filesystem, and it preserves the inode so any editor holding the file open follows it.

2. **Close the residual window in the DB.** Immediately after the `replace`, and before `sync_one_file`, `apply_rename` issues `UPDATE documents SET vault_path = %s WHERE id = %s` with the new vault-relative POSIX path. From that microsecond on, `DELETE … WHERE vault_path = <old_rel>` matches zero rows even if a stale delete job somehow fires. The statement runs on the autocommit connection the CLI already opens (cli.py:8132-8133), so its lock footprint is a single row for a single statement — deliberately **not** wrapped in a longer transaction, per the recorded `relink-derived` ↔ watcher deadlock (memory: `feedback_relink_watcher_deadlock`).

3. **Converge, don't coordinate.** The watcher's follow-up `_handle_move` re-runs the same `UPDATE` (idempotent — it matches nothing, because step 2 already moved the row) and then `sync_one_file(dst)`, which body-hash short-circuits to `skipped` (sync.py:673-683, 753-771) because the content is unchanged. Result: at worst one redundant no-op sync. **No daemon needs to be stopped**; `brain note move` is safe to run with `brain vault sync --watch` and `brain-mcp` live. This is documented in the command docstring.

Cross-device fallback: `Path.replace` raises `OSError(EXDEV)` if the destination folder is on a different mount (a symlinked subfolder). `apply_rename` catches **`OSError`** specifically (no bare except) and falls back to the current write-new + unlink-old sequence, logging a warning that the watcher may briefly see a delete. The whole block is already inside the snapshot/restore `try` (rename.py:239-311), so a failure mid-fallback restores the vault.

#### 5.12 Error handling summary

| Condition | Raised | Surfaced as |
|---|---|---|
| id prefix too short / not found / ambiguous | `IdPrefixError` subclasses (errors.py:24-46) via the existing `_resolve_id` | existing CLI handler, exit 1 |
| doc is not vault-tier | `RenameError` (rename.py:146-149) | red stderr, exit 1 |
| doc has no `vault_path`, or the file is missing on disk | `RenameError` (rename.py:150-161) | red stderr, exit 1 |
| destination escapes the vault | `VaultPathEscape` → `typer.BadParameter` | Typer usage error, exit 2 |
| destination file exists | `RenameError` (§5.9) | red stderr, exit 1 |
| malformed frontmatter | `RenameError` (rename.py:546-548) | red stderr, exit 1 |
| any write failure mid-apply | original exception, after snapshot restore (rename.py:286-311) | traceback + logged snapshot dir |
| post-move sync errors | `report.sync_report.errors` | red stderr per file, exit 1 (mirrors cli.py:8159-8162) |
| count/facet query fails (`psycopg.Error`) | caught in the CLI, footer degrades to `? matched` + a stderr warning; **results still print** | exit 0 |

`RenameError` is re-parented: `class RenameError(BrainError)`. `BrainError` derives from `Exception`, so every existing `except RenameError` and `except Exception` site keeps working; this is a pure additive fix to a pre-existing rule violation.

---

### 6. Edge cases and failure modes

1. **Empty tsquery.** `_build_tsquery` returns `""` for punctuation-only input (rename of intent: search.py:127-129); `to_tsquery('')` matches nothing. The count query returns `0` and the facet CTE is empty. Footer prints `0 matched · 0 shown · …`; `--facets` prints `no facets (0 documents matched)`. No crash, no divide-by-zero.
2. **`fts_only` / `NullEmbedder`.** `hybrid_search` auto-degrades (search.py:324). `embed_ms` and `embed_cached` stay `None`; `search_meta_line` omits the embed segment entirely rather than printing `embed 0ms`, which would falsely imply a free embed. The existing stderr degradation hint (cli.py:4092-4098) still prints, above the footer.
3. **Warm LRU cache makes embed appear free.** `embed_cached=True` and the footer says `embed 0ms (cached)`. Without this the second MCP search in a session would look like a 40× speedup the user cannot reproduce from a fresh shell.
4. **Total count exceeds what the vector leg can surface.** `total_documents` is lexical-only by construction (§5.2). A pure-semantic hit ranked #1 by cosine with zero lexical overlap is *displayed* but **not** counted. Documented in `--meta` help and the field docstring as: "documents that lexically match; the vector leg may surface additional near-neighbours". Deliberate — the alternative (a capped, RRF-shaped number) is less honest, not more.
5. **Cold count on a large corpus.** Measured 2.7 s cold vs 35 ms warm. Mitigations: opt-in at the library layer, `--no-meta` at the CLI, and the count runs *after* the ranked results are in hand so a Ctrl-C during it still leaves the table printed.
6. **A document is deleted between the ranking query and the count.** The count can exceed the number of rows the metadata fetch resolves. `hybrid_search` already skips orphaned docs (search.py:519-522); the footer's `N shown` is `len(results)`, so it stays truthful and the two numbers simply disagree by one. No exception.
7. **Facets on a huge tag vocabulary.** Live corpus already returns 161 facet rows for one query. Truncated to the top 8 tags plus a `(+N more)` line; `--json` reports `tag_truncated`. Sources and content types are not truncated (bounded cardinality: 4 sources, ~8 content types).
8. **`brain note move` onto the note's current folder.** No-op, exit 0, `already in <folder> — nothing to do`. Verified by the same-path branch at rename.py:274/279.
9. **`brain note move` on an ingested-tier doc** (`_ingested/…`, `kind='ingested'`). Rejected by the existing tier guard (rename.py:146-149) with `document is 'ingested', not 'vault' — only vault-tier docs can be renamed`. Correct: `_ingested/` layout is machine-owned (`_ingested/<source>/YYYY-MM-DD-<id8>-<slug>.md`) and moving a mirror would desync it from its ingest source.
10. **Non-TTY invocation without `--yes`.** `typer.confirm` raises `Abort` on EOF → exit 1 with `Aborted.`, nothing written. The `--yes` help text and the command docstring both state that scripts must pass it.
11. **The vault watcher processes the move's `modified(old)` event after the file has already been renamed away.** The worker's stale-upsert guard (watch.py:775-795) sees `not job.abs_path.exists()` and `continue`s without counting an error — the exact guard that exists for this shape of race.
12. **Cross-filesystem destination folder.** `OSError(EXDEV)` → logged warning → write+unlink fallback (§5.11). Behaviour degrades to today's, never worse.

---

### 7. Security and safety

| Risk | Guard |
|---|---|
| SQL injection via the query string into the count/facet SQL | The query never reaches SQL text. It goes through `_build_tsquery`, which itself binds it as a `%s` parameter to `plainto_tsquery` (search.py:132-141); the resulting tsquery is bound as `%s` again. Facet/count SQL contains only literals and `%s`. |
| SQL injection via filter values (`--tag`, `--kind`, `--thread`, `--person`) | Untouched: all travel in `SearchPredicate.where_params` as bound tuples. The refactor **moves** the existing code; it does not rewrite a single predicate string. |
| f-string interpolation into SQL | Only `join_clause` and `fts_filter`, both drawn from `SearchPredicate`, both assembled exclusively from module-level literals plus `%s` placeholders. A unit test asserts no `SearchPredicate` field ever contains a value from the caller's inputs. |
| Path traversal via `<NEW-FOLDER>` | `assert_within_vault` (§5.10), applied in **both** the CLI and `plan_rename`, resolving symlinks on both sides. |
| Silent data loss by overwriting a note at the destination | Hard `RenameError`, no `--force`, no overwrite path (§5.9). |
| Partial multi-file writes leaving a broken vault | Existing snapshot → write → restore-on-exception contract (rename.py:208-215, 286-311), unchanged and now covering the `Path.replace` too. |
| Backlink destruction by a racing watcher `DELETE` | `Path.replace` steers the watcher into `_handle_move`; the in-place `vault_path` UPDATE closes the residual window (§5.11). |
| Long-held locks reintroducing the `relink-derived` ↔ watcher deadlock | The new `UPDATE` is a single autocommit statement on a row already being written; no new transaction, no `graph_entities` access. |
| Sensitive content leaking into logs | The footer prints counts and milliseconds only. Facet values are tags / source kinds / content types — never titles, never snippets, never bodies. Nothing is logged at INFO. |
| Destructive DB operations | None. Feature 1 issues `SELECT` only. Feature 2 issues one `UPDATE … WHERE id=%s`. No `DELETE`, no `DROP`, no `TRUNCATE`. No test in this section may touch port 55432. |

---

### 8. Test plan

All tests use the real Postgres test DB (port 5434, `second_brain_test`) via the `test_db` fixture (conftest.py:370) and the `fake_embedder` fixture. All fixture data is synthetic (invented titles, `*.example.com` addresses). No production module is reopened; `unittest.mock.patch` is used only on `brain.cli._build_embedder` (the established patch point, cli.py:7876-7878) and on `subprocess`/`$EDITOR` boundaries.

#### Red-first failing tests (these prove the gaps)

| File | Test | Fails today with |
|---|---|---|
| `tests/test_search_totals.py` | `test_total_documents_exceeds_returned_page` — seed 7 docs all containing `"quarterly"`, search with `limit=2`, assert `len(results) == 2` **and** `diag.total_documents == 7` | `AttributeError: 'SearchDiagnostics' object has no attribute 'total_documents'` |
| `tests/test_cli_note_move.py` | `test_move_relocates_file_and_preserves_document_id` | `SystemExit(2)`, `No such command 'move'` |
| `tests/test_rename_atomicity.py` | `test_apply_rename_preserves_inode_so_watcher_sees_a_move` — capture `old.stat().st_ino`, apply, assert `new.stat().st_ino` equals it | fails: current write+unlink produces a **new** inode, which is exactly what makes watchdog emit `deleted` instead of `moved` |

#### `tests/test_search_predicate.py` (new, pure logic — 95% target)

- `test_no_filters_yields_true_predicate_and_prepare_flag` — `where_sql == "TRUE"`, `has_filters is False`, `join_clause == ""`, `prepare_flag is True`.
- `test_each_filter_appends_one_clause_and_one_param` — parameterized over all 10 filters; `where_sql.count("%s") == len(where_params)`.
- `test_predicate_fields_never_contain_caller_values` — the injection guard: build with `tag="'; DROP TABLE documents; --"` and assert the literal string appears in `where_params` and **nowhere** in `where_sql` / `fts_filter` / `join_clause`.
- `test_predicate_is_frozen_and_params_are_a_tuple` — immutability contract.
- `test_naive_after_is_stamped_utc` — `_ensure_utc` moved intact (mirrors the existing assertion in `tests/test_search_metadata_filters.py`).

#### `tests/test_search_totals.py` (new)

- the red-first test above;
- `test_total_respects_metadata_filters` — 7 docs, 3 tagged `alpha`; `tag="alpha"` → `total_documents == 3`;
- `test_total_is_none_when_not_requested` — default `total_count=False` leaves it `None`;
- `test_fts_count_semantics_unchanged` — the existing `tests/test_search_diagnostics.py` assertions still hold with `total_count=True` (`fts_count` remains `len(fts_rows)`, ≤ 50, and `0` on an off-corpus query even when `total_documents` is also `0`);
- `test_total_counts_documents_not_chunks` — one doc chunked into 4 matching chunks → `total_documents == 1`;
- `test_timing_fields_populated` — `embed_ms`, `sql_ms`, `total_ms` are non-`None` floats and `total_ms >= sql_ms`;
- `test_embed_ms_none_under_fts_only` and `test_embed_cached_true_on_second_identical_query`.

#### `tests/test_search_facets.py` (new)

- `test_facets_group_by_source_content_type_and_tag` — seed across `manual`/`gmail`/`krisp` with overlapping tags; assert exact bucket counts;
- `test_facet_counts_are_documents_not_chunks`;
- `test_facets_respect_filters` — with `source_kind="gmail"` only gmail buckets appear;
- `test_tag_truncation_reports_remainder` — 12 distinct tags, `top_tags=8` → 8 buckets, `tag_truncated == 4`;
- `test_null_source_falls_back_to_manual_label` — a doc with `source_id IS NULL` lands in the `manual` bucket, matching `search_table`'s display fallback;
- `test_empty_match_set_yields_empty_facets` — no exception, all tuples empty, `total_documents == 0`.

#### `tests/test_cli_search_meta.py` (new — the backward-compat firewall)

- **`test_default_json_is_still_a_bare_list_of_seven_key_objects`** — `json.loads(stdout)` is a `list`; `set(payload[0]) == {"id","title","source_kind","snippet","score","content_type","tags"}`. This test is the contract; it must never be edited.
- `test_footer_goes_to_stderr_not_stdout` — CliRunner with `mix_stderr=False`; `"matched" not in stdout`, `"matched" in stderr`.
- `test_json_stdout_is_parseable_with_footer_enabled` — the piping guarantee, asserted by parsing stdout while stderr is non-empty.
- `test_no_meta_suppresses_the_footer`.
- `test_json_meta_envelope_shape` — exact key set of the envelope; `payload["results"]` deep-equals the bare-list output of the same query without `--meta`.
- `test_facets_implies_meta_under_json`.
- `test_zero_results_still_prints_footer`.
- `test_footer_omits_embed_segment_under_fts_only`.
- `test_count_query_failure_degrades_gracefully` — `mocker.patch` `brain.search._count_matching_documents` to raise `psycopg.OperationalError`; assert exit 0, table present on stdout, `? matched` + a warning on stderr.

#### `tests/test_mcp_search_meta.py` (new)

- `test_brain_search_keeps_session_id_and_results_keys`;
- `test_brain_search_result_objects_unchanged` — the 7-key set;
- `test_brain_search_gains_total_and_timing_keys`;
- `test_brain_search_facets_default_none_and_populated_when_requested`.

#### `tests/test_rename_move_plan.py` (new — pure-ish logic, 95% target)

- `test_new_folder_none_keeps_current_folder` — regression: today's rename behaviour is byte-identical;
- `test_new_folder_sets_destination_relative_to_vault_root`;
- `test_empty_or_dot_folder_targets_the_vault_root`;
- `test_leading_and_trailing_slashes_are_normalized`;
- `test_traversal_folder_raises_vault_path_escape` — parameterized over `"../.."`, `"/etc"`, `"a/../../.."`;
- `test_symlink_out_of_vault_raises_vault_path_escape` — create a real symlink under `tmp_path`;
- `test_collision_raises_rename_error_naming_the_path`;
- `test_move_to_same_folder_is_a_noop_plan` — `op.new_path.resolve() == op.old_path.resolve()`;
- `test_path_form_references_are_collected_for_a_move`;
- **`test_identical_rewrites_are_filtered_out`** — the no-op filter: a file containing only `[[Same Title]]` yields zero `ReferenceMatch` entries for a same-title move;
- `test_rename_reference_count_excludes_noops` — the behaviour-change regression test for `brain note rename`.

#### `tests/test_rename_atomicity.py` (new)

- the inode red-first test above;
- `test_cross_device_oserror_falls_back_to_write_and_unlink` — `mocker.patch("pathlib.Path.replace", side_effect=OSError(18, "Invalid cross-device link"))`; assert the file still lands at the destination and the old path is gone;
- `test_vault_path_updated_before_sync` — `mocker.patch` `brain.vault.rename.sync_one_file` with a side effect that reads `documents.vault_path`; assert it already holds the **new** path when sync runs (this is the watcher-race guarantee);
- `test_failure_mid_apply_restores_every_snapshotted_file` — extends the existing restore coverage to the move path.

#### `tests/test_cli_note_move.py` (new)

- the red-first relocation test (asserts new file exists, old gone, `documents.id` unchanged, `documents.vault_path` == new relative POSIX path);
- `test_incoming_links_survive_the_move` — seed note B linking to note A, move A, assert A's incoming `links` row still exists (the backlink-destruction regression);
- `test_dry_run_writes_nothing` — file still at the old path, `vault_path` unchanged, every planned line present in stdout;
- `test_dry_run_lists_every_file_that_would_be_rewritten` — exact `path:line  old → new` lines;
- `test_confirmation_required_without_yes` — `input="n\n"` → exit 1, nothing moved;
- `test_yes_skips_confirmation`;
- `test_collision_exits_1_with_actionable_message`;
- `test_traversal_folder_exits_2`;
- `test_ingested_tier_doc_is_rejected`;
- `test_move_creates_missing_destination_folder`;
- `test_no_link_refactor_moves_file_but_leaves_references`;
- `test_derived_fence_survives_the_move` — seed a note whose body contains a `BRAIN_DERIVED_START/END` block; assert byte-identical fence content after the move.

Coverage: `search_predicate.py` and `facets.py` are pure logic → 95%. `rename.py` changes → 95% (it is pure logic over a filesystem). CLI additions → 85%. `format_search.py` → 95%.

---

### 9. Open questions — with the decision taken

1. **Should `--json` become an object?** — **No.** Bare list forever; new fields behind `--meta`; MCP object grows keys. Defended in §3.3. Revisit only at a 1.0 with a documented breaking-change window.
2. **Extend `fts_count` or add a sibling?** — **Sibling (`total_documents`).** `fts_count`'s capped semantics are load-bearing for `brain gaps` and for the historical rows already in `search_queries` (migration 023). Redefining it would silently recalibrate the gap detector.
3. **Should the total count be lexical-only or lexical ∪ vector?** — **Lexical-only.** A vector-inclusive total would be capped at `CANDIDATE_LIMIT` and therefore meaningless as a "total". The `--meta` help and field docstring state the semantics explicitly.
4. **Is the total on by default given the 2.7 s cold cost?** — **On by default for human output, off by default at the library layer.** The interactive user is already waiting ~6 s for the embed; +35 ms warm is invisible and the cold case is a page-cache artifact that warms after the first query. `--no-meta` exists for anyone who disagrees. Every non-CLI caller pays nothing.
5. **Footer on stdout or stderr?** — **stderr**, following `_warn_if_fts_only_degraded` (cli.py:4090). It keeps `--json` pipeable and `> file` clean, at the cost of the footer vanishing under `2>/dev/null`. Correct trade for a tool whose primary consumer is a script.
6. **Should facets be a separate `brain search --facets` or a new `brain facets` command?** — **A flag on `search`.** Facets are always *of* a query; a standalone command would duplicate all 12 filter flags.
7. **How many tags in the facet panel?** — **8**, with `(+N more)`. Constant `DEFAULT_TOP_TAGS` in `facets.py`, no env knob (YAGNI). `--json` always reports the truncated remainder so a consumer can ask for more later.
8. **Add a `BRAIN_SEARCH_META` env knob?** — **No.** `--no-meta` covers it; a `Config` field means touching `config.py`, `doctor`, `.env.example`, and the setup profiles for a preference one flag already expresses.
9. **`plan_move` function or `new_folder` parameter?** — **Parameter.** §5.6: a separate function duplicates ~55 lines of guards and forces a second `RenameOp` construction site.
10. **Should `RenameOp` gain a `moved: bool` field?** — **No.** Derivable from `old_path.parent != new_path.parent`; adding a field to a frozen dataclass breaks the three existing construction sites (cli.py:8118, `scripts/collapse_gmail_threads.py`, `plan_rename`).
11. **Confirmation prompt on `brain note move` when `brain note rename` has none?** — **Yes, prompt (skippable with `--yes`).** A move rewrites path-form links across the *whole* vault, and unlike rename the user cannot see the blast radius from the command line. `--dry-run` also skips the prompt (nothing to confirm).
12. **Stop the watcher during a move?** — **No.** The `Path.replace` + immediate `vault_path` UPDATE makes the operation watcher-safe by construction (§5.11). Requiring a daemon stop would be a footgun and would contradict the "converge, don't coordinate" design the watcher already follows.
13. **Should `brain note move` accept multiple ids (`brain note move a b c projects/`)?** — **No, single-note for v1.** Multi-move needs its own confirmation UX, partial-failure semantics, and a batched snapshot; ship the single case, add `--from-folder` batch mode only if asked for.
14. **Re-parent `RenameError` to `BrainError` inside this section, or defer?** — **Do it here.** It is a one-line, strictly-additive fix to a documented rule violation in the exact file this section already edits (Scout Law), and `BrainError` derives from `Exception` so no `except` clause anywhere changes behaviour.
15. **Add an MCP `brain_note_move` tool?** — **No.** Vault mutation over MCP is a separate trust decision; `brain note rename` has no MCP tool either. Out of scope for this section.
16. **Does `search.py` stay under 800 lines?** — **Yes, ~714**, because `build_predicate` (~45 lines) moves out as the timing/count code (~55 lines) moves in. `format.py` is left untouched at 783 by putting the new renderers in `format_search.py`. `cli.py` (9760) and `mcp_server.py` (3405) are pre-existing over-limit files with a separately-tracked split; this section adds 125 and 18 lines respectively and does not attempt that refactor.
