"""Aggregate per-person doc rosters for the People Hub wiki pages.

Pure logic only — reads from ``directory_entries`` and ``documents``, produces
:class:`PersonRecord` aggregates that downstream Phase B/C consumers turn into
``<vault>/people/<slug>.md`` pages and ``brain people`` CLI output.

The aggregation mirrors the participant-key derivation used by the metadata
linker (see ``brain.vault.derived_links.pass_runner._build_snapshot``) so a
person's roster on the hub is consistent with the derived edges that link
their docs to each other. The only intentional divergence: this module does
not bridge name↔email at extraction time. Both keys are emitted per doc, and
``directory_entries`` is consulted at *resolution* time to map each key to a
canonical display_name. The end result is identical because every name and
email a key could resolve to also lives in ``directory_entries``.
"""
import logging
import posixpath
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import psycopg

from brain.vault.derived_links.participants import extract_gmail_addresses
from brain.vault.slug import slugify

logger = logging.getLogger(__name__)


@dataclass
class DocRef:
    """One document associated with a person — used to render their roster.

    ``vault_slug`` is the basename of the doc's ``vault_path`` minus the
    trailing ``.md`` (e.g. ``2026-05-06-3508c63e-ai-cos-jam-session``). Phase B
    feeds this into a wiki-link of the form ``[[<vault_slug>|<title>]]`` so
    the existing Quartz link rewriter routes it to the right ``_ingested/``
    page. ``None`` for docs that have not been mirrored to the vault yet
    (rare — should resolve on the next ``brain vault export`` / sync).
    """

    document_id: str
    title: str
    source_kind: str
    date: datetime | None
    vault_slug: str | None


@dataclass
class PersonRecord:
    """Aggregated roster for one person, ready for page emission.

    ``display_name`` is the lowercased / normalized form stored in
    ``directory_entries`` (see :func:`brain.vault.derived_links.participants
    .normalize_participant`). Phase B's renderer is responsible for turning
    it into a presentable form (e.g. title-casing) — keeping the canonical
    matching key here means the data layer round-trips losslessly.
    """

    slug: str
    display_name: str
    primary_email: str
    all_emails: list[str]
    docs: list[DocRef]
    in_people_yml: bool


# ---- Internal helpers -------------------------------------------------------


@dataclass
class _DirectoryIndex:
    """Pre-computed views over ``directory_entries`` used during aggregation.

    Built once per :func:`aggregate_people` call (one SELECT) so per-doc
    resolution is dictionary lookups rather than repeated SQL.
    """

    # Set of every ``display_name`` ever recorded with at least one (name,
    # email) pair. Empty-name rows (``display_name=''``) are excluded — those
    # are bare-email Gmail headers with no resolvable identity.
    known_names: set[str]

    # Per-name, the email with the highest sum-of-occurrences across all
    # sources. ``people_yml`` rows win unconditionally (mirrors the precedence
    # in :meth:`DirectoryStore.resolve_name_to_email`).
    primary_email_by_name: dict[str, str]

    # Per-name, the sorted distinct list of every email seen across sources.
    emails_by_name: dict[str, list[str]]

    # Reverse index: email → canonical display_name. Used to resolve email
    # participant keys to a person. ``people_yml`` wins; otherwise the name
    # with the highest summed occurrence_count for that email.
    canonical_name_by_email: dict[str, str]

    # Per-name: True iff at least one ``directory_entries`` row for this name
    # has ``source='people_yml'``. Drives the curated badge on the page and
    # the always-emit override below the doc-count threshold.
    in_people_yml_by_name: dict[str, bool]


