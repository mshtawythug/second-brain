"""Unit tests for :func:`brain.related.compute_related`.

``compute_related`` is the per-document public entry point into the hybrid
Related-docs signal — the one ``brain ui``'s related-docs panel calls live,
in place of reading a precomputed ``static/related/<slug>.json``. The
corpus-wide driver (:func:`brain.related._iter_hybrid_neighbors`) is covered
by :mod:`tests.test_build_related_signal`; these tests pin the *per-document*
contract that driver does not exercise:

1. Ranking — a candidate sharing a distinctive title lexeme outranks one that
   only mentions it in the body, and both survive the ``_MIN_RRF_SCORE`` cut.
2. ``limit`` truncation, and its ``< 1`` rejection.
3. Source docs the corpus-wide precompute would skip (NULL ``vault_path``)
   still resolve, because a UI reader can open one.
4. Unknown / malformed document ids return ``[]`` without poisoning the
   caller's transaction.

Fixtures are synthetic throughout (CLAUDE.md rule 15). The helpers mirror
:mod:`tests.test_build_related_signal`'s ``_vector`` / ``_insert_doc`` — test
modules in this repo stay self-contained rather than importing each other.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.queries import sync_chunk_search_metadata
from brain.related import DEFAULT_RELATED_LIMIT, compute_related

VECTOR_DIM = 4096


def _vector(*components: float) -> str:
    """Return a pgvector literal of length ``VECTOR_DIM``, leading components
    set from ``components`` and the remainder zero.
    """
    values = [0.0] * VECTOR_DIM
    for index, value in enumerate(components):
        if index >= VECTOR_DIM:
            break
        values[index] = value
    return "[" + ",".join(str(v) for v in values) + "]"


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str | None,
    chunk_contents: list[str],
    chunk_vectors: list[str] | None = None,
    draft: bool = False,
) -> str:
    """Insert a document plus chunks and sync the migration-009 search
    metadata so the weighted multi-field ``chunks.tsv`` carries the title at
    weight A. Without that sync the title-overlap ranking under test cannot
    be measured at all.
    """
    body = "\n".join(chunk_contents) if chunk_contents else f"{title} body"
    row = conn.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, vault_path, draft, kind)
        VALUES (%s, %s, %s, 'note', %s, %s, 'vault')
        RETURNING id::text
        """,
        (title, body, f"hash-{title}-{vault_path}", vault_path, draft),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])

    vectors = chunk_vectors or []
    for index, content in enumerate(chunk_contents):
        if index < len(vectors):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                "VALUES (%s::uuid, %s, %s, %s::vector)",
                (doc_id, index, content, vectors[index]),
            )
        else:
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content) "
                "VALUES (%s::uuid, %s, %s)",
                (doc_id, index, content),
            )

    sync_chunk_search_metadata(conn, doc_id)
    return doc_id


