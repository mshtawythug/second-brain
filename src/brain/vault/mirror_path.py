"""The ``_ingested/`` mirror path rule: where a not-yet-mirrored document's file goes.

Split out of :mod:`brain.vault.export` (F15) so the rule is one small, pure unit:
the result depends only on the document's own fields, the collision oracle and
(for the vacancy guard) which files already exist under the vault root. No clock,
no randomness.

Only rows with no stored ``documents.vault_path`` ever reach this module — both
callers in :mod:`brain.vault.export` return a stored path before asking — so a
change to this rule can never move a file that is already mirrored. Plan:
``docs/plans/2026-11-19-vault-mirror-path.md``.
"""
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .slug import gmail_slug, slugify

_MAX_SHORT_ID = 8  # first N chars of an external/document UUID for filename use

#: F15. Characters that are inert in a filename on POSIX, macOS and Windows and
#: inside an Obsidian ``[[wikilink]]``. A legacy segment made only of these (and not
#: starting with a dot) is kept byte for byte, so every id that never caused the bug
#: keeps the name it always had.
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._@+=~-]+")

#: F15. A ``metadata.date`` prefix holding one of these would nest (``2026/01/15``) or
#: escape (``../../../x``) the vault; such a prefix falls back to the ingest date.
_PATH_HOSTILE = ("/", "\\", "\x00")


class MirrorPathDoc(Protocol):
    """The document fields the path rule reads (``_DocumentForExport`` has them all)."""

    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, Any]: ...

    @property
    def ingested_at(self) -> datetime | None: ...

    @property
    def source_kind(self) -> str | None: ...

    @property
    def source_external_id(self) -> str | None: ...


def _short_id(value: str) -> str:
    """Return the first 8 hex chars of a UUID-like value (no surrounding dashes).

    Used for the collision suffix and, before F15, verbatim for the external-id
    slot in filenames. Stripping dashes first makes ``-aabbccdd`` collisions
    deterministic regardless of the full UUID's hyphenation style. For non-UUID
    values (e.g. Slack's ``1234567.890`` timestamps) the strip-then-truncate
    behavior preserves the leading bytes verbatim.
    """
    return value.replace("-", "")[:_MAX_SHORT_ID]


