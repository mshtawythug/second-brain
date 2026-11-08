"""``brain search --rewrite`` and ``brain explain --rewrite`` on real Postgres (RM-54).

The real ``hybrid_search``, the real CLI and the real ``chat.chat_json`` run here; only the HTTP
under ``chat`` is a ``MockTransport``, through ``chat._build_client``. These tests pass
``--fts-only`` wherever ranks are asserted, because ``FakeEmbedder``'s hashed vectors have arbitrary
cosines, so the vector leg would add hits that have nothing to do with the words. All data is
synthetic.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from typer.testing import CliRunner

from brain import chat
from brain.cli import app
from brain.db import connect
from brain.embeddings import OllamaEmbedError
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import _cached_query_embed, hybrid_search
from brain.search_pipeline import run_search
from tests.conftest import TEST_DATABASE_URL
from tests.embed_down import QueryEmbedDownEmbedder

ORIGINAL_TERM = "gorsemantle"
PARAPHRASE_TERM = "furzeledger"
SECOND_TERM = "brackenroll"
SKIPPED = "query rewrite skipped"


class ChatStub:
    """The HTTP under ``chat``: counts requests and answers with ``handler``."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.read()))
        return self.handler(request)


def _reply(*paraphrases: object) -> Callable[[httpx.Request], httpx.Response]:
    content = json.dumps({"paraphrases": list(paraphrases)})
    return lambda _r: httpx.Response(200, json={"message": {"content": content}})


@pytest.fixture
def install_chat(monkeypatch: pytest.MonkeyPatch) -> Callable[..., ChatStub]:
    def _install(handler: Callable[[httpx.Request], httpx.Response]) -> ChatStub:
        stub = ChatStub(handler)
        monkeypatch.setattr(
            chat,
            "_build_client",
            lambda host, timeout: httpx.Client(
                base_url=host, timeout=timeout, transport=httpx.MockTransport(stub)
            ),
        )
        return stub

    return _install


def _seed(
    conn: psycopg.Connection[Any],
    embedder: Any,
    key: str,
    term: str,
    *,
    sensitivity: str = "normal",
    source_kind: str = "manual",
    tags: list[str] | None = None,
) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=f"Note {key}",
            content=f"The {term} register for {key} is on the shelf.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=f"{source_kind}:{key}",
        tags=tags or [],
        sensitivity=sensitivity,
    )
    return str(result.document_id)


@pytest.fixture
def corpus(
    test_db: psycopg.Connection[Any], fake_embedder: Any, patch_embedder: Any
) -> dict[str, str]:
    patch_embedder(fake_embedder)
    return {
        "original": _seed(test_db, fake_embedder, "original", ORIGINAL_TERM),
        "paraphrase": _seed(test_db, fake_embedder, "paraphrase", PARAPHRASE_TERM),
    }


def _search_titles(*extra: str) -> tuple[Any, list[str]]:
    result = CliRunner().invoke(
        app, ["search", ORIGINAL_TERM, "--json", "--fts-only", "--no-meta", *extra]
    )
    assert result.exit_code == 0, result.output
    return result, [row["title"] for row in json.loads(result.stdout)]


# --- AC1: off unless asked -----------------------------------------------------------------------


