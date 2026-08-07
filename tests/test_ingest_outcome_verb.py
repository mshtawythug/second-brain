"""``_ingest_outcome_verb`` reports a mirror repair instead of "skipped" (#23).

The repair path gives an existing row its missing vault file back — the
document becomes visible in the vault, the wiki and the UI again. Before this,
the command that did that work printed **`skipped (already ingested)`**,
because the body hash was unchanged and every rule in the helper keyed off the
body.

That is the same failure class as the silent success the repair was added to
fix: a command reporting that nothing happened while something did. Worse than
merely unhelpful — it actively teaches the user their re-run had no effect, so
they stop re-running.

Pure logic over hand-built `IngestResult`s: no DB, no filesystem, so a verb
regression cannot hide behind ingest-pipeline noise.

All fixture data is synthetic.
"""
from __future__ import annotations

import pytest

from brain.cli_ingest import _ingest_outcome_verb
from brain.ingest import IngestResult

_DOC_ID = "3f2a1c9d-4b5e-6f70-8a9b-0c1d2e3f4a5b"


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


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_a_repair_is_not_reported_as_skipped() -> None:
    """The headline fix: unchanged body + repair must not read as "skipped"."""
    verb = _ingest_outcome_verb(_result(mirror_repaired=True))

    assert "skipped" not in verb
    assert verb == "repaired mirror"


def test_a_repair_is_reported_under_ingest_dir_wording_too() -> None:
    """``ingest-dir`` passes a shorter ``already_verb``; the repair still wins.

    A bulk re-run over a corpus is exactly where an orphan gets fixed without
    anyone watching, so this is the path that most needs to say so.
    """
    verb = _ingest_outcome_verb(
        _result(mirror_repaired=True), already_verb="skipped"
    )

    assert verb == "repaired mirror"


def test_a_repair_survives_the_force_flag() -> None:
    """``--force`` reports "updated" even on an unchanged body.

    Without the annotation the repair would be silently absorbed by the louder
    verb — true, but hiding the thing the user re-ran the command to get.
    """
    verb = _ingest_outcome_verb(_result(mirror_repaired=True), force=True)

    assert verb == "updated (mirror repaired)"


def test_a_repair_alongside_a_real_body_change_reports_both() -> None:
    verb = _ingest_outcome_verb(
        _result(body_changed=True, mirror_repaired=True)
    )

    assert verb == "updated (mirror repaired)"


# ---------------------------------------------------------------------------
# Everything else is unchanged
# ---------------------------------------------------------------------------


def test_unchanged_reingest_still_says_skipped() -> None:
    """The new branch must not fire when there was genuinely nothing to do."""
    assert _ingest_outcome_verb(_result()) == "skipped (already ingested)"


def test_ingest_dir_keeps_its_bare_skipped_wording() -> None:
    assert _ingest_outcome_verb(_result(), already_verb="skipped") == "skipped"


def test_a_new_document_still_says_ingested() -> None:
    assert _ingest_outcome_verb(_result(created=True)) == "ingested"


def test_a_created_document_is_not_relabelled_a_repair() -> None:
    """A brand-new row gets a mirror, it does not get one *repaired*.

    If both flags were ever set, "ingested" is the more informative headline —
    the document is new, which subsumes the mirror existing.
    """
    verb = _ingest_outcome_verb(_result(created=True, mirror_repaired=True))

    assert verb == "ingested"


def test_a_body_change_without_a_repair_still_says_updated() -> None:
    assert _ingest_outcome_verb(_result(body_changed=True)) == "updated"


def test_force_without_a_repair_still_says_updated() -> None:
    assert _ingest_outcome_verb(_result(), force=True) == "updated"


def test_an_empty_document_still_wins_over_everything() -> None:
    """No document id means no document — a repair is not representable."""
    verb = _ingest_outcome_verb(
        _result(document_id=None, mirror_repaired=True)
    )

    assert verb == "skipped (empty document)"


@pytest.mark.parametrize("force", [False, True])
def test_the_default_is_no_repair(force: bool) -> None:
    """``mirror_repaired`` defaults False, so no existing caller changes."""
    assert "repaired" not in _ingest_outcome_verb(_result(), force=force)
