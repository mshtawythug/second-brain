"""The **only** module in this package permitted to mutate anything.

Every operation here delegates to a function the CLI, the MCP server, or F8's
``vault/`` seams already own. Nothing is reimplemented, and nothing shells out
to the CLI:

===================  =========================================================
UI operation         Delegates to
===================  =========================================================
create               ``vault.note_builder.create_vault_note``
update (vault)       frontmatter merge → ``vault._atomic.atomic_write_text``
                     → ``vault.sync.sync_one_file``
update (ingested)    ``ingest.update_document`` / ``ingest.apply_tags``
draft toggle         ``ingest.update_document(new_draft=…)``
move / rename        ``vault.rename.plan_rename`` → ``apply_rename``
delete               ``vault.delete.delete_document``
===================  =========================================================

**Tier dispatch is the thing to get right.** For ``kind='vault'`` the *file* is
the source of truth — ``update_document`` deliberately skips mirror writes for
that tier — so a vault edit writes the file and re-syncs. For
``kind='ingested'`` the *row* is the source of truth and the mirror is
regenerated from it. Crossing those wires corrupts one tier or the other, which
is why every write goes through :func:`update_note`'s single dispatch rather
than through per-route logic.

Full-corpus operations must never run from a request handler: the recorded
``relink-derived`` ↔ watcher deadlock caused hours of ``graph_entities``
contention. ``sync_one_file`` — scoped to exactly one document — is the only
sync this module may call.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from ..errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
    VaultNoteSyncError,
    VaultPathEscape,
)
from ..queries import fetch_document, resolve_document_prefix
from ..sensitivity import DEFAULT_SENSITIVITY, is_confidential
from ..vault._atomic import atomic_write_text
from ..vault.delete import delete_document
from ..vault.frontmatter import body_hash, dump_frontmatter, parse_frontmatter
from ..vault.paths import assert_within_vault
from ..vault.rename import apply_rename, plan_rename
from . import queries as ui_queries
from .errors import UiBadRequest, UiConflict, UiForbidden, UiNotFound
from .render import render_markdown

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .context import UiContext
    from .schemas import NoteCreate, NotePatch

logger = logging.getLogger(__name__)

#: Frontmatter keys the export layer owns. A UI edit merges *around* them
#: rather than through them, so a save cannot strip an id or a tier marker that
#: a later ``brain vault sync`` depends on.
_PROTECTED_FRONTMATTER = frozenset({"id", "kind", "source", "vault_path"})

#: The withheld message. Vocabulary deliberately matches MCP ``brain_show``
#: so a client handles ONE spelling of "withheld" across both surfaces. Only
#: the *trigger* differs: MCP gates on an explicit ``include_confidential``
#: argument, the UI on whether the server is loopback-bound (see
#: ``UiContext.serve_confidential_bodies``).
_WITHHELD_NOTICE = (
    "body withheld: sensitivity=confidential and this server is not bound to "
    "loopback. Restart `brain ui` without --host, or pass "
    "--include-confidential."
)


def resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve an id prefix, mapping the brain's errors onto HTTP ones.

    ``resolve_document_prefix`` enforces a 6-character minimum, which exists so
    a typo cannot match a wide swathe of the corpus.
    """
    try:
        return resolve_document_prefix(conn, prefix)
    except IdPrefixTooShort as exc:
        raise UiBadRequest(str(exc), code="id_prefix_too_short") from exc
    except IdPrefixNotHex as exc:
        raise UiBadRequest(str(exc), code="id_prefix_not_hex") from exc
    except IdPrefixAmbiguous as exc:
        raise UiBadRequest(str(exc), code="id_prefix_ambiguous") from exc
    except IdPrefixNotFound as exc:
        raise UiNotFound(f"no document matches prefix {prefix!r}", code="note_not_found") from exc


def _vault_file(ctx: UiContext, vault_path: str | None) -> Path | None:
    """Absolute path of a vault mirror, guarded against traversal.

    ``vault_path`` comes from the database rather than from the request, but it
    is still validated: a row written by an older or buggier code path must not
    be able to make the UI read or write outside the vault.
    """
    if not vault_path:
        return None
    target = ctx.cfg.vault_path / vault_path
    assert_within_vault(target, ctx.cfg.vault_path)
    return target


