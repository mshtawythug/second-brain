# Ollama embed `num_ctx`: size the embedder's context window to its own inputs

**Status:** design, 2026-10-16. Plan: `docs/plans/2026-10-16-ollama-embed-num-ctx.md`.
**Follows:** `docs/specs/2026-10-15-ollama-num-ctx-design.md` (PR #32, merged
`7545ce1`), §4 D3, which recorded this as a follow-up: *"`qwen3` (non-default)
has a larger trained context and would inherit the global value; `/api/embed`
also defaults `truncate=true`. Both are a separate embedder change with their
own tests."*

## 1. Problem

PR #32 made every brain `/api/chat` request carry `options.num_ctx`. The other
Ollama POST site, `embeddings._OllamaEmbedderBase._embed_batch` →
`/api/embed`, still sends only `model`, `input` and `keep_alive`. Two
consequences:

1. **The runner's context window is the server's, not the brain's.** On the
   author's machine the Homebrew launchd plist sets
   `OLLAMA_CONTEXT_LENGTH=65536` and `OLLAMA_KV_CACHE_TYPE=q8_0` for every
   client (`plutil -p ~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`).
   Ollama clamps a request's context to the model's trained context
   (`server/sched.go:effectiveContext`), so what the embedder actually loads
   at depends on the model:

   * **arctic** (`snowflake-arctic-embed2`, the default): trained at 8,192, so
     it loads at 8,192. **Measured, `ollama.log`, 2026-10-16 12:43 load:**
     `n_ctx = 8192`, `n_batch = n_ubatch = 2048`, `causal_attn = 0`, memory
     breakdown `context 0` MiB, `compute 56.64` MiB on the GPU. Arctic is a
     non-causal BERT (`bert.attention.causal = false`, `pooling_type = 2`) and
     allocates **no KV cache**, so its `n_ctx` costs nothing. Its compute buffer
     scales with `n_ubatch`, which Ollama sets to `min(2048, num_ctx)` for
     embedding models (§2.3).
   * **qwen3** (`qwen3-embedding:8b`, non-default): a causal Qwen3 decoder with
     last-token pooling. `max_position_embeddings = 40960` (the model's HF
     `config.json`), so on this machine (`OLLAMA_CONTEXT_LENGTH=65536`) it would
     load at 40,960. Without that variable, Ollama 0.33.0 picks a default by
     VRAM tier (`server/routes.go`: 4,096 below 23 GiB, 32,768 below 47 GiB,
     262,144 above), so the inherited window is `min(that, 40,960)`. The
     figures below are for a server whose `OLLAMA_CONTEXT_LENGTH` is ≥ 40,960.
     Per token, its q8_0 KV cache is
     36 layers × 8 KV heads × 128 dims × (K+V) × 1.0625 B = 78,336 B
     (76.5 KiB). That is **3,060 MiB at 40,960 and 153 MiB at 2,048.** This is
     *derived* with the same formula PR #32's spec verified against the log for
     `llama3.1:8b` (32 layers → 68 KiB/token). It is not measured, because
     `qwen3-embedding` is not pulled on this machine (`ollama list`).

2. **An over-window input is truncated silently.** `api.EmbedRequest.Truncate`
   defaults to true. With it unset, Ollama cuts each input to the window
   (`server/routes.go:EmbedHandler`, `truncateInput`). When the runner rejects
   an input against the physical batch, Ollama retries it cut to `num_batch`
   (`truncateInputToLimit(text, opts.NumBatch)`). Either way the brain stores
   a vector of the input's *prefix* as if it were the whole chunk. Nothing
   logs it, and retrieval for the tail of that chunk is silently lost.

## 2. Re-derived facts

### 2.1 What the brain embeds, and how large it is

Every `embed()` call site, re-derive with
`grep -rn '\.embed(' src/brain --include='*.py'`:

| Call site | Input | Bound |
|---|---|---|
| `ingest._embed_chunks` (ingest, `brain edit`, watcher) | `chunk_text` output | chunker ceiling |
| `vault/sync.py` (vault note upsert) | `chunk_text` output | chunker ceiling |
| `cli_ingest.reembed` | **stored** `chunks.content` | whatever was stored (§2.2) |
| `graph_rag/communities_summary.py` | community summaries | LLM output, `num_predict` 256 (512 on retry) |
| `search._cached_query_embed`, `graph_rag/global_.py` | the user's query (+ qwen3's ~30-token instruct prefix) | **none** |
| `mcp_server` / `ui/server` warm-up probes | `"hello"` / `"warm"` | 1 token |

