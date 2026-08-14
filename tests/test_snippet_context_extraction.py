"""Tests for :mod:`brain.snippet_context` — the Wave 4 extraction.

``_expand_snippet_with_neighbors`` moved out of ``search.py`` (which was over
the 800-line ceiling and not grandfathered) into ``snippet_context.py``, and
its inlined ``4 * SNIPPET_LENGTH`` cap became the ``max_chars`` parameter.
Nothing else changed: the outward walk is byte-for-byte the one that shipped.

The wave's actual subject — an Otsu cut over neighbour relevance — was built,
measured against the live corpus, and REMOVED, because it engaged on 74.5% of
results and changed zero bytes of the delivered payload on 55 of 55. The
finding is recorded in ``brain.snippet_context``'s module docstring. What
survives here are the tests of behaviour that still exists, and every one of
them was written for, and verified by, a mutation:

* the extraction's byte-identical output (against a literal captured from the
  pre-move implementation, not a second run of the code under test);
* the ``max_chars`` cap and its equality with ``4 × SNIPPET_LENGTH``;
* the whole-chunk admission rule the design rests on;
* the neighbour window and the token budget as bounds.

Chunks are inserted directly rather than produced by the ingest pipeline. The
chunker's paragraph packing and overlap stitching would make ``chunk_index``
and chunk text an emergent property of the fixture prose, and every assertion
here is about which exact chunk was admitted. All fixture content is synthetic.
"""
from typing import Any

import psycopg
import pytest

from brain.search import SNIPPET_LENGTH
from brain.snippet_context import (
    DEFAULT_SNIPPET_MAX_CHARS,
    NEIGHBOR_WINDOW,
    expand_snippet_with_neighbors,
)

CHUNK_BEFORE = "Alpha ridge provisioning notes for the northern cluster."
CHUNK_MATCHED = "Beacon signal calibration log entry seventeen."
CHUNK_AFTER = "Zephyr appendix concerning gardening implements."

#: The exact string the pre-extraction ``_expand_snippet_with_neighbors``
#: returned for (CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER) with a budget that
#: admits both neighbours. Captured as a literal, NOT by re-running the helper —
#: a comparison against the code under test would agree with itself no matter
#: what the code did.
LEGACY_EXPANSION = (
    "Alpha ridge provisioning notes for the northern cluster."
    "\n\n"
    "Beacon signal calibration log entry seventeen."
    "\n\n"
    "Zephyr appendix concerning gardening implements."
)


def _seed_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    chunks: list[str],
) -> str:
    """Insert a document and its chunks verbatim; return the document_id.

    ``chunks`` land at ``chunk_index`` 0..n-1 exactly as given.
    """
    doc_id = conn.execute(
        """
        INSERT INTO documents (title, content, content_hash, content_type)
        VALUES (%s, %s, %s, 'note')
        RETURNING id
        """,
        (title, "\n\n".join(chunks), f"snippet-ctx-test:{title}"),
    ).fetchone()[0]
    for idx, text in enumerate(chunks):
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content) VALUES (%s, %s, %s)",
            (doc_id, idx, text),
        )
    return str(doc_id)


# ---------------------------------------------------------------------------
# The constant this module inlined before the extraction
# ---------------------------------------------------------------------------


def test_default_snippet_max_chars_equals_four_times_snippet_length() -> None:
    """``DEFAULT_SNIPPET_MAX_CHARS`` must stay equal to ``4 × SNIPPET_LENGTH``.

    ``snippet_context`` cannot import ``search`` (``search`` imports it), so the
    cap is a literal there. This assertion is the only thing standing between
    the two constants and silent drift — if ``SNIPPET_LENGTH`` is ever retuned,
    the snippet cap must move with it or the hard cap stops meaning what its
    docstring says.
    """
    assert DEFAULT_SNIPPET_MAX_CHARS == 4 * SNIPPET_LENGTH


# ---------------------------------------------------------------------------
# The extraction's byte-identity
# ---------------------------------------------------------------------------


def test_expansion_is_byte_identical_to_the_pre_extraction_output(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """The moved helper returns exactly what the pre-move one returned."""
    # Arrange
    doc_id = _seed_doc(
        test_db,
        title="ExtractionByteIdentityDoc",
        chunks=[CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER],
    )

    # Act
    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=1,
        best_content=CHUNK_MATCHED,
        embedder=fake_embedder,
        budget_tokens=400,
    )

    # Assert: against the captured literal, not against a second run.
    assert got == LEGACY_EXPANSION


