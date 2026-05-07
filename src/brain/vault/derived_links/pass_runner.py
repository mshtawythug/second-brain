"""Linker pass — rebuild derived_links rows for a set of touched documents."""
import datetime
import json
import logging
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast

import psycopg

from brain.vault.derived_links.directory import DirectoryStore
from brain.vault.derived_links.participants import extract_gmail_addresses
from brain.vault.derived_links.rules import (
    DocSnapshot,
    Evidence,
    rule_same_day_participant,
    rule_shared_participant,
    rule_shared_thread,
)

_logger = logging.getLogger(__name__)

# Source kinds the linker considers. Manual / vault docs never carry the
# metadata shapes the rules read, so they're excluded from both the touched
# snapshot pass and the candidate pool.
_LINKABLE_SOURCE_KINDS: frozenset[str] = frozenset({"gmail", "krisp"})


def rebuild_derived_for(
    conn: psycopg.Connection[Any],
    doc_ids: set[str],
    *,
    directory: DirectoryStore,
    owner_participants: frozenset[str] = frozenset(),
) -> tuple[int, set[str]]:
    """Rebuild ``derived_links`` rows whose src or dst is in ``doc_ids``.

    Steps (all in one transaction):
      1. SELECT touched docs + their participant keys + dates.
      2. SELECT every other Gmail/Krisp doc (candidate pool).
      3. Compute candidate pairs (de-duplicated by canonical ordering); run
         R1/R2/R3 against each.
      4. R3 supersedes R2 for the same pair.
      5. DELETE FROM derived_links WHERE src or dst IN doc_ids (capturing
         the row endpoints so callers see partners that LOST an edge in
         this pass too).
      6. UPSERT the new edge set with ``(LEAST, GREATEST)`` ordering. The
         INSERT uses ``ON CONFLICT (src, dst, rule) DO UPDATE`` so a
         concurrent rebuild from another connection (``brain vault
         sync --watch`` worker vs foreground ``brain vault sync``) cannot
         race the INSERT into a UniqueViolation — the row simply gets its
         ``evidence`` + ``weight`` refreshed to the values this rebuild
         computed.

    Returns ``(inserted_count, affected_ids)``:

    - ``inserted_count`` — number of ``derived_links`` rows inserted in
      step 6.
    - ``affected_ids`` — superset of ``doc_ids`` containing every endpoint
      that had an edge added (step 6) OR removed (step 5). This is the
      touched-set that downstream callers (Phase D's fence renderer)
      iterate over to decide which ``_ingested/`` files need their fence
      regenerated. Includes the input ``doc_ids`` even when no edges were
      added or removed, so callers can rely on the input being a subset
      of the output.

    ``owner_participants`` is the set of corpus-owner identifiers (emails
    or display names, lowercased + trimmed at config-load time per
    :class:`brain.config.Config.owner_participants`) that should be
    stripped from each :class:`DocSnapshot.participant_keys` BEFORE rule
    evaluation. Without this filter the owner is a participant on
    essentially every doc, which floods R2 / R3 with noise edges keyed
    on a single shared identity. The default (empty frozenset) is a
    fast-path no-op and preserves the historical behaviour for tests
    and any caller that hasn't opted in.

    The set short-circuits to ``(0, set())`` when ``doc_ids`` is empty —
    no DB round-trip, no transaction.
    """
    if not doc_ids:
        return 0, set()

    # 1+2. Snapshot every linkable doc once. The corpus is small (~500 rows
    #      at full scale per the spec), so a single SELECT + Python-side hash
    #      join beats per-touched-doc round-trips. Joining to ``sources``
    #      keeps manual / vault rows out — they don't carry the metadata
    #      shapes the rule functions read.
    rows = conn.execute(
        """
        SELECT d.id::text, s.kind, d.metadata
        FROM documents d
        JOIN sources s ON s.id = d.source_id
        WHERE s.kind = ANY(%s)
        """,
        (sorted(_LINKABLE_SOURCE_KINDS),),
    ).fetchall()

    snapshots: dict[str, DocSnapshot] = {}
    for row_id, source_kind, metadata in rows:
        snap = _build_snapshot(
            document_id=str(row_id),
            source_kind=str(source_kind),
            metadata=dict(metadata or {}),
            directory=directory,
            owner_participants=owner_participants,
        )
        snapshots[snap.document_id] = snap

    # Touched docs that aren't linkable (manual / vault / missing) contribute
    # no pairs. Any pre-existing edges with them are still removed by the
    # DELETE below.
    touched_in_corpus = {d for d in doc_ids if d in snapshots}

    # 3+4. Walk the touched set, pair each against the rest of the corpus,
    #      de-dupe via canonical ordering, then apply rules.
    seen_pairs: set[tuple[str, str]] = set()
    pair_evidence: list[tuple[str, str, Evidence]] = []

    for touched_id in touched_in_corpus:
        a = snapshots[touched_id]
        for other_id, b in snapshots.items():
            if other_id == touched_id:
                continue

            canonical = _canonical_pair(touched_id, other_id)
            if canonical in seen_pairs:
                continue
            seen_pairs.add(canonical)

            for evidence in _evaluate_pair(a, b):
                pair_evidence.append((canonical[0], canonical[1], evidence))

    # 5+6. DELETE then INSERT in one transaction. The DELETE scope is the
    #      original ``doc_ids`` set (not just ``touched_in_corpus``) so a
    #      caller that passes a now-deleted / kind-changed doc still has its
    #      stale edges cleared.
    #
    #      The DELETE returns the endpoints of every removed row so the
    #      affected-set captures partners that LOST an edge in this pass.
    #      Without that, a fence renderer wouldn't know to regenerate the
    #      partner's "Related" section after a deletion.
    doc_ids_list = list(doc_ids)
    affected_ids: set[str] = set(doc_ids)
    with conn.transaction():
        deleted_rows = conn.execute(
            "DELETE FROM derived_links "
            "WHERE src_document_id = ANY(%s) OR dst_document_id = ANY(%s) "
            "RETURNING src_document_id::text, dst_document_id::text",
            (doc_ids_list, doc_ids_list),
        ).fetchall()
        for src, dst in deleted_rows:
            affected_ids.add(str(src))
            affected_ids.add(str(dst))
        for src, dst, evidence in pair_evidence:
            # ``ON CONFLICT DO UPDATE`` makes the INSERT race-safe against a
            # concurrent rebuild (e.g. ``brain vault sync --watch`` worker
            # firing on a file event while a foreground ``brain vault sync``
            # commits a fresh ``derived_links`` row in the same window).
            # Without this, our DELETE-then-INSERT could DELETE a row, observe
            # a concurrent INSERT of the same canonical pair from a sibling
            # transaction, and crash on UniqueViolation. The semantics are
            # preserved: rebuild's intent is "make these rows reflect the
            # current snapshot", and an UPSERT does exactly that — refreshes
            # ``evidence`` + ``weight`` to the freshly-computed values.
            conn.execute(
                """
                INSERT INTO derived_links
                    (src_document_id, dst_document_id, rule, evidence, weight)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (src_document_id, dst_document_id, rule)
                DO UPDATE SET evidence = EXCLUDED.evidence,
                              weight = EXCLUDED.weight
                """,
                (
                    src,
                    dst,
                    evidence.rule,
                    json.dumps(evidence.payload),
                    evidence.weight,
                ),
            )
            affected_ids.add(src)
            affected_ids.add(dst)

    _logger.info(
        "rebuilt %d derived edges across %d touched docs "
        "(%d in corpus, %d affected ids)",
        len(pair_evidence),
        len(doc_ids),
        len(touched_in_corpus),
        len(affected_ids),
    )
    return len(pair_evidence), affected_ids


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Return ``(LEAST, GREATEST)`` so the same unordered pair always orders identically.

    Mirrors the SQL ``LEAST(src, dst) / GREATEST(src, dst)`` canonicalization
    that backstops ``derived_links``' ``UNIQUE (src, dst, rule)`` constraint.
    """
    return (a, b) if a < b else (b, a)


def _evaluate_pair(a: DocSnapshot, b: DocSnapshot) -> list[Evidence]:
    """Run R1, R2, R3 against ``(a, b)`` and apply R3-supersedes-R2.

    R1 and R3/R2 are independent rules and may co-exist on the same pair
    (different ``derived_links.rule`` rows). Within the participant family,
    R3 (same-day) is strictly stronger than R2 (no date constraint), so R2
    is suppressed when R3 fires.
    """
    evidences: list[Evidence] = []

    r1 = rule_shared_thread(a, b)
    if r1 is not None:
        evidences.append(r1)

    r3 = rule_same_day_participant(a, b)
    if r3 is not None:
        evidences.append(r3)
    else:
        r2 = rule_shared_participant(a, b)
        if r2 is not None:
            evidences.append(r2)

    return evidences


def _build_snapshot(
    *,
    document_id: str,
    source_kind: str,
    metadata: dict[str, Any],
    directory: DirectoryStore,
    owner_participants: frozenset[str] = frozenset(),
) -> DocSnapshot:
    """Project a DB row into a :class:`DocSnapshot` for rule evaluation.

    Gmail keys are derived from ``from``/``to`` headers via
    :func:`extract_gmail_addresses`, with each ``(display, email)`` pair
    contributing the email plus either the directory-resolved email for
    ``display`` or the normalized display name itself.

    Krisp keys come from ``metadata['_participant_keys']`` (populated at
    ingest time by :func:`brain.ingest._apply_pre_insert_metadata`). Name-only
    keys are bridged to emails via :meth:`DirectoryStore.resolve_name_to_email`
    so cross-source linking works without baking the directory into the
    pre-insert step.

    ``owner_participants`` (lowercased at config load) is subtracted from the
    final key set — emails AND display-name keys both run through the same
    ``key.lower() in owner_participants`` filter — so the corpus owner can't
    create participant-overlap edges between every doc they're on. The
    default (empty frozenset) skips the dict-comp entirely (zero overhead
    for callers that haven't opted in).
    """
    if source_kind == "gmail":
        keys = _gmail_participant_keys(metadata, directory)
    elif source_kind == "krisp":
        keys = _krisp_participant_keys(metadata, directory)
    else:  # pragma: no cover - SELECT filter keeps us here
        keys = set()

    if owner_participants:
        keys = {k for k in keys if k.lower() not in owner_participants}

    return DocSnapshot(
        document_id=document_id,
        source_kind=cast(Literal["gmail", "krisp", "manual"], source_kind),
        metadata=metadata,
        participant_keys=frozenset(keys),
        date=_parse_date(metadata.get("date"), source_kind=source_kind),
    )


def _gmail_participant_keys(
    metadata: dict[str, Any], directory: DirectoryStore
) -> set[str]:
    """Build participant keys for a Gmail snapshot.

    Each ``(display, email)`` pair contributes the email itself; the display
    name (if present) is run through the directory — if it resolves to an
    email, that email is added (catches the ``"person-x"`` → ``person-a@…`` bridge);
    otherwise the normalized display name is added so a Krisp doc that only
    knows the name can still match.
    """
    keys: set[str] = set()
    for display, email in extract_gmail_addresses(metadata):
        keys.add(email)
        if display:
            resolved = directory.resolve_name_to_email(display)
            keys.add(resolved if resolved else display)
    return keys


def _krisp_participant_keys(
    metadata: dict[str, Any], directory: DirectoryStore
) -> set[str]:
    """Build participant keys for a Krisp snapshot.

    Reads ``metadata['_participant_keys']`` (sorted list, ingest-time output)
    and bridges name-only keys to emails via the directory so a Krisp call
    labeled ``**Ali Sarkis | 0:01**`` can match a Gmail with
    ``from: "Ali Sarkis <redacted@example.com>"``.
    """
    raw = metadata.get("_participant_keys")
    if not isinstance(raw, list):
        return set()

    keys: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        token = entry.strip()
        if not token:
            continue
        if "@" in token:
            keys.add(token)
            continue
        resolved = directory.resolve_name_to_email(token)
        keys.add(resolved if resolved else token)
    return keys


def _parse_date(
    raw: Any, *, source_kind: str
) -> datetime.date | None:
    """Parse ``metadata['date']`` per source convention.

    - Krisp stores ISO date strings (``2026-04-15`` or ``2026-04-15T12:00``)
      passed through ``brain ingest-stdin --date``.
    - Gmail stores RFC 5322 strings (``Wed, 15 Apr 2026 12:00:00 -0700``).

    Returns ``None`` for missing, non-string, or unparseable values — R3
    quietly degrades when the date is missing rather than crashing the pass.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    if source_kind == "krisp":
        # ``date.fromisoformat`` is strict YYYY-MM-DD on 3.11+. Slicing the
        # first 10 characters tolerates the ``YYYY-MM-DDTHH:MM:SS`` variant
        # without pulling in ``datetime.fromisoformat``'s wider but still
        # not-RFC-5322 grammar.
        try:
            return datetime.date.fromisoformat(text[:10])
        except ValueError:
            _logger.debug("krisp date parse failed: %r", text)
            return None

    # Gmail (and any future RFC-5322 source).
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        _logger.debug("gmail date parse failed: %r", text)
        return None
    if parsed is None:
        return None
    return parsed.date()
