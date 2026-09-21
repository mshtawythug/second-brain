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

This module ships:

- Pure-string helpers: :func:`extract_fence`, :func:`strip_fence`,
  :func:`replace_fence`. No DB.
- DB-reading renderer: :func:`render_fenced_section` — produces the
  fenced markdown for a doc id from current ``derived_links`` rows.
- File-rewriting renderer: :func:`rewrite_derived_fences` — for each
  affected ingested-tier doc, regenerates the fence on disk via an
  atomic write. Skips vault-tier docs (Q3=a) and docs without an
  ``_ingested/`` mirror (covered by export, not the fence renderer).
"""
import datetime
import logging
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
import yaml

from ...sensitivity import CONFIDENTIAL, not_confidential_sql
from .._atomic import atomic_write_text
from ..paths import safe_wikilink_alias

# Stable HTML-comment markers. Universal CommonMark — Obsidian, Quartz, GFM,
# and standard Markdown all treat the line as a passthrough HTML comment.
# Public so the sync engine and watcher can detect fence regions without
# re-importing internal regex state.
FENCE_START_MARKER: str = "<!-- BRAIN_DERIVED_START -->"
FENCE_END_MARKER: str = "<!-- BRAIN_DERIVED_END -->"

# Rules surfaced in the Quartz-visible fence. ``shared_participant`` (R2) is
# excluded because it's high-recall / low-precision: at corpus scale it
# produces an N×N hairball that breaks Quartz's force-directed graph layout
# (the user is a participant in nearly every doc, so almost every doc shares
# a participant with almost every other doc). R2 edges remain queryable via
# ``brain backlinks`` / ``brain graph`` / MCP — only the Quartz surface is
# narrowed, not the underlying ``derived_links`` table.
#
# Sorted to give psycopg a stable parameter binding shape.
FENCE_RULES: tuple[str, ...] = ("same_day_participant", "shared_thread")

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

    Queries every derived edge whose ``src`` or ``dst`` is ``doc_id`` AND
    whose ``rule`` is in :data:`FENCE_RULES` (i.e. R1/R3 — ``shared_thread``
    and ``same_day_participant``). R2 (``shared_participant``) is filtered
    out at the SQL layer because it's high-recall / low-precision and at
    corpus scale produces a graph hairball that breaks Quartz's
    force-directed layout. R2 edges remain queryable via every other read
    surface (``brain backlinks`` / ``brain graph`` / MCP); only the
    Quartz-visible fence is narrowed.

    Joins ``documents`` for each partner's title / vault_path / metadata
    and emits one bullet per edge:

        - [[<partner-filename-stem>|<partner-title>]] *(<rule>)*

    Sort order (deterministic, primary→tertiary):

    1. ``rule weight DESC`` — highest-weight rules surface first (R1
       ``shared_thread`` weight 1.0 before R3 ``same_day_participant``
       weight 0.7).
    2. Partner ``metadata->>'date'`` DESC — within a weight tier, newer
       partners win. Partners with a missing or unparseable date sort
       AFTER partners with a date.
    3. Partner document ``id`` ASC — final tie-breaker so two partners
       with identical weight + identical date (or both undated) always
       emit in the same order across runs. Without this every relink →
       sync round-trip risked reshuffling the bullet list and bumping
       file mtimes unnecessarily.

    Both the SQL query and the in-memory sort are stable: SQL
    ``ORDER BY`` pins the row delivery order, and Python's ``list.sort``
    is guaranteed stable, so the tertiary id key dominates only when the
    weight + date keys tie.

    Partners without a ``vault_path`` (not exported yet) are skipped — the
    wiki-link wouldn't resolve in Quartz anyway. If skipping leaves the
    bullet list empty (or the rule filter dropped every edge for this
    doc), returns ``None`` (caller should remove the fence rather than
    emit an empty section).

    **F6: confidential partners are excluded, unconditionally.** This function
    has exactly ONE consumer -- :func:`rewrite_derived_fences`, which writes the
    result into an ``_ingested/`` mirror -- and that file is an egress boundary:
    ``_ingested/`` appears in NEITHER Quartz config's ``ignorePatterns``, and a
    fence bullet on a *normal* host renders as a visible anchor whose text is
    the partner's TITLE and whose target is the partner's slug. The host's own
    ``sensitivity: normal`` frontmatter is what keeps that page published, so
    ``RemoveConfidential`` never looks at the partner named inside it.

    **One predicate, both endpoints.** ``iter_suggestions`` needs two gates
    because it joins ``sd`` and ``td`` separately; here the ``CASE`` join
    resolves *the end that is not the host* whichever column it sits in, so a
    single predicate on ``partner`` covers a confidential document in the
    ``src`` position and in the ``dst`` position alike. Both directions are
    pinned by test, because "the symmetric join covers it" is an argument, and
    an argument is not a measurement.

    **No ``exclude_confidential`` parameter, deliberately** -- a departure from
    :func:`brain.connect.iter_suggestions`, which has one. That function serves
    two audiences (a terminal, permissive; the MCP boundary, gated) and needs a
    knob to tell them apart. This one serves a single audience and that audience
    publishes, so a permissive direction would have no legitimate caller and
    would exist only as a flag someone can set wrong. The gate that cannot be
    turned off cannot be turned off by mistake.
    """
    partner_not_confidential = not_confidential_sql("partner")
    # F6 gate, interpolated rather than bound: ``not_confidential_sql`` returns a
    # frozen literal built from a module constant that never touches caller
    # input, and every other parameter in this statement is positional -- adding
    # a bound parameter on a conditional clause is how the sibling call sites
    # bind the wrong value in the wrong order. See that function's docstring.
    rows = conn.execute(
        f"""
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
        WHERE (dl.src_document_id = %s::uuid
               OR dl.dst_document_id = %s::uuid)
          AND dl.rule = ANY(%s)
          AND {partner_not_confidential}
        ORDER BY dl.weight DESC,
                 partner.metadata->>'date' DESC NULLS LAST,
                 partner.id ASC
        """,
        (doc_id, doc_id, doc_id, list(FENCE_RULES)),
    ).fetchall()

    bullets: list[tuple[float, bool, int, str, str]] = []
    # Sort key tuple meaning:
    #   index 0: -weight  → ascending sort puts highest weight first
    #   index 1: date_missing (False<True) → with-date partners first within tier
    #   index 2: -date.toordinal() → ascending puts newest date first
    #   index 3: partner_id ASC → deterministic tertiary tie-breaker so
    #            partners with identical weight + identical date emit in
    #            the same order across runs (no mtime churn from
    #            relink → sync cycles re-shuffling tied bullets).
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
        bullet = f"- [[{stem}|{safe_wikilink_alias(title)}]] *({rule})*"
        bullets.append(
            (-float(weight), date_missing, date_key, str(partner_id), bullet)
        )

    if not bullets:
        return None

    bullets.sort(key=lambda b: (b[0], b[1], b[2], b[3]))
    body_lines = [FENCE_START_MARKER, _SECTION_HEADING]
    body_lines.extend(b[4] for b in bullets)
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


