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
from brain.errors import ElicitError
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


def _seed_gap(conn: psycopg.Connection, tenant_id: str = "default") -> str:
    return conn.execute(
        "INSERT INTO elicitation_gaps (tenant_id, signal_kind, target_type, target_id, "
        "score, evidence_ids, status) VALUES (%s,'delta','org','Acme',0.9,"
        "ARRAY['d1','d2','d3'],'surfaced') RETURNING id::text",
        (tenant_id,),
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


def _seed_evidence_doc(
    conn: psycopg.Connection,
    *,
    vault_path: str,
    title: str = "Evidence Doc",
    content_hash: str = "ev-src-footer-hash",
) -> str:
    """Insert a synthetic ingested doc WITH a vault_path; return its uuid."""
    return conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, kind, "
        "vault_path) VALUES (%s, 'evidence body', %s, 'transcript', 'ingested', %s) "
        "RETURNING id::text",
        (title, content_hash, vault_path),
    ).fetchone()[0]


def test_accept_codify_links_evidence_vault_paths(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codified note gains a ## Source section wiki-linking evidence docs."""
    # Arrange — vault + a real evidence doc with a vault_path.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    ev_path = "_ingested/krisp/2026-01-01-abcd1234-evidence-doc.md"
    ev_id = _seed_evidence_doc(test_db, vault_path=ev_path)
    gid = _seed_gap(test_db)
    gap = Gap(
        gap_id=gid,
        signal_kind="delta",
        target_type="org",
        target_id="Acme",
        score=0.9,
        evidence_ids=[ev_id],
        rationale="r",
    )

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[gap],
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "MY CORRECTED RULE"),
    )

    # Assert — corrected body + grounded Source section both present.
    assert outcomes[0].action == "accepted"
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "MY CORRECTED RULE" in content
    assert "## Source" in content
    # Path-form wiki-link to the real evidence vault_path (sans .md).
    assert "_ingested/krisp/2026-01-01-abcd1234-evidence-doc" in content


def test_accept_codify_includes_entity_name(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entity-typed gap whose target_id is a real entity UUID names it."""
    # Arrange — a graph entity + an evidence doc, gap targets the entity UUID.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    entity_id = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default','org','Globex Corp','globex','desc',3) RETURNING id::text"
    ).fetchone()[0]
    ev_id = _seed_evidence_doc(
        test_db,
        vault_path="_ingested/krisp/2026-02-02-beef0001-globex-sync.md",
        content_hash="ev-entity-hash",
    )
    gid = _seed_gap(test_db)
    gap = Gap(
        gap_id=gid,
        signal_kind="delta",
        target_type="org",
        target_id=entity_id,
        score=0.9,
        evidence_ids=[ev_id],
        rationale="r",
    )

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[gap],
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "ENTITY RULE BODY"),
    )

    # Assert — entity name surfaces as plain text under the Source section.
    assert outcomes[0].action == "accepted"
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "## Source" in content
    assert "Elicited from: Globex Corp" in content


def test_accept_codify_without_resolvable_evidence_adds_no_source_section(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap with no UUID/vault_path evidence still codifies — no empty section."""
    # Arrange — default gap (evidence_ids are non-UUID placeholders d1/d2/d3,
    # target_id 'Acme' is not an entity UUID), so nothing resolves.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    gid = _seed_gap(test_db)

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[_gap(gid)],
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "RULE WITHOUT SOURCES"),
    )

    # Assert — note authored, but no Source section appended.
    assert outcomes[0].action == "accepted"
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "RULE WITHOUT SOURCES" in content
    assert "## Source" not in content


def test_skip_does_not_touch_other_tenant_gap(
    test_db: psycopg.Connection,
) -> None:
    """A 'default' session must not mutate an 'other'-tenant gap row."""
    # Arrange — one gap per tenant; the session only sees the default one.
    other_gid = _seed_gap(test_db, tenant_id="other")
    default_gid = _seed_gap(test_db, tenant_id="default")

    # Act — dismiss the default gap.
    run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[_gap(default_gid)],
        tenant_id="default",
        vault_path=None,
        input_fn=iter(["s"]).__next__,
    )

    # Assert — default transitioned, other untouched (WHERE tenant scoping holds).
    default_status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (default_gid,)
    ).fetchone()[0]
    assert default_status == "dismissed"
    other_status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (other_gid,)
    ).fetchone()[0]
    assert other_status == "surfaced"


def test_skip_tenant_mismatch_raises(test_db: psycopg.Connection) -> None:
    """A status UPDATE that matches no row for the session's tenant fails loud.

    Drives ``run_session`` with a tenant that does not own the gap; the
    ``rowcount == 1`` guard must raise :class:`ElicitError` rather than
    silently no-op (the cross-tenant correctness contract).
    """
    # Arrange — gap belongs to 'default', session runs as 'other'.
    gid = _seed_gap(test_db, tenant_id="default")

    # Act / Assert
    with pytest.raises(ElicitError):
        run_session(
            Config.load(),
            test_db,
            drafter=_FakeDrafter(),
            gaps=[_gap(gid)],
            tenant_id="other",
            vault_path=None,
            input_fn=iter(["s"]).__next__,
        )

    # The gap's status is unchanged — no ghost update slipped through.
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()[0]
    assert status == "surfaced"


