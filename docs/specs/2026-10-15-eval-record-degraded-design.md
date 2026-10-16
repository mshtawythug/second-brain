# `brain eval` must not record, or gate on, a degraded run — design

**Date:** 2026-10-15
**Branch:** `fix/eval-record-refuses-degraded`
**Plan:** `docs/plans/2026-10-15-eval-record-degraded.md`

## 1. The bug

`run_eval` (`src/brain/eval/runner.py`) tolerates
`brain.embeddings.OllamaEmbedError`: when a query's embed call fails, it logs a
warning, records `actual_doc_ids=[]`, and scores that query `0.0` on all three
metrics. The run then exits `0` like any other.

That tolerance is right for an ad-hoc run: one slow embed should not cost the
operator the other N-1 rows. It is wrong for the two paths whose output is
treated as a reference:

1. **`--record-baseline NAME` saves the `0.0` rows as real scores.** Observed
   during a re-record: one query's embed call timed out because another process
   had a large model loaded in Ollama. The run exited `0` and wrote that query as
   `0.0`. Committed, the baseline's means would have read 0.2546 / 0.3503 /
   0.6825 (nDCG@5 / MRR / Recall@20) instead of the true 0.2724 / 0.3826 /
   0.7089. Every later `--fail-below` run would then compare against a corrupted
   reference: a real regression on that query is hidden, and its recovery reads
   as an improvement.
2. **`--diff --fail-below` gates on the `0.0` rows.** A degraded *current* run
   scores a well-ranked query `0.0`, the mean drops, and the gate exits `3`,
   reporting a retrieval regression that is really an embedder timeout. The
   inverse is worse: if the excluded rows were simply dropped, a run where
   *every* embed failed would compare an empty set, show zero deltas, and pass.

Nothing in the report said a query was degraded. `EvalResult` had no field for
it; the only evidence was a `WARNING` log line, which `--json` consumers and
anyone not watching stderr never see.

## 2. Representation

`EvalResult` gains `degraded: bool = False`. `run_eval` sets it `True` exactly
when it caught `OllamaEmbedError` for that query; the metrics stay `0.0` so an
ad-hoc run's arithmetic is unchanged. The default keeps every existing
constructor call and every committed baseline valid.

A helper `degraded_results(report) -> list[EvalResult]` in `brain.eval.runner`
is the one place that answers "which rows are degraded", so the record refusal,
the diff exclusion and the stderr warnings cannot disagree about the definition.

**The baseline file does not carry the flag.** `save_baseline` refuses a
degraded report (section 3), so in a saved file the flag would always be
`false`: a constant that only adds a line per query to the next `ci.json`
re-record. `save_baseline` strips it, and `load_baseline` builds every row with
the default. The file format is therefore unchanged, and so is `ci.json`'s
byte layout on its next honest re-record. `--json` report output (`asdict`) does
carry the flag, because there it is information.

Only `OllamaEmbedError` is caught today, so only it degrades a row. Voyage
errors and every other exception still propagate and fail the run loudly. That
is a pre-existing asymmetry, not something this change widens; it is noted, not
fixed, here.

## 3. Behaviour

| Mode | Degraded rows present | Exit | Writes / reports |
|---|---|---|---|
| plain run | yes | `0` (unchanged) | report as before; **new:** stderr warning with the count |
| `--record-baseline NAME` | yes | **`1`** | **nothing written**; stderr names the count and says to retry |
| `--record-baseline NAME` | no | `0` (unchanged) | file written as before |
| `--diff` | yes | `0` (unchanged) | degraded rows **excluded** from per-query and aggregate deltas, listed in `degraded_queries`; stderr warning with the count |
| `--diff --fail-below` | yes | **`1`** | diff report emitted first, then a stderr message: the gate cannot be evaluated |
| `--diff --fail-below` | no | `0` / `3` (unchanged) | unchanged |

### 3.1 Recording refuses (exit 1)

The refusal lives in `brain.eval.baseline.save_baseline`, not in the CLI: it
raises `DegradedBaselineError` (a subclass of `EvalBaselineError`) before
touching the filesystem. Putting it at the writer rather than the command means
any future caller of `save_baseline` gets the guard too, and the CLI change is
only an exception mapping. The CLI catches `EvalBaselineError`, prints its
message in red on stderr, and exits `1`, exactly as it already does for a
`load_baseline` failure.

