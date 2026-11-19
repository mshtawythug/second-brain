"""Parse a document's date (RFC 2822 or ISO 8601) into a TZ-aware UTC ``datetime``.

A leaf so the ingest pipeline (which promotes ``metadata.date`` to ``sent_at``) and
``brain ingest-stdin``'s ``--date`` check (0.4.0 QA F4) accept exactly the same
strings. Moved verbatim from :mod:`brain.ingest`, which still imports it under its
old private name.
"""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any


def parse_sent_at(raw: Any) -> datetime | None:
    """Parse a date string into a TZ-aware UTC ``datetime``.

    Accepts both RFC 2822 (Gmail ``Date:`` header style — e.g.
    ``"Tue, 04 May 2026 14:23:01 -0400"``) and ISO 8601 (Krisp-style — e.g.
    ``"2026-05-04T14:23:01+00:00"``) inputs. Returns ``None`` for any input
    we can't parse — callers log + skip the column rather than crashing the
    ingest. Naive datetimes are treated as UTC.

    An offset that pushes the instant past ``datetime.min`` / ``datetime.max`` in UTC
    (``0001-01-01T00:00:00+05:00``) is ``None`` too: ``astimezone`` raises
    ``OverflowError`` there, and that crashed ``--date`` and the pipeline's
    ``sent_at`` promotion alike (review rev-137 #3).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    parsed: datetime | None = None
    # RFC 2822 first — ``parsedate_to_datetime`` is strict-but-tolerant and
    # is the format Gmail's ``Date:`` header uses, so it's the more common
    # case for this codebase.
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            # ``fromisoformat`` accepts the trailing-Z form in 3.11+.
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None
