"""T7 — ``build_predicate(source_missing=...)`` and its eval-neutrality pin.

Two independent claims, tested separately because they fail separately.

**(a) The default branch is byte-identical to the pre-change code.** This is the
entire argument for why T7 needs no ``tests/eval/baselines/ci.json`` re-record,
and until now it was only ever *asserted*. :data:`PRE_CHANGE_PREDICATES` is a
table captured by running the **unmodified** ``build_predicate`` over one case
per filter plus a combination, so the pin compares against what the function
actually produced rather than against a transcription of what it looked like it
would produce. Any edit that perturbs the emitted SQL for a caller that did not
opt in turns this red.

**(b) The new clause reaches the documents nothing else can.**
``d.source_id IN (SELECT id FROM sources WHERE kind=%s)`` is false whenever
``source_id`` is NULL, for every value of ``kind`` — so a source-less document
is invisible to the source filter in all four of its settings. The DB-backed
test below seeds exactly that shape and drives the real predicate against a
real Postgres, because the claim is about what SQL *matches*, and a string
comparison cannot check that.

No PII: three synthetic notes, no names, no addresses.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.search_predicate import build_predicate

#: One case per filter, plus a combination, captured from ``build_predicate``
#: **before** ``source_missing`` existed. Each value is the exact ``where_sql``
#: that call produced.
#:
#: ``fts_filter`` and ``join_clause`` are checked too (below) but are not listed
#: here: both are pure functions of ``where_sql`` (``f" AND {where_sql}"`` and a
#: single literal), so pinning them as separate strings would be pinning the
#: same fact three times and would drift into a table nobody re-derives.
PRE_CHANGE_PREDICATES: dict[str, tuple[dict[str, Any], str]] = {
    "no_filters": ({}, "TRUE"),
    "source_kind": (
        {"source_kind": "gmail"},
        "TRUE AND d.source_id IN (SELECT id FROM sources WHERE kind=%s)",
    ),
    "tag": ({"tag": "planning"}, "TRUE AND %s = ANY(d.tags)"),
    "since_days": (
        {"since_days": 7},
        "TRUE AND d.ingested_at >= NOW() - make_interval(days => %s)",
    ),
    "person_keys": (
        {"person_keys": ["someone"]},
        "TRUE AND EXISTS (SELECT 1 FROM unnest(d.participants) AS _p "
        "WHERE lower(_p) = ANY(%s::text[]))",
    ),
    "after": (
        {"after": datetime(2026, 1, 1)},
        "TRUE AND coalesce(d.sent_at, d.ingested_at) >= %s",
    ),
    "before": (
        {"before": datetime(2026, 2, 1)},
        "TRUE AND coalesce(d.sent_at, d.ingested_at) < %s",
    ),
    "updated_after": (
        {"updated_after": datetime(2026, 1, 1)},
        "TRUE AND d.updated_at >= %s",
    ),
    "updated_before": (
        {"updated_before": datetime(2026, 2, 1)},
        "TRUE AND d.updated_at < %s",
    ),
    "content_type": ({"content_type": "note"}, "TRUE AND d.content_type = %s"),
    "thread_id": ({"thread_id": "thread-1"}, "TRUE AND d.thread_id = %s"),
    "draft": ({"draft": False}, "TRUE AND d.draft = %s"),
    "without_tag": ({"without_tag": "archived"}, "TRUE AND NOT (%s = ANY(d.tags))"),
    "sensitivity": ({"sensitivity": "normal"}, "TRUE AND d.sensitivity = %s"),
    "combined": (
        {
            "source_kind": "gmail",
            "tag": "planning",
            "content_type": "note",
            "after": datetime(2026, 1, 1),
            "sensitivity": "normal",
        },
        "TRUE AND d.source_id IN (SELECT id FROM sources WHERE kind=%s) "
        "AND %s = ANY(d.tags) AND coalesce(d.sent_at, d.ingested_at) >= %s "
        "AND d.content_type = %s AND d.sensitivity = %s",
    ),
}

_JOIN = "JOIN documents d ON d.id = c.document_id"


@pytest.mark.parametrize("case", sorted(PRE_CHANGE_PREDICATES))
def test_default_branch_sql_is_byte_identical_to_the_pre_change_code(
    case: str,
) -> None:
    """Every non-opted-in caller gets the SQL it got before T7 existed.

    Not "equivalent" and not "semantically unchanged" — the same bytes. That is
    what makes the no-eval-re-record claim checkable: a plan can only assert
    eval-neutrality, but a byte comparison against a captured table can fail.
    """
    kwargs, expected_where = PRE_CHANGE_PREDICATES[case]
    predicate = build_predicate(**kwargs)

    assert predicate.where_sql == expected_where, (
        f"{case}: where_sql changed. The default branch of build_predicate is "
        "pinned because T7's whole eval-neutrality argument rests on it being "
        "byte-identical for callers that did not opt in."
    )
    # The three derived fields are functions of ``where_sql``; pinning them here
    # is what stops a future edit from changing the derivation while leaving the
    # string above intact.
    has_filters = expected_where != "TRUE"
    assert predicate.has_filters is has_filters
    assert predicate.join_clause == (_JOIN if has_filters else "")
    assert predicate.fts_filter == (f" AND {expected_where}" if has_filters else "")
    assert predicate.prepare_flag == (None if has_filters else True)


def test_the_pin_covers_every_filter_the_function_accepts() -> None:
    """The table above must not silently fall behind the signature.

    A filter added to ``build_predicate`` without a row here would be outside
    the byte-identity pin — the pin would still be green and would still be
    advertised as proving eval-neutrality, while no longer proving it for the
    new parameter. That is precisely the shape of guard this repo has been
    caught shipping before, so the coverage is itself asserted.
    """
    import inspect

    signature = inspect.signature(build_predicate)
    # ``source_missing`` is the parameter under test, not a pre-change one.
    covered = {"source_missing"}
    for kwargs, _ in PRE_CHANGE_PREDICATES.values():
        covered.update(kwargs)

    missing = set(signature.parameters) - covered
    assert not missing, (
        f"build_predicate grew {sorted(missing)} with no row in "
        "PRE_CHANGE_PREDICATES — add one (capturing the string the function "
        "actually emits) or the byte-identity pin no longer covers it"
    )


def test_source_missing_defaults_to_off() -> None:
    """The default is exactly the no-argument call, field for field.

    Stated separately from the table because it is the claim the table's
    ``no_filters`` row cannot make: that passing the parameter *explicitly* at
    its default is also a no-op.
    """
    assert build_predicate(source_missing=False) == build_predicate()


def test_source_missing_appends_one_literal_clause_and_binds_nothing() -> None:
    """Opting in adds the clause — and no parameter.

    It is a flag, not a value, so there is nothing to bind. Asserting the param
    count is what would catch a future edit that "helpfully" made the clause
    ``d.source_id IS NOT DISTINCT FROM %s``: still correct SQL, but it would
    desynchronise the positional ``where_params`` every caller splats.
    """
    predicate = build_predicate(source_missing=True)

    assert predicate.where_sql == "TRUE AND d.source_id IS NULL"
    assert predicate.where_params == ()
    assert predicate.has_filters is True
    assert predicate.join_clause == _JOIN
    assert predicate.prepare_flag is None


# --------------------------------------------------------------- DB-backed --


@pytest.fixture
def source_corpus(test_db: psycopg.Connection) -> dict[str, str]:
    """Three documents: one with **no** source row, two with real ones.

    Inserted directly rather than through ``ingest_document`` for one reason:
    every ingest path calls ``_upsert_source`` and therefore *cannot* produce a
    NULL ``source_id``. The row shape under test is real (876 of them in the
    reference corpus, written by the vault sync path) but is unreachable from
    the fixture factories.
    """
    ids: dict[str, str] = {}
    for label, kind in [("gmail_doc", "gmail"), ("manual_doc", "manual")]:
        source_id = test_db.execute(
            "INSERT INTO sources (kind, external_id) VALUES (%s, %s) RETURNING id",
            (kind, f"t7-{label}"),
        ).fetchone()[0]
        ids[label] = str(
            test_db.execute(
                "INSERT INTO documents (source_id, title, content, content_hash, "
                "content_type) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (source_id, f"T7 {label}", "synthetic body", f"t7-hash-{label}", "note"),
            ).fetchone()[0]
        )

    ids["sourceless_doc"] = str(
        test_db.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            ("T7 sourceless", "synthetic body", "t7-hash-sourceless", "note"),
        ).fetchone()[0]
    )
    return ids


def _matching_ids(
    conn: psycopg.Connection, ids: dict[str, str], **kwargs: Any
) -> set[str]:
    """Which of the seeded documents ``build_predicate(**kwargs)`` selects.

    The predicate is executed as SQL — parameterized, its own ``%s`` list
    splatted positionally exactly as every search leg does — because the claim
    under test is about what the clause MATCHES, and no string comparison can
    check that.
    """
    predicate = build_predicate(**kwargs)
    rows = conn.execute(
        f"SELECT d.id::text FROM documents d "  # noqa: S608 - literal fragment
        f"WHERE d.id = ANY(%s) AND {predicate.where_sql}",
        [list(ids.values()), *predicate.where_params],
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_the_sourceless_document_is_reachable_only_with_source_missing(
    test_db: psycopg.Connection, source_corpus: dict[str, str]
) -> None:
    """The hole, and its closure, in one test.

    Both halves are load-bearing and neither implies the other: a clause of
    ``d.source_id IS NOT NULL`` would pass "no other value returns it" while
    failing the first assertion, and dropping the clause entirely would pass
    the second half while failing the first.
    """
    assert _matching_ids(test_db, source_corpus, source_missing=True) == {
        source_corpus["sourceless_doc"]
    }, "source_missing=True did not select exactly the document with no source row"

    for kind in ("manual", "krisp", "gmail", "slack"):
        matched = _matching_ids(test_db, source_corpus, source_kind=kind)
        assert source_corpus["sourceless_doc"] not in matched, (
            f"source_kind={kind!r} returned the source-less document; the two "
            "filters are supposed to be disjoint views"
        )

    # Anti-vacuity: the loop above would also pass if `source_kind` matched
    # nothing at all, which would make "the source-less doc is absent" true for
    # the wrong reason.
    assert _matching_ids(test_db, source_corpus, source_kind="gmail") == {
        source_corpus["gmail_doc"]
    }


def test_combining_the_two_source_filters_is_an_empty_conjunction(
    test_db: psycopg.Connection, source_corpus: dict[str, str]
) -> None:
    """Documented behaviour, pinned: contradictory filters return nothing.

    ``build_predicate`` is a conjunction builder and deliberately does not
    special-case the combination. Pinning it means a future "helpful" change to
    OR them together — which would make the Source dropdown return a superset
    of what it says — has to be a deliberate edit to this test.
    """
    assert not _matching_ids(
        test_db, source_corpus, source_kind="gmail", source_missing=True
    )


# ---------------------------------------------------- whole-path SQL pin --
#
# The pin above covers ``build_predicate``. This one covers the PATH: the SQL
# ``hybrid_search`` actually hands to Postgres. Pinning only the predicate layer
# leaves exactly the gap that produced spec defect S16 — a parameter that is
# correct where it is built and never arrives where it is used.
#
# ``search.py`` is eval-gated and ``brain eval`` was NOT run for this change
# (it needs a live corpus + Ollama). These byte comparisons are the evidence
# offered in its place: with ``source_missing=False`` the emitted statements are
# identical to the pre-change ones, so no ranked result set can move.

#: The EXACT statements ``hybrid_search`` emitted BEFORE ``source_missing``
#: existed, captured by running the reverted code against a real Postgres
#: through :class:`RecordingConnection` — not transcribed by hand and not
#: re-derived from the f-string, either of which would pin what the author
#: believed rather than what the function emitted.
#:
#: Held in a golden file rather than as literals because a 700-character SQL
#: string cannot live inside the project's 100-column limit without being
#: re-wrapped, and re-wrapping a byte pin by hand is the one edit that silently
#: destroys it.
#:
#: **Regenerating this file to make a test pass defeats its entire purpose.** It
#: records what the ranker asked Postgres before this change; if a diff appears,
#: the question is what moved in the ranking SQL and whether the eval baseline
#: must be re-recorded — never how to make the comparison green again.
PRE_CHANGE_SQL_PATH = Path(__file__).parent / "fixtures" / (
    "pre_change_hybrid_search_sql.json"
)
PRE_CHANGE_STATEMENTS: dict[str, str] = json.loads(
    PRE_CHANGE_SQL_PATH.read_text()
)


class RecordingConnection:
    """A real connection that records every statement passing through it.

    Not a mock and not a monkey-patch: ``hybrid_search`` takes its connection as
    an argument, so the seam is the production signature. The statements are
    captured on the way to a real Postgres, which is why the pin reflects what
    the database was actually asked, not what a reader thinks the f-string
    builds.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql: Any, params: Any = None, **kwargs: Any) -> Any:
        self.statements.append(str(sql))
        return self._inner.execute(sql, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _statements(conn: Any, embedder: Any, **kwargs: Any) -> list[str]:
    """Every statement one ``hybrid_search`` call emits, in order."""
    from brain.search import hybrid_search

    recorder = RecordingConnection(conn)
    hybrid_search(recorder, embedder=embedder, query="anything", **kwargs)
    return recorder.statements


@pytest.mark.parametrize(
    ("case", "index", "kwargs"),
    [
        ("fts_no_filters", 1, {"fts_only": True}),
        ("fts_source_kind", 1, {"fts_only": True, "source_kind": "gmail"}),
        ("vector_no_filters", 2, {"fts_only": False}),
    ],
)
def test_the_whole_path_emits_byte_identical_sql_when_the_flag_is_off(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    case: str,
    index: int,
    kwargs: dict[str, Any],
) -> None:
    """End to end through ``hybrid_search``, not just ``build_predicate``.

    Run with the flag left at its default. A stray clause anywhere in the
    predicate — or any edit to the ranking SQL — changes these bytes, which is
    what makes "this change cannot move an eval score" a checked statement
    rather than an argued one.
    """
    emitted = _statements(test_db, fake_embedder, **kwargs)

    assert emitted[index] == PRE_CHANGE_STATEMENTS[case], (
        f"{case}: the SQL hybrid_search sends to Postgres changed while "
        "source_missing was OFF. Every non-opting caller — including the "
        "eval harness — is affected."
    )


def test_passing_the_flag_off_explicitly_is_the_same_as_not_passing_it(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """``source_missing=False`` must be indistinguishable from absence.

    Separate from the pin above because it is a different claim: the pin says
    "not passing it emits the old SQL", this says "passing it off emits the
    same SQL as not passing it". A default that was accidentally truthy would
    satisfy the first and fail this.
    """
    assert _statements(test_db, fake_embedder, fts_only=True) == _statements(
        test_db, fake_embedder, fts_only=True, source_missing=False
    )


def test_the_flag_reaches_the_sql_when_it_is_on(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """The pass-through is LIVE — the clause appears in the emitted statement.

    This is the assertion that spec defect S16 was about. Everything else in
    this module can pass with ``hybrid_search`` silently dropping the argument
    on the floor; only this fails.
    """
    emitted = _statements(test_db, fake_embedder, fts_only=True, source_missing=True)

    assert "d.source_id IS NULL" in emitted[1], (
        "source_missing=True did not reach the SQL hybrid_search executes — "
        "the kwarg is accepted and discarded"
    )
