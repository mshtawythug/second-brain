"""The ``screenshot`` kind across its surfaces (RM-33): the set, the glyph tables, the tree.

A screenshot document lands in the vault mirror under ``_ingested/screenshot/``,
and that folder gets month headers like Krisp, Gmail and calendar.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
RECIPE = REPO_ROOT / "skills" / "ingest-brain" / "sources" / "drop-folders.md"


def test_screenshot_is_a_valid_source_kind() -> None:
    assert validate_source_kind("screenshot") == "screenshot"


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_screenshot_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bscreenshot:\s*\"[^\"]+\"", match.group("body"))


def test_screenshot_folder_is_month_grouped() -> None:
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert '"_ingested/screenshot"' in match.group("body")


def test_the_skill_routes_drop_folders_to_the_recipe() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert "| Screenshots / a drop folder | `brain ingest-folder --dir PATH --since 7d`" in text
    assert "[sources/drop-folders.md](sources/drop-folders.md)" in text


def test_the_recipe_names_the_scope_the_move_and_the_folder_rule() -> None:
    text = " ".join(RECIPE.read_text(encoding="utf-8").split())

    assert text.startswith("# Drop folders and screenshots")
    assert "**refuses without `--dir PATH` or `--configured`, and `--since Nd`**" in text
    assert "Run `--dry-run` first" in text
    assert "Never point `--dir` at a folder the user did not ask for" in text
    assert "stored **confidential** by default" in text
