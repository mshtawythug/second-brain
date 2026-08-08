"""Tests for ``brain.timeline`` (Plan 05 — temporal evolution).

Three layers:

* **Unit** — pure helpers (granularity/trim validation, month parsing, bucket
  labels, limit trimming) + the ``doc_date`` column detection via a fake conn
  double + ``OllamaEnricher.summarize_bucket`` via ``httpx.MockTransport``.
* **Integration** — ``build_timeline`` against the real Postgres test DB:
  roundtrip bucketing, COALESCE fallback, since/until, multi-entity merge,
  co-topic suppression, person scope, graceful empties.
* **CLI** — ``brain timeline`` via Typer's ``CliRunner``: JSON output, graph-off
  gate, unknown-entity grace.

Seeding inserts ``graph_entities`` / ``graph_entity_mentions`` /
``graph_edge_contributions`` / ``directory_entries`` directly (parameterized)
and uses the ``seed_doc`` fixture for real documents, then UPDATEs ``sent_at`` /
``ingested_at`` / ``participants`` for full temporal control. Document bodies are
kept unique per doc (the manual-ingest path dedups by content hash). All names
are synthetic (rule 15).
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from mcp.types import INVALID_PARAMS
from typer.testing import CliRunner

from brain import mcp_server
from brain import timeline as timeline_mod
from brain.cli import app
from brain.config import Config
from brain.enrichment import OllamaEnricher
from brain.format import (
    _timeline_header,
    timeline_context_json,
    timeline_renderable,
)
from brain.mcp_compat import MCPError
from brain.timeline import (
    TimelineBucket,
    TimelineContext,
    _add_one_month,
    _bucket_label,
    _budget_doc_summaries,
    _doc_date_expr,
    _parse_month,
    _resolve_auto_granularity,
    _trim_buckets,
    _validate_granularity,
    _validate_trim,
    build_timeline,
)

TENANT = "default"
runner = CliRunner()


# ---------------------------------------------------------------------------
# Seeding helpers (real DB).
# ---------------------------------------------------------------------------


def _dt(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _insert_entity(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    etype: str = "topic",
    canonical_key: str | None = None,
    tenant: str = TENANT,
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        (tenant, etype, name, canonical_key or name.lower()),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_directory(
    conn: psycopg.Connection[Any], *, display_name: str, email: str
) -> None:
    """Insert a directory_entries row so a person resolves via the directory."""
    conn.execute(
        "INSERT INTO directory_entries (display_name, email, source) "
        "VALUES (%s, %s, 'people_yml')",
        (display_name.lower(), email.lower()),
    )


def _set_doc_dates(
    conn: psycopg.Connection[Any],
    doc_id: str,
    *,
    sent_at: datetime | None = None,
    ingested_at: datetime | None = None,
    participants: list[str] | None = None,
) -> None:
    conn.execute(
        "UPDATE documents SET sent_at = %s, "
        "ingested_at = COALESCE(%s, ingested_at), participants = %s WHERE id = %s",
        (sent_at, ingested_at, participants, doc_id),
    )


def _doc(
    seed_doc: Callable[..., str],
    conn: psycopg.Connection[Any],
    title: str,
    *,
    content_type: str = "note",
    sent_at: datetime | None = None,
    ingested_at: datetime | None = None,
    participants: list[str] | None = None,
) -> str:
    """Seed a document with a unique body, then set its temporal columns.

    The unique ``content`` matters: the manual-ingest path dedups by content
    hash, so two docs sharing a body would collapse to one row.
    """
    doc_id = seed_doc(
        title=title, content=f"Body of {title}.", content_type=content_type
    )
    _set_doc_dates(
        conn, doc_id, sent_at=sent_at, ingested_at=ingested_at, participants=participants
    )
    return doc_id


def _mention(
    conn: psycopg.Connection[Any],
    entity_id: str,
    doc_id: str,
    *,
    count: int = 1,
    tenant: str = TENANT,
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, %s, 'test')",
        (tenant, entity_id, doc_id, count),
    )


def _edge(
    conn: psycopg.Connection[Any],
    doc_id: str,
    a: str,
    b: str,
    *,
    count: int = 1,
    tenant: str = TENANT,
) -> None:
    lo, hi = sorted([a, b])
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant, doc_id, lo, hi, count),
    )


def _cfg(**overrides: Any) -> Config:
    """Load the real config and apply field overrides (frozen → replace)."""
    return dataclasses.replace(Config.load(), **overrides)


# ===========================================================================
# Unit — pure helpers
# ===========================================================================


@pytest.mark.parametrize("value", ["auto", "month", "QUARTER", " year "])
def test_validate_granularity_accepts_valid(value: str) -> None:
    assert _validate_granularity(value) in {"auto", "month", "quarter", "year"}


def test_validate_granularity_accepts_auto() -> None:
    assert _validate_granularity("AUTO") == "auto"


def test_validate_granularity_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="auto/month/quarter/year"):
        _validate_granularity("week")


# --- _resolve_auto_granularity (pure) --------------------------------------


def test_auto_granularity_empty_dates_is_month() -> None:
    # No docs → finest fallback so a future/sparse corpus still shows month view.
    assert _resolve_auto_granularity([]) == "month"


def test_auto_granularity_five_month_span_picks_month() -> None:
    # The reported failure: a ~5-month span. year=1, quarter<3, month<3 → fall
    # back to month (the finest available), NOT the degenerate single quarter.
    dates = [_dt(2024, m, 5) for m in (1, 2, 3, 4, 5)]
    # 5 distinct months (>=3) but only 2 quarters and 1 year.
    assert _resolve_auto_granularity(dates) == "month"


def test_auto_granularity_two_distinct_months_picks_month() -> None:
    # Exactly the user's "co-topic shift across 2 buckets": 2 months, all <3 →
    # month fallback (2 buckets beats 1 collapsed quarter).
    dates = [_dt(2024, 3, 1), _dt(2024, 3, 20), _dt(2024, 4, 2)]
    assert _resolve_auto_granularity(dates) == "month"


def test_auto_granularity_prefers_coarsest_clearing_bar_quarter() -> None:
    # >=3 quarters but <3 years → quarter (coarsest clearing >=3), avoiding the
    # finer month view's fragmentation.
    dates = [_dt(2024, 1, 1), _dt(2024, 4, 1), _dt(2024, 7, 1), _dt(2024, 10, 1)]
    assert _resolve_auto_granularity(dates) == "quarter"


def test_auto_granularity_prefers_year_for_multi_year_span() -> None:
    # >=3 distinct years → year (coarsest), not 36 month buckets.
    dates = [_dt(y, 6, 1) for y in (2022, 2023, 2024)]
    assert _resolve_auto_granularity(dates) == "year"


# --- _budget_doc_summaries (pure) ------------------------------------------


def _word_count(text: str) -> int:
    return len(text.split())


def test_budget_doc_summaries_uses_summary_when_present() -> None:
    rows = [("Title A", "Concrete summary of A"), ("Title B", "Concrete summary of B")]
    out = _budget_doc_summaries(rows, count_tokens=_word_count, max_tokens=100)
    assert out == ["Concrete summary of A", "Concrete summary of B"]


def test_budget_doc_summaries_falls_back_to_title_when_summary_null() -> None:
    rows = [("Title A", None), ("Title B", "   ")]
    out = _budget_doc_summaries(rows, count_tokens=_word_count, max_tokens=100)
    assert out == ["Title A", "Title B"]


def test_budget_doc_summaries_truncates_at_budget_but_keeps_first() -> None:
    rows = [
        ("T1", "one two three four"),  # 4 tokens
        ("T2", "five six seven eight"),  # would exceed a 5-token budget
    ]
    out = _budget_doc_summaries(rows, count_tokens=_word_count, max_tokens=5)
    assert out == ["one two three four"]  # first kept, second dropped


def test_budget_doc_summaries_first_always_included_even_if_over_budget() -> None:
    rows = [("T1", "a b c d e f")]  # 6 tokens > budget 2
    out = _budget_doc_summaries(rows, count_tokens=_word_count, max_tokens=2)
    assert out == ["a b c d e f"]  # never empty when docs exist


def test_budget_doc_summaries_empty_rows() -> None:
    assert _budget_doc_summaries([], count_tokens=_word_count, max_tokens=100) == []


def test_validate_trim_accepts_and_rejects() -> None:
    assert _validate_trim("OLDEST") == "oldest"
    with pytest.raises(ValueError, match="oldest/sparsest"):
        _validate_trim("middle")


def test_parse_month_valid() -> None:
    parsed = _parse_month("2024-03", field_name="since")
    assert parsed == _dt(2024, 3, 1)


def test_parse_month_invalid() -> None:
    with pytest.raises(ValueError, match="ISO month"):
        _parse_month("2024/03", field_name="until")


def test_add_one_month_within_year() -> None:
    assert _add_one_month(_dt(2024, 3)) == _dt(2024, 4)


def test_add_one_month_rolls_over_december() -> None:
    assert _add_one_month(_dt(2024, 12)) == _dt(2025, 1)


@pytest.mark.parametrize(
    "month,granularity,expected",
    [
        (3, "month", "2024-03"),
        (1, "quarter", "2024-Q1"),
        (4, "quarter", "2024-Q2"),
        (12, "quarter", "2024-Q4"),
        (7, "year", "2024"),
    ],
)
def test_bucket_label(month: int, granularity: str, expected: str) -> None:
    assert _bucket_label(_dt(2024, month), granularity) == expected


def _bucket(label: str, start: datetime, docs: int) -> TimelineBucket:
    return TimelineBucket(
        bucket=label, bucket_start=start, doc_count=docs, mention_count=docs
    )


def test_trim_buckets_under_limit_keeps_all() -> None:
    buckets = [_bucket("2024-Q1", _dt(2024, 1), 1), _bucket("2024-Q2", _dt(2024, 4), 2)]
    kept, omitted = _trim_buckets(buckets, 5, "oldest")
    assert omitted == 0
    assert [b.bucket for b in kept] == ["2024-Q1", "2024-Q2"]


def test_limit_trim_oldest() -> None:
    # Five ascending buckets (months 1..5 are arbitrary distinct anchors).
    buckets = [_bucket(f"b{i}", _dt(2024, i), i) for i in range(1, 6)]
    kept, omitted = _trim_buckets(buckets, 3, "oldest")
    assert omitted == 2
    # The 3 most-recent buckets, still ascending.
    assert [b.bucket for b in kept] == ["b3", "b4", "b5"]


def test_limit_trim_sparsest() -> None:
    # doc_counts: b1=5, b2=1, b3=2, b4=4 — limit 2 drops the 2 sparsest (b2, b3).
    buckets = [
        _bucket("b1", _dt(2024, 1), 5),
        _bucket("b2", _dt(2024, 2), 1),
        _bucket("b3", _dt(2024, 3), 2),
        _bucket("b4", _dt(2024, 4), 4),
    ]
    kept, omitted = _trim_buckets(buckets, 2, "sparsest")
    assert omitted == 2
    assert [b.bucket for b in kept] == ["b1", "b4"]


# --- doc_date column detection (fake conn double — no DB) -------------------


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeConn:
    """Minimal conn double: returns a fixed row from ``execute().fetchone()``."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def execute(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self._row)


