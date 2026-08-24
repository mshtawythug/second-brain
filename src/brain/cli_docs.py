"""`brain list` / `rm` / `mark-draft` / `mark-published` — document lifecycle commands.

Extracted verbatim from :mod:`brain.cli` (which had grown past the 800-line
ceiling in CLAUDE.md). Behaviour is unchanged — command names, flags, help
text, output and exit codes are identical to the previous in-``cli.py``
definitions.

Shared helpers still owned by ``cli.py`` are resolved through the ``brain.cli``
module object *at call time* (see the delegation block below) rather than bound
at import: ``cli.py`` imports this module to register its commands, so a
module-level import back would be a cycle. Reading the attribute at call time
additionally keeps ``monkeypatch.setattr("brain.cli.<name>", ...)`` — the patch
point the existing test suite uses — effective for these commands. Same pattern
as :mod:`brain._capture_command`.

Note that :func:`_rm_unlink_vault_mirror` moved here but is still called by
:mod:`brain._capture_command` as ``brain.cli._rm_unlink_vault_mirror``; ``cli.py``
re-exports it so that call site keeps working. F8 hollowed it out: the actual
unlink lives in :mod:`brain.vault.delete` (shared with the MCP server and
``brain ui``) and this function is now a Config-shaped adapter over it,
keeping both call sites and the ``tests/test_cli_rm.py`` suffix contract
byte-identical.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg
import typer
import yaml

from .config import Config
from .db import connect
from .format import emit_json
from .ingest import update_document
from .queries import list_documents, set_document_sensitivity
from .sensitivity import VALID_SENSITIVITY_LEVELS
from .vault.delete import (
    delete_document,
    describe_delete_target,
    unlink_vault_mirror,
)
from .vault.derived_links.fence import refresh_fences_naming
from .vault.export import regenerate_vault_file
from .vault.frontmatter import rewrite_sensitivity

if TYPE_CHECKING:
    from .graph_rag.sync import GraphSyncer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delegation to `brain.cli`-owned helpers.
#
# These names stay in `cli.py` because commands that did NOT move still call
# them (`_validate_source_choice` -> `resurface`; `_resolve_id` -> ~10
# commands; `_build_graph_syncer` -> edit/vault sync) and because the test
# suite patches them at `brain.cli.<name>`. Each wrapper resolves the
# attribute at call time, so the moved command bodies below are byte-identical
# to their pre-move form.
# ---------------------------------------------------------------------------


def _validate_source_choice(source: str | None) -> str | None:
    """Validate ``--source`` via the ``brain.cli`` owner of the source enum."""
    from . import cli as _cli

    return _cli._validate_source_choice(source)


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a document id prefix via the shared ``brain.cli`` helper."""
    from . import cli as _cli

    return _cli._resolve_id(conn, prefix)


