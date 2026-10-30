"""`brain capture import` and capture folders under `brain ingest-folder` (RM-39), end to end.

Every drop folder, vault and ``$BRAIN_HOME`` is in ``tmp_path``; every share is
synthetic. The embedder is the fake one through ``brain.cli._build_embedder``
(``patch_embedder``), and OCR is never needed (a capture folder takes text files).
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain import capture_import
from brain import vault as vault_module
from brain.capture import make_capture_title
from brain.cli import app
from brain.config import DEFAULT_CAPTURE_TITLE_WORDS
from brain.drop_folder import IMPORTED_DIR
from brain.ingest.ocr import OcrSetup
from brain.settings.ocr import OcrSettings
from tests.conftest import CountingEmbedder

REFUSAL = "capture import requires --dir PATH and --since Nd"
URL = "https://example.com/notes/synthetic-sprocket"
SHARE = f"Compare this with the sprocket plan\n\n{URL}\n"
PRIVATE_BODY = f"Private synthetic thought\n\n{URL}"
CONFIDENTIAL_SHARE = f"---\nsensitivity: confidential\n---\n{PRIVATE_BODY}\n"


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
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    vault_module.init_vault(root)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(root))
    return root


@pytest.fixture
def drop(tmp_path: Path) -> Path:
    folder = tmp_path / "iCloud-Save-to-Brain"
    folder.mkdir()
    return folder


@pytest.fixture
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ingest-folder`` resolves OCR for every run; a fake keeps ``tesseract`` out of it."""
    off = OcrSettings(mode="off", lang="eng", max_pages=50)
    monkeypatch.setattr("brain.cli_ingest_folder.ocr_resolver", lambda env: OcrSetup(off, None))


def _share(folder: Path, name: str, text: str | bytes, *, age_s: float = 600.0) -> Path:
    path = folder / name
    if isinstance(text, str):
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(text)
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def _import(*args: str) -> Result:
    return CliRunner().invoke(app, ["capture", "import", *args])


def _configure(home: Path, *tables: str) -> None:
    (home / "drop-folders.toml").write_text("".join(tables), encoding="utf-8")


