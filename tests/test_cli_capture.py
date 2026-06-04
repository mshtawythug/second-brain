"""Integration tests for `brain capture` against the real Postgres test DB.

All content is synthetic. Captures run with ``--no-enrich`` so the suite never
depends on a live Ollama; one dedicated test asserts the ``--no-enrich``
summary-NULL contract explicitly. The ``--auto`` review path is driven by a
fake enricher injected via ``monkeypatch`` of the ``brain.cli._build_enricher``
factory (a standard test double — never a prod-module monkey-patch).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.config import ConfigError
from brain.enrichment import TagProposal
from brain.vault.frontmatter import parse_frontmatter


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


def _mirror_tags(conn: psycopg.Connection, doc_id: str, vault_root: Path) -> list[str]:
    """Return the YAML ``tags`` from the doc's on-disk vault mirror.

    Resolves ``documents.vault_path`` to the mirror file under ``vault_root``
    and parses its frontmatter — the assertion surface for "did the routing
    write back to the mirror, not just the DB?".
    """
    row = conn.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    vault_path_rel = row[0]
    assert vault_path_rel is not None, "capture should have written a vault mirror"
    mirror = vault_root / vault_path_rel
    assert mirror.exists(), f"mirror missing at {mirror}"
    fields, _body = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    return list(fields.get("tags") or [])


def _doc_exists(conn: psycopg.Connection, doc_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM documents WHERE id = %s", (doc_id,)
        ).fetchone()
        is not None
    )


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

    result = CliRunner().invoke(
        app, ["capture", "--text", "capture this thought about project-ko", "--no-enrich"]
    )

    assert result.exit_code == 0, result.output
    assert "captured" in result.output
    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][1] == ["inbox"]


def test_recapture_same_text_skips_and_preserves_inbox(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Re-capturing identical text is a content-hash no-op; inbox is preserved."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    text = "a repeated capture body"

    first = CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    assert first.exit_code == 0, first.output
    before = _documents(test_db)
    assert len(before) == 1

    second = CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    assert second.exit_code == 0, second.output
    assert "already captured" in second.output

    after = _documents(test_db)
    assert len(after) == 1
    assert after[0][0] == before[0][0]  # same UUID — no new row
    assert after[0][1] == ["inbox"]


def test_recapture_force_creates_new_uuid(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--force` replaces the row under a new UUID, still tagged `inbox`."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    text = "forced re-capture body"

    CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    before = _documents(test_db)
    assert len(before) == 1
    original_id = before[0][0]

    forced = CliRunner().invoke(
        app, ["capture", "--text", text, "--no-enrich", "--force"]
    )
    assert forced.exit_code == 0, forced.output

    after = _documents(test_db)
    assert len(after) == 1
    assert after[0][0] != original_id  # new UUID
    assert after[0][1] == ["inbox"]


def test_no_enrich_leaves_summary_null(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`--no-enrich` skips summarization, so `documents.summary` stays NULL."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = CliRunner().invoke(
        app, ["capture", "--text", "unenriched capture body", "--no-enrich"]
    )
    assert result.exit_code == 0, result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][2] is None


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

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "tagged capture", "--no-enrich", "--tag", "idea"],
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

    result = CliRunner().invoke(app, ["capture", "--text", "   ", "--no-enrich"])

    assert result.exit_code == 1
    assert "empty" in result.output.lower()
    assert _documents(test_db) == []


def test_inbox_warn_threshold_zero_raises_config_error(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """An invalid BRAIN_CAPTURE_INBOX_WARN_THRESHOLD fails at config load."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD", "0")

    result = CliRunner().invoke(
        app, ["capture", "--text", "anything", "--no-enrich"]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)


def test_blank_content_type_raises_config_error(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """An explicitly-blank BRAIN_CAPTURE_CONTENT_TYPE fails at config load.

    Unset falls back to the default; set-but-blank is a config bug, not a
    "use the default" request.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_CAPTURE_CONTENT_TYPE", "")

    result = CliRunner().invoke(
        app, ["capture", "--text", "anything", "--no-enrich"]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)


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

    result = CliRunner().invoke(
        app,
        ["capture", "--text", "normalize my tag", "--no-enrich", "--tag", "Mixed Case"],
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

    result = CliRunner().invoke(
        app, ["capture", "--no-enrich"], input="some stdin capture body\n"
    )
    assert result.exit_code == 0, result.output
    assert "captured" in result.output

    docs = _documents(test_db)
    assert len(docs) == 1
    assert docs[0][1] == ["inbox"]


# ---------------------------------------------------------------------------
# Phase 2 — inbox review + list.
# ---------------------------------------------------------------------------


def _capture(text: str) -> None:
    """Run ``brain capture --text <text>`` (helper for Phase 2 seeding)."""
    result = CliRunner().invoke(app, ["capture", "--text", text, "--no-enrich"])
    assert result.exit_code == 0, result.output


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
    _capture("a thought to promote")
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="p\n")

    assert result.exit_code == 0, result.output
    assert _doc_exists(test_db, doc_id)
    assert _tags_of(test_db, doc_id) == []
    # The vault mirror's frontmatter must be written back too — no stale inbox.
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
    _capture("a thought to tag")
    doc_id = _single_doc_id(test_db)

    result = CliRunner().invoke(app, ["capture", "review"], input="t foo\n")

    assert result.exit_code == 0, result.output
    assert _tags_of(test_db, doc_id) == ["foo"]
    # The vault mirror gains the routing tag and loses inbox, matching the DB.
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
    _capture("a thought to skip")
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
    _capture("first inbox thought")
    _capture("second inbox thought")

    result = CliRunner().invoke(app, ["capture", "review"], input="q\n")

    assert result.exit_code == 0, result.output
    assert "2 item(s) remaining in inbox" in result.output
    # Nothing was processed — both rows keep their inbox tag.
    assert all(tags == ["inbox"] for _id, tags, _s in _documents(test_db))


def test_review_discard_yes_deletes_row_and_mirror(
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`d` then `y` deletes the document row AND removes its vault mirror."""
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    _capture("a thought to discard")
    doc_id = _single_doc_id(test_db)
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    vault_path_rel = row[0]
    assert vault_path_rel is not None, "capture should have written a vault mirror"
    mirror = tmp_path / vault_path_rel
    assert mirror.exists()

    result = CliRunner().invoke(app, ["capture", "review"], input="d\ny\n")

    assert result.exit_code == 0, result.output
    assert not _doc_exists(test_db, doc_id)
    assert not mirror.exists()


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
    _capture("a thought to keep")
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
    _capture("a thought to auto-route")
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
    # The vault mirror's frontmatter mirrors the routed DB tags exactly.
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
    _capture("an unsummarized thought")
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
    _capture("inbox thought one")
    _capture("inbox thought two")
    _capture("inbox thought three")

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
    _capture("first listed thought")
    _capture("second listed thought")

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
    _capture("a thought with a typo'd action")
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
    _capture("a thought tagged with nothing")
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
    _capture("a thought with no good tags")
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
    _capture("still in the inbox")
    inbox_id = _single_doc_id(test_db)
    _capture("about to be promoted")
    promoted_id = next(d for d, _tags, _s in _documents(test_db) if d != inbox_id)
    # Promote the second doc out of the inbox (drop the inbox tag).
    promote = CliRunner().invoke(app, ["tag", promoted_id[:8], "-inbox"])
    assert promote.exit_code == 0, promote.output

    result = CliRunner().invoke(app, ["capture", "list"])

    assert result.exit_code == 0, result.output
    assert inbox_id[:8] in result.output
    assert promoted_id[:8] not in result.output
