"""Unit tests for the seven tools registered by ``brain.mcp_server``.

Each test installs a fresh ``_State`` (real test-DB Config + fake embedder)
via ``monkeypatch.setattr`` and calls the tool functions directly. The
JSON-RPC round-trip is exercised separately by the protocol integration test.
"""
import logging
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
import voyageai.error
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


class _BoomConnect:
    """Stub that mimics ``connect()``: enters a context that raises immediately.

    Used by every "wraps DB error" test — extracted here so we don't reopen
    the same five-line helper four times.
    """

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or psycopg.OperationalError("simulated outage")

    def __call__(self, _url: str) -> "_BoomConnect":
        return self

    def __enter__(self) -> Any:
        raise self._exc

    def __exit__(self, *_: object) -> None:
        return None


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


def _doc_tags(doc_id: str) -> list[str]:
    """Read the current tag list for a document directly from Postgres."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _doc_metadata(doc_id: str) -> dict[str, Any]:
    """Read the current metadata blob for a document directly from Postgres."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT metadata FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _chunk_count(doc_id: str) -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _content_hash(doc_id: str) -> str:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT content_hash FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


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


class _BoomEmbedder:
    """Embedder stub that always raises a voyageai exception on ``embed``.

    Used to assert each tool wraps voyage failures as ``McpError`` rather
    than letting a raw ``VoyageError`` propagate to the MCP runtime.
    """

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise voyageai.error.RateLimitError("rate limited")

    def count_tokens(self, text: str) -> int:
        return 1


def test_brain_search_wraps_voyage_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    """A Voyage failure must surface as McpError, never a raw VoyageError."""
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="anything")
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "RateLimitError" in msg


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
# brain_ingest_stdin
# ---------------------------------------------------------------------------


