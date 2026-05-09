"""Tests for `brain ingest` and `brain ingest-dir` CLI commands."""
import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import extract_path, supported_extensions

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the real Qwen3Embedder builder for the FakeEmbedder fixture."""
    from tests.conftest import FakeEmbedder

    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())


def test_ingest_single_file(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    result = CliRunner().invoke(app, ["ingest", str(fixtures_dir / "sample.txt")])
    assert result.exit_code == 0, result.output
    assert "ingested" in result.output.lower()


def test_ingest_same_file_twice_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Second ingest of the same content should be reported as skipped."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    runner = CliRunner()
    first = runner.invoke(app, ["ingest", str(fixtures_dir / "sample.txt")])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["ingest", str(fixtures_dir / "sample.txt")])
    assert second.exit_code == 0, second.output
    assert "skipped" in second.output.lower()


def test_ingest_force_re_ingests(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """With --force, a repeat ingest replaces the prior row."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    runner = CliRunner()
    runner.invoke(app, ["ingest", str(fixtures_dir / "sample.txt")])
    forced = runner.invoke(
        app, ["ingest", str(fixtures_dir / "sample.txt"), "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert "ingested" in forced.output.lower()


def test_ingest_dir_recursive(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    result = CliRunner().invoke(app, ["ingest-dir", str(fixtures_dir)])
    assert result.exit_code == 0, result.output
    # at least sample.txt + sample.md should be mentioned
    assert "sample.txt" in result.output
    assert "sample.md" in result.output


def test_ingest_with_tag(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    result = CliRunner().invoke(
        app, ["ingest", str(fixtures_dir / "sample.txt"), "--tag", "career"]
    )
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("SELECT tags FROM documents").fetchall()[-1]
    assert "career" in row[0]


def test_ingest_dir_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """--dry-run lists files without touching the database."""
    # Sandbox vault even for dry-run (no writes occur, but keeps isolation consistent).
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    result = CliRunner().invoke(
        app, ["ingest-dir", str(fixtures_dir), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would ingest" in result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        doc_count_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert doc_count_row is not None
    assert doc_count_row[0] == 0


def test_ingest_dir_ext_filter(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """--ext limits the file types considered."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)
    result = CliRunner().invoke(
        app, ["ingest-dir", str(fixtures_dir), "--ext", "txt"]
    )
    assert result.exit_code == 0, result.output
    assert "sample.txt" in result.output
    assert "sample.md" not in result.output


def test_ingest_dir_continues_on_per_file_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """A corrupt file in the tree is reported but doesn't stop the run."""
    # Sandbox vault so mirror writes don't touch ~/brain-vault.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    _patch_embedder(monkeypatch)

    # Real good file + a bogus PDF that will fail extraction
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.txt").write_text("hello world\n\nsecond paragraph", encoding="utf-8")
    (src / "bad.pdf").write_bytes(b"not-a-real-pdf")

    result = CliRunner().invoke(app, ["ingest-dir", str(src)])
    assert result.exit_code == 0, result.output
    assert "good.txt" in result.output
    assert "failed" in result.output.lower()
    assert "bad.pdf" in result.output


def test_ingest_creates_vault_mirror(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """`brain ingest <file>` writes a mirror under ``<vault>/_ingested/manual/``.

    Setup: point ``BRAIN_VAULT_PATH`` at a sandbox tmp dir so the mirror
    write doesn't touch the real ``~/brain-vault``.
    Exercise: invoke the ingest command on a fixture text file.
    Verify: a Markdown file lands under ``_ingested/manual/`` whose body
    contains the original file's content.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _patch_embedder(monkeypatch)

    result = CliRunner().invoke(app, ["ingest", str(fixtures_dir / "sample.txt")])

    assert result.exit_code == 0, result.output
    mirror_dir = tmp_path / "_ingested" / "manual"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    body = mirrors[0].read_text(encoding="utf-8")
    expected = (fixtures_dir / "sample.txt").read_text(encoding="utf-8").strip()
    assert expected.splitlines()[0] in body


def test_ingest_dir_creates_vault_mirrors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """`brain ingest-dir` writes one mirror per ingested file.

    Uses a small isolated tmp dir (rather than ``fixtures_dir``) so we can
    enumerate exactly which files were ingested. The dir holds two simple
    text files; both should appear under ``_ingested/manual/``.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    _patch_embedder(monkeypatch)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "alpha.txt").write_text("alpha body content\n", encoding="utf-8")
    (src_dir / "beta.txt").write_text("beta body content\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["ingest-dir", str(src_dir)])

    assert result.exit_code == 0, result.output
    mirror_dir = tmp_path / "vault" / "_ingested" / "manual"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = sorted(mirror_dir.glob("*.md"))
    assert len(mirrors) == 2, f"expected two mirror files, got {mirrors}"
    bodies = [p.read_text(encoding="utf-8") for p in mirrors]
    joined = "\n".join(bodies)
    assert "alpha body content" in joined
    assert "beta body content" in joined


def test_extract_path_raises_on_unsupported(tmp_path: Path) -> None:
    bogus = tmp_path / "weird.xyz"
    bogus.write_text("irrelevant", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported file type"):
        extract_path(bogus)


def test_supported_extensions_covers_expected_types() -> None:
    exts = supported_extensions()
    for expected in (".txt", ".md", ".markdown", ".pdf", ".docx"):
        assert expected in exts


def test_extract_path_wraps_malformed_docx_as_value_error(tmp_path: Path) -> None:
    """A file with a .docx extension that isn't a real zip/OPC package raises ValueError."""
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a real docx")
    with pytest.raises(ValueError, match="malformed DOCX"):
        extract_path(bad)
