"""Classify an :class:`~brain.ingest.IngestResult` into one outcome every surface shares.

Extracted from ``cli_ingest._ingest_outcome_verb`` (Ingest spec §5.4) because the
``brain ui`` job runner must classify a result and cannot import a Typer module
(``tests/test_cli_ui.py``). The CLI keeps its wording by mapping the enum onto its
verbs; the UI puts :attr:`IngestOutcome.value` on the wire.
"""
from __future__ import annotations

from enum import StrEnum

from . import IngestResult


class IngestOutcome(StrEnum):
    """What one ingest call did, at the granularity a user is told about."""

    INGESTED = "ingested"
    UPDATED = "updated"
    SKIPPED = "skipped"
    EMPTY = "empty"
    MIRROR_REPAIRED = "mirror_repaired"


def classify(result: IngestResult, *, force: bool = False) -> tuple[IngestOutcome, bool]:
    """Return ``(outcome, mirror_repaired)`` for ``result``.

    A doc that produced no chunks (whitespace-only file, image-only PDF) comes back
    as ``document_id=None, created=False``: that is an *empty* document, not an
    unchanged re-ingest, and a repair is not representable without a document.
    ``force`` reports an in-place rewrite as an update even when the body hash is
    unchanged.

    ``result.mirror_repaired`` (#23) never *replaces* a louder outcome: a created
    document is still ``INGESTED`` (a new row gets a mirror, it does not get one
    repaired), and an update keeps ``UPDATED`` with the flag set, so a repair is
    never silently absorbed by a body change in the same run. Only when nothing
    else happened does the repair become the outcome, because reporting "skipped"
    for a run that gave an orphaned row its vault file back would teach the user
    the opposite of what happened.
    """
    if result.document_id is None:
        return IngestOutcome.EMPTY, False
    if result.created:
        return IngestOutcome.INGESTED, False
    if result.body_changed or force:
        return IngestOutcome.UPDATED, result.mirror_repaired
    if result.mirror_repaired:
        return IngestOutcome.MIRROR_REPAIRED, True
    return IngestOutcome.SKIPPED, False
