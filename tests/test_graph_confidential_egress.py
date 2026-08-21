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
    _add_chunk as _add_plain_chunk,
)
from tests.test_mcp_graphrag import (
    _add_mention,
    _build,
    _community_state,
    _FakeCommunityEnricher,
    _insert_document,
    _insert_entity,
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


def test_the_confidential_fixture_is_not_vacuous(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The premise itself, asserted OUTSIDE any ``xfail`` body.

    Every other test here calls :func:`_fixture_is_not_vacuous` inline, which is
    correct for the plain ones — a broken premise turns them red. It is NOT
    correct inside an ``xfail(strict=True)`` body, and that was the defect this
    test exists to close: ``xfail`` records a failure from ANYWHERE in the body
    as expected, so the vacuity guard firing was indistinguishable from the leak
    persisting. Verified by execution before the fix, not reasoned about: with
    ``_mark_confidential`` stubbed to a no-op — nothing in the fixture
    confidential at all — the strict-xfail pin that used to sit below this
    still reported ``1 xfailed`` and exit 0. (That pin is now an ordinary
    assertion; the marker came off when the leak was closed. This guard is kept
    because the body/marker interaction recurs the next time anyone pins
    anything here, and because a premise worth asserting is worth asserting
    whether or not a marker is currently present.)

    The hole was one-way and worth stating precisely, because over-discounting
    the marker would be its own error: strict ``xfail`` still reddens correctly
    when someone CLOSES a gap with a healthy fixture, which is what it was
    chosen for. What it could not do is tell a broken premise from an open leak.
    Hoisting the check into a test with no marker on it restores that: a broken
    fixture now fails HERE, unambiguously, where nothing can absorb it.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)


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
# THE THREE MEMBERSHIP PATHS — pinned as residuals, then CLOSED
# ---------------------------------------------------------------------------
#
# The gate on ``GraphContext.docs`` did not by itself close three sibling paths
# that themes/global populate from separate queries:
#
#   * ``ThemeGroup.doc_ids`` — ``_populate_theme_docs`` via ``_docs_by_entity``.
#   * ``ThemeGroup.summary`` under ``synthesize=true`` — ``_fetch_doc_titles``,
#     whose output goes into a PROMPT, so a leak there is unrecoverable: the
#     titles have left the machine before the summary comes back.
#   * ``CommunityGroup.doc_ids`` in ``global_`` — ``_community_doc_scores``.
#
# All three were pinned here as STRICT xfail rather than left as prose, on the
# argument that a prose remainder gets lost between passes — and this exact
# class, a gate stopping one query short of a sibling path, is what produced
# the finding in the first place.
#
# The pins then did precisely what strict xfail is for. Each path grew an
# ``exclude_confidential`` predicate, every pin flipped to XPASS, and the suite
# went RED until the markers came off — which is how these three became
# ordinary assertions instead of a handoff note nobody read. They are kept as
# POSITIVE gate tests: the paths are closed, and these are what keep them shut.
#
# Each still has a separate UNMARKED non-vacuity sibling. That separation is
# not vestigial. It was the second defect fixed here: the vacuity guard used to
# be called INSIDE an xfail body, where a broken premise and a live leak were
# indistinguishable — both recorded as green.


def test_graphrag_themes_doc_ids_exclude_the_confidential_document(
    confidential_graph: str,
) -> None:
    """``ThemeGroup.doc_ids`` must not enumerate a confidential document.

    Membership is itself derived from the content: an id in this list tells the
    caller the document exists and matched, which is the disclosure even when
    no body or title travels with it.

    Premise guarded by :func:`test_the_confidential_fixture_is_not_vacuous`,
    which carries no marker. Asserting it inline here would put the guard back
    inside a body that once swallowed it.
    """
    payload = mcp_server.brain_graphrag_themes(person="bob")

    enumerated = {i for t in payload["themes"] for i in t["doc_ids"]}
    assert confidential_graph not in enumerated


# --- residual 2: ThemeGroup.summary — confidential TITLES reach the prompt ---


class _RecordingEnricher:
    """Captures the ``doc_titles`` handed to the group-synthesis prompt.

    The leak is what CROSSES the boundary to the model, so the assertion is
    made on the prompt input rather than on the returned summary: a real
    summarizer might or might not echo a title it was given, and that choice is
    the model's, not the gate's. Recording the argument removes the ambiguity —
    and removes any need for a live Ollama.
    """

    model = "fake-model:1b"

    def __init__(self) -> None:
        self.doc_titles: list[str] = []

    def summarize_group(
        self, *, person: str | None, entity_names: list[str], doc_titles: list[str]
    ) -> str | None:
        self.doc_titles.extend(doc_titles)
        return "synthetic summary"


@pytest.fixture
def synthesis_recorder(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
    confidential_graph: str,  # noqa: ARG001 — seeds the graph, then we re-install
) -> _RecordingEnricher:
    """Re-install the MCP state with a recording enricher.

    Depends on ``confidential_graph`` (rather than sitting beside it) so the
    ordering is a data dependency instead of an assumption about fixture
    resolution order: the graph is seeded under ``graph_state``, and only then
    is ``_state`` replaced with one that carries an enricher.
    """
    recorder = _RecordingEnricher()
    monkeypatch.setattr(
        mcp_server, "_state", _make_state(fake_embedder, enricher=recorder)
    )
    return recorder


def test_themes_synthesis_prompt_is_reached_and_carries_titles(
    synthesis_recorder: _RecordingEnricher,
) -> None:
    """Non-vacuity for the pin below, and UNMARKED so it cannot be absorbed.

    ``synthesize=True`` degrades silently to ``summary=None`` when the enricher
    is missing or raises — by design. So a pin asserting "no confidential title
    in the prompt" would pass perfectly for the wrong reason if the prompt were
    never built at all. This asserts the path actually executes and really does
    carry document titles, using the non-confidential one as the witness.
    """
    mcp_server.brain_graphrag_themes(person="bob", synthesize=True)

    assert synthesis_recorder.doc_titles, (
        "the synthesis prompt was never built — themes(synthesize=True) "
        "degraded, so the pin below would pass vacuously"
    )
    assert "Public Scheduling Note" in synthesis_recorder.doc_titles


def test_graphrag_themes_summary_prompt_excludes_confidential_titles(
    synthesis_recorder: _RecordingEnricher,
) -> None:
    """No confidential TITLE may reach the group-synthesis prompt.

    Asserted on what crosses the boundary — the argument handed to
    ``summarize_group`` — rather than on the returned summary. Whether a model
    echoes a title it was given is the model's choice; whether it was given the
    title at all is the gate's. Premise guarded by the test above.
    """
    mcp_server.brain_graphrag_themes(person="bob", synthesize=True)

    assert CONF_TITLE not in synthesis_recorder.doc_titles


# --- residual 3: CommunityGroup.doc_ids in global_ --------------------------


def _relate(conn: psycopg.Connection[Any], a: str, b: str, weight: float) -> None:
    """One ``co_occurs`` edge, endpoints ordered as the schema expects."""
    src_id, dst_id = sorted((a, b))
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "VALUES ('default', %s, %s, 'co_occurs', %s, 1, 1)",
        (src_id, dst_id, weight),
    )


