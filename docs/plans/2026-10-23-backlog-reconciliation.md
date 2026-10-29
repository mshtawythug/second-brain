# Backlog reconciliation — the May 2026 roadmap against `master`, plus every other open deferral

**Date:** 2026-10-23. **Measured at:** `second-brain/master` `0bf3e36` (PR #59).
**Supersedes:** the execution tracker `docs/plans/2026-05-11-improvement-roadmap-EXEC.md` and the
status notes in its parent `2026-05-11-improvement-roadmap.md` and the wave plans
`2026-05-11-roadmap-wave-q1{a,b,c,d}.md` / `-q2-summary-wiki.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

## 0. Read this first

**The roadmap set was never committed.** All seven `2026-05-11-*roadmap*` files exist only as
untracked files in the main checkout; `git log --all -- docs/plans/2026-05-11-improvement-roadmap-EXEC.md`
returns nothing. They quote real names in their example queries, which is a good reason to keep them
out of a public repository (CLAUDE.md rule 15). This document therefore restates each item by its
number and a generic name, so it stands alone, and it does not copy the roadmap's examples.

**The supersession note for the EXEC file is not in this PR, on purpose.** That file exists only in
the main checkout, untracked, so the note has to be added there by hand. Committing the file here
would make it tracked, and the main checkout's untracked copy would then block its next
fast-forward ("untracked working tree file would be overwritten"). The text to prepend under its H1:

> **SUPERSEDED 2026-10-23.** This tracker stopped being updated in May 2026, and its PENDING rows
> do not reflect `master`. Much of it shipped under other names (`brain brief`, `brain connect`,
> `brain ask`, `brain backup`, `brain capture`, `brain usage`, GraphRAG). The current status and the
> ranked open backlog are in `docs/plans/2026-10-23-backlog-reconciliation.md`. The history below
> is left as written.

**Method.** Each status was decided from the code on `master`. That meant reading the module, finding
the registered CLI command (`cli.py`, `cli_registry.py` and each `register_*`), finding the MCP
tool (`grep -n '^def brain_' src/brain/mcp_server.py`), and checking the migrations. A keyword hit
was never enough. Example: `calendar` matches `src/brain/vault/derived_links/gws.py`, which refreshes
the *people directory* from calendar attendees. That is not item 2.3, which wants calendar events
ingested as documents. Commits were found with `git log --reverse -S '<symbol>' -- <path>`. Where a
feature reached `master` inside the v0.3.0 squash (`e084c79`), that commit is cited rather than a PR
number, because there is no finer one.

**Status vocabulary.**

| Status | Meaning |
|---|---|
| **SHIPPED** | On `master`, reachable from a CLI command or MCP tool, with tests. |
| **SHIPPED (reshaped)** | The need is met, under a different name or design from the roadmap's. |
| **PARTIAL** | Some of it exists. The row says what is missing. |
| **NOT STARTED** | No code. Verified by grep and by reading the obvious homes. |
| **SUPERSEDED** | No longer wanted as written. The row cites the decision. |
| **IN FLIGHT** | Decided and implemented in an open PR, named in the row, which is not on `master` at `0bf3e36`. The row stays true after that PR merges. Not duplicated here. |

**Ids.** `H-n` rows are handoff follow-ups (§2.2). `BL-n` rows are the other recorded deferrals
(§2.3). Numbers like 3.4 are the roadmap's own section ids, and "rank N" means a row of §3.

The prefix was chosen to be free in every document this backlog cites. Earlier drafts collided with
three other series:
- `B-n` is the consolidation spec's `B-1`…`B-26` defect record (its Appendix B);
- `R-n` sat beside the wiki-deletion state `R3` and the Ingest spec's risks `R1`–`R9`;
- `D-n` would sit beside the Ingest spec's decisions `D1`–`D10` and the consolidation spec's defects
  `D1`–`D8`.

`grep -rnoE '\bBL-?[0-9]+\b' docs/ CLAUDE.md` found no hit before this document.

## 1. Roadmap status, item by item

### 1.1 Section 1 — foundation (Q1)

| Id | Item | Status | Evidence |
|---|---|---|---|
| 1.1 | Retrieval eval harness (golden corpus, nDCG@5 / MRR / Recall@20) | **SHIPPED** | `src/brain/eval/`, `brain eval` (`cli.py`, `@app.command("eval")`), `9ae9580`. The `--fail-below` gate (exit 3) and the committed `tests/eval/baselines/ci.json` followed later. All 31 golden rows were re-judged by 2026-10-22 (PRs #41, #50). By decision (CLAUDE.md "Eval gate", 2026-10-12), the quality gate stays **local** and CI checks harness health only. |
| 1.2 | `brain explain` ranking diagnostic | **SHIPPED** | `cli_search.py` `register` → `explain`, `SearchExplanation` in `search.py`, `e944972`. The MCP twin was deferred by wave-Q1b decision D13. It is still open: see BL-6. |
| 1.3 | Interactions table (feedback signals) | **SHIPPED**. The wiki click hook is **SUPERSEDED**. | Migration `010_interactions.sql` (`7de7f68`), `brain rate` (`c7b726f`), MCP `brain_rate` (`48d77f5`), `brain_show(originating_query, session_id)`. The planned wiki click endpoint never shipped and the wiki was deleted at R3 (PR #43). `brain ui` writes `source='ui'` opens instead (`ui/telemetry.py`, migration 024). **Phase 2 is NOT STARTED:** no ranking code reads `interactions` (`grep interactions src/brain/search*.py` finds nothing). `brief.py` reads `pinned` for display only. See §3, rank 14. |
| 1.4 | Metadata-aware query filters (`--person`, `--after`/`--before`, `--kind`, `--thread`, `--draft`, tag in/out) | **SHIPPED** | `resolve_person_to_keys` + the filter kwargs on `hybrid_search` (`63857ce`), CLI + MCP flags (`c7b726f`). Name-variant matching was added in Wave A.2 (`6c2f6eb`). Updated-range filters were added later by F9 (migration 025). |
| 1.5 | Recency boost | **SHIPPED** | `recency_halflife_days` in `search.py`, `e9964a8`. |
| 1.6 | Per-chunk context window in snippets | **SHIPPED** | `snippet_context_tokens` (`e9964a8`), since extracted to `snippet_context.py`. |
| Q1-CLOSEOUT | Cross-wave review of the Q1 diff, plus a manual-QA page | **SHIPPED** for the review. The QA page is **SUPERSEDED**. | CLAUDE.md "Wave A.1 source" names the cross-wave audit (`docs/audits/2026-05-14-q1-codex-cumulative-review.md`, local and ignored) and its remediation waves. A.1 is the eval workflow; A.2 is the person variants (`6c2f6eb`); A.4 is the committed `ci.json`. **A.3 ("EXEC tracker reconciliation") is this document.** The remediation plan it names is in neither checkout, so A.3's original wording could not be compared. The QA page would have targeted the wiki, which was deleted. |

### 1.2 Section 2 — source breadth

| Id | Item | Status | Evidence |
|---|---|---|---|
| 2.1 | Ingest the owner's own code-host activity (PRs, issues, repo READMEs) | **NOT STARTED** | There is no extractor. `VALID_SOURCE_KINDS` is `{manual, krisp, gmail, slack}` (`src/brain/source_kinds.py:43`). Re-grepping `src/brain` for the code host's name finds only unrelated hits. One is a secret-detection pattern (`secret_patterns.py`). One is the AGE Dockerfile (`templates/docker/age/Dockerfile`: a clone URL, plus a comment naming a workflow). The other four are comments: `cli.py:388`, `ui/render.py:114`, `ui/static/css/graph.css:57` and `ui/static/js/graph.js:84`. |
| 2.2 | Email drafts | **SHIPPED** | `8c9a042`: drafts are included, quarantined by `documents.draft`, and guarded against partial windows. |
| 2.3 | Calendar events as documents | **NOT STARTED** | `refresh_calendar` (`vault/derived_links/gws.py`) feeds the **people directory** only, and runs after a Krisp ingest. No event is ever stored as a document, and there is no `calendar` source kind. |
| 2.4 | External markdown vaults / Apple Notes | **PARTIAL** | `brain ingest-dir <dir>` (`cli_ingest.py`) ingests a directory of `.md` / `.txt` / `.pdf` / `.docx` files as `manual`. That covers a one-off import of another markdown vault. Missing: Apple Notes export, and any re-sync or ownership model for an external vault (re-runs dedup by `source_path`, but deletions are not tracked). |
| 2.5 | Browser history and bookmarks (domain allowlist) | **NOT STARTED** | No code. The owner accepted the item on 2026-05-11 (EXEC "Resolved decisions"); it was never scheduled. |
| 2.6 | Code-repo ingestion | **NOT STARTED** | No `code` source kind and no extractor. Depends on 7.3. |
| 2.7 | Krisp action items as structured documents | **SHIPPED** | `content_type='krisp_action_items'`, `brain enrich --krisp-action-items` (deferred fetch), `brain todo` (`3ed57d0`). |
| 2.8 | Notion / Jira extractors | **NOT STARTED** | No extractor. Exports can be imported as `manual` files through `ingest-dir`, which is what a document-set import in October did (CLAUDE.md eval notes). That is a file import, not an extractor. |
| 2.9 | Long tail: social bookmarks, receipts, voice memos | **NOT STARTED** | Deliberately left for later in the roadmap itself ("defer until 2.1–2.7 land"). For voice memos, see 5.2. |

### 1.3 Section 3 — LLM enrichment

| Id | Item | Status | Evidence |
|---|---|---|---|
| 3.1 | Per-document auto-summary | **SHIPPED** | Migration 011, `brain.enrichment.OllamaEnricher`, the post-ingest hook, `brain enrich --backfill` (`6bdabfb`, `213d5ee`, `3ed57d0`). It uses local Ollama, not a hosted model (EXEC decision). The summary later reached the MCP surface as the `brief` projection (`brain_search(brief=True)`), which closed wave-Q1d deferral D16. |
| 3.2 | Auto-tags | **SHIPPED** | `brain tag <id> --auto [--accept-all]` (`3ed57d0`). |
| 3.3 | Entity extraction (people, projects, organisations) | **SHIPPED (reshaped) / PARTIAL** | Built as GraphRAG on Apache AGE (`15a69e1`, G0–G4; `src/brain/graph_rag/`). People and concepts are stored in `graph_entities` / `graph_entity_mentions`. Surfaces: `brain graphrag entity/entities/themes/search`, `brain timeline` (Plan 05), and the `brain_graphrag_*` MCP tools. **Missing:** an `--entity` filter on `brain search` / `brain_search`, which was the roadmap's "filter by entity" leg. |
| 3.4 | Near-duplicate detection | **NOT STARTED** | No MinHash or near-dup code. Only mitigations exist: Gmail thread assembly collapses per-message near-dups (`ingest/gmail.py:318`), and content-hash dedup catches exact copies. See §3, rank 11. |
| 3.5 | PDF image OCR | **NOT STARTED** | No OCR code. An image-only PDF ingests as an empty document (`document_id=None`, "skipped (empty document)" in `cli_ingest._ingest_outcome_verb`). |
| 3.6 | Document-level embeddings | **NOT STARTED** | There is no `documents.embedding` column. The two non-chunk vector columns are `graph_entities.embedding` (`embedding_targets.py:20`) and `graph_communities.summary_embedding` (`:21`). Neither is per-document. Wave Q1-D reserved `documents.summary` for this. |
| 3.7 | Contradiction / change detection | **SHIPPED** | `brain review scan / list / dismiss / snooze / resolve` and `brain_review_scan` / `brain_review_findings_list` (Plan 03, `9762a26`, `e1950b9`). They detect contradiction and staleness over entities, as the roadmap required. |

### 1.4 Section 4 — active brain

| Id | Item | Status | Evidence |
|---|---|---|---|
| 4.1 | Daily / weekly digest | **SHIPPED (reshaped) / PARTIAL** | `brain brief` (Plan 01, `b063c89`) plus MCP `brain_brief`, run daily by the `com.brain.brief` launchd job (`src/brain/templates/bin/_brain-brief-fg.sh` runs `brain brief --wiki`, which writes `<vault>/daily/…-brief.md`). The weekly half is `brain review weekly` / `brain_review_weekly`. **Missing:** Slack and email delivery, and "new entities this week". |
| 4.2 | Proactive link suggestions | **SHIPPED (reshaped)** | `brain connect list / refresh / accept / reject / stats` and `brain_connect_*` (Plan 07, `9c985ad`). They score by entity-graph affinity plus embedding affinity, and write a `## See Also` section back to the vault. |
| 4.3 | `brain ask`, a cited RAG agent | **SHIPPED** | `ask.py` (plan → retrieve → reflect → synthesize), `brain ask`, `brain_ask` (Plan 06, `ac2a5cf`, `6275165`), and `brain audio` (Plan 04) for spoken overviews. No reranker step exists yet, because 5 is not started. |
| 4.4 | Watch lists / saved searches with notification | **NOT STARTED** | It became feature F13 and was cut from the 2026-07-25 release ("Deferred from this release", `docs/specs/2026-07-25-agent-memory-safety-ui-design.md` §2). Migration 028 went to `search_query_tokens`, not to saved searches. See BL-9. |
| 4.5 | Mobile capture | **PARTIAL** | Capture exists: `brain capture` + the inbox (`_capture_command.py`, Plan 09, `78310bd`), MCP `brain_capture` (`86d1816`), and the Claude Code Stop hook (`brain claude capture-hook`, F1). A phone can capture through `brain ui --lan` (token-guarded, `ui/lan.py`) and `+ New`. **Missing:** a share-sheet hook, which is the roadmap's actual ask. |

### 1.5 Section 5 — reranker and multi-modal

The EXEC tracker uses "5" for the cross-encoder reranker (top-5 item #5, wave Q3-A). The roadmap's
Section 5 heading is multi-modal (5.1–5.3). Both are listed.

| Id | Item | Status | Evidence |
|---|---|---|---|
| 5 | Cross-encoder reranker (pluggable `Reranker` Protocol) | **NOT STARTED** | `SearchExplanation.reranker_score` is still a placeholder: `search.py:104`, `= None  # Q3-A will populate; today always None`. The prerequisite the roadmap set, an eval to prove it against, now exists and is freshly re-judged. See BL-5. |
| 5.1 | Screenshot watch folder + OCR | **NOT STARTED** | No code (the only `screenshot` hits are a demo-corpus manifest and an extractor stopword). |
| 5.2 | Voice-memo ingestion (local transcription) | **NOT STARTED** | No transcription code. `brain audio` goes the other way: it *generates* a two-host script from graph themes, plus optional TTS. |
| 5.3 | PDF image OCR | (duplicate) | Same item as 3.5. |

### 1.6 Section 6 — operations

| Id | Item | Status | Evidence |
|---|---|---|---|
| 6.1 | MCP write parity: remove, related, timeline, rate | **PARTIAL** | `brain_rm`, `brain_timeline`, `brain_rate`, `brain_tag`, `brain_edit`, `brain_note_new/rename/move` and the connect writers all exist. **Missing: `brain_related`.** Related documents are computed by `brain.related.compute_related`, and `brain ui` serves them at `GET /api/notes/{id}/related`, but no MCP tool exposes them. See BL-6. |
| 6.2 | Backup + restore | **SHIPPED** | `brain backup` / `brain restore` (F3, `src/brain/backup/`, in v0.3.0 `e084c79`). PR #47 made backups skip `<vault>/.quartz`. |
| 6.3 | Self-telemetry + a usage command | **SHIPPED (reshaped)** | `brain usage` (F7; `usage.py`, `cli_usage.py`, v0.3.0). It rolls up `search_queries` (with `duration_ms`, migration 024) and `interactions` by surface and agent (F10, migration 027). It lives in Postgres, not in a separate store, and covers searches and reads, not every CLI command's latency. That narrowing is deliberate (F7 section). |
| 6.4 | Multi-host sync | **NOT STARTED** | Related deferral: the MCP HTTP transport (`docs/specs/2026-07-25-sections/R1-DEFERRED-mcp-http-transport.md`) was evaluated and deferred. See BL-10 and §3, rank 20. |

### 1.7 Section 7 — UX and quality of life

| Id | Item | Status | Evidence |
|---|---|---|---|
| 7.1 | Interactive picker for `brain search` | **NOT STARTED** | No picker code. `brain ui` now meets most of the underlying need: a live result ledger, keyboard selection and a preview. |
| 7.2 | `brain examples` | **NOT STARTED** | No such command. `docs/cli-reference.md` and the README examples are the current answer. |
| 7.3 | Code-aware chunker | **NOT STARTED** | No code chunker. Only needed if 2.6 happens. |
| 7.4 | Tag taxonomy tools (merge, rename, suggest merges) | **PARTIAL** | Canonical normalization is enforced at every write boundary (`tags.py`), and `brain backfill normalize-tags` exists (`6dd973e`). **Missing:** merge, rename and suggest-merges. See §3, rank 13. |
| 7.5 | Opt-in query rewriting | **NOT STARTED** | No `--rewrite`. `brain ask`'s plan step writes its own sub-queries, which meets the "vague question" use case without touching search. |

### 1.8 Wave-plan deferrals not tied to a roadmap number

| Source | Deferral | Status |
|---|---|---|
| Q1-B D13 | `brain_explain` MCP tool | **NOT STARTED.** See BL-6. |
| Q1-C D1 | Wiki click endpoint | **SUPERSEDED** (wiki deleted, PR #43). The UI logs opens. |
| Q1-C non-goal | Ranking from `interactions` | **NOT STARTED.** See §3, rank 14. |
| Q1-D D16 | Summary in `brain_search` results | **SHIPPED** as the `brief` projection. |
| Q1-D non-goal | Background enrichment daemon | **NOT STARTED, and not needed.** The ingest hook plus `brain enrich --backfill` cover it. |
| Q1-D D17 | `documents.parent_document_id` | **NOT STARTED.** Wave Q1-D deferred it until something needs it, and nothing does yet. Not listed in §3. |
| Q2-SUMMARY-WIKI | Summary lede in the wiki | **SHIPPED, then SUPERSEDED.** `brain vault sync-summaries` (`95d8754`) still feeds frontmatter. The Quartz component was deleted at R3, and `brain ui` renders the lede (`tests/test_ui_browser_lede.py`). |
| Q1-B, Q1-C, Q1-D | Four small items, listed below the table | **Conditional, not scheduled.** Each is gated on a condition that does not hold at `0bf3e36`. Not ranked in §3. |

The four conditional items, each with the condition that would make it worth doing:

- **An MCP twin of `brain eval`** (Q1-B, its "MCP" section): worth doing only if eval moves into
  chat-time use. Q1-B kept it out because eval is "an ops-time activity, not a chat-time one".
- **A `--tier vault|ingested` search filter,** the other axis beside `--kind` (Q1-C D11): worth doing
  only if someone asks to filter by tier.
- **A cleanup tool for the append-only `interactions` table** (Q1-C non-goals): worth doing only if
  the table grows large enough to need pruning. Q1-C itself names no trigger; that is the natural
  one.
- **A cap on the tag vocabulary sent to the auto-tag prompt** (Q1-D D18): worth doing only if the
  vocabulary outgrows the prompt. D18 sets the threshold.

## 2. Other deferred work, folded in

### 2.1 `brain ui` tabs

| Tab | Status | Evidence |
|---|---|---|
| **Ingest** (v2) | **IN PROGRESS.** Designed by `docs/specs/2026-10-23-brain-ui-ingest-tab-design.md` (this PR). Phase 1, the job registry and the paste backend, is **SHIPPED (#73, RM-10)**. Phase 2, the paste front end, is **SHIPPED (#74, RM-11)**. Upload is RM-13. | `docs/specs/2026-07-25-brain-ui-design.md` §1.1. Non-goal of the consolidation spec (§1.2). Until RM-11, `index.html` rendered a static "Deferred to a later release" panel (`#tab-ingest`) that named `brain ingest`; it is now the paste form and job panel. |
| **Agent** (v3) | **NOT STARTED.** It should reuse the Ingest tab's job subsystem (spec §4.7). | Same two sources. `#tab-agent` names `brain ask`. |
| **Publish** | **SUPERSEDED, and gone from the UI.** | The consolidation spec, Q10 ("Delete it"), plus phase 8/9. The tab bar now has exactly three tabs, and `tests/test_ui_tabs_static.py:42-44` pins `["notes", "ingest", "agent"]` for the buttons, the sections and `main.js`'s wiring. There is no Publish button or panel left to correct. |

### 2.2 Open follow-ups from `docs/handoff/2026-10-13-merge-all-handoff.md`, "Closed out 2026-10-22"

Each is copied by meaning from "Follow-ups still open (as of 2026-10-22)". All are low-severity
residues, recorded with their repairs.

| Id | Follow-up | Source PR |
|---|---|---|
| H-1 | Move-identity residue: a brand-new, never-synced, id-less file that replaces P and is renamed within one debounce window still adopts the previous document's id. | #52 |
| H-2 | A closed watcher drops late events. The next startup sync reconciles them, but a delete is pruned only with prune on. | #51 |
| H-3 | Graph-lock residuals 2–4: `build_graph` raises for a document deleted mid-enumeration; the plan re-check compares `content_hash` only; the cross-type collapse window is cosmetic. | #53 |
| H-4 | Watcher reconnect residue: a delete committed just before the connection died leaves a ghost AGE vertex (repair: `brain graphrag remove`); a delete not applied at shutdown is redone only by `brain vault sync --prune`. | #56 |
| H-5 | Log-privacy blind spots of the G1 guard: a value derived from an exception and logged outside its handler; an exception stored on an attribute; a nested `def` or `lambda` inside a handler. | #58 |
| H-6 | Two terminal error reports still carry exception text: `vault/export.py` `summary.errors` and `cli_sensitivity.py` `result.errors`. Both were judged content-free. | #58 |
| H-7 | **SHIPPED (#61, merged at `02c2abc`).** Decided 2026-10-23: search falls back to text-only (FTS) matching when the query embed fails. The follow-ups that PR records are BL-13 to BL-15. | #61 |
| — | `graph_runner` has no `degraded` tolerance. | By design (its module docstring). Not backlog. |
| — | The graph lags while a startup backlog drains. | By design (PR #54): the lane catches up by itself. Not backlog. |

The same section's "Remains the user's" list is machine operations: stop the root Caddy, delete
`<vault>/.quartz`, decide the three unmerged July branches, keep `daemon-src`. These are not code,
so they are not ranked in §3. Item 5 (the July branches) is safe to close now that H-7 has merged, because
`TRIAGE.md` judged everything else on them superseded.

### 2.3 Other open deferrals in `docs/specs`, `docs/plans` and `CLAUDE.md`

| Id | Item | Source |
|---|---|---|
| BL-1 | **SHIPPED** (roadmap ticket RM-04, PR #66). ~~CI check for the file-size ceiling table.~~ `scripts/check_ceiling_table.py` runs in `ci.yml`'s lint job and in `bin/brain-ci`'s lint stage. It fails on a file over 800 lines with no row, on a row whose file is gone or back under, or on a malformed row, and exits 2 when the table header is missing. What a row says, and the near-ceiling budgets, stay a review. Before this, one crossing went unrecorded (`related.py`, 726 → 851 at `b7fd0e8`) and one (`vault/graph.py`, the 2026-09-02 merge) was caught only by hand. | CLAUDE.md, ceiling section, "The residue" paragraph. |
| BL-2 | **PARTLY SHIPPED** (roadmap ticket RM-06, PR #68: the smallest scope). New commands register through `cli_registry.EXTENSION_REGISTRARS` and new MCP tools are modules in `src/brain/mcp_tools/`, so neither file has to grow for a new command or tool. **The split itself is still open, as RM-48** (needs the user). `cli.py` / `mcp_server.py` split. A split was scoped during G0–G4 and "deferred deliberately". The ceiling rules forbid splitting a working module just to satisfy a number. | CLAUDE.md ceiling table, `cli.py` row. Memory note on GraphRAG G0–G4. |
| BL-3 | **SHIPPED** (roadmap ticket RM-05, PR #67). ~~Offline-test gap for CSS `url()`.~~ `test_every_css_url_resolves_on_disk` resolves every `url()` in every shipped stylesheet, with comments stripped and `data:`/`#fragment` targets skipped. It fails, naming the stylesheet and the target, on a missing, empty or directory target, on one outside `static/` (`..`, another origin, a symlink), on one no `package-data` glob ships, and on a `url(` it cannot parse. Before this, `test_every_referenced_asset_exists_on_disk` parsed only `index.html`. | Consolidation spec §12 Q18. |
| BL-4 | "Open in external editor": the default answer was copy-path, with an `obsidian://` link on request. Not built (no `clipboard` / `obsidian://` anywhere in `ui/static`). | Consolidation spec §12 Q4, §8.6. |
| BL-5 | Cross-encoder reranker. | Roadmap 5 (above). |
| BL-6 | MCP parity gaps: `brain_related` (roadmap 6.1) and `brain_explain` (Q1-B D13). | Above. |
| BL-7 | Self-hosted fonts (`font-src 'self'`). Unresolved in practice: `tokens.css` uses system stacks, and no `font-src` exists in `security.py`. This is the spec's own fallback ("set `--measure: 34rem` and the direction survives, weaker"). | Consolidation spec §12 Q14. |
| BL-8 | Retire the tab strip in favour of ⌘K plus a status footer. Superseded in part: one of the three apologetic tabs (Publish) is gone, and the Ingest spec turns another into a real tab. | Consolidation spec §12 Q17. Revisit only if the Agent tab is dropped. |
| BL-9 | Saved searches / watch lists (F13). | The 2026-07-25 parent spec §2. Roadmap 4.4. |
| BL-10 | MCP HTTP transport, for reaching the brain from another machine. | `R1-DEFERRED-mcp-http-transport.md`. |
| BL-11 | The MCP server spec's "Out of scope, deferred to a later spec" list. Mostly overtaken: tag, edit, remove and capture write tools now exist. Re-read it before reusing it. | `docs/specs/2026-04-27-mcp-server-design.md`, "Out of scope". |
| BL-12 | **SHIPPED** (roadmap ticket RM-07, PR #69). ~~**Stale docstring.** `ingest_document`'s docstring says the auto-summary is written "inside the same transaction".~~ It now says the summary is written after the commit, as its own autocommit `UPDATE`, and cites `_ingest_within_transaction`'s docstring, which already said so; `tests/test_ingest_docstring.py` fails if the stale sentence comes back. The rewrite is net-zero, and the same PR moved the extractor dispatcher out of `ingest/__init__.py` into `ingest/dispatch.py`. | This review. |
| BL-13 | **SHIPPED** (roadmap ticket RM-03, PR #65). ~~`brain ui` search has no FTS fallback.~~ `GET /api/search` now goes through `brain.search_fallback`, so with the embedder down the Notes tab shows text-only results and a class-only notice above the ledger (`#search-fallback`, set as `textContent`), and the response carries the four `fallback*` keys. Before, it answered 503 `embedding_unavailable` and showed nothing. | PR #61, follow-up F3. |
| BL-14 | **SHIPPED** (roadmap ticket RM-01, PR #63). ~~`brain recall` has no `EmbedError` mapping.~~ `recall.recall` now goes through `brain.search_fallback`, so `brain recall` and MCP `brain_recall` fall back to text-only matching and say so (a stderr notice, the header's `, text-only`, the four `fallback*` keys). Before, `cli_recall.py` let a down embedder escape as an unhandled traceback, and `brain_recall` mapped it to `INTERNAL_ERROR`. | PR #61, follow-up F2. |
| BL-15 | **SHIPPED** (roadmap ticket RM-02, PR #64). ~~`brain ask` leaks `EmbedError` when the embedder is down.~~ `ask._retrieve_hybrid` now goes through `brain.search_fallback`, so `brain ask` and MCP `brain_ask` answer from text-only retrieval and say so (a stderr notice, a `Retrieval: text-only` line, the four `fallback*` keys). Every other `ask` failure is one fixed, class-only sentence (`brain.ask_errors`), on the CLI and over MCP. `brain eval --answer` passes `fallback=False` and still aborts (spec §2.7). Before, the down embedder escaped as an unhandled traceback, because the CLI handler mapped only chat and graph errors, and MCP `brain_ask` had no `EmbedError` mapping. | PR #61, follow-up F1. |

**Closed, so not listed:** the hermetic CI eval gate (the user chose a local gate, 2026-10-12, in
CLAUDE.md); the self-hosted eval runner ("probably not worth it", CLAUDE.md); consolidation Q1–Q3,
Q5, Q7–Q13, Q15, Q16 and Q19, each marked answered or shipped in that spec's §12; and consolidation
Q6, the graph diagnostic workbench, answered "Do not port. Rebuild only if you reach for it."

## 3. Open backlog, in recommended order

> **Execution is now tracked by** `docs/specs/2026-10-23-roadmap-completion-design.md`: every open
> row below has a ticket there (`RM-01` to `RM-61`), and its Appendix B is the tracker. Close a row
> here in the same PR that closes its ticket.

The order follows four rules. Finish what is in flight. Take small items that close a recorded hole
before large features. Ship the one feature that already has a design. Do anything that needs an
eval only now that the eval has just been re-judged.

| # | Item | Size | Why this position |
|---|---|---|---|
| 1 | **H-7: FTS-only search fallback** | — | SHIPPED (#61, merged at `02c2abc`). Its follow-ups BL-13 to BL-15 are rank 1a. |
| 1a | **BL-14, BL-15, then BL-13: embedder-down handling in `brain recall`, `brain ask` and `brain ui` search** | S each | Right after PR #61, while the fallback's shape is fresh. BL-14 and BL-15 turn tracebacks into clean errors; BL-13 is the UI half of the same decision, and needs a browser test. |
| 2 | **BL-1: ceiling-table CI check** | S | Without it there has been one unrecorded crossing and one caught only by hand (BL-1). A day of work removes a whole class of review misses. |
| 3 | **BL-3: CSS `url()` in the offline test** | XS | A few lines. It closes the same blank-page failure class as the wheel-packaging bug. Do it before anyone adds fonts or images in CSS. |
| 4 | **Ingest tab, phases 1–2: paste ingest** (spec §10) | M | The first deferred UI tab, fully designed. It also delivers the job subsystem the Agent tab needs. Phase 1 (jobs + paste backend) is SHIPPED (#73, RM-10); phase 2 (the paste front end) is **SHIPPED (#74, RM-11)**. |
| 5 | **BL-6: `brain_related` + `brain_explain` MCP tools** | S | Pure parity, since both cores exist. Agents currently cannot see related documents, which the UI shows by default. |
| 6 | **Ingest tab, phase 3: file upload** | M | It needs the narrowly scoped Content-Type exception (spec §7.2), so it ships separately, with its own security review. |
| 7 | **5 / BL-5: cross-encoder reranker** | L | The highest-leverage ranking change left. It is now measurable, because all 31 golden rows are fresh (PR #50). It must clear `--fail-below` locally. |
| 8 | **4.4 / BL-9: saved searches + notification** | M | It was cut for scope, not for merit. The F5 WHERE-builder it depends on exists. The notification channel should reuse the brief daemon. |
| 9 | **Agent tab (v3)** | L | It reuses item 4's job registry. It needs its own hardening pass for rendering model output (UI spec §1.1). |
| 10 | **4.1 delivery: brief to Slack or email** | S | Small once the owner picks a channel, which is a product decision. |
| 11 | **3.4: near-duplicate detection** | M | It grows in value as more uploads arrive through the Ingest tab (cross-surface duplicates, spec §5.2 and §11 R4). |
| 12 | **3.6: document-level embeddings** | M | It needs a migration and an eval run. Re-evaluate after the reranker, which may take most of the gain. |
| 13 | **7.4: tag merge / rename** | S | Housekeeping. Normalization already prevents new drift. |
| 14 | **1.3 phase 2: interactions-aware ranking** | M | Only worth doing once `brain usage` shows enough opens to learn from. Gate it on the data. |
| 15 | **BL-4: copy-path / external-editor affordance** | XS | Small. Its default was already decided. |
| 16 | **H-1 to H-6, BL-12: watcher / graph / log residues, and the stale ingest docstring** | XS–S each | Each has a known repair, and none loses data. Take them when their module is next opened (Scout rule). |
| 17 | **3.3 leg: `--entity` search filter** | S | Mostly covered by `brain graphrag` today. Ask whether it is wanted before building it. |
| 18 | **Source breadth: 2.1, 2.3, 3.5 / 5.1, 2.8, 2.4 (Apple Notes), 5.2** | M–L each | Each is a new extractor and a new source kind. Order them by what the owner actually generates. Each needs its own design for scope and PII. |
| 19 | **2.5 browser history, 2.6 + 7.3 code ingestion, 2.9 long tail** | L | The heaviest privacy load, or the least evidence of need. Keep them, because the owner accepted them, but last. |
| 20 | **BL-10 MCP HTTP transport, 6.4 multi-host sync** | L | No current need. Leave deferred. |
| — | **Leave deferred: BL-2 (big-file split), BL-7 (fonts), BL-8 (tab strip), 7.1, 7.2, 7.5** | — | Each is either deliberately deferred with a recorded reason, or met in practice by `brain ui` / `brain ask` / `docs/cli-reference.md`. Re-open one only on a concrete ask. |

## 4. Maintenance of this document

This document is a snapshot at `0bf3e36`. It is not a live tracker. Close a row here in the same PR
that closes the item. Re-derive any SHA with `git log --reverse -S '<symbol>' -- <path>` rather than
trusting this table.