`chunk_text(target_tokens=600, overlap_tokens=100)` guarantees
`count_tokens(chunk) <= 700` cl100k tokens (`ingest/chunker.py` docstring).
Both ingest call sites, `ingest/__init__.py:1191,2171`, and `vault/sync.py`
use the defaults.

**Batch shape:** `_DEFAULT_OLLAMA_BATCH = 32` inputs per `/api/embed` POST.
Ollama embeds each input of the batch **independently** (an `errgroup`, one
`r.Embedding` per input, `server/routes.go:EmbedHandler`). Embedding models
always load with `numParallel = 1` (`server/sched.go:load`). So `num_ctx`
bounds **one input**, not the batch total, and the runner holds one sequence
of `num_ctx`.

### 2.2 Measured on the live corpus: model tokens, not cl100k

`count_tokens` is tiktoken `cl100k_base`, a proxy. Neither embedder uses that
tokenizer: arctic uses XLM-R SentencePiece and qwen3 uses Qwen's BPE. Both
real tokenizers were measured with HF `tokenizers` (their `tokenizer.json`,
truncation disabled) over **every** row of the production DB in a read-only
transaction. Only aggregates were printed, never content. Counts include
special tokens.

| Input set | n | cl100k max | arctic max | qwen3 max | max ratio arctic / qwen3 (inputs ≥ 50 cl100k) | over 2,048 |
|---|---:|---:|---:|---:|---|---:|
| `chunks.content` | 21,289 | 1,596 | 1,646 | 1,689 | 1.90 / 1.76 (p99 1.55 / 1.46) | 0 |
| logged search queries | 178 | 121 | 183 | 122 | — | 0 |
| community summaries | 132 | 66 | 79 | 69 | 1.23 / 1.11 | 0 |

**The stored corpus exceeds the chunker ceiling.** 397 chunks in 110
documents are over 700 cl100k tokens, up to 1,596. By `content_type`:
transcript 308, pdf 68, email 18, docx 1, and two Confluence-import documents
with one chunk each.

* **395 of them were ingested 2026-04-28/29, before `0d8a97b`** (2026-04-30,
  "fix(chunker): enforce hard ceiling via split fallback chain"). They were
  never re-chunked.
* **The other 2 were ingested 2026-10-08** and are only 702 and 703 cl100k
  (812/844 arctic, 1,171/966 qwen3). Re-chunking those two documents' current
  bodies with today's `chunk_text` produces no chunk over 700, and the stored
  chunks differ from a fresh chunking. So the current chunker holds its
  ceiling. Why the stored chunks differ is **not established**; it is recorded
  here, not chased.

*(Corrected 2026-10-16 after audit. The first draft said 326 chunks in 78
documents, all `krisp`/`gmail`. Its query inner-joined `sources` and silently
dropped every document with no source row. The per-tokenizer maxima in the
table above were always measured over **all** chunks, so they do not change.)*

`brain reembed --all` re-embeds stored content verbatim. So
the bound that matters for re-embedding is **the corpus maximum (1,689 model
tokens)**, not the 700 the chunker now guarantees. A window sized only to the
current chunker (for example 1,400) would refuse 49 (arctic) or 50 (qwen3)
legacy chunks on the next full re-embed. Those two counts were measured over
**all** chunks (the census query reads `chunks` directly, with no join), and
every one of them is pre-`0d8a97b`. The two over-ceiling chunks from
2026-10-08 are under 1,400 in both tokenizers (812/844 arctic, 1,171/966
qwen3).

### 2.3 What `/api/embed` does with `options.num_ctx` and `truncate`

Source: Ollama `v0.33.0` (the installed version, `ollama --version`). Read with
`gh api repos/ollama/ollama/contents/<path>?ref=v0.33.0`. The `ctx7` docs for
`/ollama/ollama` (`docs/api.md`) agree: *"`truncate`: truncates the end of each
input to fit within context length. Returns error if `false` and context length
is exceeded. Defaults to `true`"*.

* `api.EmbedRequest` has `Truncate *bool` and `Options map[string]any`. There
  is **no `shift`** field, unlike chat.
