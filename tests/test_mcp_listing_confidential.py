"""Unprompted MCP LISTING tools must not enumerate confidential documents (F6).

The F6 gate was built for the surfaces where the caller names what it wants —
``brain_search`` takes a query, ``brain_show`` takes an id. Four tools were left
open that take neither, and that is precisely what makes them worse rather than
better: a tool with no document parameter returns documents the caller never
asked for, so its whole payload is an enumeration.

The branch already ruled on this shape, in its own commit message, when it
closed ``/api/notes/{id}/links`` and ``brain_links`` / ``brain_backlinks``:

    "What must not happen is enumerating confidential documents the caller
    never named." — "A title is not nothing."

These four are the same shape and were left open:

1. ``brain_connect_list`` — the worst of them. It takes NO document id at all,
   and every row names TWO documents (``source_title`` + ``target_title``).
   ``connect.iter_suggestions`` joins ``documents`` twice with no sensitivity
   predicate, so the default call enumerates pairs the caller never named.
2. ``brain_timeline`` — buckets carry ``doc_ids`` AND ``doc_titles``, and
   ``--synthesize`` feeds the docs' SUMMARIES to a model.
3. ``brain_brief`` — captures, pinned docs, and open action items.
4. ``brain_review_weekly`` — activity, ingested, open loops, theme docs.

**3 and 4 are not title leaks — they are BODY leaks.** Both call
``todo.iter_action_item_docs``, which selects ``d.content`` and parses action-item
text straight out of the confidential body into ``open_todos[].text``. ``brain_brief``
then forwards that text to Ollama in ``_build_suggest_prompt``. That is the exact
egress F6 exists to prevent, and it was reachable with no parameters at all.

Two residuals from the previous pass are pinned here too — the paths that
``_build_doc_results``' gate stopped one query short of:

* ``ThemeGroup.doc_ids`` / ``ThemeGroup.summary`` (``graph_rag/themes.py``)
* ``CommunityGroup.doc_ids`` (``graph_rag/global_.py``)

Assertions serialize the WHOLE response rather than the fields we thought of: a
field-by-field assertion only covers the leaks someone already imagined. Every
fixture is proved NON-VACUOUS before it is trusted — a withholding assertion
against an empty payload passes for the wrong reason and reads as coverage.

Both directions are asserted for every gate. The permissive direction only ever
ADDS rows, so a bridge inverted between the MCP layer's ``include_confidential``
and a core module's ``exclude_confidential`` leaves every one-directional test
green. See ``_confidential_lens``.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.sensitivity import CONFIDENTIAL
from tests.conftest import TEST_DATABASE_URL
from tests.test_mcp_graphrag import (
    _build,
    _make_state,
    _seed_directory,
    _seed_gmail_doc,
)

#: Present ONLY in confidential bodies, never in any query or title — so finding
#: it in a response is unambiguous evidence body text escaped, with no false
#: positive from the caller's own echoed input.
BODY_MARKER = "quokkavolt"

#: The confidential document's title. Distinct from the body marker so a TITLE
#: leak and a BODY leak are told apart rather than collapsed into one assertion.
CONF_TITLE = "Confidential Wind-Down Memo"

#: The ordinary document's title. Its presence is what proves a gate excluded
#: one tier rather than breaking the tool.
OPEN_TITLE = "Public Scheduling Note"

#: Text parsed out of the confidential body by ``todo.parse_action_items``. It
#: reaches ``open_todos[].text`` — and from there the Ollama prompt — so it is
#: tracked separately from ``BODY_MARKER``: this is the substance, not a marker.
CONF_TODO_TEXT = f"Escrow the {BODY_MARKER} tranche before Friday"


def _mark_confidential(conn: psycopg.Connection[Any], doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s", (CONFIDENTIAL, doc_id)
    )


def _blob(payload: Any) -> str:
    """Serialize a whole MCP response for substring assertions.

    ``ensure_ascii=False`` is load-bearing, not tidiness. The default escapes
    every non-ASCII character, so a title containing an em-dash serializes as
    ``Severance bands \\u2014 synthetic`` and a substring check for the title
    silently fails to match. On a ``not in`` assertion that is worse than a bug:
    it PASSES, reporting "no leak" about a payload that contains the title in
    escaped form. Measured — every scan assertion in this module failed this way
    before the flag was added, and the withholding ones would have passed.
    """
    return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plain (non-graph) state, for connect / brief / review_weekly
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Any,
) -> mcp_server._State:
    """Install an MCP state whose vault is a scratch dir.

    Mirrors ``test_mcp_graphrag._make_state``'s ``Config`` (suppression off,
    concepts pinned off) but constructs it rather than mutating it: ``Config`` is
    a frozen dataclass, and reaching past that with ``object.__setattr__`` to
    retrofit a vault path is the kind of test-only hack that hides a real wiring
    gap. ``brain_review_weekly(emit=True)`` genuinely needs a writable vault, so
    the vault is a constructor argument here.
    """
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "reviews").mkdir(exist_ok=True)
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=vault,
            graph_generic_df_ratio=1.0,
            graph_concepts=False,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    content: str,
    sensitivity: str,
    content_type: str = "note",
    ingested_at: datetime | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, tags, draft,
           sensitivity, ingested_at)
        VALUES (%s, %s, %s, %s, %s, 'vault', %s, false, %s,
                COALESCE(%s, NOW()))
        """,
        (
            doc_id,
            title,
            content,
            f"hash-{doc_id}",
            content_type,
            [],
            sensitivity,
            ingested_at,
        ),
    )
    return doc_id


