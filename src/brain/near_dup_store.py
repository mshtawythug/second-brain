"""SQL for near-duplicate detection: signatures, scans, pairs and labels (RM-19).

The arithmetic lives in the pure leaf :mod:`brain.near_dup`; every statement over
``document_minhash`` and ``near_duplicates`` (migration 030) lives here.

- **Stale** means no signature row, or one whose ``content_hash`` differs from the document's.
  :func:`scan` refreshes stale documents (every document with ``recompute_all``), oldest first,
  each in its own transaction, so a capped or interrupted scan keeps what it did.
- **Refreshing** a document (:func:`refresh_document`) stores its signature (empty arrays under
  the 50-word floor, so a stub is not revisited and never matches a band), finds candidates through
  the GIN index on ``bands`` among FRESH signatures only, drops two documents of one thread,
  verifies each candidate at the 0.85 cut, then deletes this document's OPEN pairs that did not
  verify and upserts the verified ones. **A scan never writes a status**: a dismissed pair stays
  dismissed and a confirmed one stays confirmed, whatever later edits do (plan R3, AC5).
- **The lens is on both ends.** A pair names two documents, so :func:`list_pairs` and
  :func:`count_open` drop a pair when EITHER end is confidential, as
  :func:`brain.connect.iter_suggestions` gates both endpoints. The gated count also drops draft
  ends: it is the brief's published count, and a delivered number cannot have drafts removed
  after the fact (plan R4).

Nothing here refuses, merges or deletes a document (RM-19.D1). Plan:
``docs/plans/2026-10-30-rm-19-near-duplicates.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from . import near_dup
from .errors import BrainError
from .sensitivity import not_confidential_sql

#: The labels a pair carries; a scan writes only ``open``, and only on a new pair.
STATUSES = ("open", "dismissed", "confirmed")
#: The most partners :func:`check_document` returns.
CHECK_LIMIT = 20


class NearDupError(BrainError):
    """A refused near-duplicate operation (unknown pair, bad status or limit)."""


@dataclass(frozen=True)
class ScanReport:
    """What one :func:`scan` did."""

    examined: int
    short: int
    pairs_new: int
    pairs_dropped: int
    #: Stale documents left for a later scan (the ``limit`` was reached).
    remaining: int


@dataclass(frozen=True)
class RefreshResult:
    """What refreshing one document did."""

    short: bool
    pairs_new: int
    pairs_dropped: int


@dataclass(frozen=True)
class DocRef:
    """One end of a pair."""

    document_id: str
    title: str


@dataclass(frozen=True)
class PairRow:
    """One recorded pair, both ends named."""

    doc_a: DocRef
    doc_b: DocRef
    similarity: float
    status: str
    detected_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """The wire shape of ``brain dedup list --json`` and MCP ``brain_dedup_list``."""
        return {
            "doc_a": {"id": self.doc_a.document_id, "title": self.doc_a.title},
            "doc_b": {"id": self.doc_b.document_id, "title": self.doc_b.title},
            "similarity": round(self.similarity, 4),
            "status": self.status,
            "detected_at": self.detected_at.isoformat(),
        }


@dataclass(frozen=True)
class DuplicateRef:
    """A partner of one document, as the Ingest tab's ``possible_duplicates`` lists it."""

    document_id: str
    title: str
    vault_path: str | None
    sensitivity: str
    similarity: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        """One entry of the job result's ``possible_duplicates`` (lensed by the route)."""
        return {
            "document_id": self.document_id,
            "title": self.title,
            "vault_path": self.vault_path,
            "sensitivity": self.sensitivity,
            "similarity": round(self.similarity, 4),
            "status": self.status,
        }


def tables_present(conn: psycopg.Connection[Any]) -> bool:
    """Whether migration 030's two tables exist (a cheap catalog probe)."""
    row = conn.execute(
        "SELECT to_regclass('public.document_minhash') IS NOT NULL "
        "AND to_regclass('public.near_duplicates') IS NOT NULL"
    ).fetchone()
    return bool(row and row[0])


_STALE_FROM = """
    FROM documents d
    LEFT JOIN document_minhash m ON m.document_id = d.id
    WHERE m.document_id IS NULL OR m.content_hash <> d.content_hash
"""
_STALE_IDS_SQL = f"SELECT d.id::text {_STALE_FROM} ORDER BY d.ingested_at, d.id LIMIT %s"
_STALE_COUNT_SQL = f"SELECT count(*) {_STALE_FROM}"
_ALL_IDS_SQL = "SELECT id::text FROM documents ORDER BY ingested_at, id LIMIT %s"
_ALL_COUNT_SQL = "SELECT count(*) FROM documents"


