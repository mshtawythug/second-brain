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

import json
import math
from pathlib import Path
from typing import Any

import psycopg

from brain.queries import sync_chunk_search_metadata
from brain.wiki.build_related import (
    DEFAULT_RELATED_LIMIT,
    _build_self_tsquery,
    _corpus_common_lexemes,
    _iter_hybrid_neighbors,
    regenerate_related_json,
)

VECTOR_DIM = 4096


def _vector(*components: float) -> str:
    """Return a pgvector literal of length ``VECTOR_DIM`` with the leading
    components set to ``components`` and the rest zero. Used to construct
    near-orthogonal / near-collinear synthetic embeddings for the
    hybrid-signal tests below.
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
    vault_path: str,
    chunk_contents: list[str],
    chunk_vectors: list[str] | None = None,
    source_kind: str | None = None,
    draft: bool = False,
) -> str:
    """Insert a document with chunks and the migration-009 ``title_text`` /
    ``tags_text`` columns properly synced.

    Synthetic fixtures must call :func:`sync_chunk_search_metadata` so the
    weighted multi-field tsv (title at weight A) reflects the doc's title
    — otherwise FTS hits land on body content only and the title-overlap
    tests can't measure what they intend to.
    """
    source_id: str | None = None
    if source_kind is not None:
        row = conn.execute(
            "INSERT INTO sources (kind, external_id, metadata) "
            "VALUES (%s, %s, '{}'::jsonb) RETURNING id::text",
            (source_kind, f"{source_kind}-{title}-{vault_path}"),
        ).fetchone()
        assert row is not None
        source_id = str(row[0])
    body = "\n".join(chunk_contents) if chunk_contents else f"{title} body"
    row = conn.execute(
        """
        INSERT INTO documents
          (source_id, title, content, content_hash, content_type, vault_path,
           draft, kind)
        VALUES (%s, %s, %s, %s, 'note', %s, %s, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            body,
            f"hash-{title}-{vault_path}",
            vault_path,
            draft,
            "ingested" if source_id is not None else "vault",
        ),
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


def _hybrid(
    conn: psycopg.Connection[Any],
    *,
    k: int = DEFAULT_RELATED_LIMIT,
    vector_sim_floor: float = 0.0,
) -> dict[str, list[tuple[str, str, float]]]:
    """Run :func:`_iter_hybrid_neighbors` and group rows by source vault
    path → list of ``(related_vault_path, related_title, score)``.
    Convenience for the assertions below.
    """
    grouped: dict[str, list[tuple[str, str, float]]] = {}
    for row in _iter_hybrid_neighbors(
        conn, k=k, vector_sim_floor=vector_sim_floor
    ):
        src = str(row[0])
        rel = str(row[1])
        title = str(row[2])
        score = float(row[4])
        grouped.setdefault(src, []).append((rel, title, score))
    for entries in grouped.values():
        entries.sort(key=lambda x: -x[2])
    return grouped


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

    tsquery = _build_self_tsquery(
        test_db,
        doc_id,
        title="COMPANY_REDACTED Enrollment Reference Brief",
        corpus_common=frozenset(),
    )

    assert tsquery, "title with 4 meaningful tokens must produce a non-empty tsquery"
    # Title-only path now combines ``(plainto AND-form) | <per-token OR>``
    # (Phase F.C tuning) so a doc containing every title lexeme still
    # matches via the AND clause:
    assert _matches(
        test_db, tsquery, "An COMPANY_REDACTED enrollment reference brief document"
    )
    # And — the F.C fix — a doc with even ONE distinctive title token
    # ("COMPANY_REDACTED" alone) now matches via the OR clause. The pre-fix
    # title-only path rejected this and produced zero FTS candidates for
    # long distinctive titles such as "COMPANY_REDACTED — SVP of Engineering …".
    assert _matches(test_db, tsquery, "An COMPANY_REDACTED overview document")
    assert "[" not in tsquery and "]" not in tsquery
    # No body augmentation happened — body-only words must NOT appear.
    assert not _matches(test_db, tsquery, "insurance carrier networks")


