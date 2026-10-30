"""``brain dedup`` (``brain.cli_dedup``), through the real CLI (RM-19).

Real-DB tests on the 5434 test database; every document is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli, near_dup
from brain.cli_registry import EXTENSION_REGISTRARS
from tests.near_dup_fakes import body, insert_doc, near_copy, pairs

runner = CliRunner()


def _run(*args: str) -> Any:
    return runner.invoke(cli.app, ["dedup", *args])


def _seed_pair(conn: psycopg.Connection[Any], seed: int = 1, **kwargs: Any) -> tuple[str, str]:
    text = body(400, seed)
    return (
        insert_doc(conn, text, title=f"Copy one {seed}", **kwargs),
        insert_doc(conn, near_copy(text), title=f"Copy two {seed}"),
    )


def test_dedup_is_registered_through_the_extension_seam() -> None:
    assert "dedup" in [name for name, _ in EXTENSION_REGISTRARS]
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "dedup" in result.stdout


def test_scan_reports_what_it_did(test_db: psycopg.Connection[Any]) -> None:
    _seed_pair(test_db)
    insert_doc(test_db, body(30, 9), title="Stub")

    result = _run("scan")

    assert result.exit_code == 0, result.output
    assert "Examined 3 documents (1 under 50 words); 1 new pair, 0 dropped." in result.stdout


def test_scan_limit_says_how_many_remain(test_db: psycopg.Connection[Any]) -> None:
    for seed in range(3):
        insert_doc(test_db, body(80, 300 + seed))

    result = _run("scan", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert "2 documents left for the next scan" in result.stdout


def test_scan_all_recomputes(test_db: psycopg.Connection[Any]) -> None:
    _seed_pair(test_db)
    _run("scan")

    result = _run("scan", "--all")

    assert "Examined 2 documents" in result.stdout


def test_scan_refuses_a_zero_limit(test_db: psycopg.Connection[Any]) -> None:
    result = _run("scan", "--limit", "0")

    assert result.exit_code != 0


def test_list_shows_both_titles_and_ids(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    _run("scan")

    result = _run("list")

    assert result.exit_code == 0, result.output
    assert "Copy one 1" in result.stdout and "Copy two 1" in result.stdout
    assert first[:8] in result.stdout and second[:8] in result.stdout


def test_list_is_permissive_at_the_terminal(test_db: psycopg.Connection[Any]) -> None:
    # The terminal is inside the F6 boundary: a confidential pair is listed.
    _seed_pair(test_db, sensitivity="confidential")
    _run("scan")

    payload = json.loads(_run("list", "--json").stdout)

    assert len(payload) == 1


def test_list_json_shape(test_db: psycopg.Connection[Any]) -> None:
    _seed_pair(test_db)
    _run("scan")

    [row] = json.loads(_run("list", "--json").stdout)

    assert set(row) == {"doc_a", "doc_b", "similarity", "status", "detected_at"}
    assert row["status"] == "open"
    assert row["similarity"] >= near_dup.JACCARD_CUT


def test_list_empty_says_so(test_db: psycopg.Connection[Any]) -> None:
    result = _run("list")

    assert result.exit_code == 0
    assert "No open pairs" in result.stdout


def test_list_refuses_an_unknown_status(test_db: psycopg.Connection[Any]) -> None:
    result = _run("list", "--status", "merged")

    assert result.exit_code != 0


def test_dismiss_takes_prefixes_in_either_order(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    _run("scan")

    result = _run("dismiss", second[:8], first[:8])

    assert result.exit_code == 0, result.output
    assert pairs(test_db) == [(*near_dup.ordered_pair(first, second), "dismissed")]
    assert "dismissed" in result.stdout
    assert json.loads(_run("list", "--status", "dismissed", "--json").stdout)


def test_confirm_labels_the_pair(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    _run("scan")

    result = _run("confirm", first, second)

    assert result.exit_code == 0, result.output
    assert pairs(test_db)[0][2] == "confirmed"


def test_dismiss_an_unrecorded_pair_is_a_clean_exit_1(test_db: psycopg.Connection[Any]) -> None:
    a = insert_doc(test_db, body(100, 1))
    b = insert_doc(test_db, body(100, 2))

    result = _run("dismiss", a, b)

    assert result.exit_code == 1
    assert "no recorded pair" in result.output


def test_dismiss_a_bad_prefix_is_a_clean_exit_1(test_db: psycopg.Connection[Any]) -> None:
    result = _run("dismiss", "zzzzzzzz", "abcdef12")

    assert result.exit_code == 1


def test_dismiss_a_document_with_itself_is_a_clean_exit_1(
    test_db: psycopg.Connection[Any],
) -> None:
    a = insert_doc(test_db, body(100, 1))

    result = _run("dismiss", a, a)

    assert result.exit_code == 1


@pytest.mark.fresh_schema
def test_without_migration_030_dedup_says_run_init(test_db: psycopg.Connection[Any]) -> None:
    test_db.execute("DROP TABLE near_duplicates")
    test_db.execute("DROP TABLE document_minhash")

    for args in (("scan",), ("list",), ("dismiss", "abcdef12", "abcdef34")):
        result = _run(*args)
        assert result.exit_code == 1, args
        assert "brain init" in result.output


def test_a_bad_config_is_a_clean_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_BRIEF_SINCE_HOURS", "0")

    result = _run("list")

    assert result.exit_code == 1
    assert "Configuration error" in result.output
