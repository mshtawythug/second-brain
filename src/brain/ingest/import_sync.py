"""The prune pass behind ``brain ingest-dir --sync``: mirror an external tree's deletions (RM-37).

``ingest-dir`` imports a directory of files as ``manual`` documents, deduped by ``source_path``
(stored resolved and absolute by every file extractor). Re-running it picks up new and changed
files, but a file deleted from the tree left its document behind. This leaf finds those
documents and deletes them through :func:`brain.vault.delete.delete_document`, the one delete
path every surface shares. It imports no CLI and no Typer; ``brain.cli_import_sync`` prints.

A prune deletes, so what it may touch is narrow, and every rule fails toward keeping a row:

- **Candidates** are ``documents.kind = 'ingested'`` rows whose source is ``manual`` (the only
  kind ``ingest-dir`` writes, so a drop-folder or Notion row is never this import's to prune),
  with a ``source_path`` that is absolute, under the resolved root by path COMPONENTS (so
  ``/a/notes`` never matches ``/a/notes-old/…``), not under a brain vault nested inside the
  root, and with a suffix in this run's extension set. A vault-tier row is excluded in SQL and
  is never read.
- **Gone** means ``lstat`` raised ``FileNotFoundError`` or ``NotADirectoryError``. Any other
  ``OSError`` (an unreadable parent, ``ELOOP``, ``EIO``, a stalled network volume) means the
  row cannot be checked: it is kept and counted. A file that exists but cannot be read is
  therefore never deleted. ``--apply`` checks each candidate again just before its delete.
- **Refusals:** a root that does not exist or is not a directory (R1a) and a root inside the
  brain vault, which the vault watcher owns (R2), in :func:`resolve_sync_root`; an empty walk
  while the database holds documents under the root (R1b: an unmounted drive must not prune a
  whole import), and, with ``--apply``, a mass deletion (more than half of the in-scope rows and
  more than :data:`MASS_DELETE_MIN`: a partly-unmounted tree or a moved subfolder), in
  :func:`refusal_for`. ``--allow-empty`` overrides the last two. The mass-deletion guard is
  stricter than the ticket and was added at plan approval
  (``docs/plans/2026-10-30-rm-37-external-vault-sync.md``).

On a case- or normalization-insensitive volume (APFS by default) a path stored as
``/Volumes/Archive/Notes/…`` and a root typed as ``/volumes/archive/notes`` name the same
directory. Components that match only after case folding and NFC normalization count as under
the root when ``os.path.samefile`` says the two prefixes are the same directory; on a
case-sensitive volume they are different directories, ``samefile`` is false, and the scope does
not widen.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import psycopg

from ..errors import BrainError, describe_failure
from ..vault.delete import DeleteReport, DocumentGraphSyncer, delete_document

__all__ = [
    "MASS_DELETE_MIN",
    "ImportSyncRefused",
    "PruneCandidate",
    "PruneOutcome",
    "PrunePlan",
    "PruneReport",
    "apply_prune",
    "is_under",
    "plan_prune",
    "refusal_for",
    "resolve_sync_root",
]

#: The mass-deletion guard fires only above this many candidates, so pruning a handful of
#: files from a small tree never needs ``--allow-empty``.
MASS_DELETE_MIN = 5

_GONE_ERRORS = (FileNotFoundError, NotADirectoryError)

# Read-only. ``documents.kind = 'ingested'`` is refusal R3: a vault-tier row never reaches
# Python. ``sources.kind = 'manual'`` is decision D1. Containment, the vault exclusion and the
# extension filter are path rules, applied in Python by :func:`plan_prune`.
_CANDIDATE_SQL = """
SELECT d.id::text, d.title, d.source_path
FROM documents d
JOIN sources s ON s.id = d.source_id
WHERE d.kind = 'ingested'
  AND s.kind = 'manual'
  AND d.source_path IS NOT NULL
