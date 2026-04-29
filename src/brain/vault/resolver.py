"""Resolve a :class:`ParsedLink` to an existing document, if any.

The resolution order matches the spec — explicit prefix wins, then exact
title (case-insensitive), then alias, then a 6+-hex id-prefix fallback.
Every "no match" path returns ``None`` rather than raising; the sync engine
turns ``None`` into an ``unresolved_links`` row.
"""
import re
from dataclasses import dataclass
from typing import Any

import psycopg

from .links import ParsedLink

# Minimum length of a hex-only id prefix — same threshold as
# ``brain.queries.resolve_document_prefix``. Below this we don't even try.
_MIN_ID_PREFIX_LEN = 6
_HEX_RE = re.compile(r"^[0-9a-f-]+$")


@dataclass(frozen=True)
class ResolvedTarget:
    """Outcome of a successful link resolution.

    ``kind`` mirrors ``documents.kind`` (``"vault"`` or ``"ingested"``) so
    callers that filter by tier (e.g. backlinks for vault-only views) don't
    need a follow-up SELECT.
    """

    document_id: str
    kind: str


def resolve_link(
    conn: psycopg.Connection[Any],
    parsed: ParsedLink,
    *,
    exclude_doc_id: str | None = None,
) -> ResolvedTarget | None:
    """Resolve ``parsed`` to a ``documents`` row, or return ``None``.

    Resolution order:

    1. **Explicit ``[[brain:<prefix>]]``** — id prefix lookup against
       ``documents.id``. Min 6 hex chars enforced; collisions → ``None``.
    2. **Explicit ``[[<source>:<external_id>]]``** — exact match on
       ``(sources.kind, sources.external_id)``.
    3. **``[[Title]]`` — case-insensitive exact match** on
       ``documents.title``. Multiple matches → ``None`` (caller materializes
       as unresolved).
    4. **Alias** — case-insensitive match against any element of
       ``documents.metadata->'aliases'`` (a JSON array of strings).
    5. **6+-hex id-prefix fallback** — when the parser classified the value
       as a title but it happens to look like a hex prefix, try id-prefix
       lookup before giving up. Lets ``[[7c2a8b]]`` work even without the
       explicit ``brain:`` prefix.

    ``exclude_doc_id`` skips that document during every resolution step —
    used during sync to prevent a note from "linking to itself" when its
    title happens to match its own body text.
    """
    if parsed.target_type == "doc-id":
        return _resolve_id_prefix(
            conn, parsed.target_value, exclude_doc_id=exclude_doc_id
        )
    if parsed.target_type == "source-external":
        # ``target_source`` is set whenever ``target_type`` is
        # ``source-external``; mypy doesn't track this so assert it.
        assert parsed.target_source is not None
        return _resolve_source_external(
            conn,
            source=parsed.target_source,
            external_id=parsed.target_value,
            exclude_doc_id=exclude_doc_id,
        )
    # ``target_type == 'title'`` — try title, alias, then id-prefix fallback.
    by_title = _resolve_by_title(
        conn, parsed.target_value, exclude_doc_id=exclude_doc_id
    )
    if by_title is not None:
        return by_title
    by_alias = _resolve_by_alias(
        conn, parsed.target_value, exclude_doc_id=exclude_doc_id
    )
    if by_alias is not None:
        return by_alias
    if _looks_like_id_prefix(parsed.target_value):
        return _resolve_id_prefix(
            conn, parsed.target_value, exclude_doc_id=exclude_doc_id
        )
    return None


def _looks_like_id_prefix(value: str) -> bool:
    """True iff ``value`` could plausibly be a UUID prefix.

    Hex-only (digits + ``a-f`` + hyphens) and at least
    :data:`_MIN_ID_PREFIX_LEN` characters long. Anything else (titles with
    spaces, mixed case alphabetic words, etc.) skips the id-prefix branch.
    """
    if len(value) < _MIN_ID_PREFIX_LEN:
        return False
    return bool(_HEX_RE.match(value.lower()))


def _resolve_id_prefix(
    conn: psycopg.Connection[Any],
    prefix: str,
    *,
    exclude_doc_id: str | None,
) -> ResolvedTarget | None:
    """Look up a document by id prefix (case-insensitive hex).

    Returns ``None`` for: shorter than 6 chars, non-hex input, no match,
    or multiple matches (ambiguous). Multiple matches are *not* an error —
    they're a signal the user should disambiguate via the explicit
    ``[[brain:<longer-prefix>]]`` form, which the sync engine surfaces in
    its diagnostics.
    """
    if not _looks_like_id_prefix(prefix):
        return None
    sql = (
        "SELECT id::text, kind FROM documents "
        "WHERE id::text LIKE %s "
    )
    params: list[Any] = [prefix.lower() + "%"]
    if exclude_doc_id is not None:
        sql += "AND id <> %s "
        params.append(exclude_doc_id)
    sql += "LIMIT 2"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None
    return ResolvedTarget(document_id=str(rows[0][0]), kind=str(rows[0][1]))


