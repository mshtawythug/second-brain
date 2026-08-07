"""Two documents sharing a title must both ingest cleanly (#23).

**The defect.** `_ingested_relative_path` has always had correct, deterministic
collision handling — append a short hash of the immutable ``documents.id``. But
the SINGLE-DOCUMENT path passed an empty ``set()`` as its collision oracle, so
the check could never see a path an existing row already owned. PostgreSQL's
``documents_vault_path_idx`` raised instead, as a raw psycopg traceback.

**Why it was worse than a traceback.** The row commits BEFORE the mirror write,
so the failure left ``vault_path IS NULL``: searchable, scanned by
``backfill scan-secrets``, and invisible in the vault, the wiki and the UI tree,
all of which key on ``vault_path IS NOT NULL``. Re-running `brain ingest`
reported ``skipped (already ingested)`` at exit 0 because content-hash dedup
short-circuits ahead of the mirror write — so the user was told everything was
fine and never learned the document was missing.

**Two fixes, tested separately here.** The collision oracle now consults the
database (prevention), and a row with no recorded mirror is treated as "mirror
still owed" so a re-run repairs it (recovery for orphans that already exist, and
for any future post-commit failure whatever its cause).

Note the oracle question this turned on: ``note_builder._unique_target`` checks
the FILESYSTEM, while the constraint that actually rejects is a DB unique index.
A row can own a ``vault_path`` whose file is missing — precisely the orphan state
this bug produced — so the filesystem was never the right authority.

All documents are synthetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from tests.conftest import FakeEmbedder

_SHARED_TITLE = "Synthetic duplicate title"


def _ingest(
    conn: psycopg.Connection[Any], *, body: str, vault: Path, path_name: str
) -> Any:
    """Ingest a FILE-shaped doc (``source_path`` set) sharing ``_SHARED_TITLE``.

    File-shaped rather than stdin-shaped on purpose: the dedup ladder keys
    file ingests on ``source_path``, so two files with the same title and
    different bodies are two distinct documents — which is exactly the
    collision this module is about. Stdin ingests would dedup on content hash
    and never reach the slug.
    """
    return ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=_SHARED_TITLE,
            content=body,
            content_type="markdown",
            source_path=f"/synthetic/{path_name}",
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault,
    )


def _vault_paths(conn: psycopg.Connection[Any]) -> list[str | None]:
    rows = conn.execute(
        "SELECT vault_path FROM documents ORDER BY vault_path NULLS LAST"
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# Prevention
# --------------------------------------------------------------------------


def test_second_document_with_the_same_title_ingests_cleanly(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """RED-FIRST: the second ingest used to raise UniqueViolation.

    Asserts the OUTCOME (both rows carry a distinct mirror path) rather than
    just "no exception" — an implementation that swallowed the error would
    satisfy the weaker assertion while still producing the orphan.
    """
    vault = tmp_path / "vault"

    first = _ingest(test_db, body="First body, alpha.\n", vault=vault, path_name="a.md")
    second = _ingest(test_db, body="Second body, beta.\n", vault=vault, path_name="b.md")

    assert first.document_id is not None
    assert second.document_id is not None
    assert first.document_id != second.document_id

    paths = _vault_paths(test_db)
    assert None not in paths, f"a document was left without a mirror: {paths}"
    assert len(set(paths)) == 2, f"both documents must get distinct paths: {paths}"


def test_collision_suffix_is_derived_from_the_document_id(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The suffix is a function of the immutable id, never a counter.

    A counter (``-2``, ``-3``) would reshuffle whenever documents are added or
    removed, renaming files in the user's vault on an unrelated ingest. Keying
    off ``documents.id`` makes the path stable for the life of the row.
    """
    vault = tmp_path / "vault"
    _ingest(test_db, body="First body, alpha.\n", vault=vault, path_name="a.md")
    second = _ingest(
        test_db, body="Second body, beta.\n", vault=vault, path_name="b.md"
    )

    assert second.document_id is not None
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (second.document_id,)
    ).fetchone()
    assert row is not None
    assert str(second.document_id)[:8] in str(row[0]), (
        f"the suffix must come from the document id, got {row[0]!r}"
    )


def test_three_documents_sharing_a_title_all_resolve(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Three collisions, three distinct paths, no orphans.

    Two is the minimum reproduction; three proves the oracle keeps seeing
    previously-assigned paths rather than resolving only the first conflict.
    """
    vault = tmp_path / "vault"
    for n, body in enumerate(("alpha", "beta", "gamma")):
        _ingest(
            test_db, body=f"Body {body}.\n", vault=vault, path_name=f"doc{n}.md"
        )

    paths = _vault_paths(test_db)
    assert len(paths) == 3
    assert None not in paths, f"orphan produced: {paths}"
    assert len(set(paths)) == 3, f"paths must be distinct: {paths}"


def test_files_exist_on_disk_for_every_document(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The recorded path must correspond to a real file.

    A ``vault_path`` pointing at nothing is the mirror-image of the orphan: the
    DB claims a mirror the user cannot open.
    """
    vault = tmp_path / "vault"
    _ingest(test_db, body="First body, alpha.\n", vault=vault, path_name="a.md")
    _ingest(test_db, body="Second body, beta.\n", vault=vault, path_name="b.md")

    for path in _vault_paths(test_db):
        assert path is not None
        assert (vault / str(path)).is_file(), f"missing mirror file for {path}"


# --------------------------------------------------------------------------
# Recovery — orphans that already exist
# --------------------------------------------------------------------------


def test_reingest_repairs_an_orphaned_row(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """REGRESSION: a re-run repairs a row whose mirror was never written.

    Simulates the pre-fix state directly — a committed row with
    ``vault_path IS NULL`` — because that is what any post-commit failure
    leaves behind, whatever caused it. Before this, dedup short-circuited ahead
    of the mirror write and the retry reported ``skipped`` at exit 0 while
    repairing nothing, so no amount of re-running ever helped.
    """
    vault = tmp_path / "vault"
    result = _ingest(
        test_db, body="First body, alpha.\n", vault=vault, path_name="a.md"
    )
    assert result.document_id is not None

    # Arrange the orphan state: row present, mirror not recorded.
    test_db.execute(
        "UPDATE documents SET vault_path = NULL WHERE id = %s", (result.document_id,)
    )
    assert _vault_paths(test_db) == [None]

    # Act — the identical ingest, which dedup will treat as "already ingested".
    again = _ingest(
        test_db, body="First body, alpha.\n", vault=vault, path_name="a.md"
    )

    # Assert the repair happened AND is reported, so a bare "skipped" cannot
    # understate a run that actually did work.
    assert again.mirror_repaired is True, (
        "the re-run repaired the mirror but did not report it; understating "
        "the work done is the same failure class as the silent success"
    )
    paths = _vault_paths(test_db)
    assert paths and paths[0] is not None, "the orphan must be repaired"
    assert (vault / str(paths[0])).is_file()


def test_normal_reingest_does_not_claim_a_repair(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Scope check: ``mirror_repaired`` is False when nothing was owed.

    Without this the flag could be set unconditionally and the repair test
    would still pass, making the signal meaningless.
    """
    vault = tmp_path / "vault"
    _ingest(test_db, body="First body, alpha.\n", vault=vault, path_name="a.md")

    again = _ingest(
        test_db, body="First body, alpha.\n", vault=vault, path_name="a.md"
    )

    assert again.mirror_repaired is False