def _build_directory_index(conn: psycopg.Connection[Any]) -> _DirectoryIndex:
    """Pull ``directory_entries`` once and project the indexes the resolver
    needs. See :class:`_DirectoryIndex` for what each index represents.
    """
    rows = conn.execute(
        "SELECT display_name, email, source, occurrence_count "
        "FROM directory_entries "
        "WHERE display_name <> ''"
    ).fetchall()

    # (name, email) → summed count across sources, plus a flag tracking whether
    # any source is people_yml.
    counts: dict[tuple[str, str], int] = {}
    is_people_yml: dict[tuple[str, str], bool] = {}

    for name, email, source, count in rows:
        key = (name, email)
        counts[key] = counts.get(key, 0) + int(count)
        if source == "people_yml":
            is_people_yml[key] = True

    known_names: set[str] = set()
    name_emails: dict[str, list[tuple[str, int, bool]]] = {}
    email_names: dict[str, list[tuple[str, int, bool]]] = {}

    for (name, email), total in counts.items():
        people_yml = is_people_yml.get((name, email), False)
        known_names.add(name)
        name_emails.setdefault(name, []).append((email, total, people_yml))
        email_names.setdefault(email, []).append((name, total, people_yml))

    primary_email_by_name: dict[str, str] = {}
    emails_by_name: dict[str, list[str]] = {}
    in_people_yml_by_name: dict[str, bool] = {}

    for name, items in name_emails.items():
        emails_by_name[name] = sorted({email for email, _, _ in items})
        in_people_yml_by_name[name] = any(p for _, _, p in items)
        # people_yml rows win regardless of count. If multiple people_yml rows
        # exist for one name (caller bug — _people.yml should map one name to
        # one email) the alphabetically-first email wins for determinism.
        people_yml_emails = sorted(email for email, _, p in items if p)
        if people_yml_emails:
            primary_email_by_name[name] = people_yml_emails[0]
            continue
        # Otherwise: highest-summed-count, ties broken alphabetically.
        ranked = sorted(items, key=lambda triple: (-triple[1], triple[0]))
        primary_email_by_name[name] = ranked[0][0]

    canonical_name_by_email: dict[str, str] = {}
    for email, candidates in email_names.items():
        people_yml_names = sorted(
            (name, total) for name, total, p in candidates if p
        )
        if people_yml_names:
            # Prefer the highest-occurrence people_yml entry; alpha tiebreak
            # so the same input directory always produces the same answer.
            ranked_yml = sorted(people_yml_names, key=lambda pair: (-pair[1], pair[0]))
            canonical_name_by_email[email] = ranked_yml[0][0]
            continue
        ranked = sorted(candidates, key=lambda triple: (-triple[1], triple[0]))
        canonical_name_by_email[email] = ranked[0][0]

    return _DirectoryIndex(
        known_names=known_names,
        primary_email_by_name=primary_email_by_name,
        emails_by_name=emails_by_name,
        canonical_name_by_email=canonical_name_by_email,
        in_people_yml_by_name=in_people_yml_by_name,
    )


def _doc_participant_keys(
    *, source_kind: str, metadata: dict[str, Any]
) -> set[str]:
    """Extract the raw participant key set for one doc.

    Mirrors :func:`brain.vault.derived_links.pass_runner._build_snapshot`'s
    extraction step but emits *both* the email and the display name (when
    present) for each Gmail header pair, leaving directory bridging to the
    resolution step. Result is identical to the pass-runner's output once
    resolution runs.

    For Krisp, reads ``metadata['_participant_keys']`` (a sorted list set at
    ingest time by :func:`extract_krisp_speakers`). Strings are returned
    as-is; bridging from name to email happens during resolution.
    """
    keys: set[str] = set()
    if source_kind == "gmail":
        for display, email in extract_gmail_addresses(metadata):
            keys.add(email)
            if display:
                keys.add(display)
    elif source_kind == "krisp":
        raw = metadata.get("_participant_keys")
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, str) and entry.strip():
                    keys.add(entry.strip())
    return keys


def _resolve_key_to_person(
    key: str,
    *,
    directory: _DirectoryIndex,
) -> str | None:
    """Map a participant key to a canonical display_name, or None.

    ``key`` is either an email (``fixture@example.com``) or a normalized display
    name. Emails resolve via ``canonical_name_by_email``; names resolve to
    themselves only if they appear in ``known_names`` (so a Krisp speaker
    label like ``"random one-off name"`` that has no directory entry is
    silently dropped — exactly the long-tail noise we want to filter).
    """
    lower = key.lower()
    if "@" in lower:
        return directory.canonical_name_by_email.get(lower)
    if lower in directory.known_names:
        return lower
    return None


def _doc_date(
    *,
    source_kind: str,
    metadata: dict[str, Any],
    sent_at: datetime | None,
) -> datetime | None:
    """Pick the best timestamp for a doc. ``sent_at`` (Gmail typed column)
    wins; otherwise parse ``metadata['date']`` per source convention.

    Returns ``None`` for docs we can't date — the renderer sorts these last.
    """
    if sent_at is not None:
        return sent_at
    raw = metadata.get("date")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if source_kind == "krisp":
        # Krisp ingest passes ISO date strings (YYYY-MM-DD or YYYY-MM-DDTHH:MM).
        # Slice the first 10 chars so the strict ``date.fromisoformat`` grammar
        # still accepts the time-suffix variant.
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            logger.debug("krisp date parse failed: %r", text)
            return None
    # Gmail (and any RFC-5322 source).
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        logger.debug("gmail date parse failed: %r", text)
        return None
    return parsed


def _vault_slug(vault_path: str | None) -> str | None:
    """Strip the trailing ``.md`` and any directory prefix from ``vault_path``.

    Phase B uses this as the inner of a ``[[<slug>|<title>]]`` wiki-link; the
    existing link rewriter (``brain.vault.link_rewrite``) resolves bare slugs
    against the doc-title index during sync, so the basename is enough.
    """
    if not vault_path:
        return None
    base = posixpath.basename(vault_path)
    if base.endswith(".md"):
        base = base[: -len(".md")]
    return base or None


def _sort_docs(docs: list[DocRef]) -> list[DocRef]:
    """Sort by date desc, then title asc. Docs with ``date=None`` sort last."""
    def key(d: DocRef) -> tuple[int, float, str]:
        if d.date is None:
            return (1, 0.0, d.title)
        # Negate timestamp so ``sorted`` (asc) yields date-desc.
        return (0, -d.date.timestamp(), d.title)

    return sorted(docs, key=key)


