"""Tests for `brain gaps` — search-failure-driven knowledge-gap detection."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any
from unittest import mock

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.gaps import (
    SearchFailure,
    SearchFailureDetector,
    _canonical_key,
    cluster_failed_queries,
    record_search_query,
    top_search_failures,
)

# Same explicit-credentials URL idiom every other CLI test uses (conn.info.dsn
# strips the password).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _insert_search_query(
    conn: psycopg.Connection,
    query: str,
    result_count: int = 0,
    *,
    session_id: uuid.UUID | None = None,
    source: str = "cli",
    tenant_id: str = "default",
) -> None:
    """Insert one ``search_queries`` row (``at`` defaults to NOW())."""
    conn.execute(
        "INSERT INTO search_queries "
        "(tenant_id, query, result_count, session_id, source) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            tenant_id,
            query,
            result_count,
            str(session_id) if session_id is not None else None,
            source,
        ),
    )


# ---------------------------------------------------------------------------
# record_search_query
# ---------------------------------------------------------------------------


def test_record_search_query_zero_result(test_db: psycopg.Connection) -> None:
    record_search_query(
        test_db, query="benefits policy", result_count=0,
        session_id=None, source="cli",
    )
    row = test_db.execute(
        "SELECT query, result_count, source, session_id, tenant_id "
        "FROM search_queries"
    ).fetchone()
    assert row == ("benefits policy", 0, "cli", None, "default")


def test_record_search_query_nonzero(test_db: psycopg.Connection) -> None:
    sess = uuid.uuid4()
    record_search_query(
        test_db, query="q3 hiring", result_count=3,
        session_id=sess, source="mcp", tenant_id="default",
    )
    row = test_db.execute(
        "SELECT query, result_count, source, session_id FROM search_queries"
    ).fetchone()
    assert row[0] == "q3 hiring"
    assert row[1] == 3
    assert row[2] == "mcp"
    assert str(row[3]) == str(sess)


def test_record_search_query_db_error_swallowed() -> None:
    """A transient OperationalError must never break the search response."""
    conn = mock.MagicMock()
    conn.execute.side_effect = psycopg.OperationalError("connection reset")
    # Must NOT raise.
    record_search_query(
        conn, query="anything", result_count=0, session_id=None, source="cli"
    )


def test_record_search_query_undefined_table_swallowed_with_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing table (migration 019 unapplied) must NOT break search.

    Regression: observed live against a pre-019 prod DB — the propagating
    UndefinedTable aborted `brain search` with a traceback before results were
    rendered. The contract is now: swallow, roll back the poisoned
    transaction, and warn with an actionable `brain init` hint.
    """
    conn = mock.MagicMock()
    conn.execute.side_effect = psycopg.errors.UndefinedTable(
        'relation "search_queries" does not exist'
    )
    with caplog.at_level("WARNING", logger="brain.gaps"):
        # Must NOT raise.
        record_search_query(
            conn, query="anything", result_count=0,
            session_id=None, source="cli",
        )
    conn.rollback.assert_called_once()
    assert any("brain init" in r.getMessage() for r in caplog.records)
    # Privacy: the raw query string must not appear at WARNING level.
    assert not any("anything" in r.getMessage() for r in caplog.records)


def test_record_search_query_other_schema_errors_propagate() -> None:
    """Schema errors other than the missing table are real bugs — propagate."""
    conn = mock.MagicMock()
    conn.execute.side_effect = psycopg.errors.UndefinedColumn(
        'column "nope" of relation "search_queries" does not exist'
    )
    with pytest.raises(psycopg.errors.UndefinedColumn):
        record_search_query(
            conn, query="anything", result_count=0,
            session_id=None, source="cli",
        )


# ---------------------------------------------------------------------------
# cluster_failed_queries / _canonical_key (pure)
# ---------------------------------------------------------------------------


def test_cluster_empty() -> None:
    assert cluster_failed_queries([]) == []


def test_cluster_merges_similar() -> None:
    """At a low-enough threshold, near-duplicate queries form one cluster.

    ``"benefits policy"`` and ``"benefits plan"`` share 1 of 3 distinct tokens
    (Jaccard ≈ 0.33), so they merge only at a threshold ≤ 0.33. (The spec's
    informal "0.4" is below the real Jaccard and would NOT merge — corrected
    here to a mathematically consistent threshold.)
    """
    clusters = cluster_failed_queries(
        ["benefits policy", "benefits plan"], threshold=0.3
    )
    assert len(clusters) == 1
    assert sorted(clusters[0]) == ["benefits plan", "benefits policy"]


def test_cluster_keeps_distinct() -> None:
    clusters = cluster_failed_queries(["benefits policy", "q3 hiring"])
    assert len(clusters) == 2


