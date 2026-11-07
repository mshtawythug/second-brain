# RM-54: Opt-in query rewriting (`--rewrite`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, RM-54. Size S, wave 9, depends on RM-09
(merged). Nothing is built: `src/brain/query_rewrite.py` does not exist, and neither `--rewrite` nor a
rewrite budget appears anywhere in `src/`.

## Rulings and deviations (recorded after approval)

- **D1–D5, D7–D9 approved as recommended.**
- **D6: the spec's "measured once both ways" is NOT met.** Prod stays off limits, and the standing
  policy forbids real Ollama generation in tests or E2E (`feedback_disk_hygiene_agents`), so neither
  the real corpus nor a real model is available. The coordinator withdrew its first ruling, a synthetic
  run with real Ollama, once this conflict was raised. The one-off, uncommitted eval script goes in the
  PR body as an owner step, read-only, reporting aggregate means only. A run with a mocked chat on a
  small synthetic corpus in a throwaway 5434 database tests the script and measures nothing; the PR
  says whether that proof has run. No golden-corpus content, or anything derived from it, is committed.
- **No real-model run.** The plan's end-to-end run against `llama3.1:8b` is dropped for the same
  reason. **The reply shape was never checked against the real model.** Mocked replies follow what
  existing `chat_json` callers and tests document: `brief.py`'s `{"suggestions": [...]}` for a list of
  strings under one key, and `tests/test_chat.py`'s `{"message": {"content": ...}}` envelope. If the
  real model answers another shape, the result is a notice and the original search, which is tested.
- **Egress notes are user-facing:** both are in `--rewrite`'s help text and in `docs/cli-reference.md`.
- **Test-first was not followed in order.** The implementation was drafted before the tests, while
  the disk was under the floor and pytest could not run. The mutation proofs stand in for RED.
- **A paraphrase search that fails while another succeeds** reports `applied: true` with its failure
  class in `skipped_reason`, and prints a notice. The plan had no line for it; silence would hide a
  weaker result.
- **Cleaning sits in the leg, not the rewriter,** so no rewriter can hand back uncleaned text.
- **Line budgets:** see the PR; `query_rewrite.py`, `search_pipeline.py` and `format_search.py` came in
  over their estimates. None is near 800.
- **The example** lives in `src/brain/resources/examples.md`, not `examples.py`.

## Goal and acceptance criteria (quoted from the spec)

**Goal:** "A vague query can opt into paraphrase retrieval, one run at a time."

