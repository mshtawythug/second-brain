"""No confidential text — or its shadow — reaches MCP (F6).

F6's premise is that the local CLI sits *inside* the trust boundary while a
hosted model sits outside. MCP serves the hosted model, so it is the egress
point that matters.

Three distinct leaks are pinned here, because closing one does not close the
others and each looks correct in isolation:

1. **Body text in a search snippet.** The obvious one.
2. **Membership.** Redacting the snippet but still returning the row leaves an
   oracle: a hit for ``severance`` proves the withheld body contains
   "severance", and a caller reconstructs the body a word at a time. This is
   why the filter belongs in the predicate, not in the renderer.
3. **Body-derived metadata.** ``documents.summary`` is LLM-generated *from*
   the content, so returning it beside ``content: None`` hands back the same
   substance, condensed.

The assertions deliberately search the **whole serialized response** rather
than the fields we thought of. A field-by-field assertion only covers the
leaks someone already imagined; serializing catches the one added next week.

Every fixture carries a **non-empty** secret and a **non-null** summary — a
withholding test against an empty fixture passes vacuously and is worse than
no test, because it reads as coverage.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from tests.conftest import TEST_DATABASE_URL

#: The query used to retrieve the confidential document. It matches the body,
#: so it is echoed back in headers and telemetry — that is the caller's own
#: input, not a leak, and assertions must not confuse the two.
QUERY = "severance clause staff"

#: A marker present ONLY in the confidential body and NEVER in any query. This
#: is what the assertions look for: finding it anywhere in a response is
#: unambiguous evidence that body text escaped, with no false positive from
#: the echoed query.
BODY_MARKER = "quokkavolt"

#: Likewise for the LLM summary, which is derived from that body.
SECRET_SUMMARY = "Bands run one eighty to two twenty under quokkavolt terms."


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
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=vault_dir,
            recall_budget_tokens=2000,
            recall_passage_tokens=120,
            recall_max_candidates=25,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


@pytest.fixture
def confidential_doc(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    mcp_state: mcp_server._State,  # noqa: ARG001 — ordering
) -> str:
    """One confidential doc (with chunks) plus a normal decoy. Returns its id."""
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    result = ingest_document(
        test_db,
        doc=ExtractedDoc(
            title="Confidential Comp Doc",
            content=(
                f"Internal only. The {QUERY} applies, filed under "
                f"{BODY_MARKER}. "
            ) * 6,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        embedder=fake_embedder,
        source_kind="manual",
        source_external_id="w5-egress-conf",
    )
    doc_id = result.document_id
    assert doc_id is not None
    test_db.execute(
        "UPDATE documents SET sensitivity='confidential', summary=%s WHERE id=%s",
        (SECRET_SUMMARY, doc_id),
    )
    # A normal document that also matches, so an empty result set cannot be
    # mistaken for "the query matched nothing".
    ingest_document(
        test_db,
        doc=ExtractedDoc(
            title="Public Comp Overview",
            content="Public compensation overview for staff engineers. " * 6,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        embedder=fake_embedder,
        source_kind="manual",
        source_external_id="w5-egress-pub",
    )
    return doc_id


def _fixture_is_not_vacuous(test_db: psycopg.Connection[Any], doc_id: str) -> None:
    """Guard: the doc really is confidential AND really has a summary.

    Without this, every assertion below could pass because there was nothing
    to leak — the exact vacuous-pass class these tests exist to avoid.
    """
    row = test_db.execute(
        "SELECT sensitivity, summary, content FROM documents WHERE id=%s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "confidential", "fixture must be confidential"
    assert row[1], "fixture must have a NON-NULL summary or the test is vacuous"
    assert BODY_MARKER in row[2], "fixture body must contain the marker"


# ---------------------------------------------------------------------------
# brain_search — snippet AND membership
# ---------------------------------------------------------------------------


def test_search_returns_no_confidential_row(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Membership is itself derived from the content — the oracle.

    A snippet-only assertion passes while this hole stands, which is why the
    row's absence is asserted separately.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_search(query=QUERY, limit=10)

    titles = [r["title"] for r in payload["results"]]
    assert "Confidential Comp Doc" not in titles


def test_search_response_contains_no_confidential_text_anywhere(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Serialize the WHOLE response — not the fields we happened to think of."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_search(query=QUERY, limit=10)

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_search_still_returns_normal_documents(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The filter must exclude one tier, not break search."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_search(query="compensation staff engineers", limit=10)

    titles = [r["title"] for r in payload["results"]]
    assert "Public Comp Overview" in titles


def test_search_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The escape hatch exists and is explicit, mirroring ``brain_show``."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_search(
        query=QUERY, limit=10, include_confidential=True
    )

    titles = [r["title"] for r in payload["results"]]
    assert "Confidential Comp Doc" in titles


def test_facet_counts_do_not_leak_membership(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Facets computed over a wider match set than the results re-open the door.

    A count of 1 beside zero visible rows says "there is a confidential
    document matching this query" just as loudly as returning it.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_search(
        query=QUERY, limit=10, facets=True
    )

    facets = payload.get("facets")
    if facets:
        total = sum(
            bucket["count"]
            for axis in ("source", "content_type", "tag")
            for bucket in (facets.get(axis) or [])
        )
        assert total == 0, f"facets counted excluded documents: {facets}"


# ---------------------------------------------------------------------------
# brain_recall — the same hole, same fix
# ---------------------------------------------------------------------------


def test_recall_returns_no_confidential_passage(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Recall hands whole expanded windows to a model — strictly worse to leak."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_recall(query=QUERY)

    titles = [p["title"] for p in payload["passages"]]
    assert "Confidential Comp Doc" not in titles


def test_recall_response_contains_no_confidential_text_anywhere(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_recall(query=QUERY)

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_recall_context_block_is_clean(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The context block is the artifact pasted into a model's window."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_recall(query=QUERY)

    assert BODY_MARKER not in payload["context_block"].lower()


def test_recall_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_recall(
        query=QUERY, include_confidential=True
    )

    titles = [p["title"] for p in payload["passages"]]
    assert "Confidential Comp Doc" in titles


# ---------------------------------------------------------------------------
# brain_show — the body-derived summary
# ---------------------------------------------------------------------------


def test_show_withholds_the_body_derived_summary(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """``summary`` is written by an LLM FROM the body — it is the body, condensed.

    The fixture guard above proves the summary is non-null, so this cannot
    pass vacuously.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_show(id_prefix=confidential_doc)

    assert payload["content"] is None
    assert "summary" not in payload
    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_show_withheld_payload_key_set_is_pinned(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The mirror image of the normal-path byte-identical test.

    A byte-identical assertion on the NORMAL branch says nothing about the
    WITHHELD branch — which is exactly how the summary leak shipped. Pinning
    the withheld key set means any field added unconditionally above the
    withhold block trips this instead of silently escaping.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_show(id_prefix=confidential_doc)

    assert set(payload) == {
        "id",
        "title",
        "content",
        "content_type",
        "tags",
        "source_path",
        "ingested_at",
        "source_kind",
        "sensitivity",
        "withheld",
    }


def test_show_include_confidential_returns_body_and_summary(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_doc)

    payload = mcp_server.brain_show(
        id_prefix=confidential_doc, include_confidential=True
    )

    assert BODY_MARKER in payload["content"]
    assert payload["summary"] == SECRET_SUMMARY


# ---------------------------------------------------------------------------
# brain_resurface — the fourth surface, and the one that nearly passed vacuously
# ---------------------------------------------------------------------------


def _make_resurfaceable(conn: psycopg.Connection[Any]) -> None:
    """Age every doc past ``resurface_min_age_days`` so the tool returns rows.

    This helper is the whole reason these tests are meaningful. A first probe
    of ``brain_resurface`` reported no leak — because the result set was
    EMPTY: resurface only considers documents older than a minimum age, and a
    freshly-ingested fixture never qualifies. The "safe" reading was an
    artifact of an empty list.
    """
    conn.execute(
        "UPDATE documents SET ingested_at = NOW() - INTERVAL '400 days'"
    )


def test_resurface_fixture_is_not_vacuous(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Guard: without ageing, resurface returns nothing and proves nothing."""
    _fixture_is_not_vacuous(test_db, confidential_doc)
    _make_resurfaceable(test_db)

    payload = mcp_server.brain_resurface(limit=50, include_confidential=True)

    assert payload["items"], (
        "resurface must return rows here, or the leak assertions below are "
        "vacuous — this is exactly how the first probe gave a false all-clear"
    )


def test_resurface_excludes_confidential_documents(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """A resurfaced row is itself a disclosure, and it carries a body snippet."""
    _fixture_is_not_vacuous(test_db, confidential_doc)
    _make_resurfaceable(test_db)

    payload = mcp_server.brain_resurface(limit=50)

    titles = [i["title"] for i in payload["items"]]
    assert "Confidential Comp Doc" not in titles


def test_resurface_response_contains_no_confidential_text_anywhere(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_doc)
    _make_resurfaceable(test_db)

    payload = mcp_server.brain_resurface(limit=50)

    blob = json.dumps(payload, default=str).lower()
    assert BODY_MARKER not in blob


def test_resurface_still_returns_normal_documents(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Excluding one tier must not empty the feature."""
    _fixture_is_not_vacuous(test_db, confidential_doc)
    _make_resurfaceable(test_db)

    payload = mcp_server.brain_resurface(limit=50)

    titles = [i["title"] for i in payload["items"]]
    assert "Public Comp Overview" in titles


def test_resurface_include_confidential_opts_back_in(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    _fixture_is_not_vacuous(test_db, confidential_doc)
    _make_resurfaceable(test_db)

    payload = mcp_server.brain_resurface(limit=50, include_confidential=True)

    titles = [i["title"] for i in payload["items"]]
    assert "Confidential Comp Doc" in titles


# ---------------------------------------------------------------------------
# brain_list — the fifth surface, and the one the enumeration walked past
# ---------------------------------------------------------------------------
#
# `brain_list` sits thirty lines above `brain_resurface` in `mcp_server.py`.
# Resurface applies `_confidential_lens`; list did not — it called
# `queries.list_documents` with no `sensitivity` argument at all, even though
# that function has accepted one since F6. So the gap was never a missing
# capability, only an un-passed argument, on the single most listing-shaped
# tool in the module: no query required, "show me the corpus".
#
# The CLI's `brain list` is deliberately unfiltered and stays that way --
# `list_documents`'s own docstring says a tier you cannot see is a tier you
# forget you set, and the CLI sits INSIDE the trust boundary. MCP does not.


def test_list_does_not_name_a_confidential_document(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The unprompted listing surface must not name a confidential doc.

    No query is involved, so there is no "the caller asked for it" defence
    available here: this is the corpus painting itself for a hosted model.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    rows = mcp_server.brain_list(limit=50)

    titles = [r["title"] for r in rows]
    assert "Public Comp Overview" in titles, (
        "non-vacuity: the decoy must be listed, or an empty result would pass "
        "this test while listing nothing at all"
    )
    assert "Confidential Comp Doc" not in titles


def test_list_response_contains_no_confidential_text_anywhere(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """Serialize the whole response, not the fields we happened to think of."""
    _fixture_is_not_vacuous(test_db, confidential_doc)

    blob = json.dumps(mcp_server.brain_list(limit=50), default=str)

    assert BODY_MARKER not in blob
    assert SECRET_SUMMARY not in blob
    assert "Confidential Comp Doc" not in blob


def test_list_opt_in_still_returns_the_confidential_document(
    test_db: psycopg.Connection[Any], confidential_doc: str
) -> None:
    """The gate must be a gate, not a deletion.

    This is the assertion that keeps the two above honest: without it, a
    `brain_list` that returned nothing at all would satisfy them both.
    """
    _fixture_is_not_vacuous(test_db, confidential_doc)

    titles = [r["title"] for r in mcp_server.brain_list(limit=50, include_confidential=True)]

    assert "Confidential Comp Doc" in titles
    assert "Public Comp Overview" in titles
