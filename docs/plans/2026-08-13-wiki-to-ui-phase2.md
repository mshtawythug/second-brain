# Wiki → `brain ui` consolidation — phase 2 (navigation + content parity) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

*Planner: `phase2-planner-2`. Written 2026-08-13. Tree: `<worktree>` @ `f8c76c0`, branch `feat/wiki-to-ui-consolidation`. Contract: `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` (1,968 lines).*

## Reading order

> Report 3 supersedes Reports 1 and 2 wherever they differ. Reports 1 and 2 are retained because how an estimate was corrected is checkable and a corrected estimate alone is not.
>
> **AND READ APPENDIX C FIRST IF YOU ARE ABOUT TO IMPLEMENT A ROW.** Added 2026-08-20, a week after this plan was written. **Four of this document's declared mutations are defective** — one is inert, one reddens a different assertion than the one it names, one cannot be performed at all, and one deadlocks the harness into a run that reads as a successful proof. Each would ship looking like coverage. Appendix C names them, gives the substitute mutation for each, and re-derives the plan's other load-bearing claims against the tree as it stands today. **The spec has an Appendix B for exactly this purpose and this document had nothing; that asymmetry is what Appendix C closes.**

## Provenance — what this document is and is not

> ### ⚠️ THIS FILE IS NOT SAFE YET — it is gitignored
>
> **`docs/plans/` is ignored at `.gitignore:57`** (confirmed: `git check-ignore -v` resolves this file to that rule). This document survives the session that wrote it, but it **cannot be committed** and **will not survive a clean clone or a worktree migration** — migrating trees silently drop ignored files. **A file on disk is not a file that is safe.**
>
> Transcribing this plan *reduced* the exposure the planner raised; it did not close it.
>
> **This is a recurrence, not a discovery.** The phase-0 handoff §7 item 1 already recorded it — *"`docs/plans/` (this file) and `docs/audits/` are STILL gitignored and carry the same exposure"* — where it was named and not fixed. Current state: `docs/specs/` **tracked**, `docs/audits/` **ignored**, `docs/plans/` **ignored**.
>
> **The argument for fixing it is already written in `.gitignore` itself, at lines 53–56**, justifying why `docs/specs/` was un-ignored: *"CLAUDE.md rule 11b mandates that all specs live there, so leaving it ignored meant every design doc in the project was one clean checkout from being lost. It was PII-scrubbed first and is now synthetic-only… keep it that way (rule 15)."* **Rule 11b mandates plans live in `docs/plans/` in the same sentence it mandates specs live in `docs/specs/`.** The reasoning transfers unchanged, and the PII precondition is already met for this document (see PII clearance, below).
>
> `.gitignore` was deliberately **not** edited by the planner, the scribe, or the coordinator. It is a user decision and has been escalated as one.

- **Transcribed 2026-08-13 by a scribe agent (`plan-scribe`)** from `phase2-planner-2`'s messages, which were the plan's **only copy**.
- **The planner was dispatched read-only** and had no Write/Edit tool, so it could not author this file itself. It flagged the CLAUDE.md 11b violation rather than routing around the constraint.
- **The content is verbatim.** The scribe did not edit, condense, reorder, or improve it, and has **NOT re-verified any claim, line number, file path, or count against the tree.** Every `file:line` citation is the planner's, inherited unchecked. Per this repo's standing lesson — *never inherit a count; re-derive even a correction* — **re-derive before acting on any of them.** The planner was itself caught passing through one unverified spec citation; see S10.
- **T3 was BLOCKED ON ATTRIBUTION at the time of writing.** `src/brain/related.py` was being actively written by another agent during the planning pass — **610 lines at 18:17:26, 613 lines at 18:30:23**, and untracked (`??`). T3 must not start until that writer confirms `related.py` **finished**, not merely that it is theirs.

**Supersession handling.** The planner exercised its own call on five conflicts and instructed that the resolutions be *applied*, with the superseded value kept in a struck-through parenthetical so the correction stays visible. That is what this document does — nothing is silently resolved, and every prior value remains readable. **S1 is the deliberate exception: it is recorded and left unresolved.**

**Delivery note (disclosed for the record).** The planner's first attempt to send this content did not reach the scribe; the sends were accepted but nothing arrived, while the planner sat idle believing delivery had succeeded. A second attempt arrived intact. Two minor artefacts of that: part 2 was delivered twice, byte-identical (transcribed once), and one earlier planner message — the ready-to-run T1 recon SQL — was not re-sent in the three parts. It is preserved verbatim in the appendix, labelled as such.

---
---

# REPORT 1 — message `2bdce5b8` (the full breakdown)

*Note for the record: R1's §0 "S1" and §6 cut-list were written before the phase 6 move landed at 18:17. Read with Report 3.*

---

# Phase 2 plan — navigation + content parity

Read-only pass. Nothing written. Everything below is anchored to source I read in `<worktree>`.

---

## 0. HIGHEST-VALUE PART FIRST — what I believe the spec gets wrong

| # | Claim | Status | Evidence |
|---|---|---|---|
| **S1** | §11 phase 2: *"related-docs (after the §9.2 move)"* | **CONTRADICTS ITSELF, 3 ways.** §11 puts the §9.2 move in **phase 6**. §9.2's own VERIFIED block (d) says `compute_related(doc_id, limit)` is **NEW CODE, not a move**, and *"Authoring it is a **phase 5** need"*. So one row is simultaneously phase 2, phase 5 and phase 6. | spec:1894 vs 1898 vs 1487-1492 |
| **S2** | §3.2 email-thread mode is a **PORT — and simplify** | **NOT A PORT. It is new design, and it sits on top of a live rendering defect nobody has recorded.** `ingest/gmail.py:451-456` emits **raw HTML** `<details><summary>…</summary></details>` into the document body. `ui/render.py:234` parses with `html=False`. So every Gmail thread in `brain ui` **renders its `<details>` blocks as literal escaped text today**. §5 (phase 1, "rendering parity") never mentions raw HTML — it counted markdown constructs only — and neither does `docs/audits/2026-08-11-phase1-markdown-parity-report.md` (grep: zero hits for `details`/`raw HTML`). **Phase 1 exited with this open.** The port cannot be "reuse the body and add a toggle": `html=True` is an XSS hole and breaks §2.5 + A3. | gmail.py:404-456, render.py:234 |
| **S3** | **Q15** — *"`/api/search` must project a date; `schemas.py:361-371` returns no date key"* | **STALE — already fixed.** `schemas.py:361 result_date()` and `:395 "date": result_date(result)` exist. Don't budget it. | schemas.py:361-400 |
| **S4** | §3.1a TOC: *"headings are already parsed by `render.py`, so this is a token walk, not a new dependency"* | **Half true, and the missing half is a trap.** markdown-it tokenizes headings, but `render.py` emits **no `id` attributes** — grep for `heading_open`/`slugify`/`anchor` in `ui/*.py` returns nothing. A TOC needs anchors added *and* a heading list exported. Worse: `notes_service.py:189` renders `strip_redundant_title_heading(body, title)`, so the TOC **must walk the stripped body** or it will list an H1 that does not exist in the HTML it links into. | render.py, notes_service.py:188-191 |
| **S5** | Path references throughout: `quartz_overrides/…` | **Wrong path.** It is `src/brain/quartz_overrides/`. There is no top-level `quartz_overrides/` in this worktree. Cosmetic, but every executor will `ls` the wrong place. | verified |

Two more, softer:

- **S6 — §3.2 "People Hub … + additive live view".** The 51 `people/*.md` pages are `kind='vault'` documents with `vault_path` set, so **they already render in `brain ui` today** via `/api/tree` and `/api/search`. A `routes_people.py` is a nicer view, not parity. **I recommend cutting it from phase 2** and saying so explicitly rather than letting it ride as a PORT row.
- **S7 — §11 phase ordering.** Phase 3 ("Instrument & Page" token system, D1–D8, `--ink-3`) is **not done**, and phase 2 lands ~10 new visual surfaces (TOC, marginalia, palette, tag index, recent rail, lede, link kinds, icons, breadcrumbs, month groups). Every one gets restyled in phase 3. §11 permits reordering 1–5. **This is the biggest sequencing decision on the table and it is yours, not mine.** Cost of doing 2 first ≈ one restyling pass over ~10 surfaces; cost of doing 3 first ≈ phase 3 styles a shell with nothing new in it and phase 2 then edits phase 3's tokens anyway. My weak preference: **phase 3 first for `tokens.css`/`components.css` only**, then phase 2 builds against settled tokens.

---

## 1. THREE HARD BLOCKERS — none of them are code

