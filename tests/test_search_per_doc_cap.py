"""Phase D regression — per-document FTS candidate cap (plan revision #1).

Without the cap, a single long doc whose title or body matches the query
can fill the entire 50-candidate FTS slot and starve every other matching
doc, so a top-N FTS-only search returns one doc with N chunks instead of
N distinct docs. The window-function CTE in ``hybrid_search`` keeps at
most :data:`brain.search.PER_DOC_CHUNK_CAP` chunks per ``document_id``
before the global ``LIMIT 50``, so other matching docs survive.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import CANDIDATE_LIMIT, PER_DOC_CHUNK_CAP, hybrid_search


def _ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    content: str,
) -> str:
    """Helper — ingest a doc and return its document_id."""
    res = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
        tags=[],
    )
    assert res.document_id is not None
    return res.document_id


def test_long_doc_does_not_starve_other_matching_docs(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A long matching doc with chunks > CANDIDATE_LIMIT must not starve
    small matching docs.

    The cap is meaningful only when removing it would actually exhaust
    the 50-candidate FTS slot. We size the long doc so its raw chunk
    count exceeds :data:`brain.search.CANDIDATE_LIMIT` (=50). Without the
    cap, those chunks alone would fill the candidate set and the 5 small
    docs would be invisible.

    Verified non-tautological: patching ``PER_DOC_CHUNK_CAP = 999``
    locally makes this test fail (the long doc fills all 50 candidate
    slots, small docs never make it through). Restoring the cap to 3
    makes it pass.
    """
    # Long doc — 60 distinct paragraphs, each containing the rare
    # keyword 'pelican' so every chunk independently matches the FTS
    # query. Each paragraph is large enough (~2400 chars / ~600 tokens
    # via FakeEmbedder.count_tokens which is len/4) that the chunker
    # emits roughly one chunk per paragraph at the default 600-token
    # target. We need >50 chunks total so removing the cap actually
    # starves the small docs.
    long_paragraphs = [
        f"Section {i} discussing pelican habitat and pelican migration. "
        + ("filler content " * 200)
        for i in range(60)
    ]
    long_body = "\n\n".join(long_paragraphs)
    long_doc_id = _ingest(
        test_db, fake_embedder, title="Long Pelican Doc", content=long_body
    )

    # Confirm the chunker actually produced more chunks than the global
    # candidate slot — otherwise removing the cap wouldn't starve the
    # small docs and the test would be tautological.
    long_chunk_row = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (long_doc_id,)
    ).fetchone()
    assert long_chunk_row is not None
    long_chunk_count = int(long_chunk_row[0])
    assert long_chunk_count > CANDIDATE_LIMIT, (
        f"long doc must emit >{CANDIDATE_LIMIT} chunks for the cap test "
        f"to be non-tautological (got {long_chunk_count}). Bump the "
        f"paragraph count."
    )

    # Five small docs — title-keyword + a one-line body so each contributes
    # exactly one matching chunk.
    small_ids = [
        _ingest(
            test_db,
            fake_embedder,
            title=f"Small Pelican Doc {i}",
            content=f"Brief mention of pelican {i} here.",
        )
        for i in range(5)
    ]

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="pelican",
        limit=6,
        fts_only=True,
    )
    returned_ids = {r.document_id for r in results}
    # All 5 small docs must appear despite the long doc having more chunks
    # than CANDIDATE_LIMIT. Without the per-doc cap, the long doc would
    # consume every slot and starve these.
    for sid in small_ids:
        assert sid in returned_ids, (
            f"small doc {sid} missing from results — long doc starved it. "
            f"Got: {returned_ids}"
        )
    # The long doc itself should still rank.
    assert long_doc_id in returned_ids


def test_per_doc_chunk_cap_constant_value() -> None:
    """The cap is a small positive integer — guards against accidental
    regressions to 0/None and from drifting too high (defeats the cap)."""
    assert isinstance(PER_DOC_CHUNK_CAP, int)
    assert 1 <= PER_DOC_CHUNK_CAP <= 10
