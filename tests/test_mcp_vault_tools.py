"""Tests for the Phase 7 vault-aware MCP tools.

Mirrors the existing ``tests/test_mcp_server.py`` pattern: each test
installs a fresh ``_State`` (real test-DB Config + fake embedder + a
``tmp_path`` vault) via ``monkeypatch.setattr`` and calls the tool
functions directly. No subprocess, no JSON-RPC round-trip — those live
in ``test_mcp_server_protocol.py``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import McpError

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from brain.vault.frontmatter import parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Initialize a fresh vault under ``tmp_path`` and return its path.

    Uses :func:`brain.vault.init_vault` so the resulting directory has the
    canonical structure (``_templates/note.md``, ``_templates/daily.md``,
    ``daily/``, etc.) every authoring test depends on.
    """
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    """Install an MCP _State pointing at the test DB + fake embedder + vault.

    ``vault_path`` is the per-test ``tmp_path/vault`` so vault-mutating
    tests (note creation, daily, link proposal) are fully isolated from
    one another and from the user's real ``~/brain-vault``.
    """
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=vault_dir),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


# ---------------------------------------------------------------------------
# Direct INSERT helpers — graph queries only touch documents + links, so we
# bypass the chunk pipeline to keep the test DB minimal.
# ---------------------------------------------------------------------------


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    kind: str = "vault",
    vault_path: str | None = None,
) -> str:
    """Insert a documents row by hand. Returns ``doc_id`` for chaining."""
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            title,
            f"body of {title}",
            f"hash-{doc_id}",
            "note",
            kind,
            vault_path,
        ),
    )
    return doc_id


def _link_row(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    dst: str,
    text: str,
    kind: str = "wiki",
    display: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (src, dst, text, kind, display),
    )


