# RM-55 — Summarize a tag (`brain tags summarize`, MCP `brain_tags_summarize`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** A short, cited synthesis of what one tag's documents say, from the terminal and over MCP.

**Architecture:** A new leaf `src/brain/tag_summary.py` reads the N most recent non-draft documents
that carry the tag, builds a bounded prompt from their `documents.summary` values (titles where a
summary is missing), makes one `chat.chat_json` call, and returns a cleaned synthesis with in-range
`[N]` citations only. `brain tags summarize` (in `cli_tags.py`) and `brain_tags_summarize` (in
`mcp_tools/tags.py`) are thin surfaces over it. The MCP surface applies the F6 lens to the
**input** query, so a confidential document never reaches the prompt.

**Tech Stack:** Python 3.11, Typer, psycopg 3, the MCP SDK (`mcp` 2.x), pytest.

**Spec:** `docs/specs/2026-10-23-roadmap-completion-design.md`, section "RM-55 — Summarize a tag"
(and Appendix B, row RM-55). It depends on RM-21 (the `tags` sub-app and `mcp_tools/tags.py`, #87)
and RM-02 (`describe_ask_failure`, #64), both merged.

**Base:** `second-brain/master` at `dda6ba4e`. Branch `feat/rm-55-tag-summary`, worktree
`.claude/worktrees/impl-rm55`.

## Global Constraints

- Default N: **20** (RM-55.D1, spec).
- Each summary is cut to **120 tokens**. The whole input is capped at
  `TAG_SUMMARY_MAX_INPUT_TOKENS = 3000`, a named constant declared as a `CallBudget` "as in RM-54"
  (spec).
- `OllamaUnavailable`, `OllamaContextOverflow` and `ChatOutputTruncated` each map to their own
  fixed, class-only sentence. The CLI exits 1 and MCP returns `INTERNAL_ERROR` (spec, AC3).
- MCP excludes confidential documents from the **input** unless `include_confidential` (spec, AC2).
- The synthesis cites only in-range documents (spec, AC1).
- The `CallBudget` fits the default `num_ctx` of 8,192 (spec, AC4).
- Out of scope: caching summaries (spec). No migration, no new config knob (spec row `config.py`).
- Repo rules: `ruff check`, `mypy src/` and the named tests all pass; there is no local full suite;
  every pytest run holds `.claude/full-suite.lock`; no PII (synthetic fixtures only); no
  monkey-patching of production modules (`monkeypatch.setattr` on the `brain.cli` patch points and
  on `brain.chat.chat_json` is the standard test double, as `tests/test_mcp_ask_fallback.py` uses);
  no real Ollama generation in any test or E2E.

## Affected files

Counts from `wc -l` at `dda6ba4e`. All of them stay under 800, so there are no ceiling rows and
`docs/ceiling-table.md` is not touched. `mcp_server.py` (5,174) and `cli.py` are **not** edited.

| File | Now | After (est.) | Change |
|---|---:|---:|---|
| `src/brain/tag_summary.py` | new | ~230 | The leaf: fetch, prompt, clean, failure sentences. |
| `src/brain/citations.py` | new | ~45 | `[N]` helpers lifted out of `ask.py` (D4). |
| `src/brain/ask.py` | 723 | ~705 | Imports the lifted helpers; net negative. |
| `src/brain/ask_errors.py` | 70 | ~74 | The Ollama-down sentence becomes a shared constant (D9). |
| `src/brain/ollama_budget.py` | 187 | ~200 | `TAG_SUMMARY_*` constants and one `CallBudget`. |
| `src/brain/cli_tags.py` | 223 | ~300 | `summarize` command. |
| `src/brain/mcp_tools/tags.py` | 103 | ~175 | `brain_tags_summarize` (a tool module stays under 400). |
| `src/brain/mcp_tool_classes.py` | 193 | ~194 | `brain_tags_summarize` added to `READ_ONLY_TOOLS` (D7). |
| `tests/test_tag_summary.py` | new | — | nodb: prompt, budget packing, cleaning, citations, failures. |
| `tests/test_citations.py` | new | — | nodb: the lifted helpers. |
| `tests/test_tag_summary_fetch.py` | new | — | DB: the fetch statement (lens, draft, order, limit). |
| `tests/test_cli_tags_summarize.py` | new | — | DB: the CLI with a fake chat. |
| `tests/test_mcp_tools_tags.py` | 196 | ~330 | DB: the MCP tool with a fake chat. |
| `tests/test_ollama_num_ctx.py` | 498 | ~515 | The pinned budget list and a prompt-frame measurement. |
| `tests/test_mcp_http_readonly.py` | — | — | `REGISTERED 51→52`, `READS 28→29`. |
| `docs/cli-reference.md`, `docs/configuration.md`, `CHANGELOG.md`, `src/brain/resources/examples.md` | | | Docs (Task 6). |

### Expected conflicts with #123 (RM-54, approved, not merged)

RM-55 touches none of `search_pipeline.py`, `format_search.py` or `cli_search.py`. It will conflict
in:

- `src/brain/ollama_budget.py`: both add a constant after `CHAT_DEFAULT_NUM_PREDICT` and a
  `CallBudget` at the end of `capped_call_budgets()`. The resolution keeps both, with RM-54's first.
- `tests/test_ollama_num_ctx.py`: both edit `test_default_capped_call_requirements_are_pinned` and
  the caller-name set. The resolution is `[2736, 4672, 8120, 1792, 5048]`.
- `CHANGELOG.md` `[Unreleased]`, and probably `docs/cli-reference.md` and
  `src/brain/resources/examples.md` (adjacent insertions).
- `docs/configuration.md`: #123 rewrites the `OLLAMA_HOST` row. RM-55 does **not** edit that row;
  it only adds an MCP-tool row next to line 898, so that file should merge cleanly.

**Recommendation:** merge #123 first, then `git merge second-brain/master` into this branch (no
rebase) and resolve as above. If RM-55 is ready first, the same resolution applies to #123.

## Design decisions

**D1. Which documents, and in what order.** Options: (a) `ingested_at DESC`; (b)
`coalesce(doc_date, ingested_at) DESC`, the "discovery date" that `ui/queries.py` already uses;
(c) the most-accessed first. **Recommend (b)**, with `id` as the tiebreak so the order is stable.
"Most recent" means when the thing happened, not when it was ingested.

**D2. Drafts.** Options: include drafts, or exclude them (`NOT d.draft`). **Recommend excluding
them.** Every browseable-corpus read excludes drafts. A tag summary describes the corpus, and an
unsent draft is not part of it yet. Under the F6 residue audit, this statement filters `draft`, so
it must carry the `sensitivity` predicate (outcome 1, gated). It does:
`(%s::text IS NULL OR d.sensitivity = %s)`, the same shape as `tag_mapping.tag_counts`.

**D3. CLI confidentiality tier.** Options: (a) both tiers, with no flag; (b) exclude by default,
with `--include-confidential`. **Recommend (a).** The terminal is inside the F6 boundary. This
matches `brain tags list`/`suggest-merges` and `brain ask` (which defaults to
`exclude_confidential=False`). The help text names the egress, as RM-54's does: "Summaries of the
tag's documents go to the chat model at OLLAMA_HOST."

**D4. "Sanitized the way `ask` sanitizes answers".** `ask._sanitize_answer` and `_CITATION_RE` are
private to `ask.py`. Options: (a) import the private names across modules; (b) copy them; (c) lift
them into a new leaf `brain/citations.py`, which `ask.py` then imports. **Recommend (c).** It is
DRY, `ask.py` shrinks, and the behaviour stays identical. `test_ask*` pins it, and those tests run
unchanged. The public names are `strip_dangling_citations(text: str, count: int) -> str`, which is
the body of `_sanitize_answer`, and `cited_refs(text: str, count: int) -> tuple[int, ...]`, the
in-range refs, de-duplicated, in first-appearance order (the loop core of `_build_citations`).
`ask._build_citations` keeps building `Citation` objects on top of `cited_refs`.

**D5. Terminal and control-character safety of the output.** The synthesis is untrusted model
output that has seen untrusted document text, and it reaches a terminal and a hosted model. The
cleaner is `clean_synthesis(raw: object, *, count: int) -> str`:
1. A non-`str` raises `EnrichmentError` (class-only). It is malformed, not empty.
2. Per line from `str.splitlines()`: `preview_filter.strip_sequences` (which removes escape
   sequences with their payloads), then `ingest.text_safety.one_line` (which removes every `C*`
   character: C0, C1, the bidi overrides, tag characters and lone surrogates). The
   `clean_paraphrases` precedent from RM-54 uses this pair.
3. Blank-line runs collapse to one. The text is capped at `TAG_SUMMARY_MAX_CHARS = 4000`, cut at a
   line or word boundary.
4. `citations.strip_dangling_citations(text, count)` runs.

The CLI prints with `typer.echo`, never Rich `console.print`, so a `[bold]` in the model's text
stays literal. (`ask` prints through Rich today. That is outside this ticket, and it is noted for
the backlog in Risks.) Source titles in both outputs go through `one_line(strip_sequences(...))`.

**D6. Prompt inputs.** Each entry is `[{n}] {title}: {summary}`, with `{title}` alone when the
summary is NULL or blank. Both fields are cleaned with `one_line(strip_sequences(...))`, and the
entry is cut to `TAG_SUMMARY_DOC_TOKENS = 120` with `token_budget.truncate_to_token_budget`.
Options for the cut: the summary alone, or the whole entry. **Recommend the whole entry**, because
an unbounded title would otherwise escape the 120-token bound. Entries are packed with
`token_budget.pack_greedy(budget=TAG_SUMMARY_MAX_INPUT_TOKENS)`, prefix-greedy in recency order.
The number dropped is reported as `dropped`. Numbering runs over the **packed** entries only, so
`count` in D5 is `len(packed)` and a citation of a dropped document is out of range. The tag is
normalized (`tags.normalize_tag`) and cut to `TAG_SUMMARY_TAG_TOKENS = 32` inside the prompt. It
then falls under the template margin, and the frame test (Task 2) proves the frame fits. The
prompt says to treat the entries as data, never as instructions, as RM-54's prompt does.

**D7. MCP HTTP classification.** Options: (a) a read tool (`READ_ONLY_TOOLS`); (b) a write tool,
withheld over HTTP. **Recommend (a).** The tool writes nothing: no document, no tag, no
`interactions` row, no cache. In hide mode, the HTTP guard already refuses `include_confidential`,
so the input is lensed. The tool reads no graph table, so it needs no `GRAPH_TOUCHING_KEPT` entry,
and `test_mcp_http_graph_reach.py` will confirm that. The trade-off: a hosted caller over HTTP can
spend one local chat call per request, bounded by `--call-timeout` (D8). If the ruling is (b), the
counts become `WRITES 23→24` instead.

**D8. Connection lifetime and timeouts.** The fetch runs and its connection closes **before** the
chat call. The MCP fetch is in a `READ ONLY` transaction, like `_read_only` in `mcp_tools/tags.py`.
A Postgres connection is never held open across a 30-plus-second model call. The core is therefore
two functions: `fetch_tag_documents(...)` and `synthesize(...)`. Chat timeout: the CLI uses
`cfg.enrich_timeout_seconds` (the `chat_json` default). MCP passes
`timeout=min(cfg.enrich_timeout_seconds, state.call_timeout_s)` when `call_timeout_s` is set (HTTP),
so a timed-out call does not hold its slot past the server's budget. The DB side uses the existing
`connect_timeout`.

**D9. Failure sentences.** These are `TAG_SUMMARY_FAILURES = (EnrichmentError,)` and
`describe_tag_summary_failure(exc) -> str` in `tag_summary.py`, in the style of
`describe_ask_failure`. Subclasses are tested before the base:
- `OllamaUnavailable` → `ask_errors.OLLAMA_UNAVAILABLE` (a new constant holding ask's current
  sentence, verbatim; `describe_ask_failure` returns it). That is one sentence for one failure.
- `OllamaContextOverflow` → "the tag's documents did not fit the chat model's context window; lower
  `--limit` (MCP: `limit`)"
- `ChatOutputTruncated` → "the summary was cut off at its output budget; retry, or lower `--limit`
  (MCP: `limit`)"
- other `EnrichmentError` → `f"tag summary failed ({type(exc).__name__})"`

A Postgres failure on the CLI prints `describe_failure(exc)`, class-only, and exits 1. On MCP it
goes through `ctx.wrap_db_error`. An embedder failure cannot happen, because only `count_tokens`
(offline tiktoken) is used.

**D10. Completion budget.** Options: `CHAT_DEFAULT_NUM_PREDICT` (256), or a new
`TAG_SUMMARY_NUM_PREDICT = 512` (ask's synthesize budget). **Recommend 512.** At 256, a 20-document
synthesis with citations is likely to hit `ChatOutputTruncated`. The required `num_ctx` is
3,000 + 1,024 + 512×2 = **5,048** ≤ 8,192.

**D11. No documents.** If no visible document carries the tag, no chat call is made. The CLI prints
`no documents tagged '<tag>'` on stderr and exits 0. MCP returns
`{tag, summary: "", citations: [], considered: 0, dropped: 0}`. Under the lens, "only confidential
documents carry it" and "unknown tag" give the same answer, which is the F6 requirement
(`brain_tags_list` hides a confidential-only tag the same way).

**D12. Output shape.** Only the cited sources are returned, as `ask` does:
`{tag, summary, citations: [{ref, document_id, title, date}], considered, dropped}`. `considered`
is the number packed into the prompt and `dropped` the number that did not fit. The CLI prints the
synthesis, then `━ Sources` with `[ref]  title (id[:8], YYYY-MM-DD)`, then a dim footer
`N document(s) considered, M left out by the input budget` when M > 0. `--json` emits the dict.

**D13. Limits.** The CLI's `--limit` defaults to 20, with `min=1, max=TAG_SUMMARY_LIMIT_CEILING`
(50). MCP's `limit` defaults to 20, and a value outside 1..50 is refused with `INVALID_PARAMS`, as
`_check_limit` refuses one. The 3,000-token input cap binds before 50 × 120 tokens does, so the
ceiling bounds only the fetch. A tag that is empty after normalization is refused (CLI exit 1;
MCP `INVALID_PARAMS`).

**D14. Logging.** No new log line carries a summary, the prompt or the synthesis. If a debug line is
added at all, it carries the tag's document **count** and ids only. The repo's log-privacy scans
(`test_log_privacy_rules`, `test_log_privacy`) are in the named test set.

## Interfaces (`src/brain/tag_summary.py`)

```python
@dataclass(frozen=True)
class TagDocument:          # one fetched row
    document_id: str
    title: str
    summary: str | None
    date: datetime

@dataclass(frozen=True)
class TagCitation:
    ref: int
    document_id: str
    title: str              # cleaned, one line
    date: datetime
    def to_json(self) -> dict[str, Any]: ...

@dataclass(frozen=True)
class TagSummary:
    tag: str
    summary: str
    citations: tuple[TagCitation, ...]
    considered: int
    dropped: int
    def to_json(self) -> dict[str, Any]: ...

def fetch_tag_documents(conn, tag: str, *, limit: int, sensitivity: str | None) -> list[TagDocument]
def build_prompt(tag: str, docs: Sequence[TagDocument], *, cost: TokenCost) -> tuple[str, int]
    # (prompt, number of docs packed); docs[:packed] are the numbered entries
def clean_synthesis(raw: object, *, count: int) -> str
def synthesize(tag: str, docs: Sequence[TagDocument], *, chat: ChatJson, cfg: Config,
               cost: TokenCost, timeout: float | None = None) -> TagSummary
def describe_tag_summary_failure(exc: BaseException) -> str
TAG_SUMMARY_FAILURES: tuple[type[BaseException], ...]
```

`ChatJson` is `brain.ask.ChatJson`, imported under `TYPE_CHECKING` only, so the leaf does not load
`ask` at runtime. `chat_json` is called with `schema={"summary": "str"}`,
`num_predict=TAG_SUMMARY_NUM_PREDICT`, and `timeout` only when it is not `None`.

## Tasks

### Task 1: `citations.py` lift (nodb)

- Tests (`tests/test_citations.py`): `test_strip_keeps_in_range_markers_verbatim`,
  `test_strip_removes_out_of_range_and_zero_with_lead_space` (`"a [0] b [3] c [2]"`, count 2 →
  `"a b c [2]"`), `test_cited_refs_dedupes_in_first_appearance_order`
  (`"[2] x [1] [2] [9]"`, 2 → `(2, 1)`).
- Move the helpers out of `ask.py` (D4). The existing `tests/test_ask.py` and `tests/test_ask_*.py`
  citation tests are the "keeps ordinary behaviour" half and must pass unchanged.
- Commit: `refactor: lift ask's citation helpers into a leaf (RM-55)`.

### Task 2: budget constants and `CallBudget` (nodb)

- `ollama_budget.py`: `TAG_SUMMARY_MAX_INPUT_TOKENS = 3000`, `TAG_SUMMARY_DOC_TOKENS = 120`,
  `TAG_SUMMARY_TAG_TOKENS = 32`, `TAG_SUMMARY_NUM_PREDICT = 512`, and
  `CallBudget(caller="tag summary", input_knob="TAG_SUMMARY_MAX_INPUT_TOKENS", ...)` appended to
  `capped_call_budgets()`. All are here, not in the leaf, for RM-54's reason: this module defines
  every number the load-time check reads.
- Tests (`tests/test_ollama_num_ctx.py`): the pinned list gains `5048`; the default-fit test gains
  `"tag summary"`. A new `test_tag_summary_prompt_frame_fits_the_reserve` builds the prompt with
  zero documents and a 32-token tag. It asserts that the frame's cost is ≤
  `PROMPT_FRAME_RESERVE_TOKENS - CHAT_TEMPLATE_MARGIN_TOKENS`, measured the way the enricher frames
  are measured there. A "refuses" twin: `num_ctx_violations(5047, budgets)` names `tag summary`.
- Commit: `feat: declare the tag-summary call budget (RM-55)`.

### Task 3: the leaf, pure parts (nodb)

Tests in `tests/test_tag_summary.py`, using a fake `cost` (`len(text.split())`) and a fake `chat`
that records prompts:

| Refuses / bounds | Keeps ordinary behaviour |
|---|---|
| `test_out_of_range_citation_is_stripped_and_not_listed` (reply cites `[7]` with 3 packed) | `test_in_range_citations_map_to_their_documents_in_first_appearance_order` |
| `test_citation_of_a_budget_dropped_document_is_out_of_range` | `test_numbering_follows_recency_order` |
| `test_each_entry_is_cut_to_120_tokens` (a 500-word summary) | `test_short_entries_pass_whole` |
| `test_input_stops_at_3000_tokens_and_reports_dropped` (40 × 120-token entries → 25 packed, 15 dropped) | `test_twenty_short_documents_all_fit` |
| `test_missing_or_blank_summary_falls_back_to_title` | `test_summary_is_used_when_present` |
| `test_escape_sequences_and_controls_are_removed_from_the_synthesis` (CSI, OSC 8 with payload, C1 `\x9b`, `‮`, a lone `\ud800`, `\x00`) | `test_newlines_and_ordinary_unicode_survive` (accents, CJK, an emoji) |
| `test_escape_sequences_in_titles_and_summaries_never_reach_the_prompt` | `test_titles_reach_the_prompt` |
| `test_a_non_string_summary_raises_enrichment_error` | — |
| `test_synthesis_is_capped_at_4000_chars` | — |
| `test_no_documents_makes_no_chat_call` | — |
| `test_each_failure_family_has_its_own_fixed_sentence` (parametrized over the three subclasses and the base; asserts that a secret in the exception message is absent from the output) | `test_ollama_down_sentence_is_shared_with_ask` |

- Implement `build_prompt`, `clean_synthesis`, `synthesize`, `describe_tag_summary_failure` and
  `TAG_SUMMARY_FAILURES` (D5, D6, D9, D10, D11). The module docstring states the egress (OLLAMA_HOST)
  and the "input lens is the caller's job" contract.
- Commit: `feat: tag summary leaf, prompt and cleaning (RM-55)`.

### Task 4: fetch (DB) and the CLI (DB)

- `fetch_tag_documents` (D1, D2), with one statement:
  `WHERE d.tags @> ARRAY[%s]::text[] AND NOT d.draft AND (%s::text IS NULL OR d.sensitivity = %s)
  ORDER BY coalesce(d.doc_date, d.ingested_at) DESC, d.id LIMIT %s`, parameterized. A comment next
  to it marks it F6-gated (outcome 1).
- Tests (`tests/test_tag_summary_fetch.py`), with synthetic documents:
  `test_lens_normal_excludes_confidential` / `test_lens_none_includes_both_tiers`;
  `test_drafts_are_excluded` / `test_non_drafts_with_the_tag_are_included`;
  `test_order_is_discovery_date_desc_then_id`; `test_limit_caps_the_fetch`;
  `test_other_tags_are_not_matched` (`nebula` vs `nebulae`).
- `cli_tags.py`: `summarize(tag, --limit, --json)` (D3, D12, D13). The chat comes from a
  `_build_chat(cfg)` and the cost from `_build_embedder(cfg).count_tokens`, both delegating to the
  `brain.cli` patch points as `cli_search.py` does, so tests patch one place. The connection closes
  before `synthesize` (D8). `TAG_SUMMARY_FAILURES` → `tags summarize: <sentence>` on stderr, exit 1;
  `psycopg.Error` → `describe_failure`, exit 1.
- Tests (`tests/test_cli_tags_summarize.py`), with a fake chat:
  `test_prints_synthesis_and_only_cited_sources` / `test_json_shape`;
  `test_cli_includes_confidential_documents` (D3);
  `test_ollama_down_overflow_truncated_each_print_their_sentence_and_exit_1` (parametrized,
  asserting the exception's text is absent); `test_unknown_tag_prints_notice_exit_0_no_chat`;
  `test_empty_tag_after_normalization_is_refused`; `test_rich_markup_in_reply_stays_literal`;
  `test_escape_sequence_in_reply_never_reaches_stdout`; `test_limit_above_50_is_refused`;
  `test_tag_is_normalized_before_lookup` (`"Nebula Notes"` finds `nebula-notes`);
  `test_summarize_is_listed_in_tags_help`.
- Commit: `feat: brain tags summarize (RM-55)`.

### Task 5: MCP tool (DB)

- `brain_tags_summarize(tag: str, include_confidential: bool = False, limit: int = 20)` in
  `mcp_tools/tags.py`, registered with `app.tool()(...)` in `register`. The fetch runs inside
  `_read_only` with `sensitivity=ctx.confidential_lens(include_confidential)`, then the connection
  is released, then `chat.chat_json` is resolved at call time (`from .. import chat`) and passed to
  `synthesize` (D8). Failures go to `ctx.error(INTERNAL_ERROR, describe_tag_summary_failure(exc))`.
  The docstring states the lens, that the tool is read-only, the limits and the egress. Add the tool
  to `READ_ONLY_TOOLS` and bump the counts in `test_mcp_http_readonly.py` (D7).
- Tests (`tests/test_mcp_tools_tags.py`), with a fake chat that records prompts:
  `test_summarize_excludes_confidential_from_the_prompt_by_default` (asserts on the recorded prompt,
  not only on the return, as `test_ask_confidential_egress.py` does) /
  `test_summarize_include_confidential_reaches_both_tiers`;
  `test_confidential_only_tag_answers_like_an_unknown_tag`;
  `test_summarize_cites_only_in_range_documents`;
  `test_summarize_failures_are_internal_error_with_fixed_sentence` (parametrized over the three);
  `test_summarize_limit_is_bounded` (0 and 51 refused; 1 and 50 accepted);
  `test_summarize_empty_tag_is_invalid_params`; `test_summarize_writes_nothing` (snapshot, as
  `test_neither_tool_writes` does); `test_summarize_timeout_is_capped_by_call_timeout`
  (`call_timeout_s=5` → the fake chat sees `timeout=5`; unset → no override);
  `test_summarize_db_failure_is_class_only`. `test_both_tools_are_advertised...` gains the third
  tool.
- Named suites run alongside: `test_mcp_tool_registration.py`, `test_mcp_tools_seam.py`,
  `test_mcp_http_readonly.py`, `test_mcp_http_graph_reach.py`, `test_mcp_http_confidential.py`.
- Commit: `feat: brain_tags_summarize MCP tool (RM-55)`.

### Task 6: docs, CHANGELOG, examples

- `docs/cli-reference.md` `## brain tags`: the `summarize` line, plus one bullet on the input
  (N most recent non-draft, summaries or titles, the 3,000-token cap), the egress and the exit codes.
- `docs/configuration.md`: an MCP-tool row for `brain_tags_summarize` next to
  `brain_tags_suggest_merges` (params, the lens, read-only, and that it is served over HTTP).
- `CHANGELOG.md` `[Unreleased]`: `### Added — brain tags summarize ... (RM-55)`.
- `src/brain/resources/examples.md`: one `brain tags summarize` example, with a synthetic tag and
  no shell expansion (RM-53's rule). `tests/test_examples*.py` runs.
- Commit: `docs: brain tags summarize (RM-55)`.

## Mutation proofs

Each is run in a `git archive` copy of the branch, deleted afterwards. Each mutation is confirmed to
apply (by a `grep` of the mutated line) before its test runs. Expected result: the named test fails.

1. Drop the `sensitivity` predicate in `fetch_tag_documents` → `test_lens_normal_excludes_confidential`
   and `test_summarize_excludes_confidential_from_the_prompt_by_default` fail.
2. Pass `None` instead of `ctx.confidential_lens(...)` in the MCP tool → the same MCP test fails.
3. Drop `NOT d.draft` → `test_drafts_are_excluded` fails.
4. Skip `strip_dangling_citations` in `clean_synthesis` → `test_out_of_range_citation_is_stripped...`
   fails.
5. Number the fetched documents instead of the packed ones → `test_citation_of_a_budget_dropped_document_is_out_of_range` fails.
6. Remove `strip_sequences` (leaving `one_line`) → the OSC 8 payload case fails (its URL text
   survives).
7. Remove the per-entry 120-token cut → `test_each_entry_is_cut_to_120_tokens` fails.
8. Change `TAG_SUMMARY_MAX_INPUT_TOKENS` to 30000 → the pinned-budget test fails.
9. Reorder `describe_tag_summary_failure` so that the `EnrichmentError` base is tested first →
   the parametrized sentence test fails for each subclass.
10. Interpolate `{exc}` into the generic sentence → the secret-absent assertion fails.
11. Print with Rich `console.print` → `test_rich_markup_in_reply_stays_literal` fails.
12. Remove the tool from `READ_ONLY_TOOLS` → the `test_mcp_http_readonly.py` counts fail.

## Security considerations

- **Untrusted input:** document titles and summaries (which come from email, web and Slack bodies)
  and the model's reply. Both directions are cleaned (D5, D6). The prompt frames entries as data.
  Prompt injection cannot be eliminated, but its blast radius is one cleaned text with in-range
  citations. The tool cannot write, call another tool or widen its lens.
- **Terminal escape sequences:** `preview_filter.strip_sequences` + `one_line` on every string that
  reaches stdout or MCP; `typer.echo`, never Rich markup.
- **Confidential gating:** the MCP lens sits in the fetch predicate, so the prompt never sees a
  confidential row; the test asserts on the transmitted prompt. The CLI uses both tiers inside the
  trust boundary (D3). The HTTP guard already refuses `include_confidential` in hide mode. The
  documents fetched, `considered` and `dropped` are all computed over the lensed set only.
- **Logs:** titles and ids only, and in practice no new log lines (D14).
- **Timeouts:** chat has the explicit `enrich_timeout_seconds`, capped by `call_timeout_s` over
  HTTP. Postgres has `connect_timeout`. No connection is held across the chat call (D8).
- **SQL:** parameterized only. The tag is a bound parameter, never formatted into the statement.

## Memory updates

`cli.md` (the `brain tags summarize` command and the `brain_tags_summarize` MCP tool, its HTTP
class) and `types.md` (`TagDocument`, `TagCitation`, `TagSummary`). No schema, extractor or search
change. `MEMORY.md` index lines are updated if their RM range changes.

## Risks

- **Docker is down.** Tasks 4 and 5 and the DB half of the mutation proofs cannot run until the
  user starts it. This is the same blocker #123 has. Tasks 1–3 and 6 are nodb and can land first.
- **Conflicts with #123**, listed above. They are mechanical.
- **A very large tag:** N caps the fetch, and the 3,000-token cap bounds the prompt. The predicate
  is `tags @> ARRAY[%s]::text[]`, not `%s = ANY(tags)`, because only the containment form can use
  the GIN index `documents_tags_idx`. A tag with 10k documents then costs one indexed, `LIMIT`ed
  query plus a sort of its matches.
- **Summary coverage:** documents without `summary` (when `brain enrich` has not run) fall back to
  titles, which gives a thinner synthesis. The footer's counts make this visible. Reporting how many
  entries were title-only is an optional extra field (`title_only`), which I would add only if the
  ruling asks for it.
- **HTTP cost (D7):** a remote caller can trigger local chat calls. They are bounded per call by
  `--call-timeout`, and the server's own concurrency limits apply.
- **Backlog note, not this ticket:** `brain ask` prints its answer through Rich `console.print`, so
  Rich markup and escape sequences in a model answer reach the terminal. That is worth a ticket of
  its own.
