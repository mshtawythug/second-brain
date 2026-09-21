# brain ui — Local Web App Design

**Date:** 2026-07-25
**Status:** Approved for implementation
**Feature id:** F14
**Parent spec:** [`2026-07-25-agent-memory-safety-ui-design.md`](./2026-07-25-agent-memory-safety-ui-design.md)

---

## Inherited constraints

This document does **not** restate the global constraints. See
[`2026-07-25-agent-memory-safety-ui-design.md` §4 "Global constraints"](./2026-07-25-agent-memory-safety-ui-design.md)
— PII discipline, production-database safety, quality gates (`ruff`, `mypy`, 85% coverage), style rules,
and the no-monkey-patching rule apply to every line of F14 unchanged.

Three release-level decisions are **binding** on this document and are reflected throughout:

1. **No new runtime dependencies.** `starlette` 1.3.1 and `uvicorn` 0.49.0 are already installed as
   transitive dependencies of `mcp>=1.0` (§5.2 of the parent spec); F14 promotes them to explicit
   `[project.dependencies]` entries, adding **zero** new wheels to a fresh install. `fastapi` and
   `jinja2` are not installed and must not be added. No Node, npm, Vite, bundler, or CDN: the front end
   is hand-written static assets packaged into the wheel, and it must work fully offline (§5.3 of the
   parent spec).
2. **The three dropdown filters map onto axes that already exist** — **source kind**, **tag**, and
   **content type**. Per rejection **R3**, `workspace` and `category` are *not* becoming columns. §3.4
   below states the mapping and the evidence for each.
3. **F8 owns the CLI-free core that F14's write path calls** — `src/brain/vault/delete.py` and
   folder-aware move in `src/brain/vault/rename.py`. F14 calls those extracted functions; it does not
   duplicate their logic and never shells out to the CLI. §9.1 records this as a hard dependency.

### Migration allocation

**F14 introduces no migration.** The parent spec's allocation table (§2) is central and no section may
deviate; 024–028 belong to F5, F9, F6, F10, and F13. See §9.1 for the one cross-feature request F14
makes of F5's migration 024, and the runtime degradation that makes F14 correct with or without it.

---

## 1. Scope decision

### 1.1 Deferred tabs — stated up front so this is never mistaken for an oversight

The target UX has four tabs: **Notes**, **Ingest**, **Agent**, **Publish**.

> **v1 ships Notes only. Ingest, Agent, and Publish are deliberately deferred.** This is a decision, not
> an omission. Each of the three is a long-running *job*, not a request, and exposing any of them over
> HTTP forces a background-task subsystem — a task registry, progress streaming, cancellation, and
> failure surfacing — that is larger than the entire Notes tab. Shipping them thin would mean a spinner
> that lies about work the server is not actually tracking.

Per-tab justification:

- **Ingest — deferred to v2.** `ingest_document` (`src/brain/ingest/__init__.py:421`) chunks, embeds via
  an Ollama round-trip per batch, enriches through `_enrich_post_ingest_hook` (`:1236`), and syncs the
  graph. The parent spec measured a *single search embed* at ~6 s on the live corpus (§5.4); a PDF
  ingest is tens of seconds to minutes. File upload additionally requires promoting `python-multipart`
  to a declared dependency, which conflicts with the no-new-dependencies constraint.
- **Agent — deferred to v3.** `ask.ask` (`src/brain/ask.py:508`) is a multi-iteration
  plan → retrieve → reflect → synthesize loop against Ollama, governed by `ask_max_iterations` and
  `ask_timeout_seconds`. It needs server-sent events, mid-flight cancellation, and conversation state.
  It also renders **LLM-generated text into the DOM** — the highest-risk XSS surface in the product,
  deserving its own hardening pass rather than riding along on v1.
- **Publish — deferred to v3, with its one instant piece pulled forward.** The wiki is a Quartz static
  build: `brain.wiki` clones a pinned commit (`QUARTZ_PINNED_COMMIT`, `src/brain/wiki/__init__.py`),
  runs `npm install`, builds, and blue/green-swaps behind Caddy; `bin/brain-rebuild` is a 7-stage
  orchestrator. Triggering that from a request handler is the worst instance of the job-runner problem,
  and it already has a good interface. The only genuinely instantaneous part — the **draft ↔ published
  toggle**, backed by `documents.draft` (`migrations/007_email_thread_and_draft.sql`) and
  `cli._set_draft` (`src/brain/cli.py:6476`) — ships in v1 inside the Notes inspector.

The tab bar renders all four labels so the information architecture is visible from day one. The three
inactive tabs render a single paragraph naming the CLI command that does the job today (`brain ingest`,
`brain ask`, `brain-rebuild`) — roughly twelve lines of static HTML each. A documented redirect, not a
stub.

### 1.2 Definition of done — Notes tab

Done means all of the following hold, each verified by the tests in §8:

1. **Left rail.** The full vault renders as a nested, collapsible tree from a single indexed query;
   every leaf carries a document id; expansion state survives reload; `+ New` creates a real
   vault-tier note on disk and in the database in one action; the tree is fully navigable by keyboard
   with correct `role="tree"` ARIA semantics.
2. **Middle column.** The *same* `hybrid_search` the CLI runs, with all three mandated dropdowns and
   the date range plumbed to real kwargs; F5's phase-split latency and match counts display on every
   query; results are keyboard-selectable; empty, loading, and error states are designed rather than
   blank.
3. **Right column.** Title, editable frontmatter strip (relative path read-only; tags, content type,
   and draft editable), rendered markdown for both tiers, an Edit → Save cycle with an explicit
   `Saved` / `Unsaved` / `Saving…` / `Conflict` indicator, and move plus delete behind typed
   confirmation.
4. **Both tiers work.** A vault-tier note saves by writing the file and running `sync_one_file`; an
   ingested-tier document saves through `update_document`. Neither path corrupts the other (§3.8).
5. **Security tests pass.** Cross-origin mutation rejected, foreign `Host` rejected, GET cannot mutate,
   `--read-only` blocks every write, path traversal blocked, CSP present on every response.
6. `ruff check` clean, `mypy src/` clean, coverage ≥ 85% overall with ≥ 95% on the pure modules.
7. `brain ui` starts against the real production database, prints its panel, opens a browser, and stops
   cleanly on Ctrl-C.

---

## 2. Module layout

New package `src/brain/ui/`, plus one CLI module. Every file carries a module docstring, full type
hints, and stays well under 400 lines.

| Path | Purpose | Est. LOC |
|---|---|---|
| `src/brain/ui/__init__.py` | Package docstring; re-exports `create_app`, `UiContext`. | 25 |
| `src/brain/ui/context.py` | `UiContext` frozen dataclass — **the dependency-injection seam**. Holds `cfg`, `conn_factory`, `embedder`, `search_fn`, `now_fn`, `read_only`, `token`, `allowed_origin`, `logging_enabled`. Built once by `server.py`; tests construct it directly. No module-level mutable globals — this is why no test needs to monkey-patch production code (parent spec §4). | 110 |
| `src/brain/ui/errors.py` | `UiError(BrainError)` base plus `UiBadRequest`, `UiNotFound`, `UiConflict`, `UiForbidden`, each carrying an HTTP status and a stable machine `code`; plus the Starlette exception handler rendering them as JSON. Inherits `BrainError` (`src/brain/errors.py:16`) per the style rules. | 120 |
| `src/brain/ui/security.py` | `OriginGuardMiddleware` (Origin / `Sec-Fetch-*` / Content-Type / token / read-only), `SecurityHeadersMiddleware` (CSP and hardening headers), `is_loopback(host)`. | 210 |
| `src/brain/ui/schemas.py` | Typed request parsing and response construction: `parse_search_params`, `parse_note_patch`, `note_to_payload`. All validation lives here so routes stay thin. | 260 |
| `src/brain/ui/tree.py` | **Pure.** `build_tree(rows) -> TreeNode` — flat `vault_path` strings to nested folders. Zero I/O. 95% coverage target. | 130 |
| `src/brain/ui/render.py` | **Pure.** `render_markdown(text, *, resolver) -> str` via `markdown-it-py` with `html=False`; a `[[wikilink]]` inline rule; a `link_open` renderer rule stripping non-`http(s)`/relative schemes. 95% coverage target. | 180 |
| `src/brain/ui/queries.py` | The reads the UI needs that `brain.queries` does not already provide: `iter_tree_rows`, plus F5-facet fallbacks. Parameterized SQL only. | 110 |
| `src/brain/ui/notes_service.py` | The **only** module permitted to mutate. Tier-aware `read_note`, `create_note`, `update_note`, `set_draft`, `move_note`, `delete_note`. Delegates exclusively to existing functions (§3.7–§3.11). Owns the optimistic-concurrency check. | 320 |
| `src/brain/ui/routes_meta.py` | `GET /api/health`, `/api/status`, `/api/facets`. | 110 |
| `src/brain/ui/routes_search.py` | `GET /api/search`. | 160 |
| `src/brain/ui/routes_tree.py` | `GET /api/tree`. | 90 |
| `src/brain/ui/routes_notes.py` | `GET/POST/PUT/DELETE /api/notes*`. Thin: parse → service → serialize. | 290 |
| `src/brain/ui/app.py` | `create_app(context: UiContext) -> Starlette` — routes, middleware stack, static mount, exception handlers, SPA fallback. | 190 |
| `src/brain/ui/server.py` | `resolve_port`, `preflight`, `build_context`, `serve` wrapping `uvicorn.Config`/`uvicorn.Server`. The only module importing uvicorn. | 170 |
| `src/brain/cli_ui.py` | The `brain ui` Typer sub-app. **Lazily** imports `brain.ui.server` inside the command body. | 160 |