def _split_file(path: Path) -> tuple[dict[str, Any], str]:
    """``(frontmatter, body)`` for an on-disk note; empty pair if it is missing."""
    if not path.is_file():
        return {}, ""
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def read_note(
    ctx: UiContext, conn: psycopg.Connection[Any], document_id: str
) -> dict[str, Any]:
    """Assemble the inspector payload for one document.

    For a vault-tier note the **file** supplies the body and therefore the
    ``body_hash``, matching what a later save will compare against. For an
    ingested-tier document the row supplies it. Getting this backwards would
    make every vault save look stale.
    """
    row = fetch_document(conn, document_id)
    if row is None:
        raise UiNotFound("no such document", code="note_not_found")

    meta = ui_queries.note_meta(conn, document_id)
    if meta is None:
        raise UiNotFound("no such document", code="note_not_found")
    vault_path, tier, draft = meta

    path = _vault_file(ctx, vault_path)
    if tier == "vault" and path is not None and path.is_file():
        _, body = _split_file(path)
    else:
        body = row.content or ""

    sensitivity = getattr(row, "sensitivity", DEFAULT_SENSITIVITY)
    withheld = is_confidential(sensitivity) and not ctx.serve_confidential_bodies

    payload: dict[str, Any] = {
        "id": document_id,
        "title": row.title,
        "tier": tier,
        "content_type": row.content_type,
        "draft": draft,
        "tags": list(row.tags or []),
        "source_kind": row.source_kind,
        "vault_path": vault_path,
        "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
        # Only a vault-tier note with a real file behind it can be edited in
        # place; an ingested row without a mirror has no user-authored source.
        "editable": tier == "vault" or bool(vault_path),
        "movable": tier == "vault" and bool(vault_path),
    }

    # The tier is reported whenever it is not the default, in BOTH modes — the
    # user should be able to see that a document exists and is confidential even
    # when they cannot read it here. Mirrors ``brain_show``, which emits
    # ``sensitivity`` only when it is meaningful.
    if sensitivity != DEFAULT_SENSITIVITY:
        payload["sensitivity"] = sensitivity

    if withheld:
        # Withholding is TOTAL. `body` is the obvious one; `summary` is the
        # trap, because ``documents.summary`` is LLM-generated FROM the body —
        # returning it beside a withheld body hands out a précis of exactly the
        # content being protected. `html` and `body_hash` are derived from the
        # body too, so neither is emitted. Search snippets are redacted
        # separately in ``routes_search`` (they come from ``chunks``).
        payload["body"] = None
        payload["html"] = ""
        payload["withheld"] = _WITHHELD_NOTICE
        return payload

    targets = _wikilink_targets(body)
    resolved = ui_queries.resolve_link_targets(conn, targets) if targets else {}
    payload["body"] = body
    payload["body_hash"] = body_hash(body)
    payload["html"] = render_markdown(
        body, resolver=lambda t: resolved.get(t.lower())
    )
    if row.summary is not None:
        payload["summary"] = row.summary
    return payload


def _wikilink_targets(body: str) -> list[str]:
    """Collect ``[[Target]]`` names so they can be resolved in one query.

    A deliberately loose scan: over-collecting costs one array parameter, while
    under-collecting would silently leave real links unresolved. Correct
    *rendering* is the parser's job (:mod:`brain.ui.render`), not this.
    """
    targets: list[str] = []
    cursor = 0
    while (start := body.find("[[", cursor)) != -1:
        end = body.find("]]", start + 2)
        if end == -1:
            break
        inner = body[start + 2 : end]
        target = inner.split("|")[0].strip()
        if target and "\n" not in target:
            targets.append(target)
        cursor = end + 2
    return targets


def create_note(
    ctx: UiContext, conn: psycopg.Connection[Any], spec: NoteCreate
) -> dict[str, Any]:
    """Create a vault-tier note on disk and in the database, in one action."""
    from ..vault.note_builder import create_vault_note

    if spec.folder:
        assert_within_vault(ctx.cfg.vault_path / spec.folder, ctx.cfg.vault_path)

    try:
        document_id = create_vault_note(
            conn,
            cfg=ctx.cfg,
            vault_path=ctx.cfg.vault_path,
            title=spec.title,
            body=spec.body,
            tags=spec.tags,
            template=spec.template,
            folder=spec.folder,
            embedder=ctx.embedder,
        )
    except VaultNoteSyncError as exc:
        raise UiConflict(str(exc), code="sync_failed") from exc

    meta = ui_queries.note_meta(conn, document_id)
    return {
        "id": document_id,
        "title": spec.title,
        "vault_path": meta[0] if meta else None,
    }


