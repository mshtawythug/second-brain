"""`GET /api/search` falls back to text-only matching when the query embed fails.

Roadmap ticket RM-03 (spec ``docs/specs/2026-10-23-roadmap-completion-design.md``).
Before it, a down embedder made the Notes tab answer ``503
embedding_unavailable`` and show nothing. Now the route goes through
``brain.search_fallback`` like ``brain search`` and MCP ``brain_search`` do, and
returns the same four ``fallback*`` keys beside ``meta``.

Real Postgres (the 5434 test database) and the real ``hybrid_search``, wrapped
in a recording double so a test can see what the route asked for. The failing
embedders come from ``tests/embed_down.py``. All data is synthetic.
"""
from __future__ import annotations

import contextlib
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.embeddings import OllamaEmbedContextOverflow
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search
from brain.sensitivity import CONFIDENTIAL
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

#: How many synthetic notes match :data:`TERM`. More than any page size used
#: below, so a short page can only mean the route asked for too few rows.
SEEDED = 6

#: A nonsense term so nothing here depends on real content.
TERM = "glacier"

#: The four keys every surface carries (``brain.search_fallback.fallback_json``).
FALLBACK_KEYS = ("fallback", "fallback_reason", "fallback_transient", "fallback_note")


class RecordingSearch:
    """``hybrid_search``, with every call's kwargs kept for the assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: psycopg.Connection[Any], **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return hybrid_search(conn, **kwargs)


class SlowQueryEmbedDownEmbedder(QueryEmbedDownEmbedder):
    """A transient query-embed failure that first waits, like a timeout does."""

    DELAY_S = 0.3

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            time.sleep(self.DELAY_S)
        return super().embed(texts, input_type=input_type)


class OverflowEmbedder(QueryEmbedDownEmbedder):
    """A query longer than the embedder's context window."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            raise OllamaEmbedContextOverflow(f"input too long {SECRET_DETAIL}")
        return super().embed(texts, input_type=input_type)


@pytest.fixture
def ui_cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
    )


@pytest.fixture
def seeded(test_db: psycopg.Connection[Any], fake_embedder: Any) -> None:
    for i in range(SEEDED):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"Glacier note {i}",
                content=f"The {TERM} survey measured the ice front {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:ui-glacier-{i}",
            tags=["field"],
        )