# ---------------------------------------------------------------------------
# brain_connect_list
# ---------------------------------------------------------------------------


@pytest.fixture
def suggestion_corpus(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> dict[str, str]:
    """Two suggestion pairs: one touching a confidential doc, one wholly open.

    The wholly-open pair is not decoration. Every assertion below is about ONE
    pair's absence; without a second pair that must SURVIVE, a gate that
    returned nothing at all would pass the strict half.

    The confidential document is the pair's TARGET, so a fix that filtered only
    ``sd`` (the source join) and not ``td`` still fails here. A suggestion names
    two documents and both joins need the predicate.
    """
    conf = _make_doc(
        test_db,
        doc_id="c1111111-0000-4000-8000-000000000001",
        title=CONF_TITLE,
        content=f"Wind-down terms filed under {BODY_MARKER}. " * 4,
        sensitivity=CONFIDENTIAL,
    )
    open_a = _make_doc(
        test_db,
        doc_id="c1111111-0000-4000-8000-000000000002",
        title=OPEN_TITLE,
        content="Routine scheduling note for the team. " * 4,
        sensitivity="normal",
    )
    open_b = _make_doc(
        test_db,
        doc_id="c1111111-0000-4000-8000-000000000003",
        title="Budget Review",
        content="Quarterly budget review notes. " * 4,
        sensitivity="normal",
    )
    for src, dst, score in ((open_a, conf, 0.9), (open_a, open_b, 0.5)):
        test_db.execute(
            "INSERT INTO link_suggestions "
            "(source_doc_id, target_doc_id, score, graph_score, embed_score, "
            " status) VALUES (%s, %s, %s, %s, %s, 'pending')",
            (src, dst, score, score, score),
        )
    return {"conf": conf, "open_a": open_a, "open_b": open_b}


def _connect_fixture_is_not_vacuous(
    conn: psycopg.Connection[Any], ids: dict[str, str]
) -> None:
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE id=%s", (ids["conf"],)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL, "fixture must be confidential"
    n = conn.execute("SELECT count(*) FROM link_suggestions").fetchone()
    assert n is not None and n[0] == 2, "fixture must hold both suggestion pairs"


def test_connect_list_hides_a_confidential_partner_by_default(
    test_db: psycopg.Connection[Any], suggestion_corpus: dict[str, str]
) -> None:
    """The default call must not name a document the caller never asked for.

    ``brain_connect_list`` takes no document id, so every title in its payload is
    a document the caller did not name. That is the whole finding.
    """
    _connect_fixture_is_not_vacuous(test_db, suggestion_corpus)

    payload = mcp_server.brain_connect_list()

    assert CONF_TITLE not in _blob(payload)


def test_connect_list_still_returns_the_wholly_open_pair(
    test_db: psycopg.Connection[Any], suggestion_corpus: dict[str, str]
) -> None:
    """Anti-vacuity: the gate excludes one tier, it does not empty the queue."""
    _connect_fixture_is_not_vacuous(test_db, suggestion_corpus)

    payload = mcp_server.brain_connect_list()

    assert len(payload) == 1, f"expected the open pair to survive, got {payload}"
    assert payload[0]["target_title"] == "Budget Review"


def test_connect_list_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], suggestion_corpus: dict[str, str]
) -> None:
    """The permissive direction. Without this the bridge could be inverted."""
    _connect_fixture_is_not_vacuous(test_db, suggestion_corpus)

    payload = mcp_server.brain_connect_list(include_confidential=True)

    assert CONF_TITLE in _blob(payload)
    assert len(payload) == 2


