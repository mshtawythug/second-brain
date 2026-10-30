"""Decide whether a vault rename's destination is the same document as its source row.

The vault watcher (``brain.vault.watch._handle_move``) renames a row in place,
which keeps its id and every incoming backlink, ONLY when the destination file
is the same document. Debouncing coalesces a pending upsert or delete of P into
a later move P → Q. So if P was re-populated by a different document before the
rename, an unconditional path-only rename would give Q the previous document's
identity and collide on ``documents_vault_path_idx``. Plan:
``docs/plans/2026-10-22-vault-watcher-move-identity.md``.

An id-less destination is compared by CONTENT (RM-25, H-1): a brand-new,
never-synced file that replaced P and was renamed within one debounce window
used to adopt P's id. Plan: ``docs/plans/2026-10-30-rm-25-move-identity.md``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ..near_dup import shingles
from ..set_similarity import jaccard
from .derived_links.fence import strip_fence
from .frontmatter import parse_frontmatter, read_frontmatter_id

#: RM-25.D1. Below this many word 5-gram shingles on EITHER side, an id-less
#: rename is kept in place. A short or empty note has too few shingles for an
#: overlap to mean anything (one edited word moves up to five of them, and
#: ``jaccard`` of two empty sets is 0.0), so it keeps the PR #52 ruling: an
#: id-less file is identified by its path.
MIN_IDENTITY_SHINGLES = 8

#: RM-25.D1. An id-less destination sharing less than this Jaccard overlap of
#: shingles with the row's stored content is another document. An editor's
#: atomic save of the same note keeps far more than a fifth of its shingles,
#: so ordinary renames keep their id and backlinks. The accepted trade: a long
#: note rewritten almost entirely AND renamed within one debounce window is
#: treated as new, and loses its backlinks.
MIN_IDENTITY_JACCARD = 0.2


def is_another_document(conn: psycopg.Connection[Any], src_rel: str, dst_abs: Path) -> bool:
    """True iff dst is a DIFFERENT document than src's vault row.

    A dst with a frontmatter id is decided by the id alone. Ids compare as
    UUIDs, because ``documents.id`` is one and an id differing only in case is
    the same document. An id that is not a UUID cannot name a document, so it
    is unproven: the row is kept (its backlinks too) and the sync reports the
    bad id.

    An id-less dst is decided by content (:func:`_differs_by_content`), since
    ``_sync_one``'s recovery branch would otherwise identify it by its path.
    """
    dst_id = read_frontmatter_id(dst_abs)
    if dst_id is None:
        return _differs_by_content(conn, src_rel, dst_abs)
    row = conn.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault' AND vault_path = %s",
        (src_rel,),
    ).fetchone()
    if row is None:
        return False
    row_id = str(row[0])
    try:
        return uuid.UUID(dst_id) != uuid.UUID(row_id)
    except ValueError:
        return False


def _differs_by_content(conn: psycopg.Connection[Any], src_rel: str, dst_abs: Path) -> bool:
    """True iff the id-less dst's body shares too few shingles with src's stored content.

    The body is the fence-stripped text after the frontmatter, the same text
    ``sync`` stores in ``documents.content``. False (keep the rename) when
    there is no row at src, when dst cannot be read or its frontmatter does
    not parse (the sync reports that itself), or when either side has fewer
    than :data:`MIN_IDENTITY_SHINGLES` shingles.
    """
    row = conn.execute(
        "SELECT content FROM documents WHERE kind = 'vault' AND vault_path = %s",
        (src_rel,),
    ).fetchone()
    if row is None:
        return False
    try:
        _fields, body = parse_frontmatter(dst_abs.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    stored = shingles(str(row[0] or ""))
    incoming = shingles(strip_fence(body))
    if min(len(stored), len(incoming)) < MIN_IDENTITY_SHINGLES:
        return False
    return jaccard(stored, incoming) < MIN_IDENTITY_JACCARD
