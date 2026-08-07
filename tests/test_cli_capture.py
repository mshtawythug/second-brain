"""Integration tests for `brain capture` against the real Postgres test DB.

All content is synthetic. Captures now route through ``create_vault_note``
(vault-tier, ``capture/`` folder) rather than ``ingest_document``. Tests
verify the regression fixes:

  * Invisibility bug: captures land as ``kind='vault'`` under ``capture/``,
    not as ``kind='ingested'`` under ``_ingested/manual/``.
  * Hang bug: the capture callback never constructs or invokes a graph syncer.

The ``--auto`` review path is driven by a fake enricher injected via
``monkeypatch`` of the ``brain.cli._build_enricher`` factory (a standard test
double — never a prod-module monkey-patch).
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.config import Config
from brain.enrichment import TagProposal
from brain.vault.frontmatter import parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _documents(conn: psycopg.Connection) -> list[tuple[str, list[str], str | None]]:
    return [
        (str(row[0]), list(row[1]), row[2])
        for row in conn.execute(
            "SELECT id, tags, summary FROM documents ORDER BY ingested_at"
        ).fetchall()
    ]


def _single_doc_id(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT id::text FROM documents LIMIT 1").fetchone()
    assert row is not None
    return str(row[0])


def _tags_of(conn: psycopg.Connection, doc_id: str) -> list[str]:
    row = conn.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _kind_and_vault_path(
    conn: psycopg.Connection, doc_id: str
) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT kind, vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def _mirror_tags(conn: psycopg.Connection, doc_id: str, vault_root: Path) -> list[str]:
    """Return the YAML ``tags`` from the doc's on-disk vault file.

    Resolves ``documents.vault_path`` to the file under ``vault_root``
    and parses its frontmatter — the assertion surface for "did the routing
    write back to the file, not just the DB?".
    """
    row = conn.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    vault_path_rel = row[0]
    assert vault_path_rel is not None, "capture should have written a vault file"
    mirror = vault_root / vault_path_rel
    assert mirror.exists(), f"vault file missing at {mirror}"
    fields, _body = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    return list(fields.get("tags") or [])


def _doc_exists(conn: psycopg.Connection, doc_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM documents WHERE id = %s", (doc_id,)
        ).fetchone()
        is not None
    )


def _doc_title(conn: psycopg.Connection, doc_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


@dataclass
class _FakeEnricher:
    """Stand-in for :class:`brain.enrichment.OllamaEnricher`.

    Only ``propose_tags`` is exercised by the ``--auto`` review path; the
    other methods stay untouched. Records call titles so tests can assert the
    enricher was actually consulted.
    """

    proposal: TagProposal
    calls: list[str] = field(default_factory=list)

    def propose_tags(
        self,
        *,
        title: str,
        summary: str,
        existing_vocab: list[str],
        current_tags: list[str],
        max_new: int = 1,
    ) -> TagProposal:
        self.calls.append(title)
        return self.proposal


@dataclass
class _SpyGraphSyncer:
    """Spy stand-in for :class:`brain.graph_rag.sync.GraphSyncer`.

    Records ``remove(conn, document_id)`` calls without touching AGE. Injected
    via ``monkeypatch`` of ``brain.cli._build_graph_syncer`` so tests can verify
    the conditional F1 logic (remove called iff ``graph_index_state`` row exists).
    """

    remove_calls: list[tuple[object, str]] = field(default_factory=list)

    def remove(self, conn: object, document_id: str) -> None:
        self.remove_calls.append((conn, document_id))


# ---------------------------------------------------------------------------
# Shared helper — seeds the inbox for Phase 2 tests.
# ---------------------------------------------------------------------------


def _capture(text: str, vault_path: Path) -> None:
    """Run ``brain capture --text <text>`` against an initialized vault."""
    vault_module.init_vault(vault_path)  # idempotent
    result = CliRunner().invoke(app, ["capture", "--text", text])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Phase 1 — core capture path.
# ---------------------------------------------------------------------------


def test_capture_creates_inbox_document(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`brain capture --text` inserts one document tagged `inbox`."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app, ["capture", "--text", "capture this thought about project-ko"]
    )

    assert result.exit_code == 0, result.output
    assert "captured" in result.output
    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][1] == ["inbox"]


def test_capture_writes_vault_tier_document(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Regression (invisibility bug): captures write kind='vault' under capture/.

    Before the fix captures wrote kind='ingested' under _ingested/manual/,
    which is hidden behind the Quartz "Show ingested" toggle.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app, ["capture", "--text", "a thought for the vault tier test"]
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    doc_id = docs[0][0]

    kind, vault_path_rel = _kind_and_vault_path(test_db, doc_id)
    assert kind == "vault", f"expected kind='vault', got {kind!r}"
    assert vault_path_rel is not None, "vault_path should be set"
    assert vault_path_rel.startswith("capture/"), (
        f"expected path under capture/, got {vault_path_rel!r}"
    )
    # File must actually exist on disk.
    assert (tmp_path / vault_path_rel).exists()


def test_capture_does_not_invoke_graph_syncer(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Regression (hang bug): capture callback never builds a graph syncer.

    Before the fix ingest_document called graph_syncer.reconcile() which
    serialized against the brain-mcp server's long-held graph transaction.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    build_syncer_calls: list[object] = []

    def _spy_build_graph_syncer(cfg: object) -> object:
        build_syncer_calls.append(cfg)
        return None  # safe no-op if somehow called

    from brain import cli as _cli

    monkeypatch.setattr(_cli, "_build_graph_syncer", _spy_build_graph_syncer)

    result = CliRunner().invoke(
        app, ["capture", "--text", "a thought that must not trigger graph sync"]
    )
    assert result.exit_code == 0, result.output
    assert build_syncer_calls == [], (
        "capture callback must NOT call _build_graph_syncer; "
        f"got {len(build_syncer_calls)} call(s)"
    )


def test_two_identical_captures_create_distinct_notes(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Re-capturing identical text creates two distinct vault notes (no dedup).

    The old ingest_document path deduped by content_hash; create_vault_note
    does not — every capture is a fresh note.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    text = "a repeated capture body"
    first = CliRunner().invoke(app, ["capture", "--text", text])
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(app, ["capture", "--text", text])
    assert second.exit_code == 0, second.output

    docs = _documents(test_db)
    assert len(docs) == 2, f"expected 2 distinct docs, got {len(docs)}"
    ids = {d[0] for d in docs}
    assert len(ids) == 2, "the two captures must have different UUIDs"
    assert all(d[1] == ["inbox"] for d in docs)


def test_extra_tags_union_with_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Extra `--tag` values are applied alongside the always-on `inbox` tag."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "tagged capture", "--tag", "idea"],
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert set(docs[0][1]) == {"inbox", "idea"}


def test_empty_text_exits_nonzero(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Whitespace-only content fails fast with exit code 1 and no row written."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(app, ["capture", "--text", "   "])

    assert result.exit_code == 1
    assert "empty" in result.output.lower()
    assert _documents(test_db) == []


def test_inbox_warn_threshold_zero_is_a_clean_usage_error(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """An invalid ``BRAIN_CAPTURE_INBOX_WARN_THRESHOLD`` is a clean exit-2 error.

    This test previously asserted ``isinstance(result.exception, ConfigError)``,
    which pinned the *presentation* that task #24 existed to remove: a raw
    ``ConfigError`` escaping as a Rich traceback. The CLI now renders the same
    boxed error a bad Typer flag gets, so a typo in an env var and a typo in a
    flag look alike. Assert what the user actually sees instead of the exception
    type that happened to carry it.

    ``exit_code == 2`` rather than ``!= 0`` is deliberate: ``!= 0`` would pass
    on a traceback again, silently restoring the behaviour this was rewritten
    for. A test that would accept the bug it was rewritten for is worse than one
    that is merely stale. Asserting the message *content* matters for the same
    reason — the whole point of the boxed form is that it names which variable
    is wrong.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD", "0")

    result = CliRunner().invoke(app, ["capture", "--text", "anything"])

    assert result.exit_code == 2, result.output
    assert "BRAIN_CAPTURE_INBOX_WARN_THRESHOLD" in result.output
    assert "integer >= 1" in result.output


