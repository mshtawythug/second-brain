"""Vault model — `.md` files on disk are the source of truth for vault notes.

Phase 1 ships :func:`init_vault` (one-shot directory + template scaffold) and
:func:`export_vault` (DB → vault dump). Later phases add sync, link parsing,
authoring commands, and a Quartz renderer on top of the same module layout.
"""
from dataclasses import dataclass, field
from pathlib import Path

from .templates import (
    DAILY_TEMPLATE,
    INGESTED_README,
    NOTE_TEMPLATE,
    VAULT_README,
)

# Subdirectories created (or ensured) by ``brain vault init``. Order is
# preserved in the summary so output stays deterministic across runs.
VAULT_SUBDIRS: tuple[str, ...] = (
    "_templates",
    "_attachments",
    "_ingested",
    "_ingested/krisp",
    "_ingested/slack",
    "_ingested/gmail",
    "_ingested/manual",
    "daily",
)

# (relative path, content) pairs written if the file does not already exist.
# Tuple-of-tuples (not dict) so iteration order is part of the contract.
VAULT_TEMPLATE_FILES: tuple[tuple[str, str], ...] = (
    ("_templates/daily.md", DAILY_TEMPLATE),
    ("_templates/note.md", NOTE_TEMPLATE),
    ("_ingested/README.md", INGESTED_README),
    ("README.md", VAULT_README),
)


@dataclass
class VaultInitSummary:
    """What ``init_vault`` actually did, for the CLI to print.

    ``created_dirs`` lists subdirs that didn't exist before this call;
    ``existing_dirs`` lists ones that did. ``written_files`` and
    ``preserved_files`` are the analogous lists for templates: a template
    is only ever written when absent (we never overwrite user edits).
    """

    vault_path: Path
    created_dirs: list[str] = field(default_factory=list)
    existing_dirs: list[str] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)
    preserved_files: list[str] = field(default_factory=list)


def init_vault(vault_path: Path) -> VaultInitSummary:
    """Create / ensure the vault folder structure and default templates.

    Idempotent — safe to re-run. Never overwrites existing files; user edits
    to ``_templates/*.md`` or ``README.md`` survive every subsequent
    ``brain vault init`` call.

    Creates the vault root if absent. Creates each subdir under
    :data:`VAULT_SUBDIRS` with ``mkdir -p`` semantics (no error if present).
    Writes each template in :data:`VAULT_TEMPLATE_FILES` only if its target
    path does not yet exist.
    """
    summary = VaultInitSummary(vault_path=vault_path)
    vault_path.mkdir(parents=True, exist_ok=True)

    for relative in VAULT_SUBDIRS:
        target = vault_path / relative
        if target.is_dir():
            summary.existing_dirs.append(relative)
        else:
            target.mkdir(parents=True, exist_ok=True)
            summary.created_dirs.append(relative)

    for relative, content in VAULT_TEMPLATE_FILES:
        target = vault_path / relative
        if target.exists():
            summary.preserved_files.append(relative)
        else:
            target.write_text(content, encoding="utf-8")
            summary.written_files.append(relative)

    return summary
