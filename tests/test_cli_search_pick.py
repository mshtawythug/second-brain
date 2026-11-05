"""``brain search --pick`` through the real CLI on the test database (RM-52 ACs 1-4).

The terminal probe, the version probe and the ``fzf`` runner are replaced here; the real ``fzf``
runs in ``tests/test_search_pick_real_fzf.py``. All documents are synthetic.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.engagement import engagement_stats
from brain.gaps import top_search_failures
from brain.ingest import Embedder
from brain.search_pick import NO_FZF_MESSAGE, NO_TERMINAL_MESSAGE, OLD_FZF_MESSAGE, FzfRun
from tests.entity_filter_seed import SHARED_TERM, seed_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

Runner = Callable[[list[str], str], FzfRun]


def _second_line(argv: list[str], stdin_text: str) -> FzfRun:
    return FzfRun(0, stdin_text.splitlines()[1] + "\n")


def _returns(returncode: int | None, stdout: str = "") -> Runner:
    return lambda _argv, _stdin: FzfRun(returncode, stdout)


@pytest.fixture
def corpus(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Embedder,
) -> list[str]:
    """Three notes the query matches, in a world with a terminal and a current ``fzf``."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    monkeypatch.setattr("brain.search_pick.has_terminal", lambda: True)
    monkeypatch.setattr("brain.search_pick.shutil.which", lambda _name: "/fake/fzf")
    monkeypatch.setattr("brain.search_pick.installed_fzf_version", lambda _binary: (0, 72))
    return [seed_document(test_db, fake_embedder, f"Survey note {n}") for n in range(3)]


def _use_runner(monkeypatch: pytest.MonkeyPatch, runner: Runner) -> None:
    monkeypatch.setattr("brain.search_pick.run_fzf", runner)


def _pick(*args: str) -> Any:
    return CliRunner().invoke(app, ["search", SHARED_TERM, "--fts-only", "--pick", *args])


def _search_rows(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT query, session_id, source, result_count FROM search_queries ORDER BY at"
    ).fetchall()


def _click_rows(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT document_id::text, query, session_id, source, agent_id FROM interactions "
        "WHERE action = 'clicked' ORDER BY at"
    ).fetchall()


# --- AC1: the id of the selected line is printed --------------------------------


def test_ac1_the_second_lines_id_is_printed_alone_on_stdout(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    offered: list[str] = []

    def runner(argv: list[str], stdin_text: str) -> FzfRun:
        offered.extend(line.split("\t")[0] for line in stdin_text.splitlines())
        return _second_line(argv, stdin_text)

    _use_runner(monkeypatch, runner)
    result = _pick()
    assert result.exit_code == 0, result.output
    assert set(offered) == set(corpus)
    assert result.stdout == f"{offered[1]}\n"


def test_the_real_argv_reaches_the_runner(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], stdin_text: str) -> FzfRun:
        seen.append(argv)
        return _second_line(argv, stdin_text)

    _use_runner(monkeypatch, runner)
    assert _pick().exit_code == 0
    argv = seen[0]
    assert argv[0] == "/fake/fzf"
    assert argv[argv.index("--preview") + 1] == "brain show {1}"
    assert not any(SHARED_TERM in arg for arg in argv), "the query must not show in ps"


# --- AC2: the search row and the click share the minted session --------------------


