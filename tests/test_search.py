"""Integration tests for hybrid search (FTS + vector via RRF)."""
import os
from datetime import UTC, datetime
from typing import Any

import pytest

from brain.db import connect
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _seed(test_db, embedder, items):
    """Seed the test DB with (title, content, tags, source_kind) tuples.

    Two small adjustments keep the suite honest without changing test intent:

    * A unique ``source_external_id`` is supplied per row so the ingest
      pipeline always creates a source row — ``_upsert_source`` otherwise
      skips insertion for manual ingests with no external id, and the
      ``source_kind`` filter can never match.
    * The title is prefixed onto the stored content so two rows sharing the
      same test content string (e.g. both "common term") hash to different
      ``content_hash`` values and don't collapse via dedup.
    """
    for title, content, tags, source_kind in items:
        ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=title,
                content=f"{title}: {content}",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind=source_kind,
            source_external_id=f"{source_kind}:{title}",
            tags=tags or [],
        )


def test_search_finds_documents_by_keyword(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("Doc A", "company-id was a great company to work at", [], "manual"),
        ("Doc B", "krisp meeting transcript about pizza", [], "manual"),
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="company-id", limit=5)
    titles = [r.title for r in results]
    assert "Doc A" in titles


def test_search_returns_snippet(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("Long doc", "alpha beta gamma. company-id was great. delta epsilon.", [], "manual"),
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="company-id", limit=5)
    assert results
    assert results[0].snippet


def test_search_filters_by_tag(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "company-id stuff", ["interview"], "manual"),
        ("B", "company-id stuff", ["personal"], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, tag="interview"
    )
    titles = [r.title for r in results]
    assert "A" in titles
    assert "B" not in titles


def test_search_filters_by_source(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "common term", [], "manual"),
        ("B", "common term", [], "krisp"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="common", limit=5, source_kind="krisp"
    )
    titles = [r.title for r in results]
    assert titles == ["B"]


def test_search_respects_limit(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        (f"Doc{i}", f"shared keyword {i}", [], "manual") for i in range(5)
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="keyword", limit=2)
    assert len(results) <= 2


def test_search_fts_only_skips_embedding(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "company-id term", [], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, fts_only=True
    )
    assert results


def test_search_since_days_filter(test_db, fake_embedder):
    """since_days=1 includes documents ingested just now (within last day)."""
    _seed(test_db, fake_embedder, [
        ("Recent", "company-id term", [], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, since_days=1
    )
    assert [r.title for r in results] == ["Recent"]


def test_search_since_days_excludes_old(test_db, fake_embedder):
    """since_days=1 excludes a document whose ingested_at is pushed back 10 days."""
    _seed(test_db, fake_embedder, [
        ("Old", "company-id term", [], "manual"),
    ])
    test_db.execute("UPDATE documents SET ingested_at = NOW() - INTERVAL '10 days'")
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, since_days=1
    )
    assert results == []


def test_search_no_matches_returns_empty(test_db, fake_embedder):
    """FTS-only search with no matches returns []."""
    _seed(test_db, fake_embedder, [
        ("A", "alpha term", [], "manual"),
    ])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="nonexistentkeyword",
        limit=5,
        fts_only=True,
    )
    assert results == []


class _MidSearchDeleteConn:
    """Connection test-double that lands a concurrent DELETE in a precise window.

    ``hybrid_search`` ranks chunks first (building ``by_doc``), then fetches
    per-document metadata via a second query (``... FROM documents d ...
    d.id = ANY(%s)``). This wrapper delegates every call to the real connection
    but, the first time it sees that metadata query, deletes ``victim_id`` on a
    *separate* connection first — reproducing the exact race an in-flight
    ``brain rm`` opens. Composition over inheritance; NOT monkey-patching
    (CLAUDE.md rule 13) — a purpose-built stand-in whose only extra behavior is
    the interleaved delete.
    """

    def __init__(self, real: Any, victim_id: str) -> None:
        self._real = real
        self._victim_id = victim_id
        self.delete_fired = False

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if (
            not self.delete_fired
            and "FROM documents d" in sql
            and "d.id = ANY" in sql
        ):
            self.delete_fired = True
            with connect(TEST_DATABASE_URL) as other:
                other.autocommit = True
                other.execute(
                    "DELETE FROM documents WHERE id = %s", (self._victim_id,)
                )
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_hybrid_search_skips_doc_deleted_mid_search(test_db, fake_embedder):
    """A doc deleted between chunk ranking and metadata fetch is skipped, not KeyError.

    Regression for overhaul Task 2.2. The ranked-chunk phase can surface a
    ``document_id`` that a concurrent ``brain rm`` removes before the
    per-document metadata SELECT runs, leaving ``docs[doc_id]`` a ``KeyError``
    that blew up the whole search. The fix skips the now-orphaned doc.
    """
    _seed(test_db, fake_embedder, [
        ("Keeper", "company-id shared keyword", [], "manual"),
        ("Victim", "company-id shared keyword", [], "manual"),
    ])
    victim_id = str(
        test_db.execute(
            "SELECT id FROM documents WHERE title = %s", ("Victim",)
        ).fetchone()[0]
    )

    race = _MidSearchDeleteConn(test_db, victim_id)
    results = hybrid_search(
        race, embedder=fake_embedder, query="company-id shared keyword", limit=5
    )

    assert race.delete_fired, "the mid-search delete window was never hit"
    titles = [r.title for r in results]
    assert "Victim" not in titles
    assert "Keeper" in titles