def refresh_fences_naming(
    conn: psycopg.Connection[Any], doc_id: str, *, vault_path: Path
) -> int:
    """Re-render every fence that could NAME ``doc_id``, plus ``doc_id``'s own.

    Call this after a document's ``sensitivity`` changes. The F6 gate in
    :func:`render_fenced_section` decides what a fence may name *at render
    time*, which does nothing for fences rendered EARLIER: marking a document
    confidential left its title and slug sitting in every partner's
    already-published page until somebody happened to run a full
    ``brain vault relink-derived``. So ``brain mark-confidential`` reported
    success -- and on the published site the document was not confidential, for
    an unbounded stretch of time, with nothing to indicate it. The gate is the
    policy; this is what makes the policy retroactive.

    Symmetric on purpose: ``brain mark-normal`` runs it too, so a document
    returned to the normal tier reappears in its partners' fences instead of
    staying invisible until the next relink. One function, both directions --
    a one-way refresh would fix the alarming case and leave the quiet one.

    **This query is deliberately NOT sensitivity-gated, which inside an F6 fix
    deserves a sentence.** It selects the ids whose files must be REWRITTEN, not
    content to emit. A partner that is itself confidential still needs its fence
    re-rendered (the host leg in :func:`rewrite_derived_fences` strips it), and
    gating here would skip exactly those files. Nothing this query returns
    reaches a page: every id it yields goes back through the renderer, which is
    where the gate lives.

    Scoped to :data:`FENCE_RULES` because only those rules are rendered -- and
    it reads the same constant the renderer does, so the two cannot drift into
    disagreeing about which edges can put a title on a page. Returns the number
    of files actually written (``rewrite_derived_fences``' own count, so a
    no-op change costs no mtime bump).
    """
    rows = conn.execute(
        """
        SELECT CASE WHEN dl.src_document_id = %s::uuid
                    THEN dl.dst_document_id::text
                    ELSE dl.src_document_id::text
               END
        FROM derived_links dl
        WHERE (dl.src_document_id = %s::uuid OR dl.dst_document_id = %s::uuid)
          AND dl.rule = ANY(%s)
        """,
        (doc_id, doc_id, doc_id, list(FENCE_RULES)),
    ).fetchall()
    affected = {doc_id} | {str(r[0]) for r in rows}
    return rewrite_derived_fences(conn, affected, vault_path=vault_path)