def _merge_frontmatter(
    existing: dict[str, Any], patch: NotePatch
) -> dict[str, Any]:
    """Apply a patch to a frontmatter dict, preserving key order.

    Key order is preserved because the file is a user-visible artefact under
    version control for many users; reordering it on every save would turn a
    one-line edit into a whole-file diff.
    """
    merged = dict(existing)
    if patch.title is not None:
        merged["title"] = patch.title
    if patch.tags is not None:
        merged["tags"] = patch.tags
    if patch.content_type is not None:
        merged["type"] = patch.content_type
    return merged


def update_note(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    document_id: str,
    patch: NotePatch,
) -> dict[str, Any]:
    """Save an edit, dispatching on tier. Raises 409 on a stale ``body_hash``."""
    current = read_note(ctx, conn, document_id)
    if "withheld" in current:
        # Editing a body you were not allowed to read would let a caller
        # overwrite confidential content sight-unseen — and the body_hash check
        # below cannot run, because no hash was issued.
        raise UiForbidden(
            "this document's body is withheld on a non-loopback bind and "
            "therefore cannot be edited here",
            code="body_withheld",
        )
    if patch.body_hash != current["body_hash"]:
        raise UiConflict(
            "this note changed on disk since you opened it",
            code="stale_write",
        )
    if patch.is_empty():
        return {
            "id": document_id,
            "fields_changed": [],
            "rechunked": False,
            "body_hash": current["body_hash"],
            "html": current["html"],
        }

    if current["tier"] == "vault":
        return _update_vault_note(ctx, conn, document_id, patch, current)
    return _update_ingested_note(ctx, conn, document_id, patch)