# ---------------------------------------------------------------------------
# brain_brief — captures, pinned, and BODY-derived todo text
# ---------------------------------------------------------------------------


@pytest.fixture
def brief_corpus(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> dict[str, str]:
    """A confidential capture that is ALSO pinned and ALSO carries action items.

    One document reached through three independent queries — ``recent_captures``,
    ``_pinned_docs`` and ``iter_action_item_docs`` — because they are three
    separate SELECTs and closing one closes neither of the others.
    """
    conf = _make_doc(
        test_db,
        doc_id="b1111111-0000-4000-8000-000000000001",
        title=CONF_TITLE,
        content=f"- [ ] {CONF_TODO_TEXT}\n- [ ] Notify counsel about {BODY_MARKER}\n",
        sensitivity=CONFIDENTIAL,
        content_type="krisp_action_items",
    )
    open_doc = _make_doc(
        test_db,
        doc_id="b1111111-0000-4000-8000-000000000002",
        title=OPEN_TITLE,
        content="- [ ] Book the team offsite room\n",
        sensitivity="normal",
        content_type="krisp_action_items",
    )
    for doc_id in (conf, open_doc):
        test_db.execute(
            "INSERT INTO interactions (document_id, action, source, at) "
            "VALUES (%s, 'pinned', 'cli', NOW())",
            (doc_id,),
        )
    return {"conf": conf, "open": open_doc}


def _brief_fixture_is_not_vacuous(
    conn: psycopg.Connection[Any], ids: dict[str, str]
) -> None:
    row = conn.execute(
        "SELECT sensitivity, content FROM documents WHERE id=%s", (ids["conf"],)
    ).fetchone()
    assert row is not None
    assert row[0] == CONFIDENTIAL, "fixture must be confidential"
    assert CONF_TODO_TEXT in row[1], "fixture body must carry the action item"
    # And the tool must actually reach it when permitted — otherwise the strict
    # assertions below pass because the window was empty, not because of a gate.
    permissive = mcp_server.brain_brief(no_enrich=True, include_confidential=True)
    assert CONF_TITLE in _blob(permissive), "fixture unreachable — test is vacuous"


def test_brief_hides_a_confidential_capture_pin_and_todo(
    test_db: psycopg.Connection[Any], brief_corpus: dict[str, str]
) -> None:
    """All three sections at once, because they are three separate queries.

    Asserted over the serialized payload rather than per-section: a leak in a
    key nobody anticipated is still a leak.
    """
    _brief_fixture_is_not_vacuous(test_db, brief_corpus)

    blob = _blob(mcp_server.brain_brief(no_enrich=True))

    assert CONF_TITLE not in blob
    assert BODY_MARKER not in blob


def test_brief_hides_the_body_derived_action_item_text(
    test_db: psycopg.Connection[Any], brief_corpus: dict[str, str]
) -> None:
    """The severe half, pinned separately from the title.

    ``iter_action_item_docs`` selects ``d.content`` and parses items out of it,
    so this is BODY text, not a title — and ``brain_brief`` forwards it to a
    hosted model in ``_build_suggest_prompt``. A title-only fix leaves this open,
    and a title-only assertion would not notice.
    """
    _brief_fixture_is_not_vacuous(test_db, brief_corpus)

    blob = _blob(mcp_server.brain_brief(no_enrich=True))

    assert CONF_TODO_TEXT not in blob


def test_brief_still_returns_the_normal_document(
    test_db: psycopg.Connection[Any], brief_corpus: dict[str, str]
) -> None:
    """Anti-vacuity: the brief still renders its ordinary content."""
    _brief_fixture_is_not_vacuous(test_db, brief_corpus)

    blob = _blob(mcp_server.brain_brief(no_enrich=True))

    assert OPEN_TITLE in blob


def test_brief_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], brief_corpus: dict[str, str]
) -> None:
    """The permissive direction, for all three sections.

    This controls BOTH claims of
    ``test_brief_hides_a_confidential_capture_pin_and_todo`` (title and
    ``BODY_MARKER``) plus the separate ``CONF_TODO_TEXT`` test — but the
    ``BODY_MARKER`` half only TRANSITIVELY, because ``CONF_TODO_TEXT`` is built
    by interpolating the marker. Asserted explicitly below so the pairing does
    not quietly lapse if the constants are ever decoupled.
    """
    _brief_fixture_is_not_vacuous(test_db, brief_corpus)

    blob = _blob(mcp_server.brain_brief(no_enrich=True, include_confidential=True))

    assert CONF_TITLE in blob
    assert BODY_MARKER in CONF_TODO_TEXT, (
        "the containment this control relies on has been broken — BODY_MARKER "
        "now needs its own assertion below"
    )
    assert CONF_TODO_TEXT in blob