def _count(conn: psycopg.Connection[Any], sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def scan(
    conn: psycopg.Connection[Any], *, recompute_all: bool = False, limit: int | None = None
) -> ScanReport:
    """Refresh stale documents (every document with ``recompute_all``), at most ``limit``.

    Raises:
        NearDupError: ``limit`` is not positive.
    """
    if limit is not None and limit < 1:
        raise NearDupError(f"limit must be at least 1 (got {limit})")
    total = _count(conn, _ALL_COUNT_SQL if recompute_all else _STALE_COUNT_SQL)
    ids = [
        str(row[0])
        for row in conn.execute(
            _ALL_IDS_SQL if recompute_all else _STALE_IDS_SQL, (limit,)
        ).fetchall()
    ]
    examined = short = new = dropped = 0
    for document_id in ids:
        with conn.transaction():
            result = refresh_document(conn, document_id)
        if result is None:  # deleted since the id list was read
            continue
        examined += 1
        short += int(result.short)
        new += result.pairs_new
        dropped += result.pairs_dropped
    return ScanReport(
        examined=examined,
        short=short,
        pairs_new=new,
        pairs_dropped=dropped,
        remaining=max(total - len(ids), 0),
    )


_CANDIDATES_SQL = """
    SELECT m.document_id::text, m.signature, d.thread_id
    FROM   document_minhash m
    JOIN   documents d ON d.id = m.document_id
    WHERE  m.bands && %s::bigint[]
      AND  m.document_id <> %s
      AND  m.content_hash = d.content_hash
      AND  cardinality(m.signature) = %s
"""

_DROP_OPEN_SQL = """
    DELETE FROM near_duplicates
    WHERE  (doc_a = %(doc)s OR doc_b = %(doc)s)
      AND  status = 'open'
      AND  (CASE WHEN doc_a = %(doc)s THEN doc_b ELSE doc_a END) <> ALL(%(keep)s::uuid[])
"""

_UPSERT_PAIR_SQL = """
    INSERT INTO near_duplicates (doc_a, doc_b, similarity) VALUES (%s, %s, %s)
    ON CONFLICT (doc_a, doc_b) DO UPDATE SET similarity = EXCLUDED.similarity
    RETURNING (xmax = 0)
"""


def refresh_document(conn: psycopg.Connection[Any], document_id: str) -> RefreshResult | None:
    """Re-sign one document and reconcile its pairs; ``None`` when the document is gone.

    The caller owns the transaction (:func:`scan` gives each document its own).
    """
    row = conn.execute(
        "SELECT content, content_hash, thread_id FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    if row is None:
        return None
    content, content_hash, thread_id = str(row[0]), str(row[1]), row[2]
    minhash = near_dup.compute(content)
    conn.execute(
        "INSERT INTO document_minhash (document_id, content_hash, signature, bands)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (document_id) DO UPDATE SET content_hash = EXCLUDED.content_hash,"
        " signature = EXCLUDED.signature, bands = EXCLUDED.bands, computed_at = NOW()",
        (
            document_id,
            content_hash,
            list(minhash.signature) if minhash else [],
            list(minhash.bands) if minhash else [],
        ),
    )
    verified = _verified_partners(conn, document_id, thread_id, minhash)
    dropped = conn.execute(
        _DROP_OPEN_SQL, {"doc": document_id, "keep": list(verified)}
    ).rowcount
    new = 0
    for partner, similarity in verified.items():
        doc_a, doc_b = near_dup.ordered_pair(document_id, partner)
        inserted = conn.execute(_UPSERT_PAIR_SQL, (doc_a, doc_b, similarity)).fetchone()
        new += int(bool(inserted and inserted[0]))
    return RefreshResult(short=minhash is None, pairs_new=new, pairs_dropped=max(dropped, 0))


def _verified_partners(
    conn: psycopg.Connection[Any],
    document_id: str,
    thread_id: str | None,
    minhash: near_dup.MinHash | None,
) -> dict[str, float]:
    """Partner id -> estimated Jaccard, for every candidate that verifies."""
    if minhash is None:
        return {}
    candidates = conn.execute(
        _CANDIDATES_SQL, (list(minhash.bands), document_id, near_dup.NUM_PERM)
    ).fetchall()
    verified: dict[str, float] = {}
    for partner, partner_signature, partner_thread in candidates:
        if near_dup.same_thread(thread_id, partner_thread):
            continue
        estimate = near_dup.estimated_jaccard(minhash.signature, partner_signature)
        if estimate >= near_dup.JACCARD_CUT:
            verified[str(partner)] = estimate
    return verified


_PARTNERS_SQL = """
    SELECT d.id::text, d.title, d.vault_path, d.sensitivity, n.similarity, n.status
    FROM   near_duplicates n
    JOIN   documents d ON d.id = (CASE WHEN n.doc_a = %(doc)s THEN n.doc_b ELSE n.doc_a END)
    WHERE  (n.doc_a = %(doc)s OR n.doc_b = %(doc)s)
      AND  n.status <> 'dismissed'
    ORDER  BY n.similarity DESC, d.id
    LIMIT  %(limit)s
"""


def check_document(conn: psycopg.Connection[Any], document_id: str) -> list[DuplicateRef]:
    """Refresh one document if stale, then list its open and confirmed partners.

    Empty when migration 030 is absent. The Ingest tab calls this after a successful ingest
    (RM-10's ``possible_duplicates``); a ``psycopg.Error`` propagates and the caller decides.
    """
    if not tables_present(conn):
        return []
    with conn.transaction():
        stale = conn.execute(
            "SELECT 1 FROM documents d LEFT JOIN document_minhash m ON m.document_id = d.id"
            " WHERE d.id = %s AND (m.document_id IS NULL OR m.content_hash <> d.content_hash)",
            (document_id,),
        ).fetchone()
        if stale is not None:
            refresh_document(conn, document_id)
        rows = conn.execute(
            _PARTNERS_SQL, {"doc": document_id, "limit": CHECK_LIMIT}
        ).fetchall()
    return [
        DuplicateRef(
            document_id=str(r[0]),
            title=str(r[1]),
            vault_path=r[2],
            sensitivity=str(r[3]),
            similarity=float(r[4]),
            status=str(r[5]),
        )
        for r in rows
    ]


# Both ends gated, by frozen fragments (never a bound level): see the module docstring.
_BOTH_ENDS_GATED = f"AND {not_confidential_sql('da')} AND {not_confidential_sql('db')}"

_LIST_SQL = """
    SELECT n.doc_a::text, da.title, n.doc_b::text, db.title,
           n.similarity, n.status, n.detected_at
    FROM   near_duplicates n
    JOIN   documents da ON da.id = n.doc_a
    JOIN   documents db ON db.id = n.doc_b
    WHERE  n.status = %s {lens}
    ORDER  BY n.similarity DESC, n.detected_at DESC, n.doc_a, n.doc_b
    LIMIT  %s
"""
_LIST_GATED_SQL = _LIST_SQL.format(lens=_BOTH_ENDS_GATED)
_LIST_PERMISSIVE_SQL = _LIST_SQL.format(lens="")


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise NearDupError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")


def list_pairs(
    conn: psycopg.Connection[Any],
    *,
    status: str = "open",
    exclude_confidential: bool = False,
    limit: int = 50,
) -> list[PairRow]:
    """Pairs with ``status``, most similar first; with the lens, both ends non-confidential.

    Raises:
        NearDupError: an unknown status, or a ``limit`` below 1.
    """
    _check_status(status)
    if limit < 1:
        raise NearDupError(f"limit must be at least 1 (got {limit})")
    rows = conn.execute(
        _LIST_GATED_SQL if exclude_confidential else _LIST_PERMISSIVE_SQL, (status, limit)
    ).fetchall()
    return [
        PairRow(
            doc_a=DocRef(document_id=str(r[0]), title=str(r[1])),
            doc_b=DocRef(document_id=str(r[2]), title=str(r[3])),
            similarity=float(r[4]),
            status=str(r[5]),
            detected_at=r[6],
        )
        for r in rows
    ]


_COUNT_SQL = """
    SELECT count(*)
    FROM   near_duplicates n
    JOIN   documents da ON da.id = n.doc_a
    JOIN   documents db ON db.id = n.doc_b
    WHERE  n.status = 'open' {lens}
"""
_COUNT_GATED_SQL = _COUNT_SQL.format(
    lens=f"{_BOTH_ENDS_GATED} AND da.draft = FALSE AND db.draft = FALSE"
)
_COUNT_PERMISSIVE_SQL = _COUNT_SQL.format(lens="")


def count_open(conn: psycopg.Connection[Any], *, exclude_confidential: bool) -> int:
    """Open pairs; gated, only pairs whose both ends are neither confidential nor drafts.

    ``0`` when migration 030 is absent, so a daemon reinstall without ``brain init`` cannot
    break the brief.
    """
    if not tables_present(conn):
        return 0
    return _count(conn, _COUNT_GATED_SQL if exclude_confidential else _COUNT_PERMISSIVE_SQL)


def set_status(
    conn: psycopg.Connection[Any], doc_x: str, doc_y: str, status: str
) -> None:
    """Label the recorded pair of two documents (in either order).

    Raises:
        NearDupError: an unknown status, or no recorded pair for the two documents.
    """
    _check_status(status)
    doc_a, doc_b = near_dup.ordered_pair(doc_x, doc_y)
    updated = conn.execute(
        "UPDATE near_duplicates SET status = %s WHERE doc_a = %s AND doc_b = %s",
        (status, doc_a, doc_b),
    ).rowcount
    if not updated:
        raise NearDupError(f"no recorded pair for {doc_a} and {doc_b}")


def section_title(count: int) -> str:
    """The brief's "Possible duplicates (N)" heading."""
    return f"Possible duplicates ({count})"


def brief_lines(count: int) -> list[str]:
    """The brief section's one line, without a bullet; empty when no pair is open."""
    return ["review them with `brain dedup list`"] if count > 0 else []