def _update_vault_note(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    document_id: str,
    patch: NotePatch,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Vault tier: the file is the source of truth, so write it and re-sync."""
    from ..vault.sync import sync_one_file

    path = _vault_file(ctx, current["vault_path"])
    if path is None or not path.is_file():
        raise UiNotFound(
            "this note has no file behind it", code="vault_file_missing"
        )

    frontmatter, body = _split_file(path)
    new_body = patch.body if patch.body is not None else body
    merged = _merge_frontmatter(frontmatter, patch)
    for key in _PROTECTED_FRONTMATTER:
        if key in frontmatter:
            merged[key] = frontmatter[key]

    atomic_write_text(path, dump_frontmatter(merged, new_body))

    report = sync_one_file(
        conn,
        embedder=ctx.embedder,
        vault_path=ctx.cfg.vault_path,
        file_path=path,
        owner_participants=ctx.cfg.owner_participants,
        graph_syncer=ctx.graph_syncer,
    )
    if report.errors:
        raise UiConflict(
            "; ".join(str(message) for _, message in report.errors),
            code="sync_failed",
        )

    changed = [
        name
        for name, value in (
            ("body", patch.body),
            ("title", patch.title),
            ("tags", patch.tags),
            ("content_type", patch.content_type),
        )
        if value is not None
    ]
    refreshed = read_note(ctx, conn, document_id)
    return {
        "id": document_id,
        "fields_changed": changed,
        "rechunked": patch.body is not None,
        "body_hash": refreshed["body_hash"],
        "html": refreshed["html"],
    }


def _update_ingested_note(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    document_id: str,
    patch: NotePatch,
) -> dict[str, Any]:
    """Ingested tier: the row is the source of truth; the mirror follows it."""
    from ..ingest import apply_tags, update_document

    try:
        result = update_document(
            conn,
            document_id=document_id,
            embedder=ctx.embedder,
            new_title=patch.title,
            new_content=patch.body,
            new_content_type=patch.content_type,
            vault_root=ctx.cfg.vault_path,
            graph_syncer=ctx.graph_syncer,
        )
    except ValueError as exc:
        raise UiBadRequest(str(exc), code="invalid_edit") from exc

    changed = list(result.fields_changed)
    if patch.tags is not None:
        apply_tags(conn, document_id, add=patch.tags)
        changed.append("tags")

    refreshed = read_note(ctx, conn, document_id)
    return {
        "id": document_id,
        "fields_changed": changed,
        "rechunked": patch.body is not None,
        "body_hash": refreshed["body_hash"],
        "html": refreshed["html"],
    }


def set_draft(
    ctx: UiContext, conn: psycopg.Connection[Any], document_id: str, *, draft: bool
) -> dict[str, Any]:
    """Toggle draft ↔ published. The one instant slice of the deferred Publish tab.

    Regenerates the mirror's ``draft:`` frontmatter so the next Quartz build
    hides or shows the note. Idempotent: setting the value it already has is a
    no-op, matching ``cli._set_draft``.
    """
    from ..ingest import update_document

    update_document(
        conn,
        document_id=document_id,
        new_draft=draft,
        vault_root=ctx.cfg.vault_path,
        graph_syncer=ctx.graph_syncer,
    )
    return {"id": document_id, "draft": draft}


def move_note(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    new_title: str | None,
    new_folder: str | None,
) -> dict[str, Any]:
    """Rename and/or move a vault-tier note, rewriting inbound wiki links.

    Delegates wholly to F8's ``plan_rename`` / ``apply_rename``, which own the
    atomicity contract — snapshot every touched file, restore on any exception.
    A hand-rolled ``os.replace`` here would lose that, and would also fail to
    rewrite path-form ``[[folder/slug|Title]]`` backlinks in *other* files,
    because ``sync_one_file`` only re-materializes links *from* the file it
    syncs.
    """
    current = read_note(ctx, conn, document_id)
    if not current["movable"]:
        raise UiBadRequest(
            "only vault-tier notes can be moved; ingested paths are derived",
            code="move_not_supported_for_ingested_tier",
        )
    if new_folder:
        assert_within_vault(ctx.cfg.vault_path / new_folder, ctx.cfg.vault_path)

    try:
        op = plan_rename(
            conn,
            vault_path=ctx.cfg.vault_path,
            document_id=document_id,
            new_title=new_title or current["title"],
            new_folder=new_folder,
        )
        report = apply_rename(
            conn, embedder=ctx.embedder, vault_path=ctx.cfg.vault_path, op=op
        )
    except VaultPathEscape as exc:
        raise UiBadRequest(str(exc), code="folder_escapes_vault") from exc
    except Exception as exc:  # noqa: BLE001 — RenameError and friends → 400
        raise UiBadRequest(str(exc), code="move_failed") from exc

    meta = ui_queries.note_meta(conn, document_id)
    return {
        "id": document_id,
        "vault_path": meta[0] if meta else None,
        "file_renamed": report.file_renamed,
        "files_rewritten": report.files_rewritten,
        "references_rewritten": report.references_rewritten,
    }


def delete_note(
    ctx: UiContext,
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    expected_title: str,
) -> dict[str, Any]:
    """Delete a document, its graph presence, and its vault mirror.

    The ``expected_title`` comparison is **server-side on purpose**. A UI-only
    confirmation protects nothing against a replayed, stale, or mis-targeted
    request — and this project has already destroyed a real note that way, on
    2026-06-09, by piping a blind confirm into ``brain capture review
    --limit 1``. Checking here means the wrong document cannot be deleted even
    if the client is wrong or malicious.

    The delete itself is F8's ``vault.delete.delete_document``, never
    reproduced: it owns the ordering (read title+path → DELETE → graph remove →
    unlink mirror), and skipping that last step is how "the next ``brain vault
    sync`` resurrects the deleted note" comes back.
    """
    current = read_note(ctx, conn, document_id)
    if expected_title != current["title"]:
        raise UiConflict(
            "the title you typed does not match this document; nothing deleted",
            code="title_mismatch",
        )

    report = delete_document(
        conn,
        document_id=document_id,
        vault_root=ctx.cfg.vault_path,
        graph_syncer=ctx.graph_syncer,
    )
    return {
        "deleted": True,
        "id": document_id,
        "title": report.title,
        "mirror_action": report.mirror_action,
        "mirror_unlinked": report.mirror_action == "unlinked",
    }