def test_hybrid_search_negative_limit_does_not_silently_truncate(
    test_db, fake_embedder
):
    """A negative ``limit`` must not silently slice the tail off the ranked list.

    Regression for overhaul Task 2.10. ``results[:limit]`` with ``limit=-2``
    returned all-but-the-last-2 docs (silent wrong data). The defensive floor
    clamps a non-positive limit to 1 instead.
    """
    _seed(test_db, fake_embedder, [
        (f"Doc{i}", "shared keyword term", [], "manual") for i in range(5)
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared keyword term", limit=-2
    )
    # Old code returned len(all_docs) - 2 == 3 (tail silently dropped); the
    # floor clamps to exactly 1.
    assert len(results) == 1


def test_search_result_carries_the_document_date(test_db, fake_embedder):
    """``SearchResult.recency_ts`` is populated, and NOT only when ranking uses it.

    It is ``coalesce(sent_at, ingested_at)`` — the same expression the recency
    boost decays over — so a hit's displayed date can never disagree with the
    date it was ranked by. ``brain ui``'s ledger gutter renders it.

    ``recency_halflife_days`` is left at its ``None`` default ON PURPOSE: the
    value used to be read *inside* the boost branch and thrown away, so a caller
    that did not ask for a recency boost got no date at all. This call is that
    caller.
    """
    _seed(test_db, fake_embedder, [("Dated Doc", "company-id term", [], "manual")])
    test_db.execute(
        "UPDATE documents SET sent_at = %s WHERE title = %s",
        (datetime(2026, 3, 9, 12, 0, tzinfo=UTC), "Dated Doc"),
    )

    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, fts_only=True
    )

    hit = next(r for r in results if r.title == "Dated Doc")
    assert hit.recency_ts is not None, (
        "no date reached the caller — the ledger gutter would render '-' for "
        "every document"
    )
    assert hit.recency_ts.date().isoformat() == "2026-03-09"


