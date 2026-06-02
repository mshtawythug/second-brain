"""Standalone builder for vault-tier notes: render frontmatter, write, and sync.

Extracted from ``brain.cli`` so callers (e.g. the tacit-knowledge elicitation
session loop) can author vault notes WITHOUT importing the Typer CLI — which
would create an import cycle (``cli`` already imports ``brain.vault``).

The heavy collaborators (``sync_one_file`` and ``make_embedder``) are imported
lazily inside the functions on purpose: importing them at module load would
re-enter the ``brain.ingest`` package while it is only partially initialized
(``brain.ingest`` imports ``brain.vault.export``, which imports this package),
raising ``ImportError``. The module-level imports below are all cycle-safe
leaves.
"""
from __future__ import annotations

import uuid
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import VaultNoteSyncError
from ..tags import normalize_tags
from .frontmatter import dump_frontmatter, parse_frontmatter
from .slug import slugify
from .templates import render_template

if TYPE_CHECKING:
    from collections.abc import Sequence

    import psycopg

    from ..config import Config
    from ..ingest import Embedder


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder. Indirected so tests can substitute a fake.

    Returns the :class:`Embedder` Protocol — callers should not depend on the
    concrete backend. The ``make_embedder`` import is deferred to avoid a
    load-time import cycle (see the module docstring).
    """
    from ..embeddings import make_embedder

    return make_embedder(cfg)


def _build_note_text(
    template_text: str,
    *,
    title: str,
    tags: list[str],
    today: date_cls,
    now: datetime,
    body: str | None = None,
) -> tuple[str, str]:
    """Render a template + force the brain-canonical frontmatter fields.

    Returns ``(file_text, document_id)``. The template's body is preserved
    verbatim unless ``body`` is provided, in which case the rendered template's
    body is replaced by ``body`` (used when a caller supplies authored content
    instead of letting the user fill the template in ``$EDITOR``). Only the
    frontmatter is rewritten so the brain-managed fields (``id``, ``title``,
    ``created``, ``updated``, ``kind``, ``tags``) are authoritative regardless
    of what the template author wrote.

    A user-template ``title:`` line is intentionally ignored — the caller's
    ``title`` argument wins. That's the contract: if you wanted the template
    to control title, you'd be using a daily template (which derives title
    from the date passed in via ``vars``).
    """
    rendered = render_template(
        template_text,
        {
            "title": title,
            "date": today.isoformat(),
            "datetime": now.isoformat(timespec="seconds"),
            "slug": slugify(title),
        },
    )

    # Try to parse the rendered template's frontmatter; if it's malformed or
    # missing entirely, build a fresh header. Either way the brain-canonical
    # fields are forced — the template's body is what we preserve.
    try:
        existing_fields, parsed_body = parse_frontmatter(rendered)
    except (ValueError, yaml.YAMLError):
        # Per the spec's risk: a malformed template shouldn't crash. Fall
        # back to a fresh frontmatter + the raw rendered text as the body.
        existing_fields = {}
        parsed_body = rendered

    note_body = parsed_body if body is None else body

    document_id = str(uuid.uuid4())
    iso_now = now.isoformat(timespec="seconds")
    fields: dict[str, Any] = dict(existing_fields)
    # Brain-managed fields override the template's choices in a fixed order
    # so frontmatter ordering is stable across runs.
    fields["id"] = document_id
    fields["title"] = title
    fields["created"] = iso_now
    fields["updated"] = iso_now
    fields["kind"] = "vault"
    if tags:
        fields["tags"] = list(tags)
    elif "tags" not in fields:
        fields["tags"] = []

    return dump_frontmatter(fields, note_body), document_id


def _unique_target(base_dir: Path, slug: str) -> Path:
    """Return a non-existing ``<base_dir>/<slug>.md`` path, suffixing on collision.

    ``brain note new`` guards against collisions before delegating here, but the
    elicitation ``_codify`` path calls :func:`create_vault_note` directly. Without
    this guard a draft whose title slugifies to an existing note's slug would
    silently overwrite that note (data loss). On collision we append ``-2``,
    ``-3``, … until a free path is found so the existing note is never clobbered.
    """
    target = base_dir / f"{slug}.md"
    if not target.exists():
        return target
    suffix = 2
    while True:
        candidate = base_dir / f"{slug}-{suffix}.md"
        if not candidate.exists():
            return candidate
        suffix += 1


def create_vault_note(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    vault_path: Path,
    title: str,
    body: str | None = None,
    tags: Sequence[str] = (),
    template: str = "note",
    folder: str = "",
    embedder: Embedder | None = None,
) -> str:
    """Author a vault-tier note: render, write to disk, sync, return its id.

    Resolves ``<vault>/_templates/<template>.md``, renders it (forcing the
    brain-canonical frontmatter) with ``body`` substituted for the template's
    placeholder body when provided, writes the file under ``vault_path`` (in
    ``folder`` if given), then runs a single-file sync so the note is indexed
    as ``kind='vault'`` (the tier ``sync_one_file`` infers for any file outside
    ``_ingested/``).

    ``embedder`` lets a caller inject a pre-built embedder (``brain note new``
    passes the one it built so test fakes wired onto
    ``brain.cli._build_embedder`` still apply); when ``None`` the configured
    embedder is built from ``cfg``.

    Raises :class:`~brain.errors.VaultNoteSyncError` if the template is missing
    or the sync reports per-file errors (the file is left on disk on a sync
    error). Returns the generated ``document_id``.
    """
    from .sync import sync_one_file

    template_path = vault_path / "_templates" / f"{template}.md"
    if not template_path.is_file():
        raise VaultNoteSyncError(
            [(template_path, f"template {template!r} not found")]
        )
    template_text = template_path.read_text(encoding="utf-8")

    now = datetime.now()
    today = now.date()
    file_text, document_id = _build_note_text(
        template_text,
        title=title,
        tags=normalize_tags(list(tags)),
        today=today,
        now=now,
        body=body,
    )

    base_dir = vault_path / folder if folder else vault_path
    # Path-traversal guard: a ``folder`` containing ``..`` segments or an
    # absolute path would resolve outside the vault root and author files in an
    # arbitrary filesystem location. Reject before any mkdir / write so nothing
    # is created on violation.
    vault_root_resolved = vault_path.resolve()
    base_dir_resolved = base_dir.resolve()
    if not base_dir_resolved.is_relative_to(vault_root_resolved):
        raise VaultNoteSyncError(
            [(base_dir, f"folder {folder!r} escapes the vault root")]
        )
    base_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(base_dir, slugify(title))
    target.write_text(file_text, encoding="utf-8")

    active_embedder = embedder if embedder is not None else _build_embedder(cfg)
    report = sync_one_file(
        conn,
        embedder=active_embedder,
        vault_path=vault_path,
        file_path=target,
        owner_participants=cfg.owner_participants,
    )
    if report.errors:
        raise VaultNoteSyncError(report.errors)
    return document_id
