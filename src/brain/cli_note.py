"""`brain note` — vault-note authoring sub-app (``new`` / ``rename`` / ``move``).

Extracted verbatim from :mod:`brain.cli` (which had grown past the 800-line
ceiling in CLAUDE.md). Behaviour is unchanged — the sub-app name, command
names, flags, help text, output and exit codes are identical to the previous
in-``cli.py`` definitions.

Only the two ``note`` subcommands moved. ``brain daily`` stays in ``cli.py``
and still shares the authoring helpers (``_resolve_vault``,
``_assert_within_vault``, ``_ensure_template``,
``_run_post_write_editor_and_sync``), so those helpers stay there too and are
resolved here through the ``brain.cli`` module object *at call time*: ``cli.py``
imports this module to register the sub-app, so a module-level import back
would be a cycle. Reading the attribute at call time additionally keeps
``monkeypatch.setattr("brain.cli.<name>", ...)`` — the patch point the existing
test suite uses — effective for these commands. Same pattern as
:mod:`brain._capture_command`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import typer

from .config import Config
from .db import connect
from .errors import VaultNoteSyncError, VaultPathEscape
from .ingest import Embedder
from .vault.note_builder import create_vault_note
from .vault.paths import assert_within_vault
from .vault.rename import RenameError, RenameOp, apply_rename, plan_rename
from .vault.slug import slugify
from .vault.sync import SyncReport

note_app = typer.Typer(
    name="note",
    help="Authoring commands for vault notes.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Delegation to `brain.cli`-owned helpers.
#
# These names stay in `cli.py` because `brain daily` / `brain elicit` (which
# did NOT move) still call them, and because the test suite patches
# `brain.cli._build_embedder`. Each wrapper resolves the attribute at call
# time, so the moved command bodies below are byte-identical to their
# pre-move form.
# ---------------------------------------------------------------------------


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _resolve_vault(override: Path | None, cfg: Config) -> Path:
    """Resolve the effective vault path via the shared ``brain.cli`` helper."""
    from . import cli as _cli

    return _cli._resolve_vault(override, cfg)


def _assert_within_vault(target: Path, vault_path: Path, *, label: str) -> None:
    """Typer-flavoured wrapper over :func:`brain.vault.paths.assert_within_vault`.

    The check itself is the pure function (so library callers — ``plan_rename``,
    the MCP server, ``brain ui`` — share one implementation); this wrapper only
    translates :class:`VaultPathEscape` into the usage error the CLI has always
    raised, message and exit code unchanged.
    """
    try:
        assert_within_vault(target, vault_path)
    except VaultPathEscape as e:
        raise typer.BadParameter(
            f"{label} must stay within the vault; "
            f"got a path that resolves outside {vault_path}"
        ) from e


def _ensure_template(vault_path: Path, name: str) -> Path:
    """Resolve a ``_templates/<name>.md`` path via the shared ``brain.cli`` helper."""
    from . import cli as _cli

    return _cli._ensure_template(vault_path, name)


def _run_post_write_editor_and_sync(
    cfg: Config, *, vault_path: Path, file_path: Path
) -> SyncReport | None:
    """Open ``$EDITOR`` then re-sync via the shared ``brain.cli`` helper."""
    from . import cli as _cli

    return _cli._run_post_write_editor_and_sync(
        cfg, vault_path=vault_path, file_path=file_path
    )


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a document id prefix via the shared ``brain.cli`` helper."""
    from . import cli as _cli

    return _cli._resolve_id(conn, prefix)


