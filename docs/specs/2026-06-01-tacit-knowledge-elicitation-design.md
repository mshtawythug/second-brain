# Tacit-Knowledge Elicitation (`brain elicit`) — Design Spec

**Date:** 2026-06-01
**Status:** Draft (pending Codex review → plan)
**Author:** Claude (design), Codex (architecture review)

---

## 1. Thesis & Motivation

Second-brain today *indexes the room*: it ingests already-captured artifacts
(Krisp transcripts, Slack, Gmail, files), embeds them, and serves hybrid +
graph retrieval. It is a **retrieval** system — it surfaces what has already
been written down.

The frontier (Polanyi, *"we can know more than we can tell"*) is **elicitation**:
the load-bearing knowledge in any individual's work is *tacit* — referenced
constantly, implied everywhere, codified nowhere. Tacit rules don't get
articulated until something is about to violate them. The right interaction is
therefore **draft-then-correct**: the system makes a *confident guess* at the
unwritten rule, and the user reacts/corrects it. The correction *is* the
elicited knowledge.

`brain elicit` adds the part that **asks**. It mines the corpus for knowledge
that is implied but never authored, drafts a confident articulation of the
underlying rule, lets the user correct it in their editor, and codifies the
result back as a vault note — which re-enters the graph automatically.

### Why second-brain is uniquely positioned
It is the only system in this space that already distinguishes what was *said*
(ingested-tier docs) from what was *deliberately authored* (vault-tier notes).
The delta between them — referenced heavily in transcripts, never written into
a note — is structurally identical to the seating-chart rule: a computable
proxy for tacit knowledge.

---

## 2. Locked Product Decisions (user, 2026-06-01)

| Decision | Choice |
|---|---|
| **Scope** | Single-player now; **multi-tenant-ready interfaces** (graph already carries `tenant_id`). |
| **Trigger** | On-demand `brain elicit` CLI command — standalone interactive loop. No scheduling, no task-coupling in v1. |
| **Gap signals** | All four, pluggable: authored-vs-ingested **delta**, high-mention/no-summary **orphan** entities, **contradiction**/reversal, **user-flagged**. |
| **Codify target** | Existing `brain note new` vault-note path. No new `content_type`. |

---

## 3. Goals / Non-Goals

**Goals**
- Surface tacit-knowledge gaps from the existing corpus via pluggable detectors.
- Rank heterogeneous signals into one queue with no magic weights.
- Draft-then-correct loop that honors "react to a confident guess."
- Codify accepted rules as vault notes, wikilinked to their source entity.
- Durable gap lifecycle (surfaced / snoozed / dismissed / resolved) so resolved
  gaps don't re-surface.

