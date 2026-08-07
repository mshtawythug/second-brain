"""``brain reembed``'s egress veto and the opt-in search sensitivity lens (F6).

Two boundaries meet here, and they are deliberately asymmetric:

- ``brain reembed`` MUST honour the ingest-time veto. It is the one command that
  could undo the whole boundary in a single pass: every body withheld at ingest
  is, by definition, sitting in a chunk with a NULL embedding — exactly the rows
  ``reembed`` exists to fill. Filling them under a hosted embedder would ship
  the entire confidential corpus off-machine at once.
- ``brain search`` MUST NOT hide anything by default. The local CLI is inside
  the trust boundary, identical to ``draft``. The filter is a LENS ("show me
  what I've marked"), and its default must leave ranked output untouched — which
  is also what keeps ``tests/eval/baselines/ci.json`` valid.

All documents are synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import (
    count_chunks_missing_embedding,
    count_confidential_documents,
)
from brain.search import hybrid_search
from brain.search_predicate import build_predicate
from brain.sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from tests.conftest import FakeEmbedder

# Shared vocabulary so ONE query matches both documents — the sensitivity
# filter, not the query, must be what separates them.
#
# Every term in _QUERY is deliberately STEM-STABLE (``plainto_tsquery(w) ==
# to_tsquery(plainto_tsquery(w))``). ``_build_tsquery`` round-trips the query
# through ``plainto_tsquery`` and ``hybrid_search`` then hands the result to
# ``to_tsquery``, which stems the already-stemmed lexeme a SECOND time. For a
# word like "provisioning" that is lossy (provis -> provi) and, because terms are
# AND-ed, one such word silently zeroes the entire FTS leg. That is a real
# pre-existing bug in search.py, reported separately; these tests avoid the
# shape so a red result here means the sensitivity filter broke, not the stemmer.
_QUERY = "quarterly planning cadence workflow"

_NORMAL_BODY = (
    "Public roadmap notes. The quarterly planning cadence and the release "
    "workflow for onboarding new teammates were reviewed at length, along "
    "with the documentation backlog.\n"
)
_CONFIDENTIAL_BODY = (
    "Restricted compensation banding notes. The payroll workflow and the "
    "quarterly planning cadence were reviewed, including the escalation "
    "path.\n"
)


class HostedFake(FakeEmbedder):
    """Hosted backend double — declares the duck-typed egress flag."""

    hosted_egress: bool = True


def _seed(
    conn: psycopg.Connection[Any], *, title: str, body: str, level: str
) -> str:
    """Ingest one synthetic note at ``level`` using a LOCAL embedder.

    Deliberately local: seeding must produce real vectors even for the
    confidential row, so that a later assertion about ``reembed`` skipping it is
    about ``reembed``'s own behaviour and not an artifact of how it was seeded.
    """
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=title,
        sensitivity=level,
    )
    assert result.document_id is not None
    return result.document_id


# --------------------------------------------------------------------------
# reembed veto
# --------------------------------------------------------------------------


def test_count_confidential_documents_counts_only_marked_rows(
    test_db: psycopg.Connection[Any],
) -> None:
    """The finalize gate's input: how many rows are deliberately non-normal."""
    assert count_confidential_documents(test_db) == 0

    _seed(
        test_db,
        title="Synthetic normal one",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    assert count_confidential_documents(test_db) == 0

    _seed(
        test_db,
        title="Synthetic secret one",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )
    assert count_confidential_documents(test_db) == 1


def test_null_chunk_count_excludes_confidential_when_asked(
    test_db: psycopg.Connection[Any],
) -> None:
    """``exclude_confidential`` removes confidential chunks from the work set.

    Asserted on the COUNT rather than only the iterator because the two are what
    produce ``reembed``'s ``embedded N/total`` progress line — a denominator that
    disagreed with the iterator would leave a run that never reaches its total.
    """
    # Arrange — two docs, then blank every embedding so both are "missing".
    _seed(
        test_db,
        title="Synthetic normal two",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    conf_id = _seed(
        test_db,
        title="Synthetic secret two",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )
    test_db.execute("UPDATE chunks SET embedding = NULL")

    # Act
    total = count_chunks_missing_embedding(test_db)
    scoped = count_chunks_missing_embedding(test_db, exclude_confidential=True)

    # Assert
    conf_chunks = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s", (conf_id,)
    ).fetchone()
    assert conf_chunks is not None
    assert conf_chunks[0] > 0, "the confidential doc must have chunks to exclude"
    assert total > scoped, "excluding must shrink the work set"
    assert total - scoped == conf_chunks[0]


def test_reembed_under_hosted_embedder_skips_and_reports(
    test_db: psycopg.Connection[Any],
    monkeypatch: Any,
) -> None:
    """``brain reembed`` refuses confidential chunks, says so, and skips finalize.

    ``_build_embedder`` is replaced with a test double via ``monkeypatch``
    (permitted by CLAUDE.md rule 13 — it is not a production-module rewrite)
    because the active backend is otherwise chosen from env config that would
    require a real Voyage API key.
    """
    # Arrange
    _seed(
        test_db,
        title="Synthetic normal three",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    _seed(
        test_db,
        title="Synthetic secret three",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )
    test_db.execute("UPDATE chunks SET embedding = NULL")
    monkeypatch.setattr("brain.cli_ingest._build_embedder", lambda cfg: HostedFake())

    # Act
    result = CliRunner().invoke(app, ["reembed"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "confidential document(s) skipped" in result.output
    assert "finalize skipped" in result.output
    assert "NOT NULL" in result.output, (
        "the message must explain WHY finalize was skipped, or the user cannot "
        "tell it apart from an ordinary NULL backlog"
    )

    # ...and the confidential chunks are still NULL, i.e. never embedded.
    still_null = test_db.execute(
        """
        SELECT count(*) FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.sensitivity = %s AND c.embedding IS NULL
        """,
        (CONFIDENTIAL,),
    ).fetchone()
    assert still_null is not None and still_null[0] > 0


def test_reembed_under_local_embedder_fills_confidential_chunks(
    test_db: psycopg.Connection[Any],
    monkeypatch: Any,
) -> None:
    """Under a LOCAL backend, reembed treats confidential rows normally.

    This is the documented recovery path: ingest under a hosted embedder leaves
    NULLs, the user switches to a local backend, and ``brain reembed`` gives
    those documents vectors with no manual repair. Without this test the veto
    could be implemented as a permanent exclusion and nobody would notice.
    """
    _seed(
        test_db,
        title="Synthetic secret four",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )
    test_db.execute("UPDATE chunks SET embedding = NULL")
    monkeypatch.setattr("brain.cli_ingest._build_embedder", lambda cfg: FakeEmbedder())

    result = CliRunner().invoke(app, ["reembed", "--no-finalize"])

    assert result.exit_code == 0, result.output
    assert "confidential document(s) skipped" not in result.output
    remaining = test_db.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    ).fetchone()
    assert remaining is not None and remaining[0] == 0


# --------------------------------------------------------------------------
# search lens
# --------------------------------------------------------------------------


def test_search_without_the_filter_returns_both_tiers(
    test_db: psycopg.Connection[Any],
) -> None:
    """DEFAULT BEHAVIOUR IS UNCHANGED: an unfiltered search hides nothing.

    The most important assertion in this module. The local CLI is inside the
    trust boundary by design; if this ever goes red, F6 has quietly become an
    access control, every eval metric has moved, and ``ci.json`` is invalid.
    """
    _seed(
        test_db,
        title="Synthetic normal five",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    _seed(
        test_db,
        title="Synthetic secret five",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )

    results = hybrid_search(
        test_db,
        embedder=FakeEmbedder(),
        query=_QUERY,
        limit=10,
        fts_only=True,
    )

    titles = {r.title for r in results}
    assert "Synthetic normal five" in titles
    assert "Synthetic secret five" in titles, (
        "an unfiltered brain search must still return confidential documents — "
        "the local CLI is inside the trust boundary (same posture as draft)"
    )


def test_search_filtered_to_confidential_returns_only_marked_docs(
    test_db: psycopg.Connection[Any],
) -> None:
    """The lens works: ``sensitivity='confidential'`` narrows to marked docs."""
    _seed(
        test_db,
        title="Synthetic normal six",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    _seed(
        test_db,
        title="Synthetic secret six",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )

    results = hybrid_search(
        test_db,
        embedder=FakeEmbedder(),
        query=_QUERY,
        limit=10,
        fts_only=True,
        sensitivity=CONFIDENTIAL,
    )

    titles = {r.title for r in results}
    assert titles == {
        "Synthetic secret six"
    }, f"expected only the confidential doc, got {sorted(titles)}"


def test_search_filtered_to_normal_excludes_confidential(
    test_db: psycopg.Connection[Any],
) -> None:
    """The lens works in the other direction too (both literals are reachable)."""
    _seed(
        test_db,
        title="Synthetic normal seven",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    _seed(
        test_db,
        title="Synthetic secret seven",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )

    results = hybrid_search(
        test_db,
        embedder=FakeEmbedder(),
        query=_QUERY,
        limit=10,
        fts_only=True,
        sensitivity=DEFAULT_SENSITIVITY,
    )

    titles = {r.title for r in results}
    assert titles == {"Synthetic normal seven"}


def test_default_predicate_is_byte_identical_to_omitting_the_parameter() -> None:
    """PROOF (1/2) that ``sensitivity=None`` cannot move a single eval metric.

    Compares the whole :class:`SearchPredicate` field-for-field, not just
    ``where_sql``. Every field matters to the emitted SQL and to planning:
    ``join_clause`` decides whether the ``documents`` JOIN is present at all,
    and ``prepare_flag`` decides whether psycopg prepares the statement. If the
    new parameter left ``where_sql`` alone but flipped ``has_filters``, the
    unfiltered fast path would silently regain a JOIN and lose its prepared
    statement — a performance regression invisible to a result-set assertion.

    Pure logic, no DB: this is the algebraic half of the argument.
    """
    baseline = build_predicate()
    with_none = build_predicate(sensitivity=None)

    assert with_none == baseline, (
        "passing sensitivity=None must construct an identical predicate to "
        "omitting it entirely"
    )
    # ...and spell out the fast-path invariants the equality above subsumes, so
    # a future dataclass field cannot make the comparison vacuously true.
    assert with_none.where_sql == "TRUE"
    assert with_none.where_params == ()
    assert with_none.has_filters is False
    assert with_none.join_clause == ""
    assert with_none.fts_filter == ""
    assert with_none.prepare_flag is True


def test_default_search_results_are_identical_with_and_without_the_kwarg(
    test_db: psycopg.Connection[Any],
) -> None:
    """PROOF (2/2): the ranked output itself is unchanged on the default path.

    The empirical half. Ranking is what ``tests/eval/baselines/ci.json`` would
    measure, so identical ordering AND identical scores across both call shapes
    is the direct evidence that no baseline re-record is required.

    Scores are compared exactly rather than approximately: the two calls run the
    same SQL against the same rows, so any difference at all would mean the
    parameter perturbed the query plan or the RRF inputs.
    """
    _seed(
        test_db,
        title="Synthetic normal nine",
        body=_NORMAL_BODY,
        level=DEFAULT_SENSITIVITY,
    )
    _seed(
        test_db,
        title="Synthetic secret nine",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )

    omitted = hybrid_search(
        test_db, embedder=FakeEmbedder(), query=_QUERY, limit=10, fts_only=True
    )
    explicit_none = hybrid_search(
        test_db,
        embedder=FakeEmbedder(),
        query=_QUERY,
        limit=10,
        fts_only=True,
        sensitivity=None,
    )

    assert [r.document_id for r in omitted] == [
        r.document_id for r in explicit_none
    ], "the default path must return the same documents in the same order"
    assert [r.score for r in omitted] == [
        r.score for r in explicit_none
    ], "identical scores — the parameter must not perturb ranking at all"
    # Both tiers present, i.e. the default really is "no filter" and not
    # "accidentally normal-only".
    assert len(omitted) == 2


def test_explain_reports_the_sensitivity_filter(
    test_db: psycopg.Connection[Any],
) -> None:
    """``matched_filters`` carries the level so ``brain explain`` can show it."""
    _seed(
        test_db,
        title="Synthetic secret eight",
        body=_CONFIDENTIAL_BODY,
        level=CONFIDENTIAL,
    )

    results = hybrid_search(
        test_db,
        embedder=FakeEmbedder(),
        query=_QUERY,
        limit=5,
        fts_only=True,
        sensitivity=CONFIDENTIAL,
        explain=True,
    )

    assert results
    assert results[0].explain is not None
    assert results[0].explain.matched_filters["sensitivity"] == CONFIDENTIAL
