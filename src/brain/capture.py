"""Pure helpers for the quick-capture inbox (`brain capture`): slug + doc build."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from brain.ingest import ExtractedDoc
from brain.ingest.stdin import make_doc
from brain.tags import normalize_tags

# Runs of non-word characters separate slug segments WITHIN a single word.
# ``\w`` (with re.UNICODE) keeps unicode letters/digits/underscore so accented
# captures survive instead of being stripped to ASCII.
_NON_WORD_RUN = re.compile(r"[^\w]+", flags=re.UNICODE)


def slugify_title(text: str, max_words: int) -> str:
    """Casefold ``text``, keep the first ``max_words`` whitespace words, hyphenate.

    Words are split on whitespace, so a hyphenated token like ``project-ko``
    counts as one word and survives intact. Within each word, any run of
    non-word characters collapses to a single ``-`` and leading/trailing ``-``
    are trimmed; empty words are dropped. The cleaned words are then joined
    with ``-``.

    Example::

        slugify_title("follow up with person-a re project-ko", max_words=6)
        # -> "follow-up-with-person-a-re-project-ko"

    Raises:
        ValueError: when ``max_words`` is less than 1.
    """
    if max_words < 1:
        raise ValueError(f"max_words must be >= 1 (got {max_words})")
    folded = unicodedata.normalize("NFKC", text).strip().casefold()
    cleaned: list[str] = []
    for raw_word in folded.split()[:max_words]:
        slug_word = _NON_WORD_RUN.sub("-", raw_word).strip("-")
        if slug_word:
            cleaned.append(slug_word)
    return "-".join(cleaned)


def make_capture_title(content: str, *, today: date, max_words: int) -> str:
    """Build the deterministic auto-title for a capture.

    Shape: ``<ISO date>-capture-<slug>`` (e.g. ``2026-06-04-capture-follow-up``).
    ``today`` is injected by the caller so this stays pure and frozen-date
    testable — never call ``date.today()`` here.
    """
    return f"{today.isoformat()}-capture-{slugify_title(content, max_words)}"


def make_capture_doc(
    content: str,
    title: str | None,
    content_type: str,
    tags: list[str],
) -> ExtractedDoc:
    """Build the quick-capture :class:`ExtractedDoc` via :func:`ingest.stdin.make_doc`.

    Captures have no upstream file or source id, so ``source_path`` is ``None``.
    The normalized ``tags`` are recorded under ``metadata['tags']`` as provenance
    on the document itself (the queryable copy is written to ``documents.tags``
    by :func:`brain.ingest.ingest_document`); when no tags are supplied the
    metadata is left empty.

    Raises:
        ValueError: when ``content`` is empty/whitespace-only, or when ``title``
            is ``None``/blank (the caller is responsible for resolving the
            auto-title before calling).
    """
    if not content.strip():
        raise ValueError("capture content is empty")
    if title is None or not title.strip():
        raise ValueError("capture title is required")
    normalized = normalize_tags(tags)
    metadata: dict[str, Any] = {"tags": normalized} if normalized else {}
    return make_doc(
        content=content,
        title=title.strip(),
        content_type=content_type,
        source_path=None,
        metadata=metadata,
    )