def test_the_default_max_chars_is_the_same_cut_as_the_old_constant(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Omitting ``max_chars`` cuts exactly where ``4 * SNIPPET_LENGTH`` did.

    The parameter's default and an explicit ``4 * SNIPPET_LENGTH`` must produce
    the same string on input long enough for the cap to bite — otherwise the
    extraction changed truncation behaviour while looking like it had not.
    """
    long_chunk = "x" * (2 * DEFAULT_SNIPPET_MAX_CHARS)
    doc_id = _seed_doc(
        test_db,
        title="ExtractionDefaultCapDoc",
        chunks=[CHUNK_BEFORE, long_chunk, CHUNK_AFTER],
    )
    common = dict(
        document_id=doc_id,
        best_chunk_index=1,
        best_content=long_chunk,
        embedder=fake_embedder,
        budget_tokens=400,
    )

    implicit = expand_snippet_with_neighbors(test_db, **common)
    explicit = expand_snippet_with_neighbors(
        test_db, max_chars=4 * SNIPPET_LENGTH, **common
    )

    assert implicit == explicit
    assert len(implicit) == 4 * SNIPPET_LENGTH


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------


def test_expansion_respects_max_chars(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """``max_chars`` is the final hard cap, and it is honoured as given."""
    doc_id = _seed_doc(
        test_db,
        title="MaxCharsDoc",
        chunks=[CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER],
    )

    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=1,
        best_content=CHUNK_MATCHED,
        embedder=fake_embedder,
        budget_tokens=400,
        max_chars=40,
    )

    assert len(got) == 40, f"expected a hard cut at max_chars=40, got {len(got)}"


def test_expansion_admits_neighbours_in_full_never_mid_chunk(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Admission is whole-chunk. This is the losslessness the design rests on.

    Chunks are the boundary-aware unit and they already exist, so an admitted
    neighbour is never sliced mid-sentence. Only the final ``max_chars`` cut
    may land inside a chunk.
    """
    doc_id = _seed_doc(
        test_db,
        title="WholeChunkDoc",
        chunks=[CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER],
    )

    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=1,
        best_content=CHUNK_MATCHED,
        embedder=fake_embedder,
        budget_tokens=400,
    )

    parts = got.split("\n\n")
    assert parts == [CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER], f"got {parts!r}"


def test_neighbor_window_is_the_fetch_bound(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Nothing beyond ``chunk_index ± NEIGHBOR_WINDOW`` can be admitted.

    Without this bound an unbounded budget would pull in the whole document —
    which, given the live corpus's ~2,281-char median chunk, would be a very
    large payload indeed.
    """
    far = "far outside the window sentinel."
    chunks = [far] + [f"filler chunk {i}." for i in range(1, 6)] + [far]
    matched_index = 3
    doc_id = _seed_doc(test_db, title="WindowDoc", chunks=chunks)

    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=matched_index,
        best_content=chunks[matched_index],
        embedder=fake_embedder,
        budget_tokens=99999,
        max_chars=10**6,
    )

    admitted = got.split("\n\n")
    assert len(admitted) <= 2 * NEIGHBOR_WINDOW + 1, f"window breached: {admitted!r}"
    assert far not in admitted, "a chunk outside the window was admitted"


def test_expansion_never_exceeds_the_token_budget(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """``budget_tokens`` is a hard ceiling on what the walk may admit.

    This is the bound the live measurement found to be dominant: at the shipped
    200-token budget against a ~570-token median chunk, only 3 of 55 live
    results admitted a neighbour at all.
    """
    # FakeEmbedder.count_tokens is ~1 token per 4 chars.
    fat = "neighbour " * 400  # ≈ 4000 chars ≈ 1000 tokens
    doc_id = _seed_doc(test_db, title="BudgetDoc", chunks=[fat, CHUNK_MATCHED, fat])
    budget = 5

    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=1,
        best_content=CHUNK_MATCHED,
        embedder=fake_embedder,
        budget_tokens=budget,
    )

    assert got == CHUNK_MATCHED, (
        f"budget={budget} cannot afford a {fake_embedder.count_tokens(fat)}-token "
        f"neighbour; got {got!r}"
    )


@pytest.mark.parametrize("budget", [0, 1])
def test_no_usable_budget_returns_the_matched_chunk_alone(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    budget: int,
) -> None:
    """A zero / near-zero budget admits nothing."""
    doc_id = _seed_doc(
        test_db,
        title=f"NoBudgetDoc{budget}",
        chunks=[CHUNK_BEFORE, CHUNK_MATCHED, CHUNK_AFTER],
    )

    got = expand_snippet_with_neighbors(
        test_db,
        document_id=doc_id,
        best_chunk_index=1,
        best_content=CHUNK_MATCHED,
        embedder=fake_embedder,
        budget_tokens=budget,
    )

    assert got == CHUNK_MATCHED