def _unresolved_row(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    text: str,
    kind: str = "wiki",
    display: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO unresolved_links
          (src_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s)
        """,
        (src, text, kind, display),
    )


# ---------------------------------------------------------------------------
# brain_backlinks
# ---------------------------------------------------------------------------


def test_brain_backlinks_returns_inbound_links(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001 — fixture installs state
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111", title="A")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222", title="B")
    c = _make_doc(test_db, doc_id="33333333-3333-3333-3333-333333333333", title="C")
    _link_row(test_db, src=a, dst=b, text="[[B]]")
    _link_row(test_db, src=c, dst=b, text="[[B|alias]]", display="alias")
    rows = mcp_server.brain_backlinks(id_prefix=b[:8])
    titles = {r["src_title"] for r in rows}
    assert titles == {"A", "C"}
    expected_keys = {
        "src_document_id",
        "src_title",
        "src_kind",
        "link_text",
        "link_kind",
    }
    for r in rows:
        assert set(r.keys()) == expected_keys
        assert r["src_kind"] == "vault"


def test_brain_backlinks_empty_when_none(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111", title="A")
    assert mcp_server.brain_backlinks(id_prefix=a[:8]) == []


def test_brain_backlinks_unknown_id_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_backlinks(id_prefix="ffffff")
    assert "not found" in exc_info.value.error.message


def test_brain_backlinks_short_prefix_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_backlinks(id_prefix="abc")
    assert "6 characters" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# brain_links
# ---------------------------------------------------------------------------


def test_brain_links_resolved_only(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111", title="A")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222", title="B")
    _link_row(test_db, src=a, dst=b, text="[[B]]")
    _unresolved_row(test_db, src=a, text="[[Nope]]")
    rows = mcp_server.brain_links(id_prefix=a[:8])
    assert len(rows) == 1
    assert rows[0]["dst_document_id"] == b
    assert rows[0]["dst_title"] == "B"
    assert rows[0]["resolved"] is True
    assert rows[0]["link_text"] == "[[B]]"


def test_brain_links_with_unresolved(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111", title="A")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222", title="B")
    _link_row(test_db, src=a, dst=b, text="[[B]]")
    _unresolved_row(test_db, src=a, text="[[Nope]]")
    rows = mcp_server.brain_links(id_prefix=a[:8], include_unresolved=True)
    resolved = [r for r in rows if r["resolved"]]
    unresolved = [r for r in rows if not r["resolved"]]
    assert len(resolved) == 1
    assert len(unresolved) == 1
    assert unresolved[0]["dst_document_id"] is None
    assert unresolved[0]["dst_title"] is None
    assert unresolved[0]["dst_kind"] is None
    assert unresolved[0]["link_text"] == "[[Nope]]"


def test_brain_links_unknown_id_errors(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_links(id_prefix="ffffff")
    assert "not found" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# brain_orphans
# ---------------------------------------------------------------------------


def test_brain_orphans_vault_only_default(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111", title="A")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222", title="B")
    _make_doc(
        test_db,
        doc_id="33333333-3333-3333-3333-333333333333",
        title="D orphan",
    )
    _make_doc(
        test_db,
        doc_id="44444444-4444-4444-4444-444444444444",
        title="E ingested orphan",
        kind="ingested",
    )
    _link_row(test_db, src=a, dst=b, text="[[B]]")
    rows = mcp_server.brain_orphans()
    titles = {r["title"] for r in rows}
    # Only the vault orphan; ingested-tier stays out by default.
    assert titles == {"D orphan"}
    for r in rows:
        assert r["kind"] == "vault"


def test_brain_orphans_with_all_includes_ingested(
    test_db: psycopg.Connection,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _make_doc(
        test_db,
        doc_id="33333333-3333-3333-3333-333333333333",
        title="D orphan",
    )
    _make_doc(
        test_db,
        doc_id="44444444-4444-4444-4444-444444444444",
        title="E ingested orphan",
        kind="ingested",
    )
    rows = mcp_server.brain_orphans(vault_only=False)
    titles = {r["title"] for r in rows}
    assert titles == {"D orphan", "E ingested orphan"}


def test_brain_orphans_empty_graph(
    test_db: psycopg.Connection,  # noqa: ARG001 — fresh schema fixture
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    assert mcp_server.brain_orphans() == []


# ---------------------------------------------------------------------------
# brain_note_new
# ---------------------------------------------------------------------------


def test_brain_note_new_happy_path(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_note_new(
        title="person-x Q1 review",
        body="A few thoughts on the quarter.",
    )
    assert payload["vault_path"] == "person-x-q1-review.md"
    target = vault_dir / "person-x-q1-review.md"
    assert target.is_file()
    fields, body = parse_frontmatter(target.read_text())
    assert fields["title"] == "person-x Q1 review"
    assert fields["id"] == payload["document_id"]
    assert fields["kind"] == "vault"
    assert "source-mcp" in fields["tags"]
    assert "A few thoughts on the quarter." in body
    # Sync ran — the DB row exists.
    row = test_db.execute(
        "SELECT title, kind, tags FROM documents WHERE id = %s",
        (payload["document_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] == "person-x Q1 review"
    assert row[1] == "vault"
    assert "source-mcp" in row[2]


def test_brain_note_new_with_folder_and_tags(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_note_new(
        title="Sprint plan",
        body="Plan body.",
        folder="projects",
        tags=["interview", "career"],
    )
    target = vault_dir / "projects" / "sprint-plan.md"
    assert target.is_file()
    fields, _ = parse_frontmatter(target.read_text())
    assert sorted(fields["tags"]) == ["career", "interview", "source-mcp"]
    assert payload["vault_path"] == "projects/sprint-plan.md"
    # Source path is absolute and points at the file we wrote.
    assert payload["source_path"] == str(target)
    _ = test_db  # silence unused warning


def test_brain_note_new_dedups_source_mcp_in_user_tags(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """User-supplied 'source-mcp' must not duplicate the auto tag."""
    mcp_server.brain_note_new(
        title="Dup tag",
        body="body",
        tags=["source-mcp", "career"],
    )
    fields, _ = parse_frontmatter((vault_dir / "dup-tag.md").read_text())
    assert sorted(fields["tags"]) == ["career", "source-mcp"]


def test_brain_note_new_rejects_existing_path(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    mcp_server.brain_note_new(title="Same title", body="first")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="Same title", body="second")
    assert "already exists" in exc_info.value.error.message
    _ = vault_dir  # silence unused warning


def test_brain_note_new_rejects_traversal(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """``folder='../../etc'`` must be rejected before any disk write."""
    pre_files = sorted(p.name for p in vault_dir.iterdir())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(
            title="Escape", body="body", folder="../../etc"
        )
    msg = exc_info.value.error.message
    assert "vault" in msg.lower() or "folder" in msg.lower()
    # Vault is unchanged; nothing leaked.
    assert sorted(p.name for p in vault_dir.iterdir()) == pre_files
    # No DB row was created.
    cnt = test_db.execute(
        "SELECT count(*) FROM documents WHERE title = %s", ("Escape",)
    ).fetchone()
    assert cnt is not None
    assert cnt[0] == 0


def test_brain_note_new_rejects_unknown_template(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(
            title="X", body="body", template="nonexistent"
        )
    assert "nonexistent" in exc_info.value.error.message
    _ = vault_dir


def test_brain_note_new_rejects_missing_templates_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_embedder: object,
) -> None:
    """A vault without ``_templates/`` is rejected with a clear hint."""
    raw = tmp_path / "raw"
    raw.mkdir()
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=raw),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="X", body="body")
    assert "vault init" in exc_info.value.error.message


def test_brain_note_new_missing_vault_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_embedder: object,
) -> None:
    """A vault path that doesn't exist on disk is rejected up front."""
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=tmp_path / "does-not-exist",
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="X", body="body")
    assert "vault path does not exist" in exc_info.value.error.message


