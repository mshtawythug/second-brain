"""``brain_review_weekly``'s EMIT path must not publish confidential docs (F6).

``2b2b321`` closed this same hole on the CLI and its message asserted "The MCP
twins of both functions gate correctly; the CLI was the outlier." **That is
false for the emit path, and this module is the correction.** The MCP tool
gates its RETURN VALUE on the caller's ``include_confidential`` — which is
right, the caller is asking — and then hands that same permissively-built report
to ``emit_weekly_page``. One call with ``include_confidential=true`` therefore
published to ``<vault>/reviews/<week>.md`` exactly what ``2b2b321`` had just
stopped the CLI from publishing. The tool's docstring documented it as
deliberate ("NOTE the emitted page follows the same gate"), which is the
two-audience conflation ``2b2b321`` named: a caller's READ lens applied to a
published FILE.

``render_weekly_md`` emits no ``sensitivity:`` frontmatter key, so Quartz's
``RemoveConfidential`` plugin has nothing to read, and ``reviews/`` is in
neither Quartz config's ``ignorePatterns``. The page IS the egress boundary.

**Body egress, not only titles.** ``todo.iter_action_item_docs`` selects
``documents.content`` and parses task text out of it, so the open-loops section
republishes body text one item at a time.

**``brain_brief`` is NOT affected, and that is measured here rather than
assumed** — see ``test_brain_brief_writes_nothing_to_the_vault``. It has no
vault write at all; its permissive payload reaches the caller and stops there.

Every ``not in`` assertion is paired with a control proving the same fixture DOES
put the string where the assertion looks — including one that writes the
UNGATED report to the page medium itself, so "absent from the page" is measured
against a page demonstrably able to carry it. All fixture data is synthetic.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.activity import current_iso_week
from brain.sensitivity import CONFIDENTIAL

from .test_mcp_review_scan import _install_state

#: Distinctive synthetic strings — a substring hit is unambiguous evidence.
CONF_TITLE = "Wind-down memo (synthetic confidential)"
NORMAL_TITLE = "Roadmap notes (synthetic normal)"
CONF_ITEM = "escalate the synthetic wind-down to counsel"
NORMAL_ITEM = "draft the synthetic roadmap one-pager"


def _blob(payload: Any) -> str:
    """Serialize a whole MCP response for substring assertions.

    ``ensure_ascii=False`` is load-bearing: the default escapes non-ASCII, so a
    title carrying an em-dash would serialize escaped and a ``not in`` check
    would PASS on a payload that contains it. See ``test_mcp_review_confidential``.
    """
    return json.dumps(payload, default=str, ensure_ascii=False)


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
def emit_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> mcp_server._State:
    """MCP state whose ``vault_path`` is a throwaway tmp dir."""
    return _install_state(monkeypatch, fake_embedder, tmp_path)


@pytest.fixture
def seeded(test_db: psycopg.Connection, seed_doc: Callable[..., str]) -> None:
    """One normal + one confidential doc, each with a title leg and a body leg.

    The two differ ONLY in ``sensitivity``, so any difference in what a surface
    emits is attributable to the gate and nothing else. The normal pair is what
    makes every ``not in`` non-vacuous.
    """
    normal = seed_doc(title=NORMAL_TITLE, content="normal body")
    conf = seed_doc(title=CONF_TITLE, content="confidential body")
    _mark_confidential(test_db, conf)
    _interact_now(test_db, normal)
    _interact_now(test_db, conf)

    _action_items_doc(test_db, NORMAL_TITLE, NORMAL_ITEM)
    conf_items = _action_items_doc(test_db, CONF_TITLE, CONF_ITEM)
    _mark_confidential(test_db, conf_items)


def _page(root: Path) -> str:
    return (root / "reviews" / f"{current_iso_week()}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Controls — the fixture, and the page medium, can both carry the strings
# ---------------------------------------------------------------------------


def test_permissive_return_value_carries_confidential(
    emit_state: mcp_server._State,  # noqa: ARG001 — installs state
    seeded: None,
) -> None:
    """CONTROL — with ``include_confidential=true`` the RETURN VALUE leaks by design.

    The caller asked; the caller is inside the trust boundary. This is not the
    bug, and it must keep working after the fix — it is the other half of the
    two-audience split.
    """
    payload = mcp_server.brain_review_weekly(
        no_graph=True, emit=False, include_confidential=True
    )

    blob = _blob(payload)
    assert CONF_TITLE in blob
    assert CONF_ITEM in blob
    assert NORMAL_TITLE in blob


def test_ungated_report_written_to_the_page_medium_does_carry_confidential(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """CONTROL for every page ``not in`` below — the PAGE can carry these strings.

    Builds the permissive report the pre-fix tool handed to ``emit_weekly_page``
    and writes it through the same renderer. Without this, "the confidential
    title is not in the page" could be satisfied by a renderer that drops the
    sections entirely, or by an empty week.
    """
    from datetime import date as date_cls

    from brain.db import connect
    from brain.review import build_weekly_report, emit_weekly_page

    with connect(emit_state.cfg.database_url) as conn:
        permissive = build_weekly_report(
            conn,
            emit_state.cfg,
            week=current_iso_week(),
            generated_on=date_cls.today(),
            no_graph=True,
            enricher=None,
            exclude_confidential=False,
        )
    emit_weekly_page(emit_state.cfg.vault_path, permissive)

    page = _page(emit_state.cfg.vault_path)
    assert CONF_TITLE in page
    assert CONF_ITEM in page
    assert NORMAL_TITLE in page


# ---------------------------------------------------------------------------
# The leak — one MCP call publishes what the CLI was just stopped from publishing
# ---------------------------------------------------------------------------


def test_emitted_page_withholds_confidential_even_when_caller_opts_in(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """THE FIX. ``include_confidential=true`` is a READ lens, not a publish lens."""
    mcp_server.brain_review_weekly(
        no_graph=True, emit=True, include_confidential=True
    )

    page = _page(emit_state.cfg.vault_path)
    # Non-vacuity first — if these fail the fixture is broken and the two
    # withholding assertions below would mean nothing.
    assert NORMAL_TITLE in page
    assert NORMAL_ITEM in page
    # The leak.
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page


def test_return_value_stays_permissive_while_the_page_does_not(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """The DIVERGENCE is the design, asserted from ONE invocation.

    Two audiences, one call: the caller who asked keeps their complete
    retrospective; the file Quartz serves does not. Asserting both from a single
    return makes this a statement about the split rather than two unrelated
    behaviours that happen to be green.
    """
    payload = mcp_server.brain_review_weekly(
        no_graph=True, emit=True, include_confidential=True
    )

    blob = _blob(payload)
    assert CONF_TITLE in blob
    assert CONF_ITEM in blob

    page = _page(emit_state.cfg.vault_path)
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page


def test_default_lens_page_and_payload_both_withhold(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """Default ``include_confidential=false``: nothing regressed on the gated path.

    Here the returned report is ALREADY built with ``exclude_confidential=True``,
    so the fix must reuse it rather than pay for a second identical build — and
    the page must still be gated.
    """
    payload = mcp_server.brain_review_weekly(no_graph=True, emit=True)

    blob = _blob(payload)
    assert NORMAL_TITLE in blob
    assert CONF_TITLE not in blob
    assert CONF_ITEM not in blob

    page = _page(emit_state.cfg.vault_path)
    assert NORMAL_TITLE in page
    assert NORMAL_ITEM in page
    assert CONF_TITLE not in page
    assert CONF_ITEM not in page


def test_emit_false_writes_no_page(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """``emit=false`` must not create the page at all — the write is one condition."""
    mcp_server.brain_review_weekly(
        no_graph=True, emit=False, include_confidential=True
    )

    assert not (emit_state.cfg.vault_path / "reviews").exists()


# ---------------------------------------------------------------------------
# brain_brief — measured, not inherited
# ---------------------------------------------------------------------------


def test_brain_brief_writes_nothing_to_the_vault(
    emit_state: mcp_server._State,
    seeded: None,  # noqa: ARG001 — seeds the DB
) -> None:
    """``brain_brief`` has no emit path, so its permissive payload is terminal-only.

    Its CLI twin publishes to ``<vault>/daily/<YYYY>/<date>-brief.md`` under
    ``--wiki`` and was fixed in ``2b2b321``; the MCP tool has no such flag. The
    permissive payload is asserted first so this is "wrote nothing DESPITE
    carrying the confidential strings", not "wrote nothing because it had
    nothing".
    """
    before = sorted(p for p in emit_state.cfg.vault_path.rglob("*") if p.is_file())

    payload = mcp_server.brain_brief(no_enrich=True, include_confidential=True)

    blob = _blob(payload)
    assert CONF_TITLE in blob
    assert CONF_ITEM in blob

    after = sorted(p for p in emit_state.cfg.vault_path.rglob("*") if p.is_file())
    assert after == before
