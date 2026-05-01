"""Rewrite vault-tier wiki-links to vault-root-relative path form.

Quartz's ``ObsidianFlavoredMarkdown`` plugin treats the inner text of a
``[[X]]`` marker as a literal filepath/slug — it does no frontmatter
lookup. So a vault note that contains ``[[company-mc <> COMPANY_REDACTED - Recap]]``
emits ``href="./company-mc-<>-COMPANY_REDACTED---Recap"`` and 404s in the rendered
site, even though the brain DB resolved the target to
``_ingested/gmail/Mon, 20 Ap-19dacef6-re-company-mc-company-ko-recap.md``.

This module fixes that by rewriting every resolvable ``[[X]]`` in a
vault-tier file to ``[[<vault-root-relative-path>|<display>]]`` form
during ``brain vault sync``. The path is the target document's
``vault_path`` (sans ``.md``); the display preserves whatever the user
wrote (title, alias, or the resolved title for the ``[[brain:<id>]]``
no-display case).

Mirrors the precedent set by
:func:`brain.vault.derived_links.fence.rewrite_derived_fences` — same
in-place body rewriting during sync, same atomic-write pattern, same
"counter-on-write" reporting via :class:`brain.vault.sync.SyncReport`.

Public API:

- :func:`rewrite_wiki_links` — pure: take a body string + connection,
  return ``(rewritten_body, replacements_made)``. No filesystem access.
- :func:`rewrite_vault_links` — I/O wrapper: read a file, rewrite its
  body, write it back atomically when changed. Returns whether the file
  was rewritten.
"""
import logging
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
import yaml

from ._atomic import atomic_write_text
from .frontmatter import dump_frontmatter, parse_frontmatter
from .links import ParsedLink, iter_wiki_links_with_spans
from .resolver import resolve_link

logger = logging.getLogger(__name__)


def rewrite_wiki_links(
    body: str,
    *,
    document_id: str,
    conn: psycopg.Connection[Any],
) -> tuple[str, int]:
    """Rewrite resolvable ``[[…]]`` markers in ``body`` to path-form inner text.

    For every wiki-link or embed:

    1. Parse via :func:`brain.vault.links.iter_wiki_links_with_spans` so
       we get exact byte spans (length-preserving over fenced code +
       inline code, which the parser already masks).
    2. Resolve via :func:`brain.vault.resolver.resolve_link`, excluding
       ``document_id`` from candidates so a self-link is left alone.
    3. If the target has a ``vault_path``, build the new inner as
       ``<path><#heading?>|<display>``:

       - ``<path>`` is the target's ``vault_path`` with the trailing
         ``.md`` stripped (POSIX form).
       - ``<heading>`` is preserved from the original link if present.
       - ``<display>`` is the original ``parsed.display_text`` if set,
         else the original ``parsed.target_value`` (which preserves the
         user's chosen label — title text, source-external string, etc.).
         The one exception: ``[[brain:<id>]]`` with no display reads the
         resolved target's ``documents.title`` so the rendered site shows
         a human-friendly label instead of a UUID prefix.

    4. Compare the candidate inner to the link's existing inner; if
       byte-equal, no rewrite (idempotent fast path — the second sync
       pass over an already-rewritten body produces zero replacements).
    5. Replace each affected span in reverse order so earlier indices
       stay valid as later substitutions change the body length.

    Unresolved links, links whose target has ``vault_path IS NULL``, and
    self-links are left untouched. The rewriter is contractually
    non-destructive: it only ever writes path-form for links the resolver
    confirmed.

    Returns a ``(new_body, replacements)`` tuple. ``replacements`` is the
    number of spans rewritten — a body with three resolvable links and
    one already-in-path-form yields ``replacements == 2``.
    """
    spans = iter_wiki_links_with_spans(body)
    if not spans:
        return body, 0

    # Walk the spans once, collecting ``(start, end, new_raw)`` tuples for
    # each rewrite we want to apply. We iterate forward (document order)
    # for deterministic logging, then apply substitutions in reverse to
    # preserve byte offsets.
    edits: list[tuple[int, int, str]] = []
    for parsed, start, end in spans:
        try:
            target = resolve_link(conn, parsed, exclude_doc_id=document_id)
        except psycopg.Error as e:
            logger.warning(
                "link rewrite: resolver failed on %s — leaving link alone: %s",
                parsed.raw,
                e,
            )
            continue
        if target is None:
            continue
        # Per-link SELECT (not batched): personal-corpus scale (≤10K docs);
        # batching would require a second pass over edits and add complexity
        # for negligible gain.
        target_row = conn.execute(
            "SELECT vault_path, title FROM documents WHERE id = %s",
            (target.document_id,),
        ).fetchone()
        if target_row is None:
            # Race: target was resolved a moment ago but vanished. Leave
            # the link untouched; the user (or a follow-up sync) will see
            # the unresolved row in the DB and surface the issue.
            continue
        target_vault_path, target_title = target_row
        if not target_vault_path:
            logger.debug(
                "link rewrite: target %s has no vault_path — leaving %s alone",
                target.document_id[:8],
                parsed.raw,
            )
            continue

        path_no_ext = _strip_md_extension(str(target_vault_path))
        display = _choose_display(parsed, resolved_title=str(target_title))
        new_inner = _build_inner(
            path_no_ext, heading=parsed.heading, display=display
        )
        new_raw = ("![[" if parsed.kind == "embed" else "[[") + new_inner + "]]"

        if new_raw == parsed.raw:
            # Already in canonical path form — idempotent fast path.
            continue
        edits.append((start, end, new_raw))

    if not edits:
        return body, 0

    new_body = body
    for start, end, new_raw in reversed(edits):
        new_body = new_body[:start] + new_raw + new_body[end:]
    return new_body, len(edits)


