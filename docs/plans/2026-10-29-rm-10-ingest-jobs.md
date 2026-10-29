# RM-10 — Ingest tab, phase 1: jobs and the paste backend

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-10 in `docs/specs/2026-10-23-roadmap-completion-design.md` (wave 3).
**Design:** `docs/specs/2026-10-23-brain-ui-ingest-tab-design.md` (the "Ingest spec"): the phase-1 row of
§10, §4, §5.1, §5.3, §5.4, §5.6, §6 (paste routes), §7.1, §7.5, §9.1, §9.2 and §9.3 (paste rows).
Decisions D1–D5 and D8–D10 apply as written.
**Branch:** `feat/rm-10-ingest-jobs`, from `second-brain/master` after PR #72 (RM-09) merged.

## Goal

Paste ingest works end to end over HTTP, on a kind-agnostic job registry that the Agent tab (RM-18a)
can reuse without editing `jobs.py`.

## Files

New modules (each planned well under 400 lines):

| File | Responsibility |
|---|---|
| `src/brain/ui/jobs.py` | `JobRegistry`, `JobSnapshot`, `JobEvent`, `JobHandle`, `JobCancelled`, `QueueFull`, `JobUnknown`, `NotCancellable`, `ShuttingDown`, `JobError`. Starlette-free, psycopg-free. One lock, frozen snapshots, the `run_next` test seam, a daemon worker, `stop(timeout)`. Per-kind retention override. The 200-event cap. |
| `src/brain/ui/ingest_limits.py` | Module constants: one running job, `MAX_QUEUED = 4`, `RETAIN = 50`, `RETAIN_FOR = 1 h`, `EMBED_BATCH = 16`, `MAX_PASTE_BODY_BYTES = 2 MiB`, `MAX_PASTE_WIRE_BYTES = 6 × 2 MiB + 4 KiB`, `PASTE_CONTENT_TYPES = ("note", "transcript")`, `STOP_TIMEOUT_S = 10.0`. Not config knobs (Ingest spec §4.5). |
| `src/brain/ui/ingest_progress.py` | `ProgressEmbedder(inner, handle, batch_size=16)`. It forwards `hosted_egress` and `produces_embeddings` from `inner`, using the readers' own defaults (C5); it embeds in slices with `check_cancelled()` before each slice; it calls `enter_commit()` after the last slice. |
| `src/brain/ui/ingest_service.py` | The second permitted mutator (§1.3). Builds `PasteSpec`, the paste runner, the failure mapping (§4.5 table) and the result payload (§5.4, including `possible_duplicates: []`, summary status and graph state). Holds `IngestRuntime`, which builds the job's own embedder and enricher once per worker thread (§5.3). |
| `src/brain/ui/ingest_schemas.py` | `PasteSpec` and `parse_paste` (§5.1 validation at submit). A leaf beside `ui/schemas.py`, so that module keeps its headroom under the ceiling. |
| `src/brain/ui/routes_jobs.py` | `GET /api/jobs`, `GET /api/jobs/{id}?after=`, `POST /api/jobs/{id}/cancel`. |
| `src/brain/ui/routes_ingest.py` | `GET /api/ingest/config`, `POST /api/ingest/paste` (a `Content-Length` precheck, then a byte counter over `request.stream()`; it never calls `request.body()`). |
| `src/brain/ingest/outcome.py` | `IngestOutcome` (StrEnum) and `classify(result, *, force=False) -> (outcome, mirror_repaired)`, extracted from `cli_ingest._ingest_outcome_verb`. |

Existing files touched:

- `ui/app.py`: the five routes, plus a `lifespan` that calls `jobs.start()` / `jobs.stop(timeout=10)` and
  logs one WARNING line per job id still running at timeout.
- `ui/context.py`: the `jobs: JobRegistry | None = None`, `ingest_enabled: bool = False` (fail-closed
  default) and `ingest_runtime` fields.
- `ui/server.py`: `build_context` builds the registry and runtime, and sets
  `ingest_enabled = loopback and not read_only`.
