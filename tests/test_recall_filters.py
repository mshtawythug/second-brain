"""``recall()`` passes every metadata filter through to search (F2).

Recall deliberately owns no filtering logic of its own — it forwards to
``hybrid_search`` so a recall and a search with the same flags select exactly
the same documents. A filter silently dropped in the forwarding would be
invisible: the agent would get *plausible* passages from outside the scope it
asked for, which is worse than an error.

The first test is the structural guard: it compares ``recall()``'s filter
parameters against ``hybrid_search``'s by introspection, so a filter added to
search later is flagged here rather than quietly unreachable from recall.

All fixture data is synthetic.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from brain.config import Config
from brain.recall import recall
from brain.search import hybrid_search
from tests.conftest import TEST_DATABASE_URL

#: Filters ``hybrid_search`` exposes that recall deliberately does NOT forward,
#: each with the reason. Anything else appearing in search must be forwarded.
_INTENTIONALLY_NOT_FORWARDED = {
    "limit": "recall derives its own over-fetch from the token budget",
    "snippet_context_tokens": "recall sets this from cfg.recall_passage_tokens",
    "vector_sim_floor": "taken from cfg",
    "recency_halflife_days": "taken from cfg",
    "explain": "a debugging surface, not a recall concern",
    "diagnostics": "recall owns its own holder to read fts_count",
    "total_count": "recall reports candidates_considered instead",
    "conn": "positional",
    "embedder": "passed explicitly",
    "query": "passed explicitly",
    "updated_after": "F9 edit-range filters; not in F2's documented surface",
    "updated_before": "F9 edit-range filters; not in F2's documented surface",
    "sensitivity": "F6 lens; recall inherits the default (both tiers)",
}


def _cfg() -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        recall_passage_tokens=120,
        vector_sim_floor=0.0,
        recency_halflife_days=None,
    )


def _ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    external_id: str,
    source_kind: str = "manual",
    tags: list[str] | None = None,
    content_type: str = "note",
) -> str:
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    body = (
        "Platform staffing and the quarterly migration runway were discussed "
        "at length, with hiring plans for the following two quarters. "
    ) * 10
    result = ingest_document(
        conn,
        doc=ExtractedDoc(
            title=title,
            content=f"{body} Marker for {external_id}.",
            content_type=content_type,
            source_path=None,
            metadata={},
        ),
        embedder=embedder,
        source_kind=source_kind,
        source_external_id=external_id,
        tags=tags or [],
    )
    return result.document_id


# ---------------------------------------------------------------------------
# The structural guard
# ---------------------------------------------------------------------------


def test_recall_forwards_every_search_filter_it_should() -> None:
    """A filter added to search must not become silently unreachable.

    Introspection rather than a hand-maintained list, so this cannot rot: any
    new ``hybrid_search`` parameter either appears in ``recall``'s signature
    or is named in ``_INTENTIONALLY_NOT_FORWARDED`` with a reason.
    """
    search_params = set(inspect.signature(hybrid_search).parameters)
    recall_params = set(inspect.signature(recall).parameters)

    unforwarded = search_params - recall_params - set(_INTENTIONALLY_NOT_FORWARDED)

    assert unforwarded == set(), (
        f"hybrid_search grew filter(s) recall does not forward: {sorted(unforwarded)}. "
        "Either forward them or record why not in _INTENTIONALLY_NOT_FORWARDED."
    )


# ---------------------------------------------------------------------------
# Behavioural: each filter actually narrows the result set
# ---------------------------------------------------------------------------


def test_source_kind_filter_narrows_results(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _ingest(test_db, fake_embedder, title="Manual Note", external_id="w4-f-1")
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp Call",
        external_id="w4-f-2",
        source_kind="krisp",
    )

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        source_kind="krisp",
    )

    assert result.passages
    assert {p.source_kind for p in result.passages} == {"krisp"}


def test_tag_filter_narrows_results(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="Tagged Note",
        external_id="w4-f-3",
        tags=["planning"],
    )
    _ingest(test_db, fake_embedder, title="Untagged Note", external_id="w4-f-4")

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        tag="planning",
    )

    assert [p.title for p in result.passages] == ["Tagged Note"]


def test_without_tag_filter_excludes(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _ingest(
        test_db,
        fake_embedder,
        title="Excluded Note",
        external_id="w4-f-5",
        tags=["draft-ish"],
    )
    _ingest(test_db, fake_embedder, title="Kept Note", external_id="w4-f-6")

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        without_tag="draft-ish",
    )

    assert "Excluded Note" not in [p.title for p in result.passages]
    assert "Kept Note" in [p.title for p in result.passages]


def test_content_type_filter_narrows_results(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _ingest(test_db, fake_embedder, title="A Note", external_id="w4-f-7")
    _ingest(
        test_db,
        fake_embedder,
        title="A Transcript",
        external_id="w4-f-8",
        content_type="transcript",
    )

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        content_type="transcript",
    )

    assert result.passages
    assert {p.content_type for p in result.passages} == {"transcript"}


def test_since_days_filter_narrows_results(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="Old Note", external_id="w4-f-9")
    _ingest(test_db, fake_embedder, title="Recent Note", external_id="w4-f-10")
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s",
        (datetime.now(UTC) - timedelta(days=90), doc_id),
    )

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        since_days=7,
    )

    assert "Old Note" not in [p.title for p in result.passages]


def test_after_and_before_bound_the_window(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="Windowed", external_id="w4-f-11")
    stamp = datetime(2026, 3, 15, tzinfo=UTC)
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s", (stamp, doc_id)
    )

    inside = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        after=datetime(2026, 3, 1, tzinfo=UTC),
        before=datetime(2026, 4, 1, tzinfo=UTC),
    )
    outside = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        after=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert "Windowed" in [p.title for p in inside.passages]
    assert "Windowed" not in [p.title for p in outside.passages]


def test_filters_compose(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Two filters together must narrow, not one silently winning."""
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp Planning",
        external_id="w4-f-12",
        source_kind="krisp",
        tags=["planning"],
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Krisp Other",
        external_id="w4-f-13",
        source_kind="krisp",
    )
    _ingest(
        test_db,
        fake_embedder,
        title="Manual Planning",
        external_id="w4-f-14",
        tags=["planning"],
    )

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        source_kind="krisp",
        tag="planning",
    )

    assert [p.title for p in result.passages] == ["Krisp Planning"]