def rewrite_derived_fences(
    conn: psycopg.Connection[Any],
    doc_ids: set[str],
    *,
    vault_path: Path,
) -> int:
    """Regenerate the derived-edges fence for every affected ``_ingested/`` file.

    Driven by ``rebuild_derived_for``'s ``affected_ids`` return value:
    every doc whose edges changed (gained or lost) gets its fence rewritten
    on disk, so Quartz's ``/graph`` view stays consistent with the latest
    ``derived_links`` rows.

    Behavior per the user's Q3–Q5 decisions (with the 2026-05-08 idempotency fix):

    - **Q3=a** vault-tier files are silently skipped — user-authored notes
      stay untouched in v1. Only docs with ``kind='ingested'`` are
      candidates.
    - **Skip on byte-identical** (supersedes the original Q4=b "always
      rewrite"): if the freshly-rendered file text is byte-identical to
      what's already on disk, the file is NOT written and is NOT counted.
      The returned ``written`` count reflects actual disk writes, so
      ``relink-derived → sync`` is a true no-op for unchanged docs (no
      mtime bumps, no Quartz rebuild churn) and the caller's reported
      counter cannot lie about disk effect.
    - **Q5=b** + Q2a fence content uses ``[[<filename-stem>|<title>]]
      *(<rule>)*`` (rendered by :func:`render_fenced_section`).
    - **F6: a confidential host gets its fence STRIPPED, not rendered.**
      The second of the two legs, and it is weaker than the first -- say so
      rather than let a reader assume both close measured leaks. The partner
      gate in :func:`render_fenced_section` closes a disclosure that was
      *reproduced*: a confidential title inside a published normal page. This
      leg closes nothing measured, because a confidential host's own mirror
      carries ``sensitivity: confidential`` and Quartz's ``RemoveConfidential``
      does read that key. It is here because ``_ingested/`` is in neither
      Quartz config's ``ignorePatterns``, so that TypeScript filter is the ONLY
      thing unpublishing the page -- and a SQL-side pipeline should not depend
      on a downstream filter in another language being correct, which is the
      same argument that made ``people.aggregate_people`` fail closed. Cost,
      stated because it is real: derived-link navigation disappears from
      confidential mirrors in local Obsidian.

      STRIP rather than SKIP so the pipeline converges. Skipping would leave a
      previously-rendered fence frozen on disk the moment a document is marked
      confidential; stripping removes it on the next pass. (Neither fixes the
      *partner*-side staleness -- ``brain mark-confidential`` regenerates only
      the marked document's own mirror, so its title sits in every partner's
      published fence until the next relink. That gap is reported, not fixed
      here.)

    Returns the count of files actually written. Docs in ``doc_ids`` that
    map to a vault-tier row, have no ``vault_path`` set, whose mirror
    file is missing on disk, OR whose freshly-rendered text matches the
    on-disk text byte-for-byte are silently dropped from the count —
    those skips aren't failures, they're "the renderer has no work to do
    here."

    Atomicity: each write goes through a sibling temp file plus
    :func:`os.replace`, which is atomic on POSIX (rename(2)). A crash
    mid-write leaves the original file intact; the temp file is cleaned
    up on the next pass over the same doc id.

    Empty input short-circuits to ``0`` — no DB round-trip, no FS scan.
    """
    if not doc_ids:
        return 0

    # Local imports break a cycle: ``brain.vault.frontmatter`` imports
    # :func:`strip_fence` from this module so its ``body_hash`` ignores the
    # fence content; we in turn need its parse/dump helpers to round-trip
    # the YAML frontmatter while editing the body. Lazy-loading at call
    # time keeps both modules' top-level imports cycle-free.
    from ..frontmatter import dump_frontmatter, parse_frontmatter

    # Single batch lookup: kind + vault_path for every affected doc id. At
    # production scale (~500 ingested docs) the IN-list fits comfortably in
    # one query and saves N round-trips on a full corpus relink.
    rows = conn.execute(
        "SELECT id::text, kind, vault_path, sensitivity FROM documents "
        "WHERE id = ANY(%s)",
        (sorted(doc_ids),),
    ).fetchall()

    written = 0
    for doc_id, kind, vp, sensitivity in rows:
        # Q3=a: vault-tier files stay untouched in v1.
        if kind != "ingested":
            continue
        # No mirror exported yet — the fence renderer has no file to
        # rewrite. Export will produce one on the next pass; this is not
        # an error.
        if not vp:
            continue
        target = vault_path / str(vp)
        if not target.is_file():
            _logger.debug(
                "fence: skipping %s — vault_path %r has no file on disk",
                doc_id, vp,
            )
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as e:
            _logger.warning(
                "fence: could not read %s: %s — skipping", target, e
            )
            continue
        try:
            frontmatter, body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError) as e:
            _logger.warning(
                "fence: malformed frontmatter in %s: %s — skipping",
                target, e,
            )
            continue

        # F6 host leg — see the docstring. ``None`` routes to ``strip_fence``
        # below, which is the same path an edge-less document takes, so a
        # confidential mirror converges on "no fence" instead of freezing
        # whatever was rendered before it was marked.
        new_fence = (
            None
            if sensitivity == CONFIDENTIAL
            else render_fenced_section(conn, doc_id)
        )
        new_body = (
            strip_fence(body) if new_fence is None
            else replace_fence(body, new_fence)
        )
        new_text = dump_frontmatter(frontmatter, new_body)

        # Idempotency: skip the write entirely when the rendered text is
        # byte-identical to what's already on disk. This is the contract
        # that lets ``relink-derived → sync`` round-trip without bumping
        # mtimes — counters reflect real disk effect, not "we ran the
        # renderer."  See ``docs/plans/2026-05-08-vault-sync-fence-strip-bug.md``.
        if new_text == text:
            continue

        try:
            atomic_write_text(target, new_text)
        except OSError as e:
            _logger.warning(
                "fence: could not rewrite %s: %s — skipping", target, e
            )
            continue
        written += 1
    return written
