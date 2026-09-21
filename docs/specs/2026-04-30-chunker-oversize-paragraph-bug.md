# Bug: chunker can emit chunks larger than the embedder context window

- **Filed:** 2026-04-30
- **Component:** `src/brain/ingest/chunker.py`
- **Severity:** High — silently ingests most of a folder, then aborts the whole `brain ingest-dir` run on the first oversized file. Killed `brain ingest-dir competitive-strategy/` at file ~70/262 during a bulk import.
- **Affected backends:** All embedders that have a finite `num_ctx` (Arctic Embed v2 via Ollama, Qwen3 via Ollama, Voyage SaaS). Observed against `arctic`.

## TL;DR

`brain.ingest.chunker.chunk_text` does not enforce an upper bound on chunk size. When a paragraph contains no `.!?` sentence terminators (DOM dumps, JSON, terraform plans, Playwright accessibility trees, single-line tables), `_split_long_paragraph` returns the entire paragraph as one piece, and that piece becomes a single chunk regardless of token count. The embedder then rejects it with HTTP 400 from Ollama: `the input length exceeds the context length`.

The chunker's `target_tokens` is treated as a *packing budget*, not a *hard ceiling* — there is no fallback path when sentence-splitting fails to reduce a paragraph below budget.

## Reproducer

**File that triggered the original failure:**
```
/Users/you/workspace/example-project/extracts/large-single-paragraph.md
```
- 172,230 chars
- 2,680 lines
- **Paragraphs (split on `\n\s*\n`): 1**
- Content: Playwright accessibility-tree dump (`- generic [ref=eN]:`-style YAML-ish text), no `.!?` outside of bracketed strings
- `tiktoken cl100k_base` token count for the whole file: ≈42K tokens
- Result: chunker emits one ~42K-token chunk, Ollama returns HTTP 400

**Synthetic minimal repro** (no external file needed):
```python
import tiktoken
from brain.ingest.chunker import chunk_text

enc = tiktoken.get_encoding("cl100k_base")
# 50,000 chars of "- foo\n  - bar\n" with no blank lines and no .!?
text = ("- node\n  - child\n  - other\n") * 2000
chunks = chunk_text(text, target_tokens=600, overlap_tokens=100,
                    count_tokens=lambda t: len(enc.encode(t)))
oversize = [c for c in chunks if len(enc.encode(c.content)) > 700]
assert not oversize, f"BUG: {len(oversize)} chunks exceed target+overlap"
```
This currently fails: a single chunk of ~10K+ tokens is produced.

**End-to-end repro:**
```bash
brain ingest /path/to/large-single-paragraph.md
# → OllamaEmbedError: Ollama returned HTTP 400: {"error":"the input length exceeds the context length"}
```

## Root cause walkthrough

`src/brain/ingest/chunker.py`:

1. **`chunk_text` (lines 19–63)** splits on `_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")`. A file with no blank lines is one paragraph.

2. **Lines 41–44** — for each paragraph:
   ```python
   if count_tokens(para) <= target_tokens:
       units.append(para)
   else:
       units.extend(_split_long_paragraph(para, target_tokens, count_tokens))
   ```
   So oversized paragraphs are handed to `_split_long_paragraph`.

3. **`_split_long_paragraph` (lines 67–84)** splits on `_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")` and packs sentences greedily. Crucially: **if a single "sentence" still exceeds `target_tokens`, it is appended as-is** (line 81: `current.append(sent)` runs unconditionally after the flush check). For a paragraph with zero `.!?`, `sentences == [para]`, the loop appends the full paragraph, and `_split_long_paragraph` returns `[para]`.

4. **Lines 47–58** then pack `units` into chunks:
   ```python
   if current and current_tokens + unit_tokens > target_tokens:
       chunks_text.append(" ".join(current))
       current = []; current_tokens = 0
   current.append(unit)
   ```
   Same bug: an oversized unit becomes its own chunk verbatim. No splitting fallback.

5. **`_add_overlap` (lines 87–94)** prepends the tail of chunk N onto chunk N+1, so even a "correctly sized" chunk can grow by up to `overlap_tokens` (currently 100). The chunker's effective ceiling — when it works — is `target_tokens + overlap_tokens` ≈ 700 tokens. There is no enforcement that the result is ≤ this number.

**Why ch13 (177KB, 1 paragraph) succeeded but ch11 (172KB, 1 paragraph) failed:** ch13's content happens to contain enough `.!?` patterns that `_SENTENCE_SPLIT` produced sub-budget pieces. ch11 has fewer sentence terminators, so the longest "sentence" landed above Ollama's `num_ctx`. The behavior is content-dependent and silent — every file is one bad sentence away from rejection.

