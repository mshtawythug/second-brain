"""Gmail receipt attachments: find, fetch, bound and extract them (RM-45).

The leaf behind ``brain ingest-gmail --with-attachments``. An attachment is a third
party's file (anyone can mail the owner a PDF, and a filter can label it a receipt),
so every step here is bounded, and nothing here logs, prints or raises with content.

**From the outside in:**

1. :func:`plan_thread` walks each rendered message's MIME tree **iteratively**, at most
   ``max_mime_depth`` deep and ``max_parts_per_message`` nodes wide, and keeps the PDF,
   PNG and JPEG parts that carry a filename. An image marked inline, or carrying a
   ``Content-ID`` (a logo the HTML body draws), is not a receipt and is not kept. The
   plan's fingerprint (the mail body, each part's ``(message id, part id, declared
   size)``, :data:`ATTACHMENT_POLICY_VERSION`) lets a re-run skip a thread without
   downloading anything. It keys on the part id: Gmail's attachment ids change between
   ``messages.get`` calls, a message's part ids do not.
2. :func:`extract_attachments` checks the declared size (over ``max_bytes`` is skipped
   **unfetched**), the thread's text budget, the run's count cap and its deadline, in
   that order, before any download.
3. The bytes come from inline ``body.data`` or one ``gws gmail users messages
   attachments get`` call through the injected runner (the CLI passes
   ``real_gws_runner`` with a timeout and :func:`gws_stdout_cap`). Ids are checked
   before they enter the ``--params`` JSON. The answer is size-checked, parsed (a
   ``RecursionError`` is malformed), and only its ``data`` string is read. base64url is
   checked for its alphabet and its length **before** decoding, and the decoded length
   after.
4. Magic bytes decide the type (:func:`sniff`); the declared type only prefiltered.
5. The bytes are written to a fixed name in a private directory
   (:func:`attachment_workdir`, mode 0700) with ``O_CREAT | O_EXCL | O_NOFOLLOW`` and mode
   0600, so the sender's filename never touches the filesystem, and the file is removed
   after extraction, on error too. A PDF is opened by ``pypdf`` first and refused when
   encrypted or over ``max_pages``. Then the registry extracts it
   (``ingest.dispatch.extract_path``), so RM-32's PDF bounds and RM-33's image bounds
   apply unchanged.
6. :func:`brain.ingest.attachment_text.render_section` redacts card numbers, cuts and
   neutralizes the text.

Every outcome is one :class:`AttachmentRecord`, the only thing stored in metadata: ids,
the sniffed type, sizes and a status, **never the filename** (metadata is not scanned by
the secret guard; the name appears only in the guarded body).
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal

from brain.errors import BrainError, DirectoryRefreshError

from . import ExtractedDoc
from .attachment_text import render_section
from .dispatch import extract_path
from .stale_tempdirs import sweep_stale_tempdirs

__all__ = [
    "ATTACHMENT_POLICY_VERSION",
    "FINAL_STATUSES",
    "RETRYABLE_STATUSES",
    "WORKDIR_PREFIX",
    "AttachmentLimits",
    "AttachmentRecord",
    "AttachmentRun",
    "Candidate",
    "GmailAttachmentError",
    "ThreadAttachments",
    "ThreadPlan",
    "attachment_workdir",
    "decode_base64url",
    "extract_attachments",
    "gws_stdout_cap",
    "plan_thread",
    "sniff",
]

#: Bumped when the extraction or the text rules change, so a re-run re-extracts.
ATTACHMENT_POLICY_VERSION = "v1"
#: The private work directory's name prefix (also what the stale sweep matches).
WORKDIR_PREFIX = "brain-gmail-att-"
#: Statuses a re-run tries again; every other status is final for unchanged mail.
RETRYABLE_STATUSES = frozenset({"failed", "over cap", "deadline", "unreadable", "truncated"})
FINAL_STATUSES = frozenset(
    {"extracted", "empty", "too large", "too many pages", "encrypted", "unsupported",
     "malformed"}
)

Kind = Literal["pdf", "png", "jpeg"]
Runner = Callable[[list[str]], str]
Extract = Callable[[Path], ExtractedDoc]

_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,1024}")
_PART_ID_RE = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,32}")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]*={0,2}")
_PDF_MIMES = frozenset({"application/pdf"})
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg"})
_OCTET_STREAM = "application/octet-stream"
_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg")
_SUFFIX: dict[Kind, str] = {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
#: The JSON around the base64 in one ``attachments get`` answer, generously.
_STDOUT_SLACK_BYTES = 64 * 1024
#: Errors a hostile file can draw out of pypdf or an extractor. Their messages can
#: quote the file, so they are reduced to a status and never kept.
_EXTRACT_ERRORS: tuple[type[BaseException], ...] = (
    ValueError, OSError, RecursionError, KeyError, IndexError, TypeError, AttributeError,
)


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
    """The file could not be written or parsed."""

    status = "unreadable"


@dataclass(frozen=True)
class AttachmentLimits:
    """The caps; tests pass small ones."""

    max_bytes: int = 10 * 1024 * 1024
    max_per_run: int = 50
    max_pages: int = 50
    max_section_chars: int = 50_000
    max_thread_chars: int = 200_000
    run_deadline_s: float = 900.0
    max_mime_depth: int = 32
    max_parts_per_message: int = 500


def gws_stdout_cap(limits: AttachmentLimits) -> int:
    """The most stdout one ``attachments get`` may write: the base64 of the cap, plus slack."""
    return _max_base64_chars(limits.max_bytes) + _STDOUT_SLACK_BYTES


def _max_base64_chars(max_bytes: int) -> int:
    return 4 * math.ceil(max_bytes / 3)


@dataclass(frozen=True)
class Candidate:
    """One qualifying part. ``filename`` is the sender's, raw: shown only through a section."""

    message_id: str
    part_id: str
    filename: str
    declared_size: int
    attachment_id: str | None
    inline_data: str | None


