"""Ingest pipeline: extract → chunk → embed → store."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
from pgvector.psycopg import register_vector  # noqa: F401  (ensures adapter loaded)

from brain.errors import EnrichmentError, IngestAmbiguousSource, OllamaUnavailable
from brain.tags import normalize_tags

if TYPE_CHECKING:
    from brain.enrichment import OllamaEnricher
from brain.vault.derived_links.directory import (
    DirectoryStore,
    GwsRunner,
    refresh_calendar,
    refresh_contacts,
)
from brain.vault.derived_links.participants import (
    extract_gmail_addresses,
    extract_krisp_speakers,
)
from brain.vault.export import regenerate_vault_file

from .chunker import chunk_text
from .sub_tokens import extract_sub_tokens

_logger = logging.getLogger(__name__)

# Contacts refresh is rate-limited to once per 24 hours. Krisp ingest is the
# trigger; without this gate, every transcript would re-fetch the full Google
# People page.
_CONTACTS_REFRESH_INTERVAL = timedelta(hours=24)


class Embedder(Protocol):
    """Narrow interface for embedding clients used by the ingest pipeline.

    ``dim`` is the embedder's native output dimension. Schema-wiring code
    (``db.ensure_embedding_column``, ``queries.finalize_embedding_index``)
    reads it to keep the ``chunks.embedding`` column in lockstep with the
    active backend, so callers stay backend-agnostic.
    """

    dim: int

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...


@dataclass
class ExtractedDoc:
    """A document produced by an extractor and ready to be ingested."""

    title: str
    content: str
    content_type: str
    source_path: str | None
    metadata: dict[str, Any]


@dataclass
class IngestResult:
    """Outcome of :func:`ingest_document`.

    ``body_changed`` is ``True`` iff ``existing_hash != incoming_hash`` at the
    moment the in-place UPDATE was applied.  Concretely:

    - New document (``created=True``): always ``True``.
    - In-place UPDATE (``created=False``) where the stored content_hash
      differs from the incoming hash: ``True`` — body was rewritten.
    - In-place UPDATE where hashes match (same body, ``--force`` or changed
      tags): ``False`` — no body change even though chunks were mechanically
      rebuilt.
    - No-op short-circuit (same hash, no ``--force``): ``False``.

    Note: ``_update_doc_in_place`` always rebuilds chunks (DELETE + INSERT)
    regardless of ``body_changed`` — chunk row UUIDs change on every forced
    re-ingest, but ``body_changed`` reports the *content-hash identity*, not
    the chunk-row machinery.  The mirror trigger in :func:`ingest_document`
    reads ``body_changed OR force`` so an in-place update with only tag changes
    still propagates to disk.
    """

    document_id: str | None
    created: bool
    body_changed: bool = False


@dataclass
class UpdateResult:
    """Outcome of :func:`update_document`.

    ``fields_changed`` lists the document columns that were actually mutated
    (subset of ``{"title", "content", "content_type", "metadata", "tags"}``).
    ``rechunked`` is ``True`` iff the body was replaced and chunks were
    re-embedded.
    """

    document_id: str
    fields_changed: list[str]
    rechunked: bool


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_search_metadata(title: str, tags: list[str]) -> tuple[str, str]:
    """Compute the per-document ``(title_text, tags_text)`` chunk columns.

    Migration 009 splits chunks.tsv into a weighted multi-field tsvector — the
    title goes under weight ``A`` and the tags under weight ``B``. Both are
    denormalized onto every chunk row so the FTS rewrite stays a single
    GIN-indexable expression. This helper centralizes the projection so all
    four ``INSERT INTO chunks`` sites + the ``sync_chunk_search_metadata``
    helper in :mod:`brain.queries` agree on the exact textual form.

    ``tags_text`` mirrors the SQL backfill pattern
    ``array_to_string(d.tags, ' ')`` — space-joined, empty string when the
    tag list is empty (NOT ``None``; we never write SQL NULL here so the
    coalesce inside the generated tsv stays a no-op rather than a noisy
    cast).
    """
    return title, " ".join(tags) if tags else ""


def _is_gmail_thread_doc(doc: ExtractedDoc, source_kind: str) -> bool:
    """Return True iff ``doc`` is a P2.1 merged-thread Gmail doc.

    The marker shape — ``source_kind == "gmail"`` AND
    ``content_type == "email_thread"`` AND ``metadata.thread_id`` populated
    with a non-empty string — is the same triple that the partial unique
    index ``uq_documents_gmail_thread`` (migration 008) constrains. Anything
    else (legacy ``content_type == "email"`` per-message rows, manual
    ``email_thread`` docs without a thread_id) falls through to the legacy
    content_hash dedup path. Centralising the check here keeps the call
    sites in :func:`ingest_document` and :func:`_ingest_within_transaction`
    in lockstep — divergence between them would silently break the upsert.
    """
    thread_id = doc.metadata.get("thread_id")
    return (
        source_kind == "gmail"
        and doc.content_type == "email_thread"
        and isinstance(thread_id, str)
        and bool(thread_id)
    )


# Metadata keys promoted into typed columns on ``documents``. The mapping is
# intentionally narrow: every key here must correspond to a column added by
# migration 007. Anything outside this set stays in the JSONB ``metadata``
# blob, untouched.
_PROMOTED_COLUMNS: tuple[str, ...] = (
    "thread_id",
    "rfc_message_id",
    "in_reply_to",
    "sent_at",
    "participants",
    "duration_min",
)


def _parse_sent_at(raw: Any) -> datetime | None:
    """Parse a date string into a TZ-aware UTC ``datetime``.

    Accepts both RFC 2822 (Gmail ``Date:`` header style — e.g.
    ``"Tue, 04 May 2026 14:23:01 -0400"``) and ISO 8601 (Krisp-style — e.g.
    ``"2026-05-04T14:23:01+00:00"``) inputs. Returns ``None`` for any input
    we can't parse — callers log + skip the column rather than crashing the
    ingest. Naive datetimes are treated as UTC.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    parsed: datetime | None = None
    # RFC 2822 first — ``parsedate_to_datetime`` is strict-but-tolerant and
    # is the format Gmail's ``Date:`` header uses, so it's the more common
    # case for this codebase.
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            # ``fromisoformat`` accepts the trailing-Z form in 3.11+.
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _promote_metadata_to_columns(metadata: dict[str, Any]) -> dict[str, Any]:
    """Project a metadata blob onto the typed ``documents`` columns.

    Returns a ``{column_name: value}`` dict — keys are limited to the columns
    in :data:`_PROMOTED_COLUMNS`. Missing-or-``None`` metadata keys are
    omitted (callers never need to write a NULL explicitly — the column
    default of NULL handles it on INSERT, and on UPDATE we just leave the
    column alone). Malformed values are logged at WARNING and skipped so a
    bad header never blocks an ingest.

    Type-coercion rules:

    - ``thread_id`` / ``rfc_message_id`` / ``in_reply_to`` — accept ``str``;
      anything else is logged + skipped.
    - ``date`` (RFC 2822 or ISO 8601 string) → ``sent_at`` (TZ-aware UTC).
      Unparseable strings log + skip; we never store a partially-parsed
      datetime.
    - ``participants`` — accept ``list[str]`` (Krisp speakers, Gmail headers
      already-parsed by an upstream extractor). Non-list / mixed-type lists
      log + skip rather than silently coercing.
    - ``duration_min`` — accept ``int`` (or a ``str`` that parses cleanly).
      Floats like ``42.7`` round down via ``int(...)``; non-numeric values
      log + skip.
    """
    out: dict[str, Any] = {}

    for key in ("thread_id", "rfc_message_id", "in_reply_to"):
        raw = metadata.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            out[key] = raw
        else:
            _logger.warning(
                "metadata.%s expected str, got %s; skipping column promotion",
                key,
                type(raw).__name__,
            )

    raw_date = metadata.get("date")
    if raw_date is not None:
        sent_at = _parse_sent_at(raw_date)
        if sent_at is not None:
            out["sent_at"] = sent_at
        else:
            _logger.warning(
                "metadata.date is not parseable (%r); leaving sent_at NULL",
                raw_date,
            )

    raw_participants = metadata.get("participants")
    if raw_participants is not None:
        if isinstance(raw_participants, list) and all(
            isinstance(p, str) for p in raw_participants
        ):
            out["participants"] = list(raw_participants)
        else:
            _logger.warning(
                "metadata.participants expected list[str], got %r; "
                "skipping column promotion",
                type(raw_participants).__name__,
            )

    raw_duration = metadata.get("duration_min")
    if raw_duration is not None:
        try:
            # ``bool`` is a subclass of ``int`` in Python — reject it
            # explicitly so ``True`` doesn't end up as ``1`` in the column.
            if isinstance(raw_duration, bool):
                raise TypeError("bool is not a valid duration")
            out["duration_min"] = int(raw_duration)
        except (TypeError, ValueError):
            _logger.warning(
                "metadata.duration_min expected int, got %r; "
                "skipping column promotion",
                raw_duration,
            )

    return out


