"""``brain.settings.engagement``: the engagement stage's knobs (roadmap ticket RM-22b).

Every test passes a plain ``dict``, never ``os.environ``, so a variable exported
in the developer's shell cannot change a result. All values are synthetic.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.engagement import (
    MAX_WEIGHT,
    EngagementSettings,
    load_engagement_settings,
)

pytestmark = pytest.mark.nodb

ALPHA = "BRAIN_ENGAGEMENT_ALPHA"
BETA = "BRAIN_ENGAGEMENT_BETA"


def test_defaults_are_zero_and_off() -> None:
    settings = load_engagement_settings({})

    assert settings == EngagementSettings(alpha=0.0, beta=0.0)
    assert not settings.enabled


@pytest.mark.parametrize("raw", ["", "  ", "0", "0.0"])
def test_blank_or_zero_keeps_it_off(raw: str) -> None:
    assert not load_engagement_settings({ALPHA: raw, BETA: raw}).enabled


def test_alpha_alone_turns_it_on() -> None:
    settings = load_engagement_settings({ALPHA: "0.5"})

    assert settings.enabled
    assert (settings.alpha, settings.beta) == (0.5, 0.0)


def test_beta_alone_turns_it_on() -> None:
    # D1: a ratings-only stage is coherent, so beta alone is enough.
    settings = load_engagement_settings({BETA: "0.25"})

    assert settings.enabled
    assert (settings.alpha, settings.beta) == (0.0, 0.25)


@pytest.mark.parametrize("raw", ["0", "0.01", "1"])
def test_each_knob_accepts_the_closed_range(raw: str) -> None:
    settings = load_engagement_settings({ALPHA: raw, BETA: raw})

    assert (settings.alpha, settings.beta) == (float(raw), float(raw))


@pytest.mark.parametrize("name", [ALPHA, BETA])
@pytest.mark.parametrize("raw", ["-0.1", "1.01", "nan", "inf", "lots"])
def test_a_bad_knob_is_refused_without_quoting_it(name: str, raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_engagement_settings({name: raw})

    assert name in str(caught.value)
    assert raw not in str(caught.value)


def test_a_bad_knob_is_refused_while_the_other_is_off() -> None:
    with pytest.raises(ConfigError, match=BETA):
        load_engagement_settings({ALPHA: "0", BETA: "lots"})


def test_the_upper_bound_is_one() -> None:
    assert MAX_WEIGHT == 1.0
