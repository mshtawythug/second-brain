"""The document-level image pre-scan that runs before OCR hands a PDF to pdfium (RM-32, #99).

pdfium decodes an image at the size its stream declares, and not every image it decodes is a
page object that :func:`brain.ingest.pdf.embedded_image_pixels` can count: a soft mask
(``/SMask``) or a stencil ``/Mask``, an annotation's appearance stream, a tiling pattern and a
Type3 glyph each draw image XObjects of their own. rev-99 measured two of them: a 1 px image
whose SMask was large counted 1 px and rendered at 520 MB of RSS, and an annotation
appearance image at 565 MB.

So before pdfium opens the file, :func:`scan_document_images` reads every indirect object
pypdf's cross-reference places in the file (the reader ``extract_pdf`` already holds), and
refuses the whole document when any SINGLE image declares more than ``max_image_pixels``
(``/Width`` x ``/Height``). The bound is per image, so a 50-page scan with one page-sized image
a page is unaffected. Only the dictionaries are read; no image is decoded.

**What counts as an image.** Every stream whose dictionary has a ``/Width`` or a ``/Height``,
whatever its ``/Subtype``: pdfium decodes a ``/SMask`` or ``/Mask`` stream with or without
``/Subtype /Image`` (sec-99b). Every ``/SMask`` and ``/Mask`` an image names is also followed
and sized, so a mask is checked even where the cross-reference does not list it.

**The walk's own cost is bounded** (sec-99b measured 401 xref entries aliasing one 40 KB
object at 11.3 s and 638 MB, because pypdf parses and caches each entry separately): two
entries at one file offset refuse the document; the walk stops past
:data:`MAX_SCANNED_OBJECTS` objects, :data:`MAX_SCAN_BYTES` bytes of object spans or
:data:`MAX_SCAN_SECONDS` of wall clock; and every object the scan resolves is dropped from
pypdf's cache again, so the cache does not grow with the walk. Every doubt fails closed: a
missing or non-numeric dimension, any of those budgets, or an error from the walk.

**What remains.** An inline image (``BI ... ID ... EI``) is not a stream object, so the scan
does not see it: on a page pdfium counts it as a page object, but one inside a Type3 glyph
procedure or a tiling pattern's content is in neither count. For ``/JPXDecode`` the codestream
carries its own size, which pdfium may decode instead of the declared one; that was not
measured. And pypdf's own parse of a hostile file predates RM-32. Backlog BL-32 proposes a
process-level memory and time guard for all three.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import IndirectObject, StreamObject

from .ocr import OcrError

#: The most native pixels any one embedded image may declare: 4x the page cap
#: (``brain.ingest.pdf.MAX_PIXELS``), headroom for a real scan's one page-sized image. The
#: per-page sum in ``brain.ingest.pdf`` uses the same bound.
MAX_IMAGE_PIXELS = 280_000_000
#: The most indirect objects the pre-scan reads. A 50-page scan holds a few hundred, and a
#: large born-digital PDF tens of thousands; past the cap the document is refused.
MAX_SCANNED_OBJECTS = 100_000
#: The most bytes of object spans (from one xref offset to the next) the pre-scan parses.
MAX_SCAN_BYTES = 512 * 1024 * 1024
#: The longest the pre-scan may take, before the 600 s OCR deadline even starts.
MAX_SCAN_SECONDS = 10.0

Clock = Callable[[], float]


class ImageTooLargeError(OcrError):
    """A PDF declares an image over the decode bound, or one the scan cannot size or finish."""


def _resolved(value: Any) -> Any:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _dimension(stream: StreamObject, key: str) -> float:
    """A declared image dimension, refused unless it is a positive finite number."""
    value = _resolved(stream.get(key))
    # pypdf's NumberObject / FloatObject subclass int / float; its BooleanObject does not.
    if not isinstance(value, (int, float)):
        raise ImageTooLargeError(f"image {key} missing or not a number")
    if not (math.isfinite(value) and value > 0):
        raise ImageTooLargeError(f"image {key} not a positive size")
    return float(value)


def _is_image(stream: StreamObject) -> bool:
    """A stream pdfium may decode as an image: ``/Subtype /Image``, or any declared size."""
    return (
        "/Width" in stream
        or "/Height" in stream
        or _resolved(stream.get("/Subtype")) == "/Image"
    )


def _check_image(stream: StreamObject, max_image_pixels: int) -> None:
    if _dimension(stream, "/Width") * _dimension(stream, "/Height") > max_image_pixels:
        raise ImageTooLargeError("an embedded image is too large to decode")


def _check_object(obj: Any, max_image_pixels: int) -> None:
    if not isinstance(obj, StreamObject) or not _is_image(obj):
        return
    _check_image(obj, max_image_pixels)
    for key in ("/SMask", "/Mask"):
        # A /Mask may also be a colour-key array, which holds no pixels: only streams count.
        mask = _resolved(obj.get(key))
        if isinstance(mask, StreamObject):
            _check_image(mask, max_image_pixels)


def _object_refs(
    reader: PdfReader, max_objects: int, max_bytes: int
) -> list[IndirectObject]:
    """Every object the cross-reference places in the file, one per distinct offset.

    Refused when two entries share an offset (pypdf would parse and cache that object once per
    entry), past ``max_objects`` entries, or when the objects span more than ``max_bytes``.
    Objects compressed into an object stream (``reader.xref_objStm``) are left out: a stream
    may not live inside an object stream (PDF 1.7, 7.5.7), so no image can be there. An image's
    indirect ``/Width`` that does live there is still read, through :func:`_resolved`.
    """
    entries = [
        (offset, number, generation)
        for generation, table in reader.xref.items()
        for number, offset in table.items()
    ]
    if len(entries) > max_objects:
        raise ImageTooLargeError("too many objects to scan")
    offsets = sorted(offset for offset, _, _ in entries)
    if len(set(offsets)) != len(offsets):
        raise ImageTooLargeError("cross-reference entries alias one offset")
    if offsets and offsets[-1] - offsets[0] > max_bytes:
        raise ImageTooLargeError("objects span too many bytes to scan")
    return [IndirectObject(number, generation, reader) for _, number, generation in sorted(entries)]


def _scan(
    reader: PdfReader, refs: list[IndirectObject], max_image_pixels: int, clock: Clock,
    max_seconds: float,
) -> None:
    cache = reader.resolved_objects
    deadline = clock() + max_seconds
    for ref in refs:
        if clock() > deadline:
            raise ImageTooLargeError("the image pre-scan ran out of time")
        cached = len(cache)
        _check_object(ref.get_object(), max_image_pixels)
        # Drop what this object pulled into pypdf's cache (it is insertion-ordered).
        while len(cache) > cached:
            cache.popitem()


def scan_document_images(
    reader: PdfReader,
    *,
    max_image_pixels: int = MAX_IMAGE_PIXELS,
    max_objects: int = MAX_SCANNED_OBJECTS,
    max_bytes: int = MAX_SCAN_BYTES,
    max_seconds: float = MAX_SCAN_SECONDS,
    clock: Clock = time.monotonic,
) -> None:
    """Refuse ``reader``'s document if any image declares more than ``max_image_pixels``.

    Raises :class:`ImageTooLargeError` for an oversized image, an image whose size cannot be
    read, a walk past any of its budgets, or any error the walk itself raises (its class is
    kept as the cause, for :func:`brain.errors.describe_failure`).
    """
    try:
        refs = _object_refs(reader, max_objects, max_bytes)
        _scan(reader, refs, max_image_pixels, clock, max_seconds)
    except ImageTooLargeError:
        raise
    except (
        PyPdfError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ArithmeticError,
        RecursionError,
    ) as exc:
        raise ImageTooLargeError("the image pre-scan failed") from exc
