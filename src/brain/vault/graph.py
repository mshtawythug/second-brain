"""Read-only link graph queries over the ``links`` / ``unresolved_links`` /
``derived_links`` tables.

Phase 4 read API: backlinks, outgoing links, orphans, and a
:class:`GraphData` snapshot suitable for export. Plain SELECTs over
Phase 2's wiki-link materialization plus Phase 5's metadata-derived edges —
no schema changes, no writes, no embedder dependency. Callers pass a
``psycopg.Connection``; this module never opens or closes one itself.

Resolution conventions:

- ``document_id`` is always a full UUID (string). Callers needing to
  resolve a user-supplied prefix should hand off to
  :func:`brain.queries.resolve_document_prefix` first.
- ``vault_only=True`` filters by ``documents.kind='vault'`` — the spec's
  default for orphan / graph views since ingested-tier nodes (Krisp,
  Slack, Gmail mirrors) usually have no ``[[refs]]`` and clutter the graph.

Derived-edge semantics:

- ``derived_links`` rows are stored in canonical ``(LEAST, GREATEST)``
  order and carry an undirected meaning ("these two docs share a
  thread / participant"). Read paths therefore treat a derived edge as
  symmetric: both :func:`backlinks_for` and :func:`outgoing_links_for`
  return the partner regardless of which side of the storage row the
  caller's document sits on.
- Edges materialized in Python carry ``link_kind='derived'``. The schema
  CHECK in migration 003 still restricts ``links.link_kind`` to
  ``'wiki' | 'embed'`` — derived edges live in a sibling table, so the
  enum extension is a Python-only convention.
- Wiki-link edges keep ``rule=None``, ``weight=None``,
  ``evidence=None``; derived edges populate all three.
"""
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from ..sensitivity import CONFIDENTIAL, not_confidential_sql

#: Two frozen variants per title-bearing read, selected by
#: ``exclude_confidential`` — the pattern :mod:`brain.ui.queries` uses, and for
#: the same reason: a ``%s`` placeholder for the level would put a positional
#: parameter inside a fragment three statements share, so every one of them
#: would have to bind it first, in order, or silently bind the wrong thing.
#:
#: WHY THESE READS NEEDED A GATE AT ALL. They return document TITLES. The
#: ``/api/notes/{id}/links`` rail names every document linking to the note on
#: screen, and ``routes_links``' own module docstring shows the author reasoned
#: about confidentiality and scoped it to *bodies* ("keeps a confidential body
#: out of a rail that the note route itself may have withheld") — the payload
#: carries no bodies, and so the rail looked safe. Titles were not considered.
_NOT_CONFIDENTIAL = f"AND d.sensitivity <> '{CONFIDENTIAL}'"

_BACKLINKS_SELECT = """
        SELECT d.id::text, d.title, d.kind, l.link_text, l.link_kind
        FROM links l
        JOIN documents d ON d.id = l.src_document_id
        WHERE l.dst_document_id = %s
"""
_BACKLINKS_ORDER = "        ORDER BY LOWER(d.title), l.link_text\n        "

_BACKLINKS_SQL_ANY = _BACKLINKS_SELECT + _BACKLINKS_ORDER
_BACKLINKS_SQL = f"{_BACKLINKS_SELECT}          {_NOT_CONFIDENTIAL}\n{_BACKLINKS_ORDER}"

_OUTGOING_SELECT = """
        SELECT d.id::text, d.title, d.kind, l.link_text, l.link_kind
        FROM links l
        JOIN documents d ON d.id = l.dst_document_id
        WHERE l.src_document_id = %s
"""
_OUTGOING_ORDER = _BACKLINKS_ORDER

_OUTGOING_SQL_ANY = _OUTGOING_SELECT + _OUTGOING_ORDER
_OUTGOING_SQL = f"{_OUTGOING_SELECT}          {_NOT_CONFIDENTIAL}\n{_OUTGOING_ORDER}"