def _upsert_source(
    conn: psycopg.Connection,
    *,
    kind: str,
    external_id: str | None,
    metadata: dict[str, Any],
) -> str | None:
    """Return an existing source row id, or insert a new one. Returns None for
    purely manual ingests with no external id and no metadata."""
    if external_id is None and not metadata:
        return None
    row = conn.execute(
        "SELECT id FROM sources WHERE kind=%s AND external_id IS NOT DISTINCT FROM %s",
        (kind, external_id),
    ).fetchone()
    if row:
        return str(row[0])
    new = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) VALUES (%s, %s, %s) RETURNING id",
        (kind, external_id, json.dumps(metadata)),
    ).fetchone()
    assert new is not None  # RETURNING id always yields a row
    return str(new[0])


def ingest_document(
    conn: psycopg.Connection,
    *,
    embedder: Embedder,
    doc: ExtractedDoc,
    source_kind: str | None = None,
    source_external_id: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    force: bool = False,
    gws_runner: GwsRunner | None = None,
    vault_root: Path | None = None,
    draft: bool = False,
    enricher: OllamaEnricher | None = None,
    enrich: bool = True,
    enrich_min_tokens: int = 50,
) -> IngestResult:
    """Ingest a single extracted document.

    Dedup rules (evaluated in order):

    1. **Gmail thread** — deduped by ``(thread_id, content_type='email_thread')``.
       Re-ingest of the same thread UPDATEs the existing row in place (UUID
       stable; links / derived_links / unresolved_links preserved).

    2. **File-based** (``doc.source_path`` is not ``None``) — deduped by
       ``source_path`` (``WHERE source_path=%s AND kind='ingested'``). When a
       match is found the row is UPDATEd in place: the document UUID is
       preserved, tags are **union**-merged (existing ∪ incoming, insertion
       order), and ``links`` / ``derived_links`` / ``unresolved_links``
       referencing the UUID survive. Chunks are always rebuilt. When no
       source_path match exists the doc is INSERTed as a new row directly —
       migration 006 narrows the content_hash UNIQUE constraint to stdin-only
       rows, so the INSERT cannot collide with another file at a different path.
       ``force=True`` forces the UPDATE even when the content_hash is unchanged.

    3. **Sourced stdin** (``source_path`` is ``None``, ``source_external_id``
       is not ``None`` — Krisp, Slack, ...) — deduped by
       ``(source_kind, source_external_id)`` via a JOIN on ``sources``. When a
       match is found the row is UPDATEd in place (same UUID / tag union /
       link-preservation semantics as the file branch). When no sourced row
       exists, control falls through to rule 4.

    4. **Content-hash fallback** — reached only when ``source_path is None``
       (stdin ingests). Deduped by SHA-256 of ``doc.content`` scoped to
       ``kind='ingested' AND source_path IS NULL``. Same-hash is a no-op;
       ``force=True`` does DELETE + INSERT (new UUID).

    Tags are **union**-merged on all UPDATE paths: existing curated tags are
    preserved; incoming tags are added. The union is computed via
    ``normalize_tags([*existing, *incoming])`` which is idempotent (casefold +
    hyphenate) and preserves first-seen insertion order.

    Sources are deduped by ``(kind, external_id)``. A repeat ingest pointing
    at the same external id reuses the existing source row.

    Source defaults: file-based ingests (``doc.source_path`` set) default to
    ``source_kind="manual"`` with ``source_external_id=doc.source_path`` when
    the caller does not pass them, so the resulting document carries a
    ``source_id`` pointing at a ``sources`` row with ``kind="manual"``.
    Without this, downstream consumers (vault-export frontmatter, the graph
    filter chips) lose the manual-source signal. Explicit ``source_kind`` /
    ``source_external_id`` arguments always win, so a file-based ingest can
    still be tagged as e.g. ``krisp`` if needed. Stdin ingests must pass
    ``source_kind`` explicitly — they get no default.

    Source-specific side effects (Gmail directory upserts, Krisp directory
    refresh triggers) are dispatched via :func:`_run_source_hooks`. The
    ``gws_runner`` argument is only consulted by the Krisp hook; passing
    ``None`` skips the calendar / contacts refresh with a logged warning so
    callers without a runner wired (early CLI paths, tests) still succeed.

    Vault mirror: when ``vault_root`` is supplied AND the call actually wrote
    a row (``result.created`` or ``force``), the corresponding mirror file
    under ``vault_root / _ingested/<source>/...md`` is regenerated via
    :func:`brain.vault.export.regenerate_vault_file`. The call runs OUTSIDE
    the DB transaction — a filesystem failure is logged at WARNING and
    swallowed so a transient mirror error never rolls back a successful
    ingest. Recovery is via ``brain vault export --force``. Library callers
    (tests, internal pipelines) that don't care about the mirror omit
    ``vault_root`` and get the legacy DB-only behavior unchanged. Named
    ``vault_root`` to distinguish from the per-document
    ``documents.vault_path`` relative path.

    Wave Q1-D enrichment: when ``enrich=True`` (default) AND ``enricher`` is
    not None, a post-ingest hook generates a 2-3 sentence summary via local
    Ollama and writes it to ``documents.summary`` inside the same
    transaction. Skip rules: caller passes ``enrich=False``, content is
    shorter than ``enrich_min_tokens`` tokens, ``documents.summary`` is
    already populated with the same ``content_hash``, or Ollama is
    unavailable (logged at WARNING — the ingest still commits;
    ``brain enrich --backfill`` picks the row up later). Passing
    ``enricher=None`` with ``enrich=True`` is a no-op with a debug log
    (library callers / tests that don't care about enrichment leave it
    None).
    """
    if doc.source_path is not None:
        if source_kind is None:
            source_kind = "manual"
        if source_external_id is None:
            source_external_id = doc.source_path
    if source_kind is None:
        raise ValueError(
            "source_kind is required when doc.source_path is None"
        )

    # Gmail-thread upsert (P2.2): a merged-thread doc keys on the stable
    # Gmail ``threadId`` rather than a per-message ``messageId``. Mirroring
    # that invariant onto the ``sources`` row keeps source dedup aligned
    # with the new partial unique index ``uq_documents_gmail_thread`` —
    # repeated thread-batched ingests reuse the same ``sources`` row instead
    # of accumulating one per re-ingest. The override only fires when the
    # caller actually produced a thread doc (``content_type='email_thread'``
    # AND ``metadata.thread_id`` populated); legacy per-message gmail rows
    # still pass message_id as ``source_external_id`` and are unaffected.
    if _is_gmail_thread_doc(doc, source_kind):
        thread_id = doc.metadata.get("thread_id")
        # _is_gmail_thread_doc guarantees thread_id is a non-empty str.
        assert isinstance(thread_id, str) and thread_id
        source_external_id = thread_id

    h = _content_hash(doc.content)
    tags = tags or []
    source_metadata = source_metadata or {}

    result = _ingest_within_transaction(
        conn,
        embedder=embedder,
        doc=doc,
        source_kind=source_kind,
        source_external_id=source_external_id,
        source_metadata=source_metadata,
        tags=tags,
        force=force,
        gws_runner=gws_runner,
        content_hash=h,
        draft=draft,
        enricher=enricher,
        enrich=enrich,
        enrich_min_tokens=enrich_min_tokens,
    )

    # Mirror writes happen OUTSIDE the transaction so a filesystem error
    # cannot roll back the DB ingest (CLAUDE.md: prefer recoverable drift
    # over an aborted ingest — drift is exactly what `brain vault export`
    # exists to reconcile). ``body_changed`` covers the gmail-thread
    # in-place upsert case where ``created`` stays ``False`` but the body
    # was rewritten — without it the on-disk mirror would lag the DB.
    if (
        vault_root is not None
        and result.document_id is not None
        and (result.created or result.body_changed or force)
    ):
        try:
            # ``force=True`` because we just inserted/replaced the DB row —
            # we know a change happened. Without it, the body-hash skip in
            # ``_write_doc_file`` would mask any frontmatter-only delta
            # (relevant when ``--force`` rewrites a doc with the same body
            # but different metadata).
            regenerate_vault_file(
                conn, result.document_id, vault_path=vault_root, force=True
            )
        except OSError as exc:
            # Only OSError is reachable here: ``regenerate_vault_file``'s
            # ValueError paths (no document with id, kind='vault') are both
            # impossible for a row we just inserted with default
            # ``kind='ingested'``. Catching ValueError would mask unrelated
            # bugs.
            _logger.warning(
                "vault mirror write failed for document %s: %s; "
                "DB ingest succeeded — recover via `brain vault export`",
                result.document_id,
                exc,
            )

    return result


