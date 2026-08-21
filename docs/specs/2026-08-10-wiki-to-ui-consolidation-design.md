# Wiki → `brain ui` Consolidation Design

**Date:** 2026-08-10
**Status:** Draft — **all research landed**; no `TBD` sections remain. Awaiting decisions on Q1–Q18.
**Supersedes (on completion):** the Quartz static-site wiki as the primary reading surface
**Parent specs:**
[`2026-07-25-brain-ui-design.md`](./2026-07-25-brain-ui-design.md) (F14, the `brain ui` app),
[`2026-04-28-vault-model-design.md`](./2026-04-28-vault-model-design.md) (the two-tier vault model)

---

> **Evidence standard.** Every factual claim below carries a `file:line` citation read directly
> from source, or is labelled as a runtime measurement. Where a briefing that fed this document
> was wrong, §0.1 records the correction rather than propagating it. §7.1 and the 4.4 s figure in
> §10.2 are **measured against a live run**, not read from source — they are labelled as such.

> ⚠️ **This document is not under version control, and neither is any other spec in this
> repository.** `.gitignore:53` ignores `docs/specs/`, and `git ls-files docs/specs/` returns
> **zero** tracked files — including the parent spec `2026-07-25-brain-ui-design.md`. Meanwhile
> CLAUDE.md rule 11b *mandates* that all specs live in exactly that directory. Every design
> document in this project is therefore local-only and one clean checkout from gone.
>
> This is the same failure mode as the orphaned logo in §7.2, at a larger scale — and §7.2's
> argument applies verbatim: *a backup outside version control is not a rescue.* The phasing in
> §11 depends on this document surviving. **Resolving this is Q13, and it is not a
> wiki-retirement question — it affects the whole repository.**

---

## 0. The decision

**Port the wiki's reading experience into `brain ui`, then retire Quartz entirely.**

The rationale below is **recorded, not re-argued**. It was settled before this document existed.
A future reader who wants to reverse it should write a new spec engaging with §10 — particularly
§10.1, which states the case *against* this decision as strongly as it can be stated.

1. **Search quality is the whole argument.** The wiki's search is client-side fuzzy matching over
   a static `contentIndex.json` emitted at build time. `brain ui` delegates to
   `brain.search.hybrid_search` — the RRF-over-FTS+vector ranking core the
   `brain eval --baseline ci --diff --fail-below` gate protects (CLAUDE.md, "Eval gate (CI)").
   Only the latter can answer a paraphrased query. Two retrieval implementations over one corpus
   is a defect, and only one of them is measured.
2. **Quartz is structurally incapable of the rest.** It is a static site generator: no
   request-time server, no database connection, no write path. Live search, ingest, the agentic
   `brain ask` loop, per-request identity, and real authentication cannot be hosted there at any
   effort level. §3 records three concrete cases where the port **simplifies** existing features
   for exactly this reason.
3. **The machinery buys a property the user does not need.** The pinned upstream commit
   (`QUARTZ_PINNED_COMMIT`, `src/brain/wiki/__init__.py:5`), the npm install, the blue/green
   swap, the fastpath manifest, the parser cache, and the watcher daemon all exist to produce an
   **immutable, shareable, publishable artifact**. The user publishes to nobody.
4. **The cost is measurable.** `src/brain/wiki/` is **6,139 lines** across 15 modules;
   `src/brain/quartz_overrides/` is **73 files / 19,819 lines** (`__pycache__` excluded); `vault/quartz_overlay.py` adds
   236. Roughly **26,000 lines** — against **4,072 lines** that constitute the entirety of
   `brain ui` today (`ui/*.py` + `ui/static/*`). The wiki is six times the size of the thing
   replacing it.

**But see §10.1.** Points 1–4 are real and the decision stands, yet the honest counter-argument —
that we are trading *always-up* and *always-fast* for *editable* and *better-ranked* — is strong
enough that this spec states it plainly rather than burying it.

### 0.1 Corrections to briefings that fed this document

Recorded because a spec that silently absorbs a wrong premise propagates it.

- **`bin/brain-rebuild` is not wiki machinery.** `src/brain/maintenance.py:89-127` is an 8-stage
  orchestrator: `embeddings`, `summaries`, `search`, `graph`, `graph-weights`, `communities`,
  `connect`, `wiki`. Seven of eight stages are corpus maintenance unrelated to Quartz. The
  orchestrator survives (§9.1).
- **…and even the `wiki` stage is only 40% Quartz.** `maintenance.py:70-88` shows `wiki_steps`
  is five steps: `vault export`, `vault sync-summaries`, `vault prune-orphans`,
  `vault render --overlay`, `python -m brain.wiki.build_swap`. **Only the last two are
  Quartz.** The first three maintain the `_ingested/` mirror — which is precisely how Obsidian
  and vim see ingested content (§8.1). Deleting the stage wholesale would silently stop mirror
  maintenance and quietly break the external-editing workflow this document is trying to
  protect. §9.4 handles this: the stage is **re-formed**, not deleted.
