# RM-18a — Agent tab backend: plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

The ticket is the spec: `docs/specs/2026-10-23-roadmap-completion-design.md`, RM-18 (decisions
A1–A10) and RM-18a. This plan is the task list, the files, the tests and the risks. Branch
`feat/rm-18a-agent-backend`, cut from `second-brain/master` at `cc62cf4` (PR #77 merged).

## Scope

In: `ask.py` (`on_step`), the new `ui/ask_service.py` and `ui/routes_ask.py`, the job list
filter, the per-kind retention for `"ask"`, and the wiring (`context.py`, `server.py`,
`app.py`). Out: the front end (RM-18b), follow-ups (RM-18c), graph modes (A9, RM-18a.D1).

## Files

| File | Change |
|---|---|
| `src/brain/ask.py` | `ask` and `ask_no_loop` gain `on_step: Callable[[str, int], None] \| None = None`, called before each step with `("plan" \| "retrieve" \| "reflect" \| "synthesize", iteration)`. `ask` forwards it to `ask_no_loop`. Budget §2.2: **+≤ 25** (684 at `cc62cf4`). |
| `src/brain/ui/ask_service.py` (new) | `ASK_KIND = "ask"`, `ASK_RETAIN_FOR` (10 minutes, A10), `MAX_QUESTION_CHARS` (2,000), `AskRuntime` (the injected `ChatJson` and a monotonic clock), `AskTimedOut`, the deadline, the step-guarding embedder and chat wrappers, the runner, `ask_failure` (the error mapping), the availability gate, and `lens_ask_result`. Not a mutator: the only rows it causes are `interactions` rows, written through `ui/telemetry.record_ui_open`. |
| `src/brain/ui/routes_ask.py` (new) | `GET /api/ask/config`, `POST /api/ask` (capped read, `parse_ask`, submit, 202). |
| `src/brain/ui/routes_jobs.py` | `GET /api/jobs` leaves out kind `"ask"` (A10). `snapshot_payload` gains `serve_confidential_bodies` (default `False`, fail-closed) and lenses an ask result through `lens_ask_result`; every other kind keeps `lensed_result`. |
| `src/brain/ui/jobs.py` | `get()` and `cancel()` evict expired jobs first (see D3). |
| `src/brain/ui/context.py` | `ask_runtime: AskRuntime \| None = None`. |
| `src/brain/ui/server.py` | `build_context` builds `AskRuntime(chat=chat.chat_json)` and passes `retain_for_by_kind={ASK_KIND: ASK_RETAIN_FOR}` to the registry. |
| `src/brain/ui/app.py` | The two routes. |
| Tests | `tests/test_ask_on_step.py` (new), `tests/test_ui_ask_service.py` (new, runner on `run_next()`), `tests/test_ui_ask_routes.py` (new, 5434), `tests/test_ui_jobs_evict_on_read.py` (eviction on get/cancel; new, because `test_ui_jobs.py` is at 797), `tests/test_ui_security.py` (ask rows). |
| Docs | `docs/cli-reference.md` (`brain ui` section), `CHANGELOG.md` (Unreleased), the backlog row (item 9), Appendix B's RM-18a row (follow-up commit), memory `cli.md` / `types.md`. |

No over-ceiling file is touched, so no CLAUDE.md hop is owed. No knob is added, so
`docs/configuration.md` is unchanged. No migration.

## Design

- **Runner** (on the registry's one worker, A1). Checks cancel on pick-up, starts the deadline,
  then calls `ask.ask(conn, cfg, embedder=<guarded>, chat=<guarded>, question, no_loop,
  limit=cfg.ask_docs_per_iter, max_iterations=cfg.ask_max_iterations, mode="hybrid",
  exclude_confidential=not ctx.serve_confidential_bodies, on_step=...)` (A7, A9).
- **`on_step`** → `handle.check_cancelled()`, the deadline check, then
  `handle.progress(step, done=iteration, total=max_iterations)` (A3). The events are the
  steps, in order (`total` is 1 for `no_loop`).
- **Cancel** (A4). Cooperative, at every step boundary and before every embed. `enter_commit()`
  is never called. After `ask` returns, one more `check_cancelled()`, then the interaction rows.
  So a cancel is honoured until the result is recorded, and a cancelled ask writes nothing.
- **Deadline.** `cfg.ask_max_iterations × 3 × cfg.ask_timeout_seconds`, from pick-up, on
  `AskRuntime.clock` (injected; `time.monotonic` in production). Checked in `on_step`, before
  each chat call (the chat wrapper) and before each embed. Passing it raises `AskTimedOut` → the
  job fails with code `timeout`.
- **Embedder.** The worker's own (`ingest_runtime.embedder()`, thread-local, the one the ingest
  jobs use), wrapped so every embed is a cancel and deadline point. It forwards `dim`,
  `count_tokens`, `hosted_egress` and `produces_embeddings` with the same defaults
  `ProgressEmbedder` uses (C5), so the F6 egress veto and the FTS-only path see the real
  backend.
- **Result payload.** `AskResult.to_dict()` (RM-02's four `fallback*` keys included); each
  citation's `snippet` cut to `cfg.snippet_max_chars`, and its stored `sensitivity` read back
  (one parameterized query); `elapsed_ms`.
- **Error mapping** (`ask_failure`). `OllamaUnavailable` → `chat_unavailable`,
  `OllamaContextOverflow` → `context_overflow`, `ChatOutputTruncated` → `output_truncated`,
  other `EnrichmentError` → `synthesis_failed`, `EmbedError` → `embedder_unavailable` (each
  message from `describe_ask_failure`, RM-02, never the exception text); `psycopg.Error` →
  `database_error`; `AskTimedOut` → `timeout`; anything else → `internal_error`. Class names
  only.
- **Routes.** `GET /api/ask/config` → `{enabled, reason, max_question_chars, modes: ["hybrid"]}`.
  `POST /api/ask` with `{question, no_loop?}` → 202 and the snapshot. Unknown fields, a
  non-string or blank question, one over 2,000 characters, or a non-bool `no_loop` are 400s.
  `--read-only` is refused by the middleware (403 `read_only`); a non-loopback bind gets 403
  `ask_requires_loopback` (A6). Status and cancel reuse `/api/jobs/{id}` and its `cancel`.
- **Retention and listing** (A10). `retain_for` is 10 minutes for `"ask"`; `GET /api/jobs` omits
  ask jobs; a client reads an ask only by its id.

## Decisions and deviations

- **D1 — the citation lens keys on `serve_confidential_bodies`, not `_titles`.** The coordinator
  asked that the nested citations be lensed, fail-closed, because `lensed_result` reads top-level
  keys only. `lens_ask_result` does that: a citation whose read-back `sensitivity` is not
  `normal` (missing counts) has `document_id`, `title` and `snippet` withheld unless the server
  serves confidential bodies; if any citation is withheld, `answer` and `sub_queries` are too
  (both can paraphrase a withheld document), and `title_withheld` is set. It keys on the BODIES
  flag because an ask is a requested surface (A7: on a loopback server an answer may draw on
  confidential bodies, as the CLI's does; acceptance 7), and because ask jobs are unlisted, so
  the reason the titles gate covers ingest results (a listed id is not a capability) does not
  apply. With bodies withheld, retrieval already excludes confidential documents, so the lens is
  the second, fail-closed layer.
- **D2 — a dedicated step-guard embedder, not `ProgressEmbedder`.** The ticket says the progress
  embedder is "reused". It cannot be, as is: it calls `enter_commit()` after its last batch, which
  would close an ask's cancel window at the first query embed, and its `embedding` events would
  interleave with the step events acceptance 2 pins. The wrapper here keeps the cancel check
  before every embed and copies its capability-flag forwarding.
- **D3 — `JobRegistry.get()` and `cancel()` evict first.** Eviction ran only in `submit()` and
  `list_recent()`, so a finished job outlived its retention for as long as nobody submitted or
  listed. With ask jobs unlisted, `GET /api/jobs/{id}` would have served a 10-minute result
  indefinitely (acceptance 8). A regression test covers both kinds.
- **D4 — messages from `describe_ask_failure`.** One place for the CLI, MCP and UI (its own
  docstring names this ticket). The context-overflow sentence mentions `--limit`, which the tab
  does not offer; it is still true advice for the CLI and is kept rather than forked.
- **RM-18a.D1** graph modes not in v1; **RM-18a.D2** ask retention 10 minutes, unlisted.

## Tests (RED first)

1. `on_step` order for the loop and `no_loop`; `ask` forwards it; a raising `on_step` propagates
   out of `ask` (A4); default `None` changes nothing.
2. Runner on `run_next()`: succeeded with the payload (snippet cut, sensitivity, `elapsed_ms`,
   fallback keys); events in step order; cancel during retrieve → `cancelled` and no interaction
   row (with telemetry ON, beside a success that does write rows); chat down → `chat_unavailable`
   with the fixed message; every code of the mapping; a chat that advances the clock past the
   deadline → `timeout` at the next step; the chat and embed wrappers check the deadline; the
   embedder wrapper forwards the capability flags.
3. Routes (5434): config in each mode; 202 submit; validation 400s; 413 over the wire cap;
   `--read-only` → 403 `read_only`; non-loopback → 403 `ask_requires_loopback`; a confidential
   document cited only when bodies are served; the lens withholds a non-normal citation when
   bodies are not; asks absent from `GET /api/jobs`; gone from `GET /api/jobs/{id}` after 10
   minutes (injected registry clock) while an ingest job of the same age stays.
4. Security: the ask POST is refused without the Origin and without JSON, like every mutation.

Mutation proofs (in a `git archive` copy): the listing filter, the deadline check, the cancel
check before the interaction write, the lens withholding, the evict-on-get line, the
`exclude_confidential` argument.

## Risks

- An ask holds the single worker for up to the deadline; a queued ingest waits (A1). The front
  end (RM-18b) shows the queue state.
- An ask result keeps confidential bodies in memory for 10 minutes (A10).

## Evidence

### Mutation proofs (2026-10-30)

Run in a `git archive` copy of `c2235e3` (scratch directory, deleted after), with
`PYTHONDONTWRITEBYTECODE=1`, every `__pycache__` purged before each run, and `PYTHONPATH`
pinned to the copy's `src`; `brain` was checked by realpath to import from the copy. Each
mutation's anchor matched exactly once and the file was checked to differ before its tests
ran. **11 of 11 turned their tests red**, and the restored copy ran 70 passed.

| # | Mutation | Red test |
|---|---|---|
| M1 | drop the `kind != ASK_KIND` listing filter | `test_ask_jobs_are_left_out_of_the_job_list` |
| M2 | `Deadline.check` never raises | `test_a_chat_that_runs_past_the_deadline_ends_timeout_at_the_next_step` |
| M3 | drop the cancel check before the interaction rows | `test_a_cancel_during_synthesis_is_honoured_before_anything_is_recorded` |
| M4 | the citation lens never withholds | `test_the_lens_withholds_a_non_normal_citation_when_bodies_are_not_served` |
| M5 | `get()` no longer evicts | `test_get_evicts_a_job_past_its_retention_without_a_list_or_submit` |
| M6 | `exclude_confidential=False` in the runner | `test_a_confidential_document_is_cited_only_when_bodies_are_served[False]` |
| M7 | `hosted_egress` not forwarded | `test_the_step_guard_embedder_forwards_the_capability_flags` |
| M8 | no guard in the runner's `on_step` | the deadline test (the job now fails one step later) |
| M9 | no `_model` on the wrapper | `test_the_step_guard_embedder_keeps_the_backends_query_cache_identity_apart` |
| M10 | no `plan` step report in `ask` | `tests/test_ask_on_step.py` |
| M11 | the guarded chat skips its guard | `test_the_guarded_chat_checks_the_deadline_before_calling` |

### End to end (real uvicorn, middleware and worker; 5434 sibling database; scripted chat)

`brain ui` itself always uses the real `chat_json`, and this ticket may not call a live
Ollama, so the pass builds the app with `server.build_context` (the real registry, runtime,
middleware and worker thread) against `second_brain_test_rm18a`, swaps in a scripted
`ChatJson`, serves it with uvicorn on loopback, and drives it over HTTP. Two synthetic
documents: one `normal`, one `confidential`.

```
=== loopback, bodies served ===
GET /api/ask/config -> {'enabled': True, 'reason': None, 'max_question_chars': 2000, 'modes': ['hybrid']}
POST /api/ask -> 202 {'kind': 'ask', 'state': 'queued'}
events: [('queued', None, None), ('plan', 1, 3), ('retrieve', 1, 3), ('reflect', 1, 3), ('synthesize', 1, 3)]
state: succeeded error: None
answer: It ships in three waves [1]. Pricing is in [2]. Bogus.
citations: [(1, 'Sealed zorbel memo', 'confidential', False), (2, 'Zorbel rollout notes', 'normal', False)]
elapsed_ms int: True fallback: None
GET /api/jobs lists ask? False

=== bodies withheld ===
POST /api/ask -> 202 {'kind': 'ask', 'state': 'queued'}
events: [('queued', None, None), ('plan', 1, 3), ('retrieve', 1, 3), ('reflect', 1, 3), ('synthesize', 1, 3)]
state: succeeded error: None
answer: It ships in three waves [1]. Pricing is in. Bogus.
citations: [(1, 'Zorbel rollout notes', 'normal', False)]
GET /api/jobs lists ask? False

=== cancel during a slow plan step ===
POST /api/ask -> 202 {'kind': 'ask', 'state': 'queued'}
POST cancel -> 202 running
events: [('queued', None, None), ('plan', 1, 3)]
state: cancelled error: None

=== --read-only ===
config: {'enabled': False, 'reason': 'read_only', 'max_question_chars': 2000, 'modes': ['hybrid']}
POST -> 403 read_only
```

The out-of-range `[9]` (and `[2]` where only one document was retrieved) is dropped by
`ask._sanitize_answer`, as in the CLI.
