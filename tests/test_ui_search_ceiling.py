"""Defect #27: the ranked-results ceiling, made explicit in the payload.

``search.CANDIDATE_LIMIT`` bounds BOTH ranking legs' candidate pools, and
neither leg's ``LIMIT`` mentions the caller's ``limit``. At most
``2 * CANDIDATE_LIMIT`` distinct documents can therefore ever reach the RRF
merge, however many documents actually match.

**The consequence is not slowness, it is unreachability.** A caller paging past
that bound does not get a slow page, it gets an EMPTY one — and until this
module existed there was nothing in the response that distinguished

    "there are no more matching documents"

from

    "the ranker stopped looking, and 457 more documents match".

Both were ``{"results": []}``. The ledger rendered the second as *"No notes
matched. Try fewer filters."*, which is not merely unhelpful: it is false, and
it advises the reader to widen a query that is already returning more than the
ranker will look at.

THE RULING WAS NOT TO RAISE THE CEILING. Raising ``CANDIDATE_LIMIT`` changes
the candidate pool of every query and is eval-gated (``brain eval --fail-below``
exits 3 on an nDCG@5 / MRR / Recall@20 regression), so it would trade a visible
limit for a silent ranking change. The ceiling stays; it stops being invisible.

WHY THE SIGNAL IS DERIVED FROM ``total_documents`` AND NOT FROM LEG SATURATION.
The obvious alternative is to report the ceiling when a leg came back holding
exactly ``CANDIDATE_LIMIT`` rows. That was rejected on the search module's own
documented behaviour rather than on taste: ``SearchDiagnostics.fts_count``
records that "the vector leg always returns nearest neighbours", so the vector
leg is saturated on very nearly every query and a saturation-based signal would
fire almost always — noise, not signal. ``total_documents`` is by construction
an exact, uncapped, LEXICAL-ONLY ``count(DISTINCT document_id)``; comparing it
against the size of the ranked set names exactly how many matches were never
looked at. The lexical-only scope is inherited deliberately and is the one
false negative this signal has: a ranked set truncated purely on the vector
side reports ``exhausted``. See ``test_the_signal_is_lexical_only_and_says_so``.

No PII: every value here is an integer.
"""
from __future__ import annotations

import pytest

from brain.search import CANDIDATE_LIMIT
from brain.ui import schemas

#: Opens NO database connection — every function under test is pure arithmetic
#: over three integers. The marker keeps this file off the MACHINE-WIDE
#: advisory lock and off the schema reset; see
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb


def test_the_ceiling_is_derived_from_candidate_limit_never_copied() -> None:
    """A literal here would advertise the OLD bound the day the constant moves.

    This is the same two-sources-of-truth failure ``MAX_OFFSET``'s own comment
    records for the Ollama port guard, where only one of two copies was
    redirected and the guard went on guarding a port nothing dialled.
    """
    assert schemas.MAX_RANKED_DOCUMENTS == 2 * CANDIDATE_LIMIT
    # MAX_OFFSET was already derived from CANDIDATE_LIMIT for exactly the same
    # reason. It is now derived from the NAMED quantity instead, because "the
    # largest offset" and "the largest number of rankable documents" are the
    # same number only because they have the same cause.
    assert schemas.MAX_OFFSET == schemas.MAX_RANKED_DOCUMENTS


def test_a_filled_over_fetch_reports_more() -> None:
    """``ranked == fetch_limit`` means the ranker had at least this much."""
    payload = schemas.ranking_payload(
        ranked=25, fetch_limit=25, total_documents=544
    )
    assert payload["status"] == schemas.RANKING_MORE


def test_a_short_ranking_that_covers_every_match_reports_exhausted() -> None:
    """The genuine end of the result set: the ranker ran dry AND nothing is
    left behind it."""
    payload = schemas.ranking_payload(
        ranked=7, fetch_limit=25, total_documents=7
    )
    assert payload["status"] == schemas.RANKING_EXHAUSTED


def test_a_short_ranking_with_matches_left_behind_reports_ceiling() -> None:
    """THE DEFECT. The ranker ran dry at 87 while 544 documents match, so 457
    matching documents were never ranked and cannot be reached by paging."""
    payload = schemas.ranking_payload(
        ranked=87, fetch_limit=100, total_documents=544
    )
    assert payload["status"] == schemas.RANKING_CEILING
    assert payload["ranked_documents"] == 87
    assert payload["max_ranked_documents"] == 2 * CANDIDATE_LIMIT


def test_an_empty_page_past_the_ceiling_is_not_exhaustion() -> None:
    """The exact response shape the defect was reported against.

    A page beyond the ranked set is empty. ``exhausted`` here would be the lie:
    it says "you have seen everything" to a caller who has seen 87 of 544.
    """
    payload = schemas.ranking_payload(
        ranked=0, fetch_limit=125, total_documents=544
    )
    assert payload["status"] == schemas.RANKING_CEILING
    assert payload["status"] != schemas.RANKING_EXHAUSTED


def test_an_empty_page_over_an_empty_corpus_is_exhaustion() -> None:
    """The other empty page, which must NOT be dressed up as a ceiling.

    Nothing matched, nothing was withheld. Reporting ``ceiling`` here would
    send every genuinely-empty search to the "refine your query, there is more"
    message and make the new signal worthless in the direction it matters.
    """
    payload = schemas.ranking_payload(ranked=0, fetch_limit=25, total_documents=0)
    assert payload["status"] == schemas.RANKING_EXHAUSTED


def test_a_failed_count_is_unknown_rather_than_exhausted() -> None:
    """``total_documents`` is ``None`` when the count query failed, and
    ``SearchDiagnostics`` is explicit that a caller "must render the total as
    unknown, never as zero".

    Treating ``None`` as zero here would resolve to ``exhausted`` — the precise
    lie this signal exists to stop, reintroduced through the error path.
    """
    payload = schemas.ranking_payload(
        ranked=12, fetch_limit=25, total_documents=None
    )
    assert payload["status"] == schemas.RANKING_UNKNOWN


def test_the_signal_is_lexical_only_and_says_so() -> None:
    """The documented false negative, asserted rather than left to the prose.

    ``total_documents`` counts LEXICAL matches only; the vector leg may surface
    near-neighbours it does not count. So a ranked set that is larger than the
    lexical total is normal, and is reported ``exhausted`` even though the
    vector leg may itself have been truncated. Pinned so the limitation is a
    known property with a name rather than a surprise.
    """
    payload = schemas.ranking_payload(ranked=30, fetch_limit=50, total_documents=4)
    assert payload["status"] == schemas.RANKING_EXHAUSTED


def test_the_four_statuses_are_distinct() -> None:
    """Anti-vacuity for every comparison above: two constants that collapsed to
    the same string would make several of these tests pass while the payload
    told the ledger nothing."""
    statuses = {
        schemas.RANKING_MORE,
        schemas.RANKING_EXHAUSTED,
        schemas.RANKING_CEILING,
        schemas.RANKING_UNKNOWN,
    }
    assert len(statuses) == 4, f"statuses collapsed: {statuses}"