## Expected behavior

`chunk_text` must guarantee that every emitted `Chunk.content` satisfies:
```
count_tokens(chunk.content) ≤ target_tokens + overlap_tokens
```
regardless of input. No path through the chunker should produce a chunk that the configured embedder will reject.

## Proposed fix

Add a hard-split fallback chain inside `_split_long_paragraph` (or a new helper invoked from it):

1. Sentence split on `[.!?]` (existing behavior).
2. If any resulting sentence is still `> target_tokens`, split that sentence on **single newlines** (`\n`).
3. If any resulting line is still `> target_tokens`, split on **whitespace** (` `, then `\t`).
4. If any resulting word is still `> target_tokens` (rare — base64 blobs, no-space minified JSON), split by **token count** using the supplied `count_tokens` and `tiktoken`'s decode (or by character count as a last resort).

Each step is only invoked when the previous step left a piece over budget — so well-formed prose still flows through the existing fast path unchanged.

After the fallback, `_add_overlap` must also respect the ceiling: cap the prepended tail so `count_tokens(tail + cur) ≤ target_tokens + overlap_tokens`, or skip overlap when `cur` is already at or above `target_tokens`.

**API:** no signature changes. Internal-only refactor.

**Defensive ceiling check:** at the end of `chunk_text`, assert (or log + drop) any chunk that still exceeds `target_tokens + overlap_tokens`. This is a backstop, not the primary fix — the splitter chain should make it unreachable.

## Tests required

Add to `tests/test_chunker.py`:

1. **`test_paragraph_with_no_sentence_terminators_respects_budget`** — paragraph of 10K tokens, zero `.!?`, asserts every chunk ≤ `target_tokens + overlap_tokens`.
2. **`test_giant_single_sentence_respects_budget`** — paragraph that is one sentence of 10K tokens (one `.` at the very end). Asserts every chunk ≤ ceiling.
3. **`test_paragraph_with_only_newlines_respects_budget`** — DOM-dump style: 10K tokens of `- foo\n  - bar\n` with no blank lines. Asserts ceiling.
4. **`test_word_longer_than_budget_respects_budget`** — single base64 blob token of `target_tokens + 1` tokens. Asserts ceiling (validates the character/token-level fallback).
5. **`test_overlap_does_not_exceed_ceiling`** — N small paragraphs, large overlap_tokens. Asserts `_add_overlap` never inflates a chunk past the ceiling.
6. **Regression test:** include a fixture file under `tests/fixtures/` containing a 5-10K-token slice of the original `large-single-paragraph.md` (or a synthetic equivalent in the same Playwright-tree style — do NOT commit the original file, it's not ours). Test that `chunk_text` produces only sub-ceiling chunks.

Existing tests must continue to pass unchanged.

## Acceptance criteria

- [ ] All emitted chunks satisfy `count_tokens(chunk.content) ≤ target_tokens + overlap_tokens` for arbitrary input.
- [ ] `brain ingest /Users/you/workspace/example-project/extracts/large-single-paragraph.md` succeeds without preprocessing.
- [ ] All 6 new tests above pass.
- [ ] `pytest --cov=brain.ingest.chunker` ≥ 95% (per CLAUDE.md per-module target for pure logic).
- [ ] `ruff check && mypy src/` clean.
- [ ] Full suite (`pytest`) passes — no regressions.

## Out of scope

- Re-chunking already-ingested third-party standards docs. The 11 sibling chapter files that succeeded under the buggy chunker are stored with potentially over-sized-but-luckily-under-num_ctx chunks. They will only be re-chunked on `brain ingest --force` or after a fresh import; not part of this fix.
- Tuning `target_tokens`, `overlap_tokens`, or the sentence-split regex for retrieval quality.
- Ollama `num_ctx` configuration — Arctic Embed v2's default 8192 is fine; the bug is that the chunker can exceed it.
- Embedder-side defensive truncation. The fix belongs in the chunker.

## Workaround currently in place

For that file specifically, a normalized copy was written to `/tmp/chunker-fix/large-single-paragraph.md` with blank lines inserted every 20 lines (`awk 'NR%20==0 {print ""; print; next} {print}'`) and ingested from there. The original file is unchanged. After this fix lands, `brain ingest --force` against the original path should succeed and supersede the /tmp-pathed copy.

## Files to touch

- `src/brain/ingest/chunker.py` — implement fallback chain.
- `tests/test_chunker.py` — new tests.
- `tests/fixtures/` (new) — minimal Playwright-tree-style fixture if used.

No schema, migration, embedder, or CLI changes required.