**Scope in:** "a new leaf `src/brain/query_rewrite.py`; `--rewrite` on `brain search` and `brain
explain`; a `CallBudget` entry in `ollama_budget.py`." **Scope out:** "MCP (agents paraphrase for
themselves); the UI; turning it on by default."

1. "Off unless `--rewrite`."
2. "With a fake chat, the paraphrase hits fuse as specified."
3. "Chat down or a context overflow → the original search, a notice, exit 0."
4. "`explain` names the query that found each result."
5. "The new `CallBudget` appears in `capped_call_budgets()` and fits the default `num_ctx`."

**Eval:** "measured once both ways and recorded in the PR; **never adopted as a default** (the roadmap's
own ruling). `ci.json` unchanged." See D6: the real corpus is out of reach under the standing rules.

## Design

### `ollama_budget.py` (+≤ 10)

`REWRITE_MAX_INPUT_TOKENS = 256` lives here, not in `query_rewrite.py`: the module's docstring makes it
the single definition of every number the load-time check reads, and it imports nothing from `brain`.
`capped_call_budgets()` gains `CallBudget("query rewrite", "REWRITE_MAX_INPUT_TOKENS", 256,
CHAT_DEFAULT_NUM_PREDICT)`: 256 + 1024 + 256x2 = **1,792**, well inside 8,192. Its signature does not
change, so `config.py` is not edited (the spec's requirement).

### `src/brain/query_rewrite.py` (new, ≤ 180 lines)

A leaf. `chat` is imported inside the function that calls it, so `search_pipeline` can import this
module's pure parts without loading the chat transport on every surface.

- **`make_rewriter(cfg, embedder, enabled) -> Rewriter | None`**: `None` when off, so the CLI passes one
  line per command. The rewriter caps the query with `token_budget.truncate_to_token_budget(cost=
  embedder.count_tokens, budget=REWRITE_MAX_INPUT_TOKENS)`, which works under `BRAIN_EMBEDDER=none` too.
  It calls `chat.chat_json(prompt, schema={"paraphrases": "list"}, cfg=cfg,
  num_predict=CHAT_DEFAULT_NUM_PREDICT)`. Only the prompt is capped: the original search still runs the
  whole query.
- **`clean_paraphrases(raw, *, original) -> tuple[str, ...]`**: model output is untrusted. See Security.
- **`fuse_rankings(rankings) -> dict[str, FusedDoc]`**: pure. `rankings` is `(query, is_original,
  doc_ids)`. Each document scores the sum of `weight * rrf_contribution(rank)` with the original at
  1.0 and paraphrases at 0.5. Insertion order is original-first and the sort is stable, so an exact tie
  goes to the original's order (two paraphrases agreeing on rank 0 tie the original's rank 0:
  0.5/61 + 0.5/61 = 1/61).
- **`FusedDoc(fused_score, hits: tuple[QueryHit, ...])`** and **`QueryHit(query, is_original, rank)`**,
  frozen.
- **`rewrite_notice(reports) -> str | None`**: a fixed sentence plus the class-only reason, in the shape
  of `rerank_notice`.

### `search_pipeline.py` (+≤ 80)

`run_search(..., rewriter: Rewriter | None = None)`. With `None` the path is exactly today's. Otherwise:

1. Run the original through the existing fallback path, carrying `diagnostics` and `total_count`.
2. Call `rewriter(query)` under the same deliberate broad catch `apply_stages` uses: an optional
   improvement never costs the search.
3. Run each paraphrase with the same kwargs **minus `diagnostics` and `total_count`**. A denylist, not
   an allowlist, so a filter added later reaches paraphrases automatically and a paraphrase can never
   widen the set. If the original fell back, paraphrases run `fts_only=True`, so a dead embedder does
   not cost three timeouts. Each is wrapped in `with conn.transaction():` because `brain explain` is
   not in autocommit and a failed statement would otherwise abort everything after it; a failure drops
   that paraphrase only.
4. Fuse, then `apply_stages` over the fused rows with the **original** query, then cut to `limit`.

The row kept for a document is the original's if it has one, otherwise the first paraphrase's.

The leg's clock covers the chat call and the extra searches, and lands in a
`StageReport(name="rewrite", ...)` first in `stages`. That gives, with no new code, the footer's
`rewrite …ms` segment, a correct `total` (`search_meta_line` adds stage time to `diag.total_ms`, which
stays the original's), and the `--json --meta` key. `applied=False` with a class-only reason when the
chat failed or nothing usable came back.

`PipelineResult` gains `fused: Mapping[str, FusedDoc] | None = None`. `pipeline_json` does not read it,
so the MCP and UI payloads cannot change.

### Output

- **`brain search --rewrite`:** rows as today, in fused order. Bare `--json` keeps its exact keys.
- **`brain explain --rewrite`:** the existing table, then a second "Found by" table (`format_search.py`):
  ID, fused score, and one line per query with its rank, e.g. `original #1` and `"paraphrase" #3`.
  Cells are `rich.text.Text`, because a model reply containing `[/]` raises `MarkupError` as a plain cell.
- **`brain explain --rewrite --json`:** each row adds `fused_score` and `found_by`. Both are present
  whenever `--rewrite` was passed, including when the chat failed (the original alone), so a consumer
  reads one shape. Absent without the flag, so that output stays byte-identical.
- **Notice:** stderr, through `_warn_if_fell_back`. Exit 0.

### What the extra queries do not touch

