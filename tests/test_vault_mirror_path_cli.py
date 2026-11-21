"""F15 end to end: ``ingest-code`` and ``ingest-folder`` mirror into a flat directory.

QA row AX3: ``brain ingest-code`` wrote
``_ingested/code/2026-11-14-/srv/rep-sample-repo-src-widget-py.md`` — the first
eight characters of the absolute source path became stray nested directories,
and ``documents.vault_path`` stored the same string. Here ``tmp_path`` is the
absolute path, so the old rule nests under its first eight characters.

Every repository, folder and vault is in ``tmp_path``; OCR is the fake engine
from the folder suite (a ``monkeypatch.setattr`` test double).
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest.ocr import OcrSetup
from tests import code_repo_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.test_cli_ingest_folder import AUTO, _Engine, _png

_FLAT_NAME = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}-[a-z0-9-]+\.md")


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _assert_flat(conn: psycopg.Connection[Any], vault: Path, kind: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.vault_path FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = %s ORDER BY d.vault_path",
        (kind,),
    ).fetchall()
    paths = [str(r[0]) for r in rows]
    assert paths
    for path in paths:
        pure = PurePosixPath(path)
        assert pure.parent == PurePosixPath(f"_ingested/{kind}"), path
        assert _FLAT_NAME.fullmatch(pure.name), path
        assert (vault / path).is_file()
    assert [p for p in (vault / "_ingested" / kind).iterdir() if p.is_dir()] == []
    return paths


def test_ingest_code_mirrors_land_flat_in_ingested_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    repo = fx.build_repo(tmp_path)
    vault = tmp_path / "vault"
    monkeypatch.setenv("HOME", str(fx.home_of(tmp_path)))
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app,
        ["ingest-code", str(repo), "--author-email", fx.OWNER_EMAIL, "--since", fx.SINCE],
    )

    assert result.exit_code == 0, result.output
    assert len(_assert_flat(test_db, vault, "code")) == len(fx.OWNER_KEPT)


def test_ingest_folder_mirrors_land_flat_in_ingested_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    fake = _Engine()
    monkeypatch.setattr(
        "brain.cli_ingest_folder.ocr_resolver",
        lambda env: OcrSetup(settings=AUTO, engine=fake),
    )
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    drop = tmp_path / "drop"
    drop.mkdir()
    _png(drop, "shot.png")

    result = CliRunner().invoke(
        app, ["ingest-folder", "--no-enrich", "--dir", str(drop), "--since", "7d"]
    )

    assert result.exit_code == 0, result.output
    assert len(_assert_flat(test_db, vault, "screenshot")) == 1