def test_brain_note_new_skips_template_when_blank(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Passing ``template=''`` writes only the user-supplied body."""
    mcp_server.brain_note_new(
        title="Bare", body="just my body", template=""
    )
    text = (vault_dir / "bare.md").read_text()
    fields, body = parse_frontmatter(text)
    assert body.strip() == "just my body"
    # The default note template would have added a `# Bare` header line —
    # confirm it's *not* there because we asked for no template.
    assert "# Bare" not in body
    assert fields["title"] == "Bare"


def test_brain_note_new_rejects_oversize_body(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Bodies above 256 KB must be rejected with INVALID_PARAMS."""
    big = "x" * (256 * 1024 + 1)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="Big", body=big)
    assert "262144" in exc_info.value.error.message  # 256 KB in bytes
    assert "max" in exc_info.value.error.message


def test_brain_note_new_rejects_empty_title(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="   ", body="body")
    assert "title is empty" in exc_info.value.error.message


# ---------------------------------------------------------------------------
# brain_daily
# ---------------------------------------------------------------------------


def test_brain_daily_creates_fresh(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_daily(date="2026-04-29")
    assert payload["created"] is True
    target = vault_dir / "daily" / "2026" / "2026-04-29.md"
    assert target.is_file()
    assert payload["vault_path"] == "daily/2026/2026-04-29.md"
    fields, _ = parse_frontmatter(target.read_text())
    assert fields["title"] == "2026-04-29"
    assert "source-mcp" in fields["tags"]
    # DB row exists.
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE id = %s",
        (payload["document_id"],),
    ).fetchone()
    assert row is not None


