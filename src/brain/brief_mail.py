"""The delivered brief's email identity (RM-16): its subject, and how ingest knows the owner's copy.

``brain brief --wiki`` can email the owner a ``[brain brief] YYYY-MM-DD`` message
(:mod:`brain.brief_delivery`), and ``brain ingest-gmail`` must not ingest it back. The skip is
done here, on the fetched message, not in the Gmail query: what Gmail makes of a negated group
cannot be checked offline, and a misread would silently drop all of the owner's sent mail (plan,
Deviations 11). A leaf, so ``ingest/gmail.py`` can use it without importing the delivery code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from email.utils import parseaddr
from typing import Any

#: The fixed subject prefix of every delivered brief.
SUBJECT_PREFIX = "[brain brief]"

#: Reply and forward markers, repeated and in any case (``Re: FW: Fwd:``), stripped before the
#: prefix check: the owner's reply to or forward of a brief quotes its body.
_REPLY_MARKERS = re.compile(r"^\s*(?:(?:re|fwd?)\s*:\s*)*", re.IGNORECASE)


def is_own_brief_mail(from_header: str | None, subject: str | None, owner: str | None) -> bool:
    """Whether a message is the owner's own delivered brief: BOTH the owner's address in ``From``
    (case-insensitive, display name ignored) AND a subject that starts with the brief's prefix
    once any ``Re:`` / ``Fwd:`` / ``Fw:`` markers are stripped.

    The owner's replies to and forwards of a brief are skipped too: they quote its body. With no
    ``owner`` (``BRAIN_USER_EMAIL`` unset), nothing is skipped; delivery by email needs it.
    """
    if not owner or not from_header or not subject:
        return False
    _, address = parseaddr(from_header)
    return (
        address.casefold() == owner.strip().casefold()
        and _REPLY_MARKERS.sub("", subject, count=1).startswith(SUBJECT_PREFIX)
    )


def is_own_brief_message(msg: Mapping[str, Any], owner: str | None) -> bool:
    """:func:`is_own_brief_mail` on a Gmail Message resource, from its ``payload.headers``."""
    headers = {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in (msg.get("payload") or {}).get("headers") or []
    }
    return is_own_brief_mail(headers.get("from"), headers.get("subject"), owner)