**Static assets** — `src/brain/ui/static/` (packaged into the wheel, §2.1):

| Path | Purpose | Est. LOC |
|---|---|---|
| `index.html` | The single hand-written HTML shell. Semantic landmarks, no interpolation, no inline script or style. | 190 |
| `css/tokens.css` | Design tokens: palette (light and dark), type scale, spacing, motion. | 170 |
| `css/base.css` | Reset, semantic element defaults, focus-visible system, reduced-motion block. | 140 |
| `css/layout.css` | Three-column ledger grid, responsive collapse. | 160 |
| `css/components.css` | Tree, ledger rows, chips, inspector, modal, buttons, inputs. | 330 |
| `js/dom.js` | `$`, `el`, `toast` — the three primitives every render module shares. Added during the split; see the note below. | 30 |
| `js/api.js` | `fetch` wrapper: base URL, token header, JSON content type, error normalization. | 110 |
| `js/store.js` | Plain-object store, `subscribe`/`dispatch`, URL ↔ state sync. | 150 |
| `js/tree.js` | Tree render plus roving-tabindex keyboard navigation. | 150 |
| `js/results.js` | Search box, filters, date range, result ledger, latency line. | 190 |
| `js/inspector.js` | Right column: view/edit toggle, save indicator, frontmatter strip, move and delete modals. | 240 |
| `js/keys.js` | Global keyboard map plus typing guard. | 100 |
| `js/main.js` | Bootstrap, router, wiring. | 120 |

#### `tree_nav.js` stays OUTSIDE `js/`, and `dom.js` is a ninth file

The table above is the plan; what shipped differs in two places, both deliberately.

**`static/tree_nav.js` was not folded into `js/tree.js`.** The obvious tidy — one directory, one
tree module — would undo the reason that file exists. `tree_nav.js` is **pure**: no DOM, no `state`,
no imports. It takes a tree node and a `Set` of expanded paths and returns plain descriptors, or
takes a key and an index and returns an action object. That purity is not stylistic:

- **It makes a whole regression class inexpressible.** The visible-item list used to be assembled by
  a `push` inside the recursive DOM build, where "a collapsed folder contributes no children" held
  only because that statement sat above `if (open)`. Hoist one line and navigation walks into
  collapsed folders with every test green. `flattenVisible` makes the same property a *return
  value*: there is no push to hoist. A property enforced by a function signature outranks one
  enforced by reviewer attention.
- **It is executable without a browser.** `tests/test_ui_tree_nav.py` runs nine mutation tests
  directly under node, in milliseconds, with no chromium, no HTTP server and no `page` fixture.
  `js/tree.js` cannot be tested that way — it calls `document`, `localStorage` and `state` on nearly
  every line, so it needs the Playwright harness and its ~1s-per-test cost.

Folding it into `js/tree.js` re-fuses exactly the seam those two properties depend on. The decision
half would become DOM-coupled, the nine node tests would need the browser harness or would be
deleted, and the "collapsed folders contribute no children" invariant would go back to being a
statement-order coincidence. **The file count is not the point; the seam is.** Twelve modules with
the seam re-fused is a worse design than thirteen with it intact.

The import stays absolute (`from "/static/tree_nav.js"`) like every other specifier, so
`test_every_es_module_import_resolves_to_a_file` resolves it to a real file on disk.

**`js/dom.js` was added.** `$`, `el` and `toast` are used by `tree.js`, `results.js`, `inspector.js`
and `main.js` alike. Keeping to seven modules meant housing element construction inside `store.js`,
whose entire job is state and URL sync — a Single-Responsibility violation traded for a file count.

**Count, stated honestly.** §4.1 says "twelve files", counting `index.html` + 4 CSS + the 7 JS
modules above. That count already omitted `theme.js` (loaded un-deferred in `<head>` so the stored
theme applies before first paint) and `tree_nav.js`, both of which predate this note. The real
figure after the split is **15 loadable static assets**: `index.html` + 4 CSS + 8 under `js/` +
`theme.js` and `tree_nav.js` at the static root. Counted on disk with `find`, not summed by hand —
an earlier revision of this very paragraph said "14", which is 1+4+8+2 done wrong while correcting
someone else's count. Twenty files ship in total; the other five are images (3 logos, a favicon,
an app icon), which §4.1's "twelve" never counted because they are not loaded by the shell.

**Packaging follows from that**, and is the same trap twice. setuptools resolves `package-data`
globs with `glob`, where `*` stops at `/` — so `static/*.js` does **not** match `static/js/main.js`.
Shipping without a nested pattern produces a wheel whose `index.html` loads a module graph that
404s: a blank page, with every local test green, because the tests read the source tree and not the
wheel. `pyproject.toml` therefore declares `static/*.js` (for `theme.js` and `tree_nav.js`) **and**
`static/js/*.js`, alongside the matching `static/css/*.css`. All three are mutation-proven by
`tests/test_ui_static_assets.py::test_every_shipped_asset_matches_a_declared_glob`.

### 2.1 Two small refactors, and packaging

**DRY refactor (required).** `_assert_within_vault` exists twice today — `src/brain/cli.py:7726` and
`src/brain/mcp_server.py:265`, the latter carrying an explicit comment reading "copied here rather than
imported so the MCP server has no dependency on a Typer-based module." A third copy is unacceptable.
Move it to `src/brain/vault/paths.py` as `assert_within_vault(target, vault_root, *, label) -> None`
raising `ValueError`, and have all three callers wrap it in their own framework's error type. That
module is the natural home — it already holds `strip_md_extension:16` and `safe_wikilink_alias:38` — and
this removes existing duplication rather than adding any.

**Import-cost refactor (required).** `_VALID_SOURCE_KINDS` (`src/brain/cli.py:4063`) must not be
imported from `cli.py`; pulling that 9,760-line Typer module into an HTTP handler is unacceptable
startup cost. Extract to `src/brain/source_kinds.py` as `VALID_SOURCE_KINDS: frozenset[str]`, and have
`cli.py` import it.

**Note on `cli.py`.** It is 9,760 lines, roughly twelve times the 800-line ceiling in the inherited
style rules. This design deliberately does not add to it — hence `cli_ui.py`, following the existing
`cli_connect.py` / `cli_demo.py` / `_capture_command.py` precedent.

**Packaging.** This repository has already shipped a broken wheel for exactly this class of mistake:
commit `ed8195f`, *"fix(packaging): ship migrations inside the wheel so brain init works on pip
installs"*. Static assets carry identical risk with a worse failure mode — a blank page and no error.

```toml
[tool.setuptools.package-data]
"brain.ui" = ["static/*.html", "static/css/*.css", "static/js/*.js"]

[tool.setuptools.exclude-package-data]
"brain.ui" = ["__pycache__/*", "*.pyc"]
```

Explicit extension patterns, never `**/*` — matching the convention already documented in that file.
`static/`, `static/css/`, and `static/js/` are plain data directories with **no `__init__.py`**, the same
shape as `brain.demo/corpus/` and `brain.templates/docker/age/`. Runtime resolution uses
`importlib.resources.files("brain.ui") / "static"`, mirroring `db.migrations_dir()`
(`src/brain/db.py:160`) and `demo` (`src/brain/demo/__init__.py:100`). Test file 10 (§8) asserts the
directory resolves and every asset referenced by `index.html` exists.

**Explicit dependency promotion** in `[project.dependencies]`:

```
"starlette>=0.47,<2",
"uvicorn>=0.30",
```

Both already resolve today via `mcp>=1.0,<2.0` (verified in `.venv`: starlette 1.3.1, uvicorn 0.49.0),
so this adds no new wheels — it only stops relying on a transitive.

---

## 3. HTTP API contract

**Conventions.** All API responses are `application/json`. Errors use one envelope:

```json
{ "error": { "code": "note_not_found", "message": "no document matches prefix 'abc123'" } }
```

`code` is a stable machine string; `message` never contains SQL, connection strings, or file contents —
mirroring `mcp_server._wrap_db_error` (`src/brain/mcp_server.py:183`), which deliberately exposes only
`type(e).__name__`. Every mutation requires `Content-Type: application/json` and passes the Origin guard
(§5). Every response carries the security headers of §5.3.

### 3.1 `GET /api/health`

No database access. `{"status":"ok","version":"0.2.1","read_only":false,"vault":"…"}`. Used by the shell
to detect a dead server and by the CLI tests.

### 3.2 `GET /api/status`

→ **`brain.queries.summary_counts(conn)`** (`src/brain/queries.py:578`), returning `StatusCounts`
(`:564`). Single-CTE round trip.

```json
{"documents": 1195, "chunks": 20871, "sources": 4,
 "last_ingest": "2026-07-24T18:03:11Z",
 "by_kind": [["gmail", 612], ["krisp", 388], ["manual", 195]]}
```

Errors: `503 database_unavailable` on `psycopg.OperationalError`.

### 3.3 `GET /api/facets`

Populates the three dropdowns. **Primary path: delegate to F5's `--facets` output.** F5 (search
transparency) owns facet counts, and counts are what make a dropdown useful — "krisp (388)" beats
"krisp". F14 consumes that function rather than building a parallel one.

