"""The reads `brain ui` needs that :mod:`brain.queries` does not already provide.

Every statement here is parameterized. **This is the only module in the package
allowed to contain SQL** — a route that grows its own query is a review
rejection, because that is exactly how a second, un-eval-gated implementation of
the brain starts (spec §9.3).
"""
from __future__ import annotations

from typing import Any

import psycopg

#: One indexed pass over the exported corpus. ``documents_vault_path_idx`` is a
#: partial unique index on precisely ``vault_path WHERE vault_path IS NOT NULL``
#: (migration 003), so this is an index-ordered scan rather than a sort.
_TREE_SQL = """
    SELECT id::text, title, vault_path, kind, draft,
           coalesce(sent_at, ingested_at)
    FROM documents
    WHERE vault_path IS NOT NULL
    ORDER BY vault_path
"""

#: Wiki-link targets are matched on title OR on the extension-stripped vault
#: path, mirroring how ``brain.vault.resolver`` resolves them, so the inspector
#: linkifies exactly what a real vault sync would.
_RESOLVE_LINKS_SQL = """
    SELECT id::text, title, vault_path
    FROM documents
    WHERE lower(title) = ANY(%s)
       OR lower(regexp_replace(coalesce(vault_path, ''), '\\.md$', '')) = ANY(%s)
"""

_DISTINCT_CONTENT_TYPES = (
    "SELECT content_type, count(*) FROM documents "
    "WHERE content_type IS NOT NULL GROUP BY 1 ORDER BY 1"
)

_DISTINCT_SOURCE_KINDS = (
    "SELECT s.kind, count(d.id) FROM sources s "
    "LEFT JOIN documents d ON d.source_id = s.id "
    "GROUP BY 1 ORDER BY 1"
)


#: ``DocumentRow`` (``brain.queries``) deliberately carries the *show* fields
#: and not the vault placement ones, so the three columns the inspector needs to
#: decide "which tier is this, and can it be edited" come from here.
_NOTE_META_SQL = """
    SELECT vault_path, kind, draft
    FROM documents
    WHERE id = %s
"""


def iter_tree_rows(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    """Every exported document, ordered by ``vault_path``, for the left rail."""
    return list(conn.execute(_TREE_SQL).fetchall())


#: Which of a set of documents are confidential. Used to redact search snippets
#: — snippet text comes straight out of ``chunks``, so it is body text by
#: another name and is the least obvious place a withheld body leaks.
_CONFIDENTIAL_IDS_SQL = """
    SELECT id::text
    FROM documents
    WHERE id = ANY(%s) AND sensitivity = %s
"""


def confidential_document_ids(
    conn: psycopg.Connection[Any], document_ids: list[str]
) -> set[str]:
    """Return the subset of ``document_ids`` marked confidential.

    One round trip for a whole result page rather than one per row. An empty
    input short-circuits without touching the database.
    """
    if not document_ids:
        return set()
    from ..sensitivity import CONFIDENTIAL

    rows = conn.execute(
        _CONFIDENTIAL_IDS_SQL, (list(document_ids), CONFIDENTIAL)
    ).fetchall()
    return {str(row[0]) for row in rows}


def note_meta(
    conn: psycopg.Connection[Any], document_id: str
) -> tuple[str | None, str, bool] | None:
    """``(vault_path, kind, draft)`` for one document, or ``None`` if it is gone.

    ``kind`` is the *tier* — ``'vault'`` or ``'ingested'`` (migration 003) — and
    is emphatically not ``content_type``; ``hybrid_search``'s own docstring
    warns about exactly that confusion.
    """
    row = conn.execute(_NOTE_META_SQL, (document_id,)).fetchone()
    if row is None:
        return None
    return (row[0], str(row[1] or "vault"), bool(row[2]))


def resolve_link_targets(
    conn: psycopg.Connection[Any], targets: list[str]
) -> dict[str, str]:
    """Map wiki-link targets to document ids, case-insensitively.

    One round trip for every link in a note rather than one per link. Returns a
    lowercase-keyed dict; unmatched targets are simply absent, which
    :func:`brain.ui.render.render_markdown` renders as an unresolved link.
    """
    if not targets:
        return {}
    lowered = [t.strip().lower() for t in targets if t.strip()]
    if not lowered:
        return {}
    rows = conn.execute(_RESOLVE_LINKS_SQL, (lowered, lowered)).fetchall()

    resolved: dict[str, str] = {}
    for doc_id, title, vault_path in rows:
        if title:
            resolved.setdefault(str(title).lower(), str(doc_id))
        if vault_path:
            stem = str(vault_path)
            if stem.endswith(".md"):
                stem = stem[:-3]
            resolved.setdefault(stem.lower(), str(doc_id))
    return resolved


def content_type_buckets(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """``content_type`` → document count, for the Type dropdown.

    Used only as the corpus-wide fallback that populates the dropdown before a
    query runs; once a search has run, F5's ``compute_facets`` supplies
    match-scoped counts and this is not consulted.
    """
    return [
        {"value": str(value), "count": int(count)}
        for value, count in conn.execute(_DISTINCT_CONTENT_TYPES).fetchall()
    ]


def source_kind_buckets(
    conn: psycopg.Connection[Any], *, known: frozenset[str]
) -> list[dict[str, Any]]:
    """``sources.kind`` → document count, unioned with the known kinds.

    The union matters: a brain with no Slack rows yet should still offer
    ``slack`` in the dropdown at count 0, rather than the option appearing only
    after the first ingest.
    """
    counts = {
        str(kind): int(count)
        for kind, count in conn.execute(_DISTINCT_SOURCE_KINDS).fetchall()
        if kind
    }
    for kind in known:
        counts.setdefault(kind, 0)
    return [
        {"value": kind, "count": counts[kind]} for kind in sorted(counts)
    ]
