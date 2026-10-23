# Roadmap completion — every open item, designed and ticketed

**Date:** 2026-10-23
**Status:** Design. **Not approved for implementation**: CLAUDE.md rule 7 requires explicit approval.
Once approved, the tickets below are executed one at a time, in the order §4 sets, each as its own PR.
**Measured at:** `second-brain/master` `02c2abc` (PR #61 merged). Every symbol and path below was
read at that commit. Line numbers drift; where a symbol is named, trust the symbol over the number.
**Backlog this closes:** [`docs/plans/2026-10-23-backlog-reconciliation.md`](../plans/2026-10-23-backlog-reconciliation.md)
(cited below as "the backlog"). Its §1 statuses and §3 order are the input; this document adds the
designs, the tickets and the tracker.
**Designs referenced, not repeated:**
[`2026-10-23-brain-ui-ingest-tab-design.md`](./2026-10-23-brain-ui-ingest-tab-design.md) ("the
Ingest spec"), [`2026-07-25-brain-ui-design.md`](./2026-07-25-brain-ui-design.md) ("F14"),
[`2026-08-10-wiki-to-ui-consolidation-design.md`](./2026-08-10-wiki-to-ui-consolidation-design.md)
("the consolidation spec"), [`2026-07-25-sections/R1-DEFERRED-mcp-http-transport.md`](./2026-07-25-sections/R1-DEFERRED-mcp-http-transport.md)
("R1"), and [`docs/plans/2026-10-23-search-fts-fallback.md`](../plans/2026-10-23-search-fts-fallback.md)
("the fallback plan").

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

> **No PII.** The May 2026 roadmap files this document restates exist only untracked in the main
> checkout, and they quote real names in their examples. Nothing here copies a name, an example
> query or a document title from them. Every item is restated generically, by its roadmap number.

---

## 0. How to read this, and the decisions that cut across tickets

- **§1** lists every open item and the ticket that owns it. Nothing open is left without a ticket.
  Items already shipped are in Appendix A, with evidence, and get no ticket.
- **§2** is the Definition of Done and the global constraints. It binds every ticket, so the
  tickets do not restate it.
- **§3** is the tickets, grouped by wave. Ids are `RM-01`…`RM-61`, and sub-tickets add a letter
  (`RM-14a`). Each ticket is one PR unless it says otherwise. Decision ids are scoped to their
  ticket (`RM-14.D2`), so they cannot collide with the Ingest spec's `D1`–`D10` or the
  consolidation spec's `Q1`–`Q19`.
- **§4** is the execution plan: waves, what may run in parallel, and the migration numbers.
- **§5** is the short list of tickets that need the user: four, each either changing a standing
  user decision or answering a question the user's own sources left open. Everything else carries a
  recommended default and needs no question.
- **Appendix B** is the tracker. Its status column is the only part of this document a ticket PR
  edits after approval.

### 0.1 Cross-cutting decisions

| # | Question | Decision | Section |
|---|---|---|---|
| G1 | How do new CLI commands and MCP tools avoid growing `cli.py` (8,819 lines) and `mcp_server.py` (5,078)? | A **registration seam** lands first (RM-06). After it, a new command module touches `cli_registry.py` only, and a new MCP tool lives in `src/brain/mcp_tools/` and is registered without editing `mcp_server.py`. The existing files are **not split** (that is RM-48, which needs the user). | §2.2, RM-06 |
| G2 | How do new ingest extractors avoid growing `ingest/__init__.py` (2,369)? | The extension dispatcher `_EXTRACTORS` / `extract_path` moves to a new leaf, `ingest/dispatch.py`, with a registration function (RM-07). `ingest/__init__.py` re-exports it and shrinks. | RM-07 |
| G3 | How do new knobs avoid growing `config.py` (2,669)? | New knob groups live in **feature settings leaves** under `src/brain/settings/`, read from `os.environ` **after** `Config.load()` has published the dotenv chain into it (`config.py`, the `os.environ.setdefault` loop in `_load_field_dict`). `config.py` is not grown for any ticket here (RM-08). | §2.3, RM-08 |
| G4 | How do ranking stages (fallback, reranker, document leg, engagement) compose without each surface wiring them separately? | One composition function, `brain.search_pipeline.run_search` (RM-09), called by every user-facing surface. No stage edits the ranking code in `search.py` (823, over the ceiling); RM-20 adds only a two-line public alias there. `brain eval` calls the stages it measures directly, and never the fallback. | §2.7, RM-09 |
| G5 | How is a new source added? | A fixed nine-point protocol (§2.4): extractor or stdin recipe, dispatcher or source-kind entry, credentials, a **mandatory scope flag**, PII rules, synthetic fixtures, dependency evidence, and the graph and enrichment cost. Prefer the **agent-orchestrated MCP → `brain ingest-stdin`** pattern (as Krisp and Slack do) over a new API client. | §2.4 |
| G6 | Where do long-running UI tasks run? | On the Ingest spec's `JobRegistry` (Ingest spec §4), reused by the Agent tab. One worker for every job kind. | RM-10, RM-18 |
| G7 | Reranker runtime | Ollama has **no rerank endpoint** (upstream issue ollama/ollama#3368 is still open, and #16076, opened in 2026, says there is no `/api/rerank`). The local backend is **`llama-server` from llama.cpp, started with `--embedding --pooling rank --reranking`**, serving `bge-reranker-v2-m3` (an external binary over HTTP, no new Python dependency). Voyage is the hosted fallback, through the already-declared `voyageai`, using **the model Voyage currently recommends, re-checked at build time**: on 2026-10-23 Voyage's reranker docs (docs.voyageai.com/docs/reranker) recommend `rerank-3`, with `rerank-3-lite` for latency-sensitive use; both were announced on Voyage's blog on 2026-09-30 as drop-in upgrades of `rerank-2.5`. **Default OFF** until `brain eval` proves a gain. | RM-14 |
| G8 | Brief delivery channel | **Email to the owner's own address through the existing `gws` CLI**, opt-in, default off. Slack is supported through an incoming webhook to a private channel. The `#team` channel is **rejected**: it is shared, and a personal brief is not a team post. | RM-16 |
| G9 | Saved-search notification | Through the **daily brief**: a "Watch hits" section, delivered by whatever G8 channel is enabled. There is no per-ingest push. | RM-15 |
| G10 | Agent tab transport | **Polling** the job registry, as the Ingest tab does. **No NDJSON stream in v1**: `ask`'s LLM steps are non-streaming JSON calls (`chat.chat_json`), so a stream would carry the same five to eight step events polling already delivers. | RM-18 |

---

## 1. Scope and inventory

### 1.1 What was read

- The backlog (§1, §2, §3). It was added by PR #60 (commits `a3de604` to `d1388cf`); its statuses
  were measured at `0bf3e36`. Every status used here was re-checked against `02c2abc`. The one
  change since is PR #61: H-7 is now **shipped** (Appendix A), and this PR flips the backlog's
  H-7 rows to match.
- The Ingest spec (all of it), F14 §1.1, the consolidation spec §8.6 and §12, R1, the fallback plan,
  the MCP server spec's "Out of scope" list (`docs/specs/2026-04-27-mcp-server-design.md`), and the
  parent release spec's F13 deferral (`docs/specs/2026-07-25-agent-memory-safety-ui-design.md` §2).
- The May roadmap and its execution tracker, for each item's intent and the owner's 2026-05-11
  resolved decisions: enrichment on local Ollama; browser history **accepted**, allowlist-only;
  issue trackers **Notion and Jira**, not Linear; **all of 4.1–4.5**; "perfect for me", not
  open-source-generic; reranker local-first with a Voyage fallback, decided at build time.
- `CLAUDE.md`, whole, and the code each ticket touches (cited per ticket).

### 1.2 The inventory: every open item, and its ticket

Status is the backlog's, re-checked at `02c2abc`. "Rank" is the backlog §3 row.

| Item | Source | Status | Rank | Ticket |
|---|---|---|---|---|
| `brain recall` has no `EmbedError` handling | BL-14 (fallback plan F2) | NOT STARTED | 1a | RM-01 |
| `brain ask` leaks `EmbedError` | BL-15 (fallback plan F1) | NOT STARTED | 1a | RM-02 |
| `brain ui` search has no automatic fallback | BL-13 (fallback plan F3) | NOT STARTED | 1a | RM-03 |
| CI check for the ceiling table | BL-1 | NOT STARTED | 2 | RM-04 |
| CSS `url()` unchecked by the offline test | BL-3 | NOT STARTED | 3 | RM-05 |
| `cli.py` / `mcp_server.py` growth (smallest scope: seams) | BL-2 | Deferred | — | RM-06 |
| Extractor dispatcher inside an over-ceiling file | G2 (new, enabling 2.x) | — | — | RM-07 |
| Stale `ingest_document` docstring | BL-12 | NOT STARTED | 16 | RM-07 |
| Knob growth in `config.py` | G3 (new, enabling) | — | — | RM-08 |
| Ranking stages wired per surface | G4 (new, enabling 5 / 3.6 / 1.3) | — | — | RM-09 |
| Ingest tab, phase 1 (jobs + paste backend) | Ingest spec §10 | NOT STARTED | 4 | RM-10 |
| Ingest tab, phase 2 (paste front end) | Ingest spec §10 | NOT STARTED | 4 | RM-11 |
| MCP `brain_related` | 6.1, BL-6 | PARTIAL | 5 | RM-12 |
| MCP `brain_explain` | Q1-B D13, BL-6 | NOT STARTED | 5 | RM-12 |
| Ingest tab, phase 3 (file upload) | Ingest spec §10 | NOT STARTED | 6 | RM-13 |
| Cross-encoder reranker | 5, BL-5, MCP spec "Out of scope" | NOT STARTED | 7 | RM-14a, RM-14b, RM-14c |
| Saved searches / watch lists | 4.4, BL-9 (F13) | NOT STARTED | 8 | RM-15 |
| Brief delivery (Slack / email) | 4.1 | PARTIAL | 10 | RM-16 |
| Brief "new entities this week" | 4.1 | PARTIAL | 10 | RM-17 |
| Agent tab (v3) | F14 §1.1, backlog §2.1 | NOT STARTED | 9 | RM-18a, RM-18b, RM-18c |
| Near-duplicate detection | 3.4 | NOT STARTED | 11 | RM-19 |
| Document-level embeddings | 3.6 | NOT STARTED | 12 | RM-20 |
| Tag merge / rename / suggest-merges | 7.4 | PARTIAL | 13 | RM-21 |
| Interactions-aware ranking | 1.3 phase 2, Q1-C non-goal | NOT STARTED | 14 | RM-22a, RM-22b |
| Copy-path / external-editor affordance | BL-4, consolidation Q4 | NOT STARTED | 15 | RM-23 |
| `--entity` search filter | 3.3 (missing leg) | PARTIAL | 17 | RM-24 (**NEEDS USER CONFIRMATION**) |
| Move-identity residue | H-1 | Open | 16 | RM-25 |
| Late events after watcher close; delete not redone without prune | H-2, H-4 | Open | 16 | RM-26 |
| Graph-lock residuals 2–4 | H-3 | Open | 16 | RM-27 |
| Log-privacy guard blind spots | H-5 | Open | 16 | RM-28 |
| Terminal error reports carrying exception text | H-6 | Open | 16 | RM-29 |
| Calendar events as documents | 2.3 | NOT STARTED | 18 | RM-30 |
| Code-host activity (PRs, issues, READMEs) | 2.1 | NOT STARTED | 18 | RM-31 |
| PDF image OCR | 3.5 / 5.3 | NOT STARTED | 18 | RM-32 |
| Screenshot watch folder + OCR | 5.1 | NOT STARTED | 18 | RM-33 |
| Jira extractor | 2.8 | NOT STARTED | 18 | RM-34 |
| Notion extractor | 2.8 | NOT STARTED | 18 | RM-35 |
| Apple Notes | 2.4 | PARTIAL | 18 | RM-36 |
| External markdown vault, with deletions | 2.4 | PARTIAL | 18 | RM-37 |
| Voice memos (local transcription) | 5.2, 2.9 | NOT STARTED | 18 | RM-38 |
| Mobile capture share-sheet | 4.5 | PARTIAL | — | RM-39 |
| Ingest tab, phase 4 (inbox folder) | Ingest spec §10, §5.5 | NOT STARTED (optional) | — | RM-40 |
| Browser history + bookmarks (allowlist) | 2.5 | NOT STARTED | 19 | RM-41 |
| Code-aware chunker | 7.3 | NOT STARTED | 19 | RM-42 |
| Code-repo ingestion | 2.6 | NOT STARTED | 19 | RM-43 |
| Social bookmarks | 2.9 | NOT STARTED | 19 | RM-44 |
| Receipts | 2.9 | NOT STARTED | 19 | RM-45 |
| MCP HTTP transport | BL-10, MCP spec "Out of scope" | Deferred | 20 | RM-46 |
| Multi-host sync | 6.4 | NOT STARTED | 20 | RM-47 |
| `cli.py` / `mcp_server.py` full split | BL-2 | Deferred | leave | RM-48 (**NEEDS USER CONFIRMATION**) |
| Hermetic CI eval gate (and self-hosted runner) | CLAUDE.md "Eval gate", decision 2026-10-12 | Closed by user decision | — | RM-49 (**NEEDS USER CONFIRMATION**) |
| Self-hosted fonts | BL-7, consolidation Q14 | Unresolved | leave | RM-50 |
| Retire the tab strip | BL-8, consolidation Q17 | Superseded in part | leave | RM-51 (**NEEDS USER CONFIRMATION**) |
| Interactive picker for `brain search` | 7.1 | NOT STARTED | leave | RM-52 |
| `brain examples` | 7.2 | NOT STARTED | leave | RM-53 |
| Opt-in query rewriting | 7.5 | NOT STARTED | leave | RM-54 |
| `brain_summarize_tag` | BL-11 (MCP spec "Out of scope") | NOT STARTED | — | RM-55 |
| MCP twin of `brain eval` | Q1-B "MCP" section | Conditional | — | RM-56 |
| `--tier vault\|ingested` filter | Q1-C D11 | Conditional | — | RM-57 |
| Pruning the append-only `interactions` table | Q1-C non-goals | Conditional | — | RM-58 |
| Cap on the auto-tag vocabulary prompt | Q1-D D18 | Conditional | — | RM-59 |
| `documents.parent_document_id` | Q1-D D17 | Conditional | — | RM-60 |
| Background enrichment daemon | Q1-D non-goal | NOT STARTED, not needed | — | RM-61 |

### 1.3 Open things that are deliberately not ticketed

- **Machine operations** from the merge-all handoff's "Remains the user's" list: stop the root Caddy,
  delete `<vault>/.quartz`, decide the three unmerged July branches, keep `daemon-src`. They are not
  code. The July branches are safe to close now that H-7 has merged (backlog §2.2).
- **Superseded items**: the wiki click endpoint (Q1-C D1), the Q1 manual-QA page, the Publish tab,
  the Quartz summary lede, consolidation Q6 (graph workbench), and the MCP spec's "per-document
  permission scoping" (met by F6 sensitivity, migration 026, for the single-user case this product
  is). Appendix A lists each with its evidence.
- **By-design non-items** from the handoff: `graph_runner` has no `degraded` tolerance, and the graph
  lags while a startup backlog drains.

---

## 2. Global constraints and Definition of Done

This section restates no rule from `CLAUDE.md`; it says how each one applies to this program, and
adds the program-level policies (§2.2–§2.8) that the tickets rely on.

### 2.1 Definition of Done (every ticket)

A ticket is done when **all** of the following hold. A reviewer checks each item by name.

1. **Plan first.** Before code, the implementer writes `docs/plans/<date>-<ticket-slug>.md` from
   the ticket, naming the files, tests and risks (CLAUDE.md rules 7 and 11b), and gets it approved.
   The ticket is the spec; the plan is the task list.
2. **TDD, RED first.** Each behaviour lands as a failing test first. Every bug fix carries a
   regression test that fails without the fix (rule 12). Guards are mutation-proven: the check is
   shown to fail when the guarded line is removed, in a `git archive` copy, never in a live
   worktree (memory note "No mutations during a run of record").
3. **Tests run against the test Postgres on port 5434 only.** No test, script or manual check in a
   ticket connects to the production database on port 55432, or reads or writes `~/brain-vault` or
   `data/postgres/`. Fixtures use a `tmp_path` vault and the `brain demo` sandbox (port 55433) for
   any screenshot.
4. **Quality gates.** `ruff check`, `mypy src/` and `pytest` are green; coverage stays ≥ 85% overall
   and meets the per-module floors (pure logic 95%, ingest 90%, CLI 85%). New UI suites are listed
   explicitly in `.github/workflows/ci.yml`'s browser roster (`tests/test_ci_workflow.py` turns red
   otherwise).
5. **No monkey-patching** (rule 13). Seams are constructor or keyword arguments. `monkeypatch`,
   `mock.patch` and `mocker.patch` are allowed.
6. **Class-only failure text.** Every log line and every user-visible error for a caught exception
   uses `brain.errors.describe_failure` (the class name), never the exception message.
   `tests/test_log_privacy.py` (G1) must stay green without a new allowlist entry, unless the ticket
   argues for one.
7. **Parameterized SQL, explicit timeouts.** Every new HTTP call (httpx), subprocess (`gws`, `gh`,
   `git`, `tesseract`, `whisper-cli`, `afconvert`, `osascript`, `llama-server` probes) and DB connection has an explicit
   timeout. Every external binary is found with `shutil.which` and is a soft dependency: absent, the
   command exits 1 with an install hint, and `brain doctor` reports it.
8. **Migrations** are new numbered raw-SQL files, additive and idempotent, at the number §2.8
   allocates. None edits an applied migration.
9. **Ceiling.** No new module crosses 800 lines. A ticket that touches a file in the CLAUDE.md
   ceiling table follows the promotion rule (promote the row's descriptive last hop to a SHA, then
   add its own descriptive hop), and if it grows the file it records the reason in both places
   (inline, and a module-docstring pointer). §2.2 lists the files each ticket is expected to touch.
10. **Review + audit loop to zero.** `superpowers:code-reviewer` (opus) and a separate completion
    auditor (opus) both run, and the loop repeats until "APPROVED" and "AUDIT PASSED" (rule 14).
    Reviewer and auditor briefs carry the no-destructive-DB rule and forbid connecting to port 55432
    at all. Tickets marked **security review** add `ecc:security-reviewer` on opus.
11. **Green CI, then merge.** The PR's `ci` workflow (lint, tests, browser harness) is green on the
    PR head. Commits and pushes need the user's permission (rule 4); merges follow the user's
    standing instruction for this program.
12. **Close the backlog row in the same PR.** The ticket's row in the backlog (§1 or §2) is updated
    to SHIPPED with the PR number, and its row in Appendix B here moves to DONE.
13. **Docs.** `docs/cli-reference.md`, `docs/configuration.md` (for every new knob or MCP tool),
    `CHANGELOG.md` (Unreleased), and the relevant skill under `skills/` are updated in the same PR.
14. **Memory files** (rule 11): `cli.md` for commands and MCP tools, `schema.md` for migrations,
    `types.md` for public dataclasses and Protocols, `extractors.md` for extractors, `search.md` for
    ranking changes, and `MEMORY.md`'s index line when the hook changes.
15. **Daemon reinstall when `src/` changes.** After merge, the non-editable launchd install is
    reinstalled from the merged `master` (memory note "Daemon install": delete `daemon-src` `build/`
    and egg-info first, diff `RECORD` against git). **Skills sync when `skills/` changes**:
    `bin/brain-skills-sync`.
16. **No PII** anywhere in code, tests, fixtures, docs, commit messages or the PR body (rule 15).
    Fixtures for new sources are synthetic, built in the test from named constants, never pasted
    from a real export.
17. **End-to-end.** The implementer exercises the feature through the real CLI or browser against
    the test database or the demo sandbox, and pastes the transcript in the PR (CLAUDE.md "Lessons
    Learned" 2).

### 2.2 The ceiling, applied to this program

The over-800 set at `02c2abc` is the sixteen files in the CLAUDE.md table, and the table matches it
(re-derived with `find src -name '*.py' -exec wc -l {} + | sort -rn`). This program touches six of
them, plus `setup.py` if RM-14c adopts the reranker. The seams in Wave 2 exist so that each is touched rarely, and so that most touches are net
zero or net negative.

| Over-ceiling file | Tickets that must touch it | How growth is avoided |
|---|---|---|
| `cli.py` | RM-02 (net negative, −3; no growth record), RM-06 (one call), RM-15 (net negative: the two-audience body of `brief` moves to `brief_publish.py`), RM-20 (net zero: `init`'s chunks-only `ensure_embedding_column` call becomes `ensure_relational_embedding_columns`; the AGE-gated graph call stays), RM-21 (net negative), RM-48 (only if confirmed) | RM-06's seam means later command modules register through `cli_registry.py` only. RM-14a, RM-20 and RM-22b do not touch `eval_cmd`, because `run_eval` builds its own stages (RM-14a). |
| `mcp_server.py` | RM-01 (net negative), RM-02 (net negative, −6; no growth record), RM-06 (about +12 lines: the `ToolContext` and the registration call; recorded), RM-09 (net zero), RM-24 (one parameter), RM-46 (net negative: R1's state builders go to `mcp_state.py`), RM-57 (only if triggered) | New tools go to `src/brain/mcp_tools/` (RM-06). RM-24's and RM-57's parameters on `brain_search` cannot avoid the file, because that tool lives there; each is recorded. |
| `ingest/__init__.py` | RM-07 (net negative), RM-42 (net zero: two calls re-pointed) | The dispatcher moves to `ingest/dispatch.py`; every later extractor registers there. RM-42's chunker choice lives in `ingest/chunk_dispatch.py`. |
| `config.py` | none | Feature settings leaves (RM-08). New fixed-ceiling chat calls (RM-54, RM-55) add `CallBudget` entries in the `ollama_budget.py` leaf, which `config.py` already consults; no knob is added. |
| `search.py` | RM-20 (a two-line public alias for the query-embed cache), RM-24 (two keyword arguments forwarded to the predicate, about 6 lines), RM-57 (only if triggered) | Every ranking stage composes in `search_pipeline.py` (RM-09); no stage edits the ranking code. |
| `cli_ingest.py` | RM-07 (re-pointed import, net zero), RM-37 (`--sync` options, recorded), RM-45 (`--with-attachments`, recorded) | RM-37's prune logic is in `ingest/import_sync.py`; RM-45's attachment logic is in `ingest/gmail_attachments.py`. |
| `vault/watch.py` | RM-26 (wiring, about 15 lines) | The logic goes to a new leaf, `vault/owed_work.py`; `watch.py` gets wiring only, recorded. RM-25 changes only the `vault/move_identity.py` leaf. |
| `setup.py` | RM-14c (only if the reranker is adopted) | One offer line, recorded. RM-33 does not touch `_SHIM_NAMES` (the brief's wrapper is not in it either; `bin/launchd.py` owns the wrapper tuple). |
| `graph_rag/extract.py`, `vault/sync.py`, `vault/graph.py`, `queries.py`, `connect.py`, `people.py`, `timeline.py`, `enrichment.py` | none planned | — |

**Near-ceiling files with a budget.** These are under 800 today and several tickets add to them.
The budget is a cap the reviewer checks; a ticket that would exceed it extracts first.

| File | Lines at `02c2abc` | Budget | Spent by |
|---|---|---|---|
| `cli_search.py` | 728 | +60 in total | RM-12 (net negative: `explanation_json` moves out), RM-14a (`--rerank/--no-rerank`, ≤ 12), RM-24 (`--entity`, ≤ 15), RM-52 (`--pick`, ≤ 12), RM-54 (`--rewrite`, ≤ 12). Logic in leaves (`search_pick.py`, `query_rewrite.py`). If any ticket would push it past 790, that ticket first moves `explain` into `cli_explain.py`. |
| `ui/render.py` | 799 | 0 | No ticket here renders new markdown server-side. RM-18b renders model text client-side with `textContent`. |
| `vault/export.py` | 764 | +1 | RM-29 (the `describe_failure` import; the line it changes is rewritten in place). |
| `backup/restore.py` | 755 | +15 | RM-20 (the reconcile call, net zero), RM-47 (writes `restored-from.json`, ≤ 15). |
| `ui/notes_service.py` | 711 | 0 | Ingest lives in `ui/ingest_service.py` (Ingest spec §1.3). |
| `ui/queries.py` | 724 | +50 | RM-15 (watch-hit titles, if the UI shows them), RM-19 (duplicate pairs). New SQL beyond that goes to a new `ui/queries_*.py` leaf. |
| `ask.py` | 654 | +120 | RM-02 (≤ 30), RM-09 (net zero), RM-18a (`on_step`, ≤ 25), RM-18c (`history`, ≤ 50). |
| `graph_rag/reconcile.py` | 757 | +15 | RM-30 (the concept-skip gate and its watermark write). Anything more extracts first. |
| `doctor_runtime.py` | 563 | +60 | RM-14b, RM-16, RM-31, RM-32, RM-33, RM-36, RM-38, RM-43, RM-47, RM-52 (one probe each). The ticket that would pass +60 first moves the source probes into `doctor_sources.py`. |

**BL-1 (RM-04) turns this from a review habit into a CI check** for the over-800 set. The near-ceiling
budgets stay a review checkpoint.

### 2.3 Config growth policy: feature settings leaves (G3)

`config.py` is over the ceiling, and every knob there costs a two-place growth record. So knobs
introduced by this program live in `src/brain/settings/<feature>.py`, created by RM-08:

```python
# src/brain/settings/_env.py  (RM-08)
def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool: ...
def env_choice(env: Mapping[str, str], name: str, choices: frozenset[str], default: str) -> str: ...
def env_int(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int: ...
def env_float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float: ...
# each raises brain.config.ConfigError (a RuntimeError subclass, config.py) naming the variable,
# never echoing a secret value
```

Each feature has a frozen dataclass and a loader, `load_<feature>_settings(env: Mapping[str, str])
-> <Feature>Settings`. Callers pass `os.environ` **after** `Config.load()`, which has already
published the dotenv chain into the process environment (`Config._load_field_dict`, the
`os.environ.setdefault` loop). Tests pass a plain dict, so no test touches the real environment.
**One carve-out:** `eval/runner.run_eval(stages=None, env=None)` falls back to `os.environ` when
neither is given (RM-14a), so `eval_cmd` needs no change. Every test of `run_eval`, existing and
new, passes `stages` or `env` explicitly, so a variable exported in the developer's shell cannot
change a test's result.
Secrets (webhook URLs, API tokens) are fields with `repr=False`, and `brain doctor` reports only
whether they are set.

### 2.4 The new-source protocol (G5)

Every source ticket (RM-30 … RM-45) answers these nine points, in this order, in a **Protocol**
block inside its design:

1. **Path.** One of: (a) *agent-orchestrated*: an MCP connector the owner already has fetches the
   content, and a skill recipe pipes it into `brain ingest-stdin --source <kind>`, as Krisp and
   Slack do; (b) *CLI shell-out*: an authenticated local CLI the machine already has (`gws`, `gh`),
   wrapped like `ingest/gmail.py`, with an injectable runner for tests; (c) *local files*: an
   extractor registered with `ingest/dispatch.py` (RM-07). Prefer (a), then (b). A new HTTP client
   to a SaaS API is a last resort and needs a written reason.
2. **Source kind.** A new member of `brain.source_kinds.VALID_SOURCE_KINDS`. After RM-07 that is
   the only place a kind is defined in Python (`vault/links._SOURCE_KINDS` stops being a second
   copy), and `ui.schemas.VALID_SOURCE_KINDS` re-exports it, so the facet panel and the source
   dropdown pick it up. **Three front-end tables are per kind and are not derived from it:** the
   glyph maps `SOURCE_ICONS` in `ui/static/js/results.js` and `ui/static/js/discovery.js` (both fall
   back to the `vault` glyph), and `MONTH_GROUPED_PATHS` in `ui/static/js/tree.js` (which
   `_ingested/<kind>` folders get month headers). Each source ticket either adds its entries to all
   three or states that the fallback is intended. The kind is a single lowercase word, because it
   becomes the mirror directory `_ingested/<kind>/`.
3. **Dedup key.** `(kind, external_id)` for sourced stdin, `source_path` for files, `content_hash`
   as the fallback (the four rules in `ingest_document`'s docstring). The ticket names which.
   **Volatile fields** (visit counts, occurrence lists, last-seen dates) never go in the body,
   because a body change re-chunks, re-embeds and re-extracts; they go in `metadata`, and a re-run
   whose body is unchanged applies them with `update_document(document_id=…, metadata_patch=…)`
   (no `new_content`, so no re-chunk).
4. **Credentials.** Never stored by `brain`. Path (a) uses the connector's own auth; path (b) uses
   the CLI's own login (`gws auth`, `gh auth`); a secret that must exist (a webhook URL) lives in
   `$BRAIN_HOME/.env`, is read through a settings leaf, and is never logged.
5. **A mandatory scope flag.** Like `brain ingest-gmail`, which refuses to run without one of
   `--query/--label/--from/--since/--until` (`cli_ingest.ingest_gmail`). No source ever bulk-imports
   an account. The ticket names the flags and the refusal message. Two accepted deviations, each
   stated in its ticket: an agent recipe (path (a)) carries the scope in the recipe text rather than
   in a flag (RM-34), and a user-made export carries it in what the owner chose to export (RM-35,
   RM-44).
6. **PII and sensitivity.** What is stored, what is dropped, and the default sensitivity. Content
   that is mostly third-party (calendar attendees, issue comments) keeps only the fields search
   needs (attendee addresses go to `metadata.participants`, never into the body). Content that is
   private by nature (screenshots, voice memos, personal notes, an employer's code or tickets)
   defaults to `confidential`, overridable per run. Every ingest goes through the existing secret
   guard (`ingest/guard.py`).
7. **Tests.** Synthetic fixtures only, built from named constants; the runner or the extractor is
   injected; one end-to-end test through `ingest_document` against the 5434 database; one CLI test
   for the scope refusal.
8. **Dependencies.** Prefer an external binary found on `PATH` (soft dependency) over a Python
   package. A Python package needs an evidence block in `pyproject.toml` in the house style (what
   was measured, which versions, why the bounds), and an import inside the extractor so the base
   CLI does not load it.
9. **Graph and enrichment cost.** Every new document gets an Ollama summary (unless `--no-enrich`
   or under `enrich_min_tokens`) and, when the graph is enabled, concept extraction in the watcher's
   `GraphLane` at about 8–10 minutes per document (memory note "Ollama + GraphRAG perf"). A bulk
   source would therefore occupy the lane for days. Each source ticket states: whether its
   `content_type` skips concept extraction (a new leaf, `graph_rag/extract_policy.py`, holds
   `CONCEPT_SKIP_CONTENT_TYPES`, and RM-30 wires it into `reconcile_plan` and `reconcile_document`
   with a skip watermark, so a skipped document counts as done; the people aspect still runs), whether the summary is skipped by
   default, its per-run cap, and the worst-case drain time of one capped run.

### 2.5 Egress rules for anything that leaves the machine

F6 (migration 026, `brain.sensitivity`) is the boundary. Every ticket that sends content off the
machine (RM-14 Voyage reranking, RM-16 delivery, RM-46 HTTP transport) or writes a published
artifact (the brief's vault note, the watch-hits section) obeys three rules:

1. **Confidential documents never leave.** A hosted backend gets the `hosted_egress` flag, mirroring
   `embeddings.is_hosted_embedder`, and the stage excludes confidential rows before the call.
2. **Default off.** Every push or hosted path is enabled by a knob the owner sets. A third-party
   sync service in the path (iCloud Drive in RM-39) counts as leaving the machine.
3. **Titles before bodies.** A push carries titles and counts by default. Body-derived text (todo
   items, snippets) is a second opt-in.

### 2.6 New dependencies

No new optional extra is needed for OCR. Two packages become **explicit base dependencies**,
because `brain` would import them directly (RM-32), and both are already installed by every resolve
that installs `pdfplumber`:

| Package | Ticket | Evidence to record in `pyproject.toml` |
|---|---|---|
| `pypdfium2` | RM-32 (page rasterization) | `pdfplumber` 0.11.10 (the resolved version) hard-requires `pypdfium2>=5.9.0`; 5.13.0 is resolved, so declaring it adds zero wheels. The floor is set to the version the RM-32 tests run on, not guessed. |
| `Pillow` | RM-32 (PNG encoding of rendered pages; test fixtures in RM-32 and RM-33) | `pdfplumber` 0.11.10 hard-requires `Pillow>=12.2.0`; 12.3.0 is resolved, so declaring it adds zero wheels. |
| `tree-sitter`, `tree-sitter-language-pack` (optional extra `code`) | RM-42, only if RM-42.D1 picks tree-sitter | Parsing many languages has no stdlib answer; the default is `ast` for Python plus a structural fallback, so the extra may never be needed. |

External binaries (soft dependencies, found with `shutil.which`, reported by `brain doctor`):
`llama-server` from llama.cpp (RM-14b), `tesseract` (RM-32), `whisper-cli` from whisper.cpp and
`afconvert` / `afinfo` (RM-38, the latter two macOS built-ins), `osascript` (RM-36, built-in), `gh`
(RM-31), `git` (RM-43), `fzf` (RM-52), and the existing `gws`. On 2026-10-23 `gh`, `gws` and `fzf`
are installed on the owner's machine; `tesseract`, `llama-server` and `whisper-cli` are not. Each
ticket that adds a binary adds its `brain doctor` row (the `doctor_runtime.py` budget, §2.2).

### 2.7 Eval policy for anything that touches ranking

- The quality gate is **local** (CLAUDE.md "Eval gate", decision 2026-10-12): `brain eval --baseline
  ci --diff --fail-below` on the owner's machine, against the 2,068-document set `ci.json` was
  recorded on (re-derive the count with `brain status` before believing a delta). An exit 3 with a
  moved document count is "the corpus moved", not a regression (CLAUDE.md, item 1).
- **A stage that is off by default changes nothing**: `ci.json` is not re-recorded, and the stage
  adds no key to `config_signature` while it is off (RM-14a, RM-20, RM-22b each assert this).
- **A stage being adopted** is measured on and off in the same session, the numbers go in the PR,
  and `ci.json` is re-recorded in the adopting PR with the trigger named in the commit message.
- **`brain eval` never falls back.** The fallback is a user-facing policy; the eval's degraded-row
  semantics depend on embed failures staying visible (`tests/test_search_fallback_eval_untouched.py`).
  `run_eval` therefore applies stages through `search_pipeline.apply_stages` after its own
  `hybrid_search` call (RM-09, RM-14a) and never calls `run_search`, so it cannot fall back.

### 2.8 Migration allocation (central; no ticket may deviate)

Head at `02c2abc` is `028_search_query_tokens.sql`. Applied migrations are recorded by name in
`schema_migrations` (`db.run_migrations`), so two tickets must never ship the same number.

| Number | File | Owner | Wave |
|---|---|---|---|
| 029 | `029_saved_searches.sql` | RM-15 | 4 |
| 030 | `030_near_duplicates.sql` | RM-19 | 5 |
| 031 | `031_document_embeddings.sql` | RM-20 | 5 |

**Conditional tickets are not pre-allocated.** RM-22b (a materialized engagement table, only if its
query budget is exceeded) and RM-60 (`documents.parent_document_id`, trigger-gated) take **the next
free number at the moment they merge**, and update this table in that PR. No number is held for
them, so no later ticket waits on a ticket that may never be built. Every new source uses the
existing `sources` / `documents` tables. Any other ticket that discovers it needs a migration does
the same: next free number at merge, table updated in the PR.

---


## 3. Tickets

Every ticket lists its fields in this order: **Source**, **Goal**, **Scope in / Scope out**,
**Design**, **Security / privacy**, **Ceiling**, **Acceptance criteria** (numbered), **Test plan**,
**Eval**, **Depends on**, **Size**, **Risks**, **Decisions**. "Ceiling" names the over-ceiling and
budgeted files touched (§2.2) and how growth is avoided; "Eval" says whether ranking moves and
whether `ci.json` is re-recorded. §2.1 (DoD) applies to every ticket and is not repeated. Four
kinds of ticket deviate, and say so: a parent of sub-tickets carries only the fields its
sub-tickets share; the three Ingest-tab tickets (RM-10, RM-11, RM-13) delegate design, acceptance
and tests to numbered sections of the Ingest spec; the trigger-gated group (RM-56 to RM-60) is one
table plus shared fields, and its acceptance criteria and test plans are written when a trigger
fires; and a closure (RM-61) has no design.

### Wave 1 — Embedder-down handling and two cheap guards

The fallback decision (the fallback plan, "The decision") is fresh, and three surfaces still break
when the embedder is down. Then two small guards close recorded holes. This is the backlog's §3
ranks 1a, 2 and 3, in that order.

#### RM-01 — `brain recall` falls back to text-only matching

- **Source:** BL-14; fallback plan follow-up F2 and its ruling table (`recall.recall` row).
- **Goal:** When the embedder is down, `brain recall` and MCP `brain_recall` return a text-only
  context block with the same one-line notice `brain search` prints, instead of a traceback (CLI) or
  `INTERNAL_ERROR embedding failed` (MCP).
- **Scope in:** `recall.recall`, `RecallResult`, `cli_recall.recall`, MCP `brain_recall`.
  **Scope out:** any change to packing, budgets or ranking; the eval runner.
- **Design:**
  - `recall.recall` calls `search_fallback.search_with_fts_fallback(hybrid_search, conn,
    embedder=embedder, query=query, **the kwargs it passes today)` in place of the direct
    `hybrid_search` call. Its docstring's "propagates … `EmbedError`" becomes "falls back on
    `EmbedError`; propagates `psycopg.Error`".
  - `RecallResult` gains `fallback: SearchFallback | None = None` (last field, defaulted, so every
    existing constructor call stays valid).
  - `RecallResult.to_dict()` merges `search_fallback.fallback_json(self.fallback)`: the same four
    always-present nullable keys (`fallback`, `fallback_reason`, `fallback_transient`,
    `fallback_note`) that `brain search --json --meta` and MCP `brain_search` emit.
  - `RecallResult.context_block()` appends `, text-only` to the header when `fallback` is set. The
    header is the pasted artifact an agent reads, so the marker has to be inside it, not only on
    stderr. *(As shipped; this paragraph first said the header "is covered by the fixed
    `_ENVELOPE_TOKENS` reserve (48 tokens)".)* That claim was false: the header echoes the query,
    which nothing caps, and a long query overran the budget on `master` even without a fallback
    (13 of 53 budgets in the RM-01 sweep). So the query the header echoes is capped at
    `_HEADER_QUERY_MAX_TOKENS` (16, measured with `count_tokens`, with one `…` appended when cut),
    and a test asserts that the worst-case header (the cap, the counts, `, truncated` and
    `, text-only`) fits the 48-token reserve, which therefore stays 48. Packing is untouched, and a
    query under the cap is echoed exactly as before. The JSON `query` stays whole.
  - `cli_recall.recall` prints `fallback.note` to stderr (yellow, `err=True`) when set, and exits 0.
  - MCP `brain_recall`: its `except EmbedError` branch becomes unreachable and is deleted, with a
    one-line comment naming `recall.recall` as where the failure is now handled (the treatment PR
    #61 gave `brain_search`). `_wrap_embed_error` stays for the other tools.
- **Security / privacy:** all fallback text is class-only (`SearchFallback.from_error` uses
  `describe_failure`). No new egress.
- **Ceiling:** `mcp_server.py` shrinks by the deleted branch: promote its row's trail and add a
  descriptive hop; no growth record is owed. `recall.py` (354) and `cli_recall.py` (199) stay small.
- **Acceptance criteria:**
  1. With an embedder that raises a transient `EmbedError`, `brain recall "q"` exits 0, prints
     text-only passages, and prints the transient note on stderr.
  2. With a permanent `EmbedError`, the note names a configuration problem and `brain doctor`.
  3. `brain recall --json` carries the four keys, all `null` on a normal run.
  4. The context block header contains `text-only` exactly when a fallback happened.
  5. With `, text-only` appended and a long query, `context_block()` as a whole still fits
     `budget_tokens` (the budget is honoured end to end, as `recall`'s docstring promises).
  6. MCP `brain_recall` returns the payload with the four keys and never `INTERNAL_ERROR` for an
     embed failure; `payload_tokens` still counts the whole payload.
  7. A `psycopg.Error` still maps exactly as before.
  8. `tests/test_search_fallback_eval_untouched.py` stays green (the eval does not import recall).
- **Test plan:** unit tests on `RecallResult` (header, `to_dict`, the budget with the marker); DB
  tests on `recall.recall` with `tests/embed_down.py`'s failing embedder (transient, permanent,
  context overflow); CLI tests through `CliRunner` (stderr note, exit 0, JSON keys); an MCP test
  calling `brain_recall` with the failing embedder in `_State`. The pre-fix traceback is reproduced
  RED first.
- **Eval:** none; the eval does not use recall. `ci.json` unchanged.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** an agent reading `context_block()` may miss that the context is lexical only. The
  header marker is the mitigation.
- **Decisions:** RM-01.D1, add a `fallback` field to `RecallResult` rather than raising: **field**
  (the user's search decision applies to every retrieval surface a person reads).

#### RM-02 — `brain ask` retrieves text-only when the embedder is down, and every failure is clean

- **Source:** BL-15; fallback plan follow-up F1 and the `ask._retrieve_hybrid` ruling row.
- **Goal:** `brain ask` and MCP `brain_ask` answer from text-only retrieval when the query embed
  fails, and say so outside the answer prose. When the chat model is also down, they exit with one
  clean message. No `ask` failure is ever a traceback, and no `ask` error text quotes an exception
  message.
- **Scope in:** `ask._retrieve_hybrid`, `ask._retrieve`, `AskResult`, the `ask` CLI handler in
  `cli.py`, MCP `brain_ask`, a new leaf `src/brain/ask_errors.py`. **Scope out:** synthesis
  prompts; graph-mode retrieval logic (`graph_rag/fuse.py` already degrades on its own).
- **Design:**
  - `_retrieve_hybrid` goes through `search_with_fts_fallback(hybrid_search, …)` and returns
    `FallbackSearch`. `_retrieve` returns `(results, graph_summary, fallback)`; `ask` and
    `ask_no_loop` keep the **first** fallback seen across sub-queries (every sub-query hits the same
    embedder, so the first is representative).
  - `AskResult` gains `retrieval_fallback: SearchFallback | None = None`. `to_dict()` merges
    `fallback_json(...)`, so one consumer check (`fallback == "fts_only"`) works on every surface.
    The existing `fallback_used` key (the planning loop was skipped or degraded) keeps its meaning
    and name: it is part of the `--json` and MCP output contract (`tests/test_cli_ask.py`,
    `tests/test_mcp_ask.py`, and the ask implementation spec). Its docstring gains one line
    distinguishing the two.
  - **The answer prose is not edited.** A marker injected into LLM text would mix metadata into
    content and disturb the `[N]` citation sanitizer. Instead, the CLI prints
    `retrieval_fallback.note` on stderr, and `_print_ask_result` adds one line under the answer,
    `Retrieval: text-only (semantic ranking unavailable)`. The MCP tool docstring tells the calling
    agent to relay the note when `fallback` is set.
  - New leaf `src/brain/ask_errors.py`:

    ```python
    ASK_FAILURES: tuple[type[BaseException], ...] = (
        EnrichmentError, GraphTenantError, GraphBackendError, EmbedError,
    )  # EnrichmentError covers OllamaUnavailable, OllamaContextOverflow, ChatOutputTruncated

    def describe_ask_failure(exc: BaseException) -> str:
        """One fixed, class-only sentence per failure family; never the exception message."""
    ```

    `OllamaUnavailable`, `OllamaContextOverflow` and `ChatOutputTruncated` all subclass
    `EnrichmentError` (`errors.py`), so `describe_ask_failure` tests the specific classes **before**
    the base. Messages: `OllamaUnavailable` → "Ollama is not available. Start it (for example `brew
    services start ollama`) and retry."; `OllamaContextOverflow` → "the question and the retrieved
    documents did not fit the chat model's context window; ask a narrower question or lower
    `--limit`"; `ChatOutputTruncated` → "the model's answer was cut off at its output budget; retry,
    or ask a narrower question"; any other `EnrichmentError` → "answer synthesis failed (<Class>)";
    graph errors → "graph retrieval failed (<Class>)"; a residual `EmbedError` (a graph mode that
    embeds the query outside the hybrid leg) → "the embedding model is unavailable; retry later"
    when `exc.transient`, else "the embedding model is not usable (not pulled, or a bad key); run
    `brain doctor`".
  - `cli.py`'s `ask` handler: the three `except` branches collapse into one, `except ASK_FAILURES
    as exc:` → `typer.secho(f"ask: {describe_ask_failure(exc)}", …)`, exit 1. The current branches
    interpolate `{exc}` into user-facing text; this removes that (DoD item 6). Net change to
    `cli.py`: negative.
  - MCP `brain_ask`: the same tuple maps to `INTERNAL_ERROR` with `describe_ask_failure(exc)`;
    `ValueError` and `psycopg.Error` keep their mappings.
- **Security / privacy:** class-only text everywhere; the confidential lens (`exclude_confidential`)
  is forwarded unchanged into the fallback call.
- **Ceiling:** `cli.py` shrinks, net −3 even with `eval --answer`'s `fallback=False` (promote the
  row; descriptive hop; no growth record). `mcp_server.py` shrinks too, net −6: one `except` replaces
  three and two imports go (promote the row; descriptive hop; no growth record). `ask.py` 654 → 684
  (budget, §2.2). *(As shipped in PR #64; this line first said `mcp_server.py` would grow by about
  two lines, recorded.)*
- **Acceptance criteria:**
  1. Embedder down, chat up, `--no-loop`: exit 0, an answer, the stderr note, and the `Retrieval:
     text-only` line; `--json` has `fallback: "fts_only"`.
  2. Embedder down, chat up, full loop: the same.
  3. Embedder down **and** chat down: exit 1 with the Ollama sentence only; empty stdout; no
     traceback.
  4. A graph-mode run whose graph leg raises `EmbedError`: exit 1 with the matching embed sentence.
  5. `OllamaContextOverflow` and `ChatOutputTruncated` each produce their own sentence, not the
     generic synthesis one.
  6. No branch prints an exception message: a test raises each family with a sentinel message and
     asserts the sentinel is absent from stderr and from the MCP error.
  7. MCP `brain_ask` returns the keys on fallback, and `INTERNAL_ERROR` with the fixed sentence for
     each failure family.
- **Test plan:** unit tests for `describe_ask_failure` (one per class, subclass ordering, sentinel
  absent); `ask` tests with a fake `ChatJson` and the failing embedder (both modes); `CliRunner`
  tests for criteria 1–6, with the pre-fix traceback reproduced RED first; MCP tests.
- **Eval:** none. `brain eval --answer` does **not** fall back: `ask()` and `ask_no_loop()` take
  `fallback: bool = True`, and `_run_answer_eval_cli` passes `fallback=False`, so an embed failure
  still aborts the run with exit 1 (§2.7; spec 2026-10-16 §3.4). *(Corrected during RM-02, by
  team-lead ruling; this line first said the answer eval "inherits the fallback", which
  contradicted §2.7 and would have left the answer eval's `except EmbedError` unreachable.)*
- **Depends on:** nothing. It may be developed beside RM-01, but the two merge one after the other
  (both touch `mcp_server.py`).
- **Size:** S.
- **Risks:** a weaker answer that reads confidently. Mitigated by the stderr note, the result line
  and the MCP keys.
- **Decisions:** RM-02.D1, fall back or refuse: **fall back** (the user's search decision; the
  fallback plan deferred only the marker question, answered above). RM-02.D2, new keys or reuse
  `fallback_used`: **new keys, with the same names search uses**.

#### RM-03 — `brain ui` search falls back automatically, with a visible notice

- **Source:** BL-13; fallback plan follow-up F3 and its `ui/routes_search.py` ruling row.
- **Goal:** With the embedder down, the Notes tab still returns results, and a notice above the
  ledger says they are text-only and why.
- **Scope in:** `ui/routes_search.search`, `ui/static/js/results.js`, `ui/static/index.html` (one
  notice element), a rule in `ui/static/css/components.css`, the `ci.yml` browser roster.
  **Scope out:** the related panel (it embeds no query), the facets.
- **Design:**
  - The route calls `search_with_fts_fallback(ctx.search_fn, conn, embedder=ctx.embedder,
    query=spec.query, limit=spec.fetch_limit, …)`. `UiSearchSpec.filter_kwargs()` carries `limit`
    (as `fetch_limit`) and the query today; the route takes both out of the splatted dict and passes
    them by keyword, so nothing is passed twice. `ctx.search_fn` stays the injection point
    (`ui/context.py`), so tests keep their recording double.
  - The `except EmbedError` → `UiUnavailable(..., code="embedding_unavailable")` branch becomes
    unreachable and is deleted, with a comment pointing at the helper. The code string stays only if
    another route still raises it (checked with grep at implementation).
  - The response gains `fallback_json(...)`'s four keys beside `meta`. `meta.fts_only` is already
    `true` after a fallback, because `embed_ms` stays `None` (fallback plan, "Details of the retry").
  - Front end: `index.html` gains `<p id="search-fallback" class="search-fallback" role="status"
    hidden></p>` above the ledger. `results.js` sets its `textContent` to `fallback_note` and
    un-hides it when `fallback` is non-null, and hides it otherwise. No `innerHTML`.
- **Security / privacy:** the note is class-only and server-built; the client never interpolates
  it into markup.
- **Ceiling:** none of the touched files is near 800 (`routes_search.py` 219, `results.js` 262).
- **Acceptance criteria:**
  1. With the injected embedder raising a transient `EmbedError`, `GET /api/search?q=…` returns 200
     with results, `fallback: "fts_only"` and a non-empty `fallback_note`.
  2. The browser shows the note and the results; the meta line still reads `fts-only`.
  3. A following search without a fallback hides the note.
  4. UI telemetry records the search with a `duration_ms` covering both attempts.
  5. A request with `limit` set returns the same page size with and without a fallback (no
     duplicate-keyword error, no double over-fetch).
- **Test plan:** route tests in `tests/test_ui_search_fallback.py` (real 5434 database, failing
  embedder double); a browser suite `tests/test_ui_browser_search_fallback.py` that stubs
  `/api/search` with a fallback payload through `page.route`, the hermetic pattern
  `tests/test_ui_browser.py` documents, added to the explicit `ci.yml` roster;
  `check_single_innerhtml` stays at one.
- **Eval:** none.
- **Depends on:** nothing. It merges one at a time with RM-04 (both edit `ci.yml` and
  `tests/test_ci_workflow.py`).
- **Size:** S.
- **Risks:** none beyond the shared "weaker ranking" risk the notice addresses.
- **Decisions:** RM-03.D1, a "retry with semantic ranking" button: **no**; the next search retries
  the embed anyway, since the query-embed cache does not cache exceptions.

#### RM-04 — CI fails when the ceiling table and the tree disagree

- **Source:** BL-1; CLAUDE.md ceiling section, "The residue" paragraph.
- **Goal:** A file that crosses 800 lines without a row, or a row for a file that is no longer
  over, turns CI red with a message that says which and what to do.
- **Scope in:** `scripts/check_ceiling_table.py`, its tests, one step in `ci.yml`'s `lint` job, the
  lint stage of `bin/brain-ci`, `tests/test_bin_brain_ci.py`, and the CLAUDE.md paragraph that says
  the check is "deliberately **not implemented**". **Scope out:** checking the *contents* of trails
  (a review job), and the near-ceiling budgets of §2.2.
- **Design:**
  - **Where the row set is parsed from:** `CLAUDE.md`, the one table whose header row is exactly
    `| File | Lines | Why it is not being split |`. Each data row's first cell is a backticked path
    relative to `src/brain/`. The parser stops at the first line after the table that does not
    start with `|`. The header is the anchor because it is unique in the file; a heading-text
    anchor would break on the next rewording.
  - **Script:** `scripts/check_ceiling_table.py`, outside the package like the other `scripts/`
    tools, with pure functions and `main(argv: list[str] | None = None) -> int`:

    ```python
    CEILING = 800
    HEADER = "| File | Lines | Why it is not being split |"

    def count_lines(path: Path) -> int: ...                    # data.count(b"\n"), identical to `wc -l`
    def over_ceiling(src_root: Path) -> dict[str, int]: ...    # rel path -> lines, lines > CEILING
    def table_rows(claude_md: str) -> list[TableRow]: ...      # raises TableNotFound
    def compare(over: Mapping[str, int], rows: Sequence[TableRow]) -> CeilingReport: ...
    ```

    `TableRow` holds the path and the Lines cell; `compare` also checks that the Lines cell's
    `wc -l src/brain/<path>` names the same path as the first cell, which catches a copied row.
    `CeilingReport` lists `missing_rows` (over, no row), `stale_rows` (row, but not over, or the file
    is gone) and `mismatched_rows`. Exit 0 when all are empty, 1 otherwise, 2 when the table is not
    found. Each message names the file and the remedy ("add a row in the CLAUDE.md trail form" /
    "remove the row and add a 'deliberately absent' note", the form `backup/restore.py` and
    `related.py` use).
  - **CI:** a step in the `lint` job, after `mypy src/`: `python scripts/check_ceiling_table.py`.
    The lint job has no database, and the check needs none.
  - **`bin/brain-ci`:** its lint stage runs each step with a status variable and an ok/fail line
    (`RUFF_STATUS`, `MYPY_STATUS`). The new step follows the same pattern (`CEILING_STATUS`, a
    `step` line, an `ok` line, and inclusion in the lint stage's combined exit status), so it is a
    small block rather than one line.
  - **`tests/test_bin_brain_ci.py`:** its drift check compares the script with the workflow but
    does not cover lint steps today; it is extended so a lint step present in `ci.yml` and missing
    from `bin/brain-ci` (or the reverse) fails.
- **Security / privacy:** none.
- **Ceiling:** no `src/` change. `CLAUDE.md`'s paragraph is rewritten to say the check exists, where
  it runs, and what it still cannot catch (the budgets, and the contents of a row).
- **Acceptance criteria:**
  1. On `master`, the script exits 0.
  2. A synthetic tree with an 801-line file and no row exits 1 and names the file.
  3. An 800-line file is not over (the rule is "> 800").
  4. A row for a deleted file, and a row for a 799-line file, each exit 1.
  5. A row whose Lines cell names another path exits 1.
  6. A `CLAUDE.md` without the header exits 2.
  7. Mutation proof: in a `git archive` copy, deleting one real row turns the script red.
  8. Removing the step from `bin/brain-ci` while `ci.yml` keeps it fails the drift test.
- **Test plan:** `tests/test_ceiling_table_check.py` loads the script with `importlib.util` (as
  `tests/test_embedding_smoke.py` loads its script) and drives the pure functions on `tmp_path`
  trees and synthetic markdown; one test runs it on the real repo; `tests/test_ci_workflow.py`
  asserts the lint job runs the step; `tests/test_bin_brain_ci.py` gains the lint-step drift case.
- **Eval:** none.
- **Depends on:** nothing. It merges one at a time with RM-03 (`ci.yml`) and with every ticket that
  edits `CLAUDE.md`'s ceiling table.
- **Size:** S.
- **Risks:** a future reformatting of the table header disables the check. Exit 2 makes that loud.
- **Decisions:** RM-04.D1, a script or a default-suite test: **a script plus tests**. A named CI
  step reads as a policy failure, which is what it is; a test failure would not.

#### RM-05 — The offline test resolves every CSS `url()`

- **Source:** BL-3; consolidation spec Q18.
- **Goal:** A typo'd `url()` in a stylesheet fails the default suite, as a typo'd `src=` in
  `index.html` already does.
- **Scope in:** `tests/test_ui_static_assets.py`. **Scope out:** source changes.
- **Design:** a new test, `test_every_css_url_resolves_on_disk`, walks `static_dir()/css/*.css`.
  For each file it first **strips `/* … */` comments** (`css/graph.css` and `css/related.css`
  mention `url()` inside comments), then extracts `url(...)` targets with a quote-aware regex using
  non-overlapping `finditer`, so the `url(%23n)` nested **inside** `base.css`'s `data:` SVG is
  consumed as part of the outer `data:` target and never matched on its own. It skips `data:` URIs
  and pure `#fragment` references, resolves each remaining target relative to the stylesheet's own
  directory, and asserts `is_file()` (not `exists()`: an empty target resolves to the directory,
  which exists) and that the file stays inside `static_dir()`. Today every real `url(` is the one
  `data:` SVG, so the test passes on `master` and becomes load-bearing the day RM-50 adds a font.
- **Security / privacy:** none.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. Green on `master`.
  2. A synthetic stylesheet with `url("missing.woff2")` fails, naming the stylesheet and the target.
  3. A `url()` that escapes `static_dir()` fails.
  4. `data:` URIs pass, including one that nests `url(%23id)` inside it.
  5. A `url()` mentioned only inside a comment is ignored.
  6. An empty `url()` fails rather than resolving to the directory.
- **Test plan:** the test itself, plus a parametrized test of the extraction function over
  synthetic CSS (quoted, unquoted, whitespace, several per line, a comment, a nested `data:` URI, an
  empty target). Mutation proof per DoD.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** none.

### Wave 2 — Seams that stop growth in over-ceiling files

**Why this wave is inserted ahead of the backlog's rank 4.** The backlog's order has no seams, and
without them roughly twenty later tickets would each grow `cli.py`, `mcp_server.py`,
`ingest/__init__.py`, `config.py` or `search.py`, each with its own two-place growth record. Four
small refactors, none of which changes behaviour, remove that. They are the smallest scope of BL-2
the ceiling rules allow: extraction *rather than growth*, not a split to satisfy a number.

#### RM-06 — Registration seams for new CLI commands and MCP tools

- **Source:** BL-2 (smallest scope); G1.
- **Goal:** After this ticket, a new command module or MCP tool module is added without editing
  `cli.py` or `mcp_server.py`.
- **Scope in:** `cli_registry.py`, one call in `cli.py`, a new package `src/brain/mcp_tools/`, the
  registration block and the `__main__` block of `mcp_server.py`,
  `tests/test_mcp_tool_registration.py`, `tests/test_mcp_server_protocol.py`. **Scope out:** moving
  any existing command or tool (that is RM-48).
- **Design:**
  - **CLI.** `cli_registry.py` gains `EXTENSION_REGISTRARS: tuple[tuple[str, Registrar], ...] = ()`
    and `register_extensions(app: typer.Typer) -> None`, which runs them in order. `cli.py` calls
    `register_extensions(app)` once, after its last registration, so `brain --help` lists extension
    commands last. A later ticket adds one import and one tuple entry in `cli_registry.py`. The rule
    in `cli_registry`'s docstring still holds: an extension module never imports `brain.cli` at
    module scope.
  - **The policy that comes with it:** a new feature prefers a **new command or sub-app** over a
    new flag on a command that lives in `cli.py`. A flag on an existing `cli.py` command is the one
    case the seam cannot absorb; a ticket that needs one records the growth.
  - **MCP, and why the helpers are passed rather than imported.** `tests/test_mcp_server_protocol.py`
    starts the server as `python -m brain.mcp_server`, which runs the module as `__main__`. A tool
    module that did `from brain import mcp_server` would load a **second** copy whose `_state` is
    `None`, so every extension tool would fail in exactly the process the protocol test exercises.
    Instead, `src/brain/mcp_tools/_context.py` defines

    ```python
    @dataclass(frozen=True)
    class ToolContext:
        get_state: Callable[[], Any]                       # mcp_server._get_state
        connection: Callable[[Any], AbstractContextManager[psycopg.Connection[Any]]]  # _mcp_conn
        error: Callable[[int, str], Exception]             # _mcp_error
        wrap_db_error: Callable[[psycopg.Error], Exception]
        wrap_embed_error: Callable[[EmbedError], Exception]
        confidential_lens: Callable[[bool], str | None]
    ```

    and `mcp_tools/__init__.py` exposes `register_extension_tools(app: MCPServerProtocol, ctx:
    ToolContext, *, modules: Sequence[ModuleType] | None = None) -> None`. With `modules=None` it
    imports every tool module of the package and calls its `register(app, ctx)`; a test passes
    `modules=[its_test_module]`, so a throwaway server never touches the real modules. Each tool
    module keeps the context in a module global set by `register`, defines its `brain_*` functions
    at module scope, and registers them with `app.tool()(fn)`, never a decorator (so the
    helper-between-decorator trap that once dropped `brain_recall` cannot happen there).
    **`register` refuses to rebind**: if the module already holds a different context it raises,
    unless the test-only helper `mcp_tools._reset_contexts_for_tests()` was called first. Because the
    context holds the importable module's own functions, `_get_state` reads the `_state` that
    `main()` set, and the tests' `monkeypatch.setattr(mcp_server, "_state", …)` (used in 39 test
    files) keeps working.
  - `mcp_server.py` gains, after its last tool, the `ToolContext(...)` construction and
    `register_extension_tools(mcp_app, ctx)`, **guarded by `if __name__ != "__main__":`**. Its
    `__main__` block becomes `from brain.mcp_server import main as _main; _main()`. So under
    `python -m brain.mcp_server`, the `__main__` copy defines its built-in tools but registers no
    extension (its context would be the wrong copy's), the import of `brain.mcp_server` then
    registers the extensions exactly once with that module's context, and `_main()` serves that
    module's `mcp_app`. Correctness under `python -m` therefore rests on that single registration,
    which the refusal to rebind would expose if it ever happened twice.
  - `tests/test_mcp_tool_registration.py` extends its roster to every module-level `brain_*`
    function in `brain.mcp_tools.*`, and asserts each is advertised by `mcp_app.list_tools()`.
- **Security / privacy:** none; no tool is added. `mcp_tools/__init__.py`'s docstring lists the lens
  helper every tool must use.
- **Ceiling:** `cli.py` +1 line and `mcp_server.py` about +12 lines, each recorded inline and in the
  module docstring as "the extension seam: later commands and tools do not grow this file".
- **Acceptance criteria:**
  1. A test-only extension command, registered through `EXTENSION_REGISTRARS` on a throwaway app,
     appears in `--help` after every built-in command.
  2. A test-only tool module, registered with `register_extension_tools(throwaway_app, ctx,
     modules=[test_module])`, is listed by that server's `list_tools()`, calling it reads the state
     the test installed with `monkeypatch.setattr(mcp_server, "_state", …)`, and no real tool
     module's context changes.
  3. A second `register` with a different context raises, unless the reset helper ran first.
  4. The registration guard fails if a module-level `brain_*` function in `brain.mcp_tools` is not
     advertised.
  5. Over `python -m brain.mcp_server`, the advertised tool list equals the in-process
     `list_tools()` (so extension registration runs exactly once, in the served module). RM-12
     extends this test to **call** its first extension tool over stdio.
  6. `import brain.cli` still does not load starlette or uvicorn (`tests/test_cli_ui.py`).
- **Test plan:** the tests named in the criteria, plus the existing registration and import-cost
  tests.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** an extension tool forgetting the confidential lens. Every tool ticket here names its
  lens, and its tests pin it.
- **Decisions:** RM-06.D1, a package or one module for MCP extensions: **a package**, one module per
  feature area (`related.py`, `saved_search.py`, `dedup.py`, …), each under 400 lines. RM-06.D2, move
  the helpers into a leaf or pass them: **pass them** (`ToolContext`); moving `_state` would break
  the 39 test files that install it on `mcp_server`.

#### RM-07 — Extractor registry, one source-kind definition, and the stale ingest docstring

- **Source:** G2, enabling every 2.x / 3.5 / 5.x ticket; BL-12.
- **Goal:** New file extractors register in a leaf module; a new source kind is defined in one place
  in Python; `ingest_document`'s docstring tells the truth about when the summary is written.
- **Scope in:** new `src/brain/ingest/dispatch.py`; `ingest/__init__.py` (re-export, shrink, and the
  docstring); `vault/links.py`; `source_kinds.py` (docstring); `cli_ingest.py` (import re-point);
  the Ingest spec's §3.1 mention of `ingest._EXTRACTORS`. **Scope out:** any new extractor or kind.
- **Design:**
  - `ingest/dispatch.py`:

    ```python
    ExtractorFn = Callable[[Path], ExtractedDoc]

    @dataclass(frozen=True)
    class ExtractorSpec:
        name: str                                   # "pdf"
        extensions: tuple[str, ...]                 # (".pdf",)
        load: Callable[[], ExtractorFn]             # lazy import, so the base CLI stays light
        wrap_errors: tuple[type[Exception], ...]    # parser errors re-raised as ValueError
        in_directory_walks: bool = True             # False: only an explicit --ext or command uses it

    def register_extractor(spec: ExtractorSpec) -> None: ...   # ValueError on a duplicate extension
    def extract_path(path: Path) -> ExtractedDoc: ...          # the current contract, unchanged
    def supported_extensions(*, directory_walk: bool = False) -> list[str]: ...
    ```

    The four built-ins (text, markdown, pdf, docx) register in `dispatch.py` itself, with the same
    lazy imports and the same error wrapping `extract_path` has today (`PyPdfError` and
    `PackageNotFoundError` → `ValueError`). `in_directory_walks` exists so that a later image or
    audio extractor (RM-33, RM-38) does not silently change what `brain ingest-dir` picks up:
    `ingest-dir` asks for `supported_extensions(directory_walk=True)`. `ingest/__init__.py` deletes
    `_EXTRACTORS`, `extract_path` and `supported_extensions` and re-exports the two functions, so
    `from brain.ingest import extract_path` keeps working.
  - **One source-kind definition.** `vault/links._SOURCE_KINDS` becomes `from ..source_kinds import
    VALID_SOURCE_KINDS as _SOURCE_KINDS` (an identity re-export, like `ui.schemas`). `source_kinds`
    imports only `brain.errors`, so there is no cycle. `source_kinds`'s docstring loses the "genuine
    separate copy" sentence. (The three per-kind front-end tables of §2.4 point 2 stay per ticket.)
  - **BL-12.** The `ingest_document` docstring's enrichment paragraph is rewritten to say the summary
    runs **after the commit**, as its own autocommit `UPDATE`, citing `_ingest_within_transaction`'s
    docstring, which already says so.
- **Security / privacy:** none.
- **Ceiling:** `ingest/__init__.py` shrinks (promote its row's descriptive last hop, add this one; no
  growth record). `cli_ingest.py` imports change in place (net zero; hop recorded).
- **Acceptance criteria:**
  1. Every existing extractor test passes unchanged.
  2. Registering a duplicate extension raises.
  3. A test-only extractor registered at run time is used by `extract_path` and listed by
     `supported_extensions()`; with `in_directory_walks=False` it is absent from
     `supported_extensions(directory_walk=True)`.
  4. `vault.links._SOURCE_KINDS is source_kinds.VALID_SOURCE_KINDS`.
  5. The docstring no longer says the summary is written "inside the same transaction" (a text
     test, so the stale sentence cannot come back).
- **Test plan:** `tests/test_ingest_dispatch.py`; the identity test; the docstring test.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** a new kind becomes a valid `[[<kind>:<id>]]` link prefix, so a vault page literally
  titled `<kind>:something` would resolve as a source link. That is already true of the four
  existing kinds; each source ticket names its kind and checks the vault for colliding titles in
  its plan.
- **Decisions:** none.

#### RM-08 — Feature settings leaves

- **Source:** G3.
- **Goal:** Knobs added by this program never grow `config.py`.
- **Scope in:** new package `src/brain/settings/` with `_env.py` (the parsers in §2.3) and an
  `__init__.py` docstring stating the policy; a short section in `docs/configuration.md`.
  **Scope out:** moving existing knobs.
- **Design:** §2.3. Parsers raise `brain.config.ConfigError` with the variable's name, and never
  echo the value. The `docs/configuration.md` section says these knobs are read from the same
  `.env` chain as every other.
- **Security / privacy:** secret-typed fields are `repr=False`; a test asserts a secret value never
  appears in `repr()` or in an error message.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. Each parser accepts valid values and refuses invalid ones with the variable named.
  2. An unset variable yields the default.
  3. A value set in a `.env` file is honoured after `Config.load()`.
  4. A secret never appears in `repr()` or an error.
- **Test plan:** pure tests at 95%+, and one integration test with a `tmp_path` `.env` using
  `tests/env_isolation.py`.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** none.

#### RM-09 — One search pipeline for every user-facing surface

- **Source:** G4; enabling RM-14 (reranker), RM-20 (document leg), RM-22b (engagement) and RM-54
  (rewrite).
- **Goal:** Every surface a person or agent searches through composes the same stages in the same
  order, and a new stage is wired once.
- **Scope in:** new leaf `src/brain/search_pipeline.py`; the six user-facing surfaces:
  `cli_search.search`, `cli_search.explain`, MCP `brain_search`, `ui/routes_search.search`,
  `recall.recall` (after RM-01) and `ask._retrieve_hybrid` (after RM-02). **Scope out:**
  `eval/runner.run_eval` (it applies stages itself, RM-14a), `graph_rag/fuse.py`, and
  `demo.query_demo`, each of which keeps its recorded reason for calling `hybrid_search` directly.
- **Design:**

  ```python
  class SearchStage(Protocol):
      name: str
      def candidate_limit(self, limit: int) -> int: ...      # how deep the stage needs to see
      def signature(self) -> dict[str, Any]: ...             # merged into eval's config_signature
      def apply(self, conn: psycopg.Connection[Any], *, query: str, session_id: str | None,
                results: list[SearchResult]) -> StageOutcome: ...

  @dataclass(frozen=True)
  class StageReport:
      name: str
      applied: bool
      skipped_reason: str | None      # class-only
      ms: float

  @dataclass(frozen=True)
  class StageOutcome:
      results: list[SearchResult]
      report: StageReport

  @dataclass(frozen=True)
  class PipelineResult:
      results: list[SearchResult]
      fallback: SearchFallback | None
      stages: tuple[StageReport, ...]

  def apply_stages(conn, *, query: str, session_id: str | None, results: list[SearchResult],
                   stages: Sequence[SearchStage]) -> tuple[list[SearchResult], tuple[StageReport, ...]]: ...

  def run_search(search_fn: SearchFn, conn, *, embedder: Embedder, query: str, limit: int,
                 session_id: str | None = None, fallback: bool = True,
                 stages: Sequence[SearchStage] = (), **kwargs: Any) -> PipelineResult: ...

  def build_stages(env: Mapping[str, str]) -> tuple[SearchStage, ...]: ...  # () here; reads settings leaves
  ```

  `run_search` asks `search_fn` for `max(limit, *(s.candidate_limit(limit) for s in stages))` rows
  (through `search_with_fts_fallback` when `fallback` is true), runs `apply_stages`, and truncates
  to `limit`. `limit` is passed explicitly and must not also be in `kwargs` (the UI route takes its
  `fetch_limit` out of `filter_kwargs()` first, RM-03). A stage that fails (timeout, unreachable
  backend) returns its input unchanged with `applied=False` and a class-only `skipped_reason`; a
  stage never raises past `apply_stages`. `stage_reports_json(stages)` projects the reports for the
  `--json --meta` envelope and MCP as a fifth always-present key, `stages: []`.
- **Boundary guard.** `tests/test_search_pipeline_boundary.py` walks the AST of every module under
  `src/brain` and fails on any **call** of `hybrid_search(…)` or `search_with_fts_fallback(…)`
  (a bare name or an attribute) outside an allowlist: `search.py`, `search_fallback.py`,
  `search_pipeline.py`, `eval/runner.py`, `graph_rag/fuse.py`, `demo/__init__.py`, and the
  patch-point shim function `cli_search.hybrid_search`, which forwards to `cli.hybrid_search`.
  Importing or **passing** `hybrid_search` (as the patch points in `cli.py`, `cli_search.py`,
  `ui/server.py`, `mcp_server.py`, `recall.py` and `ask.py` do) is allowed. Guarding
  `search_with_fts_fallback` too is what stops a seventh surface from getting the fallback while
  silently skipping the stages. (`related.py` is not on the list: it neither imports nor calls
  `hybrid_search`; it only mirrors its structure.)
- **Security / privacy:** none; behaviour is identical with zero stages.
- **Ceiling:** `mcp_server.py`: `brain_search` swaps one call for another (net zero; hop recorded).
  `cli_search.py` and `ask.py` net zero.
- **Acceptance criteria:**
  1. With no stages, every surface's output is byte-identical to `master`
     (`tests/test_search_output_unchanged.py` stays green; the `stages: []` key appears only in the
     `--meta` envelope and in MCP).
  2. A test stage that reorders results is applied on every surface (one test per surface).
  3. A test stage that raises is reported as skipped, and the results are the unstaged ones.
  4. The guard fails on a synthetic module (in `tmp_path`) that calls `hybrid_search(…)` or
     `search_with_fts_fallback(…)` outside the allowlist, and passes one that only imports it.
  5. The UI route with a `limit` parameter does not pass `limit` twice.
- **Test plan:** pure tests for `run_search` and `apply_stages` with a fake `search_fn`;
  per-surface integration tests; the guard with its mutation proof.
- **Eval:** none (no stage yet).
- **Depends on:** RM-01, RM-02, RM-03.
- **Size:** S.
- **Risks:** an output-shape drift on a frozen contract. The bare `--json` list is untouched; only
  the opt-in envelope and MCP gain a key, as the fallback plan ruled for its own keys.
- **Decisions:** RM-09.D1, stages see `SearchResult`s, not chunks: **results** (a stage that needs
  chunk text reads the snippet, which already carries `snippet_context_tokens` of context).

### Wave 3 — The Ingest tab and MCP parity

The backlog's ranks 4, 5 and 6, in that order. The Ingest spec is the design; RM-10, RM-11 and
RM-13 add only what the roadmap needs on top, and delegate the rest to numbered sections of it.

#### RM-10 — Ingest tab, phase 1: jobs and the paste backend

- **Source:** Ingest spec §10, phase 1; backlog rank 4.
- **Goal:** Paste ingest works end to end over HTTP, on a job registry that the Agent tab can reuse.
- **Scope in / out, design, acceptance criteria and test plan:** exactly the Ingest spec's phase-1
  row, §4 (the job subsystem), §5.1, §5.3, §5.4, §5.6, §6 (the paste routes), §7.1, §7.5, §9.1,
  §9.2 and §9.3 (paste rows). Decisions D1–D5 and D8–D10 apply as written.
- **Additions from this program:**
  - `JobRegistry` is built kind-agnostic, with `kind` a free string, because RM-18a registers `"ask"`
    on it without changing `jobs.py` (Ingest spec §4.7).
  - The registry supports a **per-kind retention override** (`retain_for` by kind), which RM-18a
    uses to keep ask results for a shorter time than ingest results.
  - The job's result payload for a new document carries `possible_duplicates: []` as an
    always-present key, filled by RM-19 later, so the front end's shape is fixed now.
  - Paste builds an `ExtractedDoc` directly and never calls `extract_path`, so this ticket does not
    depend on RM-07 (the dispatcher matters only for upload, RM-13).
- **Security / privacy:** the Ingest spec's §7 in full (D7, D8, and the §1.3 mutation-confinement
  amendment).
- **Ceiling:** none of the touched files is in the table except `cli_ingest.py`, which shrinks (the
  Ingest spec's §10, "Ceiling check").
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** M (the Ingest spec estimates about 900 source and 1,100 test lines).
- **Risks and decisions:** the Ingest spec's §11 and §12, recommended defaults as written.

#### RM-11 — Ingest tab, phase 2: the paste front end

- **Source:** Ingest spec §10, phase 2, plus the paste half of its "After" row.
- **Goal:** Paste ingest is usable from the browser, with live progress, cancel and a link to the
  new document.
- **Scope in:** `jobs.js`, `ingest.js` (paste mode), `ingest.css`, the `#tab-ingest` panel in
  `index.html`, `tests/test_ui_tabs_static.py`, the browser tests, the `ci.yml` roster entry, and
  the paste half of the Ingest spec's "After" row: `docs/cli-reference.md` (the `brain ui` section),
  `README.md`, `docs/guides/brain-ui-on-your-phone.md` (ingest is unavailable over `--lan`), and
  the `types.md` memory file for `JobSnapshot`, `JobRegistry`, `IngestOutcome` and
  `ProgressEmbedder`. **Scope out:** upload (RM-13).
- **Design and acceptance criteria:** the Ingest spec's §8, and its §3.4 items 1, 2, 4, 5 and 6 for
  paste. `#tab-ingest` stops being a `class="shell deferred-tab"` section, and
  `tests/test_ui_tabs_static.py`'s copy expectations change in the same PR.
- **Security / privacy:** the Ingest spec's §7.5; every server string is inserted with
  `textContent`.
- **Ceiling:** none; `index.html` (218 lines) and the new JS and CSS files are small.
- **Test plan:** the Ingest spec's §9.4 paste rows, hermetic (stubbed with `page.route`), plus a
  manual pass on the demo sandbox.
- **Eval:** none.
- **Depends on:** RM-10.
- **Size:** M.
- **Risks and decisions:** the Ingest spec's §11 and §12.

#### RM-12 — MCP `brain_related` and `brain_explain`

- **Source:** roadmap 6.1 (the missing tool), Q1-B D13, BL-6; backlog rank 5.
- **Goal:** An agent can ask for the documents related to one it has open, and can ask why a search
  ranked what it did, with the same answers the UI and `brain explain` give.
- **Scope in:** new `src/brain/mcp_tools/related.py` (both tools, through RM-06's seam); a shared
  `explanation_json(explain: SearchExplanation | None) -> dict[str, Any] | None` moved into
  `format_search.py` from `cli_search.explain`'s inline literal; `docs/configuration.md` MCP rows.
  **Scope out:** any ranking change; a truncation flag on `RelatedDoc`.
- **Design:**
  - `brain_related(id: str, limit: int = DEFAULT_RELATED_LIMIT, include_confidential: bool =
    False) -> dict`, where `DEFAULT_RELATED_LIMIT` is `related.py`'s constant (10). It resolves the
    id prefix like `brain_show` and calls `related.compute_related(conn, doc_id, limit=…,
    vector_sim_floor=cfg.vector_sim_floor, exclude_confidential=not include_confidential)`. It
    **gates the root too**, as `ui/routes_related` does: a confidential root with
    `include_confidential=false` gets `INVALID_PARAMS` "document is confidential; pass
    include_confidential=true", naming no title and no candidate, checked before ranking.
    `compute_related` already cuts each snippet at `related.SNIPPET_LENGTH` (240 characters) and
    `RelatedDoc` has no truncation flag, so the tool returns the snippet as is and adds no flag.
    `limit` is capped by `mcp_limits.check_ceiling(limit, ceiling=RELATED_LIMIT_CEILING,
    param="limit")`, with `RELATED_LIMIT_CEILING = 20` a module constant (not a `config.py` knob).
    It returns `{id, related: [{id, title, vault_path, source, score, snippet}], vector_sim_floor}`.
  - `brain_explain(query: str, limit: int = 10, source: str | None = None, tag: str | None = None,
    since_days: int | None = None, include_confidential: bool = False) -> dict`. It runs
    `search_pipeline.run_search(…, explain=True, sensitivity=None if include_confidential else
    "normal")` and returns `{query, results: [{…search_results_json row without snippet…, explain:
    …}], fallback…, stages}`. Snippets are left out (an explanation is about rank, not content),
    which also keeps the payload small. The lens is the oracle-closing kind: confidential rows are
    excluded from the match set, not redacted.
  - `cli_search.explain` switches to `explanation_json` (net negative for `cli_search.py`).
- **Security / privacy:** both lenses exclude confidential documents by default (MCP is outside the
  F6 trust boundary); the root gate closes the "documents similar to the withheld one" leak that
  `routes_related`'s docstring describes.
- **Ceiling:** no over-ceiling file (RM-06); `cli_search.py` shrinks.
- **Acceptance criteria:**
  1. `brain_related` with `limit=10` returns the same ids, in the same order, as `GET
     /api/notes/{id}/related` (whose default is the same `DEFAULT_RELATED_LIMIT`) for the same
     configuration.
  2. A confidential root without `include_confidential` is refused before ranking; with it, it is
     answered.
  3. A confidential candidate is never returned without `include_confidential`.
  4. `brain_explain` returns, per result, the same `explain` object `brain explain --json` prints.
  5. `limit` above the ceiling is `INVALID_PARAMS`.
  6. Both tools appear in `list_tools()`, and the registration guard covers them.
  7. `tests/test_mcp_server_protocol.py` calls `brain_related` over `python -m brain.mcp_server`
     and gets a result (the first extension tool called over stdio; RM-06 criterion 5).
- **Test plan:** DB tests on a synthetic three-document corpus (one confidential); a parity test
  against the UI route and the CLI; MCP error-mapping tests; the stdio protocol test.
- **Eval:** none.
- **Depends on:** RM-06, RM-09.
- **Size:** S.
- **Risks:** none beyond the lens, which the tests pin.
- **Decisions:** RM-12.D1, one module or two: **one** (`mcp_tools/related.py`); both are read-only
  ranking diagnostics.

#### RM-13 — Ingest tab, phase 3: file upload

- **Source:** Ingest spec §10, phase 3, plus the upload half of its "After" row; backlog rank 6.
- **Goal:** Upload one `.md`, `.markdown`, `.txt`, `.pdf` or `.docx` file per job from the
  browser, refused before staging when its type or size is wrong.
- **Scope in:** the Ingest spec's phase-3 row; the allowlist constant below; the amendment of the
  Ingest spec's §7.4 and its §9 test-table row; the upload half of the "After" docs
  (`docs/cli-reference.md`, `README.md`). **Scope out:** images and audio (RM-33 and RM-38 add
  extractors, which are **not** uploadable until a ticket adds them to the allowlist on purpose).
- **Design and acceptance criteria:** the Ingest spec's §5.2, §7.2–§7.4, and §3.4 item 3, with one
  change. The spec reads the upload allowlist from `ingest.supported_extensions()`; after RM-07 and
  RM-33 that would admit images. So the allowlist becomes an explicit constant,
  `UPLOAD_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".pdf", ".docx"})`
  in `ui/ingest_limits.py`, asserted by a test to be a subset of
  `ingest.dispatch.supported_extensions()`. The Ingest spec's §7.4 (which names
  `ingest.supported_extensions()`) and its §9 row for `tests/test_ui_upload_names.py` are amended in
  the same PR, and staging calls `ingest.dispatch.extract_path`.
- **Security / privacy:** the Ingest spec's §7.2–§7.4. **Security review** with
  `ecc:security-reviewer` on opus, in addition to the rule-14 loop, as the spec requires.
- **Ceiling:** none (the Ingest spec's §10, "Ceiling check").
- **Test plan:** the Ingest spec's §9.2 upload rows, the §9.3 parametrized Content-Type test that
  proves the exception is exactly one route, the §9.4 upload browser rows, and the subset test for
  `UPLOAD_EXTENSIONS`.
- **Eval:** none.
- **Depends on:** RM-11, RM-07.
- **Size:** M.
- **Risks and decisions:** the Ingest spec's §11 and §12.

### Wave 4 — Ranking and the active brain

The backlog's ranks 7 to 10: the reranker (measurable now that PR #50 re-judged all 31 golden rows),
saved searches, the Agent tab, and brief delivery. The Agent tab comes after saved searches and
delivery here, not between them as in the backlog, because RM-15 and RM-16 are small and independent
of it, and the Agent tab's front end is the largest single item in the wave.

#### RM-14 — Cross-encoder reranker (three sub-tickets)

- **Source:** roadmap 5 (the top-5 list's #5), BL-5, the MCP server spec's "Out of scope" list
  ("reranking before returning search results"); the owner's 2026-05-11 decision: local first
  (`bge-reranker-v2-m3`), Voyage as the fallback, decided at build time.
- **Goal:** An optional second-stage reranker that reorders the top candidates of every user-facing
  search, turned on only if `brain eval` shows a gain.
- **Fields shared by the three sub-tickets:** **Security / privacy:** §2.5; the local backend is
  loopback-only by configuration, and the hosted one never receives a confidential passage.
  **Ceiling:** no over-ceiling file in any sub-ticket (`setup.py` only if RM-14c adopts).
  **Depends on:** RM-08, RM-09. **Size:** L in total (M + S + S). The rest is per sub-ticket.

**What the runtime can be, checked on 2026-10-23.** Ollama has no rerank API: the upstream tracking
issue (ollama/ollama#3368) is open, a 2026 issue (ollama/ollama#16076) states there is no
`/api/rerank`, and community workarounds read scores out of `/api/generate` text or embedding
magnitudes, which their own authors call less accurate than cross-encoder scoring. So "via Ollama",
as the May roadmap assumed, is not available. The candidates:

| Runtime | New Python dependency | Notes |
|---|---|---|
| **`llama-server` (llama.cpp)**, serving a `bge-reranker-v2-m3` GGUF | **None** (an external binary over HTTP, through the already-declared `httpx`) | Recommended. A local service on loopback, Metal-accelerated, installed with Homebrew (`llama.cpp`). It must be started with `--embedding --pooling rank --reranking`: `--reranking` only enables the endpoint, and the rank pooling is what makes the model score pairs. API: `POST {BRAIN_RERANK_URL}/v1/rerank` (aliases `/rerank` and `/reranking`), body `{query, documents, top_n}`, response `results[]` of `{index, relevance_score}`. **Verify the flags, path and payload against the llama.cpp server README at implementation**, and pin the tested `llama-server` version in the guide. |
| `sentence-transformers` `CrossEncoder` | Yes: `torch` (hundreds of MB) | Rejected: the heaviest dependency in the tree, for one stage. |
| `fastembed` / ONNX Runtime | Yes: `onnxruntime` | Rejected for v1: smaller than torch, but a new native wheel, and `bge-reranker-v2-m3` support there is not confirmed. Recorded as the fallback if `llama-server` proves unworkable. |
| **Voyage** (hosted) | **None** (`voyageai` is declared) | The hosted alternative, subject to §2.5. Model: the one Voyage currently recommends, re-checked at build time. On 2026-10-23 Voyage's reranker docs (docs.voyageai.com/docs/reranker) recommend `rerank-3`, and `rerank-3-lite` for latency-sensitive use. |

##### RM-14a — The reranker stage, backends, settings and eval wiring (default OFF)

- **Source / goal:** the parent's; this sub-ticket builds the stage and its plumbing, and ships it
  off.
- **Scope in:** new leaves `src/brain/rerank.py` (Protocol and stage) and
  `src/brain/rerank_backends.py`; `src/brain/settings/rerank.py`; `search_pipeline.build_stages`;
  `eval/runner.run_eval` and its `EvalResult`; `cli_search.py` (`--rerank/--no-rerank`);
  `format_search` (a `rerank` timing phase); the UI's meta line. **Scope out:** turning it on;
  installing the runtime (RM-14b); any edit to `cli.py`'s `eval_cmd`.
- **Design:**

  ```python
  class Reranker(Protocol):
      name: str              # "llamacpp" | "voyage"
      model: str
      hosted_egress: bool    # mirrors embeddings.is_hosted_embedder's flag
      def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]: ...

  class LlamaCppReranker:    # httpx.Client(timeout=...), POST {url}/v1/rerank; hosted_egress=False
  class VoyageReranker:      # voyageai.Client(api_key=..., max_retries=0, timeout=...).rerank(
                             #   query, passages, model=...); hosted_egress=True

  @dataclass(frozen=True)
  class RerankStage:         # implements search_pipeline.SearchStage
      reranker: Reranker
      depth: int
      max_chars: int
      timeout: float
      name: str = "rerank"
  ```

  - `candidate_limit(limit) = max(limit, depth)`. `apply` takes the first `depth` results, builds
    each passage as `title + "\n" + snippet` cut to `max_chars`, scores them, and reorders the head
    by score, descending (ties keep their prior order). The tail beyond `depth` keeps its order after
    the head. `signature()` returns `{"reranker": name, "reranker_model": model, "rerank_depth":
    depth}`.
  - **Egress.** When `hosted_egress` is true, confidential rows in the head are **not sent**: one
    `SELECT id FROM documents WHERE id = ANY(%s) AND sensitivity = 'confidential'` finds them, the
    others are scored and reordered, and each confidential row keeps its original index.
  - **`score` is not overwritten.** `SearchResult.score` stays the RRF-plus-recency score, and
    `explain.reranker_score` (the existing placeholder on `SearchExplanation`) is set through
    `dataclasses.replace`, so `search.py` is not edited. A reranked list may therefore be
    non-monotonic in `score`; the docs say so, and the bare `--json` list's shape is unchanged.
  - **Failure.** A timeout, connection error or malformed response returns the input unchanged with a
    class-only `skipped_reason`, and the CLI prints one stderr line, "reranker unavailable; results
    are in hybrid order". Search never fails because of the reranker.
  - **Settings** (`settings/rerank.py`): `BRAIN_RERANKER` = `none` (default) | `llamacpp` |
    `voyage`; `BRAIN_RERANK_DEPTH` = 20 (2 to 50; `CANDIDATE_LIMIT` is 50); `BRAIN_RERANK_TIMEOUT_S`
    = 5.0; `BRAIN_RERANK_URL` = `http://127.0.0.1:8081` (loopback only: a non-loopback host is a
    `ConfigError`, because the passages are document text); `BRAIN_RERANK_MODEL` (defaults
    are named constants in `settings/rerank.py`: `LLAMACPP_RERANK_DEFAULT_MODEL =
    "bge-reranker-v2-m3"` and `VOYAGE_RERANK_DEFAULT_MODEL = "rerank-3"`, the model Voyage's reranker
    docs recommend as of 2026-10-23; the constant is re-checked against those docs at build time and
    its value and check date are recorded in the guide); `BRAIN_RERANK_MAX_CHARS` = 1,500. The Voyage key is read from `VOYAGE_API_KEY`, the
    variable `Config` already uses.
  - **Surfaces.** `build_stages(env)` adds `RerankStage` when `BRAIN_RERANKER != none`, so every
    surface on the pipeline gets it. `brain search` and `brain explain` gain `--rerank/--no-rerank`
    (an override for one run). MCP and the UI follow the setting. The UI's meta line shows `rerank
    0.4` beside `embed` and `rank`.
  - **Eval, without touching `cli.py`.** `run_eval` gains `stages: Sequence[SearchStage] | None =
    None` and `env: Mapping[str, str] | None = None`. With `stages=None` it builds them with
    `search_pipeline.build_stages(env if env is not None else os.environ)`; `eval_cmd` has already
    populated `os.environ` by calling `Config.load()` (the §2.3 carve-out). **Every test passes
    `stages` or `env` explicitly**: existing `run_eval` tests are updated to pass `stages=()`, so a
    `BRAIN_RERANKER` exported in a developer's shell cannot change them. After its own `hybrid_search` call, `run_eval` runs `search_pipeline.apply_stages` (never
    `run_search`, so it never falls back). Each stage's `signature()` is merged into
    `config_signature` (`eval/runner.py`, the dict built after the loop), so the signature is
    unchanged while every stage is off. Each `EvalResult` gains `stage_ms: dict[str, float]` from
    the `StageReport`s, which is what RM-14c reads for latency.
- **Security / privacy:** the parent's, plus: the Voyage key is never logged; the egress filter is
  mutation-proven.
- **Ceiling:** `cli_search.py` +≤ 12 (budget, §2.2). No over-ceiling file.
- **Acceptance criteria:**
  1. With `BRAIN_RERANKER=none`, every surface's output and `config_signature` are byte-identical to
     before (`test_search_output_unchanged.py` and a signature test).
  2. With a fake reranker, the head is reordered by its scores on every surface, the tail is kept,
     and `explain.reranker_score` is set.
  3. A hosted fake never receives a confidential passage, and confidential rows keep their indices.
  4. A timing-out fake yields hybrid order and one stderr line; exit 0.
  5. A non-loopback `BRAIN_RERANK_URL` is a `ConfigError`.
  6. `run_eval(stages=[fake])` scores the reranked order and records `stage_ms`; a transient embed
     failure is still a degraded row (no fallback).
  7. With `stages=None` and `env={"BRAIN_RERANKER": "llamacpp", …}` (a dict, not the process
     environment), `run_eval` builds the stage itself and `config_signature` gains the three keys.
  8. Every existing `run_eval` test passes `stages=()` and is unaffected by an exported
     `BRAIN_RERANKER`.
- **Test plan:** pure tests for the stage (ordering, ties, tail, egress filter, failure); backend
  tests against an `httpx.MockTransport` (llama.cpp, using the documented request and response
  shape) and a fake Voyage client passed to the constructor; settings tests; eval-runner tests;
  CLI and MCP surface tests.
- **Eval:** none while off; `ci.json` unchanged.
- **Depends on:** RM-08, RM-09.
- **Size:** M.
- **Risks:** the llama.cpp rerank payload changing between releases. Mitigated by a backend test
  pinned to the documented schema, and by the guide pinning a version.
- **Decisions:** RM-14a.D1, overwrite `score`: **no** (above). RM-14a.D2, a per-call rerank
  parameter on MCP `brain_search`: **no, follow the setting**; a parameter would grow
  `mcp_server.py` for a knob agents do not need.

##### RM-14b — The local runtime: guide, doctor probe and an optional launchd job

- **Source / goal:** the parent's; the local reranker can be installed, started at login and checked
  by `brain doctor`.
- **Scope in:** `docs/guides/local-reranker.md`; a `brain doctor` row in `doctor_runtime.py`; an
  opt-in launchd template `src/brain/templates/launchd/com.brain.reranker.plist.j2`; its wiring in
  `bin/launchd.py` (a new `_OPTIONAL_LABELS` tuple, swept by uninstall and checked by `brain doctor`
  only when the plist exists, because `doctor_runtime.py` treats `_INSTALLED_LABELS` as daemons that
  must be running) and `bin/brain-install-launchd` / `bin/brain-uninstall-launchd`. **Scope out:**
  `brain setup` (until RM-14c adopts).
- **Design:** the doctor row runs `GET {BRAIN_RERANK_URL}/health` with a 2 s timeout, only when
  `BRAIN_RERANKER=llamacpp`. The plist runs `llama-server -m <gguf> --embedding --pooling rank
  --reranking --host 127.0.0.1 --port 8081`; its `ProgramArguments` are fixed in the template, and
  only the model path is a variable, so the host cannot be set to `0.0.0.0`. It is installed only
  by an explicit `--with-reranker` flag, parsed in `brain.bin.launchd:install_main` (the
  `bin/brain-install-launchd` script is a passthrough to it). The label is **sticky**, as RM-33
  specifies for optional labels: the flagless install `brain-up.sh` runs on every start re-renders an
  already-installed reranker plist and never removes it. The guide names the tested
  `llama.cpp` version, the GGUF's source and size, and its measured resident memory.
- **Security / privacy:** loopback bind only; the model file is local.
- **Ceiling:** `doctor_runtime.py` +≤ 25 (budget, §2.2). `setup.py` is not touched.
- **Acceptance criteria:**
  1. The doctor row reports "ok", "not running" and "not configured" in the three states.
  2. The plist renders with `127.0.0.1`, the three flags and the configured model path.
  3. A first `brain-install-launchd` without `--with-reranker` does not install it;
     `brain-uninstall-launchd` removes it when present.
  4. After `--with-reranker`, a plain flagless `brain-up` keeps `com.brain.reranker` installed and
     re-renders it.
- **Test plan:** a doctor test against an `httpx.MockTransport`; a plist render test; install and
  uninstall tests in the style of the existing launchd tests (no real `launchctl`).
- **Eval:** none.
- **Depends on:** RM-14a.
- **Size:** S.
- **Risks:** memory: one more resident model. The guide states its measured size.
- **Decisions:** RM-14b.D1, ship a launchd job: **yes, opt-in**, because a reranker that is not
  running is silently skipped, and a daemon is how the other models stay up.

##### RM-14c — Measure, then adopt or record

- **Source:** RM-14a and RM-14b; the eval policy (§2.7).
- **Goal:** A recorded, reproducible decision on whether the reranker is on.
- **Scope in:** two eval runs on the owner's machine, the adoption rule, and either the adoption
  changes or a measurement note. **Scope out:** CI; any change to the code default.
- **Design:**
  - On the owner's machine (not CI), run `brain eval --baseline ci --diff` with
    `BRAIN_RERANKER=none` and with `BRAIN_RERANKER=llamacpp` (and `voyage` if a key is configured),
    in one session, after checking the document count with `brain status`. Record the three means,
    the per-category table, and the p50 and p95 of the `rerank` entry of `EvalResult.stage_ms`
    (RM-14a) over the golden queries.
  - **Adoption rule (all three):** no mean regresses by more than `1e-4`; nDCG@5 or MRR improves by
    at least **0.02** absolute; p95 added latency is at most **1.0 s**.
  - **If adopted:** the owner's `$BRAIN_HOME/.env` sets `BRAIN_RERANKER=llamacpp`; `brain setup`
    offers it (a recorded `setup.py` growth); `ci.json` is re-recorded in this PR with the trigger
    "ranking change (reranker adopted)"; the `search.md` memory file is updated. The **code default
    stays `none`**, because the runtime is an external service a fresh install does not have.
  - **If not adopted:** `docs/plans/<date>-reranker-measurement.md` records the numbers, and the
    backlog row is closed as "built, measured, not adopted".
- **Security / privacy:** none beyond RM-14a.
- **Ceiling:** `setup.py`, only if adopted (recorded in both places, and its row's hop promoted).
- **Acceptance criteria:**
  1. The PR carries both runs' means, per-category tables and latency percentiles, and the document
     count.
  2. Each of the three adoption conditions is evaluated explicitly.
  3. If adopted, `brain eval --baseline ci --diff --fail-below` exits 0 with the reranker on, after
     the re-record.
- **Test plan:** no new automated test; the recorded commands and their outputs are the deliverable.
- **Eval:** this ticket is the eval.
- **Depends on:** RM-14b.
- **Size:** S.
- **Risks:** the document set moves between the two runs. Both run in one session after `brain
  status`, and the count goes in the PR.
- **Decisions:** RM-14c.D1, the 0.02 bar: **0.02**. For a golden row with one judged document,
  moving it from rank 2 to rank 1 raises that row's MRR from 0.5 to 1.0 and its nDCG@5 from about
  0.63 to 1.0, which on 31 rows moves the means by about 0.016 (MRR) and 0.012 (nDCG@5). So 0.02 asks
  for more than one such improvement, net; a single-row move is within judgment noise.

#### RM-15 — Saved searches and watch lists, notified through the daily brief

- **Source:** roadmap 4.4, F13 ("accepted in principle, cut for scope"), BL-9; backlog rank 8.
- **Goal:** `brain watch add "<query>"` saves a search; each morning's brief lists documents that
  newly match it.
- **Scope in:** first, the extraction of `brief`'s two-audience sequence out of `cli.py` into a new leaf
  `src/brain/brief_publish.py` (the prerequisite RM-16 and RM-19 also build on); then migration
  029, `src/brain/saved_search.py` (core), `src/brain/cli_watch.py` (a `watch` sub-app, through
  RM-06), `src/brain/mcp_tools/saved_search.py`, and a section in `brief.py`. **Scope out:**
  per-ingest push; macOS notifications; a UI panel.
- **Design:**
  - **Step 1, the extraction.** In `cli.py`'s `def brief` (read at `02c2abc`) the `--wiki` work is
    interleaved with enrichment, so it cannot move alone. The function opens one connection and
    assembles the permissive payload (`exclude_confidential=False`) and, under `--wiki`, the gated
    one (`exclude_confidential=True`); closes the connection; computes `withheld = published is not
    None and published != data` while both still carry `suggestions=[]`; then, unless `--no-enrich`,
    calls `suggest_next_steps(data, cfg)` and gives the gated payload its own call only when
    `withheld`, else the same suggestions; and finally writes the page with `write_brief_to_vault`
    before the `--json` early return. That **whole two-audience sequence** moves into
    `brief_publish.build_brief(cfg: Config, *, on_date: date, since_hours: int, todo_since_days: int,
    enrich: bool, wiki: bool, connect: Callable[[str], AbstractContextManager[psycopg.Connection[Any]]]
    = db.connect) -> tuple[BriefData, Path | None]`, returning the terminal payload and the written
    page's path (or `None`). The connection opener is injected so tests use the 5434 database and
    RM-15's watch check (below) runs on the same connection. `cli.py` keeps argument parsing, the
    call, and the printing (`emit_json` or `_print_brief` plus the published-page note). The
    two-audience comment moves with the code. The move is behaviour-identical, proven by the
    existing `brief` tests passing unchanged, including `--json --wiki` still writing the page.
  - **Data model (`029_saved_searches.sql`, additive):**

    ```sql
    CREATE TABLE IF NOT EXISTS saved_searches (
      id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      name             TEXT NOT NULL UNIQUE,
      query            TEXT NOT NULL,
      filters          JSONB NOT NULL DEFAULT '{}',   -- source, tag, person, kind, without_tag
      mode             TEXT NOT NULL DEFAULT 'lexical' CHECK (mode IN ('lexical', 'semantic')),
      enabled          BOOLEAN NOT NULL DEFAULT TRUE,
      created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS saved_search_hits (
      saved_search_id  UUID NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
      document_id      UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      notified_at      TIMESTAMPTZ,
      PRIMARY KEY (saved_search_id, document_id)
    );
    CREATE INDEX IF NOT EXISTS idx_saved_search_hits_unnotified
      ON saved_search_hits (saved_search_id) WHERE notified_at IS NULL;
    ```

  - **Matching (the F5 WHERE-builder reuse).** A check runs `hybrid_search` through `run_search`,
    with the saved filters **plus `updated_after = last_checked_at − 1 h`** (the F9 filter, migration
    025). `updated_at` is set on insert and on a real edit, and not by maintenance jobs, so the window
    is exactly "new or changed knowledge". Without the window, new documents would compete with every
    old match for the 50 candidate slots and could be crowded out. Lexical mode passes
    `fts_only=True`. **Semantic mode** passes `explain=True` (the only way `vector_cosine` is
    populated) and keeps hits whose `explain.vector_cosine` is at least `BRAIN_WATCH_MIN_COSINE`
    (0.45, `settings/watch.py`). If a semantic check falls back to text-only, it records **no** hits
    and does **not** advance `last_checked_at`, so the next check retries the same window; the brief
    says "<name>: skipped, embedder unavailable".
  - **Concurrency.** One check is one transaction: `SELECT … FROM saved_searches WHERE id = %s FOR
    UPDATE` (a manual `brain watch check` and the 07:00 run serialize on the row), the search, then
    `INSERT … ON CONFLICT (saved_search_id, document_id) DO NOTHING RETURNING document_id` (the
    returned ids are the new hits, so a racing check cannot report a hit twice), then `UPDATE …
    SET last_checked_at = GREATEST(last_checked_at, %s)` with the check's start time, so it never
    moves backwards. Lexical checks are fast; a semantic check holds the row lock for one embed,
    which is acceptable.
  - **Adding** a saved search seeds `saved_search_hits` with its current matches, marked notified,
    so the first brief is not a flood.
  - **CLI** (`brain watch …`): `add NAME QUERY [--source] [--tag] [--person] [--kind]
    [--without-tag] [--semantic]`; `list [--json]`; `check [NAME] [--json] [--peek]` (`--peek`
    reports without recording); `hits [NAME] [--since 7d] [--json]` (recent hits by `first_seen_at`,
    notified or not); `rm NAME`; `pause NAME`; `resume NAME`. Filters are validated with the
    functions `brain search` uses (`validate_source_kind`, `resolve_person_to_keys`).
  - **Brief.** `BriefData` gains `watch_hits: list[WatchHit]` (default empty) and its `to_dict` key.
    Every `brain brief` (with or without `--wiki`) runs a check of every enabled watch **first**,
    inside `build_brief`, on its connection, **before** either payload is assembled; the check
    records new hits but marks nothing. The two payloads then read different sets:
    - the **terminal** payload (permissive lens) lists every hit whose `first_seen_at` falls inside
      the brief's own window (`since_hours`), **whether or not it is notified**, so a hit the 07:00
      run already published still shows in a later terminal `brain brief` that day;
    - the **published** payload (gated lens, `--wiki` only) lists the **unnotified** hits, at most 5
      per watch plus "and N more (`brain watch hits <name>`)". After the page is written,
      `build_brief` marks **every** hit it found unnotified as notified, including the ones beyond
      the display cap and the confidential ones the gated page withheld; the terminal brief and
      `brain watch hits` still show them.
    MCP `brain_brief` runs no check; it reads the terminal set through its MCP lens.
  - **MCP** (`mcp_tools/saved_search.py`): `brain_watch_list()`, `brain_watch_add(name, query, …)`,
    `brain_watch_check(name=None, include_confidential=False)` (peek only: an agent never consumes
    the owner's notifications), `brain_watch_remove(name, confirm=False)`.
- **Security / privacy:** saved queries are the owner's own text, stored locally. Hit titles go
  through the F6 lens on every egress path (MCP, the vault note, RM-16's delivery).
- **Ceiling:** `cli.py` shrinks (the `brief --wiki` extraction; row updated). `brief.py` 315 → about
  360. No over-ceiling file grows.
- **Acceptance criteria:**
  1. The extraction is behaviour-identical: every existing `brief` test passes unchanged.
  2. `add` seeds existing matches; an immediate `check` reports zero new hits.
  3. Ingesting a matching synthetic document makes the next `check` report it once, and only once.
  4. An edited old document that newly matches is reported; an old match is never re-reported.
  5. Filters apply (a `--source` mismatch is not a hit).
  6. `brief --wiki` lists unnotified non-confidential hits (at most 5 per watch), marks all found
     hits notified, including the withheld and the over-cap ones; a second `--wiki` run lists none.
  7. A hit marked notified by a `--wiki` run still appears in a later plain `brain brief` whose
     window covers its `first_seen_at`, and a confidential hit appears there too.
  8. Two concurrent checks of one watch report each new hit exactly once, and `last_checked_at` is
     the later of the two start times.
  9. A semantic check that falls back records nothing and leaves `last_checked_at` unchanged.
  10. MCP `brain_watch_check` never marks; lexical mode works with the embedder down.
- **Test plan:** DB tests for the core (seed, diff, window, filters, mark and peek, the race with
  two connections and a barrier); CLI tests; brief tests (section, gating, marking); MCP tests; a
  migration test (idempotent re-apply).
- **Eval:** none.
- **Depends on:** RM-06, RM-09.
- **Size:** M.
- **Risks:** a very broad watch produces a long section; the 5-per-watch cap bounds it.
- **Decisions:** RM-15.D1, per-ingest push: **no**; the daily brief is the cadence. RM-15.D2,
  lexical default: **yes**, deterministic and embedder-independent. RM-15.D3, mark withheld and
  over-cap hits: **yes** (above).

#### RM-16 — Brief delivery: email to self (or a private Slack webhook), opt-in

- **Source:** roadmap 4.1 ("Delivery: … email yourself, post to Slack, or all three"); backlog rank
  10, "a product decision". This ticket makes it a setting with a recommended default.
- **Goal:** The 07:00 brief can arrive in the owner's inbox, without anyone opening a terminal.
- **Scope in:** new `src/brain/brief_delivery.py`; `src/brain/settings/brief_delivery.py`; a call in
  `brief_publish.build_brief` (RM-15); the subject exclusion in `ingest/gmail.py`'s query
  builder; a `brain doctor` row; docs. **Scope out:** HTML email; per-section toggles beyond the two
  below; any edit to `cli.py` or the daemon script.
- **Design:**
  - **Channel recommendation.** **Email to the owner's own address through the existing `gws` CLI**
    (`gws gmail +send --to … --subject … --body …`). It needs no new credential store, the recipient
    is fixed to `BRAIN_USER_EMAIL`, and nothing new is installed. **Slack** is supported through an
    **incoming webhook bound to a private channel or a DM**, configured by the owner. The `#team`
    channel already used for status pings is **rejected** as a destination: it is shared, and a
    brief names the owner's documents.

    ```python
    class DeliveryChannel(Protocol):
        name: str
        def send(self, *, subject: str, body: str) -> None: ...   # raises DeliveryError

    class GwsEmailChannel:       # runner injected (the gmail.py runner shape), timeout 30 s,
                                 # --to is ALWAYS settings.recipient == cfg.user_email
    class SlackWebhookChannel:   # httpx POST {"text": body}, timeout 10 s, URL from settings (repr=False)

    def render_delivery(brief: BriefData, *, include_todos: bool) -> tuple[str, str]: ...
    def deliver_brief(brief: BriefData, settings: BriefDeliverySettings,
                      channel: DeliveryChannel | None) -> DeliveryOutcome: ...
    ```

  - **What is sent:** the **gated** payload only (the one `build_brief` writes, built with
    `exclude_confidential=True`). By default: the date, counts, capture titles, pinned titles, watch
    hits (RM-15), new entities (RM-17), and "open `brain brief` for detail". **Todo text is
    body-derived** and is sent only when `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true`.
  - **The subject is fixed:** `[brain brief] YYYY-MM-DD`. Self-sent mail would otherwise come back
    in through `brain ingest-gmail --since …`, so `ingest/gmail.py`'s query builder appends
    `-subject:"[brain brief]"` to every Gmail query it builds.
  - **Settings:** `BRAIN_BRIEF_DELIVERY` = `none` (default) | `email` | `slack`;
    `BRAIN_BRIEF_SLACK_WEBHOOK_URL` (a secret; it must start `https://hooks.slack.com/`, else
    `ConfigError`); `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS` = false. `email` with no `BRAIN_USER_EMAIL`
    is a `ConfigError` naming the variable.
  - **Wiring:** `build_brief` calls `deliver_brief` after the vault page is written, when `wiki`
    is set and delivery is configured. Nothing changes in `cli.py` or `_brain-brief-fg.sh`.
  - **Failure:** a delivery error never fails the brief: the page is already written, one class-only
    WARNING is logged (`describe_failure`), and the exit code is 0. `gws`'s scope for sending is not
    verified on this machine (`ingest-gmail` only reads), so an authorization failure from `gws` is
    mapped to a WARNING that names the fix, `bin/gws-reauth`. `brain doctor` reports the channel and
    whether its prerequisite exists (`gws` on `PATH`; the webhook set), never the URL.
- **Security / privacy:** §2.5 in full. The recipient cannot be anyone but the owner. The webhook URL
  is a secret and is never logged.
- **Ceiling:** `ingest/gmail.py` (669) +≤ 5; `doctor_runtime.py` budget (§2.2). No over-ceiling file.
- **Acceptance criteria:**
  1. With delivery `none`, nothing is sent and nothing is printed about it.
  2. With `email`, the injected runner receives exactly one `gws gmail +send` call whose `--to` is
     `BRAIN_USER_EMAIL`, whose subject is `[brain brief] <date>`, and whose body contains no
     confidential title and no todo text by default.
  3. With `slack`, an `httpx.MockTransport` receives one POST; a non-Slack URL is a `ConfigError`.
  4. A failing channel leaves the page written and exits 0 with one WARNING; an authorization
     failure's WARNING names `bin/gws-reauth`.
  5. No log line or error contains the webhook URL (a sentinel test).
  6. Every Gmail query `ingest/gmail.py` builds excludes the brief's subject.
- **Test plan:** pure render tests; channel tests with an injected runner or transport; settings
  tests; a `build_brief` test with a fake channel; the sentinel test; a gmail query-builder test.
- **Eval:** none.
- **Depends on:** RM-08, RM-15 (`brief_publish`); best after RM-17 so its section exists.
- **Size:** S.
- **Risks:** spam filtering of self-sent mail; the fixed subject lets a filter label it.
- **Decisions:** RM-16.D1, channel: **email via `gws`, off by default** (above).

#### RM-17 — The brief's "new this week" entities section

- **Source:** roadmap 4.1, the missing "new entities or projects newly mentioned" bullet.
- **Goal:** The brief names people, organisations and projects that first appeared in the corpus in
  the last 7 days.
- **Scope in:** a new leaf `src/brain/brief_entities.py`, a field and a section in `brief.py`.
  **Scope out:** topics and tools (too noisy for a daily list); any graph write.
- **Design:** `new_entities(conn, *, tenant_id, since: datetime, exclude_confidential: bool, limit:
  int = 10) -> list[NewEntity]`. `graph_entity_mentions` carries no timestamp (`012_graphrag.sql`),
  and `graph_entities.created_at` resets when `graph_rag/aggregates.py` deletes an orphaned entity
  that is later re-created, so `created_at` is used **only as a prefilter** (`created_at >= since`).
  "New" is decided by an aggregate over the entity's mentioning documents: `MIN(d.ingested_at)` over
  non-draft documents, computed **with the confidential lens inside the same aggregate** (when
  `exclude_confidential`, confidential documents are excluded before the `MIN`), must be `>= since`.
  An entity known only from confidential documents therefore has no qualifying rows and is not
  listed. `entity_type IN ('person', 'org', 'project')`. `BriefData` gains `new_entities`; the
  section is omitted when the graph is disabled or the AGE tables are absent (a cheap existence
  check), so a stock install's brief is unchanged.
- **Security / privacy:** entity names only; the lens is applied in the aggregate, not after it.
- **Ceiling:** `brief.py` +≤ 25.
- **Acceptance criteria:**
  1. An entity first mentioned by a document ingested yesterday is listed.
  2. An entity whose earliest mentioning document was ingested 10 days ago is not, even if its
     `graph_entities` row was re-created yesterday.
  3. An entity mentioned only by confidential documents is absent from the gated payload and present
     in the permissive one.
  4. Graph disabled → no section, no error.
- **Test plan:** DB tests on the AGE test instance (5434); brief tests.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** the prefilter misses an entity created before `since` whose older mentioning documents
  were all deleted since; accepted, because it only delays a name, it never leaks one.
- **Decisions:** RM-17.D1, "first appeared" by `ingested_at` or by the document's own date
  (`coalesce(sent_at, ingested_at)`): **`ingested_at`**. The section reports what is new to the
  brain; dating by `sent_at` would hide an old thread imported this week, which is exactly what the
  owner has not yet seen.

#### RM-18 — The Agent tab (three sub-tickets)

- **Source:** F14 §1.1 ("Agent — deferred to v3"), the Ingest spec §4.7 and Q12/Q13, backlog §2.1
  and rank 9.
- **Goal:** Ask a question in the browser, watch the plan → retrieve → reflect → synthesize steps
  progress, cancel it, and read a cited answer whose citations open in the Notes tab.
- **Fields shared by the three sub-tickets:** **Security / privacy:** F14 §1.1's hardening pass
  (RM-18b) and decisions A5–A8 below. **Ceiling:** the `ask.py` budget (§2.2); no over-ceiling file.
  **Eval:** none; `ask` is not eval-gated. **Depends on:** RM-02, RM-10, RM-11. **Size:** L in total
  (M + M + S). **Risks:** an ask holds the single worker for up to a few minutes; the panel says so,
  and cancel frees it. **Decisions:** A1–A10 below.

**Architecture, in the house style.**

| # | Question | Decision |
|---|---|---|
| A1 | Where does an ask run? | On the Ingest tab's `JobRegistry` worker, as job kind `"ask"`. One worker for ingest and ask (Ingest spec Q13): this machine cannot hold two chat-model runs and an embed at once. An ask queued behind an ingest says "waiting for the ingest job to finish". |
| A2 | Transport | **Polling** `GET /api/jobs/{id}?after=<seq>` (Ingest spec §4.3). **No NDJSON stream in v1.** `ask`'s steps call `chat.chat_json`, which returns whole JSON objects (structured output), so there is no token stream to forward; a stream would deliver the same five to eight step events polling does, at a 1 s granularity that is honest for 10–90 s runs. The `/stream` endpoint the Ingest spec sketches stays the upgrade path if synthesis ever streams tokens. |
| A3 | Progress | `ask.ask` and `ask_no_loop` gain `on_step: Callable[[str, int], None] \| None = None`, called with `("plan" \| "retrieve" \| "reflect" \| "synthesize", iteration)` before each step. The runner maps it to `handle.progress(...)`, calls `handle.check_cancelled()`, and checks the job deadline (A10) in it. |
| A4 | Cancellation | Cooperative, at every step boundary and before every embed (the Ingest spec's progress-embedder wrapper is reused). Ask writes nothing to the corpus (only `interactions` rows at the end, through `ui/telemetry.py`), so `enter_commit()` is never called and a cancel is honoured until the result is recorded. `ask.py` has no `except` clause at `02c2abc`, so `JobCancelled` (a `BrainError`) propagates out of `ask.ask`; a test pins that. |
| A5 | Conversation state | **Client-side only**, in `sessionStorage` (read in `try`): the turns of this tab, capped at 20. **No server persistence**: the same reasoning as the Ingest spec's D4, plus answers quote confidential bodies. A follow-up sends at most the previous **3** turns' questions and answers (server-capped at 2,000 tokens, oldest dropped first) to the **plan step only** (RM-18c). |
| A6 | Where it is allowed | Loopback-bound, writable servers, like ingest (Ingest spec D8). On `--read-only` or a LAN bind the tab is disabled and says why. |
| A7 | Confidential lens | `exclude_confidential = not ctx.serve_confidential_bodies`, passed to `ask.ask`. On a loopback server that serves confidential bodies, an answer may draw on them, as the CLI's does. |
| A8 | Rendering model output | **`textContent` only.** Markdown is not rendered in v1. RM-18b gives the hardening rules. |
| A9 | Modes | `hybrid` only in v1. Graph modes need `connect_age` and compete with the watcher's graph lane for the per-tenant write lock (PR #53); they come later on request. |
| A10 | Retention of results | Ask results quote document bodies, including confidential ones on a loopback server, so they are kept for less time than ingest results: `retain_for` is **10 minutes** for kind `"ask"` (RM-10's per-kind override), and ask jobs are **left out of the `GET /api/jobs` list view**; a client reads an ask only by the id it submitted. |

##### RM-18a — Agent tab backend

- **Source / goal:** the parent's; this sub-ticket is the server half of A1–A7 and A10.
- **Scope in:** `ask.py` (`on_step`); new `src/brain/ui/ask_service.py` (builds the runner; not a
  mutator, so the Ingest spec's two-mutator rule is unchanged: interaction rows go through
  `ui/telemetry.py`, which already writes UI telemetry); new `src/brain/ui/routes_ask.py`; the job
  list filter. **Scope out:** the front end (RM-18b); follow-ups (RM-18c); graph modes.
- **Design:**
  - Routes: `GET /api/ask/config` (enabled, reason, maximum question length, modes) and `POST
    /api/ask` with `{question: str ≤ 2,000 characters, no_loop: bool}` → 202 with the
    `JobSnapshot`. Status and cancel reuse `/api/jobs/{id}` and `/api/jobs/{id}/cancel`.
  - Result payload: `AskResult.to_dict()` (including RM-02's fallback keys), each citation's
    `snippet` cut to `cfg.snippet_max_chars`, and `elapsed_ms`.
  - Error mapping: `describe_ask_failure` (RM-02) → the job's `error = {code, message}`, with codes
    `chat_unavailable`, `context_overflow`, `output_truncated`, `synthesis_failed`,
    `embedder_unavailable`, `database_error`, `timeout` and `internal_error`. Messages are fixed per
    code, as the Ingest spec's §4.5 requires.
  - **Deadline:** each chat call keeps `cfg.ask_timeout_seconds`. The job as a whole is capped at
    `ask_max_iterations × 3 × ask_timeout_seconds`; the deadline is checked in `on_step` and before
    each chat call (the runner wraps the injected `ChatJson`), and passing it ends the job `failed`
    with code `timeout`.
  - Retention and listing per A10.
- **Security / privacy:** A6, A7, A10.
- **Ceiling:** `ask.py` +≤ 25.
- **Acceptance criteria:**
  1. A submitted ask with a fake `ChatJson` reaches `succeeded` with the payload.
  2. The events name each step, in order.
  3. A cancel during retrieve ends `cancelled` with no interaction row written.
  4. Chat down → `chat_unavailable` with the fixed message.
  5. A fake chat that sleeps past the deadline ends `timeout`, checked at the next step or chat call.
  6. `--read-only` → 403 `read_only` from the middleware; a LAN bind → 403 `ask_requires_loopback`.
  7. A confidential document is cited only when the server serves confidential bodies.
  8. An ask job is absent from `GET /api/jobs` and gone from `GET /api/jobs/{id}` after its retention.
- **Test plan:** runner tests on `run_next()` (no thread, no sleep; the deadline uses an injected
  clock); route tests on the 5434 database; security tests extending `tests/test_ui_security.py`.
- **Eval:** none.
- **Depends on:** RM-10, RM-02.
- **Size:** M.
- **Risks:** an ask queued behind a long PDF ingest waits; the panel shows the queue position.
- **Decisions:** RM-18a.D1, graph modes: **not in v1** (A9). RM-18a.D2, ask retention: **10
  minutes, unlisted** (A10).

##### RM-18b — Agent tab front end and the model-output hardening pass

- **Source / goal:** the parent's; this sub-ticket is the browser half and the hardening pass F14
  §1.1 requires.
- **Scope in:** `ui/static/js/ask.js`, `ui/static/css/ask.css`, the `#tab-agent` panel in
  `index.html` (it stops being a `class="shell deferred-tab" hidden` section, and
  `tests/test_ui_tabs_static.py`'s copy expectations change in the same PR, as RM-11 does for
  Ingest), reuse of `jobs.js` (RM-11), browser tests, the `ci.yml` roster entry, and the Agent tab's
  docs: `docs/cli-reference.md` (the `brain ui` section) and `docs/guides/brain-ui-on-your-phone.md`
  (the Agent tab is unavailable over `--lan`). **Scope out:** markdown rendering of answers.
- **Design — the hardening rules, each tested:**
  1. Model text (answer, sub-queries) is inserted only with `textContent` or `createTextNode`.
     `check_single_innerhtml` still finds exactly one `innerHTML` in the whole front end, and a new
     static test fails if `ask.js` contains `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
     `document.write`, `eval`, `new Function` or `setAttribute("on…")`.
  2. Citation markers are parsed in JS with `/\[(\d{1,2})\]/g` over the answer **string**; each match
     whose number exists in the server's `citations` list becomes an `<a>` built with
     `createElement`, its `href` set to `?id=` + `encodeURIComponent(id)` where `id` must match the
     UUID pattern, and its text the marker. Anything else stays text. The server's `_sanitize_answer`
     already drops out-of-range markers; the client does not rely on that.
  3. No model-supplied string ever becomes an attribute, other than through the UUID-checked `href`.
  4. The CSP is unchanged (`script-src 'self'`, no `unsafe-inline`), so even a missed sink cannot
     run inline script.
- **Security / privacy:** this sub-ticket is the hardening pass. **Security review** with
  `ecc:security-reviewer` on opus, as F14 §1.1 requires.
- **Ceiling:** none (new files; `index.html` stays small).
- **Acceptance criteria:**
  1. A stubbed job payload whose answer contains `<img src=x onerror=…>`, `<script>`, `javascript:`
     and `[99]` renders those characters as visible text, creates no `img`, `script` or extra `a`
     element, and raises no dialog.
  2. `[1]` becomes a link that opens the cited note in the Notes tab.
  3. The cancel button and the "waiting for the ingest job" state render.
  4. The tab is no longer marked deferred, and the static tab test reflects it.
  5. The CSP header is unchanged.
- **Test plan:** the browser suite `tests/test_ui_browser_agent.py` is **hermetic**, like the rest of
  the CI browser job (`tests/test_ui_browser.py`, "WHY IT IS HERMETIC"): it stubs `/api/ask/config`,
  `/api/ask` and `/api/jobs/{id}` through `page.route`, with the malicious payloads above. The
  end-to-end test that injects a fake `ChatJson` through the `UiContext` and checks the real job
  payload is a **route test** (5434 database), not a browser test. The CSP-unchanged assertion goes
  in `tests/test_ui_security.py`. The static sink test goes beside `check_single_innerhtml`.
- **Eval:** none.
- **Depends on:** RM-18a, RM-11.
- **Size:** M.
- **Risks:** a later edit adds a client-side markdown renderer; the static sink test fails first.
- **Decisions:** RM-18b.D1, markdown in answers: **not in v1**; if it is wanted later, it is
  rendered server-side through `render.py`'s hardened path, which first needs room under its 799
  lines (§2.2).

##### RM-18c — Follow-up questions

- **Source / goal:** the parent's (A5); a follow-up question keeps its context.
- **Scope in:** `ask.ask` gains `history: Sequence[AskTurn] = ()`; `POST /api/ask` gains `history:
  [{question, answer}]` (≤ 3 turns); the "Follow up" control in `ask.js`. **Scope out:** MCP
  `brain_ask` (unchanged); history in synthesis.
- **Design:** `_plan_prompt` includes the history as "Earlier in this conversation: …" so the
  planned sub-queries are self-contained; synthesis sees only the current question and the
  retrieved documents. The server re-validates the 3-turn, 2,000-token cap (oldest dropped first).
  The tab sends history only when the user presses "Follow up".
- **Security / privacy:** history comes from the client, so the server re-caps it and treats it as
  untrusted prompt input; it never reaches synthesis.
- **Ceiling:** `ask.py` +≤ 50.
- **Acceptance criteria:**
  1. With a fake chat, a follow-up's plan prompt contains the previous question and answer,
     truncated oldest-first at the cap.
  2. A fresh ask sends no history.
  3. MCP `brain_ask` is unchanged.
- **Test plan:** unit tests on `_plan_prompt` with and without history; a route test for the server
  cap; a hermetic browser row for "Follow up".
- **Eval:** none.
- **Depends on:** RM-18b.
- **Size:** S.
- **Risks:** history steers the plan off-topic; only the plan step sees it.
- **Decisions:** RM-18c.D1, history in synthesis too: **no**; it would let an earlier answer's
  claims enter a new answer without a citation.

### Wave 5 — Corpus quality

The backlog's ranks 11 to 15 and 17. RM-20 and RM-22b are measured on top of whatever RM-14c
adopted, which is why both depend on it.

#### RM-19 — Near-duplicate detection (flag, never refuse)

- **Source:** roadmap 3.4; backlog rank 11; the Ingest spec's risk R4 (the same file ingested by the
  CLI and by upload becomes two documents).
- **Goal:** The owner sees pairs of documents that are near-copies (forwarded emails, re-sent drafts,
  a file ingested twice by two paths) and can confirm or dismiss each pair.
- **Scope in:** migration 030; new leaf `src/brain/near_dup.py` (pure MinHash); new
  `src/brain/near_dup_store.py` (SQL); `src/brain/cli_dedup.py` (`brain dedup …`, through RM-06);
  `src/brain/mcp_tools/dedup.py`; a `brain dedup scan` line in
  `src/brain/templates/bin/_brain-brief-fg.sh`; a read-only line in `brief.py`; the Ingest tab's
  `possible_duplicates` key (RM-10). **Scope out:** refusing ingest; deleting or merging documents;
  collapsing duplicates in search results (RM-19.D2).
- **Design:**
  - **Algorithm (no new dependency).** Word 5-gram shingles over the lowercased,
    whitespace-collapsed body, each hashed to 64 bits with `hashlib.blake2b(digest_size=8)`; a
    128-value MinHash signature from fixed-seed universal hashes modulo the Mersenne prime 2⁶¹−1; 32
    bands of 4 rows for LSH. Each **band hash** is `blake2b(band_index.to_bytes(1) + the band's four
    values)`, so equal values in different bands never collide, and every 64-bit digest is converted
    to a **signed** int64 (`int.from_bytes(d, "big", signed=True)`) to fit `BIGINT`. The S-curve
    threshold is about (1/32)^(1/4) ≈ 0.42, so candidates are found well below the cut; each candidate
    pair is then verified by its signatures' estimated Jaccard ≥ **0.85** (the roadmap's figure).
    Documents under 50 tokens are skipped (templates and stubs collide), and two documents of one
    Gmail thread are never paired (thread assembly already handles them).
  - **Data model (`030_near_duplicates.sql`):**

    ```sql
    CREATE TABLE IF NOT EXISTS document_minhash (
      document_id   UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
      content_hash  TEXT NOT NULL,          -- recompute when it differs from documents.content_hash
      signature     BIGINT[] NOT NULL,      -- 128 values
      bands         BIGINT[] NOT NULL,      -- 32 band hashes
      computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_document_minhash_bands ON document_minhash USING GIN (bands);
    CREATE TABLE IF NOT EXISTS near_duplicates (
      doc_a       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      doc_b       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      similarity  REAL NOT NULL,
      status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'dismissed', 'confirmed')),
      detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (doc_a, doc_b),
      CHECK (doc_a < doc_b)
    );
    ```

  - **When it runs.** Never inside `ingest_document` (an over-ceiling file, and a write path that
    must stay fast). `brain dedup scan [--all] [--limit N]` computes missing or stale signatures and
    records new pairs. The brief daemon script, `_brain-brief-fg.sh`, gains its own line `"$PY" -m
    brain dedup scan --limit 500 || true` before `brain brief --wiki`, so the scan is a separate
    process whose failure cannot stop the brief, and `brief_publish` (RM-15) is not touched. The
    Ingest tab's job calls `near_dup_store.check_document(conn, doc_id)` after a successful ingest and
    fills `possible_duplicates`.
  - **Surfaces:** `brain dedup list [--status open|dismissed|confirmed] [--json]`, `brain dedup
    dismiss A B`, `brain dedup confirm A B` (a label only; deleting stays `brain rm`); MCP
    `brain_dedup_list(include_confidential=False)` (read-only, with the lens on both ends of a pair,
    as `connect.iter_suggestions` gates both endpoints); the brief's read-only "Possible duplicates
    (N)" line, which counts open pairs through the gated lens.
- **Security / privacy:** signatures are irreversible hashes of shingles; titles go through the F6
  lens on MCP and in the gated brief.
- **Ceiling:** none (new leaves; `brief.py` +≤ 10). The daemon script change needs the reinstall in
  DoD item 15.
- **Acceptance criteria:**
  1. Two synthetic documents differing by one sentence in 400 words are paired; two unrelated ones
     are not; a 30-token stub is skipped.
  2. Equal value runs in two different bands produce different band hashes.
  3. Editing a document recomputes its signature and drops a pair that no longer verifies.
  4. Deleting a document deletes its pairs (cascade).
  5. A dismissed pair is never re-opened by a later scan.
  6. Signatures are deterministic across runs (fixed seeds), and every stored value fits `BIGINT`.
  7. A failing `brain dedup scan` in the daemon script does not stop `brain brief --wiki`.
  8. The full first scan over a 2,000-document synthetic corpus runs in under 5 minutes on the
     owner's machine (measured and recorded in the PR; a budget, not a CI assertion).
- **Test plan:** pure tests (shingles, signature, band hashes, signed conversion, estimate vs exact
  Jaccard on synthetic pairs) at ≥ 95%; DB tests for scan, stale recompute, cascade and status; CLI
  and MCP tests; a brief test; a test of the daemon script's line order and `|| true`.
- **Eval:** none (no ranking change).
- **Depends on:** RM-06; RM-10 for the UI key (optional).
- **Size:** M.
- **Risks:** meeting transcripts with long shared boilerplate pair falsely. Mitigated by the 0.85
  bar, the 50-token floor, and dismiss.
- **Decisions:** RM-19.D1, flag or refuse at ingest: **flag** (a false positive would lose a
  document). RM-19.D2, collapse near-duplicates in search: **not in this ticket**; it is a ranking
  change and needs its own eval run. RM-19.D3, where the daily scan runs: **its own line in the
  daemon script**, not inside the brief.

#### RM-20 — Document-level embeddings as a re-scoring stage (default OFF)

- **Source:** roadmap 3.6; backlog rank 12 ("re-evaluate after the reranker").
- **Goal:** A "what is this document about" signal, from embedding each document's title and
  summary, that can lift a document whose best chunk ranks poorly but whose summary fits the query.
- **Scope in:** migration 031; `embedding_targets.EMBEDDING_TARGET_ALLOWLIST` gains
  `("document_embeddings", "embedding")`; a new `db.ensure_relational_embedding_columns(conn,
  embedder)` that reconciles `chunks.embedding` and `document_embeddings.embedding`, called in place
  of the single-target call in `cli.py`'s `init`, `backup/restore.py` and `demo/__init__.py`; new
  `src/brain/doc_embeddings.py` (backfill), `src/brain/search_doc_leg.py` (the stage),
  `src/brain/cli_docembed.py` (`brain docembed backfill [--stale] [--limit]`, through RM-06),
  `src/brain/settings/doc_leg.py`; a two-line alias in `search.py`. **Scope out:** adding candidates
  (RM-20.D1); HNSW on the new column (RM-20.D2).
- **Design:**
  - **Data model (`031_document_embeddings.sql`):** `document_embeddings(document_id UUID PRIMARY KEY
    REFERENCES documents(id) ON DELETE CASCADE, embedding vector(1024), basis TEXT NOT NULL CHECK
    (basis IN ('title_summary', 'title_head')), basis_hash TEXT NOT NULL, embedder TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`. `vector(1024)` is the house starting dimension.
  - **Dimension reconciliation.** Allowlisting the target alone reconciles nothing: `init` calls
    `ensure_embedding_column` once per target explicitly (`cli.py`, `init`), and so do
    `demo/__init__.py` (twice) and `backup/restore.py`. `ensure_relational_embedding_columns`
    reconciles both relational targets with the same semantics as `chunks.embedding` today (resize
    when empty; refuse with the destructive-reset hint when rows exist at another dimension), and
    each of those three call sites switches to it (net zero lines). The graph target keeps its
    separate, AGE-gated call.
  - **What is embedded:** `title + "\n" + summary` when `documents.summary` exists, else `title +
    "\n" +` the first 512 tokens of content. `basis_hash` is the SHA-256 of that text. `--stale`
    re-embeds rows whose basis changed **or** whose `embedder` differs from the active embedder.
    Under a hosted embedder, **confidential documents are skipped**, mirroring the F6 egress veto in
    `_embed_chunks`; they have no vector.
  - **The stage re-scores candidates; it adds none.** For each candidate from the pipeline it reads
    the document vector, computes the cosine to the query vector, ranks the candidates by that
    cosine, and fuses the two ranks with `rank_fusion.rrf_contribution` (k = 60), times
    `BRAIN_DOC_LEG_WEIGHT`. A candidate **with no vector** (not yet backfilled, or confidential under
    a hosted embedder) gets no doc-leg contribution and keeps its pipeline rank relative to the
    others: neutral, never demoted for being confidential. Because only documents the hybrid search
    already returned are re-scored, every filter and the oracle-closing sensitivity exclusion hold by
    construction. The query vector comes from the query-embed LRU in `search.py`, through a public
    alias `cached_query_embedding` (a two-line, recorded growth), so the stage costs no second embed.
    `signature()` returns `{"doc_leg_weight": weight}`.
  - **Settings:** `BRAIN_DOC_LEG=false` (default), `BRAIN_DOC_LEG_WEIGHT=1.0`.
- **Security / privacy:** §2.5; the F6 egress veto is mirrored for hosted embedders.
- **Ceiling:** `search.py` +2, recorded in both places. `cli.py` and `backup/restore.py` net zero
  (one call replaced by another; hops recorded).
- **Acceptance criteria:**
  1. Off: outputs and `config_signature` unchanged.
  2. On, with a synthetic corpus where one document's summary matches and its chunks barely do, that
     document moves up; filters still exclude what they excluded.
  3. A confidential document is never embedded under a hosted fake embedder, and keeps its rank
     (neither promoted nor demoted) when the stage runs.
  4. `--stale` re-embeds a row whose `embedder` column names another backend.
  5. `brain init` on a qwen3-dimension test database resizes the new column; `brain restore` and the
     demo seed reconcile it too.
- **Test plan:** pure fusion tests; DB tests for backfill, stale (basis and embedder), egress skip,
  and dimension reconciliation through `init`, restore and the demo; stage tests per surface; an
  eval-runner test with the stage.
- **Eval:** measured on the owner's machine on top of the pipeline RM-14c left, with RM-14c's
  adoption rule; adopted → `ci.json` re-recorded (trigger "ranking change (document leg adopted)");
  otherwise recorded and kept off.
- **Depends on:** RM-09, RM-14c.
- **Size:** M.
- **Risks:** a summary-dominated signal over-promotes long documents; the weight knob and the eval
  bound it.
- **Decisions:** RM-20.D1, re-score or add candidates: **re-score**; a recall-adding leg needs the
  predicate inside the stage protocol, and is worth building only if a measurement shows the right
  documents are missing from the candidate set rather than mis-ordered in it. RM-20.D2, an HNSW
  index: **no**; a re-score stage reads at most 50 vectors by primary key, and an index would serve
  only a future recall-adding leg.

#### RM-21 — Tag merge, rename and suggested merges

- **Source:** roadmap 7.4; backlog rank 13.
- **Goal:** `brain tags rename old new`, `brain tags merge a b --into c` and `brain tags
  suggest-merges`, writing every place a tag lives.
- **Scope in:** move the synonym-mapping engine of `brain backfill normalize-tags` (`cli.py`:
  `_load_tag_mapping`, `_apply_tag_mapping` and the per-document loop of `backfill_normalize_tags`)
  into a new leaf `src/brain/tag_mapping.py`, leaving `cli.py` a thin caller; new
  `src/brain/cli_tags.py` (a `tags` sub-app, through RM-06); `src/brain/mcp_tools/tags.py`
  (read-only). **Scope out:** an MCP bulk rename.
- **Design:**
  - **What the engine does today, and keeps doing.** `backfill_normalize_tags` runs in autocommit
    (`conn.autocommit = True`): for each document whose normalized tags differ, it issues one
    `UPDATE documents SET tags = %s WHERE id = %s`, calls `sync_chunk_search_metadata(conn, doc_id)`
    (which refreshes `chunks.tags_text`, the tag text search reads since migration 009), and, when
    the document has a `vault_path`, rewrites that file's frontmatter with `rewrite_tags`. It does
    not call the mirror regenerator; `rewrite_tags` on the file at `vault_path` covers both tiers'
    files. `rename` and `merge` build a mapping `{old: new}` and run this same per-document,
    idempotent, re-runnable loop. **No single transaction** is introduced: a crash part-way leaves
    some documents renamed, and a re-run finishes the rest, which is the property `normalize-tags`
    already relies on.
  - **Dry-run by default**; `--apply` writes. `updated_at` is not bumped (migration 025: tag
    maintenance is not new knowledge).
  - If RM-15 has shipped, `saved_searches.filters` tag values are rewritten too, so a watch does not
    silently stop matching.
  - `suggest-merges` (pure, in `tag_mapping.py`): pairs whose `difflib.SequenceMatcher` ratio is
    ≥ 0.85, singular/plural pairs, and pairs whose document sets overlap with Jaccard ≥ 0.8
    (`set_similarity.jaccard`), ranked, each with its evidence (counts, ratio, overlap).
  - `brain tags list [--json]`: each tag and its count.
  - MCP: `brain_tags_list`, `brain_tags_suggest_merges`. **No MCP bulk rename**: a corpus-wide
    rewrite is an owner action at the terminal.
- **Security / privacy:** local only; MCP is read-only for tags.
- **Ceiling:** `cli.py` shrinks (record the hop).
- **Acceptance criteria:**
  1. `rename` dry-run lists affected documents and writes nothing; `--apply` updates the rows,
     `chunks.tags_text` and the files' frontmatter; a second run is a no-op.
  2. After a rename, `brain search --tag <new>` and a text query matching the new tag find the
     document, and `--tag <old>` does not.
  3. `merge` unions tags without duplicates and preserves order (`normalize_tags`).
  4. `backfill normalize-tags` output is byte-identical before and after the extraction.
  5. Suggestions include a synthetic plural pair and a co-occurrence pair, with evidence.
- **Test plan:** pure tests for the mapping engine and the suggestions; DB and `tmp_path` vault
  tests for apply (both tiers); the normalize-tags regression test; the search-after-rename test;
  MCP tests.
- **Eval:** none.
- **Depends on:** RM-06.
- **Size:** S.
- **Risks:** a merge into an existing tag cannot be undone without a mapping back; the dry-run default
  and the printed mapping are the mitigation.
- **Decisions:** RM-21.D1, MCP writes: **read-only**. RM-21.D2, one transaction for a rename:
  **no**, the per-document idempotent loop above.

#### RM-22 — Interactions-aware ranking (two sub-tickets, gated on data)

- **Source:** roadmap 1.3 phase 2; the Q1-C non-goal; backlog rank 14 ("gate it on the data").
- **Goal:** Results the owner opens and rates well rank a little higher, once there is enough
  engagement data to learn from.
- **Fields shared by the two sub-tickets:** **Security / privacy:** interactions are local rows;
  nothing leaves the machine. The rest is per sub-ticket.

##### RM-22a — The engagement readout that decides the gate

- **Source / goal:** the parent's; a measured answer to "is there enough engagement data to learn
  from", and the size figures RM-58 needs.
- **Scope in:** a new leaf `src/brain/engagement.py`; a `--engagement` flag on `brain usage`
  (`cli_usage.py`). **Scope out:** any ranking change.
- **Design:** `brain usage` is a single command today (`cli_usage.register` does
  `app.command()(usage)`), so the readout is a flag, `brain usage --engagement [--json]`, rather than
  a subcommand; turning `usage` into a Typer group would change its invocation contract for no gain.
  `engagement_stats(conn, *, days: int = 90) -> EngagementStats` returns opens and clicks by surface,
  distinct documents opened, `rated_useful`, `rated_irrelevant`, the share of opens that came from a
  search session, `interactions_rows` (the table's row count) and `interactions_bytes`
  (`pg_total_relation_size('interactions')`). The readout prints whether the gate is met. `brain
  usage` already has `--days/-d` (default 30, `cli_usage.py`); with `--engagement`, `--days` sets
  only the window of the readout's counts, and **the gate line is always computed over 90 days**,
  whatever `--days` says, so the gate cannot be met by widening a window. **The gate (recommended
  values):** at least **500 opens across at least 100 distinct documents** and **30
  explicit ratings** over 90 days.
- **Security / privacy:** counts only; no titles.
- **Ceiling:** none (`cli_usage.py` is small; `usage.py`, 623 lines, is not touched).
- **Acceptance criteria:**
  1. Counts match a synthetic `interactions` fixture.
  2. The gate line flips exactly at the thresholds.
  3. `interactions_rows` and `interactions_bytes` are reported.
  4. `brain usage` without the flag is byte-identical to before.
  5. `brain usage --engagement --days 7` and `--days 365` print different counts but the same gate
     line, computed over 90 days.
- **Test plan:** DB tests on a synthetic fixture; CLI tests for both forms.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** the thresholds are judgment calls; they are named constants with this rationale beside
  them.
- **Decisions:** RM-22a.D1, the thresholds: **500 opens / 100 documents / 30 ratings over 90
  days**. RM-22a.D2, a flag or a subcommand: **a flag** (above).

##### RM-22b — The engagement stage (default OFF), built only when the gate is met

- **Source / goal:** the parent's.
- **Scope in:** a new leaf `src/brain/search_engagement.py`; `src/brain/settings/engagement.py`; its
  entry in `search_pipeline.build_stages`. **Scope out:** a materialized table, unless RM-22b.D2
  triggers.
- **Design:**
  - `EngagementStage` multiplies each candidate's score by `clamp(1 + α·log1p(opens_decayed) +
    β·(useful − irrelevant), 0.8, 1.25)`, where `opens_decayed` sums opens with a 30-day half-life,
    then re-sorts. One `SELECT … WHERE document_id = ANY(%s)` per search over the candidates; no
    migration. `signature()` returns `{"engagement_alpha": α, "engagement_beta": β}`.
  - **Feedback-loop guard, concretely:** opens whose `session_id` equals the current request's
    `session_id` are excluded from `opens_decayed`, so a result is never boosted by a click from the
    same search session; opens with a `NULL` session (CLI ratings, direct opens) always count. The
    multiplier is capped at 1.25, and opens decay.
  - **Settings:** `BRAIN_ENGAGEMENT_ALPHA=0` (default, which means off), `BRAIN_ENGAGEMENT_BETA=0`.
- **Security / privacy:** the parent's.
- **Ceiling:** none (a new leaf).
- **Acceptance criteria:**
  1. Off: outputs and `config_signature` unchanged.
  2. On, a synthetic heavily-opened document moves up, within the cap.
  3. Opens carrying the current request's `session_id` do not count; `NULL`-session opens do.
  4. A `rated_irrelevant` document moves down, within the clamp.
- **Test plan:** pure tests for the boost and the clamp; a DB test for the aggregate; per-surface
  stage tests; an eval-runner test with the stage.
- **Eval:** the golden judgments are independent of opens, so the eval can only prove the stage does
  no harm. **Adoption rule:** no mean regresses by more than `1e-4` on top of the pipeline RM-14c
  left, and the owner opts in. `ci.json` is re-recorded on adoption only if a metric moves.
- **Depends on:** RM-22a (gate met), RM-09, RM-14c.
- **Size:** M.
- **Risks:** rich-get-richer; the cap, the decay and the same-session exclusion bound it.
- **Decisions:** RM-22b.D1, demote on `rated_irrelevant`: **yes**, symmetric with useful, within the
  clamp. RM-22b.D2, a materialized table: **only if** the per-search query exceeds 20 ms at p95 on
  the owner's data; it then takes the next free migration number at merge (§2.8) and is refreshed by
  the brief daemon.

#### RM-23 — Copy the note's path (external-editor affordance)

- **Source:** BL-4; consolidation spec §8.6 and Q4 (answer: option (a), copy the path; (b) on
  request; (c) scoped out).
- **Goal:** One click in the inspector copies the note's path, so it can be opened in any editor.
- **Scope in:** `ui/static/js/inspector.js`, the `GET /api/notes/{id}` payload, `UiContext` (one
  derived boolean). **Scope out:** an `obsidian://` link (RM-23.D1); a server-spawned editor.
- **Design:** a "Copy path" button calls `navigator.clipboard.writeText(path)`. It is rendered only
  when `window.isSecureContext` and `navigator.clipboard` exist: an `http://127.0.0.1` page is a
  secure context, a LAN `http://` page is not, so the button is simply absent over `--lan`. `GET
  /api/notes/{id}` adds `abs_path` **only on a loopback-bound server**: `UiContext` gains one
  derived boolean from the bind classification `ui.context.address_class` already makes (the same
  one the Ingest spec's D8 gate uses), because an absolute filesystem path is information a LAN
  reader does not need. Elsewhere the button copies `vault_path`. The status text ("Copied", or the
  failure) is set with `textContent`.
- **Security / privacy:** `abs_path` only on a loopback bind; no server-side editor spawning.
- **Ceiling:** `inspector.js` 517 → about 540.
- **Acceptance criteria:**
  1. On loopback, clicking puts the absolute path on the clipboard.
  2. A LAN-bound context's payload has no `abs_path`.
  3. A clipboard failure is reported in the status text.
  4. No `innerHTML` is added.
- **Test plan:** a route test for the `abs_path` gating; a hermetic browser test with clipboard
  permissions granted (`page.route` stubs); the static test.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** XS.
- **Risks:** browsers differ in clipboard permission prompts; the button reports failure rather than
  failing silently.
- **Decisions:** RM-23.D1, the `obsidian://` link: **not now**; it needs the Obsidian vault name,
  which `brain` does not know, and Q4 made it on-request.

#### RM-24 — `--entity` search filter — **NEEDS USER CONFIRMATION** (§5, U4)

- **Source:** roadmap 3.3 ("filter by entity", the leg GraphRAG did not ship); backlog rank 17, whose
  own wording is "Ask whether it is wanted before building it".
- **Goal:** `brain search "q" --entity "<name>"` returns only documents that mention that entity in
  the graph.
- **Scope in:** a new leaf `src/brain/entity_filter.py`; `search_predicate.build_predicate`;
  `hybrid_search`'s signature; `cli_search.py` (`--entity`, `--entity-type`); MCP `brain_search`
  (`entity`). **Scope out:** the UI's filter row (v1).
- **Design:**
  - `resolve_entity_ids(conn, *, tenant_id: str, name: str, entity_type: str | None) -> list[str]`
    matches `canonical_key = lower(name)` first, then a case-insensitive `name` match, through
    `graph_rag.aliases` when a name has been merged. It raises `EntityNotFound` or `EntityAmbiguous`
    (several types and no `--entity-type`), mirroring `PersonNotFound` / `PersonAmbiguous`.
  - `build_predicate` gains `entity_ids` and `entity_tenant`, adding `d.id IN (SELECT gem.document_id
    FROM graph_entity_mentions gem WHERE gem.tenant_id = %s AND gem.entity_id = ANY(%s::uuid[]))`.
    `hybrid_search` gains the two keyword arguments and forwards them (a recorded growth of about 6
    lines in `search.py`). Every leg, the total count and the facets see the filter, because they
    share the predicate.
  - With the graph disabled or the AGE tables absent: exit 2 (CLI) / `INVALID_PARAMS` (MCP) with
    "`--entity` needs the graph: set BRAIN_GRAPH_ENABLED=true and run `brain graphrag build`".
- **Security / privacy:** the filter can only narrow a result set; MCP's confidential lens is
  unchanged.
- **Ceiling:** `search.py` (about +6) and `mcp_server.py` (one parameter), both recorded;
  `cli_search.py` budget ≤ 15.
- **Acceptance criteria:**
  1. A synthetic entity's documents are the only results.
  2. Ambiguity and not-found map to exit 2 / `INVALID_PARAMS`.
  3. `total_documents` and the facets agree with the filtered set.
  4. Graph off → the error above.
- **Test plan:** resolver DB tests on 5434 (AGE); predicate unit tests; CLI and MCP tests.
- **Eval:** none (a filter; no golden row uses it).
- **Depends on:** RM-09.
- **Size:** S.
- **Risks:** an alias the resolver misses gives empty results; `EntityNotFound`'s message points at
  `brain graphrag entities`.
- **Decisions:** RM-24.D1, build it: **needs the user (U4)**; recommended **yes**, because the owner
  asked for the whole roadmap and it is small, but the backlog itself asked to confirm first.

### Wave 6 — Watcher, graph and log-privacy residues

The backlog's rank 16: low-severity residues, each with a known repair, none losing data. They are a
wave of their own here, rather than "when the module is next opened", because the owner asked for
the whole backlog to close. They touch disjoint files except RM-25 and RM-26 (both in `vault/`),
which merge one after the other.

#### RM-25 — Move identity for an id-less replacement file (H-1)

- **Source:** H-1 (PR #52 residue).
- **Goal:** A brand-new file that replaces a note and is renamed within one debounce window never
  inherits the old note's identity or backlinks.
- **Scope in:** `vault/move_identity.py`. **Scope out:** `vault/watch.py`; files that carry an id
  (already handled by PR #52).
- **Design:** `vault.move_identity.is_another_document` returns `False` for an id-less destination
  today (the plan's ruling: an id-less file is identified by its path), so a never-synced id-less
  file that replaces P and is renamed P → Q within the window adopts P's id. For an **id-less**
  destination only, the function now compares content: the word 5-gram shingles of the row's stored
  content and of the destination's body (`near_dup.shingles`, RM-19), compared with
  `set_similarity.jaccard`. **When either side has fewer than `MIN_IDENTITY_SHINGLES` = 8 shingles**
  (a short or empty note has too few for the comparison to mean anything; `jaccard` of two empty
  sets is 0.0), the in-place rename is kept, exactly as today. Otherwise, a Jaccard **below 0.2**
  means another document. An editor's atomic save of the same note keeps far more than 20% of its
  shingles, so ordinary renames keep their id and backlinks. Both thresholds are named constants
  with that rationale.
- **Security / privacy:** none.
- **Ceiling:** `move_identity.py` is a 46-line leaf; `vault/watch.py` is not touched.
- **Acceptance criteria:**
  1. The H-1 sequence (replace P with an unrelated id-less file, rename within the window) no longer
     carries P's id: the old row is deleted and Q syncs fresh.
  2. An atomic-save rename of the same content keeps the id.
  3. A rewrite that keeps at least 20% of the shingles keeps the id (the documented boundary).
  4. Renaming a short or empty id-less note (under 8 shingles on either side) keeps its id and its
     backlinks.
- **Test plan:** the watcher race harness PR #52's tests use (barriers, no sleeps), RED first for
  criterion 1 and as a regression guard for criterion 4.
- **Eval:** none.
- **Depends on:** RM-19 (`near_dup.shingles`).
- **Size:** S.
- **Risks:** a long note rewritten almost entirely **and** renamed within one debounce window loses
  its backlinks; recorded as the accepted trade.
- **Decisions:** RM-25.D1, the thresholds: **8 shingles, Jaccard 0.2**.

#### RM-26 — Owed watcher work survives shutdown and reconnects (H-2, H-4)

- **Source:** H-2 (PR #51) and H-4 (PR #56).
- **Goal:** No delete the watcher owes is lost, and no ghost graph vertex is left, across a shutdown
  or a lost connection.
- **Scope in:** a new leaf `src/brain/vault/owed_work.py`; wiring in `vault/watch.py`. **Scope out:**
  upserts (the startup `sync_vault` already reconciles them).
- **Design:** the problems: (H-2) a watcher closed by the drain drops late events, and the next
  startup sync reconciles upserts, but a delete is pruned only with prune on; (H-4a) a delete that
  committed just before the connection died re-runs as a no-op and leaves a ghost AGE `Document`
  vertex (repaired today by hand with `brain graphrag remove`); (H-4b) a delete not applied at
  shutdown is redone only by `brain vault sync --prune`. `owed_work.py` keeps a small journal at
  `$BRAIN_HOME/state/watcher-owed.jsonl` (append-only JSON lines, each written with one `write` and
  an `fsync`; rotated after a successful replay):
  - the drain's dropped-event path (H-2) and `_report_unapplied` (H-4b) append `{"action":
    "delete", "path": "<vault-relative>"}` for a dropped or unapplied **delete**, and for a dropped
    or unapplied **move** they append a delete of its **source** path (the destination is a new
    file, which the startup sync picks up);
  - the delete job captures the row's document id **before** deleting; when a reconnect re-run finds
    the row already gone, it hands that id to the graph lane's remove (H-4a), the same operation
    `brain graphrag remove` performs;
  - at startup, before the observer starts, `replay_owed(conn, vault_root, graph_lane)` deletes the
    row of each owed path whose file is still absent (a **targeted** prune of exactly those paths,
    never a full prune), and enqueues graph removes for owed ids.
- **Security / privacy:** the journal holds vault-relative paths and document ids, no content, and
  is created with mode 0600.
- **Ceiling:** `vault/watch.py` gets wiring only (the append calls, the id capture, the replay call):
  about 15 lines, recorded inline and in its docstring, after promoting its row's descriptive hop.
- **Acceptance criteria:**
  1. A late delete event after close is applied at the next start.
  2. A late **move** event after close leaves no row at its source path after the next start.
  3. A delete unapplied at shutdown is applied at the next start, without `--prune`.
  4. The reconnect re-run of a committed delete removes the AGE vertex.
  5. An owed path whose file has reappeared is **not** deleted.
  6. A corrupt journal line is skipped with a class-only WARNING, and the rest replays.
- **Test plan:** the PR #51 and #56 harnesses (barriers, `tests/thread_wait.py`), the AGE test
  instance for criterion 4, RED first for each of H-2, H-4a, H-4b and the move case.
- **Eval:** none.
- **Depends on:** nothing; merges after RM-25 (both in `vault/`).
- **Size:** S.
- **Risks:** a journal replayed after the owner restored an older backup; every owed path is
  re-checked against disk before anything is deleted.
- **Decisions:** RM-26.D1, a file journal or a table: **a file**; the watcher may owe work precisely
  when the database is unreachable.

#### RM-27 — Graph-lock residuals 2–4 (H-3)

- **Source:** H-3 (PR #53, its plan's "Residuals").
- **Goal:** The graph build tolerates a concurrent delete, and never applies a plan whose inputs
  changed.
- **Scope in:** `graph_rag/build.py`, `graph_rag/reconcile_plan.py`, `graph_rag/cross_type.py`'s
  docstring. **Scope out:** the cross-type collapse window itself (residual 4 is accepted).
- **Design:**
  - **Residual 2.** `build_graph` treats a document deleted between id enumeration and its read
    phase as **skipped** (counted in the build report as `skipped_deleted`), not as a
    `GraphReconcileError`. A deleted document needs no graph.
  - **Residual 3.** `ReconcilePlan` re-checks only `content_hash`, so a metadata-only edit (for
    example to participants) between plan and write can be overtaken. The plan records an
    `inputs_fingerprint` (SHA-256 over every column the plan read: `content_hash`, `content_type`,
    and the participant metadata it resolved), and the write re-checks that.
  - **Residual 4.** The W6/W7 cross-type collapse window is cosmetic. No code change: the module
    docstring of `graph_rag/cross_type.py` records why, and this ticket closes it as accepted.
- **Security / privacy:** none.
- **Ceiling:** `graph_rag/build.py` (346) and `reconcile_plan.py` are well under 800.
- **Acceptance criteria:**
  1. A document deleted mid-build is counted as `skipped_deleted`, and the build succeeds.
  2. A participants-only edit between plan and write invalidates the plan.
  3. A no-op re-check still passes.
- **Test plan:** barrier-driven tests on the AGE test instance.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** none new.
- **Decisions:** RM-27.D1, residual 4: **accept**, documented in place.

#### RM-28 — Close the log-privacy guard's blind spots (H-5)

- **Source:** H-5 (PR #58, the G1 guard's "Not covered").
- **Goal:** The G1 guard catches the three patterns it documents as not covered.
- **Scope in:** `tests/test_log_privacy.py` (or a new helper module beside it), and the real hits in
  `src/` if there are ten or fewer. **Scope out:** a runtime log filter.
- **Design:** three new AST rules: (1) **taint propagation**: a name assigned, inside an `except … as
  e` handler, from an expression that uses `e` (other than `describe_failure(e)` or
  `type(e).__name__`) is tainted for the rest of the function, so logging it after the handler is
  flagged; (2) **attribute stores**: `obj.attr = e` (or a tainted value) is flagged unless the stored
  value is class-only; (3) **nested scopes**: a `def` or `lambda` defined inside a handler is scanned
  with `e` in scope. Each rule ships with synthetic positive and negative snippets (the file's
  existing self-test pattern) and a mutation proof. Real hits in `src/` are fixed in the same PR when
  there are ten or fewer; otherwise they are listed in the PR, and a follow-up ticket is appended to
  Appendix B.
- **Security / privacy:** this ticket is privacy work; it adds no allowlist entry without a written
  reason.
- **Ceiling:** test code only; `tests/test_log_privacy.py` (541 lines) stays under 800, or the new
  rules move to `tests/log_privacy_rules.py`.
- **Acceptance criteria:**
  1. Each rule flags its synthetic positive case.
  2. Each rule spares its synthetic negative case.
  3. The guard is green on `src/` at merge.
- **Test plan:** the self-tests above, plus mutation proofs.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** S.
- **Risks:** false positives; each rule has a negative case.
- **Decisions:** RM-28.D1, fix real hits in the same PR: **when there are ten or fewer**.

#### RM-29 — Class-only terminal error reports (H-6)

- **Source:** H-6 (PR #58).
- **Goal:** No terminal error report can quote an exception message.
- **Scope in:** `vault/export.py` (`summary.errors`), `cli_sensitivity.py` (`_apply_redact`).
  **Scope out:** log lines (RM-28).
- **Design:** `vault/export.py`'s `summary.errors.append(f"{relative}: {e}")` becomes
  `f"{relative}: {describe_failure(e)}"`, which keeps the errno an operator needs
  (`OSError[ENOSPC]`). `cli_sensitivity.py`'s `_apply_redact` records `describe_failure(exc)` instead
  of `str(exc)`, with the documented hash-collision case given a fixed sentence ("redacted body
  collides with another document's content hash"). Both were judged content-free; this aligns them
  with the house rule, so no reviewer has to re-judge them.
- **Security / privacy:** the design is the privacy fix.
- **Ceiling:** `vault/export.py` (764) does not import `describe_failure` today, so it gains one
  import line (+1, within its §2.2 budget); the changed line is rewritten in place.
- **Acceptance criteria:**
  1. A sentinel exception message never appears in the export report.
  2. A sentinel exception message never appears in the sensitivity report.
  3. The errno class still appears for an `OSError`.
- **Test plan:** sentinel tests on both report paths.
- **Eval:** none.
- **Depends on:** nothing.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** none.

### Wave 7 — Source breadth, part 1

The backlog's rank 18, ordered by what the owner generates most and by shared machinery: calendar
and the code host first (daily activity, existing authenticated CLIs), then OCR (which three later
tickets reuse), then the drop-folder runner (which four later tickets reuse), then the trackers and
the note apps. Every ticket answers the nine points of §2.4 in a **Protocol** block inside its
design.

#### RM-30 — Calendar events as documents

- **Source:** roadmap 2.3. Today `vault/derived_links/directory.refresh_calendar` reads events only
  to refresh the **people directory**, after a Krisp ingest; no event is a document.
- **Goal:** Meetings without a transcript (external events, interviews, conferences) are searchable,
  filterable by `--person`, and dated by their start time.
- **Scope in:** `src/brain/ingest/calendar.py`, `src/brain/cli_ingest_calendar.py` (`brain
  ingest-calendar`, through RM-06), the `calendar` kind, the three front-end tables, and the
  **concept-skip policy and its consumer** (below): a new leaf `graph_rag/extract_policy.py`,
  `graph_rag/reconcile_plan.py` and `graph_rag/reconcile.py`. **Scope out:** writing to the
  calendar; a daily daemon run (RM-30.D2).
- **Design — Protocol:**
  1. *Path (b):* a `gws` shell-out, reusing the `gws calendar events list` call shape and the
     injectable runner type of `directory.refresh_calendar`.
  2. *Kind:* `calendar`; `content_type` `calendar_event`. Front end: a glyph in both `SOURCE_ICONS`
     tables, and `_ingested/calendar` added to `MONTH_GROUPED_PATHS` (events are a chronological
     feed).
  3. *Dedup:* `(calendar, <event id>)` for single events, and **one document per recurring
     series**, keyed `(calendar, <recurringEventId>)` (one document per instance would create dozens
     of near-identical rows, which RM-19 would then flag). The body holds the title, the series rule
     or the event's time, the location text with URLs removed, the description and the attendees'
     display names. The **occurrence dates are volatile**, so they go in `metadata.occurrences` (the
     count and the last ten), never in the body; a re-run whose body is unchanged applies them with
     `update_document(metadata_patch=…)`, so no re-chunk and no re-extraction.
  4. *Credentials:* `gws`'s own login; none stored.
  5. *Scope:* `--since YYYY-MM-DD` **and** `--until YYYY-MM-DD` are both required; the window is at
     most 92 days, and `--until` may be at most 14 days in the future. Without them: exit 2,
     "ingest-calendar requires --since and --until (at most 92 days, ending at most 14 days ahead)".
     `--calendar` defaults to `primary`; `--dry-run` lists titles and dates.
  6. *PII and sensitivity:* attendee addresses go only into `metadata.participants` (the field
     `--person` resolves through, as Gmail's do), never into the body. Conference links and dial-in
     blocks are dropped (they carry meeting PINs). Declined events, events with no title, and
     working-location or out-of-office entries are skipped. Events whose visibility is `private` are
     stored as `confidential`; others default to `normal`. `sent_at` is the start time, so date
     filters and the recency boost use it. A future event's age is clamped to zero by the recency
     boost (`search.py`), so it gets the maximum boost; the 14-day limit bounds that, and the docs
     say so.
  7. *Tests:* synthetic `gws` JSON fixtures (single, recurring with exceptions, declined, private,
     all-day, future), the injected runner, a DB end-to-end test, the scope refusal.
  8. *Dependencies:* none (`gws` exists).
  9. *Cost:* `calendar_event` is added to `CONCEPT_SKIP_CONTENT_TYPES` (the people aspect, which is
     what an event is about, still runs); summaries are skipped by default (events are short;
     `--enrich` opts in). The cap is the 92-day window; at a few events a day, one run is a few
     hundred documents with no concept extraction, so it drains in minutes.
  - **The concept-skip policy, wired to its consumer.** A constant alone would do nothing: the
    concept aspect is gated by `ReconcileConfig.concepts_enabled` and runs inside
    `graph_rag.reconcile.reconcile_document`, and whether a document needs it is decided by
    `graph_rag.reconcile_plan`, which compares the document's `graph_index_state` watermark for
    `aspect = 'concepts'` (`CONCEPTS_ASPECT`) with an expected watermark. At `02c2abc`,
    `plan_reconcile` compares the **whole tuple** `(content_hash, c_inputs_hash, extractor_ver, sver)`,
    with `extractor_ver = extractor.version`, against `index_state(…, CONCEPTS_ASPECT)`, and when the
    aspect is stale it also fetches the body (`content=fetch_doc_content_at(…) if concept_stale`).
    `reconcile_document` then writes the watermark from the plan's `concept_inputs_hash` and
    `concept_extractor_ver`. `graph_rag/build.py`'s backfill re-plans every document it is given, so
    a skipped document whose stored row did not equal the expected tuple would be re-planned as stale
    forever. So the write and the comparison must come from one place:
    1. `extract_policy.py` holds `CONCEPT_SKIP_CONTENT_TYPES: frozenset[str]`,
       `CONCEPT_SKIP_POLICY_VERSION` (bumped whenever the set changes), and **one helper**,
       `skip_watermark(content_type: str) -> tuple[str, str] | None`, which returns
       `("skip:<content_type>:<policy version>", "skipped-by-policy")` (the inputs hash and the
       extractor version) for a skipped type, and `None` otherwise.
    2. `plan_reconcile`, when the concept aspect is active and `skip_watermark(content_type)` is not
       `None`, uses **that pair** in place of `(c_inputs_hash, extractor.version)`: the expected
       watermark is exactly `(content_hash, "skip:<type>:<ver>", "skipped-by-policy", sver)`, and the
       plan carries the same pair in `concept_inputs_hash` and `concept_extractor_ver`. When such a
       document is stale, the plan **does not fetch the body** (`content` stays `""`), because nothing
       will be extracted from it; `ReconcilePlan` gains `concept_skipped: bool` to say so.
    3. `reconcile_document` runs its **normal concept path with an empty extraction**. At
       `02c2abc` it extracts with `extracted = extractor.extract(plan.content)` under `if
       plan.concept_stale:` (step 2 of the function), then, still under `plan.concept_stale`, calls
       `rewrite_doc_relational(…, entity_types=CONCEPT_ENTITY_TYPES)`, which also **deletes the
       document's previous concept mentions and contributions**, and, unless the tenant refresh is
       deferred, `_gc_orphan_concepts`. For a plan with `concept_skipped`, only the extraction call
       changes: the guard becomes `if plan.concept_stale and not plan.concept_skipped`, so
       `extracted` stays `[]` and no extractor is called. Everything after it runs as for any stale
       concept aspect: the rewrite with empty rows clears whatever concept mentions the document had,
       the orphan concept GC runs (per document, or in `build_graph`'s deferred
       `refresh_aggregates`), the AGE sync re-reads the document's now concept-free mentions so its
       concept `MENTIONED_IN` edges go, and the watermark is written from the plan's pair through
       the existing `upsert_index_state`, so the stored row is the tuple step 2 expects. The people
       aspect runs unchanged. This is what makes `CONCEPT_SKIP_POLICY_VERSION` work: adding a type
       to the set and bumping the version makes its already-extracted documents stale, and the next
       backfill strips their concept mentions.
    4. On the next backfill both aspects match, so `reconcile_document` returns `skipped=True` and
       `graph_rag.build`'s `BuildResult` counts the document under `skipped`, not `reconciled`.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** `graph_rag/reconcile.py` (757) gains at most 15 lines, within the 43 lines of headroom
  it has (§2.2 budget); `reconcile_plan.py` is well under 800.
- **Acceptance criteria:**
  1. Each fixture class lands as specified (single, series, declined skipped, private confidential,
     all-day).
  2. A re-run is a no-op; a changed description updates in place; new occurrences change only
     `metadata.occurrences`, without re-chunking.
  3. `brain search --source calendar --person "<synthetic>"` finds the event.
  4. A window over 92 days, or ending more than 14 days ahead, is refused.
  5. No attendee address appears in a document body.
  6. With concepts enabled, the first `brain graphrag build --backfill --concepts` reconciles a
     `calendar_event` document (its people aspect is indexed, its stored concepts row equals the
     tuple `skip_watermark` produces, and its body is never fetched for concepts), and a second
     `brain graphrag build --backfill --concepts` reports it under `BuildResult.skipped`, not
     `reconciled`. A fake extractor that fails the test if called is never called.
  7. A document that was concept-extracted earlier, whose `content_type` is then added to
     `CONCEPT_SKIP_CONTENT_TYPES` with a `CONCEPT_SKIP_POLICY_VERSION` bump, loses all its concept
     mentions, contributions and concept `MENTIONED_IN` edges on the next `brain graphrag build
     --backfill --concepts`, while its people mentions remain; concept entities left with no
     mentions are garbage-collected.
- **Test plan:** as protocol point 7, plus a test that a metadata-only re-run does not call the
  embedder, and AGE-instance tests (5434) for criteria 6 and 7, with a fake extractor that fails the
  test if it is called (criterion 7 first extracts with a working fake, then bumps the policy).
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** M.
- **Risks:** a daily stand-up series is long; the occurrence summary in metadata bounds it.
- **Decisions:** RM-30.D1, per-series documents: **yes**. RM-30.D2, a daily daemon run: **not by
  default**; the owner can schedule `brain ingest-calendar` on RM-33's launchd interval if wanted.

#### RM-31 — Code-host activity: pull requests, issues and READMEs

- **Source:** roadmap 2.1 ("your authored activity", not whole organisations).
- **Goal:** The owner's own PRs, reviews, issues and repository READMEs are searchable.
- **Scope in:** a recipe in `skills/ingest-brain/SKILL.md`; `src/brain/ingest/github.py`;
  `src/brain/cli_ingest_github.py` (`brain ingest-github`, through RM-06); the `github` kind; the
  front-end tables; a `brain doctor` row. **Scope out:** organisation-wide pulls; commits.
- **Design — Protocol:**
  1. *Path:* **(a) first**: a recipe for the GitHub MCP connector the owner already has (search the
     owner's PRs and issues, fetch each, pipe to `brain ingest-stdin --source github …`). **(b) in
     the same ticket**: `brain ingest-github`, shelling out to the already-authenticated `gh` CLI
     (`gh search prs --author @me`, `gh api`), with an injectable runner, for unattended refresh.
  2. *Kind:* `github`; `content_type` `github_pr`, `github_issue`, `github_readme`. Front end: a
     glyph in both tables; no month grouping (the fallback is intended: PRs are not a feed by date).
  3. *Dedup:* `(github, "<owner>/<repo>#<number>")` and `(github, "<owner>/<repo>:README")`; a
     re-run updates in place when the PR changed. The PR's state and review count are volatile and go
     in metadata (§2.4 point 3).
  4. *Credentials:* `gh auth` or the connector's own.
  5. *Scope:* at least one of `--repo OWNER/REPO` (repeatable) or `--mine` (the authenticated user is
     author, assignee or reviewer), **and** `--since`. Refusal: exit 2, "ingest-github requires
     --repo or --mine, and --since".
  6. *PII and sensitivity:* the body plus the **owner's own** review comments by default; other
     people's comments only with `--with-thread`. Repositories the API reports as private are
     stored as `confidential`. The secret guard runs on every body.
  7. *Tests:* synthetic `gh` JSON fixtures; the runner injected; the scope refusal; the private →
     confidential rule; a skill-text test that the recipe names the scope and the sensitivity.
  8. *Dependencies:* none (`gh` is a soft dependency with a `brain doctor` row).
  9. *Cost:* summaries and concepts on (PR descriptions are prose worth both). Cap: 500 items per
     run, reported. Worst case, 500 new documents at 8–10 minutes of concept extraction each is
     about three days of graph lane time; the guide says to start with a short `--since`, and the
     lane drains by itself (PR #54).
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** none (`doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. Without `--repo`/`--mine` or `--since`, exit 2 with the message above.
  2. A synthetic PR lands with only the owner's comments by default.
  3. A private repository's documents are `confidential`.
  4. A re-run updates in place; a state change touches metadata only.
  5. The recipe names the scope and the sensitivity.
  6. A run stops at 500 items and says so.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** M.
- **Risks:** API rate limits on a long `--since`; the per-run cap bounds a run.
- **Decisions:** RM-31.D1, build (b) too: **yes**; "my activity this month" is a recurring pull, and
  (a) needs a chat session each time.

#### RM-32 — OCR, and image-only PDFs

- **Source:** roadmap 3.5 = 5.3. Today an image-only PDF ingests as an empty document ("skipped
  (empty document)", `cli_ingest._ingest_outcome_verb`).
- **Goal:** Scanned PDFs and receipts become searchable text.
- **Scope in:** a new leaf `src/brain/ingest/ocr.py`; `ingest/pdf.py`; `src/brain/settings/ocr.py`;
  `pyproject.toml` (two explicit dependencies, §2.6); a `brain doctor` row. **Scope out:** images
  outside PDFs (RM-33); handwriting.
- **Design:**
  - `ocr.py`: an `OcrEngine` Protocol (`image_to_text(png: bytes, *, lang: str, timeout: float) ->
    str`) and `TesseractOcr`, which runs `tesseract stdin stdout -l <lang>` with a timeout (60 s per
    page) through an injectable runner.
  - `pdf.py`: when the extracted text averages under 25 characters per page, rasterize the pages (at
    most `BRAIN_OCR_MAX_PAGES`, default 50, at 200 dpi) with `pypdfium2`, encode each as PNG with
    `Pillow`, and OCR them. The text is joined with page markers, and `metadata.ocr = {"pages": n,
    "engine": "tesseract"}` records it, so `brain show` and `explain` can tell OCR text from
    born-digital text.
  - **Dependencies.** `pdfplumber` 0.11.10, the resolved version, hard-requires `pypdfium2>=5.9.0`
    and `Pillow>=12.2.0`, so both are always installed. Because `brain` would now import them
    directly, both become **explicit base dependencies** in `pyproject.toml`, each with an evidence
    block (the resolved versions, 5.13.0 and 12.3.0; zero new wheels on the measured resolve; the
    floors set to the versions the tests run on). An optional extra would be a no-op, so none is
    added. Only `tesseract` is optional: without it, an image-only PDF stays "skipped (empty)" with
    a hint line naming the missing binary.
  - Settings `settings/ocr.py`: `BRAIN_OCR` = `auto` (default: OCR when `tesseract` exists) | `off`;
    `BRAIN_OCR_LANG` = `eng`; `BRAIN_OCR_MAX_PAGES` = 50.
- **Security / privacy:** OCR runs locally. Scanned documents often carry account numbers; the
  secret guard runs on the OCR text like any other body.
- **Ceiling:** none (`pdf.py` 61 lines; `doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. A synthetic image-only PDF ingests with its text when OCR is available.
  2. A born-digital PDF is never OCR'd.
  3. The page cap holds.
  4. A timeout on one page skips that page with a class-only WARNING.
  5. Without `tesseract`, the document is "skipped (empty)" with the hint.
- **Test plan:** synthetic image-only PDFs, built in the test by rendering text to an image with
  `Pillow` and placing it with `reportlab` (a dev dependency); a fake engine in unit tests; one
  `live_ocr`-marked test with the real `tesseract`, skipped when absent; the doctor row.
- **Eval:** none.
- **Depends on:** RM-07, RM-08.
- **Size:** M.
- **Risks:** OCR quality on low-resolution scans; the page cap and the per-page timeout bound the cost
  of a bad file.
- **Decisions:** RM-32.D1, engine: **`tesseract`** (Homebrew, offline, mature); macOS Vision through
  `pyobjc` was rejected (a large native dependency, macOS-only). RM-32.D2, an `[ocr]` extra: **no**
  (it would install nothing `pdfplumber` does not already require).

#### RM-33 — Image extractor and the drop-folder runner (screenshot watch folder)

- **Source:** roadmap 5.1. The drop-folder runner it introduces is reused by RM-38, RM-39 and RM-40.
- **Goal:** Screenshots saved to a folder become searchable within minutes.
- **Scope in:** an image extractor in `ingest/dispatch.py`; a new leaf `src/brain/drop_folder.py`;
  `src/brain/settings/drop_folders.py`; `brain ingest-folder` (through RM-06); the `screenshot`
  kind; the launchd job and its wiring (below); the front-end tables. **Scope out:** a long-running
  watcher process; HEIC images.
- **Design — Protocol:**
  1. *Path (c):* an image extractor registered for `.png`, `.jpg` and `.jpeg` with
     `in_directory_walks=False`: OCR through RM-32's engine; `content_type` `screenshot`; an image
     with no OCR text is an empty document (skipped). Files arrive through the drop-folder runner:

     ```python
     SF_DATALESS = 0x40000000   # stat.SF_DATALESS exists only from Python 3.13

     @dataclass(frozen=True)
     class DropFolder:
         path: Path                                # resolved; must exist
         mode: Literal["ingest", "capture"]        # "capture" → capture-inbox notes (RM-39)
         kind: str | None                          # a VALID_SOURCE_KINDS member when mode == "ingest"
         extensions: frozenset[str]
         sensitivity: str
         auto: bool = True                         # False: only an explicit run uses it (RM-40)

     def scan(folder: DropFolder, *, since: datetime) -> list[Path]: ...
     def run(conn, folder: DropFolder, *, since: datetime, ingest: IngestFn) -> DropReport: ...
     ```

     `scan` uses `lstat`, never follows a symlink, skips `<path>/imported/`, and skips a file that
     is not on disk: since macOS 14 an iCloud-evicted file keeps its real name and is marked
     *dataless* (`os.lstat(p).st_flags & SF_DATALESS`), and reading one blocks or fails; on older
     macOS, the `.<name>.icloud` placeholder files are skipped by name. **Move first, then ingest.**
     `run` moves each file to `<path>/imported/<stem>-<sha8><ext>`, where `sha8` is the first 8 hex
     digits of the file's SHA-256, and ingests it from that final path, so the stored
     `source_path` is the file's permanent location. The document's title is set from the original
     stem, not the renamed file (a PDF's title is otherwise its file's stem, Ingest spec C6). Two
     different files that both arrive as `New Recording 1.m4a` therefore become two documents.
     **The destination may already exist**, when the same file is dropped twice. Then the content
     hash in its name matches, the earlier import already holds the only copy that matters, and the
     run is a no-op that deletes the redundant drop copy and ingests nothing (the existing document
     already has that `source_path`). **Only a file this run itself moved is ever moved back:** if the
     ingest fails after a move this run made, the file returns to the drop folder and the failure is
     reported; a destination that existed before the run is never moved or deleted.
  2. *Kind:* `screenshot`. Front end: a glyph in both tables; `_ingested/screenshot` in
     `MONTH_GROUPED_PATHS`.
  3. *Dedup:* `source_path` (the `imported/` path, stable and content-addressed by its suffix).
  4. *Credentials:* none.
  5. *Scope:* `brain ingest-folder --dir PATH --since 7d` for a one-off, or `--configured` for every
     configured folder with `auto = true`; `--since` is mandatory either way. Folders come from
     `$BRAIN_HOME/drop-folders.toml` (a list of `[[folder]]` tables), read by
     `settings/drop_folders.py`.
  6. *PII and sensitivity:* **default `confidential`**, because a screenshot can capture anything on
     screen, including credentials the secret guard's patterns miss. The secret guard still runs.
  7. *Tests:* `tmp_path` trees with symlinks, an `imported/` folder, a `.icloud` stub, a file whose
     `st_flags` carry the dataless bit (through an injected `lstat`), mtimes around `--since`, and two
     different files with the same name in two runs; synthetic PNGs rendered with `Pillow`; a fake
     ingest for `run`; the CLI scope refusal.
  8. *Dependencies:* RM-32's `tesseract`; `Pillow` (explicit since RM-32).
  9. *Cost:* `screenshot` is in `CONCEPT_SKIP_CONTENT_TYPES`; summaries follow `enrich_min_tokens`
     (most screenshots are below it). The cap is `--since`.
  - **Watching.** An opt-in launchd job, `com.brain.dropfolders`, runs `brain ingest-folder
    --configured --since 2d` every 10 minutes. Wiring: a template
    `src/brain/templates/launchd/com.brain.dropfolders.plist.j2`; a wrapper script
    `src/brain/templates/bin/_brain-dropfolders-fg.sh`, installed as a shim in `$BRAIN_HOME/.shims/`
    by `bin/launchd.py`'s wrapper loop (the tuple that today holds `_brain-watcher-fg` and
    `_brain-brief-fg`); the label in a new `_OPTIONAL_LABELS` tuple in `bin/launchd.py` (RM-14b
    introduces it for the reranker, or this ticket does if it lands first), which uninstall sweeps
    and which `brain doctor` checks only when the plist exists, because `doctor_runtime.py` imports
    `_INSTALLED_LABELS` as the set of daemons that must be running; and a `--with-dropfolders` flag.
    `bin/brain-install-launchd` is a passthrough to `brain.bin.launchd:install_main`, which parses no
    arguments today, so the flag is parsed **in `install_main`** (`src/brain/bin/launchd.py`).
    `brain-up.sh` runs the installer **without flags** on every start, so an optional label is
    **sticky**: a flagless install re-renders and re-bootstraps every optional plist that is already
    installed (so a template change reaches it) and never removes one; only `brain-uninstall-launchd`
    removes it. `setup.py`'s `_SHIM_NAMES` is **not** touched (the brief's wrapper is not in it
    either).
- **Security / privacy:** protocol point 6; configured folders only; no symlink is followed.
- **Ceiling:** none (`doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. A synthetic PNG with rendered text is ingested from a configured folder, titled by its original
     stem.
  2. A symlink, a `.icloud` stub and a dataless file are skipped; files in `imported/` are never
     rescanned.
  3. Two different files with the same name, dropped in two runs, become two documents.
  4. The same file dropped twice is a no-op the second time: the drop copy is removed, the
     earlier `imported/` file is untouched, and no document is added.
  5. A failed ingest after this run's own move leaves the file back in the drop folder; a failure
     on a file whose destination already existed never moves or deletes that destination.
  6. `--since` excludes older files; a folder with `auto = false` is skipped by `--configured`.
  7. The plist renders; a first install without `--with-dropfolders` does not install it; doctor
     does not report it missing when it was never installed.
  8. After `brain-install-launchd --with-dropfolders`, a plain flagless `brain-up` keeps
     `com.brain.dropfolders` installed and re-renders it; only `brain-uninstall-launchd` removes it.
- **Test plan:** protocol point 7, plus plist render and launchd install/uninstall tests in the
  style of the existing ones (no real `launchctl`).
- **Eval:** none.
- **Depends on:** RM-06, RM-32.
- **Size:** M.
- **Risks:** a screenshot of a credential the guard does not recognise; the confidential default
  keeps it inside F6's boundary.
- **Decisions:** RM-33.D1, launchd interval or a watchdog observer: **interval** (no second always-on
  process; ten minutes is fast enough). RM-33.D2, move before ingest: **yes** (above).

#### RM-34 — Jira issues

- **Source:** roadmap 2.8; the owner's decision "Notion + Jira, skip Linear".
- **Goal:** The owner's own tickets are searchable, inside the confidential tier.
- **Scope in:** a recipe in `skills/ingest-brain/SKILL.md`; the `jira` kind; the front-end tables.
  **Scope out:** a Jira API client or CLI wrapper.
- **Design — Protocol:**
  1. *Path (a):* the Atlassian MCP connector the owner has (`searchJiraIssuesUsingJql`,
     `getJiraIssue`) → `brain ingest-stdin --source jira --content-type jira_issue
     --sensitivity confidential …`. No authenticated Jira CLI exists on the machine (checked on
     2026-10-23), and the connector already holds the auth.
  2. *Kind:* `jira`. Front end: a glyph in both tables; no month grouping (intended fallback).
  3. *Dedup:* `(jira, "<site-host>/<ISSUE-KEY>")`, so the same key on two sites cannot collide.
     Status and assignee are volatile and go in metadata.
  4. *Credentials:* the connector's.
  5. *Scope — an accepted deviation from §2.4 point 5:* path (a) has no CLI flag to enforce, so the
     recipe carries the scope. It requires a JQL naming the owner and a time bound, by default
     `(assignee = currentUser() OR reporter = currentUser()) AND updated >= -30d`, written with the
     parentheses (without them `AND` binds tighter and the bound applies to `reporter` only), and it
     tells the agent to refuse a JQL with no time bound. Project-wide pulls are not part of the
     recipe.
  6. *PII and sensitivity:* summary, status, description and the owner's comments; others' comments
     only on request. **Default `confidential`**, because work-tracker content is the employer's.
  7. *Tests:* the kind (validation, facet, mirror path); a synthetic `ingest-stdin` end-to-end with
     the recipe's metadata shape; skill-text tests that the recipe contains the parenthesized JQL
     and `--sensitivity confidential`.
  8. *Dependencies:* none.
  9. *Cost:* summaries and concepts on; the 30-day bound keeps a run to tens of issues.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. The kind validates and buckets in the facet panel.
  2. A synthetic `ingest-stdin --source jira` document lands under `_ingested/jira/` as confidential.
  3. The recipe contains `(assignee = currentUser() OR reporter = currentUser()) AND updated >=
     -30d` verbatim, and `--sensitivity confidential`.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-07.
- **Size:** S.
- **Risks:** the scope rests on the agent following the recipe, the same trust model Krisp and Slack
  use; the recipe states the refusal explicitly.
- **Decisions:** RM-34.D1, a CLI client: **no** (above).

#### RM-35 — Notion pages (from Notion's Markdown export)

- **Source:** roadmap 2.8.
- **Goal:** Pages the owner exports from Notion become searchable, and update in place on re-export.
- **Scope in:** `src/brain/ingest/notion_export.py`; `brain ingest-notion <export-path>` (through
  RM-06); the `notion` kind; the front-end tables. **Scope out:** the Notion API; images and
  attachments.
- **Design — Protocol:**
  1. *Path (c):* no Notion connector is attached, so the source is Notion's own "Export → Markdown &
     CSV" folder or zip. If a Notion MCP connector is attached later, a recipe replaces this path;
     the kind stays.
  2. *Kind:* `notion`; `content_type` `notion_page`. Front end: a glyph in both tables; no month
     grouping.
  3. *Dedup:* `(notion, <32-hex page id>)`, parsed from the export's file names (`Title <id>.md`).
     **Not `source_path`**: every export lands in a new folder, so path dedup would duplicate the
     whole workspace on each re-export. The extractor yields documents with `source_path=None` and
     an external id, the sourced-stdin dedup rule.
  4. *Credentials:* none (a local export).
  5. *Scope — an accepted deviation from §2.4 point 5:* the export path is the scope; the owner
     chooses what to export. `--since` filters by the page's last-edited property when present. A
     zip is read without extracting it to disk.
  6. *PII and sensitivity:* page text; images and attachments are skipped and counted; database CSVs
     are ingested one row per document only with `--with-databases`. Default sensitivity `normal`,
     overridable with `--sensitivity`.
  7. *Tests:* a synthetic export tree (nested pages, id suffixes, a CSV, an image) built in
     `tmp_path`; a re-export to a new folder is an in-place update.
  8. *Dependencies:* none (`zipfile` is stdlib).
  9. *Cost:* summaries and concepts on; a per-run cap `--max` (default 200) and a printed count of
     what remains, so a large workspace drains over several runs; 200 new pages are about 30 hours
     of graph lane time in the worst case.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. Titles lose the id suffix.
  2. A re-export to a new folder updates in place.
  3. CSV rows are ingested only with the flag; images are skipped and counted.
  4. A file without a parseable id is skipped and counted, never guessed.
  5. `--max` stops the run and reports the remainder.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** M.
- **Risks:** Notion changes its export naming; criterion 4 makes that loud.
- **Decisions:** RM-35.D1, the export instead of an API client: **yes** (no connector to reuse, no
  token to store).

#### RM-36 — Apple Notes

- **Source:** roadmap 2.4 (the Apple Notes half).
- **Goal:** Notes in the folders the owner names become searchable.
- **Scope in:** `src/brain/ingest/apple_notes.py`; `brain ingest-apple-notes` (through RM-06); the
  `applenotes` kind; the front-end tables; a `brain doctor` row. **Scope out:** attachments; locked
  notes.
- **Design — Protocol:**
  1. *Path (b):* `osascript -l JavaScript` (macOS built-in) lists the notes of named folders and
     returns `{id, name, body (HTML), modified}` as JSON, through an injectable runner, with a 120 s
     timeout.
  2. *Kind:* `applenotes` (one word, because it becomes `_ingested/applenotes/`). Front end: a glyph
     in both tables; no month grouping.
  3. *Dedup:* `(applenotes, <note id>)`; an edited note updates in place.
  4. *Credentials:* none; the first run triggers macOS's Automation permission prompt, which the
     guide documents.
  5. *Scope:* `--folder NAME` (repeatable) is required; `--since` is optional. "All notes" is not
     offered.
  6. *PII and sensitivity:* the body is converted from Notes' HTML to Markdown by a small stdlib
     `html.parser` converter (headings, lists, links, checklists); attachments are skipped; locked
     notes cannot be read and are counted. **Default `confidential`** (overridable with
     `--sensitivity normal`): personal notes are private by nature, the same reasoning as RM-33's
     screenshots.
  7. *Tests:* synthetic JXA JSON through the runner; the converter at ≥ 95%; the scope refusal; a
     non-macOS host exits 1 with a clear message.
  8. *Dependencies:* none.
  9. *Cost:* summaries and concepts on; `--max` (default 200) per run, as RM-35.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** none (`doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. Without `--folder`, exit 2.
  2. Synthetic notes land as `confidential`; locked ones are counted.
  3. The converter keeps headings, lists, links and checklists.
  4. A non-macOS host exits 1.
  5. `--max` stops the run and reports the remainder.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** M.
- **Risks:** Notes scripting is slow on big folders; the timeout and `--since` bound a run.
- **Decisions:** RM-36.D1, `markdownify` for HTML: **no** (a new dependency for a narrow, testable
  converter). RM-36.D2, default sensitivity: **confidential**.

#### RM-37 — External markdown vault, with deletions tracked

- **Source:** roadmap 2.4 (the external-vault half). `brain ingest-dir` already imports one; what is
  missing is re-sync with deletions (backlog §1.2).
- **Goal:** Re-running an external-vault import mirrors deletions, safely.
- **Scope in:** a new leaf `src/brain/ingest/import_sync.py`; `--sync`, `--apply` and
  `--allow-empty` on `brain ingest-dir` (`cli_ingest.py`). **Scope out:** a new kind; two-way sync.
- **Design — Protocol:**
  1. *Path (c):* the existing `ingest-dir` walk and extractors; `--sync` adds a prune pass after it.
     `import_sync.py` finds ingested documents whose `source_path` (stored resolved and absolute by
     the extractors) lies under the resolved `<dir>` and no longer exists on disk, and deletes them
     through `vault.delete.delete_document`. **Dry-run by default** (it lists what it would delete);
     `--apply` deletes.
  2. *Kind:* stays `manual` (an imported file is a manual import). No front-end change.
  3. *Dedup:* `source_path`, as today.
  4. *Credentials:* none.
  5. *Scope:* the directory argument, plus three refusals: (1) `<dir>` is missing or unmounted, or
     holds zero supported files while the database holds documents under it, unless
     `--allow-empty` (an unmounted drive must not prune a whole import); (2) `<dir>` is inside the
     brain vault (that tree belongs to the vault watcher); (3) only `kind = 'ingested'` rows are ever
     candidates.
  6. *PII and sensitivity:* unchanged from `ingest-dir` (`--sensitivity` as today).
  7. *Tests:* `tmp_path` trees, DB tests, CLI tests for each refusal.
  8. *Dependencies:* none.
  9. *Cost:* unchanged from `ingest-dir` (the prune pass costs nothing in Ollama).
- **Security / privacy:** deletes only ingested rows under the named directory; never vault-tier
  rows.
- **Ceiling:** `cli_ingest.py` gains the three options (over the ceiling; recorded inline and in its
  docstring, after promoting its row's hop); the logic is in the leaf.
- **Acceptance criteria:**
  1. A deleted file's document is listed in dry-run and removed with `--apply`.
  2. Each of the three refusals.
  3. A vault-tier row is never touched.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-07.
- **Size:** M.
- **Risks:** a mistyped directory; dry-run by default plus the three refusals.
- **Decisions:** RM-37.D1, a new kind: **no** (stays `manual`).

#### RM-38 — Voice memos with local transcription

- **Source:** roadmap 5.2 (and the voice-memo part of 2.9).
- **Goal:** Voice memos become searchable transcripts, transcribed on the machine.
- **Scope in:** an audio extractor in `ingest/dispatch.py`; `src/brain/settings/voice.py`; the
  `voice` kind; the front-end tables; a `brain doctor` row. **Scope out:** speaker diarization; reading
  the Voice Memos app's private store.
- **Design — Protocol:**
  1. *Path (c):* an audio extractor registered for `.m4a`, `.mp3` and `.wav` with
     `in_directory_walks=False`. It reads the duration with `afinfo` (macOS built-in, 10 s timeout),
     converts to 16 kHz mono WAV with `afconvert` (60 s timeout), and transcribes with `whisper-cli`
     from whisper.cpp (Homebrew `whisper-cpp`) with a timeout of `2 × duration + 60 s`, all through
     an injectable runner. Memos arrive through RM-33's drop-folder runner (the owner shares or
     exports from the Voice Memos app into a configured folder), so the move-first naming applies:
     two `New Recording 1.m4a` files are two documents.
  2. *Kind:* `voice`; `content_type` `voice_memo`. Front end: a glyph in both tables;
     `_ingested/voice` in `MONTH_GROUPED_PATHS`.
  3. *Dedup:* `source_path` (the `imported/` path).
  4. *Credentials:* none.
  5. *Scope:* the drop folder plus the mandatory `--since`.
  6. *PII and sensitivity:* the transcript only; the audio is not copied into the brain. **Default
     `confidential`** (overridable per folder in `drop-folders.toml`): voice memos are private by
     nature, the same reasoning as RM-33's screenshots.
  7. *Tests:* runner-injected duration, conversion and transcription; a `live_whisper`-marked test
     skipped without the binary and model.
  8. *Dependencies:* `whisper-cli` and a model file named by `BRAIN_WHISPER_MODEL` in
     `settings/voice.py` (no default download; the guide names a tested small English model and its
     size); `BRAIN_WHISPER_MAX_MINUTES` (default 60) in the same leaf.
  9. *Cost:* summaries and concepts on (memos are prose, like transcripts); the cap is `--since` and
     the maximum duration.
- **Security / privacy:** audio never leaves the machine; protocol points 4–6.
- **Ceiling:** none (`doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. A synthetic `.m4a` path yields the fake transcript as a confidential `voice` document.
  2. A duration over `BRAIN_WHISPER_MAX_MINUTES` is skipped and counted.
  3. A conversion or transcription timeout skips the file with a class-only WARNING.
  4. `ingest-dir` does not pick up audio (`in_directory_walks=False`).
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-33.
- **Size:** M.
- **Risks:** long memos; the duration cap bounds them.
- **Decisions:** RM-38.D1, `faster-whisper` instead: **no** (a new native Python dependency, where
  whisper.cpp is a Homebrew binary with Metal support). RM-38.D2, default sensitivity:
  **confidential**.

#### RM-39 — Mobile share-sheet capture

- **Source:** roadmap 4.5 ("a share-sheet hook, not an app"); backlog: capture exists, the share
  sheet is missing.
- **Goal:** Share text or a link from the phone into the capture inbox, with no network surface on
  the Mac.
- **Scope in:** `docs/guides/mobile-capture.md` (the Shortcut, step by step); a new leaf
  `src/brain/capture_import.py` (`import_file`); `brain capture import` on the existing `capture`
  sub-app (`_capture_command.py`, 469 lines); the `mode = "capture"` branch of RM-33's runner. **Scope out:** an HTTP endpoint (RM-39.D1); an iOS app.
- **Design:** an iOS Shortcut ("Save to Brain") writes the shared text or URL, plus an optional
  comment, as a `.md` file into an iCloud Drive folder. On the Mac that folder is a configured drop
  folder with `mode = "capture"`. `brain capture import --dir <folder> --since 7d` takes each file
  through RM-33's move-first step (so two shares with the same file name stay two notes), and turns
  it into a capture-inbox note through the same path `brain capture` uses. **There is one code
  path:** `drop_folder.run` dispatches on `mode`, and for `mode = "capture"` it calls the same
  function `brain capture import` calls (`capture_import.import_file`, a new leaf), so `brain
  ingest-folder --configured` (which RM-33's launchd job runs every 10 minutes) and `brain capture
  import` cannot diverge. Dataless (evicted) files and `.icloud` stubs
  are skipped until they download.
- **Security / privacy:** **the content passes through a third-party sync service.** Between the
  phone and the Mac, the shared text sits in the owner's iCloud Drive, so Apple's service stores it
  (end-to-end encrypted only if the owner has Advanced Data Protection on; the guide says so and
  recommends it). The guide also says not to share anything through this path that the owner would
  not keep in iCloud. Capture notes are authored vault-tier notes with sensitivity `normal` by
  default; the Shortcut can add `sensitivity: confidential` to the file's frontmatter (a second
  Shortcut action, "Save to Brain (confidential)"), which the import honours. Once imported, the file
  sits in `imported/` inside iCloud Drive, so the import **deletes** it after a successful capture
  rather than keeping it (unlike RM-33's screenshots, which stay local).
- **Ceiling:** none.
- **Acceptance criteria:**
  1. A synthetic shared file becomes an inbox note carrying the comment and the URL.
  2. A re-run does not duplicate; two different files with the same name become two notes.
  3. Dataless files and `.icloud` stubs are skipped.
  4. A file with `sensitivity: confidential` frontmatter becomes a confidential note.
  5. The source file is gone from the iCloud folder after a successful import.
  6. `brain ingest-folder --configured` over a `mode = "capture"` folder produces exactly the notes
     `brain capture import` produces for the same files (both go through `capture_import.import_file`;
     a test asserts the call).
- **Test plan:** a `tmp_path` folder, DB plus a `tmp_path` vault, CLI tests.
- **Eval:** none.
- **Depends on:** RM-33.
- **Size:** S.
- **Risks:** iCloud sync delay; placeholders are retried on the next run.
- **Decisions:** RM-39.D1, an HTTP endpoint instead: **no**. A Shortcut sends no `Origin` header, so
  it would need a new token-only, Origin-exempt write route, which is a change to the security model
  (Ingest spec D8, Q1). The folder needs no network surface and queues offline. RM-39.D2, keep the
  file after import: **no**, delete it (above).

#### RM-40 — Ingest tab, phase 4: the inbox folder

- **Source:** Ingest spec §10 phase 4 and §5.5 ("only if asked"; the owner asked for the whole
  roadmap).
- **Goal:** Ingest, by name, a file already sitting in the inbox folder, from the browser.
- **Scope in:** `GET /api/ingest/inbox` and `POST /api/ingest/inbox` in `ui/routes_ingest.py`; the
  inbox list in `ingest.js`; job kind `ingest.inbox`. **Scope out:** paths in requests; automatic
  ingest of the inbox.
- **Design:** the Ingest spec's §5.5 design, with two changes. The inbox is a drop folder from
  RM-33's `drop-folders.toml` with `mode = "ingest"`, `kind = "manual"` and **`auto = false`**, so
  the 10-minute launchd job (`--configured`) never ingests it before the user picks a file; no
  `config.py` knob is needed. `GET` lists names; `POST` takes `{name}` (never a path), resolves it
  under the root with the resolve-then-compare rule of `vault/paths.py`, refuses symlinks with
  `lstat`, and submits the job, which uses RM-33's move-first step (so the file ends in `imported/`,
  which answers §5.5's open lifecycle question).
- **Security / privacy:** a new traversal surface. **Security review** with `ecc:security-reviewer`
  on opus.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. A name containing `/`, a `..` name and a symlink are each refused.
  2. A file swapped between listing and open is refused (the open re-validates the name and uses
     `lstat`).
  3. The launchd job's `--configured` run leaves the inbox untouched.
  4. An ingested file ends in `imported/` and appears in the tree.
- **Test plan:** route tests on the 5434 database with a `tmp_path` inbox, one per refusal; a
  hermetic browser row for the list and the pick; a test that `--configured` skips `auto = false`.
- **Eval:** none.
- **Depends on:** RM-13, RM-33.
- **Size:** S.
- **Risks:** the listing-to-open race (TOCTOU) §5.5 names; criterion 2 pins it.
- **Decisions:** RM-40.D1, the inbox root: **a drop folder with `auto = false`**.

### Wave 8 — Source breadth, part 2: the heaviest privacy load and the least evidence of need

The backlog's rank 19. Kept, because the owner accepted each item on 2026-05-11; last, for the
reasons the backlog gives.

#### RM-41 — Browser history and bookmarks, allowlist only

- **Source:** roadmap 2.5; the owner's decision "build, whitelist-driven, opt-in per domain".
- **Goal:** Pages from allowlisted sites that the owner revisits or bookmarks become searchable.
- **Scope in:** `src/brain/ingest/browser.py`; `brain ingest-web` (through RM-06); the `web` kind;
  the front-end tables. **Scope out:** page fetch by default (RM-41.D2); other browsers.
- **Design — Protocol:**
  1. *Path (c):* read the browser's local stores with stdlib `sqlite3` and `plistlib`: Chrome's
     `History` (copied to a temporary file first, because Chrome holds a lock on it) and its
     `Bookmarks` JSON; Safari's `History.db` and `Bookmarks.plist` (which need Full Disk Access; the
     guide says so).
  2. *Kind:* `web`; `content_type` `web_page`. Front end: a glyph in both tables; no month grouping.
  3. *Dedup:* `(web, <normalized URL>)`. The body is the title, the normalized URL and the host, plus
     the fetched text with `--fetch`. **The visit count and last-visit time are volatile and go in
     `metadata`**; a re-run whose body is unchanged applies them with `update_document(metadata_patch
     =…)`, so a daily run does not re-chunk, re-embed or re-extract every page.
  4. *Credentials:* none.
  5. *Scope:* **an allowlist is mandatory**: `$BRAIN_HOME/web-allowlist.txt`, one host or `*.domain`
     per line; the command refuses to run when the file is missing or empty. Also required:
     `--browser chrome|safari` and `--since`. A page qualifies when its host is allowlisted **and** it
     was bookmarked or visited at least `--min-visits` times (default 3).
  6. *PII and sensitivity:* the URL's **query string and fragment are dropped** (they carry session
     tokens and search terms). **No page fetch by default.** `--fetch` (opt-in) downloads the page
     body: https only, allowlisted hosts only, no redirect off the allowlist, 1 MiB cap, 10 s timeout,
     HTML reduced to text with stdlib `html.parser`. Default sensitivity `normal`.
  7. *Tests:* synthetic `History` SQLite files with the Chrome schema subset, Safari plist fixtures,
     the allowlist refusal, query stripping, and `--fetch` against an `httpx.MockTransport` (a
     redirect off the allowlist is refused).
  8. *Dependencies:* none.
  9. *Cost:* `web_page` is in `CONCEPT_SKIP_CONTENT_TYPES`, and summaries are skipped unless
     `--fetch` (a title-and-URL body is under `enrich_min_tokens` anyway); per-run cap `--max`
     (default 500).
- **Security / privacy:** protocol points 4–6. **Security review** with `ecc:security-reviewer` on
  opus (`--fetch` is a network egress).
- **Ceiling:** none.
- **Acceptance criteria:**
  1. No allowlist → refused.
  2. An allowlisted, often-visited page lands with its query string stripped.
  3. A page off the allowlist never becomes a document.
  4. A second run with only new visits changes `metadata` only, without re-chunking.
  5. `--fetch` refuses a redirect off the allowlist.
  6. Safari without Full Disk Access exits 1 with the guide's pointer.
- **Test plan:** protocol point 7, plus a test that a metadata-only re-run does not call the embedder.
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** L.
- **Risks:** browser schema changes; the fixtures pin the columns read, and a missing column fails
  loudly.
- **Decisions:** RM-41.D1, per-URL documents: **yes**, qualified by the visit threshold so a host's
  long tail does not flood search. RM-41.D2, fetch by default: **no**.

#### RM-42 — Code-aware chunker

- **Source:** roadmap 7.3 (paired with 2.6).
- **Goal:** Code is chunked at function and class boundaries, so a hit is a whole definition.
- **Scope in:** a new leaf `src/brain/ingest/chunk_dispatch.py` (named apart from the existing
  `chunker.py`); a new leaf `src/brain/ingest/code_chunker.py`; the two `chunk_text(...)` calls in
  `ingest/__init__.py`. **Scope out:** prose chunking (unchanged); tree-sitter (RM-42.D1).
- **Design:**
  - `chunk_dispatch.chunk_for(content_type: str, content: str, *, count_tokens) -> list[Chunk]`
    sends `code` to `chunk_code` and everything else to the existing `chunker.chunk_text`.
    `ingest/__init__.py`'s two `chunk_text(...)` calls (new document and in-place update) become
    `chunk_for(...)`: a rewrite in place, net zero lines, recorded as a hop.
  - `code_chunker.chunk_code`: Python is split with the stdlib `ast` module at top-level functions and
    classes (large classes by method), with the module docstring and imports as a header chunk.
    Other languages use a structural fallback: a small per-language table of top-level declaration
    patterns (JavaScript/TypeScript, Go, Rust, Java, shell), then blank-line blocks, packed to the
    token budget. Every chunk starts with `path:start-end` and the enclosing symbol's name, so a hit
    says where it is.
- **Security / privacy:** none.
- **Ceiling:** `ingest/__init__.py`, net zero, hop recorded.
- **Acceptance criteria:**
  1. Python boundaries are exact on synthetic files.
  2. Fallback boundaries hold on the other languages' fixtures.
  3. Prose `content_type`s chunk byte-identically to before (a `chunk_text` regression test).
- **Test plan:** pure tests on synthetic sources per language; the prose regression.
- **Eval:** none for prose (unchanged chunks).
- **Depends on:** RM-07.
- **Size:** M.
- **Risks:** the fallback splits a definition in an unlisted language; packing keeps every chunk
  within budget regardless.
- **Decisions:** RM-42.D1, tree-sitter: **not in v1**; it becomes the `[code]` extra (§2.6) only if
  RM-43's code queries show the fallback misses boundaries that matter.

#### RM-43 — Code-repository ingestion (the owner's own files)

- **Source:** roadmap 2.6.
- **Goal:** Files the owner wrote in their repositories become searchable.
- **Scope in:** `src/brain/ingest/code_repo.py`; `brain ingest-code <repo-dir>` (through RM-06); the
  `code` kind; the front-end tables; a `brain doctor` row. **Scope out:** other authors' files;
  dependencies; history.
- **Design — Protocol:**
  1. *Path (c):* `git ls-files`, and `git log --author=<email> --name-only --since=<date>` to keep
     only files the owner touched; each file goes through `ingest_document` with
     `content_type="code"`, so RM-42 chunks it.
  2. *Kind:* `code`. Front end: a glyph in both tables; no month grouping.
  3. *Dedup:* `source_path` (file).
  4. *Credentials:* none (a local clone).
  5. *Scope:* `<repo-dir>` must be a git work tree; `--author-email` (repeatable) and `--since` are
     required; `--ext` narrows. Always excluded: anything `git` ignores, vendored and build
     directories (`node_modules`, `.venv`, `vendor`, `dist`, `build`, `third_party`), files over
     200 KB, binaries, minified files and lockfiles.
  6. *PII and secrets:* **the secret guard runs in `reject` mode** for this command (a key committed
     to a repository is the classic leak); default sensitivity `confidential` (an employer's code),
     so it stays inside F6's boundary; `--sensitivity normal` for personal repositories.
  7. *Tests:* a synthetic git repository built in `tmp_path` with two authors, an ignored file, a
     vendored directory and a planted fake key (from `tests/secret_fixtures.py`) that must be
     rejected.
  8. *Dependencies:* `git` (always present on the owner's machine; still a soft check).
  9. *Cost:* `code` is in `CONCEPT_SKIP_CONTENT_TYPES`, and summaries are off by default (`--enrich`
     opts in): a code file's summary adds little to function-level chunks. The per-run cap is
     `--max-files` (default 2,000); with both skipped, a capped run costs only embeddings, minutes
     rather than days.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** none (`doctor_runtime.py` budget).
- **Acceptance criteria:**
  1. Only the owner's files are ingested.
  2. Every exclusion holds.
  3. The planted key is rejected.
  4. The default sensitivity is `confidential`.
  5. A re-run updates in place.
  6. A run ingests at most `--max-files` files and requests no summary by default; with concepts
     enabled, a second `brain graphrag build --backfill --concepts` reports an ingested `code`
     document under `BuildResult.skipped`, not `reconciled`, and the fake extractor is never called
     (behaviour, on the AGE test instance).
- **Test plan:** protocol point 7, plus the cost assertions of criterion 6.
- **Eval:** after ingesting, the owner adds 3–5 code queries to the local golden corpus and
  re-records `ci.json` (triggers: a document-set change and a golden-corpus edit), so code retrieval
  is measured from then on.
- **Depends on:** RM-06, RM-42.
- **Size:** M.
- **Risks:** a large monorepo; `--ext`, the 200 KB limit and `--max-files` bound a run.
- **Decisions:** RM-43.D1, the secret guard in `reject` mode for code: **yes**. RM-43.D2, summaries
  and concepts for code: **off by default**.

#### RM-44 — Social bookmarks

- **Source:** roadmap 2.9 (the social-bookmarks item).
- **Goal:** Posts the owner liked or bookmarked become searchable.
- **Scope in:** `src/brain/ingest/social_archive.py`; `brain ingest-bookmarks --format x-archive
  <archive-dir>` (through RM-06); the `social` kind; the front-end tables. **Scope out:** browser
  bookmarks (RM-41); any platform API.
- **Design — Protocol:**
  1. *Path (c):* the platform's own data-archive export. **The ticket's first task is to pin the
     field set** against the archive's published schema (described in the plan, never copied from a
     real export): which files hold likes and bookmarks, and which fields each post carries. The
     files are JavaScript whose payload follows a `window.YTD… =` prefix; they are parsed as JSON
     after the prefix, never executed. The bookmarks file is read **only if it exists** (archives
     differ in what they include); likes are the baseline.
  2. *Kind:* `social`; `content_type` `social_post`. Front end: a glyph in both tables; no month
     grouping.
  3. *Dedup:* `(social, <post id>)`.
  4. *Credentials:* none (a local export).
  5. *Scope — an accepted deviation from §2.4 point 5:* the archive path is the scope (the owner
     chose what to export), plus `--since`.
  6. *PII and sensitivity:* posts are third-party public text; the author handle is stored **when
     present** (it is optional, since some archive entries carry only the post id and text);
     nothing else about the author. The post URL is stored without its query string. The post's
     date is derived from its id (the platform's snowflake ids encode a timestamp: `(id >> 22) +
     1288834974657` milliseconds since the Unix epoch), because the archive's entries may carry no
     date. Default sensitivity `normal`.
  7. *Tests:* a synthetic archive tree (with and without the bookmarks file, with and without
     handles), the prefix parser (never `eval`), the snowflake date conversion.
  8. *Dependencies:* none.
  9. *Cost:* `social_post` is in `CONCEPT_SKIP_CONTENT_TYPES`; posts are under `enrich_min_tokens`,
     so no summaries. Cap: `--since`.
- **Security / privacy:** protocol points 4–6; the file is never evaluated.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. The prefix is stripped and the payload parsed as JSON, never executed.
  2. One document per post; a re-run is a no-op.
  3. Query strings are stripped; a missing handle is allowed.
  4. A post's date matches its id's encoded timestamp.
  5. A missing bookmarks file is not an error.
  6. `--since` is honoured.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-06, RM-07.
- **Size:** S.
- **Risks:** the archive format changes; the parser fails loudly on an unknown shape.
- **Decisions:** RM-44.D1, one kind for any platform: **yes, `social`**.

#### RM-45 — Receipts (Gmail attachments)

- **Source:** roadmap 2.9 (receipts).
- **Goal:** Receipt attachments are searchable together with their email.
- **Scope in:** a new leaf `src/brain/ingest/gmail_attachments.py`; a call from `ingest/gmail.py`
  (669 lines); `--with-attachments` on `brain ingest-gmail` (`cli_ingest.py`). **Scope out:**
  attachments as separate documents (RM-45.D1); non-PDF, non-image attachments.
- **Design — Protocol:**
  1. *Path (b):* receipt emails already ingest through `brain ingest-gmail --label <receipts-label>`.
     With `--with-attachments`, each PDF or image attachment of at most 10 MiB is downloaded with
     `gws gmail users messages attachments get` (the injectable runner `gmail.py` already uses, 60 s
     timeout), extracted through `ingest.dispatch` (PDF text, or RM-32's OCR), and appended to the
     thread document as an `## Attachment: <name>` section.
  2. *Kind:* `gmail` (unchanged); no front-end change.
  3. *Dedup:* the existing Gmail thread rule; the attachment text is part of the thread body.
  4. *Credentials:* `gws`'s own login; the attachment call uses the same read scope as message
     fetches. Whether the installed `gws` exposes `attachments get` is checked first in the ticket's
     plan (`gws gmail users messages attachments --help`); if it does not, the ticket stops and
     reports rather than adding an API client.
  5. *Scope:* the existing mandatory Gmail scope flags; `--with-attachments` is additionally refused
     without `--label` or `--query` (an attachment pull over a date range alone is too broad).
  6. *PII and sensitivity:* receipts carry addresses and partial card numbers; with
     `--with-attachments` the default sensitivity becomes `confidential`, and the secret guard runs on
     the appended text.
  7. *Tests:* runner-injected attachment fixtures (a synthetic PDF built with `reportlab`), the
     dispatch path, the size cap, the CLI refusal.
  8. *Dependencies:* none new (RM-32 for OCR of scanned receipts).
  9. *Cost:* as Gmail today; a cap of 50 attachments per run.
- **Security / privacy:** protocol points 4–6.
- **Ceiling:** `cli_ingest.py` gains one option (recorded); `gmail.py` +≤ 15.
- **Acceptance criteria:**
  1. A synthetic message with a PDF attachment gains the section.
  2. An attachment over 10 MiB is skipped and counted.
  3. `--with-attachments` without `--label` or `--query` is refused.
  4. With the flag, documents default to `confidential`.
  5. At most 50 attachments are fetched per run.
- **Test plan:** protocol point 7.
- **Eval:** none.
- **Depends on:** RM-32.
- **Size:** M.
- **Risks:** large pulls; the size and count caps bound them.
- **Decisions:** RM-45.D1, attachments as child documents: **no**, which is why RM-60's
  `parent_document_id` stays unbuilt.

### Wave 9 — The deferred, the conditional, and the polish

The backlog's rank 20 and its "leave deferred" row, plus the conditional items of its §1.8. Each
gets the smallest design that respects the rule or decision that deferred it. Four tickets need the
user, and are listed in §5: RM-24 (in Wave 5), RM-48, RM-49 and RM-51.

#### RM-46 — MCP over HTTP, for reaching the brain from another machine

- **Source:** BL-10; the MCP server spec's "Out of scope" ("HTTP / SSE transport"); R1, which is a
  complete design ("evaluated, deferred").
- **Goal:** An opt-in, loopback-by-default, bearer-authenticated, **read-only-by-default**
  streamable-HTTP transport, so a second device on a private network can call the read tools.
- **Scope in:** R1's §4 module layout (`mcp_auth.py`, `mcp_http.py`, `cli_mcp.py`, docs), with the
  updates below; a new leaf `src/brain/mcp_state.py`. **Scope out:** OAuth, SSE, multi-tenant
  tokens (R1's own exclusions).
- **Design:** **R1 as written**, with five updates for `02c2abc`:
  1. **Registration.** R1's `brain mcp` sub-app registers through RM-06's `cli_registry` seam, not
     through two lines in `cli.py` (R1 §4's `cli.py` row is superseded).
  2. **Tool classification.** R1's read-only allowlist fails closed (any tool not in
     `READ_ONLY_TOOLS` is withheld), so a new tool is safe by default; but R1's counts ("21 read / 16
     write of the 37 registered") are stale. The ticket re-classifies **every** tool registered at
     implementation time, explicitly, each against its docstring as R1 did: the 42 `brain_*` tools in
     `mcp_server.py` at `02c2abc` (which include tools added after R1, such as `brain_graphrag_remove`,
     a write) plus every extension tool (RM-12, RM-15, RM-19, RM-21, RM-55).
  3. **State builders.** R1 §4 extracts `build_state(cfg)` and `install_state(state)` out of
     `main()` **inside** `mcp_server.py`. Here they go into the new leaf `mcp_state.py` and are
     re-exported from `mcp_server`, so `mcp_server.py` shrinks instead of growing.
  4. **Dependency pins.** R1's `pyproject.toml` row (`mcp>=1.28,<2.0`) is **superseded**: `mcp`
     2.0.0 is installed (the Ingest spec's C1), so the pin stays `<3.0`, with the floor raised to the
     version the HTTP tests run on, in an evidence block.
  5. **The SDK audit is re-run first** (R1 §2), because it read `mcp` 1.28.0. Every SDK claim R1
     makes (the `transport_security` behaviour, `remove_tool`, the 421 response) is re-verified
     against the installed 2.x before code.
  The stdio path stays byte-for-byte unchanged, as R1 requires. Every confidential lens keeps its
  MCP default (exclude).
- **Security / privacy:** R1 §7. **Security review** with `ecc:security-reviewer` on opus.
- **Ceiling:** `mcp_server.py` shrinks (the state builders move to `mcp_state.py`; the hop is
  recorded). No `cli.py` change (update 1).
- **Acceptance criteria:**
  1. Every case in R1 §8 (its test plan) passes on the installed `mcp` 2.x.
  2. Each failure mode in R1 §6 has a test.
  3. Every registered tool, including the extension tools, is classified read or write, and an
     unclassified tool is withheld in read-only mode (a test registers a dummy tool and asserts it).
  4. The stdio protocol test is unchanged and green.
- **Test plan:** R1 §8, plus the classification test above.
- **Eval:** none (a transport; no ranking change).
- **Depends on:** RM-06.
- **Size:** L.
- **Risks:** R1 §6 and §7; the largest is a write tool exposed over a network, which read-only by
  default and `--allow-writes` address.
- **Decisions:** RM-46.D1, build now or keep deferred: **build, off by default**; nothing listens
  until `brain mcp serve` runs. R1 deferred it for lack of need, not for a rule, and the owner asked
  for the whole roadmap.

#### RM-47 — Multi-host use, without bidirectional sync

- **Source:** roadmap 6.4 ("WAL streaming or periodic dump-and-rsync … defer until 6.2 is solid").
- **Goal:** Use the brain from a second machine without ever having two writers.
- **Scope in:** `docs/guides/second-machine.md`; `backup/restore.py` (writes a marker); a `brain
  doctor` line. **Scope out:** two-way sync (RM-47.D1).
- **Design:** **one brain, reached from elsewhere**, which is RM-46, plus a documented one-way
  **read replica** recipe: `brain backup` on the primary, copy the archive, `brain restore` on the
  second machine into its own database, and treat that copy as read-only (writes there are lost at
  the next restore). `brain restore` today persists nothing from the archive's `manifest.json`, so it
  gains one step: after a successful restore it writes `$BRAIN_HOME/state/restored-from.json` with
  the manifest's `created_at` (the `BackupManifest` field) and the archive's file name. The doctor
  line reads that file and reports "read replica, restored from a backup taken at <created_at>".
- **Security / privacy:** a replica holds the whole corpus; the guide says to keep it on an encrypted
  disk and to treat the archive like the corpus.
- **Ceiling:** `backup/restore.py` (755) +≤ 15 (budget, §2.2).
- **Acceptance criteria:**
  1. After a restore, `restored-from.json` holds the manifest's `created_at`.
  2. The doctor line names it; without the file, the line is absent.
  3. The guide's recipe works end to end on the demo sandbox.
- **Test plan:** a restore test against a synthetic archive (the existing restore test fixtures); a
  doctor test reading a synthetic marker.
- **Eval:** none.
- **Depends on:** RM-46.
- **Size:** S.
- **Risks:** a user writes to the replica; the guide and the doctor line say it is read-only.
- **Decisions:** RM-47.D1, bidirectional sync: **never**. Two-way sync of a Postgres database plus a
  markdown vault is a conflict-resolution product: the vault watcher, the graph lane and
  content-hash dedup all assume one writer.

#### RM-48 — Splitting `cli.py` and `mcp_server.py` — **NEEDS USER CONFIRMATION** (§5, U1)

- **Source:** BL-2; CLAUDE.md's ceiling table (the `cli.py` row: "A split was scoped during the
  GraphRAG build (G0–G4) and deferred deliberately"); the ceiling rules ("splitting a 9,000-line CLI
  to satisfy a number is how you turn a working module into six broken ones").
- **Goal (only if confirmed):** smaller, cohesive CLI and MCP modules, with identical behaviour.
- **Scope in (only if confirmed):** moving whole existing Typer sub-apps and whole MCP tool families.
  **Scope out:** rewriting any function while it moves; standalone commands.
- **Design:** the conflict first: this would reverse a standing decision, and RM-06 already removes
  the reason to split (new code no longer lands in either file). The smallest scope, if the owner
  confirms: move the **`add_typer` sub-apps** out of `cli.py` (`vault`, `backfill`, `owner`,
  `claude`, `graphrag`, `elicit`, `capture`, `review`, `connect`, `gaps`, `demo`), one per PR, into
  `cli_<group>.py` modules registered at their current call positions through `cli_registry`, so
  `brain --help` order is unchanged. `enrich` and `eval` are standalone `@app.command`s, not
  sub-apps, and stay where they are. On the MCP side, move whole tool **families**
  (`brain_graphrag_*`, `brain_connect_*`, `brain_review_*`) into `mcp_tools/` modules through
  RM-06's `ToolContext`. Each PR is behaviour-identical, proven by a `brain --help` snapshot and an
  MCP `list_tools()` snapshot taken before and after.
- **Security / privacy:** none.
- **Ceiling:** every PR is **net negative** for `cli.py` or `mcp_server.py`; each promotes its row's
  descriptive hop and adds its own. A sub-app module that would itself exceed 800 lines (`graphrag`
  is the likely one) is split along its own sub-groups (`communities`, `aliases`) in the same PR.
- **Acceptance criteria:**
  1. The `brain --help` snapshot (every level) is identical before and after each PR.
  2. The `list_tools()` snapshot is identical before and after each PR.
  3. The full suite passes unchanged.
  4. No moved module exceeds 800 lines.
- **Test plan:** the two snapshots, plus the existing registration guard.
- **Eval:** none.
- **Depends on:** RM-06.
- **Size:** XL if confirmed (about eleven PRs).
- **Risks:** a moved command loses its help position, or a decorator binds to the wrong function (the
  `brain_recall` incident); the snapshots catch both.
- **Decisions:** RM-48.D1: **recommended, do not split** (§5, U1).

#### RM-49 — A hermetic CI eval gate — **NEEDS USER CONFIRMATION** (§5, U2)

- **Source:** CLAUDE.md "Eval gate (CI)", "Decision recorded (2026-10-12): … the user chose to leave
  the retrieval-quality gate local — there will be no hermetic CI gate"; also the self-hosted runner
  ("probably not worth it").
- **Goal (only if reopened):** CI catches ranking-code regressions without the owner's corpus.
- **Scope in (only if reopened):** a committed synthetic corpus and judgments, a CI job in
  `eval.yml`. **Scope out:** gating real retrieval quality, which only the local gate can measure.
- **Design:** the conflict first: this is a direct user decision. The smallest scope, if reopened: a
  committed synthetic corpus (about 60 documents) and a deterministic offline embedder (the
  `DemoEmbedder` `brain demo` already uses), with 15 synthetic queries and judgments, run by
  `eval.yml` on a hosted runner. It would gate **ranking-code regressions** (fusion, filters,
  recency, the pipeline stages), not retrieval quality on the real corpus.
- **Security / privacy:** synthetic data only.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. A deliberate RRF change on a test branch turns the hermetic job red.
  2. The local real-corpus gate is unchanged.
- **Test plan:** `tests/test_eval_workflow_contract.py` extended for the new job.
- **Eval:** this ticket is eval infrastructure; `ci.json` is untouched.
- **Depends on:** nothing.
- **Size:** M if reopened.
- **Risks:** the job is read as a quality gate; its name and docs must say "ranking-code regression".
- **Decisions:** RM-49.D1: **recommended, keep it closed** (§5, U2). The local gate plus RM-14c and
  RM-20's measurements cover every ranking change this program makes.

#### RM-50 — Self-hosted fonts

- **Source:** BL-7; consolidation spec Q14 ("Add it", with a fallback of system stacks and `--measure:
  34rem`).
- **Goal:** The reading page uses a real text face, delivered from the package, with no external
  origin.
- **Scope in:** `ui/security.py`'s `CSP`; `ui/static/fonts/` (the face and its licence);
  `pyproject.toml`'s `package-data`; a `@font-face` rule. **Scope out:** the instrument chrome (it
  keeps system stacks).
- **Design:** Q14's default: add `font-src 'self'` to `CSP`; ship **one** text face for the reading
  page ("Instrument & Page"), an OFL-licensed serif subset to Latin, about 120 KB of `woff2` at
  most, under `ui/static/fonts/` with its licence file; declare a **separate** `package-data` glob
  for `static/fonts/*` (setuptools globs do not cross `/`, the wheel-packaging lesson); `@font-face`
  with `font-display: swap` in `reading.css`. RM-05's test then guards every `url()`.
- **Security / privacy:** the CSP widens by `font-src 'self'` only; no external origin, no
  `unsafe-inline`.
- **Ceiling:** `ui/security.py` (243 lines) +1.
- **Acceptance criteria:**
  1. The built wheel contains the font (the packaging test).
  2. The CSP test expects the new directive and nothing else.
  3. No external origin appears in the static tree.
  4. The reading page's computed font family is the face; other chrome keeps system stacks.
- **Test plan:** the CSP header test; `tests/test_ui_static_assets.py` (the glob and RM-05's `url()`
  test); a hermetic browser row for the computed font family.
- **Eval:** none.
- **Depends on:** RM-05.
- **Size:** S.
- **Risks:** a face choice is taste.
- **Decisions:** RM-50.D1, which face: **the implementer proposes two OFL candidates with
  screenshots from the demo sandbox, and the reviewer picks**; no further question to the owner.

#### RM-51 — Retiring the tab strip — **NEEDS USER CONFIRMATION** (§5, U3)

- **Source:** BL-8; consolidation spec Q17 ("Three of four tabs are apologies"), recorded there as
  "*No default — your call.*"
- **Goal:** Q17 and BL-8 are closed with evidence, or answered by the user.
- **Scope in:** closure notes in the backlog and in the consolidation spec's §12. **Scope out:** any
  UI change, unless the user answers otherwise.
- **Design:** after RM-11 and RM-18b, the strip holds Notes, Ingest and Agent, all real; Publish was
  deleted in phases 8 and 9. Q17's premise (apology tabs) is gone. Because Q17 was left to the
  user, this ticket asks rather than closes on its own: recommended answer, **close as moot**.
- **Security / privacy:** none.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. The backlog row and Q17 record the user's answer and the evidence.
  2. `tests/test_ui_tabs_static.py` still pins the three tabs (if closed as moot).
- **Test plan:** none new.
- **Eval:** none.
- **Depends on:** RM-18b.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** RM-51.D1: **recommended, close as moot** (§5, U3).

#### RM-52 — `brain search --pick`

- **Source:** roadmap 7.1 ("an fzf-style picker"). `brain ui` meets most of the need; this is the
  terminal half, kept small.
- **Goal:** Pick a search result interactively in the terminal, and record the pick.
- **Scope in:** a new leaf `src/brain/search_pick.py`; `--pick` on `brain search` (`cli_search.py`);
  a `brain doctor` row for `fzf`. **Scope out:** a picker for other commands; making it the default.
- **Design:**
  - `--pick` works when stdout is a TTY and `fzf` is on `PATH`. The results are written to `fzf`'s
    stdin as tab-separated lines, `<id>\t<title>\t<source>`, with `--delimiter '\t' --with-nth 2..`
    (the id is hidden) and `--preview 'brain show {1}'`. fzf substitutes `{1}` as a quoted **field
    index**, so the id is never interpolated into a shell string by `brain`, and no document text is
    ever part of a command. `brain show` has no brief mode (its options are `--json`, `--query`,
    `--originating-query`, `--session-id` and `--graph-retrieved`), so the preview is the plain
    `brain show` output.
  - The chosen document's id is printed (so the command stays scriptable), and a `clicked`
    interaction is recorded with the query and a session id. CLI searches have no session today
    (`cli_search.search` passes `session_id=None` to `record_search_query`), so `--pick` mints a UUID,
    logs the search's `search_queries` row with it, and uses the same id for the `clicked` row;
    RM-22's engagement data can then tie the click to the search.
  - Without `fzf`, or without a TTY: exit 1 with an install hint or "--pick needs a terminal".
- **Security / privacy:** no shell interpolation of document text; only field indices reach fzf's
  preview command.
- **Ceiling:** `cli_search.py` +≤ 12 (budget, §2.2); `doctor_runtime.py` budget.
- **Acceptance criteria:**
  1. With a fake `fzf` runner that selects the second line, the second result's id is printed.
  2. The `search_queries` row and the `clicked` interaction share the minted session id.
  3. Without a TTY, or without `fzf`, the command exits 1 with the message.
  4. Without `--pick`, output and `search_queries` rows are unchanged (`session_id` stays `NULL`).
- **Test plan:** the cases above with an injected runner and TTY probe.
- **Eval:** none.
- **Depends on:** RM-09.
- **Size:** XS.
- **Risks:** `fzf` flag differences across versions; only long-stable flags are used.
- **Decisions:** RM-52.D1, the picker on by default for a TTY: **no**; it would change scripted
  output.

#### RM-53 — `brain examples`

- **Source:** roadmap 7.2.
- **Goal:** Find the right command by example.
- **Scope in:** `brain examples [TOPIC]` (through RM-06); a packaged resource
  `src/brain/resources/examples.md` with its own `package-data` glob. **Scope out:** generating
  examples from `docs/cli-reference.md`.
- **Design:** the command prints curated, grouped invocations from the resource, by topic: search,
  ingest, graph, brief and review, maintenance. Every example is synthetic (rule 15). A test parses
  every `brain …` line in the resource and asserts that its command path exists in the Typer app, so
  the examples cannot rot silently.
- **Security / privacy:** synthetic examples only.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. Each topic prints, and an unknown topic lists the valid ones.
  2. The rot test passes, and fails when an example names a command that does not exist.
  3. The installed wheel contains the resource.
- **Test plan:** the three criteria, including a packaging test.
- **Eval:** none.
- **Depends on:** RM-06.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** RM-53.D1, a curated resource or generated text: **curated** (shorter, and grouped by
  task).

#### RM-54 — Opt-in query rewriting (`--rewrite`)

- **Source:** roadmap 7.5 ("default off on CLI, default off on MCP; damages exact-name lookups").
- **Goal:** A vague query can opt into paraphrase retrieval, one run at a time.
- **Scope in:** a new leaf `src/brain/query_rewrite.py`; `--rewrite` on `brain search` and `brain
  explain`; a `CallBudget` entry in `ollama_budget.py`. **Scope out:** MCP (agents paraphrase for
  themselves); the UI; turning it on by default.
- **Design:** one `chat.chat_json` call with a JSON schema of up to two paraphrases produces them;
  the query is capped at `REWRITE_MAX_INPUT_TOKENS` (a named constant, 256) and the call's
  completion at `CHAT_DEFAULT_NUM_PREDICT`. That bounded call is declared as a `CallBudget` in
  `ollama_budget.capped_call_budgets`, so the existing load-time check in `config.py` validates it
  against `BRAIN_OLLAMA_NUM_CTX` without a new knob or a `config.py` edit. The pipeline runs the
  original and each paraphrase and fuses the document rankings with
  `rank_fusion.rrf_contribution`, the original weighted 1.0 and the paraphrases 0.5. `explain` shows,
  per result, which query found it. Chat down → the original search alone, plus a one-line notice;
  `OllamaContextOverflow` (impossible within the budget, but mapped anyway) → the same.
- **Security / privacy:** the query goes only to the local chat model.
- **Ceiling:** `cli_search.py` +≤ 12 (budget, §2.2).
- **Acceptance criteria:**
  1. Off unless `--rewrite`.
  2. With a fake chat, the paraphrase hits fuse as specified.
  3. Chat down or a context overflow → the original search, a notice, exit 0.
  4. `explain` names the query that found each result.
  5. The new `CallBudget` appears in `capped_call_budgets()` and fits the default `num_ctx`.
- **Test plan:** pure fusion tests; fake-chat surface tests; a budget test.
- **Eval:** measured once both ways and recorded in the PR; **never adopted as a default** (the
  roadmap's own ruling). `ci.json` unchanged.
- **Depends on:** RM-09.
- **Size:** S.
- **Risks:** latency (one chat call plus two extra searches); it is opt-in per run.
- **Decisions:** RM-54.D1, an MCP parameter: **no**.

#### RM-55 — Summarize a tag (`brain tags summarize`, MCP `brain_tags_summarize`)

- **Source:** BL-11, the MCP server spec's "Long-form `brain_summarize_tag(tag)` tool". The spec's
  other three out-of-scope items are covered elsewhere: HTTP transport (RM-46), reranking (RM-14),
  and per-document permission scoping (F6, Appendix A).
- **Goal:** A short, cited synthesis of what one tag's documents say.
- **Scope in:** a new leaf `src/brain/tag_summary.py`; `brain tags summarize TAG` (on RM-21's `tags`
  sub-app); `brain_tags_summarize` in `mcp_tools/tags.py`; a `CallBudget` entry in
  `ollama_budget.py`. **Scope out:** caching summaries.
- **Design:** take the N most recent documents carrying the tag (default 20) and their
  `documents.summary` values (titles where a summary is missing). The prompt is bounded: each summary
  is cut to 120 tokens and the whole input to `TAG_SUMMARY_MAX_INPUT_TOKENS` (a named constant,
  3,000), declared as a `CallBudget` as in RM-54. `chat.chat_json` returns a short synthesis with
  `[N]` citations, sanitized the way `ask` sanitizes answers. `OllamaUnavailable`,
  `OllamaContextOverflow` and `ChatOutputTruncated` map to fixed, class-only sentences in the style
  of RM-02's `describe_ask_failure`, and exit 1 (CLI) / `INTERNAL_ERROR` (MCP). Summaries are body-derived, so the MCP lens excludes
  confidential documents from the **input**, not just from the output.
- **Security / privacy:** the MCP lens on the input documents; the chat model is local.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. The synthesis cites only in-range documents.
  2. MCP excludes confidential inputs unless `include_confidential`.
  3. Ollama down, a context overflow and a truncated output each give their own fixed sentence.
  4. The `CallBudget` fits the default `num_ctx`.
- **Test plan:** fake-chat tests on the CLI and MCP paths; a budget test.
- **Eval:** none.
- **Depends on:** RM-21, RM-02.
- **Size:** S.
- **Risks:** a very large tag; N caps the input.
- **Decisions:** RM-55.D1, N: **20**.

#### RM-56 to RM-60 — Trigger-gated items

Each is recorded with its trigger, the command that shows whether the trigger holds, its scope and
the design to build when it does. They are **not built** until then; that is the recommended
default, and it follows the condition each source attached. The tracker marks them
*trigger-gated*.

| Ticket | Item (source) | Trigger | How to check | Scope and design when triggered | Size |
|---|---|---|---|---|---|
| RM-56 | MCP twin of `brain eval` (Q1-B "MCP") | The owner starts asking agents to run evals in chat | A request to that effect | In: `mcp_tools/eval.py`, `brain_eval_diff(baseline="ci")`, read-only, never records, returning the three mean deltas and the changed config keys. Out: recording baselines over MCP. | S |
| RM-57 | `--tier vault\|ingested` filter (Q1-C D11) | A request to filter by tier | A request | In: `build_predicate` gains `tier` → `d.kind = %s`; the CLI, MCP `brain_search` and the UI filter row; recorded growth of `search.py` and `mcp_server.py`. Out: new tiers. | XS |
| RM-58 | Pruning `interactions` (Q1-C non-goals) | The table passes 1,000,000 rows or 500 MB | `brain usage --engagement` (RM-22a reports `interactions_rows` and `interactions_bytes`) | In: a separate command, `brain interactions prune --older-than 2y [--apply]` (through RM-06; `brain usage` stays a single command, RM-22a.D2), dry-run by default, never deleting ratings or pins. Out: pruning `search_queries`. | S |
| RM-59 | Cap on the auto-tag vocabulary prompt (Q1-D D18) | The vocabulary sent by `brain tag --auto` outgrows D18's threshold | `brain tags list --json` (RM-21) gives the vocabulary size | In: send the top-K tags by frequency plus tags co-occurring with the document's nearest neighbours. Out: changing the tag model. | S |
| RM-60 | `documents.parent_document_id` (Q1-D D17) | A feature needs parent/child documents | A ticket that needs it (RM-45 deliberately does not) | In: a migration at the next free number at merge (§2.8), a nullable self-reference with `ON DELETE SET NULL` and an index. Out: any UI. | S |

**Fields shared by RM-56 to RM-60.** **Security / privacy:** RM-56 is read-only; the others are
local. **Ceiling:** none beyond the growth each row names, recorded when built. **Acceptance
criteria and test plan:** written in the ticket's plan when its trigger fires, following §2.1, and
numbered there. **Eval:** none. **Depends on:** RM-58 on RM-22a and RM-06, RM-59 on RM-21, RM-56
on RM-06, the rest on nothing. **Risks:** building them early adds surface nobody uses. **Decisions:** wait for the
trigger.

#### RM-61 — A background enrichment daemon (closed: not needed)

This ticket is a **closure**: it has no goal, design, dependency or test plan of its own, only the
ruling and where it is recorded.

- **Source:** Q1-D non-goal; backlog §1.8 ("NOT STARTED, and not needed").
- **Ruling:** close it. The post-ingest hook writes summaries, and `brain enrich --backfill` repairs
  the tail. The one gap a daemon would close, summaries lost while Ollama was down, is small and
  visible in `brain status`. If it ever matters, the cheapest fix is one line in the brief daemon
  script (`brain enrich --backfill --limit 50`), not a new daemon.
- **Scope in:** the closure note in the backlog. **Scope out:** any code.
- **Ceiling:** none.
- **Acceptance criteria:**
  1. The backlog row is closed with this ruling.
- **Size:** XS.
- **Risks:** none.
- **Decisions:** RM-61.D1: **close**.

---

## 4. Execution plan

### 4.1 Waves, and why in this order

| Wave | Tickets | Why here |
|---|---|---|
| 1 | RM-01, RM-02, RM-03, RM-04, RM-05 | Backlog ranks 1a, 2, 3. The fallback decision is fresh; the guards are tiny and close recorded holes. |
| 2 | RM-06, RM-07, RM-08, RM-09 | Inserted ahead of rank 4 (argued at the wave's head): every later wave then lands in leaves instead of over-ceiling files. RM-09 needs Wave 1's three surfaces settled. |
| 3 | RM-10, RM-11, RM-12, RM-13 | Ranks 4, 5, 6. The only feature already designed; it delivers the job registry the Agent tab needs. RM-12 is pure parity. |
| 4 | RM-14a, RM-14b, RM-14c, RM-15, RM-16, RM-17, RM-18a, RM-18b, RM-18c | Ranks 7–10. The reranker is measurable now; saved searches and delivery are small; the Agent tab builds on Wave 3. RM-15 carries the `brief --wiki` extraction RM-16 and later tickets build on. |
| 5 | RM-19, RM-20, RM-21, RM-22a, RM-22b, RM-23, RM-24 | Ranks 11–15 and 17. RM-20 and RM-22b are measured on top of RM-14c's outcome; RM-22b waits for its data gate; RM-24 waits for U4. |
| 6 | RM-25, RM-26, RM-27, RM-28, RM-29 | Rank 16, scheduled rather than opportunistic, because the owner asked for the whole backlog. RM-25 needs RM-19's shingles. |
| 7 | RM-30 … RM-40 | Rank 18. Shared machinery first (OCR, then the drop-folder runner). |
| 8 | RM-41 … RM-45 | Rank 19. Heaviest privacy load, least evidence of need. |
| 9 | RM-46, RM-47, RM-50, RM-52, RM-53, RM-54, RM-55, RM-61; RM-48, RM-49 and RM-51 per §5; RM-56–RM-60 on their triggers | Rank 20 and the "leave deferred" row. |

### 4.2 What may run in parallel, and what must merge one at a time

Two separate rules govern ordering:

1. **What can start** is decided by each ticket's "Depends on" (Appendix B). A ticket whose
   dependencies have merged may be developed in its own worktree, beside others (memory note on
   parallel agents: one worktree each, broad test suites run one at a time).
2. **What must merge one at a time** is decided by the shared files below. Two tickets that share a
   file are merged in sequence, and the second rebases on the first before its final CI run. Sharing
   a file never forces serial *development* by itself; a "Depends on" does.

| Shared file or resource | Tickets that touch it |
|---|---|
| `CLAUDE.md` (ceiling-table rows; the BL-1 paragraph) | RM-04, and every ticket that touches an over-ceiling file: RM-01, RM-02, RM-06, RM-07, RM-09, RM-14c (if adopted), RM-15, RM-20, RM-21, RM-24, RM-26, RM-37, RM-42, RM-45, RM-46, RM-48, RM-57. Each promotes the row's last hop before adding its own. |
| `mcp_server.py` | RM-01, RM-02, RM-06, RM-09, RM-24, RM-46, RM-48, RM-57 |
| `cli.py` | RM-02, RM-06, RM-15, RM-20, RM-21, RM-48 |
| `ingest/__init__.py` | RM-07, RM-42 |
| `search.py` | RM-20, RM-24, RM-57 |
| `cli_ingest.py` | RM-07, RM-37, RM-45 |
| `vault/watch.py`, `vault/move_identity.py` | RM-25, RM-26 |
| `setup.py` | RM-14c (if adopted) |
| `backup/restore.py` | RM-20, RM-47 |
| `cli_registry.py` | RM-06, then every command added through the seam: RM-15, RM-19, RM-20, RM-21, RM-30, RM-31, RM-33, RM-35, RM-36, RM-41, RM-43, RM-44, RM-46, RM-53, RM-58 (if triggered) |
| `mcp_tools/__init__.py` | RM-06, then RM-12, RM-15, RM-19, RM-21, RM-55, RM-56 |
| `search_pipeline.py` | RM-09, then RM-14a, RM-20, RM-22b, RM-54 |
| `cli_search.py` (budget, §2.2) | RM-03 (no change), RM-09, RM-12, RM-14a, RM-24, RM-52, RM-54; each checks the remaining budget at rebase |
| `ask.py` | RM-02, RM-09, RM-18a, RM-18c |
| `brief.py`, `brief_publish.py` | RM-15, RM-16, RM-17, RM-19 |
| `templates/bin/_brain-brief-fg.sh` | RM-19 |
| `source_kinds.py`, and the per-kind tables in `results.js`, `discovery.js`, `tree.js` | RM-30, RM-31, RM-33, RM-34, RM-35, RM-36, RM-38, RM-41, RM-43, RM-44 |
| `ingest/dispatch.py` registrations | RM-07, then RM-33, RM-38 |
| `graph_rag/extract_policy.py`, `graph_rag/reconcile_plan.py`, `graph_rag/reconcile.py` | RM-27 (`reconcile_plan.py`), RM-30 (creates the policy and wires it), then RM-33, RM-41, RM-43, RM-44 (add types; bump the policy version) |
| `ui/static/index.html` | RM-03, RM-11, RM-18b |
| `ci.yml` (browser roster, lint job) and `tests/test_ci_workflow.py` | RM-03, RM-04, RM-11, RM-13, RM-18b, RM-23, RM-40, RM-50, RM-49 (if reopened) |
| `ui/routes_ingest.py`, `ui/static/js/ingest.js` | RM-10, RM-11, RM-13, RM-40 |
| `skills/ingest-brain/SKILL.md` | RM-31, RM-34 |
| `doctor_runtime.py` (budget, §2.2) | RM-14b, RM-16, RM-31, RM-32, RM-33, RM-36, RM-38, RM-43, RM-47, RM-52 |
| `pyproject.toml` | RM-32, RM-46, RM-50, RM-53 |
| `bin/launchd.py`, `bin/brain-install-launchd`, `bin/brain-uninstall-launchd` | RM-14b, RM-33 |
| `docs/configuration.md` | every ticket with a knob or an MCP tool: RM-08, RM-12, RM-14a, RM-15, RM-16, RM-19, RM-20, RM-21, RM-22b, RM-32, RM-33, RM-38, RM-46, RM-55 |
| `CHANGELOG.md` (Unreleased) | every ticket; conflicts are routine, and the rebase resolves them |
| Migrations | RM-15 (029), RM-19 (030), RM-20 (031); conditional tickets take the next free number at merge (§2.8) |
| The live machine (eval runs, daemon reinstall) | RM-14c, RM-20, RM-22b and RM-43's eval steps; every `src/` merge's reinstall. One at a time, by the team lead, never during another ticket's run of record. |

**Parallel development sets**, by wave, once their dependencies have merged. Members of a set may be
developed at once; they merge **in the order listed**, because each set shares at least one
merge-order file above.

| Wave | Set (merge order) | Shared merge-order files |
|---|---|---|
| 1 | RM-01, RM-02, RM-03, RM-04, RM-05 | `mcp_server.py` (RM-01, RM-02); `ci.yml` (RM-03, RM-04); `CLAUDE.md` (RM-01, RM-02, RM-04) |
| 2 | RM-06, RM-07, RM-08; then RM-09 (depends on Wave 1) | `CLAUDE.md`, `cli_registry.py` |
| 3 | RM-10 → RM-11 → RM-13 (a dependency chain) beside RM-12 | `ci.yml`, `docs/configuration.md` |
| 4 | RM-14a, RM-15, RM-17; then RM-14b, RM-16, RM-18a; then RM-14c, RM-18b; then RM-18c | `brief.py` (RM-15, RM-17), `search_pipeline.py`, `cli_registry.py` |
| 5 | RM-19, RM-21, RM-22a, RM-23; then RM-20, RM-24 | `cli_registry.py`, `mcp_tools/__init__.py`, `CLAUDE.md` |
| 6 | RM-27, RM-28, RM-29 beside RM-25 → RM-26 | `CLAUDE.md` (RM-26) |
| 7 | RM-30, RM-31, RM-34; then RM-32; then RM-33; then RM-35, RM-36, RM-38, RM-39; then RM-37, RM-40 | `source_kinds.py` and the per-kind tables, `cli_registry.py`, `doctor_runtime.py` |
| 8 | RM-41, RM-42, RM-44, RM-45; then RM-43 | as Wave 7 |
| 9 | RM-46, RM-50, RM-52, RM-53, RM-54, RM-55; then RM-47 | `pyproject.toml`, `cli_search.py`, `cli_registry.py` |

### 4.3 Migration allocation

§2.8 is authoritative: 029 (RM-15), 030 (RM-19), 031 (RM-20). Conditional tickets (RM-22b, RM-60) and
any other ticket that discovers a need take the next free number when they merge, and update §2.8 in
that PR; no number is held in reserve.

---

## 5. Open questions — the NEEDS USER CONFIRMATION set

Four tickets need the user. Two would change a standing user decision; two answer a question the
user's own sources left to the user. Every other decision in this document carries a recommended
default and needs no answer.

| # | Ticket | Why it needs the user | Recommended answer |
|---|---|---|---|
| U1 | RM-48: split `cli.py` / `mcp_server.py` | The split was "deferred deliberately" (CLAUDE.md, ceiling table), and the ceiling rules forbid splitting to satisfy a number. | **Do not split.** RM-06's seam stops the growth; extract a sub-app only when a ticket already changes most of it. |
| U2 | RM-49: a hermetic CI eval gate | On 2026-10-12 the user chose to keep the retrieval-quality gate local, with no hermetic CI gate. | **Keep it closed.** The local gate covers every ranking change here. |
| U3 | RM-51: retire the tab strip | Consolidation Q17 is recorded as "No default — your call". | **Close as moot.** After RM-11 and RM-18b, all three tabs are real. |
| U4 | RM-24: the `--entity` search filter | The backlog's rank 17 says to ask whether it is wanted before building it. | **Build it.** It is small (S), and the owner asked for the whole roadmap. |

Two choices are **not** questions, because the owner already decided them on 2026-05-11 and this
document follows them: browser history is built, allowlist-only (RM-41), and the trackers are Notion
and Jira (RM-34, RM-35). Brief delivery (RM-16) and the MCP HTTP transport (RM-46) send content off
the machine only when the owner turns them on; both default off. In particular, the brief-delivery
channel, which the backlog (rank 10) calls a product decision, is decided by the owner setting
`BRAIN_BRIEF_DELIVERY` (`none`, `email` or `slack`); building the opt-in needs no answer first,
which is why RM-16 is not a fifth question.

---

## Appendix A — Already done (not ticketed)

| Item | Evidence |
|---|---|
| 1.1 Eval harness, `--fail-below`, committed `ci.json` | `src/brain/eval/`, `brain eval`, `9ae9580`; all 31 rows re-judged (PRs #41, #50). |
| 1.2 `brain explain` | `cli_search.explain`, `SearchExplanation`, `e944972`. (The MCP twin is RM-12.) |
| 1.3 Interactions table (phase 1) | Migration 010, `brain rate`, MCP `brain_rate`, UI opens (`ui/telemetry.py`, migration 024). |
| 1.4 Metadata filters | `resolve_person_to_keys` and the `hybrid_search` filter kwargs (`63857ce`, `c7b726f`, `6c2f6eb`); updated-range filters (migration 025). |
| 1.5 Recency boost; 1.6 snippet context | `e9964a8`; `snippet_context.py`. |
| Q1-CLOSEOUT, the cross-wave review and its remediation waves | Backlog §1.1: the cross-wave audit named in CLAUDE.md ("Wave A.1 source"); A.1, the eval workflow (`.github/workflows/eval.yml`); A.2, the person-name variants (`6c2f6eb`); A.4, the committed `tests/eval/baselines/ci.json`. A.3, the tracker reconciliation, is the backlog itself. The Q1 manual-QA page is superseded (it targeted the deleted wiki). |
| 2.2 Email drafts | `8c9a042`. |
| 2.7 Krisp action items | `brain enrich --krisp-action-items`, `brain todo` (`3ed57d0`). |
| 3.1 Auto-summary; 3.2 auto-tags | Migration 011, `OllamaEnricher`, `brain tag --auto`. |
| Q1-D D16, the summary in `brain_search` results | Shipped as the `brief` projection (`brain_search(brief=True)`; backlog §1.3 and §1.8). |
| 3.3 Entity extraction (reshaped) | GraphRAG on AGE (`15a69e1`). (The `--entity` leg is RM-24.) |
| 3.7 Contradiction / change detection | `brain review scan …` (`9762a26`, `e1950b9`). |
| 4.1 Daily / weekly digest (reshaped) | `brain brief`, the `com.brain.brief` launchd job, `brain review weekly`. (Delivery is RM-16; new entities RM-17.) |
| 4.2 Proactive link suggestions (reshaped) | `brain connect` (`9c985ad`). |
| 4.3 `brain ask` | `ask.py`, `brain ask`, `brain_ask` (`ac2a5cf`, `6275165`). |
| 6.1 MCP write parity, except `brain_related` | `brain_rm`, `brain_timeline`, `brain_rate`, `brain_tag`, `brain_edit`, `brain_note_*`. |
| 6.2 Backup + restore | `brain backup` / `brain restore` (F3). |
| 6.3 Self-telemetry (reshaped) | `brain usage` (F7), migrations 024 and 027. |
| H-7 Text-only search fallback | PR #61, merged at `02c2abc`: `search_fallback.py`, `brain search`, `brain explain`, MCP `brain_search`. This PR flips the backlog's H-7 rows to SHIPPED. |
| Per-document permission scoping (MCP spec "Out of scope") | Met for a single-user product by F6 sensitivity (migration 026) and every MCP tool's confidential lens. |
| Superseded: the wiki click endpoint, the Publish tab, the Quartz summary lede, consolidation Q6 | The wiki was deleted at R3 (PR #43); `brain ui` logs opens and renders the lede; the Publish tab was removed in phases 8 and 9. |

## Appendix B — Ticket tracker

Every ticket starts at `TODO`. A ticket PR moves its own row to `IN PROGRESS` and then `DONE (#PR)`.
A title marked *NEEDS USER CONFIRMATION* waits for §5's answer; *trigger-gated* waits for the trigger
in its ticket; a *closure note* ticket is done when its PR records the closure.

| Id | Title | Size | Wave | Depends on | Status |
|---|---|---|---|---|---|
| RM-01 | `brain recall` text-only fallback | S | 1 | — | DONE (#63) |
| RM-02 | `brain ask` fallback and clean errors | S | 1 | — | DONE (#64) |
| RM-03 | `brain ui` search fallback notice | S | 1 | — | DONE (#65) |
| RM-04 | Ceiling-table CI check | S | 1 | — | TODO |
| RM-05 | CSS `url()` offline test | XS | 1 | — | TODO |
| RM-06 | CLI and MCP registration seams | S | 2 | — | TODO |
| RM-07 | Extractor registry, one kind definition, BL-12 | S | 2 | — | TODO |
| RM-08 | Feature settings leaves | XS | 2 | — | TODO |
| RM-09 | One search pipeline | S | 2 | RM-01, RM-02, RM-03 | TODO |
| RM-10 | Ingest tab phase 1: jobs + paste backend | M | 3 | — | TODO |
| RM-11 | Ingest tab phase 2: paste front end | M | 3 | RM-10 | TODO |
| RM-12 | MCP `brain_related` + `brain_explain` | S | 3 | RM-06, RM-09 | TODO |
| RM-13 | Ingest tab phase 3: file upload | M | 3 | RM-11, RM-07 | TODO |
| RM-14a | Reranker stage, backends, eval wiring (off) | M | 4 | RM-08, RM-09 | TODO |
| RM-14b | Local reranker runtime | S | 4 | RM-14a | TODO |
| RM-14c | Reranker measurement and adoption | S | 4 | RM-14b | TODO |
| RM-15 | Saved searches and watch lists (with the `brief --wiki` extraction) | M | 4 | RM-06, RM-09 | TODO |
| RM-16 | Brief delivery (email to self / Slack webhook) | S | 4 | RM-08, RM-15 | TODO |
| RM-17 | Brief "new this week" entities | S | 4 | — | TODO |
| RM-18a | Agent tab backend | M | 4 | RM-10, RM-02 | TODO |
| RM-18b | Agent tab front end and output hardening | M | 4 | RM-18a, RM-11 | TODO |
| RM-18c | Agent tab follow-up questions | S | 4 | RM-18b | TODO |
| RM-19 | Near-duplicate detection | M | 5 | RM-06 | TODO |
| RM-20 | Document-level embedding stage (off) | M | 5 | RM-09, RM-14c | TODO |
| RM-21 | Tag rename, merge, suggest-merges | S | 5 | RM-06 | TODO |
| RM-22a | Engagement readout and gate | S | 5 | — | TODO |
| RM-22b | Engagement ranking stage (off; built when RM-22a's gate is met) | M | 5 | RM-22a, RM-09, RM-14c | TODO |
| RM-23 | Copy-path affordance | XS | 5 | — | TODO |
| RM-24 | `--entity` search filter (NEEDS USER CONFIRMATION) | S | 5 | RM-09 | TODO |
| RM-25 | H-1 move identity | S | 6 | RM-19 | TODO |
| RM-26 | H-2 + H-4 owed watcher work | S | 6 | — | TODO |
| RM-27 | H-3 graph-lock residuals | S | 6 | — | TODO |
| RM-28 | H-5 log-privacy guard | S | 6 | — | TODO |
| RM-29 | H-6 class-only error reports | XS | 6 | — | TODO |
| RM-30 | Calendar events | M | 7 | RM-06, RM-07 | TODO |
| RM-31 | Code-host activity | M | 7 | RM-06, RM-07 | TODO |
| RM-32 | OCR and image-only PDFs | M | 7 | RM-07, RM-08 | TODO |
| RM-33 | Images and the drop-folder runner | M | 7 | RM-06, RM-32 | TODO |
| RM-34 | Jira issues | S | 7 | RM-07 | TODO |
| RM-35 | Notion export | M | 7 | RM-06, RM-07 | TODO |
| RM-36 | Apple Notes | M | 7 | RM-06, RM-07 | TODO |
| RM-37 | External vault sync with deletions | M | 7 | RM-07 | TODO |
| RM-38 | Voice memos | M | 7 | RM-33 | TODO |
| RM-39 | Mobile share-sheet capture | S | 7 | RM-33 | TODO |
| RM-40 | Ingest tab phase 4: inbox folder | S | 7 | RM-13, RM-33 | TODO |
| RM-41 | Browser history and bookmarks | L | 8 | RM-06, RM-07 | TODO |
| RM-42 | Code-aware chunker | M | 8 | RM-07 | TODO |
| RM-43 | Code-repository ingestion | M | 8 | RM-06, RM-42 | TODO |
| RM-44 | Social bookmarks | S | 8 | RM-06, RM-07 | TODO |
| RM-45 | Receipts (Gmail attachments) | M | 8 | RM-32 | TODO |
| RM-46 | MCP over HTTP | L | 9 | RM-06 | TODO |
| RM-47 | Multi-host via one brain + read replica | S | 9 | RM-46 | TODO |
| RM-48 | Split `cli.py` / `mcp_server.py` (NEEDS USER CONFIRMATION) | XL | 9 | RM-06 | TODO |
| RM-49 | Hermetic CI eval gate (NEEDS USER CONFIRMATION) | M | 9 | — | TODO |
| RM-50 | Self-hosted fonts | S | 9 | RM-05 | TODO |
| RM-51 | Retire the tab strip (NEEDS USER CONFIRMATION; closure note if answered as recommended) | XS | 9 | RM-18b | TODO |
| RM-52 | `brain search --pick` | XS | 9 | RM-09 | TODO |
| RM-53 | `brain examples` | XS | 9 | RM-06 | TODO |
| RM-54 | Opt-in `--rewrite` | S | 9 | RM-09 | TODO |
| RM-55 | Summarize a tag | S | 9 | RM-21, RM-02 | TODO |
| RM-56 | MCP twin of `brain eval` (trigger-gated) | S | 9 | RM-06 | TODO |
| RM-57 | `--tier` filter (trigger-gated) | XS | 9 | — | TODO |
| RM-58 | Prune `interactions` (trigger-gated) | S | 9 | RM-22a, RM-06 | TODO |
| RM-59 | Auto-tag vocabulary cap (trigger-gated) | S | 9 | RM-21 | TODO |
| RM-60 | `documents.parent_document_id` (trigger-gated) | S | 9 | — | TODO |
| RM-61 | Background enrichment daemon (closure note, no code) | XS | 9 | — | TODO |
