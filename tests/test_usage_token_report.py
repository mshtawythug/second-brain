"""``brain usage``'s token-cost rollups — measured vs counterfactual (Wave 5).

The failure mode these tests exist to prevent is a single blended headline
number: "this brain saves N% of tokens". That claim is only computable over
calls that HAD a cheaper mode, and most calls do not. So the report carries two
clauses with two denominators, and the word "counterfactual" on the second —
tested literally, because the wording is the deliverable.

The other pinned decision is ``None``-not-``0``. A brain where nothing used a
cheap mode has NO savings figure; reporting ``0`` would claim it measured one
and found nothing.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
from typer.testing import CliRunner

from brain.cli import app
from brain.cli_usage import _tokens_line
from brain.gaps import record_search_query
from brain.usage import UsageReport, UsageTotals, build_usage_report


def _log(
    conn: psycopg.Connection[Any],
    query: str,
    *,
    payload_tokens: int | None = None,
    baseline_tokens: int | None = None,
) -> None:
    record_search_query(
        conn,
        query=query,
        result_count=2,
        session_id=None,
        source="cli",
        payload_tokens=payload_tokens,
        baseline_tokens=baseline_tokens,
    )


def _totals(**kwargs: Any) -> UsageTotals:
    """A ``UsageTotals`` with only the token fields varied."""
    base: dict[str, Any] = {
        "searches": 10,
        "sessions": 0,
        "opens": 0,
        "feedback": 0,
        "documents_ingested": 0,
        "zero_result": 0,
        "duration_p50_ms": None,
        "duration_p95_ms": None,
    }
    return UsageTotals(**{**base, **kwargs})


def _report(totals: UsageTotals) -> UsageReport:
    return UsageReport(
        days=30,
        totals=totals,
        daily=[],
        by_surface=[],
        by_agent=[],
        top_queries=[],
        ingested_by_source=[],
    )


# ---------------------------------------------------------------------------
# The SQL rollup
# ---------------------------------------------------------------------------


def test_totals_sum_only_measured_rows(
    test_db: psycopg.Connection[Any],
) -> None:
    """Unmeasured rows contribute nothing and are excluded from the denominator."""
    # Arrange — two measured calls, one that measured nothing.
    _log(test_db, "measured one", payload_tokens=100)
    _log(test_db, "measured two", payload_tokens=250)
    _log(test_db, "human table call")

    # Act
    totals = build_usage_report(test_db, days=1).totals

    # Assert
    assert totals.payload_tokens_total == 350
    assert totals.measured_calls == 2
    assert totals.searches == 3, "the unmeasured call is still a search"


def test_totals_are_none_not_zero_when_nothing_was_measured(
    test_db: psycopg.Connection[Any],
) -> None:
    """A window with no priced call has no cost figure — ``0`` would be a claim."""
    _log(test_db, "unmeasured call")

    totals = build_usage_report(test_db, days=1).totals

    assert totals.payload_tokens_total is None
    assert totals.measured_calls == 0


def test_counterfactual_savings_is_none_when_no_brief_calls(
    test_db: psycopg.Connection[Any],
) -> None:
    """PINS None-NOT-ZERO — nothing had an alternative, so there is no figure."""
    # Arrange — measured, but no cheaper mode was ever in effect.
    _log(test_db, "measured only", payload_tokens=100)

    # Act
    totals = build_usage_report(test_db, days=1).totals

    # Assert
    assert totals.counterfactual_calls == 0
    assert totals.counterfactual_savings_tokens is None
    assert totals.counterfactual_savings_rate is None


def test_counterfactual_baseline_excludes_rows_missing_payload_tokens(
    test_db: psycopg.Connection[Any],
) -> None:
    """The baseline sum is scoped to rows where the difference is computable.

    Enforced twice over: the write path refuses a baseline without a payload,
    and the aggregate's FILTER carries the ``payload_tokens IS NOT NULL``
    conjunct anyway. The row is inserted here in raw SQL precisely to prove the
    SQL half stands on its own — a pre-028 backfill, a hand edit or a future
    writer could reintroduce the shape the Python gate rejects.
    """
    # Arrange — one honest counterfactual pair...
    _log(test_db, "brief call", payload_tokens=400, baseline_tokens=1000)
    # ...and one orphaned baseline the aggregate must ignore.
    test_db.execute(
        "INSERT INTO search_queries "
        "(query, result_count, source, baseline_tokens) "
        "VALUES ('orphan baseline row', 1, 'cli', 9999)"
    )

    # Act
    totals = build_usage_report(test_db, days=1).totals

    # Assert
    assert totals.counterfactual_calls == 1
    assert totals.baseline_tokens_total == 1000, "9999 must not be summed in"
    assert totals.counterfactual_payload_tokens == 400
    assert totals.counterfactual_savings_tokens == 600


def test_savings_are_differenced_over_one_population_not_two(
    test_db: psycopg.Connection[Any],
) -> None:
    """The savings figure must not mix denominators.

    ``payload_tokens_total`` spans EVERY measured call; the baseline spans only
    the counterfactual ones. Subtracting the first from the second differences
    two different row sets — here it would yield ``1000 - 5400 = -4400`` and
    report a brain that "saves" a negative number of tokens. The correct answer
    uses the payload summed over the same rows as the baseline.
    """
    # Arrange — one cheap brief call, plus expensive non-brief traffic.
    _log(test_db, "the brief call", payload_tokens=400, baseline_tokens=1000)
    _log(test_db, "expensive call one", payload_tokens=2500)
    _log(test_db, "expensive call two", payload_tokens=2500)

    # Act
    totals = build_usage_report(test_db, days=1).totals

    # Assert
    assert totals.payload_tokens_total == 5400
    assert totals.counterfactual_savings_tokens == 600
    assert totals.counterfactual_savings_tokens > 0


def test_usage_json_carries_both_totals_separately(
    test_db: psycopg.Connection[Any],
) -> None:
    """Machine consumers get the parts, not a pre-blended percentage."""
    # Arrange
    _log(test_db, "brief call", payload_tokens=400, baseline_tokens=1000)
    _log(test_db, "plain call", payload_tokens=600)

    # Act
    payload = build_usage_report(test_db, days=1).to_dict()["totals"]

    # Assert
    assert payload["payload_tokens_total"] == 1000
    assert payload["measured_calls"] == 2
    assert payload["baseline_tokens_total"] == 1000
    assert payload["counterfactual_payload_tokens"] == 400
    assert payload["counterfactual_calls"] == 1
    assert payload["counterfactual_savings_tokens"] == 600
    # No blended rate is emitted: a consumer must choose its own population.
    assert "savings_rate" not in payload


# ---------------------------------------------------------------------------
# Rendering — the wording is the deliverable
# ---------------------------------------------------------------------------


def test_usage_render_labels_counterfactual_explicitly() -> None:
    """Blunt, and exactly the point: the literal word must appear.

    The failure this guards against is a headline that reads "saved 61,880
    tokens (−29.4%)" with no signal that the comparison is against a call that
    never ran.
    """
    line = _tokens_line(
        _report(
            _totals(
                searches=517,
                payload_tokens_total=148_203,
                measured_calls=412,
                baseline_tokens_total=210_476,
                counterfactual_payload_tokens=148_596,
                counterfactual_calls=210,
            )
        )
    )

    assert line is not None
    assert "counterfactual" in line
    assert "measured" in line
    # Two clauses, two denominators — both must be visible on the line.
    assert "412 of 517 calls" in line
    assert "over 210 brief calls" in line


def test_render_reports_no_token_line_when_nothing_was_measured() -> None:
    """"tokens served 0" would read as "retrieval was free". Say nothing."""
    assert _tokens_line(_report(_totals())) is None


def test_render_omits_the_savings_clause_without_a_counterfactual() -> None:
    """Measured cost still reports; the second clause simply is not there."""
    line = _tokens_line(
        _report(
            _totals(searches=4, payload_tokens_total=8_000, measured_calls=4)
        )
    )

    assert line is not None
    assert "tokens served 8,000" in line
    assert "counterfactual" not in line


def test_render_signs_a_more_expensive_mode_as_an_increase() -> None:
    """A cheap mode that turned out dearer must not be laundered into a saving.

    ``+10.0%``, not ``10.0%`` and certainly not ``−10.0%``.
    """
    line = _tokens_line(
        _report(
            _totals(
                searches=1,
                payload_tokens_total=1_100,
                measured_calls=1,
                baseline_tokens_total=1_000,
                counterfactual_payload_tokens=1_100,
                counterfactual_calls=1,
            )
        )
    )

    assert line is not None
    assert "+10.0%" in line
    assert "−10.0%" not in line
    # And the negative saving itself carries the same U+2212 minus, not an
    # ASCII hyphen — one glyph per clause.
    assert "counterfactual savings −100 " in line
    assert "savings -100" not in line


def test_cli_prints_the_token_line(
    test_db: psycopg.Connection[Any],
) -> None:
    """End-to-end through the command, not just the helper.

    ``DATABASE_URL`` is already pointed at the test DB session-wide by the
    conftest fixture, so the command reaches the same rows written here.
    """
    # Arrange
    _log(test_db, "brief call", payload_tokens=400, baseline_tokens=1000)

    # Act
    result = CliRunner().invoke(app, ["usage", "--days", "1"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "tokens served 400" in result.stdout
    assert "counterfactual savings 600" in result.stdout