@dataclass(frozen=True)
class ThreadPlan:
    """What a thread offers, before any download."""

    candidates: list[Candidate]
    malformed: int
    fingerprint: str


@dataclass(frozen=True)
class AttachmentRecord:
    """One part's outcome; :meth:`as_metadata` is what the document stores."""

    message_id: str
    part_id: str
    status: str
    type: str | None = None
    bytes: int | None = None
    pages: int | None = None
    ocr: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        fields = {
            "message_id": self.message_id,
            "part_id": self.part_id,
            "status": self.status,
            "type": self.type,
            "bytes": self.bytes,
            "pages": self.pages,
            "ocr": self.ocr,
        }
        return {key: value for key, value in fields.items() if value is not None}


@dataclass(frozen=True)
class ThreadAttachments:
    """The sections to append and one record per candidate, in candidate order."""

    sections: list[str]
    records: list[AttachmentRecord]


@dataclass
class AttachmentRun:
    """One run's budget and counts: shared by every thread of an ``ingest-gmail`` run."""

    limits: AttachmentLimits
    clock: Callable[[], float] = time.monotonic
    processed: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    #: The OCR-unavailable hint, the first time an image needed OCR it could not get.
    ocr_hint: str | None = None
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def past_deadline(self) -> bool:
        return self.clock() - self.started > self.limits.run_deadline_s


# --- planning ----------------------------------------------------------------


def _header(node: Mapping[str, Any], name: str) -> str | None:
    headers = node.get("headers")
    if not isinstance(headers, list):
        return None
    for header in headers:
        if isinstance(header, Mapping) and str(header.get("name", "")).lower() == name:
            value = header.get("value")
            return value if isinstance(value, str) else None
    return None


def _qualifies(node: Mapping[str, Any]) -> bool:
    filename, mime = node.get("filename"), node.get("mimeType")
    if not isinstance(filename, str) or not filename or not isinstance(mime, str):
        return False
    mime = mime.strip().lower()
    if mime in _PDF_MIMES:
        return True
    if mime == _OCTET_STREAM:
        return filename.lower().endswith(_EXTENSIONS)
    if mime not in _IMAGE_MIMES:
        return False
    disposition = (_header(node, "content-disposition") or "").strip().lower()
    return not disposition.startswith("inline") and _header(node, "content-id") is None


def _candidate(message_id: str, node: Mapping[str, Any]) -> Candidate | None:
    """The node as a candidate, or ``None`` when its fields are malformed."""
    part_id, body = node.get("partId"), node.get("body")
    if not isinstance(part_id, str) or _PART_ID_RE.fullmatch(part_id) is None:
        return None
    if not isinstance(body, Mapping):
        return None
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    attachment_id, data = body.get("attachmentId"), body.get("data")
    return Candidate(
        message_id=message_id,
        part_id=part_id,
        filename=node["filename"],
        declared_size=size,
        attachment_id=attachment_id if isinstance(attachment_id, str) else None,
        inline_data=data if isinstance(data, str) else None,
    )


