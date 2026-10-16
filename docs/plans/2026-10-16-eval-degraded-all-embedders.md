# `brain eval` degraded-run handling for every embedder — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-16-eval-degraded-all-embedders-design.md`
**Branch:** `fix/eval-degraded-all-embedders`

**Goal:** a transient embed failure degrades the row on every backend (Voyage
included); a permanent one aborts the run with a clean exit 1; the answer eval
stops escaping embed failures as tracebacks.

**Test doubles (no monkey-patching of production code):**
- the **real** `ArcticEmbedder` over an `httpx.Client` with an
  `httpx.MockTransport` handler that raises a transport error or returns a
  status for named texts and otherwise answers with `FakeEmbedder` vectors;
- the **real** `VoyageEmbedder` with an injected fake SDK client (`client=`)
  that raises a named `voyageai.error` class for named texts;
- conftest's `FlakyEmbedder`, now raising a *transient* `OllamaEmbedError`.

Failing query strings are unique per test: `brain.search` caches query embeds
per embedder identity.

## Task 1 — classify at the raise site (RED → GREEN)

Files: `src/brain/errors.py`, `src/brain/embeddings.py`,
`tests/test_embed_error_transience.py` (new).

1. RED: Ollama — `ReadTimeout`, `ConnectError`, `RemoteProtocolError`, HTTP
   408/429/500/503 → `OllamaEmbedError.transient is True`; HTTP 400/401/404,
   non-JSON 200, missing `embeddings`, wrong count, `UnsupportedProtocol` →
   `False`. Voyage — `RateLimitError`, `ServiceUnavailableError`, `Timeout`,
   `APIConnectionError`, `ServerError`, `TryAgain` → `True`;
   `AuthenticationError`, `InvalidRequestError`, `MalformedRequestError`,
   `APIError` → `False`. `EmbedError("x").transient is False`;
   `NullEmbedder().embed` → `EmbedDisabledError` with `transient is False`.
2. GREEN: `EmbedError.__init__(*args, transient=False)`; the two classifiers.

## Task 2 — `run_eval` degrades only the transient (RED → GREEN)

Files: `src/brain/eval/runner.py`, `tests/test_eval_runner.py`,
`tests/conftest.py`.

1. RED (rule-12 regression, gap 1): a real `VoyageEmbedder` whose client raises
   `RateLimitError` for one query → that row `degraded`, the other healthy.
   Today `VoyageEmbedError` escapes `run_eval`.
2. RED (gap 2): a real `ArcticEmbedder` answering HTTP 404 → `run_eval` raises
   `OllamaEmbedError` with `transient is False`. Today the row degrades.
3. RED: an unclassified `EmbedError` subclass aborts the run.
4. Keep green: a real `ArcticEmbedder` timing out → degraded (PR #33's case at
   the real-backend level).
5. GREEN: `except EmbedError` + `if not exc.transient: raise`; log names the
   class. `FlakyEmbedder` and the one `side_effect` in
   `test_run_eval_handles_ollama_failure_gracefully` say `transient=True`.
6. `embed_failure_message(exc, *, command)`; unit-test both wordings and that
   the class name appears.

## Task 3 — CLI mappings (RED → GREEN)

Files: `src/brain/cli.py`, `tests/test_cli_eval.py`, `tests/test_cli_ask.py`.

1. RED: `eval --record-baseline` with a Voyage rate limit → exit 1 via
   `SystemExit`, the PR #33 degraded refusal, nothing written. Today:
   escaped `VoyageEmbedError`.
2. RED: plain `eval --json` with a Voyage rate limit → exit 0, one degraded row,
   count warning. Today: exit 1.
3. RED: plain `eval` with an Ollama 404 → exit 1 via `SystemExit`, the
   permanent message, failing query text absent from it. Today: exit 0.
4. RED: `eval --answer` with an embedder raising a transient and a permanent
   `EmbedError` → exit 1 via `SystemExit`, the matching message. Today:
   traceback.
5. GREEN: the two `except EmbedError` blocks, inline why-comments, module
   docstring sixth growth.

## Task 4 — docs

- `degraded_run_message` backend-neutral advice (+ test).
- `graph_runner.py` docstring: why no degraded flag; the signal.
- `EmbedError` / `EmbedDisabledError` docstrings.
- CHANGELOG `[Unreleased]` → Fixed.
- `tests/eval/baselines/README.md` degraded section: any backend, transient
  only; a permanent error exits 1 without a report.
- CLAUDE.md: eval paragraph; the false "Voyage SDK retries handled" line; the
  `cli.py` row — promote `d22c354` (9,336) then add a descriptive hop.

## Task 4b — after PR #36 merges (spec section 3.6)

Merge master in; keep #36's overflow raise (permanent by default) ahead of the
generic status raise. Add the three overflow tests (classification, `run_eval`
abort, CLI exit 1 naming `BRAIN_OLLAMA_EMBED_NUM_CTX`) and mutation M15
(`transient=True` on the overflow error → red).

## Task 5 — verify

