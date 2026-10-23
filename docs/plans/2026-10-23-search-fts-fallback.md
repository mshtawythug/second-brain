# Search falls back to text-only matching when the query embed fails

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Date:** 2026-10-23. **Status:** implemented on `feat/search-fts-fallback`.

## The decision (made by the user, not re-opened here)

When the embedder is unreachable (local Ollama down, Voyage erroring), `brain
search` and MCP `brain_search` used to fail loudly: the CLI printed a traceback
or a red error and exited non-zero, and MCP returned `INTERNAL_ERROR
embedding failed: <Class>`. The user decided that **user-facing search falls
back to text-only (FTS-only) matching when the query embedding cannot be
computed, and says so clearly.**

## Design

One new leaf module, `src/brain/search_fallback.py` (well under 800 lines), so
none of the over-ceiling files carries the logic:

- `SearchFallback` (frozen dataclass): `reason` (class-only, from
  `brain.errors.describe_failure`), `transient` (copied from
  `EmbedError.transient`), and a `note` property that renders the one-line
  notice. `fallback_json(fallback | None)` projects it as four always-present,
  nullable keys: `fallback` (`"fts_only"` or `null`), `fallback_reason`,
  `fallback_transient`, `fallback_note`.
- `search_with_fts_fallback(search_fn, conn, *, query, embedder, **kwargs)`
  returns a `FallbackSearch(results, fallback)`. It calls `search_fn` (the
  surface's own `hybrid_search` binding, so existing patch points keep
  working); on an `EmbedError` it calls `search_fn` again with `fts_only=True`.

It **reuses** `hybrid_search`'s existing `fts_only` path: the second call is the
same code path `--fts-only` and the `NullEmbedder` (`BRAIN_EMBEDDER=none`)
already take. There is no second FTS implementation.

Details of the retry:

- `hybrid_search` runs the FTS leg *before* the query embed, and the embed is a
  Python call, not SQL, so an `EmbedError` never leaves the connection in an
  aborted transaction. Retrying on the same connection is safe.
- A caller-supplied `SearchDiagnostics` is reset to its defaults before the
  retry (so no field from the failed attempt survives), and `total_ms` is then
  rewritten to the wall clock of BOTH attempts. A 30 s embed timeout followed by
  a 40 ms FTS search must not report `total 40ms`.
- The query-embed LRU cache does not cache exceptions, so a later search after
  the embedder recovers embeds normally.
- `embed_ms` stays `None` after a fallback, so the existing `fts_only` meta key
  (derived from `embed_ms is None`) reads `true`, and `brain explain`'s
  `matched_filters["fts_only"]` is `true` because the second call really ran
  with `fts_only=True`.
- The wrapper logs the fallback at INFO, class-only. Not WARNING: the CLI
  configures no logging, so Python's last-resort handler would print a WARNING
  to stderr as a second, unformatted copy of the CLI's own notice. MCP logs INFO
  by default, so the server log still records it.

### Which failures fall back (ruling)

- **Transient `EmbedError`** (Ollama down, timeout, dropped connection, HTTP
  408/429/5xx): falls back. The notice says the embedder is unavailable and that
  semantic ranking will return when it is back.
- **Permanent `EmbedError`** (model not pulled, bad API key, an unusable
  response): **also falls back** (the recommended
  option). Search results are still useful, and refusing to search because of a
  config problem helps nobody. The notice names it as a configuration problem
  that a retry will not fix, and points at `brain doctor`.
- **`OllamaEmbedContextOverflow`** (the query itself is longer than
  `BRAIN_OLLAMA_EMBED_NUM_CTX`; the transport sends `truncate: false`): permanent,
  and falls back, but it is not a configuration fault, so `brain doctor` would be
  the wrong advice. Its notice says to shorten the query for semantic ranking
  (`SearchFallback.context_overflow`, detected by `isinstance` with a call-time
  import so the leaf does not load httpx/tiktoken). Added at review, 2026-10-23.
- **Anything else** (`psycopg.Error`, `PersonNotFound`, programming errors) is
  not caught and fails exactly as before.

### Visibility (never silent)

| Surface | Signal |
|---|---|
| `brain search` (table) | one-line notice on stderr; results on stdout; exit 0 |
| `brain search --json` (bare list) | stderr notice; **the list shape is unchanged** (see ruling below) |
| `brain search --json --meta` | the four `fallback*` keys in the envelope, plus `fts_only: true` |
| `brain explain` (table / `--json`) | stderr notice; `matched_filters.fts_only` is `true` |
| MCP `brain_search` | the four `fallback*` keys (always present, `null` when no fallback) plus `fts_only: true`; `fallback_note` is the short human note |

**Ruling — the bare `--json` list keeps its shape.** It is a frozen contract
(`tests/test_search_output_unchanged.py`; the `consult-brain` skill parses it),
and a list cannot gain a top-level key. Switching it to an object only when the
embedder is down would break a `jq '.[]'` consumer at exactly the moment it is
least expected. The machine-readable field lives in the opt-in `--json --meta`
envelope, which exists for this kind of metadata, and the stderr notice is
always printed. Adding a per-result key was rejected too: whether the vector leg
ran is a property of the search, not of a result.

All notice and field text is class-only (`describe_failure`). The exception
MESSAGE never reaches stderr, the JSON, or the MCP wire.

## Rulings per caller of `hybrid_search`

