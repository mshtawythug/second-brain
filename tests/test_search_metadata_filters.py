"""Integration tests for Q1-C metadata filter kwargs on ``hybrid_search``.

One test per filter, plus a few combination tests to lock the AND
composition + a SQL-injection probe to lock the parameterized binding.
The recency-boost / snippet-context paths are exercised in their own
test modules; here we focus on the WHERE-clause composition.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search


def _seed(
    test_db: psycopg.Connection,
    embedder: object,
    *,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_kind: str = "manual",
    content_type: str = "note",
    participants: list[str] | None = None,
    sent_at: datetime | None = None,
    ingested_at: datetime | None = None,
    thread_id: str | None = None,
    draft: bool = False,
) -> str:
    """Ingest one doc and apply the metadata mutations the filter needs.

    The ingest pipeline doesn't set ``participants`` / ``sent_at`` /
    ``thread_id`` / ``draft`` for manual ingests, so we patch those
    fields directly on the row after ingest. Returns the doc id.
    """
    result = ingest_document(
        test_db,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=f"{title}: {content}",
            content_type=content_type,
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=f"{source_kind}:{title}",
        tags=tags or [],
    )
    assert result.document_id is not None
    doc_id = result.document_id
    test_db.execute(
        "UPDATE documents SET participants=%s, sent_at=%s, "
        "thread_id=%s, draft=%s WHERE id=%s",
        (participants, sent_at, thread_id, draft, doc_id),
    )
    if ingested_at is not None:
        test_db.execute(
            "UPDATE documents SET ingested_at=%s WHERE id=%s",
            (ingested_at, doc_id),
        )
    return doc_id


# ---------------------------------------------------------------------------
# Person filter
# ---------------------------------------------------------------------------


def test_person_filter_matches_overlapping_participant_key(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Doc A", content="shared term",
        participants=["alice@x.com"],
    )
    _seed(
        test_db, fake_embedder,
        title="Doc B", content="shared term",
        participants=["bob@y.com"],
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        person_keys=["alice@x.com"],
    )
    titles = [r.title for r in results]
    assert titles == ["Doc A"]


def test_person_filter_matches_display_email_combination_form(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Gmail's case-preserved ``Display <email>`` combination form is matched
    via the resolver's lowercased expanded keys — the SQL lowercases each
    stored participant via ``unnest`` before comparing."""
    _seed(
        test_db, fake_embedder,
        title="Combo", content="shared term",
        participants=["Alice Doe <alice@x.com>"],
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        # Mirror EXACTLY what `_expand_person_keys` produces for the resolver
        # — all lowercased, no case-preserved entries. If the SQL ever
        # regresses to a case-sensitive overlap, this test will fail.
        person_keys=[
            "alice@x.com",
            "alice doe",
            "alice doe <alice@x.com>",
        ],
    )
    assert [r.title for r in results] == ["Combo"]


