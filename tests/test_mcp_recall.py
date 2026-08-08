"""MCP ``brain_recall`` (F2) and ``agent_id`` on the search surface (F10).

The load-bearing assertion here is ``session_id IS NULL`` on the logged row.
The ``no_click`` gap detector flags any search with a session id that no later
open follows; a recall's *result is the content*, so an agent essentially
never calls ``brain_show`` afterwards. Minting a session id would make every
recall look like a search failure and poison ``brain gaps`` — quietly, and in
a way that would only show up as degraded gap-mining weeks later.

All fixture data is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from brain.mcp_compat import MCPError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=vault_dir,
            recall_budget_tokens=2000,
            recall_passage_tokens=120,
            recall_max_candidates=25,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


@pytest.fixture
def corpus(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    mcp_state: mcp_server._State,  # noqa: ARG001 — ordering
) -> None:
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    body = (
        "The platform migration review covered staffing, the runway, and the "
        "quarterly hiring plan in detail. "
    ) * 25
    for i in range(6):
        ingest_document(
            test_db,
            doc=ExtractedDoc(
                title=f"Platform Migration Review {i}",
                content=f"{body} Entry {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            embedder=fake_embedder,
            source_kind="manual",
            source_external_id=f"w4-mcp-recall-{i}",
        )


# ---------------------------------------------------------------------------
# The no_click contract
# ---------------------------------------------------------------------------


def test_recall_logs_with_a_null_session_id(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    """Load-bearing: a recall must stay invisible to the no_click detector."""
    mcp_server.brain_recall(query="platform migration staffing")

    row = test_db.execute(
        "SELECT session_id FROM search_queries WHERE query = %s",
        ("platform migration staffing",),
    ).fetchone()
    assert row is not None, "the recall must still be logged"
    assert row[0] is None, (
        "a session_id would make every recall look like a no_click search "
        "failure and poison brain gaps"
    )


def test_recall_preserves_the_lexical_miss_signal(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    """``fts_count = 0`` must still reach ``search_queries``.

    Suppressing session tracking must not suppress the zero-result signal —
    that is the half of gap-mining recall legitimately contributes to.
    """
    mcp_server.brain_recall(query="zzzz-no-such-term-anywhere")

    row = test_db.execute(
        "SELECT fts_count, result_count FROM search_queries WHERE query = %s",
        ("zzzz-no-such-term-anywhere",),
    ).fetchone()
    assert row is not None
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Shape and budget
# ---------------------------------------------------------------------------


def test_recall_returns_context_block_and_passages(corpus: None) -> None:
    payload = mcp_server.brain_recall(query="platform migration staffing")

    assert payload["context_block"].startswith("# recall:")
    assert payload["passages"]
    assert set(payload) >= {
        "context_block",
        "passages",
        "query",
        "budget_tokens",
        "used_tokens",
        "candidates_considered",
        "dropped",
        "truncated",
        "fts_count",
    }


def test_recall_mints_no_session_id_key(corpus: None) -> None:
    """Absent from the payload too, so no caller can pass one back."""
    payload = mcp_server.brain_recall(query="platform migration staffing")

    assert "session_id" not in payload


def test_recall_honours_an_explicit_budget(
    corpus: None, fake_embedder: Any
) -> None:
    payload = mcp_server.brain_recall(
        query="platform migration staffing", budget_tokens=500
    )

    assert fake_embedder.count_tokens(payload["context_block"]) <= 500
    assert payload["budget_tokens"] == 500


def test_recall_defaults_to_the_configured_budget(corpus: None) -> None:
    payload = mcp_server.brain_recall(query="platform migration staffing")

    assert payload["budget_tokens"] == 2000


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"budget_tokens": 0}, "budget_tokens"),
        ({"max_candidates": 0}, "max_candidates"),
    ],
)
def test_recall_rejects_non_positive_bounds(
    corpus: None, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(MCPError, match=message):
        mcp_server.brain_recall(query="anything", **kwargs)


def test_recall_filters_are_forwarded(
    test_db: psycopg.Connection[Any], corpus: None, fake_embedder: Any
) -> None:
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    ingest_document(
        test_db,
        doc=ExtractedDoc(
            title="Krisp Platform Call",
            content="platform migration staffing runway discussion " * 20,
            content_type="transcript",
            source_path=None,
            metadata={},
        ),
        embedder=fake_embedder,
        source_kind="krisp",
        source_external_id="w4-mcp-recall-krisp",
    )

    payload = mcp_server.brain_recall(
        query="platform migration staffing", source="krisp"
    )

    assert payload["passages"]
    assert {p["source_kind"] for p in payload["passages"]} == {"krisp"}


def test_recall_rejects_an_unknown_person(corpus: None) -> None:
    with pytest.raises(MCPError):
        mcp_server.brain_recall(
            query="platform migration", person="No Such Synthetic Person"
        )


def test_recall_records_the_agent(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    mcp_server.brain_recall(
        query="attributed recall", agent_id="research-agent"
    )

    row = test_db.execute(
        "SELECT agent_id FROM search_queries WHERE query = %s",
        ("attributed recall",),
    ).fetchone()
    assert row is not None
    assert row[0] == "research-agent"


# ---------------------------------------------------------------------------
# brain_search attribution + the Wave-2 handoff
# ---------------------------------------------------------------------------


def test_search_records_the_agent(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    mcp_server.brain_search(query="attributed search", agent_id="capture-bot")

    row = test_db.execute(
        "SELECT agent_id, source FROM search_queries WHERE query = %s",
        ("attributed search",),
    ).fetchone()
    assert row == ("capture-bot", "mcp")


def test_search_without_an_agent_logs_null(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    mcp_server.brain_search(query="unattributed search")

    row = test_db.execute(
        "SELECT agent_id FROM search_queries WHERE query = %s",
        ("unattributed search",),
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_search_accepts_updated_range_filters(corpus: None) -> None:
    """The Wave-2 handoff: F9's filters are reachable over MCP."""
    payload = mcp_server.brain_search(
        query="platform migration",
        updated_after="2020-01-01",
        updated_before="2099-01-01",
    )

    assert payload["results"], "a wide window must not exclude everything"


def test_search_rejects_a_malformed_updated_filter(corpus: None) -> None:
    with pytest.raises(MCPError, match="updated_after"):
        mcp_server.brain_search(query="platform", updated_after="not-a-date")


def test_updated_after_excludes_untouched_documents(
    test_db: psycopg.Connection[Any], corpus: None
) -> None:
    """A future lower bound must return nothing, not silently ignore itself."""
    payload = mcp_server.brain_search(
        query="platform migration", updated_after="2099-01-01"
    )

    assert payload["results"] == []