# ---------------------------------------------------------------------------
# brain_review_weekly
# ---------------------------------------------------------------------------


@pytest.fixture
def weekly_corpus(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> dict[str, Any]:
    """A confidential doc ingested + interacted with inside one ISO week.

    The week is derived from a fixed timestamp rather than ``now()`` so the test
    is deterministic; the tool is then called with that explicit ``week``.
    """
    moment = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)  # 2026-W10, a Wednesday
    conf = _make_doc(
        test_db,
        doc_id="a1111111-0000-4000-8000-000000000001",
        title=CONF_TITLE,
        content=f"- [ ] {CONF_TODO_TEXT}\n",
        sensitivity=CONFIDENTIAL,
        content_type="krisp_action_items",
        ingested_at=moment,
    )
    open_doc = _make_doc(
        test_db,
        doc_id="a1111111-0000-4000-8000-000000000002",
        title=OPEN_TITLE,
        content="- [ ] Book the team offsite room\n",
        sensitivity="normal",
        content_type="krisp_action_items",
        ingested_at=moment,
    )
    for doc_id in (conf, open_doc):
        test_db.execute(
            "INSERT INTO interactions (document_id, action, source, at) "
            "VALUES (%s, 'opened', 'cli', %s)",
            (doc_id, moment + timedelta(hours=1)),
        )
    return {"conf": conf, "open": open_doc, "week": "2026-W10"}


def _weekly_fixture_is_not_vacuous(
    conn: psycopg.Connection[Any], fx: dict[str, Any]
) -> None:
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE id=%s", (fx["conf"],)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL
    permissive = mcp_server.brain_review_weekly(
        week=fx["week"], emit=False, include_confidential=True
    )
    assert CONF_TITLE in _blob(permissive), "fixture unreachable — test is vacuous"