def test_brain_daily_idempotent(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    first = mcp_server.brain_daily(date="2026-04-29")
    second = mcp_server.brain_daily(date="2026-04-29")
    assert first["created"] is True
    assert second["created"] is False
    assert second["document_id"] == first["document_id"]
    assert second["vault_path"] == first["vault_path"]
    _ = vault_dir


def test_brain_daily_year_folder_created(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """The ``daily/<YYYY>/`` directory is created on demand."""
    assert not (vault_dir / "daily" / "2030").exists()
    mcp_server.brain_daily(date="2030-01-15")
    assert (vault_dir / "daily" / "2030").is_dir()


def test_brain_daily_invalid_date(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date="not-a-date")
    assert "YYYY-MM-DD" in exc_info.value.error.message


def test_brain_daily_default_today(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """No ``date`` arg → uses today's local date."""
    from datetime import date as date_cls

    payload = mcp_server.brain_daily()
    today = date_cls.today().isoformat()
    assert payload["vault_path"].endswith(f"{today}.md")
    assert (vault_dir / payload["vault_path"]).is_file()


# ---------------------------------------------------------------------------
# brain_link_proposal
# ---------------------------------------------------------------------------


def _seed_vault_note(
    vault_dir: Path,
    *,
    relative: str,
    doc_id: str,
    title: str,
    body: str = "Existing body.\n",
) -> Path:
    """Write a vault note with frontmatter directly to disk + return its path.

    Used by ``brain_link_proposal`` tests that need a known src on disk.
    The DB row is created separately via :func:`_make_doc` so callers can
    seed both halves consistently.
    """
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nid: {doc_id}\ntitle: \"{title}\"\nkind: vault\ntags: []\n---\n\n{body}"
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_brain_link_proposal_title_resolution(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    src_id = "11111111-1111-1111-1111-111111111111"
    dst_id = "22222222-2222-2222-2222-222222222222"
    _make_doc(test_db, doc_id=src_id, title="Q1 review", vault_path="q1-review.md")
    _make_doc(test_db, doc_id=dst_id, title="person-x conversation")
    src_path = _seed_vault_note(
        vault_dir,
        relative="q1-review.md",
        doc_id=src_id,
        title="Q1 review",
    )
    pre_text = src_path.read_text()
    payload = mcp_server.brain_link_proposal(
        src_id_prefix=src_id[:8],
        dst_id_or_title="person-x conversation",
    )
    assert payload["src_vault_path"] == "q1-review.md"
    assert payload["src_document_id"] == src_id
    assert payload["dst_document_id"] == dst_id
    assert payload["dst_title"] == "person-x conversation"
    assert payload["link_text"] == "person-x conversation"
    assert "[[person-x conversation]]" in payload["proposed_text"]
    assert payload["line_no"] >= 1
    # File is unchanged — proposals are read-only.
    assert src_path.read_text() == pre_text


def test_brain_link_proposal_id_prefix_resolution(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    src_id = "11111111-1111-1111-1111-111111111111"
    dst_id = "abcdef12-3456-7890-abcd-ef1234567890"
    _make_doc(test_db, doc_id=src_id, title="Source", vault_path="source.md")
    _make_doc(test_db, doc_id=dst_id, title="Some destination")
    _seed_vault_note(
        vault_dir, relative="source.md", doc_id=src_id, title="Source"
    )
    payload = mcp_server.brain_link_proposal(
        src_id_prefix=src_id[:8],
        dst_id_or_title="abcdef12",
    )
    assert payload["dst_document_id"] == dst_id
    assert payload["link_text"] == f"brain:{dst_id[:8]}"
    assert "[[brain:abcdef12]]" in payload["proposed_text"]


def test_brain_link_proposal_ambiguous_title(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    src_id = "11111111-1111-1111-1111-111111111111"
    _make_doc(test_db, doc_id=src_id, title="Source", vault_path="source.md")
    _make_doc(
        test_db,
        doc_id="22222222-2222-2222-2222-222222222222",
        title="Same name",
    )
    _make_doc(
        test_db,
        doc_id="33333333-3333-3333-3333-333333333333",
        title="Same name",
    )
    _seed_vault_note(
        vault_dir, relative="source.md", doc_id=src_id, title="Source"
    )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8],
            dst_id_or_title="Same name",
        )
    msg = exc_info.value.error.message
    assert "ambiguous" in msg
    # Candidate ids appear in the error so callers can disambiguate.
    assert "22222222"[:8] in msg or "22222222" in msg


def test_brain_link_proposal_unknown_dst(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    src_id = "11111111-1111-1111-1111-111111111111"
    _make_doc(test_db, doc_id=src_id, title="Source", vault_path="source.md")
    _seed_vault_note(
        vault_dir, relative="source.md", doc_id=src_id, title="Source"
    )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8],
            dst_id_or_title="Definitely missing title",
        )
    assert "no document found" in exc_info.value.error.message


def test_brain_link_proposal_rejects_ingested_src(
    test_db: psycopg.Connection,
    vault_dir: Path,  # noqa: ARG001 — kept so other paths are valid
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Ingested-tier sources must be rejected — proposals are vault-only."""
    src_id = "11111111-1111-1111-1111-111111111111"
    dst_id = "22222222-2222-2222-2222-222222222222"
    _make_doc(
        test_db,
        doc_id=src_id,
        title="Krisp transcript",
        kind="ingested",
        vault_path="_ingested/krisp/x.md",
    )
    _make_doc(test_db, doc_id=dst_id, title="Vault note")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8],
            dst_id_or_title="Vault note",
        )
    assert "vault-tier" in exc_info.value.error.message


def test_brain_link_proposal_empty_args(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError):
        mcp_server.brain_link_proposal(src_id_prefix="", dst_id_or_title="x")
    with pytest.raises(McpError):
        mcp_server.brain_link_proposal(src_id_prefix="abcdef", dst_id_or_title="")


def test_brain_link_proposal_writes_nothing(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """End-to-end check that the on-disk file + DB row are unchanged."""
    src_id = "11111111-1111-1111-1111-111111111111"
    dst_id = "22222222-2222-2222-2222-222222222222"
    _make_doc(test_db, doc_id=src_id, title="Q1", vault_path="q1.md")
    _make_doc(test_db, doc_id=dst_id, title="person-x")
    src_path = _seed_vault_note(
        vault_dir, relative="q1.md", doc_id=src_id, title="Q1"
    )
    pre_text = src_path.read_text()
    pre_db = test_db.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s",
        (src_id,),
    ).fetchone()
    mcp_server.brain_link_proposal(
        src_id_prefix=src_id[:8], dst_id_or_title="person-x"
    )
    assert src_path.read_text() == pre_text
    post_db = test_db.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s",
        (src_id,),
    ).fetchone()
    assert pre_db == post_db  # no link inserted


def test_brain_link_proposal_unknown_src(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix="ffffff", dst_id_or_title="anything"
        )
    assert "not found" in exc_info.value.error.message


def test_brain_link_proposal_missing_src_file(
    test_db: psycopg.Connection,
    vault_dir: Path,  # noqa: ARG001 — vault exists, file doesn't
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Source row exists in DB but the file is gone — surface clearly."""
    src_id = "11111111-1111-1111-1111-111111111111"
    dst_id = "22222222-2222-2222-2222-222222222222"
    _make_doc(test_db, doc_id=src_id, title="Phantom", vault_path="phantom.md")
    _make_doc(test_db, doc_id=dst_id, title="person-x")
    # Note: no _seed_vault_note call — file is intentionally missing.
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8], dst_id_or_title="person-x"
        )
    assert "missing on disk" in exc_info.value.error.message


def test_brain_link_proposal_src_without_vault_path(
    test_db: psycopg.Connection,
    vault_dir: Path,  # noqa: ARG001
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Vault doc with NULL ``vault_path`` cannot be a link source."""
    src_id = "11111111-1111-1111-1111-111111111111"
    _make_doc(test_db, doc_id=src_id, title="No path", vault_path=None)
    _make_doc(
        test_db,
        doc_id="22222222-2222-2222-2222-222222222222",
        title="person-x",
    )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8], dst_id_or_title="person-x"
        )
    assert "no vault_path" in exc_info.value.error.message


def test_brain_link_proposal_id_prefix_ambiguous(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A 6-char hex prefix matching multiple docs surfaces an INVALID_PARAMS."""
    src_id = "11111111-1111-1111-1111-111111111111"
    _make_doc(test_db, doc_id=src_id, title="Source", vault_path="source.md")
    # Two docs whose IDs share the same 6-char prefix.
    _make_doc(
        test_db,
        doc_id="aaaaaaa1-1111-1111-1111-111111111111",
        title="Target one",
    )
    _make_doc(
        test_db,
        doc_id="aaaaaaa2-2222-2222-2222-222222222222",
        title="Target two",
    )
    _seed_vault_note(
        vault_dir, relative="source.md", doc_id=src_id, title="Source"
    )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix=src_id[:8], dst_id_or_title="aaaaaaa"
        )
    assert "ambiguous" in exc_info.value.error.message


def test_brain_link_proposal_id_lookalike_falls_through_to_title(
    test_db: psycopg.Connection,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A title that *looks* hex but matches no id falls through to title resolution.

    Regression: the id-prefix branch must not raise "no document found"
    when the value happens to be hex but the user meant it as a title.
    """
    src_id = "11111111-1111-1111-1111-111111111111"
    _make_doc(test_db, doc_id=src_id, title="Source", vault_path="source.md")
    # Title is hex-only and 6+ chars but does NOT match any id prefix.
    _make_doc(
        test_db,
        doc_id="99999999-9999-9999-9999-999999999999",
        title="abcdef",
    )
    _seed_vault_note(
        vault_dir, relative="source.md", doc_id=src_id, title="Source"
    )
    payload = mcp_server.brain_link_proposal(
        src_id_prefix=src_id[:8], dst_id_or_title="abcdef"
    )
    assert payload["dst_document_id"] == "99999999-9999-9999-9999-999999999999"
    # Title-resolution path: link_text equals the dst title.
    assert payload["link_text"] == "abcdef"


# ---------------------------------------------------------------------------
# DB-error wrapping for the read-only graph tools
# ---------------------------------------------------------------------------


class _BoomConnect:
    """Stub that mimics ``connect()``: enters a context that raises immediately.

    Mirrors the helper in ``test_mcp_server.py`` — kept private here so the
    two test modules don't share state.
    """

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or psycopg.OperationalError("simulated outage")

    def __call__(self, _url: str) -> _BoomConnect:
        return self

    def __enter__(self) -> Any:
        raise self._exc

    def __exit__(self, *_: object) -> None:
        return None


def test_brain_backlinks_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_backlinks(id_prefix="abcdef")
    msg = exc_info.value.error.message
    assert "database error" in msg
    assert "OperationalError" in msg
    assert "simulated outage" not in msg


def test_brain_links_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_links(id_prefix="abcdef")
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_orphans_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_orphans()
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_link_proposal_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_link_proposal(
            src_id_prefix="abcdef", dst_id_or_title="anything"
        )
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_note_new_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="DBerr", body="body")
    msg = exc_info.value.error.message
    assert "database error" in msg


def test_brain_daily_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date="2026-04-29")
    msg = exc_info.value.error.message
    assert "database error" in msg


# ---------------------------------------------------------------------------
# Embed-error wrapping for the authoring tools
# ---------------------------------------------------------------------------


class _BoomEmbedder:
    """Embedder that always raises on ``embed`` — mirrors test_mcp_server.py."""

    dim = 4096

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        from brain.embeddings import OllamaEmbedError

        raise OllamaEmbedError("rate limited")

    def count_tokens(self, text: str) -> int:
        return 1


def test_brain_note_new_wraps_embed_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
    vault_dir: Path,  # noqa: ARG001
) -> None:
    """A re-embed failure during note creation must surface as McpError."""
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_note_new(title="EmbedErr", body="body that needs embedding")
    msg = exc_info.value.error.message
    assert "embedding failed" in msg
    assert "OllamaEmbedError" in msg


def test_brain_daily_wraps_embed_error(
    monkeypatch: pytest.MonkeyPatch,
    mcp_state: mcp_server._State,
    vault_dir: Path,  # noqa: ARG001
) -> None:
    monkeypatch.setattr(mcp_state, "embedder", _BoomEmbedder())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date="2026-04-29")
    msg = exc_info.value.error.message
    assert "embedding failed" in msg


# ---------------------------------------------------------------------------
# brain_daily — defensive paths around an existing-but-corrupt file
# ---------------------------------------------------------------------------


def test_brain_daily_existing_file_missing_id(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A pre-existing daily file with no ``id`` in its frontmatter is rejected."""
    iso_date = "2026-04-29"
    target_dir = vault_dir / "daily" / "2026"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{iso_date}.md"
    target.write_text(f"---\ntitle: \"{iso_date}\"\nkind: vault\n---\n\nbody\n")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date=iso_date)
    assert "no id in frontmatter" in exc_info.value.error.message


def test_brain_daily_existing_file_malformed_frontmatter(
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A pre-existing daily file with corrupt frontmatter surfaces a clean error."""
    iso_date = "2026-04-29"
    target_dir = vault_dir / "daily" / "2026"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{iso_date}.md"
    target.write_text("---\nfoo: [unclosed\n---\nbody\n")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date=iso_date)
    assert "malformed frontmatter" in exc_info.value.error.message


def test_brain_daily_missing_vault_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_embedder: object,
) -> None:
    """A vault path that doesn't exist on disk is rejected."""
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=tmp_path / "does-not-exist",
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_daily(date="2026-04-29")
    assert "vault path does not exist" in exc_info.value.error.message
