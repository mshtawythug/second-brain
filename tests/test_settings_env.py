"""Tests for ``brain.settings._env``, the parsers every feature settings leaf uses (RM-08).

Pure and dict-driven: every test hands the parser a plain ``dict`` as ``env``,
so nothing here reads or writes the process environment. The ``.env``
integration lives in ``tests/test_settings_dotenv.py``.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from brain.config import ConfigError
from brain.settings._env import env_bool, env_choice, env_float, env_int, env_str

pytestmark = pytest.mark.nodb

NAME = "BRAIN_SETTINGS_TEST_KNOB"
COLOURS = frozenset({"red", "green", "blue"})
# Shaped like a credential, so a refusal that echoed it would be a leak.
SECRET_SHAPED = "sk-live-0123456789abcdefSECRET"

Parser = Callable[[dict[str, str]], object]

# One call per parser, each with a distinctive default. ``env_str`` never
# refuses, so it appears here (AC2) and not in the refusal table.
PARSERS_WITH_DEFAULTS: dict[str, tuple[Parser, object]] = {
    "bool": (lambda env: env_bool(env, NAME, True), True),
    "choice": (lambda env: env_choice(env, NAME, COLOURS, "green"), "green"),
    "int": (lambda env: env_int(env, NAME, 7, minimum=1), 7),
    "float": (lambda env: env_float(env, NAME, 2.5, minimum=0.0), 2.5),
    "str": (lambda env: env_str(env, NAME, "fallback"), "fallback"),
    "str_none": (lambda env: env_str(env, NAME, None), None),
}

REFUSING_PARSERS: dict[str, Parser] = {
    "bool": lambda env: env_bool(env, NAME, False),
    "choice": lambda env: env_choice(env, NAME, COLOURS, "red"),
    "int": lambda env: env_int(env, NAME, 1, minimum=1),
    "float": lambda env: env_float(env, NAME, 1.0, minimum=0.0),
}


# ---------------------------------------------------------------------------
# AC2 — an unset (or blank) variable yields the default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
@pytest.mark.parametrize("parser_name", sorted(PARSERS_WITH_DEFAULTS))
def test_unset_or_blank_yields_default(parser_name: str, raw: str | None) -> None:
    parser, default = PARSERS_WITH_DEFAULTS[parser_name]
    env = {} if raw is None else {NAME: raw}

    assert parser(env) == default


def test_other_variables_do_not_leak_into_the_named_one() -> None:
    env = {"BRAIN_SETTINGS_OTHER": "99"}

    assert env_int(env, NAME, 3, minimum=1) == 3


# ---------------------------------------------------------------------------
# AC1 — env_bool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("  Yes ", True),
        ("On", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
        (" No\t", False),
        ("Off", False),
    ],
)
def test_env_bool_accepts_every_spelling(raw: str, expected: bool) -> None:
    assert env_bool({NAME: raw}, NAME, not expected) is expected


@pytest.mark.parametrize("raw", ["2", "y", "n", "enabled", "truthy", "-1", "t"])
def test_env_bool_refuses_other_values(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_bool({NAME: raw}, NAME, False)

    message = str(caught.value)
    assert NAME in message
    assert "1/true/yes/on" in message and "0/false/no/off" in message


def test_env_bool_agrees_with_config_on_spellings() -> None:
    """A leaf flag and a ``config.py`` flag accept exactly the same words."""
    from brain import config
    from brain.settings import _env

    assert _env.TRUTHY is config._TRUTHY_ENV_VALUES
    assert _env.FALSY is config._GRAPH_ENABLED_FALSY


# ---------------------------------------------------------------------------
# AC1 — env_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("red", "red"), ("BLUE", "blue"), ("  Green  ", "green")],
)
def test_env_choice_accepts_a_choice_case_insensitively(raw: str, expected: str) -> None:
    assert env_choice({NAME: raw}, NAME, COLOURS, "red") == expected


@pytest.mark.parametrize("raw", ["purple", "re d", "redd", "r"])
def test_env_choice_refuses_anything_else(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_choice({NAME: raw}, NAME, COLOURS, "red")

    message = str(caught.value)
    assert NAME in message
    assert "blue, green, red" in message  # sorted, so the message is stable


def test_env_choice_default_outside_choices_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="default"):
        env_choice({}, NAME, COLOURS, "purple")


def test_env_choice_rejects_non_lowercase_choices() -> None:
    """Values are lowercased before comparison, so an uppercase choice could never match."""
    with pytest.raises(ValueError, match="lowercase"):
        env_choice({}, NAME, frozenset({"Red", "green"}), "green")


# ---------------------------------------------------------------------------
# AC1 — env_int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("5", 5), (" 12 ", 12), ("+3", 3), ("1", 1)])
def test_env_int_accepts_integers_at_or_above_minimum(raw: str, expected: int) -> None:
    assert env_int({NAME: raw}, NAME, 9, minimum=1) == expected


def test_env_int_minimum_is_inclusive_and_may_be_negative() -> None:
    assert env_int({NAME: "-2"}, NAME, 0, minimum=-2) == -2


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_env_int_refuses_below_minimum(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_int({NAME: raw}, NAME, 9, minimum=1)

    assert str(caught.value) == f"{NAME} must be an integer >= 1"


@pytest.mark.parametrize("raw", ["1.5", "ten", "1e3", "0x10", "5 apples"])
def test_env_int_refuses_non_integers(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_int({NAME: raw}, NAME, 9, minimum=1)

    assert str(caught.value) == f"{NAME} must be an integer >= 1"


def test_env_int_maximum_is_inclusive() -> None:
    assert env_int({NAME: "50"}, NAME, 20, minimum=2, maximum=50) == 50
    assert env_int({NAME: "2"}, NAME, 20, minimum=2, maximum=50) == 2


@pytest.mark.parametrize("raw", ["51", "1", "abc"])
def test_env_int_refuses_outside_a_closed_range(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_int({NAME: raw}, NAME, 20, minimum=2, maximum=50)

    assert str(caught.value) == f"{NAME} must be an integer in [2, 50]"


@pytest.mark.parametrize(
    ("default", "minimum", "maximum"),
    [(0, 1, None), (51, 2, 50), (1, 2, 50)],
    ids=["below", "above", "below_range"],
)
def test_env_int_default_outside_bounds_is_a_programming_error(
    default: int, minimum: int, maximum: int | None
) -> None:
    with pytest.raises(ValueError, match="default"):
        env_int({NAME: "5"}, NAME, default, minimum=minimum, maximum=maximum)


def test_env_int_equal_bounds_are_a_legal_closed_range() -> None:
    assert env_int({NAME: "5"}, NAME, 5, minimum=5, maximum=5) == 5


def test_env_int_empty_bounds_are_a_programming_error() -> None:
    with pytest.raises(ValueError, match="minimum exceeds maximum"):
        env_int({}, NAME, 5, minimum=10, maximum=2)


# ---------------------------------------------------------------------------
# AC1 — env_float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.45", 0.45), (" 5 ", 5.0), ("1e-2", 0.01), ("0", 0.0), ("+2.5", 2.5)],
)
def test_env_float_accepts_floats_at_or_above_minimum(raw: str, expected: float) -> None:
    assert env_float({NAME: raw}, NAME, 9.0, minimum=0.0) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["-0.01", "-5"])
def test_env_float_refuses_below_minimum(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_float({NAME: raw}, NAME, 1.0, minimum=0.0)

    assert str(caught.value) == f"{NAME} must be a finite number >= 0.0"


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_env_float_refuses_non_finite_values(raw: str) -> None:
    """``float()`` parses these, and ``nan`` slips past every ``<`` bound check."""
    with pytest.raises(ConfigError) as caught:
        env_float({NAME: raw}, NAME, 1.0, minimum=-math.inf)

    assert NAME in str(caught.value)


@pytest.mark.parametrize("raw", ["fast", "1,5", "0.5s"])
def test_env_float_refuses_non_numbers(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_float({NAME: raw}, NAME, 1.0, minimum=0.0)

    assert str(caught.value) == f"{NAME} must be a finite number >= 0.0"


def test_env_float_maximum_is_inclusive() -> None:
    assert env_float({NAME: "1.0"}, NAME, 0.5, minimum=0.0, maximum=1.0) == 1.0


@pytest.mark.parametrize("raw", ["1.01", "-0.1", "nan"])
def test_env_float_refuses_outside_a_closed_range(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        env_float({NAME: raw}, NAME, 0.5, minimum=0.0, maximum=1.0)

    assert str(caught.value) == f"{NAME} must be a finite number in [0.0, 1.0]"


@pytest.mark.parametrize(
    ("default", "maximum"),
    [(-0.5, None), (1.5, 1.0), (math.nan, None)],
    ids=["below", "above", "nan"],
)
def test_env_float_default_outside_bounds_is_a_programming_error(
    default: float, maximum: float | None
) -> None:
    with pytest.raises(ValueError, match="default"):
        env_float({NAME: "0.5"}, NAME, default, minimum=0.0, maximum=maximum)


def test_env_float_equal_bounds_are_a_legal_closed_range() -> None:
    assert env_float({NAME: "0.5"}, NAME, 0.5, minimum=0.5, maximum=0.5) == 0.5


def test_env_float_empty_bounds_are_a_programming_error() -> None:
    with pytest.raises(ValueError, match="minimum exceeds maximum"):
        env_float({}, NAME, 0.5, minimum=1.0, maximum=0.0)


# ---------------------------------------------------------------------------
# AC1 — env_str
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("eng", "eng"),
        ("  http://127.0.0.1:8081  ", "http://127.0.0.1:8081"),
        ("Mixed Case", "Mixed Case"),
    ],
)
def test_env_str_returns_the_stripped_value(raw: str, expected: str) -> None:
    assert env_str({NAME: raw}, NAME, "default") == expected


# ---------------------------------------------------------------------------
# AC4 — a refusal never carries the value, not even through a chained cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parser_name", sorted(REFUSING_PARSERS))
def test_refusal_never_echoes_the_value(parser_name: str) -> None:
    parser = REFUSING_PARSERS[parser_name]

    with pytest.raises(ConfigError) as caught:
        parser({NAME: SECRET_SHAPED})

    error = caught.value
    assert NAME in str(error)
    assert SECRET_SHAPED not in str(error)
    assert all(SECRET_SHAPED not in str(arg) for arg in error.args)
    # ``int()`` / ``float()`` quote their input in their own ValueError; a
    # chained cause or context would print it in any traceback.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True or error.__context__ is None
    assert SECRET_SHAPED not in repr(error)


@pytest.mark.parametrize(
    "parser",
    [
        lambda env: env_int(env, NAME, 20, minimum=2, maximum=50),
        lambda env: env_float(env, NAME, 0.5, minimum=0.0, maximum=1.0),
    ],
    ids=["int_range", "float_range"],
)
def test_range_refusal_never_echoes_the_value(parser: Parser) -> None:
    with pytest.raises(ConfigError) as caught:
        parser({NAME: SECRET_SHAPED})

    assert SECRET_SHAPED not in str(caught.value)
    assert caught.value.__cause__ is None