def rewrite_vault_links(
    file_path: Path,
    *,
    document_id: str,
    conn: psycopg.Connection[Any],
) -> bool:
    """Read ``file_path``, rewrite its wiki-links, write back atomically.

    Returns ``True`` iff the file's bytes changed (i.e. at least one link
    was rewritten and the new body differs). On any I/O or YAML error the
    function logs a warning and returns ``False`` — the rewrite is a
    best-effort polish step; a failure here MUST NOT fail the sync run.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(
            "link rewrite: could not read %s: %s — skipping", file_path, e
        )
        return False
    try:
        frontmatter, body = parse_frontmatter(text)
    except (ValueError, yaml.YAMLError) as e:
        logger.warning(
            "link rewrite: malformed frontmatter in %s: %s — skipping",
            file_path,
            e,
        )
        return False

    try:
        new_body, replacements = rewrite_wiki_links(
            body, document_id=document_id, conn=conn
        )
    except psycopg.Error as e:
        # Defense in depth — ``rewrite_wiki_links`` already swallows
        # per-link resolver errors; this branch catches a connection-level
        # problem that escaped the inner handler. Log and move on.
        logger.warning(
            "link rewrite: DB error rewriting %s — skipping: %s",
            file_path,
            e,
        )
        return False

    if replacements == 0 or new_body == body:
        return False

    new_text = dump_frontmatter(frontmatter, new_body)
    try:
        atomic_write_text(file_path, new_text)
    except OSError as e:
        logger.warning(
            "link rewrite: could not write %s: %s — skipping", file_path, e
        )
        return False
    return True


def _strip_md_extension(vault_path: str) -> str:
    """Drop a trailing ``.md`` from a POSIX vault path, otherwise return as-is.

    ``vault_path`` is stored in POSIX form (forward-slash separators); we
    parse it through :class:`pathlib.PurePosixPath` so we don't accidentally
    pick up Windows separators when the renderer runs on macOS / Linux.
    Files without a ``.md`` suffix (defensive — every vault file should
    have one, but a corrupted row could exist) are returned untouched and
    the rewriter falls back to the path verbatim.
    """
    p = PurePosixPath(vault_path)
    if p.suffix.lower() == ".md":
        return str(p.with_suffix(""))
    return vault_path


def _choose_display(parsed: ParsedLink, *, resolved_title: str) -> str:
    """Pick the display label for the rewritten link.

    Precedence:

    1. Original ``parsed.display_text`` if the user wrote one
       (``[[X|alias]]`` or ``[[brain:<id>|alias]]``).
    2. For ``[[brain:<id>]]`` (no display), use the resolved target's
       ``documents.title`` — the user clearly wanted a human label, not a
       raw UUID prefix.
    3. Otherwise fall back to ``parsed.target_value`` (the original
       title text, source-external string, etc.) so what the user typed
       remains the visible label.
    """
    if parsed.display_text:
        return str(parsed.display_text)
    if parsed.target_type == "doc-id":
        return resolved_title
    return str(parsed.target_value)


def _build_inner(path: str, *, heading: str | None, display: str) -> str:
    """Concatenate the rewritten link inner: ``<path>[#heading]|<display>``."""
    head = f"#{heading}" if heading else ""
    return f"{path}{head}|{display}"
