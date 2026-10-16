# Ollama `num_ctx` — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-10-15-ollama-num-ctx-design.md`. **Branch:** `perf/ollama-num-ctx`.

**Goal:** every brain `/api/chat` request sends `options.num_ctx`
(`BRAIN_OLLAMA_NUM_CTX`, default 8192), plus `truncate:false` and `shift:false`.
The capped call budgets are validated when config loads, and an over-window
prompt raises `OllamaContextOverflow` instead of being truncated silently.

## Affected files

| File | Change | Ceiling |
|---|---|---|
| `src/brain/ollama_budget.py` | **new** leaf: constants, `CallBudget`, `capped_call_budgets`, `num_ctx_violations` | new, < 800 |
| `src/brain/errors.py` | `OllamaContextOverflow(EnrichmentError)` | under |
| `src/brain/chat.py` | `num_ctx` threaded + sent; `truncate`/`shift` false; retry factor from leaf; overflow classification | under |
| `src/brain/enrichment.py` | `num_ctx` ctor kwarg → `_chat_with_retry`; `make_enricher` threads it; `_EXTRACT_NUM_PREDICT` from leaf | **in table**: inline why + docstring pointer + trail row |
| `src/brain/graph_rag/extract.py` | `make_extractor` threads `num_ctx`; chunk constants from leaf | **in table**: same |
| `src/brain/audio.py` | `_DEFAULT_NUM_PREDICT` from leaf | under |
| `src/brain/config.py` | `DEFAULT_OLLAMA_NUM_CTX` re-export, `ollama_num_ctx` field + parse + budget validation; stale `/api/generate` comment fixed | **in table**: inline why + docstring pointer + promote `f0abf64` hop + new descriptive hop |
| `CLAUDE.md` | trail rows for `config.py`, `enrichment.py`, `graph_rag/extract.py` | — |
| `.env.example`, `docs/` knob reference | document `BRAIN_OLLAMA_NUM_CTX` | — |
| `tests/test_ollama_num_ctx.py` | **new**: M1–M15 targets | — |
| existing tests that call `chat_json_with_client` | pass the now-required `num_ctx=` | — |

## Tasks (TDD — each red first, then green)

1. **Leaf module.** Write `test_required_num_ctx_counts_retry_output`, the
   boundary tests (exactly-fits passes, +1 fails, the message names the knob and
   the required value), and `test_system_prompts_fit_frame_reserve`. Then
   implement `ollama_budget.py`.
2. **Error type.** Add `OllamaContextOverflow` to `errors.py`.
3. **Transport.** Write `test_chat_once_sends_num_ctx_truncate_shift`,
   `test_chat_json_threads_cfg_num_ctx`,
   `test_truncation_retry_grows_by_shared_factor`, and the overflow tests (400
   and 5xx bodies naming the context window → `OllamaContextOverflow`, while a
   plain 5xx stays `OllamaUnavailable`). Then implement in `chat.py`. Update the
   existing `chat_json_with_client` call sites in the tests.
4. **Enricher / extractor / audio.** Write `test_enricher_sends_its_num_ctx`,
   `test_make_enricher_threads_num_ctx`, `test_make_extractor_threads_num_ctx`,
   `test_extract_chunk_constants_are_the_budgeted_ones`, and
   `test_audio_num_predict_is_the_budgeted_one`. Then implement them.
5. **Config.** Write `test_config_num_ctx_default_is_8192`, the parse
   rejections (`0`, `-1`, `abc`), `test_config_rejects_num_ctx_below_budget`
   (e.g. `BRAIN_OLLAMA_NUM_CTX=4096` with the default audio cap), and
   `test_config_accepts_raised_cap_with_raised_num_ctx`. Then implement them.
6. **Docs + ceiling bookkeeping.** Update `.env.example`, the knob docs, the
   module docstrings, and the CLAUDE.md rows (promote before appending).
7. **Verify.** Run `ruff check`, `mypy src/`, and the full suite (`bin/brain-ci`,
   or its env on a sibling DB). Then run the mutation catalog (spec §7), all
   five clauses for each row, with `PYTHONDONTWRITEBYTECODE=1`.

## Risks

* **Reload alternation with other tools** sharing `llama3.1:8b` (spec §5).
  Documented; outside our control by design.
* **Older Ollama without `truncate`/`shift`.** Go's JSON decoder ignores
  unknown fields, so the request still works. On such a server, only the
  "loud" half of D5 degrades.
* **A user with raised caps.** With `BRAIN_OLLAMA_NUM_CTX` unset, the window
  grows to fit them. Only an explicit value that is too small fails at startup,
  and the error names the knob to raise. *(The first draft failed every raised
  cap at startup. The suite of record caught it via
  `test_enrich_max_input_tokens_override`.)* The live `.env` sets neither cap.
