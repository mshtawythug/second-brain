"""Aggregate per-person doc rosters and emit the People Hub wiki pages.

Two layers, side-by-side in this module so the page renderer can never
drift from the data shape it's rendering.

Phase A — aggregation (pure read, no I/O):
    :func:`aggregate_people` SELECTs from ``directory_entries`` and
    ``documents`` and returns a sorted list of :class:`PersonRecord`,
    one per emittable page.

Phase B — page emission (pure rendering + atomic-per-file writeback):
    :func:`render_person_md` and :func:`render_index_md` are pure
    string producers — same input always yields byte-identical output,
    which is what makes :func:`emit_people_pages` idempotent.
    :func:`emit_people_pages` is the I/O-side orchestrator: it
    aggregates, computes the target file set, deletes any
    ``<vault>/people/*.md`` no longer in the set, and writes each
    page atomically through :func:`brain.vault._atomic.atomic_write_text`.

The Phase A aggregation mirrors the participant-key derivation used
by the metadata linker (see ``brain.vault.derived_links.pass_runner
._build_snapshot``) so a person's roster on the hub is consistent
with the derived edges that link their docs to each other. The only
intentional divergence: this module does not bridge name↔email at
extraction time. Both keys are emitted per doc, and
``directory_entries`` is consulted at *resolution* time to map each
key to a canonical display_name. The end result is identical because
every name and email a key could resolve to also lives in
``directory_entries``.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import psycopg

from brain.vault._atomic import atomic_write_text
from brain.vault.derived_links.directory import _score_directory_rows
from brain.vault.derived_links.participants import extract_gmail_addresses
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.paths import safe_wikilink_alias, strip_md_extension
from brain.vault.slug import slugify

logger = logging.getLogger(__name__)

__all__ = [
    "DocRef",
    "EmitReport",
    "PersonRecord",
    "aggregate_people",
    "emit_people_pages",
    "humanize_display_name",
    "render_index_md",
    "render_person_md",
]


@dataclass
class DocRef:
    """One document associated with a person — used to render their roster.

    ``vault_target`` is the doc's ``vault_path`` in POSIX form with the
    trailing ``.md`` stripped (e.g.
    ``_ingested/krisp/2026-05-06-3508c63e-ai-cos-jam-session``). Phase B
    feeds this into a wiki-link of the form ``[[<vault_target>|<title>]]``
    so :func:`brain.vault.link_rewrite.rewrite_wiki_links` resolves it
    in one shot via ``_resolve_by_vault_path`` (matching the recent-rail
    homepage precedent in :func:`brain.wiki.build_homepage._render_bullets`).

    ``None`` for docs that have not been mirrored to the vault yet (rare
    — should resolve on the next ``brain vault export`` / sync).
    """

    document_id: str
    title: str
    source_kind: str
    date: datetime | None
    vault_target: str | None


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

    # Both per-name primary-email picks and per-email canonical-name picks
    # share the same precedence rules as ``DirectoryStore.resolve_name_to_email``
    # (people_yml wins → highest summed count → alpha tiebreak); reuse that
    # module's helper so the rules cannot drift. ``skip_ambiguous=False``
    # because every person needs *some* primary email and every email needs
    # *some* canonical name — alpha tiebreak is the deterministic fallback.
    for name, items in name_emails.items():
        emails_by_name[name] = sorted({email for email, _, _ in items})
        in_people_yml_by_name[name] = any(p for _, _, p in items)
        winner = _score_directory_rows(items, skip_ambiguous=False)
        # ``items`` is non-empty by construction (the outer loop only adds a
        # name when at least one (name, email) row exists); ``_score_directory_rows``
        # therefore returns a non-None winner. ``assert`` keeps mypy honest.
        assert winner is not None
        primary_email_by_name[name] = winner

    canonical_name_by_email: dict[str, str] = {}
    for email, candidates in email_names.items():
        winner = _score_directory_rows(candidates, skip_ambiguous=False)
        assert winner is not None
        canonical_name_by_email[email] = winner

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


def _vault_target(vault_path: str | None) -> str | None:
    """Project ``vault_path`` to the inner of a wiki-link, sans ``.md``.

    Phase B feeds the result into ``[[<vault_target>|<title>]]``. The full
    POSIX-form path is preserved (rather than just the basename) so
    :func:`brain.vault.resolver._resolve_by_vault_path` matches the link
    against ``documents.vault_path`` during ``brain vault sync`` —
    bare-basename inner text would only resolve in Quartz, leaving every
    person-page wiki-link recorded in ``unresolved_links`` on the brain
    side. Mirrors the canonical wiki-link target shape emitted by
    :func:`brain.wiki.build_homepage._render_bullets` for the recent rail.

    Delegates the ``.md``-stripping + POSIX normalization to
    :func:`brain.vault.paths.strip_md_extension` so the three vault
    renderers (homepage, people, daily-index) cannot drift on the
    canonical link-target shape.

    Returns ``None`` for docs without a ``vault_path`` (rare — Krisp/Gmail
    rows that haven't been mirrored to the vault yet). The renderer
    surfaces those as plain-text titles with no wiki-link.
    """
    if not vault_path:
        return None
    return strip_md_extension(vault_path) or None


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

    # Self-protect against future mixed-case callers — every comparison
    # against ``owner_keys`` already does ``.lower()`` on the haystack, so
    # normalizing the needle once keeps that contract explicit even when a
    # caller passes ``BRAIN_OWNER_PARTICIPANTS=Ali@Example.COM``.
    owner_keys = frozenset(k.lower() for k in owner_keys)

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
            vault_target=_vault_target(vault_path),
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


# ---- Phase B — page rendering -----------------------------------------------


# Frontmatter fields emitted on every people page. ``kind: people`` is the
# marker Phase C's Quartz overlay uses for distinct styling. ``slug`` lets
# Quartz resolve the canonical URL even when filename collisions force a
# numeric suffix on the on-disk path.
_PERSON_KIND_FRONTMATTER: str = "people"

# Frontmatter ``kind`` for the index page itself. Distinct from ``people``
# so a future Quartz override can style "directory of directories" rows
# differently from individual roster pages.
_PEOPLE_INDEX_KIND_FRONTMATTER: str = "people-index"

# Per-page directory under the vault. Kept as a module-level constant so
# the emitter, the cleanup pass, and tests refer to the same name.
_PEOPLE_DIR_NAME: str = "people"

# Plain-text placeholder for curated persons with zero matched documents.
# Phase A allows this case (a ``_people.yml`` entry the user added before
# any Krisp/Gmail data accumulated) — the page must still render so the
# user sees their entry made it into the directory.
_NO_DOCS_PLACEHOLDER: str = "*No documents yet.*"


def humanize_display_name(display_name: str) -> str:
    """Re-cap the lowercased canonical name for use in headings + frontmatter.

    PersonRecord stores names in the normalized lowercase form
    (``"person-person-luke"``) so the matching layer can compare cleanly.
    The page heading wants the human form (``"person-person-luke"``).
    ``str.title()`` is the standard multi-word capitalizer; rare
    apostrophe / camel-case names ("d'Arcy", "person-person-marc") are slightly
    mangled to "D'Arcy" / "person-person-marc" — acceptable trade-off, given the
    directory layer normalized the input in the first place. The user
    can fix any specific name by adjusting their ``_people.yml`` if it
    matters for their corpus.

    Public so the ``brain people`` CLI (Phase C) can render the same
    title-cased form in its terminal output without re-implementing
    the rule. Keep the internal alias below for backwards compat
    inside this module — every call site already routes through it.
    """
    return display_name.title()


# Internal alias — every existing call site reads from this name. Public
# helper above is the supported import surface; this private alias is
# preserved to keep the module's render functions readable.
_humanize_display_name = humanize_display_name


def _render_doc_line(doc: DocRef) -> str:
    """Render one document line of a person's roster.

    Format: ``### YYYY-MM-DD · [[<vault_target>|<safe_title>]] (<source_kind>)``.

    H3 (rather than a bullet) so each document becomes its own anchor
    in Quartz's table-of-contents — the user can jump to a specific
    doc on a person's page from the right rail. Date defaults to
    ``"undated"`` when ``DocRef.date is None`` (the renderer never
    fabricates a date — a missing one is information).

    When ``vault_target`` is ``None`` (doc not yet mirrored to the
    vault), emit the title as plain text rather than a link — the
    next sync that exports the doc will populate ``vault_path`` and
    the next refresh cycle will turn it into a link.
    """
    date_str = doc.date.strftime("%Y-%m-%d") if doc.date is not None else "undated"
    safe_title = safe_wikilink_alias(doc.title)
    link_text = (
        f"[[{doc.vault_target}|{safe_title}]]" if doc.vault_target else safe_title
    )
    return f"### {date_str} · {link_text} ({doc.source_kind})"


def _render_person_body(record: PersonRecord, *, title: str) -> str:
    """Render the body of a single person page (everything below frontmatter).

    Layout, in order:
      - ``# <title>`` (H1) — the human-readable name.
      - Primary email as an ``mailto:`` link.
      - "Other emails:" — comma-separated list of every email in
        ``all_emails`` that isn't the primary. Omitted when the person
        has only one email.
      - ``## Documents (N)`` (H2) — count is the doc total.
      - One ``### YYYY-MM-DD · [[…|…]] (kind)`` line per document, in
        the order Phase A's :func:`_sort_docs` produced (date desc,
        then title asc).
      - When ``record.docs`` is empty, the placeholder
        :data:`_NO_DOCS_PLACEHOLDER` line.

    The body always ends with a trailing newline so
    :func:`brain.vault.frontmatter.dump_frontmatter` round-trips
    cleanly through the parser. Same input always produces byte-
    identical output (idempotency contract).
    """
    lines: list[str] = [f"# {title}", ""]
    primary = record.primary_email
    lines.append(f"**Primary email:** [{primary}](mailto:{primary})")
    others = [email for email in record.all_emails if email != primary]
    if others:
        lines.append(f"**Other emails:** {', '.join(others)}")
    lines.extend(["", f"## Documents ({len(record.docs)})", ""])
    if not record.docs:
        lines.append(_NO_DOCS_PLACEHOLDER)
    else:
        lines.extend(_render_doc_line(doc) for doc in record.docs)
    lines.append("")  # ensure trailing newline
    return "\n".join(lines)


def render_person_md(record: PersonRecord) -> str:
    """Render one ``<vault>/people/<slug>.md`` page as a Markdown string.

    Output shape (frontmatter + body):

    .. code-block:: markdown

        ---
        title: person-person-luke
        slug: person-person-luke
        kind: people
        emails:
          - person-person-luke@example.com
          - person-person-luke-alt@example.com
        doc_count: 12
        in_people_yml: true
        ---

        # person-person-luke

        **Primary email:** [person-person-luke@example.com](mailto:person-person-luke@example.com)
        **Other emails:** person-person-luke-alt@example.com

        ## Documents (12)

        ### 2026-05-06 · [[_ingested/krisp/…|AI CoS Jam Session]] (krisp)
        ### 2026-04-29 · [[_ingested/gmail/…|Fwd: April Hiring Thread]] (gmail)
        …

    Frontmatter notes:
      - ``title`` is the title-cased rebuild of ``display_name``.
      - ``slug`` mirrors ``record.slug`` — the canonical URL component,
        used by the index page's wiki-links and consumed by the Phase C
        Quartz overlay.
      - ``kind: people`` is the page-class marker the overlay uses.
      - ``emails`` is the alphabetized list from
        ``record.all_emails`` (already sorted by Phase A).
      - ``doc_count`` matches ``len(record.docs)`` so the heading
        ``## Documents (N)`` and the frontmatter never disagree.
      - ``in_people_yml`` drives the curated-badge rendering.

    Pure: no I/O, no DB. Stable across runs given the same record —
    that's the property :func:`emit_people_pages` relies on for its
    "skip if byte-identical" idempotency gate.
    """
    title = _humanize_display_name(record.display_name)
    fields: dict[str, Any] = {
        "title": title,
        "slug": record.slug,
        "kind": _PERSON_KIND_FRONTMATTER,
        "emails": list(record.all_emails),
        "doc_count": len(record.docs),
        "in_people_yml": record.in_people_yml,
    }
    body = _render_person_body(record, title=title)
    return dump_frontmatter(fields, body)


def render_index_md(records: list[PersonRecord]) -> str:
    """Render the ``<vault>/people/index.md`` roster page.

    Alphabetized list of every person with a page. The plan calls for
    a Markdown table; we emit a bullet list instead because Markdown
    tables and Obsidian wiki-link aliases collide on the ``|``
    separator (``[[X|Y]]`` inside a cell breaks the table parser, and
    ``\\|`` escaping breaks the wiki-link parser). A bullet list
    preserves every signal the table would have carried — name,
    doc count, primary email, curated indicator — without the
    syntactic conflict, and matches the recent-rail bullets in
    :func:`brain.wiki.build_homepage._render_bullets`.

    Each line is shaped:

    .. code-block:: markdown

        - [✅ ] [[people/<slug>|<Display Name>]] — N docs · email@x.com

    The leading ``✅`` (always followed by a space, even when blank,
    so column alignment is byte-stable across lines) flags
    :attr:`PersonRecord.in_people_yml=True` rows. Curated entries
    with zero docs render as ``— 0 docs · …`` rather than being hidden.

    Pure: no I/O. Returns the empty-state placeholder body when
    ``records`` is empty so the index page always renders.
    """
    fields: dict[str, Any] = {
        "title": "People",
        "slug": "people",
        "kind": _PEOPLE_INDEX_KIND_FRONTMATTER,
    }
    sorted_records = sorted(records, key=lambda r: r.display_name)
    lines: list[str] = ["# People", ""]
    if not sorted_records:
        lines.append("*No people yet — add entries to `_people.yml` "
                     "or wait for the directory threshold to fill in.*")
    else:
        for rec in sorted_records:
            display = _humanize_display_name(rec.display_name)
            badge = "✅ " if rec.in_people_yml else ""
            lines.append(
                f"- {badge}[[people/{rec.slug}|{display}]] — "
                f"{len(rec.docs)} docs · {rec.primary_email}"
            )
    lines.append("")  # ensure trailing newline
    body = "\n".join(lines)
    return dump_frontmatter(fields, body)


# ---- Phase B — page emission ------------------------------------------------


@dataclass
class EmitReport:
    """Counters surfaced by :func:`emit_people_pages` for the CLI summary.

    All four fields default to zero so callers don't need a sentinel
    None-check. ``pages_written`` only counts pages whose bytes
    actually changed on disk — a re-run with no DB drift returns
    ``pages_written=0`` (idempotency contract). ``pages_deleted``
    counts pages removed because the corresponding person was
    dropped from the directory (e.g. a ``_people.yml`` entry the
    user removed). ``index_written`` is a bool surfaced as int so
    the CLI's "rows changed" line is one consistent format.
    """

    pages_written: int = 0
    pages_deleted: int = 0
    index_written: bool = False
    skipped_unchanged: int = 0
    deleted_paths: list[Path] = field(default_factory=list)


def _people_dir(vault_path: Path) -> Path:
    """Return ``<vault_path>/people``. Created on demand by the emitter."""
    return vault_path / _PEOPLE_DIR_NAME


def _existing_person_pages(people_dir: Path) -> set[Path]:
    """Return every ``<people_dir>/<slug>.md`` currently on disk.

    Excludes ``index.md`` (managed separately) and any path that isn't
    a regular ``.md`` file (subdirectories, dotfiles). The set is the
    "what's there now" half of the cleanup contract — the emitter
    diffs it against the freshly-aggregated target set and removes
    anything in the prior-set-but-not-target.

    Returns an empty set when ``people_dir`` doesn't exist yet (first
    run on a fresh vault) — the emitter creates the directory on the
    first write.
    """
    if not people_dir.is_dir():
        return set()
    out: set[Path] = set()
    for entry in people_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        if entry.name == "index.md":
            continue
        out.add(entry)
    return out


def _write_if_changed(target: Path, rendered: str) -> bool:
    """Write ``rendered`` to ``target`` iff its bytes differ from what's there.

    Returns ``True`` iff the file was actually written (bytes drifted),
    ``False`` on a no-op skip. The skip preserves the file's mtime so
    the Quartz watcher doesn't fire a needless rebuild — same property
    the recent-rail emitter relies on.

    Atomic — sibling tempfile + ``os.replace`` via
    :func:`brain.vault._atomic.atomic_write_text`. A crash mid-write
    leaves the prior version intact.

    Creates the parent directory on demand so the first run on a
    fresh vault doesn't require a separate scaffold step.
    """
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as exc:
            # Read failure is not fatal — we'll still attempt the write.
            # Logged at DEBUG because the more interesting signal is the
            # write attempt that follows.
            logger.debug("people emit: could not read %s for compare: %s", target, exc)
            existing = None
        if existing == rendered:
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, rendered)
    return True


def emit_people_pages(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    owner_keys: frozenset[str],
    min_docs: int,
) -> EmitReport:
    """Aggregate, render, and write every ``<vault>/people/<slug>.md`` + index.

    Pipeline (per run):

    1. **Aggregate.** Call :func:`aggregate_people` to get the
       canonical roster — slug-collision-resolved, owner-filtered,
       threshold-applied.
    2. **Compute the target set.** ``{<slug>.md for slug in records}``.
       The index page is tracked separately (``index.md``) so its
       presence never gets confused with a curated person.
    3. **Cleanup pass.** List every ``<people_dir>/*.md`` currently
       on disk (excluding ``index.md``), and ``unlink`` any path not
       in the target set. This is what propagates "user removed
       person-person-luke from ``_people.yml``" to the wiki: the next emit drops
       ``person-person-luke.md``.
    4. **Write each per-person page.** Skip files whose rendered
       bytes match what's already there (idempotency). Atomic write
       via :func:`brain.vault._atomic.atomic_write_text`.
    5. **Write the index page.** Same skip-if-unchanged logic.

    Args:
        conn: live DB connection. Reads only — no writes.
        vault_path: the vault root. ``<vault_path>/people/`` is
            managed; nothing outside it is touched.
        owner_keys: forwarded to :func:`aggregate_people` as-is.
        min_docs: forwarded to :func:`aggregate_people` as-is.

    Returns:
        :class:`EmitReport` with counters for the CLI summary line.
        ``deleted_paths`` carries the actual unlinked paths so callers
        (and tests) can inspect what cleanup did.

    The function never raises on individual page write failures —
    failures are logged at WARNING and the run continues so one bad
    file doesn't drop the whole hub. Aggregation errors propagate
    (a SQL failure means we can't safely produce a target set, and
    deleting pages against a partial target would lose data).
    """
    records = aggregate_people(
        conn, owner_keys=owner_keys, min_docs=min_docs
    )

    people_dir = _people_dir(vault_path)
    target_paths: set[Path] = {people_dir / f"{rec.slug}.md" for rec in records}

    report = EmitReport()

    # Cleanup pass first — deletes are independent of writes.
    for stale in sorted(_existing_person_pages(people_dir) - target_paths):
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning(
                "people emit: could not delete stale page %s: %s — skipping",
                stale, exc,
            )
            continue
        report.pages_deleted += 1
        report.deleted_paths.append(stale)

    # Per-person pages.
    for rec in records:
        target = people_dir / f"{rec.slug}.md"
        rendered = render_person_md(rec)
        try:
            changed = _write_if_changed(target, rendered)
        except OSError as exc:
            logger.warning(
                "people emit: could not write %s: %s — skipping",
                target, exc,
            )
            continue
        if changed:
            report.pages_written += 1
        else:
            report.skipped_unchanged += 1

    # Index page.
    index_target = people_dir / "index.md"
    index_rendered = render_index_md(records)
    try:
        report.index_written = _write_if_changed(index_target, index_rendered)
    except OSError as exc:
        logger.warning(
            "people emit: could not write index %s: %s",
            index_target, exc,
        )
        report.index_written = False

    return report