def test_ac2_search_row_and_click_share_one_minted_session(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    _use_runner(monkeypatch, _second_line)
    result = _pick("--agent", "synthetic-agent")
    picked = result.stdout.strip()
    [(query, search_session, search_source, count)] = _search_rows(test_db)
    [(doc, click_query, click_session, click_source, agent)] = _click_rows(test_db)
    assert search_session is not None
    assert uuid.UUID(str(search_session))
    assert click_session == search_session
    assert (query, click_query) == (SHARED_TERM, SHARED_TERM)
    assert (search_source, click_source) == ("cli", "cli")
    assert count == len(corpus)
    assert doc == picked
    assert agent == "synthetic-agent"


def test_each_pick_mints_its_own_session(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    _use_runner(monkeypatch, _second_line)
    assert _pick().exit_code == 0
    assert _pick().exit_code == 0
    assert len({row[1] for row in _search_rows(test_db)}) == 2


def test_the_click_counts_as_a_read_from_a_search_session(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    _use_runner(monkeypatch, _second_line)
    assert _pick().exit_code == 0
    stats = engagement_stats(test_db)
    assert (stats.clicks, stats.search_session_reads) == (1, 1)


# --- AC3: refusals, before any work --------------------------------------------


@pytest.mark.parametrize(
    ("seam", "value", "message"),
    [
        ("brain.search_pick.has_terminal", lambda: False, NO_TERMINAL_MESSAGE),
        ("brain.search_pick.shutil.which", lambda _name: None, NO_FZF_MESSAGE),
        ("brain.search_pick.installed_fzf_version", lambda _binary: (0, 1), OLD_FZF_MESSAGE),
    ],
    ids=["no-terminal", "no-fzf", "old-fzf"],
)
def test_ac3_a_refusal_exits_1_with_the_message_before_any_work(
    corpus: list[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    seam: str,
    value: Any,
    message: str,
) -> None:
    def must_not_build(_cfg: Any) -> Any:
        raise AssertionError("a refusal must not build the embedder")

    def must_not_run(_argv: list[str], _stdin: str) -> FzfRun:
        raise AssertionError("a refusal must not start fzf")

    monkeypatch.setattr(seam, value)
    monkeypatch.setattr("brain.cli._build_embedder", must_not_build)
    _use_runner(monkeypatch, must_not_run)
    result = _pick()
    assert result.exit_code == 1
    assert message in result.stderr
    assert result.stdout == ""
    assert _search_rows(test_db) == []


@pytest.mark.parametrize("flag", ["--json", "--facets"])
def test_pick_with_a_competing_output_flag_is_a_usage_error(
    corpus: list[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    flag: str,
) -> None:
    def must_not_run(_argv: list[str], _stdin: str) -> FzfRun:
        raise AssertionError("a usage error must not start fzf")

    _use_runner(monkeypatch, must_not_run)
    result = _pick(flag)
    assert result.exit_code == 2
    assert "--pick" in result.stderr
    assert _search_rows(test_db) == []


@pytest.mark.parametrize("flags", [("--no-meta",), ("--brief",), ("--meta",), ("-n", "2")])
def test_flags_that_do_not_compete_still_work(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    _use_runner(monkeypatch, _second_line)
    result = _pick(*flags)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() in corpus


# --- AC4: without --pick nothing changes ------------------------------------------


def test_ac4_without_pick_session_id_stays_null_and_fzf_never_runs(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    def must_not_run(_argv: list[str], _stdin: str) -> FzfRun:
        raise AssertionError("plain search must not start fzf")

    _use_runner(monkeypatch, must_not_run)
    result = CliRunner().invoke(app, ["search", SHARED_TERM, "--fts-only"])
    assert result.exit_code == 0, result.output
    assert "Search results" in result.stdout
    assert " matched" in result.stderr
    [(_query, session, source, _count)] = _search_rows(test_db)
    assert (session, source) == (None, "cli")
    assert _click_rows(test_db) == []


def test_pick_prints_neither_the_table_nor_the_footer(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_runner(monkeypatch, _second_line)
    result = _pick()
    assert "Search results" not in result.output
    assert " matched" not in result.output


# --- D3: `brain gaps` -------------------------------------------------------------


def _no_click(conn: psycopg.Connection[Any]) -> list[tuple[str, int]]:
    return [
        (f.query, f.count)
        for f in top_search_failures(conn, since_days=7, limit=10)
        if f.kind == "no_click"
    ]


def test_d3_an_aborted_pick_counts_as_no_click(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    _use_runner(monkeypatch, _returns(130))
    assert _pick().exit_code == 130
    assert _no_click(test_db) == [(SHARED_TERM, 1)]


def test_d3_a_completed_pick_does_not_count_as_no_click(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    _use_runner(monkeypatch, _second_line)
    assert _pick().exit_code == 0
    assert _no_click(test_db) == []


def test_d3_plain_cli_searches_never_count_as_no_click(
    corpus: list[str], test_db: psycopg.Connection[Any]
) -> None:
    for _ in range(3):
        result = CliRunner().invoke(app, ["search", SHARED_TERM, "--fts-only"])
        assert result.exit_code == 0, result.output
    assert len(_search_rows(test_db)) == 3
    assert _no_click(test_db) == []


# --- Nothing picked ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("runner", "exit_code", "message"),
    [
        (_returns(130), 130, ""),
        (_returns(1), 1, "nothing picked"),
        (_returns(2), 1, "fzf failed (exit 2)"),
        (_returns(None), 1, "fzf could not start"),
        (_returns(0, "unoffered\tT\tmanual\n"), 1, "fzf returned a line that was not offered"),
        (_returns(0, ""), 1, "fzf returned a line that was not offered"),
    ],
    ids=["abort", "no-match", "fzf-failed", "cannot-start", "unoffered", "empty"],
)
def test_nothing_picked_prints_no_id_and_records_no_click(
    corpus: list[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    runner: Runner,
    exit_code: int,
    message: str,
) -> None:
    _use_runner(monkeypatch, runner)
    result = _pick()
    assert result.exit_code == exit_code
    assert result.stdout == ""
    assert message in result.stderr
    assert _click_rows(test_db) == []
    assert len(_search_rows(test_db)) == 1, "the search itself is still logged"


def test_d2_no_results_is_exit_1_on_stderr_and_fzf_does_not_start(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    def must_not_run(_argv: list[str], _stdin: str) -> FzfRun:
        raise AssertionError("no results must not start fzf")

    _use_runner(monkeypatch, must_not_run)
    result = CliRunner().invoke(
        app, ["search", "zzqxnomatchterm", "--fts-only", "--pick"]
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "(no results)" in result.stderr
    [(_query, session, _source, count)] = _search_rows(test_db)
    assert count == 0
    assert session is not None


# --- The wait holds no connection ---------------------------------------------------


def test_no_connection_from_the_search_is_held_while_a_human_chooses(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    count_sql = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    )
    observer = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    try:
        row = observer.execute(count_sql).fetchone()
        assert row is not None
        before = row[0]
        during: list[int] = []
        rows_seen: list[int] = []

        def runner(argv: list[str], stdin_text: str) -> FzfRun:
            found = observer.execute(count_sql).fetchone()
            logged = observer.execute("SELECT count(*) FROM search_queries").fetchone()
            assert found is not None and logged is not None
            during.append(found[0])
            rows_seen.append(logged[0])
            return _second_line(argv, stdin_text)

        _use_runner(monkeypatch, runner)
        assert _pick().exit_code == 0
    finally:
        observer.close()
    assert during == [before]
    assert rows_seen == [1], "the search row is committed before fzf starts"


# --- A failed click write never loses the id ----------------------------------------


@pytest.mark.parametrize(
    "seam",
    ["brain.search_pick.record_interaction", "brain.search_pick.connect"],
    ids=["write-fails", "connect-fails"],
)
def test_a_failed_click_write_still_prints_the_id_and_exits_0(
    corpus: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    seam: str,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.OperationalError("SENTINEL-DETAIL")

    _use_runner(monkeypatch, _second_line)
    monkeypatch.setattr(seam, boom)
    with caplog.at_level(logging.WARNING, logger="brain.search_pick"):
        result = _pick()
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() in corpus
    assert "OperationalError" in caplog.text
    assert "SENTINEL-DETAIL" not in caplog.text
    assert SHARED_TERM not in caplog.text


def test_a_document_deleted_during_the_wait_still_prints_the_id(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    def runner(argv: list[str], stdin_text: str) -> FzfRun:
        run = _second_line(argv, stdin_text)
        test_db.execute("DELETE FROM documents WHERE id = %s", (run.stdout.split("\t")[0],))
        test_db.commit()
        return run

    _use_runner(monkeypatch, runner)
    result = _pick()
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() in corpus
    assert _click_rows(test_db) == []


# --- Confidential: the same list as plain search ---------------------------------------


def test_pick_offers_exactly_what_plain_search_returns_for_the_same_filter(
    corpus: list[str], monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection[Any]
) -> None:
    secret = corpus[0]
    test_db.execute("UPDATE documents SET sensitivity = 'confidential' WHERE id = %s", (secret,))
    test_db.commit()
    offered: list[str] = []

    def runner(argv: list[str], stdin_text: str) -> FzfRun:
        offered.extend(line.split("\t")[0] for line in stdin_text.splitlines())
        return _second_line(argv, stdin_text)

    _use_runner(monkeypatch, runner)
    assert _pick("--sensitivity", "normal").exit_code == 0
    assert secret not in offered
    assert set(offered) == set(corpus[1:])

    offered.clear()
    assert _pick().exit_code == 0
    assert set(offered) == set(corpus), "no filter returns both tiers, as plain search does"
