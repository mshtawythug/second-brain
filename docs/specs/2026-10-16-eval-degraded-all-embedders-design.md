# `brain eval` degraded-run handling for every embedder — design

**Date:** 2026-10-16
**Branch:** `fix/eval-degraded-all-embedders`
**Plan:** `docs/plans/2026-10-16-eval-degraded-all-embedders.md`
**Builds on:** `docs/specs/2026-10-15-eval-record-degraded-design.md` (PR #33)

## 1. What PR #33 left out

PR #33 made a *degraded* row (an embed call that failed, scored `0.0` without
measuring retrieval) unrecordable and ungateable. It defined "degraded" as
"`run_eval` caught `OllamaEmbedError`". That definition has two holes, and the
spec recorded both as out of scope:

1. **It is keyed on one backend's exception class.** `VoyageEmbedder.embed`
   wraps every SDK failure in `VoyageEmbedError`, a sibling of
   `OllamaEmbedError` under `brain.errors.EmbedError`. `run_eval` does not catch
   it, so under `BRAIN_EMBEDDER=voyage` one rate-limited query aborts the whole
   run with a traceback. The shared base exists for exactly this: its docstring
   already says "`brain eval`'s per-query tolerance" catches `EmbedError`. The
   docstring is false today; the code catches only the Ollama class.
2. **It cannot tell a retry from a misconfiguration.** Every `OllamaEmbedError`
   degrades the row, including the ones no retry will fix: model not pulled
   (HTTP 404), a malformed response. A run against a missing model therefore
   exits `0` with every row at `0.0` and advises "retry when the embedder is
   healthy (e.g. no other large model loaded in Ollama)". The record and gate
   refusals still fire, so nothing is poisoned, but the advice is wrong and the
   real cause is buried in N per-query log lines.

There are also two neighbouring runners the PR #33 spec named without deciding:
`graph_runner.py` and the answer eval (`brain eval --answer`).

## 2. Facts, re-derived from the code (2026-10-16, base `8900725`)

### 2.1 What each backend can raise from one `embed` call

| Backend | Raises | Wraps | Retries before raising? |
|---|---|---|---|
| `arctic`, `qwen3` (`_OllamaEmbedderBase._embed_batch`) | `OllamaEmbedError` | `httpx.HTTPStatusError` (any status), any other `httpx.HTTPError`, a non-JSON body (`ValueError`), a missing `embeddings` list, a count mismatch | No |
| `voyage` (`VoyageEmbedder.embed`) | `VoyageEmbedError` | every `voyageai.error.VoyageError` | **No.** `voyageai.Client(max_retries=0)` is the SDK default and `embeddings.py` does not pass `max_retries`. CLAUDE.md's "Voyage SDK retries handled in `embeddings.py`" is false; corrected in this change. |
| `none` (`NullEmbedder.embed`) | `EmbedDisabledError` | nothing failed | n/a — and **unreachable from `run_eval`**: `hybrid_search` coerces a backend with `produces_embeddings = False` to `fts_only` (`search.py`, `fts_only = fts_only or not getattr(...)`) and never calls `embed`. An eval under `none` is a measured FTS-only run, named in `config_signature["embedder"]`; it is not degraded. |

`voyageai` 0.5.0's own status mapping (`api_requestor.handle_error_response` /
`_interpret_response_line`): 400 `InvalidRequestError`, 401
`AuthenticationError`, 422 `MalformedRequestError`, 429 `RateLimitError`, 500
`ServerError`, 502/503/504 `ServiceUnavailableError`, a request timeout
`Timeout`, a transport failure `APIConnectionError`, anything else `APIError`.
The SDK's own retry predicate (`client._make_retry_controller`) is
`RateLimitError | ServiceUnavailableError | Timeout`.

A vector-dimension mismatch is **not** an embed failure. The embed call
succeeds; pgvector then rejects the query with a `psycopg` error, which is not
an `EmbedError` and already aborts the run. `brain init`'s
`ensure_embedding_column` is the guard for that. Unchanged here.

### 2.2 Transient vs permanent

One rule, applied to both transports by HTTP meaning rather than by vendor:

**Transient** (the same request may succeed later — degrade the row, say "retry"):

- a timeout — `httpx.TimeoutException`; Voyage `Timeout`;
- a transport failure — `httpx.NetworkError` (connect/read/write/close),
  `httpx.RemoteProtocolError` (server dropped the connection); Voyage
  `APIConnectionError`. "Ollama is not running" lands here, and "start it and
  retry" is the right advice;