- **Backlinks already have a backend.** An earlier briefing (and this document's first draft)
  claimed none existed. Wrong: `src/brain/vault/graph.py:163` provides `backlinks_for`, `:220`
  `outgoing_links_for`, `:305` `orphans`, and `:350` `graph_data(conn, root=, depth=,
  include_ingested=, include_derived=)` returning `GraphData` — 620 lines with zero CLI coupling.
  `vault/graph_format.py` adds pure `to_json:90` / `to_dot:147` / `to_mermaid:213`. A `brain ui`
  route can call these today with no new Python logic. This collapses what was scoped as a
  build-a-backend task into a wire-up task.
- **Confidential withholding is conditional.** `routes_search.py:58-59` sets
  `exclude_confidential = not ctx.serve_confidential_bodies`. On the default **loopback** bind
  confidential bodies **are** served and rows are **not** excluded; the exclusion, and the
  match-membership-oracle argument at `routes_search.py:42-55`, engage only on a non-loopback
  bind without `--include-confidential` (`cli_ui.py:126-133`).
  > ⚠ **INCOMPLETE as of 2026-08-20 — there are now TWO flags, and this bullet knows only one.**
  > `serve_confidential_bodies` still governs bodies exactly as described. A second flag,
  > **`serve_confidential_titles`**, now governs whether an *unprompted* listing may **name** a
  > confidential document, and it defaults **False** independently of the bind. See **Appendix B-18**.
- **`brain wiki` is a one-command sub-app** — only `install` (`cli.py:8415`). Its help string,
  "Wiki workspace management (Quartz install, Caddyfile rendering)" (`cli.py:351`), is where
  Caddy enters. The retirement surface is overwhelmingly `wiki/` + `quartz_overrides/`, not CLI.
- **~~The `brain.wiki` coupling is deeper than reported.~~ — THIS CORRECTION WAS ITSELF WRONG.**
  It claimed `graph_rag/build.py:236`, `vault/derived_links/directory.py:37`, `config.py:885`,
  `graph_rag/cross_type.py:165`, and `graph_rag/extract.py:483` all "reach into `brain.wiki`". A
  grep **restricted to import statements** finds that **only `graph_rag/build.py:236` is an
  import**. `directory.py:37` is a docstring; `config.py:885` and `extract.py:483` are comments;
  and `cross_type.py:165` does not mention `brain.wiki` at all. The original grep matched on the
  substring `wiki` anywhere in the file, including prose.

  **They are not nothing** — a docstring or comment naming a moved module becomes stale
  documentation and must be updated — but they are **not import breakage** and must not be used
  to size the move. The authoritative list is §9.2c. [⚠ **Appendix B-2 (S8)** — §9.2c is **stale**, and this is the **third** site directing a reader to it; B-2 named only the two inside §9.2. See the 2026-08-20 re-derivation for the current list.]

- **Meta: treat "verified" in this document as a claim to check, not a guarantee.** The bullet
  above, the People Hub disposition (§9.2), and the Mermaid row (§3.4.1) were each stated with the
  vocabulary of verification and each turned out wrong, in three distinct ways: a grep that
  matched comments as code, a DB assertion carried forward instead of re-run, and an overlay
  mistaken for the composed product. §9.3 trap 1 already warns that "grep-based sweeps are unsafe;
  every deletion is reviewed individually against its importers" — and these are three places this
  document broke its own rule. The failure mode is **over-confident sourcing**, and it is the
  reason §9.2b insists the phase-9 check is `python -c "import brain.cli"` rather than a reviewer's
  judgement.

---

## 1. Scope, non-goals, and what "retired" means

### 1.1 In scope

- Bringing `brain ui`'s **reading** experience to parity-or-better with the wiki, for the
  capabilities §3's matrix marks **PORT**.
- The **markdown-rendering gap** (§5) — a correctness blocker, not polish.
- **Graph and link surfaces** (§6), including backlinks and related-docs.
- **Branding, visual identity, and the rescue of an at-risk uncommitted design asset** (§7).
- The **external-editing and write-concurrency contract** (§8).
- **Honest treatment of what is lost**: availability, latency, LAN access, no-JS reading (§10.1).
- A **staged retirement** that never leaves the user without a reading surface (§9, §11).

### 1.2 Non-goals

- **Publishing to anyone.** No hosting, no static export for third parties, no remote auth. If
  that need returns it returns as a new spec, and the honest answer then is "re-add a static
  exporter", not "keep Quartz alive on the chance".
- **Re-litigating the decision.** See §0 and §10.1.
- **Un-deferring the Ingest and Agent tabs** (`2026-07-25-brain-ui-design.md` §1.1;
  `index.html:77-101`). The **Publish** tab is different — retirement makes its copy false (§9.6).
- **Any change to `hybrid_search` ranking.** See §2.2.
- **Porting features the wiki does not actually have.** §3.4 records three Quartz/Obsidian
  staples confirmed *absent* from this deployment. Their absence is a finding, not a backlog.

### 1.3 What "retired" means, precisely

A **five-state ladder**; each rung is a separate, reversible decision. This spec commits to
**R3**. **R4** requires an explicit user request.

| State | Name | Meaning |
|---|---|---|
| **R0** | *Today* | Wiki is the primary reading surface. |
| **R1** | **Demoted** | `brain ui` is primary. The wiki still builds and serves; nothing in the daily loop points at it. |
| **R2** | **Dormant** | Quartz build steps off by default; `com.brain.build` daemon uninstalled. Code present, tests green. The vault-mirror steps of the old `wiki` stage keep running (§0.1, §9.4). |
| **R3** | **Deleted** | `wiki/`, `quartz_overrides/`, `quartz_overlay.py`, the Quartz build steps, `brain wiki install`, Caddy, the wiki-only `bin/` wrappers and plists, and ~41 test files removed. **Preceded by the moves of §9.2.** Vault untouched. |
| **R4** | **Vault-format simplification** | *Only on request.* Removing vault-format concessions that exist purely for Quartz. **Not** on the default path — the vault is read by Obsidian and vim too (§8). |

**Non-negotiable invariant across all states:** the `.md` files in the vault and the Postgres
corpus are never modified by the retirement. Deleting a renderer must not touch its input.

---

## 2. Inherited and binding constraints

### 2.1 Zero new runtime dependencies — and it is *enforced*, not merely asserted

`starlette`/`uvicorn` are promoted transitives adding no wheels; `fastapi` and `jinja2` are
forbidden; **no Node, no npm, no bundler, no CDN**, and the front end must work fully offline
(`2026-07-25-brain-ui-design.md` §"Inherited constraints" ¶1).

This is not aspirational. `tests/test_ui_static_assets.py:123-137` walks the entire static tree
and **fails on any line containing `http://` or `https://`**, with the docstring "The offline
guarantee, enforced rather than asserted in prose. A single CDN reference would…". Any proposal
in this document that reaches the network is not merely against policy — it turns the suite red.

This is the single most consequential constraint here. It is why the graph (§6) and the wiki's
typography (§7.3) are genuinely hard: the obvious answer to both is a network fetch.

### 2.2 Search must delegate to `hybrid_search` — always

`routes_search.py:64-77` passes `vector_sim_floor`, `recency_halflife_days`, and
`snippet_context_tokens` from `ctx.cfg` so ranking is identical to `brain search`.
`ui/queries.py:1-7` states the rule in source: that module is **the only place in the package
allowed to contain SQL**, because "a route that grows its own query is a review rejection — that
is exactly how a second, un-eval-gated implementation of the brain starts."

**Binding:** no feature here may introduce a retrieval path bypassing `hybrid_search`. New
*non-ranking* reads (backlinks, graph adjacency, people aggregation) are permitted and belong in
`ui/queries.py` or delegate to existing pure modules.

### 2.3 Mutation is confined to one module, which delegates rather than implements

`notes_service.py:1-30` is the only module permitted to mutate; it delegates to
`vault.note_builder`, `vault._atomic.atomic_write_text:50`, `vault.sync.sync_one_file`,
`ingest.update_document`, `vault.rename`, `vault.delete`. Its docstring records the rule §6 will
be tempted to break:

> Full-corpus operations must never run from a request handler: the recorded `relink-derived` ↔
> watcher deadlock caused hours of `graph_entities` contention. `sync_one_file` — scoped to
> exactly one document — is the only sync this module may call.
> — `notes_service.py:26-30`

**Binding.** Verified still true: `sync_one_file` is the only sync entrypoint imported into
`ui/`. A graph panel that triggers `graphrag build`, or a related-docs panel that triggers a
corpus-wide embedding pass, is forbidden.

### 2.4 Optimistic concurrency exists, works, and must be extended rather than bypassed

Mandatory `body_hash` on every note `PUT` (`schemas.py:193-216`); mismatch → `UiConflict` /
`stale_write` (`notes_service.py:285-289`). §8.2 verifies this is computed from a **fresh disk
read**, not a cached or DB value — which is what makes it actually correct.

### 2.5 The security model is a floor

CSP `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
(`security.py:62-71`) — sustainable **only** because the shell has no inline script or style
(`index.html:8-17`). Plus the Origin/`Sec-Fetch-*`/Content-Type/token/read-only guard
(installed by `build_middleware`, `security.py:222`), anti-DNS-rebinding (`security.py:6`), method-explicit routing
(`app.py:128-162`), conditional confidential withholding (§0.1) **— now two flags, not one; see
Appendix B-18 —** and errors that leak only a class name (`app.py:90-106`).

`img-src 'self' data:` (`security.py:66`) already permits the logo of §7 with no CSP change.
**But fonts are a different directive** — see T1/Q14.

### 2.6 Where this port strains the constraints

| Strain | Constraint | Decided in |
|---|---|---|
| Interactive force-directed graph (Quartz uses the full `d3` bundle + PixiJS + tween.js) | §2.1 — hard | §6.3, Q2 |
| Wiki typography (Geist via Google Fonts CDN) | §2.1 — **would fail `test_ui_static_assets`**; needs `font-src 'self'` | §7.3, **T1/Q14** |
| Mermaid / LaTeX rendering | §2.1 | §5.4 — **live decision**, not dissolved (§3.4.1) |
| "Open in external editor" | §2.5 — new process-spawn primitive | §8.5, Q4 |

**This spec does not silently choose on any of these.** §12 collects them.

---

## 3. Feature parity matrix

Dispositions: **PORT** (before demotion, R1) · **PORT-LATER** (before deletion, R3) ·
**EXISTS** (equal or better already) · **DROP** (deliberate, with reason) ·
**DECIDE** (needs the user).

### 3.0 Provenance of this matrix — read this before trusting a row

The first version of this matrix was built from an **overlay-only grep**, which is a diff and not
the product (§3.4.1). It has since been re-derived by walking the **composed** wiki's own
declarations:

- **`quartz.config.ts:121-284`** — every `Plugin.*` entry in the transformer, filter, and emitter
  lists.
- **`quartz.layout.ts:74-311`** — every `Component.*` entry in the shared and page layouts.

That pass **added three capabilities this matrix had missed entirely** (§3.1a) and is the reason
§3.4.1 exists. Rows are tagged in the **Src** column with how each was established:

| Tag | Meaning |
|---|---|
| **`cfg`** | Declared in `quartz.config.ts` / `quartz.layout.ts` — the composed product. Highest confidence. |
| **`py`** | Established from Python source or the live database. Highest confidence. |
| **`ovl`** | **Rests on the overlay only.** Re-derive against the composed wiki before the phase it gates. |

> **Honest limitation.** The `cfg` walk enumerates what the wiki *declares*. It does not measure
> what the user *uses* — only the corpus can do that, and it was consulted for the rows where it
> was decisive (People Hub: 51 docs / 537 edges; aliases: 0; mermaid: 2). Rows without a corpus
> measurement are dispositions on declared capability, not on observed behaviour.

### 3.1 Navigation and discovery

| Wiki capability | Implementation | Src | Disposition | Notes |
|---|---|---|---|---|
| Hybrid search | `Search.tsx`, `search.inline.ts`, client-side fuzzy over `contentIndex.json` | `cfg` | **DROP** → **EXISTS** | Replaced by `hybrid_search` (`routes_search.py`). The point of the exercise. |
| **Command palette** (Cmd/Ctrl-P) | `CommandPalette.tsx` + `commandPalette.inline.ts` | `cfg` | **PORT** | Primary fast-nav; no stock equivalent. `brain ui`'s ⌘K focuses the search box (`index.html:56`) — that is not a palette. Real work. |
| **Explorer** — folder tree, per-folder counts, persisted "Show ingested" toggle, per-source month grouping (`Mon D · Title`) for krisp/gmail | `Explorer.tsx` + `explorer.inline.ts` | `cfg` | **PORT** | `ui/tree.py` + `routes_tree.py` give the tree; **counts, the ingested toggle, and month grouping do not exist.** |
| Breadcrumbs | `Component.Breadcrumbs` (`layout.ts`) | `cfg` | **PORT-LATER** | Cheap; derivable from `vault_path`. |
| SPA navigation | stock | `cfg` | **EXISTS** | `brain ui` is an SPA by construction — though see §10.4 on Back/Forward. |
| Popovers (link-hover preview) | stock | `ovl` | **PORT-LATER** | Core Obsidian affordance, zero custom code upstream. Server-side render already available on the note fetch. |
| Reader mode, reading time | `Component.ReaderMode`, `Component.ContentMeta` (`layout.ts`) | `cfg` | **PORT-LATER** | Low cost, low risk. |
| RSS, sitemap | stock, enabled | `cfg` | **DROP** | `baseUrl` is localhost; publishes to nobody. |

#### 3.1a Three capabilities the overlay-only pass missed entirely

Found by the §3.0 `cfg` walk. **None appeared anywhere in the previous 1,237 lines** — verified by
`grep -i "tableofcontents\|folderpage\|aliasredirect"` returning zero hits.

| Wiki capability | Implementation | Src | Disposition | Notes |
|---|---|---|---|---|
| **Table of contents** | `Plugin.TableOfContents()` (`quartz.config.ts:227`) + `Component.DesktopOnly(Component.TableOfContents())` (`quartz.layout.ts:285`) | `cfg` | **PORT** | **A right-sidebar TOC on every page is a first-order reading affordance**, and losing it silently at R3 is exactly the R-6 regression this document is structured to prevent. `brain ui` has nothing. Natural home: §7.1.5's marginalia column, beside backlinks. Server-side: headings are already parsed by `render.py`, so this is a token walk, not a new dependency. |
| **Folder index pages** | `Plugin.FolderPage()` (`quartz.config.ts:276`) | `cfg` | **PORT-LATER** | §3.2 listed `TagPage` but not this. Clicking a folder in the wiki yields a real page listing its contents; in `brain ui` a folder is a tree node that only expands. Cheap given `ui/tree.py` already has the data. |
| **Alias redirects** | `Plugin.AliasRedirects()` (`quartz.config.ts:273`); frontmatter `aliases` parsed at `vault/sync.py:667` into `metadata['aliases']` (`:1057`) | `cfg` + `py` | **DROP — measured** | **Corpus count: 0.** Read-only query — `SELECT count(*) FROM documents WHERE metadata ? 'aliases'` returns **zero**. The machinery is wired end-to-end but **no document in the corpus uses it**, so nothing redirects today and **no link breaks at retirement**. The parsing in `sync.py` survives regardless (it is vault-tier, not Quartz). |

**The alias result is the clearest vindication of doing this pass properly**: had it come back
non-zero, R3 would silently break every inbound link to an aliased note, and no other section of
this document would have caught it.

### 3.2 Content presentation

| Wiki capability | Implementation | Src | Disposition | Notes |
|---|---|---|---|---|
| Callouts / admonitions | stock OFM (`quartz.config.ts:129`) | `cfg` | **PORT** | §5. |
| Syntax highlighting | `Plugin.SyntaxHighlighting()` (`quartz.config.ts:125`) | `cfg` | **PORT** | §5. |
| **Auto-summary TL;DR lede** | `SummaryLede.tsx`, `Component.SummaryLede` | `cfg` | **PORT** | `documents.summary` already populated; pure presentation. |
| **Link-kind visual system** — 5 treatments (wiki / external / tag / ingested / derived) | `Plugin.LinkKindMark()` (`quartz.config.ts:149`) + `_links.scss` | `cfg` | **PORT** | Explicitly fixed the "every link looks identical" complaint. `render.py:48-49` has only `wikilink` / `wikilink--unresolved`. |
| **Email-thread reading mode** — newest open, older collapsible, "show only my replies" | `Plugin.EmailThreadReader()` (`quartz.config.ts:213`) | `cfg` | **PORT — and simplify** | Bakes `BRAIN_USER_EMAIL` in at **build time** because Quartz has no per-request identity. A live server reads config per request. **The port removes a hack.** |
| **Backlinks as marginalia** (Tufte side-notes ≥1280px) | `Component.Backlinks` (`quartz.layout.ts`) | `cfg` + `py` | **PORT** | Backend exists: `vault/graph.py:163` `backlinks_for`. §6.2. |
| **Related-docs panel** — precomputed hybrid FTS+vector RRF per page, lazy-fetched, fails closed | `Component.RelatedDocs` + `wiki/build_related.py` | `cfg` + `py` | **PORT — and simplify** | Static site must **precompute** into `static/related/<slug>.json`. A live server computes on request. §9.2 moves the scoring logic to `src/brain/related.py`. ⚠ **Scoring landed; the panel did not** — Appendix B-7/B-17, deferred to phase 5. |
| **Home "Recently captured" rail** — 12 most-recent ingested, absolute date baked server-side, relative text recomputed client-side so it never decays | `build_homepage.py` + `Plugin.RelativeDate()` (`quartz.config.ts:178`) | `cfg` + `py` | **PORT** | The never-decays trick is a *static-site workaround*; a live server can just send fresh data. Keep the affordance, drop the mechanism. |
| **Tag listing pages** | `Plugin.TagPage()` (`quartz.config.ts:277`) + `pages/TagContent.tsx` | `cfg` | **PORT** | `brain ui` has a tag *filter* but no tag *index*. |
| **People Hub pages** | `wiki/build_people.py` emit tail | `py` | **SURVIVES AS-IS + additive live view** | The emitter writes **into the vault** (`cli.py:6944`): **51 live `kind='vault'` documents, 51 inbound and 537 outbound `links` rows**. It is not Quartz output. It moves with the aggregation half (§9.2); `ui/routes_people.py` is an *additional* way to read the same data. |
| Source iconography — gmail 📧, krisp 🎙️, slack 💬, manual ✍️, vault 🌱 | `util/sourceIcons.ts` | `ovl` | **PORT** | Trivial; `brain ui` has none. High recognition value. |
| SVG paper-grain overlay | both surfaces | `ovl` | **EXISTS** | Independently implemented on both; `security.py:66` already allows `data:` for it. |

### 3.3 Graph

Covered in full in §6. Summary: **PORT the document-link graph** (backed by `vault/graph.py`),
**DECIDE** the render technology (Q2) and the feature subset (Q3), **DROP** the stock-view
toggle (a dev/diff affordance), **DECIDE** the graph diagnostic workbench (Q6).

### 3.4 Confirmed **absent** — do not scope work for these

A finding, not a backlog. Each is a Quartz/Obsidian staple that this deployment does **not** use:

| Feature | Status |
|---|---|
| **Mermaid** | ~~Not present~~ — **THIS ROW WAS WRONG. Mermaid IS enabled.** See §3.4.1. |
| **Image lightbox** | Not present. |
| **Print styles** | Not present. |

#### 3.4.1 Correction — Mermaid **is** present, and the method that missed it was flawed

**`quartz.config.ts:129` enables `Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false })`,
and the overlay contains no `ofm.ts` override** (verified: `quartz/plugins/transformers/` holds
`codeCopy`, `derivedFenceMark`, `emailThread`, `emptyDoorFilter`, `index`, `latex`, `linkKindMark`,
`linkSourceTag`, `relativeDate`, `reloadSignal` — no `ofm.ts`). So **stock Quartz's transformer
runs**, and stock defaults `mermaid: true` (`ofm.ts:52`), rendering fences plus an expand modal
(`ofm.ts:523-777`). **Two real ingested documents contain mermaid fences.**

**Root cause, recorded because it invalidates a method and not just a row:** the inventory grepped
`quartz_overrides/` — *the overlay* — instead of the **composed** wiki (stock Quartz + overlay).
The overlay is a diff, not the product. Print styles and image lightbox happen to be absent from
*stock* too, so the method error produced correct answers on those two rows and read as
corroboration.

> **Consequence, and the remediation actually performed.** An earlier revision of this paragraph
> claimed the re-verified rows "are marked in place" — **they were not; no row carried any mark,
> and the claim was false.** It has now been done: §3.0 records the composed-wiki walk, every row
> in §3.1–§3.3 carries a **Src** tag (`cfg` / `py` / `ovl`), and §3.1a adds the three capabilities
> the overlay-only pass had missed. **Rows still tagged `ovl` rest on the overlay alone and must
> be re-derived before the phase they gate.** Mermaid returns to §5.4 as a live decision and Q7
> loses the word "permanently" for that row.
>
> Recorded rather than quietly fixed, because a document asserting remediation it had not
> performed is precisely the failure §0.1's meta-bullet names — and it occurred *one section
> after* that bullet was written.

**Image lightbox and print styles remain confirmed absent** — absent from both stock and overlay —
and need only the Q7 confirmation that this is intentional.

### 3.5 Drop list

| Capability | Reason |
|---|---|
| Blue/green swap + polling reload (`reload.js`, ETag, Caddy) | Solves "the static bundle changed underneath you". A live server has no such problem. |
| Graph "stock view" toggle | A dev/diff affordance for comparing against upstream Quartz. |
| `_cmdk.scss` | A 6-line vestigial shim. |
| RSS + sitemap | Publishes to nobody. |
| `contentIndex.json` and every emitter feeding it | Replaced by live queries. |

---

## 4. Architecture — what gets added, and where

### 4.1 Requirement A1 (blocking): repair the front-end seam first

`brain ui`'s front end is **one 621-line `app.js` and one 471-line `app.css`**, not the modular
split its own design document specified (`2026-07-25-brain-ui-design.md` §2 lists twelve files:
`css/tokens|base|layout|components.css`, `js/api|store|tree|results|inspector|keys|main.js`).

This document proposes adding a command palette, an explorer with counts and grouping, a graph
panel, backlinks, related-docs, a people view, a tag index, source icons, and branding. Every one
of those would land in the same file.

> **A1.** Split `static/app.js` and `static/app.css` into the F14-designed modules **before** any
> new surface lands. Pure refactor, no behaviour change — cheap to review, cheap to revert.

Mechanics: `index.html:20` already loads `app.js` as `<script type="module">`, so native ES
imports work with no bundler. **Packaging is in better shape than an earlier revision implied:**
`pyproject.toml:455` already declares the html/css/js globs, and `test_ui_static_assets.py:59-67,
77-98` already **auto-enforces** — via `rglob` — that every shipped asset matches a `package-data`
glob. That guard fires by itself the moment `js/*.js` lands; no test change is needed for the
split. What *is* missing is `*.png` / `*.svg` / `*.ico` for §7.4's logo, and CSS `url()` parsing
for §7.3's fonts (T5/Q18). This repo has already shipped one
broken wheel for exactly this class of mistake (`app.py:30-41`, commit `ed8195f`).

### 4.2 Attachment points

Nothing here invents an architectural layer.

| Addition | Server | Client | Rule honoured |
|---|---|---|---|
| Backlinks, outgoing links, orphans | `vault/graph.py:163,220,305` via a new `routes_links.py` | inspector / marginalia | §2.2 — non-ranking read, existing pure module |
| Document-link graph | `vault/graph.py:350` `graph_data()`; serialize via `graph_format.to_json:90` | new `js/graph.js` | §2.3 — reads only, never builds |
| Related docs | `src/brain/related.py` after the §9.2 move — **module exists; no route calls it** (B-7/B-17) | inspector panel — **not built; phase 5** | §2.2 — reuses the same FTS+vector RRF scoring |
| People view | `wiki/build_people.aggregate_people` after the §9.2 move | new route + view | §2.3 — read-only aggregation |
| Richer markdown | `render.py` `build_renderer():137-148` | none (server-rendered) | §2.1 |
| Logo, favicon, source icons | static assets | CSS | §2.5 — `img-src 'self' data:` already allows |
| Command palette, explorer counts, tag index | existing routes + small reads | new modules | §2.2 |
| Open in external editor | §8.5 | inspector button | §2.5 — treated as its own security class |

### 4.3 What must **not** be added

- A second search index of any kind (§2.2).
- Any route triggering a full-corpus job — `graphrag build`, `relink-derived`, unscoped
  `vault sync`, `brain-rebuild` (§2.3).
- Any inline `<script>` or behaviour-bearing `style=""` (§2.5).
- Any network fetch from a static asset — it fails `test_ui_static_assets.py:137` (§2.1).

---

## 5. The markdown-rendering gap

### 5.1 The gap

`render.py:144` constructs the parser as `MarkdownIt("commonmark", {"html": False,
"linkify": False})` — **strict CommonMark**, which excludes:

| Feature | `brain ui` | Wiki | Disposition |
|---|---|---|---|
| Tables | ✗ literal pipes | ✓ | **PORT** |
| Strikethrough | ✗ | ✓ | **PORT** |
| Task lists (`- [ ]`) | ✗ literal brackets | ✓ | **PORT** |
| Footnotes | ✗ | ✓ | **PORT** |
| Callouts / admonitions | ✗ | ✓ | **PORT** |
| Syntax highlighting | ✗ | ✓ | **PORT** |
| `![[embeds]]` | ✗ (`render.py:81-101` handles `[[…]]` only) | ✓ | **PORT-LATER** |
| **LaTeX — block math** | ✗ | ✓ | **PORT** |
| **LaTeX — inline single-`$`** | ✗ | ✗ **deliberately disabled** | **PRESERVE THE DISABLE** — §5.4 |
| Mermaid | ✗ | **✓ enabled** (stock OFM, `quartz.config.ts:129`) | **DECIDE — Q7.** 2 ingested docs contain fences. |

Task lists are not cosmetic: the auto-memory rule `feedback_person_page_meeting_inline.md`
mandates `- [ ]` action items in every person-page meeting entry, and `brain todo` reads them.
Rendering those as literal text on the surface replacing the wiki is a day-one parity failure.

> **VERIFIED 2026-08-11 — four corrections to the table above, plus the corpus counts it never
> carried.** Every "`brain ui`" cell was checked by **running `render_markdown()`** on the
> construct, not by reading `render.py`. Every count was taken **twice and independently**: a
> read-only `SELECT` over `documents` (1,393 rows: 1,257 ingested + 136 vault) and a filesystem
> scan of the on-disk vault at `brain.config.DEFAULT_VAULT_PATH` (1,400 content files: 142
> authored + 1,258 `_ingested`, **excluding `.quartz/` and `node_modules/`** — see the warning at
> the end of this block). The two agree on every row.
>
> | Feature | Docs affected (disk / DB) | Correction |
> |---|---|---|
> | Tables | **468 / 467** (461 `_ingested`, 7 authored) | biggest gap in the corpus; **free** to fix |
> | Task lists | **145 / 145** (124 / 21) | count confirms the "day-one parity failure" framing |
> | Strikethrough | **38 / 38** (all `_ingested`) | **free** to fix |
> | Footnotes | **3 / 3** | needs a new wheel for 3 documents |
> | Callouts | **1 / 1** (authored) | needs a new wheel — and possibly a custom rule (§5.2) |
> | Mermaid | **2 / 2** (both `_ingested`) | **already renders** — see §5.4 |
> | Block math | **0 / 0** | **zero usage** — see §5.4 |
> | `![[embeds]]` | **0 / 0** | renders **wrongly today** — see §5.4 |
>
> **(a) "Syntax highlighting — `brain ui` ✗" is misleading; it is the CHEAPEST item here, not the
> hardest.** `brain ui` **already emits** `<pre><code class="language-python">` — verified by
> running the renderer. Only *tokenisation* is missing. And **pygments 2.20.0 is already installed
> transitively**: `rich` (a declared dependency) requires `pygments>=2.13.0,<3.0.0`. That is
> exactly the situation `pyproject.toml` already documents for `starlette`/`uvicorn` — *"they
> resolve transitively via `mcp` today, so declaring them adds zero new wheels"* — so the
> precedent for declaring it is already written in this repository. It is also the one PORT item
> that genuinely lands in `build_renderer()` as §5.2 claims, because `highlight` is a `MarkdownIt`
> constructor option.
>
> **(b) The `![[embeds]]` citation is wrong, and the behaviour is worse than "not handled".** The
> function is **`_wikilink_inline_rule` at `render.py:72-101`**, not `:81-101` (`:81` is the
> `startswith("[[")` line *inside* it). And `![[Some Note]]` does not degrade to literal text — it
> renders a literal `!` followed by a **live unresolved-wikilink anchor**:
> `!<a class="wikilink wikilink--unresolved" title="no note matches this link">Some Note</a>`.
> That is a **visible rendering defect today**, not a no-op. Corpus usage is 0, so PORT-LATER
> stands as a priority call, but the defect belongs on the D-list of §7.1.3.
>
> **(c) Block math and Mermaid dispositions contradict each other** — corrected in §5.4.
>
> **(d) Phase 1's exit criterion (§11) is scoped wrong** — see the standalone note at the end of
> §5. Not corrected in §11 itself, which is outside this pass's edit scope.
>
> **METHOD WARNING for anyone re-running these counts.** A naive `rglob("*.md")` over the vault
> sweeps `.quartz/node_modules/`, which contains the `katex`, `remark-math` and
> `micromark-extension-math` READMEs. My first pass did exactly that and reported **7 files
> containing `$$`** — every one of them tooling documentation, not content. Excluding
> `.quartz/` drops it to **0**. Any count of this vault that does not exclude `.quartz/` is wrong
> in the direction of inventing usage.

> **VERIFIED 2026-08-11 — the `_wikilink_inline_rule` citation, and why correction (b) above is
> now stale through no fault of its own.**
>
> **Current value: `_wikilink_inline_rule` spans `render.py:77-117`.** Measured by AST walk
> (`ast.FunctionDef.lineno`/`end_lineno`), not by grep — a grep finds the `def` line but cannot
> give the end, and the end is what a range citation claims.
>
> **Correction (b) above says `:72-101`. That was CORRECT WHEN WRITTEN, and phase 1's own
> implementation staled it.** Reconstructed from git rather than inferred:
>
> | | span | why |
> |---|---|---|
> | spec §5 original | `:81-101` | **genuinely wrong** — `:81` is the `startswith("[[")` line *inside* the function |
> | correction (b), at `HEAD` | `:72-101` | **correct at the time** — confirmed by AST-walking `git show HEAD:src/brain/ui/render.py` |
> | now | `:77-117` | phase 1 added 5 import lines above it (`pygments` ×4, `mdit_py_plugins` ×1), shifting the start 72→77, and ~11 docstring lines inside it for the embed fix, extending the end 101→117 |
>
> **The lesson is not "nobody checked" — it is that a line-range citation decays under any edit
> above or inside it, silently, and the person most likely to stale one is whoever is editing that
> file.** That happened twice in this phase: this citation, and a `pyproject.toml` comment citing
> `markdown-it-py 3 -> 4.2` that phase 1's own floor raise invalidated. Both were caught by the
> author of the change rather than by a later reader, which is the only point at which it is cheap.
>
> Prefer citing a **symbol name** over a line range where the prose allows it; `_wikilink_inline_rule`
> is stable across every edit in this phase, while its line numbers moved twice.

> **VERIFIED 2026-08-12 — §8.3's `localStorage` draft buffer is REFUSED, not
> pending. Do not implement it.**
>
> §8.3 prescribes persisting the in-progress edit to `localStorage` so a draft
> survives a conflict. **Phase 4 refused it on privacy grounds, and the refusal
> was ruled and upheld rather than deferred.**
>
> `UiContext.serve_confidential_bodies` deliberately WITHHOLDS document bodies
> when the server is not bound to loopback — the same control MCP `brain_show`
> enforces with `include_confidential`. Writing the body to `localStorage`
> persists it past the session, on disk, outside that control. **A privacy
> control that a convenience feature can bypass is not a control.**
>
> The problem §8.3 was solving is solved differently and without persistence:
> the edit already survives a 409 in memory (measured — it strands rather than
> discards), `confirmDiscardDraft` stops the reopen destroying it, and
> `overwriteOnDisk` provides an exit that keeps it. **Nothing is pending here.**
>
> What would reverse this: a design conversation with the withholding behaviour
> explicitly on the table — e.g. buffering only for documents whose sensitivity
> is not confidential, or only when loopback-bound. Not a phase-4 line item.

### 5.2 Every addition lands in one function

`build_renderer()` (`render.py:137-148`) is the single construction point — deliberately a
function, not a module singleton, "because `MarkdownIt` instances carry mutable rule state"
(`render.py:139-143`). Additions are plugin registrations there; no other module changes.

> **VERIFIED 2026-08-11 — "no other module changes" is FALSE for four of the eight PORT items,
> and the change it actually requires collides with §2.1.**
>
> **Spec said** every addition is a plugin registration in `build_renderer()`. **Actual:** the
> eight PORT items split cleanly in two, and only one half is free.
>
> | Feature | What it actually needs | New wheel? |
> |---|---|---|
> | Tables | `md.enable("table")` — **native to markdown-it-py** | **No** |
> | Strikethrough | `md.enable("strikethrough")` — **native** | **No** |
> | Syntax highlighting | `highlight=` constructor option (see §5.1 correction) | **No** |
> | Task lists | `mdit_py_plugins.tasklists` | **YES** |
> | Footnotes | `mdit_py_plugins.footnote` | **YES** |
> | Callouts | `mdit_py_plugins.admon` — *and see the caveat below* | **YES** |
> | Block math | `mdit_py_plugins.dollarmath` + a display engine | **YES** |
>
> **`mdit_py_plugins` is neither installed nor declared.** Verified by import: all five of
> `mdit_py_plugins.{footnote,tasklists,dollarmath,admon,attrs}` raise `ModuleNotFoundError` in
> this venv, and `pyproject.toml` declares only `markdown-it-py>=3.0`.
>
> **This collides with §2.1**, which this document calls binding *and enforced*. §5 never mentions
> it, so an executor discovers it mid-phase — after the free items are already merged.
>
> **The ask is narrower than a new dependency, and that distinction matters.** `markdown-it-py`
> declares `mdit-py-plugins>=0.5.0` as its **`plugins` extra**, so the change is
> `markdown-it-py[plugins]>=3.0` — an extra of an already-declared dependency, not a wholly new
> package. It is still a new wheel in the resolved tree and still needs an explicit §2.1 waiver.
>
> **Raised as Q19 (§12)**, because it is *one* decision gating *four* items and it must be settled
> before phase 1 opens rather than inside it.
>
> **NOT VERIFIED — the callout row may be worse than a plugin registration.** The wiki's callouts
> are Obsidian `> [!note]` syntax via OFM (`quartz.config.ts:129`). `mdit_py_plugins.admon`
> implements `!!!` admonition syntax, which is **not the same grammar**. If that holds, callouts
> need a **custom block rule**, not an off-the-shelf plugin — materially larger than §5.1's
> one-line "PORT". This was **not confirmed**: the package is absent and no package was installed
> into the shared venv to check. Confirm before sizing. Note the corpus stake is **one authored
> document** (see §5.1 correction), so "do not port" is a live option.

### 5.3 Requirement A3: every addition ships a mutation-verified XSS test

`render.py:10-28` documents three hardening measures, the first carrying a warning that governs
how this section is executed:

> `html=False`. **This is not the preset default** — verified on markdown-it-py 4.2.0,
> `MarkdownIt("commonmark")` alone renders a literal `<script>` tag straight through… **(The F14
> design document states the opposite; the document is wrong and the code follows the
> measurement.)** — `render.py:11-18`

A design document was wrong about a security default and only a written test caught it. That is
auto-memory `feedback_prove_the_check_can_fail.md` exactly: ~12 confirmed guards in this repo
did nothing.

> **A3.** Every plugin added to `build_renderer()` ships (a) a rendering test and (b) a dedicated
> XSS test that **fails** against a deliberately-unhardened variant — mutation-verified, not
> merely written.

The sharp ones are attribute-emitting plugins (callouts, footnotes, task lists) and URL-emitting
plugins: `_render_link_open` (`render.py:125-134`) enforces the scheme allowlist for *links*, but
a plugin emitting its own anchor or `<img>` bypasses that render rule entirely.

### 5.4 The three formerly-hard cases, largely dissolved by evidence

**Mermaid — LIVE DECISION (Q7), not a drop.** An earlier revision recorded this as "confirmed not
present"; **§3.4.1 corrects that** — stock OFM runs with `mermaid: true` and two ingested documents
carry fences. So the options tree is **not** moot:

| Option | Cost | Constraint impact |
|---|---|---|
| **(a) Render the fence as a labelled code block** | Zero. Two documents show diagram *source* instead of a diagram. | None. |
| **(b) Vendor a JS renderer** | Large vendored artifact, manual security updates | Against the spirit of §2.1; needs an explicit waiver. |
| **(c) Server-side pre-render to inline SVG** | Needs a renderer reachable from Python | Violates §2.1 absent a pure-Python option. |

**With n=2 the honest recommendation is (a)** — but that is now a *decision informed by evidence*,
not a finding of absence. If those two documents matter to the user, (b) becomes arguable.

> **VERIFIED 2026-08-11 — option (a) is not a work item with "zero cost". It is the STATUS QUO.**
>
> **Spec said** option (a) costs zero and shows diagram source instead of a diagram. **Actual:**
> `brain ui` **already does exactly that, today.** Running the renderer on a ` ```mermaid ` fence
> yields `<pre><code class="language-mermaid">graph TD; A--&gt;B;</code></pre>` — a labelled code
> block with the source escaped. There is nothing to implement for (a); Q7 is a choice between
> *keeping current behaviour* and *adding a renderer*, which is a materially easier question than
> the table implies.
>
> The n=2 count is **confirmed** — both documents are `_ingested`, found identically by the DB
> query and the disk scan:
> `_ingested/manual/<redacted-doc-1>.md` and
> `_ingested/manual/<redacted-doc-2>.md` (filenames redacted — PII policy).

**LaTeX — port block math, and *preserve the inline disable as a decision*.** The wiki enables
block math and **deliberately disables inline single-`$`**, because the corpus is prose about
money and single-`$` was swallowing constructions like "costs \$5 … saves \$12" into math spans —
**measured at 551 spans across 171 pages**. This is a hard-won, corpus-specific finding.

> **A5.** Any LaTeX support in `brain ui` enables **block math only**. Inline single-`$` math
> stays off, with a comment citing the 551-span measurement. Adopting a plugin's stock defaults
> silently regresses this, and the regression is invisible until a reader notices mangled prose.
> This requires its own test: a fixture containing `$5` and `$12` in one sentence must render as
> literal text.

> **VERIFIED 2026-08-11 — block math has ZERO corpus usage, and its PORT disposition contradicts
> §5.4's own Mermaid reasoning.**
>
> **Spec said** block math is a **PORT** (§5.1 table). **Actual: 0 documents use `$$`** — measured
> twice, `SELECT count(*) … content LIKE '%$$%'` over 1,393 DB rows returns **0**, and the disk
> scan of 1,400 vault content files returns **0**. (`latex.ts:34` records *"only 2 contain `$$`"*;
> that figure is **stale** — see the `.quartz/` warning in §5.1, which is the likeliest way it was
> produced.)
>
> **The contradiction, stated plainly.** §5.4 recommends **doing nothing** for Mermaid at n=2, on
> the reasoning that n=2 does not justify the cost. §5.1 mandates a **PORT** for block math at
> **n=0** — and block math is the *more* expensive of the two: it needs `mdit_py_plugins.dollarmath`
> (a new wheel, §5.2) **plus** a display engine, and KaTeX is a large vendored JS artifact, which is
> a second §2.1 problem. Same evidence threshold, opposite dispositions, with the cheaper feature
> getting the stricter treatment.
>
> **Recommend block math move from PORT to PORT-LATER**, revisited if a document ever uses it.
>
> **A5 itself is unaffected and should be kept — but it is a REGRESSION test, not an
> implementation item.** The property it protects **already holds on both sides today**:
> `costs $5 and saves $12` renders as literal text in `brain ui` (verified by running the
> renderer — strict CommonMark has no inline-math rule at all), and `latex.ts:26-54` disables
> `singleDollarTextMath` on the wiki side with the 551-span rationale recorded in source. A5's
> value is that it fails loudly if a future plugin's stock defaults switch inline math back on.

**`![[embeds]]` — PORT-LATER, with two hazards.** Structurally the same shape as the existing
wikilink rule plus the existing `resolve_link_targets` round trip. But (a) transclusion cycles
(A embeds B embeds A) need a depth cap, and (b) transcluding a **confidential** document's body
into a non-confidential note's HTML would route around the withholding of §0.1 — any embed
implementation must consult the same sensitivity check.

> **VERIFIED 2026-08-11 — a third hazard, and it is live right now.**
>
> Both hazards above are real and correctly stated. But `![[…]]` is **not inert today**: the `!`
> is consumed as literal text and the `[[…]]` still matches `_wikilink_inline_rule`
> (`render.py:72-101`), so the reader gets a stray `!` glued to a clickable "unresolved link"
> anchor. Deferring the *feature* is defensible; leaving the *mis-render* undocumented is not,
> because it looks like a broken link rather than an unsupported feature.
>
> **Corpus usage is 0** (both DB and disk), so nothing renders this way today and PORT-LATER
> remains the right call — but any note that later uses an embed hits the defect immediately, and
> the cheap interim fix is to make the rule refuse a `!`-prefixed `[[`, degrading to literal text
> as §5.4's own "malformed input degrades" principle intends.

**Category 4 — Quartz-specific syntax.** The derived-edges fence
(`2026-04-30-derived-edges-in-bodies-design.md`) must at minimum **not be corrupted**. Note §9.5:
`derived_links` is `brain connect`'s data source, not decoration.

### 5.5 Two phase-1 blockers found 2026-08-11 that §5 never carried

> **VERIFIED 2026-08-11 — (1) enabling the parsers is not sufficient, because the rendered
> elements have NO STYLING. This is a cross-phase dependency.**
>
> `.note-body` (`components.css:196-211`) styles exactly `h1, h2, h3`, `p`, `ul`, `ol`, `pre`,
> `code` and `a`. Grepping all four stylesheets, there is **no rule anywhere** for `table`,
> `thead`, `th`, `td`, `blockquote`, `input[type=checkbox]`, or `hr`.
>
> So turning on the table rule yields an **unstyled** table — no borders, no cell padding, no
> header weight — and task lists yield bare default checkboxes. Phase 1's own exit criterion is
> *"every markdown construct … renders **correctly**"*, which unstyled output does not meet, while
> §11 places the identity/styling work in **phase 3**.
>
> **Resolve one of two ways, explicitly:** either phase 1 carries its own baseline CSS for the
> constructs it enables (recommended — it is small, and a table with no borders is a defect, not a
> style preference), or phase 1's exit criterion is reworded to "parses correctly" with the visual
> bar deferred to phase 3. It cannot be left implicit; as written the criterion is unmeetable.
>
> One item genuinely belongs to phase 3 either way: the **syntax-highlighting theme**. A Pygments
> colour scheme picked in phase 1 must agree with the phase-3 token system, so phase 1 should ship
> the semantic markup plus a neutral theme and let phase 3 own the palette.

> **VERIFIED 2026-08-11 — (2) phase 1's exit criterion (§11) is scoped to the wrong corpus, and a
> literal reading lets the phase exit with ~96% of affected documents still wrong.**
>
> **Spec said** (§11, phase 1 exit): *"Every markdown construct **the vault** contains renders
> correctly."* **Actual:** the affected documents are overwhelmingly **`_ingested`**, not authored
> vault notes — and `brain ui` serves **both** tiers from `documents`:
>
> | Construct | `_ingested` | authored vault | ingested share |
> |---|---|---|---|
> | Tables | 461 | 7 | **98%** |
> | Task lists | 124 | 21 | **86%** |
> | Strikethrough | 38 | 0 | **100%** |
> | Footnotes | 3 | 0 | **100%** |
>
> Read literally, phase 1 could satisfy its exit criterion by fixing 7 tables and 21 task lists
> while 461 and 124 respectively still render as literal pipes and brackets.
>
> **Recommend the criterion read "every markdown construct **the corpus** contains"**, since that
> is what the surface actually displays. **Not corrected in §11 itself — that section is outside
> this pass's edit scope.** Whoever owns §11 should make the one-word change there.

---

## 6. Graph and link surfaces

The single largest scope item, and the sharpest constraint collision in the document.

### 6.1 What the wiki has

`Graph.tsx` (468 lines) + `graph.inline.ts` (**2,302 lines**) + `graph.scss` (505). Stack:
**the full `d3` bundle (`"d3": "^7.9.0"`, imported `from "d3"` at `graph.inline.ts:55`) + PixiJS
(WebGL) + tween.js** — a full npm chain. **Four render surfaces**: sidebar
local (depth 1), fullscreen global (~1,300 nodes), local fullscreen, and the diagnostic workbench.

Roughly fifteen documented features: zoom, drag, hover-highlight, click-to-navigate, in-graph
search, tier/source filter chips, recency-based node sizing (linear decay over a year),
degree-based radius, hub-label boost, derived-edge dash styling with rule+weight tooltips, and
orphan/tag-node toggles. Data comes from `contentIndex.json` — a build-time artifact from a
TypeScript emitter with no Python equivalent.

### 6.2 What Python already provides — the good news

`src/brain/vault/graph.py` (620 lines, **zero CLI coupling**):

| Function | Line | Gives us |
|---|---|---|
| `graph_data(conn, root=, depth=, include_ingested=, include_derived=)` | `:350` | The whole graph, or a rooted neighbourhood at depth N |
| `backlinks_for` | `:163` | "What links here" |
| `outgoing_links_for` | `:220` | Forward links |
| `orphans` | `:305` | The orphan toggle's data |

`vault/graph_format.py` (388 lines, pure) adds `to_json:90`, `to_dot:147`, `to_mermaid:213`.

**A `brain ui` route can call these today with no new Python logic.** Depth-limited rooted
queries (`root=`, `depth=`) also mean the *sidebar local graph* — the affordance actually used
while reading — is cheap and needs no global layout at all.

This also settles §3.1's backlinks-as-marginalia row and, with the §9.2 move of
`build_related.py`, the related-docs row.

### 6.3 The collision, and the options

`d3` and PixiJS are npm packages. §2.1 forbids Node/bundler/CDN and
`test_ui_static_assets.py:137` **enforces** it by grepping the static tree for `http(s)://`.

| Option | What it costs | Constraint impact |
|---|---|---|
| **(a) Vendor d3-force only**, render with Canvas 2D or SVG | **Not one file.** `package.json` declares the **full `d3` bundle** (`"d3": "^7.9.0"`) and `graph.inline.ts:55` imports `from "d3"`. `d3-force` is separable and ISC, but pulling it means **~10 ES modules** including `d3-dispatch`, `d3-quadtree`, `d3-timer`. | Permissible — no network — but the cost is a small vendored *tree*, not a file. Needs an update procedure and licence notes. |
| **(b) Hand-roll the force simulation** (~100–150 lines) | Development + tuning time | **Zero** vendoring. Within this project's norms — `_wikilink_inline_rule` (`render.py:72-101`) is precedent for hand-rolling rather than adding a dependency. |
| **(c) Vendor PixiJS too** | Several hundred KB–1 MB minified in the wheel | Legal (MIT) but undermines the hand-written-and-auditable ethos the whole front end is built on. |
| **(d) Server-rendered static SVG** for the local neighbourhood only | No interactivity | **Zero** constraint pressure; Python emits SVG with no dependency. Pairs naturally with `graph_data(root=, depth=1)`. |

**Honest quality gap for (a), (b), and (d):** visible degradation past a few hundred nodes
against PixiJS at 1,300+; loss of tween transitions; loss of label-collision avoidance and
fit-to-bounds zoom.

**The cost is not in the rendering primitive — it is in the feature subset.** Fifteen features
across four surfaces is the real number. Q3 forces that decision; Q2 forces the primitive.

**Recommendation (Q2 answered — confirm only if you disagree):** (d) for the sidebar local graph — which covers the
common reading case at zero constraint cost — plus (b) or (a) for a fullscreen view *only if* Q3
says the fullscreen global graph is genuinely used.

### 6.4 GraphRAG is a separate surface — do not build a second force canvas

The entity graph has its own reusable API (`format.graph_context_json`, `graph_stats_json`),
already proven by `mcp_server.py` as a non-CLI consumer.

> **A6.** Make the **document-link** graph the visual centrepiece (the direct Quartz analog) and
> expose **GraphRAG** as a search/browse surface, not a second force canvas.

**Availability caveat, and it is real:** `BRAIN_GRAPH_ENABLED` is code-default-on, but
`brain setup --profile minimal|standard` writes it **false**; only `full` enables it. So the
`graph_*` tables may be empty on a given install. The UI must render an **honest degraded state**
("graph not built — run `brain-rebuild --only graph`"), never an error and never an empty canvas
that looks like a bug.

### 6.5 The binding rule

Per §2.3, **no graph surface may trigger a build.** `graphrag build`, `graphrag refresh`,
`communities refresh`, and `connect refresh` are `brain-rebuild` stages
(`maintenance.py:105-124`) and stay there.

---

## 7. Branding, identity, and an asset at risk

### 7.1 Design direction — **"Instrument & Page"**

> **Method note.** Unlike every other section, this one is **measured, not read**. The app was
> started read-only on a spare port and driven with Playwright; computed styles and
> canvas-resolved contrast ratios were captured at 1600 px and 780 px in both themes.
> Screenshots went to gitignored scratch and were deleted. All examples below are synthetic.

**The direction.** A Swiss/International **instrument panel** that recedes to near-monochrome,
framing a single editorially typeset **page** that is the only lit, warm, high-craft surface.
Chrome is a tool; the note is the work; **the contrast between them is the hierarchy.**

**This answers Q9, and it resolves Q5 with it** (§7.3).

#### 7.1.1 Why a new direction rather than an extension of "Archival Terminal"

Two reasons, the first of which is decisive:

**(a) There is nothing to extend — the stylesheet describes a design that is not on screen.**

| `app.css` claims | Measured |
|---|---|
| `:12` "Depth comes from hairlines and sunken/raised grounds" | Surface pairs measure **1.05–1.09:1**. There is no depth. |
| `:9,:52` "capped at 68ch" as a reading measure | Renders at **751 px / ~92 characters**. Classical range is 45–75. |

The `--measure: 68ch` bug (`app.css:52`) is instructive: `ch` is the width of the digit `0`,
roughly **35% wider in a serif** than the average character. The unit silently hides the error.
**Fix: express the measure in `rem` (`36rem`), never `ch`.**

**(b) Its premise is the collision.** "Archival Terminal" is warm beige, system serif, verdigris.
That *is* what makes `brain ui` look like a different product from the wiki — so extending it
would entrench the very problem §7.3 exists to resolve.

#### 7.1.2 The role split — how the identity conflict dissolves

Rather than blending two palettes (§7.3 warns this produces a third identity nobody chose), the
direction **assigns them different jobs**:

| Role | Identity | Rationale |
|---|---|---|
| **Instrument** (chrome, rail, ledger, controls) | **Inherits the wiki's token system** — Geist / Geist Mono, major-third scale, pure-neutral zinc, indigo-violet accent, and the per-source provenance palette from `styles/brain/_tokens.scss` | The app and the wiki become **one visual product** rather than two. |
| **Page** (the note body) | **Deliberately diverges** — a real text face (Newsreader) | A reading surface should not be typeset in a UI face. |

This is a **role split, stated as such** — not a compromise between two palettes.

#### 7.1.3 Measured defects — these exist **today**, independent of the migration

Recorded here because they are found, verified, and would otherwise be silently inherited by
every new surface this document adds.

| # | Defect | Evidence |
|---|---|---|
| **D1** | **`--measure: 68ch` renders at 751 px / 92 characters.** | `app.css:52`; measured at 18 px serif |
| **D2** | **Every note renders its title twice** — `app.js:313` appends an `<h1 class="note-title">`, and the server-rendered body contributes the document's own leading `#`. Both 36 px, identical strings, ~40 px apart. **At 780 px the pair consumes the entire first screen.** | `app.js:313` verified |
| **D3** | **Depth does not exist.** Surface pairs 1.05–1.09:1; the hairline carrying all structure is 1.33:1. | measured |
| **D4** | **Seven text tokens fail WCAG AA**, including the **⌘K hint at 2.89:1** — the discoverability affordance for the primary interaction (`index.html:56`). **Selection state fails SC 1.4.11 at 1.13:1** (fill only; no border, weight, or marker). | measured |
| **D5** | **`aria-selected` missing from the tree.** `role="tree"`, `treeitem`, `aria-level`, `aria-expanded`, and roving tabindex are all verified present — but the WAI-ARIA tree pattern requires `aria-selected`. Result rows also lack `aria-current` (it appears only on tabs, `app.js:535`). | verified |
| **D6** | **The ledger gutter — the widest position in every row — shows a raw UUID prefix** (`result.id.slice(0, 8)`), while **`/api/search` returns no date field at all** despite `documents.doc_date` existing (migration 021). Snippets also leak raw Markdown (`**bold**`, `## Heading`) with no query-term highlighting. | `app.js:219`; `schemas.py:361-371` verified — no date key; `doc_date` appears nowhere in `ui/` |
| **D7** | **Edit mode has no design.** `app.js:375-386` returns before `.note-body` is built, so none of the reading treatment applies. The textarea is `min-height: 26rem` with `resize: vertical` — **a user-resizable minimum, not a fixed height** — which still leaves ~370 px dead below it in a 956 px pane, because nothing makes it fill. | `app.js:375-386`, `app.css:355-361` verified. An earlier revision said "fixed 26 rem"; there is no `height` declaration to find. |
| **D8** | **The CSS `color-scheme:` property is never declared**, so native scrollbars and the date-picker popover render light-mode inside dark mode. | `app.css` — the only occurrence of the string is the **`prefers-color-scheme` media feature** at `:73`, which is a *different thing* (it queries the OS preference; it does not tell the UA to render native widgets dark). An earlier revision wrote "no `color-scheme` anywhere", which a re-run of the grep contradicts — the defect is real, the evidence line was not. |

> **A11.** D1–D8 are fixed as part of phase 3, *before* new surfaces are layered on. D2, D4, and
> D6 in particular are cheap and high-visibility: a duplicated `<h1>`, a failing contrast token,
> and a UUID where a date belongs are the three things a user notices first.

> **VERIFIED 2026-08-11 — the D-table was RE-MEASURED against the current stylesheet. Three items
> are already fixed, D4's headline number no longer exists, and one defect is worse than
> recorded.** Method and re-derivable conversion code are in the §7.1.4 correction below; every
> figure here was cross-checked against the browser's own painted pixels.
>
> | Defect | Status 2026-08-11 | Measured |
> |---|---|---|
> | **D1** `--measure: 68ch` → 92 chars | **ALREADY FIXED** | `--measure: 48ch` (`css/tokens.css:87`), confirmed as the computed value in-browser |
> | **D8** `color-scheme` never declared | **ALREADY FIXED** | declared in **all three** scopes — `:root` (`tokens.css:51`), the `prefers-color-scheme` block (`:110`), and `:root[data-theme="dark"]` (`:126`). Both dark paths were *driven* in a browser and both compute `color-scheme: dark` |
> | **D3** surfaces "1.05–1.09:1" | **understated** | **1.05–1.15:1** (light 1.05 / 1.07 / 1.13; dark 1.05 / 1.09 / 1.15) |
> | **D3** hairline "1.33:1" | **one point of a range** | 1.33:1 is `--rule` on `--paper` in light *only*; the true range is **1.06–1.43:1** across all eight pairings |
> | **D4** "**seven** text tokens fail AA" | **WRONG — it is two** | `--ink-faint` and `--flag-draft`. "Seven" counted CSS **rules**, not tokens |
> | **D4** "⌘K hint at **2.89:1**" | **NO LONGER TRUE** | `kbd.hint` (`css/components.css:120-122`) now uses `--ink-muted`: **6.10:1** light, **6.92:1** dark. 2.89:1 is `--ink-faint` on `--paper`, whose only remaining consumer is `.search-glyph` |
> | **D4** "selection fails 1.4.11 at **1.13:1**" | **CONFIRMED — and worse** | see below |
>
> **The two text tokens that actually fail SC 1.4.3, measured in the rendered DOM:**
>
> | Element | Token | Light | Dark | Minimum lightness change to pass |
> |---|---|---|---|---|
> | `.search-glyph` | `--ink-faint` | **2.89:1 FAIL** | **3.64:1 FAIL** | L 66.0%→55.0% light; 54.0%→58.9% dark |
> | `.dot-draft` (9px) | `--flag-draft` | **3.21:1 FAIL** | 8.44:1 PASS | L 64.0%→55.8% |
> | `.save-state[dirty]` (11px) | `--flag-draft` | **3.37:1 FAIL** | 7.73:1 PASS | L 64.0%→56.9% |
>
> **`--ink-faint` fails in DARK too, not light only** — 3.33–3.83:1 across the three paper grounds.
> `components.css:88-97` already documents this and claims a 1.4.3 exemption for `.search-glyph` as
> `aria-hidden` decoration, which is defensible — **but treated as a UI icon under SC 1.4.11 (3:1)
> it still fails in light at 2.89:1**, so the exemption does not fully rescue it.
>
> **SELECTION — confirmed, worse than recorded, and structurally unfixable by tuning.**
> `.result.is-selected` (`css/components.css:154`) is `background: var(--accent-soft)` **and
> nothing else** — no border, no marker rail, no weight change. Measured against the ground it
> actually sits on:
>
> | Selected surface | Ground | Light | Dark |
> |---|---|---|---|
> | Ledger row (`.result.is-selected`) | `--paper-sunk` | **1.05:1** | 1.35:1 |
> | Tree label (`.tree-label.is-selected`) | `--paper` | **1.13:1** | 1.28:1 |
>
> The spec's 1.13:1 is the *tree* case; the **ledger row is 1.05:1**, and it is the one with no
> compensating text-colour change. **This means `components.css:154` violates §7.1.4's own
> non-negotiable rule #2** ("`--accent-wash` may never carry selection alone"), which is therefore
> already validated by measurement rather than merely asserted.
>
> **The structural finding: `--accent-soft` cannot be both the wash and the indicator.** Raising it
> to clear 3:1 as a non-text indicator requires L 93%→**62–66%** (light), which destroys it as a
> wash. Rule #2's remedy — wash **plus** a 3px accent marker rail — is not a stylistic preference;
> it is the only way to satisfy SC 1.4.11 without abandoning the wash.
>
> **`--rule` fails SC 1.4.11 in every position, both themes — 8 of 8 pairings, missed entirely by
> the original audit:**
>
> | Ground | `--paper` | `--paper-sunk` | `--paper-raised` | `--accent-soft` |
> |---|---|---|---|---|
> | Light | 1.33:1 | 1.24:1 | 1.40:1 | 1.18:1 |
> | Dark | 1.35:1 | 1.43:1 | 1.24:1 | 1.06:1 |
>
> `--hair` (`= 1px solid var(--rule)`) is used **20 times**, so this is the single most widespread
> non-text failure in the stylesheet. Passing needs L 88%→**63–66%** (light) or 30%→**48–55%**
> (dark) — a very large move, because the token is currently doing decorative-hairline duty at a
> value that cannot also satisfy 1.4.11. **This is a design conflict, not a wrong number**, and is
> the expensive item in phase 3.
>
> **Also failing SC 1.4.11:** `.withheld`'s 3px `--flag-draft` marker on `--paper-sunk` at
> **2.99:1** in light — short by 0.01. *(Token-computed, not DOM-measured — see NOT VERIFIED.)*
>
> **Passing, recorded so phase 3 does not "fix" them:** focus ring `--accent` **5.09:1** light /
> **7.12:1** dark; `--ink` 11.04–16.94:1; `--ink-muted` 5.40–7.30:1 on every ground; `--flag-danger`
> and `--flag-ok` pass as text on all three papers; inverted chips (`--paper` on
> `--accent`/`--flag-danger`/`--ink`) 4.84–16.12:1.
>
> **A correction in the ORIGINAL AUDIT'S FAVOUR.** A relayed briefing held that the audit's
> `--ink-muted` range of **5.42–6.41:1** was an error and the real range was 5.66–7.27:1. **The
> audit was not wrong.** 5.42 and 6.41 are *exactly* `--ink-muted` on `--accent-soft` and on
> `--paper-raised`, **light theme only** — a correct light-theme range that includes the accent
> ground. The 5.68–7.30:1 figure measured here is the *three-paper, both-themes* range. Two
> different sets, both right. Recorded because this document's credibility depends on retracting
> accusations as carefully as it makes them.
>
> **NOT VERIFIED — flagged rather than estimated.** `.withheld` and `.note-body` never rendered in
> the harness (entering edit mode replaces the body; the withheld path needs a fixture the sweep
> did not reach), so their figures — including the 2.99:1 marker-rail failure — are **computed from
> tokens, not measured in the DOM**. Hover, disabled and visited states were **not** swept; only
> default, `:focus-visible` and `.is-selected`. The scrim `oklch(0% 0 0 / 0.4)`
> (`components.css:257`) is alpha-composited and has no static pair to measure.

#### 7.1.4 Token system

> ⚠️ **The token block itself is NOT in this document, and that is a gap, not a style choice.**
> The design research produced a complete drop-in CSS custom-property block — light "Desk" / dark
> "Lamp", every ratio canvas-computed — but only the two rules below and the `--ink-3` hexes were
> carried across. **Consequence: phase 3's exit criterion ("every text token clears AA on every
> permitted ground") is currently unverifiable by anyone except that block's author**, because the
> ground hexes it is measured against are not written down here.
>
> **A13 — before phase 3 starts, inline the full token block into this section** (or commit it to
> `src/brain/ui/static/css/tokens.css` and cite that path). Until then, treat §7.1.4 as a
> statement of *intent* with two verified constraints, not as a specification.
>
> **A13's first check, so it is not rediscovered the hard way.** `#8B8B95` has relative luminance
> **0.26124**, so clearing 4.5:1 requires a ground at **L ≤ 0.01916** — about `#252525`. Recomputed
> here from the WCAG formula:
>
> | Dark ground | Contrast vs `#8B8B95` | |
> |---|---|---|
> | `#252525` | **4.54:1** | passes, barely |
> | `#262626` | **4.49:1** | **fails** |
> | `#2A2A2E` (a typical "raised" surface) | **4.24:1** | **fails** |
>
> **The tension this exposes is structural, not arithmetic.** §7.1.1's central complaint is that
> depth does not exist (surfaces at 1.05–1.09:1). Adding *real* depth means several dark grounds —
> and **any dark ground lighter than ~`#252525` breaks non-negotiable rule #1.** The two goals pull
> against each other on the dark side, and the token block has to resolve that explicitly rather
> than discover it during implementation.
>
> **Whoever executes A13: check `--ink-3` against the *lightest* of the four dark grounds first.**
> That is where it breaks if it breaks. The light side is fine and mutually consistent (6.14:1 on
> white, 4.84:1 on inset).
>
> *(The `#2A2A2E` row was relayed as 4.03:1; recomputation gives **4.24:1**. Same verdict — it
> fails — so nothing downstream changes, but the figure is corrected here rather than copied.)*
>
> What *was* independently checked: `#616168` yields **6.14:1 on white** and **4.84:1** on the
> implied inset ground — mutually consistent in a way that does not happen by accident — and the
> `--accent-wash` figure is forced by SC 1.4.11 rather than chosen. **The dark-side `--ink-3`
> (`#8B8B95`) is tight and cannot be verified without the dark ground hexes**, which is exactly
> what A13 supplies.

The research *produced* such a block — light "Desk" / dark "Lamp", every ratio canvas-computed —
but, per the box above, **it is not reproduced here** (A13). What did carry across are the two
rules below, stated as **non-negotiable** and binding on any later tuning:

1. **`--ink-3` is tuned to `#616168` / `#8B8B95` precisely so it clears 4.5:1 on all four
   grounds.** The obvious choice (zinc-500) **fails on inset at 3.81:1**. Do not "simplify" this
   token back to a scale value.
2. **`--accent-wash` may never carry selection alone.** Selection is always wash **plus a 3 px
   accent marker rail** (5.96 / 6.64:1) — this is what fixes D4's SC 1.4.11 failure.

> **VERIFIED 2026-08-11 — `--ink-3` does not exist in the shipped palette, `#9D9D9D` fails every
> light ground, and A13's "unverifiable by anyone except that block's author" is now closed for the
> CURRENT palette.**
>
> **(a) The token does not exist.** `grep` for `ink-3` across `src/brain/ui/static/**` (CSS, JS,
> HTML) returns **zero hits**. `--ink-3`, `--accent-wash` and the zinc/`#252525` vocabulary belong
> to the *proposed* "Instrument & Page" system, not the shipped "Archival Terminal" palette, whose
> twelve colour tokens are `--paper{,-sunk,-raised}`, `--ink{,-muted,-faint}`, `--rule`,
> `--accent{,-soft}`, `--flag-{draft,danger,ok}` (`css/tokens.css:54-65`). Nothing in the running
> app can be checked against rule #1 today because the token it constrains is not there.
>
> **(b) Measured against the grounds that DO ship.** Reported as measurement, not as reopening a
> settled decision:
>
> | Candidate | Light — `--paper` / `-sunk` / `-raised` / `--accent-soft` | Dark — same four |
> |---|---|---|
> | **`#9D9D9D`** | 2.52 / 2.34 / 2.64 / 2.24 — **FAILS all four** | 6.80 / 7.17 / 6.23 / 5.31 — passes all four |
> | `#616168` (spec light) | 5.70 / 5.30 / 5.99 / 5.06 — passes | 3.00 / 3.17 / 2.75 / 2.34 — fails all four |
> | `#8B8B95` (spec dark) | 3.13 / 2.91 / 3.29 / 2.78 — fails | 5.47 / 5.76 / 5.01 / **4.27 FAIL** |
>
> **`#9D9D9D` is viable as a DARK-side value only**; as a single cross-theme value it fails every
> light ground. And **§7.1.4's own warning is vindicated** — *"check `--ink-3` against the lightest
> of the four dark grounds first; that is where it breaks if it breaks"* — `#8B8B95` **does** break,
> at **4.27:1**, though on `--accent-soft` rather than `--paper-raised`. These figures are against
> the *current* grounds; if the Instrument palette ships its own, **re-run this table, do not reuse
> it.**
>
> **(c) METHOD — this is the part the original §7.1.4 lacked, and why it did not reproduce.**
>
> **Chromium returns computed colours as `oklch(...)`, not `rgb(...)`.** Any audit that
> regex-scrapes `getComputedStyle(el).color` for three numbers reads the **OKLCH components as
> RGB** and manufactures failures. This was hit directly during this pass: the first DOM sweep
> produced `#01004B`-style values with *every* pair "failing". The fix is to resolve every colour
> by **painting it** — `ctx.fillStyle = c; ctx.fillRect(...); getImageData(...)` — which normalises
> any CSS colour syntax to the sRGB actually rendered.
>
> **Conversion, OKLCH → OKLab → linear sRGB → sRGB → WCAG luminance:**
>
> ```python
> _LMS_FROM_LAB = ((1.0,  0.3963377774,  0.2158037573),
>                  (1.0, -0.1055613458, -0.0638541728),
>                  (1.0, -0.0894841775, -1.2914855480))
> _RGB_FROM_LMS = (( 4.0767416621, -3.3077115913,  0.2309699292),
>                  (-1.2684380046,  2.6097574011, -0.3413193965),
>                  (-0.0041960863, -0.7034186147,  1.7076147010))
>
> def oklch_to_linear_srgb(L, C, H_deg):          # L 0..1, C absolute, H degrees
>     h = math.radians(H_deg)
>     lab = (L, C * math.cos(h), C * math.sin(h))
>     lms_ = [sum(m[i] * lab[i] for i in range(3)) for m in _LMS_FROM_LAB]
>     lms  = [v ** 3 for v in lms_]
>     return tuple(sum(m[i] * lms[i] for i in range(3)) for m in _RGB_FROM_LMS)
>
> def _encode(c):                                  # linear -> gamma sRGB, gamut-clipped
>     c = max(0.0, min(1.0, c))
>     return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
>
> def _decode(c8):                                 # 8-bit sRGB -> linear (WCAG's own definition)
>     c = c8 / 255.0
>     return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
>
> def oklch_to_rgb8(L, C, H):
>     return tuple(round(_encode(v) * 255) for v in oklch_to_linear_srgb(L, C, H))
>
> def luminance(rgb8):
>     r, g, b = (_decode(c) for c in rgb8)
>     return 0.2126 * r + 0.7152 * g + 0.0722 * b
>
> def contrast(a, b):
>     la, lb = luminance(a), luminance(b)
>     return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
> ```
>
> **Clip-then-quantise-to-8-bit before luminance is load-bearing**, not a rounding detail: 8-bit is
> what the compositor paints, and WCAG defines its linearisation on the 8-bit value. Minimum-fix
> figures come from a 60-iteration bisection on L holding C and H fixed.
>
> **Cross-check that makes the table trustworthy:** 24 token renderings, arithmetic vs the
> browser's painted pixels — **worst per-channel delta 1/255** (one channel of `--accent` dark,
> `#54B9A5` vs `#53B9A5`; pure rounding). All 22 rendered DOM pairs then matched the arithmetic to
> two decimals. The two dark scopes (`tokens.css:110` media, `:126` manual) are **byte-identical**,
> so every dark figure in this document covers both.
>
> **(d) A13's status.** A13 asked for the token block to be inlined so phase 3's exit criterion
> ("every text token clears AA on every permitted ground") became verifiable. For the **current**
> palette that is now done: the tokens are at `src/brain/ui/static/css/tokens.css` (cite that path,
> per A13's own wording), and the measured pass/fail table for every token × ground × theme is in
> the §7.1.3 correction above. **A13 remains OPEN for the proposed Instrument palette**, which
> still has no written token block and therefore still has no verifiable exit criterion.

#### 7.1.5 Structural proposals — recommendations, not decisions

| Proposal | Note |
|---|---|
| **Retire the tab strip** | Three of four tabs are apologies (§1.2). Move them to ⌘K plus a status footer. **Product decision — T4/Q17.** |
| **A designed empty state** | Use `/api/status` counts plus recents-by-day to replace the ~70%-empty void. |
| **3 px per-source provenance rail** on ledger rows and tree ticks | Inherited directly from the wiki's palette; pairs with the source icons of §3.2. |
| **Marginalia column** for backlinks, tags, outline, graph neighbourhood | Reuses the wiki's existing `_marginalia.scss` concept — and is where §6.2's `backlinks_for` and §6.3's local graph land. |
| **Edit-in-place** | Toggling Edit changes face and affordances but **the text does not move**. Fixes D7. |

#### 7.1.6 Note on the open-design skill collection

The research invoked `creative-director`, `web-design-guidelines`, and `design-brief`, and
inspected eight others. **Nearly all open-design skills are 42–43-line catalogue stubs
advertising an uninstalled upstream bundle.** Only `design-brief` (252 lines) carries real
content, and **its token table was rejected** because every face in it (Inter, Playfair, Space
Grotesk) is a Google-Fonts dependency this project forbids (§2.1).

This is why the token vocabulary above does not match that collection's. Recorded so a future
reader does not mistake the divergence for carelessness — and so the global CLAUDE.md preference
for that collection is understood to have been *followed and found wanting here*, not skipped.

### 7.2 Requirement A4 (phase 0): rescue the orphaned logo

**`dist/static/brain-logo.svg` — 2,315 bytes, hand-authored, `<title>Second Brain aperture
logo</title>`, dated 2026-05-14. Asymmetric aperture rings plus connected graph nodes in
terracotta and indigo-violet matching `_tokens.scss`. It has zero references anywhere: not in
`git ls-files`, not in any tracked source, not linked from dist's own rendered output. `dist/` is
gitignored. Nothing regenerates it. A clean rebuild destroys it permanently.**

> **A4 — phase 0, before anything else.** Commit `brain-logo.svg` into **`src/brain/ui/static/`**
> — the tree that *survives* — alongside the PNGs the UI needs.
>
> **Do not** commit it into `quartz_overrides/quartz/static/`: that is the tree being deleted.
>
> **`dist/` contains real ingested content.** Nothing from `dist/` other than this one design
> asset may be committed, ever (CLAUDE.md rule 15). The commit must add exactly one file.

A backup exists outside the repo, but a backup outside version control is not a rescue.

### 7.3 The identity conflict — **resolved by the role split of §7.1.2**

The two surfaces have genuinely different identities. The first draft of this document framed
that as "pick one, do not blend". **§7.1.2 supersedes that framing with a better answer:** assign
the wiki's token system to the *instrument* and a real text face to the *page*. That is neither
a pick nor a blend — it is a role split, and it is what makes the app and the wiki read as one
product while still giving the reading surface its own voice.

The axis table below is retained as the record of what differed. **Q5 is answered by §7.1.2**;
only the font-delivery question survives, as **T1**.

| Axis | Wiki | `brain ui` |
|---|---|---|
| **Wordmark** | "Second Brain" | lowercase "brain" (`index.html:27`) |
| **Colour space** | hex / rgba | **OKLCH** |
| **Palette** | "Linear-style charcoal + indigo-violet", accent `#5e6ad2` / `#7170ff` | "Archival Terminal", verdigris accent `oklch(52% 0.09 178)`, papers "Foolscap" / "Lamplight" |
| **Type** | **Geist / Geist Mono via Google Fonts CDN** | pure system stacks, zero network |
| **Token source** | `quartz/styles/brain/_tokens.scss` (197 lines, of 3,758 across **21** partials; 4,352 for the whole `styles/` tree) | `app.css` |
| **Logo** | 512×512 PNG pair + `favicon.ico` (48×48) + orphan SVG | **none** |
| **Source icons** | 📧 🎙️ 💬 ✍️ 🌱 (`util/sourceIcons.ts`) | **none** |

Two hard notes:

1. **Fonts cannot arrive over the network, and the fix requires a CSP change (T1).** Geist ships
   from the Google Fonts CDN today, and `test_ui_static_assets.py:137` fails on `https://` in the
   static tree. **The test is not absolute, and the nuance matters here:** `:133-140` skips lines
   beginning `*`, `//`, `<!--` or `#` and exempts `www.w3.org` — so a **vendored licence header
   citing a URL passes**, while `@import url(https://…)` still fails. The only compliant route is **self-hosting woff2 in the wheel**, and that needs
   **`font-src 'self'` added to the CSP** — without it `default-src 'none'` (`security.py:63`)
   blocks every `@font-face` **silently**. Verified favourable details: `src: url("/static/fonts/…")`
   is scheme-relative and **passes** the offline test; the cost is ≈115 KB for three variable
   woff2 (Geist, Geist Mono, Newsreader); and the change adds no `unsafe-inline`, no nonce, and no
   external origin. **The "but 296 KB of logo PNGs already ship" comparison has been removed: it
   is true today and false the moment this plan completes**, because those PNGs live in
   `quartz_overrides/`, which R3 deletes (§1.3). Post-R3 the wheel *loses* 296 KB and *gains* 115 KB.
   Judge the 115 KB on its own merits. **If refused, the
   system-stack fallback works with one change (`--measure: 34rem`) and the direction survives,
   weaker.** T1.
2. **`_tokens.scss` is the reference for the instrument half** per §7.1.2. OKLCH → hex is a
   lossy, one-way translation of *intent*, which is exactly why the role split beats a merge.

### 7.4 Logo port mechanics

`PageTitle.tsx:12-31` renders the light/dark pair at 48×48 (`width="48" height="48"`, `3rem` in
CSS), both `alt=""` and `aria-hidden="true"` since the adjacent `<span>` carries the accessible
name; theme swap is pure CSS on `:root[saved-theme="dark"]` (`PageTitle.tsx:60-70`) with no JS.
`brain ui` has neither logo nor favicon (`index.html:1-21`); its header is a bare text
`<span class="brand">brain</span>` (`index.html:27`).

1. **CSP:** no change needed — `img-src 'self' data:` (`security.py:66`) covers a same-origin
   PNG/SVG, and `<link rel="icon">` needs nothing. Do **not** inline the logo as a `data:` URI:
   it bloats the stylesheet and defeats caching.
2. **Packaging:** add explicit `static/*.png`, `static/*.svg`, `static/*.ico` patterns to
   `[tool.setuptools.package-data]` — never `**/*`. Omit this and the wheel ships a broken image
   with no error (`app.py:30-41`).
3. **Theme mechanism:** `brain ui` uses `data-theme` on `<html>` (`index.html:2`) set by
   `theme.js` before first paint; Quartz uses `saved-theme`. Copy the *pattern*, not the selector.
4. **Accessibility:** keep `alt=""` + `aria-hidden="true"` on both images so a screen reader
   announces the wordmark once.

---

## 8. External editing and the write-concurrency contract

### 8.1 The requirement

The user edits vault files from **Obsidian, VS Code, and vim**, and that must keep working:

> **Vault tier** (`kind='vault'`) — the `.md` file on disk is the source of truth; the DB is a
> derived index that `brain vault sync` rebuilds from the file. **Edit in any text editor.**
> — `docs/vault-and-wiki.md:14`

`notes_service.py:19-25` encodes the same rule: for `kind='vault'` the file is authoritative and
`update_document` skips mirror writes; for `kind='ingested'` the row is authoritative and the
mirror is regenerated. "Crossing those wires corrupts one tier or the other."

**And this is why §0.1's second correction matters:** the `_ingested/` mirror — how Obsidian and
vim see ingested content — is maintained by `vault export` / `sync-summaries` / `prune-orphans`,
which currently live *inside* the `wiki` stage (`maintenance.py:70-88`). Deleting that stage
wholesale breaks external reading of 1,257 of 1,392 documents.

### 8.2 Verdict: vault-tier coexistence is **already safe** — better than "two writers, no lock"

Verified in source, and worth recording as a strength rather than assumed as a hazard:

`read_note` computes `body_hash` via `_split_file` (`notes_service.py:114-118`), which is a
**fresh `path.read_text()` on every call** — not a cached value and not the DB. `update_note`
(`:268-301`) compares the incoming hash against a freshly re-read `read_note`. So the standard
race is caught correctly:

> GET at T0 → external editor saves at T1 → browser saves at T2 with the stale hash →
> **409 `stale_write`**, nothing overwritten.

And the reverse direction is clean: `sync_one_file`'s `body_unchanged` idempotency
(`vault/sync.py:710,813`) makes the watcher's echo of a UI save a no-op — one wasted
`sync_one_file`, no loop, no corruption.

### 8.3 Three real gaps

**G1 — No draft recovery on a 409. This blocks "`brain ui` is my primary UI".**
`app.js:411-413` shows a toast — *"This note changed on disk. Reopen it to see the current
version."* — and **discards the user's in-progress edit**. No auto-merge, no draft buffer.
Acceptable for a v1 side-tool; unacceptable for the surface someone does all their writing in.

> **A7 — must land before R1 (demotion), not before R3.** A client-side draft buffer
> (`localStorage`) that preserves unsaved text across a 409, plus a way to see both versions.
> Losing writing is the fastest way to make the user distrust the new surface.

**G2 — `_ingested/` has no reliable single source of truth.** `sync_one_file`'s
`body_unchanged` check applies to **both** tiers, branching by tier only for metadata and
sensitivity. So a hand-edit to a file under `_ingested/` **is** written into `documents.content`
by the watcher (the file transiently wins), and is clobbered only when something later calls
`regenerate_vault_file(force=True)` (`vault/export.py:694-733`) — a re-ingest, a `brain tag`, or
a UI edit. Net: **last-writer-wins between two independent mechanisms**, not the one-way DB→file
relationship the docs describe.

This predates the migration but becomes far more visible once external editing is a *named,
encouraged* workflow. Both code paths were traced; **no reproducing test was built**.

> **A8.** Decide the intended invariant — either "`_ingested/` is read-only to humans and the DB
> always wins" (and enforce it), or "hand-edits to `_ingested/` are honoured until regeneration"
> (and document it) — then **pin it with a test**. An ambiguity this document merely describes is
> an ambiguity that will surprise someone.

**G3 — a narrow TOCTOU in `_update_vault_note` (`notes_service.py:304-360`).** It reads the file
for the hash check, reads it **again** at `:320` for the frontmatter merge, then writes
`patch.body if patch.body is not None else body` (`notes_service.py:321`). **Narrower than first
stated:** read #2's body *is* used on a metadata-only save, and its frontmatter is always used.
The discard happens only when a body edit is present. The existing design doc calls this "a sub-millisecond window… not worth a lock"
(`2026-07-25-brain-ui-design.md:1061-1066`). **That understates it**: the real window spans two
reads plus a merge. Still low probability on a single-user machine — but record it accurately,
and note it grows if a second *automated* writer ever touches the vault.

**Not a concurrency token:** `documents.updated_at` (migration 025) is used nowhere for
concurrency — only `body_hash` is. It is descriptive provenance consumed by search filters
(`search_predicate.py:125,128`). Do not describe it otherwise.

### 8.4 The contract

| # | Property | Mechanism | Status |
|---|---|---|---|
| C1 | The file always wins for `kind='vault'`. | Tier dispatch, `notes_service.py:19-25` | **Holds** |
| C2 | A save against a changed-on-disk body fails loudly with 409. | Fresh-read `body_hash`, `notes_service.py:114-118,285-289` | **Holds — verified** |
| C3 | A note open in the inspector that changes on disk surfaces that fact. | — | **GAP** |
| C4 | UI writes are atomic; no half-written file. | `atomic_write_text`, `notes_service.py:50` | **Holds** |
| C5 | A 409 preserves the user's work and offers a path forward. | — | **GAP → A7** |
| C6 | Nothing in `brain ui` triggers a full-corpus sync. | `sync_one_file` only — verified as the sole sync import in `ui/` | **Holds — must not regress** |
| C7 | `_ingested/` has one stated, tested source-of-truth rule. | — | **GAP → A8** |

For **C3**, options (not picked): poll `body_hash` on window focus; a long-poll/SSE endpoint; or
simply re-fetch on focus. **The last is nearly free and covers the realistic case** — "I edited
it in Obsidian, now I'm back in the browser".

### 8.5 The watcher becomes near-mandatory

**Without `brain vault sync --watch` running, external edits are invisible** to search, MCP, and
the UI until a manual `brain vault sync`. Today that is a nuisance. Once `brain ui` is the
primary surface it becomes a correctness problem: the user edits in Obsidian, searches in
`brain ui`, and gets stale results with no indication why.

> **A9.** Decide whether the vault-sync watcher becomes a **hard dependency** of `brain ui`, and
> how its absence surfaces. Minimum: `brain doctor` and the UI status line must report
> "watcher not running — external edits will not appear in search". Q8.

This collides directly with a retirement trap: `_maybe_install_launchd` (`setup.py:826-897`)
gates the **entire** launchd install — including the surviving vault-sync watcher — behind
`wiki_installed`. Post-retirement that gate must be rethought (§9.5, Q8).

**Watcher reliability history**, worth citing because it bounds how much to trust it:
`PollingObserver`, not FSEvents (Python 3.13 silently stopped delivering them,
`vault/watch.py:78-86`); 500 ms per-path debounce; `_MAX_PENDING=1000` overflow falls back to a
full `sync_vault` (slow but correct). Two past incidents: a bare `deleted` event for a file still
on disk once caused a real `DELETE FROM documents` (fixed with a re-stat guard,
`watch.py:748-791`); and an in-vault rename decomposed into delete+upsert and **silently orphaned
every incoming backlink** via `ON DELETE CASCADE` (fixed by in-place `vault_path` UPDATE,
`watch.py:995-1071`; auto-memory records this as HIGH severity).

### 8.6 "Open in external editor" — buildable, but its own security class

Precedent is CLI-only: `editor.py:15-49` — `find_editor()` reads `$VISUAL`/`$EDITOR`/`vi` then
runs a **blocking** `subprocess.run`. For `brain ui` the **server** spawns the process, which
changes everything:

- **Loopback-only, or it is broken as well as unsafe.** On a non-loopback bind the editor opens
  on the *server's* desktop, not the requester's. Gate on the existing `is_loopback` /
  `serve_confidential_bodies` seam (`ui/context.py:82-113`), 403 otherwise, and extend the
  `Sec-Fetch-Site` same-origin check — spawning a process is strictly higher-stakes than reading
  a body, and `ui/security.py:6` already documents the DNS-rebinding threat model.
- **Must be non-blocking `Popen`, never `run`.** The CLI's blocking pattern would hang the HTTP
  request until the user closes the file (`code --wait` is the anti-pattern), and a terminal
  editor has **no TTY at all** under a server process. Realistically this restricts it to GUI
  launch (`open -a`, `code`), not `$EDITOR` reused verbatim from the server environment.

| Option | Risk |
|---|---|
| **(a) Copy path to clipboard** | ~None. Client-only, zero server surface, works with every editor. |
| **(b) `obsidian://` / `vscode://` link** | Low. Needs a scheme exemption for *chrome* — not for note content, where `render.py:43`'s allowlist must stand. CSP unaffected. |
| **(c) Server spawns the editor** | High. A local HTTP server that spawns processes is an arbitrary-execution primitive one Origin-guard mistake from disaster. |

