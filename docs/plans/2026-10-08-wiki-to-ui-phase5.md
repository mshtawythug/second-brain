# Wiki → `brain ui` consolidation — phase 5 (sidebar graph + related-docs panel) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

*Written 2026-10-08 against `master` @ `121df23`. Contract:
`docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` §6, §11 (phase-5 row), §12 Q2/Q3,
Appendix B-4 and B-17. Branch: `feat/wiki-to-ui-phase5`. Every count below is SHA-bound or
replaced by the command that derives it.*

## 0. What phase 5 is, in one paragraph

Two reading-surface affordances that the wiki had and `brain ui` does not: a **sidebar local
graph** of the open note's link neighbourhood (depth 1, click-to-navigate, hover-highlight —
Q3's subset, nothing more), and a **related-docs panel** over HTTP backed by the
`compute_related` function that has sat in `src/brain/related.py` with no consumer since
phase 2 (B-17). Exit criterion from §11: *reads materialized state only; honest degraded state
when unbuilt; the related-docs panel answers over HTTP, or the row is not exited.*

## 1. Rulings this plan makes (the spec left them open)

| # | Question | Ruling | Why |
|---|---|---|---|
| **R1** | **B-4:** where does the related route get `vector_sim_floor`? | `ctx.cfg.vector_sim_floor` — the same `Config` value runtime `brain search` reads. No new knob, no literal, no default. | The missing default exists to stop the panel diverging from search without anyone noticing; reading the one shared value is the only answer that cannot diverge. A test pins it: with `cfg.vector_sim_floor` set to a sentinel, the route's `compute_related` call must receive that sentinel (mutation: hard-code `0.25` in the route → red). |
| **R2** | **Q2:** render primitive | Geometry computed **server-side** in Python (`ui/graph_layout.py`, pure, deterministic radial layout). The client builds the `<svg>` with `document.createElementNS` from the JSON — **no layout code in JS, no vendored library, no `innerHTML`**. | The spec's (d) said "Python emits SVG". The client enforces a single-`innerHTML` rule (`tests/test_ui_static_behaviour.py`) and builds every node with `el()`/`textContent`; injecting a server SVG string would break that rule or need `DOMParser`, which is the same class of risk. Server-side geometry + client-side element creation keeps everything (d) was protecting — zero dependency, zero network, zero force simulation — and keeps the client's rule. Recorded as a deviation from the letter, not the intent. |
| **R3** | **Q3:** feature subset | Sidebar local graph, depth 1, click-to-navigate, hover-highlight. Nothing else: no zoom, no drag, no fullscreen, no filters, no search, no recency sizing. | Q3's default, verbatim. The fullscreen global graph is "do not build unless reached for" and nobody has. |
| **R4** | Neighbour cap | At most **24** neighbours drawn, sorted by `(title.lower(), id)`; the rest reported as `truncated: K`. The full list is already one click away in the links rail. *(Corrected 2026-10-08 in the phase-5 fix round: false. The links rail is the marginalia's "Linked from" list — backlinks only — so an outgoing-only or derived-only neighbour past the cap is on no rail. The client's caption says "+N more not shown" for exactly that reason. See §7.4.)* | A People Hub page has hundreds of edges; a 300-node ring is unreadable and the rail exists. 24 is the largest count at which 12-px labels on a 320-px ring do not collide at depth 1 (re-derive if the inspector width changes). *(Both claims false — the rail holds backlinks only, and labels can collide from 5 neighbours on, far below 24; see §7.4.)* |
| **R5** | "Unbuilt" for the **link** graph | The route reports `corpus_linked: bool` (any row in `links` or `derived_links`). An isolated note in a linked corpus draws nothing. A note in a corpus with **no** links at all draws a one-line notice naming `brain vault sync`. | §6.4's degraded-state rule, applied to the document-link graph rather than the GraphRAG graph. "Nothing" is honest for an isolated note; a notice is honest for a vault that has never been synced; an error or an empty canvas is neither. |
| **R6** | Confidentiality gate on the related panel | `exclude_confidential = not (ctx.serve_confidential_titles and ctx.serve_confidential_bodies)` — strict unless BOTH lenses are permissive. The graph route gates on `serve_confidential_titles` alone (titles only, like the links rail). | `RelatedDoc.snippet` is a slice of chunk **content**: body egress. The links rail and the graph carry titles only. Each route takes the strictest lens that covers what it carries. |
| **R7** | Snippet length in the panel | Cap at `BRAIN_SNIPPET_MAX_CHARS` (`cfg.snippet_max_chars`, default 1600) **server-side**, same knob search uses. | One cap for every snippet surface; the ceiling rule says refused-or-trimmed-with-a-flag, never a silent cut — emit `snippet_truncated: bool`. |
| **R8** | §6.5 binding rule | Neither route imports anything from `brain.maintenance`, `brain.graph_rag`, or `brain.wiki`; a test asserts the import graph. | No graph surface may trigger a build. |

