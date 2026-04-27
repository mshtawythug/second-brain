"""Unit tests for the four read tools registered by ``brain.mcp_server``.

Each test installs a fresh ``_State`` (real test-DB Config + fake embedder)
via ``monkeypatch.setattr`` and calls the tool functions directly. The
JSON-RPC round-trip is exercised separately by the protocol integration test.
"""
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from mcp import McpError

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema fresh
    fake_embedder: object,
) -> Iterator[mcp_server._State]:
    """Install a server state pointing at the test DB + fake embedder.

    Uses ``monkeypatch.setattr`` so the previous value is restored after the
    test (whether or not main() was ever called)."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, voyage_api_key="fake"),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _ingest(
    conn: psycopg.Connection,
    embedder: object,
    *,
    title: str,
    content: str,
    source_kind: str = "manual",
    source_external_id: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Helper: ingest a document and return its UUID.

    Mirrors the ``seed_doc`` fixture but lets each test pick the source kind
    and external id explicitly so we can test the source filter."""
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=source_external_id,
        tags=tags or [],
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# brain_search
# ---------------------------------------------------------------------------


def test_brain_search_returns_expected_shape(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001 — fixture installs state
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="Doc A",
        content="Doc A: company-id was a great company to work at",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Doc B",
        content="Doc B: krisp meeting transcript about pizza",
    )
    results = mcp_server.brain_search(query="company-id")
    assert results, "expected at least one search hit"
    expected_keys = {
        "id",
        "title",
        "source_kind",
        "snippet",
        "score",
        "content_type",
        "tags",
    }
    for r in results:
        assert set(r.keys()) == expected_keys
    titles = [r["title"] for r in results]
    assert "Doc A" in titles


def test_brain_search_respects_filters(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="Manual one",
        content="Manual one: company-id shared term",
        source_kind="manual",
        source_external_id="manual:one",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp one",
        content="Krisp one: company-id shared term",
        source_kind="krisp",
        source_external_id="krisp:one",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp two",
        content="Krisp two: company-id shared term",
        source_kind="krisp",
        source_external_id="krisp:two",
    )
    results = mcp_server.brain_search(query="company-id", source="krisp")
    assert results
    for r in results:
        assert r["source_kind"] == "krisp"
    titles = {r["title"] for r in results}
    assert titles == {"Krisp one", "Krisp two"}


# ---------------------------------------------------------------------------
# brain_show
# ---------------------------------------------------------------------------


def test_brain_show_full_document(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    body = "Specific body: person-a said the deal closed friday."
    doc_id = _ingest(test_db, fake_embedder, title="person-x update", content=body)
    payload = mcp_server.brain_show(id_prefix=doc_id[:8])
    assert payload["id"] == doc_id
    assert payload["title"] == "person-x update"
    assert payload["content"] == body
    assert payload["content_type"] == "note"
    assert payload["tags"] == []
    assert payload["source_kind"] is None  # manual ingest with no external id
    assert payload["ingested_at"] is not None


def test_brain_show_unknown_id_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="ffffff")
    assert "not found" in exc_info.value.error.message


def test_brain_show_ambiguous_prefix_errors(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    # Force two documents whose IDs share a 6-char prefix. Insert directly to
    # bypass the chunk pipeline (no embedding required for this assertion).
    for new_id, content in (
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "alpha"),
        ("aaaaaabb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "bravo"),
    ):
        test_db.execute(
            "INSERT INTO documents (id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s)",
            (new_id, content, content, content + "_h", "note"),
        )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="aaaaaa")
    assert "ambiguous" in exc_info.value.error.message


def test_brain_show_short_prefix_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Defensive: <6 chars must be rejected before touching the DB."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="abc")
    assert "6 characters" in exc_info.value.error.message


def test_brain_show_non_hex_prefix_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Defensive: a `_` or `%` must not slip through into the LIKE query."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="abc_de%")
    assert "hex digits" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# brain_list
# ---------------------------------------------------------------------------