def _ingest_within_transaction(
    conn: psycopg.Connection,
    *,
    embedder: Embedder,
    doc: ExtractedDoc,
    source_kind: str,
    source_external_id: str | None,
    source_metadata: dict[str, Any],
    tags: list[str],
    force: bool,
    gws_runner: GwsRunner | None,
    content_hash: str,
    draft: bool = False,
    enricher: OllamaEnricher | None = None,
    enrich: bool = True,
    enrich_min_tokens: int = 50,
) -> IngestResult:
    """Body of :func:`ingest_document` that runs inside the DB transaction.

    Extracted so the public function can wrap the post-commit vault mirror
    write outside the ``with conn.transaction()`` block — a filesystem
    failure must not roll back a successful DB ingest. Early returns from
    this helper exit the surrounding ``with conn.transaction()`` cleanly
    (commit on normal return, rollback on exception).
    """
    h = content_hash
    is_thread = _is_gmail_thread_doc(doc, source_kind)
    with conn.transaction():
        if is_thread:
            # P2.2: gmail-thread upsert. Lookup keys on the
            # ``(kind, thread_id, content_type='email_thread')`` triple
            # constrained by ``uq_documents_gmail_thread``. Same-body
            # re-ingests short-circuit; body-changed re-ingests UPDATE
            # in place so the document UUID stays stable across thread
            # growth (links / derived_links / unresolved_links keep
            # pointing at the same row instead of cascade-dropping).
            thread_id = doc.metadata.get("thread_id")
            assert isinstance(thread_id, str) and thread_id  # _is_gmail_thread_doc
            existing_thread = conn.execute(
                "SELECT id, content_hash, draft FROM documents "
                "WHERE thread_id=%s AND kind='ingested' "
                "AND content_type='email_thread'",
                (thread_id,),
            ).fetchone()
            if existing_thread is not None:
                existing_id, existing_hash, existing_draft = existing_thread
                # Q1-A safety rule: if the incoming ingest is draft-only
                # (``draft=True``) but the stored thread is already published
                # (``existing_draft=False``), skip the update entirely.
                #
                # Why: ``brain ingest-gmail --since N`` returns only messages
                # that arrived within the time window. When the window captures
                # a draft reply on a thread whose sent messages are older than
                # N days, ``to_extracted_thread`` sees only the draft message
                # and returns a draft-only body. Updating the stored doc with
                # that partial body would overwrite the full sent-message
                # content with a draft-only view — "publishing draft-only
                # content" even though ``draft`` is blocked from flipping.
                # The stored doc already reflects the true (published) state of
                # the thread; the draft reply is ephemeral and may never be
                # sent, so the safest action is a no-op.
                if draft and not existing_draft:
                    return IngestResult(
                        document_id=str(existing_id),
                        created=False,
                        body_changed=False,
                    )
                if not force and existing_hash == h:
                    return IngestResult(
                        document_id=str(existing_id),
                        created=False,
                        body_changed=False,
                    )
                # Body changed iff the existing row's hash differs from
                # the incoming. ``force=True`` may have brought us here
                # with identical hashes (an explicit re-process request) —
                # in that case the body has NOT changed; the D11 skip
                # inside ``_enrich_post_ingest_hook`` will short-circuit
                # the re-summary correctly. When hashes differ, the
                # update is a real body refresh and the hook must
                # re-enrich (Codex finding 1 fix).
                body_changed = existing_hash != h
                _update_doc_in_place(
                    conn,
                    embedder=embedder,
                    document_id=str(existing_id),
                    doc=doc,
                    source_kind=source_kind,
                    source_external_id=source_external_id,
                    source_metadata=source_metadata,
                    tags=tags,
                    content_hash=h,
                    body_changed=body_changed,
                    gws_runner=gws_runner,
                    draft=draft,
                    enricher=enricher,
                    enrich=enrich,
                    enrich_min_tokens=enrich_min_tokens,
                )
                return IngestResult(
                    document_id=str(existing_id),
                    created=False,
                    body_changed=body_changed,
                )
            # No existing thread doc — fall through to the standard INSERT
            # path. The partial unique index ``uq_documents_gmail_thread``
            # protects against concurrent dual-insert: a second concurrent
            # transaction reaching this branch raises IntegrityError on
            # the INSERT below, which bubbles up to the caller.
        elif doc.source_path is not None:
            # File-based ingest: dedup by source_path. UPDATE in place when a
            # matching row is found so the UUID (and any FK references via
            # links/derived_links/unresolved_links) is preserved across
            # content changes.
            #
            # No DB-level UNIQUE on source_path — two concurrent CLI
            # invocations for the same path can race past this SELECT and both
            # INSERT, producing two rows for one path. Acceptable for sequential
            # CLI use; would need a UNIQUE INDEX or pg_advisory_xact_lock on a
            # hash of source_path if invoked concurrently.
            existing_row = conn.execute(
                "SELECT id, content_hash FROM documents "
                "WHERE source_path=%s AND kind='ingested'",
                (doc.source_path,),
            ).fetchone()
            if existing_row:
                existing_id, existing_hash = existing_row
                if not force and existing_hash == h:
                    return IngestResult(
                        document_id=str(existing_id), created=False, body_changed=False
                    )
                body_changed = existing_hash != h
                _update_doc_in_place(
                    conn,
                    embedder=embedder,
                    document_id=str(existing_id),
                    doc=doc,
                    source_kind=source_kind,
                    source_external_id=source_external_id,
                    source_metadata=source_metadata,
                    tags=tags,
                    content_hash=h,
                    body_changed=body_changed,
                    gws_runner=gws_runner,
                    draft=draft,
                    enricher=enricher,
                    enrich=enrich,
                    enrich_min_tokens=enrich_min_tokens,
                )
                return IngestResult(
                    document_id=str(existing_id),
                    created=False,
                    body_changed=body_changed,
                )
            # No source_path row — exit the if/elif/else chain and proceed
            # directly to the INSERT block below. Skipping the content_hash
            # fallback is correct: migration 006 narrows the content_hash
            # UNIQUE constraint to (kind='ingested' AND source_path IS NULL),
            # so the INSERT cannot IntegrityError on hash collision with another
            # file ingest. The existing regression test
            # test_two_files_with_same_content_at_different_paths_are_separate_docs
            # locks this in.
        else:
            # source_path is None — try sourced lookup first, then fall through
            # to content_hash fallback. All inside this `else` so file ingests
            # above NEVER reach content_hash dedup.
            if source_external_id is not None:
                # Sourced-stdin branch (Krisp, Slack, ...). Dedup by
                # (source_kind, source_external_id) via JOIN on sources.
                # UPDATE in place so the UUID and all FK references survive.
                #
                # sources(kind, external_id) is UNIQUE per migration 001, but
                # one source row CAN link to multiple documents if a prior
                # `brain rm` left an orphaned source row. Raise loudly on >1
                # rows rather than silently picking one — the user must resolve.
                sourced_rows = conn.execute(
                    """
                    SELECT d.id, d.content_hash
                    FROM documents d
                    JOIN sources s ON d.source_id = s.id
                    WHERE s.kind = %s AND s.external_id = %s
                    FOR UPDATE OF d
                    """,
                    (source_kind, source_external_id),
                ).fetchall()
                if len(sourced_rows) > 1:
                    raise IngestAmbiguousSource(
                        f"Multiple documents share source ({source_kind!r}, "
                        f"{source_external_id!r}): "
                        f"{[str(r[0]) for r in sourced_rows]}. "
                        "Resolve manually before re-ingesting."
                    )
                if sourced_rows:
                    existing_id, existing_hash = sourced_rows[0]
                    if not force and existing_hash == h:
                        return IngestResult(
                            document_id=str(existing_id), created=False, body_changed=False
                        )
                    body_changed = existing_hash != h
                    _update_doc_in_place(
                        conn,
                        embedder=embedder,
                        document_id=str(existing_id),
                        doc=doc,
                        source_kind=source_kind,
                        source_external_id=source_external_id,
                        source_metadata=source_metadata,
                        tags=tags,
                        content_hash=h,
                        body_changed=body_changed,
                        gws_runner=gws_runner,
                        draft=draft,
                        enricher=enricher,
                        enrich=enrich,
                        enrich_min_tokens=enrich_min_tokens,
                    )
                    return IngestResult(
                        document_id=str(existing_id),
                        created=False,
                        body_changed=body_changed,
                    )
                # No sourced row — fall through to content_hash fallback below
                # (still inside the `else: source_path is None` block).

            # Content-hash fallback. Reached from:
            #   * sourced branch when no (kind, external_id) row found
            #   * direct path (source_external_id is None — stdin manual)
            # NOT reached from the file branch above (which exits the chain).
            #
            # Scope to stdin-only rows: migration 006 narrows the
            # documents.content_hash UNIQUE index to (kind='ingested' AND
            # source_path IS NULL). An unscoped SELECT could match a vault-tier
            # doc or a file ingest with the same body, and --force would DELETE
            # the wrong row.
            existing = conn.execute(
                "SELECT id FROM documents "
                "WHERE content_hash=%s AND kind='ingested' AND source_path IS NULL",
                (h,),
            ).fetchone()
            if existing:
                if not force:
                    return IngestResult(document_id=str(existing[0]), created=False)
                conn.execute("DELETE FROM documents WHERE id=%s", (existing[0],))

        source_id = _upsert_source(
            conn,
            kind=source_kind,
            external_id=source_external_id,
            metadata=source_metadata,
        )

        chunks = chunk_text(doc.content, count_tokens=embedder.count_tokens)
        if not chunks:
            return IngestResult(document_id=None, created=False)

        embeddings = embedder.embed(
            [c.content for c in chunks], input_type="document"
        )

        # Pre-insert: derive source-specific metadata fields (e.g. Krisp
        # ``_participant_keys``) so they're stored alongside the doc row in
        # one INSERT instead of an extra UPDATE after the fact. The leading
        # underscore on the key flags it as derived/internal — the linker
        # pass (B.4) reads it via a single SELECT.
        _apply_pre_insert_metadata(doc, source_kind=source_kind)

        # Project email/krisp metadata onto typed columns. The base INSERT
        # always writes the same fixed columns; the promoted columns ride
        # along as a dynamic suffix so a doc with no recognized metadata
        # keys produces the exact same SQL as before migration 007.
        promoted = _promote_metadata_to_columns(doc.metadata)
        promoted_columns = list(promoted.keys())
        promoted_values = [promoted[c] for c in promoted_columns]
        extra_cols = "".join(f", {c}" for c in promoted_columns)
        extra_placeholders = ", %s" * len(promoted_columns)

        doc_row = conn.execute(
            f"""
            INSERT INTO documents (source_id, title, content, content_hash, content_type,
                                   source_path, tags, metadata, draft{extra_cols})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s{extra_placeholders})
            RETURNING id
            """,
            (
                source_id,
                doc.title,
                doc.content,
                h,
                doc.content_type,
                doc.source_path,
                tags,
                json.dumps(doc.metadata),
                draft,
                *promoted_values,
            ),
        ).fetchone()
        assert doc_row is not None  # RETURNING id always yields a row
        document_id = str(doc_row[0])

        title_text, tags_text = _chunk_search_metadata(doc.title, tags)
        for c, emb in zip(chunks, embeddings, strict=True):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding, "
                "title_text, tags_text, search_extras) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    document_id,
                    c.index,
                    c.content,
                    emb,
                    title_text,
                    tags_text,
                    extract_sub_tokens(c.content),
                ),
            )

        # Wave Q1-D — Krisp action-items docs always carry the
        # ``action-items`` tag so ``brain list --tag action-items`` and
        # ``brain todo`` work out of the box. Mechanical, not LLM-derived;
        # never re-proposed by ``brain tag --auto`` (D19 + D20).
        _maybe_autotag_action_items(conn, document_id=document_id, doc=doc, tags=tags)

        # Wave Q1-D — auto-summary hook. Runs BEFORE the source-specific
        # hook (D14) so a future gmail/krisp hook that wants to consume the
        # freshly-written summary can do so. Skip rules live inside the
        # helper; failure modes (Ollama down, malformed JSON) are caught and
        # logged — the ingest itself always commits.
        _enrich_post_ingest_hook(
            conn,
            document_id=document_id,
            doc=doc,
            enricher=enricher,
            enrich=enrich,
            min_tokens=enrich_min_tokens,
            content_hash=h,
        )

        _run_source_hooks(
            conn,
            source_kind=source_kind,
            doc=doc,
            document_id=document_id,
            gws_runner=gws_runner,
        )

        return IngestResult(
            document_id=document_id, created=True, body_changed=True
        )


