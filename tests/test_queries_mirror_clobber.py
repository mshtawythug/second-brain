"""Tests for clobbered-mirror classification in ``brain.queries``.

Regression coverage for a DATA-LOSS hazard found on the live vault. A stray
ingest wrote a mirror file over ``_ingested/manual/<slug>.md`` -- a path a real
``documents`` row still owned as its ``vault_path`` -- and that writer's own row
never landed in this database. The file's frontmatter ``id`` therefore resolved
to nothing.

The classifier keyed on the id alone, so:

- ``brain doctor`` reported it as an orphan file and printed
  ``brain vault prune-orphans`` as the remedy;
- ``brain vault prune-orphans --apply`` would have DELETED the live document's
  only mirror, converting a repairable clobber into a ghost row;
- ``ghost_rows`` stayed 0 the whole time (the file exists, so the row's
  ``vault_path`` still resolves), so nothing else flagged it either -- while the
  vault, and any wiki published from it, served the wrong document's content.

Such a file is a *clobbered mirror*, not an orphan. It is repaired by rewriting
from the DB, never by deleting.

All fixtures are synthetic; the real corpus is never touched.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import (
    iter_clobbered_mirror_files,
    iter_orphan_mirror_files,
    iter_stale_mirror_files,
    mirror_drift_summary,
)
from brain.vault.frontmatter import dump_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# A syntactically valid UUID that no ingest will ever mint: stands in for the
# foreign writer's document id, which lives in some other database (or nowhere).
_FOREIGN_ID = "8e8ca3c2-0000-4000-8000-5feedb70a15b"


def _ingest_owned(
    conn: psycopg.Connection, fake_embedder: Any, *, title: str, body: str, rel: str
) -> str:
    """Ingest a doc and pin its ``vault_path`` to ``rel``. Returns its UUID.

    Bodies must be unique per call: ``documents_content_hash_stdin_idx`` is
    UNIQUE for stdin-tier rows and would otherwise dedupe the seeds away.
    """
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    conn.execute(
        "UPDATE documents SET kind='ingested', vault_path=%s WHERE id=%s",
        (rel, result.document_id),
    )
    return result.document_id


def _write_mirror(path: Path, *, doc_id: str, title: str, body: str) -> None:
    """Write a mirror file carrying frontmatter ``id``/``title`` at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter({"id": doc_id, "title": title}, body), "utf-8")


def _clobbered_vault(
    conn: psycopg.Connection, fake_embedder: Any, vault: Path
) -> tuple[str, Path, Path]:
    """Build the live shape: one clobbered mirror + one true orphan.

    Returns ``(owner_id, clobbered_path, orphan_path)``.

    The two files differ in exactly one respect -- whether a live row claims
    their path as its ``vault_path`` -- which is the property under test. The
    true orphan is the POSITIVE CONTROL: without it, an assertion that the
    clobbered file is absent from the orphan list would also pass if the sweep
    had simply stopped finding anything at all.
    """
    clobbered_rel = "_ingested/manual/clobbered-note.md"
    owner_id = _ingest_owned(
        conn,
        fake_embedder,
        title="clobbered-note",
        body="the real body of the owning document",
        rel=clobbered_rel,
    )
    clobbered_path = vault / clobbered_rel
    # A foreign document's bytes sitting at the owning row's canonical path.
    _write_mirror(
        clobbered_path,
        doc_id=_FOREIGN_ID,
        title="foreign intruder",
        body="content that belongs to a different document\n",
    )

    orphan_path = vault / "_ingested" / "manual" / "genuinely-orphaned.md"
    _write_mirror(
        orphan_path,
        doc_id="11111111-1111-4111-8111-111111111111",
        title="genuinely-orphaned",
        body="row really is gone\n",
    )
    return owner_id, clobbered_path, orphan_path


