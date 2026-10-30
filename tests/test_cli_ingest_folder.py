"""`brain ingest-folder` end to end (RM-33), through the CLI and the 5434 database.

Every folder and ``$BRAIN_HOME`` is in ``tmp_path``; every image is a synthetic PNG
rendered with Pillow. OCR is a fake engine swapped in for
``cli_ingest_folder.ocr_resolver`` with ``monkeypatch.setattr`` (a standard test
double, rule 13), so ``tesseract`` is never run here.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from PIL import Image, ImageDraw, ImageFont
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.drop_folder import IMPORTED_DIR
from brain.ingest.ocr import OcrSetup, OcrTimeoutError
from brain.settings.ocr import OcrSettings
from tests.conftest import CountingEmbedder

SCOPE_REFUSAL = "ingest-folder requires --dir PATH or --configured, and --since Nd"
AUTO = OcrSettings(mode="auto", lang="eng", max_pages=50)
OCR_TEXT = "SYNTHETIC DASHBOARD 7731 quarterly widget totals"


class _Engine:
    """A fake OCR engine: each call pops the next answer (the last one repeats)."""

    name = "fake-ocr"

    def __init__(self, *answers: str | BaseException) -> None:
        self.answers = list(answers) or [OCR_TEXT]
        self.calls = 0

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str:
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _Engine:
    fake = _Engine()
    monkeypatch.setattr(
        "brain.cli_ingest_folder.ocr_resolver",
        lambda env: OcrSetup(settings=AUTO, engine=fake),
    )
    return fake


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    return brain_home


@pytest.fixture
def drop(tmp_path: Path) -> Path:
    folder = tmp_path / "drop"
    folder.mkdir()
    return folder


def _png(folder: Path, name: str, text: str = "synthetic", *, age_s: float = 600.0) -> Path:
    img = Image.new("RGB", (400, 80), "white")
    ImageDraw.Draw(img).text((10, 20), text, fill="black", font=ImageFont.load_default(size=24))
    path = folder / name
    img.save(path, format="PNG")
    return _settled(path, age_s)


def _settled(path: Path, age_s: float = 600.0) -> Path:
    """Backdate ``path`` past the runner's settle window."""
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def _note(folder: Path, name: str = "note.md") -> Path:
    path = folder / name
    path.write_text("# Synthetic note\n\nSome synthetic body text for a drop.\n")
    return _settled(path)


def _configure(home: Path, *tables: str) -> None:
    (home / "drop-folders.toml").write_text("".join(tables), encoding="utf-8")


def _table(path: Path, extra: str = "") -> str:
    return f'[[folder]]\npath = "{path}"\n{extra}\n'


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-folder", "--no-enrich", *args])


