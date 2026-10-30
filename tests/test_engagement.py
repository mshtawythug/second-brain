"""``brain.engagement`` — the readout that decides RM-22b's gate (RM-22a).

DB tests on a synthetic ``interactions`` fixture whose true counts are known
(AC1), the gate flipping exactly at each threshold (AC2), the table's size
figures (AC3), and the 90-day rule the CLI relies on (AC5's library half).

All fixture data is synthetic.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg
import pytest

from brain import cli, engagement, usage
from brain.ask import AskResult, Citation
from brain.engagement import (
    GATE_MIN_DOCUMENTS,
    GATE_MIN_OPENS,
    GATE_MIN_RATINGS,
    GATE_WINDOW_DAYS,
    RATING_ACTIONS,
    READ_ACTIONS,
    EngagementStats,
    SurfaceEngagement,
    engagement_gate,
    engagement_stats,
)
from brain.gaps import record_search_query
from brain.interactions import record_interaction

_SEARCH_SESSION = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
#: A session no search is logged under, as ``brain_ask`` writes its citations.
_ASK_SESSION = uuid.UUID("00000000-0000-4000-8000-00000000a5c0")


def _docs(conn: psycopg.Connection[Any], n: int, *, prefix: str = "e") -> list[str]:
    ids = []
    for i in range(n):
        row = conn.execute(
            "INSERT INTO documents (title, content, content_type, kind, content_hash) "
            "VALUES (%s, 'body', 'note', 'vault', %s) RETURNING id::text",
            (f"Synthetic {prefix}{i}", f"{prefix}-hash-{i}"),
        ).fetchone()
        assert row is not None
        ids.append(str(row[0]))
    return ids


def _age_all(conn: psycopg.Connection[Any], days: int) -> None:
    """Move every interaction written so far ``days`` into the past."""
    conn.execute(
        "UPDATE interactions SET at = NOW() - make_interval(days => %s)", (days,)
    )


def _bulk(
    conn: psycopg.Connection[Any],
    *,
    action: str,
    count: int,
    documents: list[str],
    source: str = "cli",
) -> None:
    """``count`` rows of ``action``, cycling over ``documents``."""
    conn.execute(
        """
        INSERT INTO interactions (document_id, action, source)
        SELECT (%s::uuid[])[1 + (g %% %s)], %s, %s
        FROM generate_series(0, %s - 1) AS g
        """,
        (documents, len(documents), action, source, count),
    )


def _seed_known(conn: psycopg.Connection[Any]) -> list[str]:
    """The AC1 fixture: one row of every shape, with a hand-countable answer."""
    a, b, c = _docs(conn, 3)
    record_search_query(
        conn, query="synthetic q", result_count=2, session_id=_SEARCH_SESSION,
        source="mcp",
    )
    # Reads: 4 opens + 1 click, over documents a and b; c is only rated.
    record_interaction(
        conn, document_id=a, action="opened", source="mcp", session_id=_SEARCH_SESSION
    )
    record_interaction(
        conn, document_id=b, action="opened", source="ui", session_id=_SEARCH_SESSION
    )
    record_interaction(
        conn, document_id=a, action="opened", source="mcp", session_id=_ASK_SESSION
    )
    record_interaction(conn, document_id=b, action="opened", source="cli")
    record_interaction(conn, document_id=a, action="clicked", source="wiki")
    # Ratings: 2 useful, 1 irrelevant. A pin is not a rating.
    record_interaction(conn, document_id=a, action="rated_useful", source="cli")
    record_interaction(conn, document_id=c, action="rated_useful", source="mcp")
    record_interaction(conn, document_id=c, action="rated_irrelevant", source="cli")
    record_interaction(conn, document_id=c, action="pinned", source="ui")
    # A graph-target rating: in the table, not a document signal.
    record_interaction(
        conn, action="rated_useful", source="mcp", target_type="entity",
        target_id="synthetic-entity",
    )
    return [a, b, c]


# ---------------------------------------------------------------------------
# AC1 — counts match a synthetic fixture
# ---------------------------------------------------------------------------


def test_counts_match_the_synthetic_fixture(test_db: psycopg.Connection[Any]) -> None:
    _seed_known(test_db)

    stats = engagement_stats(test_db, days=30)

    assert stats.days == 30
    assert stats.opens == 4
    assert stats.clicks == 1
    assert stats.reads == 5
    assert stats.documents_opened == 2, "c was rated, never opened"
    assert stats.rated_useful == 2, "the graph-target rating is not a document rating"
    assert stats.rated_irrelevant == 1
    assert stats.ratings == 3, "a pin is not an explicit rating"


def test_opens_and_clicks_are_split_by_surface(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_known(test_db)

    stats = engagement_stats(test_db, days=30)

    assert stats.by_surface == (
        SurfaceEngagement(surface="mcp", opens=2, clicks=0),
        SurfaceEngagement(surface="cli", opens=1, clicks=0),
        SurfaceEngagement(surface="ui", opens=1, clicks=0),
        SurfaceEngagement(surface="wiki", opens=0, clicks=1),
    )


def test_only_opens_under_a_logged_search_session_count_as_from_search(
    test_db: psycopg.Connection[Any],
) -> None:
    """The ask's own session id is not a search session, nor is a NULL one."""
    _seed_known(test_db)

    stats = engagement_stats(test_db, days=30)

    assert stats.search_session_reads == 2
    assert stats.search_session_share == pytest.approx(2 / 5)


