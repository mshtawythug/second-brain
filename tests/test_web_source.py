"""The ``web`` kind across its surfaces (RM-41): the set, the glyph tables, the tree, the skill.

A ``web`` page is not dated content worth month headers (the ticket: no month
grouping), so ``_ingested/web`` is deliberately absent from ``MONTH_GROUPED_PATHS``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.graph_rag.extract_policy import CONCEPT_SKIP_CONTENT_TYPES
from brain.ingest import browser
from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
GUIDE = REPO_ROOT / "docs" / "guides" / "browser-history.md"


def test_web_is_a_valid_source_kind() -> None:
    assert validate_source_kind("web") == "web" == browser.SOURCE_KIND


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_web_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bweb:\s*\"[^\"]+\"", match.group("body"))


def test_the_web_folder_is_not_month_grouped() -> None:
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert '"_ingested/web"' not in match.group("body")


def test_web_pages_skip_concept_extraction() -> None:
    assert browser.CONTENT_TYPE in CONCEPT_SKIP_CONTENT_TYPES


def test_the_skill_row_names_the_refusal_and_forbids_editing_the_allowlist() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert "`brain ingest-web --browser chrome --since YYYY-MM-DD`" in text
    assert "**REFUSES without `$BRAIN_HOME/web-allowlist.txt`**" in text
    assert "Never create or extend the allowlist for the user" in text


def test_the_guide_covers_the_allowlist_full_disk_access_and_fetch() -> None:
    text = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert "ingests **nothing** until you list the hosts" in text
    assert "Full Disk Access" in text
    assert "resolve only to **public** addresses" in text
    assert "**removed before anything is stored**" in text