**Non-Goals (v1)**
- Scheduling / proactive digests / task-coupled interjection.
- Multi-tenant *operation* (only multi-tenant-ready *interfaces*).
- A new `content_type` for elicited notes.
- LLM-scored confidence metrics inside the loop (trust the user's edit step).
- Screen-capture / passive observation sources.

---

## 4. Architecture

New package `src/brain/elicit/`, composed on existing surfaces
(`graph_rag/`, `enrichment.py`, `interactions.py`, `edit_session.py`,
`rank_fusion.py`, the vault authoring path). No changes to `search.py`,
no changes to `interactions`.

```
src/brain/elicit/
  __init__.py   # exports: GapDetector, Gap, ElicitDraft, ElicitOutcome,
                #          build_queue, run_session
  schema.py     # frozen dataclasses (value objects)
  detectors.py  # GapDetector Protocol + 4 concrete detectors + DETECTOR_REGISTRY
  queue.py      # build_queue(): detect → upsert → ranked read-back
  drafter.py    # GapDrafter(enricher): Gap → ElicitDraft
  session.py    # run_session(): interactive loop + _codify() (codify lives here)
```

### 4.1 Value objects (`schema.py`)
- `Gap`: `gap_id`, `signal_kind`, `target_type` ∈ {person, org, project, topic,
  tool, doc} (the graph `entity_type` set + `doc`), `target_id`, `score`,
  `evidence_ids: list[str]`, `evidence_texts: list[str]`, `rationale`.
- `ElicitDraft`: `gap_id`, `title`, `draft_text`, `evidence_ids`, `evidence_texts`.
- `ElicitOutcome`: `gap_id`, `action` ∈ {accepted, skipped, snoozed, dismissed},
  `note_id: str | None`, `snoozed_days: int | None`.

`Gap` carries `evidence_texts` (truncated summaries) so the editor comment block
is self-contained — no second DB query in `session.py`.

### 4.2 Detectors (`detectors.py`) — Open/Closed
`GapDetector` Protocol: `signal_kind: str` (class var) + `detect(conn, tenant_id, limit) -> list[Gap]`.
A `DETECTOR_REGISTRY: dict[str, type[GapDetector]]` dispatches by signal kind.
Adding a signal = new detector + registry entry; existing detectors untouched.

- **`DeltaDetector`** (`delta`): pure SQL. Entities with many
  `graph_entity_mentions` in `documents.kind = 'ingested'` but **zero** authored
  (`kind = 'vault'`) mentions. The cleanest computable tacit-knowledge proxy.
- **`OrphanEntityDetector`** (`orphan`): pure SQL. Graph entities with high
  `mention_count` but NULL/thin description — the graph knows *who* matters, not *why*.
- **`ContradictionDetector`** (`contradiction`): **stub returning `[]`** unless
  `ELICIT_CONTRADICTION_ENABLED=true` (see §7, Wave 4). Hardest signal; deferred.
- **`UserFlaggedDetector`** (`user_flagged`): wraps a user-supplied target string
  (`--target "<person/topic>"`) into a `Gap`. Trivial.

### 4.3 Queue (`queue.py`)
`build_queue(conn, cfg, tenant_id, signals, limit) -> list[Gap]`. A thin
orchestrator, NOT an in-memory merge:
1. Run each active detector.
2. **Upsert** results into `elicitation_gaps` via `INSERT … ON CONFLICT DO UPDATE`
   (dedup is the DB partial-unique-index contract, §5).
3. Read back the queue ordered by ranked score, excluding `resolved`/`dismissed`
   and snoozed-in-the-future rows, filtered by guardrails (§6).

**Ranking:** per-detector min-max normalize raw scores to `[0,1]`, then apply the
shared `rrf_contribution(rank, k=60)` from `brain.rank_fusion` (same primitive as
`search.py`/`fuse.py`). Gaps appearing under multiple signals sum their RRF
contributions. No hand-tuned weights.

### 4.4 Drafter (`drafter.py`) — Dependency Inversion
`GapDrafter.__init__(enricher: OllamaEnricher)` — injected, not constructed
internally (tests pass a fake). `draft(conn, gap, tenant_id) -> ElicitDraft`
fetches `evidence_texts` from `documents.summary` (fallback: head of content),
then calls a **dedicated** `_ELICIT_SYSTEM_PROMPT` via the enricher (the task is
"articulate a rule," distinct from `_SUMMARY_SYSTEM_PROMPT`). Adds a
`draft_rule(gap_rationale, evidence_texts)` method to `OllamaEnricher` alongside
`summarize()` — reuses the existing transport/retry, no subclassing, no second
Ollama client.

### 4.5 Session + codify (`session.py`)
`run_session(cfg, conn, enricher, gaps, tenant_id) -> list[ElicitOutcome]`.
Per gap: call drafter → present draft → prompt:

```
[e] edit in $EDITOR   [s] skip   [n] snooze N days   [q] quit
```

- **`e`**: write `ElicitDraft.draft_text` (+ evidence doc-ids as header comments)
  to a tmpfile and launch via `run_editor_session()` from `brain.edit_session`
  (reuses its `$EDITOR` resolution + temp cleanup). On non-empty, *changed* body
  → `_codify()`. On empty/unchanged → re-prompt ("[e]dit again or [s]kip?").
- **`s`**: status → `dismissed`.
- **`n`**: prompt days → status `snoozed`, set `snoozed_until`.
- **`q`**: exit loop.

`_codify(cfg, conn, vault_path, draft, tenant_id) -> str` lives in `session.py`
(single consumer — YAGNI, no separate module). It calls the extracted
`_create_note(...)` helper (see §4.6) to author the note, then updates the
`elicitation_gaps` row to `resolved` with `resolved_note_id`. It does **not**
write to `interactions` — that table is append-only with a fixed `action` CHECK
that has no "codified" value, and the resolved gap row (`status='resolved'` +
`resolved_note_id`) is itself the durable record of the codification.

`session.py` MUST NOT import `cli.py`.

### 4.6 Note-creation helper extraction
Extract `_create_note(cfg, vault_path, title, body, tags) -> str` from the
`note_new()` Typer command into `brain.vault` (or `brain.ingest`), returning the
new `document_id`. `note_new()` becomes a thin CLI wrapper over it; `session.py`
calls the helper directly with `no_edit=True`.

---

## 5. Data Model — Migration `017_elicitation_gaps.sql`

New table; **`interactions` is not modified** (it is append-only with an XOR
constraint and no update path; gap state is inherently mutable).

```sql
CREATE TABLE IF NOT EXISTS elicitation_gaps (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    signal_kind       TEXT NOT NULL
                      CHECK (signal_kind IN ('delta','orphan','contradiction','user_flagged')),
    target_type       TEXT NOT NULL
                      CHECK (target_type IN ('person','org','project','topic','tool','doc')),
    target_id         TEXT NOT NULL,
    score             FLOAT NOT NULL CHECK (score >= 0),
    evidence_ids      TEXT[] NOT NULL DEFAULT '{}',
    rationale         TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'surfaced'
                      CHECK (status IN ('surfaced','snoozed','dismissed','resolved')),
    snoozed_until     TIMESTAMPTZ,
    resolved_note_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
    first_surfaced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS elicitation_gaps_tenant_signal_target_idx
    ON elicitation_gaps (tenant_id, signal_kind, target_id)
    WHERE status != 'resolved';

CREATE INDEX IF NOT EXISTS elicitation_gaps_queue_idx
    ON elicitation_gaps (tenant_id, status, score DESC)
    WHERE status IN ('surfaced','snoozed');
```

- Partial unique index → re-running detectors upserts (`ON CONFLICT DO UPDATE SET
  score = …, updated_at = now()`) rather than duplicating; a `resolved` gap may
  resurface as a new row.
- Snooze filter on read: `snoozed_until IS NULL OR snoozed_until < now()`.

---

## 6. Guardrails Against Garbage Codification

1. **Minimum evidence** — `ELICIT_MIN_EVIDENCE_DOCS` (default 3). SQL filter
   `array_length(evidence_ids,1) >= min`; below-threshold gaps are never drafted.
2. **Required active edit** — `run_editor_session()` body must be non-empty *and*
   changed from the draft; else refuse to codify and re-prompt. Never auto-save
   the raw draft.
3. **Score floor** — `ELICIT_MIN_GAP_SCORE` (default 0.3, post-normalization).
   Below-floor gaps appear in `brain elicit list` but are skipped in the loop
   unless `--include-low-confidence`. No extra LLM call for confidence.

Evidence doc citations render in the editor header so the user has ground truth
to check the draft against.

---

## 7. CLI Surface

- `brain elicit` — run the interactive loop over the ranked queue.
- `brain elicit --target "<person/topic>"` — user-flagged signal, single gap.
- `brain elicit --signal delta|orphan|contradiction` — restrict to one detector.
- `brain elicit list [--json]` — show the queue without entering the loop (read-only, no Ollama).
- `brain elicit --include-low-confidence` — include below-floor gaps in the loop.

CLI hardwires `tenant_id = cfg.graph_tenant_id`; **no `--tenant` flag in v1**.

---

## 8. Config Knobs (`config.py`)

| Env (BRAIN_ prefix per convention) | Config field | Default | Purpose |
|---|---|---|---|
| `BRAIN_ELICIT_MIN_EVIDENCE_DOCS` | `elicit_min_evidence_docs: int` | 3 | Min distinct source docs before a gap is draftable. |
| `BRAIN_ELICIT_MIN_GAP_SCORE` | `elicit_min_gap_score: float` | 0.3 | Post-normalization score floor for the loop ([0,1]). |
| `BRAIN_ELICIT_QUEUE_LIMIT` | `elicit_queue_limit: int` | 20 | Max gaps surfaced per run. |
| `BRAIN_ELICIT_CONTRADICTION_ENABLED` | `elicit_contradiction_enabled: bool` | false | Gates the contradiction detector (Wave 4). |
| `BRAIN_ELICIT_CONTRADICTION_MIN_DOCS` | `elicit_contradiction_min_docs: int` | 5 | Min entity doc-count for contradiction scan. |

All validated via `ConfigError` in `Config._load_field_dict`, mirroring the
existing `BRAIN_PEOPLE_HUB_MIN_DOCS` (int) / `BRAIN_VECTOR_SIM_FLOOR` (float) /
`BRAIN_GRAPH_ENABLED` (bool) parsing patterns.

---

## 9. Multi-Tenant Readiness (minimum)

- Every public fn in `detectors/queue/drafter/session` takes `tenant_id: str`
  (required param, no default).
- `session.py` resolves `tenant_id = cfg.graph_tenant_id` once at CLI entry and
  threads it down.
- `elicitation_gaps.tenant_id` defaults to `'default'` so single-player works
  unconfigured.
- When multi-tenant ships, only `session.py`'s resolution changes. No `--tenant`
  flag, no infra now.

---

## 10. Testing Strategy (≥85%, per-module targets apply)

Three layers, following existing fixtures (`FakeEmbedder`, `FakeRunner`,
`httpx.MockTransport` — **no monkey-patching**):

- **Unit** — `FakeDetector` (implements Protocol, canned `list[Gap]`) tests
  `queue.py` ranking with no DB; `FakeDrafter` (canned `ElicitDraft`) tests
  `session.py` state machine with no Ollama; `OllamaEnricher.draft_rule()` tested
  via `httpx.MockTransport` (mirrors `test_enrichment.py`).
- **Integration** (real test DB via the existing `test_db` fixture — no new
  marker; runs in the default suite like the rest of the DB tests) — one test per
  SQL detector (seed known vault/ingested split, assert gaps);
  `test_queue_excludes_resolved_gaps`; upsert-dedup test.
- **Session/CLI** (`CliRunner`) — `elicit list` read-only; interactive loop via
  injected stdin (`input="s\nq\n"`, `input="e\n"` with `$EDITOR` env override),
  `FakeDrafter` passed in (DI — `run_session` takes the drafter/enricher as args,
  never calls `make_enricher()` internally).

---

## 11. Phased Build Order (team-driven-development)

**Wave 1 — Schema + detectors (no Ollama)**
Migration 017; `schema.py`; `detectors.py` (Protocol + Delta + Orphan +
Contradiction stub + UserFlagged + registry); `queue.py` (upsert + RRF);
`brain elicit list` CLI; config knobs. Tests: per-detector unit + Delta/Orphan
integration + `elicit list` CLI.

**Wave 2 — Drafter (Ollama)**
`_ELICIT_SYSTEM_PROMPT` + `draft_rule()` on `OllamaEnricher`; `drafter.py`. Tests:
`FakeDrafter` + `draft_rule()` via `MockTransport`.

**Wave 3 — Session + codify**
Extract `_create_note()` helper; `session.py` loop + `_codify()`; wire
`brain elicit` / `--target`. Tests: `CliRunner` skip/quit + accept-via-editor.

**Wave 4 — Contradiction detector + polish**
Implement `ContradictionDetector` behind the flag (entity-scoped summaries →
batched LLM contradiction call); `--signal contradiction`; integration test +
perf guard (< 30s for 200 entities).

Each wave ends with the CLAUDE.md §14 review+audit loop (Codex code review +
completion audit) until APPROVED + AUDIT PASSED.

---

## 12. Risks

- **Contradiction detector latency** — O(N²) pair space; mitigated by entity-scoped
  single-call heuristic + feature flag + perf gate (Wave 4).
- **Sparse summaries** — delta/orphan evidence_texts fall back to content head;
  contradiction detector silently returns `[]` + WARN when summaries are null.
- **Draft quality** — mitigated by the three §6 guardrails; the user's edit is the
  source of truth, never the raw draft.
- **`cli.py` already large** — extracting `_create_note()` reduces, not grows, it;
  the deferred cli.py >800-line split is out of scope here.