def test_a_session_with_two_logged_searches_counts_its_read_once(
    test_db: psycopg.Connection[Any],
) -> None:
    """A JOIN onto search_queries would count the read once per matching search."""
    (doc,) = _docs(test_db, 1)
    for query in ("synthetic first", "synthetic refined"):
        record_search_query(
            test_db, query=query, result_count=1, session_id=_SEARCH_SESSION,
            source="mcp",
        )
    record_interaction(
        test_db, document_id=doc, action="opened", source="mcp",
        session_id=_SEARCH_SESSION,
    )

    stats = engagement_stats(test_db, days=30)

    assert stats.reads == 1
    assert stats.search_session_reads == 1
    assert stats.search_session_share == 1.0


def test_the_search_session_check_is_a_join_not_a_subplan(
    test_db: psycopg.Connection[Any],
) -> None:
    """The search-session leg must be a join, with no SubPlan anywhere.

    Postgres cannot turn a sub-select inside an aggregate FILTER into a join,
    so it runs it as a SubPlan. A correlated one re-scans search_queries for
    every read row (27.6 s on 30k x 30k synthetic rows). An uncorrelated IN is
    hashed only while the planner thinks the hash fits in work_mem; past
    roughly 300k logged searches it is not, and the count ran past 120 s.
    search_queries is never pruned, so every install reaches that size. A
    join against the distinct session ids scales with no such cliff.
    """
    plan = "\n".join(
        str(row[0])
        for row in test_db.execute(
            "EXPLAIN (VERBOSE) " + engagement._TOTALS_SQL,
            {
                "days": 30,
                "reads": list(READ_ACTIONS),
                "ratings": list(RATING_ACTIONS),
            },
        ).fetchall()
    )

    assert "SubPlan" not in plan, plan
    assert "public.search_queries" in plan, plan
    assert re.search(r"(Hash|Merge)( Right| Left)? Join|Nested Loop", plan), plan


def test_an_ask_citation_counts_as_a_read_but_not_a_search_session_read(
    test_db: psycopg.Connection[Any],
) -> None:
    """`brain ask` logs one `opened` row per citation, under the ask's own session.

    Those rows are reads (they count toward the gate), but no search is logged
    under the ask's session, so the search-session share tells them apart.
    Written through the real CLI writer, so a change to its shape is seen here.
    """
    a, b = _docs(test_db, 2)
    result = AskResult(
        answer="synthetic answer [1] [2]",
        citations=[
            Citation(ref=1, document_id=a, title="t", source_kind=None, snippet="s"),
            Citation(ref=2, document_id=b, title="t", source_kind=None, snippet="s"),
        ],
        iterations_used=1,
        sub_queries=["synthetic"],
        fallback_used=False,
        session_id=_ASK_SESSION.hex,
    )
    cli._log_ask_interactions(test_db, result, source="cli")

    stats = engagement_stats(test_db, days=30)

    assert (stats.opens, stats.documents_opened) == (2, 2)
    assert stats.search_session_reads == 0
    assert stats.search_session_share == 0.0


