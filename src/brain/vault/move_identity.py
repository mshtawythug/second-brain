"""Decide whether a vault rename's destination is the same document as its source row.

The vault watcher (``brain.vault.watch._handle_move``) renames a row in place,
which keeps its id and every incoming backlink, ONLY when the destination file
is the same document. Debouncing coalesces a pending upsert or delete of P into
a later move P → Q. So if P was re-populated by a different document before the
rename, an unconditional path-only rename would give Q the previous document's
identity and collide on ``documents_vault_path_idx``. Plan:
``docs/plans/2026-10-22-vault-watcher-move-identity.md``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg

from .frontmatter import read_frontmatter_id


def is_another_document(conn: psycopg.Connection[Any], src_rel: str, dst_abs: Path) -> bool:
    """True iff dst's frontmatter id names a DIFFERENT document than src's vault row.

    An id-less dst is NOT another document: ``_sync_one``'s recovery branch
    identifies an id-less file by its path, so the rename is kept (see the
    ruling in the plan). Ids compare as UUIDs, because ``documents.id`` is one
    and an id differing only in case is the same document. An id that is not
    a UUID cannot name a document, so it is unproven like an id-less one: the
    row is kept (its backlinks too) and the sync reports the bad id.
    """
    dst_id = read_frontmatter_id(dst_abs)
    if dst_id is None:
        return False
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