def test_self_tsquery_long_title_matches_partial_overlap(
    test_db: psycopg.Connection[Any],
) -> None:
    """Phase F.C regression — long, distinctive titles must retrieve
    partial-overlap neighbors.

    Pre-fix: ``_build_self_tsquery`` returned a 7-way AND
    (``'30' & 'min' & 'meet' & 'person-b' & 'topic-b' & 'ali' & 'sarki'``)
    so a candidate body that contained only ``person-x`` was rejected and
    the FTS leg returned zero candidates. The doc 3508c63e in the live
    corpus exhibited this exact failure: its only person-x-mentioning
    relative (a Example Group gmail thread) never surfaced.

    Post-fix: the title-only path returns ``(AND-form) | <per-token OR>``,
    so a body containing even one distinctive title token matches.
    """
    title = "30 min meeting between person-x and Ali Sarkis"
    doc_id = _doc(
        test_db,
        title=title,
        chunk_contents=["Body about the meeting itself, no other lexemes overlap."],
    )

    tsquery = _build_self_tsquery(
        test_db, doc_id, title=title, corpus_common=frozenset()
    )

    assert tsquery
    # Partial-overlap candidate: contains "person-x" and nothing else from
    # the title. The pre-fix behavior would NOT match this. The post-fix
    # behavior MUST.
    assert _matches(
        test_db,
        tsquery,
        "Example Group: person-x replied confirming the slot.",
    )
    # And the all-tokens-present body still matches (regression boundary —
    # AND clause stays useful for stronger ranking).
    assert _matches(
        test_db,
        tsquery,
        "30 min meeting between person-x and Ali Sarkis recap",
    )


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

    tsquery = _build_self_tsquery(
        test_db, doc_id, title="Notes", corpus_common=frozenset()
    )

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

    tsquery = _build_self_tsquery(
        test_db, doc_id, title="Meeting Recap", corpus_common=frozenset()
    )

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

    tsquery = _build_self_tsquery(
        test_db, doc_id, title="On the Bus", corpus_common=frozenset()
    )

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

    tsquery = _build_self_tsquery(
        test_db, doc_id, title="", corpus_common=frozenset()
    )

    assert tsquery == ""


def test_self_tsquery_returns_empty_for_stop_word_only_title_and_empty_body(
    test_db: psycopg.Connection[Any],
) -> None:
    # "On the" has 0 meaningful tokens; chunks contain only stop-words
    # (Postgres English config strips them, leaving an empty tsv).
    doc_id = _doc(test_db, title="On the", chunk_contents=["the and or for"])

    tsquery = _build_self_tsquery(
        test_db, doc_id, title="On the", corpus_common=frozenset()
    )

    assert tsquery == ""


# ---------------------------------------------------------------------------
# Path 4: punctuation safety
# ---------------------------------------------------------------------------


def test_self_tsquery_handles_punctuation_safely(
    test_db: psycopg.Connection[Any],
) -> None:
    title = "person-x [[meeting]]"
    doc_id = _doc(test_db, title=title, chunk_contents=["Body about catch-up sync."])

    tsquery = _build_self_tsquery(
        test_db, doc_id, title=title, corpus_common=frozenset()
    )

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
    # still match a relevant body. After Phase F.C tuning the title-only
    # path is ``(AND-form) | <per-token OR>``, so any one of the lexemes
    # is sufficient — but a body containing all three still matches.
    assert _matches(
        test_db, tsquery, "Meeting notes from a sync with person-x"
    )


