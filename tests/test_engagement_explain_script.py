"""``scripts/engagement_explain.sql``: the owner's read-only timing of the engagement statement.

RM-22b, ruling D2: before the stage is turned on, the owner runs this script on
their own data and needs p95 <= 20 ms. Two halves. Without a database: the
script's statement is the module's ``_ENGAGEMENT_SQL`` (named parameters as
``$1..$5``), so a change to one without the other fails here, and the script
can write nothing. On the 5434 test database: it runs, prints its timing and a
plan, and leaves every row where it was. All data is synthetic.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.engagement import READ_ACTIONS
from brain.search_engagement import (
    _ENGAGEMENT_SQL,
    ENGAGEMENT_HALF_LIFE_DAYS,
    ENGAGEMENT_WINDOW_DAYS,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "engagement_explain.sql"
PLACEHOLDERS = {
    "%(ids)s": "$1",
    "%(reads)s": "$2",
    "%(session)s": "$3",
    "%(half_life_s)s": "$4",
    "%(window_days)s": "$5",
}
WRITES = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|COMMIT)\b"
)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def _script_statement() -> str:
    match = re.search(r"\$q\$(.*?)\$q\$", SCRIPT.read_text(), re.S)
    assert match is not None, "the script holds its statement between $q$ quotes"
    return match.group(1)


def _code(text: str) -> str:
    """The script without its ``--`` comments, where the instructions name SQL words."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


@pytest.mark.nodb
def test_the_script_times_exactly_the_modules_statement() -> None:
    expected = _ENGAGEMENT_SQL
    for named, positional in PLACEHOLDERS.items():
        expected = expected.replace(named, positional)

    assert _normalized(_script_statement()) == _normalized(expected)


def _constant(name: str) -> str:
    """The right-hand side of the script's ``<name> CONSTANT <type> := <value>;``."""
    match = re.search(rf"^\s*{name}\s+CONSTANT\s+\S+\s*:=\s*(.+?);\s*$", SCRIPT.read_text(), re.M)
    assert match is not None, f"the script declares {name} as a CONSTANT"
    return match.group(1)


def _product(expression: str) -> int:
    """``30 * 86400`` -> 2592000: the only arithmetic the script's constants use."""
    assert re.fullmatch(r"\d+(\s*\*\s*\d+)*", expression), expression
    result = 1
    for factor in expression.split("*"):
        result *= int(factor)
    return result


@pytest.mark.nodb
def test_the_script_binds_the_modules_parameters() -> None:
    # The statement text is pinned above; these are the values it is run with,
    # which the module passes from its own constants (``fetch_engagement``).
    reads = re.findall(r"'([^']*)'", _constant("reads"))

    assert reads == list(READ_ACTIONS)
    assert _product(_constant("half_life_s")) == ENGAGEMENT_HALF_LIFE_DAYS * 86400
    assert _product(_constant("window_days")) == ENGAGEMENT_WINDOW_DAYS


@pytest.mark.nodb
def test_the_script_runs_read_only_and_writes_nothing() -> None:
    code = _code(SCRIPT.read_text())

    assert "BEGIN READ ONLY;" in code
    assert code.rstrip().endswith("ROLLBACK;")
    assert WRITES.findall(code) == []


def test_the_script_prints_its_timing_and_plan_and_changes_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Synthetic timing', 'body', 'note', 'vault', %s) RETURNING id::text",
        (f"timing-{uuid.uuid4().hex}",),
    ).fetchone()[0]
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source) VALUES (%s, 'opened', 'cli')",
        (doc,),
    )
    notices: list[str] = []
    test_db.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))

    test_db.cursor().execute(SCRIPT.read_text())

    assert any(re.search(r"p95=[\d.]+ ms", n) for n in notices), notices
    assert any("GroupAggregate" in n or "HashAggregate" in n for n in notices), notices
    assert test_db.execute("SELECT count(*) FROM interactions").fetchone() == (1,)
    assert test_db.execute("SELECT count(*) FROM documents").fetchone() == (1,)


def test_the_script_says_so_when_there_is_nothing_to_time(
    test_db: psycopg.Connection[Any],
) -> None:
    notices: list[str] = []
    test_db.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))

    test_db.cursor().execute(SCRIPT.read_text())

    assert any("nothing to time" in n for n in notices), notices
