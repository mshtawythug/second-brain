"""Which of a set of documents are confidential: one statement, shared by every caller (F6).

Three paths need the same answer at their boundary: ``brain ui`` blanks a
confidential row's snippet (:mod:`brain.ui.routes_search`), the hosted reranker
withholds its passage (:mod:`brain.rerank`), and the document-vector backfill
re-checks a batch just before a hosted embed call (:mod:`brain.doc_embeddings`).
One definition, so the three cannot drift.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from .sensitivity import CONFIDENTIAL

_CONFIDENTIAL_IDS_SQL = """
    SELECT id::text
    FROM documents
    WHERE id = ANY(%s::uuid[]) AND sensitivity = %s
"""


def confidential_document_ids(
    conn: psycopg.Connection[Any], document_ids: Sequence[str]
) -> set[str]:
    """Return the subset of ``document_ids`` marked confidential.

    One round trip for a whole page rather than one per row. An empty input
    short-circuits without touching the database.
    """
    if not document_ids:
        return set()
    rows = conn.execute(_CONFIDENTIAL_IDS_SQL, (list(document_ids), CONFIDENTIAL)).fetchall()
    return {str(row[0]) for row in rows}