def test_doc_date_expr_uses_generated_column_when_present() -> None:
    assert _doc_date_expr(_FakeConn((1,))) == "d.doc_date"  # type: ignore[arg-type]


def test_doc_date_expr_falls_back_to_coalesce_when_absent() -> None:
    assert (
        _doc_date_expr(_FakeConn(None))  # type: ignore[arg-type]
        == "COALESCE(d.sent_at, d.ingested_at)"
    )


# --- summarize_bucket (MockTransport) --------------------------------------


def _enricher(transport: httpx.MockTransport) -> OllamaEnricher:
    return OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=httpx.Client(base_url="http://x", transport=transport),
    )


def test_summarize_bucket_returns_text_on_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps({"summary": "Q1 ramp-up."})}},
        )

    enricher = _enricher(httpx.MockTransport(handler))
    out = enricher.summarize_bucket(
        bucket_label="2024-Q1",
        entity_name="remote-work",
        doc_titles=["Sync notes"],
        cotopics=["async-comms"],
    )
    assert out == "Q1 ramp-up."


def test_summarize_bucket_returns_none_on_ollama_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    enricher = _enricher(httpx.MockTransport(handler))
    assert (
        enricher.summarize_bucket(
            bucket_label="2024-Q1",
            entity_name="remote-work",
            doc_titles=["Sync notes"],
            cotopics=[],
        )
        is None
    )