`record_search_query` still records only the original, once: paraphrases never reach `search_queries`,
and `brain gaps` keeps keying off the original's `fts_count`. `N matched` and `--facets` keep describing
the original's lexical match set, so a fused row can sit outside that count. The docs say so.

## Affected files

| File | Change | Budget |
|---|---|---|
| `src/brain/query_rewrite.py` | new leaf | ≤ 180 |
| `src/brain/ollama_budget.py` | constant + budget entry | +≤ 10 |
| `src/brain/search_pipeline.py` | the rewrite leg, `PipelineResult.fused` | +≤ 80 |
| `src/brain/cli_search.py` | flag on both commands, notice, explain output | **+≤ 12 (spec §2.2)** |
| `src/brain/format_search.py` | "Found by" table, explain JSON projection | +≤ 45 |
| `src/brain/format.py` | **none**: 783/800, which is why the table goes to `format_search.py` | 0 |
| `src/brain/examples.py` | one `search` example | +1 |
| `tests/test_query_rewrite.py` | new: pure and fake-chat | ≤ 300 |
| `tests/test_search_rewrite_surfaces.py` | new: CLI on real Postgres, real `chat_json` over `MockTransport` | ≤ 300 |
| `tests/test_ollama_num_ctx.py` | the two pinned lists gain the fourth budget | small |
| `docs/cli-reference.md`, `CHANGELOG.md` | the flag, its costs, what it does not change | |

No file over 800 is touched, so `docs/ceiling-table.md` has no change. No migration, no config knob.

## Tests: every guard has a pair

| Guard | Refuses | Keeps ordinary behaviour |
|---|---|---|
| Off by default | no flag: no chat request at all (the transport fails the test if called) | `--rewrite`: exactly one chat request |
| Output unchanged when off | `found_by`/`fused_score` absent from `explain --json` | same rows and keys as before, via the existing surface tests |
| Paraphrase hygiene: control | ESC sequences, NUL, C1, bidi and a **lone surrogate** (`json.loads` yields one from `\ud800`; it would raise on encode, in psycopg or on print) are gone or the item is dropped | accented and CJK paraphrases pass unchanged |
| Hygiene: shape | non-strings, nested values, a non-list, over-long items, more than two items | a normal two-item list returns both, in order |
| Hygiene: dedupe | an echo of the original (case and spacing aside), and duplicates of each other | a real paraphrase differing by one word stays |
| Rich markup | `[/]` and `[red]` in a paraphrase render literally and do not raise | an ordinary paraphrase renders in the table |
| Filters reach paraphrases | a document excluded by `--source` / `--sensitivity normal` / `--without-tag` never appears through a paraphrase (real DB) | the same document appears through the paraphrase without the filter |
| Diagnostics stay the original's | `fts_count` and `total_documents` reflect the original when a paraphrase matches and the original does not | a plain search's diagnostics are unchanged |
| One logged query | one `search_queries` row, holding the original; no paraphrase text in the table | that row's `result_count` is the fused count |
| Failure paths (AC3) | connect error, 5xx, overflow body, malformed JSON twice, truncated reply, 404 model: original results, one notice, exit 0 | a healthy chat prints no notice |
| Notice is content-free | no query, reply or body text in the notice, `skipped_reason` or logs | the class name is there |
| Failed paraphrase | a raising `search_fn` drops one paraphrase; `explain`'s transaction still works afterward (real DB, non-autocommit) | the other paraphrase still fuses |
| Dead embedder | original falls back: paraphrases run text-only with **no** further embed attempt | healthy embedder: paraphrases get the vector leg |
| Query cap | a query far over 256 tokens: the prompt's query is ≤ 256 tokens, and the original search still uses the whole query | a short query is sent verbatim |
| Exact-name protection | the original's rank 0 outranks two paraphrases' rank 1 | paraphrase-only documents do enter the list |
| Budget (AC5) | an explicit `BRAIN_OLLAMA_NUM_CTX` below 1,792 is a `ConfigError` naming the call | the default fits |
| Frame fits the reserve | the prompt's frame, measured off the wire with real tiktoken, is ≤ `PROMPT_FRAME_RESERVE_TOKENS - CHAT_TEMPLATE_MARGIN_TOKENS` | |
| Fusion arithmetic (AC2) | hand-computed scores, exact ties, empty rankings, a paraphrase-only document | `found_by` names each query with its rank |
| `--rerank` order | the reranker receives fused rows and the original query | no stage: fused order is the output |

