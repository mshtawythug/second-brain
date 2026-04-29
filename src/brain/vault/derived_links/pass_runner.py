"""Linker pass — rebuild derived_links rows for a set of touched documents."""
from typing import Any

import psycopg

from brain.vault.derived_links.directory import DirectoryStore


def rebuild_derived_for(
    conn: psycopg.Connection[Any],
    doc_ids: set[str],
    *,
    directory: DirectoryStore,
) -> int:
    """Rebuild `derived_links` rows whose src or dst is in `doc_ids`.

    Steps (all in one transaction):
      1. SELECT touched docs + their participant keys + dates.
      2. SELECT every other Gmail/Krisp doc with at least one matching key.
      3. Compute candidate pairs; run R1/R2/R3 against each.
      4. R3 supersedes R2 for the same pair.
      5. DELETE FROM derived_links WHERE src IN doc_ids OR dst IN doc_ids.
      6. INSERT the new edge set with (LEAST, GREATEST) ordering.

    Returns the count of inserted edges.
    """
    raise NotImplementedError("Implemented in Task B.4")
