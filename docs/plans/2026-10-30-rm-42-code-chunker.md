# RM-42 — Code-aware chunker

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-42". The ticket is the
spec; this is the task list. Base: `second-brain/master` at `864f6eb`.

## What ships

A chunker choice keyed on `content_type`. `code` is chunked at definition boundaries; every other
content type goes to `chunker.chunk_text` unchanged. No extractor produces `code` yet (RM-43 adds
it), so this ticket changes no stored chunk. No migration, no knob, no CLI or MCP change. The
`Chunk` dataclass, embedding and the tsv columns are unchanged.

## Files

| File | Change |
|---|---|
| `src/brain/ingest/chunk_dispatch.py` (new leaf, < 80 lines) | `chunk_for`. |
| `src/brain/ingest/code_chunker.py` (new leaf, < 450 lines) | `chunk_code`, the Python `ast` path, the structural fallback and its language table, budget packing. Stdlib only (`ast`, `re`). |
| `src/brain/ingest/__init__.py` (over the ceiling) | The two `chunk_text(...)` calls become `chunk_for(...)`, one line each, and the import line becomes `from .chunk_dispatch import Chunk, chunk_for` (`chunk_dispatch` re-exports `Chunk` in `__all__` for that). **Net zero.** CLAUDE.md row: its last hop (`7a09e97`) is already SHA-bound, and `git log --oneline 7a09e97..HEAD` is empty at `864f6eb`, so there is nothing to promote. The RM-42 hop is written SHA-bound from the start, by a later commit in the same PR that does not touch the file (the `bc3b868` precedent). |
| `tests/test_code_chunker.py` (new) | AC1 and AC2 on synthetic sources, budget guarantees, parse-failure fallback. Pure; no DB. |
| `tests/test_chunk_dispatch.py` (new) | Routing, the metadata hints, AC3. Pure; no DB. |
| `CHANGELOG.md`, spec Appendix B row → DONE, backlog row 7.3 → SHIPPED, CLAUDE.md ceiling row | Docs. |
| memory `extractors.md`, `types.md` | "verify merged". |

`vault/sync.py` (vault notes, authored markdown) and `graph_rag/extract.py` (concept extraction
windows) keep calling `chunk_text` directly; neither ever sees `code`.

## Signature — where the path comes from (decision D-A)

`ExtractedDoc` has `source_path` and `metadata`; `update_document` has neither path nor doc, only
the row's `content_type` and the post-patch metadata (`effective_meta`). Metadata is the one
carrier both sites have, and it is persisted, so a later `brain edit` of a code document re-chunks
with the same header. So:

```python
def chunk_for(
    content_type: str,
    content: str,
    metadata: Mapping[str, Any] | None,
    count_tokens: Callable[[str], int],
    /,
) -> list[Chunk]: ...
```

`chunk_code` reads two optional hints from `metadata`: `path` (repo-relative, shown in the chunk
header) and `language` (wins over the extension of `path`). RM-43 sets both and needs no further
change to `ingest/__init__.py`.

The call sites, each one line under 100 columns (net zero):

```python
chunks = chunk_for(doc.content_type, doc.content, doc.metadata, embedder.count_tokens)
chunks = chunk_for(cur_type, new_content, effective_meta, embedder.count_tokens)
```

**Approved spec deviation (RM-42.D-A):** this departs from the spec's `*, count_tokens`. with the metadata argument added, the keyword
form is 103 and 105 columns, and wrapping costs a line at each site, which breaks "net zero".
Positional-only keeps the sites one line; mypy still catches a swapped callable or mapping.

At the update site the type is the stored `cur_type`: an edit that changes `content_type` and
content in the same call chunks by the old type until the next content edit. `new_content_type or
cur_type` would make the line 113 columns. **Residual:** this is a known limitation. No caller
changes a document to or from `code` today.

## `chunk_code` behaviour

- **Budget (decision D-B):** every chunk, header included, is `<= INGEST_CHUNK_TARGET_TOKENS +
  INGEST_CHUNK_OVERLAP_TOKENS` (700), imported from `ollama_budget`: the bound the embedder's
  `num_ctx` floor is validated against. No overlap for code (a chunk is a whole definition).
- **Lines:** `\r\n` and `\r` are normalised to `\n`; line numbers are 1-based, inclusive, counted on
  that text. Empty or whitespace-only content gives `[]`. Blank lines at a segment's edges are
  trimmed, and a segment of only blank lines is dropped.
- **Header:** each chunk's content is `f"{path}:{start}-{end} {symbol}\n{body}"`, where path is
  `metadata["path"]` or `<code>` when absent. A path long enough to eat half the budget is
  shortened from the left with `…`.