def _build_graph_syncer(cfg: Config) -> GraphSyncer:
    """Build the people-aspect graph syncer via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_graph_syncer(cfg)


def _validate_sensitivity_choice(level: str | None) -> str | None:
    """Reject an unknown ``--sensitivity`` value as a usage error.

    Without this, ``--sensitivity confidental`` (sic) would filter on a level
    no row carries and return an empty list — which reads as "nothing is
    marked confidential", the most dangerous possible wrong answer for a
    confidentiality filter.
    """
    if level is None:
        return None
    if level not in VALID_SENSITIVITY_LEVELS:
        raise typer.BadParameter(
            f"--sensitivity must be one of "
            f"{'/'.join(sorted(VALID_SENSITIVITY_LEVELS))} (got {level!r})"
        )
    return level


def list_docs(
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    sensitivity: str | None = typer.Option(
        None,
        "--sensitivity",
        help="Only documents at this trust tier (normal|confidential).",
    ),
    limit: int = typer.Option(20, "--limit", "-n", min=1),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the table."
    ),
) -> None:
    """List documents in the brain."""
    _validate_source_choice(source)
    _validate_sensitivity_choice(sensitivity)
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        rows = list_documents(
            conn, source=source, tag=tag, sensitivity=sensitivity, limit=limit
        )
    if json_output:
        emit_json(
            [
                {
                    "id": r.id,
                    "title": r.title,
                    "content_type": r.content_type,
                    "tags": r.tags,
                    "source_kind": r.source_kind,
                    "ingested_at": r.ingested_at,
                    # Always present and always a string — a consumer deciding
                    # whether to display a body must never have to infer the
                    # tier from a missing key.
                    "sensitivity": r.sensitivity,
                }
                for r in rows
            ]
        )
        return
    for r in rows:
        kind = r.source_kind or "manual"
        typer.echo(f"{r.id[:8]}  {kind:<8}  {r.content_type:<10}  {r.title}")


def rm(
    id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a document (and its chunks) from the brain.

    When the document has a ``vault_path`` the on-disk mirror file under
    ``cfg.vault_path / vault_path`` is also unlinked. Without that step the
    next ``brain vault sync`` would re-ingest the file by ``content_hash``
    (or, after a slug rename, create a fresh row), silently undoing the rm.
    A missing mirror is tolerated (debug log only) — the DB delete still
    proceeds.
    """
    cfg = Config.load()
    graph_syncer = _build_graph_syncer(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        # Read the title BEFORE the delete so the prompt can name the doc.
        # `delete_document` re-reads title + vault_path itself (one extra
        # single-row SELECT) rather than taking them as arguments, so the
        # "read before delete" invariant is enforced inside the shared path
        # instead of trusted from each caller.
        target = describe_delete_target(conn, document_id=doc_id)
        assert target is not None  # _resolve_id confirmed the doc exists
        if not yes:
            typer.confirm(f"Delete '{target.title}' ({doc_id[:8]})?", abort=True)
        report = delete_document(
            conn,
            document_id=doc_id,
            vault_root=cfg.vault_path,
            graph_syncer=graph_syncer,
        )
    typer.echo(f"removed {doc_id[:8]}{report.suffix}")


def mark_draft(id: str = typer.Argument(...)) -> None:
    """Quarantine a document: set ``draft=true`` and regenerate its mirror.

    A draft doc still lives in the DB and is reachable via ``brain search`` /
    ``brain show`` / ``brain list`` (the CLI is local — the user wants to
    see drafts). Only the wiki hides it: the Quartz contentIndex emitter
    skips ``draft: true`` entries entirely, so the doc disappears from the
    explorer tree, the graph view, and full-text search on the rendered site.

    Idempotent — running it twice on an already-draft doc is a no-op and
    prints ``<short-id> is already draft``. Use ``brain mark-published`` to
    re-publish.
    """
    _set_draft(id, draft=True)


def mark_published(id: str = typer.Argument(...)) -> None:
    """Un-quarantine a document: set ``draft=false`` and regenerate its mirror.

    Inverse of ``brain mark-draft``. Idempotent — running it on a doc that
    is already published prints ``<short-id> is already published`` and
    exits 0.
    """
    _set_draft(id, draft=False)


def _set_draft(id_prefix: str, *, draft: bool) -> None:
    """Shared body for ``mark-draft`` / ``mark-published``.

    Resolves ``id_prefix``, no-ops idempotently when the column already
    matches ``draft``, otherwise calls :func:`update_document` with
    ``new_draft=draft`` and ``vault_root=cfg.vault_path`` so the on-disk
    mirror is regenerated with the new ``draft:`` frontmatter line. Echoes
    a one-line confirmation.

    Errors (prefix not found / ambiguous) propagate via
    :func:`_resolve_id` → ``typer.Exit(code=1)``.
    """
    cfg = Config.load()
    graph_syncer = _build_graph_syncer(cfg)
    target_state_label = "draft" if draft else "published"
    other_state_label = "published" if draft else "draft"
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id_prefix)
        row = conn.execute(
            "SELECT draft FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the row exists
        current_draft = bool(row[0])
        label = doc_id[:8]
        if current_draft == draft:
            typer.echo(f"{label} is already {target_state_label}")
            return
        try:
            update_document(
                conn,
                document_id=doc_id,
                new_draft=draft,
                vault_root=cfg.vault_path,
                graph_syncer=graph_syncer,
            )
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    typer.echo(f"marked {label} as {target_state_label} (was {other_state_label})")


def mark_confidential(id: str = typer.Argument(...)) -> None:
    """Mark a document confidential: withheld from agents, never sent to a hosted embedder.

    Raises the document's trust tier. A confidential document still lives in
    the DB and is fully reachable from the local CLI — the boundary is at
    *egress*: MCP ``brain_show`` withholds the body unless explicitly asked,
    the hosted-embedder path refuses to ship it, and the wiki drops it from
    the published index.

    Idempotent — running it twice prints ``<short-id> is already confidential``
    and exits 0. Use ``brain mark-normal`` to reverse it.
    """
    _set_sensitivity(id, level="confidential")


def mark_normal(id: str = typer.Argument(...)) -> None:
    """Return a document to the normal trust tier.

    The inverse of ``brain mark-confidential``, and the **only** sanctioned
    downgrade: re-ingest is deliberately escalate-only (it can raise a
    document's tier but never lower it), so without this command a document
    marked confidential — correctly or by accident — could never be un-marked.

    Idempotent, like its counterpart.
    """
    _set_sensitivity(id, level="normal")


def _set_sensitivity(id_prefix: str, *, level: str) -> None:
    """Shared body for ``mark-confidential`` / ``mark-normal``.

    ``set_document_sensitivity`` returns ``True`` iff the row actually
    changed (its ``sensitivity <> %s`` guard), which is what makes the
    idempotent message truthful with no second round-trip. It also stamps
    ``updated_at=NOW()``: changing a document's trust tier IS a change to the
    user's knowledge about it, so ``--updated-after`` must surface it.

    The mirror is regenerated on a real change so the on-disk frontmatter
    reflects the new tier — but **only for ingested-tier rows**, whose mirror
    is DB-derived. Vault-tier authored notes are file-source-of-truth and
    ``regenerate_vault_file`` refuses them outright ("restore from backup or
    git instead"), so calling it unconditionally would make
    ``mark-confidential`` fail on exactly the notes most likely to hold
    sensitive material.

    The ``kind`` is pre-checked from the DB rather than by catching that
    ``ValueError`` and string-matching its message — the same choice
    :func:`brain.ingest.update_document` makes at its own mirror-write site,
    and for the same reason: the upstream message can be rephrased without
    notice.

    A vault-tier note instead gets **one frontmatter field rewritten in
    place** via :func:`~brain.vault.frontmatter.rewrite_sensitivity`. That is
    not belt-and-braces: for vault-tier rows the file is authoritative, and
    ``sync._sensitivity_from_frontmatter`` reads the tier back off the
    frontmatter on every pass, treating a missing key as ``normal``. Without
    the on-disk write the column would flip and then silently revert on the
    next ``brain vault sync --watch`` pass — the command would appear to
    succeed and quietly undo itself. ``test_the_mark_survives_a_resync`` pins
    that end-to-end.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id_prefix)
        label = doc_id[:8]
        row = conn.execute(
            "SELECT kind, vault_path FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the row exists
        kind, vault_path_rel = row
        changed = set_document_sensitivity(
            conn, document_id=doc_id, level=level
        )
        if not changed:
            typer.echo(f"{label} is already {level}")
            return
        if kind == "vault":
            if vault_path_rel:
                try:
                    rewrite_sensitivity(cfg.vault_path / vault_path_rel, level)
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    # The DB change already committed. Surface loudly: on a
                    # vault-tier note the on-disk value is authoritative, so a
                    # failed write means the next sync WILL revert the tier.
                    typer.secho(
                        f"warning: could not write sensitivity into "
                        f"{vault_path_rel}: {exc}. The next `brain vault sync` "
                        f"will revert {label} to the frontmatter's value — fix "
                        f"the file and re-run.",
                        fg="yellow",
                        err=True,
                    )
        else:
            try:
                regenerate_vault_file(
                    conn, doc_id, vault_path=cfg.vault_path, force=True
                )
            except OSError as exc:
                # The DB change already committed; a mirror write failure must
                # not lose it. Mirrors update_document's recovery guidance.
                logger.warning(
                    "vault mirror write failed for document %s: %s; "
                    "DB update succeeded — recover via `brain vault export`",
                    doc_id,
                    exc,
                )
        # The tier change is not complete until the pages that NAME this
        # document agree with it. The F6 gate in ``render_fenced_section``
        # decides what a fence may name at RENDER time and is silent about
        # fences rendered earlier, so without this the command printed
        # "marked ... as confidential" while every partner's published page
        # still carried this document's title and slug — for an unbounded
        # stretch, until somebody happened to run a full relink. Runs for BOTH
        # directions: ``mark-normal`` puts the document back into its partners'
        # fences rather than leaving it invisible until the next relink.
        #
        # Best-effort, and deliberately AFTER the DB commit and the mirror
        # write: a fence-refresh failure must not lose either. It is warned,
        # not raised, with the same recovery guidance as the mirror path —
        # `brain vault relink-derived` rebuilds every fence from scratch.
        try:
            refresh_fences_naming(conn, doc_id, vault_path=cfg.vault_path)
        except (OSError, psycopg.Error) as exc:
            typer.secho(
                f"warning: could not refresh derived-link fences after marking "
                f"{label} as {level}: {exc}. Other documents' published pages "
                f"may still name it — run `brain vault relink-derived`.",
                fg="yellow",
                err=True,
            )
    typer.echo(f"marked {label} as {level}")


def _rm_unlink_vault_mirror(*, cfg: Config, vault_path_rel: str | None) -> str:
    """Remove the on-disk vault mirror after ``brain rm`` deletes the DB row.

    Config-shaped adapter over :func:`brain.vault.delete.unlink_vault_mirror`,
    kept because :mod:`brain._capture_command` calls it through
    ``brain.cli._rm_unlink_vault_mirror``. Returns the suffix appended to the
    CLI's ``removed <id>`` line — a user-facing contract asserted by
    ``tests/test_cli_rm.py``:

    - ``vault_path`` NULL → ``" (db only)"`` (e.g., raw ``ingest-stdin`` rows
      that never made it into a vault export).
    - File present + unlinked → ``" (file: <vault_path>)"``.
    - File already absent on disk → ``" (db only, file already gone)"`` so
      the user sees that the row was deleted but the cleanup was a no-op
      (e.g., the user manually removed the mirror first, or a previous
      partial rm).
    """
    _, suffix = unlink_vault_mirror(
        vault_root=cfg.vault_path, vault_path_rel=vault_path_rel
    )
    return suffix


def register_list(app: typer.Typer) -> None:
    """Attach ``brain list`` to ``app``.

    Separate from :func:`register_lifecycle` because ``resurface`` / ``tag`` /
    ``edit`` are declared between them in ``cli.py``, and Typer lists commands
    in registration order — splitting the two calls is what keeps
    ``brain --help`` byte-identical.
    """
    app.command(name="list")(list_docs)


def register_lifecycle(app: typer.Typer) -> None:
    """Attach the document-lifecycle commands to ``app``.

    ``mark-confidential`` / ``mark-normal`` are registered after the
    draft pair so ``brain --help`` groups the two trust-tier axes together
    and in a stable order.
    """
    app.command()(rm)
    app.command(name="mark-draft")(mark_draft)
    app.command(name="mark-published")(mark_published)
    app.command(name="mark-confidential")(mark_confidential)
    app.command(name="mark-normal")(mark_normal)
