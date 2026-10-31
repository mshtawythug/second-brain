"""The ``code`` kind across its surfaces (RM-43): the set, the glyph tables, the skill row."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain import cli_ingest_code
from brain.source_kinds import validate_source_kind

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"


def test_code_is_a_valid_source_kind_and_the_command_uses_it() -> None:
    assert validate_source_kind("code") == cli_ingest_code.SOURCE_KIND == "code"


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_code_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bcode:\s*\"[^\"]+\"", match.group("body"))


def test_code_has_no_month_grouping() -> None:
    """The ticket states that the fallback (no month headers) is intended."""
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert "_ingested/code" not in match.group("body")


def test_the_skill_routes_a_repository_to_the_command() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert "`brain ingest-code <repo-dir> --author-email" in text
