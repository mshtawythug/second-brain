"""``brain ingest-dir`` walks only extractors registered for directory walks (RM-07).

An extractor registered with ``in_directory_walks=False`` (a later image or audio
extractor) must not change what an existing ``ingest-dir`` run picks up; an
explicit ``--ext`` still reaches it. ``--dry-run`` lists the walk without opening
a database connection, so no DB fixture is needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, dispatch
from brain.ingest.dispatch import BUILTIN_EXTRACTORS, ExtractorRegistry, ExtractorSpec

PROBE_EXTENSION = ".probe"


def _never_called(path: Path) -> ExtractedDoc:
    raise AssertionError("dry-run must not extract")


def _walk_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "note.md").write_text("# Synthetic\n\nbody\n", encoding="utf-8")
    (root / f"capture{PROBE_EXTENSION}").write_text("probe", encoding="utf-8")
    return root


@pytest.fixture
def probe_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A fresh default registry (restored by monkeypatch) holding the built-ins
    # plus one extractor that stays out of directory walks.
    registry = ExtractorRegistry(BUILTIN_EXTRACTORS)
    registry.register(
        ExtractorSpec(
            name="probe",
            extensions=(PROBE_EXTENSION,),
            load=lambda: _never_called,
            in_directory_walks=False,
        )
    )
    monkeypatch.setattr(dispatch, "_DEFAULT_REGISTRY", registry)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))


@pytest.mark.usefixtures("probe_registry")
def test_ingest_dir_default_walk_skips_non_walk_extractor(tmp_path: Path) -> None:
    # Arrange
    root = _walk_tree(tmp_path)

    # Act
    result = CliRunner().invoke(app, ["ingest-dir", str(root), "--dry-run"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "found 1 file(s)" in result.output
    assert "note.md" in result.output
    assert PROBE_EXTENSION not in result.output


@pytest.mark.usefixtures("probe_registry")
def test_ingest_dir_explicit_ext_reaches_non_walk_extractor(tmp_path: Path) -> None:
    # Arrange
    root = _walk_tree(tmp_path)

    # Act
    result = CliRunner().invoke(
        app, ["ingest-dir", str(root), "--dry-run", "--ext", PROBE_EXTENSION.lstrip(".")]
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert f"capture{PROBE_EXTENSION}" in result.output
    assert "note.md" not in result.output
