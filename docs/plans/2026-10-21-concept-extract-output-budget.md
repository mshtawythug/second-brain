# Concept extraction: split, then salvage, a reply truncated at `num_predict` — plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/concept-extract-output-budget`, base `second-brain/master` `6948c6f`.

## 1. Symptom

The vault watcher's stderr log (`com.brain.watcher.err.log`, untimestamped) carries 51
`concept extraction: chunk N failed (chat failed after 2 attempts: …)` warnings:
44 × `Unterminated string starting at`, 5 × `Expecting value`, 2 × `Expecting
property name enclosed in double quotes`. **Every** decode position falls between
char 4,873 and char 8,488. Some recurred after the 2026-10-16 reinstall to `6948c6f`.

## 2. Hypothesis tested and REJECTED: `num_ctx` exhaustion

The brief proposed that PR #32 (`7545ce1`: `num_ctx=8192`, `shift:false`) cut the
output off because extraction's 8,000-token input cap leaves no room for output.

**False, on three independent legs:**

1. **Arithmetic.** `BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS` (8,000) is a *document head
   cap*, not a per-call input. The capped text is then split by `chunk_text` into
   chunks of at most `EXTRACT_CHUNK_TARGET_TOKENS + EXTRACT_CHUNK_OVERLAP_TOKENS` =
   1,600 cl100k tokens, one model call each. Worst case per call = 1,600 + 1,024
   frame reserve + 1,024 × 2 retry output = 4,672 ≤ 8,192 — and `config.py` already
   validates exactly this at load (`capped_call_budgets` lists
   "graphrag concept extract (one chunk)").
2. **Reproduction (real local Ollama, SYNTHETIC text only** — fictitious
   tool/org/project names, 1,578 cl100k tokens, the exact `_EXTRACT_SYSTEM_PROMPT`,
   `num_ctx` 8192, `shift`/`truncate` false, `format: json`, temperature 0):

   | `num_predict` | `done_reason` | `prompt_eval_count` | `eval_count` | chars | decode error |
   |---|---|---|---|---|---|
   | 1,024 (first attempt) | `length` | 2,277 | 1,024 | 2,956 | Unterminated string starting at 2949 |
   | 2,048 (the retry) | `length` | 2,277 | 2,048 | 5,850 | Unterminated string starting at 5845 |

   prompt + output = 4,325 tokens, nowhere near 8,192. The 2,048 row lands squarely in
   the production range (≈2.9 chars/token). A `num_ctx` cut would sit at ~5,900
   output tokens ≈ 15–20k chars — no production failure is anywhere near that.
3. **Pre-existing.** `num_predict` 1,024 → 2,048 predates PR #32; PR #32 neither
   caused nor was meant to fix this.

## 3. Root cause

On an entity-dense chunk `llama3.1:8b` emits one entry **per mention**, not per
distinct entity (the probe: 137 entries, 78 distinct, at 2,048 tokens), so it never
closes the array within the completion budget. `chat_json_with_client` correctly
classifies the decode failure as truncation and retries at 2× `num_predict`; the
retry truncates too; it raises `EnrichmentError`; `OllamaExtractor.extract` skips
**the whole chunk** — discarding every complete, valid entity the model had already
emitted. On exactly the chunks with the most concepts, extraction yields zero.

## 4. Design

Revised after review (team-lead, 2026-10-21): salvaging only the prefix of a
truncated reply keeps the head of the chunk and silently loses every entity after
the cut, which on a dense chunk is its whole tail. So a truncated chunk is
**split first, salvaged only at the bound**.

