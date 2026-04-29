"""Tests for :mod:`brain.vault.graph` against a real Postgres test DB.

Each test seeds documents and (for resolved links) ``links`` /
``unresolved_links`` rows directly via SQL. We avoid going through the
sync engine here so the test DB stays small and the assertions stay
narrow — ``brain.vault.graph`` is plain SELECTs, the contract is that
*the queries themselves* return the right shape.
"""
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