def test_search_result_date_falls_back_to_ingested_at(test_db, fake_embedder):
    """With no ``sent_at`` the coalesce yields ``ingested_at``, never NULL."""
    _seed(test_db, fake_embedder, [("Undated Doc", "company-id term", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, fts_only=True
    )
    hit = next(r for r in results if r.title == "Undated Doc")
    assert hit.recency_ts is not None
    assert hit.recency_ts.tzinfo is not None, (
        "a naive timestamp would break `.date()` comparisons downstream"
    )


# ------------------------------------------- the phase-0 recency_ts hoist --
#
# `meta[5]` used to be read INSIDE the recency-boost branch. The read was
# hoisted out (three nested conditionals collapsed to one `and`) so every
# caller receives `SearchResult.recency_ts`, not only those that enable the
# boost. The two tests below pin the two halves of that change: the defensive
# normalisation that the hoist put on a hot path, and the equivalence claim
# itself.


class _StaticCursor:
    """Just enough cursor to hand back rows already fetched."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _NaiveRecencyConn:
    """A REAL connection whose per-document metadata comes back tz-NAIVE.

    ``search.py`` normalises a naive ``recency_ts`` to UTC before the boost
    subtracts it from ``now``. That branch cannot currently fire against this
    schema — ``documents.ingested_at`` is ``TIMESTAMPTZ NOT NULL``, so psycopg
    always returns an aware value and the condition is evaluated but never
    taken. It is the ONLY line of the phase-0 diff with no coverage.

    Defensive code that cannot fire is exactly the code that rots unnoticed
    until a schema change makes it live, and the failure would not be quiet: an
    aware/naive subtraction raises ``TypeError`` inside the boost, turning every
    search into a 500. So rather than delete the branch or leave it unproven,
    this simulates the condition it exists for — the database handing back a
    naive timestamp — by stripping tzinfo from that one column on its way out.

    A wrapper, not a monkeypatch: production code is untouched and the real
    connection does all the work.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        cursor = self._inner.execute(*args, **kwargs)
        sql = str(args[0]) if args else ""
        if "AS recency_ts" not in sql:
            return cursor
        stripped: list[tuple[Any, ...]] = []
        for row in cursor.fetchall():
            values = list(row)
            if values[5] is not None and values[5].tzinfo is not None:
                values[5] = values[5].replace(tzinfo=None)
            stripped.append(tuple(values))
        return _StaticCursor(stripped)


def test_a_naive_recency_timestamp_is_normalised_rather_than_crashing(
    test_db, fake_embedder
):
    """B4 — the one uncovered line of the phase-0 diff.

    With the boost ENABLED the normalisation is load-bearing: without it,
    ``now - recency_ts`` subtracts an aware datetime from a naive one and
    raises ``TypeError``. Asserting the search merely succeeds is therefore a
    real assertion here, not a smoke test — and the returned value must also be
    aware, so a future "fix" that swallows the TypeError instead of normalising
    would still fail.
    """
    _seed(test_db, fake_embedder, [
        ("Naive Doc", "company-id appears here", [], "manual"),
    ])

    results = hybrid_search(
        _NaiveRecencyConn(test_db),
        embedder=fake_embedder,
        query="company-id",
        limit=5,
        recency_halflife_days=180.0,
    )

    assert results, "the naive-timestamp path returned nothing at all"
    assert results[0].recency_ts is not None
    assert results[0].recency_ts.tzinfo is not None, (
        "recency_ts came back naive: the normalisation did not fire, and the "
        "only reason the boost above it did not raise TypeError is luck"
    )


def test_recency_ts_is_populated_even_when_the_boost_is_disabled(
    test_db, fake_embedder
):
    """B5, part 1 — the hoist itself, expressed as the regression it prevents.

    THIS is what fails if someone puts the ``meta[5]`` read back inside the
    boost branch. Scores cannot detect that change — the boost still computes
    identically when enabled — so a golden test on ranking alone would stay
    green while every caller that leaves ``recency_halflife_days`` at None
    silently loses the field again. The asymmetry is the property; the score is
    not.
    """
    _seed(test_db, fake_embedder, [
        ("Hoist Doc", "company-id appears here too", [], "manual"),
    ])

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="company-id",
        limit=5,
        recency_halflife_days=None,   # the boost is OFF
    )

    assert results
    assert all(r.recency_ts is not None for r in results), (
        "recency_ts is None with the boost disabled, so the read is back "
        "inside the boost branch and every caller that does not opt into "
        "recency has lost the document's own date"
    )
    assert all(r.recency_ts.tzinfo is not None for r in results)


#: A fixed, deliberately unbalanced corpus: the query term appears once in the
#: title of the first, once in the body of the second, and twice in the body of
#: the third, so FTS and vector legs disagree and RRF actually has work to do.
#: A corpus where every document ties would pin nothing.
_GOLDEN_CORPUS = [
    ("Golden Alpha", "beacon term appears in this body once", [], "manual"),
    ("Golden Beta", "unrelated filler text about scheduling", [], "manual"),
    ("Golden Gamma", "beacon beacon term twice in this body", [], "manual"),
]


#: title -> RRF score, recorded from a real run and confirmed identical across
#: two runs. A MAPPING, not an ordered list, and that is deliberate: two of the
#: three documents score EXACTLY equal (0.0163934426), so their relative order
#: is decided by dict insertion order rather than by the ranker. Pinning that
#: order would pin an implementation accident and produce a test that flips
#: without the arithmetic changing — a flake wearing a golden test's clothes.
#: The strict part of the ordering is asserted separately below.
GOLDEN_RANKING = {
    "Golden Alpha": 0.0322580645,
    "Golden Gamma": 0.0163934426,
    "Golden Beta": 0.0163934426,
}


def test_ranking_is_pinned_on_a_fixed_corpus(test_db, fake_embedder):
    """B5, part 2 — the equivalence claim, MEASURED rather than argued.

    Collapsing three nested conditionals into one ``and`` was argued to preserve
    scores, and that argument was confirmed by a second reading. Two readings
    agreeing is corroboration of a method, not independent evidence — so this
    pins the actual numbers on a fixed input.

    The boost is DISABLED on purpose. With it enabled, scores depend on
    ``datetime.now()`` and no exact value can be pinned without freezing the
    clock, which would need a seam in production code this test is not entitled
    to add. Disabled, the score is pure RRF and fully deterministic — and RRF is
    precisely the arithmetic the refactor touched.

    ``recency_ts`` is asserted present here too: it is the one field that must
    survive with the boost off, and pinning ranking without it would leave this
    green through exactly the regression that matters.
    """
    _seed(test_db, fake_embedder, _GOLDEN_CORPUS)

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="beacon term",
        limit=10,
        recency_halflife_days=None,
    )

    scores = {r.title: round(r.score, 10) for r in results}
    assert scores == GOLDEN_RANKING, (
        f"hybrid_search scoring changed on a fixed corpus.\n"
        f"  expected: {GOLDEN_RANKING}\n"
        f"  actual:   {scores}\n"
        "If this is an intentional ranking change, re-record the constant AND "
        "re-run the eval gate — a golden test cannot tell an improvement from a "
        "regression, only that the numbers moved."
    )

    # The one ordering the SCORES actually determine. Asserted as a strict
    # inequality rather than by index, so it stays true regardless of how the
    # two tied documents happen to fall.
    assert results[0].title == "Golden Alpha"
    assert results[0].score > results[1].score

    assert all(r.recency_ts is not None for r in results)