def test_review_weekly_hides_confidential_activity_and_loops(
    test_db: psycopg.Connection[Any], weekly_corpus: dict[str, Any]
) -> None:
    """Activity, ingested and open-loop sections are three separate queries."""
    _weekly_fixture_is_not_vacuous(test_db, weekly_corpus)

    blob = _blob(
        mcp_server.brain_review_weekly(week=weekly_corpus["week"], emit=False)
    )

    assert CONF_TITLE not in blob
    assert CONF_TODO_TEXT not in blob
    assert BODY_MARKER not in blob


def test_review_weekly_still_returns_the_normal_document(
    test_db: psycopg.Connection[Any], weekly_corpus: dict[str, Any]
) -> None:
    """Anti-vacuity."""
    _weekly_fixture_is_not_vacuous(test_db, weekly_corpus)

    blob = _blob(
        mcp_server.brain_review_weekly(week=weekly_corpus["week"], emit=False)
    )

    assert OPEN_TITLE in blob


def test_review_weekly_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], weekly_corpus: dict[str, Any]
) -> None:
    """The permissive direction — for the BODY claim as well as the title.

    ``…_hides_confidential_activity_and_loops`` denies three strings over one
    blob: the title, the action-item text, and the body marker. Only the title
    was controlled, so the two body-derived claims rested on nothing — the same
    shape ``test_timeline_include_confidential_opts_back_in`` documents one
    section below, on a different tool.

    Two ``in`` assertions cover all three claims, and only because
    ``CONF_TODO_TEXT`` is built from ``BODY_MARKER`` — asserting the longer
    string present necessarily proves the marker reached the payload too. That
    containment is load-bearing, not incidental: decouple the two constants and
    this control silently stops pinning ``BODY_MARKER``.
    """
    _weekly_fixture_is_not_vacuous(test_db, weekly_corpus)

    blob = _blob(
        mcp_server.brain_review_weekly(
            week=weekly_corpus["week"], emit=False, include_confidential=True
        )
    )

    assert CONF_TITLE in blob
    assert BODY_MARKER in CONF_TODO_TEXT, (
        "the containment this control relies on has been broken — BODY_MARKER "
        "now needs its own assertion below"
    )
    assert CONF_TODO_TEXT in blob


# ---------------------------------------------------------------------------
# Graph surfaces: brain_timeline + the two named residuals
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps schema + graph fresh
    fake_embedder: object,
) -> mcp_server._State:
    """Install a graphrag MCP state (concepts pinned off, suppression disabled).

    Defined here rather than imported: a pytest fixture is only visible to the
    module that DEFINES it, and re-binding an imported one shadows the import.
    """
    state = _make_state(fake_embedder)
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


