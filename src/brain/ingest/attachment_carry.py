"""Which stored attachment outcomes a re-run reuses, with their sections (RM-45, sec-117 F2).

Pure logic over the stored thread document: its ``content`` and its
``metadata["attachments"]`` records. A part whose record is final is not fetched or
parsed again (so a thread with more parts than one run's cap makes progress across
runs, and stops starving the threads after it), and an extracted part's section is
taken from the stored body, so a flagged run never drops text it stored before.

**How a stored section is found.** The sections are the tail of the body, in record
order, joined by a blank line, and each extracted record carries its section's length
(``chars``) and SHA-256. The tail is cut from the **end** of the body by those lengths, so
the mail text before it (a third party's, unescaped) can never be read as a section, and
every piece must hash to its record. Any mismatch (the secret guard rewrote a section, a
record from an older version, an edited body) makes the stored sections untrusted: the
extracted parts are then parsed again, and the caller keeps the stored body if that
cannot be done in this run.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .gmail_attachments import FINAL_STATUSES, AttachmentRecord, Carried, PartKey

__all__ = ["carried_parts", "stored_records", "stored_sections"]

_SEPARATOR = "\n\n"


def stored_records(raw: object) -> list[AttachmentRecord]:
    """The records in ``metadata["attachments"]`` this version can read, in stored order."""
    if not isinstance(raw, list):
        return []
    records = [AttachmentRecord.from_metadata(item) for item in raw]
    return [record for record in records if record is not None]


def stored_sections(
    content: str, records: Sequence[AttachmentRecord]
) -> dict[PartKey, str] | None:
    """The stored section of every extracted record, or ``None`` if they cannot be trusted."""
    extracted = [record for record in records if record.status == "extracted"]
    if not extracted:
        return {}
    lengths = [record.chars for record in extracted]
    if any(length is None or length <= 0 for length in lengths):
        return None
    total = sum(length for length in lengths if length) + len(_SEPARATOR) * (len(extracted) - 1)
    if len(content) < total + len(_SEPARATOR):
        return None
    if content[-total - len(_SEPARATOR) : -total] != _SEPARATOR:
        return None
    tail = content[-total:]
    found: dict[PartKey, str] = {}
    position = 0
    for record in extracted:
        assert record.chars is not None  # checked above
        piece = tail[position : position + record.chars]
        if hashlib.sha256(piece.encode("utf-8")).hexdigest() != record.sha256:
            return None
        found[record.key] = piece
        position += record.chars + len(_SEPARATOR)
        if position < len(tail) and tail[position - len(_SEPARATOR) : position] != _SEPARATOR:
            return None
    return found


def carried_parts(content: str, raw_records: object) -> dict[PartKey, Carried]:
    """The final stored outcomes a re-run keeps; an extracted one only with its section."""
    records = stored_records(raw_records)
    sections = stored_sections(content, records)
    carried: dict[PartKey, Carried] = {}
    for record in records:
        if record.status not in FINAL_STATUSES:
            continue
        if record.status != "extracted":
            carried[record.key] = (record, None)
        elif sections is not None:
            carried[record.key] = (record, sections[record.key])
    return carried
