"""Read a Gmail thread minus the owner's own delivered briefs (RM-16): every ``ingest-gmail`` read.

``brain ingest-gmail`` reads each matched thread through :func:`read_thread_messages`, both to
ingest it and to report it under ``--dry-run``, so the skip applies to every scope flag. The
predicate is :func:`brain.brief_mail.is_own_brief_message`; the Gmail query is unchanged (plan,
Deviations 11). A leaf, so ``ingest/gmail.py`` keeps its line budget.
"""

from __future__ import annotations

import logging
from typing import Any

from brain.brief_mail import is_own_brief_message
from brain.ingest import gmail

_logger = logging.getLogger(__name__)


def read_thread_messages(stubs: list[dict[str, Any]], *, owner: str | None) -> list[Any]:
    """The stubs' full messages, minus the owner's own delivered briefs.

    A skip is logged as a count only: never a subject or a sender.
    """
    messages = [gmail.read_message(stub["id"]) for stub in stubs]
    kept = [msg for msg in messages if not is_own_brief_message(msg, owner)]
    if len(kept) < len(messages):
        _logger.info(
            "ingest-gmail: skipped %d of the owner's own delivered brief message(s)",
            len(messages) - len(kept),
        )
    return kept