## 2. API contracts (fixed here so client and server tasks can run in parallel)

### `GET /api/notes/{id_prefix}/graph`

Resolves the prefix exactly as `/api/notes/{id}/links` does (`notes_service.resolve_id`: same 400/404,
same 503 on DB failure). Calls `vault.graph.graph_data(conn, root=document_id, depth=1,
include_ingested=True, include_derived=True, exclude_confidential=strict)` with
`strict = not ctx.serve_confidential_titles`, then `graph_layout.layout(graph, root=document_id, cap=24)`.

```json
{
  "id": "<root uuid>",
  "width": 320, "height": 320,
  "nodes": [
    {"id": "<uuid>", "title": "…", "kind": "vault|ingested", "x": 160.0, "y": 160.0, "r": 9, "root": true},
    {"id": "<uuid>", "title": "…", "kind": "vault",          "x":  42.1, "y":  98.7, "r": 6, "root": false}
  ],
  "edges": [{"src": "<uuid>", "dst": "<uuid>", "kind": "wiki|derived"}],
  "truncated": 0,
  "corpus_linked": true
}
```

- Root is always `nodes[0]`, at the centre. Neighbours are on one ring, evenly spaced, in
  `(title.lower(), id)` order starting at 12 o'clock, so the picture is stable across reloads.
- Edges are only those with both endpoints drawn; each carries `kind` so derived edges can be dashed.
- Titles are plain strings; the client sets them with `textContent`. Nothing in this payload is HTML.
- `corpus_linked` is one `EXISTS` over `links` UNION `derived_links` — a single cheap query via
  `brain.ui.queries` (the only module allowed SQL).

### `GET /api/notes/{id_prefix}/related`

Same resolve/503 contract. Calls
`compute_related(conn, document_id, limit=DEFAULT_RELATED_LIMIT, vector_sim_floor=ctx.cfg.vector_sim_floor, exclude_confidential=strict)`
with `strict` per R6.

```json
{
  "id": "<root uuid>",
  "related": [
    {"id": "<uuid>", "title": "…", "vault_path": "notes/x.md", "source": "manual",
     "score": 0.0312, "snippet": "…", "snippet_truncated": false}
  ],
  "count": 1,
  "vector_sim_floor": 0.25
}
```

- `vector_sim_floor` is echoed so a reader (and a test) can see which floor produced the list.
- `score` is comparable within the list only (the dataclass says so); the client does not display it.
- Empty `related` is a normal answer, not an error: documents with no embedded chunks lose the
  vector leg and may legitimately have nothing above the floor.

## 3. Ordered task list

Waves are parallel inside, serial between. Each task names its files; **a task touches nothing
outside its list**. Shared files (`app.py`, `main.js`, `index.html`, the two rosters) have one owner
per wave.

### Wave 1 — server + graph client (three teammates, three worktrees, three sibling test DBs)

**T1 — graph layout + graph route (server).**
Files: `src/brain/ui/graph_layout.py` (NEW), `src/brain/ui/routes_graph.py` (NEW),
`src/brain/ui/queries.py` (+ `corpus_has_links(conn) -> bool`), `src/brain/ui/app.py` (+1 `Route`
line after the links route), `tests/test_ui_graph_layout.py` (NEW), `tests/test_ui_routes_graph.py` (NEW).
- `graph_layout.layout(graph: GraphData, *, root: str, cap: int = 24, size: int = 320) -> LayoutResult`
  is pure: no I/O, no randomness. Frozen dataclasses for the result. Root first; neighbours sorted;
  ring radius and node radius are named constants with the reason in a comment.
