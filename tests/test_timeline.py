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
from mcp import McpError
from mcp.types import INVALID_PARAMS
from typer.testing import CliRunner

from brain import mcp_server
from brain import timeline as timeline_mod
from brain.cli import app
from brain.config import Config
from brain.enrichment import OllamaEnricher
from brain.timeline import (
    TimelineBucket,
    _add_one_month,
    _bucket_label,
    _doc_date_expr,
    _parse_month,
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


@pytest.mark.parametrize("value", ["month", "QUARTER", " year "])
def test_validate_granularity_accepts_valid(value: str) -> None:
    assert _validate_granularity(value) in {"month", "quarter", "year"}


def test_validate_granularity_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="month/quarter/year"):
        _validate_granularity("week")


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


def test_empty_helpers_short_circuit(test_db: psycopg.Connection[Any]) -> None:
    # Direct guards: empty doc/seed inputs return [] without touching real rows.
    assert timeline_mod._top_cotopics(test_db, TENANT, [], ["x"], []) == []
    assert timeline_mod._bucket_doc_titles(test_db, []) == []
    assert timeline_mod._scope_to_person(test_db, TENANT, [], ["k"]) == []
    assert timeline_mod._owner_entity_ids(test_db, TENANT, frozenset()) == []


def test_synthesize_buckets_empty_short_circuits() -> None:
    # Defensive guard: an empty bucket list returns unchanged without the enricher.
    class _BoomEnricher:
        def summarize_bucket(self, **_kwargs: Any) -> str:
            raise AssertionError("enricher must not be called for an empty list")

    out = timeline_mod._synthesize_buckets(
        [], entity_name="x", enricher=_BoomEnricher(), synth_limit=1  # type: ignore[arg-type]
    )
    assert out == []


# ===========================================================================
# CLI — brain timeline
# ===========================================================================


def test_cli_timeline_json_output(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    result = runner.invoke(app, ["timeline", "productivity", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "productivity"
    assert payload["granularity"] == "quarter"
    assert payload["entity_names"] == ["productivity"]
    assert payload["buckets"][0]["bucket"] == "2024-Q1"
    assert payload["buckets"][0]["doc_count"] == 1
    assert "bucket_start" in payload["buckets"][0]


def test_cli_timeline_human_output(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    entity = _insert_entity(test_db, name="productivity")
    doc_id = _doc(seed_doc, test_db, "prod note", sent_at=_dt(2024, 3, 10))
    _mention(test_db, entity, doc_id)

    result = runner.invoke(app, ["timeline", "productivity"])
    assert result.exit_code == 0, result.output
    assert "Timeline" in result.output
    assert "2024-Q1" in result.output


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
    assert "month/quarter/year" in result.output


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

    payload = mcp_server.brain_timeline(query="productivity")
    assert payload["entity_names"] == ["productivity"]
    assert payload["buckets"][0]["bucket"] == "2024-Q1"
    assert payload["granularity"] == "quarter"


def test_mcp_timeline_graph_disabled(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder, graph_enabled=False)
    with pytest.raises(McpError) as exc:
        mcp_server.brain_timeline(query="x")
    assert exc.value.error.code == INVALID_PARAMS


def test_mcp_timeline_empty_query(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder)
    with pytest.raises(McpError) as exc:
        mcp_server.brain_timeline(query="   ")
    assert exc.value.error.code == INVALID_PARAMS


def test_mcp_timeline_bad_granularity(
    test_db: psycopg.Connection[Any],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mcp_state(monkeypatch, fake_embedder)
    with pytest.raises(McpError) as exc:
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
