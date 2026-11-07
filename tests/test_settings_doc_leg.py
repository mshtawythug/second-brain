"""``brain.settings.doc_leg``: the document-leg stage's knobs (roadmap ticket RM-20).

Every test passes a plain ``dict``, never ``os.environ``, so a variable exported
in the developer's shell cannot change a result. All values are synthetic.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.doc_leg import (
    DEFAULT_WEIGHT,
    MAX_WEIGHT,
    DocLegSettings,
    load_doc_leg_settings,
)

pytestmark = pytest.mark.nodb


def test_defaults_are_off_with_unit_weight() -> None:
    assert load_doc_leg_settings({}) == DocLegSettings(enabled=False, weight=1.0)
    assert DEFAULT_WEIGHT == 1.0


@pytest.mark.parametrize("raw", ["true", "1", "yes", "ON"])
def test_truthy_spellings_turn_the_leg_on(raw: str) -> None:
    assert load_doc_leg_settings({"BRAIN_DOC_LEG": raw}).enabled


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
def test_falsy_spellings_and_blank_keep_it_off(raw: str) -> None:
    assert not load_doc_leg_settings({"BRAIN_DOC_LEG": raw}).enabled


@pytest.mark.parametrize("raw", ["0", "0.25", "10"])
def test_weight_accepts_the_closed_range(raw: str) -> None:
    settings = load_doc_leg_settings({"BRAIN_DOC_LEG": "true", "BRAIN_DOC_LEG_WEIGHT": raw})

    assert settings.weight == float(raw)


@pytest.mark.parametrize("raw", ["-0.1", "10.5", "nan", "inf", "heavy"])
def test_a_bad_weight_is_refused_without_quoting_it(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_doc_leg_settings({"BRAIN_DOC_LEG": "true", "BRAIN_DOC_LEG_WEIGHT": raw})

    assert "BRAIN_DOC_LEG_WEIGHT" in str(caught.value)
    assert raw not in str(caught.value)


def test_a_bad_weight_is_refused_even_while_the_leg_is_off() -> None:
    with pytest.raises(ConfigError, match="BRAIN_DOC_LEG_WEIGHT"):
        load_doc_leg_settings({"BRAIN_DOC_LEG_WEIGHT": "heavy"})


def test_a_bad_switch_is_refused() -> None:
    with pytest.raises(ConfigError, match="BRAIN_DOC_LEG"):
        load_doc_leg_settings({"BRAIN_DOC_LEG": "sometimes"})


def test_the_upper_bound_is_ten() -> None:
    assert MAX_WEIGHT == 10.0