#: The derived-partner read aliases the joined document ``partner``, not ``d``,
#: so it cannot share :data:`_NOT_CONFIDENTIAL`. Spelled out rather than
#: aliased-to-fit: renaming the alias to reuse one constant would make the
#: CASE expression below read as if it filtered the edge rather than the
#: partner document.
_DERIVED_SELECT = """
        SELECT
            dl.rule,
            dl.weight,
            dl.evidence,
            partner.id::text,
            partner.title,
            partner.kind
        FROM derived_links dl
        JOIN documents partner
          ON partner.id = CASE
              WHEN dl.src_document_id = %(doc)s THEN dl.dst_document_id
              ELSE dl.src_document_id
          END
        WHERE (dl.src_document_id = %(doc)s OR dl.dst_document_id = %(doc)s)
"""
_DERIVED_ORDER = (
    "        ORDER BY dl.rule, LOWER(partner.title), partner.id::text\n        "
)

_DERIVED_SQL_ANY = _DERIVED_SELECT + _DERIVED_ORDER
_DERIVED_SQL = (
    f"{_DERIVED_SELECT}          AND partner.sensitivity <> '{CONFIDENTIAL}'\n"
    f"{_DERIVED_ORDER}"
)


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
    """An edge in the link graph — wiki-link, embed, or metadata-derived.

    ``link_kind`` is one of:

    - ``'wiki'`` / ``'embed'`` — wiki-link materialization (per the
      ``links.link_kind`` CHECK constraint). ``link_text`` carries the
      raw ``[[X]]`` exactly as it appeared in the source body;
      ``display_text`` carries the pipe alias when the user wrote
      ``[[X|alias]]``. ``rule`` / ``weight`` / ``evidence`` are all
      ``None``.
    - ``'derived'`` — Python-level convention (NOT a value in the schema
      CHECK) for an edge sourced from ``derived_links``. ``link_text``
      is empty; ``display_text`` is ``None``. ``rule`` is the rule that
      fired (``'shared_thread'`` / ``'shared_participant'`` /
      ``'same_day_participant'``), ``weight`` is the rule's confidence
      (per :mod:`brain.vault.derived_links.rules`), and ``evidence``
      is the JSONB payload as a Python dict.

    Derived edges are stored as ``(src=LEAST, dst=GREATEST)`` and carry
    an undirected meaning; consumers that care about direction must
    treat the pair symmetrically.

    Self-loops (src == dst) never appear: sync's resolver excludes them
    for wiki edges, and ``derived_links``' ``CHECK (src <> dst)`` does
    the same for derived.
    """

    src_document_id: str
    dst_document_id: str
    link_kind: str
    link_text: str
    display_text: str | None
    rule: str | None = None
    weight: float | None = None
    evidence: dict[str, Any] | None = None


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

    For wiki/embed edges, ``link_text`` is the raw ``[[X]]`` and
    ``rule`` / ``weight`` / ``evidence`` are ``None``. For derived edges,
    ``link_kind='derived'``, ``link_text=''``, and ``rule`` / ``weight`` /
    ``evidence`` carry the metadata-rule provenance. Derived edges are
    undirected in semantics — ``backlinks_for(X)`` returns every doc
    paired with X via ``derived_links`` regardless of which side X sits
    on in the canonical storage row.
    """

    src_document_id: str
    src_title: str
    src_kind: str
    link_text: str
    link_kind: str
    rule: str | None = None
    weight: float | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class OutgoingLinkRow:
    """One row of "documents I link TO."

    For resolved wiki/embed links (the default), every ``dst_*`` field is
    populated. For unresolved (dangling) ``[[refs]]`` returned with
    ``include_unresolved=True``, ``dst_document_id`` / ``dst_title`` /
    ``dst_kind`` are ``None`` and ``resolved=False``.

    Derived edges are also included (with ``include_derived=True``, the
    default) and carry ``link_kind='derived'`` plus populated
    ``rule`` / ``weight`` / ``evidence``. Because derived edges are
    undirected, ``outgoing_links_for(X)`` returns the same partner set
    as ``backlinks_for(X)`` for them — every doc paired with X via
    ``derived_links``, regardless of canonical storage direction.
    """

    dst_document_id: str | None
    dst_title: str | None
    dst_kind: str | None
    link_text: str
    link_kind: str
    resolved: bool
    rule: str | None = None
    weight: float | None = None
    evidence: dict[str, Any] | None = None


def backlinks_for(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    include_derived: bool = True,
    exclude_confidential: bool = False,
) -> list[BacklinkRow]:
    """Return every document that links TO ``document_id``.

    Wiki/embed rows come from a JOIN of ``links`` against ``documents``
    (one round-trip, no N+1) and are sorted by source title
    (case-insensitive) then by ``link_text`` to break ties.

    With ``include_derived=True`` (the default), derived edges are
    appended after the wiki block: every ``derived_links`` row whose src
    or dst equals ``document_id`` contributes one row keyed on the
    *partner* document. This treats derived storage as undirected per
    spec §6 — a row stored ``(A, B)`` shows up in both ``backlinks_for(A)``
    and ``backlinks_for(B)``. Derived rows sort within their block by
    rule then partner title for deterministic output.
   

    ``exclude_confidential`` DEFAULTS FALSE, which is not the fail-closed
    convention :mod:`brain.ui.queries` uses, and the difference is the caller
    set. That module serves one surface; this one is shared by ``brain
    backlinks`` at a terminal, the MCP server, and ``brain.ui``. The first two
    are the owner reading their own corpus locally — the loopback case the
    ``browseable_tag_counts`` ruling already treats as entitled — and the CLI
    offers no ``--include-confidential`` to turn a hidden neighbour back on, so
    a fail-closed default here would remove a row the owner has no way to ask
    for. The gate therefore lives at the boundary that has a policy:
    ``ui/routes_links.note_links`` passes ``not ctx.serve_confidential_titles``
    on every request, and ``tests/test_ui_confidential_titles_gate.py`` fails
    for any UI route that names a confidential document regardless of which
    query it used — so the protection does not rest on this default.

    Note for the MCP server: ``brain_backlinks`` / ``brain_links`` do not pass
    this and have no ``include_confidential`` parameter, unlike every other F6
    retrieval surface there. Flagged, not changed — see ``_confidential_lens``.
    """
    rows = conn.execute(
        _BACKLINKS_SQL if exclude_confidential else _BACKLINKS_SQL_ANY,
        (document_id,),
    ).fetchall()
    out: list[BacklinkRow] = [
        BacklinkRow(
            src_document_id=str(r[0]),
            src_title=str(r[1]),
            src_kind=str(r[2]),
            link_text=str(r[3]),
            link_kind=str(r[4]),
        )
        for r in rows
    ]
    if include_derived:
        out.extend(
            BacklinkRow(
                src_document_id=partner.document_id,
                src_title=partner.title,
                src_kind=partner.kind,
                link_text="",
                link_kind="derived",
                rule=row.rule,
                weight=row.weight,
                evidence=row.evidence,
            )
            for row, partner in _derived_partners(
                conn, document_id, exclude_confidential=exclude_confidential
            )
        )
    return out


def outgoing_links_for(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    include_unresolved: bool = False,
    include_derived: bool = True,
    exclude_confidential: bool = False,
) -> list[OutgoingLinkRow]:
    """Return every document ``document_id`` links TO.

    With ``include_unresolved=False`` (the default), only resolved links
    are returned (``links`` table joined to ``documents`` for the dst
    title/kind).

    With ``include_unresolved=True``, the result also includes dangling
    ``[[refs]]`` from ``unresolved_links`` (``dst_document_id`` /
    ``dst_title`` / ``dst_kind`` all ``None``). Resolved rows come first,
    then unresolved.

    With ``include_derived=True`` (the default), derived edges are
    appended after the resolved wiki block (and before the unresolved
    block, when included). Because ``derived_links`` rows are undirected
    in semantics, ``outgoing_links_for(X)`` returns the same partner set
    as ``backlinks_for(X)`` for them — every doc paired with X
    regardless of canonical direction.
   

    ``exclude_confidential`` DEFAULTS FALSE, which is not the fail-closed
    convention :mod:`brain.ui.queries` uses, and the difference is the caller
    set. That module serves one surface; this one is shared by ``brain
    backlinks`` at a terminal, the MCP server, and ``brain.ui``. The first two
    are the owner reading their own corpus locally — the loopback case the
    ``browseable_tag_counts`` ruling already treats as entitled — and the CLI
    offers no ``--include-confidential`` to turn a hidden neighbour back on, so
    a fail-closed default here would remove a row the owner has no way to ask
    for. The gate therefore lives at the boundary that has a policy:
    ``ui/routes_links.note_links`` passes ``not ctx.serve_confidential_titles``
    on every request, and ``tests/test_ui_confidential_titles_gate.py`` fails
    for any UI route that names a confidential document regardless of which
    query it used — so the protection does not rest on this default.

    Note for the MCP server: ``brain_backlinks`` / ``brain_links`` do not pass
    this and have no ``include_confidential`` parameter, unlike every other F6
    retrieval surface there. Flagged, not changed — see ``_confidential_lens``.
    """
    resolved_rows = conn.execute(
        _OUTGOING_SQL if exclude_confidential else _OUTGOING_SQL_ANY,
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
    if include_derived:
        out.extend(
            OutgoingLinkRow(
                dst_document_id=partner.document_id,
                dst_title=partner.title,
                dst_kind=partner.kind,
                link_text="",
                link_kind="derived",
                resolved=True,
                rule=row.rule,
                weight=row.weight,
                evidence=row.evidence,
            )
            for row, partner in _derived_partners(
                conn, document_id, exclude_confidential=exclude_confidential
            )
        )
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
    exclude_confidential: bool = False,
) -> list[GraphNode]:
    """Return documents with zero incoming AND zero outgoing links.

    "Outgoing" includes both resolved (``links.src_document_id``) and
    unresolved (``unresolved_links.src_document_id``) — a note that wrote
    ``[[Foo]]`` once isn't an orphan even when ``Foo`` doesn't yet exist.

    A document with at least one ``derived_links`` edge (on either side)
    is also not an orphan: the metadata-aware linker has surfaced it as
    connected to another doc through shared thread / participant
    overlap, and the user's intuition for "orphan" is "nothing
    connecting it" — derived edges count.

    Defaults to ``vault_only=True`` because ingested-tier orphans are
    usually noise: most Krisp / Slack / Gmail mirrors have no ``[[refs]]``
    yet, and surfacing all of them would drown the user's own notes.
    Pass ``vault_only=False`` (or the CLI's ``--all``) to include them.

    Sort: title (case-insensitive) for deterministic output, then id to
    break ties when two notes share a title.

    ``exclude_confidential`` mirrors :func:`backlinks_for` / :func:`outgoing_links_for`
    — same name, same FALSE default (include), because the CLI sits inside the
    trust boundary. It is NEW here, and its absence is the whole reason this
    function is being touched: when the F6 gate was added to those two link
    reads, it was added to *exactly the two functions someone had named*. This
    one returns document TITLES from the same module, feeds ``brain_orphans``
    on the MCP surface, and could not be gated even in principle because the
    parameter did not exist. The enumeration miss did not stop at the MCP
    layer; it propagated down into the graph layer itself.

    Unlike the two link reads this cannot use the frozen ``_NOT_CONFIDENTIAL``
    fragment: that constant carries a leading ``AND`` for statements that
    interpolate it directly, while this query joins a ``where`` LIST with
    ``' AND '``. Appending the fragment verbatim would emit ``AND AND``. The
    bare predicate is appended instead — still parameterless, still built from
    the ``CONFIDENTIAL`` module constant rather than caller input.
    """
    where = ["d.id NOT IN (SELECT src_document_id FROM links)"]
    where.append("d.id NOT IN (SELECT dst_document_id FROM links)")
    where.append("d.id NOT IN (SELECT src_document_id FROM unresolved_links)")
    where.append("d.id NOT IN (SELECT src_document_id FROM derived_links)")
    where.append("d.id NOT IN (SELECT dst_document_id FROM derived_links)")
    if vault_only:
        where.append("d.kind = 'vault'")
    if exclude_confidential:
        where.append(f"d.sensitivity <> '{CONFIDENTIAL}'")
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
    include_derived: bool = True,
    exclude_confidential: bool = False,
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

    With ``include_derived=True`` (the default), ``derived_links`` rows
    are unioned into the edge set as ``link_kind='derived'`` edges,
    carrying their canonical ``(LEAST, GREATEST)`` direction plus the
    rule / weight / evidence triple. BFS treats derived edges as
    undirected (same as wiki edges), and ingested-tier orphan filtering
    correctly counts a doc connected only via derived edges as
    connected. ``include_derived=False`` reproduces the pre-Phase-C
    behavior.

    The function issues at most three queries (wiki edges, derived
    edges, documents) — no per-node N+1.

    Cycle safety: BFS uses a visited set, so ``A → B → A`` terminates at
    depth 2 instead of looping. Self-loops never appear.

    ``exclude_confidential`` (F6) omits confidential documents from the node set,
    and an explicit predicate below drops every edge that touches one. Dropping
    the node is NOT sufficient on its own, and this said it was — see the
    reconciliation after the document fetch for the two paths on which the
    document was withheld while its edges shipped.

    This is the FOURTH read in this module to get the parameter, and it was the
    one left short. :func:`backlinks_for`, :func:`outgoing_links_for` and
    :func:`orphans` all take it; this did not, which is the same one-short shape
    that produced the original finding — the flag was added to exactly the
    functions someone had enumerated. Its only caller today is ``cli.py``, inside
    the trust boundary, so nothing was leaking in practice; it is closed so that
    a future non-CLI caller (an MCP tool, a UI route) inherits a gateable
    function rather than discovering it cannot gate this one.

    DEFAULTS FALSE (include), matching this module's three siblings rather than
    the MCP layer's ``include_confidential``.
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
    all_edges: list[GraphEdge] = [
        GraphEdge(
            src_document_id=str(r[0]),
            dst_document_id=str(r[1]),
            link_kind=str(r[2]),
            link_text=str(r[3]),
            display_text=(str(r[4]) if r[4] is not None else None),
        )
        for r in edge_rows
    ]

    if include_derived:
        derived_rows = conn.execute(
            """
            SELECT src_document_id::text, dst_document_id::text,
                   rule, weight, evidence
            FROM derived_links
            ORDER BY src_document_id, dst_document_id, rule
            """
        ).fetchall()
        all_edges.extend(
            GraphEdge(
                src_document_id=str(r[0]),
                dst_document_id=str(r[1]),
                link_kind="derived",
                link_text="",
                display_text=None,
                rule=str(r[2]),
                weight=float(r[3]),
                evidence=_coerce_evidence(r[4]),
            )
            for r in derived_rows
        )

    # Pull every document so we can filter / look up titles in Python.
    # Personal-corpus scale (low thousands) — one fetch is cheaper than
    # repeated round-trips and keeps the SQL trivially auditable.
    #
    # F6: this fetch is the ONLY sensitivity predicate in the function, so
    # everything downstream has to be reconciled against it — see the edge gate
    # immediately after ``all_docs``.
    doc_where = "" if not exclude_confidential else (
        f"WHERE {not_confidential_sql('documents')} "
    )
    doc_rows = conn.execute(
        f"SELECT id::text, title, kind FROM documents {doc_where}"
        "ORDER BY LOWER(title), id"
    ).fetchall()
    all_docs: dict[str, GraphNode] = {
        str(r[0]): GraphNode(
            document_id=str(r[0]), title=str(r[1]), kind=str(r[2])
        )
        for r in doc_rows
    }

    # F6: reconcile the edge set against the gated document set, HERE, before
    # anything downstream derives from it. This is the separate edge predicate a
    # comment in this spot used to call dead code; it was not, and the claim was
    # true on exactly one of the three paths below.
    #
    # The gate on ``all_docs`` cannot reach ``all_edges`` or the BFS frontier,
    # because both are built from the ungated fetch above. Two consequences, one
    # root cause:
    #
    #   * ``include_ingested=True`` sets ``node_filter = None`` and took
    #     ``kept_edges = list(all_edges)`` verbatim — no edge filtering at all.
    #     A withheld document still shipped its edges, carrying its UUID and,
    #     in ``link_text``, its title.
    #   * Rooted mode filtered ``kept_edges`` on ``keep``, not on ``all_docs``,
    #     so withholding a node there would not have withheld its edges either.
    #
    # Doing it once, up here, also fixes what a downstream repair could not: the
    # BFS no longer TRAVERSES a withheld document, so a node reachable only
    # through one stops arriving as an edgeless node — an unexplained node in a
    # rooted view discloses that something hidden joins it to the root.
    #
    # Unconditional rather than gated on ``exclude_confidential``: ``links`` and
    # ``derived_links`` both declare ``REFERENCES documents(id) ON DELETE
    # CASCADE`` (migrations 003 and 005), so with the flag off ``all_docs`` is
    # every document and this is a no-op. One code path, and the invariant
    # :class:`GraphData` already documents — "an edge always has both endpoints
    # in ``nodes``" — becomes true on every path instead of on the default one.
    all_edges = [
        e
        for e in all_edges
        if e.src_document_id in all_docs and e.dst_document_id in all_docs
    ]

    if root is not None:
        # Intersect BEFORE sorting, not with a trailing ``if``: ``sorted()``
        # evaluates its key over EVERY element before a comprehension's filter
        # runs, so ``all_docs[d]`` raised ``KeyError`` on the first withheld id.
        # The old guard pre-dated ``exclude_confidential`` and was unreachable
        # while ``all_docs`` was every document.
        keep = _bfs_frontier(root, all_edges, depth=depth) & all_docs.keys()
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


@dataclass(frozen=True)
class _DerivedRow:
    """Internal projection of a ``derived_links`` row used by partner queries."""

    rule: str
    weight: float
    evidence: dict[str, Any] | None


def _derived_partners(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    exclude_confidential: bool = False,
) -> list[tuple[_DerivedRow, GraphNode]]:
    """Return ``(row, partner_node)`` pairs for every derived edge touching ``document_id``.

    ``derived_links`` rows are stored undirected as ``(LEAST, GREATEST)``,
    so a single SELECT with ``OR`` over both columns finds every edge the
    document participates in. We resolve the *partner* document_id (the
    other endpoint) plus its title/kind in one round-trip via a CASE +
    JOIN, mirroring :func:`backlinks_for`'s anti-N+1 style.

    Sort order: rule, then partner title (case-insensitive), then partner
    id. Stable across calls so callers can pin output bytes in tests.
    """
    rows = conn.execute(
        _DERIVED_SQL if exclude_confidential else _DERIVED_SQL_ANY,
        {"doc": document_id},
    ).fetchall()
    return [
        (
            _DerivedRow(
                rule=str(r[0]),
                weight=float(r[1]),
                evidence=_coerce_evidence(r[2]),
            ),
            GraphNode(
                document_id=str(r[3]),
                title=str(r[4]),
                kind=str(r[5]),
            ),
        )
        for r in rows
    ]


def _coerce_evidence(raw: Any) -> dict[str, Any] | None:
    """Normalize a JSONB column read into a Python ``dict``.

    psycopg's default JSONB adapter already returns ``dict``; this
    helper is a thin defensive copy so callers can't mutate the
    underlying row's payload. ``derived_links.evidence`` is
    ``NOT NULL DEFAULT '{}'::jsonb`` per migration 005, so an empty
    ``{}`` is the worst case — a payload with no keys. Anything else
    falls through to ``None`` and the caller treats the row as
    payload-less.
    """
    if isinstance(raw, dict):
        return dict(raw)
    return None


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
    the final :class:`GraphData`. Derived edges already store undirected
    semantics; mixing them into ``edges`` here is correct.
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
