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
# read the CTE. ``coalesce(s.kind, 'manual')`` mirrors the display fallback
# ``search_table`` already applies (``r.source_kind or "manual"``), so a facet
# label always agrees with the table's Source column.
#
# ``{join_clause}`` / ``{fts_filter}`` are the only f-string slots and are
# drawn from :class:`SearchPredicate`, whose fields are built exclusively from
# literals plus ``%s``. Every user value travels as a bound parameter.
_FACET_SQL = """
    WITH matched AS (
        SELECT DISTINCT c.document_id AS id
        FROM chunks c
        {join_clause}
        WHERE c.tsv @@ %s::tsquery{fts_filter}
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
        join_clause=predicate.join_clause, fts_filter=predicate.fts_filter
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
        # JOIN is on a primary key, and a NULL source_id still yields one
        # 'manual' row), so summing that leg is an exact document count that
        # costs no extra round trip. The tag leg cannot be used — a multi-tag
        # document is counted once per tag.
        total_documents=sum(b.count for b in grouped["source"]),
    )