def _walk(message: Mapping[str, Any], limits: AttachmentLimits) -> tuple[list[Candidate], bool]:
    """The message's candidates, and whether any of its tree was unreadable."""
    message_id, payload = message.get("id"), message.get("payload")
    if not isinstance(message_id, str) or _MESSAGE_ID_RE.fullmatch(message_id) is None:
        return [], True
    if not isinstance(payload, Mapping):
        return [], True
    found: list[Candidate] = []
    malformed = False
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > limits.max_parts_per_message or depth > limits.max_mime_depth:
            return found, True
        if not isinstance(node, Mapping):
            malformed = True
            continue
        if _qualifies(node):
            candidate = _candidate(message_id, node)
            malformed = malformed or candidate is None
            if candidate is not None:
                found.append(candidate)
        children = node.get("parts")
        if children is None:
            continue
        if not isinstance(children, list):
            malformed = True
            continue
        stack.extend((child, depth + 1) for child in reversed(children))
    return found, malformed


def plan_thread(
    messages: Sequence[Mapping[str, Any]], mail_body: str, *, limits: AttachmentLimits
) -> ThreadPlan:
    """The thread's candidates, in message order, and its fingerprint.

    ``messages`` are the rendered ones (:func:`brain.ingest.gmail.rendered_messages`);
    ``mail_body`` is the thread body before any section. ``malformed`` counts messages
    whose tree was partly unreadable.
    """
    candidates: list[Candidate] = []
    malformed = 0
    for message in messages:
        found, broken = _walk(message, limits)
        candidates.extend(found)
        malformed += broken
    keys = sorted([c.message_id, c.part_id, c.declared_size] for c in candidates)
    payload = json.dumps(
        {"policy": ATTACHMENT_POLICY_VERSION, "body": mail_body, "parts": keys},
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ThreadPlan(candidates=candidates, malformed=malformed, fingerprint=fingerprint)


# --- fetching ----------------------------------------------------------------


def decode_base64url(data: str, *, max_bytes: int) -> bytes:
    """Decode Gmail's base64url ``data``, refusing an oversized or malformed payload unread."""
    if len(data) > _max_base64_chars(max_bytes):
        raise AttachmentTooLargeError("attachment over the size cap")
    if _BASE64URL_RE.fullmatch(data) is None:
        raise MalformedAttachmentError("attachment data is not base64url")
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise MalformedAttachmentError("attachment data is not base64url") from exc
    if len(raw) > max_bytes:
        raise AttachmentTooLargeError("attachment over the size cap")
    return raw


def _fetch_bytes(candidate: Candidate, *, runner: Runner, limits: AttachmentLimits) -> bytes:
    if candidate.inline_data is not None:
        return decode_base64url(candidate.inline_data, max_bytes=limits.max_bytes)
    attachment_id = candidate.attachment_id
    if attachment_id is None or _MESSAGE_ID_RE.fullmatch(attachment_id) is None:
        raise MalformedAttachmentError("attachment has no usable id")
    params = {"userId": "me", "messageId": candidate.message_id, "id": attachment_id}
    argv = [
        "gws", "gmail", "users", "messages", "attachments", "get",
        "--params", json.dumps(params),
        "--format", "json",
    ]
    try:
        out = runner(argv)
    except (DirectoryRefreshError, OSError) as exc:
        raise AttachmentFetchError("gws could not fetch the attachment") from exc
    if len(out) > gws_stdout_cap(limits):
        raise AttachmentTooLargeError("attachment over the size cap")
    try:
        parsed = json.loads(out)
    except (ValueError, RecursionError) as exc:
        raise MalformedAttachmentError("gws answer is not JSON") from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, str):
        raise MalformedAttachmentError("gws answer has no attachment data")
    return decode_base64url(data, max_bytes=limits.max_bytes)


