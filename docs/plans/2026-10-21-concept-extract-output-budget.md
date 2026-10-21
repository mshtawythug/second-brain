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
  `ChatOutputTruncated` it splits the piece in two with `split_in_half` and
  extracts each half. `split_in_half` cuts ONCE, at the boundary nearest the
  token midpoint, at the coarsest level (paragraph, then sentence, then line,
  then word) that leaves each side at least 25% of the tokens. It falls back to
  the token midpoint itself, and the two pieces concatenate back to the exact
  input. *(Corrected after the audit of `5b4940d`: the first version packed
  greedily through `chunk_text` with a half-size target. That could return three
  or four pieces, so the 7-call bound below was false; a measured repro made 13
  calls.)* A piece is **salvaged instead of split** only at the bound:
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
`llama3.1:8b` locally). The bound is pinned against the REAL splitter by
`test_extract_worst_case_truncated_chunk_costs_seven_calls`: exactly 14 requests,
because each call is a first attempt plus its doubled-budget retry. This is worth it rather than "clearly worse":
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
| `src/brain/graph_rag/themes.py`, `communities_summary.py`, `sync.py`, `src/brain/enrichment.py` | P-b: log `describe_failure(exc)`, never the message | under 800, except `enrichment.py` (over: +0 lines) |
| `tests/test_json_salvage.py` | NEW | n/a |
| `tests/test_graphrag_chunk_split.py` | NEW: depth/floor/unsplittable bounds, split-then-salvage, error policy, `split_in_half` cut choice and its ≤2 / lossless fan-out on the real tokenizer | n/a |
| `tests/test_chat.py` | truncation → typed error carrying the partial; malformed → NOT typed | n/a |
| `tests/test_graphrag_extract.py` | regressions: salvage under the floor, split keeps the tail, split-then-salvage at the floor, real worst-case call count | n/a |

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

## 6a. Review of `5b4940d`: what changed after it

**R1/R2: fan-out and the chunker backstop.** The `split_in_half` rewrite
(`6c98649`) no longer calls `chunk_text` at all, so the zero-slack packing that
overran the ceiling is gone, not patched. Measured with the real tokenizer on
3,000 seeded synthetic inputs (transcripts, paragraph mixes, unpunctuated
one-line notes, long single paragraphs; 200–1,600 tokens) and, for the call
bound, on 300 chunk-sized (1,000–1,600 tokens) always-truncating inputs:

| splitter | fan-out | `chunker backstop fired` WARNs | pieces concatenate to the input | calls per pathological chunk |
|---|---|---|---|---|
| old (greedy `chunk_text`, no overlap) | 2: 70, 3: 2,924, 4: 6 | 1,300 | 0 / 3,000 | 8–13 (mostly 10) |
| new | 2: 3,000 | 0 | 3,000 / 3,000 | 7 on 300 / 300 |

The backstop needed the "notes" input kind to show up. After a sentence's
`.`, cl100k merges the `\n\n` joiner into one token, so only unpunctuated
paragraphs make each joiner a token of its own. Pinned by
`test_split_in_half_returns_exactly_two_pieces_on_real_inputs` (300 inputs) and
`test_real_splitter_worst_case_is_seven_calls`.

**R3: trailing scalar** (`a75eada`). An item is kept only once the `,` or `]`
after it has been seen. `{"entities": [1, 2, 12` gives `[1, 2]`. The rule is
uniform, so `{A} {B}` (no separator) now gives `[]` rather than `[A]`, and an
object closed exactly at end-of-text is dropped. That costs at most one entry
per truncated piece.

**P-a: RecursionError** (`3656dcb`, `a75eada`). This is pre-existing.
`json.loads` / `raw_decode` raise `RecursionError` on output nested past the
recursion limit. It is not a `ValueError`, so it escaped `extract()`. `chat`
now treats it as a malformed reply: a retry, then a plain `EnrichmentError`.
The envelope gets the same treatment, and salvage stops at it. Pinned at the
chat, salvage and extractor levels.

**P-b: exception text at WARNING** (`b3d12ff`). This is also pre-existing.
`errors.describe_failure` logs the class and its cause's class only. It is
applied at every site in `graph_rag` / `chat` / `enrichment` where the message
can carry model output or row values:
* the concept-extraction skip;
* `summarize_group` and `summarize_bucket`;
* the themes and community summary failures;
* both graph-sync skips (a psycopg DETAIL can quote an entity name).

Each site has a sentinel test, and a per-site mutation proves the wiring.
Checked and left alone:
* `fuse.py` and `global_.py` log search/embedder failures, not model output;
* `communities_summary`'s embedding-pass WARN reports an embedder error;
* `extract`'s "Ollama unavailable" WARN carries transport and HTTP status text;
* `chat`'s reply preview is logged at DEBUG, not WARNING.

## 7. Mutation proofs — the repo's five-clause definition

The five clauses:
* **(a)** every case passes unmutated.
* **(b)** each mutation lands exactly its declared count, replacing every
  occurrence.
* **(c)** the declared cases go red, and ONLY those. This is an exact-set claim
  over every case below.
* **(d)** each red case fails with an `AssertionError` AT its targeted
  assertion line.
* **(e)** every independent claim has its own entry. Every assertion is fired
  by at least one mutation, and any that no mutation reaches is listed as
  unfired with a reason.

Restored → green is checked after every mutation as hygiene, not as a clause.

*(Corrected after the audit of `5b4940d`.)* The first record used a homegrown
clause list whose fifth clause was "restored → green", so (e) was never
checked. Its S6 row recorded a surviving single-site separator mutant
(`pos += 1` → `+= 2`, which on compact JSON returns `[{'name':'A'}, 'name']`).
J1 below now kills it at one site.

**Method.** Run 2026-10-21 against `5170d1c`, sequentially, in this worktree,
against its own sibling test DB. Each run executes all **49**
cases: every json_salvage and chunk_split test, plus the new chat, extractor,
enrichment, themes, communities and sync tests. Parametrized cases count once
per parameter.

The inventory for (e) is derived from the test files' AST (every `assert` in
every case), not hand-listed. The harness refuses to run if a new test in a
fully-covered file is missing from its case list. Raise checks assert on the
caught exception, so a missing raise or a wrong exception type is an
`AssertionError` at that line rather than pytest's `DID NOT RAISE` or an error.

The declared red sets were pinned from `--dry` runs in which every red case
was reviewed: each failure is an `AssertionError` at that case's own
assertion for the mutated claim. The judged run then re-measured them.

Two dry-run reds were NOT assertions, and both were fixed at the source rather
than declared:
* under P6, `u_lopsided` raised a `ValueError` while unpacking. The test was
  changed to assert on `pieces[0]` (`8afd864`);
* the first T1 (`<` for `>=`) raised an `IndexError`. It was replaced by the
  real old behaviour, "keep a value cut at end-of-text".

**Result: (a) 49/49. (b)–(d) OK for 60/60
mutations. (e) 87/87 assertions fired, 0 exempt.
Restored-green OK after every mutation.**

**Where one mutation covers several tests of one claim:**
* J7 covers every "the complete prefix is kept" test.
* J8 covers `j_empty` and `j_first`: nothing is invented at a stop.
* J9 covers `j_missing` and `j_notarray`.
* C6 covers `c_malformed` and `c_then`: garbage is not truncation.
* B0 and B1 cover all eight P-b tests' "no message" and "class is named"
  claims at the shared `describe_failure`. B2–B8 then prove each site is
  wired to it, one entry per site.
