# Graph Diagnostic Workbench Design

## Goal

Make the Brain UI graph useful for finding relationships, missing links, and wrong links at a glance while preserving the existing graph views.

This is an additive redesign:

- Keep the sidebar local graph.
- Keep the current global graph.
- Keep the stock/dot-grid graph fallback.
- Add a separate diagnostic workbench button for link inspection.
- Improve readability and control layout across all existing graph views.

## Current Problems

The screenshots from May 4, 2026 show three practical usability issues:

1. The local graph is useful but cramped. Controls can clip at the panel edge, labels compete with nodes, and relationship type is too subtle.
2. The global graph becomes a dense hairball. Hubs are visible, but labels, weak edges, and lower-signal nodes fight for the same attention.
3. The graph does not clearly answer diagnostic questions: which nodes are missing backlinks, which links are suspicious, which links are authored versus derived, and which related notes are implied but not linked.

The current Quartz override already has useful foundation pieces: local/global graph modes, stock mode, search, tier/source chips, node sizing knobs, derived-edge styling, label truncation, hub label thresholds, and orphan toggles. The next pass should refine and compose those pieces rather than replace the renderer wholesale.

## Product Model

The graph should support four explicit views:

1. **Local Graph**: inline right-sidebar graph for the current page. This stays compact and reading-adjacent.
2. **Global Graph**: existing customized fullscreen graph for corpus exploration.
3. **Stock Graph**: existing plain/dot-grid fallback for uncustomized Quartz semantics.
4. **Diagnostic Workbench**: new fullscreen inspect mode for link quality and relationship auditing.

The diagnostic workbench is its own button. It does not replace the current global graph button.

## Diagnostic Workbench

The workbench opens in a fullscreen modal like the existing global/local fullscreen graph, but with a three-column layout:

- Left rail: relationship mode buttons.
- Center: graph canvas.
- Right inspector: selected-node details, metrics, issues, and suggested relations.

### Relationship Modes

The left rail should expose these modes:

- **Overview**: authored, derived, missing, and suspicious signals together.
- **Incoming**: backlinks into the selected node.
- **Outgoing**: links from the selected node.
- **Missing**: unresolved links, orphan candidates, and suggested backlinks.
- **Suspicious**: stale aliases, weak links, low-confidence derived links, and self/near-duplicate links when detectable.
- **Evidence**: derived relationships and the rule/weight metadata behind them.

Mode changes should rerender or restyle the graph without navigating away from the current page.

### Inspector

Selecting or hovering a node should populate a right-side inspector with:

- Title.
- Tier/source where known.
- Incoming count.
- Outgoing count.
- Derived relation count.
- Missing/unresolved count.
- Strong relations.
- Needs-review findings.

The inspector should be useful even before write actions exist. Initial actions can be read-only affordances such as `Preview`, `Open source`, `Inspect`, and `Hide`.

### Visual Encoding

Use visual signals that can be read at a glance:

- Authored/vault nodes: existing vault color.
- Ingested nodes: existing ingested/source color.
- Derived/evidence nodes or edges: softer yellow-green treatment.
- Missing/unresolved links: yellow node/edge/badge.
- Suspicious links: red node/edge/badge.
- Current/focused node: explicit outline or halo.
- Active neighborhood: brighter nodes and stronger edges.
- Non-active context: dimmed labels and edges.

Edges should distinguish authored wiki links from derived links:

- Authored links: solid, low-to-medium alpha.
- Derived links: dashed, lower alpha, with rule/weight surfaced in inspector.
- Suspicious links: red solid or red dashed depending on cause.
- Missing suggested relation: yellow dashed.

## Existing View Readability Pass

### Sidebar Local Graph

Keep the existing local graph in the right sidebar, but fix readability:

- Prevent top controls from clipping horizontally.
- Keep button affordances reachable and visually separated.
- Give the canvas stable height after controls render.
- Use shorter, clearer controls in the sidebar than in fullscreen views.
- Keep labels readable for the current node and major neighbors.
- Dim leaf labels by default and reveal them on hover.
- Keep the current page visible even if filters would otherwise orphan it.

### Current Global Graph

Keep the current customized global graph and improve its display:

- Increase readable separation between hubs and leaves.
- Keep hub labels visible without turning the full graph into wall-of-text.
- Lower default edge opacity in dense regions.
- Make hover focus more dramatic: selected node, neighbors, and incident edges brighten while unrelated context dims.
- Keep chips/search visible but move toward a denser toolbar treatment so they do not steal canvas height.
- Keep `Show unconnected` available, but default dense global mode should hide unconnected nodes.

### Stock Graph

Keep the stock/dot-grid fallback as a simple escape hatch:

- Do not apply diagnostic colors, chips, or inspector.
- Fix only obvious modal readability issues: centering, contrast, canvas sizing, and button placement.

## Data Requirements

The first implementation should use data already available in the Quartz content index where possible:

- `title`
- `links`
- `linkRecords`
- `tier`
- `source`
- `date`
- derived-link `kind`
- derived-link `rule`
- derived-link `weight`

To support missing and suspicious diagnostics, the renderer may need additional additive fields in `contentIndex.json`:

- unresolved outgoing link text/target where available from rendered content or a future emitter extension
- per-node incoming/outgoing counts
- optional aliases or canonical path hints if already present in frontmatter

If those fields are not available in the first implementation, the workbench should still ship with authored/derived/source diagnostics and reserve unresolved/suspicious sections for available data.

## Implementation Shape

Expected files to touch:

- `quartz_overrides/quartz/components/Graph.tsx`
- `quartz_overrides/quartz/components/scripts/graph.inline.ts`
- `quartz_overrides/quartz/styles/graph.scss`
- `quartz_overrides/quartz.layout.ts`
- `quartz_overrides/quartz/plugins/emitters/contentIndex.ts` if additional diagnostic fields are needed
- focused tests under `tests/` for overlay parse checks and content-index additions

The workbench should reuse the existing Pixi/d3 renderer where practical. The likely approach is to add a new modal container and a workbench render mode rather than building an unrelated graph renderer.

## Testing

Automated tests should cover:

- Overlay files still parse under the existing smoke tests.
- New Graph config keys are present in the overlay templates.
- Any new `contentIndex` fields are emitted additively and do not replace stock-compatible fields.
- Missing/suspicious diagnostic helpers, if implemented as pure functions, have focused unit tests.

Manual verification should include:

- `brain vault render --print-overlay`
- `brain vault render`
- browser smoke of sidebar local graph, global graph, stock graph, and diagnostic workbench
- desktop and narrower viewport checks for control clipping and modal layout

Full repository verification before completion remains:

```bash
ruff check
mypy src/
pytest
```

## Out Of Scope

- Writing graph fixes back into Markdown files.
- Auto-creating links from the workbench.
- Replacing Quartz with a different graph library.
- Removing or changing the existing graph buttons' meanings.
- Committing or pushing without explicit user permission.
