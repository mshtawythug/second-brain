"""Tests for ``brain.resurface`` — spaced-repetition resurfacing (Plan 02).

Two layers:

* Pure-logic unit tests for :func:`score_document` (no DB).
* Integration tests for :func:`resurface_docs` against the real test DB,
  exercising the SQL guards (draft / action-item / min-age / NULL-timestamp),
  the access-staleness LEFT JOIN, the source filter, ordering, and the limit.

All document content is synthetic.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from brain.config import Config
from brain.resurface import ResurfaceItem, resurface_docs, score_document

# Half-lives used across the unit tests — mirror the production defaults so the
# numbers below are meaningful, but passed explicitly (score_document does not
# read Config).
_AGE_HL = 180.0
_ACCESS_HL = 90.0


# ---------------------------------------------------------------------------
# Unit tests — score_document (pure logic, no DB)
# ---------------------------------------------------------------------------


def test_score_document_old_never_accessed() -> None:
    """A year-old, never-opened doc scores high (both factors near saturation)."""
    score = score_document(
        age_days=365.0,
        last_access_days=None,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    # age_factor ≈ 0.75, access_staleness ≈ 0.94, importance == 1.0 → ≈ 0.71.
    assert score > 0.5


def test_score_document_recently_opened() -> None:
    """Opened yesterday → tiny access_staleness → far lower score than never-opened."""
    recent = score_document(
        age_days=365.0,
        last_access_days=1.0,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    never = score_document(
        age_days=365.0,
        last_access_days=None,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert recent < never
    assert recent < 0.05  # opened yesterday → near-zero staleness


def test_score_document_tags_boost() -> None:
    """5 tags + summary → importance_factor > 1.0 lifts the score above plain.

    Driven at saturated age/access so the score collapses to the importance
    factor (≈1.7), isolating the importance contribution.
    """
    rich = score_document(
        age_days=10_000.0,
        last_access_days=10_000.0,
        tag_count=5,
        has_summary=True,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    plain = score_document(
        age_days=10_000.0,
        last_access_days=10_000.0,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert rich > plain
    assert rich == pytest.approx(1.7, abs=1e-6)  # 1 + 0.1*5 + 0.2


def test_score_document_no_summary_no_tags() -> None:
    """importance_factor == 1.0 exactly when there are no tags and no summary."""
    # Saturate age + access so the score reduces to the importance factor.
    score = score_document(
        age_days=10_000.0,
        last_access_days=10_000.0,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert score == pytest.approx(1.0, abs=1e-9)


def test_age_factor_half_life() -> None:
    """At age == half-life the age factor is 0.5 (within ε), isolated via
    saturated access + neutral importance."""
    score = score_document(
        age_days=_AGE_HL,  # exactly one half-life old
        last_access_days=1_000_000.0,  # access_staleness → 1.0 (underflow)
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=1.0,
    )
    assert score == pytest.approx(0.5, abs=1e-6)


def test_access_staleness_never_opened() -> None:
    """NULL last_access is treated as age_days for the access factor."""
    age = 200.0
    via_none = score_document(
        age_days=age,
        last_access_days=None,
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    via_age = score_document(
        age_days=age,
        last_access_days=age,  # explicit: last access == age
        tag_count=0,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert via_none == pytest.approx(via_age, abs=1e-12)


def test_formula_components_independent() -> None:
    """Each factor independently moves the total in the expected direction."""
    base = score_document(
        age_days=100.0,
        last_access_days=100.0,
        tag_count=1,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    older = score_document(
        age_days=300.0,  # older → higher age_factor
        last_access_days=100.0,
        tag_count=1,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    more_recent_access = score_document(
        age_days=100.0,
        last_access_days=1.0,  # opened more recently → lower staleness
        tag_count=1,
        has_summary=False,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    more_important = score_document(
        age_days=100.0,
        last_access_days=100.0,
        tag_count=4,  # more tags → higher importance
        has_summary=True,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert older > base
    assert more_recent_access < base
    assert more_important > base


@pytest.mark.parametrize(
    ("age_days", "last_access_days", "tag_count", "has_summary"),
    [
        (15.0, None, 0, False),
        (365.0, 200.0, 3, True),
        (30.0, 5.0, 1, False),
        (1000.0, None, 2, True),
        (90.0, 90.0, 0, True),
    ],
)
def test_score_range(
    age_days: float,
    last_access_days: float | None,
    tag_count: int,
    has_summary: bool,
) -> None:
    """Score stays within (0.0, 1.5] for valid inputs (importance ≤ 1.5)."""
    score = score_document(
        age_days=age_days,
        last_access_days=last_access_days,
        tag_count=tag_count,
        has_summary=has_summary,
        age_halflife_days=_AGE_HL,
        access_halflife_days=_ACCESS_HL,
    )
    assert 0.0 < score <= 1.5


# ---------------------------------------------------------------------------
# Integration tests — resurface_docs (real test DB)
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    """Active config (DATABASE_URL forced to the test DB by the session fixture)."""
    return Config.load()


def _insert_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    content: str = "resurface body text for scoring",
    content_type: str = "note",
    tags: list[str] | None = None,
    summary: str | None = None,
    draft: bool = False,
    source_kind: str | None = "manual",
    age_days: float = 100.0,
    null_ts: bool = False,
) -> str:
    """Insert one synthetic document and return its UUID as text.

    ``age_days`` sets ``ingested_at = now() - age_days`` unless ``null_ts`` is
    True, in which case both ``sent_at`` and ``ingested_at`` are NULL (the
    excluded-by-guard case). When ``source_kind`` is None the doc has no source
    row (``source_id`` NULL).
    """
    source_id: str | None = None
    if source_kind is not None:
        src = conn.execute(
            "INSERT INTO sources (kind, external_id) VALUES (%s, %s) RETURNING id::text",
            (source_kind, str(uuid.uuid4())),
        ).fetchone()
        assert src is not None
        source_id = src[0]

    if null_ts:
        row = conn.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type, tags,
                 summary, draft, ingested_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
            RETURNING id::text
            """,
            (
                source_id,
                title,
                content,
                str(uuid.uuid4()),
                content_type,
                tags or [],
                summary,
                draft,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type, tags,
                 summary, draft, ingested_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    now() - (%s * interval '1 day'))
            RETURNING id::text
            """,
            (
                source_id,
                title,
                content,
                str(uuid.uuid4()),
                content_type,
                tags or [],
                summary,
                draft,
                age_days,
            ),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _record_open(
    conn: psycopg.Connection, doc_id: str, *, days_ago: float
) -> None:
    """Insert an 'opened' interaction at ``now() - days_ago`` for ``doc_id``."""
    conn.execute(
        """
        INSERT INTO interactions (document_id, action, source, at)
        VALUES (%s, 'opened', 'cli', now() - (%s * interval '1 day'))
        """,
        (doc_id, days_ago),
    )


def test_resurface_docs_returns_oldest_first(test_db: psycopg.Connection) -> None:
    """Oldest never-opened doc ranks first; youngest excluded by min-age guard."""
    old = _insert_doc(test_db, title="old", age_days=300.0)
    mid = _insert_doc(test_db, title="mid", age_days=100.0)
    _insert_doc(test_db, title="young", age_days=20.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=30)

    assert [it.id for it in items] == [old, mid]
    assert all(isinstance(it, ResurfaceItem) for it in items)
    # Scores strictly descending.
    assert items[0].score > items[1].score


def test_score_document_brand_new_excluded(test_db: psycopg.Connection) -> None:
    """A doc younger than min_age_days is excluded by the SQL guard."""
    _insert_doc(test_db, title="brand new", age_days=3.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14)

    assert items == []


def test_resurface_docs_access_deprioritizes(test_db: psycopg.Connection) -> None:
    """Two same-age docs: the one opened yesterday ranks below the untouched one."""
    untouched = _insert_doc(test_db, title="untouched", age_days=200.0)
    opened = _insert_doc(test_db, title="opened", age_days=200.0)
    _record_open(test_db, opened, days_ago=1.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14)

    assert [it.id for it in items] == [untouched, opened]
    opened_item = next(it for it in items if it.id == opened)
    untouched_item = next(it for it in items if it.id == untouched)
    assert opened_item.last_access_days is not None
    assert opened_item.last_access_days == pytest.approx(1.0, abs=0.05)
    assert untouched_item.last_access_days is None


def test_resurface_docs_source_filter(test_db: psycopg.Connection) -> None:
    """--source manual returns only manual-sourced docs."""
    manual = _insert_doc(test_db, title="manual doc", source_kind="manual", age_days=200.0)
    _insert_doc(test_db, title="krisp doc", source_kind="krisp", age_days=200.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14, source_kind="manual")

    assert [it.id for it in items] == [manual]
    assert items[0].source_kind == "manual"


def test_resurface_docs_excludes_drafts(test_db: psycopg.Connection) -> None:
    """A draft doc never appears, regardless of age."""
    _insert_doc(test_db, title="draft doc", draft=True, age_days=500.0)
    published = _insert_doc(test_db, title="published doc", draft=False, age_days=100.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14)

    assert [it.id for it in items] == [published]


def test_resurface_docs_excludes_action_items(test_db: psycopg.Connection) -> None:
    """content_type='krisp_action_items' is excluded by the SQL guard."""
    _insert_doc(
        test_db,
        title="action items",
        content_type="krisp_action_items",
        age_days=400.0,
    )
    note = _insert_doc(test_db, title="real note", content_type="note", age_days=100.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14)

    assert [it.id for it in items] == [note]


def test_resurface_docs_limit(test_db: psycopg.Connection) -> None:
    """limit=5 returns exactly 5 rows out of 20 eligible docs."""
    for i in range(20):
        _insert_doc(test_db, title=f"doc {i}", age_days=100.0 + i)

    items = resurface_docs(test_db, cfg=_cfg(), limit=5, min_age_days=14)

    assert len(items) == 5


def test_resurface_docs_empty_corpus(test_db: psycopg.Connection) -> None:
    """No docs → empty list, no exception."""
    items = resurface_docs(test_db, cfg=_cfg())
    assert items == []


def test_resurface_docs_rejects_nonpositive_limit(
    test_db: psycopg.Connection,
) -> None:
    """limit < 1 raises ValueError (guards the silent limit=0/-1 slice bug)."""
    _insert_doc(test_db, title="a", age_days=200.0)
    with pytest.raises(ValueError, match="limit must be"):
        resurface_docs(test_db, cfg=_cfg(), limit=0)
    with pytest.raises(ValueError, match="limit must be"):
        resurface_docs(test_db, cfg=_cfg(), limit=-1)


def test_resurface_docs_rejects_negative_min_age(
    test_db: psycopg.Connection,
) -> None:
    """min_age_days < 0 raises ValueError (would otherwise admit future docs)."""
    _insert_doc(test_db, title="a", age_days=200.0)
    with pytest.raises(ValueError, match="min_age_days must be"):
        resurface_docs(test_db, cfg=_cfg(), min_age_days=-1)


def test_resurface_docs_uses_config_defaults(test_db: psycopg.Connection) -> None:
    """With limit/min_age_days None, the cfg values drive the query."""
    import dataclasses

    for i in range(5):
        _insert_doc(test_db, title=f"doc {i}", age_days=200.0 + i)
    cfg = dataclasses.replace(_cfg(), resurface_limit=2, resurface_min_age_days=14)

    items = resurface_docs(test_db, cfg=cfg)  # no explicit limit/min_age

    assert len(items) == 2


def test_resurface_docs_null_doc_ts_excluded(test_db: psycopg.Connection) -> None:
    """A row with both sent_at and ingested_at NULL is excluded by the guard.

    ``documents.ingested_at`` is ``NOT NULL DEFAULT now()`` in the live schema,
    so a NULL doc timestamp can only arise from a corrupted / legacy row. The
    ``coalesce(...) IS NOT NULL`` guard is defensive against exactly that. To
    exercise it we drop the constraint on the *ephemeral test DB* (reset per
    test) and insert the otherwise-impossible row — this is DDL on the test
    fixture, not a patch of production code.
    """
    test_db.execute("ALTER TABLE documents ALTER COLUMN ingested_at DROP NOT NULL")
    _insert_doc(test_db, title="no timestamp", null_ts=True)
    dated = _insert_doc(test_db, title="dated", age_days=100.0)

    items = resurface_docs(test_db, cfg=_cfg(), min_age_days=14)

    assert [it.id for it in items] == [dated]