@pytest.fixture
def confidential_graph(
    test_db: psycopg.Connection[Any],
    graph_state: mcp_server._State,  # noqa: ARG001 — installs the MCP state
) -> str:
    """A person triangle where ONE document is confidential. Returns its id.

    ``bob`` co-occurs with ``alice`` in the confidential doc and with ``carol``
    in a normal one, so a query seeded on ``bob`` reaches BOTH — an empty result
    therefore cannot be mistaken for "the traversal found nothing". Mirrors the
    fixture in ``tests/test_graph_confidential_egress.py`` deliberately: these
    assertions are about the paths THAT module's gate stopped short of, so they
    have to start from the same graph.
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
        external_id="lc-conf-1",
        participants=[("alice", "alice@example.com"), ("bob", "bob@example.com")],
        content=f"Wind-down terms filed under {BODY_MARKER}. " * 6,
    )
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", (CONF_TITLE, conf_id)
    )
    test_db.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, title_text) "
        "VALUES (%s, 0, %s, %s)",
        (conf_id, f"Wind-down terms filed under {BODY_MARKER}. " * 6, CONF_TITLE),
    )
    _mark_confidential(test_db, conf_id)

    normal_id = _seed_gmail_doc(
        test_db,
        external_id="lc-norm-1",
        participants=[("bob", "bob@example.com"), ("carol", "carol@example.com")],
        content="Routine scheduling note for the team. " * 6,
    )
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", (OPEN_TITLE, normal_id)
    )
    test_db.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, title_text) "
        "VALUES (%s, 0, %s, %s)",
        (normal_id, "Routine scheduling note for the team. " * 6, OPEN_TITLE),
    )
    _build(test_db)
    return conf_id


def _graph_fixture_is_not_vacuous(
    conn: psycopg.Connection[Any], doc_id: str
) -> None:
    row = conn.execute(
        "SELECT sensitivity, content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == CONFIDENTIAL, "fixture must be confidential"
    assert BODY_MARKER in row[1], "fixture body must contain the marker"
    n = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions WHERE document_id = %s",
        (doc_id,),
    ).fetchone()
    assert n is not None and n[0] > 0, "fixture must be reachable through the graph"


# -- brain_timeline ---------------------------------------------------------


def test_timeline_hides_confidential_ids_and_titles(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Buckets carry ``doc_ids`` AND ``doc_titles``; both are enumerations.

    The id matters as much as the title: a bucket that names an id the caller
    can then feed to ``brain_show`` is a membership oracle even with the title
    withheld.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)

    blob = _blob(mcp_server.brain_timeline(query="bob"))

    assert CONF_TITLE not in blob
    assert confidential_graph not in blob


def test_timeline_still_buckets_the_normal_document(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Anti-vacuity: the timeline still returns the tier it is allowed to."""
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)

    blob = _blob(mcp_server.brain_timeline(query="bob"))

    assert OPEN_TITLE in blob


def test_timeline_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The permissive direction — for BOTH enumerations, not just the title.

    The subtlest of the vacuity gaps found on this branch, because this test
    already existed and the pairing LOOKED complete. Its withholding sibling
    asserts two separate claims over one blob — no confidential title, and no
    confidential id — while this control asserted only the title. So the id
    half was uncontrolled inside a test that passes every "does it have an
    opt-back-in sibling?" check anyone would think to run.

    Dropping ``doc_ids`` from the timeline bucket shape
    (:func:`brain.format` bucket serialization) therefore made
    "no confidential id is enumerated" vacuous while this test stayed green on
    the title — measured: every test in this module and in
    ``test_timeline.py`` passed with the field deleted.

    That mattered more here than in the other three, because the sibling's
    docstring argues the id is a disclosure INDEPENDENT of the title — "a
    bucket that names an id the caller can then feed to ``brain_show`` is a
    membership oracle even with the title withheld". A documented claim resting
    on an assertion that cannot fail is worse than an undocumented gap: it
    reads as settled.

    One control per claim, at the same layer the claim is denied over.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)

    blob = _blob(mcp_server.brain_timeline(query="bob", include_confidential=True))

    assert CONF_TITLE in blob
    assert confidential_graph in blob, (
        "the timeline payload enumerates no document ids at all, so the "
        "'confidential_graph not in blob' half of the withholding test above "
        "is vacuous"
    )


# -- residual 1: ThemeGroup.doc_ids / .summary ------------------------------


def test_graphrag_themes_group_doc_ids_hide_confidential_membership(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``ThemeGroup.doc_ids`` bypassed ``_build_doc_results`` entirely.

    The previous pass gated ``GraphContext.docs`` — the context-level list — and
    stopped there. ``doc_ids`` is populated from ``_docs_by_entity`` in
    ``_populate_theme_docs``, a different query, so confidential ids were still
    enumerated beside the filtered docs list.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(person="bob")

    leaked = {i for t in payload["themes"] for i in t["doc_ids"]}
    assert confidential_graph not in leaked


def test_graphrag_themes_group_doc_ids_opt_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The permissive direction for the same field.

    Without this, ``doc_ids = []`` would satisfy the assertion above and the
    gate would look applied while actually being a truncation.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)

    payload = mcp_server.brain_graphrag_themes(
        person="bob", include_confidential=True
    )

    leaked = {i for t in payload["themes"] for i in t["doc_ids"]}
    assert confidential_graph in leaked


