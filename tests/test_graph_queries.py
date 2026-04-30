"""Tests for :mod:`brain.vault.graph` against a real Postgres test DB.

Each test seeds documents and (for resolved links) ``links`` /
``unresolved_links`` / ``derived_links`` rows directly via SQL. We avoid
going through the sync engine here so the test DB stays small and the
assertions stay narrow — ``brain.vault.graph`` is plain SELECTs, the
contract is that *the queries themselves* return the right shape.
"""
import json
from typing import Any

import psycopg

from brain.vault.graph import (
    backlinks_for,
    graph_data,
    orphans,
    outgoing_links_for,
)


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    kind: str = "vault",
    vault_path: str | None = None,
) -> str:
    """Insert one ``documents`` row and return its id (full UUID).

    Tests don't need chunks or embeddings — graph queries only touch
    ``documents`` (for titles + kind) and ``links`` / ``unresolved_links``.
    """
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (doc_id, title, f"body of {title}", f"hash-{doc_id}", "note", kind, vault_path),
    )
    return doc_id


def _link(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    dst: str,
    text: str = "[[X]]",
    kind: str = "wiki",
    display: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (src, dst, text, kind, display),
    )


def _unresolved(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    text: str,
    kind: str = "wiki",
    display: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO unresolved_links
          (src_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s)
        """,
        (src, text, kind, display),
    )


def _derived(
    conn: psycopg.Connection[Any],
    *,
    a: str,
    b: str,
    rule: str = "shared_thread",
    weight: float = 1.0,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert a ``derived_links`` row in canonical (LEAST, GREATEST) order.

    Mirrors the canonicalization that
    :func:`brain.vault.derived_links.pass_runner.rebuild_derived_for`
    applies, so tests stay faithful to production storage layout.
    """
    src, dst = (a, b) if a < b else (b, a)
    payload = {} if evidence is None else evidence
    conn.execute(
        """
        INSERT INTO derived_links
          (src_document_id, dst_document_id, rule, evidence, weight)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (src, dst, rule, json.dumps(payload), weight),
    )


def _make_chain(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    """Return ids for the standard test corpus.

    A → B → C — three vault notes in a chain.
    D — an isolated vault orphan (no links).
    E — an ingested-tier doc linked from A.
    F — a vault note with one outgoing dangling ref ([[Nowhere]]).
    """
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    c = _make_doc(test_db, doc_id="33333333-3333-3333-3333-333333333333",
                  title="C", vault_path="c.md")
    d = _make_doc(test_db, doc_id="44444444-4444-4444-4444-444444444444",
                  title="D orphan", vault_path="d.md")
    e = _make_doc(
        test_db,
        doc_id="55555555-5555-5555-5555-555555555555",
        title="E ingested",
        kind="ingested",
        vault_path="_ingested/manual/e.md",
    )
    f = _make_doc(test_db, doc_id="66666666-6666-6666-6666-666666666666",
                  title="F dangler", vault_path="f.md")

    _link(test_db, src=a, dst=b, text="[[B]]")
    _link(test_db, src=b, dst=c, text="[[C]]")
    _link(test_db, src=a, dst=e, text="[[brain:55555555]]")
    _unresolved(test_db, src=f, text="[[Nowhere]]")
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f}


def test_backlinks_for_returns_inbound_links(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    rows = backlinks_for(test_db, ids["b"])
    assert len(rows) == 1
    assert rows[0].src_document_id == ids["a"]
    assert rows[0].src_title == "A"
    assert rows[0].src_kind == "vault"
    assert rows[0].link_text == "[[B]]"
    assert rows[0].link_kind == "wiki"


def test_backlinks_for_empty_when_none(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    # A has no incoming.
    assert backlinks_for(test_db, ids["a"]) == []
    # D is fully isolated.
    assert backlinks_for(test_db, ids["d"]) == []


def test_backlinks_for_sorts_by_title(
    test_db: psycopg.Connection[Any],
) -> None:
    """Multiple sources linking to the same dst sort case-insensitively by src title."""
    ids = _make_chain(test_db)
    z = _make_doc(test_db, doc_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                  title="zeta", vault_path="z.md")
    m = _make_doc(test_db, doc_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                  title="MIXED", vault_path="m.md")
    _link(test_db, src=z, dst=ids["b"], text="[[B-from-z]]")
    _link(test_db, src=m, dst=ids["b"], text="[[B-from-m]]")
    rows = backlinks_for(test_db, ids["b"])
    titles = [r.src_title for r in rows]
    # "A" < "MIXED" < "zeta" case-insensitively.
    assert titles == ["A", "MIXED", "zeta"]


def test_outgoing_links_for_resolved_only(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    rows = outgoing_links_for(test_db, ids["a"])
    assert len(rows) == 2
    assert all(r.resolved for r in rows)
    titles = sorted(r.dst_title for r in rows if r.dst_title is not None)
    assert titles == ["B", "E ingested"]


def test_outgoing_links_for_includes_unresolved_when_requested(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    rows = outgoing_links_for(test_db, ids["f"], include_unresolved=True)
    assert len(rows) == 1
    row = rows[0]
    assert not row.resolved
    assert row.dst_document_id is None
    assert row.dst_title is None
    assert row.dst_kind is None
    assert row.link_text == "[[Nowhere]]"


def test_outgoing_links_excludes_unresolved_by_default(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    assert outgoing_links_for(test_db, ids["f"]) == []


def test_outgoing_links_resolved_block_then_unresolved_block(
    test_db: psycopg.Connection[Any],
) -> None:
    """Resolved rows precede unresolved rows; each block sorts internally."""
    ids = _make_chain(test_db)
    # Add a dangling ref from A as well.
    _unresolved(test_db, src=ids["a"], text="[[Future]]")
    rows = outgoing_links_for(test_db, ids["a"], include_unresolved=True)
    assert len(rows) == 3
    # First two resolved, last unresolved.
    assert [r.resolved for r in rows] == [True, True, False]


def test_orphans_vault_only_default(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    rows = orphans(test_db)
    # D is a vault orphan; everything else either has links or is ingested.
    assert {r.document_id for r in rows} == {ids["d"]}


def test_orphans_with_all_includes_ingested(
    test_db: psycopg.Connection[Any],
) -> None:
    """An isolated ingested-tier doc only appears with vault_only=False."""
    ids = _make_chain(test_db)
    isolated_ingested = _make_doc(
        test_db,
        doc_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        title="lonely ingested",
        kind="ingested",
        vault_path="_ingested/krisp/lonely.md",
    )
    vault_orphans = orphans(test_db, vault_only=True)
    assert {r.document_id for r in vault_orphans} == {ids["d"]}
    all_orphans = orphans(test_db, vault_only=False)
    assert {r.document_id for r in all_orphans} == {ids["d"], isolated_ingested}


def test_orphans_doc_with_only_unresolved_link_is_not_orphan(
    test_db: psycopg.Connection[Any],
) -> None:
    """A note that wrote ``[[Foo]]`` once is not an orphan even if Foo is missing."""
    ids = _make_chain(test_db)
    # F has only an unresolved link, but it should NOT be in the orphans list
    # (it has outgoing intent).
    rows = orphans(test_db)
    ids_seen = {r.document_id for r in rows}
    assert ids["f"] not in ids_seen


def test_graph_data_full_vault_only(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _make_chain(test_db)
    snapshot = graph_data(test_db)
    node_ids = {n.document_id for n in snapshot.nodes}
    # Vault-tier nodes always included; the ingested doc is also included
    # because it's connected by an edge from A. Ingested orphan would be
    # dropped, but in our chain the ingested E is connected.
    assert ids["a"] in node_ids
    assert ids["b"] in node_ids
    assert ids["c"] in node_ids
    assert ids["d"] in node_ids  # orphan, vault tier — included
    assert ids["e"] in node_ids  # ingested, but connected
    assert ids["f"] in node_ids  # vault tier, has unresolved (still rendered)
    edge_pairs = {
        (e.src_document_id, e.dst_document_id) for e in snapshot.edges
    }
    assert (ids["a"], ids["b"]) in edge_pairs
    assert (ids["b"], ids["c"]) in edge_pairs
    assert (ids["a"], ids["e"]) in edge_pairs


def test_graph_data_drops_isolated_ingested_node(
    test_db: psycopg.Connection[Any],
) -> None:
    """Ingested-tier orphans don't appear in the default vault-only graph."""
    _make_chain(test_db)
    isolated_ingested = _make_doc(
        test_db,
        doc_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        title="lonely ingested",
        kind="ingested",
        vault_path="_ingested/krisp/lonely.md",
    )
    snapshot = graph_data(test_db, include_ingested=False)
    assert isolated_ingested not in {n.document_id for n in snapshot.nodes}


def test_graph_data_include_ingested_keeps_ingested_orphans(
    test_db: psycopg.Connection[Any],
) -> None:
    _make_chain(test_db)
    isolated_ingested = _make_doc(
        test_db,
        doc_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        title="lonely ingested",
        kind="ingested",
        vault_path="_ingested/krisp/lonely.md",
    )
    snapshot = graph_data(test_db, include_ingested=True)
    assert isolated_ingested in {n.document_id for n in snapshot.nodes}


def test_graph_data_root_depth_one(
    test_db: psycopg.Connection[Any],
) -> None:
    """BFS rooted at A with depth 1 includes A's immediate neighbours."""
    ids = _make_chain(test_db)
    snapshot = graph_data(test_db, root=ids["a"], depth=1)
    node_ids = {n.document_id for n in snapshot.nodes}
    assert node_ids == {ids["a"], ids["b"], ids["e"]}
    # C is depth 2 from A — excluded.
    assert ids["c"] not in node_ids


def test_graph_data_root_depth_zero(
    test_db: psycopg.Connection[Any],
) -> None:
    """Depth 0 returns just the root node (no edges)."""
    ids = _make_chain(test_db)
    snapshot = graph_data(test_db, root=ids["a"], depth=0)
    assert {n.document_id for n in snapshot.nodes} == {ids["a"]}
    assert snapshot.edges == []


def test_graph_data_root_unlimited_depth(
    test_db: psycopg.Connection[Any],
) -> None:
    """No depth means the whole connected component."""
    ids = _make_chain(test_db)
    snapshot = graph_data(test_db, root=ids["a"], depth=None)
    node_ids = {n.document_id for n in snapshot.nodes}
    # A's connected component reaches B, C, E (undirected BFS).
    assert ids["a"] in node_ids
    assert ids["b"] in node_ids
    assert ids["c"] in node_ids
    assert ids["e"] in node_ids
    # D and F are isolated from A.
    assert ids["d"] not in node_ids
    assert ids["f"] not in node_ids


def test_graph_data_root_handles_cycle(
    test_db: psycopg.Connection[Any],
) -> None:
    """A → B → A must terminate (no infinite loop) and produce both nodes."""
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="cyclic A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="cyclic B", vault_path="b.md")
    _link(test_db, src=a, dst=b)
    _link(test_db, src=b, dst=a)
    snapshot = graph_data(test_db, root=a, depth=10)
    assert {n.document_id for n in snapshot.nodes} == {a, b}
    # Both directed edges preserved in output.
    assert len(snapshot.edges) == 2


def test_graph_data_empty(test_db: psycopg.Connection[Any]) -> None:
    snapshot = graph_data(test_db)
    assert snapshot.nodes == []
    assert snapshot.edges == []


def test_graph_data_ignores_orphans_outside_root_frontier(
    test_db: psycopg.Connection[Any],
) -> None:
    """When ``root`` is set, vault orphans NOT in the BFS frontier are dropped."""
    ids = _make_chain(test_db)
    snapshot = graph_data(test_db, root=ids["a"], depth=1)
    # D is a vault orphan but not reachable from A.
    assert ids["d"] not in {n.document_id for n in snapshot.nodes}


def test_graph_data_edge_carries_display_text(
    test_db: psycopg.Connection[Any],
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B|alias]]", display="alias")
    snapshot = graph_data(test_db)
    assert len(snapshot.edges) == 1
    assert snapshot.edges[0].display_text == "alias"
    assert snapshot.edges[0].link_text == "[[B|alias]]"


def test_graph_data_edge_kind_embed_preserved(
    test_db: psycopg.Connection[Any],
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="![[B]]", kind="embed")
    snapshot = graph_data(test_db)
    assert snapshot.edges[0].link_kind == "embed"


# ---------------------------------------------------------------------------
# C.1 — derived edges merged into the read paths.
# ---------------------------------------------------------------------------


def _gmail_pair(
    test_db: psycopg.Connection[Any],
) -> dict[str, str]:
    """Two ingested-tier Gmail-style docs ready for a derived edge."""
    x = _make_doc(
        test_db,
        doc_id="aaaaaaaa-1111-1111-1111-111111111111",
        title="Email X",
        kind="ingested",
        vault_path="_ingested/gmail/x.md",
    )
    y = _make_doc(
        test_db,
        doc_id="bbbbbbbb-2222-2222-2222-222222222222",
        title="Email Y",
        kind="ingested",
        vault_path="_ingested/gmail/y.md",
    )
    return {"x": x, "y": y}


def test_graph_data_includes_derived_edges_by_default(
    test_db: psycopg.Connection[Any],
) -> None:
    """Derived edges land in ``graph_data().edges`` with ``link_kind='derived'``."""
    ids = _gmail_pair(test_db)
    _derived(
        test_db,
        a=ids["x"],
        b=ids["y"],
        rule="shared_thread",
        weight=1.0,
        evidence={"thread_id": "thr-9001"},
    )
    snapshot = graph_data(test_db, include_ingested=True)
    derived_edges = [e for e in snapshot.edges if e.link_kind == "derived"]
    assert len(derived_edges) == 1
    edge = derived_edges[0]
    assert edge.rule == "shared_thread"
    assert edge.weight == 1.0
    assert edge.evidence == {"thread_id": "thr-9001"}
    # Stored canonically (LEAST, GREATEST) — confirm the pair appears in
    # the result regardless of the (a, b) order we passed in.
    pair = {edge.src_document_id, edge.dst_document_id}
    assert pair == {ids["x"], ids["y"]}
    assert edge.link_text == ""
    assert edge.display_text is None


def test_graph_data_excludes_derived_when_include_derived_false(
    test_db: psycopg.Connection[Any],
) -> None:
    """``include_derived=False`` reproduces the pre-Phase-C edge set."""
    ids = _gmail_pair(test_db)
    _derived(test_db, a=ids["x"], b=ids["y"], rule="shared_thread", weight=1.0)
    snapshot = graph_data(test_db, include_ingested=True, include_derived=False)
    assert all(e.link_kind != "derived" for e in snapshot.edges)
    # No wiki edges seeded — entire edge list is empty.
    assert snapshot.edges == []


def test_graph_data_bfs_traverses_derived_edges(
    test_db: psycopg.Connection[Any],
) -> None:
    """A node only reachable via a derived edge still lands in a rooted BFS."""
    ids = _gmail_pair(test_db)
    _derived(test_db, a=ids["x"], b=ids["y"], rule="shared_thread", weight=1.0)
    snapshot = graph_data(test_db, root=ids["x"], depth=1)
    assert {n.document_id for n in snapshot.nodes} == {ids["x"], ids["y"]}


def test_backlinks_for_includes_derived_edges(
    test_db: psycopg.Connection[Any],
) -> None:
    """``backlinks_for`` returns the partner regardless of canonical side."""
    ids = _gmail_pair(test_db)
    _derived(
        test_db,
        a=ids["x"],
        b=ids["y"],
        rule="shared_thread",
        weight=1.0,
        evidence={"thread_id": "thr-1"},
    )
    rows_y = backlinks_for(test_db, ids["y"])
    derived_y = [r for r in rows_y if r.link_kind == "derived"]
    assert len(derived_y) == 1
    assert derived_y[0].src_document_id == ids["x"]
    assert derived_y[0].rule == "shared_thread"
    assert derived_y[0].weight == 1.0
    assert derived_y[0].evidence == {"thread_id": "thr-1"}

    # Symmetric: backlinks_for(X) also surfaces Y as the partner — the
    # storage row is undirected by spec §6.
    rows_x = backlinks_for(test_db, ids["x"])
    derived_x = [r for r in rows_x if r.link_kind == "derived"]
    assert len(derived_x) == 1
    assert derived_x[0].src_document_id == ids["y"]


def test_backlinks_for_excludes_derived_when_flag_off(
    test_db: psycopg.Connection[Any],
) -> None:
    """``include_derived=False`` reproduces the wiki-only behavior."""
    ids = _gmail_pair(test_db)
    _derived(test_db, a=ids["x"], b=ids["y"], rule="shared_thread", weight=1.0)
    rows = backlinks_for(test_db, ids["y"], include_derived=False)
    assert rows == []


def test_outgoing_links_for_includes_derived_edges(
    test_db: psycopg.Connection[Any],
) -> None:
    """``outgoing_links_for`` is symmetric for derived edges (same as backlinks)."""
    ids = _gmail_pair(test_db)
    _derived(
        test_db,
        a=ids["x"],
        b=ids["y"],
        rule="same_day_participant",
        weight=0.7,
        evidence={"participant": "person-a@example.com", "day_delta": 0},
    )
    rows_x = outgoing_links_for(test_db, ids["x"])
    derived_x = [r for r in rows_x if r.link_kind == "derived"]
    assert len(derived_x) == 1
    assert derived_x[0].dst_document_id == ids["y"]
    assert derived_x[0].rule == "same_day_participant"
    assert derived_x[0].weight == 0.7
    assert derived_x[0].resolved is True

    rows_y = outgoing_links_for(test_db, ids["y"])
    derived_y = [r for r in rows_y if r.link_kind == "derived"]
    assert len(derived_y) == 1
    assert derived_y[0].dst_document_id == ids["x"]


def test_derived_edge_carries_rule_weight_evidence(
    test_db: psycopg.Connection[Any],
) -> None:
    """Every derived row in graph_data is populated with the rule triple."""
    ids = _gmail_pair(test_db)
    _derived(
        test_db,
        a=ids["x"],
        b=ids["y"],
        rule="shared_participant",
        weight=0.4,
        evidence={"participant": "ali@example.com", "shared_count": 3},
    )
    snapshot = graph_data(test_db, include_ingested=True)
    derived_edges = [e for e in snapshot.edges if e.link_kind == "derived"]
    assert len(derived_edges) == 1
    edge = derived_edges[0]
    assert edge.rule == "shared_participant"
    assert edge.weight == 0.4
    assert edge.evidence == {"participant": "ali@example.com", "shared_count": 3}


def test_backlinks_for_no_double_count_with_wiki_and_derived(
    test_db: psycopg.Connection[Any],
) -> None:
    """A wiki-edge and a derived edge between the same pair render as separate rows."""
    a = _make_doc(
        test_db,
        doc_id="11111111-1111-1111-1111-111111111111",
        title="A",
        vault_path="a.md",
    )
    b = _make_doc(
        test_db,
        doc_id="22222222-2222-2222-2222-222222222222",
        title="B",
        vault_path="b.md",
    )
    _link(test_db, src=a, dst=b, text="[[B]]")
    _derived(
        test_db,
        a=a,
        b=b,
        rule="shared_participant",
        weight=0.4,
        evidence={"participant": "x@y.com"},
    )
    rows = backlinks_for(test_db, b)
    assert len(rows) == 2
    kinds = sorted(r.link_kind for r in rows)
    assert kinds == ["derived", "wiki"]
    # Both source the same partner (A) but with distinct provenance.
    assert {r.src_document_id for r in rows} == {a}
    derived_row = next(r for r in rows if r.link_kind == "derived")
    wiki_row = next(r for r in rows if r.link_kind == "wiki")
    assert derived_row.rule == "shared_participant"
    assert derived_row.weight == 0.4
    assert derived_row.link_text == ""
    assert wiki_row.rule is None
    assert wiki_row.weight is None
    assert wiki_row.link_text == "[[B]]"


def test_graph_data_orphan_with_only_derived_edge_is_not_orphan(
    test_db: psycopg.Connection[Any],
) -> None:
    """Ingested docs connected only by a derived edge survive default filtering.

    With ``include_ingested=False`` the existing rule keeps an ingested-tier
    node only when it sits on at least one edge. A derived edge counts as
    an edge — so a Gmail↔Gmail derived pair stays in the snapshot rather
    than being culled as "isolated ingested orphans."
    """
    ids = _gmail_pair(test_db)
    _derived(test_db, a=ids["x"], b=ids["y"], rule="shared_thread", weight=1.0)
    snapshot = graph_data(test_db)  # default include_ingested=False
    node_ids = {n.document_id for n in snapshot.nodes}
    assert ids["x"] in node_ids
    assert ids["y"] in node_ids


def test_orphans_excludes_doc_with_only_derived_edge(
    test_db: psycopg.Connection[Any],
) -> None:
    """A vault note paired only via a derived edge is not flagged as an orphan."""
    a = _make_doc(
        test_db,
        doc_id="11111111-1111-1111-1111-111111111111",
        title="A",
        vault_path="a.md",
    )
    b = _make_doc(
        test_db,
        doc_id="22222222-2222-2222-2222-222222222222",
        title="B",
        vault_path="b.md",
    )
    _derived(test_db, a=a, b=b, rule="shared_thread", weight=1.0)
    rows = orphans(test_db)
    assert {r.document_id for r in rows} == set()


def test_graph_data_derived_edges_render_in_whole_graph_view(
    test_db: psycopg.Connection[Any],
) -> None:
    """Whole-graph (no root) view also keeps derived edges intact."""
    ids = _gmail_pair(test_db)
    _derived(
        test_db,
        a=ids["x"],
        b=ids["y"],
        rule="shared_thread",
        weight=1.0,
        evidence={"thread_id": "t1"},
    )
    snapshot = graph_data(test_db)
    derived = [e for e in snapshot.edges if e.link_kind == "derived"]
    assert len(derived) == 1
    assert derived[0].rule == "shared_thread"
