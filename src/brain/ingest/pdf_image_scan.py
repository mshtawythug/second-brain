"""The document-level image pre-scan that runs before OCR hands a PDF to pdfium (RM-32, #99).

pdfium decodes an image at the size its stream declares, and not every image it decodes is a
page object that :func:`brain.ingest.pdf.embedded_image_pixels` can count: a soft mask
(``/SMask``) or a stencil ``/Mask``, an annotation's appearance stream, a tiling pattern and a
Type3 glyph each draw image XObjects of their own. rev-99 measured two of them: a 1 px image
whose SMask was large counted 1 px and rendered at 520 MB of RSS, and an annotation
appearance image at 565 MB.

So before pdfium opens the file, :func:`scan_document_images` reads every indirect object
pypdf's cross-reference knows (the reader ``extract_pdf`` already holds), and refuses the
whole document when any SINGLE image stream declares more than ``max_image_pixels``
(``/Width`` x ``/Height``). The bound is per image, so a 50-page scan with one page-sized image
a page is unaffected. Only the dictionaries are read; no image is decoded. The walk is capped
at :data:`MAX_SCANNED_OBJECTS`, so it cannot become a CPU sink. Every doubt fails closed: a
missing or non-numeric dimension, too many objects, or an error from the walk.

**What remains.** An inline image (``BI ... ID ... EI``) is not a stream object, so the scan
does not see it: on a page pdfium counts it as a page object, but one inside a Type3 glyph
procedure or a tiling pattern's content is in neither count. And for ``/JPXDecode`` the
codestream carries its own size, which pdfium may decode instead of the declared one; that was
not measured. Backlog BL-32 proposes a process-level memory guard for both.
"""
from __future__ import annotations

import math
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


class ImageTooLargeError(OcrError):
    """A PDF declares an image over the decode bound, or one the scan cannot size."""


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


def _object_refs(reader: PdfReader, max_objects: int) -> list[IndirectObject]:
    """Every object pypdf's cross-reference places in the file, refused past ``max_objects``.

    Objects compressed into an object stream (``reader.xref_objStm``) are left out: a stream
    may not live inside an object stream (PDF 1.7, 7.5.7), so no image can be there. An image's
    indirect ``/Width`` that does live there is still read, through :func:`_resolved`.
    """
    refs = [
        IndirectObject(number, generation, reader)
        for generation, entries in reader.xref.items()
        for number in entries
    ]
    if len(refs) > max_objects:
        raise ImageTooLargeError("too many objects to scan")
    return refs


def _check_object(obj: Any, max_image_pixels: int) -> None:
    if not isinstance(obj, StreamObject) or _resolved(obj.get("/Subtype")) != "/Image":
        return
    if _dimension(obj, "/Width") * _dimension(obj, "/Height") > max_image_pixels:
        raise ImageTooLargeError("an embedded image is too large to decode")


def scan_document_images(
    reader: PdfReader,
    *,
    max_image_pixels: int = MAX_IMAGE_PIXELS,
    max_objects: int = MAX_SCANNED_OBJECTS,
) -> None:
    """Refuse ``reader``'s document if any image stream declares more than ``max_image_pixels``.

    Raises :class:`ImageTooLargeError` for an oversized image, an image whose size cannot be
    read, more than ``max_objects`` objects, or any error the walk itself raises (its class is
    kept as the cause, for :func:`brain.errors.describe_failure`).
    """
    try:
        for ref in _object_refs(reader, max_objects):
            _check_object(ref.get_object(), max_image_pixels)
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
