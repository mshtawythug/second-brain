"""MCP ``brain_explain`` (RM-12): why a search ranked what it did, for an agent.

* AC4 — per result, ``explain`` is the object ``brain explain --json`` prints for the same
  query and corpus.
* The lens excludes confidential documents from the MATCH SET by default (the oracle-closing
  kind), and the opt-in brings them back.
* AC5 — ``limit`` above ``BRAIN_SEARCH_MAX_LIMIT`` (the ceiling ``brain_search`` enforces for the
  same pipeline) is ``INVALID_PARAMS``.

All data is synthetic.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.format_search import search_results_json
from brain.ingest import ExtractedDoc, ingest_document
from brain.mcp_compat import MCPError
from brain.mcp_tools import related as related_tool
from brain.search_fallback import fallback_json
from brain.search_pipeline import stage_reports_json
from brain.sensitivity import CONFIDENTIAL
from tests.embed_down import SECRET_DETAIL, QueryEmbedDownEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: The keys of one result: ``search_results_json``'s row minus ``snippet``, plus ``explain``.
RESULT_KEYS = ["id", "title", "source_kind", "score", "content_type", "tags", "explain"]

#: Explain fields that read the clock (``now - recency_ts``), compared approximately.
CLOCK_FIELDS = {"recency_age_days", "recency_boost", "final_score"}


def _ingest(conn: psycopg.Connection[Any], embedder: Any, title: str, body: str) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title, content=body, content_type="note", source_path=None, metadata={}
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
        tags=["field"],
    )
    return str(result.document_id)


def _seed(conn: psycopg.Connection[Any], embedder: Any) -> dict[str, str]:
    """Three notes that all match 'moraine'; ``secret`` is made confidential by the caller."""
    return {
        "first": _ingest(conn, embedder, "Moraine survey", "The moraine survey mapped ridges."),
        "second": _ingest(conn, embedder, "Moraine notes", "Moraine notes on glacial till."),
        "secret": _ingest(conn, embedder, "Moraine private", "A private moraine assessment."),
    }


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the data fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _error_code(excinfo: pytest.ExceptionInfo[BaseException]) -> int:
    return int(excinfo.value.error.code)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ shape --


def test_explain_returns_ranked_rows_with_explanations_and_pipeline_keys(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State, fake_embedder: Any
) -> None:
    # Arrange
    ids = _seed(test_db, fake_embedder)

    # Act
    payload = related_tool.brain_explain(query="moraine")

    # Assert
    no_fallback = fallback_json(None)
    assert list(payload) == ["query", "results", *no_fallback, "stages"]
    assert payload["query"] == "moraine"
    assert {key: payload[key] for key in no_fallback} == no_fallback
    assert payload["stages"] == stage_reports_json(())["stages"]
    rows = payload["results"]
    assert {row["id"] for row in rows} == set(ids.values())
    for row in rows:
        assert list(row) == RESULT_KEYS
        assert "snippet" not in row
        assert row["explain"]["final_score"] == pytest.approx(row["score"])
        assert row["explain"]["matched_filters"]["source_kind"] is None


def test_results_without_snippet_equal_the_search_projection(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The six non-explain keys are ``search_results_json``'s, so the two cannot drift."""
    # Arrange
    _seed(test_db, fake_embedder)
    seen: list[Any] = []
    real = related_tool.hybrid_search

    def spy(*args: Any, **kwargs: Any) -> Any:
        results = real(*args, **kwargs)
        seen.extend(results)
        return results

    monkeypatch.setattr(related_tool, "hybrid_search", spy)

    # Act
    rows = related_tool.brain_explain(query="moraine")["results"]

    # Assert
    expected = [
        {key: value for key, value in base.items() if key != "snippet"}
        for base in search_results_json(seen)
    ]
    assert [{k: v for k, v in row.items() if k != "explain"} for row in rows] == expected


# ------------------------------------------------------ AC4: CLI parity --


def test_each_explain_object_equals_the_cli_json(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: the CLI and the tool read the same configuration.
    ids = _seed(test_db, fake_embedder)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    monkeypatch.setattr(mcp_state, "cfg", Config.load())

    # Act
    result = CliRunner().invoke(app, ["explain", "moraine", "--json"])
    rows = related_tool.brain_explain(query="moraine", include_confidential=True)["results"]

    # Assert
    assert result.exit_code == 0, result.output
    cli = {row["id"]: row["explain"] for row in json.loads(result.output)}
    mcp = {row["id"]: row["explain"] for row in rows}
    assert set(mcp) == set(cli) == set(ids.values())
    for doc_id, cli_explain in cli.items():
        mcp_explain = mcp[doc_id]
        assert list(mcp_explain) == list(cli_explain)
        for key, value in cli_explain.items():
            if key in CLOCK_FIELDS:
                assert mcp_explain[key] == pytest.approx(value, rel=1e-3, abs=1e-3), key
            else:
                assert mcp_explain[key] == value, key


# --------------------------------------------------------- the F6 lens --


@pytest.mark.parametrize(("include_confidential", "present"), [(False, False), (True, True)])
def test_a_confidential_document_needs_the_opt_in(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    fake_embedder: Any,
    include_confidential: bool,
    present: bool,
) -> None:
    # Arrange
    ids = _seed(test_db, fake_embedder)
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid", (CONFIDENTIAL, ids["secret"])
    )

    # Act
    payload = related_tool.brain_explain(
        query="moraine", include_confidential=include_confidential
    )

    # Assert
    returned = {row["id"] for row in payload["results"]}
    assert (ids["secret"] in returned) is present
    assert ids["first"] in returned
    assert ("Moraine private" in json.dumps(payload)) is present