Fallback until F5 lands, in `brain.ui.queries`:

- **Tags** → `brain.queries.list_existing_tags(conn, min_doc_count=1)` (`src/brain/queries.py:492`),
  already normalized and alpha-sorted.
- **Content types** → `SELECT DISTINCT content_type FROM documents ORDER BY 1`.
- **Source kinds** → `SELECT DISTINCT kind FROM sources ORDER BY 1`, unioned with
  `VALID_SOURCE_KINDS` so all four appear before a source has any rows.

```json
{"sources":       [{"value":"gmail","count":612}, {"value":"krisp","count":388}],
 "content_types": [{"value":"transcript","count":388}, {"value":"email_thread","count":612}],
 "tags":          [{"value":"action-items","count":94}, {"value":"inbox","count":7}]}
```

### 3.4 `GET /api/search` — and the three-filter mapping

Delegates to **`brain.search.hybrid_search`** (`src/brain/search.py:231`). Never re-implemented.

#### The three dropdowns (binding decision 2 / rejection R3)

Per R3, F14 maps its three filters onto axes that already exist rather than inventing a parallel
taxonomy. Stated plainly:

| Dropdown | Column | `hybrid_search` kwarg | Vocabulary | Why this axis |
|---|---|---|---|---|
| **Source** | `sources.kind` via `documents.source_id` | `source_kind=` (`search.py:237`; SQL at `:328`) | `{manual, krisp, gmail, slack}` (`cli.py:4063`), unioned with live `SELECT DISTINCT kind FROM sources` | The coarsest and most-used axis, already the CLI's `--source` and MCP's `source`. Indexed by `documents_source_idx` (`001_init.sql:29`). Answers "was this a meeting, an email, or something I wrote?" |
| **Type** | `documents.content_type` | `content_type=` (`search.py:251`; SQL at `:358`) | Dynamic — the field is explicitly open and user-extensible, per the comment at `cli.py:4071-4073` stating a fixed allowlist would reject legitimate custom types | Separates `transcript` from `email_thread` from `note` from `pdf` *within* a single source. |
| **Tag** | `documents.tags` | `tag=` (`search.py:238`; SQL at `:331`) | `queries.list_existing_tags` (`queries.py:492`), pre-normalized by `tags.normalize_tags` (`tags.py:43`) | The only *user-authored* axis. GIN-indexed (`documents_tags_idx`, `001_init.sql:28`). It is how the capture inbox (`inbox` tag, `_capture_command.py:26`) and Krisp action items (`action-items`) are found. |

Deliberately **not** a dropdown: `documents.kind` (the vault/ingested tier). That is a *place*, not a
filter, and the tree already expresses it. `hybrid_search` also has no `kind` kwarg — its docstring at
`search.py:306-307` explicitly warns that `content_type` and `kind` are different things — so a tier
dropdown would require a new search parameter for no user gain.

#### The date range

Targets **F9's updated-range filters** (`documents.updated_at`, migration 025) when present. Until F9
lands, it falls back to `after` / `before` (`search.py:249-250`), which `hybrid_search` applies to
`coalesce(d.sent_at, d.ingested_at)` (`search.py:352-356`).

That fallback is honest rather than approximate: `ingested_at` is bumped to `NOW()` on every content
update — `src/brain/ingest/__init__.py:1135` and `:1769`, and `src/brain/vault/sync.py:1194` — so the
expression genuinely means "sent, or last indexed." The control is labelled **"Updated"** with a
persistent sub-label reading `sent · or last indexed` in the pre-F9 mode, so the label never overstates.
This is a **soft** dependency: F14 ships and is correct without F9.

#### Other parameters

`q` (required, ≤ 512 chars), `limit` (1–50, default 25), `person` → `queries.resolve_person_to_keys`
(`queries.py:210`) → `person_keys=` / `person_display_name=` (`search.py:247-248`), `fts_only`
(`search.py:240`).

Config-sourced kwargs, passed exactly as the CLI (`cli.py:4160+`) and MCP (`mcp_server.py:415-438`) do:
`vector_sim_floor=cfg.vector_sim_floor`, `recency_halflife_days=cfg.recency_halflife_days`,
`snippet_context_tokens=cfg.snippet_context_tokens`, plus a `SearchDiagnostics()` holder
(`search.py:65`).

#### Response — and the honest latency readout

```json
{"session_id": "9f2c…", "count": 49, "fts_count": 31,
 "timing": {"total_ms": 5842, "embed_ms": 5401, "rank_ms": 441},
 "results": [{"id":"…","title":"…","source_kind":"krisp","snippet":"…",
              "score":0.031,"content_type":"transcript","tags":["1-1"]}]}
```

**The reference UI's "49 notes · 0.4ms" is not achievable here and F14 must not imply it is.** The
parent spec §5.4 measured a real search at ~6 seconds on the live corpus, dominated by the Ollama
embedding call. F14 therefore renders F5's phase split:

> `49 notes · 5.8 s` — with `embed 5.4 · rank 0.4` on the second line

That is both truthful and the thing that makes F5's phase-split latency genuinely useful rather than
decorative. In `--fts-only` mode (or under `BRAIN_EMBEDDER=none`, where `hybrid_search` auto-degrades at
`search.py:324`), the embed phase disappears and the readout collapses to `49 notes · 0.4 s`.

`session_id` is a fresh `uuid4` per call, mirroring `brain_search` (`mcp_server.py:399`), so a later
note-open attributes to the search session.

**Side effect:** `brain.gaps.record_search_query` (`src/brain/gaps.py:100`) with `source="ui"` — gated on
`context.logging_enabled` (§9.1).

Errors: `400 invalid_source` (mirroring `cli._validate_source_choice:4066`), `400 invalid_limit`,
`400 invalid_date`, `400 person_not_found` / `person_ambiguous` (`errors.py:189` / `:202`),
`503 embedding_unavailable` on `EmbedError` (`errors.py:52`).

### 3.5 `GET /api/tree`

One indexed query, in `brain.ui.queries.iter_tree_rows`:

```sql
SELECT id::text, title, vault_path, kind, draft, coalesce(sent_at, ingested_at)
FROM documents WHERE vault_path IS NOT NULL ORDER BY vault_path
```

`documents_vault_path_idx` is a partial unique index on exactly
`vault_path WHERE vault_path IS NOT NULL` (`003_vault_model.sql:14-15`), so this is an index-ordered
scan. Flat rows feed the **pure** `ui.tree.build_tree`, returning nested
`{name, path, children[], notes[]}`.

**Why database-derived rather than a filesystem walk.** A walk would re-implement `_walk_vault`'s
exclusion rules (`src/brain/vault/sync.py:491` — skips `_templates/`, `_attachments/`, hidden
components) and would arrive with no document ids, forcing a second lookup per click. Accepted cost: a
`.md` file created on disk but not yet synced does not appear. Mitigated by the vault watcher
(`src/brain/vault/watch.py`), which normally syncs within seconds; the empty-tree state names
`brain vault sync`.

### 3.6 `GET /api/notes/{id_prefix}`

→ `queries.resolve_document_prefix` (`queries.py:77` — minimum 6 hex chars, raising `IdPrefixTooShort` /
`NotHex` / `NotFound` / `Ambiguous` from `errors.py:20-46`) → `queries.fetch_document` (`queries.py:330`,
returning `DocumentRow` with `content`, `source_path`, and the summary triple) →
`vault.graph.backlinks_for` (`src/brain/vault/graph.py:163`) → `ui.render.render_markdown`.

```json
{"id":"…","title":"…","tier":"vault","content_type":"note","draft":false,
 "tags":["1-1"],"source_kind":"manual","vault_path":"acme/People/example.md",
 "ingested_at":"…","summary":"…","body":"# raw markdown …",
 "body_hash":"sha256:…","html":"<h1>…</h1>",
 "backlinks":[{"id":"…","title":"…"}],"editable":true}
```

`body_hash` is the optimistic-concurrency token — `vault.frontmatter.body_hash`
(`src/brain/vault/frontmatter.py:79`). `tier` is `documents.kind` ∈ `{vault, ingested}`
(`003_vault_model.sql:10-11`). `editable` is false for ingested-tier rows with no `vault_path`.

*Forward-compat:* when F6 lands (migration 026), the sensitivity tier joins this payload and gets a slot
in the frontmatter strip. Not v1 work.

**Side effect:** `interactions.record_interaction(conn, document_id=…, action="opened", source="ui",
query=…, session_id=…)` (`src/brain/interactions.py:90`) when a search `session_id` accompanies the
request, mirroring MCP `brain_show`. Gated on `context.logging_enabled` (§9.1).

### 3.7 `POST /api/notes` *(mutation)*

Create a vault-tier note. → **`brain.vault.note_builder.create_vault_note`**
(`src/brain/vault/note_builder.py:136`), which renders `<vault>/_templates/<template>.md`, writes with
collision-safe `_unique_target` (`:116`), and runs `sync_one_file`. It already contains a
path-traversal guard on `folder` (`:191-196`).

Request: `{"title":"…","folder":"acme/People","tags":["a"],"template":"note","body":"optional"}`
Response `201`: `{"id":"…","vault_path":"…","title":"…"}`