def _seed_a_b_c(conn: psycopg.Connection[Any]) -> tuple[str, str, str]:
    """Seed the ranking fixture: source ``A``, strong neighbor ``B``, weak
    neighbor ``C``.

    ``B`` and ``C`` are held identical on the vector leg (both get
    ``near_vec``, cosine ≈ 0.998 with ``A``) and both mention ``ZZQUARK`` in
    their body, so both clear ``_MIN_RRF_SCORE`` through dual-leg
    accumulation. The ONLY difference is that ``B`` also carries ``ZZQUARK``
    in its *title*, which migration 009 stores at tsv weight A. That is what
    must decide the order.
    """
    src_vec = _vector(1.0, 0.0)
    near_vec = _vector(0.99, 0.05)

    a_id = _insert_doc(
        conn,
        title="ZZQUARK",
        vault_path="a-source.md",
        chunk_contents=["ZZQUARK distinctive content paragraph one."],
        chunk_vectors=[src_vec],
    )
    b_id = _insert_doc(
        conn,
        title="ZZQUARK canonical guide",
        vault_path="b-title-overlap.md",
        chunk_contents=["ZZQUARK mention in body too for dual-leg signal."],
        chunk_vectors=[near_vec],
    )
    c_id = _insert_doc(
        conn,
        title="QXBFILLER irrelevant alpha",
        vault_path="c-body-only.md",
        chunk_contents=["ZZQUARK mentioned in body but no title overlap."],
        chunk_vectors=[near_vec],
    )
    return a_id, b_id, c_id


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_compute_related_ranks_title_overlap_first(
    test_db: psycopg.Connection[Any],
) -> None:
    """``B`` (title + body overlap) must outrank ``C`` (body only).

    The assertion is deliberately about ORDER, not about row count: a
    reversed RRF sort key in ``_neighbors_for_source`` still returns two
    rows, so ``len(...) == 2`` alone would survive that defect.

    MUTATION (run 2026-08-20 — the reasoning above had never been run):
    ``related.py:498`` ``key=lambda n: (-n.score, n.title, n.document_id)``
    -> ``key=lambda n: (n.score, n.title, n.document_id)``
    -> **5 failed, 5 passed** (baseline: 10 passed).

    The row-count reasoning holds — both rows come back, in the wrong order —
    and the margin it protects is narrower than it looks: the observed scores
    were ``0.03278688`` (B) and ``0.03225806`` (C), differing in the fourth
    significant figure. ``>`` on that pair is doing real work.

    FIVE, though, not this test alone, and **three of the five go red for a
    reason that has nothing to do with what they are named for**:

    * ``test_compute_related_returns_populated_rows`` — asserts ``top.id ==
      b_id`` on the way to checking field population.
    * ``test_compute_related_honours_limit`` — ``limit=1`` truncates to the
      *wrong* document, so a truncation test fails on ranking.
    * ``test_compute_related_excludes_draft_candidates`` — asserts the full
      ``[b_id, c_id]`` order while its subject is draft exclusion.
    * ``test_compute_related_malformed_id_does_not_poison_transaction`` — its
      recovery probe is ``compute_related(...)[0].id == b_id``, so a *ranking*
      defect reddens a *transaction-isolation* test.

    That coupling is worth knowing before diagnosing a future failure here: a
    single sort-key regression presents as five unrelated-looking breakages,
    and the two named last would send a reader looking at draft filtering and
    transaction state. It is not itself a defect — asserting identity rather
    than a bare count is what makes each of those tests honest — but the
    diagnosis hazard should not have to be rediscovered under time pressure.

    Blast radius, measured on the same mutation rather than assumed: across
    ``test_related_compute``, ``test_build_related_signal``,
    ``test_build_related`` and ``test_build_related_idempotence`` it reads
    **6 failed, 41 passed** (baseline: 47 passed) — these five plus
    ``test_build_related.py::test_regenerate_related_json_caps_at_k_and_skips_drafts``,
    which is the emitter making the same ``[:k]`` truncation off the same sort.
    Restored byte-identically (``shasum``); both suites green afterwards.
    """
    a_id, b_id, c_id = _seed_a_b_c(test_db)

    related = compute_related(test_db, a_id, vector_sim_floor=0.0)

    ids = [entry.id for entry in related]
    assert ids == [b_id, c_id], [(e.id, e.title, e.score) for e in related]
    assert related[0].score > related[1].score
    assert a_id not in ids, "the source document must never be its own neighbor"


def test_compute_related_returns_populated_rows(
    test_db: psycopg.Connection[Any],
) -> None:
    """Every field the UI panel renders must be populated, not defaulted."""
    a_id, b_id, _ = _seed_a_b_c(test_db)

    top = compute_related(test_db, a_id, vector_sim_floor=0.0)[0]

    assert top.id == b_id
    assert top.title == "ZZQUARK canonical guide"
    assert top.vault_path == "b-title-overlap.md"
    assert top.source == "vault"
    assert 0.0 < top.score <= 1.0
    assert "ZZQUARK" in top.snippet


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------