`ruff check`, `mypy src/`, and in brain-ci's env against sibling DB
`second_brain_test_evaldeg`:

- **plan set:** `tests/test_eval_*.py tests/test_embeddings*.py
  tests/test_cli_eval.py tests/test_cli_ask.py tests/test_embed_error_transience.py
  tests/test_arctic_embedder.py tests/test_qwen3_embedder.py
  tests/test_graphrag_retrieval_eval.py`;
- **extended set:** the plan set plus `tests/test_make_embedder.py
  tests/test_import_hygiene.py tests/test_mcp_server.py` (other `EmbedError`
  raisers and consumers) and, after the PR #36 merge,
  `tests/test_ollama_embed_num_ctx.py tests/test_ollama_num_ctx.py`.

The report states which set a count belongs to.
Mutation catalog: every new guard, five clauses.

### Mutation catalog

Each mutation replaces one exact text, which must occur exactly the declared
number of times, and is run against the selected tests: the classification
file, the new `test_eval_runner` tests, and the new `test_cli_eval` /
`test_cli_ask` tests. Five clauses per mutation:

1. the tests pass unmutated;
2. the mutation lands its declared count;
3. the tests go red;
4. the needle appears on a `>` / `E` / `FAILED` line;
5. restored → green.

The harness runs on a copy of the tree, with `PYTHONPATH` pointed at it and
`PYTHONDONTWRITEBYTECODE=1`.

| ID | Target (file: guard) | Mutation | Count | Expected red (at least) |
|---|---|---|---|---|
| M1 | `eval/runner.py`: permanent re-raise | delete `if not exc.transient: raise` | 1 | runner aborts on Voyage 401 / Ollama 404 / unclassified / overflow; CLI 404 and Voyage-401 aborts |
| M2 | `eval/runner.py`: catch the base | `EmbedError` → `OllamaEmbedError` | 1 | runner and CLI Voyage rate-limit degrade tests |
| M3 | `errors.py`: default permanent | `transient: bool = False` → `True` | 1 | unclassified-is-permanent, unusable-response, disabled-backend, overflow |
| M4 | `embeddings.py`: Ollama timeout transient | drop `httpx.TimeoutException` | 1 | `[ReadTimeout]`, `[ConnectTimeout]`, runner Ollama timeout |
| M5 | `embeddings.py`: Ollama network error transient | drop `httpx.NetworkError` | 1 | `[ConnectError]`, `[ReadError]` |
| M6 | `embeddings.py`: 5xx boundary | `>= 500` → `> 500` | 1 | `test_ollama_retryable_failures_are_transient[500]` |
| M7 | `embeddings.py`: 429 transient | `{408, 429}` → `{408}` | 1 | `test_ollama_retryable_failures_are_transient[429]` |
| M8 | `embeddings.py`: Voyage rate limit by class | drop `"RateLimitError"` | 1 | Voyage RateLimitError classification; runner and CLI Voyage degrade tests |
| M9 | `embeddings.py`: Ollama status classification wired | `transient=_is_transient_status(...)` → `False` | 1 | Ollama 408/429/5xx classification |
| M10 | `embeddings.py`: Voyage classification wired | `transient=_is_transient_voyage_error(e)` → `False` | 1 | every Voyage transient classification; runner and CLI Voyage degrade |
| M11 | `cli.py`: `eval_cmd` maps a permanent failure | `except EmbedError` → `except ZeroDivisionError` | 1 | CLI Ollama 404, Voyage 401, overflow abort tests |
| M12 | `cli.py`: `eval --answer` maps an embed failure | `except EmbedError` → `except ZeroDivisionError` | 1 | `test_cli_eval_answer_embed_failure_exits_cleanly[*]` |
| M13 | `eval/runner.py`: advice follows the kind | `if exc.transient:` → `if not exc.transient:` | 1 | message wording test; answer-eval test; CLI abort tests |
| M14 | `eval/baseline.py`: backend-neutral advice | delete the "hosted backend's rate limit" clause | 1 | `test_plain_run_keeps_a_voyage_degraded_run` |
| M15 | `embeddings.py`: overflow is permanent | add `transient=True` to the `OllamaEmbedContextOverflow(...)` call | 1 | overflow classification, runner abort, CLI abort |
| M16 | `embeddings.py`: Voyage untyped status | delete the `http_status` fallback in `_is_transient_voyage_error` | 1 | `test_voyage_untyped_api_error_is_classified_by_status[408/429/501/507/520]` |
| M17 | `eval/runner.py`: the WARNING names the backend class | `"%s for query …", type(exc).__name__` → hard-coded `"OllamaEmbedError for query …"` | 1 | `test_run_eval_degrades_a_voyage_rate_limited_query` (caplog) |
| M18 | `errors.py`: `transient` is keyword-only | `(self, *args, transient=False)` → `(self, msg=None, transient=False)` | 1 | `test_transient_is_keyword_only_and_kept` |

Harness: `mut.py` in the author's session scratchpad. It is not checked in;
this table is its specification.
