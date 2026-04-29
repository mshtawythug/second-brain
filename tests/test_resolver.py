"""Tests for brain.vault.resolver.resolve_link.

Real-DB pattern (per CLAUDE.md): seed a small corpus of vault + ingested
documents and assert each resolution rule in isolation.
"""
import json

import psycopg

from brain.vault.links import ParsedLink
from brain.vault.resolver import (
    ResolvedTarget,
    resolve_link,
    title_collisions,
)

# ---------------------------------------------------------------------------
# Helpers — minimal DB seeding without going through the full ingest path,
# which would require an embedder. Resolution doesn't care about chunks /
# embeddings; only about ``documents`` (+ ``sources`` for source-external).
# ---------------------------------------------------------------------------


def _insert_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    kind: str = "vault",
    content: str = "body",
    source_id: str | None = None,
    aliases: list[str] | None = None,
    metadata: dict | None = None,
    content_hash: str | None = None,
) -> str:
    """Insert one row into ``documents`` and return its id."""
    meta = dict(metadata or {})
    if aliases is not None:
        meta["aliases"] = aliases
    row = conn.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, kind, metadata, source_id)
        VALUES (%s, %s, %s, 'note', %s, %s::jsonb, %s)
        RETURNING id::text
        """,
        (
            title,
            content,
            content_hash or f"hash-{title}-{kind}-{content}",
            kind,
            json.dumps(meta),
            source_id,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_source(
    conn: psycopg.Connection, *, kind: str, external_id: str
) -> str:
    """Insert one row into ``sources`` and return its id."""
    row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, '{}'::jsonb) RETURNING id::text",
        (kind, external_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _link(
    target_value: str,
    target_type: str = "title",
    target_source: str | None = None,
) -> ParsedLink:
    """Build a ParsedLink for resolver input — concise per-test factory."""
    return ParsedLink(
        raw=f"[[{target_value}]]",
        kind="wiki",
        target_type=target_type,  # type: ignore[arg-type]
        target_value=target_value,
        target_source=target_source,
        display_text=None,
        heading=None,
    )


# ---------------------------------------------------------------------------
# Resolution rules — one per test, in spec order.
# ---------------------------------------------------------------------------


def test_resolves_explicit_brain_id_prefix(test_db: psycopg.Connection) -> None:
    doc_id = _insert_doc(test_db, title="Some Note")
    parsed = _link(doc_id[:6], target_type="doc-id")
    target = resolve_link(test_db, parsed)
    assert target == ResolvedTarget(document_id=doc_id, kind="vault")


def test_brain_id_prefix_below_min_length_returns_none(
    test_db: psycopg.Connection,
) -> None:
    # 4 hex chars — below the 6-char minimum.
    parsed = _link("abcd", target_type="doc-id")
    assert resolve_link(test_db, parsed) is None


def test_brain_id_prefix_with_no_match_returns_none(
    test_db: psycopg.Connection,
) -> None:
    parsed = _link("ffffff", target_type="doc-id")
    assert resolve_link(test_db, parsed) is None


def test_brain_id_prefix_collision_returns_none(
    test_db: psycopg.Connection,
) -> None:
    """Two docs sharing a prefix → ambiguous → ``None``."""
    # Manually insert two docs with controlled UUIDs that share a prefix.
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type) "
        "VALUES ('aaaaaaaa-1111-1111-1111-111111111111', 'A', 'a', 'h-collide-A', 'note')"
    )
    test_db.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type) "
        "VALUES ('aaaaaaaa-2222-2222-2222-222222222222', 'B', 'b', 'h-collide-B', 'note')"
    )
    parsed = _link("aaaaaaaa", target_type="doc-id")
    assert resolve_link(test_db, parsed) is None


def test_resolves_explicit_source_external(test_db: psycopg.Connection) -> None:
    src_id = _insert_source(test_db, kind="krisp", external_id="abc123")
    doc_id = _insert_doc(test_db, title="Krisp call", kind="ingested", source_id=src_id)
    parsed = _link("abc123", target_type="source-external", target_source="krisp")
    target = resolve_link(test_db, parsed)
    assert target == ResolvedTarget(document_id=doc_id, kind="ingested")


def test_source_external_wrong_source_returns_none(
    test_db: psycopg.Connection,
) -> None:
    src_id = _insert_source(test_db, kind="krisp", external_id="abc123")
    _insert_doc(test_db, title="Krisp call", kind="ingested", source_id=src_id)
    parsed = _link("abc123", target_type="source-external", target_source="slack")
    assert resolve_link(test_db, parsed) is None


def test_resolves_exact_title_case_insensitive(
    test_db: psycopg.Connection,
) -> None:
    doc_id = _insert_doc(test_db, title="person-x conversation")
    parsed = _link("person-a conversation")
    target = resolve_link(test_db, parsed)
    assert target == ResolvedTarget(document_id=doc_id, kind="vault")


def test_title_collision_returns_none(test_db: psycopg.Connection) -> None:
    """Two docs with the same title → ambiguous → ``None``."""
    _insert_doc(test_db, title="person-x", content="first")
    _insert_doc(test_db, title="person-a", content="second")  # case-insensitive collision
    parsed = _link("person-x")
    assert resolve_link(test_db, parsed) is None


def test_resolves_via_alias(test_db: psycopg.Connection) -> None:
    doc_id = _insert_doc(
        test_db,
        title="person-x conversation",
        aliases=["person-x", "person-a-talk"],
    )
    parsed = _link("person-a-talk")
    target = resolve_link(test_db, parsed)
    assert target == ResolvedTarget(document_id=doc_id, kind="vault")


def test_alias_match_is_case_insensitive(test_db: psycopg.Connection) -> None:
    doc_id = _insert_doc(test_db, title="X", aliases=["My-Alias"])
    parsed = _link("MY-ALIAS")
    target = resolve_link(test_db, parsed)
    assert target is not None
    assert target.document_id == doc_id


def test_alias_collision_returns_none(test_db: psycopg.Connection) -> None:
    _insert_doc(test_db, title="A", aliases=["shared"])
    _insert_doc(test_db, title="B", content="other body", aliases=["shared"])
    parsed = _link("shared")
    assert resolve_link(test_db, parsed) is None


def test_id_prefix_fallback_when_title_misses(
    test_db: psycopg.Connection,
) -> None:
    """A 6+-hex 'title' that's actually an id prefix resolves via fallback."""
    doc_id = _insert_doc(test_db, title="Real Title")
    parsed = _link(doc_id[:8])  # a hex prefix; classified as 'title' by parser
    target = resolve_link(test_db, parsed)
    assert target is not None
    assert target.document_id == doc_id