* `EmbedHandler` passes `req.Options` to `scheduleRunner`, so `num_ctx` is
  honoured as for chat. Per input, the limit is
  `min(opts.NumCtx, kvData.ContextLength())`, less one each for BOS/EOS when
  the tokenizer would add them.
* `truncate: false`: each input is tokenized with the **model's real
  tokenizer**. Over the limit, the handler returns `api.StatusError{400, "the
  input length exceeds the context length"}`. The runner's own over-batch
  errors (`"physical batch size"`, `"context size"`, …) are normalised to the
  same 400 and message (`llm/llama_server.go:normalizeEmbeddingError`). One
  failing input fails the whole batch request. Wire body:
  `{"error":"the input length exceeds the context length"}`.
* `num_batch` for an embedding model with no explicit `num_batch` is
  `llm.DefaultEmbeddingNumBatchForContext(num_ctx) = min(2048, num_ctx)`
  (`server/routes.go:modelOptionsWithEmbeddingBatchDefault`). It is passed as
  both `-b` and `-ub` (`appendBatchArgs`). **So today's real per-input ceiling
  for arctic is already 2,048, not 8,192.** Ollama's own truncate-path retry
  cuts to `opts.NumBatch`. That matches the measured `n_ubatch = 2048` above.

### 2.4 Reloads

`server/sched.go:needsReload` reloads a runner when
`reflect.DeepEqual(runner.Options.Runner, req.opts.Runner)` fails, after
clamping the new `NumCtx` with `effectiveContext`. `Runner` holds `NumCtx` and
`NumBatch`. For embedding models, `NumBatch` is derived from `NumCtx` (§2.3),
so one `num_ctx` value means one stable runner.

* **First request after upgrade:** the resident arctic runner (8,192 / 2,048)
  is reloaded once at 2,048 / 2,048. Arctic is a 1.2 GB model and reloads in
  seconds. After that, every brain embed sends the same value and nothing
  reloads.
* **Embeddings and chat never share a runner.** They are different models
  (`snowflake-arctic-embed2` / `qwen3-embedding:8b` vs `llama3.1:8b`), and the
  scheduler keys runners by model. Their `num_ctx` values are independent, so
  neither can reload the other.
* **Another client** that embeds with the same model and no `num_ctx` will
  reload it back. This is the PR #32 §5 caveat. For arctic a reload costs
  seconds.

### 2.5 What else is unaffected

* **`VoyageEmbedder`**: a SaaS SDK, not Ollama. Its window is Voyage's. No
  change.
* **`NullEmbedder`**: never embeds. No change.
* **Vectors**: when an input fits, `truncate: false` changes nothing, and
  `num_ctx` does not enter the computation. Stored vectors and the eval
  baseline's `config_signature` are therefore unaffected, and no re-embed is
  needed.

## 3. Decisions