**Recommendation: (a) now; (b) if one-click is wanted; (c) scoped out of the first iteration or
given a dedicated security review.** It is not "just another API route." Q4.

---

## 9. The retirement

The inventory, the move list, and the traps below are verified against source. **Two named
confirmations remain**, both flagged inline rather than blocking this section: whether any
consumer of `BRAIN_USER_EMAIL` exists outside the wiki (§9.3 trap 7), and the final fate of
`_maybe_install_launchd`'s gating (§9.5, Q8).

### 9.1 What must survive

| Survives | Why | Evidence |
|---|---|---|
| `bin/brain-rebuild`, `src/brain/maintenance.py` | 7 of 8 stages are corpus maintenance | `maintenance.py:89-127` |
| `vault export` / `sync-summaries` / `prune-orphans` | Maintain the `_ingested/` mirror — **inside the `wiki` stage today** | `maintenance.py:70-79`; §8.1 |
| `src/brain/vault/**`, including `graph.py`, `graph_format.py` | `notes_service` writes through it; §6.2 reads through it | `notes_service.py:50-56` |
| `vault/daily_index.py` (228 lines) | Built for the Wiki UX Overhaul but now runs unconditionally in `brain daily` | `cli.py:203,7337,7382,7390-7405` |
| `derived_links` + the derived-edges fence | **`brain connect`'s data source**, not decoration | `connect.py:450-521` |
| `brain vault relink-derived` — **steps 1, 1.5, 2, 3, 4, 5, 5.5, 6** (`cli.py:6854,6861,6872,6895,6921,6936,6954,6970`) | **All of them survive.** An earlier revision said "steps 1–4, step 5 is wiki-only" — wrong twice: step 5 (People Hub emit) writes into the vault and survives (§9.2), and steps **5.5** (graph reconcile) and **6** (summary) were omitted entirely. | `cli.py:6944` |
| `bin/_brain-watcher-fg`, `com.brain.watcher`, `com.brain.brief` | Vault-sync watcher — becomes *more* important (§8.5) | — |
| The vault on disk, in full | §1.3 invariant | — |

