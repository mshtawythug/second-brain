"""Why a Gmail receipt attachment was not extracted: one exception class per status (RM-45).

A leaf shared by the parent (:mod:`brain.ingest.gmail_attachments`), the child process
that parses the file (:mod:`brain.ingest.attachment_worker`) and the client between them
(:mod:`brain.ingest.attachment_isolation`), so the child can name a status and the parent
can raise it. No message carries attachment content.
"""
from __future__ import annotations

from typing import ClassVar

from brain.errors import BrainError

__all__ = [
    "AttachmentFetchError",
    "AttachmentTooLargeError",
    "EncryptedAttachmentError",
    "GmailAttachmentError",
    "MalformedAttachmentError",
    "NoOcrError",
    "TooManyPagesError",
    "UnreadableAttachmentError",
    "error_for_status",
]


class GmailAttachmentError(BrainError):
    """An attachment was not extracted. Each subclass is one status; no message carries content."""

    status: ClassVar[str] = "failed"


class AttachmentTooLargeError(GmailAttachmentError):
    """Over the byte cap: declared, fetched or decoded."""

    status = "too large"


class MalformedAttachmentError(GmailAttachmentError):
    """A bad id, ``gws`` answer or base64 payload."""

    status = "malformed"


class AttachmentFetchError(GmailAttachmentError):
    """``gws`` could not fetch the attachment (missing, non-zero exit, timeout)."""

    status = "failed"


class EncryptedAttachmentError(GmailAttachmentError):
    """A password-protected PDF."""

    status = "encrypted"


class TooManyPagesError(GmailAttachmentError):
    """A PDF over the page cap."""

    status = "too many pages"


class UnreadableAttachmentError(GmailAttachmentError):
    """The file could not be written or parsed, or its parse hit a worker limit."""

    status = "unreadable"


class NoOcrError(GmailAttachmentError):
    """An image needed OCR while OCR is off or ``tesseract`` is missing (a re-run retries it)."""

    status = "no ocr"


_BY_STATUS: dict[str, type[GmailAttachmentError]] = {
    cls.status: cls
    for cls in (
        EncryptedAttachmentError, TooManyPagesError, UnreadableAttachmentError, NoOcrError
    )
}


def error_for_status(status: object) -> GmailAttachmentError:
    """The exception for a status the worker reported; anything else is ``unreadable``."""
    cls = _BY_STATUS.get(status) if isinstance(status, str) else None
    return (cls or UnreadableAttachmentError)("attachment not extracted")
