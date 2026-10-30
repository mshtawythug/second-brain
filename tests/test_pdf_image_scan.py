"""The document-level image pre-scan, and the per-page depth fail-closed (RM-32, #99).

rev-99 measured two images the per-page count could not see: a 1 px image whose ``/SMask``
was large (520 MB of RSS to render) and an annotation appearance image (565 MB). aud-99
measured a third: an image nested 15+ forms deep, past pypdfium2's default depth. Every PDF
here is synthetic and written by hand; every large image is DECLARED large but compressed
from zeros with ``zlib.compressobj``, and a test that expects a refusal never decodes it.
"""
from __future__ import annotations

import functools
import logging
import zlib
from pathlib import Path

import pypdfium2
import pytest
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from brain.errors import describe_failure
from brain.ingest.pdf import PageTooLargeError, extract_pdf, render_page_png
from brain.ingest.pdf_image_scan import (
    MAX_IMAGE_PIXELS,
    MAX_SCANNED_OBJECTS,
    ImageTooLargeError,
    scan_document_images,
)
from tests.test_ingest_pdf_ocr import _Engine, _setup
from tests.test_ingest_pdf_ocr_limits import _nested_forms_pdf, _write_pdf

#: The lowered bound every refusal test passes, and the size of the image it declares.
LOW_BOUND = 1_000_000
BIG_SIDE = 3_000  # 9 Mpx, over LOW_BOUND
SMALL_SIDE = 50

PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << "
HEAD = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]


def _gray_image(width: int, height: int, extra: bytes = b"") -> bytes:
    """An image XObject of ``width`` x ``height`` gray pixels, compressed in 1 MB pieces."""
    compressor = zlib.compressobj(9)
    zeros = b"\0" * (1 << 20)
    remaining = width * height
    parts: list[bytes] = []
    while remaining:
        count = min(remaining, len(zeros))
        parts.append(compressor.compress(zeros[:count]))
        remaining -= count
    data = b"".join([*parts, compressor.flush()])
    return (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
    ).encode() + extra + f" /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    return f"<< /Length {len(body)} ".encode() + extra + b" >>\nstream\n" + body + b"\nendstream"


def _smask_pdf(path: Path, mask_side: int) -> Path:
    """A 1x1 image whose ``/SMask`` is ``mask_side`` square: the page count sees 1 px."""
    return _write_pdf(
        path,
        [
            *HEAD,
            PAGE + b"/Im0 4 0 R >> >> /Contents 5 0 R >>",
            _gray_image(1, 1, b"/SMask 6 0 R"),
            _stream(b"q 100 0 0 100 0 0 cm /Im0 Do Q"),
            _gray_image(mask_side, mask_side),
        ],
    )


def _annotation_pdf(path: Path, appearance_side: int) -> Path:
    """A 1x1 decoy image on the page, and an annotation whose appearance draws a big one."""
    return _write_pdf(
        path,
        [
            *HEAD,
            PAGE + b"/Im0 4 0 R >> >> /Contents 5 0 R /Annots [6 0 R] >>",
            _gray_image(1, 1),
            _stream(b"q 100 0 0 100 0 0 cm /Im0 Do Q"),
            b"<< /Type /Annot /Subtype /Stamp /Rect [0 0 300 300] /AP << /N 7 0 R >> >>",
            _stream(
                b"q 300 0 0 300 0 0 cm /Big Do Q",
                b"/Type /XObject /Subtype /Form /BBox [0 0 300 300] "
                b"/Resources << /XObject << /Big 8 0 R >> >>",
            ),
            _gray_image(appearance_side, appearance_side),
        ],
    )


def _images_pdf(path: Path, sides: list[int]) -> Path:
    """One page drawing an image of each side, each under the bound on its own."""
    names = b" ".join(f"/Im{n} {4 + n} 0 R".encode() for n in range(len(sides)))
    draws = b" ".join(f"q 10 0 0 10 0 0 cm /Im{n} Do Q".encode() for n in range(len(sides)))
    contents_number = 4 + len(sides)
    return _write_pdf(
        path,
        [
            *HEAD,
            PAGE + names + f" >> >> /Contents {contents_number} 0 R >>".encode(),
            *[_gray_image(side, side) for side in sides],
            _stream(draws),
        ],
    )


class _RenderSpy:
    """Wraps the real renderer, lowered to ``LOW_BOUND``, and records each call."""

    def __init__(self) -> None:
        self.calls = 0
        self._render = functools.partial(render_page_png, max_image_pixels=LOW_BOUND)

    def __call__(self, page: pypdfium2.PdfPage) -> bytes:
        self.calls += 1
        return self._render(page)


# ---------------------------------------------------------------------------
# through extract_pdf: the bypasses rev-99 measured, and a document within limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build", [_smask_pdf, _annotation_pdf], ids=["smask", "annotation-appearance"]
)
def test_an_image_the_page_count_cannot_see_stops_ocr_before_any_render(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, build: object
) -> None:
    path = build(tmp_path / "doc.pdf", BIG_SIDE)  # type: ignore[operator]
    engine = _Engine([])
    spy = _RenderSpy()

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), render=spy, max_image_pixels=LOW_BOUND)

    assert spy.calls == 0
    assert engine.calls == []
    assert doc.content == ""
    assert caplog.messages == ["OCR skipped a PDF: ImageTooLargeError"]


