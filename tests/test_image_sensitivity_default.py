"""RM-33 review ruling M2: an image is ``confidential`` by default on every ingest path.

``brain ingest x.png`` and ``brain ingest-dir --ext png`` take the image extractor's
default tier (``dispatch.default_sensitivity``) when ``--sensitivity`` is not given; an
explicit ``--sensitivity normal`` is honoured, and other file types keep ``normal``.
OCR is a fake engine swapped in for ``brain.ingest.image.resolve_ocr`` with
``monkeypatch.setattr``; every image is a synthetic PNG rendered with Pillow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from PIL import Image, ImageDraw, ImageFont
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import dispatch
from brain.ingest.ocr import OcrSetup
from brain.settings.ocr import OcrSettings
from tests.conftest import CountingEmbedder

OCR_TEXT = "SYNTHETIC SCREEN 8812 of widget totals"


class _Engine:
    name = "fake-ocr"

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str:
        return OCR_TEXT


@pytest.fixture(autouse=True)
def _ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = OcrSetup(settings=OcrSettings("auto", "eng", 50), engine=_Engine())
    monkeypatch.setattr("brain.ingest.image.resolve_ocr", lambda env: setup)


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _png(path: Path) -> Path:
    img = Image.new("RGB", (300, 60), "white")
    ImageDraw.Draw(img).text((5, 10), "x", fill="black", font=ImageFont.load_default(size=20))
    img.save(path, format="PNG")
    return path


def _sensitivity(conn: psycopg.Connection[Any], name: str) -> str:
    """The stored tier of the document ingested from the file named ``name``."""
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE source_path LIKE %s", (f"%/{name}",)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_the_image_extractor_defaults_to_confidential() -> None:
    assert dispatch.default_sensitivity(Path("a.PNG")) == "confidential"
    assert dispatch.default_sensitivity(Path("a.jpeg")) == "confidential"
    assert dispatch.default_sensitivity(Path("a.md")) == "normal"
    assert dispatch.default_sensitivity(Path("a.unknown")) == "normal"


def test_brain_ingest_of_a_png_is_confidential_by_default(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    result = CliRunner().invoke(app, ["ingest", "--no-enrich", str(_png(tmp_path / "shot.png"))])

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, "shot.png") == "confidential"


def test_an_explicit_sensitivity_normal_is_honoured(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    path = _png(tmp_path / "shot.png")

    result = CliRunner().invoke(
        app, ["ingest", "--no-enrich", "--sensitivity", "normal", str(path)]
    )

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, "shot.png") == "normal"


def test_brain_ingest_of_markdown_stays_normal(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    path = tmp_path / "note.md"
    path.write_text("# Synthetic\n\nA synthetic note body.\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["ingest", "--no-enrich", str(path)])

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, "note.md") == "normal"


def test_ingest_dir_gives_images_confidential_and_the_rest_normal(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    _png(tmp_path / "shot.png")
    (tmp_path / "note.md").write_text("# Synthetic\n\nA synthetic note body.\n")

    result = CliRunner().invoke(
        app, ["ingest-dir", "--no-enrich", "--ext", "png,md", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, "shot.png") == "confidential"
    assert _sensitivity(test_db, "note.md") == "normal"


def test_ingest_dir_with_an_explicit_level_applies_it_to_images(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    _png(tmp_path / "shot.png")

    result = CliRunner().invoke(
        app,
        ["ingest-dir", "--no-enrich", "--ext", "png", "--sensitivity", "normal", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, "shot.png") == "normal"


def test_ingest_dir_still_refuses_a_bad_level_before_any_write(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    _png(tmp_path / "shot.png")

    result = CliRunner().invoke(
        app, ["ingest-dir", "--ext", "png", "--sensitivity", "secret", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert test_db.execute("SELECT count(*) FROM documents").fetchone() == (0,)


def test_brain_ingest_of_a_symlinked_png_is_a_one_line_refusal(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    """sec-105 nit: ELOOP from the O_NOFOLLOW open is a refusal by class, exit 1."""
    real = _png(tmp_path / "real.png")
    link = tmp_path / "link.png"
    link.symlink_to(real)

    result = CliRunner().invoke(app, ["ingest", "--no-enrich", str(link)])

    assert result.exit_code == 1
    assert "cannot read link.png: OSError[ELOOP]" in result.output
    assert str(tmp_path) not in result.output
    assert isinstance(result.exception, SystemExit)
    assert test_db.execute("SELECT count(*) FROM documents").fetchone() == (0,)


def test_brain_ingest_of_an_unreadable_png_is_a_one_line_refusal(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection[Any]
) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")

    result = CliRunner().invoke(app, ["ingest", "--no-enrich", str(path)])

    assert result.exit_code == 1
    assert (
        "cannot read broken.png: UnreadableImageError: not a readable PNG or JPEG image"
        in result.output
    )
    assert isinstance(result.exception, SystemExit)