def test_a_filter_matching_nothing_yields_an_empty_recall(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Empty is the honest answer — not an unfiltered fallback."""
    _ingest(test_db, fake_embedder, title="Only Note", external_id="w4-f-15")

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
        tag="no-such-tag",
    )

    assert result.passages == []


def test_fts_count_is_reported_from_diagnostics(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The lexical-miss signal must survive into the result.

    ``fts_count == 0`` is what ``brain gaps`` mines; a recall that dropped it
    would make every recall look like a lexical hit.
    """
    _ingest(test_db, fake_embedder, title="Findable", external_id="w4-f-16")

    hit = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="platform staffing quarterly",
        budget_tokens=4000,
        max_candidates=25,
    )
    miss = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="zzzz-no-such-term-anywhere",
        budget_tokens=4000,
        max_candidates=25,
    )

    assert hit.fts_count is not None and hit.fts_count > 0
    assert miss.fts_count == 0


def test_person_keys_are_forwarded_not_resolved_here(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Recall takes keys, never a display name to resolve.

    Resolution raises ``PersonNotFound`` / ``PersonAmbiguous``, and each
    surface maps those to its own framework's error type — the same division
    of labour ``hybrid_search`` documents.
    """
    params = inspect.signature(recall).parameters

    assert "person_keys" in params
    assert params["person_keys"].default is None
