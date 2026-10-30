"""The ``github`` kind across its surfaces (RM-31): the set, the glyph tables, the skill.

The MCP recipe (path (a)) has no flag to enforce its scope, so the skill text is
the contract, and these tests pin what it must say (acceptance criterion 5).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.source_kinds import validate_source_kind

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"


def test_github_is_a_valid_source_kind() -> None:
    assert validate_source_kind("github") == "github"


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_github_entry(js_file: str) -> None:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    assert re.search(r"\bgithub:\s*\"[^\"]+\"", match.group("body"))


def test_github_has_no_month_grouping() -> None:
    """The ticket states that the fallback (no month headers) is intended."""
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert "_ingested/github" not in match.group("body")


def _github_section() -> str:
    text = INGEST_SKILL.read_text(encoding="utf-8")
    match = re.search(r"^## GitHub PRs.*?(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert match, "ingest-brain has no '## GitHub PRs' section"
    return match.group(0)


def test_the_recipe_names_the_scope_and_the_refusal() -> None:
    section = _github_section()

    assert "refuses without `--repo OWNER/REPO` or `--mine`, and `--since`" in section
    assert "`author:@me` or `assignee:@me`, never an organisation" in section
    assert "`updated:>=YYYY-MM-DD` bound" in section
    assert "refuse a search with no date bound" in section


def test_the_recipe_names_the_sensitivity_and_the_owner_only_comments() -> None:
    section = _github_section()

    assert "stores private\nrepositories as **confidential**" in section
    assert "`--sensitivity confidential` for any\nprivate repository" in section
    assert "only the user's own comments unless `--with-thread`" in section
    assert "--source github" in section
    assert '--external-id "<owner>/<repo>#<n>"' in section


def test_the_table_row_and_the_safety_rule_exist() -> None:
    text = INGEST_SKILL.read_text(encoding="utf-8")

    assert "| The user's own GitHub PRs, issues, READMEs | `brain ingest-github" in text
    assert "- **GitHub: the user's own activity, date-bounded.**" in text


def test_maintenance_sees_a_running_ingest_github_as_an_ingest() -> None:
    from brain import maintenance

    assert maintenance.ingest_in_flight(
        ["/usr/bin/python /x/bin/brain ingest-github --mine --since 2026-10-01"]
    ) is True
