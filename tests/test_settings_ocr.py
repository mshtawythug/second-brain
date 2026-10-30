"""``brain.settings.ocr``: the OCR knobs (roadmap ticket RM-32).

Every test passes a plain ``dict``, never ``os.environ``, so a variable exported
in the developer's shell cannot change a result. All values are synthetic.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.ocr import (
    DEFAULT_LANG,
    DEFAULT_MAX_PAGES,
    MAX_MAX_PAGES,
    MODE_AUTO,
    MODE_OFF,
    OcrSettings,
    load_ocr_settings,
)

pytestmark = pytest.mark.nodb


def test_defaults_are_auto_english_fifty_pages() -> None:
    settings = load_ocr_settings({})

    assert settings == OcrSettings(mode=MODE_AUTO, lang="eng", max_pages=50)
    assert (DEFAULT_LANG, DEFAULT_MAX_PAGES) == ("eng", 50)
    assert settings.enabled


def test_off_disables_ocr() -> None:
    settings = load_ocr_settings({"BRAIN_OCR": " OFF "})

    assert settings.mode == MODE_OFF
    assert not settings.enabled


@pytest.mark.parametrize("lang", ["deu", "eng+deu", "chi_sim", "eng+chi_tra+fra"])
def test_language_codes_and_plus_joined_lists_are_accepted(lang: str) -> None:
    assert load_ocr_settings({"BRAIN_OCR_LANG": f" {lang} "}).lang == lang


@pytest.mark.parametrize(
    "lang",
    ["-psm", "eng;rm", "eng+", "+eng", "eng deu", "eng++deu", "../eng", "eng\n"  "x"],
)
def test_a_language_that_is_not_a_code_list_is_refused_without_echoing_it(lang: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_ocr_settings({"BRAIN_OCR_LANG": lang})

    assert "BRAIN_OCR_LANG" in str(caught.value)
    assert lang.strip() not in str(caught.value)


def test_max_pages_is_read_and_bounded() -> None:
    assert load_ocr_settings({"BRAIN_OCR_MAX_PAGES": "3"}).max_pages == 3
    assert load_ocr_settings({"BRAIN_OCR_MAX_PAGES": str(MAX_MAX_PAGES)}).max_pages == MAX_MAX_PAGES


@pytest.mark.parametrize("raw", ["0", "-1", "1001", "many", "2.5"])
def test_a_bad_page_cap_is_refused(raw: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_OCR_MAX_PAGES"):
        load_ocr_settings({"BRAIN_OCR_MAX_PAGES": raw})


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ConfigError, match="BRAIN_OCR must be one of: auto, off"):
        load_ocr_settings({"BRAIN_OCR": "always"})
