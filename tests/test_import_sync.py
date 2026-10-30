"""``brain.ingest.import_sync`` — the prune pass behind ``brain ingest-dir --sync`` (RM-37).

A prune DELETES documents, so the refusals and the scope rules are the point of the module and
of these tests: an unmounted or mistyped directory, a directory inside the brain vault, a
vault-tier row, a document whose file merely cannot be checked, a sibling directory that shares a
string prefix, and a mass deletion must each leave every row alone.

Every tree is built under ``tmp_path``; every row lives in the 5434 test database. All names
and contents are synthetic.
"""
from __future__ import annotations

import errno
import os
import unicodedata
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path, PurePath
from typing import Any

import psycopg
import pytest

from brain.ingest import ingest_document
from brain.ingest.dispatch import extract_path, supported_extensions
from brain.ingest.import_sync import (
    MASS_DELETE_MIN,
    FsOps,
    ImportSyncRefused,
    PrunePlan,
    apply_prune,
    is_under,
    plan_prune,
    refusal_for,
    resolve_sync_root,
)
from tests.conftest import FakeEmbedder

NOTE_BODY = "# Synthetic note\n\nA paragraph about an imaginary planning offsite.\n"
WALK_EXTENSIONS = frozenset(supported_extensions(directory_walk=True))


