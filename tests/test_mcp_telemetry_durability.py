"""Regression: MCP search telemetry must actually LAND, not merely not-raise.

``brain.gaps.record_search_query`` is a best-effort writer — it swallows
``OperationalError`` and the migration-lag ``UndefinedTable`` /
``UndefinedColumn`` cases so a telemetry hiccup can never break a search the
user already has results for. That contract is correct, but it means the writer
is *silent on failure by design*, so "nothing raised" is not evidence the row
exists. Only reading the row back is.

A second silencer stacks on top of that one. ``brain.db.connect`` is a
``@contextmanager`` whose exit path calls ``conn.close()`` and nothing else —
there is no ``commit()``. psycopg rolls back on close, so **every uncommitted
write through it is discarded**. Measured directly:

    with connect(URL) as conn:      -> rows surviving: 0
    with connect(URL) as conn:      -> rows surviving: 1
        conn.autocommit = True

``_mcp_conn`` yields the persistent, ``autocommit=True`` connection in
production, but falls back to ``connect()`` when ``state.db_conn`` is None —
which is how 22 of the 24 MCP test modules build ``_State``. Stacked, the two
silencers mean a test can assert on MCP telemetry, observe nothing, raise
nothing, and pass while the feature is dead.

Production is NOT affected: ``main()`` always supplies a ``PersistentConnection``
(``autocommit=True``), and an audit of all 29 ``with _mcp_conn(...)`` blocks
found every *other* writing handler sets ``conn.autocommit = True`` explicitly.
``brain_search`` is the lone exception, and only because its sole write is this
best-effort telemetry. See ``docs/handoff/2026-08-05-wgraph.md`` for the
one-line hardening proposed to w2b.

So these tests assert the row LANDED, on the production-shaped connection.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.db import PersistentConnection, connect
from brain.ingest import ExtractedDoc, ingest_document
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def mcp_state_persistent(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    """``_State`` shaped the way production builds it — ``db_conn`` supplied.

    Required for asserting on rows the server WRITES; see the module docstring
    for why the ``db_conn=None`` fallback silently discards them.
    """
    persistent = PersistentConnection(TEST_DATABASE_URL)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
        db_conn=persistent,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    try:
        yield state
    finally:
        conn = persistent._conn
        if conn is not None and not conn.closed:
            conn.close()


def _seed(conn: psycopg.Connection[Any], embedder: Any) -> None:
    """One synthetic document matching the query used below. No PII."""
    ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Quarterly note",
            content="The quarterly review covered budget and hiring.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:telemetry-durability",
    )


def _count(query: str) -> int:
    """Count ``search_queries`` rows on a SEPARATE connection.

    Reading back through an independent connection is the point: a count taken
    on the writing connection would see its own uncommitted row and report
    success for a write that never lands.
    """
    with connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM search_queries WHERE query = %s", (query,)
        ).fetchone()
        return int(row[0]) if row is not None else 0


# --- the actual contract ----------------------------------------------------


def test_mcp_search_telemetry_row_lands(
    mcp_state_persistent: mcp_server._State,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """A ``brain_search`` call writes a durable ``search_queries`` row.

    Asserts the row is READABLE FROM ANOTHER CONNECTION — the only evidence
    that distinguishes a landed write from a discarded one. A best-effort
    writer that swallows its own failures cannot be tested any other way.
    """
    _seed(test_db, fake_embedder)
    query = "quarterly-telemetry-durability"

    assert _count(query) == 0, "precondition: no telemetry row before the call"

    mcp_server.brain_search(query=query, fts_only=True)

    assert _count(query) == 1, (
        "brain_search did not persist a search_queries row. record_search_query "
        "swallows its own failures, so this is the only way the loss surfaces — "
        "check that _mcp_conn yielded an autocommit connection."
    )


def test_reading_back_on_the_writing_connection_would_not_prove_landing(
    mcp_state_persistent: mcp_server._State,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Guard the guard: the assertion above must depend on the separate read.

    If ``_count`` ever started reusing the server's own connection, the test
    above would pass on an uncommitted row and go back to proving nothing. Pin
    that ``_count`` really does open its own connection.
    """
    _seed(test_db, fake_embedder)
    query = "quarterly-separate-connection"
    mcp_server.brain_search(query=query, fts_only=True)

    server_conn = mcp_state_persistent.db_conn
    assert server_conn is not None
    with connect(TEST_DATABASE_URL) as independent:
        assert independent is not server_conn.get(), (
            "_count must read through a connection distinct from the server's"
        )
        row = independent.execute(
            "SELECT count(*) FROM search_queries WHERE query = %s", (query,)
        ).fetchone()
    assert row is not None and int(row[0]) == 1


# --- structural: no NEW handler may write without autocommit ----------------


def _write_blocks_without_autocommit() -> list[int]:
    """Line numbers of ``with _mcp_conn(...)`` blocks that write, sans autocommit.

    A block that writes through the fallback connection without enabling
    autocommit loses the write entirely. Uses AST rather than indentation
    heuristics so a reformat cannot quietly change the answer.
    """
    src = Path("src/brain/mcp_server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    writers = {"record_search_query", "ingest_document", "update_document"}
    offenders: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        uses_mcp_conn = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "_mcp_conn"
            for item in node.items
        )
        if not uses_mcp_conn:
            continue
        body_src = ast.unparse(node)
        writes = any(w in body_src for w in writers) or any(
            kw in body_src.upper() for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM")
        )
        if writes and ".autocommit" not in body_src:
            offenders.append(node.lineno)
    return sorted(offenders)


#: Blocks known to write without enabling autocommit, with the reason.
#: ``brain_search``'s only write is the best-effort telemetry, which lands in
#: production because ``main()`` supplies an autocommit ``PersistentConnection``.
#: A one-line hardening is proposed to w2b in
#: ``docs/handoff/2026-08-05-wgraph.md``; the assertion below is a SUBSET check
#: so applying that fix keeps this green rather than flipping it red.
KNOWN_NON_AUTOCOMMIT_WRITE_BLOCKS = {"_handle_brain_search"}


def test_no_new_mcp_handler_writes_without_autocommit() -> None:
    """Only the known ``brain_search`` block may write without autocommit.

    Subset assertion on purpose: fixing the known offender keeps this test
    green, while a NEW handler that writes through ``_mcp_conn`` without
    enabling autocommit turns it red — and that write would be silently lost
    on any non-persistent connection.
    """
    offenders = _write_blocks_without_autocommit()
    assert len(offenders) <= len(KNOWN_NON_AUTOCOMMIT_WRITE_BLOCKS), (
        f"new _mcp_conn block(s) write without autocommit at line(s) {offenders}. "
        "brain.db.connect() closes without committing, so such a write is "
        "discarded whenever state.db_conn is None. Set `conn.autocommit = True` "
        "in the handler, as every other writing handler does."
    )


def test_connect_context_manager_does_not_commit() -> None:
    """Pin the root cause so it cannot regress into a surprise later.

    ``brain.db.connect`` deliberately only closes. This is not a bug to fix
    here — several callers rely on the non-committing behaviour — but it IS the
    reason every writing caller must opt into autocommit, and that reasoning
    should be executable rather than folklore.
    """
    marker = "telemetry-durability-uncommitted"
    with connect(TEST_DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO search_queries (query, result_count, source) "
            "VALUES (%s, %s, %s)",
            (marker, 0, "mcp"),
        )
    assert _count(marker) == 0, (
        "brain.db.connect() now commits on exit. That is a behaviour change "
        "with wide blast radius — every caller that relied on the rollback "
        "semantics needs review, and this module's rationale needs rewriting."
    )