def test_extra_tags_are_normalized_at_capture_boundary(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A non-canonical `--tag` is normalized before it hits documents.tags."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "normalize my tag", "--tag", "Mixed Case"],
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert set(docs[0][1]) == {"inbox", "mixed-case"}


def test_capture_reads_content_from_stdin(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """With no --text, content is read from stdin and tagged `inbox`."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app, ["capture"], input="some stdin capture body\n"
    )
    assert result.exit_code == 0, result.output
    assert "captured" in result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][1] == ["inbox"]


# ---------------------------------------------------------------------------
# Phase 2 — inbox review + list.
# ---------------------------------------------------------------------------


def test_review_promote_removes_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`p` promotes: the `inbox` tag is dropped, the document is kept."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to promote", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="p\n")

    assert result.exit_code == 0, result.output
    assert _doc_exists(test_db, doc_id)
    assert _tags_of(test_db, doc_id) == []
    # The vault file's frontmatter must be written back too — no stale inbox.
    assert "inbox" not in _mirror_tags(test_db, doc_id, tmp_path)


def test_review_tag_adds_tag_and_removes_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`t foo` adds `foo` and drops `inbox`."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to tag", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="t foo\n")

    assert result.exit_code == 0, result.output
    assert _tags_of(test_db, doc_id) == ["foo"]
    # The vault file gains the routing tag and loses inbox, matching the DB.
    mirror_tags = _mirror_tags(test_db, doc_id, tmp_path)
    assert "foo" in mirror_tags
    assert "inbox" not in mirror_tags


def test_review_skip_keeps_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`s` skips: the `inbox` tag survives."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to skip", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="s\n")

    assert result.exit_code == 0, result.output
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_review_quit_prints_remaining_count(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`q` exits early and prints the count of inbox items still remaining."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("first inbox thought", tmp_path)
    _capture("second inbox thought", tmp_path)

    result = CliRunner().invoke(app, ["capture", "review"], input="q\n")

    assert result.exit_code == 0, result.output
    assert "2 item(s) remaining in inbox" in result.output
    # Nothing was processed — both rows keep their inbox tag.
    assert all(tags == ["inbox"] for _id, tags, _s in _documents(test_db))


def test_review_discard_yes_deletes_row_and_file(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`d` then `y` deletes the document row AND removes its vault file."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to discard", tmp_path)
    doc_id = _single_doc_id(test_db)
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    vault_path_rel = row[0]
    assert vault_path_rel is not None, "capture should have written a vault file"
    vault_file = tmp_path / vault_path_rel
    assert vault_file.exists()

    result = CliRunner().invoke(app, ["capture", "review"], input="d\ny\n")

    assert result.exit_code == 0, result.output
    assert not _doc_exists(test_db, doc_id)
    assert not vault_file.exists()


def test_review_discard_no_keeps_document_intact(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`d` then `N` (or anything not y/Y) leaves the document fully intact."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to keep", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="d\nN\n")

    assert result.exit_code == 0, result.output
    assert _doc_exists(test_db, doc_id)
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_review_auto_routes_with_fake_enricher(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--auto` applies proposed tags and removes `inbox` (fake enricher)."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to auto-route", tmp_path)
    doc_id = _single_doc_id(test_db)
    # The auto path requires a non-NULL summary (LLM unreliable on raw bodies).
    test_db.execute(
        "UPDATE documents SET summary = %s, summary_model = 'fake', "
        "summary_at = NOW() WHERE id = %s",
        ("a synthetic summary", doc_id),
    )
    enricher = _FakeEnricher(proposal=TagProposal(existing=["idea"], new=["fresh"]))
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)

    result = CliRunner().invoke(app, ["capture", "review", "--auto"])

    assert result.exit_code == 0, result.output
    assert len(enricher.calls) == 1  # enricher consulted once for the item
    tags = _tags_of(test_db, doc_id)
    assert "inbox" not in tags
    assert set(tags) == {"idea", "fresh"}
    # The vault file's frontmatter mirrors the routed DB tags exactly.
    mirror_tags = _mirror_tags(test_db, doc_id, tmp_path)
    assert "inbox" not in mirror_tags
    assert set(mirror_tags) == {"idea", "fresh"}


def test_review_auto_leaves_unsummarized_item_in_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--auto` leaves a summary-less item untouched (never aborts the batch)."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("an unsummarized thought", tmp_path)
    doc_id = _single_doc_id(test_db)
    enricher = _FakeEnricher(proposal=TagProposal(existing=["idea"], new=[]))
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)

    result = CliRunner().invoke(app, ["capture", "review", "--auto"])

    assert result.exit_code == 0, result.output
    assert enricher.calls == []  # never consulted — no summary
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_review_limit_respected(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--limit 1` surfaces a single item (total shown is 1, not 3)."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("inbox thought one", tmp_path)
    _capture("inbox thought two", tmp_path)
    _capture("inbox thought three", tmp_path)

    result = CliRunner().invoke(
        app, ["capture", "review", "--limit", "1"], input="s\n"
    )

    assert result.exit_code == 0, result.output
    assert "[1/1]" in result.output
    assert "/3]" not in result.output


def test_capture_list_json_every_item_has_inbox_tag(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`brain capture list --json` emits an array; each item carries `inbox`."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("first listed thought", tmp_path)
    _capture("second listed thought", tmp_path)

    result = CliRunner().invoke(app, ["capture", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert all("inbox" in item["tags"] for item in payload)


def test_review_empty_inbox_reports_empty(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`review` on an empty inbox prints a friendly notice and exits 0."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(app, ["capture", "review"])

    assert result.exit_code == 0, result.output
    assert "inbox is empty" in result.output


def test_review_unknown_command_leaves_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """An unrecognized action is reported and leaves the item in the inbox."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought with a typo'd action", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="x\n")

    assert result.exit_code == 0, result.output
    assert "unknown command" in result.output
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_review_tag_without_argument_leaves_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`t` with no tag argument is a no-op that keeps the item in the inbox."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought tagged with nothing", tmp_path)
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="t\n")

    assert result.exit_code == 0, result.output
    assert "no tags given" in result.output
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_review_auto_no_proposal_leaves_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--auto` with an empty proposal leaves the (summarized) item in inbox."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought with no good tags", tmp_path)
    doc_id = _single_doc_id(test_db)
    test_db.execute(
        "UPDATE documents SET summary = %s, summary_model = 'fake', "
        "summary_at = NOW() WHERE id = %s",
        ("a synthetic summary", doc_id),
    )
    enricher = _FakeEnricher(proposal=TagProposal(existing=[], new=[]))
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)

    result = CliRunner().invoke(app, ["capture", "review", "--auto"])

    assert result.exit_code == 0, result.output
    assert "no tags proposed" in result.output
    assert _tags_of(test_db, doc_id) == ["inbox"]


def test_capture_list_empty_inbox_reports_empty(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`list` (human) on an empty inbox prints a friendly notice."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(app, ["capture", "list"])

    assert result.exit_code == 0, result.output
    assert "inbox is empty" in result.output


def test_capture_list_human_table_lists_inbox_only(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """The human table lists inbox docs and omits promoted (non-inbox) ones."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("still in the inbox", tmp_path)
    inbox_id = _single_doc_id(test_db)
    _capture("about to be promoted", tmp_path)
    promoted_id = next(d for d, _tags, _s in _documents(test_db) if d != inbox_id)
    # Promote the second doc out of the inbox (drop the inbox tag).
    promote = CliRunner().invoke(app, ["tag", promoted_id[:8], "-inbox"])
    assert promote.exit_code == 0, promote.output

    result = CliRunner().invoke(app, ["capture", "list"])

    assert result.exit_code == 0, result.output
    assert inbox_id[:8] in result.output
    assert promoted_id[:8] not in result.output


# ---------------------------------------------------------------------------
# Guard / error-handling tests (new in fix pass).
# ---------------------------------------------------------------------------


def test_capture_exits_when_vault_path_not_configured(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
) -> None:
    """With no vault configured, capture exits 1 and prints a friendly message.

    Patches ``brain._capture_command.Config`` so its ``load()`` method returns
    a Config whose ``vault_path`` is ``None`` — the only way to reach this
    guard since ``Config.load()`` always resolves a default path from the env.
    """
    patch_embedder(fake_embedder)

    import brain._capture_command as _cmd

    base_cfg = Config(database_url=TEST_DATABASE_URL)
    # Bypass the type annotation at runtime — vault_path=None is the sentinel
    # the guard checks; Config.load() never produces it through env vars alone.
    no_vault_cfg = dataclasses.replace(base_cfg, vault_path=None)  # type: ignore[arg-type]

    _OrigConfig = _cmd.Config

    class _NullVaultConfig:
        @staticmethod
        def load() -> Config:
            return no_vault_cfg  # type: ignore[return-value]

    monkeypatch.setattr(_cmd, "Config", _NullVaultConfig)

    result = CliRunner().invoke(app, ["capture", "--text", "some content"])

    assert result.exit_code == 1
    assert "vault path is not configured" in result.output


def test_capture_exits_on_vault_sync_error(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A ``VaultNoteSyncError`` from ``create_vault_note`` prints each error line
    and exits 1 instead of surfacing a raw traceback.

    Simulated by providing a vault dir that is missing ``_templates/note.md``
    (``init_vault`` intentionally skipped so the template is absent).
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    # Deliberately NOT calling init_vault — _templates/note.md is absent,
    # which causes create_vault_note to raise VaultNoteSyncError.
    # Create the capture/ dir so the path exists but the template is missing.
    (tmp_path / "capture").mkdir(parents=True, exist_ok=True)

    result = CliRunner().invoke(app, ["capture", "--text", "a sync error probe"])

    assert result.exit_code == 1
    # The CLI must print a friendly "sync error: …" line rather than a traceback.
    assert "sync error" in result.output or "vault note sync" in result.output.lower()


# ---------------------------------------------------------------------------
# Performance-fix regression tests (Finding 1 + Finding 2).
# ---------------------------------------------------------------------------


def test_review_discard_does_not_call_graph_syncer(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """F1 regression: discard of a doc with NO ``graph_index_state`` row must
    NOT call ``graph_syncer.remove``.

    Fresh captures (never graph-indexed) must pay zero AGE cost on discard.
    ``capture_review`` now builds a syncer (symmetric with ``brain rm``), but
    ``_discard_item`` only calls ``remove`` when ``graph_index_state`` exists for
    the document. Injects a ``_SpyGraphSyncer`` to verify no ``remove`` call.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a fresh capture to discard — no graph state", tmp_path)

    # Arrange: inject a spy syncer; no graph_index_state row is seeded.
    spy = _SpyGraphSyncer()
    from brain import cli as _cli

    monkeypatch.setattr(_cli, "_build_graph_syncer", lambda cfg: spy)

    # Act: drive the review -> discard (d + y) path.
    result = CliRunner().invoke(app, ["capture", "review"], input="d\ny\n")

    # Assert: exit 0, row deleted, syncer.remove NOT called.
    assert result.exit_code == 0, result.output
    assert spy.remove_calls == [], (
        "syncer.remove must NOT be called for a doc with no graph_index_state row; "
        f"got {len(spy.remove_calls)} call(s)"
    )


def test_review_discard_cascades_relational_graph_rows(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Integration (cascade): discarding a document removes its graph_entity_mentions,
    graph_edge_contributions, AND graph_index_state rows via ON DELETE CASCADE.

    Seeds synthetic rows for all three tables, then discards via ``brain capture
    review``. Asserts all are gone after the DELETE.

    Note: ``graph_relationships`` and community tables are *derived* aggregate
    state (not cascade-covered). Cleaning those requires ``graph_syncer.remove``
    (see F1 conditional logic) — they reconcile on the next rebuild if skipped.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought with synthetic graph rows for cascade test", tmp_path)
    doc_id = _single_doc_id(test_db)

    # Arrange: insert two synthetic graph_entities nodes (needed for mentions
    # and for the edge-contribution canonical src_id < dst_id pair).
    entity_row_a = test_db.execute(
        "INSERT INTO graph_entities (entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s) RETURNING id::text",
        ("person", "Synthetic Entity Cascade Alpha", "synthetic-entity-cascade-alpha"),
    ).fetchone()
    entity_row_b = test_db.execute(
        "INSERT INTO graph_entities (entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s) RETURNING id::text",
        ("person", "Synthetic Entity Cascade Beta", "synthetic-entity-cascade-beta"),
    ).fetchone()
    assert entity_row_a is not None and entity_row_b is not None
    entity_id_a: str = str(entity_row_a[0])
    entity_id_b: str = str(entity_row_b[0])

    # graph_entity_mentions — FK cascades on documents DELETE.
    test_db.execute(
        "INSERT INTO graph_entity_mentions (entity_id, document_id, source) "
        "VALUES (%s::uuid, %s::uuid, %s)",
        (entity_id_a, doc_id, "people"),
    )
    # graph_edge_contributions — canonical src_id < dst_id (UUID string order).
    src_id = min(entity_id_a, entity_id_b)
    dst_id = max(entity_id_a, entity_id_b)
    test_db.execute(
        "INSERT INTO graph_edge_contributions "
        "(document_id, src_id, dst_id, cooccur_count) "
        "VALUES (%s::uuid, %s::uuid, %s::uuid, %s)",
        (doc_id, src_id, dst_id, 1),
    )
    # graph_index_state — FK is only to documents(id); no entity rows needed.
    test_db.execute(
        "INSERT INTO graph_index_state "
        "(document_id, aspect, content_hash, inputs_hash, extractor_ver) "
        "VALUES (%s::uuid, 'people', 'hash-cascade-test', 'inputs-cascade-test', 'v1')",
        (doc_id,),
    )
    # Sanity-checks: all three rows exist before discard.
    assert test_db.execute(
        "SELECT 1 FROM graph_entity_mentions WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is not None, "graph_entity_mentions should exist before discard"
    assert test_db.execute(
        "SELECT 1 FROM graph_edge_contributions WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is not None, "graph_edge_contributions should exist before discard"
    assert test_db.execute(
        "SELECT 1 FROM graph_index_state WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is not None, "graph_index_state should exist before discard"

    # Inject a no-op spy syncer (graph_index_state row IS seeded, so remove
    # will be called, but the spy does nothing — keeps the test DB-clean).
    spy = _SpyGraphSyncer()
    from brain import cli as _cli

    monkeypatch.setattr(_cli, "_build_graph_syncer", lambda cfg: spy)

    # Act: discard via capture review.
    result = CliRunner().invoke(app, ["capture", "review"], input="d\ny\n")

    assert result.exit_code == 0, result.output
    # documents row must be gone.
    assert not _doc_exists(test_db, doc_id), "document should be deleted after discard"
    # All three relational graph tables must be cleaned via FK ON DELETE CASCADE.
    assert test_db.execute(
        "SELECT 1 FROM graph_entity_mentions WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is None, (
        "graph_entity_mentions rows must be removed by FK ON DELETE CASCADE"
    )
    assert test_db.execute(
        "SELECT 1 FROM graph_edge_contributions WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is None, (
        "graph_edge_contributions rows must be removed by FK ON DELETE CASCADE"
    )
    assert test_db.execute(
        "SELECT 1 FROM graph_index_state WHERE document_id = %s::uuid", (doc_id,)
    ).fetchone() is None, (
        "graph_index_state rows must be removed by FK ON DELETE CASCADE"
    )


def test_review_auto_calls_list_existing_tags_once(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Regression (Finding 2): `review --auto` calls list_existing_tags exactly
    once regardless of inbox size (N+1 fix).

    Before the fix, `list_existing_tags(conn)` was called inside the per-item
    loop — O(N) queries for N inbox items. The hoisted call should produce
    exactly 1 query for an inbox of >= 2 items.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    # Create two inbox items, both with summaries so the enricher is consulted.
    _capture("first auto-route thought for tag-query test", tmp_path)
    _capture("second auto-route thought for tag-query test", tmp_path)
    for doc_id, _tags, _summary in _documents(test_db):
        test_db.execute(
            "UPDATE documents SET summary = %s, summary_model = 'fake', "
            "summary_at = NOW() WHERE id = %s",
            ("a synthetic summary for auto-route tag test", doc_id),
        )

    # Arrange: spy on list_existing_tags at the _capture_command import site.
    import brain._capture_command as _cmd

    tag_query_calls: list[object] = []
    original_list_existing_tags = _cmd.list_existing_tags

    def _spy_list_existing_tags(conn: object) -> list[str]:
        tag_query_calls.append(conn)
        return original_list_existing_tags(conn)  # type: ignore[arg-type]

    monkeypatch.setattr(_cmd, "list_existing_tags", _spy_list_existing_tags)

    enricher = _FakeEnricher(proposal=TagProposal(existing=["idea"], new=[]))
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)

    # Act: run --auto over 2 inbox items.
    result = CliRunner().invoke(app, ["capture", "review", "--auto"])

    assert result.exit_code == 0, result.output
    # list_existing_tags must be called exactly once — NOT once per item.
    assert len(tag_query_calls) == 1, (
        f"list_existing_tags should be called once (hoisted), "
        f"got {len(tag_query_calls)} calls for {len(_documents(test_db))} items"
    )


def test_review_discard_with_graph_state_calls_syncer_remove_once(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """F1: discard of a doc WITH a ``graph_index_state`` row calls ``syncer.remove``
    exactly once with the correct ``document_id``.

    Seeds a ``graph_index_state`` row (FK is only to ``documents(id)`` — no entity
    rows needed). Injects a ``_SpyGraphSyncer`` and asserts ``remove`` is called
    once after the DELETE.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a graph-indexed thought to discard", tmp_path)
    doc_id = _single_doc_id(test_db)

    # Arrange: seed a graph_index_state row — doc was previously graph-indexed.
    test_db.execute(
        "INSERT INTO graph_index_state "
        "(document_id, aspect, content_hash, inputs_hash, extractor_ver) "
        "VALUES (%s::uuid, 'people', 'hash-f1-test', 'inputs-f1-test', 'v1')",
        (doc_id,),
    )

    spy = _SpyGraphSyncer()
    from brain import cli as _cli

    monkeypatch.setattr(_cli, "_build_graph_syncer", lambda cfg: spy)

    # Act
    result = CliRunner().invoke(app, ["capture", "review"], input="d\ny\n")

    # Assert
    assert result.exit_code == 0, result.output
    assert not _doc_exists(test_db, doc_id), "document should be deleted after discard"
    assert len(spy.remove_calls) == 1, (
        f"syncer.remove must be called exactly once for a graph-indexed doc; "
        f"got {len(spy.remove_calls)} call(s)"
    )
    _conn, called_doc_id = spy.remove_calls[0]
    assert called_doc_id == doc_id, (
        f"syncer.remove called with wrong doc_id: {called_doc_id!r} != {doc_id!r}"
    )


def test_capture_strips_whitespace_from_explicit_title(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """F2: an explicit ``--title`` with surrounding whitespace is stored trimmed.

    ``brain capture --title "  Padded Title  "`` must store ``"Padded Title"``
    (not the padded string) in ``documents.title``.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "some capture body", "--title", "  Padded Title  "],
    )

    assert result.exit_code == 0, result.output
    doc_id = _single_doc_id(test_db)
    assert _doc_title(test_db, doc_id) == "Padded Title"


# ---------------------------------------------------------------------------
# F1 — `brain capture --json`, the confirmation a Stop-hook nudge asserts on.
# ---------------------------------------------------------------------------

#: The human line `brain capture` has always printed. Wrappers, the demo GIF
#: scripts, and user aliases parse it, so it is pinned byte-for-byte.
_HUMAN_LINE_RE = re.compile(r"^✓ captured [0-9a-f]{8}  \(.+\)  \[inbox\]$")

#: Exactly the keys F1 documents — no more, no fewer. An agent that learns to
#: read one shape must not have a seventh key appear under it silently.
_JSON_KEYS = {"document_id", "id_prefix", "title", "tags", "vault_path", "status"}


def test_capture_json_shape(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--json` emits exactly the six documented keys, keyed like the MCP twin."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "capture",
            "--json",
            "--text",
            "pgvector caps HNSW at 2000 dims, so the 4096-dim backend has no index",
            "--tag",
            "pgvector",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == _JSON_KEYS
    assert uuid.UUID(payload["document_id"])
    assert payload["id_prefix"] == payload["document_id"][:8]
    assert payload["title"]
    assert "inbox" in payload["tags"]
    assert "pgvector" in payload["tags"]
    assert payload["vault_path"].startswith("capture/")
    assert payload["status"] == "ingested"
    # The reported path is real, and it is the row the DB agrees on.
    assert (tmp_path / payload["vault_path"]).is_file()
    _kind, stored_path = _kind_and_vault_path(test_db, payload["document_id"])
    assert stored_path == payload["vault_path"]


def test_capture_json_suppresses_human_line(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(
        app, ["capture", "--json", "--text", "a synthetic thought"]
    )

    assert result.exit_code == 0, result.output
    assert "✓ captured" not in result.output


def test_capture_human_output_unchanged(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Backward-compat regression: without `--json` the line is byte-identical."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(app, ["capture", "--text", "a synthetic thought"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert _HUMAN_LINE_RE.match(lines[0]), f"human line drifted: {lines[0]!r}"


def test_capture_json_error_path_is_not_json(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A hook must not mistake an error for a success payload.

    `--json` never turns a failure into a success-shaped document: the empty
    content path keeps its exit code and its red stderr message in both modes.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    vault_module.init_vault(tmp_path)

    result = CliRunner().invoke(app, ["capture", "--json", "--text", "   "])

    assert result.exit_code == 1
    assert "capture content is empty" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)
