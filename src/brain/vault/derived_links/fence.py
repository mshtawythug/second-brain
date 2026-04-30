"""Fenced auto-section for derived edges in `_ingested/` bodies.

Each ingested-tier vault file gets a single trailing section, bracketed by
HTML-comment markers, that materializes the document's metadata-derived
partners as native Obsidian/Quartz wiki-links so they're visible in
Quartz's `/graph` view (see
``docs/specs/2026-04-30-derived-edges-in-bodies-design.md``).

The fence is what keeps this safe across the rest of the pipeline:

- Sync's ``body_hash`` strips the fence before hashing → freshly-rendered
  fences don't trigger the re-embed cascade.
- Sync's normalized-body-for-DB strips the fence → ``documents.content``
  stays clean, so vector search isn't polluted by "Related" lists.
- Wiki-link parser skips the fence → ``[[stems]]`` inside the fence don't
  double-count edges that already live in ``derived_links``.

This module is **pure logic plus one read-only DB query**. Writing files
is the renderer caller's job (added in Task D.4 alongside
``rewrite_derived_fences``); this module only produces the fenced text and
helps callers extract / strip / replace it.
"""
import datetime
import logging
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any

import psycopg

# Stable HTML-comment markers. Universal CommonMark — Obsidian, Quartz, GFM,
# and standard Markdown all treat the line as a passthrough HTML comment.
# Public so the sync engine and watcher can detect fence regions without
# re-importing internal regex state.
FENCE_START_MARKER: str = "<!-- BRAIN_DERIVED_START -->"
FENCE_END_MARKER: str = "<!-- BRAIN_DERIVED_END -->"

# Section heading inside the fence. Stable so users (and `git diff`) can grep
# for it.
_SECTION_HEADING: str = "## Related (auto-generated, do not edit)"

_logger = logging.getLogger(__name__)


def extract_fence(body: str) -> tuple[str, str | None]:
    """Split ``body`` into ``(body_without_fence, fence_text_or_None)``.

    The returned ``fence_text`` includes the start/end markers; the body
    slice excludes the entire fence region (markers + everything between).

    If multiple ``BRAIN_DERIVED_START`` markers appear in the body
    (corruption — e.g. two stacked fences from a buggy renderer), only
    the first is treated as the fence anchor; later markers stay in
    ``body_without_fence`` as plain text. Recovery is then a re-render
    away.

    Bodies without a fence return ``(body, None)`` unchanged.
    """
    start_idx = body.find(FENCE_START_MARKER)
    if start_idx == -1:
        return body, None

    # Search for the matching END marker AFTER the START. A stray END before
    # the first START is treated as content (no fence detected — earlier
    # ``find`` returned -1 — so we never reach this branch in that case).
    end_search_from = start_idx + len(FENCE_START_MARKER)
    end_idx = body.find(FENCE_END_MARKER, end_search_from)
    if end_idx == -1:
        # START with no matching END — treat as malformed and leave body
        # untouched so the user sees the corruption rather than us silently
        # eating the rest of the file.
        return body, None

    fence_close = end_idx + len(FENCE_END_MARKER)
    fence_text = body[start_idx:fence_close]
    body_without = body[:start_idx] + body[fence_close:]
    return body_without, fence_text


def strip_fence(body: str) -> str:
    """Return ``body`` with the fenced region removed.

    Idempotent — calling on a body that has no fence returns it unchanged,
    so ``strip_fence(strip_fence(x)) == strip_fence(x)``. This is what the
    sync engine uses to compute a fence-stable ``body_hash`` (and a
    fence-free ``documents.content`` projection).

    When a fence is removed, trailing whitespace it introduced (the blank
    line that separated user content from the fence, plus any newline
    after the END marker) is collapsed and replaced with a single trailing
    ``\\n``. Two fenced bodies that only differ in their fence content
    therefore strip to byte-identical strings — that's the property the
    ``body_hash`` strip relies on to avoid re-embed loops.

    Bodies without a fence are returned unchanged so the function is a
    pure no-op for files the renderer has never touched.
    """
    body_without, fence = extract_fence(body)
    if fence is None:
        return body
    trimmed = body_without.rstrip()
    return trimmed + "\n" if trimmed else ""