### 9.2 MOVE, do not delete — three modules live under `wiki/` by path only

**This is an ordering constraint, not a preference. Deleting `wiki/` before moving these
silently breaks `brain graphrag build`, `brain people`, `brain connect`, and `brain review`.**

**1. `wiki/_person_name.py` (314 lines) → `src/brain/person_name.py`.** Its docstring calls it
the "single source of truth" for person-name normalization.

> **The importer list that stood here has been struck.** It listed `aliases/__init__.py:241`,
> `reconcile.py:172,206`, `config.py:885`, and `cross_type.py:165` as importers under the heading
> "Verified importers" — they are comments and docstrings, exactly what §0.1 bullet 6 retracts.
> **The single authoritative list is §9.2c.** For this module the real importers are
> `graph_rag/aliases/__init__.py:12` (module-level), `graph_rag/person_resolver.py:87`, and
> `queries.py:196` (both function-local).

**2. `wiki/build_people.py` (934 lines) — two files stapled together.** The **aggregation half**
(`aggregate_people`, `PersonRecord`, `DocRef`, `humanize_display_name`, `_build_directory_index`,
`_doc_participant_keys`, `_resolve_key_to_person`) powers the **live** `brain people` CLI
(`cli.py:234`, `review/weekly.py:40`, `graph_rag/person_resolver.py:23,88`,
`graph_rag/build.py:236`, `queries.py:246`) — and `queries.py` even carries a comment about
avoiding a top-level dependency on the wiki package. → **`src/brain/people.py`**.

