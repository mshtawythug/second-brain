"""RM-34 — the ``jira`` source kind and its agent recipe.

Jira has no ingest code of its own: the Atlassian MCP connector fetches an
issue and the ``ingest-brain`` skill pipes it into ``brain ingest-stdin
--source jira``. What RM-34 adds is therefore the kind, the two front-end glyph
entries, and the recipe text, and each of those is pinned here. Every issue key,
host and name below is synthetic.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.facets import compute_facets
from brain.search import build_tsquery
from brain.search_predicate import build_predicate
from brain.source_kinds import VALID_SOURCE_KINDS, validate_source_kind
from brain.ui.schemas import parse_search_params

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = REPO_ROOT / "src" / "brain" / "ui" / "static" / "js"
INGEST_SKILL = REPO_ROOT / "skills" / "ingest-brain" / "SKILL.md"

JIRA_KIND = "jira"
ISSUE_EXTERNAL_ID = "example.atlassian.net/DEMO-1"
ISSUE_TITLE = "DEMO-1 — Rotate the staging signing key"
ISSUE_TERM = "zymurgical"
ISSUE_BODY = (
    "DEMO-1: Rotate the staging signing key\n\n"
    f"Description: the {ISSUE_TERM} rollout needs the staging key rotated "
    "before the next release window.\n\n"
    "Comment (owner, 2026-10-20): rotation scripted; waiting on review.\n"
)
ISSUE_METADATA = {
    "site": "example.atlassian.net",
    "issue_key": "DEMO-1",
    "status": "In Progress",
    "assignee": "Sam Rivera",
    "updated": "2026-10-21",
}
DEFAULT_JQL = (
    "(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d"
)


# ---------------------------------------------------------------------------
# The kind.
# ---------------------------------------------------------------------------


def test_jira_is_a_valid_source_kind() -> None:
    assert JIRA_KIND in VALID_SOURCE_KINDS
    assert validate_source_kind(JIRA_KIND) == JIRA_KIND


def test_ui_source_filter_accepts_jira() -> None:
    spec = parse_search_params({"q": "anything", "source": JIRA_KIND})

    assert spec.source_kind == JIRA_KIND


def test_ingest_stdin_help_names_every_kind() -> None:
    """The help text is derived, so it cannot go stale when a kind is added."""
    result = CliRunner().invoke(app, ["ingest-stdin", "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    for kind in VALID_SOURCE_KINDS:
        assert kind in result.output, f"--source help does not name {kind!r}"


# ---------------------------------------------------------------------------
# End to end: a synthetic issue through `brain ingest-stdin`.
# ---------------------------------------------------------------------------


def _ingest_issue(
    monkeypatch: pytest.MonkeyPatch, fake_embedder: object, vault: Path
) -> str:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", JIRA_KIND,
            "--external-id", ISSUE_EXTERNAL_ID,
            "--title", ISSUE_TITLE,
            "--content-type", "jira_issue",
            "--sensitivity", "confidential",
            "--date", "2026-10-21",
            "--metadata", json.dumps(ISSUE_METADATA),
            "--tag", "jira",
            "--no-enrich",
        ],
        input=ISSUE_BODY,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_synthetic_issue_lands_confidential_under_ingested_jira(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    # Arrange + act
    _ingest_issue(monkeypatch, fake_embedder, tmp_path)

    # Assert
    row = test_db.execute(
        "SELECT s.kind, d.content_type, d.sensitivity, d.vault_path, d.metadata "
        "FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.external_id = %s",
        (ISSUE_EXTERNAL_ID,),
    ).fetchone()
    assert row is not None
    kind, content_type, sensitivity, vault_path, metadata = row
    assert kind == JIRA_KIND
    assert content_type == "jira_issue"
    assert sensitivity == "confidential"
    assert vault_path is not None and vault_path.startswith("_ingested/jira/"), vault_path
    assert (tmp_path / vault_path).is_file()
    # The volatile fields live in metadata, where a status change needs no re-chunk.
    assert metadata["status"] == "In Progress"
    assert metadata["assignee"] == "Sam Rivera"
    assert metadata["issue_key"] == "DEMO-1"


def test_synthetic_issue_buckets_under_jira_in_the_facet_panel(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    # Arrange
    _ingest_issue(monkeypatch, fake_embedder, tmp_path)

    # Act
    facets = compute_facets(
        test_db,
        predicate=build_predicate(),
        tsquery=build_tsquery(test_db, ISSUE_TERM),
    )

    # Assert
    by_value = {bucket.value: bucket.count for bucket in facets.source}
    assert by_value == {JIRA_KIND: 1}


# ---------------------------------------------------------------------------
# Front end: the per-kind tables are not derived from the Python set.
# ---------------------------------------------------------------------------


def _source_icons_block(js_file: str) -> str:
    text = (STATIC_JS / js_file).read_text(encoding="utf-8")
    match = re.search(r"const SOURCE_ICONS = \{(?P<body>.*?)\};", text, re.DOTALL)
    assert match, f"{js_file} has no SOURCE_ICONS table"
    return match.group("body")


@pytest.mark.parametrize("js_file", ["results.js", "discovery.js"])
def test_both_glyph_tables_have_a_jira_entry(js_file: str) -> None:
    assert re.search(r"\bjira:\s*\"[^\"]+\"", _source_icons_block(js_file)), (
        f"{js_file}: SOURCE_ICONS has no jira glyph, so Jira rows fall back to the vault glyph"
    )


def test_jira_has_no_month_grouping() -> None:
    """The ticket states that the fallback (no month headers) is intended."""
    text = (STATIC_JS / "tree.js").read_text(encoding="utf-8")
    match = re.search(r"const MONTH_GROUPED_PATHS = new Set\(\[(?P<body>[^\]]*)\]\)", text)
    assert match, "tree.js has no MONTH_GROUPED_PATHS"
    assert "_ingested/jira" not in match.group("body")


# ---------------------------------------------------------------------------
# The recipe: its scope lives in text, so the text is the contract.
# ---------------------------------------------------------------------------


def _jira_section() -> str:
    text = INGEST_SKILL.read_text(encoding="utf-8")
    match = re.search(r"^## Jira issues.*?(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert match, "ingest-brain has no '## Jira issues' section"
    return match.group(0)


def test_recipe_carries_the_parenthesized_default_jql_verbatim() -> None:
    assert DEFAULT_JQL in _jira_section()


def _jira_command_block() -> str:
    """The fenced ``bash`` block the agent copies: the flags must be IN it.

    Checking the whole section is not enough, because the bullets below the
    block repeat ``--sensitivity confidential`` as prose. A recipe that dropped
    the flag from the command would still have passed.
    """
    blocks = re.findall(r"^```bash\n(.*?)^```", _jira_section(), re.DOTALL | re.MULTILINE)
    assert len(blocks) == 1, f"expected one bash block in the Jira section, found {len(blocks)}"
    return blocks[0]


@pytest.mark.parametrize(
    "flag",
    [
        "--sensitivity confidential",
        "--source jira",
        "--content-type jira_issue",
        '--external-id "<site-host>/<ISSUE-KEY>"',
    ],
)
def test_recipe_command_ingests_as_confidential_jira_issues(flag: str) -> None:
    assert flag in _jira_command_block(), f"the recipe's command block lacks {flag}"


def test_recipe_refuses_a_jql_without_a_time_bound() -> None:
    section = _jira_section()

    assert re.search(r"refuse[^.]*no time bound", section, re.IGNORECASE), (
        "the recipe must tell the agent to refuse a JQL with no time bound"
    )
    assert "project-wide" in section.lower()