def _vault_md_files(vault: Path) -> list[Path]:
    """All authored .md files under the vault, excluding the _templates dir."""
    return [
        p
        for p in vault.rglob("*.md")
        if "_templates" not in p.relative_to(vault).parts
    ]


def test_accept_codify_wikilinks_entity_with_vault_page(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entity WITH a resolvable People-Hub page is wiki-linked in the footer."""
    # Arrange — entity + a People-Hub-style documents row for it.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    entity_id = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default','org','Globex Corp','globex','desc',3) RETURNING id::text"
    ).fetchone()[0]
    # A real vault page for the entity (kind='vault', non-null vault_path).
    test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, kind, "
        "vault_path) VALUES ('Globex Corp','about globex','globex-page-hash',"
        "'note','vault','people/globex-corp.md')"
    )
    ev_id = _seed_evidence_doc(
        test_db,
        vault_path="_ingested/krisp/2026-02-02-beef0002-globex-sync.md",
        content_hash="ev-entity-link-hash",
    )
    gid = _seed_gap(test_db)
    gap = Gap(
        gap_id=gid,
        signal_kind="delta",
        target_type="org",
        target_id=entity_id,
        score=0.9,
        evidence_ids=[ev_id],
        rationale="r",
    )

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[gap],
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "LINKED ENTITY RULE"),
    )

    # Assert — entity surfaces as a path-form wiki-link (alias = entity name).
    assert outcomes[0].action == "accepted"
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "## Source" in content
    assert "Elicited from: [[people/globex-corp|Globex Corp]]" in content


def test_accept_codify_entity_without_vault_page_stays_plain_text(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entity with NO resolvable vault page stays plain text — no broken link."""
    # Arrange — entity + evidence doc, but no documents row for the entity.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    entity_id = test_db.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default','org','Initech','initech','desc',3) RETURNING id::text"
    ).fetchone()[0]
    ev_id = _seed_evidence_doc(
        test_db,
        vault_path="_ingested/krisp/2026-03-03-beef0003-initech-sync.md",
        content_hash="ev-noentity-link-hash",
    )
    gid = _seed_gap(test_db)
    gap = Gap(
        gap_id=gid,
        signal_kind="delta",
        target_type="org",
        target_id=entity_id,
        score=0.9,
        evidence_ids=[ev_id],
        rationale="r",
    )

    # Act
    outcomes = run_session(
        Config.load(),
        test_db,
        drafter=_FakeDrafter(),
        gaps=[gap],
        tenant_id="default",
        vault_path=vault,
        input_fn=iter(["e"]).__next__,
        edit_fn=lambda initial, *, doc_id_label: ({}, "UNLINKED ENTITY RULE"),
    )

    # Assert — plain text, no wiki-link brackets around the entity line.
    assert outcomes[0].action == "accepted"
    content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (outcomes[0].note_id,)
    ).fetchone()[0]
    assert "Elicited from: Initech" in content
    assert "Elicited from: [[" not in content


def test_codify_tenant_mismatch_creates_no_orphan_note(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting a gap owned by another tenant raises BEFORE any note is authored.

    Regression: ``_codify`` created the vault note + document row first and only
    then ran the tenant-scoped resolve UPDATE, so a mismatch left an orphan note.
    The preflight must reject up front — no vault file, no document row.
    """
    # Arrange — gap belongs to 'default'; session runs as 'other'.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    gid = _seed_gap(test_db, tenant_id="default")
    before_files = set(_vault_md_files(vault))

    # Act / Assert
    with pytest.raises(ElicitError):
        run_session(
            Config.load(),
            test_db,
            drafter=_FakeDrafter(),
            gaps=[_gap(gid)],
            tenant_id="other",
            vault_path=vault,
            input_fn=iter(["e"]).__next__,
            edit_fn=lambda initial, *, doc_id_label: ({}, "ORPHAN GUARD BODY"),
        )

    # No vault note authored, no document row created.
    assert set(_vault_md_files(vault)) == before_files
    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE content LIKE %s",
        ("%ORPHAN GUARD BODY%",),
    ).fetchone()[0]
    assert count == 0
    # The gap is untouched (still surfaced).
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id=%s", (gid,)
    ).fetchone()[0]
    assert status == "surfaced"


def test_codify_already_resolved_gap_creates_no_orphan_note(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting an already-resolved gap raises BEFORE authoring — no orphan note."""
    # Arrange — seed a gap then mark it resolved out-of-band.
    vault = tmp_path / "vault"
    init_vault(vault)
    monkeypatch.setattr(
        "brain.vault.note_builder._build_embedder", lambda _cfg: fake_embedder
    )
    gid = _seed_gap(test_db, tenant_id="default")
    test_db.execute(
        "UPDATE elicitation_gaps SET status='resolved' WHERE id=%s", (gid,)
    )
    before_files = set(_vault_md_files(vault))

    # Act / Assert
    with pytest.raises(ElicitError):
        run_session(
            Config.load(),
            test_db,
            drafter=_FakeDrafter(),
            gaps=[_gap(gid)],
            tenant_id="default",
            vault_path=vault,
            input_fn=iter(["e"]).__next__,
            edit_fn=lambda initial, *, doc_id_label: ({}, "RESOLVED GUARD BODY"),
        )

    assert set(_vault_md_files(vault)) == before_files
    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE content LIKE %s",
        ("%RESOLVED GUARD BODY%",),
    ).fetchone()[0]
    assert count == 0


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