Validation in `schemas.py`: non-empty title ≤ 200 chars; `folder` through `assert_within_vault` **before**
the service (defence in depth — `create_vault_note` guards too); tags through `tags.normalize_tags`
(`src/brain/tags.py:43`); body ≤ 256 KB, the same cap as MCP's `_MAX_NOTE_BODY_BYTES`
(`mcp_server.py:113`).

Errors: `400 invalid_title`, `400 folder_escapes_vault`, `400 template_not_found` (from
`VaultNoteSyncError`, `errors.py:268`), `409 sync_failed`.

### 3.8 `PUT /api/notes/{id}` *(mutation)*

Body: `{"body_hash":"sha256:…", "body":"…", "title":"…", "tags":[…], "content_type":"…"}` — all
optional except `body_hash`.

`body_hash` is **required**. A mismatch returns `409 stale_write` with the server's current
`{body, body_hash, html}` so the UI shows a conflict rather than silently clobbering. This is not
theoretical: the vault watcher and `brain-mcp` are both live writers on the same files.

`notes_service.update_note` dispatches on `documents.kind`:

- **`kind='vault'`** — the file is the source of truth; `cli.py:6297-6305` states this explicitly, and
  `update_document` skips vault-tier mirror writes (`ingest/__init__.py:1836`). Path: read file →
  `vault.frontmatter.parse_frontmatter` (`frontmatter.py:37`) → merge the patch into the frontmatter
  dict preserving key order → `vault.frontmatter.dump_frontmatter` (`:16`) →
  `vault._atomic.atomic_write_text` (`src/brain/vault/_atomic.py:6` — sibling temp plus `os.replace`) →
  **`vault.sync.sync_one_file`** (`src/brain/vault/sync.py:258`) with `embedder`,
  `vault_path=cfg.vault_path`, `owner_participants=cfg.owner_participants`. That call re-chunks,
  re-embeds, re-materializes wikilinks, and re-runs the derived linker for that one document. A
  non-empty `SyncReport.errors` (`sync.py:67`) → `409 sync_failed`.
- **`kind='ingested'`** — → **`brain.ingest.update_document`** (`src/brain/ingest/__init__.py:1568`) with
  `new_title`, `new_content`, `new_content_type`, `vault_root=cfg.vault_path`, `embedder`, `enricher`,
  `graph_syncer` — the exact call shape MCP `brain_edit` uses (`mcp_server.py:1036-1048`). Tag changes
  go through **`ingest.apply_tags`** (`:1506`). `ValueError` → `400`.

Response: `{"id":…, "fields_changed":[…], "rechunked":bool, "body_hash":"…", "html":"…"}` — the
re-rendered HTML rides along, so a save costs exactly one request.

### 3.9 `POST /api/notes/{id}/draft` *(mutation)*

Body `{"draft": true}`. → `ingest.update_document(new_draft=…, vault_root=cfg.vault_path,
graph_syncer=…)`, mirroring `cli._set_draft` (`src/brain/cli.py:6476-6515`) including its idempotent
no-op when the column already matches. This regenerates the on-disk mirror's `draft:` frontmatter so the
next Quartz build hides or shows it. This is the pulled-forward slice of the Publish tab (§1.1).

### 3.10 `POST /api/notes/{id}/move` *(mutation, confirmed)* — **depends on F8**

Body: `{"confirm": true, "new_title": "…"}` and/or `{"confirm": true, "new_folder": "…"}`.

Both cases call **`brain.vault.rename.plan_rename`** (`src/brain/vault/rename.py:112`) then
**`brain.vault.rename.apply_rename`** (`:199`), which F8 is making folder-aware.

This is a **hard dependency**, and the reason matters. Verified against `master` today, `plan_rename`
computes `new_relative = old_relative.with_name(f"{new_slug}.md")` (`rename.py:164`) — it changes only
the filename stem within the same parent directory and **cannot move across folders**. An earlier draft
of this design worked around that with a local `os.replace` plus `sync_one_file` in `notes_service.py`.
That workaround is **dropped**: it would have duplicated logic F8 owns, and it could not rewrite
path-form `[[folder/slug|Title]]` backlinks in other files, since `sync_one_file` re-materializes links
only *from* the synced file (`sync.py:280-289` states this). F8's folder-aware `apply_rename` inherits
the existing atomicity contract — snapshot every touched file to a tempdir, restore on any exception
(`rename.py:208-215`) — which a hand-rolled move would not have.

Response carries the `RenameReport` counters (`rename.py:96`). `RenameError` (`:46`) → `400`.
Ingested-tier documents → `400 move_not_supported_for_ingested_tier` (their paths are derived by
`vault.export._ingested_relative_path`, not user-chosen).

### 3.11 `DELETE /api/notes/{id}` *(mutation, typed confirmation)* — **depends on F8**

Body: `{"confirm": true, "expected_title": "<exact current title>"}`.

The server re-reads the title and compares. A mismatch → `409 title_mismatch`, nothing deleted. The
check is **server-side**, not a UI courtesy, so a replayed, stale, or mis-targeted request cannot
destroy the wrong document. This responds directly to the recorded incident of 2026-06-09, where a blind
confirm piped into `brain capture review --limit 1` deleted a real note.

Then → **`brain.vault.delete.delete_document(conn, document_id=…, cfg=…, graph_syncer=…)`**, the
CLI-free core F8 lifts out of the `brain rm` command body (`src/brain/cli.py:6408-6445`). That function
owns the full sequence, and F14 must not reproduce it:

1. `SELECT title, vault_path` **before** the delete.
2. `DELETE FROM documents WHERE id=%s` (chunks and links cascade).
3. `graph_syncer.remove(conn, doc_id)` — best-effort.
4. Unlink the on-disk mirror (today `cli._rm_unlink_vault_mirror`, `:6518`). **Not optional**: without
   it the next `brain vault sync` re-ingests the file and silently undoes the delete.

Response `200`: `{"deleted": true, "id": "…", "mirror_unlinked": true}`.

### 3.12 Method table — the GET-cannot-mutate guarantee

| Path | GET | POST | PUT | DELETE |
|---|:--:|:--:|:--:|:--:|
| `/api/health`, `/api/status`, `/api/facets`, `/api/search`, `/api/tree` | ✅ | 405 | 405 | 405 |
| `/api/notes/{id}` | ✅ | 405 | ✅ | ✅ |
| `/api/notes` | 405 | ✅ | 405 | 405 |
| `/api/notes/{id}/draft`, `/api/notes/{id}/move` | 405 | ✅ | 405 | 405 |

Routes declare explicit `methods=[…]`; Starlette returns 405 otherwise. A parametrized test asserts
every row (§8, file 2).

---

## 4. Front-end architecture

### 4.1 No framework, no bundler, no CDN

`index.html` loads twelve files via plain `<link>` and `<script type="module">`. ES modules are natively
supported everywhere this runs and give real imports with no build step. Nothing is fetched from the
network. Test file 10 greps the entire static tree for `http://` and `https://` outside comments and
fails on any hit — that is the offline guarantee, enforced rather than asserted.

### 4.2 State

`js/store.js` — a plain object plus a subscriber array:

```
state = {
  query, filters: {source, type, tag, after, before},
  results: [], count, ftsCount, timing, searchStatus,
  selectedId, note, editorMode: 'view'|'edit',
  saveStatus: 'saved'|'dirty'|'saving'|'error'|'conflict',
  tree, expanded: Set<string>, theme, facets
}
```

`dispatch(patch)` shallow-merges and notifies. Each render module subscribes and updates **only its own
subtree** with targeted DOM operations — no virtual DOM, no full re-render.

**URL is the source of truth** for shareable state, per the project's "URL As State" rule: `q`, `source`,
`type`, `tag`, `after`, `before`, and `id` live in `URLSearchParams`, written via `history.replaceState`
(debounced 200 ms). Deep-linking to `?q=budget&source=krisp&id=9f2c1a` restores the exact view.
Ephemeral state — editor mode, tree expansion, theme — lives in `localStorage`, not the URL.

*Forward-compat:* F13 (saved searches, migration 028) maps onto this cleanly — a saved search is exactly
this parameter set. Not v1 work, but the URL contract is designed to be its storage shape.

Search is debounced 180 ms and every in-flight request carries an `AbortController` cancelled by the next
keystroke, so a slow query can never overwrite a newer result. Given the measured ~6 s embed, this is
load-bearing, not a nicety.

### 4.3 Markdown rendering: **server-side, in Python**

Decision: render on the server with `markdown-it-py`. Four reasons:

1. **Zero new dependencies.** `markdown-it-py>=3.0` is already declared in `pyproject.toml` and —
   verified, `grep -rn "markdown_it" src/` returns zero hits — is currently unused. Client-side
   rendering would require a JS markdown library, meaning either a bundler (forbidden by binding
   decision 1) or vendoring a minified blob into the wheel, which is a license, review, and
   supply-chain burden for something Python already does.
2. **XSS defence in one testable place.** `MarkdownIt("commonmark")` defaults to `html=False`, which
   *escapes* raw HTML in the source rather than passing it through, so a note containing `<script>`
   renders as literal text. That is one setting, in Python, covered by a pytest assertion — versus
   sanitization logic in JS that would be far harder to hold to the 85% coverage bar. A `link_open`
   renderer rule additionally rejects any href scheme that is not `http`, `https`, or a same-origin
   relative path, killing `javascript:` and `data:`.