ORDER BY d.source_path, d.id
"""

LstatFn = Callable[[str], os.stat_result]
SamefileFn = Callable[[Any, Any], bool]
DeleteFn = Callable[..., DeleteReport]


class ImportSyncRefused(BrainError):
    """The prune refused to run; the message says why and what overrides it."""


@dataclass(frozen=True)
class PruneCandidate:
    """A document whose file is gone from under the root."""

    document_id: str
    title: str
    source_path: str
    relative_path: str
    """``source_path`` relative to the root, for display."""


@dataclass(frozen=True)
class PrunePlan:
    """What a prune would delete, read without deleting anything."""

    root: Path
    in_scope: int
    """Rows under the root that this run governs, present, gone or uncheckable."""
    candidates: tuple[PruneCandidate, ...]
    unverifiable: int
    """In-scope rows whose file could not be checked; kept."""
    under_vault: int
    """Rows under a brain vault nested inside the root; skipped, never counted in scope."""


@dataclass(frozen=True)
class PruneOutcome:
    """What :func:`apply_prune` did with one candidate."""

    candidate: PruneCandidate
    action: str
    """``deleted``, ``kept`` (back on disk), ``unchecked`` (no longer checkable; kept),
    ``missing`` (the row was already deleted) or ``failed``."""
    detail: str | None = None
    """For ``failed``: the exception by class only (:func:`brain.errors.describe_failure`)."""


@dataclass(frozen=True)
class PruneReport:
    """Outcome of :func:`apply_prune`, one entry per candidate, in plan order."""

    outcomes: tuple[PruneOutcome, ...]

    @property
    def deleted(self) -> int:
        return sum(o.action == "deleted" for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.action == "failed" for o in self.outcomes)


def _fold(part: str) -> str:
    return unicodedata.normalize("NFC", part).casefold()


def _has_prefix(
    parts: tuple[str, ...], prefix: Path, *, samefile: SamefileFn, allow_equal: bool
) -> bool:
    """Whether ``parts`` lies under ``prefix`` (or is it, with ``allow_equal``), by components."""
    n = len(prefix.parts)
    if len(parts) < n or (len(parts) == n and not allow_equal):
        return False
    head = parts[:n]
    if head == prefix.parts:
        return True
    if tuple(_fold(p) for p in head) != tuple(_fold(p) for p in prefix.parts):
        return False
    try:
        return samefile(PurePath(*head), prefix)
    except OSError:
        return False


def is_under(
    stored: str, root: Path, *, samefile: SamefileFn = os.path.samefile
) -> bool:
    """Whether the stored ``source_path`` lies strictly under ``root`` (module docstring).

    ``root`` is resolved. A relative path, or one holding ``..``, is never under it.
    """
    path = PurePath(stored)
    if not path.is_absolute() or ".." in path.parts:
        return False
    return _has_prefix(path.parts, root, samefile=samefile, allow_equal=False)


def resolve_sync_root(directory: Path, *, vault_root: Path) -> Path:
    """The resolved root to prune under, or :class:`ImportSyncRefused` (R1a, R2)."""
    try:
        root = directory.resolve(strict=True)
    except OSError:
        raise ImportSyncRefused(
            f"{directory} does not exist (an unmounted drive must not prune an import)"
        ) from None
    if not root.is_dir():
        raise ImportSyncRefused(f"{directory} is not a directory")
    vault = vault_root.resolve()
    if _has_prefix(root.parts, vault, samefile=os.path.samefile, allow_equal=True):
        raise ImportSyncRefused(
            f"{directory} is inside the brain vault ({vault_root}); "
            "the vault watcher owns that tree"
        )
    return root


def _probe(path: str, lstat: LstatFn) -> str:
    """``gone``, ``present`` or ``unknown`` (module docstring)."""
    try:
        lstat(path)
    except _GONE_ERRORS:
        return "gone"
    except OSError:
        return "unknown"
    return "present"


def plan_prune(
    conn: psycopg.Connection[Any],
    *,
    root: Path,
    vault_root: Path,
    extensions: Iterable[str],
    lstat: LstatFn = os.lstat,
) -> PrunePlan:
    """Read which documents under ``root`` a prune would delete. Writes nothing.

    ``root`` comes from :func:`resolve_sync_root`. ``extensions`` are this run's (``--ext``, or
    the walk's default set), lower-case with the dot.
    """
    wanted = {e.lower() for e in extensions}
    vault = vault_root.resolve()
    in_scope = unverifiable = under_vault = 0
    candidates: list[PruneCandidate] = []
    for document_id, title, source_path in conn.execute(_CANDIDATE_SQL).fetchall():
        if not is_under(source_path, root) or PurePath(source_path).suffix.lower() not in wanted:
            continue
        if is_under(source_path, vault):
            under_vault += 1
            continue
        in_scope += 1
        state = _probe(source_path, lstat)
        if state == "unknown":
            unverifiable += 1
        elif state == "gone":
            relative = PurePath(*PurePath(source_path).parts[len(root.parts):])
            candidates.append(
                PruneCandidate(
                    document_id=document_id,
                    title=title,
                    source_path=source_path,
                    relative_path=relative.as_posix(),
                )
            )
    return PrunePlan(
        root=root,
        in_scope=in_scope,
        candidates=tuple(candidates),
        unverifiable=unverifiable,
        under_vault=under_vault,
    )


def refusal_for(
    plan: PrunePlan, *, supported_files: int, apply: bool, allow_empty: bool
) -> str | None:
    """Why this prune must not run (R1b, the mass-deletion guard), or ``None``.

    ``supported_files`` is how many files the walk found. Both refusals yield to
    ``allow_empty``; the mass-deletion guard applies only to ``--apply``.
    """
    if allow_empty:
        return None
    if supported_files == 0 and plan.in_scope > 0:
        return (
            f"no supported files under {plan.root}, but {plan.in_scope} document(s) were "
            "imported from it. Is the drive mounted? Pass --allow-empty to prune them anyway."
        )
    removed = len(plan.candidates)
    if apply and removed > MASS_DELETE_MIN and 2 * removed > plan.in_scope:
        return (
            f"{removed} of {plan.in_scope} documents under {plan.root} would be removed, "
            "more than half. Check the tree is complete, or pass --allow-empty to delete them."
        )
    return None


def apply_prune(
    conn: psycopg.Connection[Any],
    plan: PrunePlan,
    *,
    vault_root: Path,
    graph_syncer: DocumentGraphSyncer | None,
    lstat: LstatFn = os.lstat,
    delete: DeleteFn | None = None,
) -> PruneReport:
    """Delete each candidate whose file is still gone; one failure does not stop the rest.

    ``conn`` is in autocommit mode, so each delete stands alone. ``delete`` defaults to
    :func:`brain.vault.delete.delete_document`, resolved at call time.
    """
    delete_fn = delete if delete is not None else delete_document
    outcomes: list[PruneOutcome] = []
    for candidate in plan.candidates:
        state = _probe(candidate.source_path, lstat)
        if state != "gone":
            action = "kept" if state == "present" else "unchecked"
            outcomes.append(PruneOutcome(candidate=candidate, action=action))
            continue
        try:
            delete_fn(
                conn,
                document_id=candidate.document_id,
                vault_root=vault_root,
                graph_syncer=graph_syncer,
            )
        except ValueError:
            outcomes.append(PruneOutcome(candidate=candidate, action="missing"))
            continue
        except (psycopg.Error, OSError) as exc:
            # The row may already be gone when the mirror unlink fails; either way the user
            # is told, by class only, and the run exits non-zero.
            outcomes.append(
                PruneOutcome(candidate=candidate, action="failed", detail=describe_failure(exc))
            )
            continue
        outcomes.append(PruneOutcome(candidate=candidate, action="deleted"))
    return PruneReport(outcomes=tuple(outcomes))
