"""Discovery reads for ``brain ui`` — recent rail, tag index, tag counts.

Covers the three functions T4 adds to :mod:`brain.ui.queries`, the module whose
docstring declares it the only place in the package allowed to hold SQL.

``recent_documents`` is a port of ``brain.wiki.build_homepage._fetch_recent_docs``
(the P4.7 wiki recent rail) that returns document **ids** rather than wiki-link
paths, because the UI navigates by id. The port carries the wiki version's
filter set. Each predicate's test is named below, along with what dropping that
predicate actually does — measured, not assumed:

==================================  ====================================  ================
predicate                           test                                  dropping it alone
==================================  ====================================  ================
``draft = FALSE``                   ``…excludes_drafts``                  2 tests red
``vault_path IS NOT NULL``          ``…excludes_unexported_documents``    nothing (below)
``vault_path <> 'index.md'``        ``…excludes_the_home_note``           1 test red
``vault_path NOT LIKE 'people/%'``  ``…excludes_people_hub_pages``        2 tests red
``ingested_at IS NOT NULL``         *(schema-pin test below)*             nothing (below)
``sensitivity <> 'confidential'``   ``…excludes_confidential_documents``  2 tests red
==================================  ====================================  ================

The last row is **not** from the wiki query — it was added 2026-08-14 for T17,
whose recent rail paints on first load rather than on navigation. Both of its
tests are named ``…excludes_confidential_documents``, one on ``recent_documents``
and one on ``documents_for_tag``; removing the predicate reddens exactly those
two and nothing else (measured: 2 failed, 21 passed).

Two of the five cannot be falsified on their own, and saying so is the point:

* ``ingested_at IS NOT NULL`` is vacuous. ``documents.ingested_at`` is
  ``NOT NULL`` in ``001_init.sql``, so no row can violate it. It is kept for
  parity with the wiki query and pinned by
  ``test_ingested_at_is_not_null_so_its_predicate_is_vacuous``, which goes red
  the day a migration relaxes the column — the day it starts to matter.
* ``vault_path IS NOT NULL`` is *redundant*, not vacuous. Under SQL's
  three-valued logic ``NULL <> 'index.md'`` and ``NULL NOT LIKE 'people/%'`` are
  both NULL, so either path predicate already drops a NULL-path row.
  ``…excludes_unexported_documents`` therefore proves the **trio** excludes
  un-exported rows (drop all three and it goes red), not that this one clause
  does. The clause stays as the statement of intent and as the guarantee that
  survives any future narrowing of the other two.

All fixtures are synthetic (CLAUDE.md r15).
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.sensitivity import CONFIDENTIAL
from brain.ui.queries import (
    browseable_tag_counts,
    documents_for_tag,
    recent_documents,
    tag_counts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _export(
    conn: psycopg.Connection[Any],
    doc_id: str,
    vault_path: str | None,
    *,
    draft: bool = False,
    sent_at: datetime.datetime | None = None,
    ingested_at: datetime.datetime | None = None,
    source_kind: str | None = None,
) -> str:
    """Place a seeded document in the vault, with optional dates / draft flag.

    ``seed_doc`` ingests through the real pipeline, so ``vault_path`` may or may
    not already be set depending on the auto-mirror config; every test here
    states the placement it depends on explicitly rather than inheriting one.

    ``source_kind`` attaches a ``sources`` row. It is opt-in because a plain
    manual ingest genuinely has none — ``ingest._upsert_source`` returns ``None``
    when there is no external id and no source metadata — which is exactly the
    vault-tier shape the LEFT JOIN has to survive.
    """
    conn.execute(
        "UPDATE documents SET vault_path = %s, draft = %s, "
        "sent_at = coalesce(%s, sent_at), "
        "ingested_at = coalesce(%s, ingested_at) WHERE id = %s",
        (vault_path, draft, sent_at, ingested_at, doc_id),
    )
    if source_kind is not None:
        conn.execute(
            "WITH new_source AS ("
            "  INSERT INTO sources (kind, external_id, metadata) "
            "  VALUES (%s, %s, '{}') RETURNING id"
            ") "
            "UPDATE documents SET source_id = (SELECT id FROM new_source) "
            "WHERE id = %s",
            (source_kind, f"ext-{doc_id}", doc_id),
        )
    return doc_id


def _mark_confidential(conn: psycopg.Connection[Any], doc_id: str) -> str:
    """Mark a seeded document confidential, and return its id.

    Written through the REAL constant rather than the literal ``'confidential'``
    so the test and ``_DISCOVERABLE`` cannot disagree about the spelling: if the
    vocabulary ever changes, both move together or this stops compiling. The
    column's vocabulary is itself pinned by migration 026's CHECK constraint.
    """
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )
    return doc_id


def _at(year: int, month: int, day: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, 9, 0, 0, tzinfo=datetime.UTC)


def _titles(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["title"]) for row in rows]


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["id"]) for row in rows]


# ---------------------------------------------------------------------------
# recent_documents — the ported predicates, one test each
# ---------------------------------------------------------------------------


def test_recent_documents_excludes_drafts(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """Drafts are quarantined from every public surface (P1.6)."""
    published = _export(
        test_db, seed_doc(title="Quarterly Planning", content="p"),
        "notes/quarterly-planning.md",
    )
    hidden = _export(
        test_db, seed_doc(title="Half-Written Draft", content="d"),
        "notes/half-written-draft.md", draft=True,
    )

    rows = recent_documents(test_db, limit=10)

    assert published in _ids(rows)
    assert hidden not in _ids(rows)


def test_recent_documents_excludes_confidential_documents(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The rail paints unprompted, so confidential titles stay off it.

    NOT parity with the vault tree, deliberately, and the difference is the
    whole argument: the tree is a surface the reader NAVIGATED to, while this
    rail appears on first paint before they have asked for anything. Identical
    content, different exposure, and the safe default on an unrequested surface
    is to omit it. Ruled 2026-08-14; the tree's own behaviour is a separate
    question and was deliberately left alone.

    THE PREMISE IS ASSERTED FIRST. ``ordinary`` is the same shape as ``secret``
    in every respect the query cares about — exported, published, dated, outside
    ``people/`` — differing only in ``sensitivity``. Without that assertion this
    test would pass just as well if the rail were empty, or if the seeding had
    silently failed, and would be measuring nothing at all.
    """
    ordinary = _export(
        test_db, seed_doc(title="Ordinary Note", content="o"),
        "notes/ordinary-note.md",
    )
    secret = _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed Note", content="s"),
            "notes/sealed-note.md",
        ),
    )

    rows = recent_documents(test_db, limit=10)

    assert ordinary in _ids(rows), (
        "the non-confidential twin is missing from the rail, so the exclusion "
        "below would hold even if the sensitivity predicate did nothing"
    )
    assert secret not in _ids(rows)
    # The title is the thing that leaks — the id tells a reader nothing.
    assert "Sealed Note" not in _titles(rows)