def external_segment(external_id: str) -> str:
    """The external-id slot of a mirror filename (F15).

    The pre-F15 segment (:func:`_short_id`) when it is already filename-safe, so
    no id that never caused the bug changes name. Otherwise — an absolute path, an
    ``owner/repo#N`` id, a dot-name, NUL, whitespace, non-ASCII — the first 8 hex
    digits of the SHA-256 of the FULL id: fixed width, stable across runs, never
    ``.``/``..`` or a separator, and it keeps the OS username out of filenames.
    """
    legacy = _short_id(external_id)
    if _SAFE_SEGMENT.fullmatch(legacy) and not legacy.startswith("."):
        return legacy
    digest = hashlib.sha256(external_id.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()[:_MAX_SHORT_ID]


def _legacy_date_prefix(doc: MirrorPathDoc) -> str:
    """Pick the YYYY-MM-DD prefix for an ingested file's filename (pre-F15 rule).

    ``metadata.date`` wins when present (a string ISO date or datetime, or a
    date/datetime object). Otherwise we fall back to ``ingested_at::date``,
    which is non-NULL by schema (``documents.ingested_at NOT NULL DEFAULT
    NOW()``) — the assert is defensive so a future schema regression
    surfaces here rather than producing a malformed filename.
    """
    raw = doc.metadata.get("date")
    if isinstance(raw, str) and raw:
        # Accept "2026-04-15" or "2026-04-15T..." — the date is the first 10 chars.
        return raw[:10]
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d") if isinstance(raw, date) else raw.date().isoformat()
    return _ingested_date(doc)


def _ingested_date(doc: MirrorPathDoc) -> str:
    assert doc.ingested_at is not None, "documents.ingested_at is NOT NULL"
    return doc.ingested_at.date().isoformat()


def _date_prefix(doc: MirrorPathDoc) -> str:
    """The legacy prefix, unless it would nest, escape or hide the file (F15)."""
    prefix = _legacy_date_prefix(doc)
    if prefix.startswith(".") or any(c in prefix for c in _PATH_HOSTILE):
        return _ingested_date(doc)
    return prefix


def _parse_iso_datetime(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string (with optional trailing ``Z``) into a datetime.

    Returns ``None`` for any non-string / unparseable input. Used by the
    Gmail-slug branch to read ``metadata.sent_at`` (set by the gmail
    extractor). Naive datetimes survive as-is — :func:`gmail_slug` treats
    them as UTC.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gmail_relative_path(doc: MirrorPathDoc) -> str | None:
    """Compute the ``_ingested/gmail/<gmail-slug>.md`` path for a Gmail doc.

    Returns ``None`` when the metadata is too sparse to build a stable slug
    (no ``thread_id`` AND no usable date) — the caller falls back to the
    generic ingested-path rules so legacy rows still export. Idempotent: a
    given ``(thread_id, sent_at, subject)`` triple always yields the same
    path.
    """
    thread_id = doc.metadata.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    sent_at = _parse_iso_datetime(doc.metadata.get("sent_at"))
    if sent_at is None and doc.ingested_at is None:
        return None
    slug = gmail_slug(
        thread_id,
        sent_at,
        doc.title,
        fallback_date=doc.ingested_at,
    )
    return f"_ingested/gmail/{slug}.md"


def _candidate(
    doc: MirrorPathDoc,
    *,
    segment: Callable[[str], str],
    date_prefix: Callable[[MirrorPathDoc], str],
) -> str:
    """The un-suffixed path, with the external-id and date rules injected."""
    source = doc.source_kind or "manual"
    slug = slugify(doc.title)
    if source == "manual":
        return f"_ingested/manual/{slug}.md"
    if source == "gmail":
        gmail_path = _gmail_relative_path(doc)
        if gmail_path is not None:
            return gmail_path
    external = (
        segment(doc.source_external_id)
        if doc.source_external_id
        else f"local-{_short_id(doc.id)}"
    )
    return f"_ingested/{source}/{date_prefix(doc)}-{external}-{slug}.md"


def _with_collision_suffix(
    candidate: str, doc: MirrorPathDoc, is_taken: Callable[[str], bool]
) -> str:
    """Resolve a taken path deterministically: ``-<doc8>``, then ``-<full doc id>``."""
    if not is_taken(candidate):
        return candidate
    # Deterministic collision suffix: short hash of the doc id, never a
    # counter (counters would shuffle every re-run).
    stem, ext = candidate.rsplit(".", 1)
    candidate = f"{stem}-{_short_id(doc.id)}.{ext}"
    # A second collision means two documents share both a slug AND the
    # first 8 hex of their UUIDs. Vanishingly rare, but "vanishingly rare"
    # is what the original empty-set bug looked like from the inside, and
    # the cost of being wrong is another UniqueViolation traceback. Widen
    # the id rather than appending a counter, so the result stays a pure
    # function of the immutable document id and is stable across re-runs.
    if is_taken(candidate):
        candidate = f"{stem}-{doc.id}.{ext}"
    return candidate


def _is_contained(relative: str) -> bool:
    """True iff ``relative`` cannot leave the vault: relative, no ``..``, no NUL."""
    if "\x00" in relative:
        return False
    pure = PurePosixPath(relative)
    return not pure.is_absolute() and ".." not in pure.parts


def ingested_relative_path(
    doc: MirrorPathDoc, is_taken: Callable[[str], bool], *, vault_root: Path
) -> str:
    """Compute the ``_ingested/<source>/...`` path for an ingested-tier doc.

    The shape depends on the source:

    - ``manual``: ``_ingested/manual/<slug>.md`` (no date prefix; older
      manual ingests had no upstream date at all).
    - ``gmail``: ``_ingested/gmail/<gmail-slug>.md`` where ``<gmail-slug>``
      is :func:`brain.vault.slug.gmail_slug` over
      ``(thread_id, sent_at, subject)``. Stable across re-ingests of the
      same thread; falls back to the generic shape below for legacy rows
      missing ``thread_id``.
    - everything else: ``_ingested/<source>/<YYYY-MM-DD>-<external-id>-<slug>.md``
      with :func:`external_segment` (or ``local-<short-doc-id>`` when no source
      row exists, e.g. legacy manual ingests under any kind).

    On collision (two docs hashing to the same path) we append the short
    document UUID — stable across re-runs because it's keyed off the immutable
    ``documents.id``.

    ``is_taken`` is the collision oracle, injected rather than assumed. The
    corpus dump passes its in-run ``used_paths`` set; the single-document path
    passes a DB-backed predicate (``export._paths_taken_by_others``). Making it a
    parameter is what stopped the two callers disagreeing about whether a
    collision is even detectable — the single-document caller used to pass an
    empty set and could therefore never detect one.

    **Vacancy guard (F15).** Where this rule differs from the pre-F15 one, the
    pre-F15 path is returned whenever something already exists there on disk and
    the path stays inside the vault. A row that was mirrored but never recorded
    its path (``vault export`` does not store it) therefore keeps its file: the
    new name is only ever given to a location that holds nothing.
    """
    current = _with_collision_suffix(
        _candidate(doc, segment=external_segment, date_prefix=_date_prefix), doc, is_taken
    )
    legacy = _with_collision_suffix(
        _candidate(doc, segment=_short_id, date_prefix=_legacy_date_prefix), doc, is_taken
    )
    if legacy != current and _is_contained(legacy) and os.path.lexists(vault_root / legacy):
        return legacy
    return current
