# Ollama `num_ctx` — size the brain's context window to its own work

**Status:** design, 2026-10-15. Plan: `docs/plans/2026-10-15-ollama-num-ctx.md`.

## 1. Problem

The brain never sends `options.num_ctx` on its `/api/chat` calls, so every load
of the chat model inherits the Ollama server default. On the author's machine
(ollama 0.33.0, Apple Silicon, 32 GB) the Homebrew launchd plist sets
`OLLAMA_CONTEXT_LENGTH=65536` (plus `q8_0` KV cache and flash attention) for
every client on the box. Every `llama3.1:8b` runner the brain triggers — chiefly
the vault watcher's post-ingest enrichment — therefore allocates a 65,536-token
KV cache for prompts that are 900–2,300 tokens long.

**Measured, from this machine's own `ollama.log`** (every `llama_context: n_ctx`
line paired with the `KV buffer size` line that follows it, `llama3.1:8b`, q8_0):

| n_ctx | KV buffer | loads seen |
|---:|---:|---:|
| 8,192 | 544 MiB | 3 |
| 16,384 | 1,088 MiB | 1 |
| 32,768 | 2,176 MiB | 194 |
| 65,536 | 4,352 MiB | 9 |

The KV buffer is exactly linear in `n_ctx` (32 layers × 8 KV heads × 128 dims ×
K+V × 1.0625 B/elt for q8_0 = 68 KiB/token). At 8,192 it is 544 MiB: **−3,808 MiB
of wired GPU memory per resident runner**, before the compute buffer, which also
shrinks with `n_ctx`. The scheduler's own estimate for the 65,536 load was
`predicted="12.6 GiB"`, and the log shows it evicting other models to fit it.
Under that memory pressure, loads took minutes and several `/api/chat` calls ran
into the enrich timeout and failed with HTTP 500 after 4 minutes.

The brain-shaped prompts in the same log (`task.n_tokens`, real llama tokenizer)
are 895–3,818 tokens, with no truncation (`truncated = 0` on every release).
The 16k–30k-token prompts that also appear in the log come from another client:
multi-turn sessions with prompt-cache checkpoint restores and `/api/ps` polling
twice a second, on 2026-08-26. The brain sends neither. **That is why the plist
is not ours to change:** it serves other tools on the machine. The brain
requests a context sized to its own work and leaves the global default alone.

## 2. Inventory — every Ollama call in `src/brain`

Re-derive with
`grep -rn 'api/chat\|api/generate\|api/embed\|\.post(' src/brain --include='*.py'`.
There are exactly **two** POST sites:

* `chat._chat_once` → `/api/chat`. **Every** chat/completion in the brain
  reaches Ollama through this one function, via `chat_json` (Config-based, per
  call client) or `chat_json_with_client` (persistent client, used by
  `OllamaEnricher._chat_with_retry`). The brain has no `/api/generate` call.
  (`config.py`'s keep_alive comment names one; that comment is stale.)
* `embeddings._OllamaEmbedderBase._embed_batch` → `/api/embed`.

All token figures are `cl100k_base` (the brain's tokenizer). `retry` is the
output budget after `chat_json_with_client` doubles `num_predict` on a
truncated JSON response (§4 D4). The system prompts were measured with tiktoken
on the merged tree.

| Caller | Model knob | Bounded input | System prompt | num_predict (retry) | Worst case |
|---|---|---|---:|---:|---:|
| `OllamaEnricher.summarize` (watcher / ingest hook, `brain enrich`) | `BRAIN_ENRICH_MODEL` | body ≤ `BRAIN_ENRICH_MAX_INPUT_TOKENS` (1,200) + title | 91 | 256 (512) | ≈ 1.9k |
| `OllamaEnricher.extract_entities` (GraphRAG concepts, per chunk) | `BRAIN_GRAPH_EXTRACT_MODEL` | chunk ≤ 1,500 + 100 overlap (`chunk_text` ceiling); the 8,000 `BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS` caps the *document*, not a call | 701 | 1,024 (2,048) | ≈ 4.4k |
| `ScriptGenerator` / `audio` (`brain audio`) | `BRAIN_AUDIO_SCRIPT_MODEL` | whole prompt ≤ `BRAIN_AUDIO_MAX_INPUT_TOKENS` (3,000)¹ | (inside cap) | 2,048 (4,096) | ≈ 7.1k |
| `propose_tags` (`brain tag --auto`, capture) | enrich | summary + the **whole** tag vocabulary (uncapped; 232 tags = 975 tokens on the live corpus today) | 167 | 256 (512) | ≈ 1.9k |
| `summarize_bucket` (`brain timeline --synthesize`) | enrich | summaries ≤ 1,200 (`_SYNTH_DOC_SUMMARY_BUDGET_TOKENS`) + bucket titles | 343 | 256 (512) | ≈ 2.5k |
| `summarize_group` (themes, communities, weekly review) | enrich | ≤ 20 entity names + ≤ 10 titles | 102 | 256 (512) | < 1k |
| `assess_contradiction` (review scan, elicit) | enrich | ≤ 10 summaries | 153 | 200 (400) | ≈ 1.6k |
| `draft_rule` (`brain gaps` / elicit) | enrich | evidence excerpts (500-char fallback each) | 95 | 256 (512) | ≈ 1.5k |
| `ask` plan / reflect / synthesize | enrich | snippets ≤ 400 chars × retrieved hits | — | 128 / 128 / 512 (1,024) | ≈ 4k |
| `brief.suggest_next_steps` | enrich | ≤ `_MAX_PROMPT_ITEMS` titles + todo texts | — | 256 (512) | < 1k |
| `/api/embed` (arctic / qwen3 / ingest + query) | `BRAIN_EMBEDDER` | chunker output | — | — | n/a |

¹ `audio.build_prompt` always includes the *first* source group even if it
alone exceeds the cap, so that one prompt is never empty. That is the one
configured cap with an escape hatch; D5 covers it.

**Nothing the brain sends needs more than 8,192 tokens.** The largest is the
audio script, at about 7.1k worst case. Every default model is `llama3.1:8b`,
which was trained at 131,072, so 8,192 is far inside what the model supports.

## 3. Goals / non-goals

* **Goal:** every brain `/api/chat` request carries an explicit `num_ctx`,
  sized to the brain's largest call, so a brain-triggered runner allocates
  544 MiB of KV cache, not 4,352 MiB (~4.25 GiB).
* **Goal:** a prompt larger than `num_ctx` must be impossible for the capped
  callers and loud for every caller. Today Ollama drops the overflow silently.
* **Non-goal:** touching the Ollama server config (the plist, `ollama serve`
  env, restarting Ollama). It belongs to the machine, not the brain.
* **Non-goal:** changing `/api/embed` (D3).

## 4. Design

**D1 — one knob, `BRAIN_OLLAMA_NUM_CTX`, default 8,192.** It is parsed with the
module's `_parse_positive_int_env` (non-int or < 1 → `ConfigError`) into
`Config.ollama_num_ctx`. When it is unset or blank, the value is the larger of
8,192 and the largest capped call's requirement (D4). A user who already raised
`BRAIN_ENRICH_MAX_INPUT_TOKENS` or `BRAIN_AUDIO_MAX_INPUT_TOKENS` therefore
keeps a working brain after upgrading, and pays only the KV cache their own
caps need. *(Changed 2026-10-15 after the suite of record caught exactly that
break: `test_enrich_max_input_tokens_override` sets the enrich cap to 8,000,
and a fixed 8,192 default made `Config.load` raise.)* `_chat_once` sends it as
`options.num_ctx` on every request: through `chat_json` (from
`cfg.ollama_num_ctx`) and through `chat_json_with_client`, where it is a
*required* keyword so mypy catches any new transport caller that forgets it.
`OllamaEnricher` takes `num_ctx` like `keep_alive` (a constructor kwarg
defaulting to the module default). `make_enricher` and
`graph_rag.extract.make_extractor`, the only two production constructors, pass
`cfg.ollama_num_ctx`.

**D2 — no per-caller overrides.** Ollama's scheduler reloads a runner when a
request's runner options differ from the loaded runner's
(`server/sched.go:needsReload` → `reflect.DeepEqual` on `Options.Runner`, which
holds `NumCtx`). Per-caller values on a shared model (all three model knobs
default to `llama3.1:8b`) would reload the model whenever the brain switched
callers, for example between enrich and extract in one ingest. One shared value
means one runner. §2 shows no caller needs more than 8,192, so there is nothing
to override. If a user raises a cap past the budget, D4 tells them at startup to
raise the one knob.

**D3 — embeddings are unchanged.** Ollama clamps a request's context to the
model's training context (`effectiveContext(..., runner.trainContext)`). This
machine's log shows it doing so for the default embedder:
`msg="requested context size too large for model" num_ctx=65536 n_ctx_train=8192`,
then `llama_context: n_ctx = 8192` with a ~10 MiB KV buffer. So the global
65,536 setting already costs arctic nothing. **Recorded follow-up, not done
here:** `qwen3` (non-default) has a larger trained context and would inherit
the global value; `/api/embed` also defaults `truncate=true`. Both are a
separate embedder change with their own tests.

**D4 — overflow is impossible for capped callers: validated when config loads.**
A new leaf module, `src/brain/ollama_budget.py`, imports nothing from `brain`,
which is why `config.py` can import it without a cycle. It owns the numbers
that must fit inside `num_ctx`:

* the completion budgets: `CHAT_DEFAULT_NUM_PREDICT` 256,
  `EXTRACT_NUM_PREDICT` 1,024, `AUDIO_NUM_PREDICT` 2,048;
* the extract chunk ceiling: `EXTRACT_CHUNK_TARGET_TOKENS` 1,500 +
  `EXTRACT_CHUNK_OVERLAP_TOKENS` 100;
* `NUM_PREDICT_RETRY_FACTOR` 2. `chat.py`'s truncation retry multiplies by
  *this* constant, so the validation and the retry cannot disagree;
* `PROMPT_FRAME_RESERVE_TOKENS` 1,024, the room for the system prompt, user
  framing, and chat template.

The modules that used to define these (`chat.DEFAULT_NUM_PREDICT`,
`enrichment._EXTRACT_NUM_PREDICT`, `audio._DEFAULT_NUM_PREDICT`,
`extract._DEFAULT_CHUNK_*`) now bind the leaf's objects under their old names.
That leaves one definition with no copy that can drift.

`required = input + PROMPT_FRAME_RESERVE_TOKENS + num_predict × NUM_PREDICT_RETRY_FACTOR`
for each of the three capped calls (enrich summarize, extract chunk, audio
script). An unset knob grows to the largest `required` (D1). If an *explicit*
`BRAIN_OLLAMA_NUM_CTX` is below any `required`, `Config.load` raises
`ConfigError` naming the caller, the input knob, and the required value. With
the defaults: 2,736 / 4,672 / 8,120 ≤ 8,192.

**The reserve is guarded, not trusted.** `test_prompt_frames_fit_frame_reserve`
calls every enricher method with empty variable inputs and measures the real
wire messages. It asserts that every system prompt plus its fixed user framing
fits within
`PROMPT_FRAME_RESERVE_TOKENS − CHAT_TEMPLATE_MARGIN_TOKENS` (1,024 − 256). If
someone grows the 701-token extract prompt past that, the test fails at the
point where the reserve would otherwise quietly stop covering it.

**D5 — overflow is loud for every caller: no silent truncation on the server.**
`_chat_once` sends `"truncate": false` and `"shift": false`
(`api.ChatRequest.Truncate` / `.Shift`). By default Ollama drops chat history
to fit (`server/prompt.go:chatPrompt`), and the runner shifts or truncates
past the window. With both off, an over-window prompt is an HTTP error that
Ollama measures with the model's **real** tokenizer. A client-side cl100k
pre-check could only approximate that tokenizer. This covers the uncapped
callers (the tag vocabulary, timeline titles, ask snippets, audio's first-group
escape hatch) and any tokenizer drift between cl100k and the model.

`shift` is a runner-launch option too: `needsReload` compares
`runner.contextShift`. The brain sends the same `shift:false` on every request,
so it adds no reload beyond D2's single runner.

**What actually arrives (Ollama 0.33.0).** With `truncate:false`, Ollama
passes the prompt through untouched (`llm/llama_server.go:281`). llama-server
refuses it, and Ollama relays that refusal as **HTTP 400**, whose `error` field
is the runner's raw JSON body as an escaped string
(`llm/llama_server.go:1648-1654`, `server/routes.go:3028-3031`):

    {"error":"{\"error\":{\"code\":400,\"message\":\"the request exceeds the available context size, try increasing it\",...}}"}

That is about 199 characters, with "context size" at about index 83. Ollama's own
`promptTooLongMessage` ("… longer than the context length …") is only reachable
when `truncate` is **true**, so it is not on the brain's path.

An HTTP error whose body names the context window (`context length` /
`context size`) maps to a new `OllamaContextOverflow(EnrichmentError)`,
whatever the status code, with a message naming `num_ctx` and
`BRAIN_OLLAMA_NUM_CTX`. The match searches the 200-character body preview,
which holds the nested phrase (R4 pins this). It is status-agnostic, so it does
not depend on which side refused or which code it chose. It subclasses
`EnrichmentError` and **not** `OllamaUnavailable`, because an over-window
prompt is permanent, not transient.

**Not every caller already handled it.** That was this paragraph's original
claim, and the PR #32 audit disproved it. Before this change, Ollama
truncated an over-window prompt silently (`truncate` and `shift` both default
on), so no caller ever saw an overflow error. Now one can occur. Two sites
caught *only* `OllamaUnavailable`, so the new type would have escaped them as a
bare `EnrichmentError`:

* `review/scans.py`, the contradiction scan's per-pair loop. It now **skips
  that pair** and logs a warning. The refusal is permanent for that one prompt,
  and the next pair is a different prompt. Aborting the whole scan with
  `ReviewError` (the scan's down-server path) would stop every other entity's
  adjudication for one oversize pair. This mirrors `elicit/detectors.py`'s
  per-entity skip.
* `cli.py`, `eval --answer`. It now gets a **clean exit 1** whose message names
  `BRAIN_OLLAMA_NUM_CTX`. A per-case skip would silently change the reported
  means.

Each has a regression test. Every other `except OllamaUnavailable` in `src/`
is followed by an `except EnrichmentError` that now catches the overflow
(re-derive with `grep -rn 'except OllamaUnavailable' src/brain`). Those sites:
`ingest/__init__.py` hook, `graph_rag/extract.py` (skips the one chunk, as it
does for any `EnrichmentError`), `cli.py` enrich backfill (skips the row), ask,
tag, gaps, and `mcp_server.py`. `audio.py` re-raises `OllamaUnavailable` and
wraps every other `EnrichmentError` in `AudioError`. `chat.py`'s own clause
only re-raises.

## 5. Consequences

* **First brain call after deploy reloads the resident `llama3.1:8b` runner
  once**, from (65,536, shift on) to (8,192, shift off). After that the runner
  stays stable.
* **Another tool that uses `llama3.1:8b` without its own `num_ctx` will reload
  it back.** The two clients then alternate reloads for as long as both are
  active. This is inherent in a per-request setting on a shared model. The only
  way to avoid it is for the machine owner to lower the global default, which is
  outside this change by design.
* Callers that depended on a 65,536 window: none (§2).

## 6. Rejected alternatives

* **Size `num_ctx` per request to the prompt.** Every distinct value reloads
  the runner (D2), which is the thrash this change exists to stop.
* **Client-side cl100k pre-flight check that raises.** It approximates a
  tokenizer the server has exactly (D5), and it would duplicate the server's
  enforcement.
* **Edit the plist / restart Ollama.** Out of scope: machine config that serves
  other tools.
* **`BRAIN_OLLAMA_NUM_CTX=0` to opt out and inherit the server default.**
  Nothing asks for it (YAGNI), and it reintroduces exactly this problem.

## 7. Mutation catalog

Every new guard or test must go red under its mutation (five clauses: passes
unmutated; mutation lands the declared count; red; the needle on a `>`/`E`
line; restored → green). Run with `PYTHONDONTWRITEBYTECODE=1`.

| # | Mutation (file: change) | Count | Test that must go red |
|---|---|---:|---|
| M1 | `chat.py`: drop `"num_ctx": num_ctx` from `options` | 1 | `test_chat_once_sends_num_ctx_truncate_shift` |
| M2 | `chat.py`: drop `"truncate": False` | 1 | same |
| M3 | `chat.py`: drop `"shift": False` | 1 | same |
| M4 | `chat.py`: `chat_json` passes a literal `num_ctx=4096` instead of `cfg.ollama_num_ctx` | 1 | `test_chat_json_threads_cfg_num_ctx` |
| M5 | `chat.py`: retry growth `*= NUM_PREDICT_RETRY_FACTOR` → `*= 3` | 1 | `test_truncation_retry_grows_by_shared_factor` |
| M6 | `chat.py`: remove the overflow classification branch | 1 | `test_context_overflow_is_overflow_not_unavailable` |
| M7 | `enrichment.py`: `_chat_with_retry` passes `num_ctx=8192` literal instead of `self._num_ctx` | 1 | `test_enricher_sends_its_num_ctx` |
| M8 | `enrichment.py`: `make_enricher` omits `num_ctx=` | 1 | `test_make_enricher_threads_num_ctx` |
| M9 | `extract.py`: `make_extractor` omits `num_ctx=` | 1 | `test_make_extractor_threads_num_ctx` |
| M10 | `config.py`: delete the budget-validation call | 1 | `test_config_rejects_num_ctx_below_budget` |
| M11 | `config.py`: `max([DEFAULT_OLLAMA_NUM_CTX]` → `max([65536]` at the parse site | 1 | `test_config_num_ctx_default_is_8192` |
| M12 | `ollama_budget.py`: `required_num_ctx` drops `× NUM_PREDICT_RETRY_FACTOR` | 1 | `test_required_num_ctx_counts_retry_output` |
| M13 | `enrichment.py`: append 120 tokens of text to `_EXTRACT_SYSTEM_PROMPT` | 1 | `test_prompt_frames_fit_frame_reserve` |
| M14 | `extract.py`: `_DEFAULT_CHUNK_TARGET_TOKENS = 1400` literal | 1 | `test_extract_chunk_constants_are_the_budgeted_ones` |
| M15 | `audio.py`: `_DEFAULT_NUM_PREDICT = 3072` literal | 1 | `test_audio_num_predict_is_the_budgeted_one` |
| M16 | `config.py`: the unset default stops growing (`max([...])` → `DEFAULT_OLLAMA_NUM_CTX`) | 1 | `test_config_unset_num_ctx_grows_to_fit_raised_cap` |
| M17 | `review/scans.py`: delete the `except OllamaContextOverflow` per-pair skip | 1 | `test_review.py::test_conflict_scan_context_overflow_skips_pair` |
| M18 | `cli.py`: delete the `eval --answer` `except OllamaContextOverflow` handler | 1 | `test_cli_ask.py::test_cli_eval_answer_context_overflow_exits_cleanly` |
| M19 | `enrichment.py`: `draft_rule` passes the literal `num_predict=256` again | 1 | `test_draft_rule_sends_the_budgeted_default_num_predict` |
| M20 | `enrichment.py`: grow `extract_entities`' user label (`TEXT:` + 80 tokens), system prompt untouched | 1 | `test_prompt_frames_fit_frame_reserve` (proves the framing is measured, not only the system prompt) |
| R3 | `ollama_budget.py`: extract input drops `+ EXTRACT_CHUNK_OVERLAP_TOKENS` | 1 | `test_default_capped_call_requirements_are_pinned` (reviewer's surviving mutation, now killed) |
| R4 | `chat.py`: overflow search window `exc.response.text[:200]` → `[:60]` | 1 | `test_context_overflow_is_overflow_not_unavailable[…]` real-0.33.0 nested-400 case (reviewer's surviving mutation, now killed) |

M6's needle is the exception that escapes under the mutation
(`EnrichmentError: Ollama returned HTTP 400`), not `OllamaContextOverflow`.
`pytest.raises` reports the escaped exception, so the expected class's name
never appears on an `E` line. The first harness run declared the wrong needle
and recorded M6 as FAIL even though the test went red. The needle was
corrected, and the re-run passed all five clauses.
