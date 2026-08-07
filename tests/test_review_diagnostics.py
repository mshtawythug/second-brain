"""QA-5 / QA-6: making an empty review queue interpretable.

Both behaviours here exist because *absence* was ambiguous. `brain review scan`
printed `No findings.` whether the corpus was healthy or structurally incapable
of producing a finding, and a snoozed row vanished with no way to see it again.

Every test that asserts an absence pairs it with a control showing the same
query returns something under the opposite condition — otherwise "0 rows" is
indistinguishable from "the query is broken", which is exactly the trap that
made the original diagnosis take an hour.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.review.queries import (
    StaleCandidateDiagnosis,
    diagnose_stale_candidates,
    list_review_queue,
)

TENANT = "default"


def _seed_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    age_days: int,
    summary: str | None,
    content_type: str = "markdown",
    draft: bool = False,
) -> str:
    row = conn.execute(
        """
        INSERT INTO documents (title, content, content_hash, content_type,
                               ingested_at, summary, draft)
        VALUES (%s, %s, md5(random()::text), %s,
                now() - make_interval(days => %s), %s, %s)
        RETURNING id::text
        """,
        (title, f"body of {title}", content_type, age_days, summary, draft),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _link_to_entity(conn: psycopg.Connection[Any], doc_id: str) -> None:
    """Give a document one graph entity mention, creating the entity once."""
    entity = conn.execute(
        """
        INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)
        VALUES (%s, 'topic', 'QA Topic', 'qa-topic')
        ON CONFLICT (tenant_id, entity_type, canonical_key)
            DO UPDATE SET name = EXCLUDED.name
        RETURNING id::text
        """,
        (TENANT,),
    ).fetchone()
    assert entity is not None
    conn.execute(
        """
        INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source)
        VALUES (%s, %s, %s, 'test')
        ON CONFLICT DO NOTHING
        """,
        (TENANT, entity[0], doc_id),
    )


# --------------------------------------------------------------- QA-5 -------


def test_no_aged_docs_is_named(test_db: psycopg.Connection) -> None:
    """A young corpus is not a broken one."""
    _seed_doc(test_db, title="Fresh Note", age_days=1, summary="s")
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.aged == 0
    assert d.reason == "no_aged_docs"
    assert d.hint is not None


def test_no_graph_entities_is_named(test_db: psycopg.Connection) -> None:
    """The case the pre-existing nudge was structurally silent on.

    ``count_stale_docs_missing_summary`` scopes *through* ``graph_entity_mentions``,
    so a corpus with none always counted zero and warned about nothing — the
    most common starting state produced the least information.
    """
    _seed_doc(test_db, title="Old Ungraphed", age_days=500, summary="s")
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.aged == 1, "control: the doc IS aged"
    assert d.in_graph == 0
    assert d.reason == "no_graph_entities"
    assert "graphrag build" in (d.hint or "")


def test_no_summaries_is_named(test_db: psycopg.Connection) -> None:
    doc = _seed_doc(test_db, title="Old Unsummarized", age_days=500, summary=None)
    _link_to_entity(test_db, doc)
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.aged == 1 and d.in_graph == 1, "control: aged and graphed"
    assert d.summarized == 0
    assert d.reason == "no_summaries"
    assert "enrich --backfill" in (d.hint or "")


def test_healthy_corpus_reports_no_reason(test_db: psycopg.Connection) -> None:
    """The load-bearing case: ``None`` means 'genuinely nothing stale'.

    If this ever returns a reason for a populated candidate set, the diagnostic
    would cry wolf on every healthy scan.
    """
    doc = _seed_doc(test_db, title="Old Complete", age_days=500, summary="a summary")
    _link_to_entity(test_db, doc)
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.summarized == 1
    assert d.reason is None
    assert d.hint is None


def test_stages_narrow_so_the_first_zero_is_the_constraint(
    test_db: psycopg.Connection,
) -> None:
    """Counts must be nested subsets, or the 'first zero' reading is wrong."""
    graphed = _seed_doc(test_db, title="Graphed", age_days=500, summary=None)
    _link_to_entity(test_db, graphed)
    _seed_doc(test_db, title="Ungraphed", age_days=500, summary="s")
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.aged >= d.in_graph >= d.summarized


def test_transcripts_and_drafts_are_not_counted_as_aged(
    test_db: psycopg.Connection,
) -> None:
    """Mirrors iter_docs_for_staleness_scan's exclusions, or the counts lie."""
    _seed_doc(test_db, title="Old Transcript", age_days=500, summary="s",
              content_type="transcript")
    _seed_doc(test_db, title="Old Draft", age_days=500, summary="s", draft=True)
    d = diagnose_stale_candidates(test_db, tenant_id=TENANT, stale_age_days=365)
    assert d.aged == 0