def _update_doc_in_place(
    conn: psycopg.Connection,
    *,
    embedder: Embedder,
    document_id: str,
    doc: ExtractedDoc,
    source_kind: str,
    source_external_id: str | None,
    source_metadata: dict[str, Any],
    tags: list[str],
    content_hash: str,
    body_changed: bool,
    gws_runner: GwsRunner | None,
    draft: bool = False,
    enricher: OllamaEnricher | None = None,
    enrich: bool = True,
    enrich_min_tokens: int = 50,
) -> None:
    """Replace title / body / metadata / tags / typed columns on an existing
    document row and rebuild its chunks. Used by the gmail-thread upsert path
    (P2.2), the file-path UPDATE-in-place branch, and the sourced-stdin
    (Krisp/Slack) UPDATE-in-place branch.

    Runs inside the caller's open transaction. The document UUID is preserved
    so any ``links`` / ``derived_links`` / ``unresolved_links`` referencing
    this doc keep pointing at the same row across re-ingest. Old chunks are
    deleted via direct DELETE (no need for ON DELETE CASCADE — we know the
    parent doc id) and re-inserted from the freshly chunked + embedded body.
    Source-specific post-ingest hooks (gmail directory upsert) re-run so
    headers from the latest message in the rebuilt thread propagate.

    Tag union semantics: existing curated tags on the row are preserved and
    merged with the incoming ``tags`` argument via
    ``normalize_tags([*existing, *incoming])``.  Incoming tags are added;
    existing tags are never removed — ``brain tag`` is the only explicit
    tag-modification surface.

    Summary-regen ordering signal: when ``body_changed`` is True this helper
    does TWO things — (1) NULL out ``summary`` / ``summary_model`` / ``summary_at``
    in the same UPDATE that overwrites ``content_hash``, so a stale summary
    describing the old body can never linger on disk, and (2) pass
    ``body_changed`` through to ``_enrich_post_ingest_hook`` so it bypasses the
    D11 idempotency check (the hook reads the freshly-updated content_hash and
    would otherwise see a match). Together, (1) handles the case where the
    hook can't regenerate (``enrich=False`` / ``enricher=None`` / content
    < min_tokens) and (2) handles the case where it can. Without (1), a body
    change with no enricher would leave the old summary on disk, misdescribing
    the new content. (Codex finding 1, 2026-05-11; Codex stop-gate finding,
    2026-05-13.)
    """
    # Partial-window guard: if the incoming extraction is all-draft but the
    # existing row is published, the ingest window is a subset of the full
    # thread (e.g. ``brain ingest-gmail --since 7d`` where only a fresh
    # draft reply falls in the window but the thread's sent messages are
    # older). The draft-only extraction is NOT authoritative — refusing the
    # UPDATE preserves the published body, metadata, chunks, and links.
    # The legitimate auto-flip direction (TRUE→FALSE when a sent reply
    # arrives) is unaffected because this guard only triggers on draft=True.
    if draft:
        draft_existing = conn.execute(
            "SELECT draft FROM documents WHERE id=%s",
            (document_id,),
        ).fetchone()
        if draft_existing is not None and not draft_existing[0]:
            return

    # FOR UPDATE SELECT locks the row against concurrent tag-writes
    # (read-modify-write safety) and reads existing tags for the union merge.
    # body_changed is signalled separately via the kwarg, so we don't need to
    # read content_hash here — the caller already computed it from the
    # pre-UPDATE hash diff.
    existing_row = conn.execute(
        "SELECT tags FROM documents WHERE id=%s FOR UPDATE",
        (document_id,),
    ).fetchone()
    existing_tags: list[str] = list(existing_row[0]) if existing_row and existing_row[0] else []

    # Union semantics: preserve existing curated tags; add incoming tags.
    # normalize_tags is idempotent (casefold + hyphenate) and preserves
    # first-seen insertion order, matching how apply_tags composes tags.
    merged_tags = normalize_tags([*existing_tags, *tags])

    # Re-evaluate the source row — same ``(kind, external_id)`` as the
    # initial insert, so this is a SELECT-with-fallback-INSERT no-op in
    # practice. Kept here so a future caller passing different
    # ``source_metadata`` still gets the source row's metadata refreshed.
    source_id = _upsert_source(
        conn,
        kind=source_kind,
        external_id=source_external_id,
        metadata=source_metadata,
    )

    chunks = chunk_text(doc.content, count_tokens=embedder.count_tokens)
    embeddings = (
        embedder.embed([c.content for c in chunks], input_type="document")
        if chunks
        else []
    )

    # Pre-insert metadata derivation (krisp adds ``_participant_keys``).
    # No-op for gmail today, but kept symmetric with the INSERT path so
    # future per-source enrichments apply on UPDATE too.
    _apply_pre_insert_metadata(doc, source_kind=source_kind)

    promoted = _promote_metadata_to_columns(doc.metadata)
    set_parts: list[str] = [
        "source_id=%s",
        "title=%s",
        "content=%s",
        "content_hash=%s",
        "content_type=%s",
        "tags=%s",
        "metadata=%s::jsonb",
        # Bump ingested_at on every content update so `brain status`'s
        # `last ingest` and the homepage recent-rail reflect actual ingest
        # activity, not just the original creation time. Default-NOW() on
        # INSERT means newly-created rows still get the right value.
        "ingested_at=NOW()",
    ]
    params: list[Any] = [
        source_id,
        doc.title,
        doc.content,
        content_hash,
        doc.content_type,
        merged_tags,
        json.dumps(doc.metadata),
    ]

    # When the body changed, NULL out the stored summary so a stale summary
    # describing the OLD body can never be left on disk. The post-ingest
    # enrich hook regenerates the summary from NULL when an enricher is
    # available; when it isn't (enrich=False, enricher=None, or content
    # shorter than min_tokens) the hook returns early — but the row is
    # now correctly NULL-summary rather than carrying a misleading
    # description of the previous body. Complements master's body_changed
    # kwarg fix to _enrich_post_ingest_hook, which handles the
    # "regenerate when possible" path; this handles the "can't regenerate,
    # don't lie" path. (Codex stop-gate finding, 2026-05-13.)
    if body_changed:
        set_parts.extend(["summary=NULL", "summary_model=NULL", "summary_at=NULL"])

    # Write the draft column directly. The entry-level partial-window
    # guard above already returned early if (existing=published,
    # incoming=draft-only), so reaching this point means either:
    #   (a) draft=False — a normal sent-message update (auto-flip TRUE→FALSE
    #       included), or
    #   (b) draft=True AND existing was also draft=True (legitimate
    #       all-draft refresh, e.g. a new draft was added to the thread).
    # In both cases the incoming ``draft`` value is authoritative.
    set_parts.append("draft=%s")
    params.append(draft)
    # Project the rebuilt thread's metadata onto the typed columns so they
    # stay in lockstep with the JSONB blob: a key that's present in the
    # new metadata writes the column; a key that's gone (e.g. an old
    # ``in_reply_to`` that no longer applies after a thread split) clears
    # the column to NULL.
    for column in _PROMOTED_COLUMNS:
        if column in promoted:
            set_parts.append(f"{column}=%s")
            params.append(promoted[column])
        else:
            set_parts.append(f"{column}=NULL")
    params.append(document_id)
    conn.execute(
        f"UPDATE documents SET {', '.join(set_parts)} WHERE id=%s",
        params,
    )

    conn.execute("DELETE FROM chunks WHERE document_id=%s", (document_id,))
    # Use merged_tags (not the incoming tags arg) so chunks.tags_text reflects
    # the full union — existing curated tags stay searchable via the weighted-B
    # tsv field even after a re-ingest that passes an empty tags list.
    title_text, tags_text = _chunk_search_metadata(doc.title, merged_tags)
    for c, emb in zip(chunks, embeddings, strict=True):
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding, "
            "title_text, tags_text, search_extras) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                document_id,
                c.index,
                c.content,
                emb,
                title_text,
                tags_text,
                extract_sub_tokens(c.content),
            ),
        )

    # Wave Q1-D — re-enrich the doc when its body actually changed.
    # The hook's D11 idempotency rule needs ``body_changed`` because the
    # ``UPDATE documents SET content_hash=%s`` above has already overwritten
    # the row's stored hash — without an explicit body-changed signal the
    # hook would always see ``existing_hash == content_hash`` and skip,
    # leaving a stale summary on disk after a body refresh (Codex
    # finding 1, 2026-05-11). The caller computed ``body_changed`` from
    # the pre-UPDATE hash diff in :func:`_ingest_within_transaction`.
    _enrich_post_ingest_hook(
        conn,
        document_id=document_id,
        doc=doc,
        enricher=enricher,
        enrich=enrich,
        min_tokens=enrich_min_tokens,
        content_hash=content_hash,
        body_changed=body_changed,
    )

    _run_source_hooks(
        conn,
        source_kind=source_kind,
        doc=doc,
        document_id=document_id,
        gws_runner=gws_runner,
    )


