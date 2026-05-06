"""Phase F.A — unit tests for ``_build_self_tsquery``.

The helper builds a ``to_tsquery``-compatible string for a source document
that the new hybrid Related-docs signal uses as its FTS-leg query.
Plan: ``docs/plans/2026-05-06-related-docs-rebuild.md`` (Phase F.A).

These tests exercise the four behavioural paths the helper must support:

1. Title-only path — descriptive titles bypass body fallback.
2. Body-fallback path — short/generic titles get augmented with the
   source doc's top-frequency body lexemes.
3. Empty-fallback path — no title and no body chunks → ``""``.
4. Punctuation-safety — wiki-link and bracket syntax in titles must
   not leak literal characters into the resulting tsquery.

The fixtures match :mod:`tests.test_build_related` (helpers ``_source``
and ``_doc``) but are simplified — we don't need the embedding for these
tests because the helper only reads the title + ``chunks.tsv``. The
chunks insert still satisfies the schema by leaving ``embedding`` NULL
(allowed after migration 002).
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.wiki.build_related import _build_self_tsquery


def _doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content: str = "",
    chunk_contents: list[str] | None = None,
) -> str:
    """Insert a document (and optional chunks) and return its UUID.

    ``chunk_contents`` controls the body-fallback path. When provided,
    each entry becomes one ``chunks.content`` row; the generated
    ``chunks.tsv`` (migration 009) picks up the body terms automatically.
    Pass ``None`` to insert zero chunks (used by the empty-signal test).
    """
    row = conn.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, vault_path, draft)
        VALUES (%s, %s, %s, 'note', %s, FALSE)
        RETURNING id::text
        """,
        (
            title,
            content or f"{title} body",
            f"hash-{title}-{content[:32]}",
            f"{title or 'untitled'}.md",
        ),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])
    for index, chunk_content in enumerate(chunk_contents or []):
        conn.execute(
            """
            INSERT INTO chunks (document_id, chunk_index, content)
            VALUES (%s::uuid, %s, %s)
            """,
            (doc_id, index, chunk_content),
        )
    return doc_id


def _matches(conn: psycopg.Connection[Any], tsquery: str, text: str) -> bool:
    """Return True iff ``to_tsquery(tsquery)`` matches ``to_tsvector(text)``."""
    row = conn.execute(
        "SELECT to_tsvector('english', %s) @@ to_tsquery('english', %s)",
        (text, tsquery),
    ).fetchone()
    assert row is not None
    return bool(row[0])


# ---------------------------------------------------------------------------
# Path 1: title-only
# ---------------------------------------------------------------------------


def test_self_tsquery_from_descriptive_title(test_db: psycopg.Connection[Any]) -> None:
    doc_id = _doc(
        test_db,
        title="COMPANY_REDACTED Enrollment Reference Brief",
        chunk_contents=["Body about something completely unrelated to insurance."],
    )

    tsquery = _build_self_tsquery(test_db, doc_id, title="COMPANY_REDACTED Enrollment Reference Brief")

    assert tsquery, "title with 4 meaningful tokens must produce a non-empty tsquery"
    # Title-only path → ``plainto_tsquery`` ANDs all stemmed lexemes:
    # COMPANY_REDACTED → 'topic-ih', Enrollment → 'enrol', Reference → 'referenc',
    # Brief → 'brief'. The matching body must therefore contain *all*
    # four un-stemmed forms.
    assert _matches(
        test_db, tsquery, "An COMPANY_REDACTED enrollment reference brief document"
    )
    # Missing one of the four required lexemes (no "brief") → no match,
    # which proves the title-only path didn't silently OR the lexemes.
    assert not _matches(test_db, tsquery, "An COMPANY_REDACTED enrollment reference document")
    assert "[" not in tsquery and "]" not in tsquery
    # No body augmentation happened — body-only words must NOT appear.
    assert not _matches(test_db, tsquery, "insurance carrier networks")


# ---------------------------------------------------------------------------
# Path 2: body-fallback for short / generic titles
# ---------------------------------------------------------------------------


def test_self_tsquery_with_short_title_falls_back_to_body(
    test_db: psycopg.Connection[Any],
) -> None:
    doc_id = _doc(
        test_db,
        title="Notes",
        chunk_contents=[
            "COMPANY_REDACTED enrollment quoting platform with carrier integrations.",
            "COMPANY_REDACTED workflows replace traditional group health plans.",
        ],
    )

    tsquery = _build_self_tsquery(test_db, doc_id, title="Notes")

    assert tsquery, "fallback path must produce a non-empty tsquery for body content"
    # The body keyword "topic-ih" was appended — a doc whose chunks contain
    # "COMPANY_REDACTED" should match this self-query even though the title alone
    # ("Notes") would not.
    assert _matches(test_db, tsquery, "COMPANY_REDACTED reference brief on enrollment quoting")
    # And the title alone doesn't carry the signal — sanity check that
    # the fallback meaningfully widened the query.
    title_only = test_db.execute(
        "SELECT plainto_tsquery('english', %s)::text", ("Notes",)
    ).fetchone()
    assert title_only is not None
    assert tsquery != title_only[0]


