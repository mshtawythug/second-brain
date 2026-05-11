"""Tests for the Q1-A per-chunk context-window expansion in hybrid_search.

Wave Q1-A (2026-05-11) adds ``snippet_context_tokens``: after the best-matching
chunk is identified, neighboring chunks (``chunk_index ± 2``) from the same
document are stitched around it up to the given token budget.  ``0`` (default)
leaves the legacy single-chunk snippet unchanged.
"""
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SNIPPET_LENGTH, hybrid_search

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ingest_multi_chunk(
    conn: psycopg.Connection,
    embedder: Any,
    *,
    title: str,
    paragraphs: list[str],
) -> str:
    """Ingest a doc built from several short paragraphs and return its document_id.

    Each paragraph is expected to become a separate chunk because they are
    separated by a blank line — the chunker splits on paragraph boundaries.
    """
    content = "\n\n".join(paragraphs)
    doc = ExtractedDoc(
        title=title,
        content=content,
        content_type="note",
        source_path=None,
        metadata={},
    )
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=doc,
        source_kind="manual",
        source_external_id=f"snippet-test:{title}",
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# Test 1: snippet_context_tokens=0 returns single-chunk snippet (default)
# ---------------------------------------------------------------------------


def test_snippet_context_zero_returns_single_chunk(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """snippet_context_tokens=0 (default) returns the raw single-chunk snippet.

    The returned snippet must be truncated to SNIPPET_LENGTH chars and must
    NOT include text from neighboring chunks.
    """
    paragraphs = [
        "alpha bravo context window test paragraph one",
        "NEIGHBOR_BEFORE: paragraph that precedes the match",
        "NEIGHBOR_AFTER: paragraph that follows the match",
    ]
    _ingest_multi_chunk(
        test_db, fake_embedder, title="CtxZeroDoc", paragraphs=paragraphs
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="alpha bravo context window",
        limit=3,
        snippet_context_tokens=0,
    )
    assert results, "expected at least one result"
    snippet = results[0].snippet
    # With no expansion, snippet is at most SNIPPET_LENGTH chars.
    assert len(snippet) <= SNIPPET_LENGTH, (
        f"expected snippet ≤ {SNIPPET_LENGTH} chars, got {len(snippet)}"
    )


# ---------------------------------------------------------------------------
# Test 2: snippet_context_tokens > 0 stitches neighbors into snippet
# ---------------------------------------------------------------------------


def test_snippet_context_expands_with_neighbors(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """A positive token budget stitches neighboring chunks around the best match.

    We ingest a doc whose chunks are:
      chunk 0: filler text (before)
      chunk 1: the query-matching chunk
      chunk 2: filler text (after)

    With a generous token budget the snippet should include content from
    the neighboring chunks so the result is longer than a single chunk.
    """
    # Use a very long, distinct filler so chunk 0 and 2 are clearly identifiable.
    filler_before = "BEFORE_MARKER " + ("word " * 20)
    match_chunk = "expand neighbor alpha bravo match"
    filler_after = "AFTER_MARKER " + ("word " * 20)

    doc_id = _ingest_multi_chunk(
        test_db,
        fake_embedder,
        title="CtxExpandDoc",
        paragraphs=[filler_before, match_chunk, filler_after],
    )
    assert doc_id  # silence unused-variable warning

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="expand neighbor alpha bravo",
        limit=3,
        snippet_context_tokens=400,   # generous budget
    )
    assert results, "expected CtxExpandDoc in results"
    snippet = results[0].snippet

    # The snippet should include the matched chunk AND at least one neighbor.
    assert "expand neighbor alpha bravo match" in snippet, (
        "matched chunk content must appear in expanded snippet"
    )
    # With budget=400, at least one neighboring chunk must be included.
    has_before = "BEFORE_MARKER" in snippet
    has_after = "AFTER_MARKER" in snippet
    assert has_before or has_after, (
        "expected at least one neighboring chunk in the expanded snippet "
        f"(budget=400). snippet={snippet!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: token budget is respected — neighbor excluded when budget exhausted
# ---------------------------------------------------------------------------


def test_snippet_context_respects_token_budget(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """A tiny token budget (1 token) means the neighbor chunk is not stitched.

    Strategy: build paragraphs large enough (600 tokens each, using
    FakeEmbedder.count_tokens = max(1, len // 4)) that the chunker places
    them in separate chunks.  The neighbor-detection marker appears only at
    the BEGINNING of each filler paragraph — well outside the overlap window
    (last 100 tokens = last 400 chars) that the chunker stitches onto the
    next chunk.  Therefore:

    * budget=1 → only the matched chunk is returned; marker absent.
    * budget=large → neighbor chunk is stitched in; marker present.
    """
    # 600 tokens = 2400 chars.  The marker occupies the first ~30 chars,
    # which is far outside the last-100-token overlap region (last 400 chars).
    filler_before = "BUDGET_NEIGHBOR_MARKER " + "a " * 1189   # ≈ 2401 chars, 600 tokens
    match_chunk = "budget token test signal unique"
    filler_after = "BUDGET_NEIGHBOR_AFTER_MARKER " + "b " * 1187  # same token count, diff text

    _ingest_multi_chunk(
        test_db,
        fake_embedder,
        title="CtxBudgetDoc",
        paragraphs=[filler_before, match_chunk, filler_after],
    )

    results_tiny = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="budget token test signal",
        limit=3,
        snippet_context_tokens=1,   # impossibly tiny — no neighbor fits
    )
    assert results_tiny, "expected CtxBudgetDoc in results (budget=1)"
    snippet_tiny = results_tiny[0].snippet

    results_large = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="budget token test signal",
        limit=3,
        snippet_context_tokens=9999,  # huge — neighbor must be included
    )
    assert results_large, "expected CtxBudgetDoc in results (budget=9999)"
    snippet_large = results_large[0].snippet

    # With budget=1: neighbor chunk (containing the marker at its start) is
    # NOT fetched, so the marker must be absent from the snippet.
    assert "BUDGET_NEIGHBOR_MARKER" not in snippet_tiny, (
        "neighbor marker must be absent from snippet when budget=1; "
        f"snippet={snippet_tiny!r}"
    )
    # With budget=9999: the neighbor IS stitched in, so its marker appears.
    assert "BUDGET_NEIGHBOR_MARKER" in snippet_large, (
        "neighbor marker must appear in snippet when budget=9999; "
        f"snippet={snippet_large!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: hard outer cap at 4 × SNIPPET_LENGTH
# ---------------------------------------------------------------------------


def test_snippet_context_hard_cap(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """The expanded snippet is capped at 4 × SNIPPET_LENGTH chars.

    Even if neighboring chunks are huge, the returned snippet must not
    exceed 4 × SNIPPET_LENGTH.
    """
    # Each filler chunk is well over SNIPPET_LENGTH chars on its own.
    long_filler = "hard cap filler " + ("x" * SNIPPET_LENGTH)
    match_chunk = "hard cap query signal unique"

    _ingest_multi_chunk(
        test_db,
        fake_embedder,
        title="CtxHardCapDoc",
        paragraphs=[long_filler, match_chunk, long_filler],
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="hard cap query signal",
        limit=3,
        snippet_context_tokens=99999,  # huge budget — cap must kick in
    )
    assert results, "expected CtxHardCapDoc in results"
    snippet = results[0].snippet
    assert len(snippet) <= 4 * SNIPPET_LENGTH, (
        f"snippet exceeds hard cap: {len(snippet)} > {4 * SNIPPET_LENGTH}"
    )