def test_summarize_bucket_returns_none_on_empty_summary() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": json.dumps({"summary": "  "})}})

    enricher = _enricher(httpx.MockTransport(handler))
    assert (
        enricher.summarize_bucket(
            bucket_label="2024-Q1", entity_name="x", doc_titles=[], cotopics=[]
        )
        is None
    )


def test_summarize_bucket_grounds_prompt_in_doc_summaries() -> None:
    # The grounding fix: the doc SUMMARIES (not just titles) reach the model.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"summary": "ok"})}}
        )

    enricher = _enricher(httpx.MockTransport(handler))
    enricher.summarize_bucket(
        bucket_label="2024-03",
        entity_name="migration-project",
        doc_titles=["Kickoff note"],
        cotopics=["data-pipeline"],
        doc_summaries=["Decided to migrate the billing service to the new schema."],
    )
    user_turn = captured["body"]["messages"][-1]["content"]
    assert "DOCUMENT SUMMARIES:" in user_turn
    assert "migrate the billing service" in user_turn


def test_summarize_bucket_includes_previous_period_context() -> None:
    # Evolution contrast: the previous bucket's label / co-topics / synthesis
    # are handed to the model so it can state what changed.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"summary": "ok"})}}
        )

    enricher = _enricher(httpx.MockTransport(handler))
    enricher.summarize_bucket(
        bucket_label="2024-04",
        entity_name="migration-project",
        doc_titles=["Phase 2 review"],
        cotopics=["rollout"],
        doc_summaries=["Rolled out the migrated service to production."],
        prev_bucket_label="2024-03",
        prev_cotopics=["data-pipeline"],
        prev_synthesis="Designed the new schema and validated it in staging.",
    )
    user_turn = captured["body"]["messages"][-1]["content"]
    assert "PREVIOUS PERIOD: 2024-03" in user_turn
    assert "PREVIOUS SYNTHESIS: Designed the new schema" in user_turn


def test_summarize_bucket_omits_previous_block_when_absent() -> None:
    # Oldest bucket (no prior period): no PREVIOUS PERIOD block in the prompt.
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"summary": "ok"})}}
        )

    enricher = _enricher(httpx.MockTransport(handler))
    enricher.summarize_bucket(
        bucket_label="2024-03",
        entity_name="x",
        doc_titles=["t"],
        cotopics=[],
        doc_summaries=["s"],
    )
    user_turn = captured["body"]["messages"][-1]["content"]
    assert "PREVIOUS PERIOD" not in user_turn


# ===========================================================================
# Integration — build_timeline against the real DB
# ===========================================================================