- Route tests seed documents and `links`/`derived_links` rows with direct SQL, the pattern
  `tests/test_ui_routes_links.py` established, and mount the route onto the real app. Cases: root
  only (isolated note, `corpus_linked` true); isolated note in an **empty** corpus
  (`corpus_linked` false); three neighbours in title order at the expected angles; the 25th
  neighbour becomes `truncated: 1`; a derived edge carries `kind: "derived"`; a confidential
  neighbour is absent with `serve_confidential_titles=False` and present with it `True`, and its
  edge is absent too; unknown prefix 404; ambiguous prefix 400; DB failure 503 leaking no SQL.
- R8 test: `routes_graph` imports nothing from `brain.maintenance`, `brain.graph_rag`, `brain.wiki`.

**T2 — related route (server).**
Files: `src/brain/ui/routes_related.py` (NEW), `src/brain/ui/app.py` (+1 `Route` line after T1's —
coordinate the anchor: T2 inserts after the `/links` route, T1 after `/related`; the integrator
resolves if both land on the same line), `tests/test_ui_routes_related.py` (NEW).
- Tests seed two documents with chunks + synthetic embeddings via the fake embedder fixture so
  the vector leg has something to rank; cases: the related list is non-empty and sorted by score
  desc; `vector_sim_floor` in the payload equals `cfg.vector_sim_floor` and `compute_related`
  received it (R1 — monkeypatch `compute_related` with a recording fake; **this is `monkeypatch`,
  not monkey-patching**); snippet longer than `cfg.snippet_max_chars` is cut and flagged
  (R7); a confidential candidate is absent under either withheld lens and present only when both
  are permissive (R6); draft candidates never appear; unknown/ambiguous/503 as above; empty
  corpus returns `related: []` with 200.
- R8 import test for `routes_related`.