def _maybe_autotag_action_items(
    conn: psycopg.Connection,
    *,
    document_id: str,
    doc: ExtractedDoc,
    tags: list[str],
) -> None:
    """Apply the ``action-items`` tag to ``krisp_action_items`` docs.

    Mechanical (no LLM), idempotent (the tag rewrite below collapses
    duplicates), and silent on non-action-items docs. Called once after
    the documents INSERT in :func:`_ingest_within_transaction`.
    """
    if doc.content_type != "krisp_action_items":
        return
    if "action-items" in tags:
        return  # already present in the caller-supplied list
    # apply_tags re-normalizes; doing this in-transaction keeps the
    # documents row + tags atomic with the original ingest.
    apply_tags(conn, document_id, add=["action-items"])


def _enrich_post_ingest_hook(
    conn: psycopg.Connection,
    *,
    document_id: str,
    doc: ExtractedDoc,
    enricher: OllamaEnricher | None,
    enrich: bool,
    min_tokens: int,
    content_hash: str,
    body_changed: bool = False,
) -> None:
    """Generate + persist a 2-3 sentence summary on ``documents.summary``.

    Runs inside the caller's open transaction so the documents row + its
    summary commit atomically. Skips silently (with a debug log) when any
    of the wave-plan §3.a rules apply:

    1. ``enrich=False`` — caller passed ``--no-enrich`` or a library caller
       deliberately disabled it.
    2. ``enricher is None`` — caller didn't wire one up (legit for tests
       and internal pipelines that don't want LLM round-trips).
    3. Content shorter than ``min_tokens`` tokens — the title alone is
       already a fine summary; no point spending an LLM round-trip.
    4. ``documents.summary IS NOT NULL`` AND ``content_hash`` unchanged
       AND ``body_changed`` is False — idempotency: same body, same model
       → reuse the prior summary (D11).
    5. ``OllamaUnavailable`` — Ollama is down / 5xx; logged at WARN. The
       ingest still commits; ``brain enrich --backfill`` picks the row up
       later.
    6. :class:`EnrichmentError` (model returned malformed JSON twice in a
       row) — logged at WARN; row stays unenriched.

    The ``body_changed`` kwarg is the Codex finding 1 (2026-05-11) fix.
    Update-path callers (:func:`_update_thread_doc_in_place`,
    :func:`update_document`) UPDATE ``documents.content_hash`` to the new
    value BEFORE invoking this hook. The hook then SELECTs the row, reads
    the just-overwritten hash, and the ``existing_hash == content_hash``
    comparison is meaningless — both sides equal the new hash. The
    pre-fix behavior fell through the D11 skip and left the prior
    (stale) summary in place even when the body had changed, which
    Q2-SUMMARY-WIKI then rendered above the article body as a wiki
    lede — user-visible drift. The fix: callers that know the body
    changed (or might have) pass ``body_changed=True``; the D11 skip
    only fires when ``body_changed`` is False AND the other conditions
    hold. The INSERT call site keeps the default ``False`` because the
    row is freshly INSERTed with ``summary IS NULL`` — the first clause
    of the D11 guard naturally short-circuits there.
    """
    if not enrich:
        return
    if enricher is None:
        _logger.debug(
            "enrich hook: no enricher supplied for %s; skipping", document_id
        )
        return
    if enricher.count_tokens(doc.content) < min_tokens:
        return
    row = conn.execute(
        "SELECT summary, content_hash, summary_model FROM documents WHERE id=%s",
        (document_id,),
    ).fetchone()
    if row is None:
        return  # defensive — the row was just INSERTed
    existing_summary, existing_hash, existing_model = row
    # Idempotency D11: reuse prior summary ONLY when the body AND model both
    # match the current enricher AND the caller hasn't told us the body
    # just changed. After a ``BRAIN_ENRICH_MODEL`` upgrade (e.g.,
    # llama3.1:8b → llama3.2:8b), the existing summary is stale relative
    # to the new model — re-enrich so improvements propagate.
    if (
        existing_summary is not None
        and not body_changed
        and existing_hash == content_hash
        and existing_model == enricher.model
    ):
        return
    try:
        result = enricher.summarize(doc.title, doc.content)
    except OllamaUnavailable as exc:
        _logger.warning(
            "auto-summary skipped for %s: Ollama unavailable (%s); "
            "run `brain enrich --backfill` later",
            document_id,
            exc,
        )
        return
    except EnrichmentError as exc:
        _logger.warning(
            "auto-summary failed for %s: %s; row marked unenriched",
            document_id,
            exc,
        )
        return
    conn.execute(
        "UPDATE documents SET summary=%s, summary_model=%s, summary_at=NOW() "
        "WHERE id=%s",
        (result.summary, result.model, document_id),
    )