def test_brain_ingest_stdin_creates_document(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_ingest_stdin(
        content="company-id dealmaker notes from the q1 review",
        source="krisp",
        external_id="krisp:meeting:42",
        title="Q1 review",
        content_type="transcript",
    )
    assert payload["created"] is True
    assert payload["document_id"] is not None
    # The doc should now show up in search.
    results = mcp_server.brain_search(query="company-id")
    titles = {r["title"] for r in results}
    assert "Q1 review" in titles


def test_brain_ingest_stdin_dedup_on_external_id(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    first = mcp_server.brain_ingest_stdin(
        content="some content body",
        source="krisp",
        external_id="krisp:dup:1",
        title="First",
    )
    second = mcp_server.brain_ingest_stdin(
        content="some content body",  # same body → same content_hash
        source="krisp",
        external_id="krisp:dup:1",
        title="First",
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["document_id"] == first["document_id"]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\t  \n"])
def test_brain_ingest_stdin_empty_content_errors(
    empty: str,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ingest_stdin(
            content=empty,
            source="krisp",
            external_id="krisp:empty",
            title="Empty",
        )
    assert "content is empty" in exc_info.value.error.message


def test_brain_ingest_stdin_auto_tags_with_source_mcp(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """No tags arg → stored tags are exactly [\"source-mcp\"]."""
    payload = mcp_server.brain_ingest_stdin(
        content="auto-tagging body",
        source="krisp",
        external_id="krisp:auto-tag",
        title="Auto",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert _doc_tags(doc_id) == ["source-mcp"]


def test_brain_ingest_stdin_user_tags_union_with_source_mcp(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """User tags are unioned (set semantics, dedup) with source-mcp."""
    payload = mcp_server.brain_ingest_stdin(
        content="union body",
        source="krisp",
        external_id="krisp:union",
        title="Union",
        tags=["interview", "source-mcp"],  # explicit dup of auto tag
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert sorted(_doc_tags(doc_id)) == ["interview", "source-mcp"]


def test_brain_ingest_stdin_passes_date_into_metadata(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``date`` arg is stored under metadata.date (parity with the CLI)."""
    payload = mcp_server.brain_ingest_stdin(
        content="dated body",
        source="krisp",
        external_id="krisp:dated",
        title="Dated",
        date="2026-01-15",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert _doc_metadata(doc_id)["date"] == "2026-01-15"


# ---------------------------------------------------------------------------
# brain_tag
# ---------------------------------------------------------------------------


def test_brain_tag_adds_and_removes(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(
        test_db, fake_embedder, title="Tagged", content="body", tags=["one"]
    )
    payload = mcp_server.brain_tag(id_prefix=doc_id[:8], add=["x"])
    assert sorted(payload["tags"]) == ["one", "x"]
    payload = mcp_server.brain_tag(id_prefix=doc_id[:8], remove=["x"])
    assert payload["tags"] == ["one"]
    payload = mcp_server.brain_tag(id_prefix=doc_id[:8], add=["a"], remove=["one"])
    assert payload["tags"] == ["a"]
    assert payload["document_id"] == doc_id


def test_brain_tag_unknown_id_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_tag(id_prefix="ffffff", add=["x"])
    assert "not found" in exc_info.value.error.message


def test_brain_tag_requires_add_or_remove(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="body")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_tag(id_prefix=doc_id[:8])
    assert "add or remove" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# brain_edit
# ---------------------------------------------------------------------------


def test_brain_edit_title_only_no_voyage_call(
    test_db: psycopg.Connection,
    counting_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Title-only edit must not call the embedder."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, voyage_api_key="fake"),
        embedder=counting_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    doc_id = _ingest(
        test_db, counting_embedder, title="Old", content="body content here"
    )
    counting_embedder.embed_calls = 0  # reset after the seed ingest
    payload = mcp_server.brain_edit(id_prefix=doc_id[:8], title="New")
    assert payload["fields_changed"] == ["title"]
    assert payload["rechunked"] is False
    assert counting_embedder.embed_calls == 0
    # And the title actually persisted.
    fresh = mcp_server.brain_show(id_prefix=doc_id[:8])
    assert fresh["title"] == "New"


def test_brain_edit_content_rechunks_and_reembeds(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(
        test_db, fake_embedder, title="A", content="original body"
    )
    pre_chunks = _chunk_count(doc_id)
    pre_hash = _content_hash(doc_id)
    payload = mcp_server.brain_edit(
        id_prefix=doc_id[:8],
        content="completely different body so the hash must change",
    )
    assert payload["rechunked"] is True
    assert "content" in payload["fields_changed"]
    post_chunks = _chunk_count(doc_id)
    post_hash = _content_hash(doc_id)
    # Both chunks and the content_hash should reflect the new body.
    assert post_chunks >= 1
    assert post_hash != pre_hash
    # We don't assert pre_chunks != post_chunks (single short body → 1 chunk
    # before and after); the hash + rechunked flag prove the path ran.
    _ = pre_chunks  # silence unused-warn


def test_brain_edit_metadata_merge_keeps_other_keys(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    # Seed a doc, then bolt on metadata via direct SQL (the ingest pipeline
    # accepts metadata only via source_metadata; documents.metadata stays at
    # ExtractedDoc.metadata which defaults to {}).
    doc_id = _ingest(test_db, fake_embedder, title="A", content="body")
    test_db.execute(
        "UPDATE documents SET metadata = %s::jsonb WHERE id = %s",
        ('{"a": 1, "b": 2}', doc_id),
    )
    payload = mcp_server.brain_edit(
        id_prefix=doc_id[:8], metadata={"b": 3}
    )
    assert payload["fields_changed"] == ["metadata"]
    assert _doc_metadata(doc_id) == {"a": 1, "b": 3}


def test_brain_edit_metadata_replace_swaps_blob(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="body")
    test_db.execute(
        "UPDATE documents SET metadata = %s::jsonb WHERE id = %s",
        ('{"a": 1, "b": 2}', doc_id),
    )
    payload = mcp_server.brain_edit(
        id_prefix=doc_id[:8],
        metadata={"only": "this"},
        replace_metadata=True,
    )
    assert payload["fields_changed"] == ["metadata"]
    assert _doc_metadata(doc_id) == {"only": "this"}


def test_brain_edit_no_args_errors(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="body")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_edit(id_prefix=doc_id[:8])
    assert "no edit fields" in exc_info.value.error.message


def test_brain_edit_replace_metadata_without_metadata_errors(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="body")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_edit(id_prefix=doc_id[:8], replace_metadata=True)
    assert "replace_metadata" in exc_info.value.error.message


def test_brain_edit_propagates_value_errors(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A ValueError from update_document (e.g. content collision) becomes McpError."""
    doc_a = _ingest(test_db, fake_embedder, title="A", content="alpha body")
    doc_b = _ingest(test_db, fake_embedder, title="B", content="bravo body")
    # Trying to set doc_b's content to doc_a's content collides on hash.
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_edit(id_prefix=doc_b[:8], content="alpha body")
    assert "collides" in exc_info.value.error.message
    _ = doc_a  # silence unused-warn


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


def test_configure_logging_sets_known_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting BRAIN_MCP_LOG_LEVEL=DEBUG must put the logger at DEBUG."""
    monkeypatch.setenv("BRAIN_MCP_LOG_LEVEL", "DEBUG")
    mcp_server._configure_logging()
    assert (
        logging.getLogger("brain.mcp").getEffectiveLevel() == logging.DEBUG
    )


def test_configure_logging_falls_back_on_unknown_level(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("BRAIN_MCP_LOG_LEVEL", "WHATEVER")
    with caplog.at_level("WARNING", logger="brain.mcp"):
        mcp_server._configure_logging()
        # Assert inside the ``at_level`` block — pytest restores the logger
        # level on exit, which would otherwise mask what we just set.
        assert (
            logging.getLogger("brain.mcp").getEffectiveLevel()
            == logging.INFO
        )
    assert any("WHATEVER" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# DB error wrapping
# ---------------------------------------------------------------------------


def test_brain_status_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """If connect() hits psycopg.Error, surface as McpError with redacted text."""
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_status()
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg
    # Redaction: do NOT leak the raw psycopg message text.
    assert "simulated outage" not in msg


def test_brain_search_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="anything")
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg
    assert "simulated outage" not in msg


def test_brain_show_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(id_prefix="abcdef")
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg
    assert "simulated outage" not in msg


def test_brain_list_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_list()
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg
    assert "simulated outage" not in msg


def test_brain_ingest_stdin_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ingest_stdin(
            content="body", source="krisp", external_id="x", title="t"
        )
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg


def test_brain_tag_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_tag(id_prefix="abcdef", add=["x"])
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_edit_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_edit(id_prefix="abcdef", title="new")
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_ingest_stdin_wraps_voyage_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ingest_stdin(
            content="body that will need embedding",
            source="krisp",
            external_id="krisp:voy",
            title="t",
        )
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "RateLimitError" in msg


def test_brain_edit_wraps_voyage_error(
    test_db: psycopg.Connection,
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    """A re-embed failure during brain_edit must surface as McpError."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="original")
    # Swap the state's embedder to one that raises on the new content.
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_edit(id_prefix=doc_id[:8], content="brand new body")
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "RateLimitError" in msg


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
