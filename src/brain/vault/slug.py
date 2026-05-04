"""Deterministic ASCII slug generators for vault filenames."""
import hashlib
import re
import unicodedata
from datetime import UTC, datetime

from brain.errors import BrainError

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MAX_LEN = 64
_FALLBACK = "untitled"

# Repeated ``Re:`` / ``Fwd:`` / ``Fw:`` prefixes are stripped before slugging
# the gmail subject so a long reply chain collapses to its underlying topic.
# Match is case-insensitive and tolerates surrounding whitespace.
_RE_FWD_PREFIX = re.compile(r"^\s*(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_NO_SUBJECT = "no-subject"
_GMAIL_SLUG_MAX = 64


def _normalize_to_dashes(text: str) -> str:
    """Lowercase ASCII normalization with non-alnum runs collapsed to hyphens.

    Shared by :func:`slugify` and :func:`gmail_slug` so both apply identical
    rules on the raw text before each layer adds its own truncation /
    fallback policy. NFKD-normalize, drop non-ASCII bytes, lowercase,
    collapse non-``[a-z0-9]`` runs to a single ``-``, and strip leading /
    trailing hyphens. Returns the empty string when nothing survives — the
    caller picks the appropriate fallback (``"untitled"`` vs ``"no-subject"``).
    """
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    dashed = _NON_ALNUM.sub("-", ascii_text)
    return dashed.strip("-")


def slugify(text: str) -> str:
    """Convert ``text`` into a filesystem-safe ASCII slug.

    Rules (must stay deterministic so re-exports produce identical filenames):

    - Unicode is transliterated via NFKD normalization with non-ASCII bytes
      dropped (``é`` → ``e``, ``中`` → ``""``).
    - Lowercased.
    - Runs of non-``[a-z0-9]`` collapse to a single ``-``.
    - Leading/trailing ``-`` are stripped.
    - Truncated to 64 characters (after stripping, so an over-long input that
      ends in a hyphen run still yields a clean slug).
    - Empty result falls back to the literal string ``"untitled"``.
    """
    stripped = _normalize_to_dashes(text)
    truncated = stripped[:_MAX_LEN].rstrip("-")
    return truncated or _FALLBACK


def _strip_re_fwd_prefixes(subject: str) -> str:
    """Repeatedly strip leading ``Re:`` / ``Fwd:`` / ``Fw:`` prefixes.

    Example: ``"Re: Re: Fwd: Re: hello"`` → ``"hello"``. Match is
    case-insensitive and tolerates surrounding whitespace; the loop
    terminates on the first pass that yields no change so a subject with no
    leading prefix returns immediately.
    """
    prev: str | None = None
    while subject != prev:
        prev = subject
        subject = _RE_FWD_PREFIX.sub("", subject)
    return subject


def gmail_slug(
    thread_id: str,
    sent_at: datetime | None,
    subject: str | None,
    *,
    fallback_date: datetime | None = None,
) -> str:
    """Return a stable, scannable Gmail mirror slug.

    Format: ``YYYY-MM-DD-<thread6>-<subject-slug>``, capped at 64 chars total.

    - ``YYYY-MM-DD`` is the message's ``sent_at`` (or ``fallback_date`` when
      ``sent_at`` is ``None``). TZ-aware datetimes are normalized to UTC
      before the date is extracted; naive datetimes are treated as UTC.
    - ``<thread6>`` is the first 6 hex characters of ``sha1(thread_id)``.
      Stable across re-ingests of the same thread.
    - ``<subject-slug>`` is the subject after stripping leading ``Re:`` /
      ``Fwd:`` / ``Fw:`` prefixes (case-insensitive, repeated), lowercased,
      with non-``[a-z0-9]`` runs collapsed to single hyphens, leading /
      trailing hyphens trimmed. Empty / ``None`` subjects fall back to
      ``"no-subject"``.
    - Total length is capped at 64. Only the subject portion is ever
      truncated; ``YYYY-MM-DD`` and ``<thread6>`` are preserved in full.

    Raises:
        BrainError: ``thread_id`` is empty (signals an upstream bug —
            silently hashing the empty string would lump every
            no-thread-id row under one prefix).
        BrainError: both ``sent_at`` and ``fallback_date`` are ``None``;
            never silently defaults to today.
    """
    if not thread_id:
        raise BrainError("gmail_slug: thread_id must be non-empty")

    chosen = sent_at if sent_at is not None else fallback_date
    if chosen is None:
        raise BrainError(
            "gmail_slug: need at least one of sent_at or fallback_date"
        )
    if chosen.tzinfo is None:
        chosen = chosen.replace(tzinfo=UTC)
    date_part = chosen.astimezone(UTC).strftime("%Y-%m-%d")

    thread6 = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:6]

    cleaned = _strip_re_fwd_prefixes(subject or "")
    subject_slug = _normalize_to_dashes(cleaned) or _NO_SUBJECT

    fixed_prefix = f"{date_part}-{thread6}-"
    budget = _GMAIL_SLUG_MAX - len(fixed_prefix)
    truncated = subject_slug[:budget].rstrip("-")
    if not truncated:
        # ``budget`` is large enough for ``no-subject`` (10 chars) given the
        # 18-char fixed prefix; the secondary rstrip guards against a
        # pathological future cap shrink.
        truncated = _NO_SUBJECT[:budget].rstrip("-") or _NO_SUBJECT
    return f"{fixed_prefix}{truncated}"
