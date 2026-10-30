"""Image extractor: OCR a PNG or JPEG into a ``screenshot`` document (RM-33).

Registered in :mod:`brain.ingest.dispatch` for ``.png``, ``.jpg`` and ``.jpeg`` with
``in_directory_walks=False``, so ``brain ingest-dir``'s default walk is unchanged. Files
normally arrive through the drop-folder runner (:mod:`brain.drop_folder`). Text comes
from RM-32's :class:`~brain.ingest.ocr.OcrEngine`; nothing leaves the machine.

**The bounds, from the outside in** (the same discipline as ``ingest/pdf.py``):

1. With OCR off or ``tesseract`` missing, :class:`OcrUnavailableError` is raised before
   the file is opened: there is nothing to read it with, and an empty document would
   consume the file.
2. A file over ``max_file_bytes`` is refused before Pillow opens it.
3. Only the PNG and JPEG decoders are reachable (``formats=``), whatever the bytes are.
   Pillow's own decompression-bomb check runs inside ``Image.open``; its error, and its
   warning when a caller raises warnings as errors, mean "too large". The global
   ``Image.MAX_IMAGE_PIXELS`` is never changed.
4. The declared size is capped from the header, before ``load()`` decodes anything:
   at most :data:`MAX_PIXELS`, each side 1 to :data:`MAX_SIDE_PX`.
5. The image is decoded, converted to grayscale and re-encoded as PNG, so ``tesseract``
   never parses the untrusted file (and EXIF and other metadata are dropped).
6. OCR runs with ``min(PAGE_TIMEOUT_S, timeout)``; the runner passes what is left of
   its own deadline.

**Errors carry no content.** Every class here is a :class:`ValueError` (so ingest
callers that treat a malformed file as a skip keep doing so) with a fixed message;
a Pillow or OCR exception is kept only as ``__cause__``, which
:func:`brain.errors.describe_failure` prints by class.
"""
from __future__ import annotations

import io
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import ExtractedDoc
from .ocr import PAGE_TIMEOUT_S, OcrError, OcrSetup, resolve_ocr

if TYPE_CHECKING:
    from PIL import Image as PilImage

#: ``documents.content_type`` of an OCR'd image.
CONTENT_TYPE = "screenshot"
#: The decoders an image file may reach.
FORMATS = ("PNG", "JPEG")
#: A file larger than this is refused unread. A 6K screenshot is a few MiB.
MAX_FILE_BYTES = 50 * 1024 * 1024
#: The most pixels a decoded image may hold: ``ingest/pdf.py``'s page bitmap cap, and
#: below Pillow's own warning threshold (about 89 Mpx), which this module never raises.
MAX_PIXELS = 70_000_000
#: The longest side, in pixels: tesseract's own limit (as in ``ingest/pdf.py``).
MAX_SIDE_PX = 32_767

ImageOpener = Callable[[Path], "PilImage.Image"]


class ImageError(ValueError):
    """An image could not be turned into text. The message never carries content."""


class ImageTooLargeError(ImageError):
    """The file, its declared pixel count or a side is over the bound."""


class UnreadableImageError(ImageError):
    """The file is not a PNG or JPEG Pillow can decode."""


class OcrUnavailableError(ImageError):
    """OCR is off, or ``tesseract`` is not on ``PATH``. The message says how to turn it on."""


class ImageOcrError(ImageError):
    """The OCR engine failed on this image (its ``OcrError`` is the cause)."""


#: The two ways OCR can be unavailable, and how to fix each.
OCR_OFF_HINT = "OCR is off: set BRAIN_OCR=auto to read images"
OCR_MISSING_HINT = (
    "the tesseract binary is not on PATH: brew install tesseract (BRAIN_OCR=auto reads "
    "images once it is)"
)


def ocr_unavailable_hint(setup: OcrSetup) -> str | None:
    """Why images cannot be read with ``setup``, and the fix; ``None`` when they can."""
    if setup.engine is not None:
        return None
    return OCR_MISSING_HINT if setup.settings.enabled else OCR_OFF_HINT


def _open_restricted(path: Path) -> PilImage.Image:
    """``Image.open`` limited to :data:`FORMATS`; reads the header only."""
    from PIL import Image

    return Image.open(path, formats=FORMATS)


def _check_size(width: int, height: int, max_pixels: int) -> None:
    if min(width, height) < 1:
        raise UnreadableImageError("image has no pixels")
    if max(width, height) > MAX_SIDE_PX:
        raise ImageTooLargeError("image side too long to OCR")
    if width * height > max_pixels:
        raise ImageTooLargeError("image too large to decode")


def _grayscale_png(
    path: Path, *, open_image: ImageOpener, max_pixels: int
) -> tuple[bytes, dict[str, Any]]:
    """Decode ``path`` within the caps; return a grayscale PNG and ``metadata.image``."""
    from PIL import Image

    try:
        with open_image(path) as img:
            width, height = img.size
            _check_size(width, height, max_pixels)
            fmt = img.format
            gray = img.convert("L")  # decodes here, after the cap
        buffer = io.BytesIO()
        gray.save(buffer, format="PNG")
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError) as exc:
        raise ImageTooLargeError("image too large to decode") from exc
    except (OSError, SyntaxError, ValueError, EOFError, struct.error) as exc:
        # UnidentifiedImageError is an OSError; a broken PNG chunk raises SyntaxError.
        raise UnreadableImageError("not a readable PNG or JPEG image") from exc
    return buffer.getvalue(), {"width": width, "height": height, "format": fmt}


def extract_image(
    path: Path,
    *,
    ocr: OcrSetup | None = None,
    timeout: float = PAGE_TIMEOUT_S,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_pixels: int = MAX_PIXELS,
    open_image: ImageOpener = _open_restricted,
) -> ExtractedDoc:
    """OCR one image into an :class:`ExtractedDoc` (``content_type="screenshot"``).

    ``ocr`` is resolved from ``os.environ`` when not given (the extractor registry passes
    only a path, as for PDFs). No recognised text is an empty document, which ingest
    skips. Raises :class:`ImageError` subclasses (all ``ValueError``) as the module
    docstring describes, and ``OSError`` when the file cannot be stat'ed.
    ``timeout``, ``max_file_bytes``, ``max_pixels`` and ``open_image`` are seams.
    """
    setup = ocr if ocr is not None else resolve_ocr(os.environ)
    if setup.engine is None:
        raise OcrUnavailableError(OCR_MISSING_HINT if setup.settings.enabled else OCR_OFF_HINT)
    if Path(path).stat().st_size > max_file_bytes:
        raise ImageTooLargeError("image file too large")
    png, image_meta = _grayscale_png(Path(path), open_image=open_image, max_pixels=max_pixels)
    try:
        text = setup.engine.image_to_text(
            png, lang=setup.settings.lang, timeout=min(PAGE_TIMEOUT_S, timeout)
        )
    except OcrError as exc:
        raise ImageOcrError("OCR failed on the image") from exc
    return ExtractedDoc(
        title=Path(path).stem,
        content=text.strip(),
        content_type=CONTENT_TYPE,
        source_path=str(Path(path).resolve()),
        metadata={"ocr": {"engine": setup.engine.name}, "image": image_meta},
    )