| B | What | Why it blocks | Who decides |
|---|---|---|---|
| **B1** | **Pagination has no cheap implementation.** `hybrid_search()` (`search.py:321-351`) has **no `offset`/`cursor` parameter**. Two options: **(a)** add `offset` to `search.py` — an **eval-gated** module, and per auto-memory `SearchResult` is already diverging across branches; requires an eval re-baseline. **(b)** over-fetch in `routes_search.py` (`limit=offset+n`, slice) — zero ranking risk, RRF is deterministic so the slice is identical to a real offset, but the phase-0 measurement shows `sql=5,854 ms` at 545 matches, so page 4 re-pays the whole rank leg. | R-2 §4.4 is in phase 2 scope | you / Codex |
| **B2** | **The 233-unreachable-documents fix cannot be UI-local.** `search_predicate.py:87-89` filters `d.source_id IN (SELECT id FROM sources WHERE kind=%s)`; 876 docs have `source_id IS NULL` and are unreachable by **every** value. `hybrid_search` takes no raw predicate, so there is no UI-only escape. Minimal honest fix: **additive** `source_missing: bool = False` kwarg on `build_predicate` (default path emits byte-identical SQL → no eval impact) + a 5th UI filter value. Separately, the *reporting* half (`/api/status` says manual=934, `/api/facets` says 58, `/api/tree` says 799) is fixable cheaply **without** touching `summary_counts` (shared with the `brain status` CLI) by having the UI stop reading `by_kind` as a source facet. | R-2 §4.2 | you / Codex |
| **B3** | **The CI browser job runs a NAMED FILE.** `.github/workflows/ci.yml:316` → `pytest tests/test_ui_browser.py -m browser --no-cov`. **Every new browser test file phase 2 adds is silently unrun in CI.** That is precisely this repo's documented failure shape — a green gate that certifies nothing. Must be fixed *before* the first new browser file lands, not after. | phase 2's exit criterion is "demonstrably works", which is browser evidence | mechanical — assign it |

---

## 2. WHICH ROWS HAVE AN EXISTING IMPLEMENTATION TO PORT

| Row | Port source | Portability |
|---|---|---|
| Source icons | `src/brain/quartz_overrides/quartz/util/sourceIcons.ts` (55 lines) | **Straight port.** Table + `inferSource` + fallback. Trivial. |
| Link-kind system | `…/plugins/transformers/linkKindMark.ts` (205) | **Straight port** of `classifyLink`'s 5-way priority order into `render.py`'s `link_open`/`wikilink` rules. The classification logic is the value; the mdast plumbing is not. |
| Explorer counts / ingested toggle / month grouping | `…/components/scripts/explorer.inline.ts` (768) — `countFiles`, `SHOW_INGESTED_KEY`, the `YYYY-MM-DD` slug parser + month grouping | **Port the algorithms, not the code.** Counts belong server-side in `ui/tree.py` (pure, 95% tier). Month grouping parses the slug in the wiki; `brain ui` has a real `date` on every `TreeNote` already — **use the date, not a slug regex.** |
| Summary lede | `…/components/SummaryLede.tsx` (72) | **Near-free.** `notes_service.py:192` already ships `summary` in the note payload. Client + CSS only. |
| Recently-captured rail | `wiki/build_homepage.py:270-333 _fetch_recent_docs` | **Port the PREDICATES**, which carry real earned knowledge: `draft=FALSE`, `vault_path IS NOT NULL`, `<> 'index.md'`, `NOT LIKE 'people/%'`, order by `COALESCE(doc_date, ingested_at)`. Rewrite the SQL to return ids. |
| Backlinks marginalia | `vault/graph.py:163 backlinks_for` — **already exists, already tested, zero CLI coupling** | Server side is free. Only the route + marginalia column is work. |
| Related docs | `wiki/build_related.py` scoring half (15 symbols + 8 constants) | **The scoring MOVES; `compute_related()` is NEW.** See S1. |
| Command palette | `…/CommandPalette.tsx` (172) + `commandPalette.inline.ts` (665) | **Port the UX contract, discard ~80% of the code.** The 665 lines are mostly a client-side fuzzy index over `contentIndex.json` + Quartz SPA plumbing. `brain ui` already loads all 1,392 titles in the `/api/tree` payload — the palette is a filter over an in-memory array. **No new route.** |
| TOC | `Plugin.TableOfContents()` — **stock Quartz, not in the overlay** | **Nothing to read.** New code. See S4. |
| Tag index | `…/components/pages/TagContent.tsx` | Partial port; needs a new route + a view surface that does not exist. |
| Email-thread | `…/transformers/emailThread.ts` + `static/emailThread.js` | **See S2 — not a port.** |
| Breadcrumbs | `Component.Breadcrumbs` — stock | Trivial, client-side from `note.vault_path`. |
| pushState (R-12) | none | New. `store.js:47` uses `history.replaceState`. |

**Not a port (new design), separated as you asked:** TOC · email-thread mode · `compute_related()` · pagination · the unreachable-source filter · tag index surface · recent-rail placement (there is no home/idle surface in `brain ui` today — the ledger's idle state is blank) · pushState.

---

## 3. THE SERIALISATION MAP — read this before dispatching

Four files are **hotspots every surface wants to touch**: `ui/app.py` (route table), `static/index.html` (markup), `static/js/main.js` (boot wiring), `static/js/store.js` (state + URL).

**Recommendation: one dedicated *integrator* owns all four for the whole phase.** Implementers deliver a self-contained module plus a ≤3-line registration diff in the task report; the integrator applies them in one batched commit per wave. Everything else is genuinely disjoint.

A fifth constraint: **`ui/queries.py`'s docstring declares it "the only module in the package allowed to contain SQL"** and calls a route growing its own query a review rejection. So **all** phase-2 SQL funnels through one file → one writer. I have made that a single up-front task (T4) rather than five contending ones.

Sixth: **`css/components.css` is at 500 lines** against the project's 800 ceiling. Ten new surfaces will blow it. Add **one stylesheet per surface** — but note `test_ui_static_behaviour.check_every_stylesheet_is_linked_in_order` guards the `<link>` order in `index.html`, so each new file needs that guard extended (and the guard's own five-clause harness entry).

---

## 4. ORDERED TASK LIST

Sizing unit: **1 P4U = one phase-4 item ≈ 0.3 agent-days** (phase 4 = 5 items ≈ 1.5 days). Total below ≈ **31 P4U ≈ 9–10 agent-days**, i.e. **~6× phase 4**. If that is too much, the cut list is at §6.

> **SUPERSEDED — see Report 3.** Authoritative total is **30.0 P4U ≈ 9 agent-days**. The base recount is 32.5; T2 is deleted (−0.5) and T3 re-baselined 3.0 → 1.0 (−2.0). The "≈31" above is a rounding of 32.5, not a different scope.

### Wave 0 — unblock (serial; nothing else starts)

| # | Task | Files | Test | Mutation that proves the test can fail | Size |
|---|---|---|---|---|---|
| **T1** | **Recon, read-only SQL.** Count: docs whose `content` contains `<details>`; docs with `content_type='email_thread'`; docs with ≥3 headings (TOC value); `_ingested/manual` folder size. Write `docs/audits/2026-08-13-phase2-recon.md`. | audit doc only | n/a — this is evidence, not code | n/a | 0.5 |
| ~~**T2**~~ | ~~**Fix the CI browser selection (B3).** Change `ci.yml:316` to a marker-safe selection covering every `tests/test_ui_browser*.py`.~~ | ~~`.github/workflows/ci.yml`~~ | ~~Add a deliberately-failing browser test on a scratch commit, confirm CI goes red, revert.~~ | ~~*This IS the mutation.*~~ | ~~0.5~~ **DELETED — done by the coordinator's agent. Surviving constraint below.** |
| **T3** | **Pull the §9.2 `build_related` move forward (S1).** Move the 15 scoring symbols + 8 constants → new `src/brain/related.py`; `wiki/build_related.py` becomes a thin emitter importing from it (spec §9.2d rule 2 — it must survive, `build_swap.py:603`/`build_watcher.py:859` need `refresh_related`); repoint `connect.py:31`. Then author ~~**`compute_related(conn, doc_id, limit)`**~~ **`compute_related(conn, doc_id, *, limit=DEFAULT_RELATED_LIMIT, vector_sim_floor)`** [⚠ **Appendix C-6.** The signature above omits `vector_sim_floor`, which is **required with no default, deliberately** — the cosine floor is shared with runtime `brain search` and must not silently diverge. The plan's form is closer to correct than the spec's, which omits `conn` as well.] — new code, needs both a `_SourceDoc` and `_corpus_common_lexemes`. ~~**PII: scrub `build_related.py:76-83` if that comment migrates (CLAUDE.md r15).**~~ | `src/brain/related.py` (new), `wiki/build_related.py`, `connect.py`, `tests/test_build_related_signal.py` | New `tests/test_related_compute.py`: seed A, B, C where A shares distinctive lexemes with B only; assert `compute_related(A)[0].id == B`. Plus: `python -c "import brain.cli"` and `brain connect` path still resolve. | Reverse the RRF sort key in `_neighbors_for_source` → B and C swap → assertion fails. (Not "returns 2 rows" — that survives the defect.) | ~~3.0~~ → **1.0** |