* **`brain.errors.ChatOutputTruncated(EnrichmentError)`** carries `partial` (the
  last reply's text) and `num_predict` (the budget it hit). Because it is a
  subclass, every existing `except EnrichmentError` caller's contract is
  unchanged. Its `str()` is the same `chat failed after 2 attempts: …` text, so
  no log line changes and the partial (document-derived) text never reaches a log.
* **`brain.chat.chat_json_with_client`** raises it instead of plain
  `EnrichmentError` when the **final** attempt's failure was the existing
  truncation classification (unterminated string, or a decode error at/after EOF).
  There is no wire change and no new signal: it reuses the classifier that
  already bumps `num_predict`.
* **`brain.json_salvage.salvage_array_items(text, key)`** is a new pure leaf
  module. It returns the complete JSON values of the array under `key` in a
  truncated prefix (`raw_decode` item by item, stopping at the first incomplete
  one).
* **`brain.graph_rag.chunk_split`** is a new module that owns the recursion.
  `extract_splitting_on_truncation` calls the model on a chunk. On
  `ChatOutputTruncated` it splits the piece roughly in half (`split_in_half`: the
  ingest chunker's paragraph → sentence → whitespace chain, target = half, no
  overlap, so the halves partition the text) and extracts each half. A piece is
  **salvaged instead of split** only at the bound:
  * it has been split `MAX_SPLIT_DEPTH` = 2 times already (a 1,600-token chunk
    bottoms out at four pieces of about 400 tokens), or
  * it is under `MIN_SPLIT_TOKENS` = 200, or
  * it will not split into two.

  Error handling: a malformed (non-truncated) sub-piece is skipped and counted,
  so its siblings still contribute. `OllamaUnavailable` from any piece still
  propagates. The chunk's own malformed reply keeps the existing skip-the-chunk
  policy. The module returns a frozen `ChunkExtraction` (entries + counts) and
  logs nothing.
* **`OllamaExtractor.extract`** routes every chunk through it. All entries feed
  the unchanged `_validate_entry` → `_finalize` path (presence check, noise
  filter, dedupe, per-doc cap). When a chunk truncated, one WARN gives the chunk
  index, pieces split, entries and pieces salvaged, and malformed pieces skipped,
  as **counts only**, never content.

**Cost.** A truncated call has already generated up to 1,024 + 2,048 tokens. Each
split level can repeat that per piece, so depth 2 caps one pathological chunk at
1 + 2 + 4 = 7 such calls (≈21.5k generated tokens, about ten minutes on
`llama3.1:8b` locally). This is worth it rather than "clearly worse":
* it only runs on chunks that today produce **zero** entities;
* the log shows 51 such chunks in total, not a per-document pattern;
* it runs in the background watcher, not on an interactive path;
* the bound is explicit and tested.

Chunks whose reply parses never enter the split path, so their cost is unchanged.

**Extractor version: NOT bumped** (stays `concepts-v6`). Output for every chunk
whose reply already parsed is byte-identical, because it takes the same single
call and the same validation path. Only chunks that previously contributed
nothing now contribute. A bump would force a full re-extract of the corpus (hours
of LLM time) for a change that touches only previously-failing chunks. The
consequence, stated honestly: documents already extracted with a skipped chunk
keep their thinner entity set until something re-extracts them (`--backfill`
after a future bump, or a content change). The log carries no document id, so
they cannot be targeted from it.

### Rejected alternatives

* **Salvage-only** (this plan's first draft): it keeps the head of the chunk and
  loses its tail. It was superseded by the split-first design above; salvage
  remains only as the leaf fallback.
* **Cap or dedupe the entity count in the prompt:** this changes extraction
  semantics for every chunk, which means `concepts-v7` and a full re-extract.
  A small model also follows count instructions unreliably.
* **Raise `EXTRACT_NUM_PREDICT`:** the model's per-mention listing is unbounded
  on dense text, so a bigger budget only moves the cliff. It costs more
  generation on every truncating chunk and grows the validated `num_ctx`
  requirement.
* **Use Ollama's `done_reason == "length"`** as the signal: it is authoritative,
  but `_chat_once` would have to return more than the content string, a
  wire-shape change across every caller, for no case the existing EOF
  classifier misses.

## 5. Affected files

| File | Change | Ceiling |
|---|---|---|
| `src/brain/errors.py` | + `ChatOutputTruncated` | under 800 |
| `src/brain/chat.py` | raise it on final truncation | under 800 |
| `src/brain/json_salvage.py` | NEW | under 800 |
| `src/brain/graph_rag/chunk_split.py` | NEW: split/salvage recursion | under 800 |
| `src/brain/graph_rag/extract.py` | call `chunk_split` + counts-only WARN | **over**: inline reason at the growth, docstring pointer, CLAUDE.md row (promote the trailing descriptive hop to `a977744` / 896, add a fresh descriptive hop) |
| `tests/test_json_salvage.py` | NEW | n/a |
| `tests/test_graphrag_chunk_split.py` | NEW: depth/floor/unsplittable bounds, split-then-salvage, error policy | n/a |
| `tests/test_chat.py` | truncation → typed error carrying the partial; malformed → NOT typed | n/a |
| `tests/test_graphrag_extract.py` | regressions: salvage under the floor, split keeps the tail, split-then-salvage at the floor | n/a |

## 6. Risks

* **The cut-off last entry is lost.** Salvage stops at the first incomplete item.
* **Are salvaged entries lower-confidence?** No. Each is a complete object the
  model emitted, and it passes the same validation, presence and cap pipeline as
  a fully parsed reply. `BRAIN_GRAPH_MAX_ENTITIES_PER_DOC` bounds any
  per-mention flood.
* **Overlap loss.** Halves have no overlap, so a concept straddling the split
  point may be missed by both halves. The chunker splits at paragraph or
  sentence boundaries, which keeps this rare.
* **Other `chat_json` callers** see a subclass of the error they already catch.
  None catches `ChatOutputTruncated`, so their behaviour is unchanged.

## 7. TDD + mutation proofs

Recorded in §8 after implementation: for each new test, the five clauses — passes
unmutated; the mutation lands (declared count); the test goes red; it fails on the
targeted assertion; restored → green.

## 8. Mutation proofs

Harness: a copy of `src/` + `tests/` in the session scratchpad, imported ahead of
the editable install, with its own sibling test DB. The worktree was never mutated
while a suite ran. Each row records the five clauses:

1. the test passes unmutated;
2. the mutation lands (1 site matched, file differs);
3. the test goes red;
4. it fails on the **targeted** assertion (quoted);
5. it is green again after restore.

All rows passed all five clauses on 2026-10-21.

| # | Test | Mutation | Red on (clause 4) |
|---|---|---|---|
| E1 | `test_graphrag_extract::…salvages_complete_entities_from_output_truncated_twice` | `chunk_split`: salvage → `[]` | `assert set() == {…Zorvex…}` |
| E2 | `…splits_a_truncated_chunk_and_keeps_its_tail` | never split (`if False`) | `{('tool','Zorvex')} == {…Lumkai…}`: the tail is lost |
| E3 | `…split_then_salvages_a_half_still_truncated_at_the_floor` | floor `>= min_tokens` → `>= 0` | WARN reads `split 2 … salvaged 3 … 3 piece(s)` |
| E4 | `…splits_a_truncated_chunk_and_keeps_its_tail` | extract.py also logs `result.entries` | `'Zorvex' not in caplog` |
| U1 | `test_graphrag_chunk_split::…split_depth_is_bounded…` | `depth < max_depth` → `<=` | `assert 15 == 7` |
| U2 | `…a_piece_under_the_floor_is_salvaged_without_splitting` | floor → `>= 0` | `assert 7 == 1` |
| U3 | `…a_piece_at_the_floor_is_split` | `>= min_tokens` → `>` | `assert 1 == 3` |
| U4 | `…a_piece_that_will_not_split_is_salvaged` | `len(pieces) < 2` → `< 1` | `assert 3 == 1` |
| U5 | `…split_then_salvage_keeps_the_parsed_half_and_the_salvaged_prefix` | salvage → `[]` | `({'name':'A'},) == ({'name':'A'},{'name':'B'})` |
| U6 | `…a_malformed_sub_piece_is_skipped_and_counted…` | `pieces_failed += 1` → `+= 0` | `assert 0 == 1` |
| U7 | `…ollama_unavailable_in_a_sub_piece_propagates` | drop `except OllamaUnavailable: raise` | `DID NOT RAISE OllamaUnavailable` |
| U8 | `…the_chunks_own_malformed_reply_propagates` | swallow `EnrichmentError` at the top | `DID NOT RAISE EnrichmentError` |
| U9 | `…split_in_half_partitions_the_text…` | `overlap_tokens=0` → `10` | `assert [...] == [...]` |
| U10 | `…a_reply_that_parses_is_returned_untouched_with_no_split` | `truncated` → `return True` | `assert not True` |
| M2a | `test_chat::…truncated_twice_raises_typed_error_with_partial` | `if truncated_text is not None` → `if False` | raises plain `EnrichmentError` past `pytest.raises(ChatOutputTruncated)` |
| M2b | same | budget doubled on attempt 2 too | `assert 512 == 256` |
| M2c | same | partial appended to the message | `'still trunc' not in …` |
| M3 | `…malformed_twice_is_not_truncation` | truncation classifier → `if True` | `assert not True` (isinstance) |
| M4 | `…truncation_then_malformed_is_not_truncation` | drop the per-attempt reset | `assert not True` (isinstance) |
| S1 | `test_json_salvage::…stops_at_a_malformed_separator` | separator check → EOF only | `[{'name':'A'}, 'name', 'B'] == [{'name':'A'}]` |
| S2 | `…key_not_followed_by_an_array_is_empty` | regex drops `\[` | `['oops'] == []` |
| S3 | `…tolerates_pretty_printed_whitespace` | whitespace set → `" "` | `[] == [{…}]` |
| S4 | `…skips_the_key_appearing_earlier_as_a_value` | regex drops `\s*:\s*\[` | `[] == [{'name':'Zorvex'}]` |
| S5/S6′ | `…before_a_mid_string_cut`, `…cut_between_items` | stop returns `[]` | `[] == [{…}]` (both) |
| S7 | `…empty_array…`, `…cut_inside_the_first_item…` | stop returns `items or [None]` | `[None] == []` (both) |
| S8 | `…missing_key_is_empty` | missing key returns `[text]` | `[…] == []` |
| S9 | `…closed_array_returns_every_item` | keep dict items only | `[{'b':[3]}] == ['a', 2, {'b':[3]}]` |

**One mutation was too weak, and the record says so.** S6, `pos += 1` → `+= 2`
on the cut-between-items test, survived. It matched two sites, and skipping one
extra character before an EOF changes nothing. The test is pinned instead by
S6′ (stop returns `[]`). The salvage loop was also simplified in the course of
these proofs: an explicit `]`/EOF check before `raw_decode` was dead-equivalent,
because `raw_decode` refuses both. It was removed rather than left untestable.
