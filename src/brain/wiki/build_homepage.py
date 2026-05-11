"""Server-side render of the home-page "Recently captured" rail (P4.7).

Phase 4.7 of the Wiki UX Overhaul. Surfaces the 12 most-recently-ingested
documents on the home page so the user lands on something fresh after every
ingest cycle (Krisp call dump, Slack thread pull, Gmail batch, …) instead of
the static "Doors / Topic clusters" copy.

Two write surfaces, both fed from the same DB query:

- ``<vault>/_partials/recent.md`` — the rendered bullet list, written
  byte-stable for inspection / debugging. The Quartz workspace ignores the
  ``_partials/`` directory (see ``ignorePatterns`` in ``quartz.config.ts``)
  so this file never becomes a public page.
- ``<vault>/index.md`` — the live home note. Its body has a fenced region
  marked by ``<!-- BRAIN_RECENT_START -->`` / ``<!-- BRAIN_RECENT_END -->``;
  this module rewrites the content between markers in-place. Mirrors the
  Phase D derived-edges fence pattern (``brain.vault.derived_links.fence``)
  exactly: stable markers, atomic write, idempotent on byte-identical input.

Why two files? The partial is the source-of-truth artifact (a future tooling
layer — e.g. a "What's new" RSS feed — can read it without parsing index.md).
The fence in index.md is what readers actually see in the rendered wiki. The
public function :func:`refresh_homepage` writes both in lockstep so they never
disagree.

Failure modes are silent-but-loud: a missing fence in index.md logs a
warning and skips the rewrite (don't auto-insert — the user might be
deliberately omitting the rail); a DB error inside :func:`refresh_homepage`
logs an error and returns ``(False, False)`` so the surrounding build never
fails *because* the rail couldn't be regenerated. The build is the customer;
the rail is a courtesy.
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from ..config import Config
from ..db import connect
from ..vault._atomic import atomic_write_text
from ..vault.frontmatter import dump_frontmatter, parse_frontmatter
from ..vault.paths import safe_wikilink_alias, strip_md_extension

# Stable HTML-comment markers — universal CommonMark passthroughs that
# Obsidian, Quartz, and GFM all leave alone. Public so tests + future
# render surfaces can detect the fence without re-importing internal regex
# state. Mirrors :data:`brain.vault.derived_links.fence.FENCE_START_MARKER`
# pattern but in its own namespace so tweaking one surface doesn't tug the
# other.
FENCE_START_MARKER: str = "<!-- BRAIN_RECENT_START -->"
FENCE_END_MARKER: str = "<!-- BRAIN_RECENT_END -->"

# Fixed window. The plan says "12 newest"; we don't make this configurable
# because the visual rail is laid out for exactly this count and adding a
# knob invites drift between the partial, the fence, and any future RSS
# consumer.
RECENT_LIMIT: int = 12

# Source-icon vocabulary mirrors `brain/quartz_overrides/quartz/util/sourceIcons.ts`
# (the SOURCE_ICONS map there is the canonical client-side copy). Keeping a
# server-side duplicate here is intentional: the partial is rendered by Python
# *before* Quartz is invoked, so we can't import the .ts module. The two
# copies must stay aligned — a future change to the client glyph table needs
# this dict bumped too. Tests in ``test_brain_recent_homepage.py`` pin every
# key/value pair to lock that contract.
_SOURCE_ICONS: dict[str, str] = {
    "gmail": "📧",
    "krisp": "🎙️",
    "slack": "💬",
    "manual": "✍️",
    "vault": "🌱",
}

# Unknown source kinds fall back to the generic "vault" glyph so a row never
# renders icon-less. Same default as ``sourceIconFor`` on the client.
_DEFAULT_SOURCE_ICON: str = _SOURCE_ICONS["vault"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecentDoc:
    """One row of the recent-rail query, projected for rendering.

    ``source_kind`` may be ``None`` for vault-tier docs (no ``sources`` row);
    callers map ``None`` → ``"vault"`` before icon lookup.

    ``vault_path`` is guaranteed non-empty by the SQL filter — the renderer
    relies on it to build the wiki-link target. Rows without a vault_path
    are excluded at the SQL layer (they aren't browseable yet).
    """

    title: str
    source_kind: str | None
    ingested_at: datetime.datetime
    vault_path: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def refresh_homepage(cfg: Config) -> tuple[bool, bool]:
    """Refresh the partial + the fence in lockstep using ``cfg``'s DB + vault.

    Convenience entry point used by the build pipeline (CLI in
    ``brain.wiki.build_swap.main`` and any caller that already has a
    :class:`Config` in hand). Opens a short-lived DB connection, runs the
    recent-docs query once, and writes both surfaces from the same row set
    so the partial and the fence can never drift.

    Returns ``(partial_changed, fence_changed)`` — each ``True`` if the
    corresponding file was written (content drifted), ``False`` if the
    write was a no-op (byte-identical input) or skipped (no fence in
    index.md, no docs to render). Tests assert specific tuples here to
    prove idempotency.

    Failure handling is intentionally permissive: any
    :class:`psycopg.Error` (DB unreachable, schema drift, …) is logged at
    ``WARNING`` and swallowed — the rail is a nice-to-have, not a build
    gate. A failed refresh leaves both files untouched; the next
    successful build retries.
    """
    try:
        with connect(cfg.database_url) as conn:
            docs = _fetch_recent_docs(conn, limit=RECENT_LIMIT)
    except psycopg.Error as exc:
        _logger.warning(
            "wiki recent rail: DB query failed (%s) — skipping refresh", exc
        )
        return (False, False)

    partial_changed = regenerate_recent_partial(cfg.vault_path, docs=docs)
    fence_changed = regenerate_recent_fence(cfg.vault_path, docs=docs)
    return (partial_changed, fence_changed)


def regenerate_recent_partial(
    vault_path: Path, *, docs: Sequence[RecentDoc]
) -> bool:
    """Write the rendered bullet list to ``<vault>/_partials/recent.md``.

    Returns ``True`` iff the file was actually written (content drifted
    from what's already on disk). A no-op return preserves the file's
    mtime so the watcher doesn't fire a needless rebuild.

    The partial directory is created on demand (``mkdir -p``) — first
    call on a fresh vault doesn't require a separate scaffold step.

    The atomic write goes through
    :func:`brain.vault._atomic.atomic_write_text` so a crash mid-write
    can never leave a half-written partial visible to consumers.
    """
    rendered = _render_bullets(docs)
    target = vault_path / "_partials" / "recent.md"

    # Idempotency: read-then-compare before write. A re-run with the same
    # DB state must produce a byte-identical file — that's the property the
    # `Phase 4.1 daily index` test pattern proved valuable; we mirror it.
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == rendered:
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, rendered)
    return True


def regenerate_recent_fence(
    vault_path: Path, *, docs: Sequence[RecentDoc]
) -> bool:
    """Replace the recent-rail fence in ``<vault>/index.md`` in place.

    Looks for the ``<!-- BRAIN_RECENT_START -->`` / ``<!-- BRAIN_RECENT_END -->``
    markers in the home note's body. If found, the content between them is
    swapped for the freshly-rendered bullet list (preserving the markers
    themselves). If the markers are missing, logs a warning and returns
    ``False`` — the renderer never auto-inserts the fence because the user
    might have deliberately removed it from the home note.

    Returns ``True`` iff the file was actually rewritten. A re-run with
    the same DB state and the same on-disk fence content returns ``False``
    without touching the file (preserves mtime, keeps the watcher quiet).

    The home note retains its existing frontmatter (id / created /
    title / tags / …) — only the body changes. ``updated`` is left
    intact so the home note doesn't bump on every rail refresh; the
    fence is a low-signal change and we don't want it polluting the
    "modified" timestamp Quartz surfaces in the page header.

    Atomic — sibling tempfile + ``os.replace`` via
    :func:`atomic_write_text`. A crash mid-write leaves the previous
    home note intact.
    """
    target = vault_path / "index.md"
    if not target.is_file():
        _logger.warning(
            "wiki recent rail: home note %s does not exist — skipping fence",
            target,
        )
        return False

    text = target.read_text(encoding="utf-8")
    try:
        frontmatter, body = parse_frontmatter(text)
    except Exception as exc:  # noqa: BLE001 — parse_frontmatter raises broadly
        _logger.warning(
            "wiki recent rail: malformed frontmatter in %s (%s) — skipping",
            target, exc,
        )
        return False

    new_body = _replace_fence(body, _render_bullets(docs))
    if new_body is None:
        _logger.warning(
            "wiki recent rail: %s missing %s/%s markers — add them by hand to "
            "enable the recent rail (skipping)",
            target, FENCE_START_MARKER, FENCE_END_MARKER,
        )
        return False

    if new_body == body:
        return False

    new_text = dump_frontmatter(frontmatter, new_body)
    atomic_write_text(target, new_text)
    return True


# ---------------------------------------------------------------------------
# Internals — query + rendering helpers.
# ---------------------------------------------------------------------------


def _fetch_recent_docs(
    conn: psycopg.Connection[Any], *, limit: int
) -> list[RecentDoc]:
    """Return the ``limit`` most-recently-ingested docs eligible for the rail.

    Filters applied at the SQL layer:

    - ``draft = FALSE`` — drafts are quarantined from every public surface
      (P1.6). The recent rail must not surface them either.
    - ``vault_path IS NOT NULL`` — without a vault path the row isn't
      browseable from the wiki, so emitting a wiki-link would 404.
    - ``ingested_at IS NOT NULL`` — defensive; the column is ``NOT NULL``
      in 001_init.sql but the predicate guards a future schema relax.

    Sort is ``ingested_at DESC`` (the spec). LEFT JOIN against ``sources``
    so vault-tier docs (no ``sources`` row) come back with ``source_kind=NULL``
    rather than being silently dropped.

    Read-only — never INSERT/UPDATE/DELETE. Safe to call from any
    autocommit-or-not context.
    """
    rows = conn.execute(
        """
        SELECT d.title, s.kind, d.ingested_at, d.vault_path
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.draft = FALSE
          AND d.vault_path IS NOT NULL
          AND d.ingested_at IS NOT NULL
        ORDER BY d.ingested_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        RecentDoc(
            title=str(title),
            source_kind=str(kind) if kind is not None else None,
            ingested_at=ingested_at,
            vault_path=str(vault_path),
        )
        for (title, kind, ingested_at, vault_path) in rows
    ]


def _render_bullets(docs: Sequence[RecentDoc]) -> str:
    """Render the recent-rail markdown body (bullets, no fence markers).

    Empty corpus → a single italic placeholder line so the rail has *some*
    visible content rather than collapsing to a blank gap. The placeholder
    uses ``*…*`` (markdown italic) which renders as muted text in the
    Linear-style theme — gentler than a hard "(no docs)" string.

    Each non-empty line is shaped:

        ``- {icon} [[<vault_path-without-md>|<safe_title>]] · {relative-date}``

    where:

    - ``{icon}`` comes from :data:`_SOURCE_ICONS` (with the ``"vault"``
      fallback for unknown kinds).
    - ``<vault_path-without-md>`` is the doc's ``vault_path`` with a
      trailing ``.md`` stripped — matches the canonical wiki-link target
      shape the link rewriter emits, so a future ``brain vault sync`` pass
      over the home note doesn't churn this body.
    - ``<safe_title>`` strips wiki-link-breaking ``[`` / ``]`` from the
      alias slot. Quartz's wiki-link regex defines aliases as
      ``[^\\[\\]\\#]``; bracketed prefixes like ``Re: [External] Re: …``
      from Gmail subjects would otherwise emit raw text. Same trick the
      derived-edges fence uses.
    - ``{relative-date}`` is :func:`_format_relative_date` of
      ``ingested_at``.

    Trailing newline so the body always ends Unix-cleanly.
    """
    if not docs:
        return "*No documents ingested yet — try `brain ingest <file>`.*\n"

    today = datetime.date.today()
    lines: list[str] = []
    for doc in docs:
        icon = _SOURCE_ICONS.get(doc.source_kind or "vault", _DEFAULT_SOURCE_ICON)
        target = strip_md_extension(doc.vault_path)
        alias = safe_wikilink_alias(doc.title)
        when = _format_relative_date(doc.ingested_at, today=today)
        lines.append(f"- {icon} [[{target}|{alias}]] · {when}")
    return "\n".join(lines) + "\n"


def _replace_fence(body: str, new_inner: str) -> str | None:
    """Return ``body`` with the fence's inner content swapped for ``new_inner``.

    Mirrors the contract of
    :func:`brain.vault.derived_links.fence.extract_fence` but reduced to a
    single-shot replace: we don't need the partial-extract API surface
    here (the partial file is the inspection surface; the fence is purely
    in-place).

    Behavior:

    - Both markers present and well-ordered (START before END) → return
      ``<prefix><START>\\n<new_inner><END><suffix>`` (note the END marker
      is appended directly after the inner content; ``new_inner`` already
      ends with ``\\n`` so the END line lands on its own line).
    - Markers missing or inverted (END before START) → return ``None``.
      The caller logs a warning and skips the rewrite.
    - Multiple START or END markers — only the first START and the
      first END after it are anchors. Strays stay in the surrounding
      body as text; corruption recovery is then a re-render away.

    Idempotency is the caller's job (compare returned body to input).
    """
    start_idx = body.find(FENCE_START_MARKER)
    if start_idx == -1:
        return None
    end_search_from = start_idx + len(FENCE_START_MARKER)
    end_idx = body.find(FENCE_END_MARKER, end_search_from)
    if end_idx == -1:
        return None

    prefix = body[: start_idx + len(FENCE_START_MARKER)]
    suffix = body[end_idx:]
    # ``new_inner`` always ends with ``\n`` (per :func:`_render_bullets`),
    # so the END marker lands on its own line. The start marker is
    # followed by exactly one newline so the bullets begin on the next
    # row, matching how the user would hand-author the fence.
    return f"{prefix}\n{new_inner}{suffix}"


def _format_relative_date(
    when: datetime.datetime, *, today: datetime.date
) -> str:
    """Render ``when`` as a coarse human-friendly relative date.

    Buckets, in order:

    - same calendar day → ``"today"``
    - 1–6 days ago     → ``"1d ago"`` … ``"6d ago"``
    - 1–4 weeks ago    → ``"1w ago"`` … ``"4w ago"``
    - 5+ weeks ago     → ``"Apr 27"`` style (locale-independent ``%b %-d``)

    Calendar-day comparison (not 24h windows) so a doc ingested at 23:59
    yesterday and another at 00:01 today both render as expected — naive
    elapsed-seconds bucketing would mis-classify the boundary.

    Future dates (clock skew, manual ``ingested_at`` overrides) bucket as
    ``"today"`` — a recent rail with a pretend-future bullet shouldn't
    surface ``"-3d ago"``.
    """
    when_date = _to_date(when)
    delta = (today - when_date).days
    if delta <= 0:
        return "today"
    if delta < 7:
        return f"{delta}d ago"
    if delta < 35:
        return f"{delta // 7}w ago"
    # ``%-d`` is GNU/BSD-specific (no leading zero); on Windows the right
    # spelling is ``%#d``. Both are absent from POSIX. Build the day
    # ourselves to stay portable.
    month = when_date.strftime("%b")
    return f"{month} {when_date.day}"


def _to_date(when: datetime.datetime) -> datetime.date:
    """Project a (possibly tz-aware) datetime onto the local calendar date.

    ``ingested_at`` is stored as ``TIMESTAMPTZ`` in Postgres; psycopg
    returns a tz-aware datetime. Convert to local time before slicing the
    date so a doc ingested at 23:30 UTC doesn't render as "tomorrow" for
    a user in UTC-5.
    """
    if when.tzinfo is not None:
        when = when.astimezone()
    return when.date()

