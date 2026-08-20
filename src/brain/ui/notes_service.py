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
import re
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from ..errors import (
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
    VaultNoteSyncError,
)
from ..queries import fetch_document, resolve_document_prefix
from ..sensitivity import DEFAULT_SENSITIVITY, is_confidential
from ..vault._atomic import atomic_write_text
from ..vault.delete import delete_document
from ..vault.frontmatter import body_hash, dump_frontmatter, parse_frontmatter
from ..vault.paths import assert_within_vault
from ..vault.rename import RenameError, apply_rename, plan_rename
from . import queries as ui_queries
from .errors import UiBadRequest, UiConflict, UiForbidden, UiNotFound
from .render import extract_headings, render_markdown

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
        #
        # ``read_only`` is part of BOTH answers, and leaving it out was not
        # cosmetic. These flags mean "can this be done HERE", not "in
        # principle": ``js/keys.js`` binds Cmd+E on ``state.note.editable``
        # ALONE, so an ``editable: true`` payload from a ``--read-only`` server
        # dropped the user into an editor whose every save the security
        # middleware then refused with a 403 — a dead end reached by keyboard,
        # invisible to a UI that had correctly hidden its Edit button.
        # ``movable`` carries the same defect for the same reason: a move is a
        # mutation, and the middleware refuses it before routing.
        "editable": (tier == "vault" or bool(vault_path)) and not ctx.read_only,
        "movable": tier == "vault" and bool(vault_path) and not ctx.read_only,
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
        if not ctx.read_only:
            payload["body"] = None
        payload["html"] = ""
        payload["withheld"] = _WITHHELD_NOTICE
        return payload

    targets = _wikilink_targets(body)
    resolved = ui_queries.resolve_link_targets(conn, targets) if targets else {}
    # ``body`` is the EDITOR's raw source and nothing else — every read surface
    # renders ``html``. A ``--read-only`` server has no editor (the middleware
    # refuses every non-safe method before routing, and ``editable`` above is
    # now False), so the field is unusable there by construction, while being
    # the largest thing on the wire: 570 KB against ~287 KB of ``html`` on the
    # largest document in this corpus. The key is OMITTED rather than nulled so
    # a client cannot mistake "not sent" for "empty note" — and so the
    # invariant holds for the withheld branch above too: a read-only payload
    # never carries a ``body`` key.
    #
    # ``body_hash`` stays. It is ~70 bytes, it is the optimistic-concurrency
    # token rather than content, and ``update_note`` reads it off this same
    # payload.
    if not ctx.read_only:
        payload["body"] = body
    payload["body_hash"] = body_hash(body)
    # ONE local, passed to BOTH walks — this hoist *is* the S4 guard, and it
    # lives here rather than inside ``extract_headings`` on purpose. Rendering
    # the stripped body while extracting headings from ``body`` would put an
    # entry at the top of every TOC pointing at an ``<h1>`` the HTML does not
    # contain: a link that scrolls nowhere, on essentially the whole vault.
    # Because both functions receive the same string, they see the same
    # ``heading_open`` sequence and mint the same ids by construction.
    # ``extract_headings`` deliberately strips nothing itself, so the decision
    # exists in exactly one place; do not move it in there.
    rendered = strip_redundant_title_heading(body, row.title)
    payload["html"] = render_markdown(
        rendered, resolver=lambda t: resolved.get(t.lower())
    )
    # Placed AFTER the withheld early-return above, and it must stay there:
    # headings are derived from the body, so a TOC beside a withheld body would
    # hand out the confidential document's section titles — the same reasoning
    # that already withholds `summary`, `html` and `body_hash`.
    payload["headings"] = [asdict(h) for h in extract_headings(rendered)]
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


#: A CommonMark ATX heading: up to three leading spaces, one to six ``#``, then
#: a space and the heading text. The space is required — ``#Title`` is a
#: paragraph, not a heading, and must not be stripped.
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(?P<text>.*))?$")


def _normalize_heading(text: str) -> str:
    """Collapse whitespace and case so ``#  My   Note`` matches ``My Note``."""
    return " ".join(text.split()).casefold()


def _drop_closing_sequence(text: str) -> str:
    """Remove a CommonMark closing ``###`` run, which is syntax, not content.

    Only a run preceded by whitespace closes a heading, so ``# C#`` keeps its
    hash and ``# Title ###`` does not.
    """
    stripped = text.rstrip()
    if not stripped.endswith("#"):
        return stripped
    trimmed = stripped.rstrip("#")
    if trimmed and not trimmed.endswith((" ", "\t")):
        return stripped
    return trimmed.rstrip()