def test_brain_list_filters_by_source_and_tag(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="Manual none",
        content="Manual none: body",
        source_kind="manual",
        source_external_id="manual:none",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp tagged",
        content="Krisp tagged: body",
        source_kind="krisp",
        source_external_id="krisp:tagged",
        tags=["x"],
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp plain",
        content="Krisp plain: body",
        source_kind="krisp",
        source_external_id="krisp:plain",
    )

    by_source = mcp_server.brain_list(source="krisp")
    titles = {r["title"] for r in by_source}
    assert titles == {"Krisp tagged", "Krisp plain"}
    expected_keys = {
        "id",
        "title",
        "content_type",
        "tags",
        "source_kind",
        "ingested_at",
    }
    for r in by_source:
        assert set(r.keys()) == expected_keys
        assert r["source_kind"] == "krisp"

    by_tag = mcp_server.brain_list(tag="x")
    assert [r["title"] for r in by_tag] == ["Krisp tagged"]
    assert by_tag[0]["tags"] == ["x"]


def test_brain_list_respects_limit(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    for i in range(3):
        _ingest(
            test_db,
            fake_embedder,
            title=f"Doc {i}",
            content=f"Doc {i}: body {i}",
        )
    results = mcp_server.brain_list(limit=2)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# brain_status
# ---------------------------------------------------------------------------


def test_brain_status_returns_counts(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="A",
        content="A: alpha body",
        source_kind="manual",
        source_external_id="manual:a",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="B",
        content="B: bravo body",
        source_kind="krisp",
        source_external_id="krisp:b",
    )
    payload = mcp_server.brain_status()
    assert payload["documents"] == 2
    assert payload["chunks"] >= 2
    assert payload["sources"] == 2
    assert payload["last_ingest"] is not None
    kinds = {row["kind"]: row["count"] for row in payload["by_kind"]}
    assert kinds == {"manual": 1, "krisp": 1}


def test_brain_status_on_empty_db(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_status()
    assert payload["documents"] == 0
    assert payload["chunks"] == 0
    assert payload["sources"] == 0
    assert payload["last_ingest"] is None
    assert payload["by_kind"] == []


# ---------------------------------------------------------------------------
# Server state lifecycle + logging configuration
# ---------------------------------------------------------------------------


def test_get_state_without_init_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call before main() runs is a programmer error, not a user error."""
    monkeypatch.setattr(mcp_server, "_state", None)
    with pytest.raises(AssertionError):
        mcp_server._get_state()


def test_configure_logging_accepts_known_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_MCP_LOG_LEVEL", "DEBUG")
    # basicConfig is a no-op once a handler is installed, but the call must
    # not raise and must not warn.
    mcp_server._configure_logging()


def test_configure_logging_falls_back_on_unknown_level(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("BRAIN_MCP_LOG_LEVEL", "WHATEVER")
    with caplog.at_level("WARNING", logger="brain.mcp"):
        mcp_server._configure_logging()
    assert any("WHATEVER" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# DB error wrapping
# ---------------------------------------------------------------------------


def test_brain_status_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    """If the connect() context manager hits psycopg.Error, surface as McpError."""

    class _Boom:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("simulated outage")

        def __exit__(self, *_: object) -> None:
            return None

    def _fail(_url: str) -> Any:
        return _Boom()

    monkeypatch.setattr(mcp_server, "connect", _fail)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_status()
    assert "simulated outage" in exc_info.value.error.message


def test_brain_search_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    class _Boom:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("simulated outage")

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mcp_server, "connect", lambda _u: _Boom())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="anything")
    assert "simulated outage" in exc_info.value.error.message


def test_brain_show_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    class _Boom:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("simulated outage")

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mcp_server, "connect", lambda _u: _Boom())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="abcdef")
    assert "simulated outage" in exc_info.value.error.message


def test_brain_list_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    class _Boom:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("simulated outage")

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mcp_server, "connect", lambda _u: _Boom())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_list()
    assert "simulated outage" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# main() — wire-up sanity (full subprocess coverage lives in
# tests/test_mcp_server_protocol.py)
# ---------------------------------------------------------------------------


def test_main_initializes_state_and_starts_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() must build _State from env and hand off to mcp_app.run(stdio)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setattr(mcp_server, "_state", None)

    captured: dict[str, object] = {}

    def _fake_run(transport: str = "stdio") -> None:
        captured["transport"] = transport
        captured["state"] = mcp_server._state

    monkeypatch.setattr(mcp_server.mcp_app, "run", _fake_run)
    mcp_server.main()
    assert captured["transport"] == "stdio"
    assert isinstance(captured["state"], mcp_server._State)
    assert captured["state"].cfg.database_url == TEST_DATABASE_URL  # type: ignore[union-attr]