def test_share_is_none_when_nothing_was_read(test_db: psycopg.Connection[Any]) -> None:
    stats = engagement_stats(test_db, days=30)

    assert stats.reads == 0
    assert stats.search_session_share is None, "must not divide by zero"


def test_the_window_excludes_older_rows(test_db: psycopg.Connection[Any]) -> None:
    (doc,) = _docs(test_db, 1)
    record_interaction(test_db, document_id=doc, action="opened", source="cli")
    record_interaction(test_db, document_id=doc, action="rated_useful", source="cli")
    _age_all(test_db, 10)
    record_interaction(test_db, document_id=doc, action="opened", source="cli")

    assert engagement_stats(test_db, days=7).reads == 1
    assert engagement_stats(test_db, days=7).ratings == 0
    assert engagement_stats(test_db, days=30).reads == 2
    assert engagement_stats(test_db, days=30).ratings == 1


def test_a_window_below_one_day_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError, match="days"):
        engagement_stats(test_db, days=0)


def test_the_default_window_is_the_gate_window(
    test_db: psycopg.Connection[Any],
) -> None:
    assert engagement_stats(test_db).days == GATE_WINDOW_DAYS == 90


def test_read_actions_are_the_ones_brain_usage_counts_as_opens() -> None:
    """One meaning of "opens" across `brain usage` and its --engagement readout."""
    assert set(READ_ACTIONS) == set(usage._READ_ACTIONS)


def test_the_ratings_count_is_driven_by_rating_actions(
    test_db: psycopg.Connection[Any],
) -> None:
    """One definition of a rating: the count reads RATING_ACTIONS, not a copy.

    One row of every feedback action. The count must be exactly the rows whose
    action is in RATING_ACTIONS, so adding ``pinned`` (or dropping an action)
    there changes this number and turns this red.
    """
    (doc,) = _docs(test_db, 1)
    for action in ("rated_useful", "rated_irrelevant", "pinned"):
        record_interaction(test_db, document_id=doc, action=action, source="cli")

    stats = engagement_stats(test_db, days=30)

    assert stats.ratings == 2, "a pin is not an explicit rating"
    assert stats.ratings == stats.rated_useful + stats.rated_irrelevant


# ---------------------------------------------------------------------------
# AC3 — the table's size is reported
# ---------------------------------------------------------------------------


def test_table_rows_and_bytes_cover_the_whole_table(
    test_db: psycopg.Connection[Any],
) -> None:
    """Every row, of every shape and age: RM-58's pruning trigger reads these."""
    _seed_known(test_db)
    _age_all(test_db, 400)

    stats = engagement_stats(test_db, days=7)

    assert stats.reads == 0
    assert stats.interactions_rows == 10, "graph-target and old rows included"
    size = test_db.execute(
        "SELECT pg_total_relation_size('interactions')"
    ).fetchone()
    assert size is not None
    assert stats.interactions_bytes == size[0] > 0


# ---------------------------------------------------------------------------
# AC2 — the gate flips exactly at the thresholds
# ---------------------------------------------------------------------------


def _stats(*, reads: int, documents: int, ratings: int, days: int = 90) -> EngagementStats:
    return EngagementStats(
        days=days,
        opens=reads,
        clicks=0,
        documents_opened=documents,
        rated_useful=ratings,
        rated_irrelevant=0,
        ratings=ratings,
        search_session_reads=0,
        by_surface=(),
        interactions_rows=0,
        interactions_bytes=0,
    )