def _apply_pre_insert_metadata(doc: ExtractedDoc, *, source_kind: str) -> None:
    """Mutate ``doc.metadata`` in place to add derived fields by source.

    Currently:
    - ``krisp`` → ``_participant_keys`` (sorted list of normalized speaker
      keys parsed from the transcript body). Always present after this call,
      even if the body has no speaker labels (empty list signals "this doc
      was processed by the linker pre-stage" to downstream code).
    """
    if source_kind == "krisp":
        doc.metadata["_participant_keys"] = sorted(extract_krisp_speakers(doc.content))


def _gmail_post_ingest_hook(
    conn: psycopg.Connection, doc: ExtractedDoc, document_id: str
) -> None:
    """Upsert (display_name, email) pairs from the Gmail From/To headers.

    Runs inside the outer ``conn.transaction()`` opened by
    :func:`ingest_document` so a directory-write failure rolls the
    document back too. ``document_id`` is unused today but kept in the
    signature for symmetry with future hooks that may need it.
    """
    del document_id  # symmetry with other source hooks
    store = DirectoryStore(conn)
    for display_name, email in extract_gmail_addresses(doc.metadata):
        store.upsert_pair(
            display_name=display_name,
            email=email,
            source="gmail",
        )


def _krisp_post_ingest_hook(
    conn: psycopg.Connection,
    doc: ExtractedDoc,
    document_id: str,
    runner: GwsRunner | None,
) -> None:
    """Trigger an incremental Calendar refresh + a stale-only Contacts refresh.

    Both refreshes degrade soft: ``refresh_calendar`` / ``refresh_contacts``
    catch runner failures internally and return 0 so a Krisp ingest never
    fails on a transient gws hiccup.

    Calendar window:
    - First run (no ``directory_refresh_state`` row for ``calendar``) →
      since = ``YYYY-01-01T00:00:00+00:00`` (current-year start, UTC).
    - Subsequent runs → since = the stored ``last_refreshed_at``.
    - until = ``datetime.now(tz=UTC)`` (always).

    Contacts cadence:
    - Runs only if no state row exists OR ``last_refreshed_at`` is older
      than 24 hours. Prevents re-fetching the People API on every ingest.
    """
    del doc, document_id  # unused — krisp metadata mutation happens pre-insert
    if runner is None:
        _logger.warning(
            "krisp post-ingest: no gws_runner provided; "
            "skipping calendar/contacts refresh"
        )
        return

    now = datetime.now(tz=UTC)

    cal_row = conn.execute(
        "SELECT last_refreshed_at FROM directory_refresh_state "
        "WHERE source = 'calendar'"
    ).fetchone()
    if cal_row is not None and cal_row[0] is not None:
        since = cal_row[0]
    else:
        since = datetime(now.year, 1, 1, tzinfo=UTC)
    refresh_calendar(conn, since=since, until=now, runner=runner)

    contacts_row = conn.execute(
        "SELECT last_refreshed_at FROM directory_refresh_state "
        "WHERE source = 'contacts'"
    ).fetchone()
    contacts_stale = (
        contacts_row is None
        or contacts_row[0] is None
        or contacts_row[0] < now - _CONTACTS_REFRESH_INTERVAL
    )
    if contacts_stale:
        refresh_contacts(conn, runner=runner)


