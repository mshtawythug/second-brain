"""Task 5.3 — unknown enum filter values fail loudly (exit 2), never silent-empty.

The ``--source`` filter is a genuinely closed enum (``sources.kind`` is only
ever written by the ingest paths: manual / krisp / gmail / slack). A typo'd
value previously matched no rows and returned an empty result set with no
signal; now it exits 2 naming the bad value + the accepted set.
"""
import pytest
import typer
from typer.testing import CliRunner

from brain.cli import _validate_source_choice, app


def _combined(result: object) -> str:
    """Merge stdout + (separately-captured) stderr for message assertions."""
    out = result.output  # type: ignore[attr-defined]
    err = result.stderr if result.stderr else ""  # type: ignore[attr-defined]
    return out + err


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "hello", "--source", "bogus"],
        ["explain", "hello", "--source", "bogus"],
        ["list", "--source", "bogus"],
        ["resurface", "--source", "bogus"],
    ],
)
def test_unknown_source_exits_2_and_names_value_and_set(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2, _combined(result)
    combined = _combined(result)
    assert "bogus" in combined
    # The error names the accepted set (spot-check two members).
    assert "krisp" in combined
    assert "manual" in combined


# The shared validator (DRY) is exercised directly so the "valid source passes"
# path needs no DB — the four commands each call this one function.


def test_validate_source_choice_returns_none_unchanged() -> None:
    assert _validate_source_choice(None) is None


@pytest.mark.parametrize("source", ["manual", "krisp", "gmail", "slack"])
def test_validate_source_choice_accepts_known_kinds(source: str) -> None:
    assert _validate_source_choice(source) == source


def test_validate_source_choice_rejects_unknown() -> None:
    with pytest.raises(typer.BadParameter) as excinfo:
        _validate_source_choice("bogus")
    message = str(excinfo.value)
    assert "bogus" in message
    assert "krisp" in message  # names the accepted set
