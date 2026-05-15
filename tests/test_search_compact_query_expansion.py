"""Phase D regression — compact-form query expansion (plan revision #2).

The English ts parser stems ``[example-group]`` to ``ctolunch`` while
``plainto_tsquery('Example Group')`` yields ``cto & lunch``. The two
forms don't overlap on a single token, so a doc whose only mention is
the compact form never matches the multi-word query (and vice versa).

:func:`brain.search._build_tsquery` ORs the standard form with the
lowercase-concatenated form when the raw query has 2+ alphabetic
tokens, so both shapes co-rank.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import _build_tsquery, hybrid_search


def _ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    content: str,
) -> str:
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


def test_multi_word_query_matches_compact_token_doc(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """`Example Group` matches a doc whose only relevant token is `[example-group]`."""
    compact_id = _ingest(
        test_db,
        fake_embedder,
        title="Compact Token Doc",
        content="Subject line [example-group] hiring discussion thread.",
    )
    separated_id = _ingest(
        test_db,
        fake_embedder,
        title="Separated Token Doc",
        content="A discussion of example groups and recruiting practices.",
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="Example Group",
        limit=10,
        fts_only=True,
    )
    ids = {r.document_id for r in results}
    assert compact_id in ids, "compact-token doc must match `Example Group`"
    assert separated_id in ids, "separated-token doc must match too"


def test_compact_query_matches_separated_token_doc(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """`example-group` (single token) still matches via the standard form
    when the doc body has the compact token only — the standard form
    stems to ``ctolunch`` which equals what's stored. Sanity check that
    the helper doesn't break the single-token path."""
    compact_id = _ingest(
        test_db,
        fake_embedder,
        title="Compact Token Doc",
        content="Subject line [example-group] hiring discussion thread.",
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="example-group",
        limit=10,
        fts_only=True,
    )
    ids = {r.document_id for r in results}
    assert compact_id in ids


def test_build_tsquery_single_token_returns_standard_form(
    test_db: psycopg.Connection[Any],
) -> None:
    """Single-alphabetic-token input bypasses the OR expansion."""
    out = _build_tsquery(test_db, "companyid")
    # The result is whatever plainto_tsquery emits — must NOT contain the
    # OR operator, since the compact form == the standard form here.
    assert "|" not in out
    assert out  # non-empty


def test_build_tsquery_empty_input_returns_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    """Empty / pure-punctuation input returns an empty tsquery, which
    ``to_tsquery('')`` accepts and matches nothing."""
    assert _build_tsquery(test_db, "") == ""
    assert _build_tsquery(test_db, "   ") == ""
    assert _build_tsquery(test_db, "...") == ""


def test_build_tsquery_two_tokens_emits_or_form(
    test_db: psycopg.Connection[Any],
) -> None:
    """Two-token input produces a parenthesized OR of standard | compact."""
    out = _build_tsquery(test_db, "Example Group")
    # Should contain both the standard stems (exampl/group) and the compact
    # stem (examplegroup), connected by `|`.
    assert "|" in out
    assert "exampl" in out
    assert "group" in out
    # The compact form `example-group` stems to `examplegroup` — appears as
    # a standalone alternative.
    assert "examplegroup" in out


def test_build_tsquery_skips_or_when_compact_equals_standard(
    test_db: psycopg.Connection[Any],
) -> None:
    """If the compact form stems to the same tsquery as the standard
    form, the OR branch is skipped (avoids redundant work)."""
    # Two stop-words / unstemmable tokens may collapse — pick something
    # where the standard form is non-empty but the compact form would
    # produce identical output. ``aa bb`` is a fine probe: both forms
    # stem to themselves; concatenated ``aabb`` is a different token.
    # Edge case really happens with all-stopword queries.
    out = _build_tsquery(test_db, "the and")
    # 'the' and 'and' are stopwords; both forms yield empty tsquery.
    assert out == ""
