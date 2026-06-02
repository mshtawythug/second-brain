"""Integration tests for the interactive elicitation session loop (Task 3.2).

A real Postgres test DB backs the gap-state transitions; the drafter, editor,
and stdin are all injected fakes so no ``$EDITOR`` launches and no Ollama is
required. The codify path authors a real vault note, so tests that exercise it
scaffold a vault with :func:`init_vault` and substitute the deterministic
``fake_embedder`` for the configured backend via the documented
``brain.vault.note_builder._build_embedder`` indirection.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from brain.config import Config
from brain.elicit.schema import ElicitDraft, Gap
from brain.elicit.session import run_session
from brain.vault import init_vault


class _FakeDrafter:
    """Return a fixed confident draft without touching the enricher / DB."""

    def draft(
        self, conn: psycopg.Connection, gap: Gap, *, tenant_id: str
    ) -> ElicitDraft:
        return ElicitDraft(
            gap_id=gap.gap_id,
            title="Tacit rule for Acme",
            draft_text="confident guess",
            evidence_ids=gap.evidence_ids,
            evidence_texts=["an evidence excerpt"],
        )


def _seed_gap(conn: psycopg.Connection) -> str:
    return conn.execute(
        "INSERT INTO elicitation_gaps (tenant_id, signal_kind, target_type, target_id, "
        "score, evidence_ids, status) VALUES ('default','delta','org','Acme',0.9,"
        "ARRAY['d1','d2','d3'],'surfaced') RETURNING id::text"
    ).fetchone()[0]


def _gap(gid: str) -> Gap:
    return Gap(
        gap_id=gid,
        signal_kind="delta",
        target_type="org",
        target_id="Acme",
        score=0.9,
        evidence_ids=["d1", "d2", "d3"],
        rationale="Acme is referenced in 3 docs but never authored in a note.",
    )


def test_skip_then_quit(test_db: psycopg.Connection) -> None:
    # Arrange
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=None,
        input_fn=iter(["s"]).__next__,
    )

    # Assert
    assert len(outcomes) == 1
    assert outcomes[0].action == "dismissed"
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()[0]
    assert status == "dismissed"


def test_quit_returns_empty(test_db: psycopg.Connection) -> None:
    # Arrange
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act — quitting before any decision yields no outcomes and no state change.
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=None,
        input_fn=iter(["q"]).__next__,
    )

    # Assert
    assert outcomes == []
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()[0]
    assert status == "surfaced"


def test_snooze_sets_future_until(test_db: psycopg.Connection) -> None:
    # Arrange
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=None,
        input_fn=iter(["n", "3"]).__next__,
    )

    # Assert
    assert outcomes[0].action == "snoozed"
    assert outcomes[0].snoozed_days == 3
    row = test_db.execute(
        "SELECT status, snoozed_until > now() FROM elicitation_gaps WHERE id=%s",
        (gid,),
    ).fetchone()
    assert row[0] == "snoozed"
    assert row[1] is True


def test_snooze_reprompts_on_invalid_days(test_db: psycopg.Connection) -> None:
    # Arrange
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act — a non-integer then a sub-1 value are both rejected before "2".
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=None,
        input_fn=iter(["n", "abc", "0", "2"]).__next__,
    )

    # Assert
    assert outcomes[0].action == "snoozed"
    assert outcomes[0].snoozed_days == 2


def test_accept_codifies(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — a real vault + deterministic embedder so the note syncs offline.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "MY CORRECTED RULE"),
    )

    # Assert
    assert outcomes[0].action == "accepted"
    assert outcomes[0].note_id
    row = test_db.execute(
        "SELECT status, resolved_note_id FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()
    assert row[0] == "resolved"
    assert row[1] is not None
    # The corrected body — not the draft — is what was authored.
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "MY CORRECTED RULE" in content


def test_accept_then_unchanged_reprompts_to_skip(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit that returns the draft verbatim re-prompts; the user then skips."""
    # Arrange
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    gid = _seed_gap(test_db)
    gaps = [_gap(gid)]

    # Act — first "e" yields an unchanged body (re-prompt), then "s" dismisses.
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=gaps,
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e", "s"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "confident guess"),
    )

    # Assert — no note authored, gap dismissed.
    assert outcomes[0].action == "dismissed"
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()[0]
    assert status == "dismissed"
