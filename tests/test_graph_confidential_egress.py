"""Confidential documents must not reach a hosted model through the GRAPH surface (F6).

The F6 gate was built on the hybrid-search surface (``brain_search`` /
``brain_recall`` / ``brain_show`` / ``brain_list``) and stopped at that module
boundary. Graph retrieval is a SECOND, parallel retrieval surface that reuses
:class:`brain.search.SearchResult` — including its ``snippet``, which carries
raw ``chunks.content``. So ``brain_graphrag_search`` / ``_themes`` / ``_entity``
returned confidential BODY TEXT to a hosted model while every search-surface
test stayed green.

Two distinct leaks are pinned here, because closing one leaves the other:

1. **Body text via the graph ``docs`` snippet.** The severe one — the graph
   path builds its snippet from ``chunks.content`` in
   :func:`brain.graph_rag._retrieval_common._build_doc_results`, the single
   funnel all three graph modes share.
2. **Titles via ``brain_orphans``.** ``vault.graph.orphans()`` had no
   ``exclude_confidential`` parameter at all — the flag was added to exactly
   the two link functions someone named, so the enumeration miss propagated
   into the graph layer itself and ``brain_orphans`` could not be gated even
   in principle.

Assertions serialize the WHOLE response rather than the fields we thought of:
a field-by-field assertion only covers the leaks someone already imagined.

Every fixture is checked NON-VACUOUS before it is trusted — a withholding test
against an empty snippet passes for the wrong reason and reads as coverage.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from tests.test_mcp_graphrag import (
    _build,
    _make_state,
    _seed_directory,
    _seed_gmail_doc,
)

#: Present ONLY in the confidential body, never in any query — so finding it in
#: a response is unambiguous evidence that body text escaped, with no false
#: positive from the caller's own echoed input.
BODY_MARKER = "quokkavolt"

#: The confidential document's title. Distinct from the marker so a title leak
#: and a body leak are told apart rather than collapsed into one assertion.
CONF_TITLE = "Confidential Wind-Down Memo"


@pytest.fixture
def graph_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps schema + graph fresh
    fake_embedder: object,
) -> mcp_server._State:
    """Install a graphrag MCP state.

    Defined here rather than imported from ``test_mcp_graphrag``: a pytest
    fixture is only visible to the module that DEFINES it, and re-binding an
    imported one shadows the import (ruff F811). Reuses that module's
    ``_make_state`` so the ``Config`` stays identical — concepts pinned off,
    suppression disabled — and these tests cannot drift from it.
    """
    state = _make_state(fake_embedder)
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


def _add_chunk(
    conn: psycopg.Connection[Any], doc_id: str, content: str, idx: int = 0
) -> None:
    """Give a document a real chunk so the graph snippet path has body to leak."""
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, title_text) "
        "VALUES (%s, %s, %s, %s)",
        (doc_id, idx, content, CONF_TITLE),
    )


def _mark_confidential(conn: psycopg.Connection[Any], doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity='confidential' WHERE id=%s", (doc_id,)
    )


@pytest.fixture
def confidential_graph(
    test_db: psycopg.Connection[Any],
    graph_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> str:
    """A person triangle where ONE document is confidential. Returns its id.

    ``bob`` co-occurs with ``alice`` in the confidential doc and with ``carol``
    in a normal one, so a query seeded on ``bob`` reaches BOTH — an empty
    result set therefore cannot be mistaken for "the traversal found nothing".
    """
    _seed_directory(
        test_db,
        [
            ("alice", "alice@example.com"),
            ("bob", "bob@example.com"),
            ("carol", "carol@example.com"),
        ],
    )
    conf_id = _seed_gmail_doc(
        test_db,
        external_id="conf-1",
        participants=[("alice", "alice@example.com"), ("bob", "bob@example.com")],
        content=f"Wind-down terms filed under {BODY_MARKER}. " * 6,
    )
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", (CONF_TITLE, conf_id)
    )
    _add_chunk(test_db, conf_id, f"Wind-down terms filed under {BODY_MARKER}. " * 6)
    _mark_confidential(test_db, conf_id)

    normal_id = _seed_gmail_doc(
        test_db,
        external_id="norm-1",
        participants=[("bob", "bob@example.com"), ("carol", "carol@example.com")],
        content="Routine scheduling note for the team. " * 6,
    )
    test_db.execute(
        "UPDATE documents SET title='Public Scheduling Note' WHERE id=%s",
        (normal_id,),
    )
    _add_chunk(test_db, normal_id, "Routine scheduling note for the team. " * 6)
    _build(test_db)
    return conf_id


def _fixture_is_not_vacuous(conn: psycopg.Connection[Any], doc_id: str) -> None:
    """Guard: the doc is really confidential AND really has body to leak.

    Without this, every assertion below could pass because there was nothing
    to leak — the exact vacuous-pass class these tests exist to avoid.
    """
    row = conn.execute(
        "SELECT d.sensitivity, d.title, "
        "(SELECT string_agg(c.content, ' ') FROM chunks c WHERE c.document_id = d.id) "
        "FROM documents d WHERE d.id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "confidential", "fixture must be confidential"
    assert row[1] == CONF_TITLE, "fixture must carry the marked title"
    assert row[2] and BODY_MARKER in row[2], (
        "fixture must have a NON-EMPTY chunk containing the body marker, "
        "or the snippet assertions pass vacuously"
    )


# ---------------------------------------------------------------------------
# brain_graphrag_search / _themes / _entity — BODY TEXT via the docs snippet
# ---------------------------------------------------------------------------


def test_graphrag_search_response_contains_no_confidential_body_anywhere(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Serialize the WHOLE response — not the fields we happened to think of."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_graphrag_search_returns_no_confidential_row(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Membership is itself derived from the content — the oracle.

    Redacting the snippet but keeping the row still proves the document exists
    and matched, which is why its absence is asserted separately from the body.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")

    assert CONF_TITLE not in [d["title"] for d in payload["docs"]]


def test_graphrag_search_still_returns_normal_documents(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The filter must exclude one tier, not break graph retrieval."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")

    assert "Public Scheduling Note" in [d["title"] for d in payload["docs"]]


def test_graphrag_search_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The escape hatch exists and is explicit, mirroring ``brain_search``."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(
        query="bob", mode="local", include_confidential=True
    )

    assert CONF_TITLE in [d["title"] for d in payload["docs"]]


def test_graphrag_entity_response_contains_no_confidential_body(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``brain_graphrag_entity`` reuses the local path — same funnel, same gate."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_entity(name="bob")

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_graphrag_entity_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_entity(name="bob", include_confidential=True)

    assert CONF_TITLE in [d["title"] for d in payload["docs"]]


# ---------------------------------------------------------------------------
# brain_orphans — TITLES, and the missing parameter underneath it
# ---------------------------------------------------------------------------


def _orphan_docs(conn: psycopg.Connection[Any]) -> str:
    """One confidential vault orphan + one normal vault orphan. Returns conf id."""
    rows = []
    for title, sensitivity in (
        (CONF_TITLE, "confidential"),
        ("Public Loose Note", "normal"),
    ):
        row = conn.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, kind, vault_path, sensitivity) "
            "VALUES (%s, %s, %s, 'note', 'vault', %s, %s) RETURNING id::text",
            (title, f"body of {title}", f"hash-{title}", f"{title}.md", sensitivity),
        ).fetchone()
        assert row is not None
        rows.append(str(row[0]))
    return rows[0]


def test_orphans_excludes_confidential_when_asked(
    test_db: psycopg.Connection[Any],
) -> None:
    """The parameter must EXIST and must actually filter (it did not exist)."""
    from brain.vault.graph import orphans

    _orphan_docs(test_db)

    titles = [n.title for n in orphans(test_db, exclude_confidential=True)]

    assert "Public Loose Note" in titles, "must not break the normal listing"
    assert CONF_TITLE not in titles


def test_orphans_includes_confidential_by_default(
    test_db: psycopg.Connection[Any],
) -> None:
    """``vault.graph`` defaults to INCLUDE — the CLI sits inside the boundary.

    This is the opposite-default half of the convention: ``mcp_server`` says
    ``include_confidential=False`` (exclude), ``vault.graph`` says
    ``exclude_confidential=False`` (include). Inverting the bridge flips the
    gate silently, so both directions are pinned.
    """
    from brain.vault.graph import orphans

    _orphan_docs(test_db)

    titles = [n.title for n in orphans(test_db)]

    assert CONF_TITLE in titles


def test_brain_orphans_omits_confidential_title_by_default(
    test_db: psycopg.Connection[Any],
    graph_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> None:
    _orphan_docs(test_db)

    payload = mcp_server.brain_orphans()

    blob = json.dumps(payload, default=str)
    assert "Public Loose Note" in blob, "must not break the normal listing"
    assert CONF_TITLE not in blob


def test_brain_orphans_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any],
    graph_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> None:
    _orphan_docs(test_db)

    payload = mcp_server.brain_orphans(include_confidential=True)

    assert CONF_TITLE in json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# fuse mode — the SECOND leg, which gating the graph leg alone leaves open
# ---------------------------------------------------------------------------


def test_graphrag_fuse_hybrid_leg_does_not_reintroduce_confidential(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Fuse returns the RRF UNION of a graph leg and a hybrid leg.

    Gating only the graph leg looks correct and tests green on every local /
    themes / entity assertion above, while the confidential document walks
    straight back in through ``hybrid_search`` — same document, same body,
    different code path. The query is chosen to match the confidential
    document's TITLE text so the hybrid leg genuinely reaches it; the
    companion opt-in test below is what proves that it does.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(query="wind-down", mode="fuse")

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob
    assert CONF_TITLE not in [d["title"] for d in payload["docs"]]


def test_graphrag_fuse_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Non-vacuity for the test above AND the opt-in direction for fuse.

    If this fails, the fuse leg never reached the confidential document at all
    and the withholding assertion above was passing for the wrong reason.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(
        query="wind-down", mode="fuse", include_confidential=True
    )

    assert CONF_TITLE in [d["title"] for d in payload["docs"]]


def test_graphrag_fuse_graph_leg_stays_gated(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The OTHER fuse leg, reached by an entity-seeded query rather than FTS.

    ``wind-down`` above matches the confidential document's title text, so it
    exercises the hybrid leg; it resolves no entity, so the graph leg stays
    empty and the graph-leg wiring goes untested. ``bob`` is the reverse — it
    seeds the traversal that reaches the confidential document. Both queries
    are needed: with only one of them, half the fuse wiring is unasserted and
    a mutation to it passes silently.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(query="bob", mode="fuse")

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob
    assert CONF_TITLE not in [d["title"] for d in payload["docs"]]


def test_graphrag_fuse_graph_leg_reaches_the_document(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Non-vacuity for the test above: ``bob`` really does reach it via fuse."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(
        query="bob", mode="fuse", include_confidential=True
    )

    assert CONF_TITLE in [d["title"] for d in payload["docs"]]


# ---------------------------------------------------------------------------
# themes mode — a third dispatch branch with its own doc-assembly path
# ---------------------------------------------------------------------------


def test_graphrag_themes_response_contains_no_confidential_body(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``themes`` assembles its ``docs`` through a different call chain.

    Local / entity / fuse all reach ``_build_doc_results`` via
    ``_retrieve_local``; themes reaches it via ``_populate_theme_docs``. A gate
    threaded through one chain and not the other is invisible to every test
    above.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(person="bob")

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob
    assert CONF_TITLE not in [d["title"] for d in payload["docs"]]


def test_graphrag_themes_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Non-vacuity for the test above: themes really does reach the document."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(person="bob", include_confidential=True)

    assert CONF_TITLE in [d["title"] for d in payload["docs"]]


# ---------------------------------------------------------------------------
# KNOWN UNFIXED RESIDUAL — themes membership, recorded as executable xfail
# ---------------------------------------------------------------------------
#
# The gate above closed ``GraphContext.docs`` for every mode. It did NOT close
# two other paths that themes/global populate from separate queries:
#
#   * ``ThemeGroup.doc_ids`` — built in ``_populate_theme_docs`` from
#     ``_docs_by_entity``, which has no sensitivity predicate.
#   * ``ThemeGroup.summary`` under ``synthesize=true`` — built from
#     ``themes._fetch_doc_titles`` (themes.py), also unfiltered, so confidential
#     TITLES feed the synthesis prompt.
#
# ``CommunityGroup.doc_ids`` in ``global_`` has the same shape.
#
# These are pinned as STRICT xfail rather than described in a handoff note: a
# prose remainder gets lost between passes, and this exact class — a gate that
# stopped one query short of a sibling path — is what produced this finding in
# the first place. Strict means that whoever closes the gap gets a RED test
# telling them to delete the marker, so the record cannot silently rot.


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: ThemeGroup.doc_ids bypasses _build_doc_results; "
    "confidential document ids are still enumerated to the caller.",
)
def test_graphrag_themes_doc_ids_still_leak_membership(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(person="bob")

    leaked = {i for t in payload["themes"] for i in t["doc_ids"]}
    assert confidential_graph not in leaked
