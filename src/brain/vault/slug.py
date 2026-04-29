"""Deterministic ASCII slug generator for vault filenames."""
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MAX_LEN = 64
_FALLBACK = "untitled"


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
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    dashed = _NON_ALNUM.sub("-", ascii_text)
    stripped = dashed.strip("-")
    truncated = stripped[:_MAX_LEN].rstrip("-")
    return truncated or _FALLBACK