- `ui/errors.py`: `UiPayloadTooLarge` (413) and `UiTooManyRequests` (429).
- `ui/routes_meta.py`: `health` gains `ingest_enabled`.
- `ui/notes_service.py`: the docstring amended to name the two permitted mutators (§1.3).
- `cli_ingest.py` (over the ceiling): `_ingest_outcome_verb` becomes a map over `classify`. **Net
  negative.** Its CLAUDE.md row is promoted, and gets a hop for this change.
- `docs/specs/2026-07-25-brain-ui-design.md` §9.3 and `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md`
  §2.3: the "only notes_service" rule restated as "only these two" (the §1.3 amendment, which the Ingest
  spec says lands in the PR that adds the module).
- `CHANGELOG.md`, `docs/cli-reference.md` (the `brain ui` section: the new endpoints, backend only),
  `docs/plans/2026-10-23-backlog-reconciliation.md` (rank 4 row), Appendix B row (follow-up commit),
  memory `cli.md` / `types.md` / `MEMORY.md`.

Not touched: `ingest/__init__.py`, `config.py`, `cli.py`, `mcp_server.py` (Ingest spec §10, "Ceiling
check"). No migration.

## Design choices the ticket leaves open (resolved here)

1. **Error mapping stays kind-agnostic.** `JobRegistry.submit(kind, runner, *, on_error=None)`.
   `on_error(exc) -> JobError(code, message)` maps a non-cancel exception to the fixed per-code envelope.
   The ingest service supplies `ingest_failure`. Without a mapper, the registry falls back to a generic
   `internal_error` naming the class. So `jobs.py` knows nothing about ingest, and RM-18a supplies its
   own mapper.
2. **Per-kind retention:** `JobRegistry(..., retain_for=timedelta(hours=1), retain_for_by_kind={})`.
   RM-18a passes `{"ask": timedelta(minutes=10)}`.
3. **`JobSnapshot.cancel_requested: bool`** is added to the spec's field list, because the cancel table
   returns `cancel_requested: true` on a 202.
4. **The failure log line** is `"ui %s job %s failed: %s", kind, job_id, describe_failure(exc)`, logged
   by the registry. That is class-only (G1), and it names the kind because the registry is shared.
5. **No `logger.exception` is added**, and no G1 allowlist entry.
6. **`ingest_enabled` defaults to False** on the dataclass (fail closed). `build_context` sets it.
7. **The reason in `GET /api/ingest/config`** is chosen in this order: `read_only`, then
   `requires_loopback` (`not ingest_enabled`), then `jobs_unavailable` (no registry or no runtime).
8. **The paste route's guard order:** `ingest_enabled` (403 `ingest_requires_loopback`), then registry
   present (503 `jobs_unavailable`), then the size checks, then validation (400), then submit
   (429 `queue_full`, 503 `shutting_down`).
9. **An empty `embed([])`** in `ProgressEmbedder` still runs `enter_commit()`. It is reached only before
   a write, so closing the window there is correct.
10. **Summary status** is read back from `documents.summary` after the job: `written` (non-NULL);
    `skipped` (enrich off, no enricher, or content shorter than `enrich_min_tokens`, judged with the
    enricher's own `count_tokens`); otherwise `pending`.
11. **Graph state:** `disabled` when `cfg.graph_enabled` is false; otherwise `queued` when
    `watcher_probe.state() == "running"`, else `not_synced`.
12. **The job list carries no `result`** (and no events). A result names its document (title, vault
    path), so the unprompted list is a listing surface like the tree. A client reads a result by
    the id it submitted. `tests/test_ui_confidential_titles_gate.py` discovers every GET route, so
    it rules on the new ones: `/api/jobs` is checked like every listing surface (seeded with a
    finished job that names the sealed document), and `/api/jobs/{job_id}` joins `_PROMPTED` with a
    written ruling (the caller names an unguessable id only the submit response returned).
    `_PROMPTED`'s size pin moves from 2 to 3 in the same edit, as that test requires.
13. **`JobHandle.job_id`** is added to the spec's Protocol, so a runner's INFO line can carry the job id
    (§7.5: the id, kind, outcome and document id, never the title).
14. **Phases.** The registry logs `queued`; the paste runner logs `preparing`; the progress embedder
    logs `embedding` per batch, then `finishing` once `enter_commit()` has closed the window.
15. **Tags are normalized twice**: at submit (`parse_paste`, the boundary the user sees) and in the
    runner (the write boundary itself, idempotent), because `ingest_document` does not normalize.
16. **Two extra test files**, `tests/test_ui_ingest_security.py` and `tests/test_ui_ingest_wiring.py`,
    hold the §9.3 paste rows and the `build_context` / lifespan tests. `tests/test_ui_security.py` is
    at 704 lines and would cross the 800-line test-file split precedent (PR #67).

## Tests (TDD: red first)

- `tests/test_ui_jobs.py` (pure, ≥95%): every §9.1 row.
- `tests/test_ingest_outcome.py` (pure): `classify` over every `IngestResult` shape, and the CLI verbs
  re-derived from it. The existing CLI ingest tests stay unmodified.
- `tests/test_ui_ingest_progress.py` (pure): slicing, cancel per slice, `enter_commit` after the last
  slice, and both flag forwards (with the readers' defaults when `inner` lacks them).
- `tests/test_ui_ingest_service.py` (real DB, paste rows of §9.2):
  - paste → row, chunks and an `_ingested/manual/` mirror;
  - a repeat paste → `skipped`;
  - mid-embed cancel cases (a)/(b)/(c), with the chunk counts asserted with `chunk_text`;
  - a no-embed `cancel_too_late`;
  - a crash during the summary → `internal_error` with the row committed, then a re-submit →
    `mirror_repaired`;
  - hosted + confidential → NULL embeddings and an egress notice;
  - `produces_embeddings=False`;
  - the secret guard under `warn` and `reject`;
  - a failing mirror → success, then `mirror_repaired`;
  - `sync_one_file` over the new mirror reaches `reconcile` on a recording `GraphSyncTarget`;
  - `JobCancelled` propagates through `ingest_document`;
  - the failure mapping table.
- `tests/test_ui_ingest_routes.py`: the §6 contract for config, paste and the jobs routes; status codes;
  envelopes; `config` in each mode; `health.ingest_enabled`; the wire cap (honest `Content-Length` and
  absent `Content-Length`); the decoded cap; validation; 429; 503.
- `tests/test_ui_security.py` additions (paste rows of §9.3): cross-origin, missing-Origin and
  `Sec-Fetch-Site: cross-site` on paste and cancel get 403; `--read-only` gets 403 on every ingest/jobs
  mutation while GETs are allowed; non-loopback gets 403 `ingest_requires_loopback` on every ingest
  mutation route **enumerated from `app.routes`**, while `config` and the job GETs answer; the
  GET-cannot-mutate table extended.
- `tests/test_ui_app_wiring.py`: reachability for the five new routes.
- `tests/test_ui_server_*`: `build_context` sets `ingest_enabled` per bind and read-only mode.

The upload rows, the octet-stream middleware exception and the browser tier are RM-13 and RM-11.

## Mutation proofs (in `git archive` copies only)

- M1: drop the `hosted_egress` forward → the hosted+confidential test goes red.
- M2: drop the `produces_embeddings` forward → its test goes red.
- M3: make `enter_commit` close without checking → cancel cases (b) and (c) go red.
- M4: remove the loopback check in the paste route → the non-loopback security test goes red.
- M5: remove `check_cancelled` before each slice → cancel case (a) goes red.
- M6: drop the `cancellable=False` on terminal states → the terminal-snapshot test goes red.
- M7: remove the wire-cap counter → the absent-`Content-Length` test goes red.

## Risks

The Ingest spec's §11 and §12, with the recommended defaults as written. One local risk: the lifespan
starts a worker thread when a test enters `TestClient` as a context manager. The route tests therefore
build the registry and drive it with `run_next()` without entering the client context. The lifespan is
tested once on its own, with a registry whose worker is joined by `stop()`.
