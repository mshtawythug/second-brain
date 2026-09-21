# Implementation Notes — Plan 06 `brain ask` (agentic synthesis)

**Branch:** `p06-ask` · **Base:** `feat/brain-next-features` @ `d643564` · **Date:** 2026-06-09

Spec: `docs/plans/2026-06-03-brain-next-features/06-brain-ask-agentic-synthesis.md`.
Phase 0 (`src/brain/chat.py`, `chat_json`) already merged — consumed AS-IS.

## Decisions locked from source reading

- **`chat` injection:** `ask()` / `ask_no_loop()` take `chat: ChatJson` — a
  `Protocol` matching `chat_json`'s exact signature `(prompt, *, schema, cfg,
  model=None, num_predict=None, timeout=None) -> dict`. Production passes the
  real `chat_json`; tests pass a closure. Keeps the loop testable with no live
  Ollama (mirrors brief.py's `chat.chat_json` consumption but injected for unit
  isolation).
- **Retrieval:** `hybrid_search(conn, embedder=…, query=sq, limit=…, …)` per
  sub-query, dedup by `document_id` into an insertion-ordered dict. Graph leg
  (`mode != hybrid`) calls `graph_rag_search(conn, cfg, sq, backend=backend,
  mode=mode, embedder=…)` and merges `ctx.docs` (which are `SearchResult`s);
  `_graph_summary(ctx)` prepends theme/community/entity labels to the synth
  prompt. `backend` required when `mode != hybrid` (ValueError otherwise).
- **Citations:** parse `[N]` markers from the synthesized answer, map to the
  numbered doc list (1-indexed), keep only in-range refs, ordered by first
  appearance, `ref` preserves the original `[N]` (no renumber → markers stay
  valid). Out-of-range `[N]` dropped (citation integrity).
- **`fallback_used`:** `True` when planning was skipped/degraded — the `no_loop`
  fast path, OR the agentic plan step yielded zero usable sub-queries (fall back
  to `[question]`). Documented on the dataclass.
- **Config knobs** (4, `ConfigError`-validated, env parse mirrors enrich block):
  `BRAIN_ASK_MAX_ITERATIONS` (int>=1, def 3), `BRAIN_ASK_DOCS_PER_ITER` (int>=1,
  def 5), `BRAIN_ASK_MODEL` (str, blank->`enrich_model`), `BRAIN_ASK_TIMEOUT_SECONDS`
  (float>0, def 90.0).
- **Interaction logging:** one `record_interaction(document_id=…, action="opened",
  source="cli"/"mcp", session_id=…)` per emitted citation, best-effort
  (swallow `psycopg.Error`/`InteractionError`). `target_type` NEVER set (doc rows
  use the XOR doc path). Off by default in `ask()`; CLI/MCP opt in via a
  `log_interactions` flag wired to the live conn.
- **No migration** (locked cross-plan decision).

## Files

New: `src/brain/ask.py`, `src/brain/eval/answer_eval.py`, `tests/test_ask.py`,
`tests/test_answer_eval.py`, `tests/test_cli_ask.py`, `tests/test_mcp_ask.py`,
`tests/test_eval_answer_harness.py`, `tests/eval/answer_corpus.yaml`.

Modified: `config.py` (4 knobs), `cli.py` (`brain ask` + `brain eval --answer`),
`mcp_server.py` (`brain_ask`), `eval/__init__.py` (re-exports), `eval/errors.py`
(reuse `EvalCorpusError`), `.env.example`, `pyproject.toml` (no change needed —
`eval` marker already present).

## Phasing (commits)

1. core loop + config + ask.py + unit/integration tests
2. CLI `brain ask`
3. MCP `brain_ask` + interaction logging
4. answer eval harness + `brain eval --answer` + graph-mode wiring + polish