def test_id_prefix_fallback_skipped_for_non_hex(
    test_db: psycopg.Connection,
) -> None:
    _insert_doc(test_db, title="Real Title")
    parsed = _link("not-hex-zz")
    assert resolve_link(test_db, parsed) is None


def test_exclude_doc_id_skips_self(test_db: psycopg.Connection) -> None:
    """Self-links are intentionally excluded so a note doesn't link to itself."""
    doc_id = _insert_doc(test_db, title="person-x")
    parsed = _link("person-x")
    # Without exclude_doc_id, resolves to itself.
    assert resolve_link(test_db, parsed) is not None
    # With exclude_doc_id, returns None.
    assert resolve_link(test_db, parsed, exclude_doc_id=doc_id) is None


def test_exclude_doc_id_skips_self_in_id_prefix(
    test_db: psycopg.Connection,
) -> None:
    doc_id = _insert_doc(test_db, title="X")
    parsed = _link(doc_id[:6], target_type="doc-id")
    assert resolve_link(test_db, parsed, exclude_doc_id=doc_id) is None


def test_exclude_doc_id_skips_self_in_source_external(
    test_db: psycopg.Connection,
) -> None:
    src_id = _insert_source(test_db, kind="krisp", external_id="x1")
    doc_id = _insert_doc(test_db, title="K", kind="ingested", source_id=src_id)
    parsed = _link("x1", target_type="source-external", target_source="krisp")
    assert resolve_link(test_db, parsed, exclude_doc_id=doc_id) is None


def test_resolution_order_explicit_beats_title(
    test_db: psycopg.Connection,
) -> None:
    """Explicit ``brain:`` short-circuits the title path."""
    title_doc = _insert_doc(test_db, title="abcdef")
    other_doc = _insert_doc(test_db, title="other", content="body other")
    # Make a brain: link whose prefix lands on `other_doc`, not `title_doc`.
    parsed = _link(other_doc[:6], target_type="doc-id")
    target = resolve_link(test_db, parsed)
    assert target is not None
    assert target.document_id == other_doc
    assert target.document_id != title_doc


def test_no_match_at_any_step_returns_none(
    test_db: psycopg.Connection,
) -> None:
    parsed = _link("does not exist anywhere")
    assert resolve_link(test_db, parsed) is None


# ---------------------------------------------------------------------------
# title_collisions helper.
# ---------------------------------------------------------------------------


def test_title_collisions_returns_all_matching_ids(
    test_db: psycopg.Connection,
) -> None:
    a = _insert_doc(test_db, title="person-x")
    b = _insert_doc(test_db, title="person-a", content="other body")
    ids = title_collisions(test_db, "person-x")
    assert sorted(ids) == sorted([a, b])


def test_title_collisions_excludes_self(test_db: psycopg.Connection) -> None:
    a = _insert_doc(test_db, title="person-x")
    b = _insert_doc(test_db, title="person-a", content="other body")
    others = title_collisions(test_db, "person-x", exclude_doc_id=a)
    assert others == [b]


def test_title_collisions_empty_for_no_match(
    test_db: psycopg.Connection,
) -> None:
    assert title_collisions(test_db, "missing") == []


# ---------------------------------------------------------------------------
# Defensive: corrupt aliases data shouldn't crash the resolver.
# ---------------------------------------------------------------------------


def test_alias_resolver_tolerates_non_array_metadata(
    test_db: psycopg.Connection,
) -> None:
    """A doc with ``metadata.aliases`` set to a string (not array) doesn't crash."""
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, metadata) "
        "VALUES ('weird', 'body', 'h-weird', 'note', "
        "'{\"aliases\": \"not-an-array\"}'::jsonb)"
    )
    parsed = _link("not-an-array")
    # Resolver must not crash; alias path silently misses; title also misses.
    assert resolve_link(test_db, parsed) is None
