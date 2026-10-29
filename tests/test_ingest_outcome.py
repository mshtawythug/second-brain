"""``brain.ingest.outcome.classify``: one IngestResult → outcome mapping for every surface.

Extracted from ``cli_ingest._ingest_outcome_verb`` (Ingest spec §5.4) so the UI can
classify a job's result without importing a Typer module. The CLI's verbs are
re-derived from ``classify`` here, and ``tests/test_ingest_outcome_verb.py`` stays
unmodified, which together show the extraction changed nothing the CLI prints.
"""
from __future__ import annotations

import pytest

from brain.cli_ingest import _ingest_outcome_verb
from brain.ingest import IngestResult
from brain.ingest.outcome import IngestOutcome, classify

pytestmark = pytest.mark.nodb

_DOC_ID = "0b6c9e1d-2a3f-4c5d-8e7f-9a0b1c2d3e4f"


def _result(
    *,
    document_id: str | None = _DOC_ID,
    created: bool = False,
    body_changed: bool = False,
    mirror_repaired: bool = False,
) -> IngestResult:
    return IngestResult(
        document_id=document_id,
        created=created,
        body_changed=body_changed,
        mirror_repaired=mirror_repaired,
    )


#: Every IngestResult shape ``_ingest_outcome_verb`` distinguishes, with the
#: (outcome, mirror_repaired) pair it must classify to and the CLI verb.
_SHAPES = [
    (_result(document_id=None), False, (IngestOutcome.EMPTY, False),
     "skipped (empty document)"),
    (_result(document_id=None, mirror_repaired=True), False, (IngestOutcome.EMPTY, False),
     "skipped (empty document)"),
    (_result(created=True), False, (IngestOutcome.INGESTED, False), "ingested"),
    (_result(created=True, mirror_repaired=True), False, (IngestOutcome.INGESTED, False),
     "ingested"),
    (_result(body_changed=True), False, (IngestOutcome.UPDATED, False), "updated"),
    (_result(), True, (IngestOutcome.UPDATED, False), "updated"),
    (_result(body_changed=True, mirror_repaired=True), False,
     (IngestOutcome.UPDATED, True), "updated (mirror repaired)"),
    (_result(mirror_repaired=True), True, (IngestOutcome.UPDATED, True),
     "updated (mirror repaired)"),
    (_result(mirror_repaired=True), False, (IngestOutcome.MIRROR_REPAIRED, True),
     "repaired mirror"),
    (_result(), False, (IngestOutcome.SKIPPED, False), "skipped (already ingested)"),
]


@pytest.mark.parametrize(("result", "force", "expected", "verb"), _SHAPES)
def test_classify_covers_every_shape_the_cli_distinguishes(
    result: IngestResult,
    force: bool,
    expected: tuple[IngestOutcome, bool],
    verb: str,
) -> None:
    assert classify(result, force=force) == expected


@pytest.mark.parametrize(("result", "force", "expected", "verb"), _SHAPES)
def test_the_cli_verb_is_re_derived_from_classify(
    result: IngestResult,
    force: bool,
    expected: tuple[IngestOutcome, bool],
    verb: str,
) -> None:
    assert _ingest_outcome_verb(result, force=force) == verb


def test_the_already_verb_still_applies_only_to_a_plain_skip() -> None:
    assert _ingest_outcome_verb(_result(), already_verb="skipped") == "skipped"
    assert _ingest_outcome_verb(_result(created=True), already_verb="x") == "ingested"


def test_the_outcome_values_are_the_wire_strings() -> None:
    """The UI puts ``outcome.value`` on the wire; the front end switches on it."""
    assert {o.value for o in IngestOutcome} == {
        "ingested", "updated", "skipped", "empty", "mirror_repaired",
    }


def test_classify_defaults_force_to_false() -> None:
    assert classify(_result()) == (IngestOutcome.SKIPPED, False)