**Real things:** Postgres is real on every surface test, and the real `chat_json` runs over
`httpx.MockTransport` through its existing `_build_client` seam. One surface test uses the real
transport against a closed port. Real Ollama is never run (see the rulings above), so what a fake
cannot check, that the model really returns `{"paraphrases": [...]}`, stays unchecked.

**Mutation proofs (git-archive copy, never the worktree):** drop the `diagnostics` denylist; swap the
weights; remove `one_line`; send `Text` back to a plain string; remove the savepoint; forward the
original's `fallback` without `fts_only`. Each must fail a named test.

**Named set on every run:** `test_log_privacy_rules`, `test_log_privacy`, `test_import_hygiene*`,
`test_vault_delete`, `test_cli_registry*`, `test_repo_hygiene_files`, the src-wide scans, the skills
tests, `test_search_pipeline_boundary`, `test_examples_rot`, `scripts/check_ceiling_table.py`.

## Security

- **Untrusted input:** the model's reply. It can only become a search query, under the user's own
  filters, in parameterized SQL, and it is printed by `explain`. So: `preview_filter.strip_sequences`
  then `ingest.text_safety.one_line` (Unicode category `C*`, which covers surrogates, NUL, bidi and
  tag characters); a length cap that **drops** rather than cuts, because half a paraphrase is a garbled
  query; at most two items; `Text` cells. `chat_json`'s `num_predict` bounds the reply, and its
  transport catches `RecursionError` on both the envelope and the inner `json.loads` (`chat.py`).
- **Confidential data:** no document text reaches the prompt, only the query. Every filter, including
  the `--sensitivity` lens, applies to paraphrases. MCP and the UI gain nothing: `fused` is not in
  `pipeline_json`, and neither surface passes a rewriter. The search set under no filters is what the
  CLI already returns inside its trust boundary, so F6 has no new statement to judge.
- **Egress, stated plainly:** the spec says "the query goes only to the local chat model". Two accuracy
  notes for the docs. The chat call goes to `OLLAMA_HOST`, which a user can point elsewhere (true of
  every chat feature; no redirects are followed). And with `BRAIN_EMBEDDER=voyage` the paraphrases are
  embedded by Voyage, exactly as the original query already is.
- **Errors and logs:** fixed notice strings with class-only reasons (`describe_failure`). `chat`'s
  exception messages embed the reply and are never printed. Query and paraphrases are never logged.
- **Regexes:** only `preview_filter`'s, already proven linear in RM-52's review.
- **Timeouts:** `chat_json`'s default `cfg.enrich_timeout_seconds` (60 s). A timeout is not retried
  (`chat_json_with_client` retries only malformed replies), so a hung server costs 60 s once, on a run
  the user opted into, with no new constant.

## Risks

- **Latency:** one chat call plus two extra searches, possibly a cold model load. Opt-in per run, and
  the footer shows it.
- **`cli_search.py` budget:** about 10 of +12 counted in advance; if it would pass, the explain
  projection moves further into `format_search.py`.
- **Prompt quality is unproven:** real `llama3.1:8b` might not return the shape asked for, and nothing
  here checks it. The failure mode is only a notice, but in that case `--rewrite` would never help.
- **Footer and facets vs. fused rows:** see "What the extra queries do not touch". Documented, not fixed.

## Decisions

**D1. Where the fan-out lives.** (a) A `SearchStage`; (b) a `rewriter` argument on `run_search`.
**Recommend (b).** A stage's `apply` sees rows and has no `search_fn`, so it cannot search, and
`build_stages` would reach every surface when the spec scopes MCP and the UI out. It still reports
through `StageReport`, so the footer and envelope need no new code.