def _resolve_source_external(
    conn: psycopg.Connection[Any],
    *,
    source: str,
    external_id: str,
    exclude_doc_id: str | None,
) -> ResolvedTarget | None:
    """Resolve via ``(sources.kind, sources.external_id)`` exact match.

    Returns the document(s) whose ``source_id`` points at the matched
    ``sources`` row. Multiple matches are unusual (the
    ``sources.UNIQUE(kind, external_id)`` constraint plus typical 1:1
    document↔source ratio means ambiguity is a sign of legacy data); we
    return ``None`` on multiple just like every other ambiguous path.
    """
    sql = (
        "SELECT d.id::text, d.kind FROM documents d "
        "JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = %s AND s.external_id = %s "
    )
    params: list[Any] = [source, external_id]
    if exclude_doc_id is not None:
        sql += "AND d.id <> %s "
        params.append(exclude_doc_id)
    sql += "LIMIT 2"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None
    return ResolvedTarget(document_id=str(rows[0][0]), kind=str(rows[0][1]))


def _resolve_by_title(
    conn: psycopg.Connection[Any],
    title: str,
    *,
    exclude_doc_id: str | None,
) -> ResolvedTarget | None:
    """Case-insensitive exact match on ``documents.title``.

    Uses ``LOWER(title) = LOWER(%s)`` rather than ``ILIKE`` so users with
    titles containing literal ``%`` / ``_`` characters don't accidentally
    pattern-match. Multiple matches → ``None`` (the caller logs an
    ambiguity diagnostic and the link lands in ``unresolved_links``).
    """
    sql = (
        "SELECT id::text, kind FROM documents "
        "WHERE LOWER(title) = LOWER(%s) "
    )
    params: list[Any] = [title]
    if exclude_doc_id is not None:
        sql += "AND id <> %s "
        params.append(exclude_doc_id)
    sql += "LIMIT 2"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None
    return ResolvedTarget(document_id=str(rows[0][0]), kind=str(rows[0][1]))


def _resolve_by_alias(
    conn: psycopg.Connection[Any],
    alias: str,
    *,
    exclude_doc_id: str | None,
) -> ResolvedTarget | None:
    """Case-insensitive match against any string in ``metadata->'aliases'``.

    ``aliases`` is a JSONB array of strings (per the spec's frontmatter
    contract). The match is exhaustive across the whole array, not just the
    first element. Like the other resolvers, multiple matches → ``None``.
    """
    # ``jsonb_array_elements_text`` flattens the array; LOWER() on both sides
    # gives case-insensitivity. Parameterized — the alias text is data, never
    # SQL.
    sql = (
        "SELECT id::text, kind FROM documents "
        "WHERE jsonb_typeof(metadata->'aliases') = 'array' "
        "AND EXISTS ( "
        "  SELECT 1 FROM jsonb_array_elements_text(metadata->'aliases') AS a "
        "  WHERE LOWER(a) = LOWER(%s) "
        ") "
    )
    params: list[Any] = [alias]
    if exclude_doc_id is not None:
        sql += "AND id <> %s "
        params.append(exclude_doc_id)
    sql += "LIMIT 2"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        return None
    return ResolvedTarget(document_id=str(rows[0][0]), kind=str(rows[0][1]))


def title_collisions(
    conn: psycopg.Connection[Any], title: str, *, exclude_doc_id: str | None = None
) -> list[str]:
    """Return ALL document ids whose title case-insensitively matches ``title``.

    Used by the sync engine to surface collision diagnostics
    ("two docs match `person-x`: 7c2a8b…, 9d3e4f…; use [[brain:<prefix>]] to
    disambiguate"). Empty list if there's no match; one element on the
    happy path; two or more on a real collision.
    """
    sql = (
        "SELECT id::text FROM documents "
        "WHERE LOWER(title) = LOWER(%s) "
    )
    params: list[Any] = [title]
    if exclude_doc_id is not None:
        sql += "AND id <> %s "
        params.append(exclude_doc_id)
    sql += "ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    return [str(r[0]) for r in rows]
