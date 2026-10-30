"""Real-DB tests for :mod:`brain.near_dup_store` (RM-19): scan, recompute, pairs, lens, labels.

All documents are synthetic (``tests/near_dup_fakes.py``).
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain import near_dup, near_dup_store
from brain.near_dup_store import NearDupError
from tests.near_dup_fakes import body, edit_doc, insert_doc, near_copy, pairs


def _ordered(a: str, b: str) -> tuple[str, str]:
    return near_dup.ordered_pair(a, b)


def _seed_pair(conn: psycopg.Connection[Any], seed: int = 1, **kwargs: Any) -> tuple[str, str]:
    text = body(400, seed)
    first = insert_doc(conn, text, title="First copy", **kwargs)
    second = insert_doc(conn, near_copy(text), title="Second copy")
    return first, second


# --- scan ---------------------------------------------------------------------------------


def test_a_near_copy_is_paired_and_an_unrelated_document_is_not(
    test_db: psycopg.Connection[Any],
) -> None:
    # AC1, DB half.
    first, second = _seed_pair(test_db)
    insert_doc(test_db, body(400, 77), title="Unrelated")

    report = near_dup_store.scan(test_db)

    assert pairs(test_db) == [(*_ordered(first, second), "open")]
    assert report.examined == 3
    assert report.pairs_new == 1


def test_a_30_token_stub_is_skipped_and_not_revisited(test_db: psycopg.Connection[Any]) -> None:
    # AC1: the stub gets an empty row, so a second scan does not examine it again.
    stub = insert_doc(test_db, body(30, 5), title="Stub")
    insert_doc(test_db, body(30, 5) + " extra", title="Another stub")

    first = near_dup_store.scan(test_db)
    second = near_dup_store.scan(test_db)

    assert first.short == 2
    assert second.examined == 0
    assert pairs(test_db) == []
    row = test_db.execute(
        "SELECT signature, bands FROM document_minhash WHERE document_id = %s", (stub,)
    ).fetchone()
    assert row == ([], [])


def test_stored_signatures_are_the_pure_computation(test_db: psycopg.Connection[Any]) -> None:
    text = body(400, 3)
    doc = insert_doc(test_db, text)

    near_dup_store.scan(test_db)

    row = test_db.execute(
        "SELECT signature, bands, content_hash = (SELECT content_hash FROM documents"
        " WHERE id = %s) FROM document_minhash WHERE document_id = %s",
        (doc, doc),
    ).fetchone()
    expected = near_dup.compute(text)
    assert expected is not None
    assert row == (list(expected.signature), list(expected.bands), True)


def test_a_second_scan_examines_nothing(test_db: psycopg.Connection[Any]) -> None:
    _seed_pair(test_db)
    near_dup_store.scan(test_db)

    report = near_dup_store.scan(test_db)

    assert (report.examined, report.pairs_new, report.pairs_dropped) == (0, 0, 0)


def test_limit_caps_the_documents_examined(test_db: psycopg.Connection[Any]) -> None:
    for seed in range(5):
        insert_doc(test_db, body(100, 200 + seed))

    report = near_dup_store.scan(test_db, limit=2)

    assert report.examined == 2
    assert report.remaining == 3
    assert near_dup_store.scan(test_db).examined == 3


def test_all_recomputes_every_document(test_db: psycopg.Connection[Any]) -> None:
    _seed_pair(test_db)
    near_dup_store.scan(test_db)

    report = near_dup_store.scan(test_db, recompute_all=True)

    assert report.examined == 2
    assert len(pairs(test_db)) == 1


def test_scan_rejects_a_non_positive_limit(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(NearDupError):
        near_dup_store.scan(test_db, limit=0)


# --- recompute, edits, deletes -------------------------------------------------------------


def test_editing_a_document_recomputes_and_drops_a_pair_that_no_longer_verifies(
    test_db: psycopg.Connection[Any],
) -> None:
    # AC3.
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    old_signature = test_db.execute(
        "SELECT signature FROM document_minhash WHERE document_id = %s", (second,)
    ).fetchone()

    edit_doc(test_db, second, body(400, 99))
    report = near_dup_store.scan(test_db)

    new_signature = test_db.execute(
        "SELECT signature FROM document_minhash WHERE document_id = %s", (second,)
    ).fetchone()
    assert report.examined == 1
    assert report.pairs_dropped == 1
    assert new_signature != old_signature
    assert pairs(test_db) == []


def test_an_edit_that_keeps_the_copy_keeps_the_pair(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)

    edit_doc(test_db, second, near_copy(body(400, 1)) + " appended")
    report = near_dup_store.scan(test_db)

    assert report.pairs_dropped == 0
    assert pairs(test_db) == [(*_ordered(first, second), "open")]


def test_an_edit_below_the_floor_drops_the_open_pair(test_db: psycopg.Connection[Any]) -> None:
    _first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)

    edit_doc(test_db, second, body(20, 4))
    near_dup_store.scan(test_db)

    assert pairs(test_db) == []


def test_deleting_a_document_deletes_its_pairs(test_db: psycopg.Connection[Any]) -> None:
    # AC4, through the store's own writes.
    first, _second = _seed_pair(test_db)
    near_dup_store.scan(test_db)

    test_db.execute("DELETE FROM documents WHERE id = %s", (first,))

    assert pairs(test_db) == []
    assert test_db.execute("SELECT count(*) FROM document_minhash").fetchone() == (1,)


def test_a_stale_partner_is_not_a_candidate(test_db: psycopg.Connection[Any]) -> None:
    # A stored signature whose document changed since is not trusted for pairing; the partner
    # finds the pair itself when it is re-signed.
    text = body(400, 8)
    first = insert_doc(test_db, text)
    near_dup_store.scan(test_db)
    edit_doc(test_db, first, near_copy(text) + " edited")  # first is stale now
    second = insert_doc(test_db, near_copy(text))

    near_dup_store.refresh_document(test_db, second)
    assert pairs(test_db) == []

    near_dup_store.refresh_document(test_db, first)
    assert pairs(test_db) == [(*_ordered(first, second), "open")]


def test_refresh_of_a_missing_document_returns_none(test_db: psycopg.Connection[Any]) -> None:
    assert (
        near_dup_store.refresh_document(test_db, "00000000-0000-0000-0000-000000000000") is None
    )


# --- labels ----------------------------------------------------------------------------------


def test_a_dismissed_pair_is_never_reopened(test_db: psycopg.Connection[Any]) -> None:
    # AC5: through a recompute of either end, a --all scan, and an edit that keeps the copy.
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, second, first, "dismissed")

    near_dup_store.scan(test_db, recompute_all=True)
    edit_doc(test_db, first, body(400, 1) + " touched")
    near_dup_store.scan(test_db)

    assert pairs(test_db) == [(*_ordered(first, second), "dismissed")]


def test_a_dismissed_pair_survives_an_edit_that_breaks_it(
    test_db: psycopg.Connection[Any],
) -> None:
    # R3: a label the owner set is kept, so a later edit back cannot re-open it.
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, first, second, "dismissed")

    edit_doc(test_db, second, body(400, 55))
    near_dup_store.scan(test_db)
    edit_doc(test_db, second, near_copy(body(400, 1)))
    near_dup_store.scan(test_db)

    assert pairs(test_db) == [(*_ordered(first, second), "dismissed")]


def test_a_confirmed_pair_is_kept(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, first, second, "confirmed")

    near_dup_store.scan(test_db, recompute_all=True)

    assert pairs(test_db) == [(*_ordered(first, second), "confirmed")]


def test_set_status_refuses_an_unknown_pair_and_status(test_db: psycopg.Connection[Any]) -> None:
    a = insert_doc(test_db, body(100, 1))
    b = insert_doc(test_db, body(100, 2))

    with pytest.raises(NearDupError, match="no recorded pair"):
        near_dup_store.set_status(test_db, a, b, "dismissed")
    with pytest.raises(NearDupError, match="status"):
        near_dup_store.set_status(test_db, a, b, "merged")


# --- exclusions -------------------------------------------------------------------------------


def test_two_documents_of_one_thread_are_never_paired(test_db: psycopg.Connection[Any]) -> None:
    text = body(400, 6)
    insert_doc(test_db, text, thread_id="synthetic-thread-1")
    insert_doc(test_db, near_copy(text), thread_id="synthetic-thread-1")
    insert_doc(test_db, near_copy(text) + " more", thread_id="synthetic-thread-2")

    near_dup_store.scan(test_db)

    # The third document is in another thread, so it pairs with both.
    assert len(pairs(test_db)) == 2


# --- reads and the lens ------------------------------------------------------------------------


def test_list_pairs_returns_both_titles(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)

    [row] = near_dup_store.list_pairs(test_db)

    assert {row.doc_a.document_id, row.doc_b.document_id} == {first, second}
    assert {row.doc_a.title, row.doc_b.title} == {"First copy", "Second copy"}
    assert row.status == "open"
    assert row.similarity >= near_dup.JACCARD_CUT


@pytest.mark.parametrize("end", ["first", "second"])
def test_the_lens_drops_a_pair_with_a_confidential_end(
    test_db: psycopg.Connection[Any], end: str
) -> None:
    # Both ends are gated: whichever is confidential, the pair is withheld.
    text = body(400, 12)
    insert_doc(test_db, text, sensitivity="confidential" if end == "first" else "normal")
    insert_doc(
        test_db, near_copy(text), sensitivity="confidential" if end == "second" else "normal"
    )
    near_dup_store.scan(test_db)

    assert near_dup_store.list_pairs(test_db, exclude_confidential=True) == []
    assert len(near_dup_store.list_pairs(test_db, exclude_confidential=False)) == 1


def test_list_pairs_filters_by_status_and_limit(test_db: psycopg.Connection[Any]) -> None:
    a1, a2 = _seed_pair(test_db, seed=1)
    _seed_pair(test_db, seed=2)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, a1, a2, "dismissed")

    assert len(near_dup_store.list_pairs(test_db, status="open")) == 1
    assert len(near_dup_store.list_pairs(test_db, status="dismissed")) == 1
    assert len(near_dup_store.list_pairs(test_db, status="open", limit=1)) == 1
    with pytest.raises(NearDupError):
        near_dup_store.list_pairs(test_db, status="merged")
    with pytest.raises(NearDupError):
        near_dup_store.list_pairs(test_db, limit=0)


def test_count_open_gated_excludes_confidential_and_draft_ends(
    test_db: psycopg.Connection[Any],
) -> None:
    # R4: the gated (published) count says nothing about confidential or draft pairs.
    _seed_pair(test_db, seed=1)
    _seed_pair(test_db, seed=2, sensitivity="confidential")
    _seed_pair(test_db, seed=3, draft=True)
    near_dup_store.scan(test_db)

    assert near_dup_store.count_open(test_db, exclude_confidential=False) == 3
    assert near_dup_store.count_open(test_db, exclude_confidential=True) == 1


def test_count_open_ignores_labelled_pairs(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, first, second, "dismissed")

    assert near_dup_store.count_open(test_db, exclude_confidential=False) == 0


# --- check_document (the Ingest tab's key) ------------------------------------------------------


def test_check_document_signs_the_new_document_and_names_its_partner(
    test_db: psycopg.Connection[Any],
) -> None:
    text = body(400, 14)
    old = insert_doc(test_db, text, title="Old copy", vault_path="notes/old.md")
    near_dup_store.scan(test_db)
    new = insert_doc(test_db, near_copy(text), title="New copy")

    [ref] = near_dup_store.check_document(test_db, new)

    assert (ref.document_id, ref.title, ref.vault_path, ref.sensitivity) == (
        old,
        "Old copy",
        "notes/old.md",
        "normal",
    )
    assert ref.status == "open"
    assert ref.to_dict()["similarity"] >= near_dup.JACCARD_CUT


def test_check_document_leaves_out_dismissed_partners(test_db: psycopg.Connection[Any]) -> None:
    first, second = _seed_pair(test_db)
    near_dup_store.scan(test_db)
    near_dup_store.set_status(test_db, first, second, "dismissed")

    assert near_dup_store.check_document(test_db, second) == []


def test_check_document_is_empty_for_a_unique_document(test_db: psycopg.Connection[Any]) -> None:
    doc = insert_doc(test_db, body(400, 15))

    assert near_dup_store.check_document(test_db, doc) == []


# --- presence, brief line ---------------------------------------------------------------


def test_tables_present(test_db: psycopg.Connection[Any]) -> None:
    assert near_dup_store.tables_present(test_db) is True


def test_brief_section() -> None:
    assert near_dup_store.brief_lines(0) == []
    assert near_dup_store.brief_lines(3) == ["review them with `brain dedup list`"]
    assert near_dup_store.section_title(3) == "Possible duplicates (3)"