def replace_fence(body: str, new_fence: str) -> str:
    """Return ``body`` with its fence swapped for ``new_fence`` (appending if absent).

    ``new_fence`` is the fully-rendered fenced section — including the
    start/end markers — typically produced by :func:`render_fenced_section`.
    The function strips any pre-existing fence first, then appends
    ``new_fence`` after exactly one blank-line separator and finishes with a
    single trailing newline.

    Edge cases handled:

    - Body without trailing newline → fence appended cleanly with separator.
    - Body with one or more trailing newlines → trailing whitespace is
      normalized so the result ends with exactly one ``\\n`` after the END
      marker (no double-blank, no missing-newline).
    - Body already containing a fence → that fence is removed before the
      new one is appended (so callers can pass the renderer's latest output
      directly without manual orchestration).
    """
    base, _existing = extract_fence(body)
    base = base.rstrip()
    if not base:
        return new_fence + "\n"
    return f"{base}\n\n{new_fence}\n"


def render_fenced_section(
    conn: psycopg.Connection[Any], doc_id: str
) -> str | None:
    """Build the fenced section for ``doc_id`` from the current ``derived_links``.

    Queries every derived edge whose ``src`` or ``dst`` is ``doc_id``,
    joins ``documents`` for each partner's title / vault_path / metadata,
    and emits one bullet per edge:

        - [[<partner-filename-stem>|<partner-title>]] *(<rule>)*

    Sort order (per Q1 decision): ``rule weight DESC``, then partner date
    DESC (``metadata->>'date'``). Partners with a missing or unparseable
    date sort after partners with a date.

    Partners without a ``vault_path`` (not exported yet) are skipped — the
    wiki-link wouldn't resolve in Quartz anyway. If skipping leaves the
    bullet list empty, returns ``None`` (caller should remove the fence
    rather than emit an empty section).
    """
    rows = conn.execute(
        """
        SELECT
            partner.id::text,
            partner.title,
            partner.vault_path,
            partner.metadata,
            dl.rule,
            dl.weight
        FROM derived_links dl
        JOIN documents partner ON partner.id = (
            CASE WHEN dl.src_document_id = %s::uuid
                 THEN dl.dst_document_id
                 ELSE dl.src_document_id
            END
        )
        WHERE dl.src_document_id = %s::uuid
           OR dl.dst_document_id = %s::uuid
        """,
        (doc_id, doc_id, doc_id),
    ).fetchall()

    bullets: list[tuple[float, bool, int, str]] = []
    # Sort key tuple meaning:
    #   index 0: -weight  → ascending sort puts highest weight first
    #   index 1: date_missing (False<True) → with-date partners first within tier
    #   index 2: -date.toordinal() → ascending puts newest date first
    for partner_id, title, vault_path, metadata, rule, weight in rows:
        if not vault_path:
            _logger.debug(
                "fence: skipping partner %s (no vault_path yet)", partner_id
            )
            continue
        stem = PurePosixPath(str(vault_path)).stem
        partner_date = _parse_metadata_date(dict(metadata or {}))
        date_missing = partner_date is None
        date_key = -partner_date.toordinal() if partner_date else 0
        bullet = f"- [[{stem}|{title}]] *({rule})*"
        bullets.append((-float(weight), date_missing, date_key, bullet))

    if not bullets:
        return None

    bullets.sort(key=lambda b: (b[0], b[1], b[2]))
    body_lines = [FENCE_START_MARKER, _SECTION_HEADING]
    body_lines.extend(b[3] for b in bullets)
    body_lines.append(FENCE_END_MARKER)
    return "\n".join(body_lines)


def _parse_metadata_date(metadata: dict[str, Any]) -> datetime.date | None:
    """Best-effort parse of ``metadata['date']`` into a :class:`datetime.date`.

    Mirrors the shapes ``brain.vault.derived_links.pass_runner._parse_date``
    accepts (ISO ``YYYY-MM-DD`` for Krisp, RFC 5322 for Gmail) but doesn't
    require knowing the source kind — the renderer is sort-only and tolerant
    of either form. Returns ``None`` for missing, non-string, or
    unparseable values; partners with a missing date sort last within the
    same weight tier.
    """
    raw = metadata.get("date")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # Try ISO YYYY-MM-DD first (Krisp). Slicing tolerates "YYYY-MM-DDTHH:MM"
    # without pulling in the wider ``datetime.fromisoformat`` grammar.
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass

    # Fall back to RFC 5322 (Gmail).
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed.date()
