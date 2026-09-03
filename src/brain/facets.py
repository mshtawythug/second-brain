"""Measure and group a whole search match set (total count + three facets)."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .search_predicate import SearchPredicate

#: Tag buckets shown before truncation. A live personal corpus already returns
#: ~160 distinct tags for a broad query, so the panel would otherwise bury the
#: signal. Source and content-type facets are NOT truncated — their cardinality
#: is bounded (4 sources, ~8 content types).
DEFAULT_TOP_TAGS = 8

#: The ``source`` bucket a document with **no** ``sources`` row lands in.
#:
#: THIS USED TO BE ``'manual'``, AND THAT WAS A WRONG ANSWER THAT LOOKED RIGHT.
#: ``coalesce(s.kind, 'manual')`` filed every source-less document under a real
#: source kind, so ``manual``'s count was inflated by all of them and a reader
#: filtering to ``manual`` got documents that have no source at all. Not an
#: omission a user could notice — the number was plausible and the rows looked
#: like rows.
#:
#: ``'none'`` rather than a dropped row, so the bucket is *clickable*: it is the
#: exact value :data:`brain.ui.schemas.SOURCE_NONE` carries, which
#: ``parse_search_spec`` turns into ``build_predicate(source_missing=True)``.
#: Clicking the facet therefore selects the documents it counted — with one
#: known exception, stated rather than claimed away. Omitting the row instead
#: would have fixed ``manual`` while making source-less documents invisible in
#: the panel, which is the same information loss in a quieter form.
#:
#: **THE EXCEPTION: a source whose kind is literally ``'none'``.** Such a
#: document has a ``source_id``, so ``coalesce(s.kind, 'none')`` COUNTS it in
#: this bucket while the click — ``source_missing=True`` →
#: ``d.source_id IS NULL`` — does NOT return it. Count and click disagree by
#: exactly those rows.
#:
#: **No supported path can create one any more, and this paragraph is kept for
#: what remains rather than for what was fixed.** The prescription it used to
#: carry — close ``--source`` over the known kind set — was right but named one
#: boundary of three. All three are now closed against
#: :data:`brain.source_kinds.VALID_SOURCE_KINDS`:
#: ``cli_ingest.ingest_stdin`` and ``mcp_server.brain_ingest_stdin`` raise
#: (``typer.BadParameter`` / ``INVALID_PARAMS``), and ``vault.sync`` — reached
#: from hand-authored frontmatter rather than an argument — warns and indexes
#: the document with no ``sources`` row, so a metadata typo costs the user a
#: facet, not the document.
#:
#: What is NOT closed, and why this stays: the column is still bare
#: ``TEXT NOT NULL`` with no CHECK (migration 001), so the guarantee is an
#: application-layer one. Rows written before those boundaries closed, and
#: anything written by direct SQL, can still land here. Re-derive the boundary
#: set before trusting this note — ``grep -rn 'INSERT INTO sources' src/`` finds
#: the two sinks, and ``grep -rn validate_source_kind src/`` finds the guards. A
#: fourth writer that skips them reopens the divergence.
#:
#: Still not fixed on the read side, for the original reason: papering over it
#: in the facet SQL would hide a write-boundary problem behind a read-side
#: special case, and no sentinel string is safe while any string is an
#: accepted kind.
#:
#: Defined HERE and mirrored by ``brain.ui.schemas.SOURCE_NONE`` rather than
#: imported from it: ``brain.facets`` is core and backs ``brain search`` on the
#: CLI, so importing a UI module would invert the dependency for one string.
#: ``tests/test_search_facets.py`` pins the two to the same value, so the
#: mirror cannot drift silently.
SOURCE_NONE_BUCKET = "none"


@dataclass(frozen=True)
class FacetBucket:
    """One ``value → document count`` pair within a facet."""

    value: str
    count: int


@dataclass(frozen=True)
class SearchFacets:
    """The full match set, grouped three ways.

    Counts are DOCUMENT counts, not chunk counts — matching the granularity of
    the rows ``brain search`` prints. A document with three tags contributes to
    three ``tag`` buckets but to exactly one ``source`` and one
    ``content_type`` bucket, which is why :attr:`total_documents` is derived
    from the source leg.
    """

    source: tuple[FacetBucket, ...]
    content_type: tuple[FacetBucket, ...]
    tag: tuple[FacetBucket, ...]
    tag_truncated: int  # distinct tags beyond ``top_tags``, not shown
    total_documents: int


# ``%s::tsquery`` — NOT ``to_tsquery('english', %s)``. ``build_tsquery`` returns
# LEXEMES that have ALREADY been through ``plainto_tsquery``; re-parsing them
# with the english config stems them a SECOND time and the result no longer
# matches the stored ``tsv``. Measured on the live 1,376-doc corpus: the query
# ``provisioning`` matched 94 documents in the ranked leg and 1 here.
#
# This module exists to stop the footer's total and the facet panel from
# drifting from the results they annotate — so it is exactly the module where a
# different tsquery binding is least acceptable. The ranked legs in
# ``search.py`` bind ``%s::tsquery``; these must match them character for
# character or the number printed beside the results describes a different
# match set.
_TOTAL_COUNT_SQL = """
    SELECT count(DISTINCT c.document_id)
    FROM chunks c
    {join_clause}
    WHERE c.tsv @@ %s::tsquery{fts_filter}