@note_app.command("new")
def note_new(
    title: str = typer.Argument(..., help="Note title (used for frontmatter + slug)."),
    folder: str = typer.Option(
        "",
        "--folder",
        "-f",
        help="Subdirectory under the vault root (default: vault root).",
    ),
    template: str = typer.Option(
        "note",
        "--template",
        "-T",
        help="Template name in _templates/ (default: 'note').",
    ),
    tag: list[str] = typer.Option(
        [], "--tag", "-t", help="Initial tag(s) for the note."
    ),
    no_edit: bool = typer.Option(
        False, "--no-edit", help="Skip launching $EDITOR after the file is written."
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Create a new vault note from a template.

    Resolves ``<vault>/<folder>/<slug(title)>.md``. Errors if the file
    already exists (use ``brain edit <prefix>`` to modify an existing note).
    Renders ``_templates/<template>.md`` with ``{{title}}`` / ``{{date}}`` /
    ``{{datetime}}`` / ``{{slug}}`` substitutions, forces the
    brain-canonical frontmatter (id, title, created, updated, kind, tags),
    writes the file, runs a single-file sync, and (unless ``--no-edit``)
    opens ``$EDITOR`` then re-syncs on exit.
    """
    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)
    # Validate the template up front so a bad ``--template`` / missing
    # ``_templates/`` surfaces a friendly CLI error (``create_vault_note``
    # re-resolves it internally for the actual render).
    _ensure_template(vault_path, template)

    slug = slugify(title)
    target_relative = Path(folder) / f"{slug}.md" if folder else Path(f"{slug}.md")
    target = vault_path / target_relative
    # Guard against ``--folder ../../etc`` and similar — we'd otherwise
    # write outside the vault BEFORE sync ever runs and noticed.
    _assert_within_vault(target, vault_path, label="--folder")
    if target.exists():
        typer.secho(
            f"note already exists at {target_relative.as_posix()}; "
            f"use `brain edit <prefix>` to modify it",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    # Build the embedder via the CLI factory (the patch point tests wire onto
    # ``brain.cli._build_embedder``) and hand it to the shared helper so the
    # session loop and ``brain note new`` share one create+sync path.
    embedder = _build_embedder(cfg)
    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            document_id = create_vault_note(
                conn,
                cfg=cfg,
                vault_path=vault_path,
                title=title,
                tags=list(tag),
                template=template,
                folder=folder,
                embedder=embedder,
            )
    except VaultNoteSyncError as exc:
        for path, reason in exc.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"created {target_relative.as_posix()} (id={document_id[:8]})"
    )

    if no_edit:
        return
    _run_post_write_editor_and_sync(
        cfg, vault_path=vault_path, file_path=target
    )


def _print_rename_plan(op: RenameOp, vault_path: Path) -> None:
    """Pretty-print a :class:`RenameOp` for ``--dry-run`` output."""
    moved = op.new_path.resolve() != op.old_path.resolve()
    if moved:
        old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
        new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"would rename {old_rel.as_posix()} → {new_rel.as_posix()}"
        )
    else:
        typer.echo(f"would update title: {op.old_title!r} → {op.new_title!r}")
    if not op.references:
        typer.echo("no references to rewrite")
        return
    file_count = len({r.file_path for r in op.references})
    typer.echo(
        f"would rewrite {len(op.references)} reference(s) "
        f"in {file_count} file(s):"
    )
    for ref in op.references:
        rel = ref.file_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"  {rel.as_posix()}:{ref.line_no}  "
            f"{ref.old_text} → {ref.new_text}"
        )


@note_app.command("rename")
def note_rename(
    id: str = typer.Argument(..., help="Document id (or 6+ char prefix)."),
    new_title: str = typer.Argument(..., help="New title."),
    no_link_refactor: bool = typer.Option(
        False,
        "--no-link-refactor",
        help="Skip rewriting [[old-title]] references in other notes.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without changing anything."
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Rename a vault note: title, file slug, and ``[[old]]`` references.

    Plans the rename first (vault scan + collision check), then applies
    atomically — every file we'd write is snapshotted first; on any error
    the snapshots are restored. With ``--dry-run`` only the plan is
    printed; no DB or disk writes occur.

    With ``--no-link-refactor``, references in other notes are left alone
    (the title in this note's frontmatter still updates, and the file is
    still moved to its new slug).
    """
    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        document_id = _resolve_id(conn, id)
        try:
            op = plan_rename(
                conn,
                vault_path=vault_path,
                document_id=document_id,
                new_title=new_title,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e

    if no_link_refactor:
        op = RenameOp(
            document_id=op.document_id,
            old_title=op.old_title,
            new_title=op.new_title,
            old_path=op.old_path,
            new_path=op.new_path,
            references=(),
        )

    if dry_run:
        _print_rename_plan(op, vault_path)
        return

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            report = apply_rename(
                conn,
                embedder=embedder,
                vault_path=vault_path,
                op=op,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e

    if op.references:
        file_count = len({r.file_path for r in op.references})
        typer.echo(
            f"rewrote {report.references_rewritten} reference(s) "
            f"in {file_count} file(s)"
        )
    if report.file_renamed:
        old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
        new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"renamed {old_rel.as_posix()} → {new_rel.as_posix()}"
        )
    else:
        typer.echo(f"updated title: {op.old_title!r} → {op.new_title!r}")
    if report.sync_report and report.sync_report.errors:
        for path, reason in report.sync_report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1)


def _normalize_folder(new_folder: str) -> Path:
    """Vault-root-relative destination directory for ``brain note move``.

    Mirrors :func:`brain.vault.rename.plan_rename`'s own normalization
    exactly — ``""`` / ``"."`` mean the vault root, surrounding whitespace
    and slashes are stripped — so the pre-flight traversal guard here and
    the destination the planner actually computes can never disagree.
    """
    stripped = new_folder.strip()
    if stripped in {"", "."}:
        return Path()
    return Path(stripped.strip("/"))


def _print_move_plan(op: RenameOp, vault_path: Path) -> None:
    """Pretty-print a move :class:`RenameOp` for ``--dry-run`` output."""
    old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
    new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
    typer.echo(f"would move {old_rel.as_posix()} → {new_rel.as_posix()}")
    if not op.references:
        typer.echo("no references to rewrite")
    else:
        file_count = len({r.file_path for r in op.references})
        typer.echo(
            f"would rewrite {len(op.references)} reference(s) "
            f"in {file_count} file(s):"
        )
        for ref in op.references:
            rel = ref.file_path.resolve().relative_to(vault_path.resolve())
            typer.echo(
                f"  {rel.as_posix()}:{ref.line_no}  "
                f"{ref.old_text} → {ref.new_text}"
            )
    typer.echo("(dry run — nothing written)")


@note_app.command("move")
def note_move(
    id: str = typer.Argument(..., help="Document id (or 6+ char prefix)."),
    new_folder: str = typer.Argument(
        ...,
        help=(
            'Destination subdirectory under the vault root. Use "" or "." '
            "for the vault root. Created if missing."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without changing anything."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    no_link_refactor: bool = typer.Option(
        False,
        "--no-link-refactor",
        help=(
            "Skip rewriting [[…]] references in other notes. "
            "The file still moves."
        ),
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Relocate a vault note to another folder, keeping its title and id.

    A move is a rename to the *same* title in a different folder, so it
    reuses the whole rename machinery: the plan phase scans the vault for
    path-form ``[[…]]`` references, and the apply phase snapshots every
    file it touches and restores them on any failure.

    The note's ``documents.id`` is preserved, so incoming backlinks
    survive. The file is relocated with an atomic rename (same inode) and
    ``documents.vault_path`` is repointed immediately afterwards — which
    means **this is safe to run with ``brain vault sync --watch`` and
    ``brain-mcp`` live**. No daemon needs to be stopped.

    There is no ``--force``: if a note already occupies the destination
    path the move fails rather than overwriting it.
    """
    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)
    # Pre-flight the traversal guard at the edge so `../../etc` is a Typer
    # usage error (exit 2) rather than a library exception. `plan_rename`
    # re-checks the fully-resolved destination anyway.
    _assert_within_vault(
        vault_path / _normalize_folder(new_folder),
        vault_path,
        label="NEW-FOLDER",
    )

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        document_id = _resolve_id(conn, id)
        row = conn.execute(
            "SELECT title FROM documents WHERE id=%s", (document_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the row exists
        current_title: str = row[0]
        try:
            op = plan_rename(
                conn,
                vault_path=vault_path,
                document_id=document_id,
                new_title=current_title,
                new_folder=new_folder,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
        except VaultPathEscape as e:
            raise typer.BadParameter(
                f"NEW-FOLDER must stay within the vault; "
                f"got a path that resolves outside {vault_path}"
            ) from e

    old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
    new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
    if op.new_path.resolve() == op.old_path.resolve():
        typer.echo(
            f"already in {old_rel.parent.as_posix()} — nothing to do"
        )
        return

    if no_link_refactor:
        op = RenameOp(
            document_id=op.document_id,
            old_title=op.old_title,
            new_title=op.new_title,
            old_path=op.old_path,
            new_path=op.new_path,
            references=(),
        )

    if dry_run:
        _print_move_plan(op, vault_path)
        return

    if not yes:
        # Unlike `rename`, a move rewrites path-form links across the whole
        # vault and the user cannot see that blast radius from the command
        # line — so it is confirmed by default.
        prompt = f"Move {old_rel.as_posix()} → {new_rel.as_posix()}"
        if op.references:
            file_count = len({r.file_path for r in op.references})
            prompt += (
                f", rewriting {len(op.references)} reference(s) "
                f"in {file_count} file(s)"
            )
        typer.confirm(f"{prompt}?", abort=True)

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            report = apply_rename(
                conn,
                embedder=embedder,
                vault_path=vault_path,
                op=op,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e

    if op.references:
        file_count = len({r.file_path for r in op.references})
        typer.echo(
            f"rewrote {report.references_rewritten} reference(s) "
            f"in {file_count} file(s)"
        )
    typer.echo(
        f"moved {old_rel.as_posix()} → {new_rel.as_posix()} "
        f"(id={op.document_id[:8]})"
    )
    if report.sync_report and report.sync_report.errors:
        for path, reason in report.sync_report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """Attach the ``note`` sub-app to ``app``.

    Called from ``cli.py`` at the point ``add_typer`` used to be invoked —
    Typer lists sub-apps in registration order, so the position of this call
    is what keeps ``brain --help`` byte-identical.
    """
    app.add_typer(note_app, name="note")
