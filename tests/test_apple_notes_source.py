"""The ``applenotes`` kind across its surfaces (RM-36): the set, the glyph tables, the skill."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.ingest.apple_notes import SOURCE_KIND
from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
GUIDE = REPO_ROOT / "docs" / "guides" / "apple-notes.md"
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"


def test_applenotes_is_a_valid_source_kind_and_one_lowercase_word() -> None:
    """One word, because the kind becomes the mirror directory ``_ingested/applenotes/``."""
    assert validate_source_kind(SOURCE_KIND) == "applenotes"
    assert re.fullmatch(r"[a-z]+", SOURCE_KIND)


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_an_applenotes_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bapplenotes:\s*\"[^\"]+\"", match.group("body"))


def test_applenotes_has_no_month_grouping() -> None:
    """The ticket: a glyph in both tables, no month grouping (the fallback is intended)."""
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert "_ingested/applenotes" not in match.group("body")


def test_the_skill_routes_to_the_command_with_its_scope_and_tier() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")
    [row] = [line for line in text.splitlines() if "brain ingest-apple-notes" in line]

    assert "`brain ingest-apple-notes --folder NAME`" in row
    assert "**REFUSES without `--folder`**" in row
    assert "**confidential**" in row
    assert "Automation" in row
    assert len(text.splitlines()) <= 300


def test_the_guide_documents_the_permission_prompt_and_the_scope() -> None:
    text = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert "System Settings → Privacy & Security → Automation" in text
    assert "`--folder NAME` is required" in text
    assert "**Locked notes** are never read" in text
    assert "**Attachments**" in text and "are not ingested" in text
    assert "stored as `confidential` unless you pass `--sensitivity normal`" in text
