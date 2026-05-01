"""Tag normalization rules used across brain (DB writes, file writes, ingest).

Single source of truth for the canonical tag form: lowercase
(``str.casefold`` for proper Unicode handling), hyphen-separated, deduped,
empty inputs dropped. Every tag-write boundary in the codebase
(``ingest.apply_tags``, ``vault.frontmatter.rewrite_tags``,
``vault.sync._coerce_tag_list``, the ``brain backfill normalize-tags``
subcommand) feeds inputs through :func:`normalize_tags` before persisting,
so future writes can never re-introduce uppercase / separator / duplicate
drift.
"""
import re
from collections.abc import Iterable

_SEPARATOR_RE = re.compile(r"[\s_]+")
_HYPHEN_RUN_RE = re.compile(r"-+")


def normalize_tag(tag: str) -> str:
    """Canonicalize a single tag string.

    Steps, in order:

    1. ``str.strip`` to drop leading/trailing whitespace.
    2. ``str.casefold`` so Unicode edge cases collapse correctly
       (German ``ß`` → ``ss``, Greek final sigma → ``σ``, etc.).
    3. Replace runs of whitespace + underscores with a single hyphen so
       ``Interview Prep`` and ``interview_prep`` both canonicalize to
       ``interview-prep``.
    4. Collapse runs of hyphens to a single hyphen, then strip leading /
       trailing hyphens — ``"  --foo--  "`` → ``"foo"``.

    An all-whitespace input (or an input that becomes empty after the
    cleanup, e.g. ``"---"``) returns the empty string. Callers that work
    with collections should use :func:`normalize_tags` which drops empties
    automatically.
    """
    folded = tag.strip().casefold()
    hyphenated = _SEPARATOR_RE.sub("-", folded)
    return _HYPHEN_RUN_RE.sub("-", hyphenated).strip("-")


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """Canonicalize, dedupe, and drop empties from an iterable of tags.

    Order is the first-seen order of the canonicalized form: the first
    occurrence of each canonical tag wins, later duplicates (regardless of
    casing or separator variant) are silently dropped. Empty / whitespace-
    only entries — and entries that canonicalize to the empty string — are
    dropped.

    The return value is always a fresh ``list[str]``; callers can mutate it
    safely.
    """
    cleaned = (normalize_tag(t) for t in tags)
    return [t for t in dict.fromkeys(cleaned) if t]