def sniff(data: bytes) -> Kind | None:
    """The type the bytes are, by magic number; ``None`` when not PDF, PNG or JPEG."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(_JPEG_SIGNATURE):
        return "jpeg"
    return None


# --- extracting --------------------------------------------------------------


@contextlib.contextmanager
def attachment_workdir(*, root: Path | None = None) -> Iterator[Path]:
    """A private (0700) directory for one run, removed on exit; stale ones swept first.

    ``root`` (default: the temp directory) is a test seam.
    """
    limits = AttachmentLimits()
    sweep_stale_tempdirs(WORKDIR_PREFIX, max_age_s=2 * limits.run_deadline_s, root=root)
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=root))
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _check_pdf(path: Path, *, max_pages: int) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(str(path))
        encrypted = reader.is_encrypted
        pages = 0 if encrypted else len(reader.pages)
    except PyPdfError as exc:
        raise UnreadableAttachmentError("PDF could not be opened") from exc
    except _EXTRACT_ERRORS as exc:
        raise UnreadableAttachmentError("PDF could not be opened") from exc
    if encrypted:
        raise EncryptedAttachmentError("PDF is encrypted")
    if pages > max_pages:
        raise TooManyPagesError("PDF over the page cap")


def _extract_file(
    data: bytes, kind: Kind, *, workdir: Path, limits: AttachmentLimits, extract: Extract
) -> ExtractedDoc:
    path = workdir / f"attachment{_SUFFIX[kind]}"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise UnreadableAttachmentError("attachment file could not be written") from exc
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
        if kind == "pdf":
            _check_pdf(path, max_pages=limits.max_pages)
        try:
            return extract(path)
        except _EXTRACT_ERRORS as exc:
            raise UnreadableAttachmentError("attachment could not be read") from exc
    finally:
        path.unlink(missing_ok=True)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ocr_engine(metadata: Mapping[str, Any]) -> str | None:
    ocr = metadata.get("ocr")
    engine = ocr.get("engine") if isinstance(ocr, Mapping) else None
    return engine if isinstance(engine, str) else None


def _gate(candidate: Candidate, run: AttachmentRun, remaining: int) -> str | None:
    """The status that stops ``candidate`` before any download, or ``None`` to go on."""
    if candidate.declared_size > run.limits.max_bytes:
        return "too large"
    if remaining <= 0:
        return "truncated"
    if run.processed >= run.limits.max_per_run:
        return "over cap"
    if run.past_deadline():
        return "deadline"
    return None


def _one(
    candidate: Candidate,
    *,
    run: AttachmentRun,
    runner: Runner,
    workdir: Path,
    extract: Extract,
    remaining: int,
) -> tuple[AttachmentRecord, str | None]:
    """Fetch and extract one candidate; its record and section."""
    record = AttachmentRecord(candidate.message_id, candidate.part_id, "failed")
    gated = _gate(candidate, run, remaining)
    if gated is not None:
        return replace(record, status=gated), None
    run.processed += 1
    try:
        data = _fetch_bytes(candidate, runner=runner, limits=run.limits)
        kind = sniff(data)
        record = replace(record, type=kind, bytes=len(data))
        if kind is None:
            return replace(record, status="unsupported", type=None), None
        doc = _extract_file(data, kind, workdir=workdir, limits=run.limits, extract=extract)
    except GmailAttachmentError as exc:
        _note_ocr_hint(exc, run)
        return replace(record, status=exc.status), None
    record = replace(
        record,
        pages=_int_or_none(doc.metadata.get("page_count")),
        ocr=_ocr_engine(doc.metadata),
    )
    max_chars = min(run.limits.max_section_chars, remaining)
    section = render_section(candidate.filename, doc.content, max_chars=max_chars)
    return replace(record, status="empty" if section is None else "extracted"), section


def _note_ocr_hint(exc: GmailAttachmentError, run: AttachmentRun) -> None:
    """Keep the OCR-unavailable hint once, rebuilt from the OCR setup, never from ``exc``."""
    from .image import OcrUnavailableError, ocr_unavailable_hint
    from .ocr import resolve_ocr

    if run.ocr_hint is None and isinstance(exc.__cause__, OcrUnavailableError):
        run.ocr_hint = ocr_unavailable_hint(resolve_ocr(os.environ))


def extract_attachments(
    candidates: Sequence[Candidate],
    *,
    run: AttachmentRun,
    runner: Runner,
    workdir: Path,
    extract: Extract = extract_path,
) -> ThreadAttachments:
    """Fetch and extract ``candidates`` within ``run``'s budget; counts go to ``run``."""
    sections: list[str] = []
    records: list[AttachmentRecord] = []
    remaining = run.limits.max_thread_chars
    for candidate in candidates:
        record, section = _one(
            candidate, run=run, runner=runner, workdir=workdir, extract=extract,
            remaining=remaining,
        )
        run.counts[record.status] += 1
        records.append(record)
        if section is not None:
            sections.append(section)
            remaining -= len(section)
    return ThreadAttachments(sections=sections, records=records)
