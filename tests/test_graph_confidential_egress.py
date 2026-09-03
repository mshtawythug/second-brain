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

import dataclasses
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


def _blob(payload: object) -> str:
    """Serialize a whole response for substring assertions.

    ``ensure_ascii=False`` is load-bearing, not tidiness. The default escapes
    every non-ASCII character, so a title containing an em-dash serializes as
    ``Wind\\u2014Down`` and a substring check for the title silently fails to
    match. On a ``not in`` assertion that is worse than a bug: it PASSES,
    reporting "no leak" about a payload that contains the string in escaped
    form.

    This module's markers are ASCII TODAY, so every bare ``json.dumps`` here was
    correct at the moment it was written — which is exactly why the hazard is
    worth removing rather than arguing about. A sibling file's ``CONF_TITLE``
    already carries an em-dash; one copied constant, and every withholding
    assertion in this file turns green-forever with no test failing to say so.
    Centralising the flag means that copy cannot silently disarm them.
    """
    return json.dumps(payload, default=str, ensure_ascii=False)


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

    blob = _blob(payload).lower()
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


def test_graphrag_search_include_confidential_carries_body_into_the_payload(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """POSITIVE CONTROL for every ``BODY_MARKER not in blob`` assertion here.

    Those five assertions (local / entity / both fuse legs / themes) read a
    SERIALIZED payload, and the only thing that puts body text into one is
    ``docs[].snippet`` via :func:`brain.format._graph_doc_json`. Nothing else
    in this file pinned that the field exists at all.

    :func:`_fixture_is_not_vacuous` is the near miss, and the reason "absence
    needs presence" is not yet the whole rule: it reads the ``chunks`` ROW, so
    it proves the marker is in the DATABASE, not that the database reaches the
    wire. Drop ``snippet`` from the wire shape and the fixture guard stays
    green while all five withholding assertions pass forever proving nothing.

    So the rule this closes is the stronger one: **the presence check must sit
    at the SAME LAYER the absence is asserted over.** Hence the same call, the
    same ``_blob(...).lower()`` blob, and the opposite
    assertion — the exact mirror of the test at the top of this section.

    This is the same failure shape as the ``ensure_ascii`` defect fixed
    alongside it, arriving from the other side: that one made a withholding
    assertion blind to content that WAS present; this one would make it blind
    to the absence of a content path. Both report "no leak" truthfully and
    meaninglessly.

    Verified two-sided rather than argued: deleting the ``"snippet"`` key from
    ``_graph_doc_json`` reddens THIS test and leaves all five
    ``BODY_MARKER not in blob`` assertions green.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_search(
        query="bob", mode="local", include_confidential=True
    )

    blob = _blob(payload).lower()
    assert BODY_MARKER in blob, (
        "the graph payload carried no body text at all, so every "
        "'BODY_MARKER not in blob' assertion in this file is vacuous"
    )


def test_graphrag_entity_response_contains_no_confidential_body(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``brain_graphrag_entity`` reuses the local path — same funnel, same gate."""
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_entity(name="bob")

    blob = _blob(payload).lower()
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

    blob = _blob(payload)
    assert "Public Loose Note" in blob, "must not break the normal listing"
    assert CONF_TITLE not in blob


def test_brain_orphans_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any],
    graph_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> None:
    _orphan_docs(test_db)

    payload = mcp_server.brain_orphans(include_confidential=True)

    assert CONF_TITLE in _blob(payload)


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

    blob = _blob(payload).lower()
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

    blob = _blob(payload).lower()
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

    blob = _blob(payload).lower()
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


