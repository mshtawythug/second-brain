"""Byte-deterministic exporters for :class:`brain.vault.graph.GraphData`.

Three target formats:

- JSON — stable shape with ``sort_keys=True``; ``json.loads`` round-trips.
- Graphviz DOT — single ``digraph G {}``; pipe to ``dot -Tsvg`` for an SVG.
- Mermaid — ``graph TD`` (top-down); paste into a Mermaid renderer.

Determinism contract: every output is a pure function of the
:class:`GraphData` input. No timestamps, no random sorts, no environment
reads. Tests pin exact byte strings; any change to a formatter will
break them — that's the point.

Derived-edge tier styling (per spec §10 Q3):

- Wiki-link edges keep the existing solid black look — they are the
  user's authoritative thinking surface.
- ``derived``/``shared_thread`` (R1, weight 1.0) — strongest derived
  signal, rendered bold (Mermaid ``==>``, DOT ``style=bold``).
- ``derived``/``same_day_participant`` (R3, weight 0.7) — medium
  confidence, plain solid arrow but in gray (Mermaid ``-->`` with a
  ``linkStyle`` directive, DOT ``color=gray``).
- ``derived``/``shared_participant`` (R2, weight 0.4) — noisiest tier,
  visually subordinated as dotted/light gray (Mermaid ``-.->``, DOT
  ``style=dotted, color="#cccccc"``).

JSON output passes ``rule`` / ``weight`` / ``evidence`` straight through
on every edge regardless of tier; wiki edges carry ``null`` for all three
to keep the schema stable.
"""
import hashlib
import json
from typing import Any

from .graph import GraphData, GraphEdge, GraphNode

# Long titles get truncated in DOT/Mermaid labels for readability — graphs
# with even moderately verbose titles produce unreadable SVG otherwise.
# JSON keeps the full title (machine-readable; renderers can wrap).
_LABEL_TRUNCATE_AT = 60
_TRUNCATION_SUFFIX = "…"  # `…`

# Mermaid node ids must be alphanumeric or underscore. We strip hyphens
# from the UUID prefix; if that produces a collision we add a stable hash
# suffix (4 hex chars) computed from the full document_id.
_MERMAID_ID_PREFIX_LEN = 8
_MERMAID_HASH_SUFFIX_LEN = 4

# Vault-tier nodes get the "primary" color; ingested-tier nodes get a
# muted gray so the user's authored notes pop visually.
_DOT_FILL_VAULT = "lightblue"
_DOT_FILL_INGESTED = "lightgray"
# Mermaid uses class definitions; the body of these is byte-stable.
_MERMAID_CLASS_VAULT = (
    "classDef vault fill:#cfe5f5,stroke:#3a7ab8,color:#000;"
)
_MERMAID_CLASS_INGESTED = (
    "classDef ingested fill:#dddddd,stroke:#888,color:#000;"
)

# Mermaid arrow shape per derived rule. R1 (`shared_thread`) and R2
# (`shared_participant`) use Mermaid's built-in bold/dotted arrows; R3
# (`same_day_participant`) renders as a plain arrow whose color is
# overridden via a `linkStyle` directive emitted after the edge block.
_MERMAID_DERIVED_ARROWS: dict[str, str] = {
    "shared_thread": "==>",        # R1 — bold built-in
    "shared_participant": "-.->",  # R2 — dotted built-in
    "same_day_participant": "-->", # R3 — plain arrow, gray via linkStyle
}
# Rules that need a per-edge `linkStyle` line to override the default
# stroke. Currently only R3 — R1 and R2 carry their styling in the arrow
# shape itself.
_MERMAID_LINKSTYLE: dict[str, str] = {
    "same_day_participant": "stroke:gray",  # R3
}

