"""Vault note rename: title rewrite + reference refactor + atomic apply.

A rename is a multi-file destructive operation — the renamed note's frontmatter
gets a new ``title`` and (usually) the file moves to a new slug-based path,
and every other note in the vault that references the old title via
``[[old-title]]`` (or ``[[old-title|alias]]``, ``[[old-title#heading]]``,
``![[old-title]]``) is rewritten to point at the new title.

The module is split into two halves so the CLI can offer ``--dry-run``:

- :func:`plan_rename` reads the DB + walks the vault, returns a fully
  populated :class:`RenameOp` describing every change the apply phase
  would make. It writes nothing to disk and nothing to the DB.
- :func:`apply_rename` snapshots every file it's about to touch into a
  tempdir, then performs the writes. On any exception the snapshots are
  copied back, leaving the vault in its pre-call state. The temp dir's
  path is logged on a hard failure so the user can recover manually.

Reference matching is delegated to
:func:`brain.vault.links.iter_wiki_links_with_spans` — that's the same parser
the sync engine uses, so code-fence / inline-code / escaped-bracket skipping
behaves identically in both code paths. Don't reach for a regex of your own
here; the parser already does the right thing.
"""
import logging
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ..ingest import Embedder
from .frontmatter import dump_frontmatter, parse_frontmatter
from .links import iter_wiki_links_with_spans
from .slug import slugify
from .sync import SyncReport, sync_one_file

logger = logging.getLogger(__name__)


class RenameError(Exception):
    """Rename cannot proceed (collision, doc not found, etc.).

    The CLI catches this and surfaces ``str(e)`` as the user-facing diagnostic;
    the rename never partially applies when this is raised from
    :func:`plan_rename`.
    """


@dataclass(frozen=True)
class ReferenceMatch:
    """One ``[[old-title]]`` (or variant) found in another vault file.

    ``span`` is the byte range in ``file_path``'s text that the link
    occupies. ``new_text`` is what it'll become after the rename — the
    rewriter just splices ``new_text`` over the ``[span]`` slice.

    ``line_no`` is 1-indexed and surfaced for human-readable diagnostics
    (``--dry-run`` output, error messages); the rewriter doesn't use it.
    """

    file_path: Path
    line_no: int
    old_text: str
    new_text: str
    span: tuple[int, int]


@dataclass(frozen=True)
class RenameOp:
    """The fully resolved plan for a single ``brain note rename``.

    Constructed by :func:`plan_rename`; consumed by :func:`apply_rename`.
    Frozen so callers can pass it around (e.g. between dry-run print and
    real apply) without worrying about mutation.

    ``references`` is grouped per-file in document order — when a single
    file has three matches, all three appear consecutively. The apply
    phase relies on this ordering to splice safely (writes back-to-front
    so earlier spans stay valid).
    """

    document_id: str
    old_title: str
    new_title: str
    old_path: Path
    new_path: Path
    references: tuple[ReferenceMatch, ...]


@dataclass
class RenameReport:
    """Outcome of :func:`apply_rename`.

    Counters mirror what the CLI prints back to the user; ``sync_report``
    carries the post-rename single-file sync results (the renamed file's
    DB row + chunks + links are re-indexed in place).
    """

    file_renamed: bool = False
    files_rewritten: int = 0
    references_rewritten: int = 0
    sync_report: SyncReport | None = None
    errors: list[str] = field(default_factory=list)