def test_graphrag_themes_doc_ids_opt_back_in(
    test_db: psycopg.Connection[Any],
    confidential_graph: str,
) -> None:
    """POSITIVE CONTROL for the themes ``doc_ids`` gate above.

    The comment block heading this section enumerates THREE residual membership
    paths. Two of them grew a control; this one did not, and the asymmetry was
    invisible because the file reads as though the section were finished. The
    community sibling — :func:`test_graphrag_global_community_doc_ids_opt_back_in`
    — spells the rule out, and this is that rule applied to the themes branch it
    was never applied to.

    ``…_exclude_the_confidential_document`` reads a set built by flattening
    ``themes[].doc_ids``. Nothing pinned that the list is populated at all, so
    blanking ``doc_ids`` in :func:`brain.format._theme_json` — the line directly
    above the ``_community_json`` one the sibling names — left that gate green.

    :func:`test_the_confidential_fixture_is_not_vacuous` does not cover this: it
    guards that the document is confidential and reachable, which is one field
    short of the ``doc_ids`` list the absence is asserted over. Same
    one-boundary-short shape, different branch.

    Verified two-sided rather than argued: replacing ``list(theme.doc_ids)``
    with ``[]`` in ``_theme_json`` reddens THIS test at its own line, while
    ``test_graphrag_themes_doc_ids_exclude_the_confidential_document`` still
    passes.
    """
    _fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(person="bob", include_confidential=True)

    enumerated = {i for t in payload["themes"] for i in t["doc_ids"]}
    assert confidential_graph in enumerated, (
        "no theme enumerated ANY document id, so the withholding assertion "
        f"beside this one is vacuous: enumerated={enumerated}"
    )


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


def test_graphrag_global_community_doc_ids_opt_back_in(
    test_db: psycopg.Connection[Any], confidential_communities: str
) -> None:
    """POSITIVE CONTROL for the community ``doc_ids`` gate above.

    That gate reads a set built by flattening ``communities[].doc_ids``. Nothing
    pinned that the list is populated at all, so blanking ``doc_ids`` in
    :func:`brain.format._community_json` left the gate green — and left it green
    across BOTH files that assert on it.

    The near miss is the reason this needs saying, because it looks like
    coverage: :func:`test_the_confidential_community_fixture_is_not_vacuous`
    guards the premise, but it asserts ``payload["communities"]`` is non-empty,
    which is one FIELD short. Non-empty communities each carrying an empty
    ``doc_ids`` satisfies that guard while ``enumerated`` is empty and the gate
    below proves nothing. A guard one boundary short of the thing it guards is
    the recurring shape on this branch, not a one-off.

    So this is the same rule the body-snippet control states, applied to the
    membership tier: the presence check must sit at the SAME LAYER the absence
    is asserted over — here, the same call, the same flattening expression, the
    flag flipped and the assertion flipped.

    Verified two-sided rather than argued: replacing ``list(community.doc_ids)``
    with ``[]`` in ``_community_json`` reddens THIS test, while
    ``test_graphrag_global_community_doc_ids_exclude_the_confidential_document``
    still passes.
    """
    _fixture_is_not_vacuous(test_db, confidential_communities)

    payload = mcp_server.brain_graphrag_search(
        query="Cluster", mode="global", include_confidential=True
    )

    enumerated = {doc_id for c in payload["communities"] for doc_id in c["doc_ids"]}
    assert confidential_communities in enumerated, (
        "no community enumerated ANY document id, so the withholding "
        "assertion beside this one is vacuous: "
        f"enumerated={enumerated}"
    )


# ---------------------------------------------------------------------------
# graph_data — the FOURTH read, and the two bugs that came with its parameter
# ---------------------------------------------------------------------------
#
# ``graph_data`` grew ``exclude_confidential`` last. Gating only the document
# fetch left two defects that share one root cause: ``keep`` (from
# ``_bfs_frontier``) and ``all_edges`` are BOTH derived from the ungated edge
# set, so the gate on ``all_docs`` never reaches them.
#
#   A. rooted + excluded -> ``KeyError``. ``sorted()`` evaluates its key over
#      EVERY element before the comprehension's trailing ``if`` runs, so
#      ``all_docs[d]`` raises on the first withheld id in the frontier. The
#      ``if`` guard pre-dated the parameter and was never reachable while
#      ``all_docs`` was every document.
#   B. ``include_ingested=True`` + excluded -> no edge filtering AT ALL. That
#      branch sets ``node_filter = None`` and does ``kept_edges =
#      list(all_edges)``. A withheld document still ships its edges, carrying
#      its UUID and — for wiki edges — its title verbatim in ``link_text``.
#
# The two are not independent findings to fix separately: a repair that only
# stops the crash (a ``try/except`` around the sort, or keeping the trailing
# ``if`` and moving on) leaves B standing, because the rooted branch filters
# ``kept_edges`` on ``keep`` rather than on ``all_docs``.
#
# C. Every one of the 19 ``graph_data(`` call sites in the tree — 18 in
#    ``tests/test_graph_queries.py``, one in ``cli.py`` — omits the parameter,
#    which is why the suite stayed green through both. So the withholding
#    assertions below are paired with POSITIVE CONTROLS on the same call with
#    the flag flipped: absence proves nothing unless presence is proven at the
#    same layer.