| Caller | Ruling | Why |
|---|---|---|
| `cli_search.search` (`brain search`) | **falls back** | in scope |
| `cli_search.explain` (`brain explain`) | **falls back** | same command family, same user; it is how a user debugs a search, and it must show the ranking the search really used |
| `mcp_server.brain_search` | **falls back** | in scope |
| `eval/runner.run_eval` | **unchanged, must not fall back** | a degraded row is the point: a transient failure scores 0.0 with `degraded=True` so record/gate refuse it, a permanent one aborts. Falling back would score an FTS-only ranking as if it were the hybrid ranking and corrupt baselines. Proven by `tests/test_search_fallback_eval_untouched.py` |
| `ask._retrieve_hybrid` (`brain ask`, MCP `brain_ask`) | unchanged in this PR; **follow-up** (F1) | Not folded in, because an answer synthesized over a silently weaker retrieval carries no marker in its prose. **Pre-existing behaviour, corrected 2026-10-23 at review** (this row first claimed the command "already exits non-zero with a clear message", which is true only when the chat call fails first): the CLI handler maps `OllamaUnavailable`, which is not an `EmbedError`. With `--no-loop`, `_retrieve` runs before any chat call; with a Voyage embedder the chat model stays up while the embed fails. In both cases `OllamaEmbedError` / `VoyageEmbedError` escapes `brain ask` as an unhandled traceback (exit 1, empty stderr), and MCP `brain_ask` has no `EmbedError` mapping either. Not fixed here: the CLI handler lives in `cli.py`, which is over the ceiling |
| `recall.recall` (`brain recall`, MCP `brain_recall`) | unchanged in this PR; **follow-up** (F2) | A budgeted context packer with its own output contract (`RecallResult`, the rendered context block); falling back there needs its own field and its own surface tests. **Pre-existing behaviour:** `cli_recall.py` has no `EmbedError` mapping, so an embed failure escapes `brain recall` as an unhandled traceback, like `brain ask` above. MCP `brain_recall` maps it to `INTERNAL_ERROR` |
| `graph_rag/fuse.py` (`graphrag search --mode fuse`) | unchanged | it already degrades: a failed vector arm is retried FTS-only and reported as `vector_arm_used=False`, and a dead FTS leg degrades to graph-only. Rewriting it onto the new helper would change its broader catch (any exception) for no user-visible gain |
| `demo.search_demo` | unchanged | FTS-only by default; `--with-embeddings` uses the deterministic offline `DemoEmbedder`, which cannot raise `EmbedError` |
| `ui/routes_search.py` (`brain ui`) | unchanged in this PR; **follow-up** | not a `hybrid_search` import (it calls an injected `search_fn`), so it was found by the `EmbedError` grep, not the call-site grep. It already answers `503 embedding_unavailable` with "retry with FTS-only search", and the client sends `fts_only`. An automatic fallback there needs a visible notice in the browser UI (a JS change and a browser test), which is its own PR |

## Affected files

- `src/brain/search_fallback.py` — new.
- `src/brain/cli_search.py` — both call sites go through the helper; stderr
  notice; envelope keys. Stays under 800.
- `src/brain/format_search.py` — `search_envelope_json(..., fallback=None)`
  inserts the four keys before `results`.
- `src/brain/mcp_server.py` — `brain_search` goes through the helper and merges
  the keys; tool docstring documents them. Its `except EmbedError` mapping is
  deleted as unreachable (`_wrap_embed_error` stays for the other tools).
  **Over the ceiling**: inline growth comment, module-docstring pointer, and
  the CLAUDE.md row's trail. The first commit (`ed893f5`) added a descriptive
  hop after `1054b52`. The review round touched the file again, so that hop
  was promoted to `5,074 (ed893f5)` and a descriptive hop was added for the
  review-round commit.
- `CLAUDE.md` — the `mcp_server.py` ceiling row.
- Tests: `tests/embed_down.py` (the failing-embedder double),
  `tests/test_search_fallback.py` (unit + DB),
  `tests/test_cli_search_fallback.py`, `tests/test_mcp_search_fallback.py`,
  `tests/test_search_fallback_eval_untouched.py`, and the lazy-import guard in
  `tests/test_import_cycles.py`; `tests/test_cli_search_meta.py` and
  `tests/test_search_output_unchanged.py` name the new envelope keys.
- Docs: `docs/cli-reference.md`, `docs/configuration.md` (MCP table),
  `skills/consult-brain/SKILL.md`, `skills/brain-ask/SKILL.md`, `CHANGELOG.md`.

## Follow-ups (recorded, not in this PR)

- **F1 — `brain ask` embed failures.** Map `EmbedError` in the `brain ask` CLI
  handler and in MCP `brain_ask` to a clean, class-only error (exit 1 /
  `INTERNAL_ERROR`), or decide to fall back there too with a marker in the
  answer. Today `--no-loop` and Voyage configs escape as an unhandled
  `EmbedError` traceback.
- **F2 — `brain recall` embed failures.** Same gap in `cli_recall.py`: an
  unhandled `EmbedError` traceback. Either map it, or fall back with a
  `fallback` field in `RecallResult` and its context block.
- **F3 — `brain ui` automatic fallback**, with a visible notice in the browser
  (the ruling table above).

## Risks

- **A silently weaker ranking.** Mitigated by the notice on every surface; the
  notice is the whole point of the decision.
- **Two FTS queries on a fallback.** The FTS leg ran once before the embed
  failed and runs again on retry. A few milliseconds against an embed failure
  that is usually a multi-second timeout; not worth threading partial state
  through `hybrid_search` (over the ceiling) to avoid it.
- **Telemetry.** `search_queries` records the fallback search like any other;
  `duration_ms` comes from the corrected `total_ms`. No schema change.
- **Eval leakage.** The helper is not imported by `brain.eval`; the eval test
  asserts both the behaviour (degraded row with no results even though FTS
  matches exist; permanent failure aborts) and the import boundary.
