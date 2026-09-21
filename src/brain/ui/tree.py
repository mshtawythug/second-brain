"""Pure: flat ``vault_path`` strings → the nested tree the left rail renders.

No I/O, no database, no config — the whole module is one fold over a list of
rows, which is why it carries a 95% coverage target and needs no fixtures.

**Why the tree is database-derived rather than a filesystem walk.** A walk
would have to re-implement ``brain.vault.sync._walk_vault``'s exclusion rules
(``_templates/``, ``_attachments/``, hidden path components) and would arrive
with no document ids, forcing a second lookup on every click. Accepted cost: a
``.md`` file created on disk but not yet synced does not appear. The vault
watcher normally closes that gap within seconds, and the empty-tree state names
``brain vault sync``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Vault paths are stored POSIX-style in ``documents.vault_path`` regardless of
#: host OS, so the separator is a constant rather than ``os.sep``.
SEP = "/"


@dataclass(frozen=True)
class TreeNote:
    """One leaf: a document, carrying enough to render and open it."""

    id: str
    title: str
    path: str
    draft: bool
    tier: str
    date: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "draft": self.draft,
            "tier": self.tier,
            "date": self.date,
        }


@dataclass
class TreeNode:
    """One folder. ``children`` are sub-folders, ``notes`` are leaves."""

    name: str
    path: str
    children: dict[str, TreeNode] = field(default_factory=dict)
    notes: list[TreeNote] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Serialize depth-first, folders before notes.

        Folders are sorted case-insensitively and emitted before notes at every
        level, so the rail's ordering is stable across reloads and does not
        depend on the SQL row order. Two folders with the same name at
        different depths cannot collide because each node is keyed inside its
        own parent's ``children`` dict, never in a flat registry.

        **Counts are RECURSIVE**, over every leaf in the subtree rather than
        over direct children only. The rail renders folders collapsed, so the
        number beside a closed folder is precisely the thing the user opened it
        to learn; a direct-children count would report 1 for a folder holding
        three notes and be wrong exactly when it is most load-bearing.

        The counts are also split by tier — ``vault_count`` +
        ``ingested_count`` == ``note_count`` at every node, by construction.
        That split is what lets the client's "show ingested" toggle recompute
        every folder's number from the payload it already has, instead of
        refetching the whole tree on a checkbox.

        The total is named ``note_count`` and not ``count`` deliberately:
        ``routes_tree`` writes its own ``count`` (the row count) onto the ROOT
        payload after this returns, so a key named ``count`` here would be
        silently clobbered at the root and survive everywhere else — the worst
        of both, and invisible until a client trusted it.
        """
        children = [
            child.to_payload()
            for _, child in sorted(
                self.children.items(), key=lambda kv: kv[0].casefold()
            )
        ]
        note_count = len(self.notes) + sum(c["note_count"] for c in children)
        # Anything not explicitly ``ingested`` counts as vault: ``build_tree``
        # already defaults a null ``kind`` to ``"vault"``, and an unexpected
        # tier landing in the vault bucket keeps the two halves summing to the
        # total rather than quietly losing a leaf from both.
        ingested_count = sum(
            1 for note in self.notes if note.tier == "ingested"
        ) + sum(c["ingested_count"] for c in children)
        return {
            "name": self.name,
            "path": self.path,
            "children": children,
            "notes": [
                note.to_payload()
                for note in sorted(self.notes, key=lambda n: n.title.casefold())
            ],
            "note_count": note_count,
            "vault_count": note_count - ingested_count,
            "ingested_count": ingested_count,
        }


def _iso(value: Any) -> str | None:
    """ISO-8601 for a datetime, ``None`` for a null, ``str`` for anything else."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_tree(rows: list[tuple[Any, ...]]) -> TreeNode:
    """Fold ``(id, title, vault_path, kind, draft, date)`` rows into a tree.

    Rows with a null or empty ``vault_path`` are skipped rather than raising:
    the caller's SQL already filters them out, and a defensive skip here means
    one unexported row can never blank the entire left rail.

    A path with no separator (``"scratch.md"``) becomes a note on the returned
    root, so vault-root files are not silently dropped.
    """
    root = TreeNode(name="", path="")
    for row in rows:
        doc_id, title, vault_path, kind, draft, date = row
        if not vault_path:
            continue
        parts = [p for p in str(vault_path).split(SEP) if p]
        if not parts:
            continue
        *folders, filename = parts

        node = root
        walked: list[str] = []
        for folder in folders:
            walked.append(folder)
            if folder not in node.children:
                node.children[folder] = TreeNode(name=folder, path=SEP.join(walked))
            node = node.children[folder]

        node.notes.append(
            TreeNote(
                id=str(doc_id),
                title=str(title) if title else filename,
                path=str(vault_path),
                draft=bool(draft),
                tier=str(kind) if kind else "vault",
                date=_iso(date),
            )
        )
    return root