3. **No extra round trip.** Rendered HTML rides along on both `GET /api/notes/{id}` and the `PUT`
   response.
4. **Wikilinks need a parser, not a regex.** `[[Target]]` is not CommonMark. Implementing it as a
   registered markdown-it **inline rule** means code fences and inline code are skipped automatically by
   the tokenizer — the same correctness property `vault.rename.collect_references` relies on
   (`rename.py:124-126`). A regex over rendered HTML would corrupt code blocks.

Accepted tradeoff: no live preview while typing. Mitigated by making edit mode a plain source
`<textarea>` — the more honest model for a file-backed vault — with a Preview toggle costing one request.

### 4.4 Accessibility

- **Semantic landmarks.** `<header>` with `<nav aria-label="Sections">`, then `<main>` containing
  `<nav aria-label="Vault">`, `<section aria-label="Search results">`, and `<article aria-label="Note">`.
  No `<div>` where a semantic element exists.
- **Tree.** `role="tree"` / `role="group"` / `role="treeitem"` with `aria-expanded` and `aria-level`.
  Roving `tabindex` — one tab stop for the whole tree, then ↑↓←→ and Home/End within, per the WAI-ARIA
  APG. Every note is a real `<a href="?id=…">`, so middle-click and Cmd-click work.
- **Results.** An ordered list of `<a>` elements; `aria-activedescendant` on the search input for the
  type-ahead relationship; `aria-live="polite"` on the count/latency line so screen readers hear results
  change.
- **Focus.** One `:focus-visible` treatment — a 2 px accent outline at 2 px offset. Never
  `outline: none`. Modals trap focus, restore it to the trigger on close, and close on Esc.
- **Modals.** `<dialog>` with `role="alertdialog"` for destructive actions; the confirm button stays
  disabled until the typed title matches.
- **Reduced motion.** Every transition sits inside `@media (prefers-reduced-motion: no-preference)`, so
  the reduced-motion default is *no animation at all* rather than an override that can be missed.
- **Contrast.** All token pairs meet WCAG AA (≥ 4.5:1 body, ≥ 3:1 large and UI) in both themes; measured
  ratios are recorded in a comment in `tokens.css`.
- **No mouse-only affordances.** Every shortcut has a visible, clickable equivalent.

### 4.5 Keyboard shortcuts

`js/keys.js` registers **one** `keydown` listener on `document`, guarded by
`if (e.target.closest('input, textarea, [contenteditable]')) return;` for single-key bindings; modifier
combinations bypass the guard.

| Keys | Action |
|---|---|
| `Cmd/Ctrl+K`, `/` | Focus search (selects existing text) |
| `Esc` | Blur search / close modal / leave edit mode (prompting if dirty) |
| `↑` `↓` | Move result selection |
| `Enter` | Open selected result |
| `Cmd/Ctrl+S` | Save (edit mode only); `preventDefault` |
| `Cmd/Ctrl+Enter` | Save and return to view |
| `Cmd/Ctrl+E` | Toggle edit mode |
| `Cmd/Ctrl+B` | Toggle the left rail |
| `?` | Shortcut sheet |

`Cmd+K` is deliberate and non-conflicting: the existing Quartz command palette is bound to
`Cmd/Ctrl+P`, and the parent spec §3 records that F14 uses `Cmd+K` in its own surface. The platform
split is `navigator.platform`-free — it tests `e.metaKey || e.ctrlKey`. The search box renders a
`<kbd>⌘K</kbd>` hint so the binding is discoverable.

---

## 5. Visual design direction

### 5.1 Direction: **"Archival Terminal"**

A research-library card catalog rendered by someone who lives in a terminal. Not minimal — *dense and
typeset*. Three deliberate moves:

1. **The middle column is a ledger, not a card grid.** Each result is a two-column row: a narrow
   monospace metadata gutter (date, source glyph, 8-character id) hard-left, then title, snippet, and tag
   chips. Rows are separated by hairline rules, not gaps and shadows. This reads like a catalog drawer —
   which is what it is — and is the explicit opposite of the banned default card grid.
2. **The right column is a reading room.** Where the ledger is dense (`--lead-dense: 1.35`), the note body
   is airy: a **serif** column capped at 68 characters with generous leading. The scale and density
   contrast between the two columns *is* the hierarchy; no boxes are needed to say "this one matters."
3. **Depth from hairlines and sunken wells, not drop shadows.** The three columns share edges via 1 px
   rules. The middle column sits one step darker than the paper (a well); the inspector one step lighter
   (a sheet on the desk). Total shadow usage in the app: two — the modal and the tag popover.

Texture: a ~2% opacity grain overlay generated inline via `feTurbulence` in a `data:` URI on
`body::before`, `pointer-events: none`. It keeps large flat areas from reading as flat digital template,
costs zero network bytes, and is disabled under `prefers-reduced-transparency`.

### 5.2 Palette

Light theme **"Foolscap"** — warm paper, warm ink. Not white, not gray.

```css
:root {
  --paper:        oklch(97.5% 0.008 85);   /* warm off-white ground */
  --paper-sunk:   oklch(95.0% 0.010 85);   /* result ledger well    */
  --paper-raised: oklch(99.0% 0.004 85);   /* inspector sheet       */
  --ink:          oklch(22.0% 0.012 60);   /* body text             */
  --ink-muted:    oklch(48.0% 0.010 60);   /* metadata, snippets    */
  --ink-faint:    oklch(66.0% 0.008 60);   /* placeholders          */
  --rule:         oklch(88.0% 0.010 75);   /* hairlines             */
  --accent:       oklch(52.0% 0.090 178);  /* verdigris             */
  --accent-soft:  oklch(93.0% 0.030 178);  /* selected row wash     */
  --flag-draft:   oklch(64.0% 0.130 68);   /* amber: draft/unsaved  */
  --flag-danger:  oklch(52.0% 0.170 25);   /* oxide red: delete     */
  --flag-ok:      oklch(52.0% 0.090 155);  /* saved                 */
}
```

Dark theme **"Lamplight"** — deliberately *not* an inversion. A warm charcoal desk under a lamp, with
parchment ink. The accent shifts lighter (52% → 72%) because verdigris at 52% fails contrast on a dark
ground; the danger red shifts up and desaturates slightly for the same reason. Both themes were chosen,
not derived.

```css
:root[data-theme="dark"] {
  --paper:        oklch(19.0% 0.012 65);
  --paper-sunk:   oklch(16.0% 0.012 65);
  --paper-raised: oklch(23.0% 0.013 65);
  --ink:          oklch(91.0% 0.012 85);
  --ink-muted:    oklch(70.0% 0.010 80);
  --ink-faint:    oklch(54.0% 0.008 80);
  --rule:         oklch(30.0% 0.012 70);
  --accent:       oklch(72.0% 0.100 178);
  --accent-soft:  oklch(28.0% 0.040 178);
  --flag-draft:   oklch(76.0% 0.120 72);
  --flag-danger:  oklch(66.0% 0.150 27);
  --flag-ok:      oklch(70.0% 0.090 155);
}
```

Theme resolution: `@media (prefers-color-scheme: dark)` sets the default; `data-theme` on `<html>`
overrides; the choice persists in `localStorage` and is applied by a tiny same-origin `.js` file loaded
in `<head>` before first paint (not inline — the CSP forbids inline script). **Dark is not the default**;
the OS decides.

Color is used **semantically**, never decoratively: verdigris = selection and interactive affordance;
amber = draft, unsaved, needs attention; oxide red = destructive only; green = saved. There is no fifth
color.

### 5.3 Typography — system stacks only, zero downloads

Three roles, three stacks, all resolved locally (binding decision 1: must work fully offline).

```css
--font-ui:    ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
              "Helvetica Neue", Arial, sans-serif;
--font-read:  ui-serif, Georgia, "Iowan Old Style", "Palatino Linotype",
              "Times New Roman", serif;
--font-mono:  ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
              "Liberation Mono", monospace;
```

The assignment carries meaning: **mono** for anything machine-shaped — relative paths, UUID prefixes,
dates, the latency readout, tag chips (the "terminal" half); **serif** for the note body only (the
"archival" half); **sans** for chrome. Reading the interface tells you what *kind* of thing you are
looking at before you read the words.

```css
--t-meta:  0.6875rem;                                  /* mono gutter       */
--t-chip:  0.75rem;
--t-ui:    0.8125rem;
--t-title: 0.9375rem;                                  /* ledger row title  */
--t-body:  clamp(1rem, 0.94rem + 0.28vw, 1.125rem);    /* serif reading     */
--t-h1:    clamp(1.5rem, 1.2rem + 1.4vw, 2.25rem);     /* note title        */
--measure: 68ch;
--lead-dense: 1.35;
--lead-read:  1.66;
```

Numeric columns use `font-variant-numeric: tabular-nums` so dates and the live latency readout do not
jitter as they update.

### 5.4 Spacing, rhythm, motion

```css
--s-1: 0.25rem; --s-2: 0.5rem;  --s-3: 0.75rem; --s-4: 1rem;
--s-5: 1.5rem;  --s-6: 2rem;    --s-7: 3rem;
--rail-w:   clamp(15rem, 18vw, 20rem);
--ledger-w: clamp(21rem, 27vw, 30rem);
--gutter-w: 5.5rem;                 /* the mono metadata gutter */
--radius:   3px;                    /* near-square: index card, not pill */
--radius-chip: 2px;
--hair: 1px solid var(--rule);
--dur-fast: 120ms; --dur: 180ms;
--ease: cubic-bezier(0.16, 1, 0.3, 1);
```