**T3 — graph client (browser).**
Files: `src/brain/ui/static/js/graph.js` (NEW), `src/brain/ui/static/css/graph.css` (NEW),
`src/brain/ui/static/js/main.js` (+ import + `wireGraph()` call **after** `wireMarginalia()`),
`src/brain/ui/static/index.html` (+1 `<link>` after `marginalia.css`),
`tests/test_ui_static_behaviour.py` (`CSS_ORDER`, `JS_ORDER`, `_INSPECTOR_APPENDS`),
`tests/test_ui_browser_graph.py` (NEW).
- Same shape as `marginalia.js`: idempotent `wireGraph()`, `subscribe(renderGraph)`, per-note
  cache with failure cached as `null`, stale-response guard on `state.note.id`, draws nothing when
  `!note || state.editing || note.withheld`, appends its own `<figure class="local-graph">` to
  `#inspector` **last** (the `1fr`-track contract in marginalia.js's header applies verbatim).
- SVG built with `createElementNS`; every title via `textContent`; each neighbour is an
  `<a href="?id=…">` wrapping the circle + label with `data-note-id`, `click` → `openNote(id)` via
  the same dynamic import marginalia uses (the store ↔ inspector cycle); `mouseenter`/`mouseleave`
  and `focus`/`blur` toggle `data-hover` on the node and its edge (hover-highlight, keyboard-reachable).
- Degraded state per R5: one `<p class="graph-empty">` naming `brain vault sync` when
  `corpus_linked` is false; nothing at all for an isolated note in a linked corpus.
- Browser tests (hermetic, API stubbed at the network layer like `tests/test_ui_browser.py`):
  the figure appears after the note paints; root at centre; N neighbours → N anchors in title
  order; clicking a neighbour opens it (stubbed `/api/notes/{id}` is requested); hovering sets
  `data-hover` on exactly that node and its edge; `truncated` renders "+K more" text; the
  empty-corpus notice; the figure is absent while editing; the figure is the **last** child of
  `#inspector` after a dispatch; no `innerHTML` in the module (source guard).

### Wave 2 — related client (one teammate, after wave 1 lands on the branch)

**T4 — related panel (browser).**
Files: `src/brain/ui/static/js/related.js` (NEW), `src/brain/ui/static/css/related.css` (NEW),
`main.js` (+ import + `wireRelated()` after `wireGraph()`), `index.html` (+1 `<link>`), the two
rosters + `_INSPECTOR_APPENDS`, `tests/test_ui_browser_related.py` (NEW).
- Same contract as T3; `<nav class="related-rail" aria-label="Related notes">` with `<h2>`, a list
  of `<a>` titles each with a `<p class="related-snippet">` via `textContent`; appended **after**
  the graph figure (append order: marginalia → graph → related; `_INSPECTOR_APPENDS` pins it).
- Draws nothing for `related: []` (not an empty rail — no rail).
- Browser tests mirror T3's: appears, ordered, click navigates, absent while editing, absent
  when the note is withheld, snippet is text (a `<b>` in the stub snippet renders as literal text),
  last-child ordering, no `innerHTML`.

### Wave 3 — closeout (one teammate)

**T5 — docs + spec + memory.**
Files: the spec (`§11` phase-5 row → exited, with the R2 deviation noted; **B-4** → closed by R1;
**B-17** → consumer now exists, the defensive note stays as history), `README.md` ("Local web UI"
— two sentences), `CLAUDE.md` only if a ceiling row is implicated (re-derive with `wc -l`; expected:
none — all new modules are new and under 800; `app.py` grows by two lines), the auto-memory
`project_wiki_to_ui_consolidation_status.md` (phase 5 → complete) and `cli.md` if any CLI surface
changed (expected: none). This plan gets a closeout section.

### Then: the rule-14 loop

Reviewer (`superpowers:code-reviewer` as a teammate) over the whole branch diff against this plan
and the spec; completion auditor over (a)–(e) of CLAUDE.md rule 14, with the five-clause mutation
harness on every new guard. Repeat until "APPROVED" and "AUDIT PASSED". Every reviewer/auditor
dispatch carries the no-destructive-DB-ops rule and the no-PII rule.

## 4. Guards that must be proven able to fail (five-clause harness each)

| Guard | Mutation that must redden it |
|---|---|
| R1 floor plumbing | route passes literal `0.25` instead of `ctx.cfg.vector_sim_floor` |
| R6 related gate | route passes `exclude_confidential=False` unconditionally |
| graph confidential gate | route passes `exclude_confidential=False` |
| graph edge gating | drop the edge filter so an edge to a withheld node ships |
| R4 cap | `cap=24` → `cap=25` |
| layout determinism | sort key `(title.lower(), id)` → `(id,)` |
| R5 notice | client draws the notice when `corpus_linked` is true |
| append-order contract | call `wireGraph()` before `wireMarginalia()` *(SUPERSEDED — this mutation can no longer redden anything: since `d5a8a93`, `placeInspectorBlock` fixes the order whatever the wiring order. Replaced by the six-arrival-order test in `tests/test_ui_browser_inspector_blocks.py`, which reddens when `INSPECTOR_BLOCKS` is reordered — see §7.2.)* |
| R8 import rule | add `import brain.maintenance` to a route module |
| no-innerHTML | add one `innerHTML =` to `graph.js` |

## 5. Environment and guardrails (unchanged from the previous phases; restated because they bit)

- Worktrees per teammate, **own venv per worktree** (`pip install -e ".[dev]"` from inside it; the
  interpreter is `~/.local/share/uv/python/cpython-3.11.15-*/bin/python3.11`), **own sibling test DB**
  per teammate (`second_brain_t_evalb`, `second_brain_t_evalc`, `second_brain_test_co`; names ≤22
  chars; **never `DROP DATABASE`**). Every teammate command `cd`s explicitly — spawned agents
  inherit the parent's cwd.
- AGE test Postgres on 5434 must be up: `docker compose -f docker-compose.age-test.yml up -d`.
- Prod Postgres 55432 is read-only for everyone. No `pkill` by pattern.
- Browser tests need the `browser` extra and a cached chromium; run them by path:
  `pytest tests/test_ui_browser_graph.py -m browser --no-cov`.
- Read exit codes from files, never from a pipe.
- **No PII** in fixtures, titles, snippets, commit messages. Synthetic UUIDs in the `aaaaaaaa-…`
  style.
- **No commit without the user's go**; teammates leave their work committed on their worktree
  branch only if told so by the coordinator; the coordinator integrates and the user decides the push.

## 6. Done means

1. Both routes answer over HTTP with the contracts in §2, gated per R6, floor per R1.
2. Opening a note in `brain ui` shows its local graph (or the honest nothing/notice) and its related
   notes; clicking either navigates; hover highlights.
3. Every guard in §4 reddens under its mutation and is restored byte-identically. *(Except the superseded append-order row, whose mutation no longer applies; its replacement guard is named in §7.2.)*
4. `ruff check` 0, `mypy src/` 0, full `pytest` green, `pytest tests/test_ui_browser*.py -m browser --no-cov` green.
5. The spec's §11 phase-5 row is marked exited and B-4 closed, in the same PR.
6. Rule-14 loop exited clean.

## 7. Closeout

*Written 2026-10-08 at `d04d540`. Every SHA below is a frozen commit; every count is either bound
to one or replaced by the command that derives it. For the present size of any file, run `wc -l`.*

### 7.1 What shipped, per task

| Task | Final commit | What landed |
|---|---|---|
| **T1** graph route | `df9742f` (merged by `1708d72`) | `GET /api/notes/{id}/graph` in `src/brain/ui/routes_graph.py` over a pure layout in `src/brain/ui/graph_layout.py`: depth-1 neighbourhood, geometry computed server-side (root centred on a 320 canvas, neighbours on a ring of radius 110 from 12 o'clock clockwise, at most 24 drawn, the rest reported as `truncated`), plus `corpus_linked`. |
| **T2** related route | `cc28ff9` (merged by `ea26a12`) | `GET /api/notes/{id}/related` in `src/brain/ui/routes_related.py` — the first consumer of `brain.related.compute_related`. Floor per R1, lens per R6, snippet cap per R7. |
| **T3** graph client | `eff393f` (merged by `552746b`) | Sidebar local graph: `static/js/graph.js` + `static/css/graph.css`, SVG built with `createElementNS`; browser suite split and gated in CI. |
| integration | `d0e63d7` | Two related-route tests tightened, carried over from review. |
| **T4** related client + inspector | `d04d540` (parts A–C: `a3e9ed8`, `d5a8a93`, `ddb177f`) | Related-notes rail (`static/js/related.js` + `static/css/related.css`); one canonical inspector block order; refresh of all three blocks after a save; one shared per-note fetch-and-cache helper. |
| **T5** closeout | the commit that added this section | Docs and comments only — see §7.6. |

The full file list is `git diff --stat 121df23..d04d540 -- src`.

### 7.2 Rulings made during execution (beyond §1)

- **A confidential root is withheld on both routes.** Under a strict lens the graph route answers
  403 `graph_withheld` and the related route 403 `related_withheld`, each carrying no title and no
  candidate. The related route checks the root's tier **before** ranking: `compute_related` gates
  candidates only, and a related list for a withheld note is a précis of what is withheld.
- **Root vanished mid-request → 404 `note_not_found`** on the graph route, naming nothing, rather
  than surfacing the layout's `ValueError` as a 500.
- **One shared sensitivity read.** Both routes read the root's tier through
  `brain.ui.queries.document_sensitivity`, so the two gates cannot drift.
- **Crowded-ring threshold.** Above `MAX_LABELLED_NEIGHBOURS` in `static/js/graph.js` (4)
  neighbour labels show only on hover or focus. *(Corrected 2026-10-09 — see §7.4.)* The threshold
  no longer keeps labels apart: `fitLabels` does, at every count and in every font, by measuring
  each label after drawing and cutting it to a slot derived from geometry alone (inside the
  inspector's edge; clear of any label sharing its row). The threshold decides when those slots are
  too narrow to be worth reading: up to 4 neighbours, labels share a row only in pairs; at 5 the 2
  and 10 o'clock labels share the root label's row and each is cut to about half a 28-char title.
  Rationale and pinning tests are in the comment above the constant.
- **Canonical inspector block order:** marginalia → graph → related, held in one place
  (`INSPECTOR_BLOCKS` / `placeInspectorBlock` in `static/js/dom.js`), so the order no longer
  depends on which fetch resolves first.
- **Refresh after save.** A save bumps a per-note revision in `static/js/store.js`; the three blocks
  key their caches on `id:rev` and refetch.
- **Shared fetch helper.** `perNoteFetch` in `static/js/note_fetch.js` is the one fetch-and-cache
  path the inspector blocks use.
- **The append-order guard was replaced, not proven.** §4's "call `wireGraph()` before
  `wireMarginalia()`" mutation stopped meaning anything once `placeInspectorBlock` took over the
  order (`d5a8a93`): the blocks land in canonical order whichever renderer is wired first. The guard
  that replaced it is the six-arrival-order test in `tests/test_ui_browser_inspector_blocks.py`,
  verified by the phase audit to redden when `INSPECTOR_BLOCKS` is reordered. The `_INSPECTOR_APPENDS`
  pinning that §3 planned for the graph and the related rail was not needed: that roster in
  `tests/test_ui_static_behaviour.py` covers `inspector.js`'s own appends only, and the two blocks go
  in through `placeInspectorBlock`, not through an append.
- **The client mirrors both routes' confidential gates (phase-5 fix round).** The closeout text
  above said each route's 403 is for a note the client never asks about. That was false under the
  default lenses (bodies served, titles not): the note route withholds on the BODIES lens, so a
  confidential note opened in full, while the graph route refuses on the TITLES lens and the related
  route unless titles AND bodies are served — two blank blocks and two console errors, no leak. The
  server stays exactly as strict. The client now reads both lenses off `/api/health` and the note's
  own `sensitivity` off its payload (`store.js`: `isConfidentialNote`, `servesConfidential`, failing
  closed until health answers), mirrors each route in one named predicate (`graphRefusedHere` in
  `graph.js`, `relatedRefusedHere` in `related.js`), does not issue the request, and shows a one-line
  notice in the block's slot (`p.graph-withheld` inside `figure.local-graph`, `p.related-withheld`
  inside `section.related-rail`). A withheld note still draws nothing in either slot. Pinned by the
  default-lens and lens-matrix tests in `tests/test_ui_browser_graph.py` and
  `tests/test_ui_browser_related.py`.

### 7.3 Graph-route latency, and when to revisit

Measured end to end on a **synthetic** corpus of 1,400 documents, 10,000 links and 3,000 derived
links (the basis; not the production corpus): **median 174 ms, p95 269 ms**, of which
`vault.graph.graph_data`'s whole-corpus read is about 88%. **Kept as is**, because the panel is lazy
— it paints after the note, so the note itself is never waiting on it. **Revisit if p95 exceeds
500 ms**, which at linear growth is roughly 2.5× that corpus. The remedy then is a rooted depth-1
read in `vault/graph.py` rather than the whole-corpus read the route filters today.

### 7.4 A plan claim that turned out false

**R4 said 24 is the largest count at which labels on the ring do not collide. It is not.** At the
cap neighbours sit about 29 units apart along the arc, but labels are horizontal and many times
wider than that, so on a ring of more than 4 neighbours they can collide. The cap of 24 stands
(it bounds the drawing); the label problem is handled by the crowded-ring rule in §7.2.

*(Corrected in the phase-5 fix round. This paragraph also said "the links rail holds the full
list". It does not: the only rail is the marginalia's backlinks ("Linked from"), so an outgoing-only
or derived-only neighbour past the cap is on no rail at all — which is why the graph's caption reads
"+N more not shown" and does not point at the rail. R4's own cell carries the same correction.)*

**The threshold's "measured, collision-free" was true on one font only.** §7.2 first said 4 was the
largest count at which every smaller count is collision-free, "measured". It was measured at 320,
400 and 1280 px — all on macOS, whose default sans is narrow. On GitHub's Linux runner the default
sans is DejaVu Sans, roughly 8% wider, and five layout tests failed there and nowhere else: a
3-neighbour ring's two lower labels overlapped, and rim labels ran past a 400 px inspector, where
`overflow-y: auto` clips them mid-word. That was a production defect for any reader with a wider
font, not a test artefact. Fixed 2026-10-09 by `fitLabels` in `static/js/graph.js` (labels are cut
to a measured, geometry-derived slot, and refitted when the inspector or the drawing is resized).
The layout suite now runs its four parametrized geometric tests at the platform font AND at a
deliberately wide face, and four refit tests (now in `tests/test_ui_browser_graph_refit.py`) run at
the wide face only, so a macOS run catches this class of defect too.

**Box checks alone cannot see an OVER-cut label, so every geometric test also applies an
optimality oracle.** *(Added 2026-10-09, after the completion audit of `8aef7be` failed on it.)*
Dropping the `Math.abs` from the row bound turned the left label of every shared-row pair into a
bare "…", and dropping the clearance from the row bound put a rim pair 1.36 units apart, inside
the 3-unit halo; both stayed 188/188 green, because a label cut too short sits inside every box.
The oracle (`_assert_optimal_cuts`, `tests/ui_graph_geometry.py`) recomputes each label's slot in
the test from the specified rules — the edge bound from the inspector's padding box, and on a
non-crowded ring the row bound from every label within the clearance of its row, at `|dx|` less
the clearance — and requires the label to be the LONGEST cut of its title that fits: one more
character must not, and a title whose capped form fits must be shown whole. It is measured on the
same element with the same `getComputedTextLength` as `fitText`. It cannot cancel a mutation: it
reads nothing from `graph.js`, and it holds the spec's clearance (4) and cap (28) itself, checking
that `graph.js` ships them only after the geometry. It runs in the rim, overlap and every-ring
tests at both faces and every width, and with no row bound in the crowded-ring test, on every
label. Three platform-font tests place their nodes so the outcome cannot depend on the font, and
measure that as preconditions: (15i) a title of `i` beside each edge of an inspector with side
borders, so one `i` is narrower than the clearance; (15j) two such titles on one row, level and
stacked half a clearance apart vertically; and (15k) a slot placed between a long title's 27- and
28-character cuts.

Measured on macOS (all 40 tests in the two modules; each mutation applied, confirmed applied,
and reverted to a clean `git diff`):

| Mutation of `graph.js` | Red | Where |
|---|---|---|
| `Math.abs` dropped from the row bound | 21 | all at the oracle: rim, overlap and every-ring tests at every width and face, (15i), both (15j) |
| row bound's `- LABEL_CLEARANCE` dropped | 8 | the oracle in seven (rim and overlap at 1280 wide, every-ring at all three platform widths and 1280 wide, (15j) stacked); (15j) level at the halo check first |
| row membership tested without the clearance | 1 | (15j) stacked, at the oracle |
| `LABEL_CLEARANCE` 4 → 0 | 34 | rim and overlap 320/400 wide at the halo check; rim and overlap 1280 wide, five crowded runs, (15j) stacked and (15k) at the oracle; (15i) and (15j) level at the halo check; the other 19 only at the constant check: in them no label's cut differs from the spec's |
| `clientWidth` → `offsetWidth` | 1 | (15i), at its spill check under the right border |
| `+ host.clientLeft` dropped | 1 | (15i), at its spill check under the left border |
| `fitText`'s search capped at 26 (`hi - 2`) | 1 | (15k), at the oracle |
| row bound applied on a crowded ring | 12 | every crowded run, at the oracle |

With the earlier spill and halo assertions removed, the oracle alone turns (15i) red for
`offsetWidth` and for the dropped `clientLeft`, and both (15j) runs red for the row clearance.

**`fitLabels` disabled outright: 25 red.** 13 of the 15 parametrized wide runs:
the rim test's 320 and 400 runs clipped, its 1280 run on the halo (there the rim labels never
reach the inspector's edge, so nothing clips; its two rim labels overlap); all three overlap runs
and all three every-ring runs on overlapping labels; and four crowded runs, two clipped (320 and
400 at threshold+1) and two at the oracle (320 and 400 at the cap). Four platform runs, all at
the oracle: the every-ring test at all three widths and the crowded test at 400 at the cap. Of
the four wide-only refit tests, (15d) and (15e) go red on clipped labels, while (15f) and (15g)
go red at a PRECONDITION, not at geometry: with no fit, the resize changes no label's cut, so
their guard that the test tested something fires first. And (15i) on labels under the inspector's
border, (15j) level on overlapping labels, (15j) stacked and (15k) at the oracle. Mutating
`fitText`'s slot to `Infinity` instead turns the same 25 red. **The two wide runs that stay green
are a named exemption, not an omission:** the crowded-ring test's 1280 wide runs. A crowded ring
shows no two labels together, so only the inspector's edge binds, and at 1280 a rim label's slot
is ~540 viewBox units against ~250 for the widest 28-character label the wide face draws. No
title reaches it, so those two runs are copies of their platform runs and do not assert the cut.
The 320 and 400 crowded runs do.

Three survivors remain, all equivalent: `<=` → `<` in `fitText`'s early return, in its binary
search, and in the row-membership test. Each differs only when a measured length equals a slot,
or a vertical gap equals the clearance, exactly.

The crowded-ring test pins the TEXT of every label, not only the box of the one in hand. On a
crowded ring `fitLabels` skips the row bound, since no two labels show together. Applied anyway,
a hidden neighbour's box cut the hovered label beside 12 o'clock on the 24 ring to "S…", while
every box check stayed green. The clearance `fitLabels` keeps (`LABEL_CLEARANCE`) is also checked
against the 3-unit halo graph.css strokes round each label, read from the computed style, in the
rim and overlap tests and in (15i) and (15j).

A draw into a HIDDEN inspector (the phone list view sets it `display: none`) is not fitted at all.
Chromium returns a non-null identity `getScreenCTM()` for a `display: none` svg, so the first
guard, which tested the CTM, passed. The all-zero inspector rect then cut every label, root
included, to a bare "…". The ResizeObserver refitted the labels when the inspector was shown
again, so no reader saw it, but the draw-time fit was wrong all the same. `fitLabels` now returns
when the svg has no width. That is the one guard, since an svg with a box sits in an inspector
with a box (regression test (15e), in `tests/test_ui_browser_graph_refit.py` with the other refit
tests). The refit observes the inspector as well as the svg: the svg is capped at 24rem, so on a
wide desktop only its percentage gutter makes its box follow the inspector's. Each observation
has its own test, (15f) and (15g). (15h) pins that every draw lets go of the svg it replaced, and
that a withdrawn svg can be garbage-collected: graph.js clears its own reference (`fitted`) on
every disconnect, and a forced collection clears a WeakRef to the svg. ResizeObserver is assumed
rather than feature-tested, following the unguarded `??` in `inspector.js` and `related.js`: any
browser that can parse those modules has it.

The false legibility claim was repeated in THREE places, not one: `graph_layout.py`'s `RING_RADIUS`
comment (corrected in the closeout commit), the same module's `DEFAULT_CAP` comment, and
the name and comment of a test in `tests/test_ui_graph_layout.py` (both corrected in the fix round —
§7.6).

### 7.5 Known limits — recorded, not fixed

- **Touch screens** cannot preview a crowded ring's labels before tapping (no hover). Follow-up
  idea: a plain neighbour list on coarse pointers.
- **Saving X with a new link to Y does not refresh Y's blocks** until reload; only the saved note
  is invalidated, by design.
- **Move, rename and draft toggle do not bump the revision**, so the blocks are not refetched by
  those actions.
- **Cached rows that point at a deleted note persist until reload** — pre-existing for backlinks,
  now shared by the graph and related blocks.
- **Superseded `id:rev` cache keys are retained deliberately** (reason in `store.js`).
- **At a 400 px viewport** rim labels clear the inspector edge by only 5–6 px on macOS. *(Bounded
  since 2026-10-09: `fitLabels` cuts each label, down to a bare ellipsis, until it sits at least
  `LABEL_CLEARANCE` (4 viewBox units) inside the inspector in any font. Only a slot narrower than
  "…" itself can still overflow, because the bare ellipsis is the floor (`fitText`'s comment). So
  the limit is now how many characters a wide font leaves on a rim label, not whether it is
  clipped.)*

### 7.6 Documents corrected in the closeout

- `src/brain/related.py` — `compute_related`'s docstring now names its consumer
  (`brain.ui.routes_related`) instead of calling itself a deposit with no caller; the comment
  beside the candidate gate now says source gating is the caller's job and names the route's
  withheld-root check. The module's ceiling trail promoted its descriptive hop to `b7fd0e8`;
  the edit is line-count neutral.
- `src/brain/ui/graph_layout.py` — the `RING_RADIUS` comment (§7.4). *(Not the only place the
  claim appeared: the fix round also corrected the `DEFAULT_CAP` comment beside it and renamed the
  `tests/test_ui_graph_layout.py` test that asserted "room for labels" from arc spacing alone.)*
- Design spec — §11 phase-5 row exited with the R2 deviation; §12 Q2 and Q3 marked shipped;
  Appendix B-4 closed by R1; Appendix B-17 given a closing note, original text kept as history.
- `README.md` "Local web UI" — the two read-only inspector blocks.
- `CLAUDE.md` — `related.py`'s ceiling-table trail promoted; no other row implicated (no new
  `src/` file crossed 800 in this phase — re-derive with
  `find src -name '*.py' -exec wc -l {} + | sort -rn`).
