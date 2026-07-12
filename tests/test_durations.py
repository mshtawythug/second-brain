"""Unit tests for the shared ``--since`` duration parser (Task 5.2)."""
from datetime import timedelta

import pytest
import typer

from brain.durations import parse_since, since_window, whole_units

# ---------------------------------------------------------------------------
# parse_since — grammar + bare-unit interpretation
# ---------------------------------------------------------------------------


def test_parse_since_bare_number_uses_days_unit() -> None:
    assert parse_since("7", bare_unit="days") == timedelta(days=7)


def test_parse_since_bare_number_uses_hours_unit() -> None:
    assert parse_since("7", bare_unit="hours") == timedelta(hours=7)


def test_parse_since_day_suffix_is_unit_independent() -> None:
    # A suffix always wins regardless of the command's bare unit.
    assert parse_since("7d", bare_unit="hours") == timedelta(days=7)


def test_parse_since_hour_suffix() -> None:
    assert parse_since("24h", bare_unit="days") == timedelta(hours=24)


def test_parse_since_minute_suffix() -> None:
    assert parse_since("90m", bare_unit="days") == timedelta(minutes=90)


def test_parse_since_strips_surrounding_whitespace() -> None:
    assert parse_since("  7d ", bare_unit="days") == timedelta(days=7)


def test_parse_since_allows_zero() -> None:
    # Bare 0 is preserved (callers map it to "no window"/"use config").
    assert parse_since("0", bare_unit="days") == timedelta(0)


@pytest.mark.parametrize("bad", ["", "d7", "-3d", "-3", "7x", "7dd", "d", "abc", "7.5"])
def test_parse_since_rejects_malformed_values(bad: str) -> None:
    with pytest.raises(typer.BadParameter):
        parse_since(bad, bare_unit="days")


# ---------------------------------------------------------------------------
# whole_units — ceil conversion (positive sub-unit windows never collapse to 0)
# ---------------------------------------------------------------------------


def test_whole_units_exact_multiple_days() -> None:
    assert whole_units(timedelta(days=7), unit="days") == 7


def test_whole_units_exact_multiple_hours() -> None:
    assert whole_units(timedelta(hours=5), unit="hours") == 5


def test_whole_units_24h_is_one_day() -> None:
    assert whole_units(timedelta(hours=24), unit="days") == 1


def test_whole_units_sub_day_rounds_up_to_one() -> None:
    # 90 minutes in a days-unit command must NOT collapse to 0 (== no filter).
    assert whole_units(timedelta(minutes=90), unit="days") == 1


def test_whole_units_25h_rounds_up_to_two_days() -> None:
    assert whole_units(timedelta(hours=25), unit="days") == 2


def test_whole_units_coarser_suffix_in_hours_unit() -> None:
    assert whole_units(timedelta(days=2), unit="hours") == 48


def test_whole_units_zero_stays_zero() -> None:
    assert whole_units(timedelta(0), unit="days") == 0


# ---------------------------------------------------------------------------
# since_window — the CLI convenience (parse + convert in one step)
# ---------------------------------------------------------------------------


def test_since_window_bare_number_is_exact() -> None:
    assert since_window("7", unit="days") == 7


def test_since_window_hour_suffix_in_days_command() -> None:
    assert since_window("24h", unit="days") == 1


def test_since_window_minute_suffix_never_zero() -> None:
    assert since_window("90m", unit="days") == 1


def test_since_window_bare_in_hours_command() -> None:
    assert since_window("6", unit="hours") == 6


def test_since_window_rejects_malformed() -> None:
    with pytest.raises(typer.BadParameter):
        since_window("-3d", unit="days")