@pytest.mark.parametrize(
    ("aged", "in_graph", "summarized", "expected"),
    [
        (0, 0, 0, "no_aged_docs"),
        (5, 0, 0, "no_graph_entities"),
        (5, 3, 0, "no_summaries"),
        (5, 3, 2, None),
    ],
)
def test_reason_precedence(
    aged: int, in_graph: int, summarized: int, expected: str | None
) -> None:
    """Pure: the FIRST empty stage wins, so the hint names the binding gate."""
    d = StaleCandidateDiagnosis(aged=aged, in_graph=in_graph, summarized=summarized)
    assert d.reason == expected
    assert (d.hint is None) == (expected is None)


# --------------------------------------------------------------- QA-6 -------


def _seed_finding(
    conn: psycopg.Connection[Any], *, target_id: str, snoozed_days: int | None
) -> str:
    status = "snoozed" if snoozed_days else "surfaced"
    row = conn.execute(
        """
        INSERT INTO elicitation_gaps
            (tenant_id, signal_kind, target_type, target_id, score,
             evidence_ids, rationale, status, snoozed_until)
        VALUES (%s, 'stale', 'doc', %s, 0.9, %s, 'qa', %s,
                CASE WHEN %s::int IS NULL THEN NULL
                     ELSE now() + make_interval(days => %s::int) END)
        RETURNING id::text
        """,
        (TENANT, target_id, [target_id], status, snoozed_days, snoozed_days),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_snoozed_finding_is_hidden_by_default(test_db: psycopg.Connection) -> None:
    doc = _seed_doc(test_db, title="Target", age_days=500, summary="s")
    _seed_finding(test_db, target_id=doc, snoozed_days=30)
    rows = list_review_queue(
        test_db, tenant_id=TENANT, signal_kinds=["stale"], limit=50
    )
    assert rows == []


def test_include_snoozed_reveals_it(test_db: psycopg.Connection) -> None:
    """The control that makes the previous test meaningful.

    Without this pairing, "0 rows by default" is indistinguishable from a query
    that returns nothing under any conditions.
    """
    doc = _seed_doc(test_db, title="Target", age_days=500, summary="s")
    _seed_finding(test_db, target_id=doc, snoozed_days=30)
    rows = list_review_queue(
        test_db,
        tenant_id=TENANT,
        signal_kinds=["stale"],
        limit=50,
        include_snoozed=True,
    )
    assert len(rows) == 1
    assert rows[0].status == "snoozed"


def test_surfaced_findings_appear_in_both_modes(
    test_db: psycopg.Connection,
) -> None:
    """The flag must widen the result set, never replace it."""
    doc = _seed_doc(test_db, title="Target", age_days=500, summary="s")
    _seed_finding(test_db, target_id=doc, snoozed_days=None)
    default = list_review_queue(
        test_db, tenant_id=TENANT, signal_kinds=["stale"], limit=50
    )
    widened = list_review_queue(
        test_db,
        tenant_id=TENANT,
        signal_kinds=["stale"],
        limit=50,
        include_snoozed=True,
    )
    assert len(default) == 1
    assert len(widened) == 1


def test_expired_snooze_reappears_without_the_flag(
    test_db: psycopg.Connection,
) -> None:
    """Snoozes self-heal — which is why no un-snooze verb is needed."""
    doc = _seed_doc(test_db, title="Target", age_days=500, summary="s")
    test_db.execute(
        """
        INSERT INTO elicitation_gaps
            (tenant_id, signal_kind, target_type, target_id, score,
             evidence_ids, rationale, status, snoozed_until)
        VALUES (%s, 'stale', 'doc', %s, 0.9, %s, 'qa', 'snoozed',
                now() - interval '1 day')
        """,
        (TENANT, doc, [doc]),
    )
    rows = list_review_queue(
        test_db, tenant_id=TENANT, signal_kinds=["stale"], limit=50
    )
    assert len(rows) == 1
