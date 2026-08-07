"""``brain usage --json`` withholds raw query strings by default (F7).

A query log is often more revealing than the documents it found: it records
what someone was *looking for*, including the searches that returned nothing.
So the machine-readable surface — the one that gets piped, pasted and handed
to agents — emits normalized labels unless the caller explicitly opts in.

The assertions serialize the **whole payload** and search it, rather than
checking the field we expect the query to live in. A field-level check only
covers the leak we already imagined; this catches one added later.

Human output is deliberately NOT redacted: it is a local terminal, inside the
trust boundary, and a user asking "what do I search for most" needs the actual
strings to act on the answer.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.gaps import record_search_query
from brain.usage import build_usage_report

#: A distinctive phrase that can only reach the output via the raw query.
PRIVATE_QUERY = "quokkavolt compensation grievance"


def _usage_command_registered() -> bool:
    """Is ``brain usage`` reachable from the CLI yet?

    The command body, its registrar and ``cli_registry.REGISTRARS`` are all on
    disk, but ``cli.py`` — which is coordinator-owned — invokes each registrar
    directly, so the command is unreachable until two lines land there (patch
    in ``docs/handoff/2026-07-26-w2b.md`` §A.1b).

    Checked at runtime rather than hard-coded so these tests **start running
    by themselves** the moment the patch lands. A static ``xfail`` would have
    to be remembered and removed; this cannot go stale.

    THE NAME FALLBACK IS LOAD-BEARING. ``cli_usage.register`` does
    ``app.command()(usage)`` with no explicit name, so Typer leaves
    ``CommandInfo.name`` as ``None`` and resolves the command name from the
    callback at build time. Matching on ``c.name`` alone was therefore
    permanently ``False`` — the patch it waits for HAD landed
    (``cli.py`` calls ``register_usage(app)``, and ``brain usage --help``
    works), but these four tests kept skipping, so the privacy contract they
    guard had ZERO executed coverage while the file presented as covered.
    Note the paragraph above claims this predicate "cannot go stale"; it had.
    ``tests/test_cli_smoke.py`` and ``tests/test_graphrag_cli_mcp_parity.py``
    both already handled the ``None`` case — this module was the outlier.
    """
    return any(
        (c.name or (c.callback.__name__.replace("_", "-") if c.callback else None))
        == "usage"
        for c in app.registered_commands
    )


requires_usage_command = pytest.mark.skipif(
    not _usage_command_registered(),
    reason=(
        "brain usage not registered in cli.py yet — coordinator patch, "
        "docs/handoff/2026-07-26-w2b.md §A.1b. Library-layer coverage above "
        "is unaffected."
    ),
)


@pytest.fixture
def logged_query(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import TEST_DATABASE_URL

    record_search_query(
        test_db,
        query=PRIVATE_QUERY,
        result_count=0,
        session_id=None,
        source="cli",
        fts_count=0,
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


def _fixture_is_not_vacuous(test_db: psycopg.Connection[Any]) -> None:
    """The query really is stored — otherwise every assertion below is empty."""
    row = test_db.execute(
        "SELECT count(*) FROM search_queries WHERE query = %s", (PRIVATE_QUERY,)
    ).fetchone()
    assert row is not None and row[0] == 1, (
        "the private query must be logged, or these tests pass vacuously"
    )


# ---------------------------------------------------------------------------
# The library projection
# ---------------------------------------------------------------------------


def test_to_dict_withholds_raw_queries_by_default(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    _fixture_is_not_vacuous(test_db)

    payload = build_usage_report(test_db, days=30).to_dict()

    assert PRIVATE_QUERY not in json.dumps(payload)


def test_to_dict_includes_them_when_asked(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    _fixture_is_not_vacuous(test_db)

    payload = build_usage_report(test_db, days=30).to_dict(raw_queries=True)

    assert PRIVATE_QUERY in json.dumps(payload)


def test_the_default_is_the_safe_one(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    """Structural: a signature change flipping the default is caught here.

    ``to_dict()`` and ``to_dict(raw_queries=False)`` must be identical — if
    someone changes the default, the redacted call keeps working while the
    bare call silently starts leaking.
    """
    _fixture_is_not_vacuous(test_db)
    report = build_usage_report(test_db, days=30)

    assert report.to_dict() == report.to_dict(raw_queries=False)


def test_counts_are_identical_either_way(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    """Only the label changes — redaction must not distort the numbers."""
    _fixture_is_not_vacuous(test_db)
    report = build_usage_report(test_db, days=30)

    redacted = report.to_dict()
    raw = report.to_dict(raw_queries=True)

    assert [q["count"] for q in redacted["top_queries"]] == [
        q["count"] for q in raw["top_queries"]
    ]
    assert redacted["totals"] == raw["totals"]


def test_a_canonical_label_is_still_emitted(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    """Redaction must not blank the row — the count needs something to label."""
    _fixture_is_not_vacuous(test_db)

    payload = build_usage_report(test_db, days=30).to_dict()

    assert payload["top_queries"]
    assert payload["top_queries"][0]["query"], "a label is still required"


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


@requires_usage_command
def test_cli_json_withholds_raw_queries_by_default(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    _fixture_is_not_vacuous(test_db)

    result = CliRunner().invoke(app, ["usage", "--json"])

    assert result.exit_code == 0, result.output
    assert PRIVATE_QUERY not in result.stdout


@requires_usage_command
def test_cli_raw_queries_flag_opts_in(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    _fixture_is_not_vacuous(test_db)

    result = CliRunner().invoke(app, ["usage", "--json", "--raw-queries"])

    assert result.exit_code == 0, result.output
    assert PRIVATE_QUERY in result.stdout


@requires_usage_command
def test_cli_json_is_parseable(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    result = CliRunner().invoke(app, ["usage", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "days",
        "totals",
        "daily",
        "by_surface",
        "by_agent",
        "top_queries",
        "ingested_by_source",
    }


@requires_usage_command
def test_human_output_is_not_redacted(
    test_db: psycopg.Connection[Any], logged_query: None
) -> None:
    """The terminal is inside the trust boundary.

    A user asking "what do I search for most" needs the real strings; showing
    them normalized labels would make the answer unactionable.
    """
    _fixture_is_not_vacuous(test_db)

    result = CliRunner().invoke(app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "quokkavolt" in result.stdout
