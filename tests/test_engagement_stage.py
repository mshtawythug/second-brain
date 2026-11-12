"""``EngagementStage`` over the real test database (RM-22b).

Two halves. The aggregate (:func:`brain.search_engagement.fetch_engagement`):
decay, window, the same-session and ask-citation exclusions, each beside the
ordinary case that must still count. The ranking over ``hybrid_search`` and the
RM-20 corpus (:mod:`tests.doc_leg_corpus`), where plain search ranks the "fit"
document last: an opened document rises (AC2), a rated-irrelevant one falls
(AC4), filters and the sensitivity lens still exclude, and ``brain ask``'s own
citations do not feed the boost (D4).

Real Postgres (the 5434 test database). All data is synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain import cli
from brain.ask import AskResult, Citation
from brain.gaps import record_search_query
from brain.search import hybrid_search
from brain.search_engagement import EngagementStage, fetch_engagement
from brain.search_pipeline import PipelineResult, run_search
from tests.doc_leg_corpus import OTHERS, QUERY, reconcile, seed
from tests.saved_search_fakes import WordEmbedder

LIMIT = OTHERS + 1
SEARCH_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000051a1")
OTHER_SEARCH_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000051a2")
ASK_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000a5c01")


def _doc(conn: psycopg.Connection[Any], n: int = 0) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES (%s, 'body', 'note', 'vault', %s) RETURNING id::text",
        (f"Synthetic engagement {n}", f"engagement-hash-{n}-{uuid.uuid4().hex}"),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _row(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    action: str = "opened",
    session: uuid.UUID | None = None,
    days_ago: float = 0.0,
) -> None:
    """One interaction ``days_ago`` days old (negative: in the future)."""
    conn.execute(
        "INSERT INTO interactions (document_id, action, source, session_id, at) "
        "VALUES (%s, %s, 'cli', %s, NOW() - make_interval(secs => %s))",
        (document_id, action, session, days_ago * 86400.0),
    )


def _log_search(conn: psycopg.Connection[Any], session: uuid.UUID) -> None:
    record_search_query(
        conn, query="synthetic", result_count=1, session_id=session, source="mcp"
    )


def _opens(conn: psycopg.Connection[Any], doc: str, session: uuid.UUID | None = None) -> float:
    found = fetch_engagement(conn, [doc], session_id=session)
    return found[doc].opens_decayed if doc in found else 0.0


# --- the aggregate: decay and window ------------------------------------------------


def test_a_fresh_open_weighs_one(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc)

    assert _opens(test_db, doc) == pytest.approx(1.0, abs=1e-6)


def test_an_open_one_half_life_old_weighs_half(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, days_ago=30)

    assert _opens(test_db, doc) == pytest.approx(0.5, abs=1e-6)


def test_a_future_dated_open_weighs_no_more_than_one(test_db: psycopg.Connection[Any]) -> None:
    # Clock skew: an ``at`` in the future must not weigh more than a fresh open.
    doc = _doc(test_db)
    _row(test_db, doc, days_ago=-5)

    assert _opens(test_db, doc) == 1.0


def test_an_open_past_the_window_weighs_nothing(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, days_ago=301)

    assert _opens(test_db, doc) == 0.0


def test_an_open_inside_the_window_still_counts(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, days_ago=29)

    assert _opens(test_db, doc) == pytest.approx(0.5 ** (29 / 30), abs=1e-6)


def test_clicks_are_reads_and_pins_are_not(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, action="clicked")
    _row(test_db, doc, action="pinned")

    assert _opens(test_db, doc) == pytest.approx(1.0, abs=1e-6)


def test_ratings_count_undecayed(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, action="rated_useful", days_ago=400)
    _row(test_db, doc, action="rated_useful")
    _row(test_db, doc, action="rated_irrelevant")

    found = fetch_engagement(test_db, [doc], session_id=None)[doc]

    assert (found.useful, found.irrelevant, found.opens_decayed) == (2, 1, 0.0)


def test_a_document_without_rows_is_absent(test_db: psycopg.Connection[Any]) -> None:
    doc, untouched = _doc(test_db, 0), _doc(test_db, 1)
    _row(test_db, doc)

    assert set(fetch_engagement(test_db, [doc, untouched], session_id=None)) == {doc}


def test_a_graph_target_row_is_ignored(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    test_db.execute(
        "INSERT INTO interactions (action, source, target_type, target_id) "
        "VALUES ('opened', 'cli', 'entity', 'synthetic-entity')"
    )

    assert fetch_engagement(test_db, [doc], session_id=None) == {}


# --- the aggregate: whose opens count -----------------------------------------------


def test_an_open_in_the_current_session_does_not_count(test_db: psycopg.Connection[Any]) -> None:
    # AC3: a result is never boosted by a click from the same search session.
    doc = _doc(test_db)
    _log_search(test_db, SEARCH_SESSION)
    _row(test_db, doc, session=SEARCH_SESSION)

    assert _opens(test_db, doc, SEARCH_SESSION) == 0.0


def test_an_open_from_another_logged_search_counts(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _log_search(test_db, OTHER_SEARCH_SESSION)
    _row(test_db, doc, session=OTHER_SEARCH_SESSION)

    assert _opens(test_db, doc, SEARCH_SESSION) == pytest.approx(1.0, abs=1e-6)
    assert _opens(test_db, doc, None) == pytest.approx(1.0, abs=1e-6)


def test_a_null_session_open_always_counts(test_db: psycopg.Connection[Any]) -> None:
    # AC3, second half: CLI opens and direct opens carry no session.
    doc = _doc(test_db)
    _row(test_db, doc, session=None)

    assert _opens(test_db, doc, SEARCH_SESSION) == pytest.approx(1.0, abs=1e-6)


def test_an_ask_citation_does_not_count(test_db: psycopg.Connection[Any]) -> None:
    # D4: an open under a session no search was logged with is ``brain ask``
    # citing the document, not a person choosing it.
    doc = _doc(test_db)
    _row(test_db, doc, session=ASK_SESSION)

    assert _opens(test_db, doc) == 0.0


def test_the_same_row_counts_once_its_session_names_a_search(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _doc(test_db)
    _row(test_db, doc, session=ASK_SESSION)
    _log_search(test_db, ASK_SESSION)

    assert _opens(test_db, doc) == pytest.approx(1.0, abs=1e-6)


# --- the ranking ----------------------------------------------------------------------


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


def _search(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    stage: EngagementStage | None,
    session_id: str | None = None,
    **kw: Any,
) -> PipelineResult:
    return run_search(
        hybrid_search,
        conn,
        embedder=embedder,
        query=QUERY,
        limit=LIMIT,
        stages=() if stage is None else (stage,),
        session_id=session_id,
        **kw,
    )


def _ids(result: PipelineResult) -> list[str]:
    return [r.document_id for r in result.results]


def _open_many(conn: psycopg.Connection[Any], doc: str, n: int, **kw: Any) -> None:
    for _ in range(n):
        _row(conn, doc, **kw)


def test_a_heavily_opened_document_moves_up(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # AC2.
    ids = seed(test_db, word)
    _open_many(test_db, ids["fit"], 20)

    plain = _ids(_search(test_db, word, stage=None))
    staged_result = _search(test_db, word, stage=EngagementStage(alpha=1.0, beta=0.0))
    staged = _ids(staged_result)

    assert plain[-1] == ids["fit"]
    assert staged[0] == ids["fit"]
    assert sorted(staged) == sorted(plain)
    (report,) = staged_result.stages
    assert (report.name, report.applied, report.skipped_reason) == ("engagement", True, None)


def test_a_rated_irrelevant_document_moves_down(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # AC4: demotion within the clamp.
    ids = seed(test_db, word)
    plain = _ids(_search(test_db, word, stage=None))
    _open_many(test_db, plain[0], 3, action="rated_irrelevant")

    staged = _ids(_search(test_db, word, stage=EngagementStage(alpha=0.0, beta=1.0)))

    assert staged[-1] == plain[0]
    assert staged[:-1] == plain[1:]
    assert ids["fit"] in staged


def test_no_engagement_leaves_the_order_as_it_was(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)

    plain = _ids(_search(test_db, word, stage=None))
    staged = _search(test_db, word, stage=EngagementStage(alpha=1.0, beta=1.0))

    assert _ids(staged) == plain
    assert staged.stages[0].applied


def test_a_source_filter_still_excludes_an_opened_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word, fit_source="krisp")
    _open_many(test_db, ids["fit"], 20)

    staged = _ids(
        _search(
            test_db, word, stage=EngagementStage(alpha=1.0, beta=0.0), source_kind="manual"
        )
    )

    assert ids["fit"] not in staged
    assert len(staged) == OTHERS


def test_the_sensitivity_lens_still_excludes_an_opened_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word, fit_sensitivity="confidential")
    _open_many(test_db, ids["fit"], 20)

    staged = _ids(
        _search(
            test_db, word, stage=EngagementStage(alpha=1.0, beta=0.0), sensitivity="normal"
        )
    )

    assert ids["fit"] not in staged
    assert len(staged) == OTHERS


def test_opens_from_the_current_search_session_do_not_lift(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # AC3 through the pipeline: run_search hands the stage its session.
    ids = seed(test_db, word)
    _log_search(test_db, SEARCH_SESSION)
    _open_many(test_db, ids["fit"], 20, session=SEARCH_SESSION)

    same = _ids(
        _search(
            test_db,
            word,
            stage=EngagementStage(alpha=1.0, beta=0.0),
            session_id=str(SEARCH_SESSION),
        )
    )
    later = _ids(
        _search(
            test_db,
            word,
            stage=EngagementStage(alpha=1.0, beta=0.0),
            session_id=str(OTHER_SEARCH_SESSION),
        )
    )

    assert same[-1] == ids["fit"]
    assert later[0] == ids["fit"]


def _cite(conn: psycopg.Connection[Any], doc: str) -> None:
    """``brain ask``'s real writer: one ``opened`` row per citation, under the ask's session."""
    result = AskResult(
        answer="synthetic answer [1]",
        citations=[Citation(ref=1, document_id=doc, title="t", source_kind=None, snippet="s")],
        iterations_used=1,
        sub_queries=["synthetic"],
        fallback_used=False,
        session_id=uuid.uuid4().hex,
    )
    cli._log_ask_interactions(conn, result, source="cli")


def test_ask_citations_do_not_feed_the_boost(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # D4, the feedback loop: with the stage on, a cited document would be
    # boosted, retrieved again, cited again, and boosted again. Twenty asks
    # citing it must not move it.
    ids = seed(test_db, word)
    for _ in range(20):
        _cite(test_db, ids["fit"])

    staged = _ids(_search(test_db, word, stage=EngagementStage(alpha=1.0, beta=0.0)))

    assert staged[-1] == ids["fit"]


def test_the_same_number_of_reads_from_searches_does_lift(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # The ordinary twin of the citation test: a person opening it from twenty
    # searches is exactly the signal the stage is for.
    ids = seed(test_db, word)
    for _ in range(20):
        session = uuid.uuid4()
        _log_search(test_db, session)
        _row(test_db, ids["fit"], session=session)

    staged = _ids(_search(test_db, word, stage=EngagementStage(alpha=1.0, beta=0.0)))

    assert staged[0] == ids["fit"]