class RecordingGraphSyncer:
    """Injected through ``apply_prune(graph_syncer=…)``; records each removal."""

    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self.removed.append(document_id)


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(import_root, vault_root)``, siblings under ``tmp_path``."""
    root = tmp_path / "notes"
    root.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    return root, vault


@pytest.fixture
def ingest_file(
    test_db: psycopg.Connection[Any], fake_embedder: FakeEmbedder
) -> Callable[..., str]:
    """Write ``path``, ingest it as ``ingest-dir`` would, return the document id."""

    def _ingest(
        path: Path, *, vault_root: Path, body: str = NOTE_BODY, source_kind: str = "manual"
    ) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{body}\n{path.name}\n", encoding="utf-8")
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=extract_path(path),
            source_kind=source_kind,
            vault_root=vault_root,
            enrich=False,
        )
        assert result.document_id is not None
        return result.document_id

    return _ingest


def _row_exists(conn: psycopg.Connection[Any], document_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM documents WHERE id=%s", (document_id,)).fetchone()
    return row is not None


def _vault_path(conn: psycopg.Connection[Any], document_id: str) -> str:
    row = conn.execute("SELECT vault_path FROM documents WHERE id=%s", (document_id,)).fetchone()
    assert row is not None and row[0] is not None
    return str(row[0])


def _plan(
    conn: psycopg.Connection[Any], root: Path, vault: Path, **kwargs: Any
) -> PrunePlan:
    return plan_prune(
        conn,
        root=resolve_sync_root(root, vault_root=vault),
        vault_root=vault,
        extensions=kwargs.pop("extensions", WALK_EXTENSIONS),
        **kwargs,
    )


def _ids(plan: PrunePlan) -> set[str]:
    return {c.document_id for c in plan.candidates}


@pytest.fixture
def case_insensitive(tmp_path: Path) -> None:
    probe = tmp_path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("tmp_path is on a case-sensitive file system")


# ---------------------------------------------------------------------------
# Refusal R1a / R2 — resolve_sync_root
# ---------------------------------------------------------------------------


def test_root_refuses_a_missing_directory(layout: tuple[Path, Path]) -> None:
    root, vault = layout

    with pytest.raises(ImportSyncRefused, match="does not exist"):
        resolve_sync_root(root / "unmounted", vault_root=vault)


def test_root_refuses_a_file(layout: tuple[Path, Path]) -> None:
    root, vault = layout
    file_path = root / "plain.md"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ImportSyncRefused, match="not a directory"):
        resolve_sync_root(file_path, vault_root=vault)


def test_root_refuses_the_vault_itself(layout: tuple[Path, Path]) -> None:
    _, vault = layout

    with pytest.raises(ImportSyncRefused, match="brain vault"):
        resolve_sync_root(vault, vault_root=vault)


def test_root_refuses_a_directory_inside_the_vault(layout: tuple[Path, Path]) -> None:
    _, vault = layout
    inner = vault / "projects" / "imported"
    inner.mkdir(parents=True)

    with pytest.raises(ImportSyncRefused, match="brain vault"):
        resolve_sync_root(inner, vault_root=vault)


def test_root_refuses_a_symlink_that_points_into_the_vault(
    tmp_path: Path, layout: tuple[Path, Path]
) -> None:
    _, vault = layout
    (vault / "inner").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(vault / "inner")

    with pytest.raises(ImportSyncRefused, match="brain vault"):
        resolve_sync_root(alias, vault_root=vault)


def test_root_refuses_through_a_symlinked_vault_root(
    tmp_path: Path, layout: tuple[Path, Path]
) -> None:
    _, vault = layout
    (vault / "inner").mkdir()
    vault_alias = tmp_path / "vault-alias"
    vault_alias.symlink_to(vault)

    with pytest.raises(ImportSyncRefused, match="brain vault"):
        resolve_sync_root(vault / "inner", vault_root=vault_alias)


@pytest.mark.usefixtures("case_insensitive")
def test_root_refuses_a_case_variant_of_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    (vault / "inner").mkdir(parents=True)

    with pytest.raises(ImportSyncRefused, match="brain vault"):
        resolve_sync_root(tmp_path / "vault" / "inner", vault_root=vault)


def test_root_accepts_a_sibling_that_shares_the_vault_prefix(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    sibling = tmp_path / "vault-notes"
    sibling.mkdir()

    assert resolve_sync_root(sibling, vault_root=vault) == sibling.resolve()


def test_root_accepts_a_parent_of_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    assert resolve_sync_root(tmp_path, vault_root=vault) == tmp_path.resolve()


def test_root_accepts_a_missing_vault(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()

    assert resolve_sync_root(root, vault_root=tmp_path / "no-vault") == root.resolve()


def test_root_is_resolved_through_symlinks_and_trailing_slashes(
    tmp_path: Path, layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    alias = tmp_path / "notes-link"
    alias.symlink_to(root)

    assert resolve_sync_root(Path(f"{alias}/"), vault_root=vault) == root.resolve()


# ---------------------------------------------------------------------------
# Containment — path components, never a string prefix
# ---------------------------------------------------------------------------


def _never_samefile(a: Any, b: Any) -> bool:
    raise AssertionError("samefile must not be consulted")


def test_is_under_matches_a_file_below_the_root() -> None:
    assert is_under("/srv/notes/a/b.md", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_a_sibling_sharing_the_prefix() -> None:
    assert not is_under("/srv/notes-old/b.md", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_the_root_itself() -> None:
    assert not is_under("/srv/notes", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_a_relative_path() -> None:
    assert not is_under("srv/notes/b.md", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_a_relative_path_even_against_a_relative_root() -> None:
    assert not is_under("srv/notes/b.md", Path("srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_a_parent_reference() -> None:
    assert not is_under("/srv/notes/../secret/b.md", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_rejects_an_unrelated_path_without_samefile() -> None:
    assert not is_under("/srv/other/b.md", Path("/srv/notes"), samefile=_never_samefile)


def test_is_under_accepts_a_case_variant_only_when_samefile_agrees() -> None:
    calls: list[tuple[str, str]] = []

    def same(a: Any, b: Any) -> bool:
        calls.append((str(a), str(b)))
        return True

    assert is_under("/srv/Notes/b.md", Path("/srv/notes"), samefile=same)
    assert calls == [("/srv/Notes", "/srv/notes")]


def test_is_under_rejects_a_case_variant_on_a_case_sensitive_volume() -> None:
    assert not is_under("/srv/Notes/b.md", Path("/srv/notes"), samefile=lambda a, b: False)


def test_is_under_rejects_a_case_variant_whose_prefix_cannot_be_checked() -> None:
    def unreachable(a: Any, b: Any) -> bool:
        raise FileNotFoundError(errno.ENOENT, "gone")

    assert not is_under("/srv/Notes/b.md", Path("/srv/notes"), samefile=unreachable)


def test_is_under_folds_unicode_normalization_before_samefile() -> None:
    nfd = unicodedata.normalize("NFD", "/srv/Café/b.md")
    nfc_root = Path(unicodedata.normalize("NFC", "/srv/Café"))

    assert is_under(nfd, nfc_root, samefile=lambda a, b: True)
    assert not is_under(nfd, nfc_root, samefile=lambda a, b: False)


# ---------------------------------------------------------------------------
# plan_prune — read-only scope and the "gone" test
# ---------------------------------------------------------------------------


def test_plan_lists_a_deleted_file_and_keeps_a_present_one(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    gone = ingest_file(root / "sub" / "gone.md", vault_root=vault)
    kept = ingest_file(root / "kept.md", vault_root=vault)
    (root / "sub" / "gone.md").unlink()
    (root / "sub").rmdir()  # the folder deleted outright: gone entirely, so it prunes

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {gone}
    assert plan.in_scope == 2
    assert plan.unverifiable == 0
    [candidate] = plan.candidates
    assert candidate.relative_path == "sub/gone.md"
    assert _row_exists(test_db, gone) and _row_exists(test_db, kept)


def test_plan_writes_nothing(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    gone = ingest_file(root / "gone.md", vault_root=vault)
    mirror = vault / _vault_path(test_db, gone)
    (root / "gone.md").unlink()
    before = test_db.execute("SELECT count(*) FROM documents").fetchone()

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {gone}
    assert test_db.execute("SELECT count(*) FROM documents").fetchone() == before
    assert mirror.exists()


def _make_vault_tier(conn: psycopg.Connection[Any], document_id: str) -> None:
    """Flip one ingested row to the vault tier, keeping its manual source and source_path.

    No ingest path writes a vault-tier row with a ``source_path``; this builds the worst case,
    a row that matches every other filter, so only ``documents.kind`` can exclude it.
    """
    conn.execute("UPDATE documents SET kind='vault' WHERE id=%s", (document_id,))


def test_plan_never_reads_a_vault_tier_row(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    owned = ingest_file(root / "vault-owned.md", vault_root=vault)
    _make_vault_tier(test_db, owned)
    (root / "vault-owned.md").unlink()

    plan = _plan(test_db, root, vault)

    assert plan.candidates == ()
    assert plan.in_scope == 0
    assert _row_exists(test_db, owned)


def test_plan_never_reads_a_vault_tier_row_with_no_source(
    test_db: psycopg.Connection[Any], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    row = test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, source_path, content_hash) "
        "VALUES ('Vault note', 'body', 'note', 'vault', %s, %s) RETURNING id::text",
        (str(root / "sourceless.md"), sha256(b"vault-tier-row").hexdigest()),
    ).fetchone()
    assert row is not None

    plan = _plan(test_db, root, vault)

    assert plan.in_scope == 0


def test_plan_ignores_stdin_documents_without_a_source_path(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    seed_doc(title="Pasted snippet")

    plan = _plan(test_db, root, vault)

    assert plan.in_scope == 0


def test_plan_ignores_a_non_manual_source_kind(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    ingest_file(root / "shot.md", vault_root=vault, source_kind="screenshot")
    (root / "shot.md").unlink()

    plan = _plan(test_db, root, vault)

    assert plan.in_scope == 0


def test_plan_ignores_a_sibling_directory_sharing_the_prefix(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    tmp_path: Path,
    layout: tuple[Path, Path],
) -> None:
    root, vault = layout
    ingest_file(tmp_path / "notes-old" / "old.md", vault_root=vault)
    (tmp_path / "notes-old" / "old.md").unlink()

    plan = _plan(test_db, root, vault)

    assert plan.in_scope == 0


def test_plan_keeps_rows_outside_this_runs_extensions(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    ingest_file(root / "gone.md", vault_root=vault)
    (root / "gone.md").unlink()

    plan = _plan(test_db, root, vault, extensions=frozenset({".pdf"}))

    assert plan.in_scope == 0


def test_plan_matches_extensions_case_insensitively(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    gone = ingest_file(root / "LOUD.MD", vault_root=vault)
    (root / "LOUD.MD").unlink()

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {gone}


def test_plan_skips_and_counts_rows_under_a_nested_vault(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], tmp_path: Path
) -> None:
    root = tmp_path / "home"
    vault = root / "vault"
    vault.mkdir(parents=True)
    other = tmp_path / "elsewhere-vault"
    ingest_file(vault / "inside.md", vault_root=other)
    (vault / "inside.md").unlink()
    gone = ingest_file(root / "outside.md", vault_root=other)
    (root / "outside.md").unlink()

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {gone}
    assert plan.under_vault == 1
    assert plan.in_scope == 1


def test_plan_follows_a_symlinked_root(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    tmp_path: Path,
    layout: tuple[Path, Path],
) -> None:
    root, vault = layout
    gone = ingest_file(root / "gone.md", vault_root=vault)
    (root / "gone.md").unlink()
    alias = tmp_path / "alias"
    alias.symlink_to(root)

    plan = _plan(test_db, alias, vault)

    assert _ids(plan) == {gone}


@pytest.mark.usefixtures("case_insensitive")
def test_plan_matches_a_case_variant_root_on_a_case_insensitive_volume(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    gone = ingest_file(tmp_path / "Notes" / "gone.md", vault_root=vault)
    (tmp_path / "Notes" / "gone.md").unlink()

    plan = _plan(test_db, tmp_path / "notes", vault)

    assert _ids(plan) == {gone}


def test_plan_matches_a_differently_normalized_root(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "Café")
    gone = ingest_file(nfd_dir / "gone.md", vault_root=vault)
    nfc_dir = tmp_path / unicodedata.normalize("NFC", "Café")
    if not nfc_dir.exists():
        pytest.skip("tmp_path's file system does not fold Unicode normalization")
    (nfd_dir / "gone.md").unlink()

    plan = _plan(test_db, nfc_dir, vault)

    assert _ids(plan) == {gone}


@pytest.fixture
def unreadable_dir(tmp_path: Path) -> Iterator[Path]:
    locked = tmp_path / "notes" / "locked"
    locked.mkdir(parents=True)
    yield locked
    locked.chmod(0o755)


def test_plan_keeps_a_file_it_cannot_check(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    unreadable_dir: Path,
    tmp_path: Path,
) -> None:
    root, vault = tmp_path / "notes", tmp_path / "vault"
    kept = ingest_file(unreadable_dir / "private.md", vault_root=vault)
    unreadable_dir.chmod(0)
    if os.access(unreadable_dir, os.X_OK):
        pytest.skip("running with privileges that ignore directory permissions")

    plan = _plan(test_db, root, vault)

    assert plan.candidates == ()
    assert plan.unverifiable == 1
    assert plan.in_scope == 1
    assert _row_exists(test_db, kept)


@pytest.mark.parametrize("code", [errno.EIO, errno.ELOOP, errno.ETIMEDOUT, errno.EACCES])
def test_plan_keeps_a_file_whose_check_fails_otherwise(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    layout: tuple[Path, Path],
    code: int,
) -> None:
    root, vault = layout
    ingest_file(root / "flaky.md", vault_root=vault)

    def failing_lstat(path: Any) -> os.stat_result:
        raise OSError(code, os.strerror(code))

    plan = _plan(test_db, root, vault, fs=FsOps(lstat=failing_lstat))

    assert plan.candidates == ()
    assert plan.unverifiable == 1


def test_plan_treats_a_replaced_parent_as_gone(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    gone = ingest_file(root / "folder" / "gone.md", vault_root=vault)
    (root / "folder" / "gone.md").unlink()
    (root / "folder").rmdir()
    (root / "folder").write_text("now a file", encoding="utf-8")

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {gone}


def test_plan_keeps_a_dangling_symlink_at_the_stored_path(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    kept = ingest_file(root / "linked.md", vault_root=vault)
    (root / "linked.md").unlink()
    (root / "linked.md").symlink_to(root / "nowhere.md")

    plan = _plan(test_db, root, vault)

    assert kept not in _ids(plan)
    assert plan.in_scope == 1


# ---------------------------------------------------------------------------
# refusal_for — R1b and the mass-deletion guard
# ---------------------------------------------------------------------------


def _counts_plan(*, candidates: int, in_scope: int) -> PrunePlan:
    from brain.ingest.import_sync import PruneCandidate

    return PrunePlan(
        root=Path("/srv/notes"),
        in_scope=in_scope,
        candidates=tuple(
            PruneCandidate(
                document_id=f"00000000-0000-0000-0000-{i:012d}",
                source_path=f"/srv/notes/{i}.md",
                relative_path=f"{i}.md",
            )
            for i in range(candidates)
        ),
        unverifiable=0,
        under_vault=0,
    )


def test_refusal_r1b_when_the_walk_is_empty_but_rows_exist() -> None:
    plan = _counts_plan(candidates=1, in_scope=1)

    reason = refusal_for(plan, supported_files=0, apply=False, allow_empty=False)

    assert reason is not None and "--allow-empty" in reason and "1 document" in reason


def test_refusal_r1b_is_overridden_by_allow_empty() -> None:
    plan = _counts_plan(candidates=3, in_scope=3)

    assert refusal_for(plan, supported_files=0, apply=True, allow_empty=True) is None


def test_refusal_r1b_does_not_fire_without_rows() -> None:
    plan = _counts_plan(candidates=0, in_scope=0)

    assert refusal_for(plan, supported_files=0, apply=True, allow_empty=False) is None


def test_refusal_r1b_does_not_fire_when_the_walk_found_files() -> None:
    plan = _counts_plan(candidates=1, in_scope=4)

    assert refusal_for(plan, supported_files=3, apply=True, allow_empty=False) is None


@pytest.mark.parametrize(
    ("candidates", "in_scope", "refused"),
    [
        (MASS_DELETE_MIN + 1, 2 * MASS_DELETE_MIN + 1, True),  # 6 of 11: more than half
        (MASS_DELETE_MIN + 1, 2 * MASS_DELETE_MIN + 2, False),  # 6 of 12: exactly half
        (MASS_DELETE_MIN + 1, MASS_DELETE_MIN + 1, True),  # 6 of 6
        (MASS_DELETE_MIN, MASS_DELETE_MIN, False),  # 5 of 5: not more than 5
        (MASS_DELETE_MIN, MASS_DELETE_MIN + 1, False),  # 5 of 6
    ],
)
def test_mass_delete_guard_thresholds(candidates: int, in_scope: int, refused: bool) -> None:
    plan = _counts_plan(candidates=candidates, in_scope=in_scope)

    reason = refusal_for(plan, supported_files=1, apply=True, allow_empty=False)

    assert (reason is not None) is refused
    if refused:
        assert reason is not None
        assert f"{candidates} of {in_scope}" in reason and "--allow-empty" in reason


def test_mass_delete_guard_only_guards_apply() -> None:
    plan = _counts_plan(candidates=9, in_scope=9)

    assert refusal_for(plan, supported_files=1, apply=False, allow_empty=False) is None


def test_mass_delete_guard_is_overridden_by_allow_empty() -> None:
    plan = _counts_plan(candidates=9, in_scope=9)

    assert refusal_for(plan, supported_files=1, apply=True, allow_empty=True) is None


def test_mass_delete_minimum_is_five() -> None:
    assert MASS_DELETE_MIN == 5


# ---------------------------------------------------------------------------
# apply_prune
# ---------------------------------------------------------------------------


def test_apply_deletes_the_row_its_mirror_and_its_graph_entry(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    gone = ingest_file(root / "gone.md", vault_root=vault)
    kept = ingest_file(root / "kept.md", vault_root=vault)
    mirror = vault / _vault_path(test_db, gone)
    assert mirror.exists()
    (root / "gone.md").unlink()
    plan = _plan(test_db, root, vault)
    graph = RecordingGraphSyncer()

    report = apply_prune(test_db, plan, vault_root=vault, graph_syncer=graph)

    assert [(o.candidate.document_id, o.action) for o in report.outcomes] == [(gone, "deleted")]
    assert report.deleted == 1 and report.failed == 0
    assert not _row_exists(test_db, gone)
    assert _row_exists(test_db, kept)
    assert not mirror.exists()
    assert graph.removed == [gone]


def test_apply_keeps_a_file_restored_after_the_plan(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    back = ingest_file(root / "back.md", vault_root=vault)
    (root / "back.md").unlink()
    plan = _plan(test_db, root, vault)
    (root / "back.md").write_text("restored", encoding="utf-8")

    report = apply_prune(test_db, plan, vault_root=vault, graph_syncer=None)

    assert [o.action for o in report.outcomes] == ["kept"]
    assert report.deleted == 0
    assert _row_exists(test_db, back)


def test_apply_keeps_a_file_it_can_no_longer_check(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = ingest_file(root / "flaky.md", vault_root=vault)
    (root / "flaky.md").unlink()
    plan = _plan(test_db, root, vault)

    def failing_lstat(path: Any) -> os.stat_result:
        raise OSError(errno.EIO, "io")

    report = apply_prune(
        test_db, plan, vault_root=vault, graph_syncer=None, fs=FsOps(lstat=failing_lstat)
    )

    assert [o.action for o in report.outcomes] == ["unchecked"]
    assert _row_exists(test_db, doc)


def test_apply_reports_a_row_already_deleted(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = ingest_file(root / "gone.md", vault_root=vault)
    (root / "gone.md").unlink()
    plan = _plan(test_db, root, vault)
    test_db.execute("DELETE FROM documents WHERE id=%s", (doc,))

    report = apply_prune(test_db, plan, vault_root=vault, graph_syncer=None)

    assert [o.action for o in report.outcomes] == ["missing"]
    assert report.failed == 0


@pytest.mark.parametrize(
    "error", [psycopg.OperationalError("synthetic detail"), PermissionError(errno.EACCES, "no")]
)
def test_apply_reports_a_failed_delete_by_class_only(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    layout: tuple[Path, Path],
    error: Exception,
) -> None:
    root, vault = layout
    ingest_file(root / "a.md", vault_root=vault)
    ingest_file(root / "b.md", vault_root=vault)
    (root / "a.md").unlink()
    (root / "b.md").unlink()
    plan = _plan(test_db, root, vault)
    attempts: list[str] = []

    def failing_delete(conn: Any, *, document_id: str, vault_root: Path, graph_syncer: Any) -> Any:
        attempts.append(document_id)
        raise error

    report = apply_prune(
        test_db, plan, vault_root=vault, graph_syncer=None, delete=failing_delete
    )

    assert len(attempts) == 2  # one failure does not stop the run
    assert [o.action for o in report.outcomes] == ["failed", "failed"]
    assert report.failed == 2
    detail = report.outcomes[0].detail
    assert detail is not None
    assert type(error).__name__ in detail and "synthetic detail" not in detail


def test_candidates_are_ordered_by_path(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    for name in ("c.md", "a.md", "b.md"):
        ingest_file(root / name, vault_root=vault)
        (root / name).unlink()

    plan = _plan(test_db, root, vault)

    assert [c.relative_path for c in plan.candidates] == ["a.md", "b.md", "c.md"]
    assert all(PurePath(c.source_path).is_absolute() for c in plan.candidates)


# ---------------------------------------------------------------------------
# A missing file on a missing volume is kept (review round, M1)
# ---------------------------------------------------------------------------


def _resolved(path: Path) -> str:
    return str(path.resolve())


def _mounts(*paths: Path) -> Callable[[str], bool]:
    mounted = {_resolved(p) for p in paths}
    return lambda candidate: candidate in mounted


def _with_dev(st: os.stat_result, dev: int) -> os.stat_result:
    fields = list(st[:10])
    fields[2] = dev
    return os.stat_result(fields)


def _gone_in(
    ingest_file: Callable[..., str], directory: Path, vault: Path, *, keep_sibling: bool
) -> str:
    """Ingest ``directory/x.md``, delete it, and keep or remove ``directory`` itself."""
    doc = ingest_file(directory / "x.md", vault_root=vault)
    if keep_sibling:
        (directory / "keep.txt").write_text("still here", encoding="utf-8")
    (directory / "x.md").unlink()
    return doc


def test_arm_a_keeps_a_file_whose_nearest_ancestor_is_a_mount_point(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = _gone_in(ingest_file, root / "mnt", vault, keep_sibling=True)

    mounted = _plan(test_db, root, vault, fs=FsOps(ismount=_mounts(root / "mnt")))
    plain = _plan(test_db, root, vault)

    assert mounted.candidates == () and mounted.unverifiable == 1
    assert _ids(plain) == {doc}


def test_arm_a_keeps_a_file_below_a_mount_point_between_it_and_the_root(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    deep = root / "mnt" / "deep"
    ingest_file(deep / "sub" / "x.md", vault_root=vault)
    (deep / "keep.txt").write_text("still here", encoding="utf-8")
    (deep / "sub" / "x.md").unlink()
    (deep / "sub").rmdir()

    plan = _plan(test_db, root, vault, fs=FsOps(ismount=_mounts(root / "mnt")))

    assert plan.candidates == () and plan.unverifiable == 1


def test_arm_a_ignores_the_root_being_a_mount_point(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = _gone_in(ingest_file, root / "sub", vault, keep_sibling=True)

    plan = _plan(test_db, root, vault, fs=FsOps(ismount=_mounts(root)))

    assert _ids(plan) == {doc}


def test_arm_b_keeps_a_file_whose_nearest_ancestor_is_on_another_device(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    other = root / "other"
    _gone_in(ingest_file, other, vault, keep_sibling=True)
    other_key = _resolved(other)

    def lstat(path: str) -> os.stat_result:
        st = os.lstat(path)
        return _with_dev(st, st.st_dev + 1) if path == other_key else st

    plan = _plan(test_db, root, vault, fs=FsOps(lstat=lstat))

    assert plan.candidates == () and plan.unverifiable == 1


def test_arm_c_keeps_a_file_whose_folder_is_left_empty(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = _gone_in(ingest_file, root / "nas", vault, keep_sibling=False)

    empty = _plan(test_db, root, vault)
    (root / "nas").rmdir()
    removed = _plan(test_db, root, vault)

    assert empty.candidates == () and empty.unverifiable == 1
    assert _ids(removed) == {doc}


def test_arm_c_does_not_apply_to_an_empty_root(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    # An empty root is refusal R1b's case, which --allow-empty overrides on purpose.
    root, vault = layout
    doc = ingest_file(root / "only.md", vault_root=vault)
    (root / "only.md").unlink()

    plan = _plan(test_db, root, vault)

    assert _ids(plan) == {doc}


def test_arm_d_keeps_a_file_whose_ancestor_holds_a_mount_point(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    # `/Volumes` with one volume unplugged: vol-a vanished, vol-b is still mounted.
    root, vault = layout
    doc = ingest_file(root / "vol-a" / "x.md", vault_root=vault)
    (root / "vol-a" / "x.md").unlink()
    (root / "vol-a").rmdir()
    (root / "vol-b").mkdir()

    mounted = _plan(test_db, root, vault, fs=FsOps(ismount=_mounts(root / "vol-b")))
    plain = _plan(test_db, root, vault)

    assert mounted.candidates == () and mounted.unverifiable == 1
    assert _ids(plain) == {doc}


def test_an_ancestor_that_cannot_be_checked_keeps_the_file(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    sub = root / "sub"
    _gone_in(ingest_file, sub, vault, keep_sibling=True)
    sub_key = _resolved(sub)

    def lstat(path: str) -> os.stat_result:
        if path == sub_key:
            raise OSError(errno.EIO, "io")
        return os.lstat(path)

    plan = _plan(test_db, root, vault, fs=FsOps(lstat=lstat))

    assert plan.candidates == () and plan.unverifiable == 1


def test_a_root_that_cannot_be_checked_keeps_the_file(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    _gone_in(ingest_file, root / "sub", vault, keep_sibling=True)
    root_key = _resolved(root)

    def lstat(path: str) -> os.stat_result:
        if path == root_key:
            raise OSError(errno.EIO, "io")
        return os.lstat(path)

    plan = _plan(test_db, root, vault, fs=FsOps(lstat=lstat))

    assert plan.candidates == () and plan.unverifiable == 1


def test_a_root_gone_mid_run_keeps_the_file(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    ingest_file(root / "x.md", vault_root=vault)

    def nothing_exists(path: str) -> os.stat_result:
        raise FileNotFoundError(errno.ENOENT, "gone")

    plan = _plan(test_db, root, vault, fs=FsOps(lstat=nothing_exists))

    assert plan.candidates == () and plan.unverifiable == 1


def test_an_ancestor_that_cannot_be_listed_keeps_the_file(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    _gone_in(ingest_file, root / "sub", vault, keep_sibling=True)

    def listdir(path: str) -> list[str]:
        raise PermissionError(errno.EACCES, "no")

    plan = _plan(test_db, root, vault, fs=FsOps(listdir=listdir))

    assert plan.candidates == () and plan.unverifiable == 1


def test_apply_rechecks_the_volume(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    doc = _gone_in(ingest_file, root / "mnt", vault, keep_sibling=True)
    plan = _plan(test_db, root, vault)

    report = apply_prune(
        test_db,
        plan,
        vault_root=vault,
        graph_syncer=None,
        fs=FsOps(ismount=_mounts(root / "mnt")),
    )

    assert [o.action for o in report.outcomes] == ["unchecked"]
    assert _row_exists(test_db, doc)


# ---------------------------------------------------------------------------
# apply_prune re-reads the row before deleting it (review round, L2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE documents SET source_path = source_path || '.moved' WHERE id=%s",
        "UPDATE documents SET kind = 'vault' WHERE id=%s",
        "UPDATE sources SET kind = 'notion' "
        "WHERE id = (SELECT source_id FROM documents WHERE id=%s)",
    ],
)
def test_apply_skips_a_row_that_changed_since_the_plan(
    test_db: psycopg.Connection[Any],
    ingest_file: Callable[..., str],
    layout: tuple[Path, Path],
    change: str,
) -> None:
    root, vault = layout
    doc = ingest_file(root / "gone.md", vault_root=vault)
    (root / "gone.md").unlink()
    plan = _plan(test_db, root, vault)
    test_db.execute(change, (doc,))

    report = apply_prune(test_db, plan, vault_root=vault, graph_syncer=None)

    assert [o.action for o in report.outcomes] == ["changed"]
    assert _row_exists(test_db, doc)


def test_apply_reports_a_row_deleted_during_the_delete(
    test_db: psycopg.Connection[Any], ingest_file: Callable[..., str], layout: tuple[Path, Path]
) -> None:
    root, vault = layout
    ingest_file(root / "gone.md", vault_root=vault)
    (root / "gone.md").unlink()
    plan = _plan(test_db, root, vault)

    def raced_delete(conn: Any, *, document_id: str, vault_root: Path, graph_syncer: Any) -> Any:
        raise ValueError(f"document not found: {document_id}")

    report = apply_prune(test_db, plan, vault_root=vault, graph_syncer=None, delete=raced_delete)

    assert [o.action for o in report.outcomes] == ["missing"]