- **Python** (`language == "python"`, or a `.py` / `.pyi` path): `ast.parse`. The top-level lines are
  partitioned. Each top-level `def` / `async def` / `class` is one segment, running from its first
  decorator through `end_lineno`, and a comment block directly above it (no blank line between)
  goes with it. Symbol: the name. Every run of other lines is a `<module>` segment; the first run,
  which holds the docstring and imports, is the header chunk. A segment over budget that is a class
  is partitioned the same way one level down: each method is `Class.method`, and the class line,
  docstring and attributes form `Class`. Anything else over budget, such as a huge function, goes
  to the line packer. `SyntaxError`, `ValueError` (NUL bytes) and `RecursionError` fall back to the
  structural path with the Python pattern row and **never raise**.
- **Structural fallback:** the table maps a language to one regex for a top-level declaration
  start. Its rows are python (for unparseable files), javascript/typescript, go, rust, java and
  shell. Extensions map to languages (`.js .jsx .mjs .cjs .ts .tsx`, `.go`, `.rs`, `.java`, `.sh
  .bash .zsh`). A matching line at column 0 opens a segment, and a run of comment, attribute or
  decorator lines directly above it goes with it. The symbol is the regex's name group. Lines
  before the first declaration form `<module>`. A language with no row, or no hint at all, is one
  `<module>` segment.
- **Packing an oversize segment:** split it into blank-line blocks, packed greedily to the budget.
  A block still over budget is packed line by line, and a single line over budget is split by
  characters. Each piece gets its own `path:start-end symbol` header. Packing estimates with the
  per-line counts, then checks every emitted chunk with exact `count_tokens`. A piece that fails
  the check is halved by lines until it passes, so the budget is a guarantee, not an estimate.
- Separate definitions are **not** merged into one chunk, so a hit is one definition.

## Acceptance tests

1. **AC1:** synthetic Python files. The test asserts each chunk's exact `(start, end, symbol)` and
   body for a header with a docstring and imports; a decorated function with a comment above it; an
   async def; a module constant between definitions; a large class split by method; a huge
   function split by lines; and `if __name__ == "__main__":` as `<module>`. Also: a Python 2 file and
   a file with a syntax error take the fallback without raising, and their boundaries are asserted.
2. **AC2:** one fixture per fallback language. The test asserts exact boundaries and symbols, an
   attached comment or attribute block, an oversize declaration split at blank lines, and an
   unknown extension packed as `<module>`.
3. **AC3:** for every prose content type the extractors emit (`txt`, `markdown`, `pdf`, `docx`,
   `note`, `email`, `email_thread`, `krisp_action_items`, `screenshot`), plus stdin's
   free-form types (`transcript`, `slack_thread`, `document`), `chunk_for(...) ==
   chunk_text(...)` holds on multi-paragraph, overlap-forcing and oversize-paragraph texts. A
   pinned `sha256` of `chunk_text`'s output on one fixture proves `chunk_text` itself did not
   change.
4. **Budget:** every chunk is `<= 700` on adversarial inputs: a 5,000-line function, one 20 KB line,
   a large class whose method is itself over budget, and a 2,000-character path.

Coverage: 100% line and branch on both new modules. Mutation proof of the boundary logic and the
packer runs in `git archive` copies with `PYTHONPATH` pinned, in batches of 5 or fewer.

## Decisions (approved by the coordinator, 2026-10-30)

- **D-A:** `chunk_for(content_type, content, metadata, count_tokens, /)`, positional-only, with
  `metadata` carrying the path and language. This is a spec deviation, recorded here and in the PR
  body. The reasons are that the update site has no `source_path`, and that it keeps
  `ingest/__init__.py` at net zero. The docstring carries a one-line example of the order.
- **D-B:** a budget of 700 tokens with the header included and no overlap, derived as
  `INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS` rather than restated.
- **D-C:** one definition per chunk; small definitions are not packed together.
- **Added at approval:** the header path is metadata, which RM-43 will set from the filesystem. So
  it goes through `ingest.text_safety.one_line`, which means a newline cannot forge a second header
  line. Its length is capped at 200 characters and its cost at a quarter of the budget, shortening
  from the left with `…`. The symbol is capped at 80 characters.

## Verification record

- Coverage of `code_chunker.py` and `chunk_dispatch.py` is 100% line and branch, from the two pure
  test files, run with `--noconftest`.
- Mutation testing ran on 15 mutants in three batches of five, plus one re-run, each in a fresh
  `git archive` copy with `PYTHONPATH` pinned and the import location asserted. The mutants cover
  the attachment floor, decorators, the partition cursor, the class-part stretch, structural ends,
  the budget, packing extension, the line tier, the character split, the trailing trim, path
  sanitising, the path token cap, the language hint, the prose arguments and the parse fallback.
  All 15 are killed. Two kills needed new tests:
  - M1 needed `@interface` directly above a class, a declaration line that itself starts with an
    attached prefix.
  - M3 needed a statement directly after a `def`, and a "no line lost or repeated" property on the
    fixtures.