#: Age each golden document by a whole number of half-lives, so the expected
#: boost factor is an exact power of two (1.0, 0.5, 0.25) rather than a number
#: that only a re-implementation of the formula could predict.
_GOLDEN_AGES_IN_HALFLIVES = {
    "Golden Alpha": 0,
    "Golden Gamma": 1,
    "Golden Beta": 2,
}
_HALFLIFE_DAYS = 180.0


def test_the_recency_boost_multiplies_rrf_by_the_decay_it_promises(
    test_db, fake_embedder
):
    """B5, part 3 — the boost branch itself, which parts 1 and 2 never enter.

    ``test_ranking_is_pinned_on_a_fixed_corpus`` above runs with
    ``recency_halflife_days=None``, so the recency-boost condition in
    ``hybrid_search`` — ``if recency_halflife_days is not None and recency_ts
    is not None`` — is False and its three-line body never executes. It pins
    pure RRF — real regression value,
    but RRF is not what the collapse touched. The claim "the equivalence is
    MEASURED" exceeded that oracle, which is the same defect it was written to
    catch, one level up.

    This runs with the boost ON and checks the arithmetic rather than a
    snapshot: score must equal the pinned RRF times ``0.5 ** (age / halflife)``.

    **Determinism without freezing the clock.** ``hybrid_search`` calls
    ``datetime.now()`` internally and this test may not add a seam to production
    code to control it. Instead the true ``now`` is BRACKETED — sampled either
    side of the call — and the score is asserted to fall between the values
    those two bounds imply. Decay is monotonic in age, so the bracket is
    mathematically guaranteed rather than a tolerance chosen to make the test
    pass; its width is the few milliseconds the call takes, against a 180-day
    half-life.

    **Why the documents are aged.** Freshly-ingested rows have an age near zero
    and therefore a factor near 1.0, which makes ``score == rrf`` — and a test
    written on a fresh corpus would pass just as happily against a boost
    hard-coded to 1.0, or deleted outright. Ageing them by whole half-lives
    puts the expected factors at 1.0, 0.5 and 0.25, far enough from 1.0 that
    removing the multiplication cannot go unnoticed.
    """
    _seed(test_db, fake_embedder, _GOLDEN_CORPUS)

    # ``recency_ts`` is coalesce(sent_at, ingested_at); setting sent_at is how a
    # test controls it without touching ingest. Dates do not participate in FTS
    # rank or vector similarity, so RRF is unaffected — asserted below rather
    # than assumed.
    for title, halflives in _GOLDEN_AGES_IN_HALFLIVES.items():
        test_db.execute(
            "UPDATE documents SET sent_at = NOW() - %s * INTERVAL '1 day' "
            "WHERE title = %s",
            (halflives * _HALFLIFE_DAYS, title),
        )

    # The yardstick is MEASURED, not read from GOLDEN_RANKING. Those constants
    # are rounded to 10 decimals (the test above compares them via
    # ``round(score, 10)``), and the true RRF here is 1/61 =
    # 0.016393442622950818… — using the rounded value as an exact multiplicand
    # understates the expected score by ~1e-11, which is larger than the
    # millisecond bracket below and made this test fail on correct code. The
    # unboosted run gives the exact same-corpus RRF at full precision.
    unboosted = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="beacon term",
        limit=10,
        recency_halflife_days=None,
    )
    exact_rrf = {r.title: r.score for r in unboosted}

    # Precondition: ageing the documents must not have moved RRF itself, or the
    # yardstick would be measuring the wrong thing.
    assert {t: round(s, 10) for t, s in exact_rrf.items()} == GOLDEN_RANKING, (
        "ageing the documents changed the unboosted RRF scores, so this test's "
        "yardstick is invalid — the boost assertions below would be meaningless"
    )

    before = datetime.now(UTC)
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="beacon term",
        limit=10,
        recency_halflife_days=_HALFLIFE_DAYS,
    )
    after = datetime.now(UTC)

    assert len(results) == len(_GOLDEN_CORPUS)

    for result in results:
        rrf = exact_rrf[result.title]
        assert result.recency_ts is not None

        # Older => smaller factor, so the LATER sample gives the lower bound.
        age_lo = max(0.0, (before - result.recency_ts).total_seconds() / 86400.0)
        age_hi = max(0.0, (after - result.recency_ts).total_seconds() / 86400.0)
        upper = rrf * 0.5 ** (age_lo / _HALFLIFE_DAYS)
        lower = rrf * 0.5 ** (age_hi / _HALFLIFE_DAYS)

        assert lower <= result.score <= upper, (
            f"{result.title}: score {result.score!r} is outside the decay the "
            f"boost promises. Expected between {lower!r} and {upper!r} — that is "
            f"the pinned RRF ({rrf}) times 0.5 ** (age / {_HALFLIFE_DAYS}), with "
            "the bracket accounting only for the milliseconds hybrid_search took "
            "to sample its own clock."
        )

        # And the factor must be the RIGHT power of two for this document's age,
        # which is what distinguishes "the boost ran" from "the boost ran with
        # the wrong half-life".
        expected_factor = 0.5 ** _GOLDEN_AGES_IN_HALFLIVES[result.title]
        assert result.score == pytest.approx(rrf * expected_factor, rel=1e-3), (
            f"{result.title} is aged "
            f"{_GOLDEN_AGES_IN_HALFLIVES[result.title]} half-lives, so its factor "
            f"must be {expected_factor}"
        )