def test_cluster_default_threshold_matches_spec_examples() -> None:
    """The §3b documented behavior at the default threshold 0.5."""
    # Jaccard ≈ 0.33 < 0.5 → separate.
    assert len(cluster_failed_queries(["benefits policy", "benefits plan"])) == 2
    # Jaccard 2/3 ≈ 0.67 ≥ 0.5 → one cluster.
    merged = cluster_failed_queries(["q3 hiring", "q3 hiring plan"])
    assert len(merged) == 1


def test_cluster_ignores_punctuation_and_case() -> None:
    clusters = cluster_failed_queries(["Q3-Hiring", "q3 hiring"])
    assert len(clusters) == 1


def test_canonical_key_sorts_and_dedupes() -> None:
    assert _canonical_key("Policy benefits Policy") == "benefits policy"
    assert _canonical_key("q3  hiring,  PLAN!") == "hiring plan q3"


# ---------------------------------------------------------------------------
# SearchFailureDetector.detect (real Postgres)
# ---------------------------------------------------------------------------


def test_detector_zero_result(test_db: psycopg.Connection) -> None:
    for _ in range(5):
        _insert_search_query(test_db, "benefits policy", 0)
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    gaps = detector.detect(test_db, tenant_id="default", limit=10)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.signal_kind == "search_failure"
    assert gap.target_type == "topic"
    assert gap.target_id == "benefits policy"
    assert gap.score == 5.0
    assert gap.evidence_ids == []
    assert "no useful result" in gap.rationale


def test_detector_below_min_cluster(test_db: psycopg.Connection) -> None:
    for _ in range(2):
        _insert_search_query(test_db, "rare topic", 0)
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=3)
    assert detector.detect(test_db, tenant_id="default", limit=10) == []


def test_detector_no_click(test_db: psycopg.Connection) -> None:
    """Non-empty results never opened in-session surface as a no-click gap."""
    sess = uuid.uuid4()
    for _ in range(3):
        _insert_search_query(
            test_db, "vendor comparison", 5, session_id=sess, source="mcp"
        )
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    gaps = detector.detect(test_db, tenant_id="default", limit=10)
    assert len(gaps) == 1
    assert gaps[0].target_id == "comparison vendor"
    assert gaps[0].score == 3.0


def test_detector_no_click_excluded_when_opened(
    test_db: psycopg.Connection, seed_doc: Any
) -> None:
    """A search whose session has an 'opened' interaction is NOT a no-click."""
    doc_id = seed_doc(title="Vendor doc", content="body about vendors")
    sess = uuid.uuid4()
    for _ in range(3):
        _insert_search_query(
            test_db, "vendor comparison", 5, session_id=sess, source="mcp"
        )
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source, session_id) "
        "VALUES (%s, 'opened', 'mcp', %s)",
        (doc_id, str(sess)),
    )
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    assert detector.detect(test_db, tenant_id="default", limit=10) == []


def test_detector_respects_lookback_window(test_db: psycopg.Connection) -> None:
    """Rows older than the lookback window are invisible to the detector."""
    test_db.execute(
        "INSERT INTO search_queries (query, result_count, source, at) "
        "VALUES ('old query', 0, 'cli', NOW() - make_interval(days => 90)), "
        "('old query', 0, 'cli', NOW() - make_interval(days => 90))"
    )
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    assert detector.detect(test_db, tenant_id="default", limit=10) == []


def test_detector_tenant_scoped(test_db: psycopg.Connection) -> None:
    """The detector only mines its own tenant's failures."""
    for _ in range(3):
        _insert_search_query(test_db, "other tenant q", 0, tenant_id="other")
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    assert detector.detect(test_db, tenant_id="default", limit=10) == []
    assert len(detector.detect(test_db, tenant_id="other", limit=10)) == 1


def test_detector_limit_caps_gaps(test_db: psycopg.Connection) -> None:
    for label in ("alpha one", "bravo two", "charlie three"):
        for _ in range(2):
            _insert_search_query(test_db, label, 0)
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    gaps = detector.detect(test_db, tenant_id="default", limit=2)
    assert len(gaps) == 2


def test_detector_empty_log_returns_empty(test_db: psycopg.Connection) -> None:
    detector = SearchFailureDetector(lookback_days=30, min_cluster_size=2)
    assert detector.detect(test_db, tenant_id="default", limit=10) == []


# ---------------------------------------------------------------------------
# top_search_failures (read view)
# ---------------------------------------------------------------------------


def test_top_search_failures_ranks_by_count(test_db: psycopg.Connection) -> None:
    for _ in range(5):
        _insert_search_query(test_db, "benefits policy overview", 0)
    for _ in range(2):
        _insert_search_query(test_db, "compensation benchmarks", 0)
    failures = top_search_failures(
        test_db, tenant_id="default", since_days=30, limit=10
    )
    assert failures[0] == SearchFailure(
        query="benefits policy overview", count=5, kind="zero_results"
    )
    assert failures[1].count == 2