def test_graphrag_themes_synthesis_never_sees_a_confidential_title(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``_fetch_doc_titles`` had no predicate, and its result reaches a model.

    Asserted at the boundary the titles actually cross — the enricher call —
    rather than on the response, because ``summary`` is best-effort and is
    ``None`` whenever Ollama is unavailable. An assertion on the payload would
    therefore pass in CI for the wrong reason: no Ollama, no summary, no leak
    visible. Capturing the prompt argument makes the claim hold with no Ollama
    at all.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    seen: list[list[str]] = []

    class _Recorder:
        def summarize_group(
            self, *, person: str, entity_names: list[str], doc_titles: list[str]
        ) -> str | None:
            seen.append(list(doc_titles))
            return "synthetic summary"

    state = mcp_server._get_state()
    state.enricher = _Recorder()  # type: ignore[assignment]

    mcp_server.brain_graphrag_themes(person="bob", synthesize=True)

    assert seen, "enricher was never called — the assertion would be vacuous"
    assert all(CONF_TITLE not in titles for titles in seen), (
        f"a confidential title reached the synthesis prompt: {seen}"
    )


# -- residual 2: CommunityGroup.doc_ids -------------------------------------


def test_graphrag_global_community_doc_ids_hide_confidential_membership(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """``CommunityGroup.doc_ids`` is built from ``_community_doc_scores``.

    Same shape as the themes residual: the context-level docs were filtered by
    ``_build_doc_results`` while the per-community id list beside them was not.

    SUBSCRIPTED, not ``.get(…, [])`` — deliberately, and not to be "hardened"
    back. Both keys are unconditional in the wire shape
    (:func:`brain.format.graph_context_json` always emits ``communities``;
    :func:`brain.format._community_json` always emits ``doc_ids``), so a
    tolerant read buys no robustness and costs the only thing that matters
    here: with ``.get(…, [])`` this assertion CANNOT TELL "the gate withheld
    the document" from "the field no longer exists". Measured, not asserted —
    deleting the ``doc_ids`` key from ``_community_json`` left this test green.
    A ``KeyError`` is the correct outcome for a vanished contract field; a
    silent empty set is a test that reports success at the moment it stops
    being able to see anything.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    # One community holding every seeded entity. The claim is about the doc-id
    # list attached to a community, not about how communities are detected, so
    # the community is seeded rather than built. ``community_key`` defaults to
    # ``gen_random_uuid()`` and is RETURNed — the pattern every other graphrag
    # test uses (``test_graphrag_global``, ``test_graphrag_retrieve``), followed
    # here so this fixture cannot drift from the columns migration 013 requires.
    row = test_db.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, source_graph_hash, members_hash, member_count, summary) "
        "SELECT 'default', 'synthetic-graph-hash', 'synthetic-members-hash', "
        "       COUNT(*), 'Wind-down planning and scheduling for the team' "
        "FROM graph_entities RETURNING community_key::text",
    ).fetchone()
    assert row is not None
    community_key = str(row[0])
    test_db.execute(
        "INSERT INTO graph_community_members "
        "(tenant_id, community_key, entity_id, member_rank) "
        "SELECT tenant_id, %s, id, ROW_NUMBER() OVER (ORDER BY id) "
        "FROM graph_entities",
        (community_key,),
    )

    payload = mcp_server.brain_graphrag_search(query="wind-down", mode="global")

    leaked = {i for c in payload["communities"] for i in c["doc_ids"]}
    assert confidential_graph not in leaked


def test_graphrag_global_community_doc_ids_opt_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The permissive direction for the same field — the missing control.

    Mirrors :func:`test_graphrag_themes_group_doc_ids_opt_back_in` above,
    because the withholding assertion beside this one had the same hole its
    themes neighbour was already protected against: ``doc_ids = []`` satisfies
    "the confidential id is not enumerated" perfectly, so the gate looks
    applied while actually being a truncation. Verified two-sided — blanking
    ``list(community.doc_ids)`` in ``_community_json`` reddens THIS test and
    leaves the assertion above green.

    Same call, same flattening expression, ``include_confidential`` flipped and
    the assertion flipped: the presence check has to sit at the SAME LAYER the
    absence is asserted over, or it proves something about a different tier.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    _seed_community(test_db, "Wind-down planning and scheduling for the team")

    payload = mcp_server.brain_graphrag_search(
        query="wind-down", mode="global", include_confidential=True
    )

    leaked = {i for c in payload["communities"] for i in c["doc_ids"]}
    assert confidential_graph in leaked, (
        "no community enumerated ANY document id, so the withholding "
        f"assertion above is vacuous: leaked={leaked}"
    )


# -- residual 3: the community admin listing --------------------------------
#
# JUDGED, not assumed. ``brain_graphrag_communities`` projects no document id and
# no document title — only ``summary``. That looked like the entity tier, which
# this branch already treats as ungated (``brain_graphrag_entities`` returns
# entity names and is exempt on the grounds that an entity is not a document).
#
# It is not the entity tier. ``communities_summary._representative_doc_titles``
# feeds document TITLES into the summarization prompt alongside the entity names,
# so a stored ``summary`` is generated from confidential titles exactly the way
# ``ThemeGroup.summary`` was — the residual this pass closed two modules over.
# Same shape, same standard, so it is closed rather than exempted.


def _seed_community(conn: psycopg.Connection[Any], summary: str) -> str:
    """One community holding every seeded entity. Returns its key."""
    row = conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, source_graph_hash, members_hash, member_count, summary) "
        "SELECT 'default', 'synthetic-graph-hash', 'synthetic-members-hash', "
        "       COUNT(*), %s FROM graph_entities RETURNING community_key::text",
        (summary,),
    ).fetchone()
    assert row is not None
    key = str(row[0])
    conn.execute(
        "INSERT INTO graph_community_members "
        "(tenant_id, community_key, entity_id, member_rank) "
        "SELECT tenant_id, %s, id, ROW_NUMBER() OVER (ORDER BY id) "
        "FROM graph_entities",
        (key,),
    )
    return key


def test_graphrag_communities_withholds_a_cluster_built_from_confidential_docs(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The community is withheld WHOLE, not served with a blanked summary.

    Redacting the summary while still returning the row would leave the row as an
    oracle — it would prove a cluster exists whose contents could not be shown.
    ``_confidential_lens`` requires exclusion, so the row goes.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    _seed_community(test_db, f"Cluster covering {CONF_TITLE} and scheduling")

    payload = mcp_server.brain_graphrag_communities()

    assert payload["count"] == 0, payload
    assert CONF_TITLE not in _blob(payload)


def test_graphrag_communities_still_lists_a_wholly_normal_cluster(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """Anti-vacuity: a cluster with no confidential member still lists.

    Without this, a gate that returned nothing at all would satisfy the
    assertion above.
    """
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    # Drop the confidential document's mentions so this community's members
    # touch only the ordinary document.
    test_db.execute(
        "DELETE FROM graph_entity_mentions WHERE document_id = %s",
        (confidential_graph,),
    )
    _seed_community(test_db, "Cluster covering scheduling only")

    payload = mcp_server.brain_graphrag_communities()

    assert payload["count"] == 1, payload


def test_graphrag_communities_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_graph: str
) -> None:
    """The permissive direction."""
    _graph_fixture_is_not_vacuous(test_db, confidential_graph)
    _seed_community(test_db, f"Cluster covering {CONF_TITLE} and scheduling")

    payload = mcp_server.brain_graphrag_communities(include_confidential=True)

    assert payload["count"] == 1
    assert CONF_TITLE in _blob(payload)
