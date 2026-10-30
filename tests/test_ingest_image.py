"""Tests for the image extractor, :mod:`brain.ingest.image` (RM-33).

Every image is synthetic: rendered with Pillow, or a hand-built header that declares
a size the file does not hold. OCR is a fake engine except in the one test that
needs the real ``tesseract``, which is skipped when the binary is absent.
"""
from __future__ import annotations

import io
import shutil
import struct
import warnings
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from brain.errors import describe_failure
from brain.ingest import image as image_mod
from brain.ingest.image import (
    CONTENT_TYPE,
    MAX_PIXELS,
    MAX_SIDE_PX,
    ImageOcrError,
    ImageTooLargeError,
    OcrUnavailableError,
    UnreadableImageError,
    extract_image,
    ocr_unavailable_hint,
)
from brain.ingest.ocr import PAGE_TIMEOUT_S, OcrSetup, OcrTimeoutError, TesseractOcr
from brain.settings.ocr import OcrSettings

AUTO = OcrSettings(mode="auto", lang="eng", max_pages=50)
OFF = OcrSettings(mode="off", lang="eng", max_pages=50)
SYNTHETIC_TEXT = "SYNTHETIC RECEIPT 4521"


class _Engine:
    """A fake OCR engine: answers each call from ``answers``, recording what it got."""

    name = "fake-ocr"

    def __init__(self, answers: Sequence[str | BaseException] = (SYNTHETIC_TEXT,)) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[bytes, str, float]] = []

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str:
        self.calls.append((png, lang, timeout))
        answer = self._answers[len(self.calls) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _setup(engine: _Engine | None = None, settings: OcrSettings = AUTO, **kw: bool) -> OcrSetup:
    return OcrSetup(settings=settings, engine=engine, **kw)


def _text_png(path: Path, text: str = SYNTHETIC_TEXT, *, fmt: str = "PNG") -> Path:
    """A white RGB image with ``text`` drawn large in black."""
    img = Image.new("RGB", (900, 120), "white")
    font = ImageFont.load_default(size=48)
    ImageDraw.Draw(img).text((20, 30), text, fill="black", font=font)
    img.save(path, format=fmt)
    return path


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _header_only_png(path: Path, width: int, height: int) -> Path:
    """A PNG whose IHDR declares ``width`` x ``height`` but whose data is a few bytes.

    Opening it reads only the header; decoding it would fail, so an error that is
    ``ImageTooLargeError`` (not ``UnreadableImageError``) proves the cap ran first.
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 8))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(body)
    return path


def _jpeg_with_declared_size(path: Path, width: int, height: int) -> Path:
    """A small real JPEG whose SOF0 marker is patched to declare ``width`` x ``height``."""
    buffer = io.BytesIO()
    Image.new("L", (16, 16), 255).save(buffer, format="JPEG", optimize=False, progressive=False)
    data = bytearray(buffer.getvalue())
    sof = data.index(b"\xff\xc0")
    # SOF0: marker(2) length(2) precision(1) height(2) width(2)
    data[sof + 5 : sof + 7] = struct.pack(">H", height)
    data[sof + 7 : sof + 9] = struct.pack(">H", width)
    path.write_bytes(bytes(data))
    return path


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "fmt"), [("shot.png", "PNG"), ("shot.jpg", "JPEG")])
def test_an_image_becomes_a_screenshot_document(tmp_path: Path, name: str, fmt: str) -> None:
    path = _text_png(tmp_path / name, fmt=fmt)
    engine = _Engine(["  " + SYNTHETIC_TEXT + "\n\n"])

    doc = extract_image(path, ocr=_setup(engine))

    assert doc.title == "shot"
    assert doc.content == SYNTHETIC_TEXT
    assert doc.content_type == CONTENT_TYPE == "screenshot"
    assert doc.source_path == str(path.resolve())
    assert doc.metadata == {
        "ocr": {"engine": "fake-ocr"},
        "image": {"width": 900, "height": 120, "format": fmt},
    }


def test_tesseract_gets_a_grayscale_png_reencoded_by_us(tmp_path: Path) -> None:
    """tesseract never parses the untrusted file: it gets a fresh grayscale PNG."""
    path = _text_png(tmp_path / "shot.jpg", fmt="JPEG")
    engine = _Engine()

    extract_image(path, ocr=_setup(engine))

    png, lang, timeout = engine.calls[0]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(png)) as sent:
        assert sent.mode == "L"
        assert sent.size == (900, 120)
    assert lang == "eng"
    assert timeout == PAGE_TIMEOUT_S


def test_the_ocr_timeout_is_cut_to_the_callers_budget(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    engine = _Engine()

    extract_image(path, ocr=_setup(engine), timeout=7.5)

    assert engine.calls[0][2] == 7.5


def test_the_ocr_timeout_never_exceeds_the_page_bound(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    engine = _Engine()

    extract_image(path, ocr=_setup(engine), timeout=PAGE_TIMEOUT_S * 10)

    assert engine.calls[0][2] == PAGE_TIMEOUT_S


def test_no_recognised_text_is_an_empty_document(tmp_path: Path) -> None:
    """OCR ran and read nothing: the spec's empty document, not an error."""
    path = _text_png(tmp_path / "blank.png", text="")
    doc = extract_image(path, ocr=_setup(_Engine([" \n\t "])))

    assert doc.content == ""
    assert doc.content_type == "screenshot"


def test_the_lang_knob_reaches_the_engine(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    engine = _Engine()

    extract_image(path, ocr=_setup(engine, OcrSettings(mode="auto", lang="deu", max_pages=50)))

    assert engine.calls[0][1] == "deu"


def test_a_non_rgb_mode_is_converted(tmp_path: Path) -> None:
    path = tmp_path / "palette.png"
    Image.new("P", (40, 20), 3).save(path, format="PNG")
    engine = _Engine()

    extract_image(path, ocr=_setup(engine))

    with Image.open(io.BytesIO(engine.calls[0][0])) as sent:
        assert sent.mode == "L"


# ---------------------------------------------------------------------------
# OCR unavailable or failing
# ---------------------------------------------------------------------------


def test_ocr_off_is_an_error_not_an_empty_document(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")

    with pytest.raises(OcrUnavailableError) as err:
        extract_image(path, ocr=_setup(None, OFF))

    assert "BRAIN_OCR=auto" in str(err.value)


def test_a_missing_tesseract_is_an_error_with_the_install_hint(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")

    with pytest.raises(OcrUnavailableError) as err:
        extract_image(path, ocr=_setup(None, missing_binary=True))

    assert "brew install tesseract" in str(err.value)


def test_ocr_unavailable_is_checked_before_the_file_is_opened(tmp_path: Path) -> None:
    """With no engine, the untrusted file is never handed to Pillow."""
    path = tmp_path / "garbage.png"
    path.write_bytes(b"not an image")

    with pytest.raises(OcrUnavailableError):
        extract_image(path, ocr=_setup(None, OFF))


def test_ocr_settings_come_from_the_environment_when_not_passed(tmp_path: Path) -> None:
    """``tests/ocr_isolation_plugin.py`` sets ``BRAIN_OCR=off`` for every test."""
    path = _text_png(tmp_path / "shot.png")

    with pytest.raises(OcrUnavailableError):
        extract_image(path)


def test_the_hint_names_each_state() -> None:
    assert ocr_unavailable_hint(_setup(_Engine())) is None
    off = ocr_unavailable_hint(_setup(None, OFF))
    missing = ocr_unavailable_hint(_setup(None, missing_binary=True))
    assert off is not None and "BRAIN_OCR=auto" in off
    assert missing is not None and "brew install tesseract" in missing


def test_an_ocr_failure_is_wrapped_without_the_text(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    engine = _Engine([OcrTimeoutError("tesseract ran past 60s")])

    with pytest.raises(ImageOcrError) as err:
        extract_image(path, ocr=_setup(engine))

    assert isinstance(err.value, ValueError)
    assert describe_failure(err.value) == "ImageOcrError from OcrTimeoutError"
    assert SYNTHETIC_TEXT not in str(err.value)


# ---------------------------------------------------------------------------
# hostile and malformed files
# ---------------------------------------------------------------------------


def test_a_file_over_the_byte_cap_is_refused_before_pillow_opens_it(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    opened: list[Path] = []

    def opener(p: Path) -> Image.Image:
        opened.append(p)
        raise AssertionError("Pillow must not open an oversized file")

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()), max_file_bytes=100, open_image=opener)

    assert opened == []


def test_a_file_at_the_byte_cap_is_read(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")
    size = path.stat().st_size

    doc = extract_image(path, ocr=_setup(_Engine()), max_file_bytes=size)

    assert doc.content == SYNTHETIC_TEXT


def test_a_header_over_the_pixel_cap_is_refused_before_decode(tmp_path: Path) -> None:
    # 8,500 x 8,500 = 72.25 Mpx: over our 70 Mpx cap, under Pillow's own 89 Mpx warning.
    path = _header_only_png(tmp_path / "big.png", 8_500, 8_500)
    engine = _Engine()

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(engine))

    assert engine.calls == []


def test_an_image_at_the_pixel_cap_is_read(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")

    doc = extract_image(path, ocr=_setup(_Engine()), max_pixels=900 * 120)

    assert doc.content == SYNTHETIC_TEXT


def test_an_image_one_pixel_over_a_lowered_cap_is_refused(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()), max_pixels=900 * 120 - 1)


def test_the_default_pixel_cap_is_below_pillows_bomb_limit() -> None:
    assert MAX_PIXELS == 70_000_000
    assert Image.MAX_IMAGE_PIXELS is not None
    assert MAX_PIXELS < Image.MAX_IMAGE_PIXELS


def test_a_side_over_tesseracts_limit_is_refused(tmp_path: Path) -> None:
    # 40,000 x 100 is only 4 Mpx, but a side tesseract cannot take.
    path = _header_only_png(tmp_path / "thin.png", MAX_SIDE_PX + 1, 100)

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_side_at_tesseracts_limit_passes_the_cap(tmp_path: Path) -> None:
    """At the limit, the cap lets it through; the 8-byte data then fails to decode."""
    path = _header_only_png(tmp_path / "thin.png", MAX_SIDE_PX, 100)

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(_Engine()))


def test_pillows_decompression_bomb_error_is_too_large(tmp_path: Path) -> None:
    # 100,000 x 100,000 is past twice Pillow's limit: Image.open raises inside itself.
    path = _header_only_png(tmp_path / "bomb.png", 100_000, 100_000)

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()))


def test_pillows_bomb_warning_raised_as_an_error_is_too_large(tmp_path: Path) -> None:
    """Under ``-W error`` Pillow's warning (89-179 Mpx) is raised; it still maps."""
    path = _header_only_png(tmp_path / "warn.png", 10_000, 10_000)

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(ImageTooLargeError):
            extract_image(path, ocr=_setup(_Engine()))


def test_a_jpeg_declaring_a_huge_size_is_refused(tmp_path: Path) -> None:
    path = _jpeg_with_declared_size(tmp_path / "big.jpg", 9_000, 9_000)

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_jpeg_declaring_the_largest_size_is_refused(tmp_path: Path) -> None:
    path = _jpeg_with_declared_size(tmp_path / "huge.jpg", 65_535, 65_535)

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_gif_named_png_is_unreadable(tmp_path: Path) -> None:
    """Only the PNG and JPEG decoders are reachable, whatever the bytes are."""
    path = tmp_path / "shot.png"
    Image.new("L", (10, 10), 0).save(path, format="GIF")

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_webp_named_jpg_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "shot.jpg"
    Image.new("RGB", (10, 10), "white").save(path, format="WEBP")

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_truncated_png_is_unreadable(tmp_path: Path) -> None:
    whole = _text_png(tmp_path / "whole.png").read_bytes()
    path = tmp_path / "cut.png"
    path.write_bytes(whole[: len(whole) // 2])
    engine = _Engine()

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(engine))

    assert engine.calls == []


def test_garbage_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_zero_byte_file_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(b"")

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(_Engine()))


def test_a_missing_file_is_an_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        extract_image(tmp_path / "gone.png", ocr=_setup(_Engine()))


@pytest.mark.parametrize(
    "error",
    [OSError("bad"), SyntaxError("bad"), ValueError("bad"), EOFError(), struct.error("bad")],
)
def test_every_decoder_failure_is_unreadable_with_a_fixed_message(
    tmp_path: Path, error: Exception
) -> None:
    path = _text_png(tmp_path / "shot.png")

    def opener(p: Path) -> Image.Image:
        raise error

    with pytest.raises(UnreadableImageError) as err:
        extract_image(path, ocr=_setup(_Engine()), open_image=opener)

    assert "bad" not in str(err.value)


def test_a_memory_error_while_decoding_is_too_large(tmp_path: Path) -> None:
    path = _text_png(tmp_path / "shot.png")

    def opener(p: Path) -> Image.Image:
        raise MemoryError

    with pytest.raises(ImageTooLargeError):
        extract_image(path, ocr=_setup(_Engine()), open_image=opener)


def test_errors_are_value_errors_so_ingest_callers_catch_them() -> None:
    for cls in (ImageTooLargeError, UnreadableImageError, OcrUnavailableError, ImageOcrError):
        assert issubclass(cls, image_mod.ImageError)
        assert issubclass(cls, ValueError)


def test_the_global_pillow_limit_is_not_changed(tmp_path: Path) -> None:
    before = Image.MAX_IMAGE_PIXELS
    extract_image(_text_png(tmp_path / "shot.png"), ocr=_setup(_Engine()))
    assert before == Image.MAX_IMAGE_PIXELS


# ---------------------------------------------------------------------------
# the real engine
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_real_tesseract_reads_a_rendered_synthetic_png(tmp_path: Path) -> None:
    binary = shutil.which("tesseract")
    assert binary is not None
    path = _text_png(tmp_path / "shot.png")

    doc = extract_image(path, ocr=_setup(TesseractOcr(binary=binary)))  # type: ignore[arg-type]

    assert "SYNTHETIC" in doc.content.upper()
    assert doc.metadata["ocr"] == {"engine": "tesseract"}


def test_an_image_with_no_pixels_is_unreadable(tmp_path: Path) -> None:
    """Pillow's PNG reader refuses a zero side itself; the cap does not rely on that."""
    path = _text_png(tmp_path / "shot.png")
    engine = _Engine()

    with pytest.raises(UnreadableImageError):
        extract_image(path, ocr=_setup(engine), open_image=lambda p: Image.new("L", (0, 10)))

    assert engine.calls == []