@pytest.fixture
def confidential_communities(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> str:
    """Two triangles + a weak bridge, ONE community's document confidential.

    ``confidential_graph`` cannot serve this pin: its three people form a path
    (alice-bob, bob-carol), and community detection needs denser structure to
    resolve stable groups, so a global assertion over it would rest on whether
    Louvain happened to emit anything. This mirrors the shape
    ``test_mcp_graphrag._seed_communities_corpus`` already uses for the global
    mode — two clean triangles, deterministic — and marks one cluster's
    document confidential. Returns that document's id.
    """
    _community_state(monkeypatch, _FakeCommunityEnricher())
    cluster_one = [
        _insert_entity(test_db, "default", f"P-{i}", f"p-{i}") for i in range(3)
    ]
    cluster_two = [
        _insert_entity(test_db, "default", f"Q-{i}", f"q-{i}") for i in range(3)
    ]
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        _relate(test_db, cluster_one[a], cluster_one[b], 0.8)
        _relate(test_db, cluster_two[a], cluster_two[b], 0.8)
    _relate(test_db, cluster_one[2], cluster_two[0], 0.05)  # weak bridge

    conf_id = _insert_document(test_db, CONF_TITLE)
    normal_id = _insert_document(test_db, "Cluster Two Doc")
    for entity in cluster_one:
        _add_mention(test_db, "default", entity, conf_id)
    for entity in cluster_two:
        _add_mention(test_db, "default", entity, normal_id)
    _add_plain_chunk(test_db, conf_id, f"Cluster one body filed under {BODY_MARKER}.")
    _add_plain_chunk(test_db, normal_id, "Cluster two discussion body.")
    _mark_confidential(test_db, conf_id)

    mcp_server.brain_graphrag_communities_build()
    return conf_id


def test_the_confidential_community_fixture_is_not_vacuous(
    test_db: psycopg.Connection[Any], confidential_communities: str
) -> None:
    """Premise for the gate below: confidential AND really clustered.

    Two things can make that gate vacuous, and neither was visible from inside
    the ``xfail`` body it used to be asserted in: the document not being
    confidential, and community detection emitting nothing at all (an empty
    ``communities`` list enumerates no ids and would read as the gate working).
    """
    _fixture_is_not_vacuous(test_db, confidential_communities)

    payload = mcp_server.brain_graphrag_search(query="Cluster", mode="global")
    assert payload["communities"], (
        "no communities materialized — the gate below would pass vacuously"
    )


def test_graphrag_global_community_doc_ids_exclude_the_confidential_document(
    confidential_communities: str,
) -> None:
    """``CommunityGroup.doc_ids`` must not enumerate a confidential document.

    The third dispatch branch, with its own doc-assembly query
    (``_community_doc_scores``) — a predicate threaded through themes and not
    through global would be invisible to every themes test above. Premise
    guarded by the test above.
    """
    payload = mcp_server.brain_graphrag_search(query="Cluster", mode="global")

    enumerated = {doc_id for c in payload["communities"] for doc_id in c["doc_ids"]}
    assert confidential_communities not in enumerated
