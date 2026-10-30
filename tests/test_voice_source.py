"""The ``voice`` kind across its surfaces (RM-38): the set, the glyph tables, the tree.

A voice memo lands in the vault mirror under ``_ingested/voice/``, and that folder gets
month headers like Krisp, Gmail, calendar and screenshots.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"


def test_voice_is_a_valid_source_kind() -> None:
    assert validate_source_kind("voice") == "voice"


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_voice_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bvoice:\s*\"[^\"]+\"", match.group("body"))


def test_voice_folder_is_month_grouped() -> None:
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert '"_ingested/voice"' in match.group("body")


INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
RECIPE = REPO_ROOT / "skills" / "ingest-brain" / "sources" / "drop-folders.md"


def test_the_skill_routes_voice_memos_to_the_recipe() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert '| Voice memos / a `kind = "voice"` drop folder |' in text
    assert "never download a model for the user" in text


def test_the_recipe_says_memos_are_confidential_and_transcript_only() -> None:
    text = " ".join(RECIPE.read_text(encoding="utf-8").split())

    assert 'configured with `kind = "voice"`' in text
    assert "only the transcript enters the brain" in text
    assert "never download one for them" in text