def _docs(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.title, d.content, d.content_type, d.sensitivity, d.source_path, s.kind
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind IN ('screenshot', 'manual') AND d.source_path IS NOT NULL
        ORDER BY d.title, d.source_path
        """
    ).fetchall()
    keys = ("title", "content", "content_type", "sensitivity", "source_path", "kind")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _sha8(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


# --- scope ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--configured"],
        ["--since", "7d"],
        ["--configured", "--since", "7"],
        ["--configured", "--since", "0d"],
        ["--configured", "--since", "366d"],
        ["--configured", "--since", "7h"],
    ],
)
def test_missing_or_malformed_scope_is_refused(args: list[str], home: Path) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert SCOPE_REFUSAL in result.output


def test_dir_without_since_is_refused(home: Path, drop: Path) -> None:
    result = _run("--dir", str(drop))

    assert result.exit_code == 2
    assert SCOPE_REFUSAL in result.output


def test_dir_and_configured_together_are_refused(home: Path, drop: Path) -> None:
    result = _run("--dir", str(drop), "--configured", "--since", "7d")

    assert result.exit_code == 2
    assert SCOPE_REFUSAL in result.output


def test_a_bad_sensitivity_is_refused(home: Path, drop: Path) -> None:
    result = _run("--dir", str(drop), "--since", "7d", "--sensitivity", "secret")

    assert result.exit_code == 2
    assert "--sensitivity" in result.output


def test_a_missing_dir_is_refused(home: Path, tmp_path: Path) -> None:
    result = _run("--dir", str(tmp_path / "nowhere"), "--since", "7d")

    assert result.exit_code == 2


# --- configuration -----------------------------------------------------------------------


def test_configured_with_no_file_does_nothing(home: Path) -> None:
    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0
    assert "no drop folders configured" in result.output


def test_a_config_error_exits_1_naming_the_key(home: Path, drop: Path) -> None:
    _configure(home, _table(drop, 'kind = "nope"'))

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "folder[0].kind" in result.output


# --- AC1: a synthetic PNG becomes a screenshot document --------------------------------


def test_a_configured_png_is_ingested_titled_by_its_original_stem(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _png(drop, "Screenshot 2026-10-30 at 09.15.00.png", "dashboard")
    sha8 = _sha8(src)
    _configure(home, _table(drop))

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    final = drop.resolve() / IMPORTED_DIR / f"Screenshot 2026-10-30 at 09.15.00-{sha8}.png"
    assert final.is_file()
    assert not src.exists()
    assert _docs(test_db) == [
        {
            "title": "Screenshot 2026-10-30 at 09.15.00",
            "content": OCR_TEXT,
            "content_type": "screenshot",
            "sensitivity": "confidential",
            "source_path": str(final),
            "kind": "screenshot",
        }
    ]
    assert "ingested  Screenshot 2026-10-30 at 09.15.00.png" in result.output
    assert "1 ingested" in result.output


def test_dir_uses_the_defaults_when_the_folder_is_not_configured(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _png(drop, "shot.png")

    result = _run("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 0, result.output
    (doc,) = _docs(test_db)
    assert (doc["kind"], doc["sensitivity"], doc["content_type"]) == (
        "screenshot", "confidential", "screenshot",
    )


def test_sensitivity_can_be_overridden_for_one_run(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _png(drop, "shot.png")

    result = _run("--dir", str(drop), "--since", "7d", "--sensitivity", "normal")

    assert result.exit_code == 0, result.output
    assert _docs(test_db)[0]["sensitivity"] == "normal"


def test_dir_uses_a_configured_folders_settings_even_when_auto_is_off(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _note(drop)
    _configure(home, _table(drop, 'kind = "manual"\nextensions = [".md"]\nauto = false\n'
                                  'sensitivity = "normal"'))

    result = _run("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 0, result.output
    (doc,) = _docs(test_db)
    assert (doc["kind"], doc["sensitivity"], doc["title"]) == ("manual", "normal", "note")
    assert engine.calls == 0


# --- AC3 / AC4 ---------------------------------------------------------------------------


def test_two_different_files_with_one_name_become_two_documents(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    _png(drop, "New 1.png", "first")
    assert _run("--configured", "--since", "2d").exit_code == 0
    _png(drop, "New 1.png", "second")
    engine.answers = ["a different synthetic screen of text entirely"]

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    assert [d["title"] for d in docs] == ["New 1", "New 1"]
    assert len({d["source_path"] for d in docs}) == 2


def test_the_same_file_dropped_twice_adds_nothing(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    first = _png(drop, "shot.png", "same")
    body = first.read_bytes()
    assert _run("--configured", "--since", "2d").exit_code == 0
    (drop / "shot.png").write_bytes(body)
    _settled(drop / "shot.png")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    assert len(_docs(test_db)) == 1
    assert not (drop / "shot.png").exists()
    assert "duplicate shot.png" in result.output
    assert engine.calls == 1


# --- AC5: a failed ingest returns the file -----------------------------------------------


def test_a_failed_ingest_leaves_the_file_back_in_the_folder(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    src = _png(drop, "shot.png")
    body = src.read_bytes()
    engine.answers = [OcrTimeoutError("tesseract ran past 60s")]

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert src.read_bytes() == body
    assert list((drop / IMPORTED_DIR).iterdir()) == []
    assert "failed    shot.png (ImageOcrError from OcrTimeoutError)" in result.output
    assert _docs(test_db) == []


def test_a_secret_in_the_ocr_text_is_refused_and_the_file_returned(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret guard runs on OCR text like any other body (protocol point 6)."""
    from tests.secret_fixtures import SYNTHETIC_AWS_KEY

    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    _configure(home, _table(drop))
    src = _png(drop, "creds.png")
    engine.answers = [f"aws key {SYNTHETIC_AWS_KEY}"]

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert src.exists()
    assert SYNTHETIC_AWS_KEY not in result.output
    assert "failed    creds.png (SecretGuardError)" in result.output
    assert _docs(test_db) == []


def test_an_image_with_no_text_is_an_empty_document_left_in_imported(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    src = _png(drop, "blank.png", "")
    sha8 = _sha8(src)
    engine.answers = ["   "]

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    assert (drop / IMPORTED_DIR / f"blank-{sha8}.png").is_file()
    assert _docs(test_db) == []
    assert "empty     blank.png" in result.output


# --- AC6 ---------------------------------------------------------------------------------


def test_since_excludes_older_files(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    old = _png(drop, "old.png", age_s=3 * 86400)
    _png(drop, "new.png")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    assert [d["title"] for d in _docs(test_db)] == ["new"]
    assert old.exists()


def test_configured_skips_a_folder_with_auto_off(
    home: Path, tmp_path: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    manual = tmp_path / "manual-only"
    manual.mkdir()
    _configure(home, _table(drop), _table(manual, "auto = false"))
    _png(drop, "auto.png")
    held = _png(manual, "held.png")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    assert [d["title"] for d in _docs(test_db)] == ["auto"]
    assert held.exists()
    assert not (manual / IMPORTED_DIR).exists()


# --- OCR unavailable, unsafe folder, dry run ---------------------------------------------


def test_an_image_folder_without_ocr_is_skipped_with_the_hint(
    home: Path, drop: Path, embedder: CountingEmbedder, monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    monkeypatch.setattr(
        "brain.cli_ingest_folder.ocr_resolver",
        lambda env: OcrSetup(settings=AUTO, engine=None, missing_binary=True),
    )
    _configure(home, _table(drop))
    src = _png(drop, "shot.png")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "brew install tesseract" in result.output
    assert src.exists()
    assert not (drop / IMPORTED_DIR).exists()


def test_a_non_image_folder_runs_without_ocr(
    home: Path, drop: Path, embedder: CountingEmbedder, monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    monkeypatch.setattr(
        "brain.cli_ingest_folder.ocr_resolver",
        lambda env: OcrSetup(settings=OcrSettings("off", "eng", 50), engine=None),
    )
    _note(drop)
    _configure(home, _table(drop, 'kind = "manual"\nextensions = [".md"]'))

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    assert [d["title"] for d in _docs(test_db)] == ["note"]


def test_an_unsafe_imported_refuses_the_folder_and_exits_1(
    home: Path, tmp_path: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (drop / IMPORTED_DIR).symlink_to(elsewhere)
    _configure(home, _table(drop))
    src = _png(drop, "shot.png")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "imported/ is not a real directory" in result.output
    assert src.exists()
    assert list(elsewhere.iterdir()) == []


def test_dry_run_lists_and_touches_nothing(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    src = _png(drop, "shot.png")

    result = _run("--configured", "--since", "2d", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "would ingest  shot.png" in result.output
    assert src.exists()
    assert not (drop / IMPORTED_DIR).exists()
    assert engine.calls == 0
    assert _docs(test_db) == []


def test_the_command_is_registered_through_the_extension_seam() -> None:
    from brain.cli_registry import EXTENSION_REGISTRARS

    assert "ingest-folder" in [name for name, _ in EXTENSION_REGISTRARS]


def test_an_unreadable_folder_is_skipped_by_class_and_exits_1(
    home: Path, drop: Path, engine: _Engine, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, _table(drop))
    _png(drop, "shot.png")
    drop.chmod(0)
    try:
        result = _run("--configured", "--since", "2d")
    finally:
        drop.chmod(0o755)

    assert result.exit_code == 1
    assert "PermissionError[EACCES]" in result.output
    assert (drop / "shot.png").exists()


def test_the_report_says_how_many_files_were_left_for_the_next_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from brain.cli_ingest_folder import _print_report
    from brain.drop_folder import DropReport, FileOutcome

    _print_report(DropReport(tmp_path, (FileOutcome("a.png", "ingested"),), deferred=3))

    assert f"{tmp_path}: 1 ingested; 3 left for the next run" in capsys.readouterr().out


def test_an_empty_run_says_nothing_new(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from brain.cli_ingest_folder import _print_report
    from brain.drop_folder import DropReport

    _print_report(DropReport(tmp_path, (), deferred=0))

    assert f"{tmp_path}: nothing new" in capsys.readouterr().out