def test_timeline_roundtrip_with_real_db(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="strategic-planning")
    cotopic = _insert_entity(test_db, name="async-comms")
    # Q1: 1 doc (note, sent_at). Q2: 2 docs (transcript). Q3: 3 docs (email-thread).
    plan = [
        ("2024 plan kickoff", _dt(2024, 1, 15), "note"),
        ("2024 plan check-in A", _dt(2024, 4, 10), "transcript"),
        ("2024 plan check-in B", _dt(2024, 5, 20), "transcript"),
        ("2024 plan review A", _dt(2024, 7, 5), "email-thread"),
        ("2024 plan review B", _dt(2024, 8, 9), "email-thread"),
        ("2024 plan review C", _dt(2024, 9, 30), "email-thread"),
    ]
    for title, sent_at, ctype in plan:
        doc_id = _doc(seed_doc, test_db, title, content_type=ctype, sent_at=sent_at)
        _mention(test_db, entity, doc_id, count=2)
        if sent_at.month >= 7:
            _mention(test_db, cotopic, doc_id)
            _edge(test_db, doc_id, entity, cotopic, count=3)

    ctx = build_timeline(test_db, _cfg(), "strategic-planning", granularity="quarter")

    assert ctx.entity_names == ["strategic-planning"]
    assert [b.bucket for b in ctx.buckets] == ["2024-Q1", "2024-Q2", "2024-Q3"]
    assert [b.doc_count for b in ctx.buckets] == [1, 2, 3]
    # mention_count = 2 per doc.
    assert [b.mention_count for b in ctx.buckets] == [2, 4, 6]
    # Co-topic only co-occurs in Q3.
    assert ctx.buckets[2].cotopics == ["async-comms"]
    assert ctx.buckets[0].cotopics == []
    assert len(ctx.buckets[2].doc_titles) == 3


def test_coalesce_fallback_to_ingested_at(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="okrs")
    # No sent_at — buckets by ingested_at (Q1).
    doc_id = _doc(seed_doc, test_db, "OKR notes", sent_at=None, ingested_at=_dt(2024, 2, 10))
    _mention(test_db, entity, doc_id)

    ctx = build_timeline(test_db, _cfg(), "okrs", granularity="quarter")

    assert [b.bucket for b in ctx.buckets] == ["2024-Q1"]
    assert ctx.buckets[0].doc_count == 1