def test_the_lens_is_a_predicate_not_a_redaction(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``sensitivity`` filter reaches the search itself, so the row never exists."""
    # Arrange
    _seed(test_db, fake_embedder)
    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(related_tool, "hybrid_search", spy)

    # Act
    related_tool.brain_explain(query="moraine")
    related_tool.brain_explain(query="moraine", include_confidential=True)

    # Assert
    assert [call["sensitivity"] for call in calls] == ["normal", None]
    assert all(call["explain"] is True for call in calls)


# ---------------------------------------------------------------- filters --


def test_filters_and_config_reach_the_search(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(related_tool, "hybrid_search", spy)
    cfg = mcp_state.cfg

    # Act
    payload = related_tool.brain_explain(
        query="moraine", limit=4, source="krisp", tag="field", since_days=7
    )

    # Assert
    assert payload["results"] == []
    (call,) = calls
    assert call["query"] == "moraine"
    assert call["limit"] == 4
    assert call["source_kind"] == "krisp"
    assert call["tag"] == "field"
    assert call["since_days"] == 7
    assert call["vector_sim_floor"] == cfg.vector_sim_floor
    assert call["recency_halflife_days"] == cfg.recency_halflife_days
    assert call["snippet_context_tokens"] == cfg.snippet_context_tokens
    assert call["snippet_max_chars"] == cfg.snippet_max_chars


def test_an_unknown_source_is_invalid_params(mcp_state: mcp_server._State) -> None:
    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_explain(query="moraine", source="carrier-pigeon")

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS


# ------------------------------------------------------------- AC5: limit --


def test_limit_above_the_search_ceiling_is_invalid_params(mcp_state: mcp_server._State) -> None:
    # Arrange
    ceiling = mcp_state.cfg.search_max_limit

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_explain(query="moraine", limit=ceiling + 1)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS
    assert str(ceiling) in str(excinfo.value)


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_a_non_positive_limit_is_invalid_params(
    mcp_state: mcp_server._State, bad_limit: int
) -> None:
    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_explain(query="moraine", limit=bad_limit)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS


@pytest.mark.parametrize("bad_since_days", [0, -1])
def test_a_non_positive_since_days_is_invalid_params_before_searching(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch, bad_since_days: int
) -> None:
    """REGRESSION: ``0`` meant "no window" while ``matched_filters`` echoed it; ``-1`` matched
    nothing. Both are now refused before the search runs."""
    # Arrange
    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(related_tool, "hybrid_search", spy)

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_explain(query="moraine", since_days=bad_since_days)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS
    assert "since_days" in str(excinfo.value)
    assert calls == []


def test_the_default_limit_is_the_clis(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    limits: list[int] = []

    def spy(*args: Any, **kwargs: Any) -> list[Any]:
        limits.append(kwargs["limit"])
        return []

    monkeypatch.setattr(related_tool, "hybrid_search", spy)

    # Act
    related_tool.brain_explain(query="moraine")

    # Assert
    assert limits == [10]


# ----------------------------------------------------------------- errors --


def test_a_failed_query_embed_falls_back_and_says_so(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    ids = _seed(test_db, fake_embedder)
    monkeypatch.setattr(
        mcp_state, "embedder", QueryEmbedDownEmbedder(fake_embedder, transient=True)
    )

    # Act
    payload = related_tool.brain_explain(query="moraine")

    # Assert
    assert {row["id"] for row in payload["results"]} == set(ids.values())
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    for row in payload["results"]:
        assert row["explain"]["vector_rank"] is None
    assert SECRET_DETAIL not in json.dumps(payload)


def test_a_database_error_is_class_only_internal_error(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    secret = "SELECT secret_detail FROM nowhere"

    def failing(*args: Any, **kwargs: Any) -> list[Any]:
        raise psycopg.errors.UndefinedTable(secret)

    monkeypatch.setattr(related_tool, "hybrid_search", failing)

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_explain(query="moraine")

    # Assert
    assert _error_code(excinfo) == INTERNAL_ERROR
    assert "UndefinedTable" in str(excinfo.value)
    assert secret not in str(excinfo.value)