def test_self_tsquery_with_two_token_title_still_falls_back(
    test_db: psycopg.Connection[Any],
) -> None:
    """Boundary case: 2 meaningful tokens (< 3) must still trigger fallback."""
    doc_id = _doc(
        test_db,
        title="Meeting Recap",
        chunk_contents=["COMPANY_REDACTED enrollment quoting platform with carrier integrations."],
    )

    tsquery = _build_self_tsquery(test_db, doc_id, title="Meeting Recap")

    assert tsquery
    # The body lexeme "topic-ih" must have been appended — it isn't in the
    # title, so a match here proves the fallback fired.
    assert _matches(test_db, tsquery, "COMPANY_REDACTED carrier networks")


def test_self_tsquery_skips_stop_words_when_counting(
    test_db: psycopg.Connection[Any],
) -> None:
    """A title like "On the Bus" has 3 raw tokens but only 1 meaningful."""
    doc_id = _doc(
        test_db,
        title="On the Bus",
        chunk_contents=["COMPANY_REDACTED enrollment carrier networks."],
    )

    tsquery = _build_self_tsquery(test_db, doc_id, title="On the Bus")

    assert tsquery
    # Body lexemes were appended — proves "On the Bus" was counted as
    # 1 meaningful token, not 3, and triggered the fallback path.
    assert _matches(test_db, tsquery, "COMPANY_REDACTED carrier networks")


# ---------------------------------------------------------------------------
# Path 3: empty signal
# ---------------------------------------------------------------------------


def test_self_tsquery_returns_empty_when_no_signal(
    test_db: psycopg.Connection[Any],
) -> None:
    # Empty title + zero chunks = empty fallback path.
    doc_id = _doc(test_db, title="", chunk_contents=None)

    tsquery = _build_self_tsquery(test_db, doc_id, title="")

    assert tsquery == ""


def test_self_tsquery_returns_empty_for_stop_word_only_title_and_empty_body(
    test_db: psycopg.Connection[Any],
) -> None:
    # "On the" has 0 meaningful tokens; chunks contain only stop-words
    # (Postgres English config strips them, leaving an empty tsv).
    doc_id = _doc(test_db, title="On the", chunk_contents=["the and or for"])

    tsquery = _build_self_tsquery(test_db, doc_id, title="On the")

    assert tsquery == ""


# ---------------------------------------------------------------------------
# Path 4: punctuation safety
# ---------------------------------------------------------------------------


def test_self_tsquery_handles_punctuation_safely(
    test_db: psycopg.Connection[Any],
) -> None:
    title = "person-x [[meeting]]"
    doc_id = _doc(test_db, title=title, chunk_contents=["Body about catch-up sync."])

    tsquery = _build_self_tsquery(test_db, doc_id, title=title)

    assert tsquery
    assert "[[" not in tsquery
    assert "]]" not in tsquery
    # The returned string is itself a parsable tsquery — Postgres errors
    # if we feed it back to ``to_tsquery`` and it isn't well-formed.
    row = test_db.execute(
        "SELECT to_tsquery('english', %s)::text", (tsquery,)
    ).fetchone()
    assert row is not None
    assert row[0]
    # And the title-derived terms (all three: person-x, topic-b, meeting)
    # still match a relevant body. ``plainto_tsquery`` ANDs the lexemes,
    # so the body must contain all three forms.
    assert _matches(
        test_db, tsquery, "Meeting notes from a sync with person-x"
    )


def test_self_tsquery_invalid_doc_id_falls_back_to_title_only(
    test_db: psycopg.Connection[Any],
) -> None:
    """A non-UUID ``doc_id`` skips the body-fallback DB read gracefully."""
    tsquery = _build_self_tsquery(test_db, "not-a-uuid", title="Notes")

    # Title "Notes" plainto_tsquery is non-empty (one meaningful token),
    # so the title fragment carries; body fallback was skipped because
    # the id wasn't valid. Result is whatever plainto_tsquery returned.
    expected_row = test_db.execute(
        "SELECT plainto_tsquery('english', %s)::text", ("Notes",)
    ).fetchone()
    assert expected_row is not None
    assert tsquery == expected_row[0] or tsquery == f"({expected_row[0]})"