@pytest.mark.parametrize(
    "build", [_smask_pdf, _annotation_pdf], ids=["smask", "annotation-appearance"]
)
def test_a_document_within_the_bound_still_ocrs(tmp_path: Path, build: object) -> None:
    path = build(tmp_path / "doc.pdf", SMALL_SIDE)  # type: ignore[operator]
    engine = _Engine(["SYNTHETIC TEXT"])

    doc = extract_pdf(path, ocr=_setup(engine), max_image_pixels=LOW_BOUND)

    assert len(engine.calls) == 1
    assert doc.content == "[page 1]\nSYNTHETIC TEXT"


def test_the_bound_is_per_image_not_per_document(tmp_path: Path) -> None:
    """Three images of 640 kpx each pass a 1 Mpx bound: a scan of many pages is unaffected."""
    path = _images_pdf(tmp_path / "three.pdf", [800, 800, 800])

    scan_document_images(PdfReader(str(path)), max_image_pixels=LOW_BOUND)


def test_the_default_bound_is_four_times_the_page_cap() -> None:
    assert MAX_IMAGE_PIXELS == 280_000_000
    assert MAX_SCANNED_OBJECTS == 100_000


# ---------------------------------------------------------------------------
# the scan's own bounds, and failing closed
# ---------------------------------------------------------------------------


def test_the_walk_is_bounded_by_the_object_count(tmp_path: Path) -> None:
    path = _smask_pdf(tmp_path / "doc.pdf", SMALL_SIDE)  # 6 objects

    scan_document_images(PdfReader(str(path)), max_objects=6)
    with pytest.raises(ImageTooLargeError, match="too many objects"):
        scan_document_images(PdfReader(str(path)), max_objects=5)


@pytest.mark.parametrize(
    "dimension",
    [b"", b"/Width /Wide", b"/Width -5", b"/Width 0", b"/Width true", b"/Width (100)"],
    ids=["missing", "name", "negative", "zero", "boolean", "string"],
)
def test_an_image_the_scan_cannot_size_fails_closed(tmp_path: Path, dimension: bytes) -> None:
    image = (
        b"<< /Type /XObject /Subtype /Image " + dimension + b" /Height 10 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\0\nendstream"
    )
    path = _write_pdf(
        tmp_path / "odd.pdf",
        [*HEAD, PAGE + b"/Im0 4 0 R >> >> /Contents 5 0 R >>", image, _stream(b"/Im0 Do")],
    )

    with pytest.raises(ImageTooLargeError, match="Width"):
        scan_document_images(PdfReader(str(path)))


def test_an_indirect_dimension_is_resolved(tmp_path: Path) -> None:
    image = (
        b"<< /Type /XObject /Subtype /Image /Width 6 0 R /Height 10 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\0\nendstream"
    )
    path = _write_pdf(
        tmp_path / "indirect.pdf",
        [*HEAD, PAGE + b"/Im0 4 0 R >> >> /Contents 5 0 R >>", image, _stream(b"/Im0 Do"), b"4000"],
    )

    scan_document_images(PdfReader(str(path)), max_image_pixels=40_000)
    with pytest.raises(ImageTooLargeError, match="too large"):
        scan_document_images(PdfReader(str(path)), max_image_pixels=39_999)


class _BrokenReader:
    """A reader whose cross-reference lists one object that cannot be read."""

    xref = {0: {1: 0}}

    def get_object(self, ref: object) -> object:
        raise PdfReadError("synthetic unreadable object")


def test_a_walk_that_raises_fails_closed() -> None:
    with pytest.raises(ImageTooLargeError) as caught:
        scan_document_images(_BrokenReader())  # type: ignore[arg-type]

    assert describe_failure(caught.value) == "ImageTooLargeError from PdfReadError"


# ---------------------------------------------------------------------------
# per page: aud-99's nested image, below pypdfium2's default depth
# ---------------------------------------------------------------------------


def _render_first_page(path: Path, max_image_pixels: int) -> bytes:
    document = pypdfium2.PdfDocument(str(path))
    try:
        return render_page_png(document[0], max_image_pixels=max_image_pixels)
    finally:
        document.close()


@pytest.mark.parametrize("depth", [15, 20, 30])
def test_an_image_nested_past_the_default_depth_is_counted_and_refused(
    tmp_path: Path, depth: int
) -> None:
    """aud-99: at 220d985 a 100x100 image 15+ forms deep was not counted, and was rendered."""
    path = _nested_forms_pdf(tmp_path / "deep.pdf", depth, image_side=100)

    with pytest.raises(PageTooLargeError, match="embedded images"):
        _render_first_page(path, max_image_pixels=5_000)


def test_a_shallow_image_within_the_bound_renders(tmp_path: Path) -> None:
    path = _nested_forms_pdf(tmp_path / "shallow.pdf", 3, image_side=100)

    assert _render_first_page(path, max_image_pixels=MAX_IMAGE_PIXELS).startswith(b"\x89PNG")
