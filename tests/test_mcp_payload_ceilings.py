"""Wave 3 — payload ceilings on the numeric-param and bare-list MCP tools.

Six MCP tools could return an unbounded payload. This file covers five of them
(``brain_show`` has its own file):

* ``brain_search`` — ``limit`` rejected only ``< 1``; 200 results ≈ ~80k tokens.
* ``brain_recall`` — ``budget_tokens`` rejected only ``< 1``.
* ``brain_graphrag_entities`` — ``limit=0`` meant ALL (6,589 entities, MEASURED
  at 246,724 tokens — the plan's "~66k" estimate was off by 3.7x).
* ``brain_graphrag_communities`` — ``limit=None`` meant ALL.
* ``brain_backlinks`` / ``brain_links`` / ``brain_orphans`` — no ``limit`` at all.

The rule every assertion serves: **the cut must be visible.** A numeric param
above its ceiling raises ``INVALID_PARAMS`` naming the ceiling (the agent can
re-ask smaller); a capped list says so, either via ``truncated`` in its
envelope or via ``more_available`` on its last row.

``brain_recall`` also carries the Wave-0 double-render regression test — see
``test_recall_delivered_payload_exceeds_budget_tokens``.

All fixture data is synthetic.
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _reconfigure(state: mcp_server._State, **fields: Any) -> None:
    """Replace fields on the fixture's own Config (never a production module)."""
    state.cfg = dataclasses.replace(state.cfg, **fields)


def _seed_doc(
    conn: psycopg.Connection[Any], *, title: str, content: str = "body"
) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES (%s, %s, 'note', 'vault', %s) RETURNING id::text",
        (title, content, str(uuid.uuid4())),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_link(conn: psycopg.Connection[Any], src: str, dst: str, text: str) -> None:
    conn.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s, 'wiki')",
        (src, dst, text),
    )


def _seed_entities(conn: psycopg.Connection[Any], count: int) -> None:
    for i in range(count):
        conn.execute(
            "INSERT INTO graph_entities "
            "(tenant_id, entity_type, name, canonical_key, doc_count) "
            "VALUES ('default', 'topic', %s, %s, %s)",
            (f"Synthetic Topic {i:03d}", f"synthetic-topic-{i:03d}", count - i),
        )


def _seed_communities(conn: psycopg.Connection[Any], count: int) -> None:
    for i in range(count):
        conn.execute(
            "INSERT INTO graph_communities (tenant_id, level, source_graph_hash, "
            "members_hash, member_count, edge_count, total_weight, summary) "
            "VALUES ('default', 0, %s, %s, %s, 1, 1.0, %s)",
            (f"hash-{i}", f"members-{i}", count - i, f"Synthetic cluster {i}"),
        )


# ---------------------------------------------------------------------------
# brain_search
# ---------------------------------------------------------------------------


def test_search_limit_above_ceiling_raises_invalid_params(
    mcp_state: mcp_server._State,
) -> None:
    """200 results × ~1,600 chars ≈ ~80k tokens. Reject, do not silently trim.

    MUTATION: delete the ``_ceiling(limit, ...)`` line in ``brain_search`` and
    this goes red (no exception raised at all).
    """
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_search(query="anything", limit=200, fts_only=True)
    assert excinfo.value.error.code == INVALID_PARAMS
    assert "50" in excinfo.value.error.message, "the ceiling must be named"