Rhythm is deliberately **non-uniform**: ledger rows use `--s-2`/`--s-3`, the inspector `--s-5`/`--s-6`.
That density contrast does hierarchy work, and it is the main reason the result will not read as a
template. Radius is 3 px everywhere except chips at 2 px — near-square corners read as index cards and
are the single most effective way to not look like default shadcn.

Motion animates `opacity` and `transform` only, never layout properties, and only for: selection wash
(120 ms), inspector cross-fade on note change (180 ms), modal scale-in (180 ms), and the save-indicator
state change (120 ms). All inside `@media (prefers-reduced-motion: no-preference)`.

### 5.5 Layout

```
┌─ brain ─────────  Notes │ Ingest │ Agent │ Publish ────────  ☾ ─┐
├──────────────┬────────────────────────┬───────────────────────────┤
│ VAULT   + New│ ⌕ search…         ⌘K   │ Q3 Planning Sync          │
│              │ ┌────┬────┬────┬─────┐ │        [Edit]  ● Saved    │
│ ▾ acme       │ │Src▾│Typ▾│Tag▾│ 📅  │ │ ─────────────────────────  │
│   ▾ People   │ └────┴────┴────┴─────┘ │ acme/People/example.md    │
│     example  │ 49 notes · 5.8 s       │ type note · vault         │
│     other    │   embed 5.4 · rank 0.4 │ tags [1-1] [planning] +   │
│   ▸ Projects │ ────────────────────── │ ○ draft   ⋯ move  ⌫ delete│
│ ▸ daily      │ 07-24 ▪ 9f2c1a         │ ─────────────────────────  │
│ ▸ _ingested  │   Q3 Planning Sync     │                           │
│              │   …matched snippet…    │   The quarter opened with  │
│              │   [1-1] [planning]     │   three unresolved …       │
└──────────────┴────────────────────────┴───────────────────────────┘
   mono gutter ─┘        dense ledger          serif reading column
```

Responsive: below 1100 px the left rail becomes an overlay drawer (`Cmd+B`); below 780 px the ledger and
inspector become a two-view stack with a back affordance. Both remain fully keyboard-operable.

---

## 6. The `brain ui` CLI command

### 6.1 Signature

```
brain ui [OPTIONS]

  --port          INTEGER  Port to bind.                       [default: 8765]
  --host          TEXT     Bind address.                       [default: 127.0.0.1]
  --open/--no-open         Open a browser on start.            [default: --open]
  --read-only              Serve read-only; block all mutations.
  --token         TEXT     Shared secret. Required when --host is not loopback.
  --auto-port/--no-auto-port
                           Bump to the next free port if taken. [default: --auto-port]
```

Port **8765** avoids every port this repository uses: production Postgres 55432, test Postgres 5434,
demo Postgres 55433 (`brain.demo.DEFAULT_DEMO_PORT`), wiki/Caddy 8080 (`setup.py:832`), Ollama 11434.

Implemented in `src/brain/cli_ui.py` as a Typer sub-app with `invoke_without_command=True` plus a
callback — the shape of `capture_app` (`_capture_command.py:35-45`) — registered with
`app.add_typer(ui_app, name="ui")`. That leaves room for `brain ui open` later without a breaking change.

### 6.2 Import-cost discipline

`cli.py` already carries an explicit comment about avoiding expensive module-scope imports
(`cli.py:204-206`, regarding the networkx chain). `cli_ui.py` therefore imports only `typer`, `Config`,
and `BrainError` at module level; `from .ui.server import serve, preflight, resolve_port` happens inside
the command body. Test file 11 asserts that importing `brain.cli` leaves `starlette` and `uvicorn` out
of `sys.modules`.

### 6.3 Startup sequence and output

1. `Config.load()` (`src/brain/config.py:735`) — `ConfigError` → red message, exit 1.
2. If `--host` is not in `{127.0.0.1, ::1, localhost}` and `--token` is empty → `typer.BadParameter`
   (exit 2): *"binding to a non-loopback address exposes your entire brain to the network; pass
   --token &lt;secret&gt; to proceed, or drop --host."*
3. `cfg.vault_path.is_dir()` — else exit 1 with a `brain vault init` hint.
   (Note: the field is `Config.vault_path`, `config.py:531`. There is no `Config.vault_root`;
   `vault_root` is only a *keyword-argument name* on `ingest_document` (`ingest/__init__.py:432`) and
   `update_document` (`:1580`), whose call sites pass `vault_root=cfg.vault_path`.)
4. One database probe: `connect(cfg.database_url)` (`src/brain/db.py:53`) plus `SELECT 1`, so a dead
   Postgres fails here — before uvicorn binds — with the remediation copy `brain doctor` uses.
5. Interaction-logging capability probe (§9.1).
6. Port resolution (§6.4).
7. Build `UiContext` → `create_app` → `uvicorn.Server(...).run()`.
8. On `--open`, `webbrowser.open(url)` (stdlib, no dependency) from a uvicorn startup callback, so the
   browser never races the bind.

Printed panel (Rich, matching the `brain doctor` / `brain setup` house style):

```
  brain ui

  URL        http://127.0.0.1:8765
  Mode       read-write
  Vault      /Users/…/brain-vault           1,195 notes
  Database   second_brain @ localhost:55432
  Embedder   arctic (1024d)

  Opening your browser… Press Ctrl-C to stop.
```

The database line prints **host, port, and database name only** — never the URL, never the password.
`_wrap_db_error` (`mcp_server.py:183`) already establishes this convention.

With `--read-only`: `Mode  read-only  ·  every write is blocked`, in amber.
With a non-loopback bind: an amber warning block naming the exposure and the URL form
`http://<host>:<port>/#t=<token>`.

### 6.4 Port already in use

`ui.server.resolve_port(start, *, attempts=20)` mirrors `brain.demo.resolve_port`
(`src/brain/demo/__init__.py:334`) and its `_port_is_free` socket probe (`:323`) — portable, no `lsof`.

- `--auto-port` (default): bumps and prints `port 8765 was busy — using 8766 instead`.
- `--no-auto-port`: exit 1, reusing `setup._check_port_free`'s remediation wording
  (`src/brain/setup.py:278-286`):

  ```
  port 8765 is already in use
    Stop the process using port 8765, pass a different --port,
    or drop --no-auto-port to auto-select the next free one.
  ```
- All 20 candidates busy → `BrainError`, exit 1.

A TOCTOU window exists between probe and bind, so `serve()` also catches `OSError` with
`errno.EADDRINUSE` from uvicorn and prints the same message rather than a traceback.

### 6.5 Shutdown

Ctrl-C → uvicorn graceful shutdown → `brain ui stopped`, exit 0. In-flight `sync_one_file` calls
complete because uvicorn drains before closing. No PID file, no launchd plist: `brain ui` is
foreground-only by design. Daemonizing a *write-capable* HTTP server that could outlive the user's
attention is a bad idea; the read-only wiki is the surface that gets a daemon.

---

## 7. Security model

### 7.1 Binding and the two distinct attack classes

Default bind is `127.0.0.1`. A local server that **mutates data** faces two different attacks, and they
need two different defences — conflating them is the common mistake.

**DNS rebinding.** An attacker's page resolves `evil.test` to `127.0.0.1` and issues *same-origin*
requests to it. The browser considers this same-origin, so an Origin check alone does **not** stop it.
The defence is Host-header validation: `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost",
"[::1]"])`. Verified in-process on starlette 1.3.1: a request carrying `Host: evil.example` returns
**400**.

**CSRF.** An attacker's page at a genuinely different origin issues a cross-origin request that rides the
user's ambient local access. Three layers:

1. `OriginGuardMiddleware` requires, for every non-safe method (anything but GET/HEAD/OPTIONS), an
   `Origin` header exactly matching the bound origin. A **missing** `Origin` on a mutation is also
   rejected — fail-closed, since some legacy form posts omit it.
2. `Sec-Fetch-Site: same-origin` is checked when present as belt-and-braces.
3. Every mutation requires `Content-Type: application/json`. This alone defeats HTML-`<form>` CSRF —
   forms can only send `urlencoded`, `multipart`, or `text/plain` — and forces a CORS preflight for any
   cross-origin `fetch`, which the Origin check then rejects.

### 7.2 Non-loopback binding

`--host` outside the loopback set requires `--token`, enforced at parse time (exit 2). The token is
compared with `secrets.compare_digest`. Delivery avoids the URL: the browser is opened at
`http://<host>:<port>/#t=<token>` — a **fragment**, which is never sent to the server, never written to
access logs, and never leaked in a `Referer`. The shell reads `location.hash` at boot, holds the token in
memory, strips the hash via `replaceState`, and sends it as an `X-Brain-UI-Token` header on every
request.

### 7.3 Response headers

Every response:

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none';
  frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store          (API responses)