* B9 covers every "the skip is reported" assertion.

Mutations that also redden downstream tests declare those reds explicitly, so
(c) holds as an exact set.

Case keys: `j_*` json_salvage, `u_*` chunk_split, `c_*` chat, `e_*` extract
and enrichment (in `tests/test_graphrag_extract.py`), `t_*` themes,
`m_*` communities summary, `s_*` sync. The harness maps each key to its test.

| # | Mutation | Red set (exact) → assertion fired |
|---|---|---|
| J1 | separator advance skips a char | `j_compact` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…` |
| J2 | whitespace set drops tab and CR | `j_allws` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…` |
| J3 | whitespace set is space only | `j_allws` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_pretty` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…` |
| J4 | any separator continues | `j_badsep` → `assert salvage_array_items(text, "entities") == []` |
| J5 | key need not open an array | `e_salvage` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`j_allws` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_between` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`j_closed` → `assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]`<br>`j_compact` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_keyvalue` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]`<br>`j_mid` → `assert salvage_array_items(text, "entities") == [`<br>`j_nested` → `assert outcome == [{"name": "A"}]`<br>`j_pretty` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`j_trailing` → `assert salvage_array_items('{"entities": [1, 2, 12', "entities") ==…`<br>`u_depth` → `assert result.entries == ({"name": "W"},) * 4`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| J6 | key need not be followed by a colon | `e_salvage` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`j_allws` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_between` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`j_closed` → `assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]`<br>`j_compact` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_keyvalue` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]`<br>`j_mid` → `assert salvage_array_items(text, "entities") == [`<br>`j_nested` → `assert outcome == [{"name": "A"}]`<br>`j_pretty` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`j_trailing` → `assert salvage_array_items('{"entities": [1, 2, 12', "entities") ==…`<br>`u_depth` → `assert result.entries == ({"name": "W"},) * 4`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| J7 | a cut discards the prefix | `e_salvage` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`j_allws` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_between` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`j_compact` → `assert salvage_array_items(text, "entities") == [{"name": "A"}, {"n…`<br>`j_keyvalue` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]`<br>`j_mid` → `assert salvage_array_items(text, "entities") == [`<br>`j_nested` → `assert outcome == [{"name": "A"}]`<br>`j_pretty` → `assert salvage_array_items(text, "entities") == [{"name": "Zorvex",…`<br>`u_depth` → `assert result.entries == ({"name": "W"},) * 4`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| J8 | a stop invents an item | `j_empty` → `assert salvage_array_items('{"entities": []}', "entities") == []`<br>`j_first` → `assert salvage_array_items('{"entities": [{"name": "Zor', "entities…` |
| J9 | missing key returns the text | `j_missing` → `assert salvage_array_items('{"other": [{"name": "Zorvex"}]}', "enti…`<br>`j_notarray` → `assert salvage_array_items('{"entities": "oops"}', "entities") == []` |
| J10 | only object items kept | `j_closed` → `assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]`<br>`j_trailing` → `assert salvage_array_items('{"entities": [1, 2, 12', "entities") ==…` |
| C1 | final truncation not typed | `c_trunc` → `assert isinstance(excinfo.value, ChatOutputTruncated)`<br>`e_salvage` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_split` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert len(requests) == 14` |
| C2 | partial keeps the FIRST reply | `c_then` → `assert not isinstance(excinfo.value, ChatOutputTruncated)`<br>`c_trunc` → `assert excinfo.value.partial == '{"answer": "still trunc'` |
| C3 | reported budget doubled again | `c_trunc` → `assert excinfo.value.num_predict == 256` |
| C4 | partial leaks into the message | `c_trunc` → `assert "still trunc" not in str(excinfo.value)` |
| C5 | message prefix reworded | `c_trunc` → `assert str(excinfo.value).startswith("chat failed after 2 attempts:…` |
| C6 | every decode failure is truncation | `c_malformed` → `assert not isinstance(excinfo.value, ChatOutputTruncated)`<br>`c_then` → `assert not isinstance(excinfo.value, ChatOutputTruncated)` |
| C7 | per-attempt reset dropped | `c_then` → `assert not isinstance(excinfo.value, ChatOutputTruncated)` |
| S1 | depth bound raised to 3 | `e_worst` → `assert len(requests) == 14`<br>`u_bound` → `assert max(per_input) == 7`<br>`u_depth` → `assert MAX_SPLIT_DEPTH == 2` |
| S2 | depth comparison off by one | `e_worst` → `assert len(requests) == 14`<br>`u_bound` → `assert max(per_input) == 7`<br>`u_depth` → `assert len(calls) == 7`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| S3 | no size floor | `e_salvage` → `assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in ca…`<br>`e_sts` → `assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in ca…`<br>`u_under` → `assert len(calls) == 1` |
| S4 | floor exclusive | `u_at` → `assert len(calls) == 3` |
| S5 | a one-piece split recurses | `u_wont` → `assert len(calls) == 1` |
| S6 | salvage at the bound yields nothing | `e_salvage` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`u_depth` → `assert result.entries == ({"name": "W"},) * 4`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| S7 | pieces_split over-counted | `e_split` → `assert "split 1 piece(s), salvaged 0 entries from 0 piece(s)" in ca…`<br>`e_sts` → `assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in ca…`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`u_at` → `assert result.pieces_split == 1`<br>`u_depth` → `assert result.pieces_split == 3`<br>`u_sts` → `assert (result.pieces_split, result.pieces_salvaged, result.entries…` |
| S8 | pieces_salvaged over-counted | `e_salvage` → `assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in ca…`<br>`e_sts` → `assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in ca…`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`u_depth` → `assert result.pieces_salvaged == 4`<br>`u_sts` → `assert (result.pieces_split, result.pieces_salvaged, result.entries…`<br>`u_under` → `assert result.pieces_salvaged == 1`<br>`u_wont` → `assert result.pieces_salvaged == 1` |
| S9 | entries_salvaged over-counted | `e_salvage` → `assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in ca…`<br>`e_sts` → `assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in ca…`<br>`e_worst` → `assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in ca…`<br>`u_depth` → `assert result.entries_salvaged == 4`<br>`u_sts` → `assert (result.pieces_split, result.pieces_salvaged, result.entries…` |
| S10 | malformed piece not counted | `u_malformed` → `assert result.pieces_failed == 1` |
| S11 | malformed piece abandons its siblings | `u_malformed` → `assert result.entries == ({"name": "B"},)` |
| S12 | OllamaUnavailable swallowed in a sub-piece | `u_unavail` → `assert isinstance(raised, OllamaUnavailable)` |
| S13 | the chunk's own malformed reply swallowed | `e_nested` → `assert "skipping chunk" in caplog.text`<br>`e_quote_key` → `assert "skipping chunk" in caplog.text`<br>`e_quote_list` → `assert "skipping chunk" in caplog.text`<br>`u_own` → `assert isinstance(raised, EnrichmentError)`<br>`u_unavail` → `assert isinstance(raised, OllamaUnavailable)` |
| S14 | truncated always true | `u_parses` → `assert not result.truncated` |
| S15 | parsed entries dropped | `e_split` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`u_malformed` → `assert result.entries == ({"name": "B"},)`<br>`u_parses` → `assert result.entries == ({"name": "A"},)`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})` |
| S16 | a parsing piece is called twice | `e_sts` → `assert len(calls) == 5`<br>`u_parses` → `assert len(calls) == 1` |
| S17 | never split | `e_split` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert len(requests) == 14`<br>`u_at` → `assert len(calls) == 3`<br>`u_bound` → `assert max(per_input) == 7`<br>`u_depth` → `assert len(calls) == 7`<br>`u_malformed` → `assert result.entries == ({"name": "B"},)`<br>`u_sts` → `assert result.entries == ({"name": "A"}, {"name": "B"})`<br>`u_unavail` → `assert isinstance(raised, OllamaUnavailable)` |
| P1 | paragraph level skipped | `e_split` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in ca…`<br>`u_para` → `assert split_in_half(text, count_tokens=_words) == [f"{first}\n\n{s…` |
| P2 | nearer-neighbour check dropped | `u_nearer` → `assert split_in_half(text, count_tokens=_words)[0] == f"{first}\n\n"` |
| P3 | no balance requirement | `u_lopsided` → `assert _words(pieces[0]) == 40` |
| P4 | token midpoint off by one | `u_midpoint` → `assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]` |
| P5 | a single token is split | `u_single` → `assert split_in_half("word", count_tokens=_words) == ["word"]` |
| P6 | greedy packing restored | `e_worst` → `assert len(requests) == 14`<br>`u_bound` → `assert max(per_input) == 7`<br>`u_fanout` → `assert len(pieces) <= 2`<br>`u_lopsided` → `assert _words(pieces[0]) == 40`<br>`u_midpoint` → `assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]`<br>`u_nearer` → `assert split_in_half(text, count_tokens=_words)[0] == f"{first}\n\n"`<br>`u_para` → `assert split_in_half(text, count_tokens=_words) == [f"{first}\n\n{s…`<br>`u_two` → `assert not [r for r in caplog.records if r.name == "brain.ingest.ch…` |
| P7 | cut whitespace dropped | `u_fanout` → `assert "".join(pieces) == text`<br>`u_nearer` → `assert split_in_half(text, count_tokens=_words)[0] == f"{first}\n\n"`<br>`u_para` → `assert split_in_half(text, count_tokens=_words) == [f"{first}\n\n{s…`<br>`u_two` → `assert all("".join(pieces) == text for pieces, text in zip(splits, …` |
| X1 | WARN carries the entries | `e_salvage` → `assert "Zorvex" not in caplog.text`<br>`e_split` → `assert "Zorvex" not in caplog.text` |
| X2 | WARN salvage counts swapped | `e_salvage` → `assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in ca…` |
| T1 | a value cut at end-of-text is kept | `j_trailing` → `assert salvage_array_items('{"entities": [1, 2, 12', "entities") ==…` |
| T2 | an item is kept before its terminator is checked | `j_badsep` → `assert salvage_array_items(text, "entities") == []`<br>`j_trailing` → `assert salvage_array_items('{"entities": [1, 2, 12', "entities") ==…` |
| T3 | salvage lets RecursionError escape | `j_nested` → `assert outcome == [{"name": "A"}]` |
| A1 | chat lets RecursionError escape | `c_nested` → `assert type(raised) is EnrichmentError`<br>`e_nested` → `assert outcome == []` |
| A2 | envelope lets RecursionError escape | `c_envelope` → `assert type(raised) is EnrichmentError` |
| B0 | describe_failure includes the message | `e_bucket` → `assert _SENTINEL not in caplog.text`<br>`e_group` → `assert _SENTINEL not in caplog.text`<br>`e_quote_key` → `assert _SENTINEL not in caplog.text`<br>`e_quote_list` → `assert _SENTINEL not in caplog.text`<br>`m_quote` → `assert sentinel not in caplog.text`<br>`s_reconcile` → `assert _SYNC_SENTINEL not in caplog.text`<br>`s_remove` → `assert _SYNC_SENTINEL not in caplog.text`<br>`t_quote` → `assert sentinel not in caplog.text` |
| B1 | describe_failure drops the class | `e_bucket` → `assert "synthesis failed (EnrichmentError" in caplog.text`<br>`e_group` → `assert "synthesis failed (EnrichmentError" in caplog.text`<br>`e_quote_key` → `assert "failed (EnrichmentError" in caplog.text`<br>`e_quote_list` → `assert "failed (EnrichmentError" in caplog.text`<br>`m_quote` → `assert "failed (RuntimeError)" in caplog.text`<br>`s_reconcile` → `assert f"graph sync ({op}) skipped for doc doc-0001: RuntimeError;"…`<br>`s_remove` → `assert f"graph sync ({op}) skipped for doc doc-0001: RuntimeError;"…`<br>`t_quote` → `assert "summary failed (RuntimeError)" in caplog.text` |
| B2 | extract skip logs the exception | `e_quote_key` → `assert _SENTINEL not in caplog.text`<br>`e_quote_list` → `assert _SENTINEL not in caplog.text` |
| B3 | summarize_group logs the exception | `e_group` → `assert _SENTINEL not in caplog.text` |
| B4 | summarize_bucket logs the exception | `e_bucket` → `assert _SENTINEL not in caplog.text` |
| B5 | themes logs the exception | `t_quote` → `assert sentinel not in caplog.text` |
| B6 | communities logs the exception | `m_quote` → `assert sentinel not in caplog.text` |
| B7 | sync reconcile logs the exception | `s_reconcile` → `assert _SYNC_SENTINEL not in caplog.text` |
| B8 | sync remove logs the exception | `s_remove` → `assert _SYNC_SENTINEL not in caplog.text` |
| B9 | skip WARN reworded | `e_nested` → `assert "skipping chunk" in caplog.text`<br>`e_quote_key` → `assert "skipping chunk" in caplog.text`<br>`e_quote_list` → `assert "skipping chunk" in caplog.text` |
| R1 | nothing is ever split | `e_split` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_sts` → `assert {(e.entity_type, e.display_name) for e in out} == {`<br>`e_worst` → `assert len(requests) == 14`<br>`u_bound` → `assert max(per_input) == 7`<br>`u_lopsided` → `assert _words(pieces[0]) == 40`<br>`u_midpoint` → `assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]`<br>`u_nearer` → `assert split_in_half(text, count_tokens=_words)[0] == f"{first}\n\n"`<br>`u_para` → `assert split_in_half(text, count_tokens=_words) == [f"{first}\n\n{s…`<br>`u_two` → `assert [len(pieces) for pieces in splits] == [2] * len(inputs)` |
| X3 | truncated chunk reported as skipped | `e_salvage` → `assert "skipping chunk" not in caplog.text` |

## Appendix — the harness

Run from the worktree root on a clean, committed tree (it restores with
`git checkout`), with `TEST_DATABASE_URL` pointing at a sibling test DB:
`.venv/bin/python harness.py <scratch-dir>`.
* `--dry` prints the measured red sets without judging.
* `--only A1,B2` restricts the run to the named mutations.

```python
"""Five-clause mutation harness (repo definition (a)-(e)) for the concept-extract output-budget tests.

(a) every case passes unmutated; (b) every mutation pair lands exactly its declared
count, replacing all occurrences; (c) the declared cases go red, and ONLY those;
(d) each red case fails with an AssertionError AT its targeted assertion line;
(e) every assertion in the inventory -- derived from the test files' AST, not
hand-listed -- is fired by at least one mutation, or listed as unfired with a
reason. Restored -> clean tree and all green is checked as hygiene.

Usage (worktree root, clean committed tree, TEST_DATABASE_URL on a sibling DB):
    .venv/bin/python harness.py <scratch-dir> [--dry]
--dry prints each mutation's measured red set and failing lines without judging.
"""
import ast
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

WT = Path.cwd()
OUT = Path(sys.argv[1])
DRY = "--dry" in sys.argv
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
OUT.mkdir(parents=True, exist_ok=True)

FILES = {
    "j": ("tests/test_json_salvage.py", None),
    "u": ("tests/test_graphrag_chunk_split.py", None),
    "c": ("tests/test_chat.py", [
        "test_chat_json_with_client_truncated_twice_raises_typed_error_with_partial",
        "test_chat_json_with_client_malformed_twice_is_not_truncation",
        "test_chat_json_with_client_truncation_then_malformed_is_not_truncation",
        "test_chat_json_with_client_reply_nested_past_the_recursion_limit_is_malformed",
        "test_chat_envelope_nested_past_the_recursion_limit_is_enrichment_error",
    ]),
    "e": ("tests/test_graphrag_extract.py", [
        "test_extract_salvages_complete_entities_from_output_truncated_twice",
        "test_extract_splits_a_truncated_chunk_and_keeps_its_tail",
        "test_extract_split_then_salvages_a_half_still_truncated_at_the_floor",
        "test_extract_worst_case_truncated_chunk_costs_seven_calls",
        "test_extract_never_raises_on_a_reply_nested_past_the_recursion_limit",
        "test_extract_skip_warning_never_quotes_the_model_reply",
        "test_summarize_group_warning_never_quotes_the_model_reply",
        "test_summarize_bucket_warning_never_quotes_the_model_reply",
    ]),
    "t": ("tests/test_graphrag_themes.py", [
        "test_themes_synthesize_failure_warning_never_quotes_the_exception",
    ]),
    "m": ("tests/test_graphrag_communities_summary.py", [
        "test_enricher_failure_warning_never_quotes_the_exception",
    ]),
    "s": ("tests/test_graphrag_sync_wiring.py", [
        "test_sync_skip_warning_never_quotes_the_exception",
    ]),
}
SHORT = {
    # json_salvage
    "j_mid": "test_salvage_returns_complete_items_before_a_mid_string_cut",
    "j_between": "test_salvage_stops_at_a_cut_between_items",
    "j_compact": "test_salvage_handles_compact_separators",
    "j_allws": "test_salvage_handles_every_json_whitespace_around_separators",
    "j_pretty": "test_salvage_tolerates_pretty_printed_whitespace",
    "j_closed": "test_salvage_of_a_closed_array_returns_every_item",
    "j_empty": "test_salvage_of_an_empty_array_is_empty",
    "j_first": "test_salvage_cut_inside_the_first_item_is_empty",
    "j_missing": "test_salvage_missing_key_is_empty",
    "j_notarray": "test_salvage_key_not_followed_by_an_array_is_empty",
    "j_keyvalue": "test_salvage_skips_the_key_appearing_earlier_as_a_value",
    "j_badsep": "test_salvage_stops_at_a_malformed_separator",
    "j_trailing": "test_salvage_drops_a_trailing_scalar_cut_at_the_end",
    "j_nested": "test_salvage_stops_at_an_item_nested_past_the_recursion_limit",
    # chunk_split
    "u_parses": "test_a_reply_that_parses_is_returned_untouched_with_no_split",
    "u_depth": "test_split_depth_is_bounded_then_each_leaf_is_salvaged",
    "u_under": "test_a_piece_under_the_floor_is_salvaged_without_splitting",
    "u_at": "test_a_piece_at_the_floor_is_split",
    "u_wont": "test_a_piece_that_will_not_split_is_salvaged",
    "u_sts": "test_split_then_salvage_keeps_the_parsed_half_and_the_salvaged_prefix",
    "u_malformed": "test_a_malformed_sub_piece_is_skipped_and_counted_its_sibling_kept",
    "u_unavail": "test_ollama_unavailable_in_a_sub_piece_propagates",
    "u_own": "test_the_chunks_own_malformed_reply_propagates",
    "u_para": "test_split_in_half_cuts_at_the_paragraph_boundary_nearest_the_midpoint",
    "u_nearer": "test_split_in_half_picks_the_nearer_of_the_two_neighbouring_cuts",
    "u_lopsided": "test_split_in_half_a_lopsided_paragraph_cut_falls_through_to_a_word_cut",
    "u_midpoint": "test_split_in_half_falls_back_to_the_token_midpoint_without_whitespace",
    "u_single": "test_split_in_half_leaves_a_single_token_whole",
    "u_fanout": "test_split_in_half_never_fans_out_past_two_and_loses_nothing",
    "u_two": "test_split_in_half_returns_exactly_two_pieces_on_real_inputs",
    "u_bound": "test_real_splitter_worst_case_is_seven_calls",
    # chat
    "c_trunc": "test_chat_json_with_client_truncated_twice_raises_typed_error_with_partial",
    "c_malformed": "test_chat_json_with_client_malformed_twice_is_not_truncation",
    "c_then": "test_chat_json_with_client_truncation_then_malformed_is_not_truncation",
    "c_nested": "test_chat_json_with_client_reply_nested_past_the_recursion_limit_is_malformed",
    "c_envelope": "test_chat_envelope_nested_past_the_recursion_limit_is_enrichment_error",
    # extract
    "e_salvage": "test_extract_salvages_complete_entities_from_output_truncated_twice",
    "e_split": "test_extract_splits_a_truncated_chunk_and_keeps_its_tail",
    "e_sts": "test_extract_split_then_salvages_a_half_still_truncated_at_the_floor",
    "e_worst": "test_extract_worst_case_truncated_chunk_costs_seven_calls",
    "e_nested": "test_extract_never_raises_on_a_reply_nested_past_the_recursion_limit",
    "e_quote_key": "test_extract_skip_warning_never_quotes_the_model_reply[missing-key]",
    "e_quote_list": "test_extract_skip_warning_never_quotes_the_model_reply[non-list]",
    "e_group": "test_summarize_group_warning_never_quotes_the_model_reply",
    "e_bucket": "test_summarize_bucket_warning_never_quotes_the_model_reply",
    # themes / communities / sync
    "t_quote": "test_themes_synthesize_failure_warning_never_quotes_the_exception",
    "m_quote": "test_enricher_failure_warning_never_quotes_the_exception",
    "s_reconcile": "test_sync_skip_warning_never_quotes_the_exception[reconcile]",
    "s_remove": "test_sync_skip_warning_never_quotes_the_exception[remove]",
}
TESTS = {k: f"{FILES[k[0]][0]}::{fn}" for k, fn in SHORT.items()}


def _inventory() -> dict[str, list[str]]:
    """Every assert in each case, as its stripped first source line (AST-derived)."""
    inv: dict[str, list[str]] = {}
    for prefix, (path, only) in FILES.items():
        src = (WT / path).read_text()
        lines = src.splitlines()
        names = {fn.split("[")[0] for k, fn in SHORT.items() if k[0] == prefix}
        defined = set()
        for node in ast.parse(src).body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            if only is None or node.name in only:
                defined.add(node.name)
            if node.name not in names:
                continue
            asserts = [lines[a.lineno - 1].strip() for a in ast.walk(node) if isinstance(a, ast.Assert)]
            assert len(asserts) == len(set(asserts)), f"duplicate assert line in {node.name}"
            for key in (k for k, fn in SHORT.items() if fn.split("[")[0] == node.name):
                inv[key] = asserts
        missing = defined - names
        assert not missing, f"new test not in the case list: {missing}"
    return inv


INVENTORY = _inventory()
# (e) exemptions: (case, snippet) -> reason. Empty unless a run shows a gap.
UNFIRED: dict[tuple[str, str], str] = {}

J = "src/brain/json_salvage.py"
C = "src/brain/chat.py"
S = "src/brain/graph_rag/chunk_split.py"
X = "src/brain/graph_rag/extract.py"
E = "src/brain/errors.py"
N = "src/brain/enrichment.py"
TH = "src/brain/graph_rag/themes.py"
CM = "src/brain/graph_rag/communities_summary.py"
SY = "src/brain/graph_rag/sync.py"
_chat_src = (WT / C).read_text()
RECURSION_BLOCK = _chat_src[_chat_src.index("        except RecursionError:\n"):_chat_src.index("        except json.JSONDecodeError as exc:\n")]
GREEDY = (
    "    from ..ingest.chunker import chunk_text\n\n"
    "    half_ = max(1, (count_tokens(text) + 1) // 2)\n"
    "    return [c.content for c in chunk_text(text, target_tokens=half_, overlap_tokens=0,"
    " count_tokens=count_tokens)]\n"
)
ENT = "assert {(e.entity_type, e.display_name) for e in out} == {"
SAL = "assert salvage_array_items("

# (label, file, [(old, new, declared_count)], {case: targeted-assert snippet})
M = [
    ("J1 separator advance skips a char", J, [("            return items\n        pos += 1\n",
                                               "            return items\n        pos += 2\n", 1)],
     {"j_compact": SAL}),
    ("J2 whitespace set drops tab and CR", J, [('_WHITESPACE = " \\t\\n\\r"', '_WHITESPACE = " \\n"', 1)],
     {"j_allws": SAL}),
    ("J3 whitespace set is space only", J, [('_WHITESPACE = " \\t\\n\\r"', '_WHITESPACE = " "', 1)],
     {"j_allws": SAL, "j_pretty": SAL}),
    ("J4 any separator continues", J, [('        if pos >= len(text) or text[pos] not in ",]":\n',
                                       "        if pos >= len(text):\n", 1)],
     {"j_badsep": SAL}),
    ("J5 key need not open an array", J, [("\\s*:\\s*\\['", "\\s*:\\s*'", 1)],
     {}),
    ("J6 key need not be followed by a colon", J, [("rf'\"{re.escape(key)}\"\\s*:\\s*\\['", "rf'\"{re.escape(key)}\"'", 1)],
     {}),
    ("J7 a cut discards the prefix", J, [("        except (json.JSONDecodeError, RecursionError):\n            return items\n",
                                         "        except (json.JSONDecodeError, RecursionError):\n            return []\n", 1)],
     {}),
    ("J8 a stop invents an item", J, [("        except (json.JSONDecodeError, RecursionError):\n            return items\n",
                                       "        except (json.JSONDecodeError, RecursionError):\n            return items or [None]\n", 1)],
     {}),
    ("J9 missing key returns the text", J, [("    if match is None:\n        return []\n",
                                            "    if match is None:\n        return [text]\n", 1)],
     {}),
    ("J10 only object items kept", J, [("        items.append(item)\n",
                                       "        items.append(item) if isinstance(item, dict) else None\n", 1)],
     {"j_closed": SAL}),
    ("C1 final truncation not typed", C, [("    if truncated_text is not None:\n", "    if False:\n", 1)],
     {}),
    ("C2 partial keeps the FIRST reply", C, [("        truncated_text = None\n", "        pass\n", 1),
                                             ("                truncated_text = response_text\n",
                                              "                truncated_text = truncated_text or response_text\n", 1)],
     {}),
    ("C3 reported budget doubled again", C, [("                if attempt == 1:\n", "                if True:\n", 1)],
     {"c_trunc": "assert excinfo.value.num_predict == 256"}),
    ("C4 partial leaks into the message", C, [('    message = f"chat failed after 2 attempts: {last_error}"\n',
                                               '    message = f"chat failed after 2 attempts: {last_error} {truncated_text}"\n', 1)],
     {"c_trunc": 'assert "still trunc" not in str(excinfo.value)'}),
    ("C5 message prefix reworded", C, [('    message = f"chat failed after 2 attempts: {last_error}"\n',
                                        '    message = f"chat failed: {last_error}"\n', 1)],
     {"c_trunc": 'assert str(excinfo.value).startswith("chat failed after 2 attempts: Unterminated")'}),
    ("C6 every decode failure is truncation", C, [('            if "Unterminated" in exc.msg or exc.pos >= len(response_text):\n',
                                                   "            if True:\n", 1)],
     {}),
    ("C7 per-attempt reset dropped", C, [("        truncated_text = None\n", "        pass\n", 1)],
     {}),
    ("S1 depth bound raised to 3", S, [("MAX_SPLIT_DEPTH = 2\n", "MAX_SPLIT_DEPTH = 3\n", 1)], {}),
    ("S2 depth comparison off by one", S, [("            if depth < max_depth and", "            if depth <= max_depth and", 1)], {}),
    ("S3 no size floor", S, [("count_tokens(piece) >= min_tokens", "count_tokens(piece) >= 0", 1)], {}),
    ("S4 floor exclusive", S, [("count_tokens(piece) >= min_tokens", "count_tokens(piece) > min_tokens", 1)], {}),
    ("S5 a one-piece split recurses", S, [("        if len(pieces) < 2:\n", "        if len(pieces) < 1:\n", 1)], {}),
    ("S6 salvage at the bound yields nothing", S, [("            salvaged = salvage_array_items(exc.partial, _ENTITIES_KEY)\n",
                                                    "            salvaged = []\n", 1)], {}),
    ("S7 pieces_split over-counted", S, [("        tally.pieces_split += 1\n", "        tally.pieces_split += 2\n", 1)], {}),
    ("S8 pieces_salvaged over-counted", S, [("            tally.pieces_salvaged += 1\n", "            tally.pieces_salvaged += 2\n", 1)], {}),
    ("S9 entries_salvaged over-counted", S, [("            tally.entries_salvaged += len(salvaged)\n",
                                              "            tally.entries_salvaged += 2 * len(salvaged)\n", 1)], {}),
    ("S10 malformed piece not counted", S, [("                tally.pieces_failed += 1\n", "                tally.pieces_failed += 0\n", 1)], {}),
    ("S11 malformed piece abandons its siblings", S, [("                tally.pieces_failed += 1\n",
                                                       "                tally.pieces_failed += 1\n                break\n", 1)], {}),
    ("S12 OllamaUnavailable swallowed in a sub-piece", S, [("            except OllamaUnavailable:\n                raise\n", "", 1)], {}),
    ("S13 the chunk's own malformed reply swallowed", S, [("    extract(text, 0)\n",
                                                          "    try:\n        extract(text, 0)\n    except EnrichmentError:\n        pass\n", 1)], {}),
    ("S14 truncated always true", S, [("        return self.pieces_split > 0 or self.pieces_salvaged > 0\n",
                                       "        return True\n", 1)], {}),
    ("S15 parsed entries dropped", S, [("            tally.entries.extend(extract_entities(piece))\n",
                                        "            tally.entries.extend(extract_entities(piece)[:0])\n", 1)], {}),
    ("S16 a parsing piece is called twice", S, [("            tally.entries.extend(extract_entities(piece))\n",
                                                 "            extract_entities(piece)\n            tally.entries.extend(extract_entities(piece))\n", 1)], {}),
    ("S17 never split", S, [("            if depth < max_depth and count_tokens(piece) >= min_tokens\n",
                             "            if False\n", 1)], {}),
    ("P1 paragraph level skipped", S, [('    re.compile(r"\\n[ \\t]*\\n\\s*"),\n', "", 1)], {}),
    ("P2 nearer-neighbour check dropped", S, [("    neighbours = cuts[max(lo - 1, 0) : lo + 1]\n",
                                               "    neighbours = cuts[lo : lo + 1]\n", 1)], {}),
    ("P3 no balance requirement", S, [("_MIN_SIDE_SHARE = 0.25\n", "_MIN_SIDE_SHARE = 0.0\n", 1)], {}),
    ("P4 token midpoint off by one", S, [("        if count_tokens(text[:mid]) < half:\n",
                                          "        if count_tokens(text[:mid]) <= half:\n", 1)], {}),
    ("P5 a single token is split", S, [("    if total < 2:\n", "    if total < 1:\n", 1)], {}),
    ("P6 greedy packing restored", S, [("    total = count_tokens(text)\n    if total < 2:\n",
                                        GREEDY + "    total = count_tokens(text)\n    if total < 2:\n", 1)], {}),
    ("P7 cut whitespace dropped", S, [("return [text[:cut], text[cut:]]", "return [text[:cut].rstrip(), text[cut:]]", 2)], {}),
    ("X1 WARN carries the entries", X, [("            for entry in result.entries:\n",
                                         '            _logger.warning("%s", result.entries)\n            for entry in result.entries:\n', 1)], {}),
    ("X2 WARN salvage counts swapped", X, [("                    result.entries_salvaged,\n                    result.pieces_salvaged,\n",
                                            "                    result.pieces_salvaged,\n                    result.entries_salvaged,\n", 1)], {}),
    ("T1 a value cut at end-of-text is kept", J, [('        if pos >= len(text) or text[pos] not in ",]":\n            return items\n',
                                                 '        if pos >= len(text) or text[pos] not in ",]":\n            return items + [item] if pos >= len(text) else items\n', 1)], {}),
    ("T2 an item is kept before its terminator is checked", J,
     [('        pos = _skip_whitespace(text, pos)\n        if pos >= len(text) or text[pos] not in ",]":\n            return items\n        items.append(item)\n',
       '        items.append(item)\n        pos = _skip_whitespace(text, pos)\n        if pos >= len(text) or text[pos] not in ",]":\n            return items\n', 1)], {}),
    ("T3 salvage lets RecursionError escape", J, [("        except (json.JSONDecodeError, RecursionError):\n",
                                                 "        except json.JSONDecodeError:\n", 1)], {}),
    ("A1 chat lets RecursionError escape", C, [(RECURSION_BLOCK, "", 1)], {}),
    ("A2 envelope lets RecursionError escape", C, [("    except (ValueError, RecursionError) as exc:\n",
                                                  "    except ValueError as exc:\n", 1)], {}),
    ("B0 describe_failure includes the message", E, [("    name = type(exc).__name__\n",
                                                    '    name = f"{type(exc).__name__}: {exc}"\n', 1)], {}),
    ("B1 describe_failure drops the class", E, [("    name = type(exc).__name__\n", '    name = "failure"\n', 1)], {}),
    ("B2 extract skip logs the exception", X, [("                    describe_failure(exc),\n", "                    exc,\n", 1)], {}),
    ("B3 summarize_group logs the exception", N, [('''"summarize_group: synthesis failed (%s); returning summary=None",
                describe_failure(exc),''', '''"summarize_group: synthesis failed (%s); returning summary=None",
                exc,''', 1)], {}),
    ("B4 summarize_bucket logs the exception", N, [('''"summarize_bucket: synthesis failed (%s); returning summary=None",
                describe_failure(exc),''', '''"summarize_bucket: synthesis failed (%s); returning summary=None",
                exc,''', 1)], {}),
    ("B5 themes logs the exception", TH, [("describe_failure(exc),  # class only", "exc,  # class only", 1)], {}),
    ("B6 communities logs the exception", CM, [("describe_failure(exc),  # class only", "exc,  # class only", 1)], {}),
    ("B7 sync reconcile logs the exception", SY, [("(entity names).\n                describe_failure(exc),",
                                                  "(entity names).\n                exc,", 1)], {}),
    ("B8 sync remove logs the exception", SY, [("describe_failure(exc),  # class only, as in reconcile",
                                               "exc,  # class only, as in reconcile", 1)], {}),
    ("B9 skip WARN reworded", X, [("failed (%s); skipping chunk", "failed (%s); dropping chunk", 1)], {}),
    ("R1 nothing is ever split", S, [("    if total < 2:\n        return [text]\n",
                                      "    if total < 10**9:\n        return [text]\n", 1)], {}),
    ("X3 truncated chunk reported as skipped", X, [('"concept extraction: chunk %d output truncated at num_predict; "',
                                                    '"concept extraction: chunk %d output truncated at num_predict; skipping chunk; "', 1)], {}),
]

# Declared red sets: pinned from reviewed --dry runs (every red line is an
# AssertionError at that case's own assert for the mutated claim), then re-measured.
TARGETS: dict[str, dict[str, str]] = {
    'J1': {
        'j_compact': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
    },
    'J2': {
        'j_allws': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
    },
    'J3': {
        'j_allws': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_pretty': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
    },
    'J4': {
        'j_badsep': 'assert salvage_array_items(text, "entities") == []',
    },
    'J5': {
        'e_salvage': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'j_allws': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_between': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'j_closed': 'assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]',
        'j_compact': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_keyvalue': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]',
        'j_mid': 'assert salvage_array_items(text, "entities") == [',
        'j_nested': 'assert outcome == [{"name": "A"}]',
        'j_pretty': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'j_trailing': 'assert salvage_array_items(\'{"entities": [1, 2, 12\', "entities") == [1, 2]',
        'u_depth': 'assert result.entries == ({"name": "W"},) * 4',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'J6': {
        'e_salvage': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'j_allws': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_between': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'j_closed': 'assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]',
        'j_compact': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_keyvalue': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]',
        'j_mid': 'assert salvage_array_items(text, "entities") == [',
        'j_nested': 'assert outcome == [{"name": "A"}]',
        'j_pretty': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'j_trailing': 'assert salvage_array_items(\'{"entities": [1, 2, 12\', "entities") == [1, 2]',
        'u_depth': 'assert result.entries == ({"name": "W"},) * 4',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'J7': {
        'e_salvage': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'j_allws': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_between': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'j_compact': 'assert salvage_array_items(text, "entities") == [{"name": "A"}, {"name": "B"}]',
        'j_keyvalue': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]',
        'j_mid': 'assert salvage_array_items(text, "entities") == [',
        'j_nested': 'assert outcome == [{"name": "A"}]',
        'j_pretty': 'assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]',
        'u_depth': 'assert result.entries == ({"name": "W"},) * 4',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'J8': {
        'j_empty': 'assert salvage_array_items(\'{"entities": []}\', "entities") == []',
        'j_first': 'assert salvage_array_items(\'{"entities": [{"name": "Zor\', "entities") == []',
    },
    'J9': {
        'j_missing': 'assert salvage_array_items(\'{"other": [{"name": "Zorvex"}]}\', "entities") == []',
        'j_notarray': 'assert salvage_array_items(\'{"entities": "oops"}\', "entities") == []',
    },
    'J10': {
        'j_closed': 'assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]',
        'j_trailing': 'assert salvage_array_items(\'{"entities": [1, 2, 12\', "entities") == [1, 2]',
    },
    'C1': {
        'c_trunc': 'assert isinstance(excinfo.value, ChatOutputTruncated)',
        'e_salvage': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_split': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert len(requests) == 14',
    },
    'C2': {
        'c_then': 'assert not isinstance(excinfo.value, ChatOutputTruncated)',
        'c_trunc': 'assert excinfo.value.partial == \'{"answer": "still trunc\'',
    },
    'C3': {
        'c_trunc': 'assert excinfo.value.num_predict == 256',
    },
    'C4': {
        'c_trunc': 'assert "still trunc" not in str(excinfo.value)',
    },
    'C5': {
        'c_trunc': 'assert str(excinfo.value).startswith("chat failed after 2 attempts: Unterminated")',
    },
    'C6': {
        'c_malformed': 'assert not isinstance(excinfo.value, ChatOutputTruncated)',
        'c_then': 'assert not isinstance(excinfo.value, ChatOutputTruncated)',
    },
    'C7': {
        'c_then': 'assert not isinstance(excinfo.value, ChatOutputTruncated)',
    },
    'S1': {
        'e_worst': 'assert len(requests) == 14',
        'u_bound': 'assert max(per_input) == 7',
        'u_depth': 'assert MAX_SPLIT_DEPTH == 2',
    },
    'S2': {
        'e_worst': 'assert len(requests) == 14',
        'u_bound': 'assert max(per_input) == 7',
        'u_depth': 'assert len(calls) == 7',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'S3': {
        'e_salvage': 'assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in caplog.text',
        'e_sts': 'assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in caplog.text',
        'u_under': 'assert len(calls) == 1',
    },
    'S4': {
        'u_at': 'assert len(calls) == 3',
    },
    'S5': {
        'u_wont': 'assert len(calls) == 1',
    },
    'S6': {
        'e_salvage': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'u_depth': 'assert result.entries == ({"name": "W"},) * 4',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'S7': {
        'e_split': 'assert "split 1 piece(s), salvaged 0 entries from 0 piece(s)" in caplog.text',
        'e_sts': 'assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in caplog.text',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'u_at': 'assert result.pieces_split == 1',
        'u_depth': 'assert result.pieces_split == 3',
        'u_sts': 'assert (result.pieces_split, result.pieces_salvaged, result.entries_salvaged) == (1, 1, 1)',
    },
    'S8': {
        'e_salvage': 'assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in caplog.text',
        'e_sts': 'assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in caplog.text',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'u_depth': 'assert result.pieces_salvaged == 4',
        'u_sts': 'assert (result.pieces_split, result.pieces_salvaged, result.entries_salvaged) == (1, 1, 1)',
        'u_under': 'assert result.pieces_salvaged == 1',
        'u_wont': 'assert result.pieces_salvaged == 1',
    },
    'S9': {
        'e_salvage': 'assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in caplog.text',
        'e_sts': 'assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in caplog.text',
        'e_worst': 'assert "split 3 piece(s), salvaged 4 entries from 4 piece(s)" in caplog.text',
        'u_depth': 'assert result.entries_salvaged == 4',
        'u_sts': 'assert (result.pieces_split, result.pieces_salvaged, result.entries_salvaged) == (1, 1, 1)',
    },
    'S10': {
        'u_malformed': 'assert result.pieces_failed == 1',
    },
    'S11': {
        'u_malformed': 'assert result.entries == ({"name": "B"},)',
    },
    'S12': {
        'u_unavail': 'assert isinstance(raised, OllamaUnavailable)',
    },
    'S13': {
        'e_nested': 'assert "skipping chunk" in caplog.text',
        'e_quote_key': 'assert "skipping chunk" in caplog.text',
        'e_quote_list': 'assert "skipping chunk" in caplog.text',
        'u_own': 'assert isinstance(raised, EnrichmentError)',
        'u_unavail': 'assert isinstance(raised, OllamaUnavailable)',
    },
    'S14': {
        'u_parses': 'assert not result.truncated',
    },
    'S15': {
        'e_split': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'u_malformed': 'assert result.entries == ({"name": "B"},)',
        'u_parses': 'assert result.entries == ({"name": "A"},)',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
    },
    'S16': {
        'e_sts': 'assert len(calls) == 5',
        'u_parses': 'assert len(calls) == 1',
    },
    'S17': {
        'e_split': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert len(requests) == 14',
        'u_at': 'assert len(calls) == 3',
        'u_bound': 'assert max(per_input) == 7',
        'u_depth': 'assert len(calls) == 7',
        'u_malformed': 'assert result.entries == ({"name": "B"},)',
        'u_sts': 'assert result.entries == ({"name": "A"}, {"name": "B"})',
        'u_unavail': 'assert isinstance(raised, OllamaUnavailable)',
    },
    'P1': {
        'e_split': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert "split 1 piece(s), salvaged 1 entries from 1 piece(s)" in caplog.text',
        'u_para': 'assert split_in_half(text, count_tokens=_words) == [f"{first}\\n\\n{second}\\n\\n", third]',
    },
    'P2': {
        'u_nearer': 'assert split_in_half(text, count_tokens=_words)[0] == f"{first}\\n\\n"',
    },
    'P3': {
        'u_lopsided': 'assert _words(pieces[0]) == 40',
    },
    'P4': {
        'u_midpoint': 'assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]',
    },
    'P5': {
        'u_single': 'assert split_in_half("word", count_tokens=_words) == ["word"]',
    },
    'P6': {
        'e_worst': 'assert len(requests) == 14',
        'u_bound': 'assert max(per_input) == 7',
        'u_fanout': 'assert len(pieces) <= 2',
        'u_lopsided': 'assert _words(pieces[0]) == 40',
        'u_midpoint': 'assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]',
        'u_nearer': 'assert split_in_half(text, count_tokens=_words)[0] == f"{first}\\n\\n"',
        'u_para': 'assert split_in_half(text, count_tokens=_words) == [f"{first}\\n\\n{second}\\n\\n", third]',
        'u_two': 'assert not [r for r in caplog.records if r.name == "brain.ingest.chunker"]',
    },
    'P7': {
        'u_fanout': 'assert "".join(pieces) == text',
        'u_nearer': 'assert split_in_half(text, count_tokens=_words)[0] == f"{first}\\n\\n"',
        'u_para': 'assert split_in_half(text, count_tokens=_words) == [f"{first}\\n\\n{second}\\n\\n", third]',
        'u_two': 'assert all("".join(pieces) == text for pieces, text in zip(splits, inputs, strict=True))',
    },
    'X1': {
        'e_salvage': 'assert "Zorvex" not in caplog.text',
        'e_split': 'assert "Zorvex" not in caplog.text',
    },
    'X2': {
        'e_salvage': 'assert "split 0 piece(s), salvaged 3 entries from 1 piece(s)" in caplog.text',
    },
    'T1': {
        'j_trailing': 'assert salvage_array_items(\'{"entities": [1, 2, 12\', "entities") == [1, 2]',
    },
    'T2': {
        'j_badsep': 'assert salvage_array_items(text, "entities") == []',
        'j_trailing': 'assert salvage_array_items(\'{"entities": [1, 2, 12\', "entities") == [1, 2]',
    },
    'T3': {
        'j_nested': 'assert outcome == [{"name": "A"}]',
    },
    'A1': {
        'c_nested': 'assert type(raised) is EnrichmentError',
        'e_nested': 'assert outcome == []',
    },
    'A2': {
        'c_envelope': 'assert type(raised) is EnrichmentError',
    },
    'B0': {
        'e_bucket': 'assert _SENTINEL not in caplog.text',
        'e_group': 'assert _SENTINEL not in caplog.text',
        'e_quote_key': 'assert _SENTINEL not in caplog.text',
        'e_quote_list': 'assert _SENTINEL not in caplog.text',
        'm_quote': 'assert sentinel not in caplog.text',
        's_reconcile': 'assert _SYNC_SENTINEL not in caplog.text',
        's_remove': 'assert _SYNC_SENTINEL not in caplog.text',
        't_quote': 'assert sentinel not in caplog.text',
    },
    'B1': {
        'e_bucket': 'assert "synthesis failed (EnrichmentError" in caplog.text',
        'e_group': 'assert "synthesis failed (EnrichmentError" in caplog.text',
        'e_quote_key': 'assert "failed (EnrichmentError" in caplog.text',
        'e_quote_list': 'assert "failed (EnrichmentError" in caplog.text',
        'm_quote': 'assert "failed (RuntimeError)" in caplog.text',
        's_reconcile': 'assert f"graph sync ({op}) skipped for doc doc-0001: RuntimeError;" in caplog.text',
        's_remove': 'assert f"graph sync ({op}) skipped for doc doc-0001: RuntimeError;" in caplog.text',
        't_quote': 'assert "summary failed (RuntimeError)" in caplog.text',
    },
    'B2': {
        'e_quote_key': 'assert _SENTINEL not in caplog.text',
        'e_quote_list': 'assert _SENTINEL not in caplog.text',
    },
    'B3': {
        'e_group': 'assert _SENTINEL not in caplog.text',
    },
    'B4': {
        'e_bucket': 'assert _SENTINEL not in caplog.text',
    },
    'B5': {
        't_quote': 'assert sentinel not in caplog.text',
    },
    'B6': {
        'm_quote': 'assert sentinel not in caplog.text',
    },
    'B7': {
        's_reconcile': 'assert _SYNC_SENTINEL not in caplog.text',
    },
    'B8': {
        's_remove': 'assert _SYNC_SENTINEL not in caplog.text',
    },
    'B9': {
        'e_nested': 'assert "skipping chunk" in caplog.text',
        'e_quote_key': 'assert "skipping chunk" in caplog.text',
        'e_quote_list': 'assert "skipping chunk" in caplog.text',
    },
    'R1': {
        'e_split': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_sts': 'assert {(e.entity_type, e.display_name) for e in out} == {',
        'e_worst': 'assert len(requests) == 14',
        'u_bound': 'assert max(per_input) == 7',
        'u_lopsided': 'assert _words(pieces[0]) == 40',
        'u_midpoint': 'assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]',
        'u_nearer': 'assert split_in_half(text, count_tokens=_words)[0] == f"{first}\\n\\n"',
        'u_para': 'assert split_in_half(text, count_tokens=_words) == [f"{first}\\n\\n{second}\\n\\n", third]',
        'u_two': 'assert [len(pieces) for pieces in splits] == [2] * len(inputs)',
    },
    'X3': {
        'e_salvage': 'assert "skipping chunk" not in caplog.text',
    },
}
M = [(lab, f, pairs, TARGETS[lab.split()[0]]) for lab, f, pairs, _ in M]


def run(tag: str) -> dict[str, tuple[str, str] | None]:
    xml = OUT / f"{tag}.xml"
    subprocess.run([".venv/bin/pytest", "-q", "--no-cov", "--tb=long", "-p", "no:cacheprovider",
                    f"--junitxml={xml}", *TESTS.values()], capture_output=True, text=True, check=False)
    res: dict[str, tuple[str, str] | None] = {}
    for tc in ET.parse(xml).iter("testcase"):
        key = next(k for k, v in TESTS.items() if v.endswith("::" + tc.get("name", "")))
        fail = tc.find("failure")
        if fail is None:
            fail = tc.find("error")
        res[key] = None if fail is None else (fail.get("message", ""), fail.text or "")
    assert set(res) == set(TESTS), set(TESTS) ^ set(res)
    return res


def failing_line(case: str, text: str) -> str:
    """The first '>'-marked line that is one of the case's own asserts."""
    marked = [ln[1:].strip() for ln in text.splitlines() if ln.startswith(">")]
    for ln in marked:
        if ln in INVENTORY[case]:
            return ln
    return marked[0] if marked else ""


def clean() -> bool:
    return subprocess.run(["git", "status", "--porcelain", "--", "src", "tests"],
                          capture_output=True, text=True).stdout.strip() == ""


ok = True
for label, _, _, targets in M:
    for k, sn in targets.items():
        if not any(sn in a for a in INVENTORY[k]):
            print(f"target not in inventory: {label}: {k}: {sn}")
            ok = False
base = run("baseline")
a = all(v is None for v in base.values())
print(f"(a) baseline {sum(v is None for v in base.values())}/{len(base)} pass: {'OK' if a else 'FAIL'}")
ok &= a
fired: set[tuple[str, str]] = set()
for label, path, pairs, targets in M:
    if ONLY is not None and label.split()[0] not in ONLY:
        continue
    assert clean(), "dirty tree before mutation"
    p = WT / path
    text = p.read_text()
    b = True
    for old, new, n in pairs:
        if text.count(old) != n:
            print(f"    (b) {label}: {old!r} occurs {text.count(old)}x, declared {n}")
            b = False
        text = text.replace(old, new)
    p.write_text(text)
    b = b and not clean()
    res = run(label.split()[0])
    red = {k for k, v in res.items() if v is not None}
    if DRY:
        print(f"{label}: (b) {'OK' if b else 'FAIL'} red:")
        for k in sorted(red):
            msg, tb = res[k]
            print(f"    {k}: {failing_line(k, tb)[:100]!r}  [{msg[:50]!r}]")
    c = red == set(targets)
    d = True
    for k in targets:
        if res.get(k) is None:
            d = False
            continue
        msg, tb = res[k]
        line = failing_line(k, tb)
        hit = msg.startswith(("assert", "AssertionError")) and targets[k] in line and line in INVENTORY[k]
        d &= hit
        if hit:
            fired.add((k, line))
        elif not DRY:
            print(f"    (d) miss {k}: msg={msg[:60]!r} line={line[:90]!r}")
    subprocess.run(["git", "checkout", "--", path], check=True)
    after = run(label.split()[0] + "-restored")
    hyg = clean() and all(v is None for v in after.values())
    ok &= b and c and d and hyg
    if not DRY:
        print(f"{label}: (b) {'OK' if b else 'FAIL'} (c) {'OK' if c else 'FAIL'} "
              f"(d) {'OK' if d else 'FAIL'} restored-green {'OK' if hyg else 'FAIL'}")
        if not c:
            print(f"    red={sorted(red)} expected={sorted(targets)}")
inventory = {(k, ln) for k, lns in INVENTORY.items() for ln in lns}
exempt = {(k, ln) for k, ln in inventory for (ek, sn) in UNFIRED if ek == k and sn in ln}
unfired = inventory - fired - exempt
stale = exempt & fired
e = not unfired and not stale
print(f"(e) {len(fired & inventory)}/{len(inventory)} assertions fired, {len(exempt)} exempt: {'OK' if e else 'FAIL'}")
for k, ln in sorted(unfired):
    print(f"    unfired: {k}: {ln}")
ok &= e
print("ALL OK" if ok else "SOME FAILED")
```
