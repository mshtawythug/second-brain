"""``brain.usage.build_usage_report`` — the F7 rollups.

Every number is a rollup over data that already existed; what these tests pin
is that the rollups are *correct*, since a usage report that quietly
double-counts or drops a dimension is worse than no report — it looks
authoritative.

Two specific traps are covered:

- **Fan-out.** Searches, interactions and ingests live in three tables. Joining
  them in one statement multiplies rows across unrelated dimensions and
  inflates every count. Asserted with fixtures whose true counts are known.
- **The `(unattributed)` bucket.** `agent_id IS NULL` must survive to the
  report as a real `None`, never coalesced in SQL into a fabricated agent.

All fixture data is synthetic.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from brain.gaps import record_search_query
from brain.interactions import record_interaction
from brain.usage import UNATTRIBUTED, build_usage_report


def _doc(conn: psycopg.Connection[Any], *, title: str, content_hash: str) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES (%s, 'body', 'note', 'vault', %s) RETURNING id::text",
        (title, content_hash),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _age_search(conn: psycopg.Connection[Any], query: str, days: int) -> None:
    conn.execute(
        "UPDATE search_queries SET at = NOW() - make_interval(days => %s) "
        "WHERE query = %s",
        (days, query),
    )


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def test_empty_corpus_reports_zeroes_not_an_error(
    test_db: psycopg.Connection[Any],
) -> None:
    report = build_usage_report(test_db, days=30)

    assert report.totals.searches == 0
    assert report.totals.zero_result_rate == 0.0, "must not divide by zero"
    assert report.daily == []
    assert report.by_agent == []


def test_totals_count_each_table_once(
    test_db: psycopg.Connection[Any],
) -> None:
    """The fan-out guard: known fixtures, known answers.

    3 searches, 2 opens, 1 feedback, 2 documents. A join-based implementation
    would return products of these rather than the numbers themselves.
    """
    doc_a = _doc(test_db, title="Doc A", content_hash="w7-a")
    _doc(test_db, title="Doc B", content_hash="w7-b")
    for i in range(3):
        record_search_query(
            test_db,
            query=f"query {i}",
            result_count=2,
            session_id=None,
            source="cli",
        )
    for _ in range(2):
        record_interaction(
            test_db, document_id=doc_a, action="opened", source="cli"
        )
    record_interaction(
        test_db, document_id=doc_a, action="rated_useful", source="cli"
    )

    totals = build_usage_report(test_db, days=30).totals

    assert totals.searches == 3
    assert totals.opens == 2
    assert totals.feedback == 1
    assert totals.documents_ingested == 2


def test_read_and_write_event_derivations(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _doc(test_db, title="Doc", content_hash="w7-c")
    record_search_query(
        test_db, query="q", result_count=1, session_id=None, source="cli"
    )
    record_interaction(test_db, document_id=doc, action="opened", source="cli")
    record_interaction(
        test_db, document_id=doc, action="rated_useful", source="cli"
    )

    totals = build_usage_report(test_db, days=30).totals

    assert totals.read_events == totals.searches + totals.opens == 2
    assert totals.write_events == totals.documents_ingested + totals.feedback == 2


def test_zero_result_uses_the_shared_predicate(
    test_db: psycopg.Connection[Any],
) -> None:
    """Must agree with ``brain gaps``, hence the shared SQL fragment.

    ``fts_count=0`` is a miss; ``fts_count=None`` with ``result_count=0`` is a
    pre-023 miss; a positive ``fts_count`` is a hit.
    """
    record_search_query(
        test_db, query="miss a", result_count=0, session_id=None, source="cli", fts_count=0
    )
    record_search_query(
        test_db, query="miss b", result_count=0, session_id=None, source="cli"
    )
    record_search_query(
        test_db, query="hit", result_count=5, session_id=None, source="cli", fts_count=5
    )

    totals = build_usage_report(test_db, days=30).totals

    assert totals.searches == 3
    assert totals.zero_result == 2
    assert totals.zero_result_rate == pytest.approx(2 / 3)


def test_latency_percentiles_ignore_null_durations(
    test_db: psycopg.Connection[Any],
) -> None:
    for i, ms in enumerate([100, 200, 300, None]):
        record_search_query(
            test_db,
            query=f"latency {i}",
            result_count=1,
            session_id=None,
            source="cli",
            duration_ms=ms,
        )

    totals = build_usage_report(test_db, days=30).totals

    assert totals.duration_p50_ms == 200
    assert totals.duration_p95_ms == 300


def test_sessions_count_distinct_non_null_only(
    test_db: psycopg.Connection[Any],
) -> None:
    session = uuid.uuid4()
    for i in range(2):
        record_search_query(
            test_db,
            query=f"sess {i}",
            result_count=1,
            session_id=session,
            source="mcp",
        )
    record_search_query(
        test_db, query="no session", result_count=1, session_id=None, source="cli"
    )

    totals = build_usage_report(test_db, days=30).totals

    assert totals.sessions == 1, "two searches, one session, one NULL ignored"


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def test_the_window_excludes_older_activity(
    test_db: psycopg.Connection[Any],
) -> None:
    record_search_query(
        test_db, query="recent", result_count=1, session_id=None, source="cli"
    )
    record_search_query(
        test_db, query="ancient", result_count=1, session_id=None, source="cli"
    )
    _age_search(test_db, "ancient", 90)

    assert build_usage_report(test_db, days=30).totals.searches == 1
    assert build_usage_report(test_db, days=365).totals.searches == 2


@pytest.mark.parametrize(("days", "limit"), [(0, 10), (-1, 10), (30, 0)])
def test_invalid_bounds_are_rejected(
    test_db: psycopg.Connection[Any], days: int, limit: int
) -> None:
    with pytest.raises(ValueError):
        build_usage_report(test_db, days=days, limit=limit)


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def test_by_surface_splits_searches_and_interactions(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _doc(test_db, title="Doc", content_hash="w7-d")
    record_search_query(
        test_db, query="from cli", result_count=1, session_id=None, source="cli"
    )
    record_search_query(
        test_db, query="from mcp", result_count=1, session_id=None, source="mcp"
    )
    record_interaction(test_db, document_id=doc, action="opened", source="mcp")

    by_surface = {s.surface: s for s in build_usage_report(test_db, days=30).by_surface}

    assert by_surface["cli"].searches == 1
    assert by_surface["cli"].opens == 0
    assert by_surface["mcp"].searches == 1
    assert by_surface["mcp"].opens == 1


def test_unattributed_rows_survive_as_none(
    test_db: psycopg.Connection[Any],
) -> None:
    """Never coalesced in SQL — the honest bucket is the whole point.

    Folding NULL into 'cli' would invent an agent that never existed and make
    "which agent" unanswerable for exactly the rows claiming to answer it.
    """
    record_search_query(
        test_db,
        query="attributed",
        result_count=1,
        session_id=None,
        source="mcp",
        agent_id="research-agent",
    )
    record_search_query(
        test_db, query="unattributed", result_count=1, session_id=None, source="mcp"
    )

    by_agent = {a.agent_id: a for a in build_usage_report(test_db, days=30).by_agent}

    assert by_agent["research-agent"].searches == 1
    assert None in by_agent, "the NULL bucket must survive to the report"
    assert by_agent[None].searches == 1
    assert by_agent[None].label == UNATTRIBUTED


def test_agents_are_distinguished_within_one_surface(
    test_db: psycopg.Connection[Any],
) -> None:
    """The headline question ``source`` alone cannot answer."""
    for agent, n in (("research-agent", 3), ("capture-bot", 1)):
        for i in range(n):
            record_search_query(
                test_db,
                query=f"{agent} {i}",
                result_count=1,
                session_id=None,
                source="mcp",
                agent_id=agent,
            )

    by_agent = {a.agent_id: a for a in build_usage_report(test_db, days=30).by_agent}

    assert by_agent["research-agent"].searches == 3
    assert by_agent["capture-bot"].searches == 1


def test_busiest_dimension_sorts_first(
    test_db: psycopg.Connection[Any],
) -> None:
    for i in range(3):
        record_search_query(
            test_db, query=f"mcp {i}", result_count=1, session_id=None, source="mcp"
        )
    record_search_query(
        test_db, query="cli 0", result_count=1, session_id=None, source="cli"
    )

    surfaces = [s.surface for s in build_usage_report(test_db, days=30).by_surface]

    assert surfaces[0] == "mcp"


# ---------------------------------------------------------------------------
# Daily, queries, sources
# ---------------------------------------------------------------------------


def test_daily_rollup_is_newest_first(
    test_db: psycopg.Connection[Any],
) -> None:
    record_search_query(
        test_db, query="today", result_count=1, session_id=None, source="cli"
    )
    record_search_query(
        test_db, query="older", result_count=1, session_id=None, source="cli"
    )
    _age_search(test_db, "older", 3)

    daily = build_usage_report(test_db, days=30).daily

    assert len(daily) == 2
    assert daily[0].day > daily[1].day


def test_a_day_with_opens_but_no_searches_still_appears(
    test_db: psycopg.Connection[Any],
) -> None:
    """Reading without searching is real activity and must not vanish."""
    doc = _doc(test_db, title="Doc", content_hash="w7-e")
    record_interaction(test_db, document_id=doc, action="opened", source="cli")

    daily = build_usage_report(test_db, days=30).daily

    assert len(daily) == 1
    assert daily[0].searches == 0
    assert daily[0].opens == 1


def test_top_queries_are_ranked_and_limited(
    test_db: psycopg.Connection[Any],
) -> None:
    for _ in range(3):
        record_search_query(
            test_db, query="popular", result_count=1, session_id=None, source="cli"
        )
    record_search_query(
        test_db, query="rare", result_count=1, session_id=None, source="cli"
    )

    top = build_usage_report(test_db, days=30, limit=1).top_queries

    assert len(top) == 1
    assert top[0].query == "popular"
    assert top[0].count == 3


def test_top_queries_carry_a_canonical_label(
    test_db: psycopg.Connection[Any],
) -> None:
    """The canonical form is what ``--json`` emits by default."""
    record_search_query(
        test_db,
        query="  Quarterly   Planning  ",
        result_count=1,
        session_id=None,
        source="cli",
    )

    top = build_usage_report(test_db, days=30).top_queries

    assert top[0].query == "  Quarterly   Planning  "
    assert top[0].canonical != top[0].query, "canonical must normalize"


def test_ingested_by_source_labels_null_as_manual(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _doc(test_db, title="Sourceless", content_hash="w7-f")

    sources = build_usage_report(test_db, days=30).ingested_by_source

    assert sources
    assert sources[0].source_kind is None
    assert sources[0].label == "manual"


def test_ingest_window_matches_the_search_window(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _doc(test_db, title="Old Doc", content_hash="w7-g")
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s",
        (datetime.now(UTC) - timedelta(days=90), doc),
    )

    assert build_usage_report(test_db, days=30).totals.documents_ingested == 0
    assert build_usage_report(test_db, days=365).totals.documents_ingested == 1