- HTTP `408`, `429`, or any `5xx` — Ollama's `500` is what an out-of-memory
  model load returns, the observed cause in PR #33; Voyage `RateLimitError`,
  `ServerError`, `ServiceUnavailableError`, and `TryAgain` (the SDK names it).

**Permanent** (the same request will fail the same way — abort the run):

- any other `4xx` — Ollama `404` (model not pulled), `400`; Voyage
  `AuthenticationError` (bad key), `InvalidRequestError`,
  `MalformedRequestError`;
- a response the client cannot use — non-JSON body, no `embeddings` list, wrong
  vector count; Voyage `APIError`;
- an `httpx.HTTPError` that is not a timeout or network failure —
  `UnsupportedProtocol` (a host URL with no scheme), `ProxyError`,
  `LocalProtocolError`, decoding errors;
- `EmbedDisabledError` — the backend is off by configuration.

### 2.3 Is there a shared "transient" marker?

There is a shared base (`brain.errors.EmbedError`); there is no transience
marker anywhere. `run_eval` cannot classify after the fact without importing
`httpx` and `voyageai` and reading `__cause__`, which would put both vendors'
exception trees into the eval package. The backend already holds the precise
cause at the point it raises.

### 2.4 `graph_runner.py`

- **local** and **themes** never embed (`retrieve.py`: "local/themes stay
  embedder-free"); themes' optional Ollama group summary is off unless an
  enricher is injected, and `run_graph_eval` injects none. Themes are scored on
  entity keysets, never on the summary.
- **fuse** embeds through the hybrid leg, and `fuse._run_hybrid_leg` is
  *never-raise* by design: a failed query embed logs a warning and the leg
  re-runs FTS-only. The row is then scored against a weaker but real ranking.
  The fact is already exposed: `GraphExplanation.matched_filters
  ["hybrid_vector_arm_used"]`.
- It has **no CLI surface, no committed baseline, and no `--fail-below`**
  (its own module docstring: the graph cases are synthetic and the
  record/diff baseline is a canary that round-trips in tests). Its one caller,
  `tests/test_graphrag_retrieval_eval.py`, passes the deterministic
  `FakeEmbedder`, which cannot fail.

### 2.5 The answer eval (`brain eval --answer`)

- Each case calls `ask_no_loop`: one `hybrid_search` (an embed) and one chat
  synthesis. Scoring is local and deterministic.
- It has **no baseline and no gate**; the docstring says so ("no baselines —
  live-model gated").
- Its established failure policy is **abort the run, exit 1**:
  `_run_answer_eval_cli` maps `OllamaUnavailable` (a *transient* chat failure)
  and, since PR #32, `OllamaContextOverflow` to a clean exit 1.
- An `EmbedError` from the retrieval step is caught by nothing, so it escapes as
  a traceback. That is the same defect PR #32 fixed for `OllamaContextOverflow`.

## 3. Decisions

### 3.1 `EmbedError.transient`, set where the error is raised

`brain.errors.EmbedError` gains a keyword-only constructor argument and
attribute `transient: bool`, default **`False`**. Each backend sets it at its
raise sites from the cause it is wrapping (section 2.2):

- `_OllamaEmbedderBase._embed_batch`: `transient=True` for a
  `TimeoutException` / `NetworkError` / `RemoteProtocolError`, and for an
  `HTTPStatusError` whose status is `408`, `429` or `>= 500`. Every other raise
  site keeps the default.
- `VoyageEmbedder.embed`: `transient=True` when the SDK error is one of
  `RateLimitError`, `ServiceUnavailableError`, `Timeout`, `APIConnectionError`,
  `ServerError`, `TryAgain`.

**Why an attribute and not a subclass.** A `TransientEmbedError` mixin would
need a transient twin of every backend class (`OllamaTransientEmbedError(
OllamaEmbedError, TransientEmbedError)` …) so that existing `except
OllamaEmbedError` handlers keep matching. One boolean on the shared base costs
nothing at the existing `except EmbedError` sites, which do not read it.

**Why the default is `False`.** Degrading a row is a tolerance, and a tolerance
needs positive evidence. An embed error raised by code that did not classify it
(a future backend, a test double) aborts the run loudly, as every embed error
did before PR #33. Getting it wrong in that direction costs one re-run;
getting it wrong the other way prints "retry" advice for a misconfiguration.
Two test doubles that model a *timeout* (`tests/conftest.py`'s `FlakyEmbedder`
and one `side_effect` in `tests/test_eval_runner.py`) are therefore updated to
say `transient=True`. That is them stating what they model, not a weakened
assertion; a new test pins that an unclassified error aborts.

### 3.2 `run_eval` catches the base, degrades only the transient

```python
except EmbedError as exc:
    if not exc.transient:
        raise
    ...  # degraded row, exactly as PR #33
```

The `EmbedError` docstring's claim about `brain eval` becomes true. The
per-query `WARNING` names the exception class instead of hard-coding
`OllamaEmbedError`.

Consequences, by backend:

| Failure | Before | After |
|---|---|---|
| Ollama timeout / connection refused / 5xx | degraded row | degraded row (unchanged) |
| Ollama 404 model missing, bad response shape | degraded row, "retry" advice | **run aborts, exit 1**, message names the error |
| Voyage rate limit / timeout / 5xx / connection | **traceback, run lost** | degraded row; record/gate refuse as PR #33 |
| Voyage bad key / invalid request | traceback | **clean exit 1**, message names the error |
| `none` | FTS-only run, not degraded | unchanged |

PR #33's whole contract holds for the newly degraded Voyage rows with no new
code: `degraded_results`, `save_baseline`'s refusal, `diff_reports`'s
exclusion and the `--fail-below` refusal all read `EvalResult.degraded`.

### 3.3 The CLI maps an aborted run to a clean exit 1

`eval_cmd` catches `EmbedError` around `run_eval` (only permanent ones reach it)
and `_run_answer_eval_cli` catches `EmbedError` around `run_answer_eval` (both
kinds reach it, section 3.4). Each prints one red stderr line and exits `1`. The
text comes from `brain.eval.runner.embed_failure_message`, so `cli.py` holds no
policy:

- transient: "the embedder failed with a retryable error … the run stopped and
  no report was written; retry when the embedder is healthy";
- permanent: "the embedder failed with an error a retry will not fix … the run
  stopped and no report or baseline was written. Fix the cause it names …".
  The advice defers to the backend's own message instead of listing one fix,
  because some permanent errors carry a precise one: section 3.6's overflow
  names `BRAIN_OLLAMA_EMBED_NUM_CTX`.

Both name the exception class and carry the backend's message. Neither
interpolates query text: the message has no access to it, and the backend
messages are built from the HTTP status, the response body and the transport
error, never the request input. The exit-code contract is unchanged (`1`
generic / `2` `BadParameter` / `3` regression): an aborted run is "the command
could not do what it was asked".

`degraded_run_message`'s advice becomes backend-neutral: "(e.g. no other large
model loaded in Ollama, or a hosted backend's rate limit has cleared)".

### 3.4 Answer eval: abort cleanly, do not add per-case tolerance

The minimal correct extension is the clean exit, not a `degraded` flag on
`AnswerScore`:

- a degraded answer row would have no consumer to protect — there is no
  baseline to poison and no gate to fool;
- the runner already aborts on a *transient* chat failure (`OllamaUnavailable`);
  tolerating a transient embed failure per case while a transient chat failure
  kills the run would make the policy depend on which half of `ask` failed;
- the defect actually present is a traceback for an operational failure.

### 3.5 `graph_runner`: out of scope, with the reason

No CLI, no committed baseline, no gate, one caller with an embedder that cannot
fail, and a fuse leg whose never-raise fallback is a design decision of
`brain.graph_rag` (spec §17d Q1), not an eval bug. There is nothing a degraded
flag would currently protect. **If** a CLI surface or committed baseline is ever
added, the signal already exists: mark a fuse row degraded when an embedder was
passed and `ctx.explanation.matched_filters["hybrid_vector_arm_used"]` is
`False`. Recorded in `graph_runner.py`'s module docstring so the next author
finds it there.

### 3.6 `OllamaEmbedContextOverflow` (PR #36): permanent, so the run aborts

PR #36 (branch `perf/ollama-embed-num-ctx`, unmerged when this was written)
sends `truncate: false` and its own `num_ctx` (`BRAIN_OLLAMA_EMBED_NUM_CTX`) on
every `/api/embed`. An input longer than that window is then refused with HTTP
400 ("the input length exceeds the context length") and raised as
`brain.embeddings.OllamaEmbedContextOverflow(OllamaEmbedError)`. Its message
already names the knob: "Raise BRAIN_OLLAMA_EMBED_NUM_CTX or shorten the input".

**Decision: abort, not degraded-with-different-advice.**

- **It is deterministic per query.** The same query text overflows the same
  window on every run. A degraded row would therefore make `--record-baseline`
  refuse forever and `--fail-below` refuse to judge forever, each time telling
  the operator to retry, which can never work. "Degraded" means "this
  measurement was unlucky", and an overflow is not luck.
- **The fix is a configuration change, and the run should not end without it.**
  A plain run that degraded the row would exit 0 with that query at `0.0`
  inside the means. That is the silent mis-measurement PR #33 set out to end;
  only a stderr count line would distinguish it from a real miss.
- **A "degraded but not retryable" state would be a third state** across
  `EvalResult`, `degraded_results`, `save_baseline`, `diff_reports`, the
  `--fail-below` refusal and every message. Each of them would need a second
  wording, and the only payoff is keeping the other N-1 rows of a run whose
  configuration has to change anyway.
- **The cost is low.** Golden-corpus queries are one-line searches. An overflow
  means the knob is set below a query's length, which is a misconfiguration to
  surface at once, not to work around row by row.

**No new code is needed for this.** `OllamaEmbedContextOverflow` is
constructed without `transient=`, so it takes the default `False`. Its status
(400) is outside the retryable set, so the generic branch would classify it
the same way. `run_eval` re-raises it. `eval_cmd` and `eval --answer` print
`embed_failure_message`, which carries the exception's own text, so the line
names `BRAIN_OLLAMA_EMBED_NUM_CTX`. That is why the permanent advice says "fix
the cause it names" and does not list a fixed set of causes (section 3.3).

**After #36 merges** (master merged into this branch before anything
references the class):

1. Resolve the expected conflict in `_OllamaEmbedderBase._embed_batch`'s
   `except httpx.HTTPStatusError` block. Keep #36's overflow branch unchanged
   (no `transient=`, so permanent) ahead of this change's generic raise, which
   keeps `transient=_is_transient_status(...)`.
2. Add tests. (a) A real `ArcticEmbedder` answering 400 with the overflow body
   raises `OllamaEmbedContextOverflow` with `transient is False`. (b)
   `run_eval` re-raises it rather than degrading the row. (c) `brain eval`
   exits 1 via `SystemExit`, and the stderr line names
   `BRAIN_OLLAMA_EMBED_NUM_CTX` and not the query text.
3. Add a mutation: construct the overflow error with `transient=True`. Tests
   (a)–(c) must go red.

## 4. Where the code goes, and the ceiling

| File | Change | Ceiling |
|---|---|---|
| `src/brain/errors.py` | `EmbedError.__init__(…, transient=False)`; docstring | under 800 |
| `src/brain/embeddings.py` | classify at the Ollama and Voyage raise sites; `EmbedDisabledError` docstring corrected (eval no longer "catches it uniformly") | under 800 |
| `src/brain/eval/runner.py` | catch `EmbedError`, re-raise permanent; `embed_failure_message` | under 800 |
| `src/brain/eval/baseline.py` | backend-neutral advice in `degraded_run_message` | under 800 |
| `src/brain/eval/graph_runner.py` | docstring: why no degraded flag, and the signal if one is needed | under 800 |
| `src/brain/cli.py` | two `except EmbedError` mappings | **over, in the table** |

`cli.py` growth follows CLAUDE.md exactly: an inline why-comment at both
sites, a sixth-growth paragraph in the module docstring, and in the CLAUDE.md
row the trailing descriptive hop ("the merge commit that added this clause",
master into `fix/eval-record-refuses-degraded`) is **promoted** to
`9,336 (d22c354)` before a new descriptive hop is added for this change.

## 5. Not done, and why

- **Retrying inside `run_eval` or turning on Voyage SDK retries.** The PR #33
  reasoning holds: a retry policy is a timeout policy, and the observed cause
  held the GPU for minutes. Enabling `max_retries` would also change ingest and
  search for every Voyage user, which is not an eval decision.
- **Classifying by `__cause__` inside the eval package.** Rejected in 3.1: it
  would import two vendors' exception trees into `brain.eval`.
- **A degraded flag for the answer eval or the graph runner.** Sections 3.4 and
  3.5.
- **Dimension mismatches.** Not an embed failure (section 2.1); already aborts.
