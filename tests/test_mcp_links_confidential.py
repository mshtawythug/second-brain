"""F6 on the MCP link-graph surfaces: ``brain_backlinks`` / ``brain_links``.

These two reach the same ``vault.graph`` functions the HTTP routes do, and did
so with **no sensitivity argument at all** -- so a hosted model could walk the
link graph and read confidential titles off the neighbours of any document it
could name. Every other F6 retrieval surface in ``mcp_server`` defaults to
excluding.

**Why this file asserts both directions separately, in separate tests.**

The parameter INVERTS across the module boundary:

    mcp_server.py   include_confidential: bool = False   # default EXCLUDE
    vault/graph.py  exclude_confidential: bool = False   # default INCLUDE

so the call sites bridge with ``exclude_confidential=not include_confidential``.
Dropping that ``not`` fails **open**, and the permissive direction only ever
*adds* rows -- every "the neighbour is present" assertion stays green while the
gate is gone. A single test cannot catch that. What catches it is one test that
fails if a confidential neighbour is EVER named by default, and a separate one
that fails if the opt-in stops working. Mutating the bridge to a hardcoded
``False`` reddens only the first; to a hardcoded ``True``, only the second. If
one mutation reddened both, one branch would be unasserted.

**The gate covers neighbours, not the subject.** Asking for the backlinks of a
confidential document still answers -- the caller reached it by id and already
holds its identity, so refusing withholds nothing while breaking navigation.
What must not happen is *enumerating* confidential documents the caller never
named. ``test_backlinks_still_answers_for_a_confidential_subject`` pins that
distinction so a later "tighten the gate" cannot quietly turn this tool from
"answers without naming" into "refuses to answer".

All fixture data is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from tests.conftest import TEST_DATABASE_URL

SUBJECT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
PUBLIC_ID = "bbbbbbbb-0000-4000-8000-000000000002"
CONF_ID = "cccccccc-0000-4000-8000-000000000003"

SUBJECT_TITLE = "Quarterly Planning Hub"
PUBLIC_TITLE = "Public Roadmap Note"
CONF_TITLE = "Confidential Neighbour Note"


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=vault_dir),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    sensitivity: str = "normal",
) -> None:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, sensitivity)
        VALUES (%s, %s, %s, %s, 'note', 'vault', %s)
        """,
        (doc_id, title, f"body of {title}", f"hash-{doc_id}", sensitivity),
    )


def _link(conn: psycopg.Connection[Any], *, src: str, dst: str) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, 'wiki', NULL)
        """,
        (src, dst, f"link-{src[:4]}-{dst[:4]}"),
    )


@pytest.fixture
def link_graph(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,  # noqa: ARG001 — ordering
) -> None:
    """SUBJECT <-> PUBLIC and SUBJECT <-> CONF, links in both directions.

    Both neighbours link *to* the subject and are linked *from* it, so one
    fixture exercises ``brain_backlinks`` and ``brain_links`` symmetrically.
    """
    _make_doc(test_db, doc_id=SUBJECT_ID, title=SUBJECT_TITLE)
    _make_doc(test_db, doc_id=PUBLIC_ID, title=PUBLIC_TITLE)
    _make_doc(test_db, doc_id=CONF_ID, title=CONF_TITLE, sensitivity="confidential")
    for other in (PUBLIC_ID, CONF_ID):
        _link(test_db, src=other, dst=SUBJECT_ID)
        _link(test_db, src=SUBJECT_ID, dst=other)


def _assert_fixture_is_not_vacuous(test_db: psycopg.Connection[Any]) -> None:
    """The confidential neighbour must really be confidential AND really linked.

    Without this, every "it is absent" assertion below could pass because the
    row was never there -- the vacuous pass this repo keeps paying for.
    """
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id=%s", (CONF_ID,)
    ).fetchone()
    assert row is not None and row[0] == "confidential"
    n = test_db.execute(
        "SELECT count(*) FROM links WHERE src_document_id=%s OR dst_document_id=%s",
        (CONF_ID, CONF_ID),
    ).fetchone()
    assert n is not None and n[0] == 2, "confidential neighbour must be linked both ways"


# ---------------------------------------------------------------------------
# brain_backlinks
# ---------------------------------------------------------------------------


def test_backlinks_default_omits_confidential_source(
    test_db: psycopg.Connection[Any], link_graph: None
) -> None:
    """Default must not name a confidential neighbour. Fails OPEN if inverted."""
    _assert_fixture_is_not_vacuous(test_db)

    titles = [r["src_title"] for r in mcp_server.brain_backlinks(SUBJECT_ID)]

    assert PUBLIC_TITLE in titles, (
        "non-vacuity: the public backlink must be returned, or an empty result "
        "would satisfy the assertion below while returning nothing at all"
    )
    assert CONF_TITLE not in titles


def test_backlinks_opt_in_returns_confidential_source(
    test_db: psycopg.Connection[Any], link_graph: None
) -> None:
    """The gate must be a gate, not a deletion. Fails if the bridge is stuck strict."""
    _assert_fixture_is_not_vacuous(test_db)

    titles = [
        r["src_title"]
        for r in mcp_server.brain_backlinks(SUBJECT_ID, include_confidential=True)
    ]

    assert CONF_TITLE in titles
    assert PUBLIC_TITLE in titles


def test_backlinks_still_answers_for_a_confidential_subject(
    test_db: psycopg.Connection[Any], link_graph: None
) -> None:
    """The gate covers NEIGHBOURS, never the subject.

    Pins "answers without naming" against a later tightening that would silently
    turn this into "refuses to answer" -- a different product.
    """
    _assert_fixture_is_not_vacuous(test_db)

    titles = [r["src_title"] for r in mcp_server.brain_backlinks(CONF_ID)]

    assert SUBJECT_TITLE in titles


# ---------------------------------------------------------------------------
# brain_links
# ---------------------------------------------------------------------------


def test_links_default_omits_confidential_target(
    test_db: psycopg.Connection[Any], link_graph: None
) -> None:
    """Default must not name a confidential target. Fails OPEN if inverted."""
    _assert_fixture_is_not_vacuous(test_db)

    titles = [r["dst_title"] for r in mcp_server.brain_links(SUBJECT_ID)]

    assert PUBLIC_TITLE in titles, "non-vacuity: the public target must be returned"
    assert CONF_TITLE not in titles


def test_links_opt_in_returns_confidential_target(
    test_db: psycopg.Connection[Any], link_graph: None
) -> None:
    """Fails if the bridge is stuck strict."""
    _assert_fixture_is_not_vacuous(test_db)

    titles = [
        r["dst_title"]
        for r in mcp_server.brain_links(SUBJECT_ID, include_confidential=True)
    ]

    assert CONF_TITLE in titles
    assert PUBLIC_TITLE in titles