def test_compute_related_honours_limit(test_db: psycopg.Connection[Any]) -> None:
    a_id, b_id, _ = _seed_a_b_c(test_db)

    related = compute_related(test_db, a_id, limit=1, vector_sim_floor=0.0)

    assert [entry.id for entry in related] == [b_id]


def test_compute_related_rejects_non_positive_limit(
    test_db: psycopg.Connection[Any],
) -> None:
    a_id, _, _ = _seed_a_b_c(test_db)

    with pytest.raises(ValueError, match="limit must be >= 1"):
        compute_related(test_db, a_id, limit=0, vector_sim_floor=0.0)


def test_compute_related_defaults_to_default_related_limit(
    test_db: psycopg.Connection[Any],
) -> None:
    """The default must be the shared constant, not a private literal."""
    a_id, _, _ = _seed_a_b_c(test_db)

    assert DEFAULT_RELATED_LIMIT >= 2
    assert len(compute_related(test_db, a_id, vector_sim_floor=0.0)) == 2


# ---------------------------------------------------------------------------
# Source-document eligibility — deliberately wider than the precompute's
# ---------------------------------------------------------------------------


def test_compute_related_works_for_source_without_vault_path(
    test_db: psycopg.Connection[Any],
) -> None:
    """``_eligible_source_docs`` skips NULL-``vault_path`` docs; the
    per-document entry point must NOT — a reader can open one, and the panel
    is expected to work there.
    """
    _, b_id, c_id = _seed_a_b_c(test_db)
    orphan_id = _insert_doc(
        test_db,
        title="ZZQUARK",
        vault_path=None,
        chunk_contents=["ZZQUARK distinctive content paragraph one."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )

    ids = [entry.id for entry in compute_related(test_db, orphan_id, vector_sim_floor=0.0)]

    assert b_id in ids and c_id in ids


def test_compute_related_excludes_draft_candidates(
    test_db: psycopg.Connection[Any],
) -> None:
    a_id, b_id, c_id = _seed_a_b_c(test_db)
    draft_id = _insert_doc(
        test_db,
        title="ZZQUARK draft companion",
        vault_path="d-draft.md",
        chunk_contents=["ZZQUARK mention in body too for dual-leg signal."],
        chunk_vectors=[_vector(0.99, 0.05)],
        draft=True,
    )

    ids = [entry.id for entry in compute_related(test_db, a_id, vector_sim_floor=0.0)]

    assert draft_id not in ids
    assert ids == [b_id, c_id]


# ---------------------------------------------------------------------------
# Unknown / malformed ids
# ---------------------------------------------------------------------------


def test_compute_related_unknown_document_returns_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_a_b_c(test_db)

    assert compute_related(
        test_db, "00000000-0000-0000-0000-000000000000", vector_sim_floor=0.0
    ) == []


def test_compute_related_malformed_id_does_not_poison_transaction(
    test_db: psycopg.Connection[Any],
) -> None:
    """A non-UUID id must be rejected in Python.

    Letting it reach ``%s::uuid`` raises ``psycopg.errors.InvalidTextRepresentation``
    and leaves the caller's transaction aborted — every later query in the
    same request would then fail with ``InFailedSqlTransaction``. This test
    fails on the raise; the follow-up query proves the connection survived.
    """
    a_id, b_id, _ = _seed_a_b_c(test_db)

    assert compute_related(test_db, "not-a-uuid", vector_sim_floor=0.0) == []

    # The connection is still usable for the caller's next query.
    assert compute_related(test_db, a_id, vector_sim_floor=0.0)[0].id == b_id


def test_compute_related_empty_corpus_returns_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    """A lone document has no neighbors — and ``_corpus_common_lexemes``
    must not divide by a zero-document corpus on the way there.
    """
    solo_id = _insert_doc(
        test_db,
        title="ZZQUARK",
        vault_path="solo.md",
        chunk_contents=["ZZQUARK distinctive content paragraph one."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )

    assert compute_related(test_db, solo_id, vector_sim_floor=0.0) == []
