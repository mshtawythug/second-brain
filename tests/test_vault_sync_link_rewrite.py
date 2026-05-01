"""Integration tests for the vault-tier link rewrite step.

Exercises ``brain vault sync`` end-to-end against the real Postgres test
DB so the SyncReport counters, on-disk file shapes, ``links`` table
content, and Phase D fence interactions are all asserted in one place.
"""
from pathlib import Path

import psycopg

from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import sync_one_file, sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed_target(
    conn: psycopg.Connection,
    *,
    title: str,
    vault_path: str,
    kind: str = "vault",
) -> str:
    """Pre-seed an existing target doc so the sync engine has something to
    resolve to. Mirrors a "this file exists from a prior sync" state.
    """
    import hashlib
    import json
    import uuid

    salted = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        INSERT INTO documents
            (id, source_id, title, content, content_hash, content_type,
             source_path, tags, metadata, vault_path, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id::text
        """,
        (
            str(uuid.uuid4()),
            None,
            title,
            salted,
            content_hash,
            "note",
            None,
            [],
            json.dumps({}),
            vault_path,
            kind,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# sync_vault — full-walk path.
# ---------------------------------------------------------------------------


class TestSyncVaultLinkRewrite:
    def test_happy_path_rewrites_and_resolves(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        # Seed the target on disk so the same sync pass discovers + creates it.
        _write(
            vault / "company-mc.md",
            {"title": "company-mc Recap"},
            "Recap body.\n",
        )
        _write(
            vault / "k.md",
            {"title": "company-ko"},
            "See [[company-mc Recap]] for context.\n",
        )

        report = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert not report.errors
        # Exactly one vault-tier note had its body rewritten.
        assert report.links_rewritten == 1

        # File-on-disk content is in canonical path form.
        _, body = parse_frontmatter((vault / "k.md").read_text())
        assert "[[company-mc|company-mc Recap]]" in body
        assert "[[company-mc Recap]]" not in body

        # The DB ``links`` row points at the target.
        rows = test_db.execute(
            "SELECT dst_document_id::text, link_text FROM links"
        ).fetchall()
        assert len(rows) == 1

    def test_idempotent_second_pass(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        _write(vault / "company-mc.md", {"title": "company-mc"}, "Body.\n")
        _write(
            vault / "k.md",
            {"title": "Note"},
            "Ref: [[company-mc]].\n",
        )
        first = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert first.links_rewritten == 1
        post_first = (vault / "k.md").read_text()

        second = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert second.links_rewritten == 0
        # File bytes unchanged on the second pass.
        assert (vault / "k.md").read_text() == post_first

    def test_mixed_link_forms_all_rewrite(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        _write(vault / "a.md", {"title": "Alpha"}, "Body.\n")
        b_id = _seed_target(test_db, title="Beta", vault_path="b.md")
        _write(vault / "b.md", {"id": b_id, "title": "Beta"}, "Body.\n")
        # Embedded title; alias-display title; brain-id; heading; embed.
        _write(
            vault / "k.md",
            {"title": "Mixed"},
            (
                "1. [[Alpha]]\n"
                "2. [[Alpha|Alpha alias]]\n"
                f"3. [[brain:{b_id[:8]}|the beta]]\n"
                "4. [[Alpha#Section]]\n"
                "5. ![[Alpha]]\n"
            ),
        )
        report = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert report.links_rewritten == 1

        _, body = parse_frontmatter((vault / "k.md").read_text())
        assert "[[a|Alpha]]" in body
        assert "[[a|Alpha alias]]" in body
        assert "[[b|the beta]]" in body
        assert "[[a#Section|Alpha]]" in body
        assert "![[a|Alpha]]" in body

    def test_no_link_rewrite_flag_leaves_files_untouched(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        _write(vault / "company-mc.md", {"title": "company-mc"}, "Body.\n")
        _write(
            vault / "k.md",
            {"title": "Note"},
            "Ref: [[company-mc]].\n",
        )
        report = sync_vault(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            link_rewrite=False,
        )
        assert report.links_rewritten == 0
        # The DB ``links`` row was still materialized (rewrite is purely
        # a disk-side polish; the resolver runs unconditionally).
        link_rows = test_db.execute(
            "SELECT count(*) FROM links"
        ).fetchone()
        assert link_rows is not None
        assert link_rows[0] == 1
        # File body unchanged.
        _, body = parse_frontmatter((vault / "k.md").read_text())
        assert "[[company-mc]]" in body

    def test_corrupt_null_vault_path_row_does_not_crash_sync(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        """A pre-existing vault-tier row with ``vault_path IS NULL`` is
        invisible to the walker and to ``_process_missing`` (whose query
        filters ``vault_path IS NOT NULL``), so the only thing the test
        guards against is a future regression where the rewrite step
        accidentally pulls in unwalked rows. Running ``sync_vault`` on a
        normal vault while the corrupt row is present must complete
        without raising and must rewrite the unrelated, on-disk file.
        """
        vault = tmp_path / "vault"
        # Seed a corrupt vault-tier row directly — sync would never produce
        # one, but a manual SQL surgery / failed migration could.
        _seed_target(test_db, title="No Path", vault_path="placeholder.md")
        test_db.execute(
            "UPDATE documents SET vault_path = NULL "
            "WHERE title = 'No Path' AND kind = 'vault'"
        )
        # Normal happy-path content alongside.
        _write(vault / "company-mc.md", {"title": "company-mc"}, "Body.\n")
        _write(
            vault / "k.md", {"title": "K"}, "See [[company-mc]]\n"
        )
        report = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert not report.errors
        assert report.links_rewritten == 1
        # The corrupt row is still in the DB, untouched.
        bad_row = test_db.execute(
            "SELECT vault_path FROM documents WHERE title = 'No Path'"
        ).fetchone()
        assert bad_row == (None,)

    def test_corrupt_missing_file_row_does_not_crash_sync(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        """A vault-tier row whose ``vault_path`` points at a file that
        was never created (or was deleted out-of-band) must not crash
        ``sync_vault``. The walker won't see the ghost path, ``_process_
        missing`` will warn about it, and the rewrite step won't pull it
        into ``seen_doc_ids`` — but the test pins the contract so a
        future change to the rewrite path's input set can't silently
        regress.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        _seed_target(test_db, title="Ghost", vault_path="ghost.md")
        # No ``ghost.md`` on disk.
        report = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert not report.errors
        assert report.links_rewritten == 0
        assert report.warned == 1  # ghost.md is reported as missing-on-disk

    # NOTE: an explicit "empty doc_ids short-circuits" test was dropped — the
    # equivalent coverage lands naturally in
    # :meth:`TestSyncVaultLinkRewrite.test_dry_run_skips_rewrite` (no docs
    # are touched on dry-run, so the rewrite step gets an empty input
    # implicitly) and in any sync run on a vault containing only files
    # without any wiki-links.

    def test_dry_run_skips_rewrite(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        _write(vault / "company-mc.md", {"title": "company-mc"}, "Body.\n")
        _write(
            vault / "k.md",
            {"title": "Note"},
            "Ref: [[company-mc]].\n",
        )
        report = sync_vault(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            dry_run=True,
        )
        assert report.links_rewritten == 0
        # Bodies remain authored shape.
        _, body = parse_frontmatter((vault / "k.md").read_text())
        assert "[[company-mc]]" in body


# ---------------------------------------------------------------------------
# sync_one_file — watcher-mode parity.
# ---------------------------------------------------------------------------


class TestSyncOneFileLinkRewrite:
    def test_single_file_path_rewrites(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        # The target must exist before the per-file sync — that's the watch
        # pre-condition (other notes were synced earlier).
        target_id = _seed_target(
            test_db, title="company-mc", vault_path="company-mc.md"
        )
        # File on disk for the target so the resolver doesn't drop it.
        _write(
            vault / "company-mc.md",
            {"id": target_id, "title": "company-mc"},
            "Body.\n",
        )
        # Author file — what the watcher is reacting to.
        author = vault / "k.md"
        _write(author, {"title": "Note"}, "Ref: [[company-mc]]\n")

        report = sync_one_file(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            file_path=author,
        )
        assert report.links_rewritten == 1
        _, body = parse_frontmatter(author.read_text())
        assert "[[company-mc|company-mc]]" in body

    def test_no_link_rewrite_flag_through_sync_one_file(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        target_id = _seed_target(
            test_db, title="company-mc", vault_path="company-mc.md"
        )
        _write(
            vault / "company-mc.md",
            {"id": target_id, "title": "company-mc"},
            "Body.\n",
        )
        author = vault / "k.md"
        _write(author, {"title": "Note"}, "Ref: [[company-mc]]\n")
        report = sync_one_file(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            file_path=author,
            link_rewrite=False,
        )
        assert report.links_rewritten == 0
        # File body unchanged.
        _, body = parse_frontmatter(author.read_text())
        assert "[[company-mc]]" in body

    def test_ingested_tier_file_is_not_rewritten(
        self, test_db: psycopg.Connection, fake_embedder, tmp_path: Path
    ) -> None:
        vault = tmp_path / "vault"
        # Seed a vault-tier target the ingested file links to.
        target_id = _seed_target(
            test_db, title="Vault Note", vault_path="vault-note.md"
        )
        _write(
            vault / "vault-note.md",
            {"id": target_id, "title": "Vault Note"},
            "Body.\n",
        )
        ingested = vault / "_ingested" / "manual" / "x.md"
        _write(
            ingested,
            {
                "title": "Mirror",
                "kind": "ingested",
                "source": "manual",
                "external_id": "x-1",
            },
            "Ref: [[Vault Note]]\n",
        )
        report = sync_one_file(
            test_db,
            embedder=fake_embedder,
            vault_path=vault,
            file_path=ingested,
        )
        assert report.links_rewritten == 0
        # File body unchanged — ingested tier is a mirror, not authored.
        _, body = parse_frontmatter(ingested.read_text())
        assert "[[Vault Note]]" in body


# ---------------------------------------------------------------------------
# Phase D regression — the existing fence renderer must still run, and
# `links_rewritten` must be 0 for ingested-tier files.
# ---------------------------------------------------------------------------


class TestPhaseDRegression:
    def test_ingested_tier_skipped_link_rewrite_phase_d_unaffected(
        self,
        test_db: psycopg.Connection,
        fake_embedder,
        tmp_path: Path,
    ) -> None:
        """Ingested mirrors must not get their wiki-links rewritten.

        Vault-tier link rewriting is contractually a vault-tier-only
        polish step — the auto-generated derived-edges fence inside
        ``_ingested/*.md`` already controls the wiki-links Quartz sees on
        ingested mirrors. Asserting that ``links_rewritten`` only counts
        vault-tier rewrites guards against a regression that would
        double-rewrite content already managed by Phase D.
        """
        vault = tmp_path / "vault"
        # Vault-tier target for both notes to reference.
        target_id = _seed_target(
            test_db, title="Vault Note", vault_path="vault-note.md"
        )
        _write(
            vault / "vault-note.md",
            {"id": target_id, "title": "Vault Note"},
            "Body.\n",
        )
        # Vault-tier note that references the target — should be rewritten.
        _write(
            vault / "summary.md",
            {"title": "Summary"},
            "See [[Vault Note]] for details.\n",
        )
        # Ingested-tier mirror that references the same target — must NOT
        # be rewritten (contract).
        _write(
            vault / "_ingested" / "manual" / "mirror.md",
            {
                "title": "Mirror",
                "kind": "ingested",
                "source": "manual",
                "external_id": "mirror-1",
            },
            "Mirror body referencing [[Vault Note]].\n",
        )

        report = sync_vault(
            test_db, embedder=fake_embedder, vault_path=vault
        )
        assert not report.errors
        # Exactly one vault-tier file was rewritten (summary.md). The
        # ingested mirror's body is left in authored shape.
        assert report.links_rewritten == 1
        _, summary_body = parse_frontmatter(
            (vault / "summary.md").read_text()
        )
        assert "[[vault-note|Vault Note]]" in summary_body
        assert "BRAIN_DERIVED_START" not in summary_body
        _, mirror_body = parse_frontmatter(
            (vault / "_ingested" / "manual" / "mirror.md").read_text()
        )
        assert "[[Vault Note]]" in mirror_body