> **VERIFIED 2026-08-11 — the "aggregation half" framing above is a LIVE TRAP.**
> **Spec said:** move the *aggregation half* of a module that is "two files stapled together".
> **Actual:** **the whole 934-line file moves. Do not split it.**
>
> The paragraph above and the "Corrected disposition" block below already contradict each other,
> and an executor reading only the bold heading will split a file that must not be split.
>
> **Evidence.** `grep -n Quartz src/brain/wiki/build_people.py` returns **seven lines** — `:340`,
> `:523`, `:524`, `:529`, `:581`, `:668`, `:804` — and **every one of them is a comment or
> docstring** (not six; count it, don't inherit it). The module contains no Quartz code at all, and
> its only writes go to `vault_path / "people"`. "Two files stapled together" is inaccurate for
> the same reason §9.2 warns about one section earlier: Quartz-referencing *prose* is not Quartz
> *output*.
>
> **All 25 top-level symbols move**, including the ones neither list above enumerates:
> `_DirectoryIndex`, `_doc_date`, `_vault_target`, `_sort_docs`, `_assign_slugs`,
> `_render_doc_line`, `_render_person_body`, `_people_dir`, `_existing_person_pages`, and the four
> `_PERSON_KIND_FRONTMATTER` / `_PEOPLE_INDEX_KIND_FRONTMATTER` / `_PEOPLE_DIR_NAME` /
> `_NO_DOCS_PLACEHOLDER` constants.
>
> A transitive-import probe (`importlib` + `sys.modules` diff, Python 3.11.15) confirms the module
> pulls in **no** `brain.wiki.*` dependency except `_person_name`, which moves with it. So no
> compatibility shim is needed, and nothing inside `wiki/` imports `build_people` at all.

> `vault/derived_links/directory.py:37` was listed here as an importer. **It is a docstring** —
> struck per §0.1 bullet 6. It still needs its prose updated when the module moves, but it does not
> break. **§9.2c is the authoritative list.**
>
> ⚠ **See Appendix B-2 (S8) before using it.** Every row naming
> `wiki.build_people` / `wiki._person_name` points at module paths that no longer
> exist. The list was accurate when written; it is now stale in 18 places.

**The markdown-emit tail SURVIVES TOO — an earlier revision of this document said "delete the
emit tail", and that was wrong and dangerous.** `emit_people_pages` is called at `cli.py:6944`
with `vault_path=cfg.vault_path`: **it writes into the vault, not into the Quartz build tree.**

Measured on the production database (read-only, 2026-08-10):

| Metric | Value |
|---|---|
| Documents with `vault_path LIKE 'people/%'` | **51** |
| …of which `kind='vault'` | **51 — all of them** |
| Inbound rows in `links` (things pointing *at* people pages) | **51** |
| **Outbound rows in `links` (people pages pointing *into* the corpus)** | **537** |

These are searchable corpus documents, wikilink targets, and are read in Obsidian and vim exactly
like the `_ingested/` mirrors. Deleting the emitter would strand 51 live documents **and 537
outbound edges**, and would leave §9.4's surviving `vault prune-orphans` step running over files
that nothing regenerates — which would then delete them.

> **Corrected disposition.** `emit_people_pages`, `render_person_md`, `render_index_md`,
> `EmitReport`, and `_write_if_changed` **move to `src/brain/people.py` with the aggregation
> half.** A live `ui/routes_people.py` on `aggregate_people()` is **additive — a nicer way to
> read the same data — not a replacement for the emitter.**

**This is the same class of error §0.1 corrects for the mirror steps**, made one section after
warning against it: mistaking "lives under `wiki/`" or "emits markdown" for "is Quartz output".
The test is not where the code lives or what it writes — it is **where the output lands**.
`_ingested/` mirrors and `people/` pages both land *in the vault*, and the vault survives (§1.3).

**3. `wiki/build_related.py` (815 lines) → `src/brain/related.py`.** `_avg_embedding` and
`_eligible_source_docs` feed `brain connect` (`connect.py:31,254`). Its docstring says "for the
Quartz wiki" and is **misleading**. Move the scoring logic parallel to `search.py`, exposing
`compute_related(doc_id, limit)` [⚠ **Appendix B-4 (S13)** — this signature cannot run: it omits both `conn` and the required `vector_sim_floor`]; `connect.py` repoints its import. **This is also how `brain ui`
gets its related-docs panel — computed live, not read from precomputed
`static/related/<slug>.json`.** Delete the JSON-emission wrapper.

> **VERIFIED 2026-08-11 — four corrections to the paragraph above.**
>
> **(a) Spec said** `_avg_embedding` and `_eligible_source_docs` feed `brain connect` at
> **`connect.py:31,254`**. **Actual:** `:31` is the import; **`:254` is a docstring.** The real
> call sites are **`connect.py:280`** (`_avg_embedding`) and **`connect.py:572`**
> (`_eligible_source_docs`). This is the same comment-counted-as-code error §0.1 bullet 6 retracts
> and §9.2 strikes twice — it recurs here.
>
> **(b) Spec said** move `_avg_embedding` + `_eligible_source_docs`. **Actual:** the surviving
> scoring half is **15 symbols plus 8 tuning constants**, and `_iter_hybrid_neighbors` must move
> with them — it is both the corpus-wide scoring entry point and the driver for the surviving test
> harness (`tests/test_build_related_signal.py`'s `_hybrid` helper, `:127-149`).
>
> | Half | Symbols | Fate |
> |---|---|---|
> | **Scoring — MOVES to `src/brain/related.py`** | `_iter_hybrid_neighbors`, `_SourceDoc`, `_Neighbor`, `_eligible_source_docs`, `_avg_embedding`, `_fts_candidates`, `_vector_candidates`, `_neighbors_for_source`, `_collapse_whitespace`, `_corpus_common_lexemes`, `_build_self_tsquery`, `_plainto_tsquery_text`, `_lexeme_to_tsquery_text`, `_to_tsquery_text`, `_top_body_lexemes` + `DEFAULT_RELATED_LIMIT`, `SNIPPET_LENGTH`, `_MIN_RRF_SCORE`, `_TOKEN_RE`, `_STOP_TOKENS`, `_BODY_FALLBACK_LEXEME_LIMIT`, `_MIN_TITLE_TOKENS_FOR_TITLE_ONLY`, `_CORPUS_FREQ_THRESHOLD` | move in phase 6 |
> | **Emission — STAYS in `wiki/build_related.py`** | `refresh_related`, `regenerate_related_json`, `RelatedSummary`, `RelatedEntry`, `_slug_from_vault_path`, `_quartz_slugify_segment`, `_target_path_for_slug`, `_prune_stale_related_files` | delete at **R3**, not phase 6 |
>
> **(c) "Delete the JSON-emission wrapper" is WRONG FOR PHASE 6 — it is an R3 action.**
> `wiki/build_swap.py:603` and `wiki/build_watcher.py:859` both `from .build_related import
> refresh_related`. Deleting the emitter in phase 6 breaks the wiki *while the wiki is still the
> live reading surface through R1/R2 dormancy*. **Correct shape:** `wiki/build_related.py` becomes
> a thin emitter that imports its scoring from `brain.related`. It dies with the rest of `wiki/`.
>
> ⚠ **Appendix B-1 (S1) — RESOLVED 2026-08-20; this pointer is updated rather than removed.**
> It used to read "this row places related-docs in a different phase from §11's two." §11 no
> longer has two: its phase-2 row is struck in place and **phase 5 owns the panel**, which is the
> same phase (d) below already names. All three statements now agree, so the row above is no
> longer in conflict with anything — what it describes is the *scoring* move, which landed.
> **Appendix B-7 (S18)** — the HTTP endpoint it implies still does not exist
> (`grep -c related src/brain/ui/app.py` → **0**, re-derived 2026-08-20) and is now owned:
> phase 5, gated on the open `vector_sim_floor` decision in B-4.
>
> **(d) `compute_related(doc_id, limit)` is NEW CODE, NOT A MOVE — do not budget it as
> migration.** No per-document entry point exists anywhere in the file. The closest thing,
> `_neighbors_for_source`, requires a `_SourceDoc` **plus** a `corpus_common` frozenset produced by
> a separate corpus-wide query (`_corpus_common_lexemes`). Authoring it is a **phase 5** need (the
> `brain ui` related-docs panel). **Phase 6 does not need it: `brain connect` only requires
> `_avg_embedding` and `_eligible_source_docs` to survive.**
>
> **PII hazard at migration time.** The tuning comment at `build_related.py:76-83` contains real
> corpus lexemes derived from live personal data (one is already partially redacted in place). It
> **must be scrubbed if any of that comment migrates into `src/brain/related.py`** — CLAUDE.md
> rule 15. The value is deliberately not reproduced here.

### 9.2b Requirement A12 — the deletion that breaks `import brain.cli`

**`cli.py:228` is a TOP-LEVEL import:**

```python
from .vault.quartz_overlay import OverlayError, apply_overlay, plan_overlay
```

§1.3 schedules `vault/quartz_overlay.py` for deletion at R3. **Delete that file without first
editing `cli.py:228` and the CLI fails at import — every command, every test, instantly.** This is
not a degraded feature; it is a dead package. The same applies to `cli.py:234,240,241`
(`wiki.build_people`, `wiki.install`), `connect.py:31`, `doctor_runtime.py:42`,
`review/weekly.py:40`, `graph_rag/aliases/__init__.py:12`, and `setup.py:22` — all **module-level**.

> **A12 — hard ordering constraint.** In every deletion commit, **remove the importing statement
> before or in the same commit as the imported module.** The verification is not `pytest` — it is
> `python -c "import brain.cli"`, which fails faster and more legibly. Add that as the first line
> of the phase-9 checklist.

The function-local imports (`cli.py:6655`, `graph_rag/build.py:236`,
`graph_rag/person_resolver.py:23,87,88`, `queries.py:196,246`, `setup.py:773`) fail only when
called, which is **worse** — a green import and a green fast test suite, then an
`ImportError` in production. They are enumerated in §9.2c so none is missed.

> **VERIFIED 2026-08-11 — A12 understates its own blast radius by ~8×.**
> **Spec said:** `cli.py:228` is *the* deletion that breaks `import brain.cli`.
> **Actual:** a bare `import brain.cli` loads **eight** wiki/quartz modules at import time.
>
> ⚠ **RE-RUN 2026-08-20 — it is now FIVE**, and the correction matters more than the number: the three that
> dropped out are exactly the three modules that moved (`_person_name`, `build_people`, `build_related`).
> **The blast radius shrank because phase 6's work was done, not because the probe was wrong.** Both the
> "eight" above and the nine-row table below are pre-move measurements. Re-run the probe; do not inherit
> either figure. Current set: `brain.vault.quartz_overlay`, `brain.wiki`, `brain.wiki.build_swap`,
> `brain.wiki.errors`, `brain.wiki.install` (Python 3.11.15, worktree `.venv`, `sys.modules` diff).
>
> Measured by running it (Python 3.11.15, worktree-resolved `brain`) and diffing `sys.modules`:
>
> ```
> brain.vault.quartz_overlay   brain.wiki                brain.wiki._person_name
> brain.wiki.build_people      brain.wiki.build_related  brain.wiki.build_swap
> brain.wiki.errors            brain.wiki.install
> ```
>
> Three of those are pulled in *transitively* and appear in no import statement in `cli.py`:
> `brain.wiki._person_name` (via `build_people`), `brain.wiki.build_related` (via `connect.py:31`),
> and `brain.wiki.errors` (via `build_swap`). An executor auditing only `cli.py` will miss them.
>
> **The nine module-level statements that must be removed before or with their targets** — this is
> the complete A12 list, and it is one longer than the prose above implies:
>
> | # | `file:line` | Statement |
> |---|---|---|
> | 1 | `cli.py:228` | `from .vault.quartz_overlay import OverlayError, apply_overlay, plan_overlay` |
> | 2 | `cli.py:234` | `from .wiki.build_people import (…)` → repoint to `.people` |
> | 3 | `cli.py:240` | `from .wiki.install import WikiInstallError` |
> | 4 | `cli.py:241` | `from .wiki.install import wiki_install as _wiki_install` |
> | 5 | `connect.py:31` | `from .wiki.build_related import _avg_embedding, _eligible_source_docs` → `.related` |
> | 6 | `doctor_runtime.py:42` | `from .wiki.build_swap import EXIT_CONFIG_ERROR` |
> | 7 | `review/weekly.py:40` | `from ..wiki.build_people import _doc_participant_keys` → `..people` |
> | 8 | `graph_rag/aliases/__init__.py:12` | `from brain.wiki._person_name import humanize_person_name` → `brain.person_name` |
> | 9 | `setup.py:22` | `from .wiki import QUARTZ_PINNED_COMMIT, QUARTZ_REPO_URL` |
>
> A12's verification command (`python -c "import brain.cli"`) is **correct and confirmed** — it is
> the right gate. It just guards more than one line.

### 9.2c The authoritative importer list

> ⚠ **RE-DERIVED 2026-08-20 — the table below is stale in more than half its rows. Do not plan from it.**
> Appendix B-2 (S8) recorded that its `build_people` / `_person_name` rows point at modules that no longer
> exist. Re-running the section's own grep today returns **6** statements outside `src/brain/wiki/`, against
> this table's **12** rows — the three moved modules took their importers with them. **The current list, and
> the line numbers that moved, are in the 2026-08-20 re-derivation at the end of Appendix B.** One row is
> now actively dangerous rather than merely stale: `cli.py:234` no longer imports `wiki.build_people`, it
> imports `vault.quartz_overlay` — the line survived and changed meaning, so an executor auditing by line
> number edits the wrong statement and still sees a plausible import.

Produced by a grep **restricted to import statements**
(`^\s*(from|import)\s+.*\bwiki\b`), after §0.1's sixth bullet was found to have counted comments:

| File:line | Symbol | Scope |
|---|---|---|
| `cli.py:234` | `wiki.build_people` (several) | module-level |
| `cli.py:240,241` | `wiki.install.WikiInstallError`, `wiki_install` | module-level |
| `cli.py:6655` | `wiki.build_swap.resolve_build_timeout_s` | function-local |
| `connect.py:31` | `wiki.build_related._avg_embedding`, `_eligible_source_docs` | module-level |
| `doctor_runtime.py:42` | `wiki.build_swap.EXIT_CONFIG_ERROR` | module-level |
| `graph_rag/aliases/__init__.py:12` | `wiki._person_name.humanize_person_name` | module-level |
| `graph_rag/build.py:236` | `wiki.build_people._build_directory_index` | function-local |
| `graph_rag/person_resolver.py:23,87,88` | `wiki.build_people._DirectoryIndex`, `wiki._person_name.expand_owner_keys`, `wiki.build_people` (several) | function-local / `TYPE_CHECKING` |
| `queries.py:196,246` | `wiki._person_name.normalize_person_name`, `wiki.build_people.aggregate_people`, `humanize_display_name` | function-local |
| `review/weekly.py:40` | `wiki.build_people._doc_participant_keys` | module-level |
| `setup.py:22` | `wiki.QUARTZ_PINNED_COMMIT`, `QUARTZ_REPO_URL` | module-level |
| `setup.py:773` | `wiki.install.wiki_install` | function-local |

Plus `cli.py:228` → `vault.quartz_overlay` (module-level), and `maintenance.py:85`, which is a
**subprocess invocation** of `python -m brain.wiki.build_swap`, not an import — it fails at
runtime with a non-zero exit, not at import.

### 9.2d Phase 6 contains NO deletions — ordering, added 2026-08-11

**This subsection did not exist. It is the ordering rule the rest of §9.2 assumes but never
states**, and without it an executor will combine moving and deleting into one commit.

1. **Phase 6 is moves + import repointing + test repointing. Nothing is deleted.** Deletions are
   R3 (§9.4, §9.5). The gate after phase 6 is `python -c "import brain.cli"` (A12), then
   `bin/brain-ci`.
2. **`wiki/build_related.py` must survive phase 6** as a thin emitter importing `brain.related` —
   `build_swap.py:603` and `build_watcher.py:859` need `refresh_related` while the wiki is still
   the live reading surface. `_person_name.py` and `build_people.py` need **no** shim: nothing
   inside `wiki/` imports either one (verified by intra-package import grep + transitive probe).
3. **A12 applies per commit:** remove the importing statement before or in the same commit as the
   imported module, for all nine module-level sites in §9.2b's table.
4. **Source and its tests move or die in the same commit.** The `e2e` marker is *deselected*, not
   *uncollected* — `addopts` filters at selection time, so orphaned test files still break
   collection with `ImportError`. See §9.3 trap 5 and its verification block.
5. **`emit_people_pages` and `vault prune-orphans` are coupled** — both live, or the surviving
   pruner deletes 51 vault documents (§9.2, §9.4).
6. **A10 touches more of `maintenance.py` than §9.4 lists:** `:42` (stage-id tuple), `:127` (stage
   registration), `:148` (`only = ["wiki"]`), `:266` (`stage_id == "wiki" and clean_cache`), plus
   the `--wiki-only` plumbing at `:132,140,142-147,300,344`. Note `--clean-cache` targets the
   **Quartz parser cache** and dies with Quartz.

### 9.3 TRAPS — the mistakes an executor will actually make

1. **`tests/test_wiki_link_parser.py` is NOT a wiki test.** It covers
   `brain.vault.links.parse_wiki_links` — the `[[wikilink]]` syntax parser used throughout vault
   sync. **"wiki" is badly overloaded in this codebase** between "the Quartz site" and "wikilink
   syntax". A grep-for-`wiki`-and-delete sweep destroys vault-core coverage.
   > **Grep-based deletion sweeps are unsafe in this repository. Every deletion is reviewed
   > individually against its importers.**
2. **`vault/daily_index.py`** looks like Wiki UX Overhaul residue; it runs unconditionally in
   `brain daily`. Deleting it breaks `brain daily`.
3. **`derived_links`** looks like wiki decoration; it is `brain connect`'s data source.
4. **`bin/_brain-watcher-fg` vs `bin/_brain-build-fg`** — near-identical wrappers, opposite
   fates. `_brain-build-fg` dies (Quartz rebuild watcher); `_brain-watcher-fg` **survives**
   (vault sync). Likewise `com.brain.build` deletes while `com.brain.watcher` and
   `com.brain.brief` stay.
5. **Source and tests must be removed in the same change.** 41 of the **440** Python files under `tests/` (~9.3%) are
   wiki/Quartz: 19 `tests/test_quartz_*.py`, `test_wiki_install.py`, `test_docs_assets_wiki.py`,
   20 under `tests/wiki/`, plus `quartz_e2e_helper.py` and `fixtures/quartz_e2e_vault/`. The
   `e2e` marker is deselected by default (`pyproject.toml:483`) but **default `pytest` still
   COLLECTS these files** — removing source without tests breaks collection with import errors.
   > **VERIFIED 2026-08-11 — the arithmetic is right and the conclusion is dangerously
   > incomplete.** The 41 checks out (19 `tests/test_quartz_*.py` + 20 `.py` under `tests/wiki/` +
   > `test_wiki_install.py` + `test_docs_assets_wiki.py`), all 19 `test_quartz_*` files are
   > genuinely Quartz-asset tests, and `pyproject.toml:483` is correct **at HEAD** (`git show
   > HEAD:pyproject.toml`).
   >
   > **Spec said:** 41 wiki test files, delete them with their source.
   > **Actual:** **16 MORE test files import wiki machinery and must be REPOINTED or SPLIT, not
   > deleted.** A `grep -rln 'brain\.wiki|quartz_overlay|brain\.quartz_overrides' tests/` returns
   > 57 files, not 41. Deleting the difference destroys the test harness for the very scoring code
   > §9.2 is preserving.
   >
   > | Test | Fate |
   > |---|---|
   > | `tests/test_person_name.py` | **MOVE** with source |
   > | `tests/test_build_people.py` | **MOVE** — incl. 4 `mocker.patch` target strings at `:1570,1578,1604,1612` |
   > | `tests/test_build_related_signal.py` (1119 lines, 24 tests) | **MOVE (mostly)** — its `_hybrid` helper (`:127-149`) drives `_iter_hybrid_neighbors`, which survives; only its 2 `regenerate_related_json` call sites need surgery |
   > | `tests/test_tsquery_stem_stability.py:240,265,284,303` | repoint → `brain.related` |
   > | `tests/test_connect.py:375` | repoint |
   > | `tests/test_graphrag_build.py:597,621` | repoint |
   > | `tests/test_cli_people.py` | docstring prose only |
   > | `tests/test_doctor_runtime.py:286,309` | modify |
   > | `tests/test_setup.py:436` | modify (patches `brain.wiki.install.wiki_install`) |
   > | `tests/test_maintenance.py:141,149` | modify (A10 — asserts `build_watcher` argv) |
   > | `tests/test_bin_scripts.py:220,221,283` | modify (asserts `build_swap`/`build_watcher` never spawned) |
   > | `tests/test_build_related.py`, `_idempotence.py`, `_schema.py` | delete at **R3** (pure emission) |
   > | `tests/test_brain_recent_homepage.py` | delete at R3 (`wiki.build_homepage`) |
   > | `tests/test_cli_vault_render.py` | delete at R3 (`wiki.build_swap`) |
   > | `tests/test_packaging_overlay.py` | delete at R3 — it guards the `pyproject.toml:440,514` package-data globs |
   > | `tests/test_quartz_overlay.py` | delete at R3 |
   >
   > **DO NOT GATE ON THE FILE COUNT.** The spec says 440 `.py` under `tests/`; it now measures
   > **445**. That delta is **not drift and not a discrepancy** — it is exactly the five untracked
   > `tests/test_ui_*.py` files from concurrent phase-0 work. The 440 was correct at `f8c76c0`.
   > Re-derive the wiki test set from imports, never from a total.
6. **`docker-compose*.yml` have zero wiki references.** The wiki was never containerized. Do not
   look there.
7. **`BRAIN_USER_EMAIL` — resolved, and the answer changes the trap.** `Config.user_email`
   already exists (`config.py:770`, loaded `:1064-1065`, serialized `:2169`) — **the per-request
   home §3.2's email-thread mode wants is already built.** But **nothing in `src/brain/` reads
   `cfg.user_email`**; today's only consumers are the Quartz transformer and wiki tests. So the
   *config plumbing survives and is ready*, while the *only current readers* are deleted. Wire
   the ported reading mode to `cfg.user_email`; do not delete the config field.
8. **Nothing from `dist/` may be committed except `brain-logo.svg`** (§7.2) — `dist/` contains
   real ingested content (CLAUDE.md rule 15).
9. **Two more shared modules that look deletable and are not** (added 2026-08-11, verified):
   - **`src/brain/log_rotation.py`** is imported by the *dying* `wiki/build_watcher.py:47` **and**
     by the *surviving* `vault/watch.py:88`. It **survives**.
   - **`src/brain/vault/slug.py` is a different file from `src/brain/wiki/slug.py`.** Two files,
     one name, opposite fates: `wiki/slug.py` dies, `vault/slug.py` survives — `build_people.py:43`
     imports `brain.vault.slug.slugify`, and that import moves to `src/brain/people.py`. This is
     trap 1's overloading problem in a second guise.
   - Also confirmed **not** wiki machinery despite appearances: `bin/brain-rebuild`,
     `src/brain/maintenance.py`, and `tests/test_wiki_link_parser.py` (trap 1).

### 9.4 The `wiki` stage is re-formed, not deleted

Per §0.1 and §8.1, `wiki_steps` (`maintenance.py:70-88`) is five steps of which only the last two
are Quartz.

> **A10.** At R2/R3, **split** the stage: rename the surviving three steps (`vault export`,
> `vault sync-summaries`, `vault prune-orphans`) into a `mirror` stage, and delete only
> `vault render --overlay` and `python -m brain.wiki.build_swap`. Drop `--wiki-only`; keep
> `--only mirror` working. Deleting the stage wholesale stops mirror maintenance for 1,257
> ingested documents and breaks external reading (§8.1).
>
> **Ordering hazard — `prune-orphans` is a deleter.** It survives into the `mirror` stage, so if
> §9.2's People Hub emitter were removed (as an earlier revision proposed) while `prune-orphans`
> kept running, the surviving prune step would find 51 `people/` files that nothing regenerates
> **and delete them.** The two decisions are coupled: **the emitter and `prune-orphans` must live
> or die together.** They live (§9.2).

### 9.5 Surface changes — modify, do not delete

| Surface | Change |
|---|---|
| `brain doctor` | Strip `wiki_freshness_doctor_check` (`doctor_runtime.py:654-702`), `WIKI_STALE_DAYS` (`:62`), the `wiki_stale` branch (`:355-470`), and the `wiki.build_swap` import (`:42`). Keep dotenv + surviving-daemon checks. **Add** the watcher-not-running check of A9. |
| `brain setup` | Strip `_check_caddy` (`:291`), `_check_quartz_sha` (`:330`), `_maybe_install_wiki` (`:737`), the `wiki_install` import (`:773`), and the `offer_wiki` **`SetupProfile` field** (`:60`; set `:91,103,115`; branched `:422,1304`) — it is a field, not a function, the `QUARTZ_PINNED_COMMIT` import (`:22`), and the `full` profile's wiki provisioning. |
| `_maybe_install_launchd` (`setup.py:826-897`) | **Currently gates the entire launchd install — including the surviving vault-sync watcher — behind `wiki_installed`.** Must be rethought: offer the watcher unconditionally? Q8. |
| `bin/brain-rebuild` | A10. |
| **`brain vault render` (`cli.py:6453`)** | **Was missing from this table entirely.** It shells `npx quartz build` (`:6641-6665`) and imports `resolve_build_timeout_s` from `brain.wiki.build_swap` (`:6655`). The command goes; see A12 for the import that goes with it. |
| `bin/monitor.py` | `:216,439` pgrep `brain.wiki.build_watcher` — repoint or remove. |
| Config | Delete `templates/Caddyfile.j2`, `--wiki-port` (default 8080), `BRAIN_WIKI_RELOAD`, `BRAIN_WIKI_KEEP_BUILDS`, `BRAIN_WIKI_BUILD_TIMEOUT_S`. |
| `docs/vault-and-wiki.md` | Splits cleanly at the section boundary: lines 9–135 (`## Vault model`) survive; 136–388 (`## Wiki (rendered view, optional)`) go. Rename the file. |
| Auto-memory | Update `quartz.md` (delete), `vault.md`, `cli.md`, `MEMORY.md` index (CLAUDE.md rule 11). |

> **VERIFIED 2026-08-11 — five corrections to the table above.** Everything not listed below was
> checked and is **exact**: the `doctor_runtime` anchors (`:62`, `:355`, `:458`, `:654`, `:690`,
> `:697`), every `setup.py` line ref (`:60,91,103,115,291,330,422,737,773,826,1304`), `brain vault
> render` at `cli.py:6453`, and the `docs/vault-and-wiki.md` split (file is 388 lines, `## Vault
> model` at **9**, `## Wiki (rendered view, optional)` at **136** — correct to the line).
>
> **(1) `bin/monitor.py` — WRONG PATH, and the scope is ~10× understated.**
> **Spec said:** `bin/monitor.py`, repoint or remove `:216,439`.
> **Actual:** **there is no `bin/monitor.py`.** `bin/brain-monitor` exists and contains **zero**
> wiki/quartz/`build_watcher` references. The real file is **`src/brain/bin/monitor.py`**, where
> `:216` and `:439` are exactly right. Line numbers correct, path wrong — an executor following
> this row literally finds no file.
> Further, "repoint or remove `:216,439`" understates the coupling badly. That module is
> Quartz-aware throughout: `:30` (`BRAIN_WIKI_PORT`, default 8080), `:65` (`.quartz` skip-dir),
> `:114-115`, `:126`, `:136`, `:227`, `:240`, `:399` (`.quartz/current`, `.quartz/builds`,
> `.build-id`), `:257-259` ("wiki url"), `:433` ("build_swap mismatch"), `:469`. **Its entire
> build-freshness section is Quartz** and goes with it.
>
> **(2) The `brain wiki` command group is missing from this table.** Ironic given the `brain vault
> render` row is annotated "was missing from this table entirely". **Also delete:** the `wiki_app`
> Typer group at `cli.py:349-354` (registered `:354`), its only command
> `@wiki_app.command("install")` at `cli.py:8415`, and `--wiki-port` at `cli.py:8425`.
>
> **(3) `setup.py` has a THIRD `wiki_installed` consumer.** The row names `_maybe_install_wiki`
> (`:737`) and `_maybe_install_launchd` (`:826`). It omits **`_print_final_report`
> (`:898-925`)**, which takes both `wiki_port` and `wiki_installed` and prints the wiki URL +
> `caddy run` hint at `:921-923`. Called at `:1352`.
>
> **(4) Three more `bin/` scripts, unmentioned anywhere in §9** (trap 4 names only
> `_brain-build-fg`): **`bin/brain-verify-fastpath`** (fast-path smoke test — Quartz
> `.cache/fastpath`, requires the wiki watcher), **`bin/brain-wiki-gif`**, and
> **`bin/brain-wiki-gif-capture.cjs`**. Also delete
> `src/brain/templates/launchd/com.brain.build.plist.j2`.
>
> **(5) `src/brain/uninstall.py:24` is an unraised decision.** `_BRAIN_HOME_SAFE_REMOVES` lists
> `"Caddyfile"` and `"build"` — wiki runtime artifacts. Not a breakage either way.
> **Recommendation: keep them**, so machines with a pre-retirement install still get cleaned up.

### 9.5b Explicitly NOT verified — do not inherit these as facts

This document has shipped four claims that looked like evidence and were not. The following were
**not** confirmed during the 2026-08-11 pass and are recorded as gaps rather than quietly
inherited:

- **`doctor_runtime.py:654-702` and `:355-470` as exact ranges.** The *anchors* were confirmed
  (`:62`, `:355`, `:458`, `:654`, `:690`, `:697`); the range endpoints were not bounded. Re-derive
  them at edit time rather than trusting the spans.
- **Whether `bin/brain-ci` is green at HEAD.** No test suite was run — the test-DB lock is
  machine-wide and a concurrent agent was active. Every "the suite stays green" assertion in §9 is
  therefore unproven, including trap 5's.
- **`emit_people_pages`'s on-disk/DB agreement was confirmed** (51 `.md` files under
  `<vault>/people/`, of which one is `index.md`; 51 rows in `documents`) — this one is *not* a gap
  and is recorded here only to distinguish it from the two above.

### 9.6 The Publish tab becomes false

`index.html:103-115` tells the user the Publish tab is deferred *because* the wiki is a Quartz
build. At R2 that is a lie in the UI. Rewrite it, or delete the tab from the bar
(`index.html:28-33`). Q10.

---

## 10. Risks and rejected alternatives

### 10.1 The steelman — what we are actually giving up

This section exists because a spec that does not state its own counter-argument will not survive
scrutiny.

**We are trading the wiki's two strongest properties — *always up* and *always fast* — for
editability and better ranking.** Concretely:

- **Availability.** `brain ui` probes Postgres at startup and **refuses to bind** if it is down.
  The wiki serves pre-built static HTML and is completely unaffected by Postgres, Ollama, or
  Docker being down. An Ollama-only outage degrades gracefully (503 on `/api/search`), but a
  Postgres outage is a **total** outage of the reading surface. There is no offline or cached
  mode by design: no service worker, no cache.
- **Latency — sampled, variable, and better than this document first assumed.** The wiki's search
  is instant client-side FlexSearch over a prebuilt index. `brain ui`'s four known samples span
  **0.43 s / 0.44 s / 3.6 s / 4.4 s** (R-1). Warm queries land near **0.44 s**; a cold embed or a
  high-recall common word costs seconds. So the gap is real but is *not* the flat two-orders-of-
  magnitude regression an earlier revision implied — and the trade is favourable more often than
  it looked: sub-second to an answer that is *correct*, versus instant to an answer that requires
  you to have guessed the right keyword. **It must still be designed for (R-1), not waved past**,
  because the worst case is the one the user notices.
- **Every wiki incident cited in §0 was a build-time or daemon bug — during which the published
  site kept serving correctly.** The 12-day staleness, the 496 MB log, `render --to .`: in all
  three the *reader* was fine, just looking at older content. **That separation between
  build-failure and read-failure is exactly what static buys, and consolidation forfeits it.**
- **Foreground-only, no daemon.** Deliberate — "daemonizing a write-capable HTTP server that
  could outlive the user's attention is a bad idea". But the wiki has three launchd daemons and
  survives reboots; closing the terminal kills `brain ui` access entirely.

None of this reverses the decision — a fast, always-up surface that cannot answer a paraphrased
query is not actually serving the use case. But these are real losses, and §10.2 tracks each as
a risk with a named mitigation rather than pretending they are not there.

### 10.2 Risks

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| **R-1** | **Latency is sparsely sampled and highly variable — not "unmeasured", and an earlier revision's "decisive" evidence was false.** That revision asserted `search_queries.duration_ms` had **zero** non-null rows. Re-run read-only on 2026-08-10 it has **three**: **434 ms, 441 ms, 3602 ms**, all `source='cli'`, all stamped 2026-08-10 12:30:21/33/41 — almost certainly this session's own design run. Separately, §7.1's live probe measured **4.4 s** (embed 2.6 / rank 1.7) for a common-word query. **Both are real.** The honest reading: one cold sample near 3.6 s, two warm samples near **0.44 s** — *below* §10.1's "optimistic 1.1 s" and the same order as the `fts_only` escape hatch — and one 4.4 s outlier on a common (thus high-recall) query. **n=4 is a probe, not a distribution.** | **High** | **Perceived-performance design is required regardless** — skeleton rows, FTS-first progressive paint (the FTS leg can paint while the vector leg resolves), and a wait state that clears AA (today it is one 11 px word at a failing 2.69:1 grey). Phase 0 still gathers a real distribution. `BRAIN_OLLAMA_KEEP_ALIVE=-1` (confirmed in local `.env`) kills the cold cliff; 180 ms debounce + `AbortController` already exist. **Note the correction cuts in favour of the migration** — warm search may be far faster than this document assumed. |
| **R-1b** | **Eight measured design defects already ship** (§7.1.3 D1–D8) — a 92-character measure, duplicated `<h1>`s, no working depth, seven AA failures including the ⌘K hint at 2.89:1, a selection state failing SC 1.4.11, a raw UUID in the ledger's widest column, an undesigned edit mode, and no `color-scheme`. New surfaces layered on top would inherit all of them. | **High** | A11 — fix in phase 3, before the phase-2 surfaces are layered on. Cheapest and most visible: D2, D4, D6. |
| **R-2** | **`brain ui` has never run against production.** `search_queries` has **zero** rows with `source='ui'` — re-verified read-only 2026-08-10, and still true even after §7.1's live design probe (that probe's 3 timing rows are all `source='cli'`). No incident history because there is no history. | **High** | Phase 0: run it against the live corpus and record findings before any port work. Every latency, correctness, and UX assumption is currently untested at real scale. |
| **R-3** | **Postgres down = total reading outage** (§10.1). | **High** | Accept and document, or add a read-only cache — **which the zero-dependency constraint makes expensive**. At minimum, R2's dormancy period is the safety net: the wiki is still there. |
| **R-4** | **LAN access regression.** `Caddyfile.j2` binds `:{{ wiki_port }}` with no host prefix — Caddy defaults to all interfaces — and runs on the host, so the wiki is reachable from a phone on the LAN today with **zero config and zero auth**. `brain ui` defaults to loopback and requires `--host` + `--token`. **This is simultaneously a security fix and a workflow regression.** | **High** | **The user reads on other devices today. The spec must state what replaces that** — Q1. Do not frame it as purely a security win. |
| **R-5** | **Draft loss on 409** (§8.3 G1). | **High** | A7, before R1. |
| **R-6** | **Reading regression noticed weeks later**, after the code is gone. | High | R1→R2→R3 ladder with a **user-confirmed** gate before R3; §3's matrix as the checklist. |
| **R-7** | **New XSS sink via a markdown plugin.** | High | A3, mutation-verified. Precedent: `render.py:11-18`. |
| **R-8** | **Deleting something load-bearing** — most plausibly `_person_name`, `build_people`'s aggregation half, `daily_index`, or the mirror steps of the `wiki` stage. | High | §9.2 moves land **before** any deletion; §9.3 traps; §9.4; `bin/brain-ci` green after each deletion commit. |
| **R-9** | **`_ingested/` last-writer-wins ambiguity** (§8.3 G2) surprises the user once external editing is encouraged. | Med | A8 — decide and pin with a test. |
| **R-10** | **Graph scope explosion** — 15 features × 4 surfaces, 2,303 lines of upstream inline TS. | Med | Q3 forces a must-have subset before any code; §6.3's option (d) covers the common case at zero cost. |
| **R-11** | **Graph panel over empty tables** — `BRAIN_GRAPH_ENABLED` is false under `minimal`/`standard` profiles. | Med | §6.4 honest degraded state. |
| **R-12** | **Back/Forward is broken by design-omission.** State uses `history.replaceState` (debounced 200 ms), not `pushState`, so Back does not step through prior searches or notes. Every wiki page is a real navigation today. | Med | Likely underspecified rather than intentional. Add to the phase-2 navigation work. |
| **R-13** | **No-JS / view-source / `curl` a note / text-browser reading is permanently lost** — content arrives via JSON after boot. | Med | **Architecturally inherent** to SPA-over-API; the only alternative (server-rendered HTML) is ruled out by binding decision 1. Accept and record. |
| **R-14** | **`app.js` becomes unmaintainable** under the weight of new panels. | Med | A1 is phase 0 and blocks everything. |
| **R-15** | **Losing the orphan logo** to a clean rebuild. | Med | A4, phase 0, into `src/brain/ui/static/`. |
| **R-16** | **Test collection breaks** when source is removed without tests. | Med | §9.3 trap 5 — same change. |
| **R-17** | **`bin/brain-ci` divergence** — green locally, red in CI. | Low | auto-memory `feedback_local_vs_ci_divergence.md`: the repo `.venv` is stale and on the wrong Python. Gate on `bin/brain-ci`. |
| **R-18** | **Coverage floor breach** (85% overall, 95% pure). | Low | CLAUDE.md gates; `render.py`, `related.py`, `people.py` target 95%. |