def _client(
    conn: psycopg.Connection[Any],
    cfg: Config,
    embedder: Any,
    search: RecordingSearch,
    *,
    logging_enabled: bool = False,
    serve_confidential: bool = True,
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield conn

    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=search,
        allowed_origin=ORIGIN,
        logging_enabled=logging_enabled,
        serve_confidential_bodies=serve_confidential,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def _down(fake_embedder: Any, *, transient: bool = True) -> QueryEmbedDownEmbedder:
    return QueryEmbedDownEmbedder(fake_embedder, transient=transient)


# ------------------------------------------------------------ the fallback --


@pytest.mark.usefixtures("seeded")
def test_a_down_embedder_returns_text_only_results_and_says_so(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    """AC 1. This was 503 ``embedding_unavailable`` with no results."""
    search = RecordingSearch()
    client = _client(test_db, ui_cfg, _down(fake_embedder), search)

    response = client.get("/api/search", params={"q": TERM})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["results"], "a text-only search over matching notes found nothing"
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_transient"] is True
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_note"]
    assert "comes back when the embedder does" in payload["fallback_note"]
    # The meta line's `fts-only` marker reads this (results.js).
    assert payload["fts_only"] is True
    assert payload["timing_ms"]["embed"] is None
    # The retry is the text-only one; the first attempt was the hybrid one.
    assert [call.get("fts_only") for call in search.calls] == [False, True]


@pytest.mark.usefixtures("seeded")
def test_a_permanent_failure_points_at_brain_doctor(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    client = _client(
        test_db, ui_cfg, _down(fake_embedder, transient=False), RecordingSearch()
    )

    payload = client.get("/api/search", params={"q": TERM}).json()

    assert payload["fallback"] == "fts_only"
    assert payload["fallback_transient"] is False
    assert "brain doctor" in payload["fallback_note"]
    assert payload["results"]


@pytest.mark.usefixtures("seeded")
def test_a_context_overflow_says_shorten_the_query(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    client = _client(
        test_db, ui_cfg, OverflowEmbedder(fake_embedder, transient=False),
        RecordingSearch(),
    )

    payload = client.get("/api/search", params={"q": TERM}).json()

    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedContextOverflow"
    assert "BRAIN_OLLAMA_EMBED_NUM_CTX" in payload["fallback_note"]


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("transient", [True, False])
def test_the_exception_message_never_reaches_the_wire(
    test_db: psycopg.Connection[Any],
    ui_cfg: Config,
    fake_embedder: Any,
    transient: bool,
) -> None:
    """Class-only: ``describe_failure``, never the message (DoD item 6)."""
    client = _client(
        test_db, ui_cfg, _down(fake_embedder, transient=transient), RecordingSearch()
    )

    response = client.get("/api/search", params={"q": TERM})

    assert SECRET_DETAIL not in response.text


@pytest.mark.usefixtures("seeded")
def test_a_healthy_search_carries_four_null_keys(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    """Always present, so the client reads one shape (``null`` = no fallback)."""
    search = RecordingSearch()
    client = _client(test_db, ui_cfg, fake_embedder, search)

    payload = client.get("/api/search", params={"q": TERM}).json()

    assert {key: payload[key] for key in FALLBACK_KEYS} == dict.fromkeys(FALLBACK_KEYS)
    assert payload["fts_only"] is False
    assert len(search.calls) == 1


@pytest.mark.usefixtures("seeded")
def test_a_requested_fts_only_search_never_reports_a_fallback(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    """``fts_only`` asked for never embeds, so nothing fell back."""
    search = RecordingSearch()
    client = _client(test_db, ui_cfg, _down(fake_embedder), search)

    payload = client.get("/api/search", params={"q": TERM, "fts_only": "1"}).json()

    assert payload["fallback"] is None
    assert payload["fts_only"] is True
    assert len(search.calls) == 1


@pytest.mark.usefixtures("seeded")
def test_a_non_embed_error_is_not_swallowed(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    """Only ``EmbedError`` falls back; a bug still fails loudly."""
    search = RecordingSearch()
    client = _client(
        test_db, ui_cfg, BrokenEmbedder(fake_embedder, transient=True), search
    )

    with pytest.raises(RuntimeError, match="synthetic programming error"):
        client.get("/api/search", params={"q": TERM})
    assert len(search.calls) == 1


# ----------------------------------------------------- confidential lens --


@pytest.mark.usefixtures("seeded")
def test_a_fallen_back_search_still_excludes_a_confidential_match(
    test_db: psycopg.Connection[Any], fake_embedder: Any, ui_cfg: Config
) -> None:
    """The text-only retry keeps the default lens: a confidential match is absent.

    The route's ``sensitivity`` kwarg reaches the retry through the helper's
    pass-through, and that is the only thing keeping the row out — the
    membership oracle ``routes_search`` documents. The permissive control below
    proves the document DOES match the text-only query, so the absence is the
    lens and not a miss.
    """
    doc_id = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Glacier budget memo",
            content=f"The {TERM} budget memo is restricted.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:ui-glacier-confidential",
        tags=["field"],
    ).document_id
    test_db.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s", (CONFIDENTIAL, doc_id)
    )
    params = {"q": TERM, "limit": "50"}

    withholding = _client(
        test_db, ui_cfg, _down(fake_embedder), RecordingSearch(),
        serve_confidential=False,
    ).get("/api/search", params=params).json()
    permissive = _client(
        test_db, ui_cfg, _down(fake_embedder), RecordingSearch(),
        serve_confidential=True,
    ).get("/api/search", params=params).json()

    assert withholding["fallback"] == permissive["fallback"] == "fts_only"
    assert str(doc_id) in {r["id"] for r in permissive["results"]}, (
        "control: the confidential note must match the text-only query"
    )
    assert str(doc_id) not in {r["id"] for r in withholding["results"]}, (
        "a fallen-back search returned a confidential document under the "
        "default lens"
    )
    assert len(withholding["results"]) == SEEDED


# ----------------------------------------------------------------- paging --


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(("limit", "offset"), [(2, 0), (2, 2), (3, 3)])
def test_the_page_size_is_the_same_with_and_without_a_fallback(
    test_db: psycopg.Connection[Any],
    ui_cfg: Config,
    fake_embedder: Any,
    limit: int,
    offset: int,
) -> None:
    """AC 5: ``limit`` is passed once, as ``fetch_limit``, on both attempts."""
    params = {"q": TERM, "limit": str(limit), "offset": str(offset)}
    healthy = RecordingSearch()
    fallen = RecordingSearch()

    normal = _client(test_db, ui_cfg, fake_embedder, healthy).get(
        "/api/search", params=params
    ).json()
    text_only = _client(test_db, ui_cfg, _down(fake_embedder), fallen).get(
        "/api/search", params=params
    ).json()

    assert text_only["fallback"] == "fts_only"
    assert len(text_only["results"]) == len(normal["results"]) == limit
    assert text_only["limit"] == normal["limit"] == limit
    assert text_only["offset"] == normal["offset"] == offset
    # No double over-fetch: every attempt asked for offset + limit rows.
    assert [call["limit"] for call in healthy.calls + fallen.calls] == [
        offset + limit
    ] * 3
    assert all(call["query"] == TERM for call in healthy.calls + fallen.calls)


# -------------------------------------------------------------- telemetry --


@pytest.mark.usefixtures("seeded")
def test_telemetry_duration_covers_both_attempts(
    test_db: psycopg.Connection[Any], ui_cfg: Config, fake_embedder: Any
) -> None:
    """AC 4. The failed embed is the wait the reader sat through; it is counted."""
    embedder = SlowQueryEmbedDownEmbedder(fake_embedder, transient=True)
    client = _client(
        test_db, ui_cfg, embedder, RecordingSearch(), logging_enabled=True
    )
    term = f"{TERM} {uuid.uuid4().hex[:8]}"

    payload = client.get("/api/search", params={"q": term}).json()

    assert payload["fallback"] == "fts_only"
    row = test_db.execute(
        "SELECT source, duration_ms, fts_count FROM search_queries WHERE query = %s",
        (term,),
    ).fetchone()
    assert row is not None, "the fallen-back search was not recorded"
    source, duration_ms, fts_count = row
    assert source == "ui"
    assert duration_ms >= int(SlowQueryEmbedDownEmbedder.DELAY_S * 1000)
    # The diagnostics describe the text-only run, not the failed attempt.
    assert fts_count == payload["fts_count"]
    assert payload["timing_ms"]["total"] >= SlowQueryEmbedDownEmbedder.DELAY_S * 1000
