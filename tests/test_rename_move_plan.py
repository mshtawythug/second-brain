"""``plan_rename(new_folder=…)`` — the move half of the planner (F8).

``plan_rename`` computed its destination as ``old_relative.with_name(...)``,
so the parent directory was structurally fixed and a cross-folder move was
not expressible at any layer. F8 adds an optional ``new_folder`` parameter
rather than a parallel ``plan_move`` function, so the doc lookup, the
vault-tier check, the missing-file check, the collision check and the
reference scan stay in exactly one place.

These tests pin the folder normalization, the traversal guard, the
collision message, and the no-op-reference filter. The default
(``new_folder=None``) case is asserted first: today's rename behaviour must
stay byte-identical.

All fixture data is synthetic.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from brain import vault as vault_module
from brain.errors import BrainError, VaultPathEscape
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.rename import RenameError, plan_rename
from brain.vault.sync import sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    *,
    note_relative: str = "inbox/weekly-sync-platform.md",
    note_title: str = "Weekly Sync Platform",
    extra: dict[str, tuple[str, str]] | None = None,
) -> tuple[Path, dict[str, str]]:
    """Build a vault with one movable note (+ optional referrers). Returns ids."""
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(vault / note_relative, {"title": note_title}, "primary body\n")
    for relative, (title, body) in (extra or {}).items():
        _write(vault / relative, {"title": title}, body)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    rows = test_db.execute(
        "SELECT title, id::text FROM documents WHERE kind='vault'"
    ).fetchall()
    return vault, {str(t): str(i) for t, i in rows}


# ---------------------------------------------------------------------------
# Folder resolution
# ---------------------------------------------------------------------------


def test_new_folder_none_keeps_current_folder(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """Regression: the default is byte-identical to pre-F8 rename behaviour."""
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Renamed Sync",
    )

    assert op.new_path == vault / "inbox" / "renamed-sync.md"


def test_new_folder_sets_destination_relative_to_the_vault_root(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder="projects/atlas",
    )

    assert op.new_path == vault / "projects" / "atlas" / "weekly-sync-platform.md"
    assert op.old_path == vault / "inbox" / "weekly-sync-platform.md"


@pytest.mark.parametrize("folder", ["", ".", "   ", " . "])
def test_empty_or_dot_folder_targets_the_vault_root(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder, folder: str
) -> None:
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder=folder,
    )

    assert op.new_path == vault / "weekly-sync-platform.md"


@pytest.mark.parametrize(
    "folder", ["/projects/atlas/", "projects/atlas/", "  projects/atlas  "]
)
def test_leading_and_trailing_slashes_are_normalized(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder, folder: str
) -> None:
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder=folder,
    )

    assert op.new_path == vault / "projects" / "atlas" / "weekly-sync-platform.md"


# ---------------------------------------------------------------------------
# Vault-escape protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder", ["../..", "a/../../..", "../sibling", "inbox/../../escape"]
)
def test_traversal_folder_raises_vault_path_escape(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder, folder: str
) -> None:
    """The library layer is guarded too, not only the CLI edge."""
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    with pytest.raises(VaultPathEscape, match="must stay within the vault"):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=ids["Weekly Sync Platform"],
            new_title="Weekly Sync Platform",
            new_folder=folder,
        )


@pytest.mark.parametrize("folder", ["/etc", "/projects/atlas", "//tmp"])
def test_absolute_looking_folder_is_read_as_vault_root_relative(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder, folder: str
) -> None:
    """A leading slash means "from the vault root", never the filesystem root.

    Security-relevant: ``new_folder="/etc"`` must NOT resolve to ``/etc``.
    The documented normalization strips leading slashes (the same rule that
    makes ``"/projects/atlas/"`` a friendly spelling of ``"projects/atlas"``),
    so the destination lands inside the vault. It is therefore accepted
    rather than rejected — but it is contained, which is the property that
    matters.
    """
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder=folder,
    )

    assert op.new_path.resolve().is_relative_to(vault.resolve())
    assert op.new_path == vault / folder.lstrip("/") / "weekly-sync-platform.md"


def test_symlink_out_of_vault_raises_vault_path_escape(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """Both sides resolve, so a symlink escape hatch is rejected."""
    vault, ids = _seed(test_db, fake_embedder, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultPathEscape):
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=ids["Weekly Sync Platform"],
            new_title="Weekly Sync Platform",
            new_folder="escape",
        )


def test_vault_path_escape_is_a_brain_error() -> None:
    """The CLI wrapper and the MCP handler both catch it by base class."""
    assert issubclass(VaultPathEscape, BrainError)


def test_rename_error_is_a_brain_error() -> None:
    """Scout-Law re-parenting (F8).

    ``BrainError`` still derives from ``Exception``, so every pre-existing
    ``except RenameError`` site is unaffected.
    """
    assert issubclass(RenameError, BrainError)
    assert issubclass(RenameError, Exception)


# ---------------------------------------------------------------------------
# Collisions and no-ops
# ---------------------------------------------------------------------------


def test_collision_raises_rename_error_naming_the_path(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """No ``--force``, no overwrite — and the message says what to do."""
    vault, ids = _seed(
        test_db,
        fake_embedder,
        tmp_path,
        extra={
            "projects/atlas/weekly-sync-platform.md": (
                "Weekly Sync Platform Copy",
                "other body\n",
            )
        },
    )
    doc_id = ids["Weekly Sync Platform"]

    with pytest.raises(RenameError) as excinfo:
        plan_rename(
            test_db,
            vault_path=vault,
            document_id=doc_id,
            new_title="Weekly Sync Platform",
            new_folder="projects/atlas",
        )

    message = str(excinfo.value)
    assert "projects/atlas/weekly-sync-platform.md" in message
    assert "already exists" in message
    assert doc_id[:8] in message, "message must be actionable — quote the id"


def test_move_to_the_same_folder_is_a_noop_plan(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """The collision guard must not fire when the note is already there."""
    vault, ids = _seed(test_db, fake_embedder, tmp_path)

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder="inbox",
    )

    assert op.new_path.resolve() == op.old_path.resolve()


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------


def test_path_form_references_are_collected_for_a_move(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """The move must find the path-form refs the link rewriter produces."""
    vault, ids = _seed(
        test_db,
        fake_embedder,
        tmp_path,
        extra={
            "daily/2026-07-14.md": (
                "Daily Entry",
                "see [[Weekly Sync Platform]] for context\n",
            )
        },
    )

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder="projects/atlas",
    )

    assert [r.file_path.name for r in op.references] == ["2026-07-14.md"]
    assert op.references[0].old_text == (
        "[[inbox/weekly-sync-platform|Weekly Sync Platform]]"
    )
    assert op.references[0].new_text == "[[Weekly Sync Platform]]"


def test_identical_rewrites_are_filtered_out(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """The no-op filter: a bare same-title reference is not a change.

    Without it, a move would rewrite (and re-mtime, and re-trigger the
    watcher on) every file that merely mentions the note by title.
    """
    vault, ids = _seed(test_db, fake_embedder, tmp_path)
    # Written AFTER the sync so the link rewriter never converts it to
    # path-form: it stays bare, and a same-title move rewrites it to itself.
    _write(
        vault / "mentions.md",
        {"title": "Mentions"},
        "bare ref: [[Weekly Sync Platform]]\n",
    )

    op = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder="projects/atlas",
    )

    assert [r.file_path.name for r in op.references] == [], (
        "a rewrite identical to the original text is not a change and must "
        "not appear in the plan"
    )


def test_rename_reference_count_excludes_noops(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder
) -> None:
    """Behaviour-change regression for ``brain note rename``.

    Pre-F8, a reference whose rewrite equalled its original text was still
    counted, so ``rewrote N reference(s)`` overstated the work done. A
    same-title move rewrites both refs below to themselves; a real title
    change rewrites both for real.
    """
    vault, ids = _seed(test_db, fake_embedder, tmp_path)
    _write(
        vault / "mentions.md",
        {"title": "Mentions"},
        "no-op: [[Weekly Sync Platform]]\nreal: [[Weekly Sync Platform|alias]]\n",
    )

    noop_move = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Weekly Sync Platform",
        new_folder="projects/atlas",
    )
    real_rename = plan_rename(
        test_db,
        vault_path=vault,
        document_id=ids["Weekly Sync Platform"],
        new_title="Platform Sync Weekly",
    )

    assert len(noop_move.references) == 0
    assert len(real_rename.references) == 2