def _assign_slugs(records: list[PersonRecord]) -> list[PersonRecord]:
    """Resolve slug collisions in a deterministic alpha-by-display_name pass.

    First person at a given base slug keeps it; subsequent collisions get
    ``-2``, ``-3``, … per the plan. Mutates each record's ``slug`` in place
    and returns the same list for caller convenience.
    """
    counts: dict[str, int] = {}
    for rec in sorted(records, key=lambda r: r.display_name):
        base = slugify(rec.display_name)
        used = counts.get(base, 0)
        rec.slug = base if used == 0 else f"{base}-{used + 1}"
        counts[base] = used + 1
    return records


# ---- Public API -------------------------------------------------------------


def aggregate_people(
    conn: psycopg.Connection[Any],
    *,
    owner_keys: frozenset[str],
    min_docs: int,
) -> list[PersonRecord]:
    """Aggregate per-person doc rosters from ``directory_entries`` + ``documents``.

    The returned list is sorted alphabetically by ``display_name`` and ready
    for page emission (slugs assigned, collisions resolved).

    Arguments:
        conn: Live Postgres connection. Read-only — no writes performed.
        owner_keys: Lowercased identifiers (emails AND/OR display names) that
            count as the corpus owner. Stripped from every doc's participant
            key set so the owner doesn't appear in *every* doc list. Persons
            whose canonical identity (display_name OR primary_email OR every
            email in ``all_emails``) sits entirely inside ``owner_keys`` are
            dropped from the result — same semantics as the derived-link
            owner filter in
            :func:`brain.vault.derived_links.pass_runner._build_snapshot`.
        min_docs: Threshold for non-curated emission. Persons with strictly
            fewer than ``min_docs`` docs and no ``_people.yml`` entry are
            dropped. Curated persons (``in_people_yml=True``) always emit
            regardless. Required positional — the default lives one layer up
            in :class:`brain.config.Config` (Phase C).

    Raises:
        ValueError: ``min_docs`` is negative.
    """
    if min_docs < 0:
        raise ValueError(f"min_docs must be >= 0 (got {min_docs!r})")

    directory = _build_directory_index(conn)

    # Pull every gmail/krisp document. Drafts (``draft=TRUE``) are excluded
    # — they're already filtered from the rendered wiki, so a People Hub page
    # listing a draft would surface a doc the user doesn't see anywhere else.
    rows = conn.execute(
        """
        SELECT d.id::text, d.title, s.kind, d.metadata, d.vault_path, d.sent_at
        FROM documents d
        JOIN sources s ON s.id = d.source_id
        WHERE s.kind IN ('gmail', 'krisp')
          AND d.draft = FALSE
        """
    ).fetchall()

    # person_name → list of DocRefs (deduped within a doc — multiple keys for
    # the same doc resolving to one person count once).
    person_docs: dict[str, list[DocRef]] = {}

    for doc_id, title, source_kind, metadata, vault_path, sent_at in rows:
        meta: dict[str, Any] = dict(metadata) if metadata else {}
        keys = _doc_participant_keys(source_kind=source_kind, metadata=meta)
        # Strip owner keys before resolution so the owner contributes nothing
        # to anyone's roster — including their own (no /people/ali-sarkis).
        keys = {k for k in keys if k.lower() not in owner_keys}

        persons_for_doc: set[str] = set()
        for key in keys:
            person = _resolve_key_to_person(key, directory=directory)
            if person is not None:
                persons_for_doc.add(person)

        if not persons_for_doc:
            continue

        date = _doc_date(source_kind=source_kind, metadata=meta, sent_at=sent_at)
        ref = DocRef(
            document_id=doc_id,
            title=title,
            source_kind=source_kind,
            date=date,
            vault_slug=_vault_slug(vault_path),
        )
        for person in persons_for_doc:
            person_docs.setdefault(person, []).append(ref)

    # Build records for every name in directory_entries (so curated 0-doc
    # entries still emit). Skip owner-identified names entirely — strict
    # interpretation per Phase A.2: a person whose primary identifier is an
    # owner key never gets a page.
    records: list[PersonRecord] = []
    for name in directory.known_names:
        if name.lower() in owner_keys:
            continue
        primary = directory.primary_email_by_name[name]
        if primary.lower() in owner_keys:
            # ``primary_email`` is always a member of ``all_emails`` (it's
            # picked from that set), so this single check is equivalent to
            # the "every email is an owner key" rule when the user has
            # listed an alias as an owner. No need for a second pass over
            # ``all_emails``.
            continue
        all_emails = directory.emails_by_name[name]
        in_yml = directory.in_people_yml_by_name.get(name, False)
        docs = _sort_docs(person_docs.get(name, []))
        if not in_yml and len(docs) < min_docs:
            continue
        records.append(
            PersonRecord(
                slug="",  # filled in by _assign_slugs
                display_name=name,
                primary_email=primary,
                all_emails=all_emails,
                docs=docs,
                in_people_yml=in_yml,
            )
        )

    _assign_slugs(records)
    records.sort(key=lambda r: r.display_name)
    return records