def _run_source_hooks(
    conn: psycopg.Connection,
    *,
    source_kind: str,
    doc: ExtractedDoc,
    document_id: str,
    gws_runner: GwsRunner | None,
) -> None:
    """Dispatch to the source-specific post-ingest hook (Gmail / Krisp / ...).

    Sources without a registered hook are a no-op. New sources should add a
    new ``_<kind>_post_ingest_hook`` function and a branch here — the body of
    :func:`ingest_document` stays untouched (Open/Closed).
    """
    if source_kind == "gmail":
        _gmail_post_ingest_hook(conn, doc, document_id)
    elif source_kind == "krisp":
        _krisp_post_ingest_hook(conn, doc, document_id, gws_runner)


def apply_tags(
    conn: psycopg.Connection,
    document_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    """Add and/or remove tags on a document; return the resulting tag list.

    ``add`` is unioned with the existing tags (idempotent — re-adding an
    existing tag is a no-op); ``remove`` strips any matching tags. Operations
    run in a single transaction. Caller is responsible for resolving any
    UUID prefix to a full ``document_id`` before calling.

    Inputs are passed through :func:`brain.tags.normalize_tags` before the
    DB write — this is the single ongoing-enforcement boundary that keeps
    ``brain tag <id> +COMPANY_REDACTED`` storing ``company-ko`` and lets a remove of
    ``COMPANY_REDACTED`` match a row that's currently stored as ``company-ko``.
    """
    # Deferred import: ``brain.queries`` imports the ``Embedder`` Protocol
    # from this module, so a top-level ``from ..queries import ...`` would
    # cycle at package-load time. The function-local import resolves cleanly
    # because both modules have finished initializing by the time
    # ``apply_tags`` is first called.
    from ..queries import sync_chunk_search_metadata

    add = normalize_tags(add or [])
    remove = normalize_tags(remove or [])
    with conn.transaction():
        if add:
            conn.execute(
                "UPDATE documents SET tags = ARRAY(SELECT DISTINCT unnest(tags || %s::text[])) "
                "WHERE id = %s",
                (add, document_id),
            )
        if remove:
            conn.execute(
                "UPDATE documents SET tags = ARRAY(SELECT t FROM unnest(tags) AS t "
                "WHERE t <> ALL(%s::text[])) WHERE id = %s",
                (remove, document_id),
            )
        # Migration 009 denormalizes documents.tags onto chunks.tags_text so
        # the weighted tsv reflects tag changes. Run inside the same
        # transaction that wrote the new tags so a rollback (e.g. SELECT
        # below failing) keeps chunks and parent in lockstep. The IS
        # DISTINCT FROM guards in the helper make this a no-op when add /
        # remove produced no net change.
        if add or remove:
            sync_chunk_search_metadata(conn, document_id)
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (document_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"document not found: {document_id}")
    return list(row[0] or [])


_MIRROR_FRONTMATTER_FIELDS = frozenset(
    {"title", "tags", "metadata", "content_type", "draft", "summary"}
)


def update_document(
    conn: psycopg.Connection,
    *,
    document_id: str,
    embedder: Embedder | None = None,
    new_title: str | None = None,
    new_content_type: str | None = None,
    new_content: str | None = None,
    metadata_patch: dict[str, Any] | None = None,
    replace_metadata: bool = False,
    new_tags: list[str] | None = None,
    new_draft: bool | None = None,
    vault_root: Path | None = None,
    enricher: OllamaEnricher | None = None,
    enrich: bool = True,
    enrich_min_tokens: int = 50,
) -> UpdateResult:
    """Update one document in place.

    Body changes (``new_content``) re-chunk + re-embed atomically: the prior
    chunks are deleted and new ones are inserted in the same transaction.
    Metadata defaults to a shallow merge — top-level keys overwrite, nested
    objects are not deep-merged. Set ``replace_metadata=True`` to swap the
    blob entirely.

    ``new_draft`` flips the top-level ``documents.draft`` boolean column
    introduced by migration 007. Unlike metadata-promoted columns this lives
    directly on ``documents``; passing ``True`` / ``False`` writes the new
    value, ``None`` leaves it alone. The wiki build (Quartz contentIndex
    emitter) hides ``draft=true`` docs from the explorer/graph/search;
    ``brain search`` / ``brain list`` still surface them so the user can
    re-publish.

    Raises :class:`ValueError` if ``new_content`` is empty/whitespace-only or
    if its SHA-256 collides with another document. ``embedder`` is required
    when ``new_content`` is provided. Empty/no-op edits are not an error and
    return an :class:`UpdateResult` with ``fields_changed=[]``.

    Vault mirror: when ``vault_root`` is supplied AND the edit actually
    changed the body or any frontmatter-bearing field
    (``title`` / ``tags`` / ``metadata`` / ``content_type`` / ``draft``),
    the mirror file under ``vault_root`` is regenerated via
    :func:`brain.vault.export.regenerate_vault_file`. Vault-tier rows
    (``kind='vault'``) are skipped via a DB pre-check — those files are
    file-source-of-truth and ``vault sync`` reconciles back to the DB.
    A filesystem error (``OSError``) on the mirror write logs a WARNING
    and does NOT roll back the DB update — drift is recoverable via
    ``brain vault export --force``. Named ``vault_root`` to distinguish
    from the per-document ``documents.vault_path`` relative path.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT title, content, content_type, metadata, tags, kind, draft "
            "FROM documents WHERE id=%s",
            (document_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"document not found: {document_id}")
        (
            cur_title,
            cur_content,
            cur_type,
            cur_meta,
            cur_tags,
            cur_kind,
            cur_draft,
        ) = row
        cur_meta = dict(cur_meta or {})
        cur_tags = list(cur_tags or [])

        fields_changed: list[str] = []
        sets: list[str] = []
        params: list[Any] = []

        rechunked = False
        new_hash: str | None = None
        if new_content is not None:
            if embedder is None:
                raise ValueError("embedder is required when new_content is provided")
            stripped = new_content.strip()
            if not stripped:
                raise ValueError("content is empty")
            if new_content != cur_content:
                new_hash = _content_hash(new_content)
                clash = conn.execute(
                    "SELECT id FROM documents WHERE content_hash=%s AND id<>%s",
                    (new_hash, document_id),
                ).fetchone()
                if clash:
                    raise ValueError(
                        f"content collides with existing document {clash[0]}"
                    )
                rechunked = True

        if new_title is not None and new_title != cur_title:
            sets.append("title=%s")
            params.append(new_title)
            fields_changed.append("title")

        if new_content_type is not None and new_content_type != cur_type:
            sets.append("content_type=%s")
            params.append(new_content_type)
            fields_changed.append("content_type")

        if metadata_patch is not None:
            new_meta = (
                metadata_patch if replace_metadata else {**cur_meta, **metadata_patch}
            )
            if new_meta != cur_meta:
                sets.append("metadata=%s::jsonb")
                params.append(json.dumps(new_meta))
                fields_changed.append("metadata")
                # Re-project the new metadata blob onto typed columns, and
                # diff against what we'd project from the prior blob. This
                # keeps the typed columns in lockstep with metadata: a
                # patch that adds ``thread_id`` populates the column; a
                # replace_metadata that drops the key NULLs the column.
                old_promoted = _promote_metadata_to_columns(cur_meta)
                new_promoted = _promote_metadata_to_columns(new_meta)
                for column in _PROMOTED_COLUMNS:
                    if column in new_promoted:
                        if new_promoted[column] != old_promoted.get(column):
                            sets.append(f"{column}=%s")
                            params.append(new_promoted[column])
                    elif column in old_promoted:
                        # replace_metadata dropped the source key — clear
                        # the typed column so it no longer disagrees with
                        # the JSONB blob.
                        sets.append(f"{column}=NULL")

        if new_tags is not None and sorted(new_tags) != sorted(cur_tags):
            sets.append("tags=%s")
            params.append(list(new_tags))
            fields_changed.append("tags")

        if new_draft is not None and bool(new_draft) != bool(cur_draft):
            sets.append("draft=%s")
            params.append(bool(new_draft))
            fields_changed.append("draft")

        if rechunked:
            assert new_hash is not None  # set above when rechunked is True
            assert embedder is not None  # checked above
            sets.append("content=%s")
            params.append(new_content)
            sets.append("content_hash=%s")
            params.append(new_hash)
            fields_changed.append("content")

            conn.execute(
                "DELETE FROM chunks WHERE document_id=%s", (document_id,)
            )
            assert new_content is not None  # gated by the empty-check above
            chunks = chunk_text(new_content, count_tokens=embedder.count_tokens)
            if chunks:
                embeddings = embedder.embed(
                    [c.content for c in chunks], input_type="document"
                )
                # Project the post-update title/tags onto the new chunks so
                # the weighted tsv reflects the user's new edits, not the
                # pre-edit state. Falls back to current values when the
                # caller didn't pass an override; the surrounding ``UPDATE
                # documents`` writes the same values back to the documents
                # row, so the chunks and parent stay in lockstep.
                final_title = new_title if new_title is not None else cur_title
                final_tags = list(new_tags) if new_tags is not None else cur_tags
                title_text, tags_text = _chunk_search_metadata(final_title, final_tags)
                for c, emb in zip(chunks, embeddings, strict=True):
                    conn.execute(
                        "INSERT INTO chunks (document_id, chunk_index, content, "
                        "embedding, title_text, tags_text, search_extras) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            document_id,
                            c.index,
                            c.content,
                            emb,
                            title_text,
                            tags_text,
                            extract_sub_tokens(c.content),
                        ),
                    )

        if sets:
            # Same rationale as update_document: bump ingested_at so
            # `brain status`'s last-ingest stat and the vault-export
            # `updated:` frontmatter field reflect actual edits.
            sets.append("ingested_at=NOW()")
            params.append(document_id)
            conn.execute(
                f"UPDATE documents SET {', '.join(sets)} WHERE id=%s",
                params,
            )

        # Migration 009 denormalizes documents.title / documents.tags onto
        # chunks.title_text / chunks.tags_text. After ``rechunked`` the
        # freshly-inserted chunks already carry the post-update values, so
        # the helper's IS DISTINCT FROM guards turn this into a no-op for
        # those rows. For the no-rechunk path (title-only or tags-only
        # edit), this is the call that propagates the change to every
        # existing chunk so the weighted tsv stays consistent.
        if "title" in fields_changed or "tags" in fields_changed:
            from ..queries import sync_chunk_search_metadata
            sync_chunk_search_metadata(conn, document_id)

        # Wave Q1-D — re-enrich on body change. The ``rechunked`` gate
        # above only fires when ``new_content != cur_content``, so reaching
        # this point means the body provably changed — pass
        # ``body_changed=True`` so the D11 idempotency check inside
        # ``_enrich_post_ingest_hook`` knows the existing summary is now
        # stale relative to the just-written body (Codex finding 1 fix —
        # without this kwarg the hook reads back the just-updated row
        # whose ``content_hash`` already matches ``new_hash``, the
        # ``existing_hash == content_hash`` check trivially passes, and
        # the hook short-circuits leaving the prior body's summary
        # rendered in the Q2-SUMMARY-WIKI lede above the new body).
        # Build an ExtractedDoc-shaped object lazily — we only need
        # ``title`` and ``content`` (the parts the enricher reads).
        # Re-uses the post-update title/content so an in-flight title
        # edit sees the new one.
        if rechunked:
            final_title = new_title if new_title is not None else cur_title
            assert new_content is not None  # rechunked implies new_content set
            new_doc = ExtractedDoc(
                title=final_title,
                content=new_content,
                content_type=cur_type,
                source_path=None,
                metadata={},
            )
            assert new_hash is not None  # rechunked implies new_hash set
            _enrich_post_ingest_hook(
                conn,
                document_id=document_id,
                doc=new_doc,
                enricher=enricher,
                enrich=enrich,
                min_tokens=enrich_min_tokens,
                content_hash=new_hash,
                body_changed=True,
            )

    # Mirror writes happen OUTSIDE the transaction so a filesystem error
    # cannot roll back the DB update. Triggered when the body was rechunked
    # OR any frontmatter-derived field changed (the mirror's frontmatter is
    # built from documents.title/tags/metadata/content_type — a metadata-only
    # edit must still propagate). Vault-tier rows are skipped via the DB
    # ``cur_kind`` pre-check rather than by catching ``regenerate_vault_file``'s
    # ValueError — pre-checking ``kind`` from the DB is more robust than
    # catching a ``ValueError`` and string-matching its message, since the
    # upstream message can be rephrased without notice.
    needs_mirror = rechunked or any(
        f in _MIRROR_FRONTMATTER_FIELDS for f in fields_changed
    )
    if vault_root is not None and cur_kind != "vault" and needs_mirror:
        try:
            # ``force=True`` because we've already gated on
            # ``rechunked or any(... in _MIRROR_FRONTMATTER_FIELDS ...)`` —
            # a frontmatter-only edit (tags / metadata / content_type) leaves
            # the body unchanged, so the body-hash skip in ``_write_doc_file``
            # would silently drop the rewrite and leave stale frontmatter on
            # disk. The full-corpus ``export_vault`` keeps the default
            # ``force=False`` so re-runs of that path stay cheap.
            regenerate_vault_file(
                conn, document_id, vault_path=vault_root, force=True
            )
        except OSError as exc:
            # Only OSError is reachable: ``no document with id`` is impossible
            # (we just SELECTed it), and ``kind='vault'`` is gated above.
            _logger.warning(
                "vault mirror write failed for document %s: %s; "
                "DB update succeeded — recover via `brain vault export`",
                document_id,
                exc,
            )

    return UpdateResult(
        document_id=document_id,
        fields_changed=fields_changed,
        rechunked=rechunked,
    )


_EXTRACTORS = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".docx": "docx",
}


def extract_path(path: Path) -> ExtractedDoc:
    """Dispatch to the correct extractor based on file extension.

    Raises ``ValueError`` for unsupported extensions or malformed input files;
    ``OSError`` for unreadable files. Backend-specific parser errors (e.g.
    :class:`pypdf.errors.PyPdfError`) are wrapped as ``ValueError`` so callers
    only need to handle a narrow set of exception types.
    """
    ext = Path(path).suffix.lower()
    name = _EXTRACTORS.get(ext)
    if name is None:
        raise ValueError(f"unsupported file type: {ext}")
    try:
        if name == "text":
            from .text import extract_text
            return extract_text(path)
        if name == "markdown":
            from .markdown import extract_markdown
            return extract_markdown(path)
        if name == "pdf":
            from pypdf.errors import PyPdfError

            from .pdf import extract_pdf
            try:
                return extract_pdf(path)
            except PyPdfError as e:
                raise ValueError(f"malformed PDF: {e}") from e
        if name == "docx":
            from docx.opc.exceptions import PackageNotFoundError

            from .docx import extract_docx
            try:
                return extract_docx(path)
            except PackageNotFoundError as e:
                raise ValueError(f"malformed DOCX: {e}") from e
    except UnicodeDecodeError as e:  # pragma: no cover - extractors use errors="replace"
        raise ValueError(f"could not decode file as UTF-8: {e}") from e
    raise AssertionError("unreachable")  # pragma: no cover


def supported_extensions() -> list[str]:
    """Return the list of file extensions that :func:`extract_path` can handle."""
    return list(_EXTRACTORS.keys())