"""


def count_matching_documents(
    conn: psycopg.Connection,
    *,
    predicate: SearchPredicate,
    tsquery: str,
) -> int:
    """Exact count of DISTINCT documents whose chunks match the lexical query.

    Lives beside :func:`compute_facets` because both answer "how big is this
    match set" off the identical predicate — keeping them in one module is
    what stops the footer's total and the facet panel's total from drifting.

    ``{join_clause}`` / ``{fts_filter}`` are the only f-string slots and are
    :class:`SearchPredicate` fields built from literals plus ``%s``;
    ``tsquery`` comes from ``brain.search.build_tsquery``, which round-trips
    the raw query through a PARAMETERIZED ``plainto_tsquery()`` and is bound
    here as ``%s::tsquery`` (see the note above the SQL). No user text reaches
    SQL text on any path.
    """
    sql = _TOTAL_COUNT_SQL.format(
        join_clause=predicate.join_clause, fts_filter=predicate.fts_filter
    )
    row = conn.execute(
        sql, [tsquery, *predicate.where_params], prepare=predicate.prepare_flag
    ).fetchone()
    return int(row[0]) if row else 0


# One round trip, one CTE, three grouped legs. The predicate appears exactly
# once (inside ``matched``), so its params bind once no matter how many legs
# read the CTE.
#
# ``coalesce(s.kind, SOURCE_NONE_BUCKET)`` — see that constant. It previously
# coalesced to ``'manual'`` to mirror the display fallback ``search_table``
# applies (``r.source_kind or "manual"``). Agreeing with the table was the
# stated reason and it was the wrong thing to optimise for: the table's
# fallback is a LABEL for one row, this is a FILTER VALUE aggregating many, and
# making them agree meant making the aggregate lie. They now disagree on
# purpose, and the disagreement is visible rather than the miscount.
#
# THREE ``.format()`` slots, no user text in any of them. ``{join_clause}`` /
# ``{fts_filter}`` are drawn from :class:`SearchPredicate`, whose fields are
# built exclusively from literals plus ``%s``; ``{source_none}`` is the module
# constant :data:`SOURCE_NONE_BUCKET`. Every user value travels as a bound
# parameter.
_FACET_SQL = """
    WITH matched AS (
        SELECT DISTINCT c.document_id AS id
        FROM chunks c
        {join_clause}
        WHERE c.tsv @@ %s::tsquery{fts_filter}
    )
    SELECT 'source' AS facet, coalesce(s.kind, '{source_none}') AS value,
           count(*)::int AS n
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
"""


def compute_facets(
    conn: psycopg.Connection,
    *,
    predicate: SearchPredicate,
    tsquery: str,
    top_tags: int = DEFAULT_TOP_TAGS,
) -> SearchFacets:
    """Group the documents matching ``tsquery`` + ``predicate`` three ways.

    Pure read — no embedder dependency, no writes. Takes the SAME
    :class:`SearchPredicate` instance the ranked legs used, so the buckets can
    never describe a different match set than the results they annotate.

    Tags are truncated in Python rather than in SQL so the remainder count
    (the ``(+N more)`` line) is exact. An empty match set yields empty tuples
    and ``total_documents == 0`` — never an exception, never a division.
    """
    sql = _FACET_SQL.format(
        join_clause=predicate.join_clause,
        fts_filter=predicate.fts_filter,
        # A module constant, not user input — the same category as the
        # ``'source'``/``'tag'`` facet labels already inlined in this statement.
        source_none=SOURCE_NONE_BUCKET,
    )
    rows = conn.execute(
        sql, [tsquery, *predicate.where_params], prepare=predicate.prepare_flag
    ).fetchall()

    grouped: dict[str, list[FacetBucket]] = {
        "source": [],
        "content_type": [],
        "tag": [],
    }
    for facet, value, count in rows:
        grouped[str(facet)].append(FacetBucket(value=str(value), count=int(count)))

    all_tags = grouped["tag"]
    shown_tags = all_tags[:top_tags] if top_tags >= 0 else all_tags
    return SearchFacets(
        source=tuple(grouped["source"]),
        content_type=tuple(grouped["content_type"]),
        tag=tuple(shown_tags),
        tag_truncated=len(all_tags) - len(shown_tags),
        # Every matched document contributes EXACTLY one source row (the LEFT
        # JOIN is on a primary key, and a NULL source_id still yields one row —
        # now the ``none`` bucket rather than ``manual``), so summing that leg
        # is an exact document count that costs no extra round trip. Splitting
        # ``none`` out of ``manual`` moved counts BETWEEN buckets and changed
        # no total. The tag leg cannot be used — a multi-tag document is
        # counted once per tag.
        total_documents=sum(b.count for b in grouped["source"]),
    )