**D2. `score` on a fused row.** (a) Keep the row's hybrid score; (b) overwrite it with the fused score.
**Recommend (a).** This is RM-14a's documented precedent (a reranked list can be out of order by
`score`), and it keeps `explain`'s `final_score` "matches `SearchResult.score`" invariant true. The fused
score is shown by `explain --rewrite`, which is where the order needs explaining.

**D3. "Found by" rendering.** (a) A column in `explain_table`; (b) a second table in `format_search.py`.
**Recommend (b).** `format.py` is at 783/800, and paraphrase text would crush an 11-column table.

**D4. How deep each query fetches.** (a) The single-search fetch (`limit`, or a stage's depth); (b)
`max(fetch, 2 * limit)`. **Recommend (b).** Truncating each ranking at `limit` gives a document at the
original's rank `limit + 1` zero credit even if both paraphrases rank it first. The spec is silent. The
cost is small because `hybrid_search` already ranks `CANDIDATE_LIMIT` candidates internally. A
one-line constant, easy to drop if you prefer the plain reading.

**D5. A paraphrase whose own embed fails while the original's succeeded.** (a) Fall back to text-only
for it and report a fallback; (b) drop that paraphrase. **Recommend (b).** The fallback notice says
"semantic ranking is off for this search", which would be false of the original. Hence paraphrases call
`search_fn` directly rather than through the fallback wrapper, inside `search_pipeline.py` (allowlisted
by the boundary test).

**D6. The eval measurement: needs your ruling.** The spec asks for one measurement both ways, recorded
in the PR. `golden_corpus.yaml` and `ci.json` are keyed to the production document set, and I may not
connect to 55432. `run_eval` also has no rewrite path, and the spec does not scope one in.
- (a) **Recommended.** A throwaway script in the scratchpad (not committed: a one-shot harness would be
  dead code) calling `run_search` with and without a rewriter and scoring with `eval.metrics`, on a
  synthetic corpus in a throwaway 5434 database with real Ollama. The PR labels this synthetic and says
  plainly that it does **not** satisfy "measured on the real corpus". The script goes in the PR body so
  the owner can run it read-only against the real corpus and add aggregate means only, never query text.
- (b) You or the owner authorize a read-only prod run, and I or the owner do it before merge.
- (c) Waive the measurement, on the grounds it can never change a default.

Either way `ci.json` is unchanged, since off-by-default code changes no ranking.

**D7. What else maps to the notice.** The spec names chat down and overflow. **Recommend** the same path
for every other way the leg yields nothing: malformed JSON twice, a truncated reply, a 4xx (model not
pulled), and no usable paraphrases. Each is an optional improvement that did not happen; exit 1 would
throw away good results. The class-only reason distinguishes them.

**D8. What `brain search --rewrite` shows.** (a) Footer segment and `stages` only; (b) also print the
paraphrases to stderr. **Recommend (a).** AC4 puts the query names on `explain`, and (b) would spend the
tight `cli_search.py` budget. `explain` is one word away.

**D9. Skills.** `consult-brain/SKILL.md` is at its 300-line cap, and the spec's stance is that agents
paraphrase for themselves. **Recommend no skill change**, with `brain examples` and `cli-reference.md` as
the discovery paths. Say if you want a line in a reference file anyway.

RM-54.D1 (an MCP parameter: **no**) is the spec's and is not reopened.

## Order of work

1. Budget entry and its pins (RED then GREEN).
2. `query_rewrite.py`: fusion and hygiene, pure tests first.
3. The pipeline leg; fake-`search_fn` tests.
4. CLI flag, output, notice; real-DB surface tests.
5. Docs, CHANGELOG, example, Appendix B.
6. `ruff`, `mypy`, the targeted and named sets; mutation proofs; the eval script's mocked proof.
7. Push once, open the PR, report for review. Auto-memory (`cli.md`, `types.md`, `search.md`) is noted
   in that report for whoever holds it.