```

No inline script and no inline style anywhere, so no nonce is required and the policy stays strict.
`img-src data:` exists solely for the grain overlay (§5.1). `form-action 'none'` and
`frame-ancestors 'none'` are extra CSRF and clickjacking hardening.

### 7.4 Other gates

- **`--read-only`.** The middleware short-circuits every non-safe method with 403 *before* routing, so
  correctness does not depend on any individual handler remembering to check.
- **Destructive operations.** Never reachable by GET (§3.12), always require `confirm: true`, and delete
  additionally requires a server-verified `expected_title` (§3.11).
- **Path traversal.** Every folder or path input passes through the extracted
  `vault.paths.assert_within_vault` (§2.1) before reaching the service layer.
- **Production database safety.** The server reads `DATABASE_URL` through `Config.load()` exactly like
  every other command, and performs no schema DDL, no `TRUNCATE`, and no unbounded `DELETE`. The only
  delete is a single parameterized `DELETE … WHERE id = %s` inside F8's `vault.delete`.

---

## 8. Test plan

New directory `tests/ui/`. Real Postgres at
`postgresql://brain:brain@localhost:5434/second_brain_test`, `FakeEmbedder` (`tests/conftest.py:417`),
and a `tmp_path` vault. **Zero monkey-patching of production modules** (parent spec §4):
`create_app(context=UiContext(...))` is the injection seam, and `UiContext` carries `search_fn` and
`now_fn` so even the search call and the clock are injectable.

Client: `starlette.testclient.TestClient` with `base_url="http://127.0.0.1:8765"`.

**Verified caveat.** On starlette 1.3.1 with httpx 0.28.1 this emits
`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2
instead`. It works correctly — I ran it: 200s on valid requests, and `TrustedHostMiddleware` correctly
returned 400 on a foreign Host. No sync alternative exists without a new dependency: `httpx.ASGITransport`
is async-only in 0.28 (verified — `AttributeError: 'ASGITransport' object has no attribute '__enter__'`),
and `pytest-asyncio` is not a dev dependency. Since binding decision 1 forbids new dependencies,
**keep `TestClient`** and add one narrowly-scoped entry to `[tool.pytest.ini_options]`:

```toml
filterwarnings = ["ignore::starlette.testclient.StarletteDeprecationWarning"]
```

| # | File | Behaviors covered |
|---|---|---|
| 1 | `tests/ui/conftest.py` | Fixtures: `ui_vault` (tmp vault, `init_vault`, `_templates/note.md`), `ui_context`, `ui_client`, `ui_client_readonly`, `seeded_notes`. No test logic. |
| 2 | `test_ui_security.py` | **Origin:** cross-origin POST/PUT/DELETE → 403; missing `Origin` → 403; correct origin passes. **DNS rebinding:** GET with `Host: evil.example` → 400. **GET-cannot-mutate:** parametrized over the §3.12 table — every read path 405s for POST/PUT/DELETE, every write path 405s for GET. **Content-Type:** `application/x-www-form-urlencoded` POST → 415. **Read-only:** `ui_client_readonly` gets 403 on every non-safe method, 200 on every safe one. **Token:** non-loopback context rejects missing and wrong tokens, accepts the right one, uses `compare_digest`. **Headers:** CSP and the three hardening headers present on every response. **Traversal:** `POST /api/notes` with `folder="../../etc"` → 400 *and* an assertion nothing was written outside `tmp_path`. **Binding:** `build_bind_host` defaults to `127.0.0.1` and refuses `0.0.0.0` without a token. |
| 3 | `test_ui_search.py` | Delegation: an injected recording `search_fn` asserts exact kwargs (`vector_sim_floor`, `recency_halflife_days`, `snippet_context_tokens` sourced from `cfg`). Each of the three mandated dropdowns filters correctly against seeded data. Date range filters correctly in both the F9 and pre-F9 modes. `limit` bounds → 400; unknown `source` → 400; `q` > 512 chars → 400. `timing` and `count` present; timing collapses to a single phase under `fts_only`. `EmbedError` → 503; `PersonNotFound` → 400. A `search_queries` row lands with `source='ui'` **when `logging_enabled`**, and none when not. |
| 4 | `test_ui_notes_read.py` | Fetch by 6-char prefix and by full UUID. Unknown → 404; 5-char → 400; ambiguous → 400; non-hex → 400. Payload carries `vault_path`, `tier`, `body_hash`, `html`, `backlinks`. Ingested-tier row without `vault_path` reports `editable: false`. An `interactions` row is written when `session_id` is supplied and logging is enabled. |
| 5 | `test_ui_render.py` (pure, 95%) | `<script>alert(1)</script>` renders escaped, not executed. `<img onerror=…>` escaped. `[x](javascript:alert(1))` stripped. Fenced code blocks preserved verbatim. `[[Target]]` → resolved anchor when the title exists, `--unresolved` class when not. `[[…]]` **inside a code fence is not linkified**. Empty body → empty string, not a crash. |
| 6 | `test_ui_tree.py` (pure `build_tree` 95% + one route test) | Flat paths nest correctly; files at vault root; deeply nested paths; stable folder-first ordering; empty input → empty root; `_ingested/` renders as a normal branch; draft flag surfaces on the leaf; identically-named folders at different depths do not collide. Route test asserts one query and the JSON shape. |
| 7 | `test_ui_notes_write.py` (integration) | `POST /api/notes` creates a real file under the vault and a real `kind='vault'` row; a slug collision appends `-2`. `PUT` on a vault-tier note rewrites the file, runs `sync_one_file`, **preserves the UUID**, and re-chunks. `PUT` on an ingested-tier document routes through `update_document` and returns `fields_changed`. Tags normalize (`Interview Prep` → `interview-prep`). Draft toggle round-trips and is idempotent. Stale `body_hash` → 409 carrying the server's current body. Missing `body_hash` → 400. |
| 8 | `test_ui_destructive.py` (integration, **depends on F8**) | DELETE without `confirm` → 400. DELETE with a wrong `expected_title` → 409 **and the row still exists**. Correct → row gone, chunks cascaded, mirror file unlinked. Move without `confirm` → 400. Rename delegates to `plan_rename`/`apply_rename` and rewrites a `[[…]]` reference in a second file. **Folder move preserves the UUID, updates `documents.vault_path`, and rewrites path-form backlinks** (the F8 behavior). Move to `../outside` → 400 with nothing moved. Move on an ingested-tier document → 400. |
| 9 | `test_ui_cli.py` | `CliRunner` over `brain ui` with injected `serve_fn` and `opener`, so nothing ever binds or blocks. `--help` lists every flag. `--host 0.0.0.0` without `--token` → exit 2. Busy port with `--no-auto-port` → exit 1 with the remediation string; `--auto-port` bumps and says so. `--no-open` does not call the opener; `--open` does. Dead database → exit 1 *before* `serve_fn` is reached. Logging capability absent → warning printed, server still starts, `logging_enabled` false. |
| 10 | `test_ui_static_assets.py` | `importlib.resources.files("brain.ui")/"static"` resolves (the `ed8195f` packaging regression class). Every `href`/`src` in `index.html` exists on disk. Correct `Content-Type` per extension. **No `http://` or `https://` anywhere in the static tree** — the offline guarantee. **No inline `<script>` or `<style>`** — the CSP guarantee. A path outside `static/` → 404. |
| 11 | `test_ui_import_cost.py` | Importing `brain.cli` in a subprocess leaves `starlette` and `uvicorn` absent from `sys.modules`. |

**Coverage targets** against the inherited 85% floor: `tree.py`, `render.py`, `schemas.py` ≥ 95%;
`notes_service.py`, `routes_*.py`, `security.py` ≥ 90%; `server.py`, `cli_ui.py` ≥ 85% via the
`serve_fn` / `opener` seams.

**Fixtures contain zero PII**, per the inherited constraint. Synthetic corpus only: notes titled
*"Q3 Planning Sync"* and *"Vendor Evaluation Notes"*; people `example-person-a` and
`person-b@example.invalid`; organization *"Acme Holdings"*. Copies the existing synthetic patterns.

---

## 9. Dependencies, risks, and rejected alternatives

### 9.1 Cross-feature dependencies

**HARD — F8 (`brain note move` + MCP CRUD parity).** F14's write path cannot land before F8 extracts the
CLI-free core. Exact functions F14's endpoints call:

| F14 endpoint | F8 function |
|---|---|
| `DELETE /api/notes/{id}` (§3.11) | `brain.vault.delete.delete_document(conn, *, document_id, cfg, graph_syncer)` |
| `POST /api/notes/{id}/move` (§3.10) | `brain.vault.rename.plan_rename(conn, *, vault_path, document_id, new_title, new_folder)` then `brain.vault.rename.apply_rename(conn, *, embedder, vault_path, op)` |

F14 must not duplicate that logic and must never shell out to the CLI. If F8 slips, F14 ships waves A–C
and E (read-only surface) and holds wave D.

**SOFT — F5 (search transparency).** F14 consumes F5's phase-split timing (§3.4) and `--facets` output
(§3.3). Until F5 lands, F14 measures total elapsed time itself with `time.perf_counter()` and computes
facets without counts. The UI degrades from `49 notes · 5.8 s (embed 5.4 · rank 0.4)` to
`49 notes · 5.8 s`.

**SOFT — F9 (`documents.updated_at`).** The date control targets F9's updated-range filters when present
and falls back to `after`/`before` on `coalesce(sent_at, ingested_at)` otherwise, with the sub-label of
§3.4 so the control never overstates.

**REQUEST TO F5 — two extra `ALTER`s in migration 024.** `interactions.source` and
`search_queries.source` are both `CHECK (source IN ('cli','mcp','wiki'))`
(`migrations/010_interactions.sql:20`, `migrations/019_search_queries.sql:22`); `'ui'` is rejected today.
F14 has **no migration allocated** and §2 of the parent spec states no section may deviate, so F14 does
not create one. Instead it asks F5 — already the owner of `024_search_queries_duration.sql`, already
touching `search_queries` — to include:

