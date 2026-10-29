"""Pure checks on an upload: its filename, its extension, its cap, and its bytes (RM-13).

The Ingest spec's §7.4, with the ticket's one change: the allow-list is the explicit
constant :data:`~brain.ui.ingest_limits.UPLOAD_EXTENSIONS`, intersected with the
extractor registry, never the registry alone.

Nothing the client sends is trusted. The ``X-Brain-Filename`` header is used only for
its extension and its display stem, and the bytes must match the extension whatever
the browser declared. No staging state lives here; ``upload_staging`` owns the disk.

Refusals raise the route-facing :class:`~brain.ui.errors.UiError` subclasses directly,
as ``ingest_schemas`` does, so a route maps nothing. Their messages are fixed strings:
never the filename, never a parser's exception text.
"""
from __future__ import annotations

import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from ..ingest.dispatch import ExtractorRegistry, spec_for, supported_extensions
from .errors import UiBadRequest, UiUnsupportedMedia
from .ingest_limits import (
    BINARY_UPLOAD_EXTENSIONS,
    DOCX_MAX_EXPANDED_BYTES,
    DOCX_MAX_MEMBERS,
    DOCX_MAX_RATIO,
    MAX_BINARY_UPLOAD_BYTES,
    MAX_FILENAME_CHARS,
    MAX_TEXT_UPLOAD_BYTES,
    UPLOAD_EXTENSIONS,
)

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_BODY = "word/document.xml"

#: Unicode categories removed from a filename: controls (NUL included) and format
#: characters, among them the bidi overrides that make ``evil<RLO>fdp.txt`` render
#: as ``eviltxt.pdf``.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf"})


@dataclass(frozen=True)
class SafeName:
    """A sanitized filename: ``stem`` carries no separator and no leading dot."""

    stem: str
    ext: str

    @property
    def basename(self) -> str:
        return f"{self.stem}{self.ext}"


def uploadable_extensions(registry: ExtractorRegistry | None = None) -> list[str]:
    """:data:`UPLOAD_EXTENSIONS` that an extractor handles, in registry order.

    ``registry`` defaults to the process-wide one; a test passes its own.
    """
    known = registry.supported_extensions() if registry else supported_extensions()
    return [ext for ext in known if ext in UPLOAD_EXTENSIONS]


def upload_cap(ext: str) -> int:
    """The per-file byte cap for an uploadable extension."""
    return MAX_BINARY_UPLOAD_BYTES if ext in BINARY_UPLOAD_EXTENSIONS else MAX_TEXT_UPLOAD_BYTES


def upload_kind(ext: str) -> str:
    """The extractor's registry name for ``ext`` (``markdown``, ``pdf``, …)."""
    spec = spec_for(Path(f"x{ext}"))
    if spec is None:
        raise _unsupported()
    return spec.name


def _bad_filename() -> UiBadRequest:
    return UiBadRequest(
        "X-Brain-Filename must name a file (percent-encoded UTF-8)", code="bad_filename"
    )


def _unsupported(message: str | None = None) -> UiUnsupportedMedia:
    allowed = ", ".join(sorted(UPLOAD_EXTENSIONS))
    return UiUnsupportedMedia(
        message or f"only these file types may be uploaded: {allowed}", code="unsupported_file"
    )


def sanitize_filename(header: str, registry: ExtractorRegistry | None = None) -> SafeName:
    """Reduce ``X-Brain-Filename`` to a safe ``(stem, ext)``.

    Percent-decoded as strict UTF-8 (the raw header must be ASCII), reduced to its
    final component (anything up to the last ``/`` or ``\\`` is dropped), stripped of
    control and format characters, surrounding space and leading dots, and capped at
    :data:`MAX_FILENAME_CHARS` with the extension kept. 400 ``bad_filename`` when
    nothing is left; 415 ``unsupported_file`` when the extension is not uploadable.
    """
    if not header.isascii():
        raise _bad_filename()
    try:
        decoded = unquote(header, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _bad_filename() from exc
    final = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        ch for ch in final if unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    ).strip().lstrip(".").strip()
    if not cleaned:
        raise _bad_filename()
    path = Path(cleaned)
    ext = path.suffix.lower()
    if ext not in uploadable_extensions(registry):
        raise _unsupported()
    stem = path.name[: -len(ext)][: MAX_FILENAME_CHARS - len(ext)]
    if not stem.strip(". "):
        raise _bad_filename()
    return SafeName(stem=stem, ext=ext)


def sniff(path: Path, ext: str) -> None:
    """Refuse (415 ``unsupported_file``) bytes that do not match ``ext``.

    ``%PDF-`` for a PDF; for a ``.docx``, a zip whose central directory holds
    ``word/document.xml`` and passes the zip-bomb bounds; for text and markdown,
    strict UTF-8 with no NUL. Reads at most the file itself, which the per-file cap
    has already bounded; a ``.docx`` is never extracted here.
    """
    if ext == ".pdf":
        _sniff_magic(path, _PDF_MAGIC, "the file is not a PDF")
    elif ext == ".docx":
        _sniff_docx(path)
    elif ext in UPLOAD_EXTENSIONS:
        _sniff_text(path)
    else:
        raise _unsupported()


def _sniff_magic(path: Path, magic: bytes, message: str) -> None:
    with path.open("rb") as handle:
        if handle.read(len(magic)) != magic:
            raise _unsupported(message)


def _sniff_text(path: Path) -> None:
    data = path.read_bytes()
    if b"\x00" in data:
        raise _unsupported("the file is not text (it contains a NUL byte)")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _unsupported("the file is not UTF-8 text") from exc


def _sniff_docx(path: Path) -> None:
    _sniff_magic(path, _ZIP_MAGIC, "the file is not a .docx (not a zip)")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise _unsupported("the file is not a .docx (a damaged zip)") from exc
    if len(members) > DOCX_MAX_MEMBERS:
        raise _unsupported("the .docx holds too many parts to unpack safely")
    if not any(member.filename == _DOCX_BODY for member in members):
        raise _unsupported("the file is not a .docx (no word/document.xml)")
    if sum(member.file_size for member in members) > DOCX_MAX_EXPANDED_BYTES:
        raise _unsupported("the .docx is too large to unpack safely")
    if any(member.file_size > DOCX_MAX_RATIO * member.compress_size for member in members):
        raise _unsupported("the .docx compresses too far to unpack safely")