def test_recent_documents_INCLUDES_confidential_when_the_session_may_see_them(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The OTHER branch of the 2026-08-14 ruling, and its own test on purpose.

    The exclusion is not absolute: the routes pass
    ``exclude_confidential=not ctx.serve_confidential_bodies``, so a loopback
    session — which already reads these documents in search, in the note route
    and in the vault tree — sees them here too. An absolute would make this the
    only surface that silently understated its own corpus.

    A SEPARATE TEST RATHER THAN A PARAMETRISED ONE, per the ruling: with both
    branches in a single parametrised assertion, a bug that ignored the flag
    entirely would still satisfy whichever branch matched the hard-coded
    behaviour, and the parameter would look tested while carrying nothing.

    TWO MUTATIONS, MEASURED, AND THEY REDDEN OPPOSITE TESTS — which is what
    proves the flag is consulted rather than the behaviour hardcoded either way.
    Gate stuck CLOSED (`sql = _RECENT_SQL`) -> **1 failed, 42 passed**, THIS
    test. Gate stuck OPEN (`sql = _RECENT_SQL_ANY`) -> **1 failed, 42 passed**,
    ``test_recent_documents_excludes_confidential_documents`` instead.
    """
    ordinary = _export(
        test_db, seed_doc(title="Ordinary", content="o"), "notes/ordinary.md",
    )
    secret = _mark_confidential(
        test_db,
        _export(test_db, seed_doc(title="Sealed", content="s"), "notes/sealed.md"),
    )

    rows = recent_documents(test_db, limit=10, exclude_confidential=False)

    assert ordinary in _ids(rows)
    assert secret in _ids(rows), (
        "a session permitted to see confidential documents did not get one in "
        "the rail; the flag is being ignored in the permissive direction"
    )


def test_documents_for_tag_INCLUDES_confidential_when_the_session_may_see_them(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The tag click-through honours the same flag as the rail.

    Its own test because it is its own query: ``_TAG_DOCS_SQL`` and
    ``_RECENT_SQL`` are separate constants, so one could be gated and the other
    left absolute with every other test still green.
    """
    secret = _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed", content="s", tags=["shared"]),
            "notes/sealed.md",
        ),
    )

    strict = documents_for_tag(test_db, "shared", limit=10)
    permissive = documents_for_tag(test_db, "shared", limit=10, exclude_confidential=False)

    assert secret not in _ids(strict)
    assert secret in _ids(permissive)


