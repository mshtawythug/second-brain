"""``brain watch`` (``brain.cli_watch``): the saved-search sub-app, through the real CLI (RM-15).

Real-DB tests on the 5434 test database. The embedder is injected at ``brain.cli._build_embedder``
(the CLI's patch point) only where a semantic watch needs one. All data is synthetic.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli
from tests.saved_search_fakes import DownEmbedder, WordEmbedder, age_watch, ingest

runner = CliRunner()


def _run(*args: str) -> Any:
    return runner.invoke(cli.app, ["watch", *args])


def _never(cfg: object) -> object:
    raise AssertionError("a lexical watch must not build an embedder")


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.cli._build_embedder", _never)


def test_watch_is_listed_in_brain_help() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "watch" in result.stdout


def test_add_seeds_and_an_immediate_check_reports_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    ingest(test_db, WordEmbedder(), title="Zephyr notes", content="zephyr notes")

    added = _run("add", "zephyr", "zephyr")
    checked = _run("check", "zephyr", "--json")

    assert added.exit_code == 0, added.output
    assert "Added watch 'zephyr' (lexical); 1 existing match" in added.stdout
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout) == [
        {"watch": "zephyr", "mode": "lexical", "skipped": None, "peek": False, "new": []}
    ]


def test_check_reports_a_new_document_once_and_peek_records_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    _run("add", "zephyr", "zephyr")
    doc = ingest(test_db, WordEmbedder(), title="Zephyr launch", content="zephyr launch")

    peeked = _run("check", "--peek", "--json")
    first = _run("check")
    second = _run("check", "--json")

    assert [n["id"] for n in json.loads(peeked.stdout)[0]["new"]] == [doc]
    assert json.loads(peeked.stdout)[0]["peek"] is True
    assert "zephyr (lexical): 1 new" in first.stdout
    assert "Zephyr launch" in first.stdout
    assert json.loads(second.stdout)[0]["new"] == []


def test_add_validates_filters_like_search(test_db: psycopg.Connection[Any]) -> None:
    bad_source = _run("add", "w", "q", "--source", "fax")
    bad_person = _run("add", "w", "q", "--person", "Nobody Synthetic")

    assert bad_source.exit_code == 2
    assert "--source" in bad_source.output
    assert bad_person.exit_code == 2
    assert "--person" in bad_person.output
    assert json.loads(_run("list", "--json").stdout) == []


def test_add_refuses_a_duplicate_name(test_db: psycopg.Connection[Any]) -> None:
    _run("add", "w", "q")

    again = _run("add", "w", "other")

    assert again.exit_code == 1
    assert "already exists" in again.output


def test_list_shows_filters_mode_and_state(test_db: psycopg.Connection[Any]) -> None:
    _run("add", "w", "quarterly plan", "--source", "krisp", "--tag", "Road Map", "--kind", "note")
    _run("pause", "w")

    listed = json.loads(_run("list", "--json").stdout)
    table = _run("list")

    assert len(listed) == 1
    row = listed[0]
    assert (row["name"], row["query"], row["mode"], row["enabled"]) == (
        "w",
        "quarterly plan",
        "lexical",
        False,
    )
    assert row["filters"] == {"source": "krisp", "tag": "road-map", "kind": "note"}
    assert table.exit_code == 0 and "quarterly plan" in table.stdout


def test_pause_resume_and_rm(test_db: psycopg.Connection[Any]) -> None:
    _run("add", "w", "zephyr")

    paused = _run("pause", "w")
    unnamed = json.loads(_run("check", "--json").stdout)
    resumed = _run("resume", "w")
    removed = _run("rm", "w")
    missing = _run("rm", "w")

    assert paused.exit_code == 0 and "Paused watch 'w'" in paused.stdout
    assert unnamed == []
    assert resumed.exit_code == 0 and "Resumed watch 'w'" in resumed.stdout
    assert removed.exit_code == 0 and "Removed watch 'w'" in removed.stdout
    assert missing.exit_code == 1 and "no watch named 'w'" in missing.output


def test_hits_lists_recent_hits_notified_or_not(test_db: psycopg.Connection[Any]) -> None:
    _run("add", "zephyr", "zephyr")
    doc = ingest(test_db, WordEmbedder(), title="Zephyr launch", content="zephyr launch")
    _run("check")
    test_db.execute("UPDATE saved_search_hits SET notified_at = NOW()")

    as_json = json.loads(_run("hits", "zephyr", "--json").stdout)
    text = _run("hits", "--since", "7d")
    none_yet = json.loads(_run("hits", "zephyr", "--since", "0h", "--json").stdout)

    assert [(h["watch"], h["id"], h["notified"]) for h in as_json] == [("zephyr", doc, True)]
    assert "Zephyr launch" in text.stdout
    assert none_yet == []


def test_semantic_add_uses_the_cli_embedder_and_refuses_when_it_is_down(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    words = WordEmbedder()
    ingest(test_db, words, title="Orbital", content="orbital mechanics primer")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: DownEmbedder())

    refused = _run("add", "orbit", "orbital mechanics", "--semantic")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: words)
    added = _run("add", "orbit", "orbital mechanics", "--semantic")

    assert refused.exit_code == 1
    assert "embedder unavailable" in refused.output
    assert added.exit_code == 0, added.output
    assert "(semantic)" in added.stdout


def test_a_semantic_check_with_the_embedder_down_says_skipped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: WordEmbedder())
    _run("add", "orbit", "orbital", "--semantic")
    age_watch(test_db, "orbit", hours=3)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: DownEmbedder())

    result = _run("check", "orbit")

    assert result.exit_code == 0
    assert "orbit (semantic): skipped, embedder unavailable" in result.stdout


def test_unknown_name_is_exit_1(test_db: psycopg.Connection[Any]) -> None:
    for args in (("check", "nope"), ("pause", "nope"), ("resume", "nope"), ("hits", "nope")):
        result = _run(*args)
        assert result.exit_code == 1, args
        assert "no watch named 'nope'" in result.output


@pytest.mark.fresh_schema
def test_without_migration_029_watch_says_run_init(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    test_db.execute("DROP TABLE saved_search_hits")
    test_db.execute("DROP TABLE saved_searches")

    result = _run("list")

    assert result.exit_code == 1
    assert "brain init" in result.output


def test_empty_states_say_so(test_db: psycopg.Connection[Any]) -> None:
    listed = _run("list")
    checked = _run("check")
    no_hits = _run("hits")

    assert "No watches" in listed.stdout
    assert "No enabled watches" in checked.stdout
    assert "No hits in that window" in no_hits.stdout


def test_a_bad_config_is_a_clean_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_BRIEF_SINCE_HOURS", "0")

    result = _run("list")

    assert result.exit_code == 1
    assert "Configuration error" in result.output