> **T2's surviving constraint, promoted to a standing rule:** ~~new browser test files must match **`test_ui_browser*.py`** so they fall inside `ci.yml:328`'s glob.~~ `tests/test_ci_workflow.py:545 test_ci_browser_selection_covers_every_browser_module` discovers modules **by marker, not filename**, so a non-matching name turns the guard **RED** rather than silently skipping. ~~The proposed `test_ui_browser_nav.py` / `test_ui_browser_reading.py` comply.~~
>
> ⚠ **RE-DERIVED 2026-08-20 — THE MECHANISM INVERTED; THE CONCLUSION SURVIVED. Appendix C-7.**
> **There is no glob any more.** `ci.yml` now names every browser module as an **explicit path in byte
> order** (in the `-m browser --no-cov` step; re-derive the list with
> `grep -n 'tests/test_ui_browser' .github/workflows/ci.yml`), because a shell glob sorts by ambient locale and expanded
> `test_ui_browser.py` last under macOS and first under a POSIX-locale runner — an order-dependent
> interaction invisible to local reproduction by construction. **So a conforming *filename* no longer
> buys inclusion: a new module must be ADDED TO THE LIST.** The marker-based guard at
> `test_ci_workflow.py:545` is unchanged and still turns RED on omission, which is why the rule's
> conclusion holds and only its mechanism is wrong — but an implementer following the rule as written
> would name the file correctly, believe it covered, and be surprised by the red. Both proposed
> modules now exist and **are** listed.
>
> *(Corrected 2026-08-21: both sites of this note said ~~"seven modules" at `ci.yml:354-360`~~. It is
> **eight**, at `:354-361`. The count is not restated in its place because nothing in the argument
> needs it — the rule is "a new module must be ADDED TO THE LIST", which holds at any count, and a
> figure that must be re-edited every time someone adds a browser test is a figure that will be
> wrong again. The grep above answers it on demand.)*

> **T3 as re-baselined:** the move itself landed at 18:17 on 2026-08-13. `related.py` (613 lines) holds all 15 symbols + 8 constants; `wiki/build_related.py` is 248 lines importing `DEFAULT_RELATED_LIMIT, _iter_hybrid_neighbors`; `connect.py:29` is repointed; no duplication. **`compute_related` still exists nowhere.** The PII sub-task is struck — see resolution 4. **T3 is BLOCKED ON ATTRIBUTION** until `phase6-mover-2` declares `related.py` finished, not merely theirs.

### Wave 1 — server (parallel; disjoint files)

| # | Task | Files | Test | Mutation | Size |
|---|---|---|---|---|---|
| **T4** | **All phase-2 SQL, one file.** `recent_documents()` (port the 5 predicates from `build_homepage.py:270-333`, return ids), `documents_for_tag()`, `tag_counts()` (note: `queries.list_existing_tags:517-522` **already computes counts and throws them away** — `/api/facets` ships `count: null` for tags today; free fix). | `ui/queries.py`, new `tests/test_ui_queries_discovery.py` | Seed a `people/x.md` doc, an `index.md`, and a draft; assert `recent_documents()` returns none of them and orders by `coalesce(doc_date, ingested_at)`. | Drop `NOT LIKE 'people/%'` → the seeded people page appears in the list → fails. Each predicate gets its own mutation entry (clause (e)). | 1.5 |
| **T5** | **`render.py`: heading anchors + TOC extraction + link-kind stamping.** Add a `heading_open` rule minting deterministic ids; export `extract_headings(text) -> list[Heading]` walking the **same stripped body** (S4); port `linkKindMark.classifyLink`'s 5-way order onto `link_open`/`wikilink`. **Anchors need no github-slugger parity** — nothing outside `brain ui` consumes them, so `wiki/_github_slugger.py` (used only by `fastpath_manifest.py`) does **not** need rescuing. | `ui/render.py`, new `tests/test_ui_render_toc.py`, `tests/test_ui_render_link_kinds.py` | (a) Two headings with the same text get distinct ids and the TOC's `href` **resolves to an id present in the HTML**. (b) A `[[wiki]]`, an `http://`, a `tags/x`, an `_ingested/…` link each get the right `data-brain-link-kind`. (c) A3 XSS test on the new attribute path. | (a) Remove the duplicate-suffix → both ids collide → the "TOC target exists and is unique" assertion fails. **Assert the target resolves, not that a `<a href="#…">` exists** — the latter survives the bug. ~~(b) Reorder `classifyLink` so `external` precedes `tag` → a `tags/` link misclassifies.~~ [⚠ **INERT AS WRITTEN — Appendix C-1.** Under `linkKindMark.ts`'s *code* the tag and external prefix sets are **disjoint**, so no reordering changes any answer. The mutation only lands once the **documented infix** semantics are ported, which they now are (`render.py:74` `_TAG_INFIX`). **Substitute:** classify `https://host/tags/retro` and assert `tag`; then delete `_TAG_INFIX` from the check at `render.py:181` and it reddens. **RUN 2026-08-20: 2 failed, 24 passed** (baseline 26) — two, not one; C-1 has the detail and the record is in the test's docstring.] | 2.5 |
| **T6** | **Pagination (B1 — ruled: over-fetch, option b).** The perf caveat is accepted, not unnoticed — page 4 re-pays a 5,854 ms rank leg. | `ui/schemas.py`, `ui/routes_search.py` | Seed 60 matching docs; assert page 2 returns rows 26–50 and that **no id appears on both pages** and **the union equals the unpaginated top-50**. | Off-by-one the slice → overlap assertion fails. (A "returns 25 rows" assertion survives an off-by-one — do not write that one.) | 1.5 |
| **T7** | **Unreachable sources (B2 — ruled: additive `source_missing` kwarg + byte-identical-SQL pin).** | ~~`search_predicate.py`, `ui/schemas.py`, `ui/routes_meta.py`~~ **+ `src/brain/search.py`** [⚠ **Appendix C-5.** Without it the feature is undeliverable as specified: `hybrid_search` calls `build_predicate` with explicitly named kwargs and does **not** splat, so a filter added only to the predicate is unreachable from the UI. Delivered at `search.py:328` and `:456`.] | (a) With the flag **off**, the generated SQL is byte-identical to the pre-change string (pin it). (b) With it on, a seeded `source_id IS NULL` doc is returned and is returned by no other value. | (a) Add a stray clause to the default branch → byte-equality fails → proves the eval-neutrality claim is checked, not asserted. (b) Change the new clause to `IS NOT NULL` → the doc vanishes. | 1.5 |
| **T8** | **Note-payload defects (R-2 §4.5).** `editable` must be `False` when `ctx.read_only` (`notes_service.py:161` currently ignores it — and `keys.js:19` Cmd+E checks **only** `state.note.editable`, so the keyboard path enters edit mode on a read-only server today, confirmed). Drop `body` from the payload when it can never be used (570 KB → ~287 KB on the largest doc). | `ui/notes_service.py`, `tests/test_ui_routes.py` (+~40 lines) | (a) `GET /api/notes/{id}` on a `--read-only` app → `editable is False`. (b) On a read-only app the payload has **no `body` key** and `html` is still non-empty. | (a) Restore the ungated expression → `editable` is `True` → fails. (b) Re-add `body` → the key-absence assertion fails. [⚠ **BOTH WERE ONLY EVER A PRESCRIPTION. Run 2026-08-20**, and the evidence now lives where a reader of the test will meet it: the two mutation records are in the docstrings of `test_read_only_server_reports_the_note_as_not_editable` and `test_read_only_payload_omits_the_body_but_still_renders` (`tests/test_ui_routes.py`). Each reddens its own test alone — **1 failed / 40 passed** on `pytest tests/test_ui_routes.py --no-cov`, at `notes_service.py:172` and `:211` respectively. A row that names a mutation nobody records running is a prescription, not coverage.] | 1.0 |
| **T9** | **Explorer counts, server half.** Recursive leaf counts on `TreeNode.to_payload()`; ingested/vault split per folder so the client toggle can recount without a refetch. | `ui/tree.py`, `tests/test_ui_tree.py` | Build a 3-level fixture; assert a grandparent's count equals the sum of its descendants' leaves, **and** that a collapsed subtree still contributes. | Change the fold to count only direct children → the grandparent's number drops → fails. | 1.0 |
| **T10** | **New read routes + route table.** `routes_links.py` (`GET /api/notes/{id}/links` → `backlinks_for` + `outgoing_links_for`), `routes_discovery.py` (`/api/recent`, `/api/tags`, `/api/tags/{tag}`). **Lazy, separate from the note payload** — R-2 §4.5 already flags a 570 KB note fetch; do not grow it. Fails closed (§3.2). | `ui/routes_links.py`, `ui/routes_discovery.py` (new), `ui/app.py` *(integrator)*, new `tests/test_ui_routes_links.py`, `tests/test_ui_routes_discovery.py` | Seed A→B; `GET /api/notes/{B}/links` lists A **and** `GET /api/notes/{A}/links` does not list A as its own backlink. Derived edges appear under `link_kind='derived'`. | Swap `src`/`dst` in the query → A becomes its own backlink → fails. (A "200 OK with a `backlinks` key" test survives that.) | 1.5 |

### Wave 2 — client surfaces (parallel; one new module each, integrator wires)