#: Title of the confidential note in the link fixture. Wiki edges store the
#: link target text verbatim in ``links.link_text``, so this exact string is
#: what a leaked edge carries — a membership oracle plus the title.
LINK_CONF_TITLE = "Confidential Wind-Down Memo"


def _insert_linkable(
    conn: psycopg.Connection[Any],
    title: str,
    *,
    kind: str = "vault",
    sensitivity: str = "normal",
) -> str:
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind, vault_path, sensitivity) "
        "VALUES (%s, %s, %s, 'note', %s, %s, %s) RETURNING id::text",
        (
            title,
            f"body of {title}",
            f"linkhash-{title}",
            kind,
            f"{title}.md" if kind == "vault" else None,
            sensitivity,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _wiki_edge(
    conn: psycopg.Connection[Any], src: str, dst: str, link_text: str
) -> None:
    conn.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s, 'wiki')",
        (src, dst, link_text),
    )


@pytest.fixture
def link_graph(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    """A link graph whose ONLY route to part of itself runs through a secret.

    ``hub -> conf -> beyond`` plus ``hub -> leaf``, and a derived edge pairing
    ``conf`` with ``leaf`` so the derived leg is exercised too, not just wiki.

    Two properties are deliberate. ``hub`` also reaches ``leaf`` directly, so an
    empty-ish result cannot be mistaken for "the traversal found nothing".
    ``beyond`` is INGESTED-tier and hangs off ``conf`` alone, which makes it the
    probe for traversal-through-a-withheld-node: if the frontier is computed
    over ungated edges, ``beyond`` arrives in the node set with no edge to
    explain it — an isolated node in a rooted view is itself a disclosure that
    something withheld connects it to the root.
    """
    ids = {
        "hub": _insert_linkable(test_db, "Public Hub Note"),
        "conf": _insert_linkable(
            test_db, LINK_CONF_TITLE, sensitivity="confidential"
        ),
        "leaf": _insert_linkable(test_db, "Public Leaf Note"),
        "beyond": _insert_linkable(test_db, "Beyond The Memo", kind="ingested"),
    }
    _wiki_edge(test_db, ids["hub"], ids["conf"], LINK_CONF_TITLE)
    _wiki_edge(test_db, ids["hub"], ids["leaf"], "Public Leaf Note")
    _wiki_edge(test_db, ids["conf"], ids["beyond"], "Beyond The Memo")
    src, dst = sorted((ids["conf"], ids["leaf"]))
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, weight) "
        "VALUES (%s, %s, 'shared_participant', 0.5)",
        (src, dst),
    )
    return ids


def _snapshot_blob(snapshot: object) -> str:
    """Serialize a whole ``GraphData`` for substring assertions.

    ``asdict`` rather than field-picking for the same reason :func:`_blob`
    serializes whole MCP responses: a field-by-field assertion only covers the
    leaks someone already imagined, and the leak here lives in ``link_text`` —
    a field nobody auditing "the node set is gated" would think to check.
    """
    return _blob(dataclasses.asdict(snapshot))  # type: ignore[call-overload]


def _link_fixture_is_not_vacuous(
    conn: psycopg.Connection[Any], ids: dict[str, str]
) -> None:
    """Guard: the fixture really is confidential and really is wired up.

    A withholding assertion over a snapshot that was never going to contain the
    document passes for the wrong reason and reads as coverage.
    """
    row = conn.execute(
        "SELECT sensitivity, title FROM documents WHERE id = %s", (ids["conf"],)
    ).fetchone()
    assert row is not None
    assert row[0] == "confidential", "fixture must be confidential"
    assert row[1] == LINK_CONF_TITLE, "fixture must carry the marked title"
    edges = conn.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s OR dst_document_id = %s",
        (ids["conf"], ids["conf"]),
    ).fetchone()
    assert edges is not None and edges[0] == 2, (
        "the confidential note must sit on BOTH an inbound and an outbound wiki "
        f"edge, or the edge assertions are vacuous: got {edges}"
    )


