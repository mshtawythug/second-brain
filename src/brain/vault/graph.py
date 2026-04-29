"""Read-only link graph queries over the ``links`` / ``unresolved_links`` tables.

Phase 4 read API: backlinks, outgoing links, orphans, and a
:class:`GraphData` snapshot suitable for export. All helpers are plain
SELECTs over Phase 2's link materialization — no schema changes, no
writes, no embedder dependency. Callers pass a ``psycopg.Connection``;
this module never opens or closes one itself.

Resolution conventions:

- ``document_id`` is always a full UUID (string). Callers needing to
  resolve a user-supplied prefix should hand off to
  :func:`brain.queries.resolve_document_prefix` first.
- ``vault_only=True`` filters by ``documents.kind='vault'`` — the spec's
  default for orphan / graph views since ingested-tier nodes (Krisp,
  Slack, Gmail mirrors) usually have no ``[[refs]]`` and clutter the graph.
"""
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class GraphNode:
    """A document node in the link graph.

    ``kind`` mirrors ``documents.kind`` (``'vault'`` or ``'ingested'``);
    formatters use it to color/style nodes (vault = primary, ingested =
    muted).
    """

    document_id: str
    title: str
    kind: str


@dataclass(frozen=True)
class GraphEdge:
    """A directed wiki-link edge from ``src_document_id`` to ``dst_document_id``.

    ``link_kind`` is ``'wiki'`` or ``'embed'`` (per the schema's CHECK).
    ``link_text`` is the raw ``[[X]]`` text exactly as it appeared in the
    source body (round-trippable). ``display_text`` carries the pipe alias
    when the user wrote ``[[X|alias]]``; ``None`` otherwise.

    Self-loops (src == dst) are excluded by every query in this module —
    sync's resolver already prevents a note from linking to itself.
    """

    src_document_id: str
    dst_document_id: str
    link_kind: str
    link_text: str
    display_text: str | None