The message names **how many** queries were degraded out of how many, never
their text. The golden corpus is gitignored because its queries and document
ids are personal; the per-query `WARNING` log line already names the query for
an operator watching the run, and the refusal message is the line most likely
to be pasted into a commit message, a PR, or a chat.

An existing baseline file of the same name is left exactly as it was. The
atomic writer already guarantees no half-written file; the refusal guarantees
no *fully* written wrong one.

### 3.2 The diff excludes degraded rows

`diff_reports` treats a degraded row in *either* report like a query present in
only one run: excluded from `per_query` and from the aggregate, surfaced
separately in a new `BaselineDiff.degraded_queries` field. A degraded row did
not measure retrieval, so a delta against it is not a delta; excluding it keeps
the aggregate like-for-like, the same principle `added_queries` /
`removed_queries` already follow. (A saved baseline cannot contain a degraded
row after this change; checking both sides costs nothing and keeps the function
correct for an in-memory baseline.)

### 3.3 `--fail-below` refuses to judge a degraded run (exit 1)

Exclusion alone is not enough for the gate. With rows excluded, a run where
every embed failed has an empty intersection, zero deltas, and would **pass**:
a gate that passes when nothing was measured. A partly degraded run is better
but still not trustworthy in either direction: its pass covers fewer queries
than the baseline, and its failure might be on queries that would have offset
it.

So when `--fail-below` is set and `degraded_queries` is non-empty, the command
emits the diff report as usual (stdout stays the diff JSON under `--json`), then
prints why the gate could not be evaluated and exits `1`, *before* the
regression check. This takes precedence over `3` deliberately: "retry, the
embedder was unhealthy" is the actionable reading, and a `3` here would send
the operator to `tests/eval/baselines/README.md` to hunt for a corpus change
that did not happen.

### 3.4 Exit-code contract

Unchanged: `1` = generic error, `2` = Typer `BadParameter`, `3` = mean-metric
regression. A degraded run under `--record-baseline` or `--fail-below` is "the
command could not do what it was asked", which is what `1` already means for a
missing corpus or an unreadable baseline. No new code is introduced, so
`tests/test_eval_workflow_contract.py` (which pins the workflow's handling of
`1`, not a list of codes) needs no change. Plain runs and plain `--diff` stay
`0`: they are informational, and an ad-hoc run that warns is the tolerance the
runner was built for.

## 4. Where the code goes, and the ceiling

- `src/brain/eval/runner.py` — `EvalResult.degraded`, set in the
  `OllamaEmbedError` branch; `degraded_results()`. Under 800 after the change.
- `src/brain/eval/baseline.py` — the save refusal, the strip-on-save, the diff
  exclusion and `degraded_queries`. Under 800 after the change.
- `src/brain/eval/errors.py` — `DegradedBaselineError`.
- `src/brain/cli.py` — **over the ceiling and in CLAUDE.md's table.** The growth
  is kept to the mapping: catch `EvalBaselineError` around `save_baseline`, the
  plain-run warning, the diff warning, and the `--fail-below` refusal. Every
  message string is built in `brain.eval.baseline` (`degraded_run_message`), so
  the CLI holds no policy. Per CLAUDE.md the growth carries (1) an inline
  why-comment at each site, (2) a module-docstring pointer, and (3) the
  `cli.py` row's trailing descriptive hop promoted to SHA-bound hops
  (`f0abf64` 9,202 → `651710f` 9,242 → `a099839` 9,255) with a new descriptive
  hop for this change.

## 5. Not done, and why

- **Retrying the embed inside `run_eval`.** Tempting, but a retry policy is a
  timeout policy, and the observed cause was another process holding the GPU
  for minutes. The operator can see that and wait; the runner cannot. Refusing
  and saying "retry" is the honest boundary.
- **Detecting a baseline that was already poisoned before this fix.** A saved
  `0.0` row is indistinguishable from a genuinely missed query. As reported, the
  degraded re-record that prompted this was caught before it was committed, so
  there is no known poisoned file to repair; the README says how to spot one (a
  single all-`0.0` row on a query that ranks fine in an ad-hoc run).
- **The graph eval runner** (`graph_runner.py`) has no equivalent tolerance; it
  is out of scope.