def test_top_search_failures_normalize_hides_raw_text(
    test_db: psycopg.Connection,
) -> None:
    """normalize=True returns derived canonical labels, merging variants."""
    _insert_search_query(test_db, "Benefits Policy", 0)
    _insert_search_query(test_db, "policy benefits", 0)
    failures = top_search_failures(
        test_db, tenant_id="default", since_days=30, limit=10, normalize=True
    )
    assert len(failures) == 1
    assert failures[0].query == "benefits policy"
    assert failures[0].count == 2


def test_top_search_failures_includes_no_click(
    test_db: psycopg.Connection,
) -> None:
    """A non-empty, never-opened search surfaces with kind='no_click'."""
    sess = uuid.uuid4()
    _insert_search_query(
        test_db, "vendor comparison", 5, session_id=sess, source="mcp"
    )
    failures = top_search_failures(
        test_db, tenant_id="default", since_days=30, limit=10
    )
    assert len(failures) == 1
    assert failures[0].kind == "no_click"
    assert failures[0].query == "vendor comparison"


def test_top_search_failures_empty(test_db: psycopg.Connection) -> None:
    assert top_search_failures(
        test_db, tenant_id="default", since_days=30, limit=10
    ) == []


# ---------------------------------------------------------------------------
# CLI: brain search logging hook + brain gaps / brain gaps push
# ---------------------------------------------------------------------------


def test_brain_search_logs_query(
    test_db: psycopg.Connection,
    patch_embedder: Any,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`brain search` against an empty corpus logs a zero-result row."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["search", "synthetic topic xyz"])
    assert res.exit_code == 0, res.output
    row = test_db.execute(
        "SELECT query, result_count, source, session_id FROM search_queries"
    ).fetchone()
    assert row[0] == "synthetic topic xyz"
    assert row[1] == 0
    assert row[2] == "cli"
    assert row[3] is None


def test_cli_search_survives_pre_019_schema(
    test_db: psycopg.Connection,
    patch_embedder: Any,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`brain search` must keep working when migration 019 is unapplied.

    Regression: observed live against a pre-019 prod DB — the search command
    died with a traceback from the gaps logging hook before rendering results.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    test_db.execute("DROP TABLE search_queries")
    test_db.commit()
    res = CliRunner().invoke(app, ["search", "synthetic topic xyz"])
    assert res.exit_code == 0, res.output
    assert "Traceback" not in res.output


def test_cli_gaps_clean_error_pre_019_schema(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brain gaps` / `gaps push` fail loudly-but-cleanly without the table."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    test_db.execute("DROP TABLE search_queries")
    test_db.commit()
    for argv in (["gaps"], ["gaps", "push"]):
        res = CliRunner().invoke(app, argv)
        assert res.exit_code == 1, res.output
        assert "brain init" in res.output
        assert "Traceback" not in res.output


def test_brain_gaps_empty(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["gaps"])
    assert res.exit_code == 0, res.output
    assert "No search failures found." in res.output


def test_brain_gaps_json(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for _ in range(2):
        _insert_search_query(test_db, "benefits policy overview", 0)
    res = CliRunner().invoke(app, ["gaps", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert data[0]["query"] == "benefits policy overview"
    assert data[0]["count"] == 2
    assert data[0]["kind"] == "zero_results"


def test_brain_gaps_table_output(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for _ in range(3):
        _insert_search_query(test_db, "q3 hiring plan", 0)
    res = CliRunner().invoke(app, ["gaps"])
    assert res.exit_code == 0, res.output
    assert "Top search failures" in res.output
    assert "q3 hiring plan" in res.output
    assert "zero-results" in res.output


def test_gaps_push_upserts_gap(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for _ in range(3):
        _insert_search_query(test_db, "vendor comparison", 0)
    res = CliRunner().invoke(app, ["gaps", "push"])
    assert res.exit_code == 0, res.output
    assert "Pushed" in res.output
    row = test_db.execute(
        "SELECT signal_kind, target_type, target_id FROM elicitation_gaps "
        "WHERE signal_kind = 'search_failure'"
    ).fetchone()
    assert row is not None
    assert row[0] == "search_failure"
    assert row[1] == "topic"
    assert row[2] == "comparison vendor"


def test_gaps_push_dry_run(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for _ in range(3):
        _insert_search_query(test_db, "vendor comparison", 0)
    res = CliRunner().invoke(app, ["gaps", "push", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Would push" in res.output
    count = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps"
    ).fetchone()[0]
    assert count == 0


def test_gaps_push_dry_run_empty(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["gaps", "push", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "No search-failure gaps to push." in res.output


def test_gaps_push_appears_in_elicit_list(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a pushed search_failure gap shows up in `brain elicit list`."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for _ in range(3):
        _insert_search_query(test_db, "compensation benchmarks", 0)
    push = CliRunner().invoke(app, ["gaps", "push"])
    assert push.exit_code == 0, push.output
    listing = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    gaps = json.loads(listing.stdout)
    assert any(g["signal_kind"] == "search_failure" for g in gaps)