**D1: a separate knob, `BRAIN_OLLAMA_EMBED_NUM_CTX`, not PR #32's.**
`BRAIN_OLLAMA_NUM_CTX` exists so that every *chat* caller shares one runner
(PR #32 D2). That argument does not reach the embedder, which is a different
model with its own runner (§2.4). Reusing the chat knob would only couple two
unrelated budgets:

* the chat value (8,192, grown further when a user raises
  `BRAIN_ENRICH_MAX_INPUT_TOKENS` or `BRAIN_AUDIO_MAX_INPUT_TOKENS`) would size
  the qwen3 KV cache: 612 MiB at 8,192, against 153 MiB at 2,048;
* raising an LLM input cap would silently reload *and* grow the embed runner;
* PR #32's load-time check validates chat budgets, which mean nothing for an
  embedder.

The embed knob is parsed like PR #32's: `_parse_positive_int_env`, so a
non-int or a value below 1 is a `ConfigError`. It lands in
`Config.ollama_embed_num_ctx` and is threaded by `make_embedder` into both
Ollama backends. It is validated for every backend, the way every other knob
is, and is inert under `voyage` / `none`.

**D2: default 2,048. The floor is the chunker ceiling × 2 = 1,400.**
`brain.ollama_budget`, the leaf PR #32 created, gains:

* `INGEST_CHUNK_TARGET_TOKENS = 600` and `INGEST_CHUNK_OVERLAP_TOKENS = 100`.
  `chunk_text`'s parameter defaults **bind** these objects, the way PR #32
  rebound `extract`'s chunk constants. The validated ceiling and the chunker's
  real defaults therefore have one definition.
* `EMBED_TOKENIZER_MARGIN_FACTOR = 2`: the allowance for the model tokenizer
  against cl100k. The measured worst ratio is 1.90 (§2.2).
* `MIN_OLLAMA_EMBED_NUM_CTX = (600 + 100) × 2 = 1,400`: an *explicit* value
  below this is a `ConfigError` at load. It names the chunker ceiling and the
  margin.
* `DEFAULT_OLLAMA_EMBED_NUM_CTX = 2048`, for three independent reasons:
  1. It holds the measured corpus maximum, 1,689 model tokens including the
     legacy over-ceiling chunks, with 359 tokens to spare. It holds the current
     chunker's 700 × 1.90 = 1,330 with 718 to spare.
  2. It equals Ollama's `DefaultEmbeddingNumBatch`, so `num_batch`/`n_ubatch`
     stay at 2,048. **Arctic, the default, keeps its per-input capacity**:
     2,048 is already its effective ceiling today (§2.3), less the one or two
     tokens `adjustTokenLimit` reserves for BOS/EOS, and its compute buffer is
     unchanged.
  3. On a server whose `OLLAMA_CONTEXT_LENGTH` is ≥ 40,960, qwen3's KV cache
     drops from 3,060 MiB to 153 MiB (§1).

  The default is a constant and does not grow, unlike PR #32's. Its only input
  is the chunker ceiling, which is not configurable, so a test pins
  `DEFAULT >= MIN`.

The floor is 1,400, not the 1,689 corpus maximum, on purpose. The floor
guards what the brain *produces today*, and anything above that is the
server's job (D3). A user who lowers the window toward 1,400 has opted in to
the server refusing legacy chunks loudly. The default never does.

**D3: `truncate: false`. Overflow is loud, never a silent prefix.** Every
`/api/embed` request sends `"truncate": false`. An over-window input is then an
HTTP 400 that Ollama measures with the model's own tokenizer (§2.3). A new
`OllamaEmbedContextOverflow(OllamaEmbedError)` is raised. Its message names
`num_ctx`, `BRAIN_OLLAMA_EMBED_NUM_CTX`, the batch size, and the largest input's
cl100k count. Ollama cannot say which input failed, so this is the closest
pointer to it. Justification:

* **It changes no current outcome.** Every stored input fits at 2,048 (§2.2:
  zero over), and arctic already could not embed more than 2,048 tokens of an
  input (§2.3). Today truncation never fires on this corpus. The switch only
  decides what a *future* overflow looks like.
* **A silent prefix is the worse failure.** A truncated embedding is stored
  permanently, looks healthy, and only degrades retrieval for that chunk's
  tail, where nobody looks. A loud error fails one ingest or one query with a
  message that names the fix.
* **It matches PR #32 D5.** Chat sends `truncate: false` for the same reason,
  and the brain should not have two overflow policies for one server.
* **No client-side pre-check.** The server has the exact tokenizer, and a
  cl100k pre-flight would only approximate it (PR #32 §6). The cl100k count in
  the message is a diagnostic only.
* **The subclass keeps every handler working, and separates permanent from
  transient failures.** It is an `OllamaEmbedError`, so every existing
  `except EmbedError` / `except OllamaEmbedError` / `except BrainError` site
  still catches it: the MCP tools' `_wrap_embed_error`, `ui/routes_search.py`
  and the CLI. `eval/runner.py` catches it too, and since
  `fix/eval-degraded-all-embedders` reads its `transient` flag (always `False`
  for an overflow) and re-raises it (below).

**What a user sees when it fires:** the ingest of that document fails with the
error (the same path any `OllamaEmbedError` takes today), `brain reembed`
stops at the batch holding the oversize chunk, or a search query errors instead
of matching on its first ~2k tokens. Whether earlier reembed batches stay
committed, and how the watcher recovers, is unchanged by this work: it is the
existing `OllamaEmbedError` behaviour, not re-specified here.

**`brain eval` aborts.** *(Updated when `fix/eval-degraded-all-embedders`
merged this branch. As first written, this paragraph said an overflowing query
became a DEGRADED row with wrong "retry" advice, pending that track.)* Every
`EmbedError` now carries `transient`, and an overflow is never transient: the
same query overflows the same window on every run. `run_eval` re-raises it
instead of scoring the row `0.0`. `brain eval` and `brain eval --answer` then
exit `1` before any report or baseline is written, with one stderr line that
carries this error's text, so it names `BRAIN_OLLAMA_EMBED_NUM_CTX`. Why abort
and not degrade: `docs/specs/2026-10-16-eval-degraded-all-embedders-design.md`
§3.6.

The overflow test is shared with chat, not copied. PR #32's
`_CONTEXT_OVERFLOW_RE` (`context (length|size)`) already matches the embed
body, so it moves to the leaf as `CONTEXT_OVERFLOW_RE`. `chat.py` keeps its
historical private name bound to the same object, so one pattern classifies
both endpoints. The embed classification is status-agnostic, like chat's.

**D4: no `shift`.** `api.EmbedRequest` has no `shift` field (§2.3), and the
embed path passes `nil` to `scheduleRunner`. Sending one would be ignored, and
it would be a wire key that pretends to do something.

## 4. Changes

| File | Change | Ceiling |
|---|---|---|
| `src/brain/ollama_budget.py` | + embed constants (D2), + `CONTEXT_OVERFLOW_RE` (moved from `chat`), docstring | 134 lines at `8900725`; stays under 800 (re-derive with `wc -l`) |
| `src/brain/ingest/chunker.py` | `chunk_text` defaults bind `INGEST_CHUNK_*` | under 800 |
| `src/brain/chat.py` | `_CONTEXT_OVERFLOW_RE = CONTEXT_OVERFLOW_RE` | under 800 |
| `src/brain/embeddings.py` | `num_ctx` kwarg on the base + both Ollama backends; `options.num_ctx` + `truncate:false` on the wire; `OllamaEmbedContextOverflow`; `make_embedder` threads `cfg.ollama_embed_num_ctx` | 460 lines at `8900725`; stays under 800 (re-derive with `wc -l`) |
| `src/brain/config.py` | field + parse + floor check | **in the over-800 table**: inline reason at the parse, docstring hop, promote `a977744` in the CLAUDE.md row |
| `.env.example`, `docs/configuration.md`, `CHANGELOG.md` | knob docs; `[Unreleased]` → Changed | n/a |
| `CLAUDE.md` | layout line for `ollama_budget.py` (now embed too); `config.py` ceiling row | n/a |

No migration, no schema change, no new module, no re-embed.

## 5. Tests (TDD; `httpx.MockTransport` only, no live Ollama)

* **Wire pin (regression):** arctic and qwen3 `/api/embed` bodies carry
  `options == {"num_ctx": N}` and `truncate is False` on **every** batch.
  There is no `shift` key, and the default is 2,048.
* **Threading:** `make_embedder` passes `cfg.ollama_embed_num_ctx` for arctic
  and qwen3.
* **Overflow:** Ollama 0.33.0's real 400 body maps to
  `OllamaEmbedContextOverflow`, which is an `OllamaEmbedError`, and the message
  names the knob and the value. A non-overflow 400 (`model not found`) stays a
  plain `OllamaEmbedError`.
* **Config:** default 2,048; an explicit value is honoured; non-int, 0 and
  values below the floor are `ConfigError` naming the knob; the floor is
  accepted; the embed knob is independent of `BRAIN_OLLAMA_NUM_CTX`.
* **Budget:** `MIN == (INGEST_CHUNK_TARGET + OVERLAP) × MARGIN`,
  `DEFAULT >= MIN`, and `chunk_text`'s signature defaults *are* the budget
  constants.
* **Shared classifier:** `chat._CONTEXT_OVERFLOW_RE is
  ollama_budget.CONTEXT_OVERFLOW_RE`.

## 6. Rejected alternatives

* **Reuse `BRAIN_OLLAMA_NUM_CTX`** (D1): it couples two independent runners.
* **Default 8,192 (arctic's trained context):** buys arctic nothing, because
  `n_ubatch` caps it at 2,048 anyway, and leaves qwen3 at 612 MiB of KV cache.
* **Floor at the measured corpus maximum (1,689):** that number belongs to
  this corpus. It would make the config check depend on data, and it is not a
  property of what the brain produces now.
* **Keep `truncate` true:** it preserves the silent-prefix failure (D3).
* **Client-side cl100k pre-check that raises:** it approximates the server's
  exact tokenizer (PR #32 §6).
* **Re-chunk the 397 over-ceiling chunks:** they fit at 2,048, and re-chunking is a
  data migration with its own risks. It is out of scope and recorded here.

## 7. Mutation catalog

Five clauses per entry: passes unmutated; the mutation lands its declared
count; red; the needle is on a `>`/`E` line; restored → green. Run with
`PYTHONDONTWRITEBYTECODE=1`. Results are in the commit report.

**E11 is a *drifted* restatement on purpose.** An identical restatement,
`re.compile(r"context (length|size)", re.IGNORECASE)`, returns the very object
the leaf holds, because `re` caches compiled patterns. The identity test cannot
see it, but such a copy is also behaviourally identical at the moment it is
written. The failure mode the test exists for is a copy that later *diverges*,
which is what E11 injects.

**E10 mutates `target_tokens`, not `overlap_tokens`, on purpose.** CPython
caches small ints (-5..256). A literal `overlap_tokens: int = 100` is therefore
the very object the leaf holds, and the identity check in
`test_chunker_defaults_are_the_budgeted_ones` cannot see it. The reviewer's
mutation R4 survives for exactly this reason, and the test docstring records
it. Like E11, that restatement is behaviourally identical when written. A
*drifted* overlap (any other value) still fails the test. `600` is outside the
cache, so E10 kills a literal restatement of the target.

**The base class takes `num_ctx` as a required keyword, with no default.** Both
concrete backends always pass it, so a default on `_OllamaEmbedderBase` could
never be reached and its mutation would survive. E13 mutates the two
reachable defaults.

| # | Mutation | Count | Must go red |
|---|---|---:|---|
| E1 | `embeddings.py`: drop `"num_ctx"` from the embed `options` | 1 | `test_embed_wire_pins_num_ctx_and_truncate` |
| E2 | `embeddings.py`: drop `"truncate": False` | 1 | same |
| E3 | `embeddings.py`: `make_embedder` arctic omits `num_ctx=` | 1 | `test_make_embedder_threads_embed_num_ctx[arctic]` |
| E4 | `embeddings.py`: `make_embedder` qwen3 omits `num_ctx=` | 1 | `test_make_embedder_threads_embed_num_ctx[qwen3]` |
| E5 | `embeddings.py`: remove the overflow branch | 1 | `test_embed_context_overflow_is_typed` |
| E6 | `embeddings.py`: `make_embedder` arctic passes `cfg.ollama_num_ctx` (the chat knob) | 1 | `test_make_embedder_threads_embed_num_ctx[arctic]` |
| E7 | `config.py`: delete the floor check | 1 | `test_config_rejects_embed_num_ctx_below_floor` |
| E8 | `config.py`: parse default → `DEFAULT_OLLAMA_NUM_CTX` (the chat default) | 1 | `test_config_embed_num_ctx_default_is_2048` |
| E9 | `ollama_budget.py`: `MIN_OLLAMA_EMBED_NUM_CTX` drops `× EMBED_TOKENIZER_MARGIN_FACTOR` | 1 | `test_min_embed_num_ctx_is_chunk_ceiling_times_margin` |
| E10 | `chunker.py`: `target_tokens: int = 600` literal (unbound) | 1 | `test_chunker_defaults_are_the_budgeted_ones` |
| E11 | `chat.py`: `_CONTEXT_OVERFLOW_RE` restated as its own, drifted `re.compile(r"context length")` | 1 | `test_chat_and_embed_share_one_overflow_pattern` |
| E12 | `config.py`: the parse reads `BRAIN_OLLAMA_NUM_CTX` | 1 | `test_config_embed_num_ctx_honours_explicit_value` |
| E13 | `embeddings.py`: both backends' `num_ctx` kwarg default → `8192` literal | 2 | `test_embedder_default_num_ctx_is_2048[arctic]` and `[qwen3]` |
| E14 | `embeddings.py`: the overflow test → `if True:` (every HTTP error misfiled as overflow) | 1 | `test_embed_non_overflow_400_stays_plain` |
| E15 | `embeddings.py`: overflow search window `body[:200]` → `body[:20]` | 1 | `test_embed_context_overflow_is_typed` (the real 0.33.0 body puts the phrase at about index 38) |
| R4b | `chunker.py`: `overlap_tokens: int = 101` (a drifted literal) | 1 | `test_chunker_defaults_are_the_budgeted_ones` |
| R4 | `chunker.py`: `overlap_tokens: int = 100` (an identical literal) | 1 | **survives by design**: small-int cache (see the E10 note above) |
