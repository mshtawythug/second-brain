"""Tests for ``brain.vault.sync.sync_one_file``.

The single-file helper is the engine behind every authoring command —
``brain note new``, ``brain daily``, and ``brain edit`` for vault-tier docs
all call it. These tests pin its scope (only the named file is touched) and
the unresolved-link contract (refs to docs not yet in the DB get tracked).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_one_file, sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def test_creates_one_doc(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Hello"}, "world\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "n.md",
    )
    assert report.created == 1
    assert report.updated == 0
    assert not report.errors
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (note_id,)
    ).fetchone()
    assert row == ("Hello",)


def test_does_not_touch_other_files(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """sync_one_file scoped to one path must not read other vault files."""
    vault = tmp_path / "vault"
    a = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a, "title": "A"}, "x\n")
    _write(vault / "b.md", {"title": "B"}, "y\n")  # has NO id; would be assigned by full sync
    sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "a.md",
    )
    cnt = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert cnt is not None
    assert cnt[0] == 1
    # b.md still lacks an id on disk (single-file sync didn't touch it).
    assert "id:" not in (vault / "b.md").read_text()


def test_unresolved_link_then_resolved_by_full_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[Foo]]`` with no Foo.md → unresolved. A later full sync resolves it."""
    vault = tmp_path / "vault"
    a = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a, "title": "A"}, "links to [[Foo]]\n")
    sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "a.md",
    )
    unresolved = test_db.execute(
        "SELECT count(*) FROM unresolved_links WHERE src_document_id=%s",
        (a,),
    ).fetchone()
    assert unresolved is not None and unresolved[0] == 1

    # Now create Foo.md and run a full sync — the unresolved link resolves.
    foo_id = str(uuid.uuid4())
    _write(vault / "foo.md", {"id": foo_id, "title": "Foo"}, "x\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    unresolved_after = test_db.execute(
        "SELECT count(*) FROM unresolved_links WHERE src_document_id=%s",
        (a,),
    ).fetchone()
    assert unresolved_after is not None and unresolved_after[0] == 0
    resolved = test_db.execute(
        "SELECT count(*) FROM links WHERE src_document_id=%s",
        (a,),
    ).fetchone()
    assert resolved is not None and resolved[0] == 1


def test_resolves_link_when_target_already_exists(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A link to an EXISTING doc resolves on the single-file sync."""
    vault = tmp_path / "vault"
    foo_id = str(uuid.uuid4())
    _write(vault / "foo.md", {"id": foo_id, "title": "Foo"}, "x\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    a = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a, "title": "A"}, "links to [[Foo]]\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "a.md",
    )
    assert report.links_resolved == 1
    assert report.links_unresolved == 0


def test_id_assignment_writes_back(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    file_path = vault / "fresh.md"
    _write(file_path, {"title": "Fresh"}, "x\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=file_path,
    )
    assert report.created == 1
    assert report.id_assigned == 1
    assert "id:" in file_path.read_text()


def test_relative_path_accepted(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Caller may pass a vault-relative path — normalized internally."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Rel"}, "x\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=Path("n.md"),
    )
    assert report.created == 1


def test_path_outside_vault_errors(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    other = tmp_path / "other.md"
    other.write_text("---\ntitle: Stray\n---\nbody\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=other,
    )
    assert report.errors
    assert "not under the vault" in report.errors[0][1]


def test_non_md_file_errors(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    f = vault / "notes.txt"
    f.write_text("not markdown")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=f,
    )
    assert report.errors
    assert "not a .md file" in report.errors[0][1]


def test_template_path_rejected(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``_templates/*.md`` files are not syncable — return an error."""
    vault = tmp_path / "vault"
    template = vault / "_templates" / "note.md"
    template.parent.mkdir(parents=True)
    template.write_text("---\ntitle: T\n---\nbody")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=template,
    )
    assert report.errors
    assert "_templates" in report.errors[0][1]


def test_missing_file_errors(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "ghost.md",
    )
    assert report.errors
    assert "does not exist" in report.errors[0][1]


def test_missing_vault_errors(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    nope = tmp_path / "nope"
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=nope,
        file_path=nope / "x.md",
    )
    assert report.errors


def test_malformed_frontmatter_surfaces_error(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A file with broken YAML in frontmatter surfaces in report.errors,
    not a traceback."""
    vault = tmp_path / "vault"
    bad = vault / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nfoo: [unclosed\n---\nbody\n")
    report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=bad,
    )
    assert report.errors
    assert "frontmatter" in report.errors[0][1].lower()


def test_ingested_classification_for_underscore_ingested(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A file under ``_ingested/`` is synced as kind='ingested'."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "manual" / "x.md",
        {"id": note_id, "title": "Mirror"},
        "body\n",
    )
    sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=vault / "_ingested" / "manual" / "x.md",
    )
    row = test_db.execute(
        "SELECT kind FROM documents WHERE id=%s", (note_id,)
    ).fetchone()
    assert row == ("ingested",)


class _EmbedderThatEditsFileMidSync:
    """Fake embedder that simulates a concurrent user save landing DURING the
    (multi-second) embed call.

    This is the exact race window the first-sync write-back clobber bug lives
    in: ``_sync_one`` reads the body, embeds (slow for a real backend), then
    writes ``dump_frontmatter(frontmatter, stale_body)`` back to disk to stamp
    the assigned id. A user save in that window would be overwritten by the
    stale body captured before the embed.

    Standard test double (mirrors ``dim`` + the Embedder Protocol surface),
    not monkey-patching of production code.
    """

    def __init__(self, inner: object, target: Path, new_text: str) -> None:
        self.dim = inner.dim  # type: ignore[attr-defined]
        self._inner = inner
        self._target = target
        self._new_text = new_text
        self.fired = False

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if not self.fired:
            self.fired = True
            # The concurrent user save lands right when the slow embed starts.
            self._target.write_text(self._new_text, encoding="utf-8")
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[attr-defined,no-any-return]

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)  # type: ignore[attr-defined,no-any-return]


def test_write_back_preserves_concurrent_user_edit(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """The deferred first-sync write-back must not clobber a concurrent edit.

    Regression: a file with no frontmatter ``id`` gets an id assigned + a
    deferred write-back. ``_sync_one`` read the body, embedded (a multi-second
    call for real backends), then wrote back the STALE body it had captured
    before the embed. A user save during that window was silently overwritten.
    """
    from brain.vault.frontmatter import dump_frontmatter  # noqa: PLC0415

    vault = tmp_path / "vault"
    file_path = vault / "fresh.md"
    # No id → triggers the deferred frontmatter write-back path.
    _write(file_path, {"title": "Fresh"}, "original body\n")

    user_edit = dump_frontmatter(
        {"title": "Fresh"}, "USER EDIT — keep this text\n"
    )
    embedder = _EmbedderThatEditsFileMidSync(fake_embedder, file_path, user_edit)

    report = sync_one_file(
        test_db, embedder=embedder, vault_path=vault, file_path=file_path
    )
    assert report.created == 1
    assert report.id_assigned == 1
    assert embedder.fired, "test invariant: the embed hook must have run"

    final = file_path.read_text()
    # The id was still stamped (write-back happened)...
    assert "id:" in final
    # ...but the user's concurrent edit survived instead of being clobbered by
    # the stale body sync read before embedding.
    assert "USER EDIT — keep this text" in final
    assert "original body" not in final