```sql
ALTER TABLE interactions   DROP CONSTRAINT IF EXISTS interactions_source_check;
ALTER TABLE interactions   ADD  CONSTRAINT interactions_source_check
  CHECK (source IN ('cli', 'mcp', 'wiki', 'ui'));
ALTER TABLE search_queries DROP CONSTRAINT IF EXISTS search_queries_source_check;
ALTER TABLE search_queries ADD  CONSTRAINT search_queries_source_check
  CHECK (source IN ('cli', 'mcp', 'wiki', 'ui'));
```

Additive, constraint-only, no data altered, safe to re-run. `interactions._VALID_SOURCES`
(`src/brain/interactions.py:60`) gains `"ui"` in the same change — its docstring states the Python and
SQL sets are kept in lockstep. The constraint names above assume Postgres's auto-generated
`<table>_<column>_check` form; the implementer **must verify with `\d interactions` rather than trust
it**. `DROP … IF EXISTS` makes a wrong guess a silent no-op, which is precisely why verification is
required.

**F14 is correct with or without this.** At startup `preflight` probes whether `'ui'` is accepted; if not
it prints

```
  ⚠  search + open logging is disabled — the source CHECK does not accept 'ui'.
     Run `brain init` after F5's migration lands. The UI works normally otherwise.
```

and sets `UiContext.logging_enabled = False`, which short-circuits `record_search_query` and
`record_interaction`. This is not optional politeness: `record_search_query` swallows only
`OperationalError`, `UndefinedTable`, and the `fts_count` `UndefinedColumn`, and its docstring at
`gaps.py:136` states "any other schema/programming error **propagates**." An unhandled `CheckViolation`
would 500 every single search.

**Forward-compat, not v1 work.** F13 (saved searches, migration 028) maps directly onto F14's
URL-as-state parameter set (§4.2). F6 (sensitivity tier, migration 026) gets a slot in the frontmatter
strip (§3.6).

### 9.2 Rejected alternatives

**1. React + Vite — rejected (binding decision 1).** Requires Node at build time and a `node_modules`
tree, breaking `pip install secondbrain-py`: the wheel would carry pre-built bundles committed as
unreviewable minified diffs. It would also add a *second* Node toolchain alongside the Quartz one the
project already fights (a pinned upstream commit, `npm install` inside `wiki.install`, a `--clean-cache`
flag on `brain-rebuild`). Vanilla ES modules keep the wheel pure-Python and the diff readable. **Cost:**
no component model, no reactivity. **Mitigation:** each JS file is single-responsibility and ≤ 240 lines,
and the store's subtree-targeted updates keep manual DOM work small. Revisit if the front end passes
~2,000 lines.

**2. Extending Quartz — rejected.** Quartz is a *static site generator*: `brain.wiki` clones a pinned
commit, builds an immutable artifact, and blue/green-swaps it behind Caddy (`build_swap.py`,
`fastpath_manifest.py`). It has no request-time server, no database connection, and no write path at all.
Bolting a mutation API onto it would mean running a minutes-long Node build to see an edit. **They are
different products:** the Quartz wiki is *published, read-only, whole-corpus, shareable, optimized for
browsing and linking*; `brain ui` is *local, read-write, single-note-focused, optimized for finding and
editing*. They share the vault and the database — not a line of code. Explicit non-goals for `brain ui`:
it does not render the wiki, does not trigger wiki builds, and does not touch `quartz_overrides/`.

**3. FastAPI — rejected (binding decision 1).** Not installed. Adding it pulls `pydantic` v2 — a compiled
extension — purely for request validation that `ui/schemas.py` does in ~260 lines of plain typed helpers.
Starlette already resolves transitively, so promoting it adds **zero** new wheels. Pydantic would also
duplicate the project's established dataclass value-object convention (`SearchResult`, `DocumentRow`,
`ExtractedDoc`, `SyncReport`) with a second, incompatible modelling system. **Cost:** no auto-generated
OpenAPI docs — acceptable for a nine-endpoint single-user app whose contract is this document.

**4. Re-implementing search — rejected, and the largest correctness risk if ignored.** Every ranking
decision lives inside `hybrid_search` (`search.py:231`): RRF at k=60, `PER_DOC_CHUNK_CAP=3`, the
compact-form tsquery expansion (`_build_tsquery:118`), the empirically-tuned `vector_sim_floor`
(`config.DEFAULT_VECTOR_SIM_FLOOR = 0.25`), the 180-day recency half-life, snippet-context expansion, and
the `produces_embeddings` FTS-only auto-degrade (`search.py:324`). All of it is regression-tested and
**eval-gated** — `brain eval --fail-below` exits 3 on an nDCG@5 / MRR / Recall@20 regression, enforced by
`.github/workflows/eval.yml`. A parallel SQL path in the UI would be invisible to that gate and would
drift silently. Test file 3 asserts the delegation.

**5. Server-rendered HTML pages (Jinja / htmx) — rejected.** `jinja2` is not installed and must not be
added. Beyond that, a full-page-reload model cannot show a live phase-split latency readout and cannot
preserve unsaved editor content across a filter change. It would also add a template-injection surface on
every page; a JSON API plus one hand-written, interpolation-free HTML shell means there is exactly one
template and it is a constant.

**6. Adding `workspace` / `category` columns — rejected upstream (R3).** Recorded here so the mapping in
§3.4 is not later mistaken for a compromise. Reserved tag namespaces reach ~90% of the behavior for zero
migration, and F5's facet counts are what actually make dropdowns useful.

**7. A filesystem-walked tree — rejected** in favour of the database-derived tree of §3.5. A walk would
re-implement `_walk_vault`'s exclusion rules (`sync.py:491`) and arrive without document ids, forcing a
second lookup per click.

### 9.3 Residual risks

**Concurrent writers — real, mitigated, not eliminated.** The vault watcher (`vault/watch.py`),
`brain-mcp`, and the UI can all write the same file and row. Mitigation is optimistic concurrency via
`body_hash` (§3.8). Residual: a watcher write landing between the UI's hash check and its `os.replace` —
a sub-millisecond window on a single-user machine. Not worth a lock.

**Full-corpus operations must never run from a request handler.** The recorded
`relink-derived ↔ watcher` deadlock caused hours-long `graph_entities` contention. `brain ui` must never
invoke `relink-derived`, `export_vault`, or any whole-vault operation from a handler. `sync_one_file` —
scoped to a single document — is the only sync the UI may call.

**Packaging — highest-probability failure.** See §2.1 and test file 10. The failure mode is a blank page
with no error, which is why it gets a dedicated test rather than a code review note.

**Drift into a second implementation of the brain.** The whole design is arranged against this:
`notes_service.py` is the *only* module permitted to mutate, and every operation delegates to a function
the CLI, MCP, or F8 already owns. Where behavior is worth sharing it is *extracted*
(`assert_within_vault`, `VALID_SOURCE_KINDS`) rather than copied. **Review rule:** reject any change that
adds SQL to a route module instead of `ui/queries.py`, or writes a vault file outside
`notes_service.py`.

---

## 10. Implementation sequence

Six waves, each independently reviewable, each ending green on `ruff check && mypy src/ && pytest`.

| Wave | Contents | Gate | Blocked by |
|---|---|---|---|
| **A — Foundations** | Extract `assert_within_vault` → `vault/paths.py` and `VALID_SOURCE_KINDS` → `source_kinds.py`, updating all callers; `pyproject` dependency promotion, package-data, `filterwarnings`. | Existing suite green; zero behavior change. | — |
| **B — Pure core** | `ui/tree.py`, `ui/render.py`, `ui/schemas.py`, `ui/errors.py`; test files 5 and 6 (pure halves). | ≥ 95% on all three. | A |
| **C — Server skeleton** | `ui/context.py`, `ui/security.py`, `ui/app.py`, `routes_meta.py`, `ui/queries.py`, `routes_tree.py`, `routes_search.py`; test files 2, 3, 6 (route half). | **All security tests pass before any write endpoint exists.** | B |
| **D — Write path** | `ui/notes_service.py`, `routes_notes.py`; test files 4, 7, 8. | Both tiers verified against the real test database. | C, **F8** |
| **E — CLI + packaging** | `ui/server.py`, `cli_ui.py`, `cli.py` registration; test files 9, 10, 11. | `brain ui` end-to-end against production, read-only first. | C |
| **F — Front end** | All of `static/`; browser pass at 1440 / 1024 / 768 / 375, both themes, keyboard-only, automated a11y check. | Manual QA against §1.2. | D, E |

Wave A is deliberately first and deliberately boring: it is the only wave touching existing code, so it
fails fast and in isolation. Wave C landing before Wave D is a **security requirement**, not a
preference — the guard must be proven before the first mutation endpoint exists.

**Follow-on obligations** (CLAUDE.md rule 11): update the auto-memory files after landing —
`cli.md` (the `brain ui` command and flags), `types.md` (`UiContext`, `TreeNode`, `SearchQuery`,
`NotePatch`, the `UiError` hierarchy), and `schema.md` **only if** F5 accepts the §9.1 constraint request.
Plus `README.md` and `docs/cli-reference.md`.