def test_the_link_graph_fixture_is_not_vacuous(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """The premise, asserted on its own, outside every other test's body."""
    _link_fixture_is_not_vacuous(test_db, link_graph)


# --- path 1 of 3: rooted -----------------------------------------------------


def test_graph_data_rooted_excludes_confidential_without_crashing(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """Rooted + excluded raised ``KeyError`` before it could withhold anything.

    Reaching the assertions at all is half of what this pins: the call itself
    was the failure. The other half is that it withholds — a repair that only
    stops the raising would satisfy the first half and leave the second open.
    """
    from brain.vault.graph import graph_data

    _link_fixture_is_not_vacuous(test_db, link_graph)

    snapshot = graph_data(
        test_db, root=link_graph["hub"], depth=None, exclude_confidential=True
    )

    blob = _snapshot_blob(snapshot)
    assert "Public Leaf Note" in blob, "must not break the normal traversal"
    assert LINK_CONF_TITLE not in blob
    assert link_graph["conf"] not in blob


def test_graph_data_rooted_does_not_traverse_through_a_withheld_node(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """``beyond`` is reachable ONLY via ``conf``, so it must not arrive either.

    Distinct from the test above, and not redundant with it: filtering the
    frontier against ``all_docs`` after the BFS stops ``conf`` from appearing
    while still letting ``beyond`` ride in on a path that runs through it,
    landing in the snapshot as an edgeless node. That artifact is the oracle —
    it says "something you cannot see joins the root to this" without naming it.
    """
    from brain.vault.graph import graph_data

    _link_fixture_is_not_vacuous(test_db, link_graph)

    snapshot = graph_data(
        test_db, root=link_graph["hub"], depth=None, exclude_confidential=True
    )

    assert link_graph["beyond"] not in _snapshot_blob(snapshot)


def test_graph_data_rooted_includes_confidential_by_default(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """POSITIVE CONTROL for the two rooted tests above.

    Same function, same root, flag flipped, assertion flipped. Without this the
    withholding pair passes on any snapshot that happens not to reach the
    document — including an empty one — and ``vault.graph``'s include-by-default
    convention goes unpinned on the one function that was added to it last.
    """
    from brain.vault.graph import graph_data

    snapshot = graph_data(test_db, root=link_graph["hub"], depth=None)

    blob = _snapshot_blob(snapshot)
    assert LINK_CONF_TITLE in blob, (
        "the default traversal never reached the confidential note, so the "
        "withholding assertions beside this one prove nothing"
    )
    assert link_graph["conf"] in blob
    assert link_graph["beyond"] in blob


# --- path 2 of 3: include_ingested=True (node_filter is None) ----------------


def test_graph_data_include_ingested_excludes_confidential_edges(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """The branch that filtered NO edges at all.

    ``include_ingested=True`` sets ``node_filter = None`` and takes
    ``kept_edges = list(all_edges)`` — the ungated fetch. The node set was
    correctly gated, which is precisely what made this hard to see: the
    document is absent from ``nodes`` and present, twice, in ``edges``.
    """
    from brain.vault.graph import graph_data

    _link_fixture_is_not_vacuous(test_db, link_graph)

    snapshot = graph_data(
        test_db, include_ingested=True, exclude_confidential=True
    )

    blob = _snapshot_blob(snapshot)
    assert "Public Leaf Note" in blob, "must not break the normal listing"
    assert LINK_CONF_TITLE not in blob, "wiki link_text carries the title verbatim"
    assert link_graph["conf"] not in blob, "edge endpoints carry the UUID"


def test_graph_data_include_ingested_honours_the_class_edge_invariant(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """``GraphData`` documents this invariant; this branch violated it.

    The class docstring says "an edge always has both endpoints in ``nodes`` (we
    never emit an edge that points at a node not in the snapshot)". Asserting it
    structurally, rather than by substring, catches the same defect through the
    contract the type already claims — so a future leak of some other withheld
    field still fails here even if it carries no marker string.
    """
    from brain.vault.graph import graph_data

    snapshot = graph_data(
        test_db, include_ingested=True, exclude_confidential=True
    )

    node_ids = {n.document_id for n in snapshot.nodes}
    # ``assert not dangling`` passes trivially on an empty edge list, so the
    # claim below is only evidence while there is something to be dangling.
    # The ``hub -> leaf`` edge survives the gate in this fixture today; this
    # line is what will notice if that stops being true, instead of the test
    # quietly turning green-because-empty.
    assert snapshot.edges, (
        "no edges survived the gate — the dangling-edge check below would "
        "pass vacuously"
    )
    dangling = [
        (e.src_document_id, e.dst_document_id)
        for e in snapshot.edges
        if e.src_document_id not in node_ids or e.dst_document_id not in node_ids
    ]
    assert not dangling, f"edges point at nodes absent from the snapshot: {dangling}"


def test_graph_data_include_ingested_includes_confidential_by_default(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """POSITIVE CONTROL for the ``include_ingested`` pair above."""
    from brain.vault.graph import graph_data

    snapshot = graph_data(test_db, include_ingested=True)

    blob = _snapshot_blob(snapshot)
    assert LINK_CONF_TITLE in blob, (
        "the default snapshot never carried the confidential note, so the "
        "withholding assertions beside this one prove nothing"
    )
    assert link_graph["conf"] in blob


# --- path 3 of 3: the default whole-graph path (node_filter is a set) --------


def test_graph_data_default_path_excludes_confidential(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """Stated plainly: this path was ALREADY correct, and is pinned anyway.

    ``node_filter`` is derived from ``all_docs``, which is gated, and
    ``kept_edges`` is filtered on ``node_filter`` — so the default path withheld
    correctly before any fix. It is covered because it was equally uncovered:
    not one of the 19 call sites passed the parameter, so "correct" here was
    unverified rather than known, and the fix below moves the edge gate upstream
    of this branch. Without a pin, a later simplification that folds these three
    branches together could reopen it silently.
    """
    from brain.vault.graph import graph_data

    _link_fixture_is_not_vacuous(test_db, link_graph)

    snapshot = graph_data(test_db, exclude_confidential=True)

    blob = _snapshot_blob(snapshot)
    assert "Public Leaf Note" in blob, "must not break the normal listing"
    assert LINK_CONF_TITLE not in blob
    assert link_graph["conf"] not in blob


def test_graph_data_default_path_drops_an_ingested_doc_orphaned_by_the_gate(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """``beyond`` is ingested and connected ONLY to ``conf``.

    With ``conf`` withheld it has no visible edge, which makes it an
    ingested-tier orphan — the category this path drops as noise by spec. Before
    the edge gate moved upstream, ``_connected_node_set`` was computed over the
    ungated edge set, so ``beyond`` counted as connected and shipped as an
    edgeless node: the same unexplained-node oracle as the rooted path, reached
    by a different route.
    """
    from brain.vault.graph import graph_data

    snapshot = graph_data(test_db, exclude_confidential=True)

    assert link_graph["beyond"] not in _snapshot_blob(snapshot)


def test_graph_data_default_path_includes_confidential_by_default(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """POSITIVE CONTROL for the default-path pair above."""
    from brain.vault.graph import graph_data

    snapshot = graph_data(test_db)

    blob = _snapshot_blob(snapshot)
    assert LINK_CONF_TITLE in blob, (
        "the default snapshot never carried the confidential note, so the "
        "withholding assertions beside this one prove nothing"
    )
    assert link_graph["conf"] in blob
    assert link_graph["beyond"] in blob, (
        "``beyond`` must be present by default, or the orphan-drop assertion "
        "beside this one passes because it was never included at all"
    )


def test_graph_data_rooted_at_a_confidential_document_returns_nothing(
    test_db: psycopg.Connection[Any], link_graph: dict[str, str]
) -> None:
    """Rooting AT the withheld document — the case the edge gate alone misses.

    Gating the edge set upstream stops the frontier from REACHING a confidential
    node, which is most of the fix; it cannot help when the caller hands one in
    as ``root``. ``_bfs_frontier`` seeds ``visited`` with ``root``
    unconditionally, so the withheld id is in the frontier no matter what the
    edges say, and the intersection against ``all_docs`` is what keeps it out of
    the result. Without that intersection this is a ``KeyError`` again — the
    same crash as the rooted bug, reached by the one route the upstream gate
    does not cover.
    """
    from brain.vault.graph import graph_data

    _link_fixture_is_not_vacuous(test_db, link_graph)

    snapshot = graph_data(
        test_db, root=link_graph["conf"], depth=None, exclude_confidential=True
    )

    assert snapshot.nodes == []
    assert snapshot.edges == []