def test_browseable_tag_counts_names_a_confidential_only_tag_when_permitted(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """THE CONSEQUENCE THE RULING ACCEPTED, asserted rather than left implicit.

    Gating makes the tag-NAME protection conditional too: on loopback a tag
    carried only by confidential documents reappears by name. That is the
    accepted behaviour — such a session reads those documents everywhere else —
    and it is asserted here so that a later reader who finds it surprising meets
    a test that says it is deliberate, rather than "fixing" it into a fourth
    rule.

    MUTATION, MEASURED: make ``browseable_tag_counts`` ignore its flag (always
    the strict SQL) -> **2 failed, 41 passed** — this test and the route-level
    twin that rides on it, which is the correct blast radius for a query the
    route delegates to.
    """
    _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed", content="s", tags=["sensitive-topic"]),
            "notes/sealed.md",
        ),
    )

    strict = {b["value"] for b in browseable_tag_counts(test_db)}
    permissive = {
        b["value"] for b in browseable_tag_counts(test_db, exclude_confidential=False)
    }

    assert "sensitive-topic" not in strict
    assert "sensitive-topic" in permissive, (
        "the ruling accepts that a confidential-only tag is named on loopback; "
        "it is not"
    )


def test_recent_documents_excludes_unexported_documents(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """No ``vault_path`` means nothing to open — the rail must not offer it.

    This fails only when **all three** ``vault_path`` predicates are dropped:
    the ``IS NOT NULL`` clause is redundant against the two path comparisons,
    which are NULL — and therefore exclusionary — for a NULL path. See the
    module docstring.
    """
    exported = _export(
        test_db, seed_doc(title="Exported Note", content="e"),
        "notes/exported-note.md",
    )
    unexported = _export(
        test_db, seed_doc(title="Unexported Note", content="u"), None
    )

    rows = recent_documents(test_db, limit=10)

    assert exported in _ids(rows)
    assert unexported not in _ids(rows)


def test_recent_documents_excludes_the_home_note(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """``index.md`` is the home note; the rail lives inside it.

    The wiki pipeline re-stamps its ``ingested_at`` on every regeneration, so
    without the predicate it would sit permanently at the top of its own rail.
    """
    real = _export(
        test_db, seed_doc(title="Vendor Shortlist", content="v"),
        "notes/vendor-shortlist.md",
    )
    home = _export(test_db, seed_doc(title="Home", content="h"), "index.md")

    rows = recent_documents(test_db, limit=10)

    assert real in _ids(rows)
    assert home not in _ids(rows)


def test_recent_documents_excludes_people_hub_pages(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """Everything under ``people/`` is a machine-generated People-Hub page.

    ``brain.people.emit_people_pages`` re-stamps every page it emits, so after a
    Krisp/Slack batch they would swamp the rail. Both the per-person roster page
    and the hub's own ``people/index.md`` are covered by the one prefix test.
    """
    genuine = _export(
        test_db, seed_doc(title="Genuine Note", content="g"),
        "notes/genuine-note.md",
    )
    roster = _export(
        test_db, seed_doc(title="Pat Roster", content="r"), "people/pat-roster.md"
    )
    hub_index = _export(
        test_db, seed_doc(title="People", content="i"), "people/index.md"
    )

    rows = recent_documents(test_db, limit=10)

    assert genuine in _ids(rows)
    assert roster not in _ids(rows)
    assert hub_index not in _ids(rows)


def test_ingested_at_is_not_null_so_its_predicate_is_vacuous(
    test_db: psycopg.Connection[Any],
) -> None:
    """Pin the constraint that makes the fifth ported predicate unfalsifiable.

    ``recent_documents`` carries ``ingested_at IS NOT NULL`` for parity with the
    wiki query, and no test can make it fire while the column is ``NOT NULL``.
    This test states *why* — and goes red if a migration ever relaxes it, which
    is precisely when the predicate stops being decorative.
    """
    row = test_db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'documents' AND column_name = 'ingested_at'"
    ).fetchone()

    assert row is not None, "documents.ingested_at is missing entirely"
    assert row[0] == "NO", (
        "documents.ingested_at is now nullable — the ingested_at predicate in "
        "brain.ui.queries.recent_documents is no longer vacuous and needs a "
        "real behavioural test"
    )


# ---------------------------------------------------------------------------
# recent_documents — ordering, ids, limit
# ---------------------------------------------------------------------------


def test_recent_documents_orders_by_event_date_not_ingest_time(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """Ranking is ``coalesce(doc_date, ingested_at)`` DESC.

    ``doc_date`` is the generated ``coalesce(sent_at, ingested_at)`` column
    (migration 021), so a meeting held last month but ingested today must rank
    by the meeting date. The seeds below invert ingest order against event
    order: sorting on ``ingested_at`` yields the opposite sequence.
    """
    older_event = _export(
        test_db, seed_doc(title="Older Event", content="o"),
        "notes/older-event.md",
        sent_at=_at(2026, 6, 1),
        ingested_at=_at(2026, 8, 13),  # ingested LAST
    )
    newer_event = _export(
        test_db, seed_doc(title="Newer Event", content="n"),
        "notes/newer-event.md",
        sent_at=_at(2026, 7, 20),
        ingested_at=_at(2026, 8, 1),  # ingested FIRST
    )
    no_event = _export(
        test_db, seed_doc(title="No Event Date", content="x"),
        "notes/no-event-date.md",
        ingested_at=_at(2026, 5, 1),  # falls back to ingested_at → oldest
    )

    rows = recent_documents(test_db, limit=10)

    assert _ids(rows) == [newer_event, older_event, no_event]


def test_recent_documents_returns_ids_and_display_fields(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The id is the point of the port — the UI navigates by id, not by path."""
    doc_id = _export(
        test_db, seed_doc(title="Vendor Evaluation", content="v"),
        "notes/vendor-evaluation.md",
        sent_at=_at(2026, 7, 4),
        source_kind="krisp",
    )

    rows = recent_documents(test_db, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == doc_id
    assert row["title"] == "Vendor Evaluation"
    assert row["vault_path"] == "notes/vendor-evaluation.md"
    assert row["source_kind"] == "krisp"
    assert str(row["date"]).startswith("2026-07-04")


def test_recent_documents_keeps_vault_notes_that_have_no_source_row(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The join to ``sources`` must be LEFT, or the vault tier disappears.

    A vault-tier note has no ``sources`` row at all (``_upsert_source`` returns
    ``None`` without an external id), so an INNER JOIN would silently drop every
    hand-authored note from the rail while ingested docs kept showing up.
    """
    authored = _export(
        test_db, seed_doc(title="Authored Note", content="a"),
        "notes/authored-note.md", sent_at=_at(2026, 7, 2),
    )
    ingested = _export(
        test_db, seed_doc(title="Ingested Note", content="i"),
        "_ingested/krisp/ingested-note.md", sent_at=_at(2026, 7, 1),
        source_kind="krisp",
    )

    rows = recent_documents(test_db, limit=10)

    assert _ids(rows) == [authored, ingested]
    assert [row["source_kind"] for row in rows] == [None, "krisp"]


def test_recent_documents_honours_the_limit(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """``limit`` caps the rail, and it caps it at the newest end."""
    for day in range(1, 6):
        _export(
            test_db, seed_doc(title=f"Note {day}", content=f"c{day}"),
            f"notes/note-{day}.md", sent_at=_at(2026, 7, day),
        )

    rows = recent_documents(test_db, limit=2)

    assert _titles(rows) == ["Note 5", "Note 4"]


@pytest.mark.parametrize("limit", [0, -1])
def test_recent_documents_rejects_a_non_positive_limit(
    test_db: psycopg.Connection[Any], limit: int
) -> None:
    """A zero/negative limit is a caller bug, not "return everything"."""
    with pytest.raises(ValueError):
        recent_documents(test_db, limit=limit)


@pytest.mark.parametrize("limit", [0, -1])
def test_documents_for_tag_rejects_a_non_positive_limit(
    test_db: psycopg.Connection[Any], limit: int
) -> None:
    """Same contract on the tag ledger — including for a tag that matches
    nothing, so the short-circuit cannot swallow a bad limit."""
    with pytest.raises(ValueError):
        documents_for_tag(test_db, "procurement", limit=limit)
    with pytest.raises(ValueError):
        documents_for_tag(test_db, "   ", limit=limit)


# ---------------------------------------------------------------------------
# documents_for_tag
# ---------------------------------------------------------------------------


def test_documents_for_tag_returns_only_documents_carrying_the_tag(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    tagged = _export(
        test_db, seed_doc(title="Tagged Note", content="t", tags=["procurement"]),
        "notes/tagged-note.md",
    )
    other = _export(
        test_db, seed_doc(title="Other Note", content="o", tags=["hiring"]),
        "notes/other-note.md",
    )

    rows = documents_for_tag(test_db, "procurement", limit=10)

    assert _ids(rows) == [tagged]
    assert other not in _ids(rows)


def test_documents_for_tag_normalizes_the_requested_tag(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """Tags are stored canonicalized, so the lookup canonicalizes too.

    Without this a link built from a display label (``Interview Prep``) silently
    returns nothing. The seed stores the canonical form deliberately: that is
    what every CLI/UI write boundary normalizes to via
    ``brain.tags.normalize_tags`` before it reaches ``ingest_document``, which
    itself stores whatever array it is handed.
    """
    doc_id = _export(
        test_db,
        seed_doc(title="Prep Notes", content="p", tags=["interview-prep"]),
        "notes/prep-notes.md",
    )

    assert _ids(documents_for_tag(test_db, "Interview Prep", limit=10)) == [doc_id]
    assert _ids(documents_for_tag(test_db, "interview_prep", limit=10)) == [doc_id]


def test_documents_for_tag_applies_the_discovery_predicates(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The tag ledger is a discovery surface: same corpus as the recent rail."""
    visible = _export(
        test_db, seed_doc(title="Visible", content="v", tags=["shared"]),
        "notes/visible.md",
    )
    draft = _export(
        test_db, seed_doc(title="Draft", content="d", tags=["shared"]),
        "notes/draft.md", draft=True,
    )
    person = _export(
        test_db, seed_doc(title="Roster", content="r", tags=["shared"]),
        "people/roster.md",
    )
    unexported = _export(
        test_db, seed_doc(title="Unexported", content="u", tags=["shared"]), None
    )

    rows = documents_for_tag(test_db, "shared", limit=10)

    assert _ids(rows) == [visible]
    for excluded in (draft, person, unexported):
        assert excluded not in _ids(rows)


def test_documents_for_tag_excludes_confidential_documents(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The tag click-through carries the same sensitivity predicate as the rail.

    Both compose ``_DISCOVERABLE``, so this and its recent-rail twin are two
    surfaces of ONE clause — which is exactly why the clause lives in the shared
    fragment. Asserted separately anyway: "the fragment is shared" is a claim
    about today's code, and this is a claim about the behaviour.
    """
    ordinary = _export(
        test_db, seed_doc(title="Ordinary Note", content="o", tags=["shared"]),
        "notes/ordinary-note.md",
    )
    secret = _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed Note", content="s", tags=["shared"]),
            "notes/sealed-note.md",
        ),
    )

    rows = documents_for_tag(test_db, "shared", limit=10)

    assert ordinary in _ids(rows), (
        "the non-confidential document with the same tag is missing, so the "
        "exclusion below would hold for the wrong reason"
    )
    assert secret not in _ids(rows)
    assert "Sealed Note" not in _titles(rows)


def test_documents_for_tag_orders_by_event_date_desc(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    older = _export(
        test_db, seed_doc(title="Older", content="o", tags=["shared"]),
        "notes/older.md", sent_at=_at(2026, 6, 1),
    )
    newer = _export(
        test_db, seed_doc(title="Newer", content="n", tags=["shared"]),
        "notes/newer.md", sent_at=_at(2026, 7, 1),
    )

    assert _ids(documents_for_tag(test_db, "shared", limit=10)) == [newer, older]


def test_documents_for_tag_short_circuits_on_an_empty_tag(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """A tag that canonicalizes to nothing matches nothing — not everything."""
    _export(
        test_db, seed_doc(title="Any Note", content="a", tags=["shared"]),
        "notes/any-note.md",
    )

    assert documents_for_tag(test_db, "   ", limit=10) == []
    assert documents_for_tag(test_db, "---", limit=10) == []


# ---------------------------------------------------------------------------
# tag_counts
# ---------------------------------------------------------------------------


def test_tag_counts_reports_a_document_count_per_tag(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The count `list_existing_tags` computes and discards (``count: null``)."""
    seed_doc(title="One", content="1", tags=["procurement", "hiring"])
    seed_doc(title="Two", content="2", tags=["procurement"])

    buckets = tag_counts(test_db)

    assert buckets == [
        {"value": "hiring", "count": 1},
        {"value": "procurement", "count": 2},
    ]


def test_tag_counts_counts_the_whole_corpus_like_list_existing_tags(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """``tag_counts`` is a drop-in for ``queries.list_existing_tags``.

    Same corpus (drafts and unexported rows included), same alpha sort, same
    ``min_doc_count`` — it only stops discarding the count. That deliberately
    makes it *wider* than :func:`documents_for_tag`, which is a browse surface
    and applies the discovery predicates; the facet count can therefore exceed
    the ledger's row count for a tag whose only other carriers are drafts. That
    divergence is inherited from ``list_existing_tags`` and from
    ``content_type_buckets``, both of which already count the whole corpus.
    """
    _export(
        test_db, seed_doc(title="A Draft", content="d", tags=["procurement"]),
        "notes/a-draft.md", draft=True,
    )
    _export(
        test_db, seed_doc(title="Published", content="p", tags=["procurement"]),
        "notes/published.md",
    )

    assert tag_counts(test_db) == [{"value": "procurement", "count": 2}]
    assert len(documents_for_tag(test_db, "procurement", limit=10)) == 1


def test_tag_counts_still_counts_confidential_documents(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The SEARCH-scoped answer keeps counting confidential documents.

    Not an oversight and not a leak at this layer: ``tag_counts`` backs
    ``/api/facets``, which annotates search — and search legitimately returns
    confidential documents (with their snippets redacted). A facet count that
    excluded them would understate its own result set.

    This is the CONTRAST test for
    ``test_browseable_tag_counts_hides_tags_carried_only_by_confidential_documents``
    below. Asserting only the browse-scoped behaviour would leave "the two
    scopes differ" as an untested claim, and a future change that filtered both
    would pass every other test in this file.
    """
    _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed", content="s", tags=["sensitive-topic"]),
            "notes/sealed.md",
        ),
    )

    assert tag_counts(test_db) == [{"value": "sensitive-topic", "count": 1}]


def test_browseable_tag_counts_hides_tags_carried_only_by_confidential_documents(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """A TAG NAME IS CONTENT — the idle rail must not name this one.

    A tag carried only by confidential documents would otherwise appear by name
    and with a count on a surface that paints before the reader asks for
    anything, while clicking it returned nothing. That is not merely a
    mismatch: an empty click-through advertises that something was withheld,
    which is more informative than the tag simply resolving.

    THE PREMISE IS ASSERTED. ``open-topic`` is carried by an ordinary document
    of the same shape and MUST still be counted, so the absence of
    ``sensitive-topic`` is evidence about the sensitivity predicate rather than
    about an empty corpus or a broken query.

    MUTATION, MEASURED: point ``browseable_tag_counts`` at ``_TAG_COUNTS_SQL``
    (i.e. undo the scope) — result recorded on the task; this test fails on the
    presence of ``sensitive-topic`` while the contrast test above stays green.
    """
    _export(
        test_db, seed_doc(title="Ordinary", content="o", tags=["open-topic"]),
        "notes/ordinary.md",
    )
    _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed", content="s", tags=["sensitive-topic"]),
            "notes/sealed.md",
        ),
    )

    buckets = browseable_tag_counts(test_db)
    values = [bucket["value"] for bucket in buckets]

    assert "open-topic" in values, (
        "the ordinary document's tag is missing, so the absence below would "
        "hold even if the sensitivity predicate did nothing"
    )
    assert "sensitive-topic" not in values, (
        f"a tag carried only by a confidential document is named on the idle "
        f"rail: {buckets}"
    )


def test_browseable_tag_counts_agrees_with_the_tag_page_it_annotates(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    """The count and its click-through describe the same corpus.

    This is the property #31.1 asked for and the label was standing in for. One
    tag on four documents — ordinary, draft, hub page, confidential — must count
    ONE, and ``documents_for_tag`` must return exactly that one document.

    Asserting both sides in one test is deliberate: the whole point is that the
    two numbers agree, and two tests asserting one number each would both pass
    while the numbers disagreed.
    """
    ordinary = _export(
        test_db, seed_doc(title="Ordinary", content="o", tags=["shared"]),
        "notes/ordinary.md",
    )
    _export(
        test_db, seed_doc(title="Draft", content="d", tags=["shared"]),
        "notes/draft.md", draft=True,
    )
    _export(
        test_db, seed_doc(title="Roster", content="r", tags=["shared"]),
        "people/roster.md",
    )
    _mark_confidential(
        test_db,
        _export(
            test_db, seed_doc(title="Sealed", content="s", tags=["shared"]),
            "notes/sealed.md",
        ),
    )

    counts = {b["value"]: b["count"] for b in browseable_tag_counts(test_db)}
    rows = documents_for_tag(test_db, "shared", limit=50)

    assert counts["shared"] == 1
    assert _ids(rows) == [ordinary]
    assert counts["shared"] == len(rows), (
        f"the index says {counts['shared']} and the tag page returns "
        f"{len(rows)} — the divergence this function exists to remove"
    )


def test_tag_counts_honours_min_doc_count(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    seed_doc(title="One", content="1", tags=["procurement", "hiring"])
    seed_doc(title="Two", content="2", tags=["procurement"])

    buckets = tag_counts(test_db, min_doc_count=2)

    assert buckets == [{"value": "procurement", "count": 2}]
