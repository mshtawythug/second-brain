"""Unit tests for the seven tools registered by ``brain.mcp_server``.

Each test installs a fresh ``_State`` (real test-DB Config + fake embedder)
via ``monkeypatch.setattr`` and calls the tool functions directly. The
JSON-RPC round-trip is exercised separately by the protocol integration test.
"""
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import McpError

from brain import mcp_server
from brain.config import Config
from brain.embeddings import OllamaEmbedError
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.frontmatter import parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
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
        cfg=Config(database_url=TEST_DATABASE_URL),
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
    payload = mcp_server.brain_search(query="company-id")
    # Q1-C: brain_search now returns {session_id, results} (breaking shape).
    assert set(payload.keys()) == {"session_id", "results"}
    import uuid as _uuid

    _uuid.UUID(payload["session_id"])  # parses cleanly
    results = payload["results"]
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


def test_brain_search_propagates_vector_sim_floor_from_state_cfg(
    test_db: psycopg.Connection,
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP ``brain_search`` must pass ``cfg.vector_sim_floor`` to ``hybrid_search``.

    Regression for a missed callsite: ``brain.cli.search`` was updated to
    forward ``cfg.vector_sim_floor`` but ``brain.mcp_server.brain_search``
    was not, so MCP callers (Claude itself) silently got the pre-fix
    no-floor behavior and the search-ranking-fix's known-bad docs leaked
    back into their results.

    The contract is: MCP brain_search calls hybrid_search with
    ``vector_sim_floor=state.cfg.vector_sim_floor`` — full stop. This
    test asserts the kwarg is wired by spying on hybrid_search.
    """
    captured: dict[str, object] = {}

    def spy_hybrid_search(*_args: object, **kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vector_sim_floor=0.42),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    monkeypatch.setattr(mcp_server, "hybrid_search", spy_hybrid_search)

    mcp_server.brain_search(query="anything")

    assert captured.get("vector_sim_floor") == 0.42, (
        "MCP brain_search must forward state.cfg.vector_sim_floor to "
        "hybrid_search; got "
        f"{captured.get('vector_sim_floor')!r} instead."
    )


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
    payload = mcp_server.brain_search(query="company-id", source="krisp")
    results = payload["results"]
    assert results
    for r in results:
        assert r["source_kind"] == "krisp"
    titles = {r["title"] for r in results}
    assert titles == {"Krisp one", "Krisp two"}


class _BoomEmbedder:
    """Embedder stub that always raises a OllamaEmbedError on ``embed``.

    Used to assert each tool wraps embedder failures as ``McpError`` rather
    than letting a raw ``OllamaEmbedError`` propagate to the MCP runtime.

    ``dim`` is required by the Embedder Protocol; T2's query-embed LRU
    cache reads it to build its key before any ``embed`` call fires, so
    a stub without ``dim`` would AttributeError before the intended
    ``embed`` failure can be wrapped. Value matches the arctic default.
    """

    dim: int = 1024

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise OllamaEmbedError("rate limited")

    def count_tokens(self, text: str) -> int:
        return 1


def test_brain_search_wraps_embed_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    """An embedder failure must surface as McpError, never a raw OllamaEmbedError."""
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_search(query="anything")
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "OllamaEmbedError" in msg


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
    search_payload = mcp_server.brain_search(query="company-id")
    titles = {r["title"] for r in search_payload["results"]}
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


def test_brain_ingest_stdin_creates_vault_mirror(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — ensures fresh schema
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Invoking ``brain_ingest_stdin`` writes a mirror under
    ``state.cfg.vault_path / _ingested/<source>/``.

    Setup: install an ``mcp_server._state`` whose Config points at the test
    DB AND whose ``vault_path`` is sandboxed to ``tmp_path`` (so the mirror
    write doesn't touch the real ``~/brain-vault``).
    Exercise: call the MCP tool function directly.
    Verify: a single Markdown file lands under ``_ingested/slack/`` whose
    body contains the input content.
    """
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    payload = mcp_server.brain_ingest_stdin(
        content="Mirror this slack thread via MCP.\n",
        source="slack",
        external_id="slack:mirror:1",
        title="Slack mirror via MCP",
        content_type="transcript",
    )

    assert payload["created"] is True
    assert payload["document_id"] is not None
    mirror_dir = tmp_path / "_ingested" / "slack"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    assert "Mirror this slack thread via MCP" in mirrors[0].read_text(
        encoding="utf-8"
    )


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


def test_brain_tag_mcp_writes_file_after_ingest(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — ensures fresh schema
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Regression for bug #2 via MCP: ``brain_tag`` writes to the file too.

    Mirrors the CLI test ``test_brain_tag_writes_file_after_ingest`` end-to-end
    via the MCP tool surface. After ``brain_ingest_stdin`` materializes the
    on-disk mirror AND populates ``documents.vault_path`` (the fix in
    ``regenerate_vault_file``), ``brain_tag`` must update the file's
    frontmatter ``tags:`` so a subsequent ``brain vault sync`` doesn't
    overwrite the DB tag list with stale ``tags: []`` from disk.

    Pattern lifted from ``test_brain_ingest_stdin_creates_vault_mirror``:
    build a ``_State`` with ``Config(vault_path=tmp_path)`` so the mirror
    write lands in the sandboxed test directory.
    """
    # Setup — install state pointing the vault at ``tmp_path``.
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    ingest_payload = mcp_server.brain_ingest_stdin(
        content="MCP path-tracker body.\n",
        source="manual",
        external_id="mcp-tag-mirror-1",
        title="MCP tag smoke",
        content_type="transcript",
    )
    assert ingest_payload["created"] is True
    doc_id = ingest_payload["document_id"]
    assert doc_id is not None

    mirror_path = tmp_path / "_ingested" / "manual" / "mcp-tag-smoke.md"
    assert mirror_path.is_file(), f"missing mirror at {mirror_path}"
    # Precondition: the freshly-written file carries the ``source-mcp`` auto
    # tag (added by ``brain_ingest_stdin``), but NOT the new tag we're about
    # to add. The latter is what the post-tag verify step keys off.
    fields_before, _ = parse_frontmatter(mirror_path.read_text(encoding="utf-8"))
    assert "mcp-new-tag" not in (fields_before.get("tags") or [])

    # Exercise — apply a tag via the MCP tool.
    tag_payload = mcp_server.brain_tag(
        id_prefix=str(doc_id)[:8], add=["mcp-new-tag"]
    )

    # Verify — payload tags include the new tag, and the on-disk frontmatter
    # mirrors the DB. Without the path-tracking fix the file would still
    # be missing ``mcp-new-tag`` (the same gap the CLI test #5 covers).
    assert "mcp-new-tag" in tag_payload["tags"]
    fields_after, _ = parse_frontmatter(mirror_path.read_text(encoding="utf-8"))
    assert "mcp-new-tag" in (fields_after.get("tags") or []), (
        "MCP brain_tag must write to the on-disk frontmatter once "
        "vault_path is populated by the ingest-time regenerate_vault_file call"
    )


def test_brain_tag_mcp_missing_mirror_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema fresh
    fake_embedder: object,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A populated ``vault_path`` whose mirror is gone → warn + DB-only success.

    The MCP ``brain_tag`` tool intentionally does NOT expose a
    ``--regenerate-file`` equivalent (recovery is the CLI's job). When the
    on-disk mirror is missing for a doc that *should* have one, the tool
    emits a WARNING via the standard logger so the MCP caller (Claude) can
    surface it, and lets the DB update stand — exit code 0, payload still
    reflects the new tag list. This test pins both the DB-update success
    contract and the warning emission so a regression that silently drops
    the warning fails here.
    """
    # Setup — sandbox the vault to ``tmp_path`` so the ingest writes its
    # mirror into the test directory and populates ``documents.vault_path``.
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    ingest_payload = mcp_server.brain_ingest_stdin(
        content="MCP missing-mirror body.\n",
        source="manual",
        external_id="mcp-tag-missing-1",
        title="MCP tag missing smoke",
        content_type="transcript",
    )
    doc_id = ingest_payload["document_id"]
    assert doc_id is not None

    # Re-point ``vault_path`` at a relative path that does NOT exist on disk
    # (the original mirror still lives at the slug-derived path under
    # ``_ingested/manual/``; we want the writeback's existence check to
    # FAIL, so steer it at a path the ingest never wrote to).
    missing_rel = "_ingested/manual/missing.md"
    test_db.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s::uuid",
        (missing_rel, doc_id),
    )
    assert not (tmp_path / missing_rel).exists()  # precondition

    # Exercise — invoke brain_tag and capture WARNING-level log records.
    with caplog.at_level(logging.WARNING, logger="brain.mcp"):
        payload = mcp_server.brain_tag(
            id_prefix=str(doc_id)[:8], add=["still-applied"]
        )

    # Verify — payload reflects the new tag, AND the missing-mirror warning
    # was emitted with a phrase unique to that branch (so a regression that
    # drops the warning fails this test even if the success-suffix wording
    # drifts elsewhere).
    assert "still-applied" in payload["tags"]
    assert any(
        "mirror missing" in r.message for r in caplog.records
    ), (
        "MCP brain_tag must log a WARNING when vault_path is set but the "
        "on-disk mirror is gone; got: "
        f"{[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# brain_edit
# ---------------------------------------------------------------------------


def test_brain_edit_title_only_no_embed_call(
    test_db: psycopg.Connection,
    counting_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Title-only edit must not call the embedder."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
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
    # The original ingest must have produced at least one chunk — belt-and-
    # braces check that the rechunk path has something to replace.
    assert pre_chunks >= 1
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


def test_brain_edit_content_change_refreshes_summary(
    test_db: psycopg.Connection,
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex finding 1 follow-up — MCP-side regression.

    Mirrors the CLI ``brain edit --content-file`` smoke gap test, but
    exercises the ``brain_edit`` MCP tool. Verifies that when
    ``_State`` carries a non-None enricher, a content-changing edit
    refreshes ``documents.summary`` via the post-ingest hook (the
    Q2-SUMMARY-WIKI lede otherwise renders the pre-edit summary above
    the new body).
    """
    from dataclasses import dataclass

    from brain.enrichment import SummaryResult

    @dataclass
    class _ScriptedEnricher:
        model: str = "fake-mcp-test-model"
        summaries: tuple[str, ...] = (
            "MCP v1 summary about the original body.",
            "MCP v2 summary about the refreshed body.",
        )
        calls: int = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def summarize(self, title: str, content: str) -> SummaryResult:
            idx = self.calls
            self.calls += 1
            return SummaryResult(
                summary=self.summaries[idx], model=self.model
            )

    fake_enricher = _ScriptedEnricher()
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
        enricher=fake_enricher,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    doc_id = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="MCP summary refresh fixture",
            content="Original body content for the MCP refresh test. " * 20,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        enricher=fake_enricher,  # type: ignore[arg-type]
    ).document_id
    assert doc_id is not None

    summary_before_row = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_before_row is not None
    assert summary_before_row[0] == "MCP v1 summary about the original body."
    assert fake_enricher.calls == 1

    mcp_server.brain_edit(
        id_prefix=doc_id[:8],
        content="Refreshed body content for the MCP refresh test. " * 20,
    )

    summary_after_row = test_db.execute(
        "SELECT summary, summary_model FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_after_row is not None
    summary_after, model_after = summary_after_row
    assert summary_after == "MCP v2 summary about the refreshed body.", (
        "`brain_edit` MCP tool must refresh documents.summary on body "
        "change; without state.enricher wiring the hook hits the "
        "'no enricher supplied' skip and the wiki lede shows a stale "
        "summary above the new body"
    )
    assert model_after == "fake-mcp-test-model"
    assert fake_enricher.calls == 2


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


def test_brain_ingest_stdin_wraps_embed_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
) -> None:
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ingest_stdin(
            content="body that will need embedding",
            source="krisp",
            external_id="krisp:embed",
            title="t",
        )
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "OllamaEmbedError" in msg


def test_brain_edit_wraps_embed_error(
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
    assert "OllamaEmbedError" in msg


# ---------------------------------------------------------------------------
# main() — wire-up sanity (full subprocess coverage lives in
# tests/test_mcp_server_protocol.py)
# ---------------------------------------------------------------------------


class _RecordingEmbedder:
    """Stand-in for ``Qwen3Embedder`` used by ``main()`` lifecycle tests.

    Records every ``embed`` invocation so warmup can be asserted, and avoids
    hitting a real Ollama server when ``main()`` runs offline.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.embed_calls = 0
        self.embed_inputs: list[tuple[list[str], str]] = []

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        self.embed_inputs.append((list(texts), input_type))
        return [[0.0] * 4096 for _ in texts]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class _BoomEmbedderFactory(_RecordingEmbedder):
    """Variant whose ``embed`` raises a ``OllamaEmbedError`` — used to verify
    the warmup failure path doesn't abort startup."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        raise OllamaEmbedError("simulated cold start failure")


class _ColdStartEmbedder(_RecordingEmbedder):
    """Variant whose first ``embed`` call raises ``OllamaEmbedError`` (Ollama
    daemon up but model still loading) and whose second call succeeds — used to
    verify the warmup-retry path covers the 2026-05-13 cold-boot race."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        self.embed_inputs.append((list(texts), input_type))
        if self.embed_calls == 1:
            raise OllamaEmbedError("simulated cold start, model still loading")
        return [[0.0] * 4096 for _ in texts]


class _NonOllamaErrorEmbedder(_RecordingEmbedder):
    """Variant whose ``embed`` raises a ``RuntimeError`` — used to verify the
    warmup block's narrow ``except OllamaEmbedError`` does NOT swallow other
    exception classes (import / programming errors must propagate so the
    operator notices)."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        raise RuntimeError("simulated non-Ollama failure")


def _install_main_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedder_cls: type[_RecordingEmbedder],
) -> dict[str, object]:
    """Set env, monkeypatch ``make_embedder`` to ``embedder_cls``, replace
    ``mcp_app.run`` with a no-op recorder, and clear ``_state``.

    Returns a ``captured`` dict that ``_fake_run`` populates so the caller
    can assert on transport + state after ``main()`` returns.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(mcp_server, "_state", None)
    monkeypatch.setattr(mcp_server, "make_embedder", embedder_cls)

    captured: dict[str, object] = {}

    def _fake_run(transport: str = "stdio") -> None:
        captured["transport"] = transport
        captured["state"] = mcp_server._state

    monkeypatch.setattr(mcp_server.mcp_app, "run", _fake_run)
    return captured


def test_main_initializes_state_and_starts_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() must build _State from env and hand off to mcp_app.run(stdio)."""
    captured = _install_main_doubles(
        monkeypatch, embedder_cls=_RecordingEmbedder
    )
    mcp_server.main()
    assert captured["transport"] == "stdio"
    assert isinstance(captured["state"], mcp_server._State)
    assert captured["state"].cfg.database_url == TEST_DATABASE_URL  # type: ignore[union-attr]


def test_main_runs_warmup_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() must fire one warmup embed call before mcp_app.run() to cut
    cold-start latency on the first real brain_search."""
    captured = _install_main_doubles(
        monkeypatch, embedder_cls=_RecordingEmbedder
    )
    mcp_server.main()
    state = captured["state"]
    assert isinstance(state, mcp_server._State)
    embedder = state.embedder
    assert isinstance(embedder, _RecordingEmbedder)
    assert embedder.embed_calls >= 1
    # Warmup uses a document-style embed (matches what brain_search would do).
    texts, input_type = embedder.embed_inputs[0]
    assert input_type == "document"
    assert texts == ["hello"]


def test_main_continues_when_warmup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If both warmup attempts raise ``OllamaEmbedError``, main() must still
    hand off to ``mcp_app.run()`` so the server stays up and search can retry
    on demand. The warning still names the exception class."""
    captured = _install_main_doubles(
        monkeypatch, embedder_cls=_BoomEmbedderFactory
    )
    sleeps: list[float] = []
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: sleeps.append(s))
    with caplog.at_level("WARNING", logger="brain.mcp"):
        mcp_server.main()
    # Server still started despite both warmup attempts blowing up.
    assert captured["transport"] == "stdio"
    state = captured["state"]
    assert isinstance(state, mcp_server._State)
    embedder = state.embedder
    assert isinstance(embedder, _BoomEmbedderFactory)
    # Initial attempt + one bounded retry, both raised.
    assert embedder.embed_calls == 2
    assert sleeps == [mcp_server._WARMUP_RETRY_DELAY_SECONDS]
    # And we logged a warning naming the exception class so an operator can
    # see why warmup didn't take.
    assert any(
        "warmup embed failed" in rec.message and "OllamaEmbedError" in rec.message
        for rec in caplog.records
    )


def test_main_warmup_retries_after_cold_start(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the first warmup embed fails (cold-boot race: Ollama daemon up but
    model still loading) and the retry succeeds, main() must log
    ``warmup embed completed (after retry)`` and emit no warning."""
    captured = _install_main_doubles(
        monkeypatch, embedder_cls=_ColdStartEmbedder
    )
    sleeps: list[float] = []
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: sleeps.append(s))
    with caplog.at_level("INFO", logger="brain.mcp"):
        mcp_server.main()
    assert captured["transport"] == "stdio"
    state = captured["state"]
    assert isinstance(state, mcp_server._State)
    embedder = state.embedder
    assert isinstance(embedder, _ColdStartEmbedder)
    # Initial failure + one successful retry.
    assert embedder.embed_calls == 2
    # We slept exactly once between attempts.
    assert sleeps == [mcp_server._WARMUP_RETRY_DELAY_SECONDS]
    # Retry succeeded — no "failed (continuing without)" warning.
    assert not any(
        "warmup embed failed" in rec.message for rec in caplog.records
    )
    # Retry-success path emits the distinct INFO line so cold-boot races stay
    # observable in operator logs.
    assert any(
        "warmup embed completed (after retry)" in rec.message
        for rec in caplog.records
    )


def test_main_warmup_does_not_swallow_non_ollama_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-``OllamaEmbedError`` exceptions during warmup must propagate so
    import / programming errors surface to the operator instead of being
    silently retried-and-warned-about. Locks in the narrow exception scope."""
    _install_main_doubles(
        monkeypatch, embedder_cls=_NonOllamaErrorEmbedder
    )
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="simulated non-Ollama failure"):
        mcp_server.main()