def strip_redundant_title_heading(body: str, title: str | None) -> str:
    """Drop a leading heading that only repeats ``title``.

    Every note ``brain note new`` and ``brain daily`` produce opens with its own
    ``# Title`` (``vault.templates.NOTE_TEMPLATE`` / ``DAILY_TEMPLATE``), and
    every read surface also renders the title as a heading of its own — so the
    title appeared twice on essentially the whole vault.

    This is a **render-time** transform and nothing more: callers pass the
    stored body in and put the result into ``html``. ``body``, ``body_hash`` and
    the file on disk are all untouched, so a round-trip through the editor
    cannot silently delete the user's heading.

    Deliberately conservative — it fires only when the *first* non-blank line is
    an ATX heading whose text matches the title. A heading further down, a
    heading with different text, a setext underline, and a body with no heading
    at all are all returned verbatim.
    """
    if not body or not title:
        return body

    lines = body.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return body

    match = _ATX_HEADING.match(lines[index])
    if match is None:
        return body
    heading = _drop_closing_sequence(match.group("text") or "")
    if not heading or _normalize_heading(heading) != _normalize_heading(title):
        return body
    return "\n".join(lines[:index] + lines[index + 1 :])


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
    """Ingested tier: the row is the source of truth; the mirror follows it.

    Body/title and tags are ONE transaction. The UI runs its connection with
    ``autocommit = True`` (``server.py``), under which ``update_document``'s own
    ``with conn.transaction()`` commits the body and title the moment it
    returns. A failing ``apply_tags`` then left a half-applied edit committed —
    body and title saved, tags not — behind a 500 that told the user the save
    had failed. The explicit transaction here makes psycopg nest the inner one
    as a SAVEPOINT, so there is a single commit at the end and a tag failure
    rolls the body and title back with it.
    """
    from ..ingest import (
        apply_tags,
        mirror_is_stale,
        update_document,
        write_vault_mirror,
    )

    try:
        with conn.transaction():
            result = update_document(
                conn,
                document_id=document_id,
                embedder=ctx.embedder,
                new_title=patch.title,
                new_content=patch.body,
                new_content_type=patch.content_type,
                # DELIBERATELY None: the mirror is a FILE, so no rollback can
                # unwrite it. `update_document` writes it after its own
                # transaction — correct only while it owns the outermost one.
                # Under the transaction opened above, its block is a SAVEPOINT
                # that commits nothing, so a mirror written there would survive
                # a rollback as an orphan the database never recorded. We take
                # the write ourselves, below, after this transaction commits.
                vault_root=None,
                graph_syncer=ctx.graph_syncer,
            )
            changed = list(result.fields_changed)
            if patch.tags is not None:
                # NOT folded into ``update_document(new_tags=...)``, which looks
                # like the obvious way to get atomicity for free. It is not
                # equivalent: ``apply_tags(add=...)`` UNIONS with the existing
                # tags, while ``new_tags`` REPLACES the column outright. Swapping
                # them would silently turn PATCH from "add these tags" into
                # "replace all tags with these" and drop tags on every edit that
                # sent a partial list — a behaviour change wearing a bug fix's
                # clothes.
                apply_tags(conn, document_id, add=patch.tags)
                changed.append("tags")
    except ValueError as exc:
        raise UiBadRequest(str(exc), code="invalid_edit") from exc

    # COMMITTED. Only now is it safe to write a file, because only now can the
    # edit no longer be rolled back.
    #
    # `changed` rather than `result.fields_changed`, and the difference is
    # load-bearing in exactly one case: a TAGS-ONLY edit. `update_document`
    # returns before `apply_tags` runs, so its list is empty for that edit and
    # `mirror_is_stale` would answer False — skipping the write and leaving the
    # old tags on disk forever. (For any other edit the two agree, and since
    # the mirror is regenerated FROM the committed row it picks up the new tags
    # either way. Only the skip is dangerous.)
    # No `vault_path is not None` guard: `Config.vault_path` is a `Path` with a
    # `default_factory` (`config.py:769`), never optional, so such a check would
    # always be True — and would falsely advertise that the vault is optional
    # here. `update_document`'s own `vault_root is not None` test is a different
    # question: that parameter IS `Path | None`, and passing None is how this
    # function suppresses the mirror write above.
    if mirror_is_stale(fields_changed=changed, rechunked=result.rechunked):
        write_vault_mirror(conn, document_id, vault_root=ctx.cfg.vault_path)

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
    # NO `except VaultPathEscape` arm here, deliberately. It was reachable —
    # `plan_rename:199` guards a destination built with `with_name(slug)`, and
    # `assert_within_vault` RESOLVES SYMLINKS, so a legal stored path can still
    # yield a destination that resolves outside the vault when a symlink sits
    # at the new name. But it was REDUNDANT: `app.py:169` registers a global
    # `VaultPathEscape` handler returning the same 400 and the same
    # `folder_escapes_vault` code, so deleting the arm changes exactly one
    # field — `message` — which here was `str(exc)`, leaking the ABSOLUTE vault
    # path that `app.py:76-78` exists to withhold.
    #
    # The enforcement is `assert_within_vault`, not this translator; the escape
    # still raises and still refuses the write. Verified by execution that the
    # clause below cannot swallow it: `issubclass(VaultPathEscape, OSError)` is
    # False.
    except (RenameError, OSError) as exc:
        # NARROWED from a blanket `except Exception` carrying a BLE001
        # suppression, which
        # mapped ANYTHING to a 400 — so a TypeError from signature drift, or any
        # genuine bug in the rename path, became a tidy user-facing "move
        # failed" forever, with the `noqa` having already told the linter to
        # stop mentioning it. Same silent-failure shape as the telemetry
        # autocommit bug; cf. `test_warm_up_does_not_swallow_a_real_bug`.
        #
        # BOTH types are required, and `RenameError` alone would be a
        # regression. `vault/rename.py` raises exactly one custom type across
        # eight sites, which makes "narrow to RenameError" look complete — but
        # `apply_rename`'s contract is snapshot, restore, then RE-RAISE THE
        # ORIGINAL error, so a filesystem failure leaves as itself. MEASURED,
        # not inferred: a real write against a read-only file raises
        # `PermissionError`; `isinstance(exc, RenameError)` is False,
        # `isinstance(exc, OSError)` is True. Dropping OSError would turn every
        # disk-full and permission failure from a 400 into a 500.
        #
        # `RenameError` is the domain failure (collision, missing file, wrong
        # tier); `OSError` is the environmental one — genuinely the user's
        # problem and genuinely not a bug. Everything else now propagates as a
        # 500, which is the point.
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