def plan_rename(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    document_id: str,
    new_title: str,
) -> RenameOp:
    """Build a :class:`RenameOp` describing the rename.

    Reads:
    - The doc row (must exist, must be ``kind='vault'``, must have a
      ``vault_path``).
    - Every ``.md`` file in the vault, scanned for ``[[old-title]]``
      references via the wiki-link parser (so code fences / inline code /
      escaped brackets are skipped automatically).

    Errors:
    - :class:`RenameError` if the doc isn't found, isn't vault-tier, or
      has no ``vault_path`` on disk.
    - :class:`RenameError` on slug collision — the new path would overwrite
      another file that already exists.
    - :class:`RenameError` if the new title is empty / whitespace.
    """
    if not new_title.strip():
        raise RenameError("new title must not be empty")
    new_title = new_title.strip()

    row = conn.execute(
        "SELECT title, vault_path, kind FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise RenameError(f"document not found: {document_id}")
    old_title, vault_path_value, kind = row
    if kind != "vault":
        raise RenameError(
            f"document is {kind!r}, not 'vault' — only vault-tier docs can be renamed"
        )
    if not vault_path_value:
        raise RenameError(
            "document has no vault_path on disk; run `brain vault sync` first"
        )

    old_relative = Path(vault_path_value)
    old_abs = vault_path / old_relative
    if not old_abs.is_file():
        raise RenameError(
            f"vault file is missing on disk: {vault_path_value} "
            "(run `brain vault sync --prune` to clean up the DB row)"
        )

    new_slug = slugify(new_title)
    new_relative = old_relative.with_name(f"{new_slug}.md")
    new_abs = vault_path / new_relative

    # Slug collision: the new path collides with a different existing file.
    # Same-file (no-op rename) is allowed — the title may change without the
    # filename slug changing.
    if new_abs.exists() and new_abs.resolve() != old_abs.resolve():
        raise RenameError(
            f"target path already exists: {new_relative.as_posix()} — "
            "pick a more distinctive title"
        )

    # ``old_relative`` (e.g. ``Path("target.md")``) gives the rename
    # scanner the path stem it needs to match path-form references like
    # ``[[target|Target]]`` produced by the post-sync wiki-link rewriter
    # (see ``brain.vault.link_rewrite``). Without this, references in
    # path form would be missed and the rename would leave broken links
    # behind.
    old_path_stem = old_relative.with_suffix("").as_posix()
    references = collect_references(
        vault_path,
        old_targets=[(old_title, old_path_stem)],
        new_title=new_title,
    )

    return RenameOp(
        document_id=document_id,
        old_title=old_title,
        new_title=new_title,
        old_path=old_abs,
        new_path=new_abs,
        references=tuple(references),
    )


def apply_rename(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    vault_path: Path,
    op: RenameOp,
) -> RenameReport:
    """Apply ``op``: rewrite references, update frontmatter, move the file.

    Atomicity contract:

    1. Snapshot every file we're about to write into a fresh tempdir
       (``brain-rename-<uuid>/``).
    2. Perform every write under a try/except.
    3. On any exception, copy snapshots back to their original paths and
       re-raise the original error after logging the snapshot path so the
       user can recover manually if the restore itself failed.
    4. On success, the snapshot dir is deleted.

    The renamed file's DB row + chunks + links are re-indexed via
    :func:`sync_one_file` after the disk writes commit. If the target
    file references survive the rename (e.g. ``[[person-x|original display]]``
    becomes ``[[person-x Q1|original display]]``) the sync will pick them up
    on the next full vault pass; this helper deliberately doesn't walk
    the entire vault.
    """
    report = RenameReport()
    files_to_rewrite = _group_by_file(op.references)
    snapshot_targets: dict[Path, bytes] = {}

    # Always snapshot the source file (we update its frontmatter + may move it)
    # plus every file with rewritten references. Reading bytes (not text)
    # preserves any oddball encoding the user has — restore is byte-exact.
    snapshot_targets[op.old_path] = op.old_path.read_bytes()
    for path in files_to_rewrite:
        if path == op.old_path:
            continue
        snapshot_targets[path] = path.read_bytes()

    backup_dir = Path(tempfile.mkdtemp(prefix="brain-rename-"))
    try:
        for path, data in snapshot_targets.items():
            backup_path = _backup_path_for(backup_dir, vault_path, path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(data)

        # Step 1: rewrite references in OTHER files first. We do this before
        # touching the source file so a failure here leaves the source intact
        # for restore.
        for path, matches in files_to_rewrite.items():
            if path == op.old_path:
                # Source-file references handled below alongside the
                # frontmatter rewrite — keeps the source's writes coherent.
                continue
            new_text = apply_matches_to_text(
                path.read_text(encoding="utf-8"),
                matches,
            )
            path.write_text(new_text, encoding="utf-8")
            report.files_rewritten += 1
            report.references_rewritten += len(matches)

        # Step 2: rewrite the source file. Two changes happen here:
        #   a. Any in-body references to the doc's own title are rewritten
        #      (rare but possible — a note that links to itself by title).
        #   b. Frontmatter ``title`` is updated and ``updated`` timestamped.
        text = op.old_path.read_text(encoding="utf-8")
        source_matches = files_to_rewrite.get(op.old_path, [])
        if source_matches:
            text = apply_matches_to_text(text, source_matches)
            report.references_rewritten += len(source_matches)
        text = _rewrite_source_frontmatter(text, new_title=op.new_title)

        # Step 3: write the source. If the target path differs (slug change),
        # write the new file then unlink the old. Same path → in-place write.
        if op.new_path.resolve() != op.old_path.resolve():
            op.new_path.parent.mkdir(parents=True, exist_ok=True)
            op.new_path.write_text(text, encoding="utf-8")
            op.old_path.unlink()
            report.file_renamed = True
        else:
            op.old_path.write_text(text, encoding="utf-8")

        # Source counted in files_rewritten only when something actually
        # changed (matches present OR title changed — title change always
        # changes frontmatter, so always count when we got here).
        report.files_rewritten += 1
    except Exception as exc:
        # Restore from snapshots — best effort. If restore itself fails we
        # log loudly so the user can recover from the backup_dir manually.
        logger.error(
            "vault rename: writes failed (%s); restoring from snapshot at %s",
            exc,
            backup_dir,
        )
        _restore_from_backup(backup_dir, vault_path, snapshot_targets.keys())
        # If the source was moved before the failure, the new path may also
        # exist — tear it down so the restore lands cleanly.
        if (
            op.new_path.resolve() != op.old_path.resolve()
            and op.new_path.exists()
        ):
            try:
                op.new_path.unlink()
            except OSError as cleanup_exc:
                logger.error(
                    "vault rename: failed to remove partially-written %s: %s "
                    "(snapshot at %s)",
                    op.new_path,
                    cleanup_exc,
                    backup_dir,
                )
        raise
    else:
        # Success — clean up the snapshot dir.
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Re-index the renamed file. We pass the new path so sync sees the new
    # frontmatter title and re-resolves any links targeted at it.
    report.sync_report = sync_one_file(
        conn,
        embedder=embedder,
        vault_path=vault_path,
        file_path=op.new_path,
    )
    return report


# ---------------------------------------------------------------------------
# Public helpers (also used by ``scripts/collapse_gmail_threads.py``).
# ---------------------------------------------------------------------------


def collect_references(
    vault_path: Path,
    *,
    old_targets: list[tuple[str, str]],
    new_title: str,
) -> list[ReferenceMatch]:
    """Walk the vault, returning every ``[[old-title]]`` / ``[[old-path]]`` reference.

    ``old_targets`` is a list of ``(old_title, old_path_stem)`` pairs. A wiki-link
    matches when its target equals one of the ``old_title`` values
    (case-insensitive) OR equals one of the ``old_path_stem`` values
    (case-sensitive POSIX path). Each match's rewrite uses the matched pair's
    ``old_title`` so the synthetic-display drop in :func:`_rewrite_link_text`
    fires correctly even when several old targets share one new title (the
    Gmail-thread collapse use case in
    :mod:`scripts.collapse_gmail_threads`).

    The walk includes the source file itself (a note may reference its own
    title) but excludes ``_templates/`` and ``_attachments/`` — those don't
    belong in the link graph and rewriting their content would mutate
    user-owned scaffolding. ``_ingested/`` IS walked because users may
    legitimately reference an ingested artifact's title from a vault note,
    but Phase 2's sync gives ingested-tier files their own DB rows so any
    references inside them are part of the live graph.

    Title comparison is case-insensitive, matching the resolver's title
    resolution rule (``LOWER(title) = LOWER(?)``). Path comparison is
    case-sensitive (vault paths are POSIX strings), matching
    :func:`brain.vault.resolver._resolve_by_vault_path` so references
    rewritten by :func:`brain.vault.link_rewrite.rewrite_wiki_links` into
    ``[[<vault-root-relative-path>|<display>]]`` form are still caught
    by this scan.
    """
    matches: list[ReferenceMatch] = []
    # Build (lower_title, path_stem) lookup tuples once. Single-element
    # ``old_targets`` is the rename use case; multi-element is the
    # gmail-thread collapse use case (one new merged thread doc replaces
    # N per-message docs).
    targets: list[tuple[str, str, str]] = [
        (old_title.lower(), old_path_stem, old_title)
        for old_title, old_path_stem in old_targets
    ]
    for path in sorted(vault_path.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(vault_path)
        except ValueError:
            continue
        first = relative.parts[0] if relative.parts else ""
        if first in {"_templates", "_attachments"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # The wiki-link parser receives the body, but the rewrite must
        # land at the right offset in the WHOLE file (frontmatter included).
        # Parse frontmatter to compute its byte length, then run the link
        # scanner over the body and shift offsets back into whole-file
        # coordinates. This skips ``[[X]]`` written inside the YAML header
        # (which is intentional: the resolver doesn't follow those either).
        try:
            _, body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            # Malformed frontmatter — sync would skip this file too. Don't
            # rewrite it; we'd be guessing where the body starts.
            continue
        body_offset = len(text) - len(body)
        for parsed, start, end in iter_wiki_links_with_spans(body):
            if parsed.target_type != "title":
                continue
            target = parsed.target_value
            target_lower = target.lower()
            matched_old_title: str | None = None
            for lower_title, path_stem, original_title in targets:
                if target_lower == lower_title or target == path_stem:
                    matched_old_title = original_title
                    break
            if matched_old_title is None:
                continue
            old_text = body[start:end]
            new_text = _rewrite_link_text(
                old_text,
                new_title=new_title,
                embed=parsed.kind == "embed",
                old_title=matched_old_title,
            )
            absolute_start = body_offset + start
            absolute_end = body_offset + end
            line_no = text.count("\n", 0, absolute_start) + 1
            matches.append(
                ReferenceMatch(
                    file_path=path,
                    line_no=line_no,
                    old_text=old_text,
                    new_text=new_text,
                    span=(absolute_start, absolute_end),
                )
            )
    return matches


def _rewrite_link_text(
    old_text: str,
    *,
    new_title: str,
    embed: bool,
    old_title: str | None = None,
) -> str:
    """Rewrite a single ``[[...]]`` (or ``![[...]]``) match's title segment.

    Preserves:
    - The embed marker (``!``) if present.
    - Pipe alias (``|display``) — only the title part is replaced. EXCEPT:
      when ``display`` equals ``old_title`` (case-insensitive), the alias
      is treated as the synthetic display added by
      :func:`brain.vault.link_rewrite.rewrite_wiki_links` for the
      bare-title case (``[[Old]]`` → ``[[<path>|Old]]``) and is dropped
      so the rename's output reads ``[[New]]`` rather than
      ``[[New|Old]]``. A user-chosen alias (``[[Old|something else]]``)
      is preserved verbatim.
    - Heading anchor (``#heading``) — preserved as-is on the new title.

    Examples:
    - ``[[Old]]`` → ``[[New]]``
    - ``[[Old|alias]]`` → ``[[New|alias]]``
    - ``[[Old|Old]]`` → ``[[New]]`` (synthetic-display drop)
    - ``[[old-path|Old]]`` → ``[[New]]`` (synthetic-display drop after
      path-form match)
    - ``[[Old#heading]]`` → ``[[New#heading]]``
    - ``![[Old]]`` → ``![[New]]``
    """
    prefix = "![[" if embed else "[["
    assert old_text.startswith(prefix), f"unexpected match shape: {old_text!r}"
    inner = old_text[len(prefix) : -2]  # strip prefix + ``]]``
    target_part, sep, display = inner.partition("|")
    _, hash_sep, heading = target_part.partition("#")
    # The title is the first segment up to ``#`` or ``|`` — discard whatever
    # whitespace surrounded it and substitute. The user's spacing inside
    # ``[[ Title ]]`` is normalized away on rewrite (the resolver was
    # ignoring it anyway); the gain in readability outweighs the loss of
    # that obscure stylistic choice.
    rebuilt_target = new_title
    if hash_sep:
        rebuilt_target = f"{rebuilt_target}#{heading}"
    # Drop the display when it equals the OLD title — this is the
    # synthetic display the post-sync link rewriter inserts for
    # bare-title references ``[[Old]]`` → ``[[<path>|Old]]``. The user
    # never typed it, so the rename should erase it rather than carry
    # the stale title forward.
    if (
        sep
        and old_title is not None
        and display.strip().lower() == old_title.strip().lower()
    ):
        sep = ""
        display = ""
    rebuilt_inner = (
        f"{rebuilt_target}|{display}" if sep else rebuilt_target
    )
    return f"{prefix}{rebuilt_inner}]]"


def _group_by_file(
    references: tuple[ReferenceMatch, ...],
) -> dict[Path, list[ReferenceMatch]]:
    """Bucket matches by their file path, preserving in-file order.

    The apply phase rewrites back-to-front per file; preserving the natural
    order returned by :func:`collect_references` (which is document order
    by virtue of the parser's deterministic walk) is what makes that
    correct without an explicit sort.
    """
    grouped: dict[Path, list[ReferenceMatch]] = {}
    for ref in references:
        grouped.setdefault(ref.file_path, []).append(ref)
    return grouped


def apply_matches_to_text(text: str, matches: list[ReferenceMatch]) -> str:
    """Splice ``new_text`` over each match's span in ``text``.

    Writes back-to-front so each splice doesn't invalidate earlier offsets.
    The matches list is required to be non-overlapping (the parser
    guarantees this by construction). Public so
    :mod:`scripts.collapse_gmail_threads` can reuse the splice logic when
    rewriting per-message gmail references onto the merged thread doc.
    """
    if not matches:
        return text
    # Sort descending by start so we splice the tail of the file first.
    ordered = sorted(matches, key=lambda m: m.span[0], reverse=True)
    out = text
    for match in ordered:
        start, end = match.span
        out = out[:start] + match.new_text + out[end:]
    return out


def _rewrite_source_frontmatter(text: str, *, new_title: str) -> str:
    """Update the ``title`` field (and ``updated:`` timestamp) in-place.

    If the file has no parseable frontmatter we add a fresh one — but the
    plan-phase guard (file must come from a synced vault doc) means the
    file always has frontmatter in practice. The defensive branch keeps
    apply_rename safe if a user mutates the file out from under us between
    plan and apply.
    """
    try:
        fields, body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError) as e:
        # Don't try to rewrite a malformed header; the apply contract is
        # "leave the file in a runnable state". Surface the failure up.
        raise RenameError(
            "frontmatter is malformed — fix manually before renaming"
        ) from e
    fields = dict(fields)
    fields["title"] = new_title
    fields["updated"] = datetime.now(UTC).isoformat()
    return dump_frontmatter(fields, body)


def _backup_path_for(
    backup_dir: Path, vault_path: Path, target: Path
) -> Path:
    """Map a vault file to its slot inside the snapshot dir.

    Uses the relative path so the backup tree mirrors the vault layout —
    aids manual recovery if restore itself fails.
    """
    relative = target.resolve().relative_to(vault_path.resolve())
    return backup_dir / relative


def _restore_from_backup(
    backup_dir: Path, vault_path: Path, paths: Iterable[Path]
) -> None:
    """Best-effort restore of every snapshotted file.

    ``paths`` is the iterable of original (vault-side) paths the snapshot
    dir holds. Each file is restored from its mirrored snapshot location.
    Failures during restore are logged but don't re-raise — the caller's
    original exception is what matters; restore-failure information goes
    via the snapshot dir path in the log line.
    """
    for path in paths:
        backup_path = _backup_path_for(backup_dir, vault_path, path)
        if not backup_path.is_file():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(backup_path.read_bytes())
        except OSError as e:
            logger.error(
                "vault rename: failed to restore %s from %s: %s",
                path,
                backup_path,
                e,
            )