### 10.3 Corpus scale — the measured baseline

Read-only, 2026-08-10: **1,392 documents** (135 vault-tier / 1,257 ingested), **13,113 chunks**,
**841 sources** (488 gmail / 264 krisp / 88 manual / 1 slack), **1,115 links**, **6,589 graph
entities**, **387 MB**, 27 migrations. (`2026-07-25-brain-ui-design.md`'s "1,195 docs / 20,871
chunks" is stale.)

**The corpus is modest. Latency is the embedding round-trip, not DB scale** — which is why R-1's
measurement task matters more than any query optimization.

### 10.4 Rejected alternatives

**RA-1 — Keep both surfaces indefinitely.** *Rejected.* Two renderers over one corpus means every
vault-format decision must satisfy two consumers and every content bug is reproduced twice. The
status quo *is* this alternative, and it produced the incidents in §0.

**RA-2 — Improve Quartz's search: emit `hybrid_search` results into `contentIndex.json`.**
*Rejected on impossibility.* `hybrid_search` embeds the *query* at request time against a live
model and compares against a live pgvector index. A static build cannot embed a query it has
never seen. The best achievable is precomputing results for a fixed query list, which is not
search.

**RA-3 — Make `brain ui` a shell that iframes the Quartz build.** *Rejected.*
`frame-ancestors 'none'` (`security.py:70`) and `default-src 'none'` would both have to be
relaxed, forfeiting §2.5 wholesale, while keeping 100% of the build machinery and inheriting the
staleness problem.

**RA-4 — Port `brain ui`'s features into Quartz instead.** *Rejected on architecture.* Quartz has
no request-time server and no DB connection. Every live feature needs a sidecar — at which point
the sidecar *is* `brain ui` and Quartz is a redundant client. It also moves the codebase's centre
of gravity to TypeScript-on-npm, violating §2.1.

**RA-5 — Adopt a JS framework/bundler to make the port easier.** *Rejected.* Violates §2.1,
breaks the offline guarantee (**and fails `test_ui_static_assets.py:137`**), and adds a build step
to a tool that has none. The answer to "`app.js` is getting big" is A1, which needs no tooling
because `index.html:20` already loads ES modules natively.

**RA-6 — Delete the wiki first, then port.** *Rejected.* Leaves the user without a reading
surface for the duration. §11's ordering makes it impossible.

**RA-7 — Change the vault format to suit `brain ui`.** *Rejected as a default* (R4, §1.3). The
vault is read by Obsidian and vim; reformatting a corpus to suit a renderer is the tail wagging
the dog. Available only on explicit request, per concession, with evidence.

**RA-8 — Keep the wiki as a read-only fallback for when Postgres is down.** *Rejected, but it is
the strongest surviving counter-proposal* (§10.1, R-3). It preserves the availability property at
the cost of keeping the entire 26,000-line build chain alive to serve an outage that has not been
measured to occur. **If the user values this, R2 (dormant) is already the answer** — a dormant
build can be run on demand — and R3 becomes a decision to accept the availability loss outright.
Recorded so the option remains visible at the R3 gate.

---

## 11. Phasing

**Principle: the user is never without a usable reading surface.** The wiki works through every
phase until the user says otherwise.

| Phase | Name | Contents | Exit criterion |
|---|---|---|---|
| **0** | **Rescue, measure, repair** | **A4** commit the orphan logo into `src/brain/ui/static/` (one file, nothing else from `dist/`). **R-1** instrument and measure real search latency; populate `search_queries.duration_ms`. **R-2** run `brain ui` against the production corpus and record findings. **A1** split `app.js`/`app.css`; extend `package-data` + `test_ui_static_assets`. | Logo committed. A measured latency distribution exists. `brain ui` has been exercised at real scale. `bin/brain-ci` green, no behaviour change. |
| **1** | **Rendering parity** | §5: tables, strikethrough, task lists, footnotes, callouts, syntax highlighting, block-math LaTeX (**A5** — inline `$` stays off, with its own test). Each with a mutation-verified XSS test (**A3**). | Every markdown construct the corpus contains renders correctly. `render.py` coverage ≥95%. |
| **2** | **Navigation + content parity** | §3.1/§3.2/**§3.1a** **PORT** rows: command palette, **table of contents**, explorer counts + ingested toggle + month grouping, backlinks marginalia (`vault/graph.py:163`), ~~related-docs (after the §9.2 move)~~ [⚠ **STRUCK — Appendix B-1 (S1), re-derived 2026-08-20.** The *scoring* move landed in phase 2; the **panel does not**. Its HTTP endpoint does not exist (`grep -c related src/brain/ui/app.py` → **0**, still) and is gated on the open decision in B-4. **Phase 5 owns it** — see that row and §9.2d(d).], summary lede, link-kind system, email-thread mode (simplified), recently-captured rail, tag index, people view, source icons, breadcrumbs. **R-12** `pushState` navigation. | Every **PORT** row demonstrably works. |
| **3** | **Identity + defect repair** | **A13 first — inline the token block, or phase 3 has no verifiable exit criterion.** §7.1's "Instrument & Page" token system; §7.4 logo + favicon; **A11 fixes D1–D8**; **Q14** font delivery; **Q15** date in the search payload; **Q16** server-side snippet rendering; R-1's perceived-performance work (skeleton + FTS-first paint); **Q18** extend the offline test to CSS `url()`. | Logo renders in both themes and ships in the wheel. D1–D8 closed. Every text token clears AA on every permitted ground; selection carries wash **plus** marker rail. |
| **4** | **Write-path hardening** | **A7** draft buffer (blocks R1). **A8** `_ingested/` invariant + test. **A9** watcher dependency + doctor check. C3 focus-refetch. **Q4** external-editor decision. | C1–C7 each covered by a test. |
| **5** | **Graph** — *and the related-docs panel* | §6, after **Q2** and **Q3**. Sidebar local graph first. **Plus the related-docs panel struck from phase 2** (§9.2d(d) already schedules its authoring here): a route module, an `app.py` registration, a client consumer — and **the B-4 ruling first**. `compute_related` itself is already written and tested (Appendix B-17); only its consumer is absent. | Reads materialized state only; honest degraded state when unbuilt. **Related-docs:** the panel answers over HTTP, or the row is not exited. |
| **6** | ⚠ *(Appendix B-3 / S9, **re-derived 2026-08-20:** **all three** of the moves this row names have already landed in phase 2 — not "three of four", a fourth was never specified; §9.2 numbers exactly three. `person_name.py`, `people.py` and `related.py` all exist, every importer already points at them, and `wiki/build_related.py` is down to 248 lines as the thin emitter §9.2d(2) requires. **The move budget for this phase is zero.** Only "No deletion in this phase" is still load-bearing.)* **Move, do not delete** | §9.2: `_person_name.py` → `person_name.py`; `build_people.py` aggregation → `people.py`; `build_related.py` scoring → `related.py`. Repoint every verified importer. **No deletion in this phase.** | Full suite green with `wiki/` still present but no longer imported by non-wiki code. |
| **7** | **R1 — Demote** | Stop pointing the daily loop at the wiki. Nothing deleted. **Gate: A7 has landed.** | User reads in `brain ui` by default. |
| **8** | **R2 — Dormant** | **A10** re-form the `wiki` stage into a `mirror` stage; delete only the two Quartz steps. Uninstall `com.brain.build`; keep `com.brain.watcher`/`com.brain.brief`. **Q8** launchd gate. **Q10** Publish tab. | No Quartz build runs unless requested. Mirror maintenance still runs. Suite green. |
| **9** | **R3 — Delete** | **A12 first: `python -c "import brain.cli"` must pass after every commit.** §9.2c importer list worked top-down; §9.5 surface changes including `brain vault render` and `cli.py:228`; §9.3 traps observed; source **and** tests in the same commits. | **Gate: explicit user confirmation they have stopped reaching for the wiki**, and an explicit decision on RA-8. Then `bin/brain-ci` green, no orphaned imports, docs + auto-memory updated. |

> **VERIFIED 2026-08-11 — phase 1's exit criterion was scoped to the wrong tier.** Spec said
> "every markdown construct **the vault** contains renders correctly"; `brain ui` serves **both**
> tiers from `documents`, and the affected documents are overwhelmingly `_ingested` — tables
> **461 ingested / 7 authored**, task lists **124 / 21**, strikethrough **38 / 0**, footnotes
> **3 / 0**. Read literally, phase 1 could satisfy this criterion by fixing 7 tables and 21 task
> lists while 461 and 124 respectively still rendered as literal pipes and brackets — exiting the
> phase with **~96% of affected documents still wrong**. **Corrected to "the corpus."** Evidence:
> counted twice and independently — a read-only `SELECT` over 1,393 DB documents (1,257 ingested +
> 136 vault) and a filesystem scan of 1,400 on-disk content files (1,258 `_ingested` + 142
> authored), which agreed on every construct. Full per-construct table and the `.quartz/`
> counting trap are in the §5.1 correction.

**Phase 0 blocks everything. Phases 1–5 may reorder. Phase 6 must precede 8 and 9.
7 → 8 → 9 is strictly ordered.** Per CLAUDE.md rule 14, each phase ends with the code-review +
completion-audit loop until both pass clean.

---

## 12. Open questions requiring the user's decision

| # | Question | Default if unanswered |
|---|---|---|
| **Q1** | **LAN reading (R-4).** The wiki is reachable from your phone today with zero config and zero auth. `brain ui` needs `--host` + `--token`. What replaces that workflow — documented `--host`/`--token`, a Tailscale-style tunnel, or accept the loss? | *No default — this is a live workflow being removed.* Blocking for R1. |
| **Q2** | **Graph render primitive** (§6.3): vendor d3-force (a), hand-roll ~150 lines (b), vendor PixiJS (c), or server-rendered SVG for the local view only (d)? | **ANSWERED — (d), then (b) only if Q3 demands it.** The graph research settles this: `graph_data(conn, root=, depth=)` (`vault/graph.py:350`) makes a depth-1 rooted neighbourhood cheap, and that neighbourhood *is* the sidebar graph — the affordance actually used while reading. A depth-1 graph is a handful of nodes, so no force simulation is needed at all: Python emits SVG directly with zero dependency, zero vendoring, and zero pressure on §2.1's enforced offline guarantee. Options (a) and (c) exist only to serve the ~1,300-node fullscreen global graph, which Q3 has not yet established is used — and (a) is now known to cost a **~10-module d3 subtree**, not one file (§6.3). **Confirm only if you disagree.** |
| **Q3** | **Graph feature subset** (§6.1): which of ~15 features across 4 surfaces are must-have? | **Sidebar local graph + click-to-navigate + hover-highlight only.** The rest deferred. |
| **Q4** | **"Open in external editor"** (§8.6): copy-path (a), `obsidian://` link (b), or server-spawns-editor (c)? | **(a)**, with (b) on request. **(c) scoped out of the first iteration.** |
| **Q5** | **Visual identity.** | **ANSWERED by §7.1.2's role split** — the *instrument* inherits the wiki's token system, the *page* gets a real text face. Neither a pick nor a blend. Only font **delivery** survives, as T1/Q14. |
| **Q6** | **Graph diagnostic workbench** (§3.3): a 4th fullscreen modal with a mode rail and inspector. Real engineering. Port it? | **Do not port.** Rebuild only if you reach for it. |
| **Q7** | **Mermaid** (§3.4.1): **it IS enabled** and 2 ingested documents use it. Render as a labelled code block (a), or vendor a renderer (b)? Separately: **image lightbox and print styles** are confirmed absent from both stock and overlay — intentional, or an unmet want? | **(a) for Mermaid** at n=2 — a decision on evidence, not a finding of absence. **Intentional** for lightbox/print, which then leave scope. |
| **Q8** | **Launchd gating** (§9.5): `_maybe_install_launchd` gates the *entire* install behind `wiki_installed`, including the vault-sync watcher that becomes near-mandatory (§8.5). Offer the watcher unconditionally? | **Yes — unconditionally**, with a `brain doctor` check when it is not running. |
| **Q9** | **Design direction** (§7.1). | **ANSWERED — "Instrument & Page."** A Swiss instrument panel receding to near-monochrome, framing one editorially typeset page. **Confirm only if you disagree.** |
| **Q10** | **Publish tab** (§9.6): delete it from the tab bar, or repurpose it? | **Delete it.** A tab describing a retired subsystem is worse than no tab. |
| **Q11** | **R4 vault-format simplification** (§1.3). | **No.** Requires an explicit ask, per concession, with evidence Obsidian/vim do not want it. |
| **Q12** | **Deletion trigger** (§11 phase 9): a fixed dormancy period, or purely on your word? And what is your answer to **RA-8** (keep the wiki as an availability fallback)? | **On your word.** No timer deletes your reading surface. |
| **Q13** | **`docs/specs/` is gitignored** (`.gitignore:53`) and **zero specs are tracked**, while CLAUDE.md rule 11b mandates specs live there. Track them, relocate them, or accept that every design document is local-only? **Repository-wide, not wiki-specific.** | **Track them** — un-ignore `docs/specs/` and commit the existing specs. A plan the phasing depends on should not be one checkout from gone. |
| **Q14** | **T1 — CSP change for self-hosted fonts** (§7.3). Add `font-src 'self'` and ship ≈115 KB of woff2, or keep system stacks? Without the CSP line, `default-src 'none'` blocks every `@font-face` **silently**. | **Add it.** No `unsafe-inline`, no nonce, no external origin. (Judge the 115 KB on its own — the old "vs 296 KB of logo PNGs" comparison was withdrawn: those PNGs live in `quartz_overrides/`, which R3 deletes.) **If refused, set `--measure: 34rem` and the direction survives, weaker.** |
| **Q15** | **T2 — `/api/search` must project a date** (`schemas.py:361-371` returns no date key; `doc_date` exists per migration 021 but appears nowhere in `ui/`). The redesigned ledger row is not implementable without it. | **Add `doc_date` to the payload.** Small, additive, unblocks D6. |
| **Q16** | **T3 — snippet rendering.** Markdown-stripped, `<mark>`-highlighted snippets must be **server-side**; doing it client-side means `innerHTML` on untrusted text. | **Server-side**, in `render.py`'s hardened path, with its own A3 XSS test. Client-side `innerHTML` on snippet text is not acceptable. |
| **Q17** | **T4 — retire the tab strip?** (§7.1.5) Three of four tabs are apologies; moving them to ⌘K plus a status footer is **a product decision, not a visual one**. | *No default — your call.* Interacts with Q10 (Publish tab). |
| **Q18** | **T5 — offline-test gap.** `test_every_referenced_asset_exists_on_disk` (`tests/test_ui_static_assets.py:112`) parses **only `index.html`** via a `(?:href\|src)="` regex, so font paths referenced from `app.css` are unchecked — a typo'd `@font-face` URL would ship silently. | **Extend it to parse `url()` in CSS.** Verified as a real gap; the fix is a few lines and prevents a blank-font failure mode identical to the wheel-packaging one. |
| **Q19** | **§2.1 waiver for `markdown-it-py[plugins]`** (§5.2, added 2026-08-11). **Four of phase 1's eight PORT items — task lists, footnotes, callouts, block math — require `mdit_py_plugins`, which is neither installed nor declared** (verified: `ModuleNotFoundError` on all five submodules). Tables, strikethrough and syntax highlighting need **no** new package. Grant the waiver, or cut phase 1 to the three free items? | **Grant it, narrowly — for task lists only.** The ask is `markdown-it-py[plugins]>=3.0`, an **extra of an already-declared dependency**, not a new package. Task lists alone justify it: **145 documents**, and `brain todo` depends on that syntax. The other three do not clear the bar on their own evidence — footnotes 3 documents, callouts **1**, block math **0** — and block math additionally needs a vendored JS display engine, a second §2.1 problem (§5.4). **This is one decision gating four items and it must be answered before phase 1 opens**, not discovered mid-phase. |

---

## Appendix A — research status

| Scope | Status | Sections |
|---|---|---|
| `brain ui` architecture map | **Landed** | Throughout |
| Wiki feature inventory | **Landed** | §3, §5.1, §5.4 |
| Graph surfaces + data availability | **Landed** | §6, §4.2 |
| Branding / theme / logo | **Landed** | §7 |
| Performance / availability risk | **Landed** | §10.1–§10.3 |
| External editing / write concurrency | **Landed** | §8 |
| Retirement surface | **Landed** | §9 |
| Design direction | **Landed** — *measured against a live run, not source* | §7.1, §7.3, Q9, Q14–Q18 |

**All research scopes have landed. No section is blocked.** What remains is decisions —
Q1–Q18 — not investigation.

Verified directly for this revision: `ui/{app,queries,notes_service,render,routes_search,schemas,security}.py`,
`cli_ui.py`, `ui/static/{index.html,app.js}`, `tests/test_ui_static_assets.py`,
`vault/{graph,graph_format,daily_index}.py`, `maintenance.py`, `connect.py`, `queries.py`,
`graph_rag/*`, `review/weekly.py`, `pyproject.toml`, `docs/vault-and-wiki.md`,
`quartz_overrides/quartz/components/PageTitle.tsx`, plus line/file counts for `wiki/` and
`quartz_overrides/`.

---

## Appendix B — Phase 2 defect record

**Recorded, not repaired.** This project's convention is that a spec defect is written
down where a reader will meet it, rather than quietly corrected — a silent fix leaves no
trace that the document was once misleading, and the next reader cannot tell a claim that
was always right from one that was patched. Every entry below therefore states the
document's claim, what is actually true, and how that was re-derived. **The body of this
spec above is deliberately left as it was**, apart from pointers into this
appendix wherever a reader is most likely to be misled (see the note below).

**Pointer count deliberately not stated.** An earlier revision of this paragraph said "the four
places"; more have since been added, and a count of the pointers is exactly the kind of figure that
rots the moment the next one lands — the same failure recorded at the foot of this appendix. The
invariant instead: **every body passage this appendix contradicts carries a pointer to the entry
that contradicts it**, and one — §11's phase-2 row — is struck in place rather than merely
annotated, because a reader skimming a phasing table reads the cell, not the footnote.

**Those pointers carry one hazard, and it is a *future* one.** Body and appendix are the same
file, so as things stand they share fate — the version-control warning at the head of this
document, and **Q13**, already cover that and are not restated here. But the pointers assume
the appendix is present. **If this spec is ever partially recovered — an older copy committed,
or the body reconstructed from another source — the body arrives annotated with references to
a section that is not there.** A pointer outliving its target is worse than no pointer: it
reads as a correction the reader cannot find, and the natural inference is that they have
missed it rather than that it is absent. Anyone restoring this document from anything other
than a whole-file copy should confirm Appendix B came with it, or strip the four pointers.

**Every claim here was re-derived against the tree on 2026-08-14 before being written**,
not inherited from the task that raised it. Several of these findings passed through two
or three corrections during the phase, and at least one was downgraded by its own author
after further measurement (B-11). Where a re-derivation *disagreed* with the raising
task, this record follows the measurement and says so.

**On the numbering.** S1–S10 are defined in the phase-2 plan's own defect table
(`docs/plans/2026-08-13-wiki-to-ui-phase2.md`, rows 53–57 and the summary at 330–339).
S13, S16, S17, S18 and S19 were allocated later, on the task list, as implementers found
them. **S14 and S15 are recorded below as B-13 and B-14.** An earlier revision of this
appendix stated they "could not be resolved from the tree". That was wrong, and the error
is worth keeping: they were raised by **task #6**, whose closing line reads "Three new spec
gaps raised as S13/S14/S15". Three plausible candidate tasks were searched and not the one
that raised them — the same looked-in-the-wrong-place failure this appendix records
elsewhere, committed while recording it.
**S11 and S12 were never allocated at all** — the "S13–S15" range cited in two tasks is
simply wrong — so they are left as a numbering gap rather than assigned to a plausible
finding: inventing a number is how a phantom result gets cited later as though it were
measured.
**And the reason S14 and S15 were unfindable is itself the finding:** they were allocated in
a message, and a message is not a record. The phase-2 plan lived only in a message until a
scribe wrote it to disk; two spec defects lived only in a message until this appendix. That
is the same exposure as this document being untracked, one level down. The durable key for every
entry below is its **task ID**, which exists; the S-number is given only where a document
actually states it.

### B-1 · S1 — related-docs is in three phases at once
*Task #2. Spec §11 (phase 2 row) vs §11 (phase 6 row) vs §9.2d:1487.*
§11's phase-2 row lists "related-docs (after the §9.2 move)"; §11's phase-6 row schedules
that move; §9.2d states `compute_related` is **new code, not a move**, and that authoring
it is a phase-5 need. One deliverable, three phases. **Ruled:** the `build_related`
scoring move was pulled forward into phase 2 wave 0, and `compute_related` was written as
new code (`src/brain/related.py:114`, task #6). Operationally resolved; **a reader of the
spec alone is still misled**, which is why this entry exists.

### B-2 · S8 — §9.2c's "authoritative importer list" points at deleted modules
*Task #9. Re-derived: the spec still contains **18** references to `wiki.build_people` /
`wiki._person_name` paths; `src/brain/wiki/build_people.py` and
`src/brain/wiki/_person_name.py` **do not exist**; `src/brain/people.py`,
`src/brain/person_name.py` and `src/brain/related.py` all do.*
The list was accurate when written and is now a trap, and a specific kind: §1389 and
§1426 both declare §9.2c **the single authoritative list**, so a planner is explicitly
directed to the stale rows. Anyone budgeting phase 6's remainder from it will plan
against module paths that were renamed mid-session.

### B-3 · S9 — §11's phase-6 row describes work already done
*Task #10. Re-derived: all three target modules exist at their new paths.*
Three of phase 6's four moves landed during phase 2 (`build_related`→`related`,
`build_people`→`people`, `_person_name`→`person_name`). §11 still describes them as
future work, so **the phase-6 budget is overstated**. The row's only still-load-bearing
clause is its "No deletion in this phase" gate.
**Corrected 2026-08-20: "four" is wrong — there are three.** §9.2 numbers exactly three moves
(`_person_name`, `build_people`, `build_related`) and §11's phase-6 cell lists exactly those
three; no fourth is specified anywhere, and this entry invented the denominator. The correction
makes the finding **stronger**, not weaker: the phase-6 move budget is not mostly spent but
**entirely** spent. Re-derived — all three modules exist at their new paths, no importer points
at the old ones (the §9.2c grep returns 6 statements, none of them to a moved module), and
`wiki/build_related.py` is the 248-line thin emitter §9.2d(2) requires rather than the 815-line
original. A count that nothing in the document supports is exactly what this appendix's standing
rule forbids, committed by the appendix.

### B-4 · S13 — the documented `compute_related` signature cannot run, and is wrong twice
*Task #17 (open user decision). Spec §9.2:1459 and §9.2d:1487 both write
`compute_related(doc_id, limit)`.*
The real signature is
`compute_related(conn, doc_id, *, limit=DEFAULT_RELATED_LIMIT, vector_sim_floor)`
(`src/brain/related.py:114`). **The spec omits two parameters, not one** — the raising
task named only `vector_sim_floor`; re-derivation shows `conn` is missing as well. The
documented form cannot be called.
`vector_sim_floor` is **required with no default, deliberately**: the floor is shared with
runtime `brain search`, and `regenerate_related_json`'s own docstring already refuses a
default for the same parameter on the grounds that it "must not silently diverge".
**The open decision is not an ergonomics question about argument defaults.** It is: *may
the related-docs panel diverge from `brain search` without anyone noticing?* The failure
mode is invisible — a wrong cosine floor still returns plausible documents in a plausible
order.

### B-5 · S16 — T7's file list omitted the one file that connects the feature
*Task #23. Re-derived: `source_missing` now appears in `search_predicate.py` (5×),
`search.py` (2×) and `ui/schemas.py` (11×).*
T7 scoped to `search_predicate.py`, `ui/schemas.py`, `ui/routes_meta.py`.
`search.hybrid_search` calls `build_predicate` with explicitly named keyword arguments and
does **not** splat `**kwargs`, so a filter added only to `build_predicate` is
**unreachable from the UI** — any value `schemas.py` put in `filter_kwargs()` would raise
`TypeError`. The connecting file, `src/brain/search.py`, was absent from the row.
**Ruled (a):** a two-line additive diff in `search.py`. Rejected: shipping the predicate
half alone, which would have delivered a kwarg that exists, type-checks, is tested at one
layer and does nothing — the "looks implemented, does nothing" shape this phase produced
three more times.

### B-6 · S17 — `linkKindMark.ts` contradicts itself, and the plan's mutation was inert
*Task #25. Re-derived: `src/brain/ui/render.py` now carries `_TAG_INFIX`, i.e. the
documented semantics were ported.*
The overlay's header documents a tag URL as one that starts with `tags/` **or contains**
`/tags/`; its code only ever prefix-matches, so `https://host/tags/retro` classifies as
`external`. **The consequence is the interesting half:** under the code reading the tag
and external prefix sets are disjoint, so no reordering can change any answer — and the
plan's prescribed T5 mutation ("reorder so external precedes tag") **would have been
inert**, a test that cannot fail, prescribed by the plan. Ported the documented semantics,
which makes precedence load-bearing and the mutation land.
*Follow-up, unowned:* reconcile the overlay's comment and code. The overlay is arguably
the buggy half.
**CLOSED 2026-08-20 — the overlay was the buggy half, and the CODE was changed, not the comment.**
`linkKindMark.ts` now carries `TAG_INFIX = "/tags/"` and an `isTagUrl()` that tests prefix **or**
infix, mirroring `render.py`'s `_is_tag_url`. Two reasons the documented reading won, and neither
is "the comment said so": a host-qualified link to this site's own tag page **is** a tag link and
prefix-only silently files it as `external`; and under prefix-only the tag/external precedence is
**inert**, because the two prefix sets are disjoint — an ordering nothing can observe is an ordering
no test can defend. **Verified by executing the patched transformer**, not by reading it: the file
was type-stripped (`node --experimental-strip-types`), its `LinkKindMark` plugin invoked on
synthetic mdast link nodes, and nine URL shapes checked against the kinds `render.py` assigns —
all agree, including `https://…/tags/retro` → `tag`. **The check was proved able to fail:** with
`isTagUrl` reverted to `startsWithAny(url, TAG_PREFIXES)` in the scratch copy, the same harness
reports `MISMATCH https://example.invalid/tags/retro: got external, want tag` and exits 1.
*(A plain `node --check` on the file, with or without `--experimental-strip-types`, exits 0 even
on a deliberately broken copy — it is not a check at all for `.ts`/`.mts`, and it was the control
run that revealed that rather than any reading of the docs.)*

### B-7 · S18 — the related-docs endpoint exists in no task's scope
*Task #37. Re-derived: `grep -c related src/brain/ui/app.py` → **0**. Still absent.*
`compute_related` is complete in `related.py`; **no HTTP route calls it**, and no `.py`
under `ui/` mentions it. T3's row scoped to `related.py`/`connect.py`; T10's to its four
endpoints; T14's to the front end. **The endpoint falls in the gap between three rows and
is owned by none** — the defect class no single implementer hits, because each row's own
scope looks complete. The route was always intended: `related.py:126-128` says so.
**Ruled:** T14 shipped backlinks-only, on the evidence that the plan's own T14 oracle
names only backlinks and the related panel has **no oracle at all**. Still blocked on
B-4's open decision, and on a design constraint — `related.py:151-154` prices each call at
"roughly one `brain search`'s worth of work", so a panel firing on every note open needs
caching or laziness by design, not as a footnote.
**→ See B-17** for the consequence: with the endpoint unbuilt, `compute_related` ships with
no caller but its test, and is recorded there as a deliberate deposit so it is not later
removed as dead code.

### B-8 · S19 — T15's declared mutation reddens the wrong assertion
*Task #52. Measured by its implementer, both mutations run.*
The row says: *return `""` for summary → the "no empty aside" assertion fails.* Measured,
returning `""` reddens the **presence** assertions and leaves **both absence assertions
green** — of course it does: with no summary there is nothing to render, so "renders no
empty aside" is trivially satisfied. The mutation that reddens the named assertion is
dropping the **emptiness guard**.
An implementer following the row literally would have seen red and recorded the absence
assertions as proven when they had never fired.
**BOTH MUTATIONS RE-RUN 2026-08-20 — the finding is confirmed and sharpened in one direction the
entry understates.** On `pytest tests/test_ui_browser_lede.py -m browser --no-cov` (baseline **13
passed**): forcing `summary` empty at `js/inspector.js:241` gives **3 failed / 10 passed**, not
one — `:411` (presence), `:422` (lede-above-body) and `:469` (lede parented to `.note-head`) — and
**both absence assertions, `:512` and `:523`, stay green**, exactly as recorded. Dropping the
emptiness guard at `:242` gives **2 failed / 11 passed**: `:512` and `:523` and nothing else, with
the presence test green. So the two mutations redden **disjoint** sets, which is the property that
makes the substitute the right one rather than merely a different one. The count matters to the
entry's own argument: a row saying "reddens the presence assertions" invites an implementer to
expect one red test and stop looking at three.

### B-9 · (no S-number) — T17's declared mutation is unimplementable
*Task #58.* The row says: *drop the client-side draft guard **and** T4's SQL guard
together — one alone leaves the other covering it.* **There is no client-side draft guard
and there cannot be one:** the discovery payload carries no `draft` field, so the client
cannot distinguish a draft from anything else. The defence-in-depth pair the instruction
reasons about never existed. The row's own fallback — assert against the route payload —
was already delivered by T10.

### B-10 · (no S-number) — the producer escapes one heading and not the other
*Task #57.* `ingest/gmail.py` escapes the `<summary>` heading but emits the newest
message's `## H2` unescaped, so CommonMark treats a bracketed address as an autolink and
the angle brackets vanish — from the rendered heading **and** from `extract_headings`.
**The same sender address renders two ways inside one document.** Upstream of rendering
entirely, so no render rule can fix it; fixing it is a producer change with re-render
implications across the 58 affected documents, i.e. the option T18 ruled out. It needs its
own decision rather than being smuggled in as a bug fix. The reply filter substring-matches
the address, so whichever form ships must be consistent or the filter silently fails on
the newest message — the one a user is most likely to filter on.

### B-11 · (no S-number) — roster membership buys one policy, not contrast coverage
*Task #46 — a correction to task #42, filed by #42's own author.*
#42 found that a feature stylesheet shipped a live WCAG AA failure because a sheet outside
`CSS_ORDER` is handed to no `css/*` guard. True, and the roster gap is real. **The
inference "so joining the roster prevents this class of defect" is false**, and was
measured: reintroducing the identical visual defect written as a raw `oklch()` literal
instead of the token, with the sheet fully inside `CSS_ORDER`, was noticed by **zero**
guards; the control using the token was noticed by exactly one. Exactly one guard is
`css/*`-scoped; the suite's actual contrast rule is a fixed six-selector list in
`components.css` and sees no feature sheet at all. **The original defect was caught partly
by luck of expression.** Recorded because #42 is otherwise carried as the strongest
argument for making the roster self-discovering, and that argument is about *visibility*,
not about contrast coverage — if the fix lands and anyone concludes feature stylesheets are
accessibility-checked, that is worse than the known gap.

### B-12 · (no S-number) — pagination has a product ceiling, not a perf caveat
*Task #27.* `search.py`'s `CANDIDATE_LIMIT` bounds **both** ranking legs independently of
the caller's `limit`, so at most ~2×`CANDIDATE_LIMIT` documents can ever be ranked for a
query. The plan's T6 caveat frames deep pages as **slow**; they are **unreachable**. Those
are different claims and only the second is a product limitation: a user paging a large
result set does not hit a slow page, they hit an empty one, with nothing distinguishing
"no more matches" from "the ranker never looked further". Implemented defensively as
`schemas.MAX_OFFSET`, **derived** from the constant rather than copied, with a test
asserting the derivation.

### B-13 · S14 — the return type is specified nowhere, and the acceptance criterion assumes one that did not exist
*Provenance, stated plainly because it is this entry's weakness: **allocated by T3's
implementer in a report that never became a task**. No defining document exists; the content
is vouched for by the coordinator who received the report, and task #6 records only that the
number was raised. **The code claims below were re-derived from the tree** — `related.py:96`
`_Neighbor` (internal) vs `:97` `RelatedDoc` (public) — and stand on their own.*
§9.2 documents `compute_related` without a return type. The acceptance criterion asserts
`.id` on the result — but the ranking internals name that field `document_id`
(`_Neighbor`), so **the criterion could not have passed against the type the module already
had**. The implementer had to introduce a public `RelatedDoc(id, title, vault_path, source,
score, snippet)` and say why, and its docstring now records the translation explicitly:
*"the same six fields, named for a consumer rather than for the ranking internals (`id`
rather than `document_id`)"*.
**The defect is the shape, not the naming:** an acceptance criterion that asserts a field
name is specifying an interface. Doing that in the criterion while the signature section is
silent puts the contract in the place least likely to be read as one.

### B-14 · S15 — source-doc eligibility was never specified, and the implementation widened it
*Provenance as for B-13: allocated in an implementer's report, no defining document,
content vouched by the coordinator. **Re-derived from the tree:** `related.py:279-290` for the
precompute's conditions, `:135-143` for the widening, and two tests that pin it.*
`_eligible_source_docs` restricts the **precompute** to documents meeting three conditions —
`draft = FALSE`, `vault_path IS NOT NULL`, and at least one chunk with a non-NULL embedding
(`related.py:287-289`) — because those are the documents it writes files for. **A reader can
open a document meeting none of them.** So `compute_related` accepts any document row and
lets each ranking leg degrade on its own: no embedded chunks and the vector leg drops out
(`_avg_embedding` returns `None`); an empty self-tsquery and the FTS leg drops out. The spec says nothing
either way, so the widening is a decision the implementer had to make and record rather than
one the document made. **Candidate eligibility is unchanged** — drafts and the source
document itself are still excluded from the *results* (`:142`, `:215-219`), which is the half
a reader is likely to assume the note is about. Recorded because "wider" without that second
sentence would read as a relaxed safety property rather than what it is: a different question
being answered on demand than in a batch.
**Both halves are pinned**, which is what makes this a decision rather than a drift:
`test_compute_related_works_for_source_without_vault_path` covers the widened source, and
`test_compute_related_excludes_draft_candidates` covers the unchanged candidate rule
(`tests/test_related_compute.py:208`, `:229`).

### B-15 · (no S-number) — T14's declared mutation deadlocks the harness and proves nothing
*Task #43. Diagnosed, not guessed: the mutant was confirmed syntactically valid with
`node --check` **before** this conclusion was drawn.*
The row says: *make the fetch synchronous/blocking → the note body assertion times out.* Run
literally, a synchronous `XMLHttpRequest` produces **13 errors in 116 s** — every test in the
file erroring at fixture setup, including tests that never touch backlinks. **Cause:** the
sync XHR blocks the page's main thread inside a `page.evaluate` that Playwright is awaiting,
while the response can only be produced by a route handler in the driver that is itself
blocked awaiting that evaluate. Deadlock.
**A 13/13 error run reads as a successful proof to anyone reading only the exit code.** The
declared mutation is unusable in any Playwright + route-stub harness. The substitute — route
the request through an *async* XHR, which never touches the page's patched `window.fetch` and
so sails past the test's gate — isolates the same property at **1 failed / 12 passed**.
**RE-RUN 2026-08-20 — THIS DOES NOT REPRODUCE AGAINST THE CURRENT TREE, and the entry is kept
rather than withdrawn.** The synchronous form was performed again exactly as described (`open(…,
false)` + `send()`, rail appended inline, restored byte-exact) and produced **1 failed / 16
passed in 8.86 s** on `pytest tests/test_ui_browser_reading.py -m browser --no-cov` — no
deadlock, one red test, on the assertion the row targets. B-15's *mechanism* is exact and that is
why it fails to fire: it needs the sync XHR to run inside a `page.evaluate` Playwright is
awaiting, and `attachBacklinks` is now reached from an ordinary subscriber render while the
harness gates `/links` **inside the page** rather than in a route handler, so there is no
driver-side handler to deadlock against. The file total moved too — B-15 counts **13** tests
where there are now **17**. Kept because the reasoning becomes true again the moment a harness
stubs that route driver-side; and the async substitute remains the better mutation either way,
since it isolates laziness instead of incidentally defeating the gate. The identical note is on
the plan's Appendix C-3, which carries the same claim.

### B-16 · (no S-number) — a test-side import guard must tolerate a legitimate RED phase
*Task #40 — calibration, not a defect.*
Two guards independently miss a test-side import of a module that does not exist: the
`browser` marker is deselected by default `addopts`, and
`test_every_es_module_import_resolves_to_a_file` walks the **shipped static tree**, so a
test-side import is outside its scope by construction. The obvious fix — scan test-side
imports too — carries a trap: **it would have passed at 15:18 and failed at 15:10 on the same,
correctly-executed TDD task.** A test importing a module that does not exist *yet* is the
correct state of a RED phase.
So any such guard must be robust to RED being a correct state, or it fires on every
properly-run test-first task — punishing the discipline it exists to support, and getting
disabled within a week, taking its real coverage with it. Practical shape: gate it on the
**end** of the workflow (CI on a finished branch, or a pre-commit hook), never on every local
run. A constraint on the roster-discovery fix, not an argument against it.
---

### B-17 · (no S-number) — `compute_related` is a deliberate deposit, not dead code
*Task #68 (completion-audit gap G2). Re-derived 2026-08-14:
`grep -rn "compute_related" --include="*.py" src tests bin` → `related.py` (definition,
`__all__`, one docstring reference) and **`tests/test_related_compute.py` and nothing
else**. No `/api/related` route in `app.py`. The `RelatedDoc` **dataclass** is referenced
nowhere outside `related.py`; the other `RelatedDoc` matches in the tree are the wiki's
TypeScript component `RelatedDocs.tsx`, which is a different thing entirely — it reads
`build_related.py`'s precomputed JSON at build time and does not touch this function.*

**This is the record the audit asked for, and its purpose is defensive.** `compute_related`
and `RelatedDoc` are production code whose only caller is a test. That is the exact
signature of dead code, and the next person to run a dead-code sweep will find it, see 613
lines of scoring machinery with no consumer, and be *right* on the evidence available to
them. **Without this entry it gets deleted by someone who did not read the thread that
created it.**

**Why the consumer does not exist: B-7 (S18).** The HTTP endpoint falls in the gap between
three task rows and is owned by none — T3 scoped to `related.py`/`connect.py`, T10 to its
four endpoints, T14 to the front end. T14 was then ruled backlinks-only *because* of that
gap. **The narrowing was sanctioned; this entry is the consequence being carried rather
than left implicit.**

**Why it was not simply added: B-4 (S13), open task #17.** `compute_related` requires
`vector_sim_floor` with **no default, deliberately** — `related.py:130-133` records that
the cosine floor is shared with runtime `brain search` and must not silently diverge from
it. Any route must therefore decide where that value comes from, and deciding it quietly
is precisely the divergence the missing default exists to prevent. **That decision is the
user's, not an implementer's**, so the endpoint waits on it rather than inventing a
default.

**What exists today, and is sound:** `compute_related(conn, doc_id, *, limit, vector_sim_floor)`
and the `RelatedDoc` value object in `related.py`; **10 tests** in
`tests/test_related_compute.py` covering ranking, limit, draft exclusion, source docs
without a `vault_path`, unknown and malformed ids, and the empty corpus; the RRF sort-key
mutation independently re-run by the completion audit and confirmed to redden its named
assertion. **The function is finished. Only its consumer is absent.**

**What completing it requires** — four things, none of which is a code change to
`related.py`: a route module, an `app.py` registration, a client consumer, and **the #17
ruling first**. Plus the design constraint B-7 already names: `related.py:151-154` prices
each call at "roughly one `brain search`'s worth of work", so a panel firing on every note
open needs caching or laziness by design.

**Two smaller instances of the same shape, recorded so they are not mistaken for
consumed contracts.** *(a)* T6's pagination has no client half — no JS module ever sets
`offset`, so the parameter is reachable only by hand-editing the URL. Scoped rather than
lost, since T6's file list was server-only, and it compounds **B-12**, where paging cannot
pass ~100 documents anyway. *(b)* `routes_links.py` serves an `outgoing` array that no
client reads — consistent with the backlinks-only ruling, and noted here so a future reader
does not infer from its presence that something depends on its shape.

**The pattern across all three:** a row can be individually complete and still deliver a
half. Nothing in the plan's structure surfaces that, which is the same observation B-7 and
B-5 make from the other direction — and it is why "delivered" and "reachable" are recorded
as different claims throughout this phase.
---

### B-18 · (no S-number) — confidentiality is now TWO flags, and the spec documents one

*Re-derived 2026-08-20 by reading the gating sites, not by grepping for the name.*

§0.1 and §2.5 describe confidential withholding as a single conditional keyed on
`serve_confidential_bodies`. **That is still true and still complete — for bodies.** It is no
longer the whole model. A second flag, **`serve_confidential_titles`**, governs a different
question: whether an **unprompted** listing surface may *name* a confidential document at all.
Body-withholding answers "may this reader read it"; title-withholding answers "may a rail the
reader never asked for mention that it exists". A surface can honour the first and violate the
second, which is exactly what happened.

**One flag, four routes, three browse surfaces** — and the two counts are both correct, which is
why they must be given together:

| Route | Gating site | Surface |
|---|---|---|
| `/api/tree` | `ui/routes_tree.py:28` | vault tree |
| `/api/recent` | `ui/routes_discovery.py:56` | home rail |
| `/api/tags` | `ui/routes_discovery.py:84` | tag index |
| `/api/tags/{tag}` | `ui/routes_discovery.py:107` | tag index |

Each site reads `strict = not ctx.serve_confidential_titles`. `ui/routes_meta.py:32` *reports* the
flag to the client but gates no listing, so it is not a fifth.

**The defect the flag exists to close is a cross-module one**, and worth restating because no
single-module test could have caught it: the tree named every confidential title while the rail
beside it hid them, **and each surface's own tests passed**. The property is agreement between
surfaces, and it lives in the gap between two green modules —
`tests/test_ui_confidential_titles_gate.py` exists to assert the surfaces against each other and to
pin the flag's route from `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES` through to `UiContext`.

**Do not inherit either number from prose, including this entry's.** The producing code's own
docstrings disagree with themselves: `routes_discovery.py` says the flag applies "identically across
all **three** routes" (its own three) and then, four lines later, calls the same set "these **two**"
rails and the total "the **three** unprompted listing surfaces". Both readings are defensible —
three *surfaces*, four *routes*, because the tag index is served by two routes — but a reader who
takes one sentence and not the other will audit the wrong number of call sites. **Flagged, not
fixed: the drifting prose is in `.py` files and is a code change** (docs-only scope).

### B-19 · (no S-number) — the facet panel's `none` bucket, and the 63% it was hiding

*Re-derived 2026-08-20. The percentage below was **measured**, not carried over from the report
that raised it — a read-only `SELECT` against the production corpus.*

`brain.facets` groups a match set by source with `coalesce(s.kind, …)`. The fallback **used to be
`'manual'`**, so every document with no `sources` row was filed under a real source kind. It is now
`SOURCE_NONE_BUCKET = "none"` (`src/brain/facets.py:38`).

**Scale, measured:** `SELECT count(*), count(*) FILTER (WHERE source_id IS NULL) FROM documents` →
**877 of 1,393 documents — 63.0%.** So `manual`'s count was not slightly inflated; the bucket was
mostly not-manual, and a reader filtering to `manual` got a majority of documents that have no
source at all. (1,393 is the same corpus total §11's phase-1 correction counted, independently, for
a different purpose — the two agree.)

**Why this is the interesting failure and not a typo:** nothing about it looked wrong. The number
was plausible, the rows were real rows, and the filter returned documents. There was no error state
to notice — which is the same shape as B-4's warning about `vector_sim_floor`, where a wrong cosine
floor still returns plausible documents in a plausible order.

**`'none'` rather than dropping the row, deliberately**, and the reasoning is worth keeping: a
dropped row would have fixed `manual`'s count while making 63% of the corpus *invisible* in the
panel — the same information loss in a quieter form. The bucket is instead **clickable**, carrying
the exact value `ui/schemas.SOURCE_NONE` turns into `build_predicate(source_missing=True)`, so
clicking the facet selects precisely the documents it counted. **This is the consumer of the
`source_missing` kwarg whose missing plumbing is B-5 (S16)** — the two entries are one feature seen
from its two ends, and the panel could not have been made honest without the `search.py` line B-5
had to add.

---

### Re-derivation of 2026-08-20 — status of B-1 … B-8

**Every row below was re-checked against the tree on 2026-08-20, after three further days of work
on this branch.** The purpose is calibration, not restatement: an appendix entry is a measurement
with a date on it, and several of these were written before the moves they describe had landed.
Where re-derivation **agreed**, the entry stands and needs no reading. Where it **disagreed**, the
disagreement is the row.

| Entry | Status 2026-08-20 | Evidence |
|---|---|---|
| **B-1 · S1** — related-docs in three phases | **Confirmed, and now ruled.** The body contradiction was still live: §11's phase-2 row still listed it. **Ruling recorded:** the scoring landed in phase 2, the **panel is deliberately deferred to phase 5**. §11's phase-2 row is struck in place and phase 5 now names the panel as its own deliverable, so the three phases collapse to one. | `grep -c related src/brain/ui/app.py` → **0** |
| **B-2 · S8** — §9.2c stale | **Confirmed and widened.** B-2 said the list points at deleted modules; re-derived, it is worse in two ways. (a) The section's own grep now returns **6** statements, against the table's **12** rows. (b) B-2 named two sites declaring §9.2c authoritative (§1389, §1426); there is a **third**, in §0.1. | see the current list below |
| **B-3 · S9** — phase-6 row overstates | **Confirmed; unchanged.** All three modules exist at their new paths and no importer still points at the old ones. The row's pointer was already in place. | `src/brain/{people,person_name,related}.py` all present |
| **B-5 · S16** — T7 omitted `search.py` | **Confirmed exactly.** T7's row in the phase-2 plan still lists only `search_predicate.py`, `ui/schemas.py`, `ui/routes_meta.py`. The feature was in fact delivered by touching `search.py` — the two-line additive diff is at **`search.py:328`** (parameter) and **`:456`** (forwarded into `build_predicate`). | `source_missing` counts: `search_predicate.py` **5**, `search.py` **2**, `ui/schemas.py` **11** — matching B-5's re-derivation exactly |
| **B-6 · S17** — `linkKindMark.ts` self-contradiction | **Confirmed, and the authoritative reading is settled: the DOCSTRING is right, the code is wrong.** The port took the documented (infix) semantics, so the Python side is now the correct implementation and the overlay is the buggy half. Recorded here because the argument runs backwards from the usual one — a T5 mutation reddens **only** under the infix reading, so the comment is not commentary on the code, it is a **load-bearing dependency of the test suite**. | `render.py:74` `_TAG_INFIX = "/tags/"`, used at `:165` (`startswith(_TAG_PREFIXES) or _TAG_INFIX in lowered`) vs `linkKindMark.ts`'s prefix-only `TAG_PREFIXES` + `startsWithAny` |
| **B-7 · S18** — endpoint owned by no task | **Confirmed; still absent.** Consistent with B-1's deferral — and now *stated* rather than left as a hole, since phase 5 owns it. Remains additionally gated on the open `vector_sim_floor` decision (B-4). | `grep -c related src/brain/ui/app.py` → **0**; no `.py` under `ui/` names `compute_related` |
| **B-8 · S19** — T15's mutation reddens the wrong assertion | **Confirmed, and now located.** The plan's T15 row still reads *return `""` for summary → the "no empty aside" assertion fails.* There are **two** absence assertions, `tests/test_ui_browser_lede.py:512` (no summary) and `:523` (blank summary), and returning `""` leaves **both green** while reddening the presence assertions at `:411-412`. The mutation that reddens the named assertions is **dropping the emptiness guard at `src/brain/ui/static/js/inspector.js:242`** (`if (summary) head.appendChild(…)`). | both files read 2026-08-20 |

**The current importer list, re-derived** — the same grep §9.2c specifies
(`^\s*(from|import)\s+.*\bwiki\b`, excluding `src/brain/wiki/` itself), plus the `quartz_overlay`
line §9.2c footnotes separately:

| File:line | Symbol | Scope |
|---|---|---|
| `cli.py:234` | `vault.quartz_overlay` (`OverlayError`, `apply_overlay`, `plan_overlay`) | module-level |
| `cli.py:240,241` | `wiki.install.WikiInstallError`, `wiki_install` | module-level |
| `cli.py:6655` | `wiki.build_swap.resolve_build_timeout_s` | function-local |
| `doctor_runtime.py:42` | `wiki.build_swap.EXIT_CONFIG_ERROR` | module-level |
| `setup.py:22` | `wiki.QUARTZ_PINNED_COMMIT`, `QUARTZ_REPO_URL` | module-level |
| `setup.py:773` | `wiki.install.wiki_install` | function-local |

Plus `maintenance.py:85`, still a **subprocess** invocation of `python -m brain.wiki.build_swap`
and still not an import — §9.2c's footnote on it is one of the few parts of the section that has
not moved.

**Where §9.2c's rows went** — each old row's importer survives, repointed off `wiki/`:
`cli.py:234 build_people` → **`cli.py:171`** `from .people import (…)`;
`connect.py:31` → **`connect.py:29`** `from .related import _avg_embedding, _eligible_source_docs`;
`review/weekly.py:40` → **`:39`** `from ..people import _doc_participant_keys`;
`graph_rag/aliases/__init__.py:12` → same line, now `from brain.person_name import humanize_person_name`;
`graph_rag/build.py:236` → `from ..people import _build_directory_index`;
`graph_rag/person_resolver.py:23,87` → `..people`, `:93` → `..person_name`;
`queries.py:196` → `.person_name`, `:246` → `.people`.

**Three of those line numbers moved by one or two lines and one did not move at all.** That is the
argument against auditing this section by line number rather than by symbol: the drift is small
enough to look like nothing and large enough to edit the wrong statement.

**A note on this appendix's own internal references.** B-1 and B-4 cite `§9.2:1459` and
`§9.2d:1487`. Those targets are now at **1463** and **1495/1498** — they drifted when the B-4
pointer was inlined into the very line it references. Harmless here, but it is the same
never-inherit-a-line-number failure the entries above document, committed by the record itself.

**What this re-derivation did not change.** No entry was **refuted**. Two were **widened** (B-2,
B-6) and three were **located more precisely** (B-5, B-8, and B-1's ruling). That is a good outcome
for the entries and a **poor** one for the two documents they describe: every defect recorded here
in phase 2 is still present in the phase-2 plan's own rows, because this appendix's convention is to
record rather than repair, and the **plan** has no appendix.

> ⚠ **THE PARAGRAPH ABOVE ENDED WITH A CLAIM ABOUT THE PLAN THAT IS NO LONGER TRUE, and it is
> struck rather than deleted because it dates this section.** It read: *"The spec now carries
> pointers at every misleading passage; `docs/plans/2026-08-13-wiki-to-ui-phase2.md` carries none,
> and its T7 and T15 rows would still mislead an implementer who read them today."* **Re-derived
> 2026-08-20:** the plan now has an **Appendix C**, a read-this-first banner at its head, and
> inline ⚠ annotations on the rows themselves — **T7's file list is struck and `+ src/brain/search.py`
> added** (Appendix C-5), and **T15's mutation is struck with the emptiness-guard substitute in its
> place** (Appendix C-2). Both were repaired in the plan after this table was written.
>
> **Two entries in the table above went stale the same way and are corrected here, not silently:**
> **B-5**'s status cell says T7's row *"still lists only"* three files — it no longer does; and
> **B-8**'s says the T15 row *"still reads"* the defective mutation — it no longer does either.
> The measurements both cells report are unchanged and still correct; only the "still" is wrong.
> **This is the appendix's own standing hazard arriving on schedule:** a status cell that describes
> another document is a claim about a moving target, and it decays faster than the finding it
> annotates. The finding is durable, the "still" is not.

---

### What this record says about the plan, as distinct from the spec

**Four entries — B-6, B-8, B-9, B-15 — are defects in the plan's own mutation column.**
*(This sentence read "four of the sixteen entries" until 2026-08-20, when it was found to be wrong: there
were seventeen. It went stale the moment B-17 was appended — a denominator that counts the list it lives in
cannot survive the list growing. The enumerated numerator is self-verifying and the denominator is gone,
which is the fix. Recorded rather than silently corrected because this appendix's own standing rule is
never to inherit a count, and this is that rule failing inside the document that states it.)* That column exists to name the mutation that proves a test can fail, and this
project's standing rule is that a mutation must redden *the assertion it targets*, not
merely produce a red run. One prescribed mutation was **inert** (B-6), one **reddened a
different assertion than the one it named** (B-8), one **could not be performed at all**
(B-9), and one **deadlocked the harness into a 13/13 error run** that reads as a successful
proof to anyone checking only the exit code (B-15). Each would have shipped looking like
coverage.

The document that instructs implementers how to prove a test can fail contained three
instructions that could not. That is not an argument against the mutation column — it is
the strongest available argument *for* the rule the column exists to enforce, since in all
three cases the implementer found the defect **by running the mutation and checking which
assertion reddened**, which is exactly what the column asks for and what a reader trusting
it would have skipped.