class _NullRecencyConn:
    """A REAL connection whose ``recency_ts`` column comes back ``NULL``.

    Sibling of :class:`_NaiveRecencyConn`, and it exists for the same reason:
    ``hybrid_search``'s recency-boost condition has TWO arms —
    ``recency_halflife_days is not None and recency_ts is not None`` — and the
    second cannot fire against this schema, because ``recency_ts`` is
    ``coalesce(sent_at, ingested_at)`` and ``documents.ingested_at`` is
    ``TIMESTAMPTZ NOT NULL``.

    Measured, not assumed: deleting that arm leaves BOTH the golden ranking test
    and the boost test above green, because every document in every fixture has
    a timestamp. An unreachable arm that no test can distinguish from a deleted
    one is precisely the code that gets "simplified" away and takes production
    down the first time a schema change or an outer join makes it live — here as
    a ``TypeError`` from ``now - None`` on every search.

    A wrapper, not a monkeypatch: production code is untouched.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        cursor = self._inner.execute(*args, **kwargs)
        sql = str(args[0]) if args else ""
        if "AS recency_ts" not in sql:
            return cursor
        nulled: list[tuple[Any, ...]] = []
        for row in cursor.fetchall():
            values = list(row)
            values[5] = None
            nulled.append(tuple(values))
        return _StaticCursor(nulled)


def test_a_null_recency_timestamp_skips_the_boost_instead_of_crashing(
    test_db, fake_embedder
):
    """The second arm of the collapsed condition, with the boost ENABLED.

    Deleting ``and recency_ts is not None`` from ``hybrid_search``'s
    recency-boost condition passes every
    other test in this file — verified by mutation. With the boost on and no
    timestamp, the mutated code reaches ``now - None`` and raises ``TypeError``,
    so this is the only oracle that can tell the collapsed condition from a
    half-collapsed one.

    The score must come back as pure RRF: no timestamp means no age, which means
    no decay — not a zero score, and not a crash.
    """
    _seed(test_db, fake_embedder, [
        ("Null Recency Doc", "beacon term appears here", [], "manual"),
    ])

    results = hybrid_search(
        _NullRecencyConn(test_db),
        embedder=fake_embedder,
        query="beacon term",
        limit=5,
        recency_halflife_days=_HALFLIFE_DAYS,
    )

    assert results, "a NULL recency_ts returned no results at all"
    assert results[0].recency_ts is None
    assert results[0].score > 0.0, (
        "a document with no timestamp scored zero: the boost was applied with a "
        "missing age rather than skipped"
    )