@dataclass(frozen=True)
class GraphData:
    """Snapshot of the link graph used by every formatter.

    ``nodes`` and ``edges`` are independent: a node may have no edges
    (orphan, isolated when ``include_ingested=False``); an edge always has
    both endpoints in ``nodes`` (we never emit an edge that points at a
    node not in the snapshot).
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]


@dataclass(frozen=True)
class BacklinkRow:
    """One row of "documents that link TO me."

    ``src_*`` describes the source of the inbound link; the destination is
    the document the caller passed to :func:`backlinks_for`.
    """

    src_document_id: str
    src_title: str
    src_kind: str
    link_text: str
    link_kind: str


@dataclass(frozen=True)
class OutgoingLinkRow:
    """One row of "documents I link TO."

    For resolved links (the default), every ``dst_*`` field is populated.
    For unresolved (dangling) ``[[refs]]`` returned with
    ``include_unresolved=True``, ``dst_document_id`` / ``dst_title`` /
    ``dst_kind`` are ``None`` and ``resolved=False``.
    """

    dst_document_id: str | None
    dst_title: str | None
    dst_kind: str | None
    link_text: str
    link_kind: str
    resolved: bool


def backlinks_for(
    conn: psycopg.Connection[Any],
    document_id: str,
) -> list[BacklinkRow]:
    """Return every document that links TO ``document_id``.

    Single SQL: a JOIN of ``links`` against ``documents`` to fetch the
    source title / kind in one round-trip (no N+1). Result is sorted by
    source title (case-insensitive) for deterministic output, then by
    ``link_text`` to break ties when the same source has multiple
    distinct ``[[refs]]`` to the same dst.
    """
    rows = conn.execute(
        """
        SELECT d.id::text, d.title, d.kind, l.link_text, l.link_kind
        FROM links l
        JOIN documents d ON d.id = l.src_document_id
        WHERE l.dst_document_id = %s
        ORDER BY LOWER(d.title), l.link_text
        """,
        (document_id,),
    ).fetchall()
    return [
        BacklinkRow(
            src_document_id=str(r[0]),
            src_title=str(r[1]),
            src_kind=str(r[2]),
            link_text=str(r[3]),
            link_kind=str(r[4]),
        )
        for r in rows
    ]


def outgoing_links_for(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    include_unresolved: bool = False,
) -> list[OutgoingLinkRow]:
    """Return every document ``document_id`` links TO.

    With ``include_unresolved=False`` (the default), only resolved links
    are returned (``links`` table joined to ``documents`` for the dst
    title/kind).

    With ``include_unresolved=True``, the result also includes dangling
    ``[[refs]]`` from ``unresolved_links`` (``dst_document_id`` /
    ``dst_title`` / ``dst_kind`` all ``None``). Resolved rows come first,
    then unresolved — both blocks individually sorted by ``link_text`` for
    stable output.
    """
    resolved_rows = conn.execute(
        """
        SELECT d.id::text, d.title, d.kind, l.link_text, l.link_kind
        FROM links l
        JOIN documents d ON d.id = l.dst_document_id
        WHERE l.src_document_id = %s
        ORDER BY LOWER(d.title), l.link_text
        """,
        (document_id,),
    ).fetchall()
    out: list[OutgoingLinkRow] = [
        OutgoingLinkRow(
            dst_document_id=str(r[0]),
            dst_title=str(r[1]),
            dst_kind=str(r[2]),
            link_text=str(r[3]),
            link_kind=str(r[4]),
            resolved=True,
        )
        for r in resolved_rows
    ]
    if include_unresolved:
        unresolved_rows = conn.execute(
            """
            SELECT link_text, link_kind
            FROM unresolved_links
            WHERE src_document_id = %s
            ORDER BY link_text
            """,
            (document_id,),
        ).fetchall()
        out.extend(
            OutgoingLinkRow(
                dst_document_id=None,
                dst_title=None,
                dst_kind=None,
                link_text=str(r[0]),
                link_kind=str(r[1]),
                resolved=False,
            )
            for r in unresolved_rows
        )
    return out


def orphans(
    conn: psycopg.Connection[Any],
    *,
    vault_only: bool = True,
) -> list[GraphNode]:
    """Return documents with zero incoming AND zero outgoing links.

    "Outgoing" includes both resolved (``links.src_document_id``) and
    unresolved (``unresolved_links.src_document_id``) — a note that wrote
    ``[[Foo]]`` once isn't an orphan even when ``Foo`` doesn't yet exist.

    Defaults to ``vault_only=True`` because ingested-tier orphans are
    usually noise: most Krisp / Slack / Gmail mirrors have no ``[[refs]]``
    yet, and surfacing all of them would drown the user's own notes.
    Pass ``vault_only=False`` (or the CLI's ``--all``) to include them.

    Sort: title (case-insensitive) for deterministic output, then id to
    break ties when two notes share a title.
    """
    where = ["d.id NOT IN (SELECT src_document_id FROM links)"]
    where.append("d.id NOT IN (SELECT dst_document_id FROM links)")
    where.append("d.id NOT IN (SELECT src_document_id FROM unresolved_links)")
    if vault_only:
        where.append("d.kind = 'vault'")
    sql = f"""
        SELECT d.id::text, d.title, d.kind
        FROM documents d
        WHERE {' AND '.join(where)}
        ORDER BY LOWER(d.title), d.id
    """
    rows = conn.execute(sql).fetchall()
    return [
        GraphNode(document_id=str(r[0]), title=str(r[1]), kind=str(r[2]))
        for r in rows
    ]


def graph_data(
    conn: psycopg.Connection[Any],
    *,
    root: str | None = None,
    depth: int | None = None,
    include_ingested: bool = False,
) -> GraphData:
    """Build a :class:`GraphData` snapshot for export.

    Without ``root``: every document with at least one link in or out, plus
    every vault-tier orphan (so the user sees their isolated thinking
    surfaces). With ``include_ingested=True`` the linked-only ingested
    tier comes along too; ingested-tier orphans stay omitted (noise).

    With ``root`` set: BFS outward from the root for ``depth`` hops
    (``None`` = unlimited). Only nodes within the BFS frontier are
    returned, and only edges where BOTH endpoints are in the frontier.
    The traversal is undirected on the edge set (you reach a backlink the
    same way you reach an outgoing link) so a focused view contains the
    immediate neighbourhood regardless of direction. Edges in the result
    keep their original direction.

    The function issues at most three queries (nodes, links, optional
    BFS-restricting filter applied in Python) — no per-node N+1.

    Cycle safety: BFS uses a visited set, so ``A → B → A`` terminates at
    depth 2 instead of looping. Self-loops never appear (sync's resolver
    excludes them).
    """
    # Single fetch of every link, plus a filter on the document set —
    # cheaper at personal-corpus scale than per-node SELECTs even when
    # rooted, and trivially cached.
    edge_rows = conn.execute(
        """
        SELECT src_document_id::text, dst_document_id::text,
               link_kind, link_text, display_text
        FROM links
        ORDER BY src_document_id, dst_document_id, link_text, link_kind
        """
    ).fetchall()
    all_edges = [
        GraphEdge(
            src_document_id=str(r[0]),
            dst_document_id=str(r[1]),
            link_kind=str(r[2]),
            link_text=str(r[3]),
            display_text=(str(r[4]) if r[4] is not None else None),
        )
        for r in edge_rows
    ]

    # Pull every document so we can filter / look up titles in Python.
    # Personal-corpus scale (low thousands) — one fetch is cheaper than
    # repeated round-trips and keeps the SQL trivially auditable.
    doc_rows = conn.execute(
        "SELECT id::text, title, kind FROM documents ORDER BY LOWER(title), id"
    ).fetchall()
    all_docs: dict[str, GraphNode] = {
        str(r[0]): GraphNode(
            document_id=str(r[0]), title=str(r[1]), kind=str(r[2])
        )
        for r in doc_rows
    }

    if root is not None:
        keep = _bfs_frontier(root, all_edges, depth=depth)
        # Filter edges to those entirely within the frontier.
        kept_edges = [
            e
            for e in all_edges
            if e.src_document_id in keep and e.dst_document_id in keep
        ]
        kept_nodes = [
            all_docs[doc_id]
            for doc_id in sorted(
                keep, key=lambda d: (all_docs[d].title.lower(), d)
            )
            if doc_id in all_docs
        ]
        return GraphData(nodes=kept_nodes, edges=kept_edges)

    # Whole-graph mode.
    if include_ingested:
        node_filter: set[str] | None = None
    else:
        # Vault-tier nodes always included; ingested-tier nodes only when
        # they sit on at least one edge (so a vault note linking to an
        # ingested artifact still shows the artifact). Ingested-tier
        # orphans are dropped — they're noise per spec.
        connected = _connected_node_set(all_edges)
        node_filter = {
            doc_id
            for doc_id, node in all_docs.items()
            if node.kind == "vault" or doc_id in connected
        }

    if node_filter is None:
        kept_nodes = list(all_docs.values())
        kept_edges = list(all_edges)
    else:
        kept_nodes = [
            all_docs[doc_id]
            for doc_id in sorted(
                node_filter, key=lambda d: (all_docs[d].title.lower(), d)
            )
        ]
        kept_edges = [
            e
            for e in all_edges
            if e.src_document_id in node_filter
            and e.dst_document_id in node_filter
        ]
    return GraphData(nodes=kept_nodes, edges=kept_edges)


def _connected_node_set(edges: Iterable[GraphEdge]) -> set[str]:
    """Return every document_id that appears as src or dst in ``edges``."""
    out: set[str] = set()
    for e in edges:
        out.add(e.src_document_id)
        out.add(e.dst_document_id)
    return out


def _bfs_frontier(
    root: str, edges: Iterable[GraphEdge], *, depth: int | None
) -> set[str]:
    """Undirected BFS from ``root`` over ``edges``, capped at ``depth`` hops.

    Returns the set of document_ids reachable from ``root`` within
    ``depth`` hops (inclusive). ``depth=None`` means unlimited (all
    reachable nodes). ``depth=0`` returns just ``{root}``.

    The traversal is undirected (treats every edge as bidirectional)
    because a "graph view rooted at X" intuitively pulls in both backlinks
    and outgoing links — the user expects to see X's neighbourhood in
    every direction. Edge orientation is preserved later when building
    the final :class:`GraphData`.
    """
    # Build an undirected adjacency map once. Cheaper than scanning the
    # edge list per hop (O(|edges|) per hop vs. O(|nodes|) per hop after
    # this preprocessing).
    adjacency: dict[str, set[str]] = {}
    for e in edges:
        adjacency.setdefault(e.src_document_id, set()).add(e.dst_document_id)
        adjacency.setdefault(e.dst_document_id, set()).add(e.src_document_id)

    visited: set[str] = {root}
    if depth == 0:
        return visited

    queue: deque[tuple[str, int]] = deque([(root, 0)])
    while queue:
        node, dist = queue.popleft()
        if depth is not None and dist >= depth:
            continue
        for neighbour in adjacency.get(node, ()):
            if neighbour in visited:
                continue
            visited.add(neighbour)
            queue.append((neighbour, dist + 1))
    return visited
