"""The OCR knobs for image-only PDFs (roadmap ticket RM-32).

``BRAIN_OCR`` is ``auto`` (the default: OCR when the ``tesseract`` binary is on
``PATH``) or ``off``. ``BRAIN_OCR_LANG`` is tesseract's ``-l`` argument, one or
more language codes joined by ``+`` (``eng``, ``eng+deu``). ``BRAIN_OCR_MAX_PAGES``
caps how many pages of one PDF are rendered and recognised.

The language is checked against tesseract's code shape, not against the
languages installed (``brain doctor`` reports those). It reaches ``tesseract``
as one argv element with no shell, so the check is not what stops injection; it
stops a value such as ``-psm`` from being read as an option.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from brain.config import ConfigError
from brain.settings._env import env_choice, env_int, env_str

MODE_AUTO = "auto"
MODE_OFF = "off"
MODES = frozenset({MODE_AUTO, MODE_OFF})

DEFAULT_LANG = "eng"
DEFAULT_MAX_PAGES = 50
#: A bound on the cap itself: at 60 s per page, 1,000 pages is over 16 hours.
MAX_MAX_PAGES = 1000

_LANG_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*")


@dataclass(frozen=True)
class OcrSettings:
    """OCR's configuration. ``mode == "off"`` means no PDF is ever OCR'd."""

    mode: str
    lang: str
    max_pages: int

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF


def load_ocr_settings(env: Mapping[str, str]) -> OcrSettings:
    """Read the OCR knobs from ``env`` (``os.environ`` after ``Config.load()``)."""
    lang = env_str(env, "BRAIN_OCR_LANG", DEFAULT_LANG)
    if not _LANG_PATTERN.fullmatch(lang):
        raise ConfigError(
            "BRAIN_OCR_LANG must be tesseract language codes (letters, digits, "
            "underscores) joined by '+'"
        )
    return OcrSettings(
        mode=env_choice(env, "BRAIN_OCR", MODES, MODE_AUTO),
        lang=lang,
        max_pages=env_int(
            env, "BRAIN_OCR_MAX_PAGES", DEFAULT_MAX_PAGES, minimum=1, maximum=MAX_MAX_PAGES
        ),
    )