def test_self_tsquery_invalid_doc_id_falls_back_to_title_only(
    test_db: psycopg.Connection[Any],
) -> None:
    """A non-UUID ``doc_id`` skips the body-fallback DB read gracefully."""
    tsquery = _build_self_tsquery(
        test_db, "not-a-uuid", title="Notes", corpus_common=frozenset()
    )

    # Title "Notes" plainto_tsquery is non-empty (one meaningful token),
    # so the title fragment carries; body fallback was skipped because
    # the id wasn't valid. Result is whatever plainto_tsquery returned.
    expected_row = test_db.execute(
        "SELECT plainto_tsquery('english', %s)::text", ("Notes",)
    ).fetchone()
    assert expected_row is not None
    assert tsquery == expected_row[0] or tsquery == f"({expected_row[0]})"


# ---------------------------------------------------------------------------
# Phase F.B — _iter_hybrid_neighbors hybrid-signal tests
# ---------------------------------------------------------------------------


def test_title_overlap_outranks_vector_drift(
    test_db: psycopg.Connection[Any],
) -> None:
    """A doc sharing a distinctive title token must outrank vector-only
    neighbors even when its vector overlap with the source is weaker.

    Setup:
    - Source titled ``ZZZTOKEN`` (1 meaningful token → body-fallback path
      contributes more lexemes). Body mentions ZZZTOKEN.
    - Two ``ZZZTOKEN``-titled neighbors with embeddings orthogonal to the
      source (cosine ~0 — title-only signal carries them).
    - Two unrelated-title neighbors with embeddings near-collinear to the
      source (cosine ~0.99 — vector-only signal carries them).
    """
    src_vec = _vector(1.0, 0.0)
    near_vec = _vector(0.99, 0.05)
    far_vec = _vector(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

    _insert_doc(
        test_db,
        title="ZZZTOKEN",
        vault_path="source.md",
        chunk_contents=[
            "ZZZTOKEN distinctive content paragraph one.",
        ],
        chunk_vectors=[src_vec],
    )
    _insert_doc(
        test_db,
        title="ZZZTOKEN canonical guide",
        vault_path="title-overlap-1.md",
        chunk_contents=[
            "Mention without overlap to source body distinctive."
        ],
        chunk_vectors=[far_vec],
    )
    _insert_doc(
        test_db,
        title="ZZZTOKEN sidebar reference",
        vault_path="title-overlap-2.md",
        chunk_contents=["Different wording entirely without distinctive."],
        chunk_vectors=[far_vec],
    )
    _insert_doc(
        test_db,
        title="QXBSOMETHING irrelevant alpha",
        vault_path="vector-only-1.md",
        chunk_contents=[
            "QXBNORELATION QXBORTHOGONAL random body filler."
        ],
        chunk_vectors=[near_vec],
    )
    _insert_doc(
        test_db,
        title="QXBSOMETHING irrelevant beta",
        vault_path="vector-only-2.md",
        chunk_contents=[
            "QXBOTHER QXBLEXEMES filler unrelated material."
        ],
        chunk_vectors=[near_vec],
    )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    related = grouped["source.md"]
    titles_in_order = [title for (_, title, _) in related]

    # Both ZZZTOKEN-titled docs must rank ABOVE the vector-only docs.
    overlap_indexes = [
        i for i, t in enumerate(titles_in_order) if t.startswith("ZZZTOKEN")
    ]
    vector_indexes = [
        i for i, t in enumerate(titles_in_order) if t.startswith("QXBSOMETHING")
    ]
    assert overlap_indexes, titles_in_order
    assert vector_indexes, titles_in_order
    assert max(overlap_indexes) < min(vector_indexes), titles_in_order


def test_cosine_floor_excludes_low_similarity_neighbors(
    test_db: psycopg.Connection[Any],
) -> None:
    """When neither leg has signal, the result is empty.

    Source doc has fully distinctive title + body lexemes (no overlap
    with other docs → empty FTS leg) and orthogonal vectors (cosine ~0,
    well below the 0.25 floor). Result: 0 neighbors.
    """
    _insert_doc(
        test_db,
        title="UNIQUEALPHA",
        vault_path="src.md",
        chunk_contents=["UNIQUEALPHA UNIQUEBETA UNIQUEGAMMA paragraph."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )
    for index in range(5):
        _insert_doc(
            test_db,
            title=f"PEACOCK_{index}",
            vault_path=f"far-{index}.md",
            chunk_contents=[f"PEACOCK PHEASANT FLAMINGO heron number {index}."],
            chunk_vectors=[_vector(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
        )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.25)
    assert grouped.get("src.md", []) == []


def test_per_doc_cap_prevents_long_doc_monopoly(
    test_db: psycopg.Connection[Any],
) -> None:
    """A title-matching doc with 60 chunks must not crowd out 5 short
    matchers. The FTS leg's per-document cap (PER_DOC_CHUNK_CAP=3) holds
    the long doc to 3 candidate slots, leaving room for all 5 short docs.

    The source title is a single distinctive token (``QQTOKEN``) so
    :func:`_build_self_tsquery` falls into the body-fallback branch and
    the resulting tsquery is broad enough to hit every neighbor's chunks
    on the FTS leg. The vector leg is muted by giving every neighbor
    embeddings orthogonal to the source — top-K is determined by the
    FTS leg's per-doc cap, which is exactly what the test is measuring.
    """
    src_vec = _vector(1.0, 0.0)
    far_vec = _vector(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

    _insert_doc(
        test_db,
        title="QQTOKEN",
        vault_path="src.md",
        chunk_contents=["QQTOKEN reference paragraph one."],
        chunk_vectors=[src_vec],
    )
    _insert_doc(
        test_db,
        title="QQTOKEN long",
        vault_path="long.md",
        chunk_contents=[f"QQTOKEN section {i} content body." for i in range(60)],
        chunk_vectors=[far_vec] * 60,
    )
    for index in range(5):
        _insert_doc(
            test_db,
            title=f"QQTOKEN short {index}",
            vault_path=f"short-{index}.md",
            chunk_contents=[f"QQTOKEN short snippet {index}."],
            chunk_vectors=[far_vec],
        )

    # Use vector_sim_floor=1.1 to mute the vector leg entirely — this
    # test is specifically about the FTS leg's per-doc cap behavior.
    grouped = _hybrid(test_db, k=10, vector_sim_floor=1.1)
    related_paths = [path for (path, _, _) in grouped["src.md"]]

    # All 5 short docs must appear in the top-K neighbor list.
    for index in range(5):
        assert f"short-{index}.md" in related_paths, related_paths


def test_self_excluded_from_neighbors(test_db: psycopg.Connection[Any]) -> None:
    """A source doc's own vault_path must never appear in its neighbor list."""
    src_vec = _vector(1.0, 0.0)
    _insert_doc(
        test_db,
        title="Self-reference doc",
        vault_path="self.md",
        chunk_contents=["Body content unique to this doc."],
        chunk_vectors=[src_vec],
    )
    _insert_doc(
        test_db,
        title="Self-reference companion",
        vault_path="other.md",
        chunk_contents=["Body content very similar wording."],
        chunk_vectors=[src_vec],
    )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    related_paths = [path for (path, _, _) in grouped.get("self.md", [])]
    assert "self.md" not in related_paths


def test_short_title_falls_back_to_body_keywords(
    test_db: psycopg.Connection[Any],
) -> None:
    """A doc titled "Notes" with body about UNIQTOPIC finds a UNIQTOPIC-titled
    neighbor via the body-keyword fallback path.
    """
    far_vec_a = _vector(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    far_vec_b = _vector(0.0, 0.0, 0.0, 0.0, 1.0)
    _insert_doc(
        test_db,
        title="Notes",
        vault_path="notes.md",
        chunk_contents=[
            "UNIQTOPIC enrollment quoting body content.",
            "UNIQTOPIC carrier integrations background.",
        ],
        chunk_vectors=[far_vec_a, far_vec_a],
    )
    _insert_doc(
        test_db,
        title="UNIQTOPIC reference",
        vault_path="reference.md",
        chunk_contents=["UNIQTOPIC overview content."],
        chunk_vectors=[far_vec_b],
    )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    related_paths = [path for (path, _, _) in grouped.get("notes.md", [])]
    assert "reference.md" in related_paths


def test_empty_corpus_returns_no_neighbors(
    test_db: psycopg.Connection[Any],
) -> None:
    """A single-doc corpus has no neighbors — empty result."""
    _insert_doc(
        test_db,
        title="Solo doc",
        vault_path="solo.md",
        chunk_contents=["Solo body."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    assert grouped.get("solo.md", []) == []


def test_hybrid_neighbors_skip_drafts(test_db: psycopg.Connection[Any]) -> None:
    """A draft doc is invisible to neighbors and its own JSON isn't generated."""
    src_vec = _vector(1.0, 0.0)
    _insert_doc(
        test_db,
        title="Source",
        vault_path="src.md",
        chunk_contents=["Body about something."],
        chunk_vectors=[src_vec],
    )
    _insert_doc(
        test_db,
        title="Hidden draft",
        vault_path="hidden.md",
        chunk_contents=["Body about something."],
        chunk_vectors=[src_vec],
        draft=True,
    )

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    # Source has no real neighbors (only the draft), and the draft itself
    # never appears as a source either.
    related_paths = [path for (path, _, _) in grouped.get("src.md", [])]
    assert "hidden.md" not in related_paths
    assert "hidden.md" not in grouped


def test_hybrid_neighbors_skip_vault_path_null(
    test_db: psycopg.Connection[Any],
) -> None:
    """Docs with vault_path NULL are invisible to the precompute."""
    src_vec = _vector(1.0, 0.0)
    _insert_doc(
        test_db,
        title="Source",
        vault_path="src.md",
        chunk_contents=["Body about UNIQUE_PHRASE."],
        chunk_vectors=[src_vec],
    )
    # Insert a doc with NULL vault_path directly.
    row = test_db.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, vault_path, draft)
        VALUES (%s, %s, %s, 'note', NULL, FALSE)
        RETURNING id::text
        """,
        ("UNIQUE_PHRASE companion", "Body", "hash-no-vault"),
    ).fetchone()
    assert row is not None
    no_vault_id = str(row[0])
    test_db.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s::uuid, 0, %s, %s::vector)",
        (no_vault_id, "UNIQUE_PHRASE companion body.", src_vec),
    )
    sync_chunk_search_metadata(test_db, no_vault_id)

    grouped = _hybrid(test_db, k=10, vector_sim_floor=0.0)
    related_paths = [path for (path, _, _) in grouped.get("src.md", [])]
    assert related_paths == []


def test_regenerate_related_json_uses_chunk_content_for_snippet(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The emitted ``snippet`` field is derived from the matching chunk's
    content (whitespace-collapsed, truncated to ``SNIPPET_LENGTH``), not
    from the document's full body.
    """
    src_vec = _vector(1.0, 0.0)
    _insert_doc(
        test_db,
        title="COMPANY_REDACTED source",
        vault_path="src.md",
        chunk_contents=[
            "COMPANY_REDACTED reference\nwith\nweird whitespace\tin\tit.",
        ],
        chunk_vectors=[src_vec],
    )
    _insert_doc(
        test_db,
        title="COMPANY_REDACTED neighbor",
        vault_path="neighbor.md",
        chunk_contents=[
            "COMPANY_REDACTED enrollment context paragraph one.",
        ],
        chunk_vectors=[src_vec],
    )

    summary = regenerate_related_json(
        test_db, vault_path=tmp_path, k=5, vector_sim_floor=0.0
    )
    assert summary.errors == []

    payload = json.loads(
        (tmp_path / "static" / "related" / "src.json").read_text(encoding="utf-8")
    )
    assert payload
    snippet = payload[0]["snippet"]
    # Whitespace collapsed (no newlines / tabs leaking through).
    assert "\n" not in snippet
    assert "\t" not in snippet
    # Snippet is from the matching chunk's content.
    assert "COMPANY_REDACTED enrollment context" in snippet


def test_regenerate_related_json_score_in_unit_interval(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Every emitted score is finite and lies in [0, 1] (RRF max << 1)."""
    src_vec = _vector(1.0, 0.0)
    for index in range(3):
        _insert_doc(
            test_db,
            title=f"SHARED_TOPIC doc {index}",
            vault_path=f"doc-{index}.md",
            chunk_contents=[f"SHARED_TOPIC body content {index}."],
            chunk_vectors=[src_vec],
        )

    regenerate_related_json(
        test_db, vault_path=tmp_path, k=5, vector_sim_floor=0.0
    )

    related_root = tmp_path / "static" / "related"
    for json_path in related_root.rglob("*.json"):
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        for entry in payload:
            score = entry["score"]
            assert isinstance(score, float)
            assert math.isfinite(score)
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Phase F.D — corpus-frequency commodity-token filter on the OR clause
# ---------------------------------------------------------------------------


def test_corpus_common_lexemes_drops_high_freq_tokens(
    test_db: psycopg.Connection[Any],
) -> None:
    """Lexemes appearing in > _CORPUS_FREQ_THRESHOLD of doc titles must be
    flagged as commodity.

    With 50 "meeting"-titled docs + 1 "zzzuniq" doc, total=51 and the
    Phase-F.D threshold (0.025) gives ``ndoc > 1.275``. "meet" (ndoc 50)
    is well above; "zzzuniq" (ndoc 1) is below.

    Sized large enough that the threshold is comfortably > 1 — at smaller
    corpora the percentage cutoff would round below 1 and accept every
    lexeme.
    """
    for index in range(50):
        _doc(test_db, title=f"Meeting recap {index}", chunk_contents=[])
    _doc(test_db, title="zzzuniq distinctive", chunk_contents=[])

    common = _corpus_common_lexemes(test_db)

    assert "meet" in common
    assert "zzzuniq" not in common


def test_commodity_tokens_filtered_from_or_clause(
    test_db: psycopg.Connection[Any],
) -> None:
    """When every meaningful title token is filtered as commodity, the OR
    leg collapses and ``_build_self_tsquery`` returns just the AND form.

    Title "Meeting Notes Brief" has 3 meaningful tokens — enough to enter
    the title-only branch. With ``corpus_common`` containing all three
    stems, the per-token OR leg drops every token, so the result is
    ``title_tsq`` alone (no ``" | "`` join). The AND form (from
    ``plainto_tsquery``) is intentionally untouched.
    """
    doc_id = _doc(
        test_db, title="Meeting Notes Brief", chunk_contents=["body"]
    )

    tsquery = _build_self_tsquery(
        test_db,
        doc_id,
        title="Meeting Notes Brief",
        corpus_common=frozenset(["meet", "note", "brief"]),
    )

    plainto_row = test_db.execute(
        "SELECT plainto_tsquery('english', %s)::text", ("Meeting Notes Brief",)
    ).fetchone()
    assert plainto_row is not None
    assert tsquery == plainto_row[0]
    assert " | " not in tsquery


def test_distinctive_token_survives_filter(
    test_db: psycopg.Connection[Any],
) -> None:
    """A distinctive (non-commodity) lexeme survives the filter and appears
    in the OR portion of the resulting tsquery, while a filtered lexeme
    does not.
    """
    doc_id = _doc(
        test_db,
        title="ZZZTOKEN Meeting Sync",
        chunk_contents=["body"],
    )

    tsquery = _build_self_tsquery(
        test_db,
        doc_id,
        title="ZZZTOKEN Meeting Sync",
        corpus_common=frozenset(["meet"]),
    )

    # The result has shape ``(<AND-form>) | <OR-of-survivors>``; isolate
    # the OR leg and assert "zzztoken" survived while "meet" was filtered.
    assert ") | " in tsquery, tsquery
    or_clause = tsquery.split(") | ", 1)[1]
    assert "zzztoken" in or_clause, or_clause
    assert "'meet'" not in or_clause, or_clause