def _notes(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT title, kind, tags, sensitivity, content, vault_path "
        "FROM documents ORDER BY title, vault_path"
    ).fetchall()
    keys = ("title", "kind", "tags", "sensitivity", "content", "vault_path")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _files(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


# --- scope ------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [[], ["--since", "7d"]])
def test_a_missing_dir_is_refused(home: Path, args: list[str]) -> None:
    result = _import(*args)

    assert result.exit_code == 2
    assert REFUSAL in result.output


@pytest.mark.parametrize("since", [None, "7", "0d", "366d", "7 d"])
def test_a_missing_or_malformed_since_is_refused(
    home: Path, drop: Path, since: str | None
) -> None:
    result = _import("--dir", str(drop), *([] if since is None else ["--since", since]))

    assert result.exit_code == 2
    assert REFUSAL in result.output


def test_an_ingest_folder_is_refused(home: Path, drop: Path) -> None:
    _configure(home, f'[[folder]]\npath = "{drop}"\n')

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 2
    assert "configured as an ingest folder" in result.output


def test_a_config_error_exits_1_naming_the_key(home: Path, drop: Path) -> None:
    _configure(home, f'[[folder]]\npath = "{drop}"\nmode = "capture"\nkind = "voice"\n')

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 1
    assert "folder[0].kind" in result.output


# --- AC1, AC4, AC5 ------------------------------------------------------------------


def test_a_share_becomes_an_inbox_note_and_leaves_icloud(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, "Save to Brain 2026-10-30 at 09.15.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 0, result.output
    assert "ingested  Save to Brain 2026-10-30 at 09.15.md" in result.output
    (note,) = _notes(test_db)
    assert (note["kind"], note["tags"], note["sensitivity"]) == ("vault", ["inbox"], "normal")
    assert "sprocket plan" in note["content"] and URL in note["content"]
    assert note["vault_path"].startswith("capture/")
    assert _files(drop) == []  # AC5: gone from the iCloud folder, imported/ included


def test_a_confidential_share_becomes_a_confidential_note(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, "private.md", CONFIDENTIAL_SHARE)

    assert _import("--dir", str(drop), "--since", "7d").exit_code == 0

    (note,) = _notes(test_db)
    assert note["sensitivity"] == "confidential"
    assert "sensitivity" not in note["content"]


def test_a_configured_confidential_folder_is_used_by_dir(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _configure(
        home,
        f'[[folder]]\npath = "{drop}"\nmode = "capture"\nsensitivity = "confidential"\n'
        "auto = false\n",
    )
    _share(drop, "share.md", SHARE)

    assert _import("--dir", str(drop), "--since", "7d").exit_code == 0

    assert _notes(test_db)[0]["sensitivity"] == "confidential"


# --- AC2 ------------------------------------------------------------------------------


def test_a_rerun_adds_nothing(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, "share.md", SHARE)

    first = _import("--dir", str(drop), "--since", "7d")
    second = _import("--dir", str(drop), "--since", "7d")

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert "nothing new" in second.output
    assert len(_notes(test_db)) == 1


def test_two_different_files_with_one_name_become_two_notes(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, "Shared.md", "first synthetic share")
    assert _import("--dir", str(drop), "--since", "7d").exit_code == 0
    _share(drop, "Shared.md", "second synthetic share")
    assert _import("--dir", str(drop), "--since", "7d").exit_code == 0

    contents = sorted(n["content"].strip() for n in _notes(test_db))
    assert contents == ["first synthetic share", "second synthetic share"]


def test_the_same_share_arriving_again_after_a_crash_is_one_note(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    """A crash after the note but before the delete leaves the imported/ copy; the same
    share arriving again is a duplicate, and both copies go."""
    _share(drop, "share.md", SHARE)
    assert _import("--dir", str(drop), "--since", "7d").exit_code == 0
    sha8 = hashlib.sha256(SHARE.encode()).hexdigest()[:8]
    (drop / IMPORTED_DIR / f"share-{sha8}.md").write_text(SHARE, encoding="utf-8")
    _share(drop, "share.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 0, result.output
    assert "duplicate" in result.output
    assert len(_notes(test_db)) == 1
    assert _files(drop) == []


# --- AC3 ------------------------------------------------------------------------------


def test_icloud_placeholders_are_skipped_until_they_download(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, ".Save to Brain 1.md.icloud", b"bplist00 placeholder")
    _share(drop, ".Save to Brain 2.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 0
    assert _notes(test_db) == []
    assert _files(drop) == [".Save to Brain 1.md.icloud", ".Save to Brain 2.md"]


# --- failures -----------------------------------------------------------------------


def test_a_share_that_is_not_text_is_rejected_and_kept(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any
) -> None:
    _share(drop, "binary.md", b"\xff\xfe\x00binary")

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 1
    assert "rejected" in result.output and "UnicodeDecodeError" in result.output
    assert [f.split("-")[0] for f in _files(drop)] == [f"{IMPORTED_DIR}/binary"]


def test_a_vault_with_no_template_sends_the_share_back(
    test_db: psycopg.Connection[Any], home: Path, drop: Path, embedder: Any,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "empty-vault"))
    (tmp_path / "empty-vault").mkdir()
    _share(drop, "share.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 1
    assert "VaultNoteSyncError" in result.output
    assert _files(drop) == ["share.md"]


def test_a_symlinked_imported_refuses_the_folder(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any,
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (drop / IMPORTED_DIR).symlink_to(elsewhere)
    _share(drop, "share.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d")

    assert result.exit_code == 1
    assert "skipped" in result.output and "imported/ is not a real directory" in result.output
    assert list(elsewhere.iterdir()) == []


# --- dry run ------------------------------------------------------------------------


def test_dry_run_lists_without_touching_anything(home: Path, drop: Path) -> None:
    _share(drop, "evil\x1b[31mname.md", SHARE)

    result = _import("--dir", str(drop), "--since", "7d", "--dry-run")

    assert result.exit_code == 0
    assert "would import  evil[31mname.md" in result.output
    assert "\x1b" not in result.output
    assert _files(drop) == ["evil\x1b[31mname.md"]


# --- AC6: one code path ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["capture-import", "ingest-folder"])
def test_both_commands_make_the_same_notes_through_import_file(
    test_db: psycopg.Connection[Any], home: Path, vault: Path, drop: Path, embedder: Any,
    no_ocr: None, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    """``brain ingest-folder --configured`` over a capture folder and ``brain capture
    import`` both reach :func:`brain.capture_import.import_file`, and make the same notes."""
    real = capture_import.import_file
    calls: list[str] = []

    def spy(conn: Any, path: Path, **kw: Any) -> str:
        calls.append(path.name)
        return real(conn, path, **kw)

    monkeypatch.setattr(capture_import, "import_file", spy)
    _configure(home, f'[[folder]]\npath = "{drop}"\nmode = "capture"\n')
    _share(drop, "a.md", SHARE, age_s=900)
    _share(drop, "b.md", CONFIDENTIAL_SHARE, age_s=800)

    if command == "capture-import":
        result = _import("--dir", str(drop), "--since", "7d")
    else:
        result = CliRunner().invoke(
            app, ["ingest-folder", "--no-enrich", "--configured", "--since", "7d"]
        )

    assert result.exit_code == 0, result.output
    assert [name.split("-")[0] for name in calls] == ["a", "b"]
    today, words = date.today(), DEFAULT_CAPTURE_TITLE_WORDS
    expected = [
        (make_capture_title(body, today=today, max_words=words), ["inbox"], level, body)
        for body, level in [(SHARE.strip(), "normal"), (PRIVATE_BODY, "confidential")]
    ]
    notes = _notes(test_db)
    assert [(n["title"], n["tags"], n["sensitivity"], n["content"].strip()) for n in notes] == (
        sorted(expected)
    )
    assert _files(drop) == []