# DOT per-edge attributes for derived rules. Stored as a tuple of raw
# `key=value` fragments that are appended to the per-edge attribute list
# alongside the existing `label="..."`. Color values that aren't bare
# Graphviz keywords (e.g. hex codes) are quoted so the parser doesn't
# choke; bare keywords (`bold`, `solid`, `gray`, `black`, `dotted`) stay
# unquoted to match the spec table verbatim.
_DOT_DERIVED_ATTRS: dict[str, tuple[str, ...]] = {
    "shared_thread": ("style=bold", "color=black"),         # R1
    "same_day_participant": ("style=solid", "color=gray"),  # R3
    "shared_participant": ("style=dotted", 'color="#cccccc"'),  # R2
}


def to_json(graph: GraphData) -> str:
    """Stable JSON: ``{"nodes": [...], "edges": [...]}``.

    Nodes are sorted by ``(title.lower(), id)`` — case-insensitive title
    primary, id tiebreaker. Edges are sorted by
    ``(src, dst, link_text, link_kind)`` so edges between the same pair
    of nodes still order consistently (a note with multiple ``[[refs]]``
    to the same target produces multiple deterministic rows).

    ``json.dumps`` is called with ``sort_keys=True`` so the per-object
    key order is also stable. ``ensure_ascii=False`` keeps non-ASCII
    titles readable instead of escaping them as ``\\uXXXX`` sequences;
    determinism is preserved because the input was already a Python
    string.

    Every edge carries ``rule``, ``weight``, ``evidence`` keys so the
    JSON shape is uniform across wiki and derived edges. Wiki edges have
    ``null`` for all three; derived edges carry the metadata-rule
    provenance pulled straight from :class:`GraphEdge`.
    """
    payload: dict[str, list[dict[str, Any]]] = {
        "nodes": [
            {
                "id": n.document_id,
                "title": n.title,
                "kind": n.kind,
            }
            for n in sorted(
                graph.nodes,
                key=lambda n: (n.title.lower(), n.document_id),
            )
        ],
        "edges": [
            {
                "src": e.src_document_id,
                "dst": e.dst_document_id,
                "kind": e.link_kind,
                "text": e.link_text,
                "display": e.display_text,
                "rule": e.rule,
                "weight": e.weight,
                "evidence": e.evidence,
            }
            for e in sorted(
                graph.edges,
                key=lambda e: (
                    e.src_document_id,
                    e.dst_document_id,
                    e.link_text,
                    e.link_kind,
                ),
            )
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)


def to_dot(graph: GraphData) -> str:
    """Graphviz DOT (digraph). Pipe to ``dot -Tsvg`` for visualization.

    Empty graph returns a valid (but empty-bodied) digraph — tools like
    ``dot`` accept it without warning.

    Conventions:

    - Node ids = first 8 chars of the document UUID with hyphens
      stripped; collision-safe via a 4-hex hash suffix.
    - Vault-tier nodes get ``fillcolor=lightblue``; ingested-tier nodes
      get ``fillcolor=lightgray``.
    - Embed edges (``link_kind='embed'``) get ``style=dashed``.
    - Derived edges pick up tier-specific ``style`` + ``color`` from
      :data:`_DOT_DERIVED_ATTRS` (see module docstring).
    - Labels: title truncated to 60 chars; all backslashes / quotes /
      newlines escaped per Graphviz quoted-string rules.
    """
    sorted_nodes = sorted(
        graph.nodes, key=lambda n: (n.title.lower(), n.document_id)
    )
    sorted_edges = sorted(
        graph.edges,
        key=lambda e: (
            e.src_document_id,
            e.dst_document_id,
            e.link_text,
            e.link_kind,
        ),
    )

    lines = [
        "digraph G {",
        "  rankdir=LR;",
        "  node [shape=box, style=\"rounded,filled\"];",
    ]
    if not sorted_nodes and not sorted_edges:
        lines.append("}")
        return "\n".join(lines) + "\n"

    id_map = _build_id_map(sorted_nodes)
    for n in sorted_nodes:
        node_id = id_map[n.document_id]
        label = _dot_escape(_truncate_label(n.title))
        fill = _DOT_FILL_VAULT if n.kind == "vault" else _DOT_FILL_INGESTED
        lines.append(
            f'  {node_id} [label="{label}", fillcolor="{fill}"];'
        )
    for e in sorted_edges:
        src = id_map.get(e.src_document_id)
        dst = id_map.get(e.dst_document_id)
        if src is None or dst is None:
            # Shouldn't happen — graph_data filters edges to nodes in the
            # same snapshot. Skip rather than emit a dangling edge.
            continue  # pragma: no cover
        label = _dot_escape(_truncate_label(_edge_label(e)))
        attrs = [f'label="{label}"']
        if e.link_kind == "embed":
            attrs.append('style="dashed"')
        elif e.link_kind == "derived" and e.rule in _DOT_DERIVED_ATTRS:
            attrs.extend(_DOT_DERIVED_ATTRS[e.rule])
        lines.append(f"  {src} -> {dst} [{', '.join(attrs)}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_mermaid(graph: GraphData) -> str:
    """Mermaid ``graph TD`` (top-down). Paste into any Mermaid renderer.

    Empty graph emits ``graph TD\\n`` — valid Mermaid syntax with no
    nodes or edges (the renderer shows an empty canvas).

    Conventions match :func:`to_dot`:

    - Node ids: alphanumeric + underscore (Mermaid syntax requirement),
      built from the UUID prefix + collision hash.
    - Vault-tier nodes get class ``vault``; ingested-tier get
      ``ingested``. Class definitions are emitted at the top so the
      output is one self-contained string.
    - Embed edges use ``-.->`` (dashed). Wiki edges use ``-->``.
    - Derived edges pick their arrow shape from
      :data:`_MERMAID_DERIVED_ARROWS` (R1 ``==>``, R2 ``-.->``, R3
      ``-->``). Rules listed in :data:`_MERMAID_LINKSTYLE` (R3) emit a
      trailing ``linkStyle <index> stroke:gray;`` directive targeting
      the edge by its 0-indexed position in the rendered output.
    - Labels: title truncated to 60 chars, with Mermaid-special chars
      escaped (``"`` → ``#quot;`` per Mermaid's HTML-entity convention).
    """
    sorted_nodes = sorted(
        graph.nodes, key=lambda n: (n.title.lower(), n.document_id)
    )
    sorted_edges = sorted(
        graph.edges,
        key=lambda e: (
            e.src_document_id,
            e.dst_document_id,
            e.link_text,
            e.link_kind,
        ),
    )

    if not sorted_nodes and not sorted_edges:
        return "graph TD\n"

    lines = ["graph TD"]
    # Class definitions kept out of the empty-graph branch — Mermaid
    # rejects standalone class definitions without nodes in some
    # renderer versions.
    lines.append(f"  {_MERMAID_CLASS_VAULT}")
    lines.append(f"  {_MERMAID_CLASS_INGESTED}")

    id_map = _build_id_map(sorted_nodes)
    for n in sorted_nodes:
        node_id = id_map[n.document_id]
        label = _mermaid_escape(_truncate_label(n.title))
        # ``id["label"]`` is Mermaid's "rectangle with text" form — works
        # for vault and ingested alike since we differentiate by class.
        lines.append(f'  {node_id}["{label}"]')
        cls = "vault" if n.kind == "vault" else "ingested"
        lines.append(f"  class {node_id} {cls};")
    # `linkStyle` directives must reference the 0-indexed position of an
    # edge in the rendered Mermaid output. We track the index as we emit
    # edges and accumulate directives in a sidecar list, then append them
    # after the edge block — keeping every edge line contiguous (some
    # Mermaid renderers stumble on linkStyle interleaved with edges).
    linkstyle_lines: list[str] = []
    edge_index = 0
    for e in sorted_edges:
        src = id_map.get(e.src_document_id)
        dst = id_map.get(e.dst_document_id)
        if src is None or dst is None:
            continue  # pragma: no cover
        arrow = _mermaid_arrow_for(e)
        label = _mermaid_escape(_truncate_label(_edge_label(e)))
        if label:
            lines.append(f'  {src} {arrow}|"{label}"| {dst}')
        else:
            lines.append(f"  {src} {arrow} {dst}")
        if e.link_kind == "derived" and e.rule in _MERMAID_LINKSTYLE:
            linkstyle_lines.append(
                f"  linkStyle {edge_index} {_MERMAID_LINKSTYLE[e.rule]};"
            )
        edge_index += 1
    lines.extend(linkstyle_lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers — kept private (formatter-internal). Stable across releases as long
# as the byte-for-byte output of the public functions is stable.
# ---------------------------------------------------------------------------


def _build_id_map(nodes: list[GraphNode]) -> dict[str, str]:
    """Map document_id → safe id string used by DOT/Mermaid.

    Strategy: first 8 chars of the UUID with hyphens stripped (UUIDs use
    only hex + hyphens, so the result is always alphanumeric). On
    collision (same 8-hex prefix shared by two docs) append a 4-char
    hash of the full id. Mermaid requires the prefix to be a letter or
    underscore — UUIDs with leading digits are common, so we always
    prepend ``n_``.
    """
    out: dict[str, str] = {}
    used: dict[str, str] = {}  # safe_id → document_id
    for node in nodes:
        candidate = "n_" + node.document_id.replace("-", "")[:_MERMAID_ID_PREFIX_LEN]
        if candidate in used and used[candidate] != node.document_id:
            suffix = hashlib.sha256(
                node.document_id.encode("utf-8")
            ).hexdigest()[:_MERMAID_HASH_SUFFIX_LEN]
            candidate = f"{candidate}_{suffix}"
        used[candidate] = node.document_id
        out[node.document_id] = candidate
    return out


def _truncate_label(text: str) -> str:
    """Shorten ``text`` to :data:`_LABEL_TRUNCATE_AT` chars with an ellipsis."""
    if len(text) <= _LABEL_TRUNCATE_AT:
        return text
    return text[: _LABEL_TRUNCATE_AT - 1] + _TRUNCATION_SUFFIX


def _mermaid_arrow_for(edge: GraphEdge) -> str:
    """Pick the Mermaid arrow shape for an edge.

    Embed edges keep the existing dashed arrow. Derived edges use the
    per-rule arrow from :data:`_MERMAID_DERIVED_ARROWS`. Wiki edges and
    any unknown derived rule fall through to the plain ``-->`` arrow —
    that way an unrecognized rule still renders as a valid Mermaid
    edge instead of crashing the formatter.
    """
    if edge.link_kind == "embed":
        return "-.->"
    if edge.link_kind == "derived" and edge.rule in _MERMAID_DERIVED_ARROWS:
        return _MERMAID_DERIVED_ARROWS[edge.rule]
    return "-->"


def _edge_label(edge: GraphEdge) -> str:
    """Pick the human label for an edge.

    Display text wins when set (``[[X|person-x]]`` shows "person-x"); otherwise
    the raw ``link_text`` ("[[person-x conversation]]"). Renderers usually
    want the same thing the user typed — ambiguity ("did they mean
    display or raw?") is resolved here once instead of every consumer.
    """
    return edge.display_text or edge.link_text


def _dot_escape(text: str) -> str:
    """Escape ``text`` for inclusion in a DOT quoted string.

    Per Graphviz: backslash escapes itself, double-quote, and newline.
    Other characters pass through. Length-preserving except for the
    inserted backslashes.
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _mermaid_escape(text: str) -> str:
    """Escape ``text`` for inclusion inside a double-quoted Mermaid label.

    Mermaid uses HTML entities for special chars inside ``"..."``:

    - ``"`` → ``#quot;``
    - ``#`` → ``#35;`` (so a literal ``#`` doesn't become an entity)

    Newlines are converted to ``<br/>`` (Mermaid's only multi-line
    convention inside labels). Backslashes are left alone — Mermaid
    doesn't process them as escapes inside quoted labels.
    """
    return (
        text.replace("#", "#35;")
        .replace('"', "#quot;")
        .replace("\n", "<br/>")
    )