def test_person_filter_is_case_insensitive_against_mixed_case_storage(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Regression: Codex stop-time review (2026-05-11) caught that the
    plain ``&&`` overlap missed mixed-case Gmail participants. The fix
    lowercases each stored participant before comparing to the
    resolver's already-lowercased keys.

    Three mixed-case variants — bare display, bare email, and the
    ``Display <email>`` combination — must all match the same resolver
    output. Locks the case-insensitive contract end-to-end.
    """
    _seed(
        test_db, fake_embedder,
        title="Display only", content="shared term",
        participants=["Alice Doe"],
    )
    _seed(
        test_db, fake_embedder,
        title="Email caps", content="shared term",
        participants=["Alice@X.com"],
    )
    _seed(
        test_db, fake_embedder,
        title="Combo caps", content="shared term",
        participants=["Alice Doe <Alice@X.com>"],
    )
    _seed(
        test_db, fake_embedder,
        title="Other person", content="shared term",
        participants=["Bob <bob@y.com>"],
    )
    # Keys exactly as `_expand_person_keys` would return for Alice.
    keys = ["alice doe", "alice@x.com", "alice doe <alice@x.com>"]
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        person_keys=keys,
    )
    titles = {r.title for r in results}
    assert titles == {"Display only", "Email caps", "Combo caps"}
    assert "Other person" not in titles


def test_person_filter_end_to_end_via_resolver(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """End-to-end: run the actual resolver against a real directory
    seeded with a mixed-case display name, then thread its keys into
    ``hybrid_search`` against a participant string stored in
    Gmail's case-preserved form. Locks the resolver↔search contract
    without any test-side massaging.
    """
    from brain.queries import resolve_person_to_keys
    from brain.vault.derived_links.directory import DirectoryStore

    DirectoryStore(test_db).upsert_pair(
        display_name="Alice Doe", email="alice@x.com", source="gmail"
    )
    _seed(
        test_db, fake_embedder,
        title="Combo caps", content="shared term",
        # Storage form preserves Gmail's case verbatim.
        participants=["Alice Doe <alice@x.com>"],
    )

    match = resolve_person_to_keys(test_db, "Alice")
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        person_keys=match.keys,
    )
    assert [r.title for r in results] == ["Combo caps"]


def test_person_filter_empty_keys_is_no_filter(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """An empty ``person_keys`` list means "no filter" — matches every doc.

    The CLI / MCP layer raises PersonNotFound before reaching
    hybrid_search when nothing resolves, so an empty list here can
    only express explicit "person filter not in use" intent.
    """
    _seed(
        test_db, fake_embedder,
        title="Doc A", content="shared term",
        participants=["alice@x.com"],
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared", person_keys=[]
    )
    assert [r.title for r in results] == ["Doc A"]


# ---------------------------------------------------------------------------
# After / before / range
# ---------------------------------------------------------------------------


def test_after_filter_excludes_older_docs(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Old", content="shared term",
        ingested_at=datetime(2026, 1, 1, tzinfo=None),
    )
    _seed(
        test_db, fake_embedder,
        title="New", content="shared term",
        ingested_at=datetime(2026, 5, 1, tzinfo=None),
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        after=datetime(2026, 4, 1),
    )
    assert [r.title for r in results] == ["New"]


def test_before_filter_excludes_newer_docs(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Old", content="shared term",
        ingested_at=datetime(2026, 1, 1),
    )
    _seed(
        test_db, fake_embedder,
        title="New", content="shared term",
        ingested_at=datetime(2026, 5, 1),
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        before=datetime(2026, 4, 1),
    )
    assert [r.title for r in results] == ["Old"]


def test_after_before_inclusive_lower_exclusive_upper(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Locked: ``after`` is inclusive (>=), ``before`` is exclusive (<).

    Three docs at 01-01 / 02-01 / 03-01; ``after=02-01, before=03-01``
    returns exactly the 02-01 row.
    """
    _seed(
        test_db, fake_embedder,
        title="Jan", content="shared term",
        ingested_at=datetime(2026, 1, 1),
    )
    _seed(
        test_db, fake_embedder,
        title="Feb", content="shared term",
        ingested_at=datetime(2026, 2, 1),
    )
    _seed(
        test_db, fake_embedder,
        title="Mar", content="shared term",
        ingested_at=datetime(2026, 3, 1),
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        after=datetime(2026, 2, 1),
        before=datetime(2026, 3, 1),
    )
    assert [r.title for r in results] == ["Feb"]


def test_after_uses_sent_at_when_present(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """``coalesce(sent_at, ingested_at)`` — ``sent_at`` wins when present."""
    _seed(
        test_db, fake_embedder,
        title="EarlySent", content="shared term",
        sent_at=datetime(2026, 1, 1),
        ingested_at=datetime(2026, 5, 1),
    )
    _seed(
        test_db, fake_embedder,
        title="LateIngest", content="shared term",
        sent_at=None,
        ingested_at=datetime(2026, 5, 1),
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        after=datetime(2026, 4, 1),
    )
    # EarlySent is filtered out (sent_at=2026-01-01 < 2026-04-01) — only
    # LateIngest's coalesce(NULL, ingested_at=2026-05-01) clears the bar.
    assert [r.title for r in results] == ["LateIngest"]


# ---------------------------------------------------------------------------
# content_type / thread / draft / without_tag
# ---------------------------------------------------------------------------


def test_content_type_filter_exact_match(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Note", content="shared term", content_type="note",
    )
    _seed(
        test_db, fake_embedder,
        title="Email", content="shared term", content_type="email",
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared", content_type="email"
    )
    assert [r.title for r in results] == ["Email"]


def test_thread_filter_exact_match(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Thread A 1", content="shared term", thread_id="thread-a",
    )
    _seed(
        test_db, fake_embedder,
        title="Thread A 2", content="shared term", thread_id="thread-a",
    )
    _seed(
        test_db, fake_embedder,
        title="Thread B", content="shared term", thread_id="thread-b",
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared", thread_id="thread-a"
    )
    titles = {r.title for r in results}
    assert titles == {"Thread A 1", "Thread A 2"}


def test_thread_filter_no_match_returns_empty(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder,
        title="Doc", content="shared term", thread_id="thread-a",
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared",
        thread_id="thread-nope",
    )
    assert results == []


def test_draft_true_returns_only_drafts(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Draft", content="shared term",
        draft=True,
    )
    _seed(
        test_db, fake_embedder, title="Published", content="shared term",
        draft=False,
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared", draft=True
    )
    assert [r.title for r in results] == ["Draft"]


def test_draft_false_returns_only_published(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Draft", content="shared term",
        draft=True,
    )
    _seed(
        test_db, fake_embedder, title="Published", content="shared term",
        draft=False,
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared", draft=False
    )
    assert [r.title for r in results] == ["Published"]


def test_draft_none_returns_both(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Draft", content="shared term",
        draft=True,
    )
    _seed(
        test_db, fake_embedder, title="Published", content="shared term",
        draft=False,
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared"
    )
    titles = {r.title for r in results}
    assert titles == {"Draft", "Published"}


def test_without_tag_excludes_tagged_docs(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Public", content="shared term",
        tags=["public"],
    )
    _seed(
        test_db, fake_embedder, title="Private", content="shared term",
        tags=["private"],
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared",
        without_tag="private",
    )
    assert [r.title for r in results] == ["Public"]


def test_without_tag_and_tag_combine_as_and(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Both", content="shared term",
        tags=["shared", "private"],
    )
    _seed(
        test_db, fake_embedder, title="Shared only", content="shared term",
        tags=["shared"],
    )
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared",
        tag="shared", without_tag="private",
    )
    assert [r.title for r in results] == ["Shared only"]


# ---------------------------------------------------------------------------
# matched_filters in SearchExplanation
# ---------------------------------------------------------------------------


def test_explain_matched_filters_captures_all_q1c_keys(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    _seed(
        test_db, fake_embedder, title="Doc", content="shared term",
        participants=["alice@x.com"],
        thread_id="thread-a",
        sent_at=datetime(2026, 2, 1),
        draft=False,
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        explain=True,
        person_keys=["alice@x.com"],
        person_display_name="Alice",
        after=datetime(2026, 1, 1),
        before=datetime(2026, 5, 1),
        content_type="note",
        thread_id="thread-a",
        draft=False,
        without_tag="private",
    )
    assert results, "expected at least one hit"
    explain = results[0].explain
    assert explain is not None
    mf = explain.matched_filters
    assert mf["person_keys"] == ["alice@x.com"]
    assert mf["person_display_name"] == "Alice"
    assert mf["after"] == "2026-01-01T00:00:00"
    assert mf["before"] == "2026-05-01T00:00:00"
    assert mf["content_type"] == "note"
    assert mf["thread_id"] == "thread-a"
    assert mf["draft"] is False
    assert mf["without_tag"] == "private"


def test_filters_compose_with_recency_and_snippet_context(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Q1-C filters compose with the Q1-A recency boost + snippet context.

    Locks "the new where_clauses block doesn't break the prior features"
    — recency_halflife_days still applies to scores, snippet_context_tokens
    still stitches neighbors, and the filter still excludes the unwanted doc.
    """
    _seed(
        test_db, fake_embedder, title="Wanted", content="shared term context",
        participants=["alice@x.com"],
        sent_at=datetime(2026, 5, 1),
    )
    _seed(
        test_db, fake_embedder, title="Filtered", content="shared term context",
        participants=["bob@y.com"],
        sent_at=datetime(2026, 5, 1),
    )
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="shared",
        person_keys=["alice@x.com"],
        recency_halflife_days=180.0,
        snippet_context_tokens=200,
    )
    assert [r.title for r in results] == ["Wanted"]


# ---------------------------------------------------------------------------
# SQL-injection probe — parameterized binding guarantees these are safe.
# ---------------------------------------------------------------------------


def test_filters_resist_sql_injection_in_string_args(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Apostrophes / semicolons / ``--`` in filter strings must be inert.

    The parameterized SQL binding (``%s`` placeholders) is the gate;
    if it ever regressed to f-string interpolation this test would
    surface either a syntax error or an unexpected row count.
    """
    _seed(
        test_db, fake_embedder, title="Doc", content="shared term",
        content_type="note", thread_id="legit",
    )
    # None of these should produce a match; none should crash either.
    sneaky_inputs = [
        "'; DROP TABLE documents; --",
        "x' OR '1'='1",
        "; SELECT 1;",
    ]
    for needle in sneaky_inputs:
        results = hybrid_search(
            test_db,
            embedder=fake_embedder,
            query="shared",
            content_type=needle,
        )
        assert results == []
        results = hybrid_search(
            test_db,
            embedder=fake_embedder,
            query="shared",
            thread_id=needle,
        )
        assert results == []
        results = hybrid_search(
            test_db,
            embedder=fake_embedder,
            query="shared",
            without_tag=needle,
        )
        # without_tag with a non-matching tag → no exclusion → 1 hit.
        assert [r.title for r in results] == ["Doc"]
    # Sanity: the docs table still exists (the DROP attempt was inert).
    row = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    assert row[0] == 1