| # | Task | Files | Test (all `browser` marker, Playwright) | Mutation | Size |
|---|---|---|---|---|---|
| **T11** | **R-12 pushState.** Replace `replaceState` at `store.js:47` for *navigational* changes (open note, run search); keep `replaceState` for keystroke-level debounced typing or Back walks every character. Add a `popstate` handler. | `js/store.js`, `js/main.js` *(integrator owns both)*, new `tests/test_ui_browser_nav.py` | Open note A, open note B, press Back → **note A's title is in the inspector** and the URL's `id` is A's. | Revert to `replaceState` → Back leaves note B on screen → fails. Assert the rendered title, **not** `history.length`. | 1.5 |
| **T12** | **Command palette.** New `js/palette.js` + `<dialog>` in `index.html` + `css/palette.css`. Fuzzy over the loaded `/api/tree` titles; ⌘P; Enter opens; `role=combobox`/`aria-activedescendant` per the overlay's a11y contract. **Decide ⌘K vs ⌘P** — ⌘K is already bound to "focus the search box" (`keys.js:11`). | `js/palette.js`, `index.html`+`keys.js`+`css` *(integrator)*, `tests/test_ui_browser_nav.py` | Press ⌘P, type 3 chars of a seeded title, ↓↓, Enter → **that specific note is open in the inspector**. | Break the arrow-key index (always select 0) → the wrong note opens → fails. (A "dialog is visible" test survives everything.) | 2.5 |
| **T13** | **TOC + breadcrumbs** in the marginalia column. Consumes T5's `headings`. | `js/marginalia.js` (new), `css/marginalia.css`, `tests/test_ui_browser_reading.py` | Click the 3rd TOC entry → the corresponding `<h2>` is scrolled into view (assert the element's `getBoundingClientRect()`, not the hash). | Emit TOC hrefs from the **unstripped** body (S4's trap) → entry 1 points at a heading absent from the DOM → fails. | 2.0 |
| **T14** | **Backlinks marginalia + related-docs panel** (consumes T10, T3). Lazy fetch after the note paints; render nothing on failure. | `js/marginalia.js` *(same owner as T13 — serialise these two)*, `tests/test_ui_browser_reading.py` | Open B (linked from A) → A's title appears in the backlinks rail. Stub the route to 500 → the note still renders and no error toast fires. | ~~Make the fetch synchronous/blocking → the note body assertion times out → fails.~~ [⚠ **DEADLOCKS THE HARNESS — Appendix C-3.** A sync `XMLHttpRequest` blocks the page's main thread inside a `page.evaluate` Playwright is awaiting, while the response can only come from a route handler blocked on that same evaluate. Result: **13 errors in 116 s**, every test in the file erroring at fixture setup — *which reads as a successful proof to anyone checking only the exit code.* **Substitute:** route the request through an **async** XHR, which never touches the test's patched `window.fetch` and sails past its gate — isolates the same property at 1 failed / 12 passed. **Re-run 2026-08-20: the deadlock no longer reproduces** — the sync form reddens exactly the named assertion at 1 failed / 16 passed. C-3 carries the detail and is kept, not deleted; the async substitute is still the better mutation.] | 1.5 |
| **T15** | **Summary lede + source icons + link-kind styling.** | `js/inspector.js`, `js/results.js`, `css/reading.css` — **two owners, serialise or split by file** | Icons: a krisp result row renders 🎙️ and a gmail row 📧 (assert the glyph next to the right row, not that any glyph exists). Lede: a note with `summary` renders the aside; one without renders **no empty aside**. | Collapse the icon map to a single default → both rows show the same glyph → fails. ~~Return `""` for summary → the "no empty aside" assertion fails.~~ [⚠ **REDDENS THE WRONG ASSERTION — Appendix C-2.** Measured: returning `""` reddens the **presence** assertions (`tests/test_ui_browser_lede.py:411-412`) and leaves **both absence assertions green** (`:512` no-summary, `:523` blank-summary) — of course it does, with no summary there is nothing to render, so "renders no empty aside" is trivially satisfied. An implementer following this row literally sees red and records the absence assertions as proven when they never fired. **Substitute:** drop the emptiness guard at `src/brain/ui/static/js/inspector.js:242` (`if (summary) head.appendChild(…)`) — then both absence assertions redden.] | 1.5 |
| **T16** | **Explorer client half** — ingested toggle (persisted, default OFF per the overlay), month grouping using `TreeNote.date`, count badges. | `js/tree.js`, `tree_nav.js`, `tests/test_ui_browser.py` *(existing file)* | Toggle off → `_ingested` subtree gone **and** every folder count drops by exactly its ingested leaf count. Krisp folder shows `Mon YYYY` headers, newest first. | Filter the rendered tree but not the counts → the counts stay high → fails. **This is the exact defect the overlay comment warns about** ("the count is computed from the post-filter trie"). | 2.0 |
| **T17** | **Recent-captured rail + tag index.** Needs a home for both — the ledger's idle state is currently blank. **This is new design, flag it as such.** | `js/discovery.js` (new), `index.html` *(integrator)*, `css/discovery.css` | Boot with no query → the rail shows the 12 most recent, no drafts, no `people/` pages. Click a tag → the ledger shows only docs carrying it. | ~~Drop the client-side draft guard **and** T4's SQL guard together — one alone leaves the other covering it, which is a test that cannot fail. Mutate **both**~~, or assert against the route payload directly. [⚠ **UNIMPLEMENTABLE — Appendix C-4.** There is **no client-side draft guard and there cannot be one**: the discovery payload carries no `draft` field, so the client cannot distinguish a draft from anything else. The defence-in-depth pair this instruction reasons about never existed. **Use the row's own fallback** — assert against the route payload — which T10 already delivered.] | 2.0 |

### Wave 3 — the one that is not a port

| # | Task | Files | Test | Mutation | Size |
|---|---|---|---|---|---|
| **T18** | **Email-thread mode (S2).** Requires a decision first: **(a)** teach `render.py` to recognise and re-emit `<details>`/`<summary>` as *structurally generated* safe HTML (never pass-through), keeping `html=False`; **(b)** change `ingest/gmail.py` to emit markdown + a fenced marker and re-render affected docs; **(c)** ship the thread as flat headings and drop collapse. Then: newest-open, older-collapsed, "only my replies" reading `cfg.user_email` **per request** (the port's genuine win — the wiki bakes `BRAIN_USER_EMAIL` at build time). | `ui/render.py` *(serialise with T5)*, `js/thread.js`, `css/thread.css` | A gmail-thread fixture renders **N `<details>` elements, not N escaped `&lt;details&gt;` strings**; newest is `open`; the filter hides sections whose `From:` ≠ config email. Plus an A3 XSS test: a `<details>` containing `<script>` renders inert. | Feed the thread through the current renderer → the "N elements" count is 0 → fails. This test **fails on today's code**, which is the point: write it first. | **3.0** |

### Wave 4 — close-out

| # | Task | Size |
|---|---|---|
| **T19** | Extend `check_every_stylesheet_is_linked_in_order` + its five-clause harness entry for every new stylesheet; keep `components.css` under 800. | 0.5 |
| **T20** | Rule-14 loop: `superpowers:code-reviewer` + completion auditor, repeat to clean. | 1.0 |
| **T21** | Update `docs/specs/` corrections (S1–S10) **in the spec itself**, per this project's convention of recording rather than quietly fixing. [⚠ **Was marked done having recorded three of ten** — S1, S8 and S9 (spec Appendix B-1/B-2/B-3). S2, S3, S4, S5, S6, S7 and S10 had no entry anywhere and the body passage each corrects stood unannotated, so the appendix's own invariant — *every body passage it contradicts carries a pointer* — was false for seven of them. **Completed 2026-08-21** as spec Appendix **B-20 … B-26**, S10 first because it was the one with a live cost: a PII citation aimed at a line range that no longer holds PII. Note the convention was never the problem — "recorded, not repaired" was kept for all seven.] | 0.5 |

**Parallelism:** W0 serial → W1 has 7 tasks across 7 disjoint files (T4/T5/T6/T7/T8/T9/T10 concurrent; the integrator lands `app.py` once at the end of the wave) → W2 has 6, with **T13+T14 serialised on `js/marginalia.js`** and **T15 split by file** → T18 serialised against T5 on `render.py`.

---

## 5. WHERE THE NEW TESTS GO

Both existing files are over the ceiling and I am not planning to grow them.

| File | Now | Plan |
|---|---|---|
| `tests/test_ui_routes.py` | 1230 | **+~40 lines only** (T8's two note-payload assertions belong with the existing note-payload tests). |
| `tests/test_ui_browser.py` | 1213 | **+~60 lines only** (T16 — the tree tests and fixtures are already here). |
| `tests/test_ui_static_behaviour.py` | 1329 | **+T19 only.** |
| **new** `tests/test_ui_routes_links.py` | — | T10 backlinks/outgoing/derived |
| **new** `tests/test_ui_routes_discovery.py` | — | T10 recent/tags, T6 pagination, T7 sources |
| **new** `tests/test_ui_queries_discovery.py` | — | T4 |
| **new** `tests/test_ui_render_toc.py` | — | T5 headings/anchors |
| **new** `tests/test_ui_render_link_kinds.py` | — | T5 classification + A3 XSS |
| **new** `tests/test_ui_render_email_thread.py` | — | T18 |
| **new** `tests/test_ui_browser_nav.py` | — | T11 pushState, T12 palette |
| **new** `tests/test_ui_browser_reading.py` | — | T13 TOC, T14 marginalia, T15 lede/icons |
| **new** `tests/test_related_compute.py` | — | T3 |

> **SUPERSEDED — see Report 3.** R1 said *"all three new `test_ui_browser_*.py` files are invisible to CI until T2 lands."* **No longer true:** `ci.yml:328` now globs `tests/test_ui_browser*.py` and `test_ci_workflow.py:545` enforces marker coverage. The residual requirement is the **naming rule** above.

Separately, and **not** phase-2 work: `test_ui_routes.py`, `test_ui_browser.py`, `test_ui_static_behaviour.py` and `test_ui_telemetry_persists.py` are all over 800, and B3 was deferred by agreement in the phase-0 handoff (§4a item 3). Still deferred; not folded in.

---

## 6. IF YOU NEED THIS SMALLER

Cut, in this order, with reasons:

1. **T17 tag index + recent rail (2.0)** — new design, no home surface exists, and neither is a regression from the wiki for a user who searches.
2. **People view (already cut, S6)** — the 51 pages render today.
3. **T18 email-thread (3.0)** — but then **file the rendering defect as a known-open item**, because it is live and phase 1 shipped past it. Do not let it disappear by being deferred.
4. **T12 palette (2.5)** — ⌘K + `/` already reach search. The row I would fight hardest to keep: §3.1 calls it "primary fast-nav" and it is the one surface that makes a 1,392-note corpus navigable.

**Do not cut T2 or T3.** *(T2 is now done; T3 is protected.)*

---

## 7. One process note

The phase-0 handoff's §8 environment list still applies verbatim — machine-wide test-DB lock, declare-intent-before-taking-it, `-m browser` runs lock-free **only for a named-file selection**, `find tests -name "test_*.py"` (433) not `ls tests/test_*.py` (409). With 7 concurrent wave-1 writers, the lock is the real throughput ceiling, not the writing. Give each implementer its own `TEST_DATABASE_URL` (**names ≤22 chars** — a 23-character name truncates silently and surfaces two files away as a fake contract violation).

### Critical Files for Implementation
- `src/brain/ui/render.py`
- `src/brain/ui/queries.py`
- `src/brain/ui/static/js/store.js`
- `src/brain/wiki/build_related.py` → `src/brain/related.py`
- `.github/workflows/ci.yml`

**— END OF REPORT 1 —**

---
---

# REPORT 2 — message `4c42d4c3` (provenance verification and corrections)

*Context: the coordinator challenged whether my evidence came from the correct checkout, because this session's shell defaulted to `<main-checkout>` on `feat/agentic-token-reduction`. Retained because the verification method is reusable and because it records a real error of mine.*

## Provenance — all evidence came from the correct tree

```
pwd                        → <main-checkout>
git rev-parse --abbrev-ref → feat/agentic-token-reduction
```

The pane shell **was** in the wrong tree. The analysis was not: every Bash call was prefixed `cd <worktree> &&`, and every `Read` used an absolute path under that root.

**The hazard was real and specific — a stale copy of the spec exists in the wrong tree:**

| | `second-brain` (wrong) | `second-brain-wiki-ui` (target) |
|---|---|---|
| spec | **1,333 lines** (101 KB, Aug 10) | **1,968 lines** (145 KB, Aug 12) |

I quoted §11 and §12 from **lines 1885–1968** — line numbers that **do not exist** in the stale copy. Positive proof of which file was read, not a reassurance.

### The decisive discriminator

| | wrong tree | target tree |
|---|---|---|
| `.github/workflows/ci.yml` | **198 lines** | **316 lines** |
| occurrences of `m browser` | **0** | **3** |

**Line 316 does not exist in the other checkout and the string appears there zero times.** I quoted it verbatim, so the read was only possible in `second-brain-wiki-ui`. `git status` showed ` M` (uncommitted working-tree edit) with the last commit being the dependabot bump `f45248f` — i.e. the coordinator's own agent had written it minutes earlier.

### The four flip-risk findings, each checked

| # | Finding | wrong tree | target | verdict |
|---|---|---|---|---|
| 1 | **S2** — `render.py` parses with `html=False` | file is **165 lines — line 234 does not exist** | 281 lines; `:233-235` = `MarkdownIt(` / `"commonmark",` / `{"html": False, …}` | **from the target. S2 stands.** |
| 2 | `ci.yml:316` named-file selection | line absent, 0 hits | present | **from the target** |
| 3 | **S3** — Q15 already fixed | `:361` = `def search_result_payload(...)`, **no `result_date` anywhere** | `:361` = `def result_date(result) -> str \| None:` | **from the target.** Reading the other branch would have produced the opposite conclusion. |
| 4 | line counts 1230 / 1213 / 1329 / 500 | 337 / **absent** / **absent** / **absent** | 1230 / 1213 / 1329 / 500 | **from the target.** Three of four files do not exist in the other checkout. |

**Nothing needed re-deriving.** Precision fix to my own citation: `html=False` is on **`:235`**, inside the `MarkdownIt(` call opening at `:233` — I had written `:234`.

## The error I did make

**I repeated the spec's `build_related.py:76-83` PII citation without opening it.** It is wrong — that range is a docstring. This is the "never inherit a count; re-derive even a correction" failure, committed against a block the spec itself labels VERIFIED, and I asserted it as a constraint on an implementer.

**Substance checked and clean:** the tuning comments did **not** carry PII into the new module. `related.py:40-90` explains RRF thresholds, the stop-token gate and the frequency cutoff in entirely generic terms, no corpus lexemes. **CLAUDE.md r15 satisfied; the warning is closed, not carried.**

## The time-based correction

**While I was planning, another writer landed the §9.2 move into this worktree.**

```
src/brain/related.py            created  18:17:26   (610 lines, UNTRACKED)
src/brain/wiki/build_related.py rewritten 18:17:33   (815 → 248 lines)
src/brain/connect.py            modified 18:17:58
```

My earlier grep showing the scoring functions still inside `build_related.py` **was correct when it ran**. This is handoff §8's *"THE LOCK AND THE TREE ARE DIFFERENT RESOURCES — a write takes no lock, emits no announcement, and is invisible to every signal we have."*

## Two habits this produced

1. **Re-stat before reporting.** Reads spanning ~40 minutes in a worktree with other active writers are a snapshot; re-check the files the largest task depends on immediately before sending, not only when challenged.
2. **Never restate a spec `file:line` without opening it.** The spec has ~17 VERIFIED blocks and executors will read line numbers out of all of them.

**— END OF REPORT 2 —**

---
---

# REPORT 3 — message `ace4afce` (re-baseline; AUTHORITATIVE where it differs)

## Plan-document status

The plan existed only in messages. I was dispatched read-only with no Write tool, declined to route around that constraint, and flagged the CLAUDE.md 11b violation instead. This document is the resolution.

## Re-baselined wave 0

Verified at 18:37 on 2026-08-13.

| # | Task | Was | Now | Note |
|---|---|---|---|---|
| **T1** | **Recon (FIRST).** Read-only `SELECT` over prod: documents with `<details>` in `content`, split by `content_type`; plus **any raw-HTML block**, not only `<details>` — `html=False` may be escaping more than S2 found. Gates T18's (a)/(b)/(c). | 0.5 | **0.5** | unchanged |
| ~~T2~~ | ~~Widen the CI browser selection~~ | 0.5 | **DELETED** | done by the coordinator's agent |
| **T3** | **Author `compute_related(conn, doc_id, limit)`** + verify the landed move. Confirmed **still absent everywhere**. | 3.0 | **1.0** | **BLOCKED ON ATTRIBUTION** |

**A12 gate run:** `python -c "import brain.cli"` → **PASS**, and **zero** surviving references to `wiki.build_people` / `wiki._person_name` outside a stale `SOURCES.txt` build artifact. The staged renames did not strand §9.2c's nine importers.

> **Caveat, load-bearing:** this A12 result was obtained against a tree being written **during** the check — `related.py` gained three lines inside the observation window. It is a green import **at one instant**, not a property of the tree. **Re-run after the write freeze.**

### Phase total

| | P4U |
|---|---|
| original (precise recount) | **32.5** |
| − T2 deleted | −0.5 |
| − T3 re-baselined | −2.0 |
| **new total** | **30.0 ≈ 9 agent-days** |

The earlier "≈31" and "≈29" were roundings of the same 32.5 recount. **The drift is arithmetic, not scope.**

**Phase 6's budget drops, not phase 2's** — ~~three of its four moves are done early~~ **all three of its moves are done early; there was never a fourth.** §11 numbers exactly three (`_person_name`, `build_people`, `build_related`) and all three have landed, so the phase-6 move budget is not mostly spent but **entirely** spent. §11 does not reflect that. *(Corrected 2026-08-21 to match spec Appendix B-3, which caught the same invented denominator on 2026-08-20; this second site was missed then.)*

## Spec defects — consolidated

| # | Defect | Status |
|---|---|---|
| **S1** | related-docs is simultaneously phase 2 / 5 / 6 | **recorded, UNRESOLVED in the spec.** Operationally pre-empted by the landed move; a reader of the spec alone would still be misled |
| **S2** | email-thread is not a port; sits on a live rendering defect | **filed as a live bug** independent of T18's fate |
| **S3** | Q15 stale — date projection already shipped | dropped from budget; retained as a stale spec claim |
| **S4** | TOC "just a token walk" — no heading ids exist; stripped-body trap | accepted |
| **S5** | `quartz_overrides/` path wrong — it is `src/brain/quartz_overrides/` | corrected in the record |
| **S6** | People Hub "additive live view" — the 51 pages already render | **cut from phase 2** |
| **S7** | Phase ordering — phase 3 tokens not done, phase 2 lands ~10 surfaces | **ruled: phase 3 token work first**, `tokens.css`/`components.css` only |
| **S8** | **§9.2c's "authoritative importer list" is now stale** — every row naming `wiki.build_people` / `wiki._person_name` points at module paths that no longer exist | **new** |
| **S9** | **§11's phase 6 row describes work already done** — three of four moves landed; only "No deletion in this phase" is still load-bearing | **new** |
| **S10** | §9.2's PII citation `build_related.py:76-83` is wrong (docstring) | **CLOSED** — substance clean, r15 satisfied. *Closed **in the code**, not in the spec: §9.2d went on asserting the hazard at that range until 2026-08-21. See spec Appendix B-20 for what closed it and what would reopen it.* |

> **Where each of these is recorded in the spec** (T21's actual deliverable):
> S1 → **B-1**, S8 → **B-2**, S9 → **B-3** (landed 2026-08-14/20);
> S10 → **B-20**, S2 → **B-21**, S4 → **B-22**, S3 → **B-23**, S5 → **B-24**, S6 → **B-25**,
> S7 → **B-26** (landed 2026-08-21). The second block is ordered by urgency, not by S-number —
> S10 leads because it was the only one carrying a live PII cost. Re-derive rather than trusting
> this line: `grep -n '^### B-2[0-6] ·' docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md`.
> S9's row above still says "three of four moves"; the denominator is **three** — corrected at the
> phase-6 budget note earlier in this document and in spec B-3.

## Blocked on attribution — exactly one task

mtimes for every file the plan touches. Only seven modified today (18:17–18:32); everything else dates to Aug 10–12 (phases 0/1/4) and is attributed.

| File | mtime | git | Task |
|---|---|---|---|
| **`src/brain/related.py`** | **18:30:23** | `??` | **T3 — BLOCKED** |
| `src/brain/people.py` | 18:29:28 | `AM` | none |
| `src/brain/person_name.py` | 18:29:00 | `AM` | none |
| `src/brain/wiki/build_related.py` | 18:17:33 | ` M` | T3 (verify only) |
| `src/brain/connect.py` | 18:17:58 | ` M` | T3 (verify only) |
| `.github/workflows/ci.yml` | 18:28:31 | ` M` | ~~T2~~ |
| `tests/test_ci_workflow.py` | 18:32:29 | ` M` | ~~T2~~ |

**T3 is the only blocked task, and it is worse than unattributed — it is in flight:** `related.py` was 610 lines at 18:17:26 and 613 at 18:30:23. T3 must not start until `phase6-mover-2` confirms **finished**, not merely **mine**.

**T4 through T21 sit on files untouched since Aug 12 and are safe to dispatch.**

## The half-staged migration — highest-severity item in this report

`people.py` and `person_name.py` are staged renames (`AM`) with rename detection intact; `related.py` is **untracked** (`??`); `build_related.py` and `connect.py` are **unstaged** (` M`).

**A commit taken now captures the two renames and leaves the `build_related` half on the floor** — `build_related.py` would import from a `brain.related` that is not in the commit. `import brain.cli` passes locally and fails for everyone else. **A self-concealing breakage. All four move together or none do.**

**— END OF REPORT 3 —**

---
---

## Closing state

**As of 2026-08-13 18:37, tree `<worktree>` @ `f8c76c0`:**

- Phase 2 is **30.0 P4U ≈ 9 agent-days**, 21 tasks, T2 deleted.
- **T1 (recon) is the first task** and gates T18's (a)/(b)/(c) choice. Provisional recommendation is **(a) structural re-emission**, because it preserves `html=False` — the property A3 and §2.5 rest on — whereas **(b)** rewrites document bodies and therefore moves `content_hash`, dedup, chunking, embeddings and `body_hash` together, which is a corpus migration wearing the costume of a rendering fix.
- **T3 is blocked on `phase6-mover-2` declaring a finished state**, not merely claiming authorship. T4–T21 are cleared.
- **Nothing commits until the four migration files move together** — `related.py` (`??`), `build_related.py` (` M`), `connect.py` (` M`), and the two staged renames (`AM`). A commit that takes only the renames produces a tree where `import brain.cli` passes locally and fails for everyone else.
- Open user decisions: commit permission; ⌘K vs ⌘P for the palette (T12); the `--ink-3` token value (phase 3).

**The A12 result carries this caveat, and it is load-bearing:** `import brain.cli` PASS and zero live references to the old module paths were obtained against a tree being written **during** the check — `related.py` gained three lines inside the observation window. It is a green import **at one instant**, not a property of the tree. **Re-run after the write freeze.**

**Two rules this phase inherits and must not lose:** *(**RECOVERED**, not lost — see the note below.)*

1. **Every behaviour needs a test whose failure mode is the behaviour**, not a status code or a string's presence. Each mutation column names what to break; a mutation that leaves the test green means the test was aimed at something that does not change when the defect is present.
2. **A confirmation that needs the lock becomes another writer competing for the resource whose freedom it is trying to certify.** Confirm freezes at file level; verify afterwards with `find src tests -name "*.py" -newermt <start> ! -newermt <end>` returning empty.

> **Recovery note.** This block was the one part of the plan at genuine risk: five of the planner's sends to the scribe failed silently, and the planner had to relay it through the coordinator to get it here. It arrived. It is **recovered, not lost.**
>
> **Scribe note on rule 2's command — a warning was relayed to me, and it did not reproduce.**
>
> I was asked to append, in my own voice, that BSD `find` on macOS cannot parse `-newermt` with a relative time string and **silently matches nothing instead of erroring** — making rule 2's verification an anti-guard that always returns the reassuring answer. The cited evidence was `-newermt '-30 minutes'` returning **0** files where `-mmin -30` returned **20**.
>
> **I re-derived it before transcribing it, and on this machine it is false.** macOS 26.3.1, `/usr/bin/find`:
>
> | window | `-newermt '-N minutes'` | `-mmin -N` |
> |---|---|---|
> | 30 min | 8 | 8 |
> | 60 min | 29 | 29 |
> | 240 min | 29 | 29 |
> | 1440 min | 29 | 29 |
>
> They agree at every window, and the absolute form (`-newermt "2026-08-13 18:00:00"`) agrees too. **Nor does it fail silently:** a bogus string errors loudly — `find: Can't parse date/time: not-a-time`, **exit 1**.
>
> So **rule 2's command stands as written on this machine.** `-mmin` remains a fine equivalent and is more portable, but a quiet-tree claim resting on `-newermt` is *not* unproven here.
>
> **The reason this note exists at all is the point.** I was handed a specific, plausible, numerically-precise finding and asked to write it in my own voice. Transcribing it would have put a false "verified" claim into the document — under a provenance header that tells every reader to re-derive inherited counts. The instruction to re-derive applies to the scribe too, and to corrections, and to the sentence you are reading. **Anyone on a different macOS or `find` build should re-run the table above rather than trust either version of this note.**

> **Scribe note on this section.** The planner sent two versions of the closing state: an earlier, shorter one at the end of Report 3, and the fuller one above, which it instructed be appended verbatim as the document's final section. The above is used. One clause appears only in the earlier variant and is recorded here so nothing is lost: on T1's recon query, *"The query is in the coordinator's hands."* The query itself is preserved in the appendix below.

---

## Appendix A — how much confidence the P4U numbers carry (recovered, verbatim)

> **Scribe note.** From an earlier `phase2-planner-2` message, not carried into its authored Report 2. Reproduced because every number in the task table is an estimate and this is the planner's own statement of how much weight they bear. Planner's text, not the scribe's.

> On sizing: my ~29 P4U is derived from file-level task decomposition, not measured throughput. It inherits phase 4's 5-items-per-1.5-days as its only calibration point, and phase 4's items were write-path hardening — deeper per item than most of phase 2's ports. **Treat it as an estimate with one data point behind it**, not a measurement.

*(The "~29" is the superseded rounding; the authoritative total is **30.0 P4U**. The confidence statement applies unchanged.)*

---

## Appendix B — the T1 recon query (recovered, verbatim)

> **Scribe note.** This came from an earlier `phase2-planner-2` message and was **not** re-sent in the three-part transmission; the closing state refers to it only as being "in the coordinator's hands". It is reproduced verbatim here because losing it costs the T1 owner a re-derivation, which is exactly what this document exists to prevent. It is the planner's text, not the scribe's.

So whoever owns T1 doesn't re-derive it. Read-only, no writes:

```sql
SELECT d.content_type,
       count(*) FILTER (WHERE d.content LIKE '%<details>%')  AS with_details,
       count(*)                                              AS total
FROM documents d
GROUP BY d.content_type
ORDER BY with_details DESC;
```

`with_details` is the number that decides T18. Rough read: **triple digits → (a) structural re-emission earns its render rule; low double digits or fewer → (c) flat headings** and the collapse affordance isn't worth the XSS surface. **(b) stays off the table unless something forces it** — it rewrites bodies, so it moves `content_hash`, dedup, chunking, embeddings and the edit path's `body_hash` all at once.

Run it against **prod read-only** (`55432`), the same way phases 0 and 1 took their corpus counts. Worth pairing with a second count for phase 1's benefit: documents containing *any* raw HTML block, not just `<details>` — S2 may not be the only construct `html=False` is silently escaping, and nobody has looked.

---

## PII clearance

Recorded from the planner, verbatim:

> **No PII in any part.** Content is file paths, symbol names, line numbers, SQL fragments, point estimates. The only email-shaped strings are **identifier names, not values**: `BRAIN_USER_EMAIL` (env var name), `cfg.user_email` (config field name). I also deliberately never reproduced the `build_related.py` tuning comment the spec flags as a hazard — verified clean, quoted nowhere — so the document carries no corpus lexemes either way.

The scribe independently scanned the assembled document: no email addresses, no phone numbers, no personal names, no attendee lists, no employer or customer references. The only identifying strings are filesystem paths containing the repository owner's own home directory, which appear in the planner's tree-discrimination evidence and are load-bearing to it.

*End of transcription. Nothing below this line was written by `phase2-planner-2`.*

---
---

# Appendix C — mutation-column defects and re-derivation

*Added 2026-08-20 by a docs-only agent, seven days after transcription. **Every claim below was
re-derived against the tree before being written**, never inherited from the report that raised it —
including the claims in the dispatch that commissioned this appendix. Nothing here was carried over
on trust. Scope was `docs/` only: where a defect's fix belongs in code, it is **flagged, not made**,
and said so.*

**Why this appendix exists.** The spec has an Appendix B recording its own defects where a reader
meets them. **This plan had nothing equivalent**, so every defect found in it during phase 2 was
recorded *in the spec's* appendix — a document an implementer working a task row has no reason to
open. Four defective mutations and one undeliverable file list sat here, unmarked, for a week. The
asymmetry was the bug; this closes it. **The plan's rows now carry inline pointers**, so the warning
arrives where the mistake would be made.

---

## The mutation column, and why its defects matter more than ordinary errors

This project's standing rule is that a mutation must redden **the assertion it targets**, not merely
produce a red run. The mutation column exists to enforce that. **Four rows in this document violate
it**, each in a different way:

| Row | Failure mode | Consequence if followed literally |
|---|---|---|
| **T5(b)** | **Inert** — cannot fail | A test recorded as proven that never could fail |
| **T15** | **Reddens a different assertion than it names** | Two absence assertions recorded as proven, having never fired |
| **T17** | **Cannot be performed at all** | Reasons about a defence-in-depth pair that does not exist |
| **T14** | **Deadlocks the harness** | A 13/13 error run that reads as a successful proof on the exit code |

**Each would have shipped looking like coverage.** That is the shared property, and it is why these
are worse than ordinary wrong instructions: a wrong instruction that fails loudly gets fixed by the
next person to run it. These four fail *quietly*, in the direction of appearing to have worked.

**This is not an argument against the mutation column.** In every case the defect was found by
**running the mutation and checking which assertion reddened** — exactly what the column asks for,
and exactly what a reader trusting the column would have skipped. The document that instructs
implementers how to prove a test can fail contained four instructions that could not. That is the
strongest available argument *for* the rule the column exists to enforce.

---

### C-1 · T5(b) — the reorder mutation is inert

**Declared:** *reorder `classifyLink` so `external` precedes `tag` → a `tags/` link misclassifies.*

**Why it cannot fail.** `linkKindMark.ts` classifies tags with `TAG_PREFIXES = ["tags/", "/tags/",
"./tags/"]` under a **prefix** match, and externals with `["http://", "https://", "mailto:"]`. Under
that reading the two sets are **disjoint** — no URL can match both — so precedence is unobservable
and reordering changes no answer.

**The reason it is not simply a typo** is that the file contradicts itself. Its header documents a
tag URL as one that **starts with** `tags/` **or contains** `/tags/`; the code only ever
prefix-matches. Under the *documented* (infix) reading the sets **overlap** —
`https://host/tags/retro` is both — and precedence becomes load-bearing, which is what the mutation
assumes.

**Authoritative reading: the docstring.** The port took the documented semantics
(`src/brain/ui/render.py:74` `_TAG_INFIX = "/tags/"`, used at `:165` as
`lowered.startswith(_TAG_PREFIXES) or _TAG_INFIX in lowered`), which makes the Python side correct
and the overlay the buggy half. **Note the direction of the argument:** the mutation reddens *only*
under the infix reading, so this comment is not commentary on the code — it is a **load-bearing
dependency of the test suite**.

**Substitute mutation:** classify `https://host/tags/retro`, assert `tag`; then remove `_TAG_INFIX`
from the check at `render.py:181` (was `:165`; re-derive rather than inherit) and it reddens.

> ✅ **CLOSED 2026-08-20, both halves — and the substitute is now RUN, not merely prescribed.**
>
> **The code half**, as C-1 required and in the direction it required.** `35fc486` added `TAG_INFIX`
> and `isTagUrl()` to `linkKindMark.ts` and wired `classifyLink` to call it ahead of the `external`
> test — verified on disk, not from the commit message: `linkKindMark.ts:155-158` defines
> `isTagUrl`, `:183` is `if (isTagUrl(url)) return "tag"`, `:184` the `external` test. So the
> overlay was edited up to its comment rather than the comment down to the code, and the precedence
> the mutation assumes is now load-bearing on both sides of the port.
>
> **The mutation half**, which this row had never carried. Run against the current tree:
>
> ```
> render.py:181  return lowered.startswith(_TAG_PREFIXES) or _TAG_INFIX in lowered
>             -> return lowered.startswith(_TAG_PREFIXES)
>
> .venv/bin/python -m pytest tests/test_ui_render_link_kinds.py --no-cov -q
> -> 2 failed, 24 passed        (baseline: 26 passed)
> ```
>
> **Two, where this appendix predicted one**, and the extra one is the more informative:
> `test_classify_link_kind_buckets[/tags/retro-tag]` reddens alongside the substitute test, because
> the port's `_TAG_PREFIXES` deliberately omits the leading-slash `/tags/` that the overlay's
> `TAG_PREFIXES` lists — `_TAG_INFIX` *is* that string and matches it at offset 0. The constant
> therefore carries the leading-slash form as well as the host-qualified one.
>
> Blast radius measured rather than assumed: widened to `test_ui_render.py` and
> `test_ui_render_toc.py` the same mutation reads **2 failed, 88 passed** (baseline: 90 passed) —
> the same two tests and nothing else. Restored byte-identically (`shasum` verified); suite green.
>
> The record itself lives in the test's docstring
> (`tests/test_ui_render_link_kinds.py::test_tag_wins_over_external_for_an_absolute_tag_url`),
> which is where this branch's other mutation records live.

### C-2 · T15 — the summary mutation reddens the wrong assertion

**Declared:** *return `""` for summary → the "no empty aside" assertion fails.*

**Measured.** Returning `""` reddens the **presence** assertions at
`tests/test_ui_browser_lede.py:411-412` and leaves **both** absence assertions **green**:

| Assertion | Line | On `""` |
|---|---|---|
| a note carrying `documents.summary` renders a lede | `:411-412` | **RED** |
| `test_a_note_without_a_summary_renders_no_empty_aside` | `:512` | green |
| `test_a_blank_summary_renders_no_empty_aside` | `:523` | green |

Of course it does: with no summary there is nothing to render, so "renders no empty aside" is
**trivially satisfied**. There are **two** absence assertions, not the one the row implies, and the
mutation exercises neither.

**The failure is quiet in the worst way.** An implementer following the row sees red, concludes the
named assertion is proven, and moves on — having proven the opposite assertion and left both of the
intended ones unfired.

**Substitute mutation:** drop the emptiness guard at
`src/brain/ui/static/js/inspector.js:242` — `if (summary) head.appendChild(el("aside", "lede",
summary))`. Without it an empty `<aside class="lede">` is appended on every path, and both `:512`
and `:523` redden.

> **RE-RUN 2026-08-20 — confirmed, with one number this entry understates.** On
> `pytest tests/test_ui_browser_lede.py -m browser --no-cov` (baseline **13 passed**): the declared
> mutation (force `summary` empty, `inspector.js:241`) gives **3 failed / 10 passed** — `:411`
> presence, plus `:422` lede-above-body and `:469` lede-parented-to-`.note-head`, which this entry
> does not mention — while `:512` and `:523` stay green. The substitute (drop the guard, `:242`)
> gives **2 failed / 11 passed**: `:512` and `:523` alone, presence green. **The two redden
> disjoint sets**, which is what makes the substitute the correct mutation rather than merely a
> different one. Worth stating because "reddens the presence assertions" invites an implementer to
> expect one red test and stop counting at three.

### C-3 · T14 — the synchronous-fetch mutation deadlocks the harness

**Declared:** *make the fetch synchronous/blocking → the note body assertion times out → fails.*

**Diagnosed, not guessed** — the mutant was confirmed syntactically valid with `node --check`
*before* this conclusion was drawn, so "it just didn't run" is excluded.

Run literally, a synchronous `XMLHttpRequest` produces **13 errors in 116 s** — every test in the
file erroring at fixture setup, **including tests that never touch backlinks**. The cause is
structural, not incidental: the sync XHR blocks the page's main thread inside a `page.evaluate` that
Playwright is awaiting, while the response can only be produced by a route handler in the driver
that is itself blocked awaiting that evaluate. **Deadlock.** The declared mutation is unusable in
any Playwright + route-stub harness, not merely in this one.

**And a 13/13 error run reads as a successful proof to anyone reading only the exit code** — the
run is red, the row said it would be red, and nothing in the exit status distinguishes "the
assertion I targeted failed" from "the harness never got far enough to evaluate it."

**Substitute mutation:** route the request through an **async** XHR. It never touches the page's
patched `window.fetch` and so sails past the test's gate, isolating the same property at
**1 failed / 12 passed**.

> ⚠ **RE-RUN 2026-08-20 — THIS ENTRY DOES NOT REPRODUCE AGAINST THE CURRENT TREE. KEPT, NOT
> DELETED.** The declared *synchronous* mutation was performed again exactly as C-3 describes —
> `attachBacklinks`'s `api(...).then(...)` replaced with `open(..., false)` + `send()` and the rail
> appended inline, `node --check`ed as `.mjs` first, restored byte-exact afterwards — and it did
> **not** deadlock:
>
> ```
> .venv/bin/python -m pytest tests/test_ui_browser_reading.py -m browser --no-cov
> -> 1 failed, 16 passed in 8.86s      (baseline: 17 passed)
> ```
>
> One test red — `test_the_note_is_readable_before_the_backlinks_arrive` — on the assertion the row
> targets: *"the backlinks rail rendered before its response was released, so the fetch is not
> lazy"*. That is the declared mutation working as declared.
>
> **Why the deadlock is not reachable here.** C-3's mechanism is exact and requires the sync XHR to
> execute inside a `page.evaluate` Playwright is awaiting. In the current code `attachBacklinks` is
> reached from `renderMarginalia`, i.e. from an ordinary subscriber render, and the harness's
> `_gate_links` holds the response **inside the page** rather than in a route handler — so there is
> no driver-side handler for the blocked main thread to be waiting on. The stated file total also
> no longer matches: C-3 counts **13** tests in that file, which now holds **17**.
>
> **Not deleted, deliberately.** The reasoning is sound about the configuration it describes and a
> future harness that stubs `/links` in a route handler, or a caller that reaches
> `attachBacklinks` from inside an awaited `page.evaluate`, brings it straight back. What is
> corrected is only the claim that it reproduces *today*. The **async substitute remains the
> better mutation regardless**, because it is the one that isolates laziness rather than
> incidentally defeating the gate.

### C-4 · T17 — the draft-guard mutation is unimplementable

**Declared:** *drop the client-side draft guard **and** T4's SQL guard together — one alone leaves
the other covering it.*

**There is no client-side draft guard, and there cannot be one:** the discovery payload carries no
`draft` field, so the client has nothing to distinguish a draft from anything else. The
defence-in-depth pair the instruction reasons about **never existed**, which means the row's premise
— that either guard alone masks the other — is false in the first place.

**Use the row's own fallback:** assert against the route payload directly. T10 already delivered
that, so this costs nothing.

### C-5 · T7 — the file list omits the one file that connects the feature

**Declared files:** `search_predicate.py`, `ui/schemas.py`, `ui/routes_meta.py`.

`search.hybrid_search` calls `build_predicate` with **explicitly named keyword arguments** and does
**not** splat `**kwargs`. So a filter added only to `build_predicate` is **unreachable from the UI**
— any value `schemas.py` put in `filter_kwargs()` would raise `TypeError`. The connecting file,
`src/brain/search.py`, is absent from the row, which makes the feature **undeliverable exactly as
specified**.

**It was in fact delivered by touching `search.py`** — a two-line additive diff at **`:328`**
(the parameter) and **`:456`** (forwarded into `build_predicate`). Re-derived counts of
`source_missing` today: `search_predicate.py` **5**, `search.py` **2**, `ui/schemas.py` **11**.

**Rejected at the time, and worth keeping rejected:** shipping the predicate half alone, which would
have delivered a kwarg that exists, type-checks, is tested at one layer, and does nothing.

**This row has a consumer the plan never names.** The `source_missing` kwarg is what makes the facet
panel's `none` bucket clickable. `brain.facets` previously grouped source-less documents under
`coalesce(s.kind, 'manual')`, filing them under a real source kind; it is now
`SOURCE_NONE_BUCKET = "none"` (`src/brain/facets.py:38`). **Measured read-only against the corpus:
877 of 1,393 documents have `source_id IS NULL` — 63.0%.** So `manual` was not slightly inflated;
the bucket was mostly not-manual. T7 and that fix are one feature seen from its two ends, and the
panel could not have been made honest without the `search.py` line this row omitted.

### C-6 · T3 — the `compute_related` signature, and the decision hiding inside it

**Declared:** `compute_related(conn, doc_id, limit)`. **Actual:**
`compute_related(conn, doc_id, *, limit=DEFAULT_RELATED_LIMIT, vector_sim_floor)`
(`src/brain/related.py:114`).

The plan's form is **closer to correct than the spec's**, which omits `conn` as well — worth stating
so the two documents' errors are not conflated. What the plan omits is `vector_sim_floor`, and it is
**required with no default, deliberately**: the cosine floor is shared with runtime `brain search`,
and `regenerate_related_json`'s own docstring already refuses a default for the same parameter on
the grounds that it "must not silently diverge".

**So the omission is not an ergonomics detail.** The open question it conceals is: *may the
related-docs panel diverge from `brain search` without anyone noticing?* The failure mode is
invisible — a wrong cosine floor still returns plausible documents in a plausible order. **That
decision is the user's**, and it is why the related-docs HTTP endpoint still does not exist.

### C-7 · T2's standing rule — the mechanism inverted, the conclusion survived

Covered inline at the rule itself. In short: **the glob is gone.** `ci.yml` now names every
browser module as an explicit path in byte order because a shell glob sorts by
ambient locale, expanding `test_ui_browser.py` last under macOS and first under a POSIX-locale
runner. A conforming **filename** therefore no longer buys inclusion — **a new module must be added
to the list.** The marker-based guard at `tests/test_ci_workflow.py:545` is unchanged and still
turns RED on omission, so the rule's conclusion holds and only its mechanism is wrong.

**Recorded rather than just corrected**, because the rule as written is the dangerous kind: an
implementer follows it, names the file correctly, believes it covered, and is surprised by a red CI
they were told the naming convention prevented.

---

## Re-derivation of the plan's other load-bearing claims

| Claim | Status 2026-08-20 | Evidence |
|---|---|---|
| **"THIS FILE IS NOT SAFE YET — `docs/plans/` is gitignored"** | **STILL TRUE, unchanged.** `docs/specs/` is now tracked; `docs/plans/` and `docs/audits/` are not. The warning at the head of this document is a week old and has not been acted on. | `git check-ignore -v` → `.gitignore:57` (`docs/plans/`), `:58` (`docs/audits/`); `docs/specs` → not ignored |
| **The half-staged migration — "highest-severity item in this report"** | **STILL LIVE, unchanged, seven days on.** The four-way split is exactly as described. **A commit taken now still captures the two renames and leaves the `build_related` half on the floor** — a self-concealing breakage where `import brain.cli` passes locally and fails for everyone else. | `related.py` `??`; `wiki/build_related.py` ` M`; `connect.py` ` M`; `people.py` / `person_name.py` staged renames |
| **T3 "BLOCKED ON ATTRIBUTION"** | **RESOLVED — unblock it.** `compute_related` exists at `related.py:114` with **10 tests** in `tests/test_related_compute.py`. The file is quiet and no longer in flight. | `related.py` outgrew the 613 lines this plan records when `compute_related` was added — re-derive with `wc -l src/brain/related.py`, and do not inherit the figure that stood here (~~714~~, already stale at HEAD: `0473b5f` took it past that) |
| **The A12 caveat: "a green import at one instant… re-run after the write freeze"** | **RE-RUN, and the answer changed.** `import brain.cli` now loads **5** wiki/quartz modules, not the spec's 8. The three that dropped out are exactly the three that moved. **The blast radius shrank because the work was done, not because the earlier probe was sloppy.** | `sys.modules` diff, Python 3.11.15, worktree `.venv`: `vault.quartz_overlay`, `wiki`, `wiki.build_swap`, `wiki.errors`, `wiki.install` |
| **S1** (defect table: "recorded, UNRESOLVED in the spec") | **NOW RULED.** The scoring landed in phase 2; the **panel is deliberately deferred to phase 5**. The spec's §11 phase-2 row is struck in place and its phase-5 row now owns the panel. | spec Appendix B-1 and the 2026-08-20 re-derivation there |
| **S8** (§9.2c stale) | **CONFIRMED AND WIDENED.** The section's own grep now returns **6** statements against its **12** rows, and a **third** site declares it authoritative. Sharpest edge: `cli.py:234` no longer imports `wiki.build_people`, it imports `vault.quartz_overlay` — **the line survived and changed meaning**, so a line-number audit lands on a real, plausible import and edits the wrong statement. | spec §9.2c now carries a re-derived list |
| **S9** (phase-6 row describes done work) | **CONFIRMED, unchanged.** All three modules exist at their new paths. | `src/brain/{people,person_name,related}.py` |

**Line references in this document have drifted and should not be visited by number.** Spot-checked:
`connect.py:31` → **`:29`**; `queries.list_existing_tags:517-522` → **`:502`**; the spec is no longer
the "1,968 lines" the header records. The transcription note already warned that every `file:line`
here is the planner's, **inherited unchecked** — that warning is still the right one, and C-7 and the
`cli.py:234` finding are why: a line that moved is a nuisance, but **a line that survived and changed
meaning passes inspection.**

## PII clearance for this appendix

No personal names, email addresses, attendee lists, employer or customer references, corpus document
titles, or internal codenames. The corpus measurement in C-5 is two aggregate integers from a
read-only `SELECT count(*)`; no document was named, quoted, or identified. No redaction elsewhere in
this document was altered.

*End of Appendix C.*