def test_without_the_flag_the_chat_is_never_called(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    stub = install_chat(_reply(PARAPHRASE_TERM))

    # Act
    _, titles = _search_titles()
    explained = CliRunner().invoke(app, ["explain", ORIGINAL_TERM, "--json", "--fts-only"])

    # Assert
    assert stub.requests == []
    assert titles == ["Note original"]
    assert explained.exit_code == 0, explained.output
    assert "found_by" not in explained.stdout
    assert "fused_score" not in explained.stdout


def test_with_the_flag_the_chat_is_called_once_and_paraphrases_fuse(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    stub = install_chat(_reply(PARAPHRASE_TERM))

    # Act
    result, titles = _search_titles("--rewrite")

    # Assert
    assert len(stub.requests) == 1
    assert ORIGINAL_TERM in stub.requests[0]["messages"][0]["content"]
    assert stub.requests[0]["options"]["num_predict"] == 256
    assert titles == ["Note original", "Note paraphrase"]
    assert SKIPPED not in result.stderr


def test_the_bare_json_keeps_its_seven_keys_under_the_flag(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    result, _ = _search_titles("--rewrite")

    # Assert
    assert {tuple(sorted(row)) for row in json.loads(result.stdout)} == {
        ("content_type", "id", "score", "snippet", "source_kind", "tags", "title")
    }


def test_the_footer_and_envelope_report_the_leg(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    human = CliRunner().invoke(app, ["search", ORIGINAL_TERM, "--fts-only", "--rewrite"])
    meta = CliRunner().invoke(
        app, ["search", ORIGINAL_TERM, "--fts-only", "--rewrite", "--json", "--meta"]
    )

    # Assert
    assert " rewrite " in human.stderr
    stage = json.loads(meta.stdout)["stages"][0]
    assert (stage["name"], stage["applied"], stage["skipped_reason"]) == ("rewrite", True, None)


# --- AC3: failure paths --------------------------------------------------------------------------


def _raises(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _status(code: int, body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(code, json={"error": body})


def _content(text: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, json={"message": {"content": text}})


SECRET_REPLY = "secretreplytext"


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_raises(httpx.ConnectError("refused")), "OllamaUnavailable"),
        (_raises(httpx.ReadTimeout("slow")), "OllamaUnavailable"),
        (_status(500, SECRET_REPLY), "OllamaUnavailable"),
        (_status(400, f"{SECRET_REPLY} exceeds the context size"), "OllamaContextOverflow"),
        (_status(404, f"model {SECRET_REPLY} not found"), "EnrichmentError"),
        (_content(f"not json {SECRET_REPLY}"), "EnrichmentError"),
        (_content('{"paraphrases": ["unterminated ' + SECRET_REPLY), "ChatOutputTruncated"),
        (_content('{"other": "' + SECRET_REPLY + '"}'), "EnrichmentError"),
    ],
)
def test_a_failed_chat_is_the_original_search_a_notice_and_exit_0(
    corpus: dict[str, str],
    install_chat: Callable[..., ChatStub],
    handler: Callable[[httpx.Request], httpx.Response],
    reason: str,
) -> None:
    # Arrange
    install_chat(handler)

    # Act
    result, titles = _search_titles("--rewrite")

    # Assert
    assert titles == ["Note original"]
    assert SKIPPED in result.stderr
    assert reason in result.stderr
    assert SECRET_REPLY not in result.output
    assert ORIGINAL_TERM not in result.stderr


def test_a_failed_chat_keeps_explains_shape(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(_raises(httpx.ConnectError("refused")))

    # Act
    result = CliRunner().invoke(
        app, ["explain", ORIGINAL_TERM, "--json", "--fts-only", "--rewrite"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [row["title"] for row in rows] == ["Note original"]
    assert rows[0]["found_by"] == [{"query": ORIGINAL_TERM, "is_original": True, "rank": 1}]


def test_a_real_refused_connection_is_a_notice(
    corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: no stub at all, the real transport against a closed port.
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")

    # Act
    result, titles = _search_titles("--rewrite")

    # Assert
    assert titles == ["Note original"]
    assert SKIPPED in result.stderr


# --- AC4: explain names the query ----------------------------------------------------------------


def test_explain_json_names_the_query_that_found_each_result(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    result = CliRunner().invoke(
        app, ["explain", ORIGINAL_TERM, "--json", "--fts-only", "--rewrite"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    rows = {row["title"]: row for row in json.loads(result.stdout)}
    assert rows["Note original"]["found_by"] == [
        {"query": ORIGINAL_TERM, "is_original": True, "rank": 1}
    ]
    assert rows["Note paraphrase"]["found_by"] == [
        {"query": PARAPHRASE_TERM, "is_original": False, "rank": 1}
    ]
    assert rows["Note original"]["fused_score"] == pytest.approx(1.0 / 61)
    assert rows["Note paraphrase"]["fused_score"] == pytest.approx(0.5 / 61)


def test_explain_table_names_the_paraphrase(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    result = CliRunner().invoke(app, ["explain", ORIGINAL_TERM, "--fts-only", "--rewrite"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "Found by" in result.stdout
    assert PARAPHRASE_TERM in result.stdout
    assert "original #1" in result.stdout


def test_a_closing_tag_in_a_paraphrase_does_not_crash_the_table(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange: full-text search ignores the tag, so this paraphrase really finds a document.
    install_chat(_reply(f"{PARAPHRASE_TERM} [/]"))

    # Act
    result = CliRunner().invoke(app, ["explain", ORIGINAL_TERM, "--fts-only", "--rewrite"])

    # Assert
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert f"{PARAPHRASE_TERM} [/]" in result.stdout


def test_escape_sequences_in_a_paraphrase_never_reach_the_terminal(
    corpus: dict[str, str], install_chat: Callable[..., ChatStub]
) -> None:
    # Arrange
    install_chat(
        _reply(f"{PARAPHRASE_TERM}\x1b]8;;http://bad.example\x07\x1b[31m\x9b\u202e\ud800")
    )

    # Act
    table = CliRunner().invoke(app, ["explain", ORIGINAL_TERM, "--fts-only", "--rewrite"])
    as_json = CliRunner().invoke(
        app, ["explain", ORIGINAL_TERM, "--fts-only", "--rewrite", "--json"]
    )

    # Assert
    assert table.exit_code == 0, table.output
    assert as_json.exit_code == 0, as_json.output
    for output in (table.output, as_json.output):
        for banned in ("\x1b", "\x9b", "\u202e", "bad.example"):
            assert banned not in output
    assert PARAPHRASE_TERM in table.stdout


# --- filters and confidential documents, every leg ---------------------------------------------


def test_a_confidential_document_matching_only_a_paraphrase_stays_out_under_the_lens(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
    install_chat: Callable[..., ChatStub],
) -> None:
    # Arrange
    patch_embedder(fake_embedder)
    _seed(test_db, fake_embedder, "original", ORIGINAL_TERM)
    _seed(test_db, fake_embedder, "secret", PARAPHRASE_TERM, sensitivity="confidential")
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    _, lensed = _search_titles("--rewrite", "--sensitivity", "normal")
    _, unlensed = _search_titles("--rewrite")

    # Assert: the paraphrase finds it, and the lens still removes it.
    assert lensed == ["Note original"]
    assert unlensed == ["Note original", "Note secret"]


@pytest.mark.parametrize(
    ("seed_kwargs", "flags"),
    [
        ({"source_kind": "krisp"}, ("--source", "manual")),
        ({"tags": ["blocked"]}, ("--without-tag", "blocked")),
    ],
)
def test_a_filter_excludes_a_document_on_the_paraphrase_leg(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
    install_chat: Callable[..., ChatStub],
    seed_kwargs: dict[str, Any],
    flags: tuple[str, ...],
) -> None:
    # Arrange
    patch_embedder(fake_embedder)
    _seed(test_db, fake_embedder, "original", ORIGINAL_TERM)
    _seed(test_db, fake_embedder, "filtered", PARAPHRASE_TERM, **seed_kwargs)
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    _, filtered = _search_titles("--rewrite", *flags)
    _, unfiltered = _search_titles("--rewrite")

    # Assert
    assert filtered == ["Note original"]
    assert unfiltered == ["Note original", "Note filtered"]


# --- the original's records --------------------------------------------------------------------


def test_only_the_original_query_is_logged(
    test_db: psycopg.Connection[Any],
    corpus: dict[str, str],
    install_chat: Callable[..., ChatStub],
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    _search_titles("--rewrite")

    # Assert
    rows = test_db.execute("SELECT query, result_count FROM search_queries").fetchall()
    assert rows == [(ORIGINAL_TERM, 2)]


def test_a_lexical_miss_stays_a_gap_when_a_paraphrase_finds_something(
    test_db: psycopg.Connection[Any],
    corpus: dict[str, str],
    install_chat: Callable[..., ChatStub],
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))
    missing = "nosuchwordanywhere"

    # Act
    result = CliRunner().invoke(
        app, ["search", missing, "--json", "--meta", "--fts-only", "--rewrite"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [row["title"] for row in payload["results"]] == ["Note paraphrase"]
    assert payload["total_documents"] == 0
    rows = test_db.execute("SELECT query, fts_count FROM search_queries").fetchall()
    assert rows == [(missing, 0)]


def test_paraphrases_are_not_logged(
    corpus: dict[str, str],
    install_chat: Callable[..., ChatStub],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    with caplog.at_level(logging.DEBUG, logger="brain"):
        _search_titles("--rewrite")

    # Assert
    assert PARAPHRASE_TERM not in caplog.text


# --- a failed paraphrase, a dead embedder --------------------------------------------------------


def test_a_failed_paraphrase_leaves_a_non_autocommit_transaction_usable(
    test_db: psycopg.Connection[Any],
    corpus: dict[str, str],
    fake_embedder: Any,
) -> None:
    # Arrange: ``brain explain``'s connection shape.
    def search_fn(conn: psycopg.Connection[Any], **kwargs: Any) -> list[Any]:
        if kwargs["query"] == "bad":
            conn.execute("SELECT 1/0")
        return hybrid_search(conn, **kwargs)  # type: ignore[no-any-return]

    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        assert conn.autocommit is False

        # Act
        outcome = run_search(
            search_fn,
            conn,
            embedder=fake_embedder,
            query=ORIGINAL_TERM,
            limit=5,
            stages=(),
            fts_only=True,
            rewriter=lambda _q: ["bad", PARAPHRASE_TERM],
        )

        # Assert
        assert [r.title for r in outcome.results] == ["Note original", "Note paraphrase"]
        assert outcome.stages[0].skipped_reason is not None
        assert "DivisionByZero" in outcome.stages[0].skipped_reason
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_a_dead_embedder_costs_one_failed_embed_not_three(
    test_db: psycopg.Connection[Any],
    corpus: dict[str, str],
    fake_embedder: Any,
    patch_embedder: Any,
    install_chat: Callable[..., ChatStub],
) -> None:
    # Arrange
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)
    patch_embedder(down)
    install_chat(_reply(PARAPHRASE_TERM))

    # Act
    result = CliRunner().invoke(app, ["search", ORIGINAL_TERM, "--json", "--rewrite", "--no-meta"])

    # Assert
    assert result.exit_code == 0, result.output
    assert down.query_calls == 1
    assert [row["title"] for row in json.loads(result.stdout)] == [
        "Note original",
        "Note paraphrase",
    ]
    assert "text-only (FTS) matches" in result.stderr


# --- paraphrases and the embedder ----------------------------------------------------------------


class _QueryEmbedder:
    """Records each query embed. From the ``fail_from``-th on, the embed fails transiently."""

    def __init__(self, inner: Any, *, fail_from: int | None = None) -> None:
        self._inner = inner
        self._fail_from = fail_from
        self.dim = inner.dim
        self.queries: list[str] = []

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if input_type != "query":
            return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]
        self.queries.extend(texts)
        if self._fail_from is not None and len(self.queries) >= self._fail_from:
            raise OllamaEmbedError("synthetic embed detail", transient=True)
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)  # type: ignore[no-any-return]


@pytest.fixture
def cold_embed_cache() -> None:
    """The query-embed LRU outlives a test, and a hit would hide an embed these tests count."""
    _cached_query_embed.cache_clear()


@pytest.mark.usefixtures("cold_embed_cache")
def test_paraphrases_get_the_vector_leg_on_a_healthy_search(
    corpus: dict[str, str],
    fake_embedder: Any,
    patch_embedder: Any,
    install_chat: Callable[..., ChatStub],
) -> None:
    # Arrange
    embedder = _QueryEmbedder(fake_embedder)
    patch_embedder(embedder)
    install_chat(_reply(PARAPHRASE_TERM, SECOND_TERM))

    # Act: no --fts-only here, unlike the rank tests above.
    result = CliRunner().invoke(app, ["search", ORIGINAL_TERM, "--rewrite", "--no-meta"])

    # Assert: one query embed each, for the original and the two paraphrases.
    assert result.exit_code == 0, result.output
    assert embedder.queries == [ORIGINAL_TERM, PARAPHRASE_TERM, SECOND_TERM]
    assert SKIPPED not in result.stderr
    assert "a paraphrase search failed" not in result.stderr


def _assert_the_vector_leg_reached_the_fusion(
    conn: psycopg.Connection[Any], embedder: Any
) -> None:
    """No document holds ``SECOND_TERM``, so a hit credited to it can only be the vector leg's.

    The floor is -1.0 because the leg keeps ``dist <= 1 - floor``. At hybrid_search's default 0.0 a
    negative cosine is dropped, and with hashed vectors whether one is negative is down to the hash.
    """
    outcome = run_search(
        hybrid_search,
        conn,
        embedder=embedder,
        query=ORIGINAL_TERM,
        limit=5,
        stages=(),
        vector_sim_floor=-1.0,
        rewriter=lambda _q: [SECOND_TERM],
    )

    report = outcome.stages[0]
    assert report.applied is True
    assert report.skipped_reason is None
    assert outcome.fused is not None
    assert any(hit.query == SECOND_TERM for doc in outcome.fused.values() for hit in doc.hits)


def test_a_paraphrases_vector_leg_reaches_the_fusion_in_autocommit(
    test_db: psycopg.Connection[Any], corpus: dict[str, str], fake_embedder: Any
) -> None:
    # ``brain search``'s connection shape: the per-paraphrase block is a real transaction.
    assert test_db.autocommit is True

    _assert_the_vector_leg_reached_the_fusion(test_db, fake_embedder)


def test_a_paraphrases_vector_leg_reaches_the_fusion_in_a_transaction(
    corpus: dict[str, str], fake_embedder: Any
) -> None:
    # ``brain explain``'s connection shape: the per-paraphrase block is a savepoint.
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False

        _assert_the_vector_leg_reached_the_fusion(conn, fake_embedder)


@pytest.mark.usefixtures("cold_embed_cache")
def test_a_paraphrases_own_embed_failure_drops_it_without_a_fallback(
    test_db: psycopg.Connection[Any],
    corpus: dict[str, str],
    fake_embedder: Any,
) -> None:
    # Arrange: the original's embed succeeds, and the paraphrase's fails.
    embedder = _QueryEmbedder(fake_embedder, fail_from=2)
    calls: list[tuple[str, bool]] = []

    def search_fn(conn: psycopg.Connection[Any], **kwargs: Any) -> list[Any]:
        calls.append((kwargs["query"], kwargs.get("fts_only", False)))
        return hybrid_search(conn, **kwargs)  # type: ignore[no-any-return]

    # Act
    outcome = run_search(
        search_fn,
        test_db,
        embedder=embedder,
        query=ORIGINAL_TERM,
        limit=5,
        stages=(),
        rewriter=lambda _q: [PARAPHRASE_TERM],
    )

    # Assert: dropped, with no text-only retry and no claim that the search fell back.
    assert calls == [(ORIGINAL_TERM, False), (PARAPHRASE_TERM, False)]
    assert outcome.fallback is None
    report = outcome.stages[0]
    assert report.applied is False
    assert report.skipped_reason == "OllamaEmbedError"
    assert "synthetic embed detail" not in (report.skipped_reason or "")
    assert all(hit.is_original for doc in outcome.fused.values() for hit in doc.hits)
