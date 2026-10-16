"""Shared helpers for tests that need the production HNSW shape on the test database.

The test database's ``chunks.embedding`` is ``vector(4096)``, over pgvector's
2,000-dim HNSW cap, so it has no HNSW index and every vector query there is
exact by accident. A test that wants to see what the index does must rebuild
the production shape under ``@pytest.mark.fresh_schema``: a 1024-dim column via
:func:`brain.db.ensure_embedding_column`, then the index via
:func:`brain.queries.finalize_embedding_index` (the calls ``brain init`` /
``brain reembed`` make). Small tables also need "planner pressure" before the
planner will choose the index the way it does on the live corpus.
"""
from __future__ import annotations

import math
from typing import Any

import psycopg

from tests.conftest import FakeEmbedder

#: The production HNSW-capable dimensionality (arctic / voyage).
HNSW_DIM = 1024

#: Name of the HNSW index :func:`brain.queries.finalize_embedding_index` builds.
HNSW_INDEX = "chunks_embedding_idx"


def use_hnsw_dimension(conn: psycopg.Connection[Any]) -> FakeEmbedder:
    """Resize ``chunks.embedding`` to 1024 dims. Call before inserting chunks."""
    from brain.db import ensure_embedding_column

    embedder = FakeEmbedder(dim=HNSW_DIM)
    ensure_embedding_column(conn, embedder)
    return embedder


def build_hnsw_index(conn: psycopg.Connection[Any], embedder: FakeEmbedder) -> None:
    """Create the real HNSW index over the chunks inserted so far."""
    from brain.queries import finalize_embedding_index

    finalize_embedding_index(conn, embedder)


def apply_planner_pressure(conn: psycopg.Connection[Any]) -> None:
    """Make a small table plan like the live corpus: through the HNSW index.

    On a few hundred rows the planner would otherwise serve these queries with a
    sequential scan and an exact sort, which is exact by accident and would let
    the defective SQL pass. Session-level, on the test's own connection.
    """
    conn.execute("SET enable_seqscan = off")
    conn.execute("SET enable_sort = off")


def plan_text(conn: psycopg.Connection[Any], sql: str, params: Any) -> str:
    """``EXPLAIN`` output for ``sql`` as one string."""
    return "\n".join(row[0] for row in conn.execute("EXPLAIN " + sql, params).fetchall())


def index_scan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every plan node (JSON ``EXPLAIN``) that scans the HNSW index."""
    found = [plan] if plan.get("Index Name") == HNSW_INDEX else []
    for child in plan.get("Plans", []):
        found.extend(index_scan_nodes(child))
    return found


def vector_literal(values: list[float]) -> str:
    """pgvector text literal for ``values``."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class SettingsProbe:
    """A real connection that records the scan settings live at one statement.

    Not a mock and not a patch: the code under test takes its connection as an
    argument, so the seam is the production signature. When a statement whose
    text contains ``marker`` arrives, the probe reads the three design-D settings
    in that same transaction, then passes the statement through.
    """

    def __init__(self, inner: psycopg.Connection[Any], marker: str) -> None:
        self._inner = inner
        self._marker = marker
        self.seen: list[tuple[Any, ...]] = []

    def execute(self, sql: Any, params: Any = None, **kwargs: Any) -> Any:
        if self._marker in str(sql):
            row = self._inner.execute(
                "SELECT current_setting('hnsw.ef_search'), "
                "current_setting('hnsw.iterative_scan'), "
                "current_setting('enable_seqscan')"
            ).fetchone()
            assert row is not None
            self.seen.append(tuple(row))
        return self._inner.execute(sql, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def current_scan_settings(conn: psycopg.Connection[Any]) -> tuple[Any, ...]:
    """The three design-D settings as the connection sees them now."""
    row = conn.execute(
        "SELECT current_setting('hnsw.ef_search', true), "
        "current_setting('hnsw.iterative_scan', true), "
        "current_setting('enable_seqscan')"
    ).fetchone()
    assert row is not None
    return tuple(row)


def floor_hitting_distance_exactly(distance: float) -> float | None:
    """A float ``f`` with ``1.0 - f == distance`` exactly, or ``None`` if none exists.

    The vector legs evaluate ``dist <= 1 - %s`` in float8, and Python floats are
    the same IEEE-754 doubles, so the search is over the representable
    neighbours of ``1 - distance``. The caller re-verifies the equality in
    Postgres; that is the check that counts.
    """
    candidate = 1.0 - distance
    for _ in range(64):
        if 1.0 - candidate == distance:
            return candidate
        candidate = math.nextafter(
            candidate, math.inf if 1.0 - candidate > distance else -math.inf
        )
    return None


#: ``(label, vault_path, draft)`` for the two ways a document is ineligible to be
#: a related-docs or ``connect`` candidate.
INELIGIBLE_TWINS: tuple[tuple[str, str | None, bool], ...] = (
    ("draft", "twins/draft-twin.md", True),
    ("vaultless", None, False),
)


def insert_ineligible_twins(conn: psycopg.Connection[Any], vector: str) -> dict[str, str]:
    """One draft doc and one vault-less doc, each with a single chunk of ``vector``.

    Give them the SOURCE's query vector: each then ranks first (cosine 1.0), so a
    leg that drops either eligibility predicate surfaces it deterministically.
    Returns ``{label: document_id}``.
    """
    twins: dict[str, str] = {}
    for label, vault_path, draft in INELIGIBLE_TWINS:
        row = conn.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, vault_path, draft, kind) "
            "VALUES (%s, %s, %s, 'note', %s, %s, 'vault') RETURNING id::text",
            (f"Synthetic {label} twin", "twin body", f"hash-twin-{label}", vault_path, draft),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s::uuid, 0, 'twin chunk', %s::vector)",
            (row[0], vector),
        )
        twins[label] = str(row[0])
    return twins
