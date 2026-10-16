"""Related-docs FTS leg: the F6 confidential gate applies inside the leg itself.

``compute_related`` cannot show this gate. An FTS-only candidate scores at most
``1 / (RRF_K + 1)`` (about 0.016), below ``_MIN_RRF_SCORE`` (0.020), so deleting
the FTS leg's gate left every related test green: the vector leg's gate hid it.
The leg is still a body boundary in its own right, because its rows carry chunk
``content``, which becomes a snippet in ``related.json``. So the gate is pinned
here, on a confidential document that ONLY the FTS leg can reach: its chunk has
no embedding at all.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.related_candidates import fts_candidates
from brain.sensitivity import CONFIDENTIAL

_TOKEN = "zzfftleaf"


def _insert_doc(conn: psycopg.Connection[Any], title: str, body: str) -> str:
    """A non-draft vault document with one chunk and NO embedding."""
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, vault_path, draft, kind) "
        "VALUES (%s, %s, %s, 'note', %s, FALSE, 'vault') RETURNING id::text",
        (title, body, f"hash-fts-{title}", f"fts/{title}.md"),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content) VALUES (%s::uuid, 0, %s)",
        (row[0], body),
    )
    return str(row[0])


def _seed(conn: psycopg.Connection[Any]) -> tuple[str, str, str]:
    source = _insert_doc(conn, "source", f"{_TOKEN} source body")
    normal = _insert_doc(conn, "normal", f"{_TOKEN} normal body")
    secret = _insert_doc(conn, "secret", f"{_TOKEN} confidential body")
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid", (CONFIDENTIAL, secret)
    )
    return source, normal, secret


def _leg_doc_ids(
    conn: psycopg.Connection[Any], source: str, *, exclude_confidential: bool
) -> list[str]:
    row = conn.execute("SELECT to_tsquery('english', %s)::text", (_TOKEN,)).fetchone()
    assert row is not None
    rows = fts_candidates(
        conn,
        tsquery=str(row[0]),
        exclude_doc_id=source,
        exclude_confidential=exclude_confidential,
    )
    return [doc_id for _, doc_id, _ in rows]


def test_confidential_document_is_gated_out_of_the_fts_leg(
    test_db: psycopg.Connection[Any],
) -> None:
    source, normal, secret = _seed(test_db)
    got = _leg_doc_ids(test_db, source, exclude_confidential=True)
    assert secret not in got
    assert got == [normal]  # the leg is not simply empty


def test_include_confidential_opens_the_fts_leg(test_db: psycopg.Connection[Any]) -> None:
    """The permissive direction, so an always-on gate cannot pass as correct."""
    source, normal, secret = _seed(test_db)
    got = _leg_doc_ids(test_db, source, exclude_confidential=False)
    assert sorted(got) == sorted([normal, secret])