def test_clobbered_mirror_is_not_listed_as_an_orphan(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """The clobbered file is excluded from the prune candidate list.

    Setup: a clobbered mirror (id unresolvable, path owned by a live row) and a
    true orphan (id unresolvable, path owned by nobody).
    Exercise: :func:`iter_orphan_mirror_files`.
    Verify: only the true orphan is yielded. The positive control proves the
    sweep still finds what it should.
    """
    vault = tmp_path / "vault"
    _, clobbered_path, orphan_path = _clobbered_vault(test_db, fake_embedder, vault)

    yielded = sorted(iter_orphan_mirror_files(test_db, vault_path=vault))

    assert yielded == [orphan_path], (
        "regression: a mirror whose path a live row owns was offered up for "
        "deletion; pruning it destroys that document's only mirror"
    )
    assert clobbered_path not in yielded


def test_clobbered_mirror_is_reported_by_its_own_iterator(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Excluding it from orphans must not make it invisible.

    A silent exclusion would be strictly worse than the original bug: the file
    would stop being reported at all while the vault kept serving the wrong
    content. It has to surface under its own category.
    """
    vault = tmp_path / "vault"
    _, clobbered_path, orphan_path = _clobbered_vault(test_db, fake_embedder, vault)

    yielded = sorted(iter_clobbered_mirror_files(test_db, vault_path=vault))

    assert yielded == [clobbered_path]
    # The two categories partition the unresolvable files; neither leaks.
    assert orphan_path not in yielded


def test_mirror_drift_summary_counts_clobber_separately(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Doctor's counters split the two shapes, and ghost_rows stays honest.

    ``ghost_rows == 0`` here is not a bug -- the file does exist -- but it is
    why the clobber slipped past every other counter and needs one of its own.
    """
    vault = tmp_path / "vault"
    _clobbered_vault(test_db, fake_embedder, vault)

    summary = mirror_drift_summary(test_db, vault_path=vault)

    assert summary.clobbered_mirrors == 1
    assert summary.orphan_files == 1
    assert summary.ghost_rows == 0
    assert summary.total_ingested_rows == 1
    assert summary.rows_with_null_vault_path == 0


def test_clean_vault_reports_no_clobber(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """POSITIVE CONTROL for the counter: a healthy mirror scores zero.

    Guards against the new counter being a constant. The file here carries the
    OWNING row's id at the owning row's path -- the healthy state -- and must
    not be mistaken for a clobber merely because a row claims the path.
    """
    vault = tmp_path / "vault"
    rel = "_ingested/manual/healthy-note.md"
    owner_id = _ingest_owned(
        test_db, fake_embedder, title="healthy-note", body="healthy body", rel=rel
    )
    _write_mirror(vault / rel, doc_id=owner_id, title="healthy-note", body="healthy\n")

    summary = mirror_drift_summary(test_db, vault_path=vault)

    assert summary.clobbered_mirrors == 0
    assert summary.orphan_files == 0
    assert summary.ghost_rows == 0
    assert list(iter_clobbered_mirror_files(test_db, vault_path=vault)) == []


def test_prune_orphans_apply_does_not_delete_a_clobbered_mirror(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END data-loss regression: ``--apply`` must spare the clobbered file.

    This is the assertion that matters to the user. Before the fix, running the
    remedy ``brain doctor`` itself printed would have unlinked a live
    document's mirror.

    Setup: clobbered mirror + true orphan in a sandboxed vault.
    Exercise: ``brain vault prune-orphans --apply``.
    Verify: the true orphan is gone (the command still works) and the clobbered
    file survives on disk with its owning row intact.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    owner_id, clobbered_path, orphan_path = _clobbered_vault(
        test_db, fake_embedder, vault
    )

    result = CliRunner().invoke(app, ["vault", "prune-orphans", "--apply"])

    assert result.exit_code == 0, result.output
    assert not orphan_path.exists(), "the command stopped pruning real orphans"
    assert clobbered_path.exists(), (
        "regression: prune-orphans --apply deleted the mirror of a live document"
    )
    still_owned = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (owner_id,)
    ).fetchone()
    assert still_owned is not None
    assert still_owned[0] == "_ingested/manual/clobbered-note.md"


def test_export_force_actually_repairs_a_clobbered_mirror(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advertised remedy must really repair the file, not no-op.

    ``brain doctor`` should point clobbered mirrors at
    ``brain vault export --force``. A remedy that silently does nothing is
    worse than none -- a previous release shipped exactly that -- so this
    executes the real command and checks the bytes on disk afterwards.

    The mechanism is worth pinning down, because ``--force`` does NOT do what
    its name suggests: ``export_vault`` forwards ``force`` only to the
    unmanaged-directory guard and never to ``_write_doc_file``'s body-hash
    skip. The rewrite happens because a clobbered file's body hash differs
    from its owning row's ``content_hash``, so the skip does not engage. This
    test is what would catch that reasoning being wrong.

    Setup: a clobbered mirror carrying a foreign id and foreign body.
    Exercise: ``brain vault export --force``.
    Verify: the file now carries the OWNING row's id and body, and the drift
    counter clears.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    owner_id, clobbered_path, _ = _clobbered_vault(test_db, fake_embedder, vault)

    before = clobbered_path.read_text(encoding="utf-8")
    assert _FOREIGN_ID in before, "fixture did not actually clobber the mirror"

    result = CliRunner().invoke(app, ["vault", "export", "--force"])
    assert result.exit_code == 0, result.output

    after = clobbered_path.read_text(encoding="utf-8")
    assert owner_id in after, (
        "the advertised remedy did not rewrite the clobbered mirror -- doctor "
        "would be printing a no-op fix"
    )
    assert _FOREIGN_ID not in after
    assert "the real body of the owning document" in after

    summary = mirror_drift_summary(test_db, vault_path=vault)
    assert summary.clobbered_mirrors == 0
    assert summary.ghost_rows == 0


# ---------------------------------------------------------------------------
# C7 iteration 2 — the guard has to be on BOTH iterators feeding the unlink loop.
# ---------------------------------------------------------------------------


def test_stale_sweep_skips_a_file_owned_by_another_live_row(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A resolvable id at someone else's canonical path is clobbered, not stale.

    ``iter_orphan_mirror_files`` consults ``_owned_vault_paths``;
    ``iter_stale_mirror_files`` did not, and both feed the same
    ``path.unlink()`` loop behind ``prune-orphans --apply --include-stale`` —
    which ``brain-rebuild`` runs UNATTENDED in its wiki stage. So doc B's bytes
    landing on doc A's mirror path made A's only on-disk copy automatically
    deletable.

    Setup: A owns ``a.md``; B is live and owns ``b.md``; B's bytes are written
    over ``a.md``. B's id resolves, and its canonical path is not ``a.md``, so
    the pre-fix classifier called ``a.md`` stale and deleted it.
    """
    # --- setup
    vault = tmp_path / "vault"
    a_rel = "_ingested/manual/a.md"
    b_rel = "_ingested/manual/b.md"
    _ingest_owned(test_db, fake_embedder, title="a", body="body of A", rel=a_rel)
    b_id = _ingest_owned(
        test_db, fake_embedder, title="b", body="body of B", rel=b_rel
    )
    # B's bytes written over A's canonical mirror.
    _write_mirror(vault / a_rel, doc_id=b_id, title="b", body="body of B\n")

    # POSITIVE CONTROL: a genuinely stale file — resolvable id, path owned by
    # nobody. Without it, "A's mirror is absent from the stale list" would also
    # pass if the sweep had stopped finding anything at all.
    stale_rel = "_ingested/manual/old-slug.md"
    _write_mirror(vault / stale_rel, doc_id=b_id, title="b", body="body of B\n")

    # --- exercise
    stale = {p.relative_to(vault).as_posix() for p in iter_stale_mirror_files(
        test_db, vault_path=vault
    )}

    # --- verify
    assert stale_rel in stale, "positive control missing — the sweep found nothing"
    assert a_rel not in stale, (
        "a live row's canonical mirror was classified stale and would be "
        "unlinked by `prune-orphans --apply --include-stale`"
    )


def test_drift_summary_and_prune_orphans_agree_on_a_vault_tier_mirror(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Doctor's counter and the prune tool must use ONE ownership definition.

    ``mirror_drift_summary`` scoped ownership to ingested-tier rows while the
    iterators used all kinds, so a live **vault**-tier row whose mirror sits
    under ``_ingested/`` was invisible to the counter: doctor called its
    clobbered file an orphan and printed the ``prune-orphans`` remedy, while
    ``prune-orphans`` itself correctly refused to list it and reported zero.
    A warning the user cannot clear, and advice that contradicts the tool.
    """
    # --- setup — a vault-TIER row owning a path under _ingested/, then clobbered.
    vault = tmp_path / "vault"
    rel = "_ingested/manual/vault-tier-owned.md"
    owner_id = _ingest_owned(
        test_db, fake_embedder, title="vault-tier-owned", body="real body", rel=rel
    )
    test_db.execute("UPDATE documents SET kind = 'vault' WHERE id = %s", (owner_id,))
    _write_mirror(vault / rel, doc_id=_FOREIGN_ID, title="intruder", body="foreign\n")

    # --- exercise
    summary = mirror_drift_summary(test_db, vault_path=vault)
    orphans = {p.relative_to(vault).as_posix() for p in iter_orphan_mirror_files(
        test_db, vault_path=vault
    )}

    # --- verify — the counter must agree with the tool, in both directions.
    assert rel not in orphans, "prune-orphans would offer to delete a live mirror"
    assert summary.orphan_files == len(orphans), (
        "doctor's orphan count disagrees with what prune-orphans would act on"
    )
    assert summary.clobbered_mirrors == 1
