"""Migration 028's columns actually get WRITTEN, and by whom (Wave 5).

A schema column nothing writes is dead weight; a column written with the wrong
number is worse than an empty one. These tests exercise the four real write
sites — CLI search, MCP ``brain_search``, CLI recall, MCP ``brain_recall`` —
against a real DB and read the row back.

The load-bearing test in this file is
:func:`test_non_brief_search_persists_null_baseline`. ``baseline_tokens`` is a
COUNTERFACTUAL: what the same call would have cost had a cheaper mode not been
used. A call that had no cheaper mode has no such number, and defaulting it to
the payload would manufacture a savings figure for every search in the system.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.gaps import record_search_query
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: Long enough that the chunk snippet reaches its cap, so a short summary is a
#: materially cheaper choice and brief mode really substitutes.
_LONG_BODY = "The quarterly review covered budget, hiring and roadmap. " * 12


def _seed(
    conn: psycopg.Connection[Any], embedder: Any, marker: str, count: int = 3
) -> str:
    """Ingest ``count`` summarized documents carrying ``marker``; return the query.

    ``marker`` is woven into the BODY, and the returned query is
    ``"quarterly <marker>"``. Both halves matter:

    - The FTS leg ANDs its terms, so a query word absent from the corpus
      returns an empty result set — and an empty set still logs a row, still
      writes a payload count (of ``"[]"``), and would let every assertion in
      this file pass while proving nothing. Three of these tests were
      vacuously green before the marker was threaded through.
    - The marker keeps each test's ``search_queries.query`` unique, which is
      how :func:`_tokens_of` finds its row.

    Every document gets a short summary so brief mode has something to
    substitute on every hit — otherwise the baseline/payload gap could be zero.
    Bodies differ per document because ``documents.content_hash`` is UNIQUE.
    """
    for i in range(count):
        result = ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Quarterly note {marker} {i}",
                content=f"{_LONG_BODY}Filed under {marker} as note {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"w5-tokens-{marker}-{i}",
            tags=["planning"],
        )
        conn.execute(
            "UPDATE documents SET summary = %s WHERE id = %s",
            ("Quarterly planning notes.", result.document_id),
        )
    return f"quarterly {marker}"


def _tokens_of(
    conn: psycopg.Connection[Any], query: str
) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT payload_tokens, baseline_tokens FROM search_queries "
        "WHERE query = %s",
        (query,),
    ).fetchone()
    assert row is not None, f"no search_queries row logged for {query!r}"
    return row[0], row[1]


@pytest.fixture
def wired(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — schema + isolation
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
) -> None:
    patch_embedder(fake_embedder)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


# ---------------------------------------------------------------------------
# brain search
# ---------------------------------------------------------------------------


def test_search_persists_payload_tokens(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """The measured half lands, and it is the cost of what was emitted.

    Not merely "non-null": the persisted number is compared against a fresh
    count over the CLI's own stdout, so a version that logged some other
    string's length would fail.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "alpha")

    # Act
    result = CliRunner().invoke(
        app, ["search", query, "--fts-only", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload_tokens, _ = _tokens_of(test_db, query)
    assert payload_tokens is not None
    emitted = json.loads(result.stdout)
    assert payload_tokens == fake_embedder.count_tokens(
        json.dumps(emitted, ensure_ascii=False)
    )


def test_brief_search_persists_both_payload_and_baseline(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """``--brief`` had an alternative, so both ends of the comparison exist."""
    # Arrange
    query = _seed(test_db, fake_embedder, "bravo")

    # Act
    result = CliRunner().invoke(
        app, ["search", query, "--fts-only", "--json", "--brief"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload_tokens, baseline_tokens = _tokens_of(test_db, query)
    assert payload_tokens is not None
    assert baseline_tokens is not None
    # Non-vacuous: the seeded corpus is built so brief really is cheaper, so a
    # baseline that merely echoed the payload would fail here too.
    assert baseline_tokens > payload_tokens, (
        f"brief must be cheaper on this corpus: {payload_tokens} vs "
        f"{baseline_tokens}"
    )


def test_non_brief_search_persists_null_baseline(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """THE ANTI-FABRICATION TEST.

    A default search had no cheaper mode available. Its ``baseline_tokens``
    must therefore be NULL — not the payload, not zero. Anything else invents
    a counterfactual for every search ever run and turns the ``brain usage``
    savings line into a number computed against nothing.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "charlie")

    # Act
    result = CliRunner().invoke(
        app, ["search", query, "--fts-only", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload_tokens, baseline_tokens = _tokens_of(test_db, query)
    assert payload_tokens is not None, "the measured half must still be written"
    assert baseline_tokens is None


def test_human_table_search_measures_nothing(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """A terminal search delivers a table, not a payload — so NULL, not 0.

    ``payload_tokens`` holds the canonical serialization of a payload. A Rich
    table with 120-char previews is not a payload at all, and pricing the JSON
    that path never emitted would file a counterfactual under the measured
    column.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "delta")

    # Act
    result = CliRunner().invoke(app, ["search", query, "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    assert _tokens_of(test_db, query) == (None, None)


def test_mcp_search_persists_what_the_envelope_reports(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """The write→read round trip, closed on the agent-facing surface.

    The envelope's ``results_tokens`` and the persisted ``payload_tokens`` must
    be the same number: if they diverged, an agent reading its own budget and
    an operator reading ``brain usage`` would be looking at two different
    costs for one call.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "echo")

    # Act
    payload = mcp_server.brain_search(query=query, fts_only=True)

    # Assert
    assert payload["results"], "the seeded corpus must produce at least one hit"
    persisted, baseline = _tokens_of(test_db, query)
    assert persisted == payload["results_tokens"]
    assert persisted == fake_embedder.count_tokens(
        json.dumps(payload["results"], ensure_ascii=False)
    )
    assert baseline is None, "a default MCP search had no cheaper alternative"


def test_mcp_brief_search_persists_the_counterfactual(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """MCP's ``brief=True`` is the other surface with a real alternative."""
    # Arrange
    query = _seed(test_db, fake_embedder, "foxtrot")

    # Act
    payload = mcp_server.brain_search(query=query, fts_only=True, brief=True)

    # Assert
    persisted, baseline = _tokens_of(test_db, query)
    assert persisted == payload["results_tokens"]
    assert baseline is not None and baseline > persisted


# ---------------------------------------------------------------------------
# brain recall
# ---------------------------------------------------------------------------


def test_recall_persists_the_delivered_payload_not_used_tokens(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """Recall logs the emitted PAYLOAD, which is not ``used_tokens``.

    ``used_tokens`` measures what was SELECTED into the budget. Every passage
    then ships twice — structured and rendered — so the payload runs
    materially larger (``brain_recall``'s own docstring records 2.01x-2.36x on
    live queries). The column holds the canonical serialization of the
    payload, so persisting ``used_tokens`` would put a number under a name
    that means something else. Pinned as a strict inequality against the
    emitted artifact re-serialized canonically — note the assertion below
    round-trips ``result.stdout`` through ``json.loads`` + compact
    ``json.dumps``, which is precisely why it passes despite Rich printing
    the same object at ``indent=2``.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "golf")

    # Act
    result = CliRunner().invoke(app, ["recall", query, "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    emitted = json.loads(result.stdout)
    payload_tokens, baseline_tokens = _tokens_of(test_db, query)
    assert payload_tokens == fake_embedder.count_tokens(
        json.dumps(emitted, ensure_ascii=False)
    )
    assert payload_tokens > emitted["used_tokens"], (
        "the delivered payload must cost more than the budget it packed: "
        f"{payload_tokens} vs {emitted['used_tokens']}"
    )
    assert baseline_tokens is None, "recall has no cheaper mode to compare to"


def test_recall_human_output_measures_the_context_block(
    test_db: psycopg.Connection[Any], fake_embedder: Any, wired: None
) -> None:
    """The default recall output IS a payload — the pasteable context block.

    Unlike ``brain search``'s table, this artifact exists to be dropped into a
    context window, so it is measured rather than left NULL.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "hotel")

    # Act
    result = CliRunner().invoke(app, ["recall", query])

    # Assert
    assert result.exit_code == 0, result.output
    payload_tokens, _ = _tokens_of(test_db, query)
    assert payload_tokens is not None
    assert payload_tokens == fake_embedder.count_tokens(result.stdout.rstrip("\n"))


def test_mcp_recall_persists_the_key_it_returns(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """The response's ``payload_tokens`` and the row's must be one number."""
    # Arrange
    query = _seed(test_db, fake_embedder, "india")

    # Act
    payload = mcp_server.brain_recall(query=query)

    # Assert
    persisted, baseline = _tokens_of(test_db, query)
    assert persisted == payload["payload_tokens"]
    assert baseline is None


# ---------------------------------------------------------------------------
# The Python-boundary gate (migration 028 carries no CHECK on purpose)
# ---------------------------------------------------------------------------


def test_a_baseline_without_a_payload_is_rejected(
    test_db: psycopg.Connection[Any],
) -> None:
    """The invariant proof 4 asserts, enforced at the only write site.

    ``count(*) WHERE baseline_tokens IS NOT NULL AND payload_tokens IS NULL``
    must be 0 forever. Checking that after the fact only tells you a bad row
    already exists; refusing the write is what keeps it true.
    """
    with pytest.raises(ValueError, match="baseline_tokens requires payload_tokens"):
        record_search_query(
            test_db,
            query="orphan baseline",
            result_count=1,
            session_id=None,
            source="cli",
            baseline_tokens=900,
        )

    row = test_db.execute(
        "SELECT count(*) FROM search_queries WHERE query = %s",
        ("orphan baseline",),
    ).fetchone()
    assert row is not None and row[0] == 0, "the row must not have been written"


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("payload_tokens", {"payload_tokens": -1}),
        ("baseline_tokens", {"payload_tokens": 10, "baseline_tokens": -5}),
    ],
)
def test_a_negative_token_count_is_rejected(
    test_db: psycopg.Connection[Any], field: str, kwargs: dict[str, int]
) -> None:
    """A negative measurement means the measuring code is broken.

    Raised rather than swallowed: this is a code bug, not migration lag, and
    it sits on the same side of ``record_search_query``'s error contract as
    the unrecognised-``source`` CheckViolation that also propagates.
    """
    with pytest.raises(ValueError, match=f"{field} must be >= 0"):
        record_search_query(
            test_db,
            query="negative tokens",
            result_count=1,
            session_id=None,
            source="cli",
            **kwargs,
        )


def test_the_gate_runs_before_the_insert_is_attempted(
    test_db: psycopg.Connection[Any],
) -> None:
    """Validation must not depend on the DB having the columns.

    A pre-028 DB swallows the UndefinedColumn (see below) — if the gate lived
    after the INSERT, a bad measurement would be silently discarded there and
    loudly rejected everywhere else.
    """
    test_db.execute("ALTER TABLE search_queries DROP COLUMN IF EXISTS payload_tokens")
    try:
        with pytest.raises(ValueError):
            record_search_query(
                test_db,
                query="gate before insert",
                result_count=1,
                session_id=None,
                source="cli",
                payload_tokens=-3,
            )
    finally:
        test_db.execute(
            "ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS payload_tokens INT"
        )


# ---------------------------------------------------------------------------
# Degraded DB — the daily-driver contract (mirrors 027's equivalent)
# ---------------------------------------------------------------------------


@pytest.mark.fresh_schema
def test_logging_survives_a_db_missing_the_028_columns(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    wired: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A binary that writes 028's columns must not break search on a pre-028 DB.

    The realistic upgrade order is: new binary lands, operator has not re-run
    ``brain init``. Search is the daily driver — it keeps working, loudly
    nagging. Copied in shape from ``tests/test_agent_attribution_degraded.py``,
    the 027 equivalent.
    """
    # Arrange
    query = _seed(test_db, fake_embedder, "juliet")
    test_db.execute("ALTER TABLE search_queries DROP COLUMN payload_tokens")

    # Act
    with caplog.at_level(logging.WARNING, logger="brain.gaps"):
        result = CliRunner().invoke(app, ["search", query, "--fts-only", "--json"])

    # Assert — results still delivered, operator still told why telemetry died.
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout), "search must still return results"
    assert "payload_tokens" in caplog.text
    assert "brain init" in caplog.text, "the warning must be actionable"
    assert "028" in caplog.text


@pytest.mark.fresh_schema
def test_the_hint_names_the_column_that_is_actually_missing(
    test_db: psycopg.Connection[Any],
) -> None:
    """REGRESSION: the guard matched against the echoed SQL, not the error.

    ``str(exc)`` on a psycopg ``UndefinedColumn`` appends PostgreSQL's
    ``LINE n:`` echo of the statement — and this INSERT names every additive
    column. The old substring scan therefore returned whichever
    ``_ADDITIVE_COLUMNS`` key iterated first (``fts_count``), not the one the
    server complained about, and told the operator to apply a migration they
    already had. Latent with three columns; 028 made the INSERT name five.

    Asserted against the FIRST key in the mapping specifically: that is the
    value the buggy version returned regardless of which column was dropped.
    """
    from brain.gaps import _ADDITIVE_COLUMNS, _missing_additive_column

    test_db.execute("ALTER TABLE search_queries DROP COLUMN payload_tokens")

    try:
        test_db.execute(
            "INSERT INTO search_queries "
            "(query, result_count, source, fts_count, agent_id, payload_tokens) "
            "VALUES ('probe', 1, 'cli', 1, 'a', 1)"
        )
    except psycopg.errors.UndefinedColumn as exc:
        test_db.rollback()
        missing = _missing_additive_column(exc)
    else:  # pragma: no cover — the column was dropped above
        pytest.fail("expected UndefinedColumn")

    assert missing == "payload_tokens"
    assert missing != next(iter(_ADDITIVE_COLUMNS)), (
        "a scan that returns the first mapping key regardless of the error "
        "would pass a bare not-None assertion"
    )


@pytest.mark.fresh_schema
def test_the_schema_hint_names_migration_028(
    test_db: psycopg.Connection[Any],
) -> None:
    """The read path's matching hint, so ``brain usage`` fails cleanly too."""
    from brain.gaps import search_queries_schema_hint

    test_db.execute("ALTER TABLE search_queries DROP COLUMN baseline_tokens")

    try:
        test_db.execute("SELECT baseline_tokens FROM search_queries LIMIT 1")
    except psycopg.errors.UndefinedColumn as exc:
        test_db.rollback()
        hint = search_queries_schema_hint(exc)
    else:  # pragma: no cover — the column was dropped above
        pytest.fail("expected UndefinedColumn")

    assert hint is not None
    assert "028" in hint
    assert "brain init" in hint
