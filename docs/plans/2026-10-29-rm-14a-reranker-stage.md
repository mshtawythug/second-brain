# RM-14a — The reranker stage, backends, settings and eval wiring (default OFF)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-14a in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 4; BL-5; backlog rank 7). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `32380e6` (RM-01..RM-10 and RM-12 merged).

## Goal

A second-stage cross-encoder reranker that reorders the head of every user-facing search, built
behind RM-09's pipeline and shipped **off**. With `BRAIN_RERANKER` unset nothing changes: every
surface's output and `brain eval`'s `config_signature` are byte-identical, so `ci.json` is not
re-recorded. Installing the runtime is RM-14b; measuring and adopting is RM-14c.

## Files

| File | Change |
|---|---|
| `src/brain/settings/rerank.py` (new leaf) | `RerankSettings` (frozen) + `load_rerank_settings(env)`. Knobs: `BRAIN_RERANKER` (`none`/`llamacpp`/`voyage`), `BRAIN_RERANK_DEPTH` (20, 2..50), `BRAIN_RERANK_TIMEOUT_S` (5.0), `BRAIN_RERANK_URL` (`http://127.0.0.1:8081`, loopback only), `BRAIN_RERANK_MODEL`, `BRAIN_RERANK_MAX_CHARS` (1,500). `voyage_api_key` from `VOYAGE_API_KEY`, `repr=False`. Constants `LLAMACPP_RERANK_DEFAULT_MODEL = "bge-reranker-v2-m3"`, `VOYAGE_RERANK_DEFAULT_MODEL = "rerank-3"`. |
| `src/brain/rerank.py` (new leaf) | `Reranker` Protocol, `RerankError`, `RerankStage` (implements `SearchStage`), the confidential-head lookup, `rerank_notice(reports)`. |
| `src/brain/rerank_backends.py` (new leaf) | `LlamaCppReranker` (httpx, `POST {url}/v1/rerank`), `VoyageReranker` (`voyageai.Client(max_retries=0, timeout=...)`), `make_reranker(settings)`. Both take an injected client for tests. |
| `src/brain/search_pipeline.py` | `build_stages(env, *, rerank=None)` adds `RerankStage` when the backend is not `none` (`rerank=True/False` overrides for one run). `_sized` becomes public `size_stages` (eval sizes its fetch with it). `SearchStage.name` becomes a read-only property in the Protocol so a frozen dataclass stage conforms (typing-only). |
| `src/brain/eval/runner.py` | `run_eval(..., stages=None, env=None)`: fetches `size_stages`' depth, runs `apply_stages` after its own `hybrid_search` (never `run_search`), truncates to `limit_per_query`, records `EvalResult.stage_ms`, merges each stage's `signature()` into `config_signature`. A skipped stage is logged by name and class only. |
| `src/brain/eval/baseline.py` | `save_baseline` drops `stage_ms` from each row, like `degraded`: latency is not a property of the corpus, and leaving it out keeps the baseline format (and `ci.json`'s byte layout) unchanged. |
| `src/brain/cli_search.py` | `--rerank/--no-rerank` on `search` and `explain`; the stage list for one run; the one-line stderr notice when the rerank stage was skipped; the footer gets the stage timings. Budget +≤12 (§2.2). |
| `src/brain/format_search.py` | `search_meta_line(..., stages=())` appends `<stage> Nms` before `total`; `timing_with_stages(meta, reports)` adds each stage's `ms` to `timing_ms` (used by the UI route only). |
| `src/brain/ui/routes_search.py` | Passes the stage reports into `timing_with_stages`. No stage, no key: the response is unchanged when off. |
| `src/brain/ui/static/js/results.js` | Meta line shows `rerank 0.4` when `timing_ms.rerank` is present. |
| `src/brain/settings/__init__.py` | Docstring: the `run_eval` fallback is now built. |
| Tests (new) | `tests/test_settings_rerank.py`, `tests/test_rerank_stage.py`, `tests/test_rerank_backends.py`, `tests/test_rerank_surfaces.py`, `tests/test_eval_runner_stages.py`, `tests/rerank_fakes.py`. |
| Tests (changed) | `tests/test_eval_runner.py`, `tests/test_eval_harness_live.py`, `tests/test_search_fallback_eval_untouched.py`: every `run_eval` call passes `stages=()` (AC8). `tests/test_search_pipeline.py`: `build_stages` is no longer empty for a configured backend. |
| `docs/configuration.md`, `docs/cli-reference.md`, `CHANGELOG.md`, `skills/consult-brain/SKILL.md` (only if under its line ceiling) | Knobs, flags, Unreleased entry. |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | Item 5 / BL-5 / rank 7: stage built, off (RM-14a, PR #76); runtime RM-14b; measurement RM-14c. Not SHIPPED: RM-14c decides. |
| Spec Appendix B | Follow-up commit once the PR number is known: RM-14a → `DONE (#PR)`. |

No over-ceiling file is touched (`search.py`, `cli.py`, `mcp_server.py`, `config.py` unchanged), so
no ceiling-table row moves. `search.py:104`'s comment ("Q3-A will populate; today always None") is
left alone on purpose: editing it would cost a ceiling hop for a comment, and with the default
`none` it is still true.

## Design (the ticket's, with the choices it leaves open)

- **Stage.** `candidate_limit(limit) = max(limit, depth)`. `apply` takes the first `depth` rows,
  builds each passage as `title + "\n" + snippet` cut to `max_chars`, scores them, and reorders the
  head by score, descending; ties keep their prior order (stable sort). The tail keeps its order.
  `signature()` = `{"reranker", "reranker_model", "rerank_depth"}`. `score` is not overwritten
  (D1); `explain.reranker_score` is set through `dataclasses.replace` when `explain` is present.
- **Egress.** When `reranker.hosted_egress`, one `SELECT id::text FROM documents WHERE id = ANY(%s)
  AND sensitivity = %s` finds the confidential rows in the head; they are not sent, and each keeps
  its index. The scored rows fill the remaining head slots in score order.
- **Nothing to score** (empty head, or every head row confidential): the backend is not called, the
  rows pass through, and the report is `applied=True`: nothing failed, so there is no notice.
- **Failure.** Backends raise `RerankError` (a `BrainError`) chained from the transport or API error,
  and for a malformed response (wrong count, a missing or duplicate index, a non-finite score). The
  stage turns it into `applied=False` with `describe_failure(exc)` (`RerankError from ReadTimeout`).
  Anything else falls to `apply_stages`' broad catch, which also skips. Search never fails.
- **CLI notice.** One stderr line, `reranker unavailable; results are in hybrid order`, when the
  `rerank` report is not applied. Exit 0.
- **`--rerank/--no-rerank`.** Unset: the setting (`stages=None`, the pipeline default).
  `--no-rerank`: no rerank stage. `--rerank` with `BRAIN_RERANKER=none` is a `ConfigError`
  (exit 2 through `BrainGroup`, like any bad knob): picking a backend silently would choose between
  a local service and a hosted one for the user.
- **Settings errors.** A bad rerank knob raises `ConfigError` wherever the stages are built: on the
  CLI that is the usual clean exit 2; MCP and the UI fail that search loudly rather than dropping a
  configured stage silently. Validated even while the backend is `none`, as `config.py` knobs are.
  `BRAIN_RERANKER=voyage` without `VOYAGE_API_KEY` is a `ConfigError`.
- **Loopback.** `BRAIN_RERANK_URL` must be `http`/`https` with host `127.0.0.1`, `::1` or
  `localhost`; anything else is a `ConfigError` naming only the variable.
- **Import cycle.** `rerank.py` imports `StageOutcome`/`StageReport` from `search_pipeline`, so
  `build_stages` imports the rerank modules inside the function.
- **Eval.** `stages=None` → `build_stages(env if env is not None else os.environ)` (§2.3's
  carve-out). Fetch `size_stages(stages, limit_per_query)`, then `apply_stages`, then truncate. With
  no stage the fetch is `limit_per_query` and the signature gains nothing (AC1). A transient embed
  failure is still a degraded row; stages are not run for it (§2.7).
- **`EvalResult.stage_ms`.** `{report.name: report.ms}`; `{}` with no stage. It appears in
  `brain eval --json` (an additive key, which is how RM-14c reads latency) and is not written into
  baselines (above).
- **UI.** `timing_ms` gains one key per stage report, and only then; with no stage the response is
  byte-identical. The JS shows `rerank` beside `embed` and `rank`.
- **MCP.** Follows the setting (D2): `brain_search` already carries `stages`, and `brain_explain`
  shows `reranker_score`. No MCP file changes.

## Decisions (recorded deviations)

1. `SearchStage.name` becomes a read-only property in the Protocol so `RerankStage` can be a frozen
   dataclass as the ticket specifies (mypy rejects a frozen attribute for a settable member).
   Typing only; every existing stage still conforms.
2. `stage_ms` is not serialized into baselines (above). The ticket says each `EvalResult` gains it;
   it does, in memory and in `brain eval --json`.
3. `--rerank` with `BRAIN_RERANKER=none` refuses rather than guessing a backend.
4. **The footer's and `brain ui`'s total include each stage's time; the `--json --meta` envelope's
   `timing_ms.total` does not.** `SearchDiagnostics.total_ms` is `hybrid_search`'s own clock, and a
   stage runs after it, so the end-to-end run printed `rerank 32ms · total 8ms`. The two places a
   person reads a headline total (the footer, the Notes tab's seconds) add the stages' `ms`
   (`format_search.search_meta_line`, `timing_with_stages`). The envelope and MCP keep
   `timing_ms.total` as the search's own time, because `search_meta_json` is shared with MCP and
   changing it would move a machine-read field; their `stages[].ms` already carries the stage's
   time, so nothing is lost.
5. **`skills/consult-brain/SKILL.md` is unchanged** (DoD 13 names "the relevant skill"). The stage
   ships off, adds no MCP tool or parameter (D2), and the skill is at the house 300-line ceiling
   (`tests/test_skill_frontmatter.py`); RM-14c, if it adopts, is where agents need to hear of it.
6. **The backlog rows read "IN PROGRESS: built, off", not SHIPPED** (DoD 12 closes the row in the
   ticket that finishes it). The reranker is not on until RM-14c's measurement decides, so closing
   BL-5 here would claim a ranking change nobody has measured.
7. **An eval row whose stage SKIPPED is refused like a degraded row** (rev-76, ruled in this PR,
   not deferred to RM-14c). A reranker that times out for one query leaves that row in hybrid
   order while `config_signature` names the reranker, so `--record-baseline` would write hybrid
   numbers under a reranker signature: PR #33's failure mode by another route. `EvalResult` gains
   `stages_skipped` (in `brain eval --json`, never in a baseline), and `stage_ms` times applied
   stages only. `save_baseline` raises `StageSkippedBaselineError`, a `DegradedBaselineError`
   subclass, so `eval_cmd`'s existing mapping exits 1, with its own message
   (`stage_skipped_run_message`). `diff_reports` puts a shared row with a skip into
   `degraded_queries`, which `eval_cmd` already refuses under `--fail-below` (exit 1).
   **`cli.py` is not edited**, and that fixes one wording choice: `eval_cmd` passes
   `degraded_run_message` counts only, so its default wording is now cause-neutral (a failed
   embed OR a skipped stage). The skip-specific variant is used where the cause is known
   (`save_baseline`). `degraded_results` keeps meaning "the embed call failed", because the
   CLI's ad-hoc warning says those rows "counted as 0.0", which a skipped row is not; skipped
   rows have their own `skipped_stage_results`. **Known gap:** a plain `brain eval` or `--diff`
   run whose only unusable rows are skips prints no count line on stderr (`cli.py`'s two warnings
   read `degraded_results`); the per-query WARNING log and `stages_skipped` in `--json` still show
   them. Closing it needs an `eval_cmd` edit, out of scope here; RM-14c, the ticket that runs the
   reranker under `brain eval`, is where to close it.
8. **Test isolation** (rev-76). `run_search(stages=None)` and `run_eval(stages=None, env=None)`
   read `os.environ`, so a shell-exported or `.env` `BRAIN_RERANKER` reached surface tests. A
   function-scoped autouse fixture, `_reranker_off`, sets `BRAIN_RERANKER=none` (set rather than
   deleted, because `Config.load()`'s `setdefault` would re-inject a `.env` value) and deletes the
   five other knobs. It lives in the plugin module `tests/rerank_isolation_plugin.py`, registered
   in `tests/conftest.py`'s `pytest_plugins` (the `tests/env_isolation.py` precedent), because
   conftest is over the 800-line ceiling: conftest grows by 3 lines, the entry, its one-line
   comment and its module-docstring growth record. `tests/test_rerank_isolation.py` runs a
   `nodb` probe in a child pytest that exports all six variables, and asserts the fixture is
   active in the suite (a dropped registration fails by name).

## Tests (TDD: each lands red first)

1. Settings: defaults; each knob's bounds; non-loopback URL (`http://10.0.0.5:8081`,
   `http://example.com`, a `file:` URL) is a `ConfigError`; voyage without a key; the key is not in
   `repr`; model defaults per backend.
2. Stage (pure, fake reranker, `conn=None` when not hosted): head reorder, ties stable, tail kept,
   depth > rows, `max_chars` cut, passage shape, signature, `candidate_limit`, `reranker_score` set
   only when `explain` exists, `score` untouched, failure → unchanged + class-only reason (the
   secret message never appears), malformed score count, nothing to score.
3. Egress (test DB): a hosted fake never receives the confidential passage, and confidential rows
   keep their indices; a local fake does receive it. Mutation-proven.
4. Backends: `httpx.MockTransport` against the documented llama.cpp request/response; out-of-order
   results mapped by index; HTTP 500, timeout, malformed JSON, missing index → `RerankError`. Voyage
   with a fake client: arguments (`model`, `top_k=None`), mapping by index, `VoyageError` →
   `RerankError`; the real constructor gets `max_retries=0` and the timeout.
5. `build_stages`: `{}` → `()`; llamacpp → one `RerankStage`; `rerank=False` → `()`; `rerank=True`
   with none → `ConfigError`.
6. Surfaces (test DB, a real `RerankStage` over a fake reranker installed in `build_stages`):
   `brain search`, `brain explain` (score set), MCP `brain_search`, MCP `brain_explain`,
   `GET /api/search` (plus its `timing_ms.rerank`), `brain recall`, `ask`'s retrieval. A timing-out
   fake: hybrid order, one stderr line, exit 0. `--no-rerank` drops the stage; `--rerank` with none
   exits 2. The footer shows `rerank`.
7. Eval: `run_eval(stages=[fake])` scores the reranked order and records `stage_ms`; a transient
   embed failure is still degraded; `stages=None, env={...llamacpp}` builds the stage and the
   signature gains the three keys; `stages=()` signature equals master's four keys; an exported
   `BRAIN_RERANKER` does not reach a test that passes `stages=()`; `save_baseline` omits `stage_ms`.
8. Unchanged: `tests/test_search_output_unchanged.py` green; with no stage the UI response has no
   `timing_ms.rerank`.

## Risks

- **The llama.cpp rerank payload changing.** The backend test is pinned to the README's documented
  shape (checked 2026-10-29: `POST /v1/rerank` with `query`, `documents`, `top_n`; `results[]` of
  `index`, `relevance_score`). RM-14b's guide pins the tested version.
- **A reranker on a hosted backend leaking a confidential passage.** The egress filter is
  mutation-proven in a `git archive` copy.
- **Byte drift while off.** The surfaces' unchanged tests and a signature test pin it.

## Eval

None: the stage is off, `ci.json` is untouched. Measuring is RM-14c.
