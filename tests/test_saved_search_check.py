"""``brain.saved_search_check``: seeding, checking and peeking a watch (RM-15).

Real-DB tests against the 5434 test database, through the real ``hybrid_search``. The race test
opens two more connections and holds one check inside its search until the other is seen
waiting on the row lock. All data is synthetic.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import psycopg
import pytest

from brain.errors import PersonNotFound
from brain.saved_search import (
    MODE_LEXICAL,
    MODE_SEMANTIC,
    SavedSearchError,
    SavedSearchNotFound,
    WatchFilters,
    get_watch,
    set_enabled,
)
from brain.saved_search_check import (
    SKIP_EMBEDDER,
    SKIP_PERSON,
    Matcher,
    add_watch,
    check_enabled,
    check_watch,
    peek_enabled,
    peek_watch,
)
from brain.search import hybrid_search
from brain.source_kinds import InvalidSourceKind
from tests.saved_search_fakes import (
    DownEmbedder,
    WordEmbedder,
    age_document,
    age_watch,
    edit,
    ingest,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _matcher(embedder: Any = None, **kwargs: Any) -> Matcher:
    words = embedder if embedder is not None else WordEmbedder()
    return Matcher(
        embedder=lambda: words, vector_sim_floor=0.25, min_cosine=0.45, **kwargs
    )


def _never_embedder() -> Any:
    raise AssertionError("a lexical check must not build an embedder")


def _lexical_matcher(**kwargs: Any) -> Matcher:
    return Matcher(embedder=_never_embedder, vector_sim_floor=0.25, min_cosine=0.45, **kwargs)


def _last_checked(conn: psycopg.Connection[Any], name: str) -> Any:
    return conn.execute(
        "SELECT last_checked_at FROM saved_searches WHERE name = %s", (name,)
    ).fetchone()[0]


def _add(conn: psycopg.Connection[Any], name: str, query: str, **kwargs: Any) -> int:
    mode = kwargs.pop("mode", MODE_LEXICAL)
    matcher = kwargs.pop("matcher", None) or _lexical_matcher()
    _, seeded = add_watch(
        conn, name=name, query=query, filters=WatchFilters(**kwargs), mode=mode, matcher=matcher
    )
    return seeded


# --- AC2: add seeds; an immediate check reports nothing ----------------------


def test_add_seeds_existing_matches_and_an_immediate_check_reports_none(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    old = ingest(test_db, words, title="Zephyr budget", content="zephyr budget review")
    age_document(test_db, old, hours=72)
    ingest(test_db, words, title="Zephyr notes", content="zephyr notes from today")
    ingest(test_db, words, title="Unrelated", content="nothing to see")

    seeded = _add(test_db, "zephyr", "zephyr")
    result = check_watch(test_db, "zephyr", _lexical_matcher())

    assert seeded == 2
    assert result.matches == [] and result.skipped is None
    notified = test_db.execute(
        "SELECT count(*) FROM saved_search_hits WHERE notified_at IS NOT NULL"
    ).fetchone()[0]
    assert notified == 2


# --- AC3: a new matching document is reported once, and only once -----------


def test_a_new_document_is_reported_once(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    new = ingest(test_db, words, title="Zephyr launch", content="zephyr launch plan")

    first = check_watch(test_db, "zephyr", _lexical_matcher())
    second = check_watch(test_db, "zephyr", _lexical_matcher())

    assert [m.document_id for m in first.matches] == [new]
    assert first.matches[0].title == "Zephyr launch"
    assert second.matches == []
    row = test_db.execute("SELECT notified_at FROM saved_search_hits").fetchone()
    assert row == (None,)


# --- AC4: an edited old document is reported; an old match never again ------


def test_an_edited_old_document_that_newly_matches_is_reported(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    doc = ingest(test_db, words, title="Garden log", content="tomatoes and basil")
    age_document(test_db, doc, hours=72)
    _add(test_db, "zephyr", "zephyr")
    age_watch(test_db, "zephyr", hours=24)
    edit(test_db, words, doc, "tomatoes basil and zephyr seeds")

    result = check_watch(test_db, "zephyr", _lexical_matcher())

    assert [m.document_id for m in result.matches] == [doc]


def test_an_old_match_edited_again_is_never_re_reported(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    doc = ingest(test_db, words, title="Zephyr log", content="zephyr first entry")
    age_document(test_db, doc, hours=72)
    _add(test_db, "zephyr", "zephyr")
    edit(test_db, words, doc, "zephyr first entry and a second")

    result = check_watch(test_db, "zephyr", _lexical_matcher())

    assert result.matches == []


def test_an_old_untouched_match_outside_the_window_is_not_a_hit(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    test_db.execute("DELETE FROM saved_search_hits")
    old = ingest(test_db, words, title="Zephyr archive", content="zephyr archive")
    age_document(test_db, old, hours=72)

    result = check_watch(test_db, "zephyr", _lexical_matcher())

    assert result.matches == []


# --- AC5: filters apply ------------------------------------------------------


def test_a_source_mismatch_is_not_a_hit(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr-krisp", "zephyr", source="krisp")
    manual = ingest(test_db, words, title="Zephyr manual", content="zephyr manual note")
    krisp = ingest(
        test_db, words, title="Zephyr call", content="zephyr call notes", source_kind="krisp"
    )

    result = check_watch(test_db, "zephyr-krisp", _lexical_matcher())

    assert [m.document_id for m in result.matches] == [krisp]
    assert manual not in {m.document_id for m in result.matches}


def test_add_validates_the_source_and_person_and_normalises_tags(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(InvalidSourceKind):
        _add(test_db, "bad-source", "q", source="fax")
    with pytest.raises(PersonNotFound):
        _add(test_db, "bad-person", "q", person="Nobody Synthetic")

    _add(test_db, "tagged", "q", tag="Road Map", without_tag="Old_Stuff")

    assert get_watch(test_db, "tagged").filters == WatchFilters(
        tag="road-map", without_tag="old-stuff"
    )


def test_a_person_that_no_longer_resolves_skips_the_check(
    test_db: psycopg.Connection[Any],
) -> None:
    _add(test_db, "w", "zephyr")
    test_db.execute("""UPDATE saved_searches SET filters = '{"person": "Gone Person"}'""")
    before = _last_checked(test_db, "w")

    result = check_watch(test_db, "w", _lexical_matcher())

    assert result.skipped == SKIP_PERSON
    assert _last_checked(test_db, "w") == before


# --- semantic mode -----------------------------------------------------------


def test_semantic_keeps_close_matches_only(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "orbit", "orbital mechanics", mode=MODE_SEMANTIC, matcher=_matcher(words))
    close = ingest(test_db, words, title="Orbital", content="orbital mechanics primer")
    ingest(test_db, words, title="Far", content="orbital cooking recipes for lunch today")

    result = check_watch(test_db, "orbit", _matcher(words))

    assert [m.document_id for m in result.matches] == [close]


def test_a_semantic_check_that_falls_back_records_nothing_and_keeps_its_window(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    _add(test_db, "orbit", "orbital mechanics", mode=MODE_SEMANTIC, matcher=_matcher(words))
    age_watch(test_db, "orbit", hours=5)
    before = _last_checked(test_db, "orbit")
    ingest(test_db, words, title="Orbital", content="orbital mechanics primer")

    result = check_watch(test_db, "orbit", _matcher(DownEmbedder()))

    assert result.skipped == SKIP_EMBEDDER and result.matches == []
    assert _last_checked(test_db, "orbit") == before
    assert test_db.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0] == 0


def test_a_semantic_watch_under_the_none_embedder_is_skipped(
    test_db: psycopg.Connection[Any],
) -> None:
    from brain.embeddings import NullEmbedder

    words = WordEmbedder()
    _add(test_db, "orbit", "orbital mechanics", mode=MODE_SEMANTIC, matcher=_matcher(words))

    result = check_watch(test_db, "orbit", _matcher(NullEmbedder()))

    assert result.skipped == SKIP_EMBEDDER


def test_a_semantic_add_that_cannot_seed_is_refused_and_leaves_no_watch(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(SavedSearchError, match="embedder"):
        _add(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_matcher(DownEmbedder()))

    with pytest.raises(SavedSearchNotFound):
        get_watch(test_db, "orbit")


# --- AC10 (core half): lexical works with the embedder down ------------------


def test_lexical_checks_never_build_an_embedder(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    new = ingest(test_db, words, title="Zephyr", content="zephyr update")

    result = check_watch(test_db, "zephyr", _lexical_matcher())

    assert [m.document_id for m in result.matches] == [new]


# --- monotonic last_checked_at ----------------------------------------------


def test_last_checked_at_never_moves_backwards(test_db: psycopg.Connection[Any]) -> None:
    _add(test_db, "w", "zephyr")
    test_db.execute(
        "UPDATE saved_searches SET last_checked_at = NOW() + INTERVAL '1 day' WHERE name = 'w'"
    )
    ahead = _last_checked(test_db, "w")

    check_watch(test_db, "w", _lexical_matcher())

    assert _last_checked(test_db, "w") == ahead


def test_a_check_advances_last_checked_at_to_its_start(test_db: psycopg.Connection[Any]) -> None:
    _add(test_db, "w", "zephyr")
    age_watch(test_db, "w", hours=5)

    result = check_watch(test_db, "w", _lexical_matcher())

    assert result.started_at is not None
    assert _last_checked(test_db, "w") == result.started_at


# --- peek: reports, never records -------------------------------------------


def test_peek_reports_without_recording(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    age_watch(test_db, "zephyr", hours=5)
    before = _last_checked(test_db, "zephyr")
    new = ingest(test_db, words, title="Zephyr", content="zephyr peek")

    peeked = peek_watch(test_db, "zephyr", _lexical_matcher())
    checked = check_watch(test_db, "zephyr", _lexical_matcher())

    assert [m.document_id for m in peeked.matches] == [new]
    assert peeked.started_at is None
    assert [m.document_id for m in checked.matches] == [new]
    assert before != _last_checked(test_db, "zephyr")


def test_peek_with_the_lens_hides_confidential_matches(
    test_db: psycopg.Connection[Any],
) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    ingest(test_db, words, title="Zephyr open", content="zephyr open")
    ingest(
        test_db, words, title="Zephyr sealed", content="zephyr sealed", sensitivity="confidential"
    )

    gated = peek_watch(test_db, "zephyr", _lexical_matcher(), sensitivity="normal")
    permissive = peek_watch(test_db, "zephyr", _lexical_matcher())

    assert [m.title for m in gated.matches] == ["Zephyr open"]
    assert sorted(m.title for m in permissive.matches) == ["Zephyr open", "Zephyr sealed"]


def test_check_enabled_skips_paused_watches(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "on", "zephyr")
    _add(test_db, "off", "zephyr")
    set_enabled(test_db, "off", enabled=False)
    ingest(test_db, words, title="Zephyr", content="zephyr")

    results = check_enabled(test_db, _lexical_matcher())
    peeks = peek_enabled(test_db, _lexical_matcher())

    assert [r.watch for r in results] == ["on"]
    assert [r.watch for r in peeks] == ["on"]
    assert len(results[0].matches) == 1


def test_a_named_check_of_a_paused_watch_runs(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "off", "zephyr")
    set_enabled(test_db, "off", enabled=False)
    ingest(test_db, words, title="Zephyr", content="zephyr")

    assert len(check_watch(test_db, "off", _lexical_matcher()).matches) == 1


# --- AC8: two concurrent checks report each hit once -------------------------


def _waiting_on_a_lock(observer: psycopg.Connection[Any], pid: int) -> bool:
    row = observer.execute(
        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
    ).fetchone()
    return row is not None and row[0] == "Lock"


def test_two_concurrent_checks_report_each_hit_once(test_db: psycopg.Connection[Any]) -> None:
    words = WordEmbedder()
    _add(test_db, "zephyr", "zephyr")
    new = {
        ingest(test_db, words, title=f"Zephyr {i}", content=f"zephyr item {i}") for i in range(3)
    }
    conn_a = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    conn_b = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    observer = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    a_in_search = threading.Event()
    b_pid = conn_b.info.backend_pid
    results: dict[str, Any] = {}

    def held_search(*args: Any, **kwargs: Any) -> Any:
        # A holds the row lock here; wait (bounded) until B is seen blocked on it.
        a_in_search.set()
        deadline = time.monotonic() + 10
        while not _waiting_on_a_lock(observer, b_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        results["b_was_blocked"] = _waiting_on_a_lock(observer, b_pid)
        return hybrid_search(*args, **kwargs)

    def run_a() -> None:
        results["a"] = check_watch(conn_a, "zephyr", _lexical_matcher(search_fn=held_search))

    try:
        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        assert a_in_search.wait(10)
        results["b"] = check_watch(conn_b, "zephyr", _lexical_matcher())
        thread_a.join(10)
    finally:
        for conn in (conn_a, conn_b, observer):
            conn.close()

    reported_a = [m.document_id for m in results["a"].matches]
    reported_b = [m.document_id for m in results["b"].matches]
    assert results["b_was_blocked"] is True
    assert sorted(reported_a + reported_b) == sorted(new)
    assert results["b"].started_at > results["a"].started_at
    assert _last_checked(test_db, "zephyr") == results["b"].started_at


# --- the matcher, and the paths a brief must survive -------------------------


def test_build_matcher_reads_the_knob_and_builds_the_embedder_at_most_once() -> None:
    from brain.config import Config
    from brain.saved_search_check import build_matcher

    built: list[object] = []
    words = WordEmbedder()

    def factory() -> Any:
        built.append(words)
        return words

    matcher = build_matcher(
        Config(database_url=TEST_DATABASE_URL, vector_sim_floor=0.3),
        embedder=factory,
        env={"BRAIN_WATCH_MIN_COSINE": "0.7"},
    )

    assert (matcher.min_cosine, matcher.vector_sim_floor) == (0.7, 0.3)
    assert built == []
    assert matcher.embedder() is words and matcher.embedder() is words
    assert len(built) == 1


def test_build_matcher_defaults_to_the_configured_embedder(
    test_db: psycopg.Connection[Any],
) -> None:
    from brain.config import Config
    from brain.saved_search_check import build_matcher

    words = WordEmbedder()
    _add(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_matcher(words))
    matcher = build_matcher(Config(database_url=TEST_DATABASE_URL, embedder="none"), env={})

    result = check_watch(test_db, "orbit", matcher)

    assert result.skipped == SKIP_EMBEDDER


def test_an_embedder_that_cannot_be_built_skips_the_semantic_check(
    test_db: psycopg.Connection[Any],
) -> None:
    from brain.config import ConfigError

    def refuse() -> Any:
        raise ConfigError("synthetic bad embedder configuration")

    _add(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_matcher())

    result = check_watch(
        test_db, "orbit", Matcher(embedder=refuse, vector_sim_floor=0.25, min_cosine=0.45)
    )

    assert result.skipped == SKIP_EMBEDDER and result.matches == []


def test_a_semantic_peek_with_the_embedder_down_says_skipped(
    test_db: psycopg.Connection[Any],
) -> None:
    _add(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_matcher())

    result = peek_watch(test_db, "orbit", _matcher(DownEmbedder()))

    assert result.skipped == SKIP_EMBEDDER and result.started_at is None


def test_check_enabled_passes_over_a_watch_removed_meanwhile(
    test_db: psycopg.Connection[Any],
) -> None:
    _add(test_db, "a", "zephyr")
    _add(test_db, "b", "zephyr")
    remover = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)

    def search_then_remove_b(*args: Any, **kwargs: Any) -> Any:
        remover.execute("DELETE FROM saved_searches WHERE name = 'b'")
        return hybrid_search(*args, **kwargs)

    try:
        results = check_enabled(test_db, _lexical_matcher(search_fn=search_then_remove_b))
    finally:
        remover.close()

    assert [r.watch for r in results] == ["a"]


def test_seeded_matches_are_not_hits_in_the_terminal_set(
    test_db: psycopg.Connection[Any],
) -> None:
    """Regression (found in the RM-15 e2e run): ``add`` seeds the current matches as already
    seen, and they then showed up in the terminal brief and ``brain watch hits`` as new hits."""
    from brain.saved_search import hits_since

    words = WordEmbedder()
    ingest(test_db, words, title="Zephyr old", content="zephyr old")
    _add(test_db, "zephyr", "zephyr")
    new = ingest(test_db, words, title="Zephyr new", content="zephyr new")
    check_watch(test_db, "zephyr", _lexical_matcher())
    test_db.execute("UPDATE saved_search_hits SET notified_at = NOW() WHERE notified_at IS NULL")

    hits = hits_since(test_db, hours=24)

    assert [h.document_id for h in hits] == [new]
    assert test_db.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0] == 2
