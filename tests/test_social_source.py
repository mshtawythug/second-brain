"""The ``social`` kind across its surfaces (RM-44): the set, the glyph tables, the tree, the skill.

A liked post is not dated content worth month headers (the ticket: no month
grouping), so ``_ingested/social`` is deliberately absent from ``MONTH_GROUPED_PATHS``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.graph_rag.extract_policy import CONCEPT_SKIP_CONTENT_TYPES
from brain.ingest import social_posts
from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"


def test_social_is_a_valid_source_kind() -> None:
    assert validate_source_kind("social") == "social" == social_posts.SOURCE_KIND


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_social_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bsocial:\s*\"[^\"]+\"", match.group("body"))


def test_the_social_folder_is_not_month_grouped() -> None:
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert '"_ingested/social"' not in match.group("body")


def test_social_posts_skip_concept_extraction() -> None:
    assert social_posts.CONTENT_TYPE in CONCEPT_SKIP_CONTENT_TYPES


def test_the_skill_row_asks_for_the_path_and_never_searches() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert "`brain ingest-bookmarks --format x-archive <archive-dir>`" in text
    assert "Never search for the archive yourself" in text
    assert "`--since` filters on when the post was **written**" in text


def test_the_cli_reference_documents_the_command() -> None:
    text = " ".join(CLI_REFERENCE.read_text(encoding="utf-8").split())

    assert "## brain ingest-bookmarks" in text
    assert "parsed as JSON, never run" in text
    assert "unzip it first" in text