@pytest.mark.fresh_schema
def test_build_timeline_inline_coalesce_when_doc_date_absent(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """Exercise the real inline-COALESCE query path (pre-migration-021 DBs).

    The migration runner always applies 021 to the test DB, so the default
    integration tests run with ``documents.doc_date`` present. Drop it here to
    prove ``build_timeline`` transparently falls back to the inline
    ``COALESCE(sent_at, ingested_at)`` expression — the contract for a DB that
    has not yet applied migration 021 (the column is auto-removed back the next
    test via the per-test schema reset).
    """
    # Dropping the generated column also drops its dependent index.
    test_db.execute("ALTER TABLE documents DROP COLUMN IF EXISTS doc_date")
    assert _doc_date_expr(test_db) == "COALESCE(d.sent_at, d.ingested_at)"

    entity = _insert_entity(test_db, name="okrs")
    by_sent = _doc(seed_doc, test_db, "sent doc", sent_at=_dt(2024, 2, 10))  # Q1
    by_ingest = _doc(
        seed_doc, test_db, "ingest doc", sent_at=None, ingested_at=_dt(2024, 5, 1)
    )  # Q2 via fallback
    _mention(test_db, entity, by_sent)
    _mention(test_db, entity, by_ingest)

    ctx = build_timeline(test_db, _cfg(), "okrs", granularity="quarter")
    assert [b.bucket for b in ctx.buckets] == ["2024-Q1", "2024-Q2"]
    assert [b.doc_count for b in ctx.buckets] == [1, 1]


def test_since_until_filter(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="hiring")
    for title, sent_at in [
        ("late 2023", _dt(2023, 11, 1)),
        ("early 2024", _dt(2024, 2, 1)),
        ("mid 2024", _dt(2024, 5, 1)),
    ]:
        doc_id = _doc(seed_doc, test_db, title, sent_at=sent_at)
        _mention(test_db, entity, doc_id)

    # since 2024-01 inclusive, until 2024-03 inclusive-of-March → only Q1 2024.
    ctx = build_timeline(
        test_db, _cfg(), "hiring", granularity="quarter", since="2024-01", until="2024-03"
    )
    assert [b.bucket for b in ctx.buckets] == ["2024-Q1"]
    assert ctx.buckets[0].doc_count == 1


def test_bucket_merge_multiple_entities(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    # Two entities both match "remote"; a shared doc is counted ONCE per bucket.
    e1 = _insert_entity(test_db, name="remote-work")
    e2 = _insert_entity(test_db, name="remote-team")
    shared = _doc(seed_doc, test_db, "Shared remote doc", sent_at=_dt(2024, 1, 10))
    only_e2 = _doc(seed_doc, test_db, "Remote team only", sent_at=_dt(2024, 2, 20))
    _mention(test_db, e1, shared, count=1)
    _mention(test_db, e2, shared, count=1)
    _mention(test_db, e2, only_e2, count=1)

    ctx = build_timeline(test_db, _cfg(), "remote", granularity="quarter")

    assert set(ctx.entity_names) == {"remote-work", "remote-team"}
    assert [b.bucket for b in ctx.buckets] == ["2024-Q1"]
    # 2 distinct docs in Q1 (shared counted once), 3 mentions total.
    assert ctx.buckets[0].doc_count == 2
    assert ctx.buckets[0].mention_count == 3


def test_cotopics_excludes_seed_and_owner(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    seed = _insert_entity(test_db, name="remote-work")
    cotopic = _insert_entity(test_db, name="async-comms")
    owner = _insert_entity(
        test_db, name="Participant Owner", etype="person", canonical_key="participant owner"
    )
    doc_id = _doc(
        seed_doc, test_db, "Owner sync", content_type="transcript", sent_at=_dt(2024, 1, 10)
    )
    _mention(test_db, seed, doc_id)
    _edge(test_db, doc_id, seed, cotopic, count=2)
    _edge(test_db, doc_id, seed, owner, count=5)  # owner co-occurs heavily

    ctx = build_timeline(
        test_db,
        _cfg(owner_participants=frozenset({"participant owner"})),
        "remote-work",
        granularity="quarter",
    )

    assert ctx.buckets[0].cotopics == ["async-comms"]  # owner + seed suppressed


def test_zero_entities_graceful(test_db: psycopg.Connection[Any]) -> None:
    ctx = build_timeline(test_db, _cfg(), "nothing-matches-this", granularity="quarter")
    assert ctx.entity_names == []
    assert ctx.buckets == []


def test_no_buckets_when_no_mentions(test_db: psycopg.Connection[Any]) -> None:
    # Entity exists but has zero mentions → resolved, but no buckets.
    _insert_entity(test_db, name="lonely-topic")
    ctx = build_timeline(test_db, _cfg(), "lonely-topic", granularity="quarter")
    assert ctx.entity_names == ["lonely-topic"]
    assert ctx.buckets == []


def test_timeline_person_scope(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="remote-work")
    _seed_directory(test_db, display_name="participant alpha", email="alpha@example.com")
    with_person = _doc(
        seed_doc,
        test_db,
        "1:1 sync",
        content_type="transcript",
        sent_at=_dt(2024, 1, 10),
        # Mixed-case participant (Gmail stores source-preserved case) — the
        # lowercased PersonMatch.keys must still overlap (regression for the
        # case-sensitive `&&` bug Codex caught).
        participants=["Alpha@Example.com"],
    )
    without_person = _doc(seed_doc, test_db, "Solo note", sent_at=_dt(2024, 1, 20))
    _mention(test_db, entity, with_person)
    _mention(test_db, entity, without_person)

    # Unscoped: both docs.
    unscoped = build_timeline(test_db, _cfg(), "remote-work", granularity="quarter")
    assert unscoped.buckets[0].doc_count == 2

    # Scoped to the person (by email): only the doc they participate in.
    scoped = build_timeline(
        test_db, _cfg(), "remote-work", granularity="quarter", person="alpha@example.com"
    )
    assert scoped.person is not None
    assert [b.doc_count for b in scoped.buckets] == [1]


def test_person_scope_empty_returns_empty(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="remote-work")
    _seed_directory(test_db, display_name="participant beta", email="beta@example.com")
    # The person participates in a doc that does NOT mention the theme.
    _doc(
        seed_doc,
        test_db,
        "Known person doc",
        content_type="transcript",
        sent_at=_dt(2024, 1, 5),
        participants=["beta@example.com"],
    )
    other = _doc(seed_doc, test_db, "Theme doc", sent_at=_dt(2024, 1, 6))
    _mention(test_db, entity, other)  # theme doc has no participant overlap

    ctx = build_timeline(
        test_db, _cfg(), "remote-work", granularity="quarter", person="beta@example.com"
    )
    assert ctx.person is not None
    assert ctx.buckets == []


def test_limit_trims_buckets_in_build(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="quarterly")
    for i in range(1, 5):  # 4 quarters
        doc_id = _doc(seed_doc, test_db, f"Q{i} doc", sent_at=_dt(2024, (i - 1) * 3 + 1, 5))
        _mention(test_db, entity, doc_id)

    ctx = build_timeline(test_db, _cfg(), "quarterly", granularity="quarter", limit=2)
    assert len(ctx.buckets) == 2
    assert ctx.buckets_omitted == 2
    # Default trim=oldest keeps the two most recent.
    assert [b.bucket for b in ctx.buckets] == ["2024-Q3", "2024-Q4"]


def test_invalid_limit_raises(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_timeline(test_db, _cfg(), "x", limit=0)


def test_synthesize_attaches_to_densest_bucket(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="okrs")
    # Q1: 1 doc, Q2: 2 docs (denser).
    for title, sent_at in [
        ("q1 doc", _dt(2024, 1, 5)),
        ("q2 doc a", _dt(2024, 4, 5)),
        ("q2 doc b", _dt(2024, 5, 5)),
    ]:
        doc_id = _doc(seed_doc, test_db, title, sent_at=sent_at)
        _mention(test_db, entity, doc_id)

    class _FakeEnricher:
        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def summarize_bucket(self, **_kwargs: Any) -> str:
            return "synthetic summary"

    ctx = build_timeline(
        test_db,
        _cfg(timeline_synth_limit=1),
        "okrs",
        granularity="quarter",
        synthesize=True,
        enricher=_FakeEnricher(),  # type: ignore[arg-type]
    )
    by_label = {b.bucket: b for b in ctx.buckets}
    assert by_label["2024-Q2"].synthesis == "synthetic summary"
    assert by_label["2024-Q1"].synthesis is None


def test_synthesize_grounds_in_doc_summaries_and_prev_context(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """build_timeline feeds doc SUMMARIES + previous-period context to the enricher.

    Synthesizes oldest→newest across two synthesized buckets and records the
    kwargs each call received, proving (a) the docs' ``summary`` values reach
    ``summarize_bucket`` as ``doc_summaries`` (title fallback when NULL) and
    (b) bucket N>0 gets the prior bucket's label + already-generated synthesis.
    """
    entity = _insert_entity(test_db, name="migration")
    # Q1 doc has a real summary; Q2 doc has none (title fallback).
    q1 = _doc(seed_doc, test_db, "Q1 kickoff", sent_at=_dt(2024, 1, 10))
    q2 = _doc(seed_doc, test_db, "Q2 rollout", sent_at=_dt(2024, 4, 10))
    test_db.execute(
        "UPDATE documents SET summary = %s WHERE id = %s",
        ("Designed the new billing schema.", q1),
    )
    _mention(test_db, entity, q1)
    _mention(test_db, entity, q2)

    calls: list[dict[str, Any]] = []

    class _RecordingEnricher:
        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def summarize_bucket(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return f"synthesis-for-{kwargs['bucket_label']}"

    ctx = build_timeline(
        test_db,
        _cfg(timeline_synth_limit=5),  # synthesize both buckets
        "migration",
        granularity="quarter",
        synthesize=True,
        enricher=_RecordingEnricher(),  # type: ignore[arg-type]
    )

    # Oldest→newest order, both synthesized.
    assert [c["bucket_label"] for c in calls] == ["2024-Q1", "2024-Q2"]
    # Q1: grounded in its real summary; no previous period.
    assert calls[0]["doc_summaries"] == ["Designed the new billing schema."]
    assert calls[0]["prev_bucket_label"] is None
    # Q2: summary NULL → title fallback; previous period carried forward.
    assert calls[1]["doc_summaries"] == ["Q2 rollout"]
    assert calls[1]["prev_bucket_label"] == "2024-Q1"
    assert calls[1]["prev_synthesis"] == "synthesis-for-2024-Q1"
    by_label = {b.bucket: b for b in ctx.buckets}
    assert by_label["2024-Q2"].synthesis == "synthesis-for-2024-Q2"


def test_synthesis_skipped_when_synth_limit_zero(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="okrs")
    doc_id = _doc(seed_doc, test_db, "q1 only", sent_at=_dt(2024, 1, 5))
    _mention(test_db, entity, doc_id)

    class _BoomEnricher:
        def summarize_bucket(self, **_kwargs: Any) -> str:
            raise AssertionError("must not be called when synth_limit == 0")

    ctx = build_timeline(
        test_db,
        _cfg(timeline_synth_limit=0),
        "okrs",
        granularity="quarter",
        synthesize=True,
        enricher=_BoomEnricher(),  # type: ignore[arg-type]
    )
    assert ctx.buckets[0].synthesis is None


def test_build_timeline_auto_picks_month_for_short_span(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The headline fix: a ~5-month span under the default 'auto' shows month
    buckets (evolution visible), not one collapsed quarter."""
    entity = _insert_entity(test_db, name="regional-migration")
    for title, sent_at in [
        ("jan", _dt(2024, 1, 10)),
        ("feb", _dt(2024, 2, 10)),
        ("mar", _dt(2024, 3, 10)),
        ("apr", _dt(2024, 4, 10)),
        ("may", _dt(2024, 5, 10)),
    ]:
        doc_id = _doc(seed_doc, test_db, title, sent_at=sent_at)
        _mention(test_db, entity, doc_id)

    ctx = build_timeline(test_db, _cfg(), "regional-migration", granularity="auto")
    assert ctx.granularity == "month"
    assert ctx.granularity_auto is True
    assert [b.bucket for b in ctx.buckets] == [
        "2024-01",
        "2024-02",
        "2024-03",
        "2024-04",
        "2024-05",
    ]


def test_build_timeline_auto_picks_quarter_for_full_year(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    # >=3 quarters, <3 years → auto resolves to quarter (coarsest clearing >=3).
    entity = _insert_entity(test_db, name="annual")
    for month in (1, 4, 7, 10):
        doc_id = _doc(seed_doc, test_db, f"m{month}", sent_at=_dt(2024, month, 5))
        _mention(test_db, entity, doc_id)

    ctx = build_timeline(test_db, _cfg(), "annual", granularity="auto")
    assert ctx.granularity == "quarter"
    assert ctx.granularity_auto is True
    assert len(ctx.buckets) == 4


def test_build_timeline_explicit_granularity_not_auto_flagged(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    # Explicit --granularity forces it exactly; granularity_auto stays False.
    entity = _insert_entity(test_db, name="forced")
    doc_id = _doc(seed_doc, test_db, "doc", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    ctx = build_timeline(test_db, _cfg(), "forced", granularity="quarter")
    assert ctx.granularity == "quarter"
    assert ctx.granularity_auto is False
    assert ctx.buckets[0].bucket == "2024-Q1"


def test_build_timeline_auto_unknown_entity_resolves_concrete_granularity(
    test_db: psycopg.Connection[Any],
) -> None:
    # No entities + auto → empty context with a concrete (month) granularity,
    # never the unresolved 'auto' sentinel, and the auto flag set.
    ctx = build_timeline(test_db, _cfg(), "no-such-entity-xyz", granularity="auto")
    assert ctx.granularity == "month"
    assert ctx.granularity_auto is True
    assert ctx.buckets == []


def test_empty_helpers_short_circuit(test_db: psycopg.Connection[Any]) -> None:
    # Direct guards: empty doc/seed inputs return [] without touching real rows.
    assert timeline_mod._top_cotopics(test_db, TENANT, [], ["x"], []) == []
    assert timeline_mod._bucket_doc_titles(test_db, []) == []
    assert timeline_mod._bucket_doc_summaries(test_db, []) == []
    assert timeline_mod._scope_to_person(test_db, TENANT, [], ["k"]) == []
    assert timeline_mod._owner_entity_ids(test_db, TENANT, frozenset()) == []


def test_synthesize_buckets_empty_short_circuits() -> None:
    # Defensive guard: an empty bucket list returns unchanged without the enricher.
    class _BoomEnricher:
        def summarize_bucket(self, **_kwargs: Any) -> str:
            raise AssertionError("enricher must not be called for an empty list")

    def _boom_fetch(_doc_ids: list[str]) -> list[str]:
        raise AssertionError("fetch_summaries must not be called for an empty list")

    out = timeline_mod._synthesize_buckets(
        [],
        entity_name="x",
        enricher=_BoomEnricher(),  # type: ignore[arg-type]
        synth_limit=1,
        fetch_summaries=_boom_fetch,
    )
    assert out == []


# ===========================================================================
# Format — timeline header / renderable (pure, no DB)
# ===========================================================================


def _ctx(entity_names: list[str], *, query: str = "hub-theme") -> TimelineContext:
    """Build a minimal TimelineContext for header rendering (synthetic names)."""
    return TimelineContext(
        query=query,
        tenant_id=TENANT,
        granularity="quarter",
        entity_names=entity_names,
    )


def test_timeline_header_shows_auto_marker() -> None:
    ctx = TimelineContext(
        query="q",
        tenant_id=TENANT,
        granularity="month",
        granularity_auto=True,
        entity_names=["topic"],
    )
    assert "month (auto)" in _timeline_header(ctx).plain


def test_timeline_header_no_auto_marker_when_explicit() -> None:
    ctx = TimelineContext(
        query="q",
        tenant_id=TENANT,
        granularity="quarter",
        granularity_auto=False,
        entity_names=["topic"],
    )
    header = _timeline_header(ctx).plain
    assert "quarter" in header
    assert "(auto)" not in header


def test_timeline_json_includes_granularity_auto() -> None:
    ctx = TimelineContext(
        query="q",
        tenant_id=TENANT,
        granularity="month",
        granularity_auto=True,
        entity_names=["topic"],
    )
    payload = timeline_context_json(ctx)
    assert payload["granularity"] == "month"
    assert payload["granularity_auto"] is True


def test_timeline_header_three_entities_unchanged() -> None:
    # <=3 matched entities: every name shown, no overflow note (no behavior change).
    header = _timeline_header(_ctx(["alpha-topic", "beta-topic", "gamma-topic"])).plain
    assert "alpha-topic, beta-topic, gamma-topic" in header
    assert "more matched entities" not in header


def test_timeline_header_single_entity_unchanged() -> None:
    header = _timeline_header(_ctx(["solo-topic"])).plain
    assert "solo-topic" in header
    assert "more matched entities" not in header


def test_timeline_header_caps_entities_with_overflow_note() -> None:
    # Regression: a hub theme that expands to many aliases must NOT dump every
    # name (sensitive alias strings leaked into the header). Show first 3 + count.
    names = [f"alias-{i:02d}" for i in range(30)]
    header = _timeline_header(_ctx(names)).plain
    assert "alias-00, alias-01, alias-02" in header
    assert "(+27 more matched entities)" in header
    # The capped-away aliases never appear in the human header.
    assert "alias-03" not in header
    assert "alias-29" not in header


def test_timeline_header_empty_entities_falls_back_to_query() -> None:
    header = _timeline_header(_ctx([], query="my-query")).plain
    assert "my-query" in header
    assert "more matched entities" not in header


def test_timeline_renderable_caps_header_entities() -> None:
    # End-to-end through the renderable: the capped header is what reaches output.
    from rich.console import Console

    names = [f"alias-{i:02d}" for i in range(10)]
    console = Console(width=200, no_color=True)
    with console.capture() as capture:
        console.print(timeline_renderable(_ctx(names)))
    text = capture.get()
    assert "alias-00, alias-01, alias-02" in text
    assert "(+7 more matched entities)" in text
    assert "alias-09" not in text


def test_timeline_json_keeps_full_entity_names() -> None:
    # --json / MCP wire shape is UNCHANGED: full entity_names list survives.
    names = [f"alias-{i:02d}" for i in range(30)]
    payload = timeline_context_json(_ctx(names))
    assert payload["entity_names"] == names
    assert len(payload["entity_names"]) == 30


# ===========================================================================
# CLI — brain timeline
# ===========================================================================


def test_cli_timeline_json_output(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    # Explicit --granularity quarter keeps this focused on the JSON plumbing.
    result = runner.invoke(
        app, ["timeline", "productivity", "--granularity", "quarter", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "productivity"
    assert payload["granularity"] == "quarter"
    assert payload["granularity_auto"] is False
    assert payload["entity_names"] == ["productivity"]
    assert payload["buckets"][0]["bucket"] == "2024-Q1"
    assert payload["buckets"][0]["doc_count"] == 1
    assert "bucket_start" in payload["buckets"][0]


def test_cli_timeline_auto_default_json(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    # Default (no --granularity) → 'auto'. One doc → month, flagged auto.
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    result = runner.invoke(app, ["timeline", "productivity", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["granularity"] == "month"
    assert payload["granularity_auto"] is True
    assert payload["buckets"][0]["bucket"] == "2024-03"


def test_cli_timeline_human_output(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    # Default auto → month; header shows the auto marker.
    result = runner.invoke(app, ["timeline", "productivity"])
    assert result.exit_code == 0, result.output
    assert "Timeline" in result.output
    assert "month (auto)" in result.output
    assert "2024-03" in result.output


def test_cli_timeline_no_graph(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    result = runner.invoke(app, ["timeline", "anything"])
    assert result.exit_code == 1
    assert "requires the graph" in result.output


def test_cli_timeline_unknown_entity(test_db: psycopg.Connection[Any]) -> None:
    result = runner.invoke(app, ["timeline", "no-such-entity-xyz"])
    assert result.exit_code == 0
    assert "No entities found" in result.output


def test_cli_timeline_bad_granularity(test_db: psycopg.Connection[Any]) -> None:
    result = runner.invoke(app, ["timeline", "x", "--granularity", "week"])
    assert result.exit_code == 2  # BadParameter
    assert "auto/month/quarter/year" in result.output


# ===========================================================================
# MCP — brain_timeline tool
# ===========================================================================


def _mcp_state(
    monkeypatch: pytest.MonkeyPatch, fake_embedder: object, *, graph_enabled: bool = True
) -> None:
    from tests.conftest import TEST_DATABASE_URL as _DB

    state = mcp_server._State(
        cfg=Config(database_url=_DB, graph_enabled=graph_enabled),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)


def test_mcp_timeline_returns_buckets(
    test_db: psycopg.Connection[Any],
    seed_doc: Callable[..., str],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)
    _mcp_state(monkeypatch, fake_embedder)

    # Explicit granularity keeps this on the MCP wire-shape plumbing.
    payload = mcp_server.brain_timeline(query="productivity", granularity="quarter")
    assert payload["entity_names"] == ["productivity"]
    assert payload["buckets"][0]["bucket"] == "2024-Q1"
    assert payload["granularity"] == "quarter"
    assert payload["granularity_auto"] is False


def test_mcp_timeline_auto_default(
    test_db: psycopg.Connection[Any],
    seed_doc: Callable[..., str],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MCP default granularity is 'auto' (mirrors the CLI / env default).
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)
    _mcp_state(monkeypatch, fake_embedder)

    payload = mcp_server.brain_timeline(query="productivity")
    assert payload["granularity"] == "month"
    assert payload["granularity_auto"] is True
    assert payload["buckets"][0]["bucket"] == "2024-03"


def test_mcp_timeline_graph_disabled(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder, graph_enabled=False)
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_timeline(query="x")
    assert exc.value.error.code == INVALID_PARAMS


def test_mcp_timeline_empty_query(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder)
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_timeline(query="   ")
    assert exc.value.error.code == INVALID_PARAMS


def test_mcp_timeline_bad_granularity(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder)
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_timeline(query="x", granularity="week")
    assert exc.value.error.code == INVALID_PARAMS


# ===========================================================================
# Phase 3 — migration 021 roundtrip (excluded from the default suite)
# ===========================================================================


@pytest.mark.phase3
def test_migration_021_doc_date_generated(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """After migration 021, ``doc_date`` == COALESCE(sent_at, ingested_at)."""
    # The migration runner applies 021, so the column is present here.
    sent = _doc(
        seed_doc, test_db, "with sent_at", sent_at=_dt(2024, 2, 10), ingested_at=_dt(2024, 6, 1)
    )
    ingest_only = _doc(
        seed_doc, test_db, "ingest only", sent_at=None, ingested_at=_dt(2024, 3, 3)
    )

    rows = test_db.execute(
        "SELECT doc_date, COALESCE(sent_at, ingested_at) FROM documents "
        "WHERE id = ANY(%s)",
        ([sent, ingest_only],),
    ).fetchall()
    assert rows, "expected seeded rows"
    for doc_date, coalesced in rows:
        assert doc_date == coalesced