def test_search_limit_at_ceiling_is_accepted(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The off-by-one boundary: ``limit == ceiling`` is a legal request."""
    _seed_doc(test_db, title="Ceiling Boundary Note")

    payload = mcp_server.brain_search(
        query="ceiling", limit=mcp_state.cfg.search_max_limit, fts_only=True
    )

    assert "results" in payload


def test_search_ceiling_is_configurable(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The knob is the documented rollback path, so it needs a test."""
    _seed_doc(test_db, title="Configurable Ceiling Note")
    _reconfigure(mcp_state, search_max_limit=5)

    with pytest.raises(MCPError):
        mcp_server.brain_search(query="configurable", limit=6, fts_only=True)
    assert mcp_server.brain_search(query="configurable", limit=5, fts_only=True)


# ---------------------------------------------------------------------------
# brain_recall — the ceiling AND the Wave-0 double-render finding
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_corpus(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    mcp_state: mcp_server._State,  # noqa: ARG001 — ordering
) -> None:
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    body = (
        "The platform migration review covered staffing, the runway, and the "
        "quarterly hiring plan in detail. "
    ) * 25
    for i in range(6):
        ingest_document(
            test_db,
            doc=ExtractedDoc(
                title=f"Platform Migration Review {i}",
                content=f"{body} Entry {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            embedder=fake_embedder,
            source_kind="manual",
            source_external_id=f"w3-recall-ceiling-{i}",
        )


def test_recall_budget_above_ceiling_raises_invalid_params(
    mcp_state: mcp_server._State,
) -> None:
    """``budget_tokens`` is capped at ``BRAIN_RECALL_MAX_BUDGET_TOKENS``.

    MUTATION: delete the ``_ceiling(effective_budget, ...)`` block in
    ``brain_recall`` and this goes red.
    """
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_recall(
            query="anything",
            budget_tokens=mcp_state.cfg.recall_max_budget_tokens + 1,
            fts_only=True,
        )
    assert excinfo.value.error.code == INVALID_PARAMS
    assert str(mcp_state.cfg.recall_max_budget_tokens) in excinfo.value.error.message


def test_recall_budget_at_ceiling_is_accepted(
    mcp_state: mcp_server._State, recall_corpus: None
) -> None:
    payload = mcp_server.brain_recall(
        query="platform migration staffing",
        budget_tokens=mcp_state.cfg.recall_max_budget_tokens,
        fts_only=True,
    )
    assert "passages" in payload


def test_recall_payload_tokens_reports_the_serialized_response(
    mcp_state: mcp_server._State, recall_corpus: None
) -> None:
    """``payload_tokens`` must measure THIS response, not an estimate.

    Counted over the payload without the key itself, which is the only
    self-consistent definition (including it would be circular).
    """
    payload = mcp_server.brain_recall(
        query="platform migration staffing", budget_tokens=400, fts_only=True
    )
    reported = payload.pop("payload_tokens")

    expected = mcp_state.embedder.count_tokens(  # type: ignore[attr-defined]
        json.dumps(payload, ensure_ascii=False)
    )
    assert reported == expected


def test_recall_delivered_payload_exceeds_budget_tokens(
    mcp_state: mcp_server._State, recall_corpus: None
) -> None:
    """REGRESSION for the Wave-0 finding: every passage ships TWICE.

    ``result.to_dict()`` already carries each passage's full ``text``, and
    ``payload["context_block"]`` then renders those same passages again. So the
    delivered payload runs at roughly 2.2x ``budget_tokens`` (measured
    2.01x-2.36x across 11 live queries at ``budget_tokens=2000``).
    ``budget_tokens`` therefore sizes the content SELECTED, not the response
    RETURNED — which is why ``BRAIN_RECALL_MAX_BUDGET_TOKENS`` is set to
    ~32000/2.36 rather than 32000.

    **When the duplication is removed (option (B) in the plan), THIS TEST IS
    THE ONE THAT MUST BE INVERTED.** That is deliberate: it leaves a trail.

    The ``> budget`` bound is intentionally loose — the point is that the
    overshoot is structural (a second full copy), not that it is exactly 2.2x
    on a six-document synthetic corpus.

    **Which assertion proves what, precisely** (measured, not asserted: blank
    the ``context_block`` and re-run). The ``payload_tokens > budget`` check
    below **still passes** with an empty block, because the JSON envelope alone
    clears 400 tokens on this corpus — so that line proves the payload is
    bigger than the budget, and nothing about *why*. The duplication claim
    rests entirely on the final assertion, that a passage's own text reappears
    inside ``context_block``. Do not read a red here as proof of the
    double-render without checking which line failed.
    """
    budget = 400
    payload = mcp_server.brain_recall(
        query="platform migration staffing", budget_tokens=budget, fts_only=True
    )

    assert payload["used_tokens"] <= budget, (
        "packing itself must still honour the budget — the overshoot is in the "
        "serialization, not the selection"
    )
    assert payload["payload_tokens"] > budget, (
        "the delivered payload must be measurably larger than the budget the "
        "caller asked for; if this fails, the double-render is gone and this "
        "test should be inverted rather than deleted"
    )
    # Pin the CAUSE, not just the symptom: the block re-renders passage text.
    assert payload["passages"], "corpus must produce at least one passage"
    first_text = payload["passages"][0]["text"]
    assert first_text[:60] in payload["context_block"]


# ---------------------------------------------------------------------------
# brain_graphrag_entities — the one genuinely breaking semantic
# ---------------------------------------------------------------------------


def test_graphrag_entities_limit_zero_no_longer_means_all(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """Pins the deliberate breaking change.

    ``limit=0`` was documented as "all" — on the live graph that is ~6,600
    entities MEASURED at 246,724 tokens, which no agent asks for deliberately. It now means
    ``BRAIN_GRAPH_ENTITIES_MAX_LIMIT``, and the payload says so.

    MUTATION, corrected after being RUN (the note here previously named a
    mutation that does not fail — see the warning below):

    - ``limit=effective_limit + 1`` → ``limit=effective_limit`` in the
      ``list_entities`` call: **RED** on ``truncated`` — without the over-fetch
      the tool cannot tell "exactly 5" from "5 of 12".
    - ``cap_rows(rows, limit=effective_limit)`` → ``rows, False``: **RED** on
      the count (6 ≠ 5, the over-fetched row leaks through).
    - the re-map ``state.cfg.graph_entities_max_limit`` → ``0``: **RED**, and
      loudly — ``cap_rows`` raises ``ValueError`` rather than returning the one
      row a ``LIMIT 0 + 1`` fetch would have reported as complete.

    **WARNING — a mutation that stays GREEN:** restoring ``limit=limit`` in the
    ``list_entities`` call does NOT fail this test. ``limit=0`` reaches the SQL
    layer as "all", 12 rows come back, and ``cap_rows`` still trims them to 5
    with ``truncated=True`` — every assertion holds. The re-map is guarded by
    ``cap_rows`` downstream, not by the SQL ``limit``. Do not cite that
    mutation as proof of anything.
    """
    _seed_entities(test_db, 12)
    _reconfigure(mcp_state, graph_entities_max_limit=5)

    payload = mcp_server.brain_graphrag_entities(limit=0)

    assert payload["count"] == 5, "limit=0 must be re-mapped to the ceiling"
    assert payload["limit_applied"] == 5
    assert payload["truncated"] is True


def test_graphrag_entities_truncated_is_false_at_an_exact_fit(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """``truncated`` must not fire when the true count equals the limit.

    This is the ``cap_rows`` ``>`` vs ``==`` rule from the *no-false-positive*
    side: a full-but-complete page must not claim there is more.

    It does NOT guard the over-fetch, despite reading like it does. Removing
    the ``+ 1`` leaves this test **green** — with 5 seeded and a ceiling of 5
    the over-fetch finds nothing extra either way. The over-fetch is guarded by
    ``test_graphrag_entities_limit_zero_no_longer_means_all`` above, which
    seeds 12. Verified by mutation; do not delete that test believing this one
    covers it.
    """
    _seed_entities(test_db, 5)
    _reconfigure(mcp_state, graph_entities_max_limit=5)

    payload = mcp_server.brain_graphrag_entities(limit=0)

    assert payload["count"] == 5
    assert payload["truncated"] is False


def test_graphrag_entities_limit_above_ceiling_raises_invalid_params(
    mcp_state: mcp_server._State,
) -> None:
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_graphrag_entities(
            limit=mcp_state.cfg.graph_entities_max_limit + 1
        )
    assert excinfo.value.error.code == INVALID_PARAMS


# ---------------------------------------------------------------------------
# brain_graphrag_communities
# ---------------------------------------------------------------------------


def test_graphrag_communities_default_is_finite(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The default was "all" (136 live communities ≈ ~6.5k tokens).

    MUTATION, corrected after being RUN: ``limit=effective_limit + 1`` →
    ``limit=effective_limit`` on the ``list_communities`` call goes **RED** on
    ``truncated`` (8 seeded, ceiling 3, but without the over-fetch the tool
    cannot see the 4th).

    **WARNING — a mutation that stays GREEN:** restoring ``limit=limit`` does
    NOT fail this test. The default call passes ``limit=None``, the SQL layer
    reads that as "all", and ``cap_rows`` still trims 8 → 3 with
    ``truncated=True``. Same shape as the entities twin above: the finite
    default is guarded by ``cap_rows``, not by the SQL ``limit``.
    """
    _seed_communities(test_db, 8)
    _reconfigure(mcp_state, graph_communities_list_limit=3)

    payload = mcp_server.brain_graphrag_communities()

    assert payload["count"] == 3
    assert payload["limit_applied"] == 3
    assert payload["truncated"] is True


def test_graphrag_communities_explicit_limit_cannot_raise_the_ceiling(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    _seed_communities(test_db, 8)
    _reconfigure(mcp_state, graph_communities_list_limit=3)

    assert mcp_server.brain_graphrag_communities(limit=2)["count"] == 2
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_graphrag_communities(limit=4)
    assert excinfo.value.error.code == INVALID_PARAMS


# ---------------------------------------------------------------------------
# The three bare-list tools
# ---------------------------------------------------------------------------


def test_backlinks_respects_limit_and_flags_more_available(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A bare list cannot hold an envelope, so the flag rides the last row.

    MUTATION: drop the ``saturation_notice`` wrapper (return the plain list)
    and this goes red on ``more_available``.
    """
    target = _seed_doc(test_db, title="Hub Note")
    for i in range(6):
        src = _seed_doc(test_db, title=f"Linker {i}")
        _seed_link(test_db, src, target, f"Hub Note {i}")

    rows = mcp_server.brain_backlinks(id_prefix=target, limit=3)

    assert len(rows) == 3
    assert rows[-1]["more_available"] is True
    assert all("more_available" not in r for r in rows[:-1]), (
        "only the FINAL element carries the flag"
    )


def test_backlinks_adds_no_flag_when_nothing_was_cut(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A complete list must stay byte-identical for existing consumers."""
    target = _seed_doc(test_db, title="Hub Note")
    src = _seed_doc(test_db, title="Only Linker")
    _seed_link(test_db, src, target, "Hub Note")

    rows = mcp_server.brain_backlinks(id_prefix=target)

    assert len(rows) == 1
    assert "more_available" not in rows[0]


def test_links_respects_limit(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    src = _seed_doc(test_db, title="Source Note")
    for i in range(5):
        dst = _seed_doc(test_db, title=f"Target {i}")
        _seed_link(test_db, src, dst, f"Target {i}")

    rows = mcp_server.brain_links(id_prefix=src, limit=2)

    assert len(rows) == 2
    assert rows[-1]["more_available"] is True


def test_orphans_respects_limit(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The worst bare-list case live: up to ~1,400 orphan rows."""
    for i in range(5):
        _seed_doc(test_db, title=f"Orphan {i}")

    rows = mcp_server.brain_orphans(vault_only=True, limit=2)

    assert len(rows) == 2
    assert rows[-1]["more_available"] is True


def test_bare_list_limit_above_ceiling_raises_invalid_params(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A caller may narrow a listing, never widen it past the operator's cap."""
    target = _seed_doc(test_db, title="Hub Note")
    _reconfigure(mcp_state, mcp_rows_max_limit=10)

    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_backlinks(id_prefix=target, limit=11)
    assert excinfo.value.error.code == INVALID_PARAMS


@pytest.mark.parametrize("bad_limit", [-3, 0])
def test_bare_list_rejects_non_positive_limit(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State, bad_limit: int
) -> None:
    target = _seed_doc(test_db, title="Hub Note")
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_orphans(limit=bad_limit)
    assert excinfo.value.error.code == INVALID_PARAMS
    assert target  # the seed is what makes the call otherwise well-formed
