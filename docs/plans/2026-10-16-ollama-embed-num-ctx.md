# Ollama embed `num_ctx` — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** every brain `/api/embed` request carries `options.num_ctx`
(`BRAIN_OLLAMA_EMBED_NUM_CTX`, default 2,048) and `truncate: false`, so an
Ollama-backed embedder loads a window sized to its inputs, and an over-window
input raises `OllamaEmbedContextOverflow` instead of being silently embedded as
its prefix.

**Spec:** `docs/specs/2026-10-16-ollama-embed-num-ctx-design.md`. Read it
first: every number below is derived there.

**Branch / worktree:** `perf/ollama-embed-num-ctx` in
`.claude/worktrees/embed-ctx`, from `second-brain/master` @ `8900725`.

**Constraints:** no live Ollama requests (they would reload a resident
runner), so use `httpx.MockTransport` only; no monkey-patching of production
modules; `ruff check` and `mypy src/` must be clean; `config.py` ceiling rules
apply (inline reason, docstring hop, promote the `a977744` hop in CLAUDE.md).

## Tasks

### T1: budget constants (leaf) + chunker binding

* **Test first** (`tests/test_ollama_embed_num_ctx.py`):
  * `test_min_embed_num_ctx_is_chunk_ceiling_times_margin`
  * `test_default_embed_num_ctx_holds_the_floor` (asserts 2,048 ≥ the floor)
  * `test_chunker_defaults_are_the_budgeted_ones`: `inspect.signature(chunk_text)`
    defaults are the leaf constants (`is`).
* **Implement:** in `ollama_budget.py`, add `INGEST_CHUNK_TARGET_TOKENS`,
  `INGEST_CHUNK_OVERLAP_TOKENS`, `EMBED_TOKENIZER_MARGIN_FACTOR`,
  `MIN_OLLAMA_EMBED_NUM_CTX` and `DEFAULT_OLLAMA_EMBED_NUM_CTX`, and update the
  module docstring. `chunker.chunk_text` defaults bind them.

### T2: shared overflow pattern

* **Test first:** `test_chat_and_embed_share_one_overflow_pattern`
  (`chat._CONTEXT_OVERFLOW_RE is ollama_budget.CONTEXT_OVERFLOW_RE`), plus the
  pattern matches the 0.33.0 embed body.
* **Implement:** move the regex and its comment to the leaf. `chat.py` binds
  the leaf object under its old name.

### T3: embedder wire + overflow

* **Test first:**
  * `test_embed_wire_pins_num_ctx_and_truncate[arctic|qwen3]`: every batch
    body has `options == {"num_ctx": N}` and `truncate is False`, and has no
    `shift` key.
  * `test_embedder_default_num_ctx_is_2048[arctic|qwen3]`
  * `test_embed_context_overflow_is_typed`: the real 0.33.0 400 body maps to
    `OllamaEmbedContextOverflow`, which is an `OllamaEmbedError`, and the
    message names the knob, `num_ctx`, the batch size and the largest input.
  * `test_embed_non_overflow_400_stays_plain`
* **Implement:** `num_ctx` kwarg on `_OllamaEmbedderBase`, `ArcticEmbedder`
  and `Qwen3Embedder` (default `DEFAULT_OLLAMA_EMBED_NUM_CTX`); the payload;
  the `OllamaEmbedContextOverflow` class; the classification in
  `_embed_batch`.

### T4: config knob + threading

* **Test first:**
  * `test_config_embed_num_ctx_default_is_2048`
  * `test_config_embed_num_ctx_honours_explicit_value`
  * `test_config_rejects_bad_embed_num_ctx[abc|0|-5]`
  * `test_config_rejects_embed_num_ctx_below_floor`, plus a test that the
    floor itself is accepted
  * `test_embed_num_ctx_independent_of_chat_num_ctx`
  * `test_make_embedder_threads_embed_num_ctx[arctic|qwen3]`
* **Implement:** add a `Config.ollama_embed_num_ctx` field, the parse and the
  floor check in `Config.load`, add the value to the constructor dict, and
  have `make_embedder` pass it. Ceiling: an inline reason at the parse plus a
  docstring hop.

### T5: docs

* `.env.example`: the knob, commented out, with a one-paragraph why.
* `docs/configuration.md`: a "Feature config knobs" row.
* `CHANGELOG.md`: an `[Unreleased]` entry under Changed.
* `CLAUDE.md`: update the `ollama_budget.py` layout line. In the `config.py`
  row, promote `a977744` and add a descriptive hop for this commit.

### T6: verify

* `ruff check`, `mypy src/`.
* Affected tests in brain-ci's env on the sibling DB
  `second_brain_test_embedctx`: embedder, config, ollama_num_ctx, ingest,
  reembed and the docs/CHANGELOG readers. Run the full suite if feasible.
* Run the mutation catalog (spec §7, E1–E13), all five clauses.

## Risks

* **The first embed after upgrade reloads arctic once** (spec §2.4). This is
  expected and costs seconds.
* **Another client embedding with the same model and no `num_ctx`** causes
  alternating reloads. This is inherent to a per-request option, as in PR #32.
* **A user who sets the knob between 1,400 and 1,689** gets loud refusals on
  the legacy over-ceiling chunks during a full re-embed. This is by design
  (spec D2) and is documented in `.env.example`.
