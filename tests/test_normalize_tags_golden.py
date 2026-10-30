"""RM-21 AC4: ``brain backfill normalize-tags`` output is byte-identical across the extraction.

The golden strings below were captured from ``cli.py`` at ``1b26c40``, BEFORE the mapping engine
moved to :mod:`brain.tag_mapping`. Every document gets a fixed UUID so ``doc_id[:8]`` and the
``ORDER BY id`` walk are deterministic. The scenario covers every line the command can print: the
dry-run plan line, the stderr warning for a missing file, a ``vault_path``-less document, a file
already canonical on disk (DB rewritten, file not), the ``--mapping`` collapse, the summary in both
modes, and a re-run that converges to a no-op. All tags and titles are synthetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

_DOC_A = "a1000000-0000-4000-8000-000000000001"
_DOC_B = "b2000000-0000-4000-8000-000000000002"
_DOC_C = "c3000000-0000-4000-8000-000000000003"
_DOC_D = "d4000000-0000-4000-8000-000000000004"
_DOC_E = "e5000000-0000-4000-8000-000000000005"

_MAPPING = {"Widgets": "Widget", "machine learning": "ML"}

_GOLDEN_DRY_RUN = (
    "a1000000  Alpha note  ['Topic One', 'topic_one', 'Widgets'] → ['topic-one', 'widget']\n"
    "c3000000  Gamma note  ['Machine Learning', 'ml'] → ['ml']\n"
    "d4000000  Delta note  ['Loose Tag'] → ['loose-tag']\n"
    "e5000000  Epsilon note  ['Mixed', 'mixed'] → ['mixed']\n"
    "would normalize 4 doc(s), rewrote 0 file(s), 0 file-missing skipped, "
    "1 already-canonical skipped\n"
)

_GOLDEN_APPLY_STDERR = (
    "file missing on disk for c3000000 (missing/gamma.md); DB updated, file skipped.\n"
)

_GOLDEN_APPLY_STDOUT = (
    "normalized 4 doc(s), rewrote 1 file(s), 1 file-missing skipped, "
    "1 already-canonical skipped\n"
)

_GOLDEN_RERUN = (
    "normalized 0 doc(s), rewrote 0 file(s), 0 file-missing skipped, "
    "5 already-canonical skipped\n"
)


def _seed(
    conn: psycopg.Connection[Any],
    doc_id: str,
    title: str,
    tags: list[str],
    vault_path_rel: str | None,
) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, title, content, content_hash, content_type, tags, kind, vault_path) "
        "VALUES (%s, %s, %s, %s, 'note', %s, 'vault', %s)",
        (doc_id, title, f"body for {title}", f"hash-{doc_id}", tags, vault_path_rel),
    )


def _write(vault: Path, rel: str, tags: list[str]) -> Path:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        dump_frontmatter({"id": "ignored", "title": rel, "tags": tags}, "body\n"),
        encoding="utf-8",
    )
    return target


@pytest.fixture
def scenario(
    test_db: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Seed five documents and their vault files; return the mapping file's path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault, "alpha.md", ["Topic One", "topic_one", "Widgets"])
    # Epsilon's file is ALREADY canonical while its DB row is not: the DB is
    # rewritten and the file is not, so it is not counted as rewritten.
    _write(vault, "epsilon.md", ["mixed"])
    _seed(test_db, _DOC_A, "Alpha note", ["Topic One", "topic_one", "Widgets"], "alpha.md")
    _seed(test_db, _DOC_B, "Beta note", ["already-fine"], None)
    _seed(test_db, _DOC_C, "Gamma note", ["Machine Learning", "ml"], "missing/gamma.md")
    _seed(test_db, _DOC_D, "Delta note", ["Loose Tag"], None)
    _seed(test_db, _DOC_E, "Epsilon note", ["Mixed", "mixed"], "epsilon.md")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(_MAPPING), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    return mapping_path


def test_normalize_tags_dry_run_output_is_byte_identical(scenario: Path) -> None:
    result = CliRunner().invoke(
        app, ["backfill", "normalize-tags", "--dry-run", "--mapping", str(scenario)]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == _GOLDEN_DRY_RUN
    assert result.stderr == ""


def test_normalize_tags_apply_and_rerun_output_is_byte_identical(scenario: Path) -> None:
    runner = CliRunner()

    applied = runner.invoke(app, ["backfill", "normalize-tags", "--mapping", str(scenario)])
    rerun = runner.invoke(app, ["backfill", "normalize-tags", "--mapping", str(scenario)])

    assert applied.exit_code == 0, applied.output
    assert applied.stdout == _GOLDEN_APPLY_STDOUT
    assert applied.stderr == _GOLDEN_APPLY_STDERR
    assert rerun.exit_code == 0, rerun.output
    assert rerun.stdout == _GOLDEN_RERUN
    assert rerun.stderr == ""


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not json", "mapping file is not valid JSON"),
        ("[1, 2]", "mapping file must be a JSON object of {from: to} strings"),
        ('{"a": 1}', "mapping entries must be strings; got str → int"),
    ],
)
def test_normalize_tags_bad_mapping_is_a_usage_error(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    message: str,
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(app, ["backfill", "normalize-tags", "--mapping", str(bad)])

    assert result.exit_code == 2
    # Rich wraps the usage-error panel to the terminal width, so compare with
    # whitespace and the panel's border characters collapsed.
    flat = " ".join(result.output.replace("│", " ").split())
    assert message in flat
