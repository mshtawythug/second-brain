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
- **§5** is the short list of tickets that would change a standing user decision. Everything else
  carries a recommended default and needs no question.
- **Appendix B** is the tracker. Its status column is the only part of this document a ticket PR
  edits after approval.

### 0.1 Cross-cutting decisions

| # | Question | Decision | Section |
|---|---|---|---|
| G1 | How do new CLI commands and MCP tools avoid growing `cli.py` (8,819 lines) and `mcp_server.py` (5,078)? | A **registration seam** lands first (RM-06). After it, a new command module touches `cli_registry.py` only, and a new MCP tool lives in `src/brain/mcp_tools/` and is registered without editing `mcp_server.py`. The existing files are **not split** (that is RM-48, which needs the user). | §2.2, RM-06 |
| G2 | How do new ingest extractors avoid growing `ingest/__init__.py` (2,369)? | The extension dispatcher `_EXTRACTORS` / `extract_path` moves to a new leaf, `ingest/dispatch.py`, with a registration function (RM-07). `ingest/__init__.py` re-exports it and shrinks. | RM-07 |
| G3 | How do new knobs avoid growing `config.py` (2,669)? | New knob groups live in **feature settings leaves** under `src/brain/settings/`, read from `os.environ` **after** `Config.load()` has published the dotenv chain into it (`config.py`, the `os.environ.setdefault` loop in `_load_field_dict`). `config.py` is not grown for any ticket here (RM-08). | §2.3, RM-08 |
| G4 | How do ranking stages (fallback, reranker, document leg, engagement) compose without each surface wiring them separately? | One composition function, `brain.search_pipeline.run_search` (RM-09), called by every user-facing surface. No stage edits the ranking code in `search.py` (823, over the ceiling); RM-20 adds only a two-line public alias there. `brain eval` calls the stages it measures directly, and never the fallback. | §2.7, RM-09 |
| G5 | How is a new source added? | A fixed eight-step protocol (§2.4): extractor or stdin recipe, dispatcher or source-kind entry, credentials, a **mandatory scope flag**, PII rules, synthetic fixtures, dependency evidence, docs. Prefer the **agent-orchestrated MCP → `brain ingest-stdin`** pattern (as Krisp and Slack do) over a new API client. | §2.4 |
| G6 | Where do long-running UI tasks run? | On the Ingest spec's `JobRegistry` (Ingest spec §4), reused by the Agent tab. One worker for every job kind. | RM-10, RM-18 |
| G7 | Reranker runtime | Ollama has **no rerank endpoint** (upstream issue ollama/ollama#3368 is still open, and #16076, opened in 2026, says there is no `/api/rerank`). The local backend is **`llama-server` from llama.cpp in `--reranking` mode** serving `bge-reranker-v2-m3` (an external binary over HTTP, no new Python dependency). Voyage `rerank-2.5` is the hosted fallback, through the already-declared `voyageai`. **Default OFF** until `brain eval` proves a gain. | RM-14 |
| G8 | Brief delivery channel | **Email to the owner's own address through the existing `gws` CLI**, opt-in, default off. Slack is supported through an incoming webhook to a private channel. The `#team` channel is **rejected**: it is shared, and a personal brief is not a team post. | RM-16 |
| G9 | Saved-search notification | Through the **daily brief**: a "Watch hits" section, delivered by whatever G8 channel is enabled. There is no per-ingest push. | RM-15 |
| G10 | Agent tab transport | **Polling** the job registry, as the Ingest tab does. **No NDJSON stream in v1**: `ask`'s LLM steps are non-streaming JSON calls (`chat.chat_json`), so a stream would carry the same five to eight step events polling already delivers. | RM-18 |

---

## 1. Scope and inventory

### 1.1 What was read

- The backlog (§1, §2, §3), at `0bf3e36`, and re-checked against `02c2abc` for this document. The
  only change between the two commits that touches the backlog is PR #61: H-7 is now **shipped**
  (Appendix A).
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
| `--entity` search filter | 3.3 (missing leg) | PARTIAL | 17 | RM-24 |
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
| Retire the tab strip | BL-8, consolidation Q17 | Superseded in part | leave | RM-51 |
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
adds the five program-level policies (§2.2–§2.7) that the tickets rely on.

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
    PR head. Commits and pushes need the user's permission (rule 4); merges are the team lead's.
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
(re-derived with `find src -name '*.py' -exec wc -l {} + | sort -rn`). This program would touch six
of them. The seams in Wave 2 exist so that each is touched rarely, and so that most touches are net
zero or net negative.

| Over-ceiling file | Tickets that must touch it | How growth is avoided |
|---|---|---|
| `cli.py` | RM-02 (net negative), RM-06 (one call), RM-16 (net zero or negative), RM-21 (net negative), RM-48 (only if confirmed) | RM-06's seam means later command modules register through `cli_registry.py` only. RM-02 replaces three `except` branches with one. RM-16 moves the `brief --wiki` branch body into a leaf. RM-21 moves the tag-mapping engine out. |
| `mcp_server.py` | RM-01 (net negative), RM-02 (about two lines), RM-06 (two lines), RM-09 (net zero), RM-24 (one parameter) | New tools go to `src/brain/mcp_tools/` (RM-06). RM-24's `entity` parameter on `brain_search` cannot avoid the file, because that tool lives there; it is recorded. |
| `ingest/__init__.py` | RM-07 (net negative), RM-42 (net zero: two calls re-pointed) | The dispatcher moves to `ingest/dispatch.py`; every later extractor registers there. RM-42's chunker choice lives in `ingest/chunkers.py`. |
| `config.py` | none | Feature settings leaves (RM-08). |
| `search.py` | RM-20 (a two-line public alias for the query-embed cache), RM-24 (two keywords through to the predicate), RM-57 (only if triggered) | Every ranking stage composes in `search_pipeline.py` (RM-09); no stage edits the ranking code. |
| `cli_ingest.py` | RM-07 (re-pointed import, net zero), RM-37 (`--sync` options, recorded), RM-45 (`--with-attachments`, recorded) | RM-37's prune logic is in `ingest/import_sync.py`; RM-45's attachment logic is in `ingest/gmail_attachments.py`. |
| `vault/watch.py` | RM-26 (wiring, about 15 lines) | The logic goes to a new leaf beside it (`vault/owed_work.py`); `watch.py` gets wiring only, recorded. RM-25 changes only the `vault/move_identity.py` leaf. |
| `graph_rag/extract.py`, `vault/sync.py`, `vault/graph.py`, `queries.py`, `setup.py`, `connect.py`, `people.py`, `timeline.py`, `enrichment.py` | none planned | — |

**Near-ceiling files with a budget.** These are under 800 today and several tickets add to them.
The budget is a cap the reviewer checks; a ticket that would exceed it extracts first.

| File | Lines at `02c2abc` | Budget | Spent by |
|---|---|---|---|
| `cli_search.py` | 728 | +60 in total | RM-14a (`--rerank/--no-rerank`, ≤ 12), RM-24 (`--entity`, ≤ 15), RM-52 (`--pick`, ≤ 12), RM-54 (`--rewrite`, ≤ 12). Logic in leaves (`search_pick.py`, `query_rewrite.py`). If any ticket would push it past 790, that ticket first moves `explain` into `cli_explain.py`. |
| `ui/render.py` | 799 | 0 | No ticket here renders new markdown server-side. RM-18b renders model text client-side with `textContent`. |
| `vault/export.py` | 764 | net zero | RM-29 rewrites in place. |
| `ui/notes_service.py` | 711 | 0 | Ingest lives in `ui/ingest_service.py` (Ingest spec §1.3). |
| `ui/queries.py` | 724 | +50 | RM-15 (watch-hit titles, if the UI shows them), RM-19 (duplicate pairs). New SQL beyond that goes to a new `ui/queries_*.py` leaf. |
| `ask.py` | 654 | +120 | RM-02 (≤ 30), RM-18a (`on_step`, ≤ 25), RM-18c (`history`, ≤ 50). |

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
# each raises brain.errors.ConfigError naming the variable, never echoing a secret value
```

Each feature has a frozen dataclass and a loader, `load_<feature>_settings(env: Mapping[str, str])
-> <Feature>Settings`. Callers pass `os.environ` **after** `Config.load()`, which has already
published the dotenv chain into the process environment (`Config._load_field_dict`, the
`os.environ.setdefault` loop). Tests pass a plain dict, so no test touches the real environment.
Secrets (webhook URLs, API tokens) are fields with `repr=False`, and `brain doctor` reports only
whether they are set.

### 2.4 The new-source protocol (G5)

Every source ticket (RM-30 … RM-45) answers these eight points, in this order, in its design:

1. **Path.** One of: (a) *agent-orchestrated*: an MCP connector the owner already has fetches the
   content, and a skill recipe pipes it into `brain ingest-stdin --source <kind>`, as Krisp and
   Slack do; (b) *CLI shell-out*: an authenticated local CLI the machine already has (`gws`, `gh`),
   wrapped like `ingest/gmail.py`, with an injectable runner for tests; (c) *local files*: an
   extractor registered with `ingest/dispatch.py` (RM-07). Prefer (a), then (b). A new HTTP client
   to a SaaS API is a last resort and needs a written reason.
2. **Source kind.** A new member of `brain.source_kinds.VALID_SOURCE_KINDS`. After RM-07 that is
   the only place a kind is defined (`vault/links._SOURCE_KINDS` stops being a second copy).
   `ui.schemas.VALID_SOURCE_KINDS` re-exports it, so the facet panel and the source dropdown pick it
   up with no UI change. The kind is a single lowercase word, because it becomes the mirror
   directory `_ingested/<kind>/`.
3. **Dedup key.** `(kind, external_id)` for sourced stdin, `source_path` for files, `content_hash`
   as the fallback (the four rules in `ingest_document`'s docstring). The ticket names which.
4. **Credentials.** Never stored by `brain`. Path (a) uses the connector's own auth; path (b) uses
   the CLI's own login (`gws auth`, `gh auth`); a secret that must exist (a webhook URL) lives in
   `$BRAIN_HOME/.env`, is read through a settings leaf, and is never logged.
5. **A mandatory scope flag.** Like `brain ingest-gmail`, which refuses to run without one of
   `--query/--label/--from/--since/--until` (`cli_ingest.ingest_gmail`). No source ever bulk-imports
   an account. The ticket names the flags and the refusal message.
6. **PII and sensitivity.** What is stored, what is dropped, and the default sensitivity. Any source
   that is mostly third-party content (calendar attendees, issue comments) defaults its documents to
   `normal` but drops fields the search does not need (raw email addresses of attendees are reduced
   to display names, as the people directory already does). Every ingest goes through the existing
   secret guard (`ingest/guard.py`).
7. **Tests.** Synthetic fixtures only, built from named constants; the runner or the extractor is
   injected; one end-to-end test through `ingest_document` against the 5434 database; one CLI test
   for the scope-flag refusal.
8. **Dependencies.** Prefer an external binary found on `PATH` (soft dependency) over a Python
   package. A Python package is added only as an **optional extra**
   (`[project.optional-dependencies]`), with an evidence block in `pyproject.toml` in the house
   style (what was measured, which versions, why the bounds), and an import inside the extractor so
   the base install never loads it.

### 2.5 Egress rules for anything that leaves the machine

F6 (migration 026, `brain.sensitivity`) is the boundary. Every ticket that sends content off the
machine (RM-14 Voyage reranking, RM-16 delivery, RM-46 HTTP transport) or writes a published
artifact (the brief's vault note, the watch-hits section) obeys three rules:

1. **Confidential documents never leave.** A hosted backend gets the `hosted_egress` flag, mirroring
   `embeddings.is_hosted_embedder`, and the stage excludes confidential rows before the call.
2. **Default off.** Every push or hosted path is enabled by a knob the owner sets.
3. **Titles before bodies.** A push carries titles and counts by default. Body-derived text (todo
   items, snippets) is a second opt-in.

### 2.6 New dependencies

Python dependencies added by this program, all **optional extras**, none in the base install:

| Extra | Package | Ticket | Why not a binary |
|---|---|---|---|
| `ocr` | `pypdfium2` (already required by `pdfplumber`, so zero new wheels on the measured resolve; declared because `brain` would now import it directly) | RM-32 | PDF page rasterization has no stdlib answer; `tesseract` does the OCR as a binary. |
| `code` | `tree-sitter`, `tree-sitter-language-pack` | RM-42 (only if RM-42.D1 picks tree-sitter) | Parsing many languages has no stdlib answer; the default is `ast` for Python plus a structural fallback, so the extra is optional. |

External binaries (soft dependencies, `shutil.which`, `brain doctor` reports them): `llama-server`
(RM-14b), `tesseract` (RM-32), `whisper-cli` from whisper.cpp and `afconvert` (RM-38, the latter a
macOS built-in), `osascript` (RM-36, built-in), `gh` (RM-31), `git` (RM-43), `fzf` (RM-52), and the
existing `gws`. Each ticket that adds one adds its
`brain doctor` row (`doctor_runtime.py`, 563 lines; growth beyond 60 lines moves the probes into
`doctor_sources.py`).

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
  `run_search` (RM-09) therefore takes the fallback as a flag, and the eval runner never sets it.

### 2.8 Migration allocation (central; no ticket may deviate)

Head at `02c2abc` is `028_search_query_tokens.sql`. Applied migrations are recorded by name in
`schema_migrations` (`db.run_migrations`), so two tickets must never ship the same number.

| Number | File | Owner | Wave |
|---|---|---|---|
| 029 | `029_saved_searches.sql` | RM-15 | 4 |
| 030 | `030_near_duplicates.sql` | RM-19 | 5 |
| 031 | `031_document_embeddings.sql` | RM-20 | 5 |
| 032 | `032_interactions_engagement.sql` (reserved; used only if RM-22b.D2 picks the materialized table) | RM-22b | 5 |
| 033 | `033_documents_parent.sql` (reserved; trigger-gated) | RM-60 | 9 |

No other ticket needs a migration. Every new source uses the existing `sources` / `documents`
tables. A ticket that discovers it needs one takes the next free number **after 033** and updates
this table in its PR.

---

## 3. Tickets

Every ticket uses the same fields. "Ceiling" names over-ceiling files touched and how growth is
avoided; "Eval" says whether ranking moves and whether `ci.json` is re-recorded. §2.1 (DoD) applies
to every ticket and is not repeated.

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
    embedder=embedder, query=query, **same kwargs as today)` in place of the direct `hybrid_search`
    call. Its docstring's "propagates ... `EmbedError`" sentence becomes "falls back on
    `EmbedError`; propagates `psycopg.Error`".
  - `RecallResult` gains `fallback: SearchFallback | None = None` (last field, defaulted, so every
    existing constructor call stays valid).
  - `RecallResult.to_dict()` merges `search_fallback.fallback_json(self.fallback)`: the same four
    always-present nullable keys (`fallback`, `fallback_reason`, `fallback_transient`,
    `fallback_note`) that `brain search --json --meta` and MCP `brain_search` emit.
  - `RecallResult.context_block()` appends `, text-only` to the header when `fallback` is set. The
    header is the pasted artifact an agent reads, so the marker has to be inside it, not only on
    stderr. The full note stays on stderr; the header stays one line.
  - `cli_recall.recall` prints `fallback.note` to stderr (yellow, `err=True`) when set, and exits 0.
  - MCP `brain_recall`: its `except EmbedError` branch becomes unreachable and is deleted, with a
    one-line comment naming `recall.recall` as where the failure is now handled (the same
    treatment PR #61 gave `brain_search`). `_wrap_embed_error` stays for the other tools.
- **Security / privacy:** All fallback text is class-only (`SearchFallback.from_error` uses
  `describe_failure`). No new egress.
- **Ceiling:** `mcp_server.py` shrinks by the deleted branch: promote its row's trail and add a
  descriptive hop; no growth record is owed. `recall.py` (354) and `cli_recall.py` (199) stay small.
- **Acceptance criteria:**
  1. With an embedder that raises a transient `EmbedError`, `brain recall "q"` exits 0, prints
     text-only passages, and prints the transient note on stderr.
  2. With a permanent `EmbedError`, the note names a configuration problem and `brain doctor`.
  3. `brain recall --json` carries the four keys, all `null` on a normal run.
  4. The context block header contains `text-only` exactly when a fallback happened.
  5. MCP `brain_recall` returns the payload with the four keys and never `INTERNAL_ERROR` for an
     embed failure; `payload_tokens` still counts the whole payload.
  6. A `psycopg.Error` still maps exactly as before.
  7. `tests/test_search_fallback_eval_untouched.py` stays green (the eval does not import recall).
- **Test plan:** unit tests on `RecallResult` (header, `to_dict`); DB tests on `recall.recall` with
  `tests/embed_down.py`'s failing embedder (transient, permanent, context overflow); CLI tests
  through `CliRunner` (stderr note, exit 0, JSON keys); MCP test calling `brain_recall` with the
  failing embedder in `_State`. Regression test: the pre-fix traceback, reproduced RED first.
- **Eval:** none; the eval does not use recall. `ci.json` unchanged.
- **Depends on:** nothing. **Size:** S.
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
    `ask_no_loop` keep the **first** fallback seen across sub-queries (all sub-queries hit the same
    embedder, so the first is representative).
  - `AskResult` gains `retrieval_fallback: SearchFallback | None = None`. `to_dict()` merges
    `fallback_json(...)`. The four keys are the same names every other surface uses, so one
    consumer check (`fallback == "fts_only"`) works everywhere. The existing `fallback_used` key
    (the planning loop was skipped or degraded) keeps its meaning and name: it is part of the
    `--json` and MCP output contract (`tests/test_cli_ask.py`, `tests/test_mcp_ask.py`, and the ask
    implementation spec). Its docstring gains one line distinguishing the two.
  - **The answer prose is not edited.** Injecting a marker into LLM text would break the
    `[N]`-citation sanitizer's assumptions and mix metadata into content. Instead: the CLI prints
    `retrieval_fallback.note` on stderr and `_print_ask_result` adds one line under the answer,
    `Retrieval: text-only (semantic ranking unavailable)`. The MCP tool docstring tells the calling
    agent to relay the note when `fallback` is set.
  - New leaf `src/brain/ask_errors.py`:

    ```python
    ASK_FAILURES: tuple[type[BaseException], ...] = (
        OllamaUnavailable, EnrichmentError, GraphTenantError, GraphBackendError, EmbedError,
    )

    def describe_ask_failure(exc: BaseException) -> str:
        """One fixed, class-only sentence per failure family; never the exception message."""
    ```

    Messages: Ollama down → "Ollama is not available. Start it (for example `brew services start
    ollama`) and retry."; `EnrichmentError` → "answer synthesis failed (<Class>)"; graph errors →
    "graph retrieval failed (<Class>)"; a residual `EmbedError` (graph modes that embed the query
    outside the hybrid leg) → "the embedding model is unavailable; retry later" when
    `exc.transient`, else "the embedding model is not usable (not pulled, or a bad key); run `brain
    doctor`".
  - `cli.py`'s `ask` handler: the three `except` branches collapse into one,
    `except ASK_FAILURES as exc: typer.secho(f"ask: {describe_ask_failure(exc)}", …); raise
    typer.Exit(1)`. The current branches interpolate `{exc}` into user-facing text; this removes
    that (DoD item 6). Net change to `cli.py`: negative.
  - MCP `brain_ask`: the same tuple maps to `INTERNAL_ERROR` with `describe_ask_failure(exc)`;
    `ValueError` and `psycopg.Error` keep their mappings.
- **Security / privacy:** class-only text everywhere; the confidential lens (`exclude_confidential`)
  is forwarded unchanged into the fallback call.
- **Ceiling:** `cli.py` shrinks (promote the row; descriptive hop; no growth record). `mcp_server.py`:
  the `brain_ask` mapping grows by about two lines; record it inline and in the module docstring.
  `ask.py` 654 → about 680.
- **Acceptance criteria:**
  1. Embedder down, chat up, `--no-loop`: exit 0, an answer, the stderr note, and the `Retrieval:
     text-only` line; `--json` has `fallback: "fts_only"`.
  2. Embedder down, chat up, full loop: the same.
  3. Embedder down **and** chat down: exit 1 with the Ollama sentence only; empty stdout; no
     traceback.
  4. A graph-mode run where the graph leg raises `EmbedError`: exit 1 with the embed sentence.
  5. No branch of the handler prints an exception message (a test raises each family with a
     sentinel message and asserts the sentinel is absent from stderr).
  6. MCP `brain_ask` returns the keys on fallback, and `INTERNAL_ERROR` with the fixed sentence for
     each failure family.
- **Test plan:** unit tests for `describe_ask_failure` (one per family, sentinel message absent);
  `ask` tests with a fake `ChatJson` and the failing embedder (both modes); CLI tests through
  `CliRunner` for criteria 1–5, with the pre-fix traceback reproduced RED first; MCP tests.
- **Eval:** none. `answer_eval` uses `ask` but scores answers on a live machine only; it inherits the
  fallback, which is the user-facing behaviour, and the answer-eval docs say so.
- **Depends on:** nothing (may land in parallel with RM-01; they touch different files except
  `mcp_server.py`, so merge them one after the other). **Size:** S.
- **Risks:** a weaker answer that reads confidently. Mitigated by the stderr note, the result line
  and the MCP keys.
- **Decisions:** RM-02.D1, fall back or refuse: **fall back** (the user's search decision; the
  fallback plan deferred only the marker question, answered above). RM-02.D2, new keys or reuse
  `fallback_used`: **new keys, same names as search**.

#### RM-03 — `brain ui` search falls back automatically, with a visible notice

- **Source:** BL-13; fallback plan follow-up F3 and its `ui/routes_search.py` ruling row.
- **Goal:** With the embedder down, the Notes tab still returns results, and a notice above the
  ledger says they are text-only and why.
- **Scope in:** `ui/routes_search.search`, `ui/static/js/results.js`, `ui/static/index.html` (one
  notice element), a CSS rule in `components.css`. **Scope out:** the related panel (it has no
  query embed), the facets.
- **Design:**
  - The route calls `search_with_fts_fallback(ctx.search_fn, conn, embedder=ctx.embedder,
    query=spec.query, …)`. `filter_kwargs()` currently carries the query; the route passes it by
    keyword and the rest as today. `ctx.search_fn` stays the injection point (`ui/context.py`), so
    tests keep their recording double.
  - The `except EmbedError` → `UiUnavailable("embedding_unavailable")` branch becomes unreachable
    and is deleted, with a comment pointing at the helper. The 503 code stays defined in
    `ui/errors.py` only if another route uses it (grep at implementation; delete it otherwise).
  - The response gains `fallback_json(...)`'s four keys beside `meta`. `meta.fts_only` is already
    `true` after a fallback, because `embed_ms` stays `None` (fallback plan, "Details of the
    retry").
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
  3. A following search with a working embedder hides the note.
  4. Telemetry records the search with the corrected `total_ms` (both attempts).
- **Test plan:** route tests (`tests/test_ui_search_fallback.py`, real 5434 DB, failing embedder
  double); a browser suite `tests/test_ui_browser_search_fallback.py`, added to the explicit
  `ci.yml` roster; `check_single_innerhtml` stays at one.
- **Eval:** none. **Depends on:** nothing. **Size:** S.
- **Risks:** none beyond the shared "weaker ranking" risk the notice addresses.
- **Decisions:** RM-03.D1, offer a "retry with semantic ranking" button: **no**; the next search
  retries the embed anyway, since the query-embed cache does not cache exceptions.

#### RM-04 — CI fails when the ceiling table and the tree disagree

- **Source:** BL-1; CLAUDE.md ceiling section, "The residue" paragraph.
- **Goal:** A file that crosses 800 lines without a row, or a row for a file that is no longer
  over, turns CI red with a message that says which and what to do.
- **Scope in:** a script, its tests, one `ci.yml` step, one `bin/brain-ci` line. **Scope out:**
  checking the *contents* of trails (a review job), and the near-ceiling budgets of §2.2.
- **Design:**
  - **Where the row set is parsed from:** `CLAUDE.md`, the one table whose header row is exactly
    `| File | Lines | Why it is not being split |`. Each data row's first cell is a backticked path
    relative to `src/brain/`. The parser stops at the first line after the table that does not
    start with `|`. The header is the anchor because it is unique in the file; a heading-text
    anchor would break on the next rewording.
  - **Script:** `scripts/check_ceiling_table.py`, outside the package like the other `scripts/`
    tools, with pure functions and a `main(argv: list[str] | None = None) -> int`:

    ```python
    CEILING = 800
    HEADER = "| File | Lines | Why it is not being split |"

    def count_lines(path: Path) -> int: ...          # data.count(b"\n"), identical to `wc -l`
    def over_ceiling(src_root: Path) -> dict[str, int]: ...   # rel path -> lines, lines > CEILING
    def table_rows(claude_md: str) -> list[TableRow]: ...     # raises TableNotFound
    def compare(over: Mapping[str, int], rows: Sequence[TableRow]) -> CeilingReport: ...
    ```

    `TableRow` holds the path and the Lines cell; `compare` also checks that the Lines cell's
    `wc -l src/brain/<path>` names the same path as the first cell, which catches a copied row.
    `CeilingReport` lists `missing_rows` (over, no row), `stale_rows` (row, not over, or the file is
    gone) and `mismatched_rows`. Exit 0 when all are empty, 1 otherwise, 2 when the table is not
    found. Each message names the file and the remedy ("add a row with the CLAUDE.md trail form" /
    "remove the row and add a 'deliberately absent' note", the form `backup/restore.py` and
    `related.py` use).
  - **CI step:** in the `lint` job, after `mypy src/`: `python scripts/check_ceiling_table.py`. The
    lint job has no database, and the check needs none. `bin/brain-ci`'s lint stage gains the same
    line, because that script exists to mirror CI faithfully.
- **Security / privacy:** none.
- **Ceiling:** no `src/` change. `CLAUDE.md`'s "deliberately **not implemented**" paragraph is
  rewritten to say the check exists, where it runs, and what it still cannot catch (budgets, and
  the contents of a row).
- **Acceptance criteria:**
  1. On `master`, the script exits 0.
  2. A synthetic tree with an 801-line file and no row exits 1 and names the file.
  3. An 800-line file is not over (the rule is "> 800").
  4. A row for a deleted file, and a row for a 799-line file, each exit 1.
  5. A row whose Lines cell names another path exits 1.
  6. A `CLAUDE.md` without the header exits 2.
  7. Mutation proof: in a `git archive` copy, deleting one real row turns the script red.
- **Test plan:** `tests/test_ceiling_table_check.py` loads the script with `importlib.util` (as
  `tests/test_embedding_smoke.py` loads its script) and drives the pure functions on `tmp_path`
  trees and synthetic markdown; one test runs it on the real repo; `tests/test_ci_workflow.py`
  gains an assertion that the lint job runs the step.
- **Eval:** none. **Depends on:** nothing. **Size:** S.
- **Risks:** a future reformatting of the table header disables the check. Exit 2 makes that loud
  rather than silent.
- **Decisions:** RM-04.D1, script vs pytest test: **script plus tests**. A test inside the default
  suite would run against whatever tree the test DB job checks out, but its failure would read as
  a test failure; a named CI step reads as a policy failure, which is what it is.

#### RM-05 — The offline test resolves every CSS `url()`

- **Source:** BL-3; consolidation spec Q18.
- **Goal:** A typo'd `url()` in a stylesheet fails the default suite, as a typo'd `src=` in
  `index.html` already does.
- **Scope in:** `tests/test_ui_static_assets.py`. **Scope out:** source changes.
- **Design:** a new test, `test_every_css_url_resolves_on_disk`, walks `static_dir()/css/*.css`,
  extracts `url(...)` targets with a quote-aware regex, skips `data:` URIs and pure `#fragment`
  references, resolves each relative to the stylesheet's own directory, and asserts the file exists
  and stays inside `static_dir()`. Today the only `url(` in the tree is a `data:` SVG in
  `base.css`, so the test passes on `master` and becomes load-bearing the day RM-50 (fonts) adds a
  real one.
- **Security / privacy:** none. **Ceiling:** none.
- **Acceptance criteria:** (1) green on `master`; (2) a synthetic stylesheet with `url("missing.woff2")`
  fails with the stylesheet and target named; (3) a `url()` that escapes `static_dir()` fails;
  (4) `data:` URIs pass.
- **Test plan:** the test itself, plus a parametrized test of the extraction function over synthetic
  CSS (quoted, unquoted, with whitespace, several per line). Mutation proof per DoD.
- **Eval:** none. **Depends on:** nothing. **Size:** XS.
- **Risks:** none. **Decisions:** none.

### Wave 2 — Seams that stop growth in over-ceiling files

**Why this wave is inserted ahead of the backlog's rank 4.** The backlog's order has no seams, and
without them roughly twenty later tickets would each grow `cli.py`, `mcp_server.py`,
`ingest/__init__.py`, `config.py` or `search.py`, each with its own two-place growth record. Four
small refactors, none of which changes behaviour, remove that. They are the smallest scope of BL-2
that the ceiling rules allow: extraction *rather than growth*, not a split to satisfy a number.

#### RM-06 — Registration seams for new CLI commands and MCP tools

- **Source:** BL-2 (smallest scope); G1.
- **Goal:** After this ticket, a new command module or MCP tool module is added without editing
  `cli.py` or `mcp_server.py`.
- **Scope in:** `cli_registry.py`, one call in `cli.py`, a new package `src/brain/mcp_tools/`, two
  lines in `mcp_server.py`, `tests/test_mcp_tool_registration.py`. **Scope out:** moving any
  existing command or tool (that is RM-48).
- **Design:**
  - **CLI.** `cli_registry.py` gains `EXTENSION_REGISTRARS: tuple[tuple[str, Registrar], ...] = ()`
    and `register_extensions(app: typer.Typer) -> None`, which runs them in order. `cli.py` calls
    `register_extensions(app)` once, after its last registration, so `brain --help` lists extension
    commands last. A later ticket adds one tuple entry and one import in `cli_registry.py`. The
    existing rule in `cli_registry`'s docstring still holds: an extension module never imports
    `brain.cli` at module scope.
  - **Policy that comes with it:** a new feature prefers a **new command or sub-app** over a new
    flag on a command that lives in `cli.py`. A flag on an existing `cli.py` command is the one
    case the seam cannot absorb; a ticket that needs one records the growth.
  - **MCP.** `src/brain/mcp_tools/__init__.py` exposes `register_extension_tools(app:
    MCPServerProtocol) -> None`, which imports each tool module and calls its `register(app)`. Each
    tool module defines its `brain_*` functions at module scope and registers them with
    `app.tool()(fn)`, never with a decorator, so the helper-between-decorator trap that once
    dropped `brain_recall` (CLAUDE.md, "One thing the ceiling audit found") cannot happen there.
    Shared helpers (`_state`, `_mcp_conn`, `_mcp_error`, `_wrap_db_error`, `_confidential_lens`)
    are reached through `from brain import mcp_server` **inside each tool function**, at call time,
    which avoids the import cycle and reads the live `_state` that `main()` sets.
  - `mcp_server.py` gains, after the last tool definition: `from .mcp_tools import
    register_extension_tools` and `register_extension_tools(mcp_app)`.
  - `tests/test_mcp_tool_registration.py` extends its roster to every module-level `brain_*`
    function in `brain.mcp_tools.*`, and asserts each is advertised by `mcp_app.list_tools()`.
- **Security / privacy:** none; no tool is added.
- **Ceiling:** `cli.py` +1 line and `mcp_server.py` +2 lines, each recorded inline and in the module
  docstring as "the extension seam: later commands and tools do not grow this file".
- **Acceptance criteria:**
  1. A test-only extension command, registered through `EXTENSION_REGISTRARS` in a throwaway app,
     appears in `--help` after every built-in command.
  2. A test-only tool module registered through `register_extension_tools` on a throwaway server
     is listed by `list_tools()` and callable.
  3. The registration guard fails if a `brain_*` function in `brain.mcp_tools` is not advertised.
  4. `import brain.cli` still does not load starlette or uvicorn (`tests/test_cli_ui.py`).
- **Test plan:** the three tests above, plus the existing registration and import-cost tests.
- **Eval:** none. **Depends on:** nothing. **Size:** S.
- **Risks:** an extension tool forgetting the confidential lens. Mitigation: `mcp_tools/__init__.py`'s
  docstring lists the lens helpers, and every tool ticket here names its lens.
- **Decisions:** RM-06.D1, a package or one module for MCP extensions: **package**, one module per
  feature area (`related.py`, `saved_search.py`, `dedup.py`, …), each under 400 lines.

#### RM-07 — Extractor registry, one source-kind definition, and the stale ingest docstring

- **Source:** G2, enabling every 2.x / 3.5 / 5.x ticket; BL-12.
- **Goal:** New file extractors register in a leaf module; a new source kind is defined in exactly
  one place; `ingest_document`'s docstring tells the truth about when the summary is written.
- **Scope in:** new `src/brain/ingest/dispatch.py`; `ingest/__init__.py` (re-export, shrink, and the
  docstring); `vault/links.py`; `cli_ingest.py` (import re-point). **Scope out:** any new extractor
  or kind.
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

    The four built-ins (text, markdown, pdf, docx) register in `dispatch.py` itself with the same
    lazy imports and the same error wrapping `extract_path` has today (`PyPdfError` and
    `PackageNotFoundError` → `ValueError`). `in_directory_walks` exists so that a later image or
    audio extractor (RM-33, RM-38) does not silently change what `brain ingest-dir` picks up:
    `ingest-dir` asks for `supported_extensions(directory_walk=True)`. `ingest/__init__.py` deletes `_EXTRACTORS`,
    `extract_path` and `supported_extensions` and re-exports the two functions, so
    `from brain.ingest import extract_path` keeps working. The Ingest spec's references to
    `ingest._EXTRACTORS` become `ingest.dispatch.supported_extensions()`; that spec is amended in the
    same PR.
  - **One source-kind definition.** `vault/links._SOURCE_KINDS` becomes `from ..source_kinds import
    VALID_SOURCE_KINDS as _SOURCE_KINDS` (an identity re-export, like `ui.schemas`). `source_kinds`
    imports only `brain.errors`, so there is no cycle. `source_kinds`'s docstring loses the "genuine
    separate copy" sentence. A test asserts identity (`is`), matching `tests/test_ui_schemas.py`.
  - **BL-12.** The `ingest_document` docstring's enrichment paragraph is rewritten to say the summary
    runs **after the commit**, as its own autocommit `UPDATE`, citing `_ingest_within_transaction`'s
    docstring, which already says so.
- **Security / privacy:** none.
- **Ceiling:** `ingest/__init__.py` shrinks (promote its row, which today ends at a descriptive
  hop; add this one; no growth record). `cli_ingest.py` imports change in place (net zero; hop
  recorded).
- **Acceptance criteria:**
  1. Every existing extractor test passes unchanged.
  2. Registering a duplicate extension raises.
  3. A test-only extractor registered at run time is used by `extract_path` and listed by
     `supported_extensions()`.
  4. `vault.links._SOURCE_KINDS is source_kinds.VALID_SOURCE_KINDS`.
  5. The docstring no longer says "inside the same transaction" about the summary (a text test, so
     the stale sentence cannot come back).
- **Test plan:** `tests/test_ingest_dispatch.py`; the identity test; the docstring test.
- **Eval:** none. **Depends on:** nothing. **Size:** S.
- **Risks:** a new kind becomes a valid `[[<kind>:<id>]]` link prefix, so a page literally titled
  `<kind>:something` would resolve as a source link. That is already true of the four existing kinds;
  each source ticket names its kind and checks the vault for colliding titles in its plan.
- **Decisions:** none.

#### RM-08 — Feature settings leaves

- **Source:** G3.
- **Goal:** Knobs added by this program never grow `config.py`.
- **Scope in:** new package `src/brain/settings/` with `_env.py` (the parsers in §2.3) and an
  `__init__.py` docstring stating the policy. **Scope out:** moving existing knobs.
- **Design:** §2.3. Parsers raise `ConfigError` with the variable name. Secrets are never echoed.
  `docs/configuration.md` gains a short "Feature settings" section explaining that these knobs are
  read from the same `.env` chain.
- **Security / privacy:** secret-typed fields are `repr=False`; a test asserts a secret value never
  appears in `repr()` or in an error message.
- **Ceiling:** none. **Acceptance criteria:** (1) each parser accepts valid values and refuses
  invalid ones with the variable named; (2) an unset variable yields the default; (3) a value set in
  a `.env` file is honoured after `Config.load()` (an integration test with a `tmp_path` `.env`,
  using `tests/env_isolation.py`).
- **Test plan:** pure tests at 95%+ and the one integration test. **Eval:** none.
- **Depends on:** nothing. **Size:** XS. **Risks:** none. **Decisions:** none.

#### RM-09 — One search pipeline for every user-facing surface

- **Source:** G4; enabling RM-14 (reranker), RM-20 (document leg), RM-22b (engagement), RM-54
  (rewrite).
- **Goal:** Every surface a person or agent searches through composes the same stages in the same
  order, and a new stage is wired once.
- **Scope in:** new leaf `src/brain/search_pipeline.py`; the six user-facing callers of
  `hybrid_search` or `search_with_fts_fallback`: `cli_search.search`, `cli_search.explain`, MCP
  `brain_search`, `ui/routes_search.search`, `recall.recall` (after RM-01), `ask._retrieve_hybrid`
  (after RM-02). **Scope out:** `eval/runner.run_eval`, `graph_rag/fuse.py`, `demo.search_demo`,
  `related.py` (each keeps its recorded reason for calling `hybrid_search` directly).
- **Design:**

  ```python
  class SearchStage(Protocol):
      name: str
      def candidate_limit(self, limit: int) -> int: ...       # how deep the stage needs to see
      def apply(self, conn: psycopg.Connection[Any], *, query: str,
                results: list[SearchResult]) -> StageOutcome: ...

  @dataclass(frozen=True)
  class StageOutcome:
      results: list[SearchResult]
      report: StageReport          # name, applied: bool, skipped_reason: str | None, ms: float

  @dataclass(frozen=True)
  class PipelineResult:
      results: list[SearchResult]
      fallback: SearchFallback | None
      stages: tuple[StageReport, ...]

  def run_search(search_fn: SearchFn, conn: psycopg.Connection[Any], *, embedder: Embedder,
                 query: str, limit: int, fallback: bool = True,
                 stages: Sequence[SearchStage] = (), **kwargs: Any) -> PipelineResult: ...
  ```

  `run_search` asks `search_fn` for `max(limit, *(s.candidate_limit(limit) for s in stages))` rows
  (through `search_with_fts_fallback` when `fallback` is true), applies the stages in order, and
  truncates to `limit`. A stage that fails (timeout, unreachable backend) returns its input
  unchanged with `applied=False` and a class-only `skipped_reason`; a stage never raises past the
  pipeline. `stage_reports_json(stages)` projects the reports for the `--json --meta` envelope and
  MCP, as a fifth always-present key, `stages: []`.
  `build_stages(cfg, settings) -> tuple[SearchStage, ...]` returns `()` in this ticket.
- **Boundary guard:** `tests/test_search_pipeline_boundary.py` parses every module under `src/brain`
  and fails if a module outside an allowlist (`search.py`, `search_fallback.py`, `search_pipeline.py`,
  `eval/runner.py`, `graph_rag/fuse.py`, `demo/*`, `related.py`) imports `hybrid_search`. That is
  what keeps a seventh surface from bypassing the pipeline.
- **Security / privacy:** none; behaviour is identical with zero stages.
- **Ceiling:** `mcp_server.py`: `brain_search` and `brain_recall` swap one call for another (net
  zero; hops recorded). `cli_search.py` net zero.
- **Acceptance criteria:**
  1. With no stages, every surface's output is byte-identical to `master`
     (`tests/test_search_output_unchanged.py` stays green, and gains the `stages: []` key only in
     the `--meta` envelope and MCP).
  2. A test stage that reorders results is applied on every surface (one test per surface).
  3. A test stage that raises is reported as skipped and the results are the unstaged ones.
  4. The boundary guard fails when a test module that imports `hybrid_search` is added to a
     `tmp_path` tree outside the allowlist.
- **Test plan:** pure tests for `run_search` with a fake `search_fn`; per-surface integration tests;
  the boundary guard with its mutation proof.
- **Eval:** none (no stage). **Depends on:** RM-01, RM-02, RM-03. **Size:** S.
- **Risks:** an output-shape drift on a frozen contract. The bare `--json` list is untouched; only
  the opt-in envelope and MCP gain a key, as the fallback plan ruled for its own keys.
- **Decisions:** RM-09.D1, stages see `SearchResult`s, not chunks: **results** (a stage that needs
  chunk text reads the snippet, which already carries `snippet_context_tokens` of context).

### Wave 3 — The Ingest tab and MCP parity

The backlog's ranks 4, 5 and 6, in that order. The Ingest spec is the design; these tickets only
add what the roadmap needs on top.

#### RM-10 — Ingest tab, phase 1: jobs and the paste backend

- **Source:** Ingest spec §10, phase 1; backlog rank 4.
- **Goal, scope, design, acceptance and tests:** exactly the Ingest spec's phase-1 row, §4 (the job
  subsystem), §5.1, §5.3, §5.4, §5.6, §6 (the paste routes), §7.1, §7.5, §9.1, §9.2 and §9.3 (paste
  rows). Decisions D1–D5, D8–D10 apply as written.
- **Additions from this program:**
  - `JobRegistry` is built kind-agnostic, with `kind` a free string, because RM-18a registers
    `"ask"` on it without changing `jobs.py` (Ingest spec §4.7).
  - The job's result payload for a new document carries `possible_duplicates: []` as an
    always-present key, filled by RM-19 later, so the front end's shape is fixed now.
  - The ingest service calls `ingest.dispatch.extract_path` (RM-07), not the old name.
- **Ceiling:** none of the touched files is in the table except `cli_ingest.py`, which shrinks
  (Ingest spec §10, "Ceiling check").
- **Eval:** none. **Depends on:** RM-07 (the dispatcher name). **Size:** M (Ingest spec: about 900
  source and 1,100 test lines).
- **Risks and decisions:** Ingest spec §11 and §12, recommended defaults as written.
- **Security / privacy:** the Ingest spec's §7 in full (D7, D8, and the §1.3 mutation-confinement amendment).

#### RM-11 — Ingest tab, phase 2: the paste front end

- **Source:** Ingest spec §10, phase 2.
- **Design and acceptance:** the Ingest spec's §8 and §3.4 items 1, 2, 4, 5 and 6 for paste; browser
  rows of §9.4. The browser suite is added to the explicit `ci.yml` roster.
- **Addition:** `index.html`'s `#tab-ingest` stops being a `deferred-tab` section;
  `tests/test_ui_tabs_static.py`'s copy expectations change in the same PR.
- **Eval:** none. **Depends on:** RM-10. **Size:** M. **Risks/decisions:** Ingest spec §11–§12.
- **Goal:** paste ingest is usable from the browser, with live progress, cancel and a link to the new document.
- **Ceiling:** none; `index.html` (218 lines) and the new JS and CSS files are small.
- **Security / privacy:** the Ingest spec's §7.5; model-free, but every server string is still inserted with `textContent`.

#### RM-12 — MCP `brain_related` and `brain_explain`

- **Source:** roadmap 6.1 (the missing tool), Q1-B D13, BL-6; backlog rank 5.
- **Goal:** An agent can ask for the documents related to one it has open, and can ask why a search
  ranked what it did, with the same answers the UI and `brain explain` give.
- **Scope in:** new `src/brain/mcp_tools/related.py` (both tools; RM-06 seam); a shared
  `explanation_json(explain: SearchExplanation | None) -> dict[str, Any] | None` moved into
  `format_search.py` from `cli_search.explain`'s inline literal. **Scope out:** any ranking change.
- **Design:**
  - `brain_related(id: str, limit: int = 5, include_confidential: bool = False) -> dict`.
    Resolves the id prefix like `brain_show`; calls `related.compute_related(conn, doc_id,
    limit=…, vector_sim_floor=cfg.vector_sim_floor, exclude_confidential=not
    include_confidential)`. **Gates the root too**, as `ui/routes_related` does: a confidential root
    with `include_confidential=false` gets `INVALID_PARAMS` "document is confidential; pass
    include_confidential=true", naming no title and no candidate, checked before ranking. Snippets
    are cut at `cfg.snippet_max_chars` with `snippet_truncated`. `limit` is capped by
    `mcp_limits.check_ceiling(limit, ceiling=RELATED_LIMIT_CEILING, param="limit")`, with
    `RELATED_LIMIT_CEILING = 20` a module constant (not a `config.py` knob). Returns `{id, related:
    [{id, title, vault_path, source, score, snippet, snippet_truncated}], vector_sim_floor}`.
  - `brain_explain(query: str, limit: int = 10, source: str | None = None, tag: str | None = None,
    since_days: int | None = None, include_confidential: bool = False) -> dict`. Runs
    `search_pipeline.run_search(..., explain=True, sensitivity=None if include_confidential else
    "normal")` and returns `{query, results: [{…search_results_json row…, explain: …}], fallback…,
    stages}`. Snippets are not included (an explanation is about rank, not content), which also
    keeps the payload small. The lens is the oracle-closing kind: confidential rows are excluded
    from the match set, not redacted.
  - `cli_search.explain` switches to `explanation_json` (net negative for `cli_search.py`).
- **Security / privacy:** both lenses default to excluding confidential documents (MCP is outside
  the F6 trust boundary); the root gate closes the "documents similar to the withheld one" leak
  `routes_related` documents.
- **Ceiling:** no over-ceiling file (RM-06).
- **Acceptance criteria:**
  1. `brain_related` on a document returns the same ids, in the same order, as `GET
     /api/notes/{id}/related` for the same config.
  2. A confidential root without `include_confidential` is refused before ranking; with it, it is
     answered.
  3. A confidential candidate is never returned without `include_confidential`.
  4. `brain_explain` returns, per result, the same `explain` object `brain explain --json` prints.
  5. `limit` above the ceiling is `INVALID_PARAMS`.
  6. Both tools appear in `list_tools()`, and the registration guard covers them.
- **Test plan:** DB tests with a synthetic three-document corpus (one confidential); a parity test
  against the UI route and the CLI; MCP error-mapping tests; `docs/configuration.md` MCP table rows.
- **Eval:** none. **Depends on:** RM-06, RM-09. **Size:** S.
- **Risks:** none beyond the lens, which the tests pin.
- **Decisions:** RM-12.D1, one module or two: **one** (`mcp_tools/related.py`); both are read-only
  ranking diagnostics.

#### RM-13 — Ingest tab, phase 3: file upload

- **Source:** Ingest spec §10, phase 3; backlog rank 6.
- **Design and acceptance:** the Ingest spec's §5.2, §7.2–§7.4, §9.2 (upload) and §9.3
  (Content-Type parametrized), and §3.4 item 3. **Security review** with `ecc:security-reviewer`
  on opus, in addition to the rule-14 loop, as the spec requires.
- **Addition:** accepted extensions come from `ingest.dispatch.supported_extensions()` intersected
  with the spec's allowlist, so a later extractor (RM-32's images) is **not** uploadable until its
  own ticket adds it to the allowlist on purpose.
- **Eval:** none. **Depends on:** RM-11. **Size:** M. **Risks/decisions:** Ingest spec §11–§12.
- **Goal:** upload one `.md`, `.txt`, `.pdf` or `.docx` file per job from the browser, refused before staging when the type or size is wrong.
- **Ceiling:** none (the Ingest spec's §10 "Ceiling check").
- **Test plan:** the Ingest spec's §9.2 upload rows, the §9.3 parametrized Content-Type test that proves the exception is exactly one route, and the §9.4 upload browser rows.

### Wave 4 — Ranking and the active brain

The backlog's ranks 7 to 10: the reranker (now measurable, after PR #50 re-judged all 31 golden
rows), saved searches, the Agent tab, and brief delivery. The Agent tab is placed after saved
searches and delivery here, not between them as in the backlog, because RM-15 and RM-16 are
independent of it and small, and the Agent tab's front end is the largest single item in the wave.

#### RM-14 — Cross-encoder reranker (three sub-tickets)

- **Source:** roadmap 5 (the top-5 list's #5), BL-5, the MCP server spec's "Out of scope" list
  ("reranking before returning search results"); the owner's 2026-05-11 decision: local first
  (`bge-reranker-v2-m3`), Voyage as the fallback, decided at build time.
- **Goal:** An optional second-stage reranker that reorders the top candidates of every
  user-facing search, enabled only if `brain eval` shows a gain.

**What the runtime can be, checked on 2026-10-23.** Ollama has no rerank API: the upstream
tracking issue (ollama/ollama#3368) is open, a 2026 issue (ollama/ollama#16076) states there is no
`/api/rerank`, and community workarounds read scores out of `/api/generate` text or embedding
magnitudes, which their own authors call less accurate than cross-encoder scoring. So "via Ollama",
as the May roadmap assumed, is not available. The candidates:

| Runtime | New Python dependency | Notes |
|---|---|---|
| **`llama-server` (llama.cpp) with `--reranking`**, serving a `bge-reranker-v2-m3` GGUF | **None** (an external binary over HTTP, through the already-declared `httpx`) | Recommended. Same shape as Ollama: a local service on loopback, Metal-accelerated, installed with Homebrew (`llama.cpp`). Exposes a rerank endpoint returning `{index, relevance_score}` per document. **Verify the exact path and payload against the llama.cpp server README at implementation**, and pin the tested `llama-server` version in the guide. |
| `sentence-transformers` `CrossEncoder` | Yes: `torch` (hundreds of MB) | Rejected: the heaviest dependency in the tree for one stage. |
| `fastembed` / ONNX Runtime | Yes: `onnxruntime` | Rejected for v1: smaller than torch, but a new native wheel, and `bge-reranker-v2-m3` support there is not confirmed. Recorded as the fallback if `llama-server` proves unworkable. |
| **Voyage `rerank-2.5`** | **None** (`voyageai` is declared) | The hosted alternative; subject to the egress rules (§2.5). |

- **Fields shared by the three sub-tickets:** Source and Goal as above. **Security / privacy:** §2.5; the local backend is loopback-only by configuration, and the hosted one never receives a confidential passage. **Size:** L in total (M + S + S). **Depends on:** RM-08, RM-09. **Ceiling:** no over-ceiling file in any sub-ticket. The rest is per sub-ticket.

##### RM-14a — The reranker stage, backends, settings and eval wiring (default OFF)

- **Scope in:** new leaves `src/brain/rerank.py` (Protocol + stage) and `src/brain/rerank_backends.py`;
  `src/brain/settings/rerank.py`; `search_pipeline.build_stages`; `eval/runner.run_eval`
  (a `stages` parameter); `cli_search.py` (`--rerank/--no-rerank`); `format_search` (a `rerank`
  timing phase); the UI's meta line. **Scope out:** turning it on; installing the runtime (RM-14b).
- **Design:**

  ```python
  class Reranker(Protocol):
      name: str              # "llamacpp" | "voyage"
      model: str
      hosted_egress: bool    # mirrors embeddings.is_hosted_embedder's flag
      def score(self, query: str, passages: Sequence[str], *, timeout: float) -> list[float]: ...

  class LlamaCppReranker:    # httpx.Client(timeout=...), POST to BRAIN_RERANK_URL; hosted_egress=False
  class VoyageReranker:      # voyageai.Client(api_key=cfg.voyage_api_key, max_retries=0,
                             #   timeout=...).rerank(query, passages, model=...); hosted_egress=True

  @dataclass(frozen=True)
  class RerankStage:         # implements search_pipeline.SearchStage
      reranker: Reranker
      depth: int
      max_chars: int
      timeout: float
      name: str = "rerank"
  ```

  - `candidate_limit(limit) = max(limit, depth)`. `apply` takes the first `depth` results, builds
    each passage as `title + "\n" + snippet`, cut to `max_chars`, scores them, and reorders the
    head by score descending (ties keep their prior order). The tail beyond `depth` keeps its order
    after the head.
  - **Egress.** When `hosted_egress` is true, confidential rows in the head are **not sent**: one
    `SELECT id FROM documents WHERE id = ANY(%s) AND sensitivity = 'confidential'` finds them, the
    others are scored and reordered, and each confidential row keeps its original index. A test
    asserts no confidential snippet reaches the backend.
  - **`score` is not overwritten.** `SearchResult.score` stays the RRF-plus-recency score, and
    `explain.reranker_score` (the existing placeholder at `search.py`, `SearchExplanation`) is set
    through `dataclasses.replace`, so `search.py` is not edited. The result order may therefore be
    non-monotonic in `score` when reranked; the docs say so, and the bare `--json` list's shape is
    unchanged.
  - **Failure.** A timeout, connection error or malformed response returns the input unchanged with
    `skipped_reason` (class-only), and the CLI prints one stderr line, "reranker unavailable;
    results are in hybrid order". Search never fails because of the reranker.
  - **Settings** (`settings/rerank.py`): `BRAIN_RERANKER` = `none` (default) | `llamacpp` |
    `voyage`; `BRAIN_RERANK_DEPTH` = 20 (2 to 50; `CANDIDATE_LIMIT` is 50); `BRAIN_RERANK_TIMEOUT_S`
    = 5.0; `BRAIN_RERANK_URL` = `http://127.0.0.1:8081` (loopback only: a non-loopback host is a
    `ConfigError`, because the passages are document text); `BRAIN_RERANK_MODEL` (default
    `bge-reranker-v2-m3` for `llamacpp`, `rerank-2.5` for `voyage`); `BRAIN_RERANK_MAX_CHARS` =
    1,500.
  - **Surfaces.** `build_stages` adds `RerankStage` when `BRAIN_RERANKER != none`, so every
    surface on the pipeline gets it. `brain search` and `brain explain` gain `--rerank/--no-rerank`
    (override for one run). MCP and the UI follow the setting. The UI's meta line shows `rerank
    0.4` beside `embed` and `rank`.
  - **Eval.** `run_eval` gains `stages: Sequence[SearchStage] = ()`, applied after `hybrid_search`
    with **no fallback**. `brain eval` passes `build_stages(...)`, so the eval measures the
    configured pipeline, and A/B is `BRAIN_RERANKER=llamacpp brain eval --baseline ci --diff`
    (no new `eval` flag, so `cli.py` is not touched). `config_signature` gains `reranker`,
    `reranker_model` and `rerank_depth` **only when the reranker is on**, so the committed
    `ci.json` signature is unchanged while it is off.
- **Ceiling:** `cli_search.py` +12 (budget, §2.2). No over-ceiling file.
- **Acceptance criteria:**
  1. With `BRAIN_RERANKER=none`, every surface's output and `config_signature` are byte-identical
     to before (`test_search_output_unchanged.py`, plus a signature test).
  2. With a fake reranker, the head is reordered by its scores on every surface, the tail is kept,
     and `explain.reranker_score` is set.
  3. A hosted fake never receives a confidential passage, and confidential rows keep their indices.
  4. A timing-out fake yields hybrid order and one stderr line; exit 0.
  5. A non-loopback `BRAIN_RERANK_URL` is a `ConfigError`.
  6. `run_eval(stages=[fake])` scores the reranked order; a transient embed failure is still a
     degraded row (no fallback).
- **Test plan:** pure tests for the stage (ordering, ties, tail, egress filter, failure); backend
  tests against an `httpx.MockTransport` (llama.cpp) and a fake Voyage client object passed to the
  constructor; settings tests; eval-runner tests; CLI and MCP surface tests.
- **Eval:** none while off; `ci.json` unchanged. **Depends on:** RM-08, RM-09. **Size:** M.
- **Risks:** the llama.cpp rerank payload changing between releases (mitigation: a backend test
  pinned to the documented schema, and the guide pins a version).
- **Decisions:** RM-14a.D1, overwrite `score`: **no** (above). RM-14a.D2, rerank in the MCP
  `brain_search` per call: **follow the setting**; a per-call parameter would grow `mcp_server.py`
  for a knob agents do not need.
- **Security / privacy:** the Voyage key is the existing `cfg.voyage_api_key`, never logged; the egress filter above is mutation-proven.

##### RM-14b — The local runtime: guide, doctor probe and optional launchd job

- **Scope in:** `docs/guides/local-reranker.md`; a `brain doctor` row (`GET {BRAIN_RERANK_URL}/health`,
  2 s timeout, only when `BRAIN_RERANKER=llamacpp`); an opt-in launchd template
  `src/brain/templates/launchd/com.brain.reranker.plist.j2` running `llama-server -m <gguf>
  --reranking --host 127.0.0.1 --port 8081`, installed only by an explicit flag on
  `brain-install-launchd` (bound to loopback in the template, never configurable to `0.0.0.0`).
- **Acceptance:** the doctor row reports ok / not running / not configured; the plist renders with
  the loopback host; the guide names the tested `llama.cpp` version and the GGUF's source and size.
- **Ceiling:** `doctor_runtime.py` +≤ 25; `setup.py` is not touched (the reranker is not offered by
  `brain setup` until RM-14c adopts it). **Depends on:** RM-14a. **Size:** S.
- **Risks:** memory: one more resident model. The guide states its measured resident size.
- **Decisions:** RM-14b.D1, ship a launchd job: **yes, opt-in**, because a reranker that is not
  running is silently skipped and a daemon is how the other models stay up.
- **Goal:** the local reranker can be installed, started at login and checked by `brain doctor`.
- **Design:** the scope above; the plist's `ProgramArguments` are fixed in the template, and only the model path is a variable.
- **Eval:** none.
- **Security / privacy:** the plist binds `127.0.0.1` only; the model file stays local.

##### RM-14c — Measure, then adopt or record

- **Scope:** on the owner's machine (not CI), run `brain eval --baseline ci --diff` with
  `BRAIN_RERANKER=none` and with `llamacpp` (and with `voyage` if a key is configured), in one
  session, after checking the document count with `brain status`. Record the three means, the
  per-category table, and p50/p95 added latency over the golden queries (from `explain` timing).
- **Adoption rule (all three):** no mean regresses by more than `1e-4`; nDCG@5 or MRR improves by
  at least **0.02** absolute; p95 added latency is at most **1.0 s**.
- **If adopted:** the owner's `$BRAIN_HOME/.env` sets `BRAIN_RERANKER=llamacpp`; `brain setup`
  offers it (a recorded `setup.py` growth); `ci.json` is re-recorded in this PR, the commit naming
  the trigger "ranking change (reranker adopted)"; `search.md` memory updated. The **code default
  stays `none`**, because the runtime is an external service a fresh install does not have.
- **If not adopted:** a short `docs/plans/<date>-reranker-measurement.md` records the numbers, and
  the backlog row is closed as "built, measured, not adopted".
- **Depends on:** RM-14b. **Size:** S. **Decisions:** RM-14c.D1, the 0.02 bar: **0.02**, about one
  golden row moving one rank at k=5 on 31 rows; smaller deltas are within judgment noise.
- **Source:** RM-14a and RM-14b; the eval policy (§2.7).
- **Goal:** a recorded, reproducible decision on whether the reranker is on.
- **Design:** the measurement steps and the adoption rule above.
- **Ceiling:** `setup.py` only if adopted (recorded in both places, and its row's hop promoted).
- **Acceptance criteria:** (1) the PR carries both runs' means, per-category tables and latency; (2) each of the three adoption conditions is evaluated explicitly; (3) if adopted, `brain eval --baseline ci --diff --fail-below` exits 0 with the reranker on after the re-record.
- **Test plan:** no new automated test; the recorded commands and their outputs are the deliverable.
- **Risks:** the document set moves between the two runs; both run in one session after `brain status`, and the count goes in the PR.
- **Security / privacy:** none beyond RM-14a.

#### RM-15 — Saved searches and watch lists, notified through the daily brief

- **Source:** roadmap 4.4, F13 ("accepted in principle, cut for scope"), BL-9; backlog rank 8.
- **Goal:** `brain watch add "<query>"` saves a search; each morning's brief lists documents that
  newly match it.
- **Scope in:** migration 029; new `src/brain/saved_search.py` (core), `src/brain/cli_watch.py`
  (a `watch` sub-app, registered through RM-06), `src/brain/mcp_tools/saved_search.py`, and a
  section in `brief.py`. **Scope out:** per-ingest push; macOS notifications; the UI (a later,
  optional panel).
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

- **Matching (the F5 WHERE-builder reuse).** A check runs `hybrid_search` through `run_search`
  with `fts_only=True` (lexical mode) and the saved filters, **plus `updated_after =
  last_checked_at − 1 h`** (the F9 filter, migration 025). `updated_at` is set on insert and on a
  real edit, and not by maintenance jobs, so the window is exactly "new or changed knowledge". The
  set difference against `saved_search_hits` is the new hits; they are inserted, and
  `last_checked_at` advances, in one transaction. Without the window, new documents would compete
  with every old match for the 50 candidate slots and could be crowded out. **Adding** a saved
  search seeds `saved_search_hits` with its current matches as already notified, so the first
  brief is not a flood. `semantic` mode runs the hybrid pipeline (with the fallback) and keeps only
  hits whose best vector cosine is at least `BRAIN_WATCH_MIN_COSINE` (0.45); lexical is the
  default, because it is deterministic and works when the embedder is down.
- **CLI** (`brain watch …`): `add NAME QUERY [--source] [--tag] [--person] [--kind]
  [--without-tag] [--semantic]`; `list [--json]`; `check [NAME] [--json] [--peek]` (`--peek`
  reports without recording); `rm NAME`; `pause NAME`; `resume NAME`. Filters are validated with
  the same functions `brain search` uses (`validate_source_kind`, `resolve_person_to_keys`).
- **Brief.** `BriefData` gains `watch_hits: list[WatchHit]` (default empty) and its `to_dict` key.
  `assemble_brief(..., exclude_confidential=…)` reads **unnotified** hits through the same lens,
  so the gated vault payload (`brief --wiki`, the F6 two-audience split) never names a confidential
  document. `brain brief --wiki` (the 07:00 daemon run) runs a check first and marks the hits it
  publishes as notified; a plain `brain brief` and MCP `brain_brief` only peek.
- **MCP** (`mcp_tools/saved_search.py`): `brain_watch_list()`, `brain_watch_add(name, query, …)`,
  `brain_watch_check(name=None, include_confidential=False)` (peek only: an agent never consumes the
  owner's notifications), `brain_watch_remove(name, confirm=False)`.
- **Security / privacy:** saved queries are the owner's own text, stored locally. Hit titles go
  through the F6 lens on every egress path (MCP, the vault note, RM-16 delivery).
- **Ceiling:** `brief.py` 315 → about 360; no over-ceiling file (the `brief` CLI command in `cli.py`
  needs no new flag).
- **Acceptance criteria:**
  1. `add` seeds existing matches; an immediate `check` reports zero new hits.
  2. Ingesting a matching synthetic document makes the next `check` report it once, and only once.
  3. An edited old document that newly matches is reported; an old match is never re-reported.
  4. Filters apply (a `--source` mismatch is not a hit).
  5. `brief --wiki` lists unnotified hits, excluding confidential ones, then marks them; a second
     run lists none.
  6. MCP `brain_watch_check` never marks.
  7. Lexical mode works with the embedder down.
- **Test plan:** DB tests for the core (seed, diff, window, filters, mark/peek); CLI tests; brief
  tests (section, gating, marking); MCP tests; migration test (idempotent re-apply).
- **Eval:** none. **Depends on:** RM-06, RM-09. **Size:** M.
- **Risks:** a very broad watch produces a long section. Mitigation: the brief shows at most 5 hits
  per watch and a count of the rest.
- **Decisions:** RM-15.D1, per-ingest push: **no**; the daily brief is the cadence, and no push
  channel exists without RM-16. RM-15.D2, lexical default: **yes**.
- **Design:** the data model, the matching rule, the CLI, the brief integration and the MCP tools above.

#### RM-16 — Brief delivery: email to self (or a private Slack webhook), opt-in

- **Source:** roadmap 4.1 ("Delivery: … email yourself, post to Slack, or all three"); backlog
  rank 10, "a product decision". This ticket makes it a setting with a recommended default.
- **Goal:** The 07:00 brief can arrive in the owner's inbox, without anyone opening a terminal.
- **Channel recommendation.** **Email to the owner's own address through the existing `gws`
  CLI** (`gws gmail +send --to … --subject … --body …`). It needs no new credential (`gws` is
  already authenticated for `ingest-gmail`), the recipient is fixed to `BRAIN_USER_EMAIL`, and
  nothing new is installed. **Slack** is supported through an **incoming webhook bound to a private
  channel or a DM**, configured by the owner. The `#team` channel already used for status pings is
  **rejected** as a destination: it is shared, and a brief names the owner's documents.
- **Scope in:** new `src/brain/brief_delivery.py`; `src/brain/settings/brief_delivery.py`; docs (the
  daemon script is unchanged). **Scope out:** HTML email; per-section toggles beyond the two below.
- **Design:**

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

  - **What is sent:** the **gated** payload only (the one `brief --wiki` writes, built with
    `exclude_confidential=True`). By default: the date, counts, capture titles, pinned titles, watch
    hits (RM-15), new entities (RM-17), and the line "open `brain brief` for detail". **Todo text is
    body-derived** and is sent only when `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true`.
  - **Settings:** `BRAIN_BRIEF_DELIVERY` = `none` (default) | `email` | `slack`;
    `BRAIN_BRIEF_SLACK_WEBHOOK_URL` (secret; must be `https://hooks.slack.com/…`, else
    `ConfigError`); `BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS` = false. `email` with no
    `BRAIN_USER_EMAIL` is a `ConfigError` naming the variable.
  - **Wiring:** the `brief` command runs delivery when `--wiki` is set and delivery is configured.
    That adds no flag to `cli.py`: the call is one line inside the existing `--wiki` branch, which
    is recorded as growth (or, preferably, the `--wiki` branch's body moves into a
    `brief_publish.py` leaf in the same PR and `cli.py` shrinks; the implementer picks whichever
    leaves `cli.py` smaller). The daemon script is unchanged.
  - **Failure:** a delivery error never fails the brief: the vault note is written first, then one
    class-only WARNING (`describe_failure`), exit 0. `brain doctor` reports the channel and whether
    its prerequisite exists (`gws` on `PATH`; the webhook set), never the URL.
- **Security / privacy:** §2.5 applies in full. The recipient cannot be anyone but the owner. The
  webhook URL is a secret and never logged.
- **Ceiling:** `cli.py`, net zero or negative as above. **Acceptance criteria:**
  1. With delivery `none`, nothing is sent and nothing is printed about it.
  2. With `email`, the injected runner receives exactly one `gws gmail +send` call whose `--to` is
     `BRAIN_USER_EMAIL`, and whose body contains no confidential title and no todo text by default.
  3. With `slack`, an `httpx.MockTransport` receives one POST; a non-Slack URL is a `ConfigError`.
  4. A failing channel leaves the vault note written and exits 0 with one WARNING.
  5. No log line or error contains the webhook URL (sentinel test).
- **Test plan:** pure render tests; channel tests with injected runner / transport; settings tests;
  a CLI test of `brief --wiki` with a fake channel; the sentinel test.
- **Eval:** none. **Depends on:** RM-08; best after RM-15 and RM-17 so the sections exist.
  **Size:** S.
- **Risks:** mail volume (one per day) and spam filtering of self-sent mail. The subject is fixed
  (`brain brief YYYY-MM-DD`), so a filter can label it.
- **Decisions:** RM-16.D1, channel: **email via `gws`, off by default** (above).

#### RM-17 — The brief's "new this week" entities section

- **Source:** roadmap 4.1, the missing "new entities or projects newly mentioned" bullet.
- **Goal:** The brief names people, organisations and projects that first appeared in the corpus in
  the last 7 days.
- **Design:** a new leaf `src/brain/brief_entities.py` with `new_entities(conn, *, tenant_id,
  since: datetime, exclude_confidential: bool, limit: int = 10) -> list[NewEntity]`, selecting
  `graph_entities` rows with `entity_type IN ('person','org','project')` and `created_at >= since`.
  With `exclude_confidential`, an entity is listed only if at least one of its
  `graph_entity_mentions` documents is not confidential. `BriefData` gains `new_entities`; the
  section is omitted when the graph is disabled or the AGE tables are absent (a cheap existence
  check), so a stock install's brief is unchanged.
- **Ceiling:** `brief.py` +≤ 25. **Acceptance:** (1) a synthetic entity created yesterday is listed;
  (2) one created 10 days ago is not; (3) an entity mentioned only by a confidential document is
  excluded from the gated payload; (4) graph disabled → no section, no error.
- **Test plan:** DB tests on the AGE test instance (5434), brief tests. **Eval:** none.
- **Depends on:** nothing. **Size:** S. **Risks:** alias merges re-create entities with a new
  `created_at`; the query treats an entity as new only if it has no mention older than `since`.
- **Decisions:** none.
- **Security / privacy:** entity names only; an entity known only from confidential documents is withheld from the gated payload.

#### RM-18 — The Agent tab (three sub-tickets)

- **Source:** F14 §1.1 ("Agent — deferred to v3"), the Ingest spec §4.7 and Q12/Q13, backlog
  §2.1 and rank 9.
- **Goal:** Ask a question in the browser, watch the plan → retrieve → reflect → synthesize steps
  progress, cancel it, and read a cited answer whose citations open in the Notes tab.

**Architecture, in the house style.**

| # | Question | Decision |
|---|---|---|
| A1 | Where does an ask run? | On the Ingest tab's `JobRegistry` worker, as job kind `"ask"`. One worker for ingest and ask (Ingest spec Q13): this machine cannot hold two chat-model runs and an embed at once. An ask queued behind an ingest says "waiting for the ingest job to finish". |
| A2 | Transport | **Polling** `GET /api/jobs/{id}?after=<seq>` (Ingest spec §4.3). **No NDJSON stream in v1.** `ask`'s steps call `chat.chat_json`, which returns whole JSON objects (structured output), so there is no token stream to forward; a stream would deliver the same five to eight step events polling does, at a 1 s granularity that is honest for 10–90 s runs. The `/stream` endpoint the Ingest spec sketches stays the upgrade path if synthesis ever streams tokens. |
| A3 | Progress | `ask.ask` and `ask_no_loop` gain `on_step: Callable[[str, int], None] \| None = None`, called with `("plan" \| "retrieve" \| "reflect" \| "synthesize", iteration)`. The runner maps it to `handle.progress(...)` and calls `handle.check_cancelled()` in it. |
| A4 | Cancellation | Cooperative, at every step boundary and before every embed (the Ingest spec's progress-embedder wrapper is reused). Ask writes nothing to the corpus (only `interactions` rows at the end, through `ui/telemetry.py`), so `enter_commit()` is never called and a cancel is honoured until the result is recorded. `ask.py` has no `except` clause at `02c2abc`, so `JobCancelled` (a `BrainError`) propagates out of `ask.ask`; a test pins that. |
| A5 | Conversation state | **Client-side only**, in `sessionStorage` (read in `try`): the list of turns in this tab, capped at 20. **No server persistence**: the same reasoning as the Ingest spec's D4, plus answers quote confidential bodies. A follow-up sends at most the previous **3** turns' questions and answers (server-capped at 2,000 tokens, oldest dropped first) to the **plan step only** (RM-18c). |
| A6 | Where it is allowed | Loopback-bound, writable servers, like ingest (Ingest spec D8). On `--read-only` or a LAN bind the tab is disabled and says why. |
| A7 | Confidential lens | `exclude_confidential = not ctx.serve_confidential_bodies`, passed to `ask.ask`. On a loopback server that serves confidential bodies, the answer may draw on them, as the CLI does. |
| A8 | Rendering model output | **`textContent` only.** Markdown is not rendered in v1. §RM-18b gives the hardening rules. |
| A9 | Modes | `hybrid` only in v1. Graph modes need `connect_age` and compete with the watcher's graph lane for the per-tenant write lock (PR #53); they come later on request. |

- **Fields shared by the three sub-tickets:** **Security / privacy:** F14 §1.1's hardening pass (RM-18b) and decisions A6–A8. **Ceiling:** the `ask.py` budget (§2.2); no over-ceiling file. **Eval:** none; `ask` is not eval-gated. **Depends on:** RM-02, RM-10, RM-11. **Size:** L in total (M + M + S). **Risks:** an ask holds the single worker for up to a few minutes; the panel says so and cancel frees it. **Decisions:** A1–A9 above.

##### RM-18a — Agent tab backend

- **Scope in:** `ask.py` (`on_step`); new `src/brain/ui/ask_service.py` (builds the runner; not a
  mutator, so the Ingest spec's two-mutator rule is unchanged: interaction rows go through
  `ui/telemetry.py`, which already writes UI telemetry); new `src/brain/ui/routes_ask.py`:
  `GET /api/ask/config` (enabled, reason, max question length, modes) and `POST /api/ask`
  `{question: str ≤ 2,000 chars, no_loop: bool, history: [{question, answer}] ≤ 3}` → 202 with
  the `JobSnapshot`. Status and cancel reuse `/api/jobs/{id}` and `/api/jobs/{id}/cancel`.
- **Result payload:** `AskResult.to_dict()` (including RM-02's fallback keys), each citation's
  `snippet` cut to `cfg.snippet_max_chars`, and `elapsed_ms`.
- **Error mapping:** `describe_ask_failure` (RM-02) → job `error = {code, message}` with codes
  `chat_unavailable`, `synthesis_failed`, `embedder_unavailable`, `database_error`,
  `internal_error`; messages are fixed per code, as the Ingest spec §4.5 requires.
- **Timeouts:** each chat call keeps `cfg.ask_timeout_seconds`; the job as a whole is capped at
  `ask_max_iterations × 3 × ask_timeout_seconds` and fails `timeout` beyond it.
- **Acceptance:** (1) a submitted ask with a fake `ChatJson` reaches `succeeded` with the payload;
  (2) the events name each step in order; (3) a cancel during retrieve ends `cancelled` with no
  interaction row written; (4) chat down → `chat_unavailable` with the fixed message;
  (5) `--read-only` → 403 `read_only` from the middleware, LAN bind → 403 `ask_requires_loopback`;
  (6) a confidential document is cited only when the server serves confidential bodies.
- **Test plan:** runner tests on `run_next()` (no thread, no sleep); route tests on the 5434 DB;
  security tests extending `tests/test_ui_security.py`. **Depends on:** RM-10, RM-02. **Size:** M.
- **Ceiling:** `ask.py` +≤ 25.
- **Goal / source / design:** the parent's; this sub-ticket is the server half of A1–A7.
- **Eval:** none.
- **Risks:** an ask queued behind a long PDF ingest waits; the job panel shows the queue position.
- **Decisions:** RM-18a.D1, graph modes: **not in v1** (A9).

##### RM-18b — Agent tab front end and the model-output hardening pass

- **Scope in:** `ui/static/js/ask.js`, `ui/static/css/ask.css`, the `#tab-agent` panel in
  `index.html`, reuse of `jobs.js` (RM-11); browser tests; the `ci.yml` roster entry.
- **Hardening rules (each one tested):**
  1. Model text (answer, sub-queries) is inserted only with `textContent` or `createTextNode`.
     `check_single_innerhtml` still finds exactly one `innerHTML` in the whole front end, and a new
     static test fails if `ask.js` contains `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
     `document.write`, `eval`, `new Function` or `setAttribute("on…")`.
  2. Citation markers are parsed in JS with `/\[(\d{1,2})\]/g` over the answer **string**; each
     match whose number exists in the server's `citations` list becomes an `<a>` built with
     `createElement`, its `href` set to `?id=` + `encodeURIComponent(id)` where `id` must match the
     UUID pattern, and its text the marker. Anything else stays text. The server's
     `_sanitize_answer` already drops out-of-range markers; the client does not trust that.
  3. No model-supplied string ever becomes an attribute other than through the UUID-checked `href`.
  4. The CSP is unchanged (`script-src 'self'`, no `unsafe-inline`), so even a missed sink cannot
     run inline script; a test asserts the header is unchanged.
- **Acceptance:** a fake `ChatJson` returning an answer containing `<img src=x onerror=…>`,
  `<script>`, `javascript:` and `[99]` renders those characters as visible text, creates no `img`,
  `script` or extra `a` element, and raises no dialog; `[1]` becomes a link that opens the cited
  note in the Notes tab; cancel and the "waiting for ingest" state render.
- **Test plan:** browser suite `tests/test_ui_browser_agent.py` (playwright, demo-style synthetic
  corpus, fake chat injected through the `UiContext`); static tests. **Security review** with
  `ecc:security-reviewer` on opus, as F14 §1.1 requires. **Depends on:** RM-18a, RM-11. **Size:** M.
- **Goal / source:** the parent's; this sub-ticket is the browser half and the hardening pass F14 §1.1 requires.
- **Design:** the hardening rules above, applied in `ask.js`.
- **Ceiling:** none (new files; `index.html` stays small).
- **Risks:** a later edit adds a client-side markdown renderer; the static sink test fails first.
- **Decisions:** RM-18b.D1, markdown in answers: **not in v1**; if wanted later, it is rendered server-side through `render.py`'s hardened path, which first needs room under its 799 lines (§2.2).

##### RM-18c — Follow-up questions

- **Scope in:** `ask.ask` gains `history: Sequence[AskTurn] = ()`; `_plan_prompt` includes it as
  "Earlier in this conversation: …" so the planned sub-queries are self-contained; synthesis sees
  only the current question and the retrieved documents. The server re-validates the 3-turn,
  2,000-token cap. The tab sends history only when the user presses "Follow up".
- **Acceptance:** with a fake chat, a follow-up's plan prompt contains the previous question and
  answer, truncated oldest-first at the cap; a fresh ask sends none; MCP `brain_ask` is unchanged.
- **Ceiling:** `ask.py` +≤ 50. **Depends on:** RM-18b. **Size:** S.
- **Decisions:** RM-18c.D1, history in synthesis too: **no**; it would let an earlier answer's
  claims leak into a new answer without a citation.
- **Goal / source:** the parent's (A5); follow-up questions keep their context.
- **Design:** the scope above.
- **Test plan:** unit tests on `_plan_prompt` with and without history; a route test for the server cap; a browser row for "Follow up".
- **Eval:** none.
- **Risks:** history steers the plan off-topic; only the plan step sees it.
- **Security / privacy:** history comes from the client, so the server re-caps it and treats it as untrusted prompt input; it never reaches synthesis.

### Wave 5 — Corpus quality

The backlog's ranks 11 to 15 and 17. RM-20 and RM-22b are measured on top of whatever RM-14c
adopted, which is why they follow it.

#### RM-19 — Near-duplicate detection (flag, never refuse)

- **Source:** roadmap 3.4; backlog rank 11; the Ingest spec's risk R4 (the same file ingested by the
  CLI and by upload becomes two documents).
- **Goal:** The owner sees pairs of documents that are near-copies (forwarded emails, re-sent
  drafts, a file ingested twice by two paths) and can confirm or dismiss each pair.
- **Scope in:** migration 030; new leaf `src/brain/near_dup.py` (pure MinHash); new
  `src/brain/near_dup_store.py` (SQL); `src/brain/cli_dedup.py` (`brain dedup …`, RM-06 seam);
  `src/brain/mcp_tools/dedup.py`; a brief section; the Ingest tab's `possible_duplicates` key
  (RM-10). **Scope out:** refusing ingest; deleting or merging documents; collapsing duplicates in
  search results (RM-19.D2).
- **Algorithm (no new dependency):** word 5-gram shingles over the lowercased, whitespace-collapsed
  body, each hashed to 64 bits with `hashlib.blake2b(digest_size=8)`; a 128-value MinHash signature
  from fixed-seed universal hashes modulo the Mersenne prime 2⁶¹−1; 32 bands of 4 rows for LSH
  candidates (the S-curve threshold is about (1/32)^(1/4) ≈ 0.42, so candidates are found well
  below the 0.85 cut); each candidate pair is then verified by its signature's estimated Jaccard
  ≥ **0.85** (the roadmap's figure). Documents under 50 tokens are skipped (templates and stubs
  collide). Two documents of one Gmail thread are never paired (thread assembly already handles
  them).
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
  records new pairs; the brief daemon's run calls the same function with `limit=500` before
  assembling, so the corpus stays current daily; the Ingest tab's job calls
  `near_dup_store.check_document(conn, doc_id)` after a successful ingest and fills
  `possible_duplicates`.
- **Surfaces:** `brain dedup list [--status open|dismissed|confirmed] [--json]`, `brain dedup
  dismiss A B`, `brain dedup confirm A B` (a label only; deleting stays `brain rm`); MCP
  `brain_dedup_list(include_confidential=False)` (read-only, lens on both ends of a pair, as
  `connect.iter_suggestions` gates both endpoints); the brief's "Possible duplicates (N)" line.
- **Security / privacy:** signatures are irreversible hashes of shingles; titles go through the F6
  lens on MCP and the gated brief.
- **Ceiling:** none (all new leaves; `brief.py` +≤ 10).
- **Acceptance criteria:**
  1. Two synthetic documents differing by one sentence in 400 words are paired; two unrelated ones
     are not; a 30-token stub is skipped.
  2. Editing a document recomputes its signature and drops a pair that no longer verifies.
  3. Deleting a document deletes its pairs (cascade).
  4. A dismissed pair is never re-opened by a later scan.
  5. Pure-function coverage ≥ 95%; signatures are deterministic across runs (fixed seeds).
  6. The full first scan over a 2,000-document synthetic corpus runs in under 5 minutes on the
     owner's machine (measured and recorded in the PR; a budget, not a CI assertion).
- **Test plan:** pure tests (shingles, signature, banding, estimate vs exact Jaccard on synthetic
  pairs); DB tests for scan, stale recompute, cascade and status; CLI and MCP tests; a brief test.
- **Eval:** none (no ranking change). **Depends on:** RM-06; RM-10 for the UI key (optional).
  **Size:** M.
- **Risks:** meeting transcripts with long shared boilerplate pair falsely. Mitigation: the 0.85
  bar, the 50-token floor, and dismiss.
- **Decisions:** RM-19.D1, flag vs refuse at ingest: **flag** (a false positive would lose a
  document). RM-19.D2, collapse near-duplicates in search: **not in this ticket**; it is a ranking
  change and needs its own eval run.
- **Design:** the algorithm, data model, schedule and surfaces above.

#### RM-20 — Document-level embeddings as a re-scoring stage (default OFF)

- **Source:** roadmap 3.6; backlog rank 12 ("re-evaluate after the reranker").
- **Goal:** A "what is this document about" signal, from embedding each document's title and
  summary, that can lift documents whose best chunk ranks poorly but whose summary fits the query.
- **Scope in:** migration 031; `embedding_targets.EMBEDDING_TARGET_ALLOWLIST` gains
  `("document_embeddings", "embedding")`, so `db.ensure_embedding_column` and
  `queries.finalize_embedding_index` reconcile its dimension like the other targets; new
  `src/brain/doc_embeddings.py` (backfill), `src/brain/search_doc_leg.py` (the stage),
  `src/brain/cli_docembed.py` (`brain docembed backfill [--stale] [--limit]`),
  `src/brain/settings/doc_leg.py`.
- **Data model (`031_document_embeddings.sql`):** `document_embeddings(document_id UUID PRIMARY KEY
  REFERENCES documents(id) ON DELETE CASCADE, embedding vector(1024), basis TEXT NOT NULL CHECK
  (basis IN ('title_summary','title_head')), basis_hash TEXT NOT NULL, embedder TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`. The `vector(1024)` is the house starting dim;
  `brain init` resizes it for the active embedder, as for `chunks.embedding`. HNSW is created when
  the dimension is ≤ 2,000.
- **What is embedded:** `title + "\n" + summary` when `documents.summary` exists, else `title +
  "\n" +` the first 512 tokens of content. `basis_hash` is the SHA-256 of that text; `--stale`
  re-embeds rows whose basis changed. Under a hosted embedder, **confidential documents are
  skipped**, mirroring the F6 egress veto in `_embed_chunks`.
- **The stage re-scores candidates; it adds none.** For each candidate from the pipeline, it reads
  the document vector and computes the cosine to the query vector, ranks candidates by that cosine,
  and fuses the two ranks with `rank_fusion.rrf_contribution` (k=60). Because it only re-scores
  documents the hybrid search already returned, every filter and the oracle-closing sensitivity
  exclusion hold by construction. The query vector comes from the query-embed LRU in `search.py`,
  through a public alias `cached_query_embedding` (a two-line, recorded growth of `search.py`), so
  the stage costs no second embed call.
- **Settings:** `BRAIN_DOC_LEG=false` (default), `BRAIN_DOC_LEG_WEIGHT=1.0` (multiplier on its RRF
  contribution).
- **Ceiling:** `search.py` +2, recorded in both places. **Acceptance criteria:**
  1. Off: outputs and `config_signature` unchanged.
  2. On, with a synthetic corpus where one document's summary matches and its chunks barely do, that
     document moves up; filters still exclude what they excluded.
  3. A confidential document is never embedded under a hosted fake embedder.
  4. `brain init` on a qwen3-dim test DB resizes the column and skips HNSW.
- **Test plan:** pure fusion tests; DB tests for backfill, stale, egress skip, dim reconcile; stage
  tests per surface; eval-runner test with the stage.
- **Eval:** measured on the owner's machine on top of the adopted pipeline, with RM-14c's adoption
  rule; adopt → re-record `ci.json` (trigger "ranking change (document leg adopted)"); else record
  and keep off. **Depends on:** RM-09, RM-14c. **Size:** M.
- **Risks:** a summary-dominated signal over-promotes long documents. The weight knob and the eval
  bound it.
- **Decisions:** RM-20.D1, re-score vs add candidates: **re-score** (above); a recall-adding leg
  needs the predicate inside the stage protocol, and is worth building only if a measurement shows
  the right documents are missing from the candidate set, not mis-ordered within it.
- **Design:** the data model, the basis text and the stage above.
- **Security / privacy:** §2.5; the F6 egress veto is mirrored for hosted embedders.

#### RM-21 — Tag merge, rename and suggested merges

- **Source:** roadmap 7.4; backlog rank 13.
- **Goal:** `brain tags rename old new`, `brain tags merge a b --into c`, and `brain tags
  suggest-merges`, writing every place a tag lives.
- **Scope in:** move the synonym-mapping engine of `brain backfill normalize-tags` (`cli.py`:
  `_load_tag_mapping`, `_apply_tag_mapping` and the per-document loop of `backfill_normalize_tags`) into
  a new leaf `src/brain/tag_mapping.py`, leaving `cli.py` a thin caller (net negative); new
  `src/brain/cli_tags.py` (a `tags` sub-app through RM-06); `src/brain/mcp_tools/tags.py`
  (read-only).
- **Design:**
  - `rename` and `merge` build a mapping `{old: new}` and run the same engine `normalize-tags`
    runs: one DB transaction over `documents.tags`, then `vault.frontmatter.rewrite_tags` for
    vault-tier files (the file is that tier's source of truth) and mirror regeneration for ingested
    rows. **Dry-run by default**; `--apply` writes. `updated_at` is not bumped (migration 025: tag
    maintenance is not new knowledge).
  - If RM-15 has shipped, `saved_searches.filters` tag values are rewritten in the same
    transaction, so a watch does not silently stop matching.
  - `suggest-merges` (pure, in `tag_mapping.py`): pairs whose `difflib.SequenceMatcher` ratio is ≥
    0.85, singular/plural pairs, and pairs whose document sets overlap with Jaccard ≥ 0.8
    (`set_similarity.jaccard`), ranked, each with its evidence (counts, ratio, overlap).
  - `brain tags list [--json]`: each tag and its count.
  - MCP: `brain_tags_list`, `brain_tags_suggest_merges`. **No MCP bulk rename**: a corpus-wide
    rewrite is an owner action at the terminal.
- **Ceiling:** `cli.py` shrinks (record the hop). **Acceptance criteria:**
  1. `rename` dry-run lists affected documents and writes nothing; `--apply` updates DB rows, vault
     frontmatter and mirrors; a second run is a no-op.
  2. `merge` unions tags without duplicates and preserves order (`normalize_tags`).
  3. `backfill normalize-tags` output is byte-identical before and after the extraction.
  4. Suggestions include a synthetic plural pair and a co-occurrence pair, with evidence.
- **Test plan:** pure tests for the mapping engine and suggestions; DB + `tmp_path` vault tests for
  apply; the normalize-tags regression test; MCP tests.
- **Eval:** none. **Depends on:** RM-06. **Size:** S.
- **Risks:** a merge into an existing tag is irreversible without a mapping back; the dry-run
  default and the printed mapping are the mitigation.
- **Decisions:** RM-21.D1, MCP writes: **read-only** (above).
- **Security / privacy:** local only; MCP is read-only for tags.

#### RM-22 — Interactions-aware ranking (two sub-tickets, gated on data)

- **Source:** roadmap 1.3 phase 2; Q1-C non-goal; backlog rank 14 ("gate it on the data").
- **Fields shared by the two sub-tickets:** **Security / privacy:** interactions are local rows; nothing leaves the machine. **Ceiling:** new leaves; `usage.py` (623) is not touched, `cli_usage.py` gains one subcommand. Everything else is per sub-ticket.

##### RM-22a — The engagement readout that decides the gate

- **Scope in:** new leaf `src/brain/engagement.py` with `engagement_stats(conn, *, days: int = 90)
  -> EngagementStats` (opens and clicks by surface, distinct documents opened, `rated_useful`,
  `rated_irrelevant`, and the share of opens that came from a search session), and a `brain usage
  engagement [--json]` subcommand registered in `cli_usage.py`.
- **The gate (recommended values):** RM-22b is built only when, over 90 days, there are at least
  **500 opens across at least 100 distinct documents** and **30 explicit ratings**. The readout
  prints whether the gate is met.
- **Acceptance:** counts match a synthetic `interactions` fixture; the gate line flips at the
  thresholds. **Depends on:** nothing. **Size:** S.
- **Source / goal:** the parent's; a measured answer to "is there enough engagement data to learn from".
- **Design:** the scope above.
- **Ceiling:** none.
- **Test plan:** DB tests on a synthetic `interactions` fixture; a CLI test of the gate line.
- **Eval:** none.
- **Risks:** the thresholds are judgment calls; they are named constants with this rationale beside them.
- **Decisions:** RM-22a.D1, thresholds: **500 opens / 100 documents / 30 ratings over 90 days**.
- **Security / privacy:** counts only; no titles.

##### RM-22b — The engagement stage (default OFF), built only when the gate is met

- **Design:** `EngagementStage` in `src/brain/search_engagement.py` multiplies each candidate's
  score by `clamp(1 + α·log1p(opens_decayed) + β·(useful − irrelevant), 0.8, 1.25)`, where
  `opens_decayed` sums opens with a 30-day half-life, then re-sorts. One `SELECT … WHERE
  document_id = ANY(%s)` per search over the candidates; no migration. If that query exceeds 20 ms
  at p95 on the owner's data, a materialized table refreshed by the brief daemon takes migration
  032 (RM-22b.D2).
- **Feedback-loop guard:** the multiplier is capped at 1.25, opens decay, and opens recorded in the
  same session as the search are excluded, so a result is not boosted by the click it just got.
- **Settings:** `BRAIN_ENGAGEMENT_ALPHA=0` (default, i.e. off), `BRAIN_ENGAGEMENT_BETA=0`.
- **Eval:** the golden judgments are independent of opens, so the eval can only prove the stage does
  no harm. **Adoption rule:** no mean regresses by more than `1e-4`, and the owner opts in. A
  `ci.json` re-record follows adoption only if a metric moves.
- **Acceptance:** off → unchanged outputs and signature; on, a synthetic heavily-opened document
  moves up within the cap; same-session opens do not count.
- **Depends on:** RM-22a (gate met), RM-09. **Size:** M.
- **Decisions:** RM-22b.D1, demote on `rated_irrelevant`: **yes**, symmetric with useful, within
  the clamp. RM-22b.D2, materialize: **only if the query budget is exceeded**.
- **Source / goal:** the parent's; results the owner opens and rates well rank a little higher.
- **Ceiling:** none (a new leaf).
- **Test plan:** pure tests for the boost and the clamp; a DB test for the aggregate; per-surface stage tests; an eval-runner test with the stage.
- **Risks:** rich-get-richer; the cap, the decay and the same-session exclusion bound it.
- **Security / privacy:** none.

#### RM-23 — Copy the note's path (external-editor affordance)

- **Source:** BL-4; consolidation spec §8.6 and Q4 (answer: option (a), copy-path; (b) on request;
  (c) scoped out).
- **Goal:** One click in the inspector copies the note's path, so it can be opened in any editor.
- **Design:** a "Copy path" button in the inspector (`inspector.js`) calls
  `navigator.clipboard.writeText(path)`. It is rendered only when `window.isSecureContext` and
  `navigator.clipboard` exist: an `http://127.0.0.1` page is a secure context, a LAN `http://` page
  is not, so the button is simply absent over `--lan`. `GET /api/notes/{id}` adds `abs_path` **only
  on a loopback-bound server** (the bind classification `ui.context.address_class` already makes,
  which the Ingest spec's D8 gate reuses; `UiContext` gains one derived boolean), because an absolute filesystem path is
  information a LAN reader does not need; elsewhere the button copies `vault_path`. A short status
  ("Copied") is set with `textContent`.
- **Ceiling:** `inspector.js` 517 → about 540. **Acceptance:** (1) loopback: clicking puts the
  absolute path on the clipboard (playwright with clipboard permissions granted); (2) a LAN-bound
  context payload has no `abs_path`; (3) no `innerHTML` added.
- **Test plan:** route test for `abs_path` gating; browser test; static test. **Eval:** none.
- **Depends on:** nothing. **Size:** XS.
- **Decisions:** RM-23.D1, the `obsidian://` link: **not now**; it needs the Obsidian vault name,
  which `brain` does not know, and Q4 made it on-request.
- **Security / privacy:** `abs_path` only on a loopback bind; no server-side editor spawning (Q4's option (c) stays out).
- **Risks:** browsers differ in clipboard permission prompts; the button reports failure in its status text rather than silently.

#### RM-24 — `--entity` search filter

- **Source:** roadmap 3.3 ("filter by entity", the leg GraphRAG did not ship); backlog rank 17. The
  backlog asks whether it is wanted; the owner asked for the whole roadmap, so it is built, small.
- **Goal:** `brain search "q" --entity "<name>"` returns only documents that mention that entity in
  the graph.
- **Design:**
  - New leaf `src/brain/entity_filter.py`: `resolve_entity_ids(conn, *, tenant_id: str, name: str,
    entity_type: str | None) -> list[str]`, matching `canonical_key = lower(name)` first, then a
    case-insensitive `name` match. Raises `EntityNotFound` or `EntityAmbiguous` (several types, no
    `--entity-type`), mirroring `PersonNotFound` / `PersonAmbiguous`.
  - `search_predicate.build_predicate` gains `entity_ids` and `entity_tenant`, adding `d.id IN
    (SELECT gem.document_id FROM graph_entity_mentions gem WHERE gem.tenant_id = %s AND
    gem.entity_id = ANY(%s::uuid[]))`. `hybrid_search` gains the two keyword arguments and forwards
    them (a recorded growth of `search.py`, about 6 lines). Every leg, the total count and the
    facets see the filter, because they share the predicate.
  - Surfaces: `brain search` and `brain explain` gain `--entity` and `--entity-type` (`cli_search.py`
    budget, §2.2); MCP `brain_search` gains `entity: str | None` (recorded growth of
    `mcp_server.py`; the tool lives there). The UI is out of scope for v1.
  - With the graph disabled or the AGE tables absent: exit 2 (CLI) / `INVALID_PARAMS` (MCP) with
    "`--entity` needs the graph: set BRAIN_GRAPH_ENABLED=true and run `brain graphrag build`".
- **Ceiling:** `search.py` and `mcp_server.py`, both recorded. **Acceptance:** (1) a synthetic
  entity's documents are the only results; (2) ambiguity and not-found map to exit 2 /
  `INVALID_PARAMS`; (3) `total_documents` and facets agree with the filtered set; (4) graph-off
  error.
- **Test plan:** resolver DB tests on 5434 (AGE), predicate unit tests, CLI and MCP tests.
- **Eval:** none (a filter; no golden row uses it). **Depends on:** RM-09 (pipeline), RM-12 not
  required. **Size:** S.
- **Decisions:** RM-24.D1, entity resolution through aliases: **yes**, if `graph_rag.aliases` has
  merged a name, the canonical entity is used.
- **Security / privacy:** the filter can only narrow a result set; MCP's confidential lens is unchanged.
- **Risks:** an alias the resolver misses gives empty results; `EntityNotFound`'s message points at `brain graphrag entities`.

### Wave 6 — Watcher, graph and log-privacy residues

The backlog's rank 16: low-severity residues, each with a known repair, none losing data. They are a
wave of their own here, instead of "when the module is next opened", because the owner asked for
the whole backlog to close. They touch disjoint files except RM-25 and RM-26 (both in `vault/`), so
those two serialize.

#### RM-25 — Move identity for an id-less replacement file (H-1)

- **Source:** H-1 (PR #52 residue).
- **Problem:** `vault.move_identity.is_another_document` returns `False` for an id-less destination
  (the plan's ruling: an id-less file is identified by its path). So a brand-new, never-synced,
  id-less file that replaces P and is renamed P → Q within one debounce window adopts P's document
  id and its backlinks.
- **Design:** for an id-less destination only, compare content: if the word-5-shingle Jaccard
  (`near_dup.shingles` from RM-19 and `set_similarity.jaccard`) between the row's stored content and
  the destination file's body is **below 0.2**, the destination is another document. An editor's
  atomic save of the same note keeps far more than 20% of its shingles, so the in-place rename (and
  the backlinks) survive for every real rename. The threshold is a named constant with that
  rationale beside it.
- **Ceiling:** `move_identity.py` is a 46-line leaf; `vault/watch.py` is not touched.
- **Acceptance:** (1) the H-1 sequence (replace P with an unrelated id-less file, rename within the
  window) no longer carries P's id; the old row is deleted and Q syncs fresh; (2) an atomic-save
  rename of the same content keeps the id; (3) a rewrite that keeps ≥ 20% shingles keeps the id
  (documented boundary).
- **Test plan:** the watcher race harness the PR #52 tests use (no sleeps; barriers), RED first.
- **Depends on:** RM-19 (shingles). **Size:** S.
- **Risks:** a note rewritten almost entirely **and** renamed within one debounce window loses its
  backlinks. Recorded as the accepted trade.
- **Decisions:** RM-25.D1, threshold: **0.2**.
- **Goal:** a brand-new file that replaces and renames a note never inherits the old note's identity or backlinks.
- **Eval:** none.
- **Security / privacy:** none.

#### RM-26 — Owed watcher work survives shutdown and reconnects (H-2, H-4)

- **Source:** H-2 (PR #51) and H-4 (PR #56).
- **Problems:** (H-2) a watcher closed by the drain drops late events; the next startup sync
  reconciles upserts, but a delete is pruned only with prune on. (H-4a) a delete that committed just
  before the connection died re-runs as a no-op and leaves a ghost AGE `Document` vertex (repair
  today: `brain graphrag remove`). (H-4b) a delete not applied at shutdown is redone only by `brain
  vault sync --prune`.
- **Design:** a new leaf `src/brain/vault/owed_work.py` keeps a small journal at
  `$BRAIN_HOME/state/watcher-owed.jsonl` (append-only JSON lines, each written with one `write` +
  `fsync`; rotated after a successful replay):
  - the drain's dropped-event path (H-2) and `_report_unapplied` (H-4b) append `{"action":
    "delete", "path": "<vault-relative>"}` instead of only logging;
  - the delete job captures the row's document id **before** deleting; when a reconnect re-run
    finds the row already gone, it hands that id to the graph lane's remove (H-4a), the same
    operation `brain graphrag remove` performs;
  - at startup, before the observer starts, `replay_owed(conn, vault_root, graph_lane)` deletes the
    row of each owed path whose file is still absent (a **targeted** prune of exactly those paths,
    never a full prune), and enqueues graph removes for owed ids.
- **Ceiling:** `vault/watch.py` gets wiring only (the two append calls, the id capture, the replay
  call): about 15 lines, recorded inline and in its docstring, promoting its row's descriptive hop.
- **Acceptance:** (1) a late delete event after close is applied at the next start; (2) a delete
  unapplied at shutdown is applied at the next start without `--prune`; (3) the reconnect re-run of
  a committed delete removes the AGE vertex; (4) an owed path whose file reappeared is **not**
  deleted; (5) a corrupt journal line is skipped with a class-only WARNING and the rest replays.
- **Test plan:** the PR #51/#56 harnesses (barriers, `tests/thread_wait.py`), AGE test instance for
  (3), RED first for each of H-2, H-4a, H-4b.
- **Depends on:** nothing; serialize with RM-25 (both `vault/`). **Size:** S.
- **Decisions:** RM-26.D1, a file journal vs a table: **file**; the watcher may be owed work
  precisely when the database is unreachable.
- **Goal:** no delete the watcher owed is lost, and no ghost graph vertex is left, across a shutdown or a lost connection.
- **Eval:** none.
- **Risks:** a journal replayed after the owner restored an older backup; each owed path is re-checked against disk before anything is deleted.
- **Security / privacy:** the journal holds vault-relative paths and document ids, no content, and is created with mode 0600.

#### RM-27 — Graph-lock residuals 2–4 (H-3)

- **Source:** H-3 (PR #53, its plan's "Residuals").
- **Design:**
  - **Residual 2.** `graph_rag.build.build_graph` treats a document deleted between id enumeration
    and its read phase as **skipped** (counted in the build report as `skipped_deleted`), not as a
    `GraphReconcileError`. A deleted document needs no graph.
  - **Residual 3.** `ReconcilePlan` re-checks only `content_hash`, so a metadata-only edit (for
    example to participants) between plan and write can be overtaken. The plan records an
    `inputs_fingerprint` (SHA-256 over every column the plan read: `content_hash`, `content_type`,
    and the participant metadata it resolved), and the write re-checks that.
  - **Residual 4.** The W6/W7 cross-type collapse window is cosmetic. **No code change**: the
    module docstring of `graph_rag/cross_type.py` records why, and this ticket closes it as
    accepted.
- **Ceiling:** `graph_rag/build.py` (346) and `reconcile_plan.py` are under 800.
- **Acceptance:** (1) a document deleted mid-build is counted and the build succeeds; (2) a
  participants-only edit between plan and write invalidates the plan; (3) a no-op re-check still
  passes.
- **Test plan:** barrier-driven tests on the AGE test instance. **Size:** S. **Depends on:** nothing.
- **Goal:** the graph build tolerates a concurrent delete, and never applies a plan whose inputs changed.
- **Eval:** none.
- **Risks:** none new.
- **Decisions:** RM-27.D1, residual 4: **accept**, documented in place.
- **Security / privacy:** none.

#### RM-28 — Close the log-privacy guard's blind spots (H-5)

- **Source:** H-5 (PR #58, the G1 guard's "Not covered").
- **Design:** extend `tests/test_log_privacy.py`'s AST scan with three rules: (1) **taint
  propagation**: a name assigned, inside an `except … as e` handler, from an expression that uses
  `e` (other than `describe_failure(e)` or `type(e).__name__`) is tainted for the rest of the
  function, so logging it after the handler is flagged; (2) **attribute stores**: `obj.attr = e`
  (or a tainted value) is flagged unless the stored value is class-only; (3) **nested scopes**: a
  `def` or `lambda` defined inside a handler is scanned with `e` in scope. Each rule ships with
  synthetic positive and negative snippets (the file's existing self-test pattern) and a mutation
  proof. Real hits in `src/` are fixed in the same PR when there are ten or fewer; otherwise they
  are listed in the PR and fixed in a follow-up ticket appended to Appendix B.
- **Acceptance:** each rule catches its synthetic case and spares its negative; the guard is green
  on `src/` at merge. **Size:** S. **Depends on:** nothing.
- **Goal:** the G1 guard catches the three patterns it currently documents as not covered.
- **Ceiling:** test code only; `tests/test_log_privacy.py` (541 lines) stays under 800, or the new rules move to `tests/log_privacy_rules.py`.
- **Eval:** none.
- **Risks:** false positives; each rule has a negative synthetic case.
- **Decisions:** RM-28.D1, fix in the same PR: **when there are ten or fewer hits**.
- **Security / privacy:** this ticket is privacy work; it adds no allowlist entry without a written reason.

#### RM-29 — Class-only terminal error reports (H-6)

- **Source:** H-6 (PR #58).
- **Design:** `vault/export.py`'s `summary.errors.append(f"{relative}: {e}")` becomes
  `f"{relative}: {describe_failure(e)}"` (which keeps the errno, `OSError[ENOSPC]`, an operator
  needs). `cli_sensitivity.py`'s `_apply_redact` records `describe_failure(exc)` instead of
  `str(exc)`, with the documented hash-collision case given a fixed sentence ("redacted body
  collides with another document's content hash"). Both were judged content-free; this aligns them
  with the house rule so no reviewer has to re-judge them.
- **Ceiling:** `vault/export.py` (764) rewritten in place, net zero.
- **Acceptance:** a sentinel exception message never appears in either report. **Size:** XS.
- **Depends on:** nothing.
- **Goal:** no terminal error report can quote an exception message.
- **Test plan:** sentinel tests on both report paths (a sentinel message must not appear).
- **Eval:** none.
- **Risks:** none.
- **Decisions:** none.
- **Security / privacy:** the design is the privacy fix.

### Wave 7 — Source breadth, part 1

The backlog's rank 18, ordered by what the owner generates most and by shared machinery: calendar
and the code host first (daily activity, existing authenticated CLIs), then OCR (which three later
tickets reuse), then the drop-folder runner (which four later tickets reuse), then the trackers and
note apps. Every ticket follows the §2.4 protocol; the "Protocol" block below each one answers its
eight points in order.

#### RM-30 — Calendar events as documents

- **Source:** roadmap 2.3. Today `vault/derived_links/directory.refresh_calendar` reads events only
  to refresh the **people directory**, after a Krisp ingest; no event is a document.
- **Goal:** Meetings without a transcript (external events, interviews, conferences) are searchable,
  filterable by `--person`, and dated by their start time.
- **Protocol:**
  1. *Path (b):* a `gws` shell-out, reusing the `gws calendar events list` call shape and the
     injectable runner type of `directory.refresh_calendar`. New `src/brain/ingest/calendar.py`
     (extraction) and `src/brain/cli_ingest_calendar.py` (`brain ingest-calendar`, RM-06 seam).
  2. *Kind:* `calendar`. `content_type`: `calendar_event`.
  3. *Dedup:* `(calendar, <event id>)` for single events; **one document per recurring series**,
     keyed `(calendar, <recurringEventId>)`, whose body lists the occurrence dates inside the window
     (one document per instance would create dozens of near-identical rows, which RM-19 would then
     flag).
  4. *Credentials:* `gws`'s own login; none stored.
  5. *Scope:* `--since YYYY-MM-DD` **and** `--until YYYY-MM-DD` are both required, the window is at
     most 92 days, and `--calendar` defaults to `primary`. Without them: exit 2, "ingest-calendar
     requires --since and --until (at most 92 days)". `--dry-run` lists titles and dates.
  6. *PII:* stored are the title, start/end, location text with URLs removed, the description, and
     attendee **display names** in the body. Attendee addresses go only into
     `metadata.participants` (the field `--person` resolves through, as Gmail's do), never into the
     body. Conference links and dial-in blocks are dropped (they carry meeting PINs). Declined
     events, events with no title, and "working location" / out-of-office entries are skipped.
     Events whose visibility is `private` are stored as `confidential`; all others default to
     `normal`. `sent_at` is the start time, so date filters and the recency boost use it.
  7. *Tests:* synthetic `gws` JSON fixtures (single, recurring with exceptions, declined, private,
     all-day), the injected runner, a DB end-to-end test, the scope refusal.
  8. *Dependencies:* none (`gws` exists).
- **Ceiling:** none. **Acceptance:** each fixture class lands as specified; a re-run is a no-op; a
  changed description updates in place; `brain search --source calendar --person "<synthetic>"`
  finds the event. **Eval:** none. **Depends on:** RM-06, RM-07. **Size:** M.
- **Decisions:** RM-30.D1, per-series documents: **yes** (above). RM-30.D2, a daily daemon run:
  **not by default**; the owner can schedule `brain ingest-calendar --since … --until …` with the
  drop-folder job's launchd interval (RM-33) if wanted.
- **Design:** the protocol answers above, plus the rendering of one event (or series) as Markdown: title, when, where, attendees' display names, description, and for a series the occurrence dates (a count plus the last ten).
- **Security / privacy:** protocol items 4–6.
- **Risks:** a daily stand-up series is long; the occurrence summary bounds it.

#### RM-31 — Code-host activity: pull requests, issues and READMEs

- **Source:** roadmap 2.1 ("your authored activity", not whole organisations).
- **Goal:** The owner's own PRs, reviews, issues and repository READMEs are searchable.
- **Protocol:**
  1. *Path:* **(a) first**: a recipe in `skills/ingest-brain/SKILL.md` for the GitHub MCP connector
     the owner already has (search the owner's PRs and issues, fetch each, pipe to `brain
     ingest-stdin --source github …`). **(b) second, in the same ticket**: `brain ingest-github`
     (new `src/brain/ingest/github.py`, `src/brain/cli_ingest_github.py`) shelling out to the
     already-authenticated `gh` CLI (`gh api` / `gh search prs --author @me`), with an injectable
     runner, for unattended refresh.
  2. *Kind:* `github`. `content_type`: `github_pr`, `github_issue`, `github_readme`.
  3. *Dedup:* `(github, "<owner>/<repo>#<number>")` and `(github, "<owner>/<repo>:README")`; a re-run
     updates in place when the PR changed.
  4. *Credentials:* `gh auth` / the connector's auth.
  5. *Scope:* at least one of `--repo OWNER/REPO` (repeatable) or `--mine` (author, assignee or
     reviewer is the authenticated user), **and** `--since`. Organisation-wide pulls are not
     offered.
  6. *PII:* body text plus the **owner's own** review comments by default; other people's comments
     only with `--with-thread`. Repositories the API reports as private are stored as
     `confidential`. The secret guard runs on every body.
  7. *Tests:* synthetic `gh` JSON fixtures; runner injected; scope refusal; the private →
     confidential rule.
  8. *Dependencies:* none (`gh` is a soft dependency with a `brain doctor` row).
- **Ceiling:** none. **Eval:** none. **Depends on:** RM-06, RM-07. **Size:** M.
- **Decisions:** RM-31.D1, build (b) too: **yes**, because "my activity this month" is a recurring
  pull and (a) needs a chat session each time.
- **Design:** the protocol answers above; `ingest/github.py` renders a PR as title, state, branch, body and the owner's review comments.
- **Acceptance criteria:** (1) the scope refusal; (2) a synthetic PR lands with only the owner's comments by default; (3) a private repository's documents are `confidential`; (4) a re-run updates in place; (5) the skill recipe names the scope and the sensitivity (a skill-text test).
- **Security / privacy:** protocol items 4–6.
- **Risks:** API rate limits on a long `--since`; one run is capped at 500 items and says so.

#### RM-32 — OCR, and image-only PDFs

- **Source:** roadmap 3.5 = 5.3. Today an image-only PDF ingests as an empty document ("skipped
  (empty document)", `cli_ingest._ingest_outcome_verb`).
- **Goal:** Scanned PDFs and receipts become searchable text.
- **Design:**
  - New leaf `src/brain/ingest/ocr.py`: `OcrEngine` Protocol (`image_to_text(png: bytes, *, lang:
    str, timeout: float) -> str`) and `TesseractOcr`, which runs `tesseract stdin stdout -l <lang>`
    with a timeout (60 s per page) through an injectable runner.
  - `ingest/pdf.py`: when the extracted text averages under 25 characters per page, rasterize the
    pages (at most `BRAIN_OCR_MAX_PAGES`, default 50, at 200 dpi) and OCR them. The text is joined
    with page markers. `metadata.ocr = {"pages": n, "engine": "tesseract"}` records it, so `brain
    show` and `explain` can tell OCR text from born-digital text.
  - Rasterization uses `pypdfium2`, which `pdfplumber` already requires at install time. It is
    declared explicitly in a new `[ocr]` extra with an evidence block (resolved version, and that it
    adds zero wheels on the measured resolve), imported inside the function, so the base install
    never imports it. Without it, or without `tesseract`, an image-only PDF stays "skipped (empty)"
    with a hint line naming the missing piece.
  - Settings `settings/ocr.py`: `BRAIN_OCR` = `auto` (default: OCR when both pieces exist) | `off`;
    `BRAIN_OCR_LANG` = `eng`; `BRAIN_OCR_MAX_PAGES` = 50.
- **Security / privacy:** OCR runs locally. Scanned documents often carry account numbers; the
  secret guard runs on the OCR text like any other body.
- **Ceiling:** none (`pdf.py` 61 lines). **Acceptance:** a synthetic image-only PDF (text rendered to
  an image with Pillow and placed with `reportlab`, both already dev dependencies) ingests with its
  text when OCR is available (fake engine in unit tests; one `live_ocr`-marked test with real
  `tesseract`, skipped when absent); a born-digital PDF is never OCR'd; the page cap holds; a
  timeout on one page skips that page with a class-only WARNING.
- **Test plan:** as above, plus `brain doctor`'s OCR row. **Eval:** none.
- **Depends on:** RM-07, RM-08. **Size:** M.
- **Decisions:** RM-32.D1, engine: **`tesseract`** (Homebrew, offline, mature). macOS Vision through
  `pyobjc` was rejected: a large new native dependency, macOS-only.
- **Risks:** OCR quality on low-resolution scans; the page cap and the per-page timeout bound the cost of a bad file.

#### RM-33 — Image extractor and the drop-folder runner (screenshot watch folder)

- **Source:** roadmap 5.1. The drop-folder runner it introduces is reused by RM-38, RM-39 and RM-40.
- **Goal:** Screenshots saved to a folder become searchable within minutes.
- **Design:**
  - **Image extractor**, registered in `ingest/dispatch.py` for `.png`, `.jpg`, `.jpeg` with
    `in_directory_walks=False`: OCR through RM-32's engine; title = file stem; `content_type =
    "screenshot"`; an image with no OCR text is an empty document (skipped).
  - **Drop-folder runner**, new `src/brain/drop_folder.py` and `brain ingest-folder` (RM-06 seam):

    ```python
    @dataclass(frozen=True)
    class DropFolder:
        path: Path              # resolved; must exist
        mode: Literal["ingest", "capture"]       # "capture" → capture-inbox notes (RM-39)
        kind: str | None        # a member of VALID_SOURCE_KINDS; required when mode == "ingest"
        extensions: frozenset[str]
        sensitivity: str
        after_ingest: Literal["keep", "move"]   # "move" → <path>/imported/

    def scan(folder: DropFolder, *, since: datetime) -> list[Path]: ...  # lstat; no symlinks; no .icloud stubs
    def run(conn, folder: DropFolder, *, since: datetime, ingest: IngestFn) -> DropReport: ...
    ```

    Folders come from `settings/drop_folders.py`, which reads `$BRAIN_HOME/drop-folders.toml`
    (a list of `[[folder]]` tables). **Scope:** `brain ingest-folder --dir PATH --since 7d` for a
    one-off, or `--configured` for every configured folder; `--since` is mandatory either way. Files
    are ingested by `source_path` (file dedup), so a re-run is a no-op.
  - **Watching:** an opt-in launchd job `com.brain.dropfolders` (template under
    `src/brain/templates/launchd/`) runs `brain ingest-folder --configured --since 2d` every 10
    minutes. No new long-running watcher process: the vault watcher stays the only one.
  - **Kind:** `screenshot`; **default sensitivity `confidential`**, because a screenshot can capture
    anything on screen, including credentials the secret guard's patterns miss.
- **Ceiling:** none. **Acceptance:** a synthetic PNG with rendered text is ingested from a
  configured folder; a symlink and an `.icloud` placeholder are skipped; `--since` excludes older
  files; a second run is a no-op; `move` moves the file only after a successful ingest.
- **Test plan:** pure tests for `scan` (tmp_path trees with symlinks, stubs, mtimes); DB tests for
  `run` with a fake ingest; CLI scope refusal; plist render test. **Eval:** none.
- **Depends on:** RM-32. **Size:** M.
- **Decisions:** RM-33.D1, launchd interval vs a watchdog observer: **interval** (no second
  always-on process; ten minutes is fast enough for screenshots).
- **Security / privacy:** screenshots default to `confidential`; configured folders only; symlinks are never followed.
- **Risks:** a screenshot of a credential the guard does not recognise; the confidential default keeps it inside F6's boundary.

#### RM-34 — Jira issues

- **Source:** roadmap 2.8; the owner's decision "Notion + Jira, skip Linear".
- **Protocol:**
  1. *Path (a):* the Atlassian MCP connector the owner has (`searchJiraIssuesUsingJql`,
     `getJiraIssue`) → `brain ingest-stdin --source jira --content-type jira_issue …`. The work is a
     recipe in `skills/ingest-brain/SKILL.md` plus the kind; no API client.
  2. *Kind:* `jira`.
  3. *Dedup:* `(jira, "<site-host>/<ISSUE-KEY>")`, so the same key on two sites cannot collide.
  4. *Credentials:* the connector's.
  5. *Scope:* the recipe **requires a JQL scope** naming the owner and a time bound, by default
     `assignee = currentUser() OR reporter = currentUser()` and `updated >= -30d`, and refuses a JQL
     with no bound. Project-wide pulls are not part of the recipe.
  6. *PII / sensitivity:* summary, status, description and the owner's comments; others' comments
     only on request. **Default `--sensitivity confidential`**, because work-tracker content is the
     employer's.
  7. *Tests:* the kind (validation, facet, mirror path); a synthetic `ingest-stdin` end-to-end with
     the recipe's metadata shape; a skill-text test that the recipe names the scope and the
     sensitivity.
  8. *Dependencies:* none.
- **Ceiling:** none. **Eval:** none. **Depends on:** RM-07. **Size:** S.
- **Decisions:** RM-34.D1, a CLI client: **no**; there is no authenticated Jira CLI on the machine,
  and the connector already holds auth.
- **Goal:** the owner's own tickets are searchable, inside the confidential tier.
- **Design:** the protocol answers above.
- **Acceptance criteria:** (1) the kind validates and buckets in the facet panel; (2) a synthetic `ingest-stdin --source jira` document lands under `_ingested/jira/`; (3) the recipe names the JQL bound and `--sensitivity confidential` (a skill-text test).
- **Security / privacy:** protocol items 4–6.
- **Risks:** the scope rests on the agent following the recipe, the same trust model Krisp and Slack use; the recipe states the refusal explicitly.

#### RM-35 — Notion pages (from Notion's Markdown export)

- **Source:** roadmap 2.8.
- **Protocol:**
  1. *Path (c):* no Notion connector is attached, so the source is Notion's own "Export → Markdown
     & CSV" folder or zip. New `src/brain/ingest/notion_export.py` and `brain ingest-notion
     <export-path>` (RM-06 seam). If a Notion MCP connector is attached later, a recipe replaces
     this path; the kind stays.
  2. *Kind:* `notion`. `content_type`: `notion_page`.
  3. *Dedup:* `(notion, <32-hex page id>)`, parsed from the export's file names (`Title <id>.md`).
     **Not `source_path`**: every export lands in a new folder, so path dedup would duplicate the
     whole workspace on each re-export. The extractor therefore yields documents with
     `source_path=None` and an external id, the sourced-stdin dedup rule.
  4. *Credentials:* none (a local export).
  5. *Scope:* the export path is the scope (the owner chooses what to export); `--since` filters by
     the page's last-edited property when present. A zip is read without extracting to disk.
  6. *PII:* page text; images and attachments skipped; database CSVs ingested one row per document
     only with `--with-databases`. Default sensitivity `normal`, overridable.
  7. *Tests:* a synthetic export tree (nested pages, id suffixes, a CSV, an image) built in
     `tmp_path`; re-export to a new folder is an in-place update.
  8. *Dependencies:* none (`zipfile` is stdlib).
- **Ceiling:** none. **Eval:** none. **Depends on:** RM-06, RM-07. **Size:** M.
- **Goal:** pages the owner exports from Notion become searchable and update in place on re-export.
- **Design:** the protocol answers above.
- **Acceptance criteria:** (1) titles lose the id suffix; (2) a re-export to a new folder updates in place; (3) CSV rows only with the flag; (4) images skipped and counted.
- **Security / privacy:** protocol items 4–6.
- **Risks:** Notion changes its export naming; a file without a parseable id is skipped and counted, never guessed.
- **Decisions:** RM-35.D1, the export instead of an API client: **yes** (no connector to reuse, no token to store).

#### RM-36 — Apple Notes

- **Source:** roadmap 2.4 (the Apple Notes half).
- **Protocol:**
  1. *Path (b):* `osascript -l JavaScript` (macOS built-in) lists notes in named folders and returns
     `{id, name, body (HTML), modified}` as JSON. New `src/brain/ingest/apple_notes.py` with an
     injectable runner, and `brain ingest-apple-notes`.
  2. *Kind:* `applenotes` (one word, because it becomes `_ingested/applenotes/`).
  3. *Dedup:* `(applenotes, <note id>)`; an edited note updates in place.
  4. *Credentials:* none; the first run triggers macOS's Automation permission prompt, which the
     guide documents.
  5. *Scope:* `--folder NAME` (repeatable) is required; `--since` optional. "All notes" is not
     offered.
  6. *PII:* the body is converted from Notes' HTML to Markdown by a small stdlib `html.parser`
     converter (headings, lists, links, checklists); attachments are skipped; locked notes cannot be
     read and are counted as skipped. Default sensitivity `normal`.
  7. *Tests:* synthetic JXA JSON through the runner; the HTML → Markdown converter at 95%; scope
     refusal; a non-macOS host exits 1 with a clear message.
  8. *Dependencies:* none.
- **Timeouts:** 120 s for the listing call. **Ceiling:** none. **Eval:** none.
- **Depends on:** RM-06, RM-07. **Size:** M.
- **Decisions:** RM-36.D1, `markdownify` for HTML: **no**; a new dependency for a narrow, testable
  converter.
- **Goal:** notes in folders the owner names become searchable.
- **Design:** the protocol answers above.
- **Acceptance criteria:** (1) `--folder` is required; (2) synthetic notes land and locked ones are counted; (3) the converter keeps headings, lists, links and checklists; (4) a non-macOS host exits 1.
- **Security / privacy:** protocol items 4–6.
- **Risks:** Notes scripting is slow on big folders; the timeout and `--since` bound a run.

#### RM-37 — External markdown vault, with deletions tracked

- **Source:** roadmap 2.4 (the external-vault half). `brain ingest-dir` already imports one; the
  missing piece is re-sync with deletions (backlog §1.2).
- **Design:** `brain ingest-dir <dir> --sync`. After the normal ingest, the new leaf
  `src/brain/ingest/import_sync.py` finds ingested documents whose `source_path` (stored resolved
  and absolute by the extractors) lies under the resolved `<dir>` and no longer exists on disk, and
  deletes them through `vault.delete.delete_document`. **Dry-run by default** (it lists what it
  would delete); `--apply` deletes.
- **Safety rules:** (1) refuse when `<dir>` is missing or unmounted, or when it holds zero
  supported files while the database holds documents under it, unless `--allow-empty` (an unmounted
  drive must not prune a whole import); (2) refuse when `<dir>` is inside the brain vault (that tree
  belongs to the vault watcher); (3) only `kind = 'ingested'` rows are ever candidates.
- **Kind:** stays `manual` (an imported file is a manual import); no new kind.
- **Ceiling:** `cli_ingest.py` gains the `--sync/--apply/--allow-empty` options (over the ceiling;
  recorded inline and in its docstring); the logic is in the leaf. **Acceptance:** a deleted file's
  document is listed in dry-run and removed with `--apply`; the three safety refusals; a vault-tier
  row is never touched.
- **Test plan:** `tmp_path` trees, DB tests, CLI tests. **Eval:** none. **Depends on:** RM-07.
  **Size:** M.
- **Goal:** re-running an external-vault import mirrors deletions, safely.
- **Security / privacy:** deletes only ingested rows under the named directory; never vault-tier rows.
- **Risks:** a mistyped directory; dry-run by default plus the three refusals.
- **Decisions:** RM-37.D1, a new kind: **no** (stays `manual`).

#### RM-38 — Voice memos with local transcription

- **Source:** roadmap 5.2 (and the voice-memo part of 2.9).
- **Protocol:**
  1. *Path (c):* an audio extractor registered for `.m4a`, `.mp3`, `.wav` with
     `in_directory_walks=False`. It converts to 16 kHz mono WAV with `afconvert` (macOS built-in) and
     transcribes with `whisper-cli` from whisper.cpp (Homebrew `whisper-cpp`), both through an
     injectable runner. Memos arrive through the RM-33 drop-folder runner (the owner shares or
     exports from the Voice Memos app into a configured folder).
  2. *Kind:* `voice`. `content_type`: `voice_memo`.
  3. *Dedup:* `source_path` (file).
  4. *Credentials:* none.
  5. *Scope:* the drop folder plus the mandatory `--since`.
  6. *PII:* the transcript only; the audio is not copied. Default sensitivity `normal`.
  7. *Tests:* runner-injected conversion and transcription; a `live_whisper`-marked test skipped
     without the binary and model.
  8. *Dependencies:* `whisper-cli` and a model file named by `BRAIN_WHISPER_MODEL` (no default
     download; the guide names a tested small English model and its size). `brain doctor` row.
- **Timeouts:** conversion 60 s; transcription `2 × duration + 60 s`.
- **Ceiling:** none. **Eval:** none. **Depends on:** RM-33. **Size:** M.
- **Decisions:** RM-38.D1, `faster-whisper` instead: **no**; a new native Python dependency, where
  whisper.cpp is a Homebrew binary with Metal support.
- **Goal:** voice memos become searchable transcripts, transcribed on the machine.
- **Design:** the protocol answers above.
- **Acceptance criteria:** (1) a synthetic `.m4a` path yields the fake transcript as a `voice` document; (2) a conversion or transcription timeout skips the file with a class-only WARNING; (3) `ingest-dir` does not pick up audio (`in_directory_walks=False`).
- **Security / privacy:** audio never leaves the machine; protocol items 4–6.
- **Risks:** long memos; `BRAIN_WHISPER_MAX_MINUTES` (default 60) skips longer ones with a count.

#### RM-39 — Mobile share-sheet capture

- **Source:** roadmap 4.5 ("a share-sheet hook, not an app"); backlog: capture exists, the share
  sheet is missing.
- **Design:** an iOS Shortcut ("Save to Brain", documented step by step in
  `docs/guides/mobile-capture.md`) writes the shared text or URL, plus an optional comment, as a
  `.md` file into an iCloud Drive folder. On the Mac that folder is a configured drop folder with
  `mode = "capture"` (RM-33's `DropFolder`): `brain capture import --dir <folder> --since 7d` (a new command on the existing
  `capture` sub-app in `_capture_command.py`, 469 lines) turns each file into a capture-inbox note
  through the same path `brain capture` uses, then moves the file to `imported/`. The RM-33 launchd
  job runs it every 10 minutes when the folder is configured. iCloud placeholder files (`.icloud`)
  are skipped until they download.
- **Why not an HTTP endpoint:** a Shortcut sends no `Origin` header, so it would need a new
  token-only, Origin-exempt write route, which is a change to the security model (Ingest spec D8,
  Q1). The folder needs no network surface and queues offline.
- **Ceiling:** none. **Acceptance:** a synthetic shared file becomes an inbox note with the comment
  and the URL; a re-run does not duplicate; placeholders are skipped.
- **Test plan:** `tmp_path` folder, DB + `tmp_path` vault, CLI tests. **Eval:** none.
- **Depends on:** RM-33. **Size:** S.
- **Goal:** share text or a link from the phone into the capture inbox, with no network surface on the Mac.
- **Risks:** iCloud sync delay; placeholders are retried on the next run.
- **Decisions:** RM-39.D1, an HTTP endpoint: **no** (above).

#### RM-40 — Ingest tab, phase 4: the inbox folder

- **Source:** Ingest spec §10 phase 4 and §5.5 ("only if asked"; the owner asked for the whole
  roadmap).
- **Design:** the Ingest spec's §5.5 design, with one change: the inbox is the first configured
  drop folder of kind `manual` from RM-33's `drop-folders.toml`, so no `config.py` knob is needed.
  `GET /api/ingest/inbox` lists names; `POST /api/ingest/inbox` takes `{name}` (never a path),
  resolves it under the root with the resolve-then-compare rule of `vault/paths.py`, refuses
  symlinks with `lstat`, and submits a job of kind `ingest.inbox`. After a successful ingest the
  file moves to `imported/`, which answers §5.5's open lifecycle question.
- **Security review:** with `ecc:security-reviewer` on opus (a new traversal surface).
- **Acceptance:** §5.5's rules, each tested (a name with `/`, `..`, a symlink, a file swapped
  between listing and open). **Depends on:** RM-13, RM-33. **Size:** S.
- **Goal:** ingest, by name, a file already sitting in the inbox folder, from the browser.
- **Ceiling:** none.
- **Eval:** none.
- **Risks:** the listing-to-open race (TOCTOU) §5.5 names; the open re-validates the name and uses `lstat`.
- **Decisions:** RM-40.D1, the inbox root: **the first configured `manual` drop folder**.

### Wave 8 — Source breadth, part 2: the heaviest privacy load and the least evidence of need

The backlog's rank 19. Kept, because the owner accepted each item on 2026-05-11; last, for the
reasons the backlog gives.

#### RM-41 — Browser history and bookmarks, allowlist only

- **Source:** roadmap 2.5; the owner's decision "build, whitelist-driven, opt-in per domain".
- **Protocol:**
  1. *Path (c):* read the browser's local stores with stdlib `sqlite3` and `plistlib`: Chrome's
     `History` (copied to a temporary file first, because Chrome holds a lock on it) and
     `Bookmarks` JSON; Safari's `History.db` and `Bookmarks.plist` (which need Full Disk Access; the
     guide says so). New `src/brain/ingest/browser.py` and `brain ingest-web`.
  2. *Kind:* `web`. `content_type`: `web_page`.
  3. *Dedup:* `(web, <normalized URL>)`; a re-run refreshes the visit count in place.
  4. *Credentials:* none.
  5. *Scope:* **an allowlist is mandatory**: `$BRAIN_HOME/web-allowlist.txt`, one host or
     `*.domain` per line. The command refuses to run when the file is missing or empty. Also
     required: `--browser chrome|safari` and `--since`. A page qualifies when its host is allowlisted
     **and** it was bookmarked or visited at least `--min-visits` times (default 3).
  6. *PII:* the URL's **query string and fragment are dropped** (they carry session tokens and search
     terms); stored are title, normalized URL, host, visit count and last visit. **No page fetch by
     default.** `--fetch` (opt-in) downloads the page body: https only, allowlisted hosts only, no
     redirect off the allowlist, 1 MiB cap, 10 s timeout, HTML reduced to text with stdlib
     `html.parser`. Default sensitivity `normal`.
  7. *Tests:* synthetic `History` SQLite files with the Chrome schema subset, Safari plist fixtures,
     the allowlist refusal, query stripping, and `--fetch` against an `httpx.MockTransport` (redirect
     off-allowlist refused).
  8. *Dependencies:* none.
- **Security review:** with `ecc:security-reviewer` on opus (`--fetch` is a network egress).
- **Ceiling:** none. **Eval:** none. **Depends on:** RM-06, RM-07. **Size:** L.
- **Decisions:** RM-41.D1, per-URL documents: **yes**, qualified by the visit threshold so a host's
  long tail does not flood search. RM-41.D2, fetch by default: **no**.
- **Goal:** pages from allowlisted sites the owner revisits or bookmarks become searchable.
- **Design:** the protocol answers above.
- **Acceptance criteria:** (1) no allowlist → refused; (2) an allowlisted, often-visited page lands with its query string stripped; (3) a page off the allowlist never becomes a document; (4) `--fetch` refuses a redirect off the allowlist; (5) Safari without Full Disk Access exits 1 with the guide's pointer.
- **Risks:** browser schema changes; the fixtures pin the columns read, and a missing column fails loudly.

#### RM-42 — Code-aware chunker

- **Source:** roadmap 7.3 (paired with 2.6).
- **Design:**
  - New leaf `src/brain/ingest/chunkers.py`: `chunk_for(content_type: str, content: str, *,
    count_tokens) -> list[Chunk]` dispatches `code` to `chunk_code` and everything else to the
    existing `chunk_text`. `ingest/__init__.py`'s two `chunk_text(...)` calls (new document and
    in-place update) become `chunk_for(...)`: a rewrite in place, net zero lines, recorded as a hop.
  - New leaf `src/brain/ingest/code_chunker.py`: Python is split with the stdlib `ast` module at
    top-level functions and classes (large classes by method), with the module docstring and imports
    as a header chunk. Other languages use a structural fallback: a small per-language table of
    top-level declaration patterns (JavaScript/TypeScript, Go, Rust, Java, shell), then blank-line
    blocks, packed to the token budget. Every chunk starts with `path:start-end` and the enclosing
    symbol name, so a hit says where it is.
- **Ceiling:** `ingest/__init__.py`, net zero, hop recorded. **Acceptance:** Python boundaries
  exact on synthetic files; fallback boundaries on the other languages' fixtures; prose
  `content_type`s chunk byte-identically to before (`chunk_text` regression).
- **Eval:** none for prose (unchanged chunks). **Depends on:** RM-07. **Size:** M.
- **Decisions:** RM-42.D1, `tree-sitter`: **not in v1**; it becomes the `[code]` extra (§2.6) only if
  RM-43's code queries show the fallback misses boundaries that matter.
- **Goal:** code is chunked at function and class boundaries, so a hit is a whole definition.
- **Test plan:** pure tests on synthetic sources per language; the prose `chunk_text` regression.
- **Risks:** the fallback splits a definition in an unlisted language; packing keeps every chunk within budget regardless.
- **Security / privacy:** none.

#### RM-43 — Code-repository ingestion (the owner's own files)

- **Source:** roadmap 2.6.
- **Protocol:**
  1. *Path (c):* `brain ingest-code <repo-dir>`, using `git` (`git ls-files`, and `git log
     --author=<email> --name-only --since=<date>` to keep only files the owner touched). New
     `src/brain/ingest/code_repo.py`; files go through `ingest_document` with `content_type="code"`,
     so RM-42 chunks them.
  2. *Kind:* `code`.
  3. *Dedup:* `source_path` (file).
  4. *Credentials:* none (local clone).
  5. *Scope:* `<repo-dir>` must be a git work tree; `--author-email` (repeatable) and `--since` are
     required; `--ext` narrows. Excluded always: anything `git` ignores, vendored and build
     directories (`node_modules`, `.venv`, `vendor`, `dist`, `build`, `third_party`), files over 200
     KB, binaries, minified files and lockfiles.
  6. *PII / secrets:* **the secret guard runs in `reject` mode** for this command (a key committed
     to a repo is the classic leak); default sensitivity `confidential` (an employer's code), so it
     stays inside F6's boundary; `--sensitivity normal` for personal repos.
  7. *Tests:* a synthetic git repo built in `tmp_path` with two authors, an ignored file, a vendored
     directory and a planted fake key (from `tests/secret_fixtures.py`) that must be rejected.
  8. *Dependencies:* `git` (always present on the owner's machine; still a soft check).
- **Eval:** after ingesting, the owner adds 3–5 code queries to the local golden corpus and
  re-records `ci.json` (triggers: a document-set change and a golden-corpus edit), so code retrieval
  is measured from then on. **Depends on:** RM-42. **Size:** M.
- **Goal:** files the owner wrote in their repositories become searchable.
- **Design:** the protocol answers above.
- **Ceiling:** none.
- **Acceptance criteria:** (1) only the owner's files are ingested; (2) every exclusion holds; (3) the planted key is rejected; (4) the default sensitivity is `confidential`; (5) a re-run updates in place.
- **Security / privacy:** protocol items 4–6.
- **Risks:** a large monorepo; `--ext`, the 200 KB cap and `--max-files` (default 2,000) bound a run.
- **Decisions:** RM-43.D1, secret guard in `reject` mode for code: **yes**.

#### RM-44 — Social bookmarks

- **Source:** roadmap 2.9 (the social-bookmarks item).
- **Design:** `brain ingest-bookmarks --format x-archive <archive-dir>` reads the liked and
  bookmarked posts files of the platform's own data-archive export (JavaScript files whose payload
  follows a `window.YTD… =` prefix; parsed as JSON after the prefix, never executed). One document
  per post: text, author handle, date, and the post URL without its query string. Kind `social`,
  `content_type` `social_post`, dedup `(social, <post id>)`. **Scope:** the archive path plus
  `--since`. Browser bookmarks are RM-41's, not this ticket's.
- **PII:** posts are third-party public text; the author handle is kept, nothing else about them.
- **Tests:** a synthetic archive tree. **Depends on:** RM-07. **Size:** S.
- **Goal:** posts the owner liked or bookmarked become searchable.
- **Ceiling:** none.
- **Acceptance criteria:** (1) the prefix is stripped and the payload parsed as JSON, never executed; (2) one document per post; (3) query strings stripped; (4) `--since` honoured.
- **Eval:** none.
- **Risks:** the archive format changes; the parser fails loudly on an unknown shape.
- **Decisions:** RM-44.D1, one kind for any platform: **yes, `social`**.
- **Security / privacy:** third-party public text only; the file is never evaluated.

#### RM-45 — Receipts (Gmail attachments)

- **Source:** roadmap 2.9 (receipts).
- **Design:** receipt emails already ingest with `brain ingest-gmail --label <receipts-label>`; the
  missing piece is their PDF and image attachments. `ingest-gmail --with-attachments` downloads each
  PDF or image attachment of at most 10 MiB with `gws gmail users messages attachments get`
  (injectable runner, 60 s timeout), extracts it through `ingest.dispatch` (PDF text, or RM-32's OCR),
  and appends it to the thread document as an `## Attachment: <name>` section. The logic lives in a
  new leaf, `src/brain/ingest/gmail_attachments.py`; `gmail.py` (669 lines) calls it.
- **Scope:** the existing mandatory Gmail scope flags; `--with-attachments` is additionally refused
  without `--label` or `--query` (an attachment pull over a date range alone is too broad).
- **PII:** receipts carry addresses and partial card numbers; with `--with-attachments` the default
  sensitivity becomes `confidential`, and the secret guard runs on the appended text.
- **Ceiling:** `cli_ingest.py` gains one option (recorded). **Depends on:** RM-32. **Size:** M.
- **Decisions:** RM-45.D1, attachments as child documents: **no**, which is why RM-60's
  `parent_document_id` stays unbuilt.
- **Goal:** receipt attachments are searchable together with their email.
- **Acceptance criteria:** (1) a synthetic message with a PDF attachment gains the section; (2) an over-size attachment is skipped and counted; (3) refused without `--label` or `--query`; (4) `confidential` by default with the flag.
- **Test plan:** runner-injected `gws` attachment fixtures, the dispatch path, the CLI refusal.
- **Eval:** none.
- **Risks:** large pulls; the size cap plus a cap of 50 attachments per run.
- **Security / privacy:** the PII bullet above.

### Wave 9 — The deferred, the conditional, and the polish

The backlog's rank 20 and its "leave deferred" row, plus the conditional items of its §1.8. Each
gets the smallest design that respects the rule or decision that deferred it. Two tickets would
change a standing user decision; they are marked and listed in §5.

#### RM-46 — MCP over HTTP, for reaching the brain from another machine

- **Source:** BL-10; the MCP server spec's "Out of scope" ("HTTP / SSE transport"); R1, which is a
  complete design ("evaluated, deferred").
- **Goal:** An opt-in, loopback-by-default, bearer-authenticated, **read-only-by-default**
  streamable-HTTP transport, so a second device on a private network can call the read tools.
- **Design:** **R1 as written**, with three updates for `02c2abc`: (1) the `brain mcp` sub-app R1
  chose registers through RM-06's seam, not in `cli.py`; (2) R1's tool-visibility allowlist must
  include the tools added since (RM-12, RM-15, RM-19, RM-21, RM-55), each classified read or write;
  (3) the stdio path stays byte-for-byte unchanged, as R1 requires; (4) R1 §2's SDK capability
  audit is re-run first, because it read `mcp` 1.28.0 and the Ingest spec (C1) measured 2.0.0 in
  the main venv. Every confidential lens stays at
  its MCP default (exclude).
- **Conflict:** R1 deferred it for lack of need, not for a rule. The owner asked for the whole
  roadmap, so it is built, **off by default**: nothing listens until `brain mcp serve` runs.
- **Security review** with `ecc:security-reviewer` on opus.
- **Acceptance:** every case in R1 §8 (test plan) passes, and each failure mode in R1 §6 has a test.
  **Depends on:** RM-06. **Size:** L.
- **Risks:** R1 §6 and §7 list them; the largest is a write tool exposed over a network, which
  read-only-by-default and `--allow-writes` address.
- **Decisions:** RM-46.D1, build now or keep deferred: **build, off by default** (R1's design is
  done, and the seam makes it cheap to keep out of `mcp_server.py`).
- **Ceiling:** none (RM-06's seam; R1's `brain mcp` sub-app is a new module).
- **Test plan:** R1's test plan, plus a test that every tool registered since R1 is classified read or write in the visibility allowlist.

#### RM-47 — Multi-host use, without bidirectional sync

- **Source:** roadmap 6.4 ("WAL streaming or periodic dump-and-rsync … defer until 6.2 is solid").
- **Smallest design:** **one brain, reached from elsewhere**, which is RM-46, plus a documented
  one-way **read replica** recipe in `docs/guides/second-machine.md`: `brain backup` on the primary,
  copy the archive, `brain restore` on the second machine into its own database, and treat that copy
  as read-only (writes there are lost at the next restore). No code beyond the guide and a `brain
  doctor` line that names a restored replica's source timestamp (`BackupManifest.created_at`, which
  F3's `manifest.json` already records).
- **Rejected:** two-way sync of a Postgres database plus a markdown vault. The vault watcher, the
  graph lane and content-hash dedup all assume one writer; merging two writers' histories is a
  conflict-resolution product, not a feature.
- **Depends on:** RM-46. **Size:** S. **Risks:** a user writes to the replica; the guide and the
  doctor line say it is read-only. **Decisions:** RM-47.D1, bidirectional sync: **never** (above).
- **Goal:** use the brain from a second machine without ever having two writers.
- **Ceiling:** none.
- **Acceptance criteria:** (1) the guide's recipe works end to end on the demo sandbox; (2) the doctor line names the replica's source timestamp.
- **Test plan:** a doctor test against a synthetic manifest.
- **Eval:** none.
- **Security / privacy:** a replica holds the whole corpus; the guide says to keep it on an encrypted disk and to treat the archive like the corpus.

#### RM-48 — Splitting `cli.py` and `mcp_server.py` — **NEEDS USER CONFIRMATION**

- **Source:** BL-2; CLAUDE.md's ceiling table (`cli.py` row: "A split was scoped during the
  GraphRAG build (G0–G4) and deferred deliberately"); the ceiling rules ("splitting a 9,000-line CLI
  to satisfy a number is how you turn a working module into six broken ones").
- **Conflict:** a standing decision against the split. RM-06 already removes the reason to split
  (new code no longer lands in either file).
- **Smallest scope, if the owner confirms:** move **whole existing Typer sub-apps** out of `cli.py`
  (`graphrag`, `vault`, `review`, `enrich`, `backfill`, `eval`) into `cli_<group>.py` modules
  registered at their current call positions through `cli_registry` (so `brain --help` order is
  unchanged), one sub-app per PR; and move whole MCP tool **families** (`brain_graphrag_*`,
  `brain_connect_*`, `brain_review_*`) into `mcp_tools/` modules. Each PR is behaviour-identical,
  proven by a `--help` snapshot and the MCP `list_tools()` snapshot before and after. No function is
  rewritten while it moves.
- **Recommendation:** **do not split.** Keep the seam (RM-06), and extract a cohesive sub-app only
  when a ticket already has to change most of it (the Scout rule).
- **Size if confirmed:** XL (about ten PRs). **Depends on:** RM-06.
- **Risks:** a moved `@app.command` loses its help position or a decorator binds to the wrong
  function (the `brain_recall` incident); the snapshots catch both.
- **Goal (only if confirmed):** smaller, cohesive CLI and MCP modules, with identical behaviour.
- **Design:** the smallest scope above.
- **Acceptance criteria:** the `brain --help` snapshot and the `list_tools()` snapshot are identical before and after each PR, and the full suite passes unchanged.
- **Test plan:** the two snapshots, plus the existing registration guard.
- **Decisions:** RM-48.D1: **recommended, do not split** (§5, U1).
- **Security / privacy:** none.

#### RM-49 — A hermetic CI eval gate — **NEEDS USER CONFIRMATION**

- **Source:** CLAUDE.md "Eval gate (CI)", "Decision recorded (2026-10-12): … the user chose to leave
  the retrieval-quality gate local — there will be no hermetic CI gate"; also the self-hosted runner
  ("probably not worth it").
- **Conflict:** a direct user decision.
- **Smallest scope, if reopened:** a committed synthetic corpus (about 60 documents) and a
  deterministic offline embedder (the `DemoEmbedder` `brain demo` already uses), with 15 synthetic
  queries and judgments, run by `eval.yml` on a hosted runner. It would gate **ranking-code
  regressions** (fusion, filters, recency, the pipeline stages), not retrieval quality on the real
  corpus, which only the local gate can measure.
- **Recommendation:** **keep it closed.** The local gate plus RM-14c/RM-20 measurement already cover
  every ranking change this program makes. Reopen only if a ranking-code regression ever ships
  unnoticed.
- **Size if reopened:** M.
- **Goal (only if reopened):** CI catches ranking-code regressions without the owner's corpus.
- **Design:** the smallest scope above.
- **Ceiling:** none.
- **Acceptance criteria:** a deliberate RRF change on a test branch turns the hermetic job red; the real-corpus gate is unchanged.
- **Test plan:** `tests/test_eval_workflow_contract.py` extended for the new job.
- **Depends on:** nothing.
- **Risks:** the job is read as a quality gate; its name and docs must say "ranking-code regression".
- **Decisions:** RM-49.D1: **recommended, keep closed** (§5, U2).
- **Security / privacy:** synthetic data only.

#### RM-50 — Self-hosted fonts

- **Source:** BL-7; consolidation spec Q14 ("Add it", with a fallback of system stacks and `--measure:
  34rem`).
- **Design:** Q14's default: add `font-src 'self'` to `ui/security.py`'s `CSP`; ship **one** text
  face for the reading page ("Instrument & Page"), an OFL-licensed serif subset to Latin, at most
  about 120 KB of `woff2` in total, under `ui/static/fonts/` with its licence file; declare a
  **separate** `package-data` glob for `static/fonts/*` (setuptools globs do not cross `/`, the
  wheel-packaging lesson); `@font-face` with `font-display: swap` in `tokens.css` or `reading.css`.
  RM-05's test then guards every `url()`.
- **Acceptance:** the wheel contains the font (the packaging test); the CSP test expects the new
  directive and nothing else; no external origin; the reading page uses the face, other chrome keeps
  system stacks. **Depends on:** RM-05. **Size:** S.
- **Risks:** a face choice is taste. **Decisions:** RM-50.D1, which face: **the implementer
  proposes two OFL candidates with screenshots from the demo sandbox; the reviewer picks**; no
  further question to the owner is needed.
- **Goal:** the reading page uses a real text face, delivered from the package, with no external origin.
- **Ceiling:** `ui/security.py` (243 lines) +1.
- **Test plan:** the CSP header test, the packaging glob test (`tests/test_ui_static_assets.py`), RM-05's `url()` test, and a browser row asserting the computed font family on the reading page.
- **Eval:** none.
- **Security / privacy:** the CSP widens by `font-src 'self'` only; no external origin, no `unsafe-inline`.

#### RM-51 — Retiring the tab strip (closed as moot)

- **Source:** BL-8; consolidation spec Q17 ("Three of four tabs are apologies").
- **Design:** no code. After RM-11 and RM-18b, the strip holds Notes, Ingest and Agent, all real;
  Publish was deleted in phase 8/9. The premise of Q17 is gone. This ticket closes BL-8 and Q17 with
  that evidence, in the backlog and in the consolidation spec's §12. Reopen only if the Agent tab is
  dropped (the backlog's own condition).
- **Depends on:** RM-18b. **Size:** XS.
- **Goal:** BL-8 and Q17 are closed, with the evidence recorded.
- **Ceiling:** none.
- **Acceptance criteria:** the backlog and the consolidation spec's §12 record the closure; `tests/test_ui_tabs_static.py` already pins the three tabs.
- **Test plan:** none new.
- **Eval:** none.
- **Risks:** none.
- **Decisions:** RM-51.D1: **close as moot**.
- **Security / privacy:** none.

#### RM-52 — `brain search --pick`

- **Source:** roadmap 7.1 ("an fzf-style picker"). `brain ui` meets most of the need; this is the
  terminal half, kept small.
- **Design:** `--pick` on `brain search`: when stdout is a TTY and `fzf` is on `PATH`, the results go
  to `fzf` with a preview command (`brain show {id} --brief`); the chosen document's id is printed
  (scriptable) and a `clicked` interaction is recorded with the query and the search's session id
  (source `cli`), which feeds RM-22. Without `fzf`: exit 1 with an install hint. Logic in a leaf
  `src/brain/search_pick.py`; `cli_search.py` budget ≤ 12.
- **Acceptance:** a fake `fzf` runner selects the second result; the interaction row is written; no
  TTY → refused with a message. **Depends on:** RM-09. **Size:** XS.
- **Decisions:** RM-52.D1, picker on by default for a TTY: **no**; it would change scripted output.
- **Goal:** pick a search result interactively in the terminal, and record the pick.
- **Ceiling:** the `cli_search.py` budget (§2.2).
- **Test plan:** the acceptance cases with an injected runner.
- **Eval:** none.
- **Risks:** `fzf` flag differences across versions; only long-stable flags are used.
- **Security / privacy:** document text is never interpolated into a shell command; only ids reach the preview command.

#### RM-53 — `brain examples`

- **Source:** roadmap 7.2.
- **Design:** `brain examples [TOPIC]` prints curated, grouped invocations from a packaged resource
  `src/brain/resources/examples.md` (a `package-data` glob of its own). Topics: search, ingest,
  graph, brief and review, maintenance. A test parses every `brain …` line in the resource and
  asserts the command path exists in the Typer app, so the examples cannot rot silently. No real
  names, queries or titles: every example is synthetic.
- **Depends on:** RM-06. **Size:** XS.
- **Goal:** find the right command by example.
- **Ceiling:** none.
- **Acceptance criteria:** (1) each topic prints; (2) the rot test fails when an example names a command that does not exist.
- **Eval:** none.
- **Risks:** none.
- **Decisions:** RM-53.D1, curated resource over generated text: **curated** (shorter and grouped by task).
- **Security / privacy:** synthetic examples only (rule 15).

#### RM-54 — Opt-in query rewriting (`--rewrite`)

- **Source:** roadmap 7.5 ("default off on CLI, default off on MCP; damages exact-name lookups").
- **Design:** `--rewrite` on `brain search` and `brain explain` only. One `chat.chat_json` call (a
  JSON schema of up to two paraphrases, `num_predict` capped) produces paraphrases; the pipeline
  runs the original and each paraphrase, and fuses the document rankings with
  `rank_fusion.rrf_contribution`, the original weighted 1.0 and paraphrases 0.5. `explain` shows,
  per result, which query found it. Chat down → the original search alone, plus a one-line notice.
  Logic in a leaf `src/brain/query_rewrite.py`; `cli_search.py` budget ≤ 12. No MCP parameter
  (agents paraphrase for themselves).
- **Eval:** measured once, both ways, and recorded in the PR; **never adopted as a default** (the
  roadmap's own ruling). `ci.json` unchanged.
- **Depends on:** RM-09. **Size:** S.
- **Risks:** latency (one chat call plus two extra searches); it is opt-in per run.
- **Goal:** a vague query can opt into paraphrase retrieval, one run at a time.
- **Ceiling:** the `cli_search.py` budget (§2.2).
- **Acceptance criteria:** (1) off unless `--rewrite`; (2) with a fake chat, paraphrase hits fuse as specified; (3) chat down → the original search plus a notice; (4) `explain` names the query that found each result.
- **Test plan:** pure fusion tests and fake-chat surface tests.
- **Decisions:** RM-54.D1, an MCP parameter: **no**.
- **Security / privacy:** the query goes only to the local chat model.

#### RM-55 — Summarize a tag (`brain tags summarize`, MCP `brain_tags_summarize`)

- **Source:** BL-11, the MCP server spec's "Long-form `brain_summarize_tag(tag)` tool". The spec's
  other three out-of-scope items are covered elsewhere: HTTP transport (RM-46), reranking (RM-14),
  and per-document permission scoping (F6, Appendix A).
- **Design:** a new leaf `src/brain/tag_summary.py` takes the N most recent documents carrying the
  tag (default 20), their `documents.summary` values (titles where a summary is missing), and asks
  `chat.chat_json` for a short synthesis with `[N]` citations, sanitized the way `ask` sanitizes
  answers. CLI `brain tags summarize TAG` (on RM-21's `tags` sub-app); MCP `brain_tags_summarize(tag,
  include_confidential=False)` in `mcp_tools/tags.py`. Summaries are body-derived, so the MCP lens
  excludes confidential documents from the input, not just from the output.
- **Depends on:** RM-21. **Size:** S.
- **Goal:** a short, cited synthesis of what one tag's documents say.
- **Ceiling:** none.
- **Acceptance criteria:** (1) the synthesis cites only in-range documents; (2) MCP excludes confidential inputs; (3) Ollama down → RM-02's clean message.
- **Test plan:** fake-chat tests on the CLI and MCP paths.
- **Eval:** none.
- **Risks:** a very large tag; N caps the input.
- **Decisions:** RM-55.D1, N: **20**.
- **Security / privacy:** the MCP lens applies to the input documents.

#### RM-56 to RM-60 — Trigger-gated items

Each is recorded with its trigger, the command that shows whether the trigger holds, and the design
to build when it does. They are **not built** until then; that is the recommended default, and it
follows the condition each source attached. The tracker marks them *trigger-gated*.

| Ticket | Item (source) | Trigger | How to check | Design when triggered | Size |
|---|---|---|---|---|---|
| RM-56 | MCP twin of `brain eval` (Q1-B "MCP") | The owner starts asking agents to run evals in chat | A request to that effect | `brain_eval_diff(baseline="ci")`, read-only, never records, returns the three mean deltas and the changed config keys; in `mcp_tools/eval.py` | S |
| RM-57 | `--tier vault\|ingested` filter (Q1-C D11) | A request to filter by tier | A request | `build_predicate` gains `tier` → `d.kind = %s`; CLI, MCP and the UI's filter row; recorded `search.py`/`mcp_server.py` growth | XS |
| RM-58 | Pruning `interactions` (Q1-C non-goals) | The table passes 1,000,000 rows or 500 MB | `brain usage engagement` (RM-22a prints the row count and table size) | `brain usage prune --older-than 2y`, dry-run by default, never deletes ratings or pins | S |
| RM-59 | Cap on the auto-tag vocabulary prompt (Q1-D D18) | The vocabulary sent by `brain tag --auto` outgrows D18's threshold | `brain tags list --json` (RM-21) gives the vocabulary size | Send the top-K tags by frequency plus tags co-occurring with the document's nearest neighbours | S |
| RM-60 | `documents.parent_document_id` (Q1-D D17) | A feature needs parent/child documents | A ticket that needs it (RM-45 deliberately does not) | Migration 033 (reserved): nullable self-reference with `ON DELETE SET NULL` and an index | S |

**Fields shared by RM-56 to RM-60.** **Ceiling:** none beyond the growth each row names, recorded
when built. **Acceptance criteria and test plan:** written in the ticket's plan when its trigger
fires, following §2.1. **Depends on:** RM-58 on RM-22a, RM-59 on RM-21, the rest on nothing.
**Risks:** building them early adds surface nobody uses. **Decisions:** wait for the trigger.
**Security / privacy:** RM-56 is read-only; the others are local.

#### RM-61 — A background enrichment daemon (closed: not needed)

- **Source:** Q1-D non-goal; backlog §1.8 ("NOT STARTED, and not needed").
- **Ruling:** close it. The post-ingest hook writes summaries, and `brain enrich --backfill` repairs
  the tail. The one gap a daemon would close, summaries lost while Ollama was down, is small and
  visible in `brain status`. If it ever matters, the cheapest fix is one line in the brief daemon
  script (`brain enrich --backfill --limit 50`), not a new daemon. **Size:** XS (the closure note).

- **Acceptance criteria:** the backlog row is closed with this ruling.
- **Risks:** none. **Decisions:** RM-61.D1: **close**. **Security / privacy:** none.

---

## 4. Execution plan

### 4.1 Waves, and why in this order

| Wave | Tickets | Why here |
|---|---|---|
| 1 | RM-01, RM-02, RM-03, RM-04, RM-05 | Backlog ranks 1a, 2, 3. The fallback decision is fresh; the guards are tiny and close recorded holes. |
| 2 | RM-06, RM-07, RM-08, RM-09 | Inserted ahead of rank 4 (argued at the wave's head): every later wave lands in leaves instead of over-ceiling files. RM-09 needs Wave 1's three surfaces settled. |
| 3 | RM-10, RM-11, RM-12, RM-13 | Ranks 4, 5, 6. The only feature already designed; it delivers the job registry the Agent tab needs. RM-12 is pure parity. |
| 4 | RM-14a, RM-14b, RM-14c, RM-15, RM-16, RM-17, RM-18a, RM-18b, RM-18c | Ranks 7–10. The reranker is measurable now; saved searches and delivery are small and independent; the Agent tab builds on Wave 3. |
| 5 | RM-19, RM-20, RM-21, RM-22a, RM-22b, RM-23, RM-24 | Ranks 11–15 and 17. RM-20 and RM-22b are measured on top of RM-14c's outcome. RM-22b waits for its data gate. |
| 6 | RM-25, RM-26, RM-27, RM-28, RM-29 | Rank 16, scheduled rather than opportunistic, because the owner asked for the whole backlog. RM-25 needs RM-19's shingles. |
| 7 | RM-30 … RM-40 | Rank 18. Shared machinery first (OCR, the drop-folder runner). |
| 8 | RM-41 … RM-45 | Rank 19. Heaviest privacy load, least evidence of need. |
| 9 | RM-46, RM-47, RM-50, RM-51, RM-52, RM-53, RM-54, RM-55, RM-61; RM-48 and RM-49 only if confirmed; RM-56–RM-60 on trigger | Rank 20 and the "leave deferred" row. |

Within a wave, tickets go in the order listed unless §4.2 allows them to run side by side.

### 4.2 What may run in parallel, and what must serialize

Parallel tickets each get their own worktree (memory note on parallel agents), and broad test suites
run serially. Two tickets are **disjoint** when they share no file below. A shared file forces
merge order, not necessarily serial work: the second ticket rebases after the first merges.

| Shared file or resource | Tickets that touch it | Rule |
|---|---|---|
| `mcp_server.py` (and its CLAUDE.md row) | RM-01, RM-02, RM-06, RM-09, RM-24, RM-48 | Merge one at a time; each promotes the row's last hop before adding its own. |
| `cli.py` (and its row) | RM-02, RM-06, RM-16, RM-21, RM-48 | Same. |
| `ingest/__init__.py` (and its row) | RM-07, RM-42 | Same. |
| `search.py` (and its row) | RM-20, RM-24, RM-57 | Same. |
| `cli_ingest.py` (and its row) | RM-07, RM-37, RM-45 | Same. |
| `vault/watch.py` (and its row) | RM-26 | — |
| `cli_search.py` budget (§2.2) | RM-12, RM-14a, RM-24, RM-52, RM-54 | Each checks the remaining budget at rebase. |
| `search_pipeline.py` | RM-09, RM-14a, RM-20, RM-22b, RM-54 | Serial: each adds a stage to `build_stages`. |
| `source_kinds.py` | RM-30, RM-31, RM-33, RM-34, RM-35, RM-36, RM-38, RM-41, RM-43, RM-44 | One-line additions; rebase conflicts are trivial, but merge one at a time. |
| `ingest/dispatch.py` registrations | RM-32, RM-33, RM-38 | Merge one at a time. |
| `brief.py` | RM-15, RM-17, RM-19 | Merge one at a time. |
| `ask.py` | RM-02, RM-18a, RM-18c | Serial (each depends on the previous). |
| `ui/static/index.html`, `ci.yml` browser roster | RM-03, RM-11, RM-13, RM-18b, RM-23, RM-40 | Merge one at a time. |
| Migrations | RM-15 (029), RM-19 (030), RM-20 (031), RM-22b (032), RM-60 (033) | Numbers are fixed by §2.8, so these may be developed in parallel; they merge in number order. |
| The live machine (eval runs, daemon reinstall) | RM-14c, RM-20, RM-22b, RM-43 eval steps; every `src/` merge's reinstall | One at a time, by the team lead, never during another ticket's run of record. |

**Safe parallel sets** (disjoint by the table): Wave 1: {RM-01, RM-03, RM-04, RM-05} then RM-02.
Wave 2: {RM-06, RM-07, RM-08} then RM-09. Wave 3: RM-12 beside RM-10/RM-11. Wave 4: {RM-14a, RM-15,
RM-17} then {RM-16, RM-18a}. Wave 5: {RM-19, RM-21, RM-23} then {RM-20, RM-24, RM-22a}. Wave 6:
{RM-27, RM-28, RM-29} beside RM-25 → RM-26. Wave 7: {RM-30, RM-31, RM-34} then RM-32 → RM-33 →
{RM-36, RM-38, RM-39, RM-35}, then RM-37, RM-40.

### 4.3 Migration allocation

§2.8 is authoritative: 029 (RM-15), 030 (RM-19), 031 (RM-20), 032 reserved (RM-22b), 033 reserved
(RM-60). Anything else takes the next free number after 033 and updates §2.8 in its PR.

---

## 5. Open questions — the NEEDS USER CONFIRMATION set

Only tickets that would change a standing user decision are here. Every other decision in this
document carries a recommended default and needs no answer.

| # | Ticket | Standing decision it would change | Recommended answer |
|---|---|---|---|
| U1 | RM-48: split `cli.py` / `mcp_server.py` | The split was "deferred deliberately" (CLAUDE.md, ceiling table), and the ceiling rules forbid splitting to satisfy a number. | **Do not split.** RM-06's seam stops the growth; extract a sub-app only when a ticket already changes most of it. |
| U2 | RM-49: a hermetic CI eval gate | The user chose on 2026-10-12 to keep the retrieval-quality gate local, with no hermetic CI gate. | **Keep it closed.** The local gate covers every ranking change here. |

Two choices are **not** questions, because the owner already decided them on 2026-05-11 and this
document follows them: browser history is built, allowlist-only (RM-41), and the trackers are Notion
and Jira (RM-34, RM-35). Brief delivery (RM-16) and the MCP HTTP transport (RM-46) send content off
the machine only when the owner turns them on; both default off.

---

## Appendix A — Already done (not ticketed)

| Item | Evidence |
|---|---|
| 1.1 Eval harness, `--fail-below`, committed `ci.json` | `src/brain/eval/`, `brain eval`, `9ae9580`; all 31 rows re-judged (PRs #41, #50). |
| 1.2 `brain explain` | `cli_search.explain`, `SearchExplanation`, `e944972`. (The MCP twin is RM-12.) |
| 1.3 Interactions table (phase 1) | Migration 010, `brain rate`, MCP `brain_rate`, UI opens (`ui/telemetry.py`, migration 024). |
| 1.4 Metadata filters | `resolve_person_to_keys` and the `hybrid_search` filter kwargs (`63857ce`, `c7b726f`, `6c2f6eb`); updated-range filters (migration 025). |
| 1.5 Recency boost; 1.6 snippet context | `e9964a8`; `snippet_context.py`. |
| 2.2 Email drafts | `8c9a042`. |
| 2.7 Krisp action items | `brain enrich --krisp-action-items`, `brain todo` (`3ed57d0`). |
| 3.1 Auto-summary; 3.2 auto-tags | Migration 011, `OllamaEnricher`, `brain tag --auto`. |
| 3.3 Entity extraction (reshaped) | GraphRAG on AGE (`15a69e1`). (The `--entity` leg is RM-24.) |
| 3.7 Contradiction / change detection | `brain review scan …` (`9762a26`, `e1950b9`). |
| 4.1 Daily / weekly digest (reshaped) | `brain brief`, the `com.brain.brief` launchd job, `brain review weekly`. (Delivery is RM-16; new entities RM-17.) |
| 4.2 Proactive link suggestions (reshaped) | `brain connect` (`9c985ad`). |
| 4.3 `brain ask` | `ask.py`, `brain ask`, `brain_ask` (`ac2a5cf`, `6275165`). |
| 6.1 MCP write parity, except `brain_related` | `brain_rm`, `brain_timeline`, `brain_rate`, `brain_tag`, `brain_edit`, `brain_note_*`. |
| 6.2 Backup + restore | `brain backup` / `brain restore` (F3). |
| 6.3 Self-telemetry (reshaped) | `brain usage` (F7), migrations 024 and 027. |
| H-7 Text-only search fallback | PR #61, merged at `02c2abc`: `search_fallback.py`, `brain search`, `brain explain`, MCP `brain_search`. |
| Per-document permission scoping (MCP spec "Out of scope") | Met for a single-user product by F6 sensitivity (migration 026) and every MCP tool's confidential lens. |
| Superseded: the wiki click endpoint, the Q1 manual-QA page, the Publish tab, the Quartz summary lede, consolidation Q6 | The wiki was deleted at R3 (PR #43); `brain ui` logs opens and renders the lede; the Publish tab was removed in phase 8/9. |

## Appendix B — Ticket tracker

Every ticket starts at `TODO`. A ticket PR moves its own row to `IN PROGRESS` and then `DONE
(#PR)`. A title marked *NEEDS USER CONFIRMATION* waits for §5's answer; *trigger-gated* waits for
the trigger in its ticket; a *closure note* ticket is done when its PR records the closure.

| Id | Title | Size | Wave | Depends on | Status |
|---|---|---|---|---|---|
| RM-01 | `brain recall` text-only fallback | S | 1 | — | TODO |
| RM-02 | `brain ask` fallback and clean errors | S | 1 | — | TODO |
| RM-03 | `brain ui` search fallback notice | S | 1 | — | TODO |
| RM-04 | Ceiling-table CI check | S | 1 | — | TODO |
| RM-05 | CSS `url()` offline test | XS | 1 | — | TODO |
| RM-06 | CLI and MCP registration seams | S | 2 | — | TODO |
| RM-07 | Extractor registry, one kind definition, BL-12 | S | 2 | — | TODO |
| RM-08 | Feature settings leaves | XS | 2 | — | TODO |
| RM-09 | One search pipeline | S | 2 | RM-01, RM-02, RM-03 | TODO |
| RM-10 | Ingest tab phase 1: jobs + paste backend | M | 3 | RM-07 | TODO |
| RM-11 | Ingest tab phase 2: paste front end | M | 3 | RM-10 | TODO |
| RM-12 | MCP `brain_related` + `brain_explain` | S | 3 | RM-06, RM-09 | TODO |
| RM-13 | Ingest tab phase 3: file upload | M | 3 | RM-11 | TODO |
| RM-14a | Reranker stage, backends, eval wiring (off) | M | 4 | RM-08, RM-09 | TODO |
| RM-14b | Local reranker runtime | S | 4 | RM-14a | TODO |
| RM-14c | Reranker measurement and adoption | S | 4 | RM-14b | TODO |
| RM-15 | Saved searches and watch lists | M | 4 | RM-06, RM-09 | TODO |
| RM-16 | Brief delivery (email to self / Slack webhook) | S | 4 | RM-08 | TODO |
| RM-17 | Brief "new this week" entities | S | 4 | — | TODO |
| RM-18a | Agent tab backend | M | 4 | RM-10, RM-02 | TODO |
| RM-18b | Agent tab front end and output hardening | M | 4 | RM-18a, RM-11 | TODO |
| RM-18c | Agent tab follow-up questions | S | 4 | RM-18b | TODO |
| RM-19 | Near-duplicate detection | M | 5 | RM-06 | TODO |
| RM-20 | Document-level embedding stage (off) | M | 5 | RM-09, RM-14c | TODO |
| RM-21 | Tag rename, merge, suggest-merges | S | 5 | RM-06 | TODO |
| RM-22a | Engagement readout and gate | S | 5 | — | TODO |
| RM-22b | Engagement ranking stage (off; built when RM-22a's gate is met) | M | 5 | RM-22a, RM-09 | TODO |
| RM-23 | Copy-path affordance | XS | 5 | — | TODO |
| RM-24 | `--entity` search filter | S | 5 | RM-09 | TODO |
| RM-25 | H-1 move identity | S | 6 | RM-19 | TODO |
| RM-26 | H-2 + H-4 owed watcher work | S | 6 | — | TODO |
| RM-27 | H-3 graph-lock residuals | S | 6 | — | TODO |
| RM-28 | H-5 log-privacy guard | S | 6 | — | TODO |
| RM-29 | H-6 class-only error reports | XS | 6 | — | TODO |
| RM-30 | Calendar events | M | 7 | RM-06, RM-07 | TODO |
| RM-31 | Code-host activity | M | 7 | RM-06, RM-07 | TODO |
| RM-32 | OCR and image-only PDFs | M | 7 | RM-07, RM-08 | TODO |
| RM-33 | Images and the drop-folder runner | M | 7 | RM-32 | TODO |
| RM-34 | Jira issues | S | 7 | RM-07 | TODO |
| RM-35 | Notion export | M | 7 | RM-06, RM-07 | TODO |
| RM-36 | Apple Notes | M | 7 | RM-06, RM-07 | TODO |
| RM-37 | External vault sync with deletions | M | 7 | RM-07 | TODO |
| RM-38 | Voice memos | M | 7 | RM-33 | TODO |
| RM-39 | Mobile share-sheet capture | S | 7 | RM-33 | TODO |
| RM-40 | Ingest tab phase 4: inbox folder | S | 7 | RM-13, RM-33 | TODO |
| RM-41 | Browser history and bookmarks | L | 8 | RM-06, RM-07 | TODO |
| RM-42 | Code-aware chunker | M | 8 | RM-07 | TODO |
| RM-43 | Code-repository ingestion | M | 8 | RM-42 | TODO |
| RM-44 | Social bookmarks | S | 8 | RM-07 | TODO |
| RM-45 | Receipts (Gmail attachments) | M | 8 | RM-32 | TODO |
| RM-46 | MCP over HTTP | L | 9 | RM-06 | TODO |
| RM-47 | Multi-host via one brain + read replica | S | 9 | RM-46 | TODO |
| RM-48 | Split `cli.py` / `mcp_server.py` (NEEDS USER CONFIRMATION) | XL | 9 | RM-06 | TODO |
| RM-49 | Hermetic CI eval gate (NEEDS USER CONFIRMATION) | M | 9 | — | TODO |
| RM-50 | Self-hosted fonts | S | 9 | RM-05 | TODO |
| RM-51 | Retire the tab strip (closure note, no code) | XS | 9 | RM-18b | TODO |
| RM-52 | `brain search --pick` | XS | 9 | RM-09 | TODO |
| RM-53 | `brain examples` | XS | 9 | RM-06 | TODO |
| RM-54 | Opt-in `--rewrite` | S | 9 | RM-09 | TODO |
| RM-55 | Summarize a tag | S | 9 | RM-21 | TODO |
| RM-56 | MCP twin of `brain eval` (trigger-gated) | S | 9 | — | TODO |
| RM-57 | `--tier` filter (trigger-gated) | XS | 9 | — | TODO |
| RM-58 | Prune `interactions` (trigger-gated) | S | 9 | RM-22a | TODO |
| RM-59 | Auto-tag vocabulary cap (trigger-gated) | S | 9 | RM-21 | TODO |
| RM-60 | `documents.parent_document_id` (trigger-gated) | S | 9 | — | TODO |
| RM-61 | Background enrichment daemon (closure note, no code) | XS | 9 | — | TODO |
