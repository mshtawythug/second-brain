"""``brain review weekly`` and ``brain brief --wiki`` must not PUBLISH confidential docs.

Both commands serve two audiences from one invocation: the operator's terminal
(inside the trust boundary) and a file written into ``cfg.vault_path`` (an egress
boundary — Quartz publishes it). ``build_weekly_report`` and ``assemble_brief``
both default ``exclude_confidential=False``, which is correct for the terminal and
wrong for the file, and the CLI was inheriting the default for both.

Neither emitted page carries a ``sensitivity:`` frontmatter key, so Quartz's
``RemoveConfidential`` plugin — which drops a page by reading that key — has
nothing to read and publishes them. ``reviews/`` and ``daily/`` are not in
``quartz.config.ts``'s ``ignorePatterns`` either. So the page IS the egress.

**This is body egress, not only titles.** Both reports reach ``documents.content``
through ``iter_action_item_docs``, which parses task text out of the body and
republishes it one item at a time — see that function's docstring.

Every absence assertion below is paired with a permissive control proving the same
fixture DOES surface the confidential string when the gate is off, so no "not in"
here can be satisfied by an empty page. All fixture data is synthetic.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli
from brain.activity import current_iso_week
from brain.sensitivity import CONFIDENTIAL

runner = CliRunner()

#: The header date for ``brain brief`` — fixed so the page path is deterministic.
BRIEF_DATE = "2026-06-09"

#: Distinctive synthetic strings. Distinctive so a substring hit is unambiguous
#: evidence rather than a near-miss on some shared word.
CONF_TITLE = "Wind-down memo (synthetic confidential)"
NORMAL_TITLE = "Roadmap notes (synthetic normal)"
CONF_ITEM = "escalate the synthetic wind-down to counsel"
NORMAL_ITEM = "draft the synthetic roadmap one-pager"


def _mark_confidential(conn: psycopg.Connection, doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
        (CONFIDENTIAL, doc_id),
    )


def _interact_now(conn: psycopg.Connection, doc_id: str) -> None:
    """One interaction at NOW() — lands the doc in the current ISO week window."""
    conn.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, 'opened', 'cli')",
        (doc_id,),
    )


def _action_items_doc(conn: psycopg.Connection, title: str, item: str) -> str:
    """A ``krisp_action_items`` document whose BODY carries one open item."""
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES (%s, %s, %s, 'krisp_action_items') RETURNING id::text",
            (title, f"- [ ] {item}\n", str(uuid.uuid4())),
        ).fetchone()[0]
    )


@pytest.fixture
def seeded(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """One normal + one confidential doc, each with a title leg and a body leg.

    The two docs differ ONLY in ``sensitivity``, so any difference in what a
    surface emits about them is attributable to the gate and to nothing else.
    The normal pair is what makes every ``not in`` assertion non-vacuous.
    """
    normal = seed_doc(title=NORMAL_TITLE, content="normal body")
    conf = seed_doc(title=CONF_TITLE, content="confidential body")
    _mark_confidential(test_db, conf)
    _interact_now(test_db, normal)
    _interact_now(test_db, conf)

    _action_items_doc(test_db, NORMAL_TITLE, NORMAL_ITEM)
    conf_items = _action_items_doc(test_db, CONF_TITLE, CONF_ITEM)
    _mark_confidential(test_db, conf_items)


# ---------------------------------------------------------------------------
# HIGH-1 — brain review weekly
# ---------------------------------------------------------------------------


def test_review_weekly_permissive_json_shows_confidential(seeded: None) -> None:
    """CONTROL for the page assertions below — the ungated payload DOES leak.

    ``--json`` returns before the emit block, so it is a terminal-only surface
    and stays permissive by design. Its job here is to prove the fixture puts
    both the confidential TITLE and the confidential BODY text into an ungated
    report, so the page test's ``not in`` cannot pass on an empty page.
    """
    result = runner.invoke(cli.app, ["review", "weekly", "--no-graph", "--json"])

    assert result.exit_code == 0, result.stdout
    blob = json.dumps(json.loads(result.stdout), ensure_ascii=False)
    assert CONF_TITLE in blob
    assert CONF_ITEM in blob
    assert NORMAL_TITLE in blob


def test_review_weekly_page_withholds_confidential(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published page must carry the normal doc and NOT the confidential one."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(cli.app, ["review", "weekly", "--no-graph"])

    assert result.exit_code == 0, result.stdout
    page = (tmp_path / "reviews" / f"{current_iso_week()}.md").read_text(
        encoding="utf-8"
    )
    # Non-vacuity first: if these fail the fixture is broken and the two
    # withholding assertions below would be meaningless.
    assert NORMAL_TITLE in page
    assert NORMAL_ITEM in page
    # The leak.
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page


def test_review_weekly_terminal_stays_permissive_while_page_does_not(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DIVERGENCE is the design, asserted in a single invocation.

    The operator's own terminal keeps counting their own confidential activity
    (``render_weekly_rich`` prints section counts); the file that publishes does
    not include it. Asserting both from one run is what makes this a statement
    about two audiences rather than two unrelated behaviours.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(cli.app, ["review", "weekly", "--no-graph"])

    assert result.exit_code == 0, result.stdout
    assert "activity: 2" in result.stdout
    page = (tmp_path / "reviews" / f"{current_iso_week()}.md").read_text(
        encoding="utf-8"
    )
    assert "## Activity (1 docs interacted with)" in page


# ---------------------------------------------------------------------------
# HIGH-2 — brain brief --wiki
# ---------------------------------------------------------------------------


def _brief_page(root: Path) -> str:
    return (root / "daily" / "2026" / f"{BRIEF_DATE}-brief.md").read_text(
        encoding="utf-8"
    )


def test_brief_permissive_json_shows_confidential(seeded: None) -> None:
    """CONTROL — the ungated brief payload DOES carry both legs."""
    result = runner.invoke(
        cli.app, ["brief", "--no-enrich", "--json", "--date", BRIEF_DATE]
    )

    assert result.exit_code == 0, result.stdout
    blob = json.dumps(json.loads(result.stdout), ensure_ascii=False)
    assert CONF_TITLE in blob
    assert CONF_ITEM in blob
    assert NORMAL_TITLE in blob


def test_brief_page_withholds_confidential(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published daily brief must carry the normal doc and not the other."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["brief", "--no-enrich", "--wiki", "--date", BRIEF_DATE]
    )

    assert result.exit_code == 0, result.stdout
    page = _brief_page(tmp_path)
    assert NORMAL_TITLE in page
    assert NORMAL_ITEM in page
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page


def test_brief_terminal_stays_permissive_while_page_does_not(
    seeded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One invocation, two audiences — the terminal still shows the user's own docs.

    ``_print_brief`` prints capture TITLES and todo TEXT, so unlike the weekly
    command this divergence is directly visible in both directions from a single
    run: the confidential strings are on stdout and absent from the file.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["brief", "--no-enrich", "--wiki", "--date", BRIEF_DATE]
    )

    assert result.exit_code == 0, result.stdout
    assert CONF_TITLE in result.stdout
    assert CONF_ITEM in result.stdout
    page = _brief_page(tmp_path)
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page
