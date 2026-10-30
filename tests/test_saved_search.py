"""``brain.saved_search``: watch definitions and the hit reads the brief uses (RM-15).

Real-DB tests against the 5434 test database. Hits are inserted directly here; the matching
that records them is ``tests/test_saved_search_check.py``. All data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.saved_search import (
    MODE_LEXICAL,
    MODE_SEMANTIC,
    PUBLISHED_CAP,
    SavedSearchError,
    SavedSearchExists,
    SavedSearchNotFound,
    WatchFilters,
    WatchNote,
    create_watch,
    get_watch,
    hits_since,
    list_watches,
    mark_notified,
    remove_watch,
    set_enabled,
    unnotified_view,
    validate_name,
    watch_tables_present,
)
from tests.saved_search_fakes import WordEmbedder, ingest


def _hit(
    conn: psycopg.Connection[Any],
    watch_id: str,
    document_id: str,
    *,
    hours_ago: int = 0,
    notified: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO saved_search_hits (saved_search_id, document_id, first_seen_at, "
        "notified_at) VALUES (%s, %s, NOW() - make_interval(hours => %s), "
        "CASE WHEN %s THEN NOW() END)",
        (watch_id, document_id, hours_ago, notified),
    )


def _docs(conn: psycopg.Connection[Any], *titles: str, confidential: bool = False) -> list[str]:
    embedder = WordEmbedder()
    return [
        ingest(
            conn,
            embedder,
            title=title,
            content=f"{title} body",
            sensitivity="confidential" if confidential else None,
        )
        for title in titles
    ]


# --- definitions -----------------------------------------------------------


def test_create_and_get_round_trip_the_filters(test_db: psycopg.Connection[Any]) -> None:
    filters = WatchFilters(source="krisp", tag="roadmap", kind="note", without_tag="draft")

    created = create_watch(
        test_db, name="Roadmap", query="quarterly plan", filters=filters, mode=MODE_SEMANTIC
    )
    fetched = get_watch(test_db, "Roadmap")

    assert fetched == created
    assert fetched.filters == filters
    assert (fetched.mode, fetched.enabled, fetched.query) == (
        MODE_SEMANTIC,
        True,
        "quarterly plan",
    )


def test_filters_store_only_the_keys_that_are_set(test_db: psycopg.Connection[Any]) -> None:
    create_watch(
        test_db, name="w", query="q", filters=WatchFilters(person="Pat Example"), mode=MODE_LEXICAL
    )

    stored = test_db.execute("SELECT filters FROM saved_searches").fetchone()[0]

    assert stored == {"person": "Pat Example"}


def test_a_second_watch_with_the_same_name_is_refused(test_db: psycopg.Connection[Any]) -> None:
    create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)

    with pytest.raises(SavedSearchExists, match="'w'"):
        create_watch(test_db, name="w", query="other", filters=WatchFilters(), mode=MODE_LEXICAL)


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "tab\there", "x" * 81])
def test_bad_names_are_refused(bad: str) -> None:
    with pytest.raises(SavedSearchError):
        validate_name(bad)


def test_names_are_stripped() -> None:
    assert validate_name("  Roadmap  ") == "Roadmap"


def test_a_blank_query_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(SavedSearchError, match="query"):
        create_watch(test_db, name="w", query="  ", filters=WatchFilters(), mode=MODE_LEXICAL)


def test_an_unknown_mode_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(SavedSearchError, match="mode"):
        create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode="fuzzy")


def test_list_is_by_name_and_can_skip_paused(test_db: psycopg.Connection[Any]) -> None:
    for name in ("b", "a", "c"):
        create_watch(test_db, name=name, query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    set_enabled(test_db, "b", enabled=False)

    assert [w.name for w in list_watches(test_db)] == ["a", "b", "c"]
    assert [w.name for w in list_watches(test_db, enabled_only=True)] == ["a", "c"]


def test_pause_and_resume(test_db: psycopg.Connection[Any]) -> None:
    create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)

    paused = set_enabled(test_db, "w", enabled=False)
    resumed = set_enabled(test_db, "w", enabled=True)

    assert (paused.enabled, resumed.enabled) == (False, True)


def test_remove_deletes_the_watch_and_its_hits(test_db: psycopg.Connection[Any]) -> None:
    watch = create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    (doc,) = _docs(test_db, "Alpha")
    _hit(test_db, watch.id, doc)

    remove_watch(test_db, "w")

    assert list_watches(test_db) == []
    assert test_db.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0] == 0


@pytest.mark.parametrize(
    "act",
    [
        lambda c: get_watch(c, "nope"),
        lambda c: remove_watch(c, "nope"),
        lambda c: set_enabled(c, "nope", enabled=False),
    ],
)
def test_an_unknown_name_is_not_found(test_db: psycopg.Connection[Any], act: Any) -> None:
    with pytest.raises(SavedSearchNotFound, match="'nope'"):
        act(test_db)


def test_tables_present_after_migration(test_db: psycopg.Connection[Any]) -> None:
    assert watch_tables_present(test_db) is True


@pytest.mark.fresh_schema
def test_tables_absent_before_migration_029(test_db: psycopg.Connection[Any]) -> None:
    test_db.execute("DROP TABLE saved_search_hits")
    test_db.execute("DROP TABLE saved_searches")

    assert watch_tables_present(test_db) is False


# --- the terminal set: hits by first_seen_at ---------------------------------


def test_hits_since_keeps_the_window_notified_or_not(test_db: psycopg.Connection[Any]) -> None:
    watch = create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    fresh, notified, old = _docs(test_db, "Fresh", "Notified", "Old")
    _hit(test_db, watch.id, fresh)
    _hit(test_db, watch.id, notified, notified=True)
    _hit(test_db, watch.id, old, hours_ago=30)

    hits = hits_since(test_db, hours=24)

    assert {h.title for h in hits} == {"Fresh", "Notified"}
    assert {h.title: h.notified for h in hits} == {"Fresh": False, "Notified": True}
    assert {h.watch for h in hits} == {"w"}


def test_hits_since_applies_the_lens_and_the_name(test_db: psycopg.Connection[Any]) -> None:
    a = create_watch(test_db, name="a", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    b = create_watch(test_db, name="b", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    (open_doc,) = _docs(test_db, "Open")
    (sealed,) = _docs(test_db, "Sealed", confidential=True)
    _hit(test_db, a.id, open_doc)
    _hit(test_db, a.id, sealed)
    _hit(test_db, b.id, open_doc)

    permissive = hits_since(test_db, hours=24)
    gated = hits_since(test_db, hours=24, exclude_confidential=True)
    only_a = hits_since(test_db, hours=24, name="a")

    assert sorted((h.watch, h.title) for h in permissive) == [
        ("a", "Open"),
        ("a", "Sealed"),
        ("b", "Open"),
    ]
    assert sorted((h.watch, h.title) for h in gated) == [("a", "Open"), ("b", "Open")]
    assert sorted(h.title for h in only_a) == ["Open", "Sealed"]


# --- the published set: unnotified, capped, gated ---------------------------


def test_unnotified_view_caps_each_watch_and_counts_the_rest(
    test_db: psycopg.Connection[Any],
) -> None:
    watch = create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    docs = _docs(test_db, *[f"Doc {i}" for i in range(PUBLISHED_CAP + 3)])
    for i, doc in enumerate(docs):
        _hit(test_db, watch.id, doc, hours_ago=i)

    view = unnotified_view(test_db)

    assert PUBLISHED_CAP == 5
    assert [h.title for h in view.hits] == [f"Doc {i}" for i in range(PUBLISHED_CAP)]
    assert view.notes == [WatchNote(watch="w", more=3)]
    assert len(view.pending) == PUBLISHED_CAP + 3


def test_unnotified_view_withholds_confidential_but_still_marks_them(
    test_db: psycopg.Connection[Any],
) -> None:
    watch = create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    (open_doc,) = _docs(test_db, "Open")
    (sealed,) = _docs(test_db, "Sealed", confidential=True)
    (seen,) = _docs(test_db, "Seen before")
    _hit(test_db, watch.id, open_doc)
    _hit(test_db, watch.id, sealed)
    _hit(test_db, watch.id, seen, notified=True)

    view = unnotified_view(test_db)

    assert [h.title for h in view.hits] == ["Open"]
    assert view.notes == []
    assert view.pending == frozenset({(watch.id, open_doc), (watch.id, sealed)})


def test_mark_notified_marks_exactly_the_pending_keys(test_db: psycopg.Connection[Any]) -> None:
    watch = create_watch(test_db, name="w", query="q", filters=WatchFilters(), mode=MODE_LEXICAL)
    shown, later = _docs(test_db, "Shown", "Later")
    _hit(test_db, watch.id, shown)
    pending = unnotified_view(test_db).pending
    _hit(test_db, watch.id, later)  # arrives after the page was built

    marked = mark_notified(test_db, pending)

    assert marked == 1
    assert [h.title for h in unnotified_view(test_db).hits] == ["Later"]


def test_mark_notified_with_nothing_pending_is_a_no_op(test_db: psycopg.Connection[Any]) -> None:
    assert mark_notified(test_db, frozenset()) == 0