def test_the_thresholds_are_the_ticket_values() -> None:
    assert (GATE_MIN_OPENS, GATE_MIN_DOCUMENTS, GATE_MIN_RATINGS) == (500, 100, 30)


def test_the_gate_is_met_exactly_at_the_thresholds() -> None:
    gate = engagement_gate(_stats(reads=500, documents=100, ratings=30))

    assert gate.met is True


@pytest.mark.parametrize(
    ("reads", "documents", "ratings"),
    [(499, 100, 30), (500, 99, 30), (500, 100, 29)],
    ids=["one-open-short", "one-document-short", "one-rating-short"],
)
def test_one_short_on_any_threshold_is_not_met(
    reads: int, documents: int, ratings: int
) -> None:
    gate = engagement_gate(_stats(reads=reads, documents=documents, ratings=ratings))

    assert gate.met is False


def test_clicks_count_toward_the_open_threshold() -> None:
    stats = EngagementStats(
        days=90, opens=499, clicks=1, documents_opened=100, rated_useful=15,
        rated_irrelevant=15, ratings=30, search_session_reads=0, by_surface=(),
        interactions_rows=0, interactions_bytes=0,
    )

    gate = engagement_gate(stats)

    assert gate.met is True
    assert (gate.reads, gate.ratings) == (500, 30)


def test_the_gate_refuses_stats_from_any_other_window() -> None:
    """A gate computed over --days would be met by widening the window."""
    for days in (7, 89, 91, 365):
        with pytest.raises(ValueError, match="90"):
            engagement_gate(_stats(reads=10_000, documents=1_000, ratings=100, days=days))


def test_the_gate_flips_on_real_rows_at_the_thresholds(
    test_db: psycopg.Connection[Any],
) -> None:
    """End to end over the DB: 499 → 500 reads flips it, all else at threshold."""
    documents = _docs(test_db, GATE_MIN_DOCUMENTS)
    _bulk(test_db, action="opened", count=GATE_MIN_OPENS - 1, documents=documents)
    _bulk(
        test_db, action="rated_irrelevant", count=GATE_MIN_RATINGS, documents=documents
    )

    before = engagement_gate(engagement_stats(test_db))
    _bulk(test_db, action="clicked", count=1, documents=documents, source="ui")
    after = engagement_gate(engagement_stats(test_db))

    assert (before.reads, before.documents, before.ratings) == (499, 100, 30)
    assert before.met is False
    assert after.reads == 500
    assert after.met is True


def test_rows_older_than_ninety_days_do_not_meet_the_gate(
    test_db: psycopg.Connection[Any],
) -> None:
    documents = _docs(test_db, GATE_MIN_DOCUMENTS)
    _bulk(test_db, action="opened", count=GATE_MIN_OPENS, documents=documents)
    _bulk(test_db, action="rated_useful", count=GATE_MIN_RATINGS, documents=documents)
    _age_all(test_db, GATE_WINDOW_DAYS + 1)

    gate = engagement_gate(engagement_stats(test_db))

    assert gate.met is False
    assert engagement_stats(test_db, days=365).reads == GATE_MIN_OPENS


def test_to_dict_carries_counts_only(test_db: psycopg.Connection[Any]) -> None:
    ids = _seed_known(test_db)

    stats = engagement_stats(test_db, days=30)
    payload = {**stats.to_dict(), "gate": engagement_gate(engagement_stats(test_db)).to_dict()}

    text = repr(payload)
    for doc_id in ids:
        assert doc_id not in text
    assert "Synthetic" not in text and "synthetic q" not in text
    assert payload["reads"] == 5
    assert payload["by_surface"][0] == {"surface": "mcp", "opens": 2, "clicks": 0}
    assert payload["gate"] == {
        "days": 90,
        "met": False,
        "reads": 5,
        "min_reads": 500,
        "documents": 2,
        "min_documents": 100,
        "ratings": 3,
        "min_ratings": 30,
    }
