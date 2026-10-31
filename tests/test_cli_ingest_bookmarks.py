"""`brain ingest-bookmarks` end to end (RM-44), through the CLI and the 5434 database.

Every archive is a synthetic tree under ``tmp_path`` (``tests/social_archive_fixtures.py``).
Nothing here reads a real export or reaches the network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain import cli_ingest_bookmarks
from brain.cli import app
from tests import social_archive_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

TODAY = datetime.now(UTC).replace(microsecond=0)
RECENT = TODAY - 2 * fx.DAY
OLDER = TODAY - 40 * fx.DAY
PID = fx.post_id_at(RECENT)
OLD_PID = fx.post_id_at(OLDER)
UNDATED_PID = "20"
LIKE = fx.fields(PID, expanded_url=fx.status_url(PID) + "?s=20&t=SyntheticToken123")
OLD_LIKE = fx.fields(OLD_PID, text=fx.OTHER_TEXT)
URL = f"https://x.com/{fx.AUTHOR}/status/{PID}"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "brain-home"
    home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(home))
    return home


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-bookmarks", *args])


def _ingest(root: Path, *args: str) -> Result:
    return _run("--format", "x-archive", str(root), *args)


def _archive(tmp_path: Path, likes: list[dict], bookmarks: list[dict] | None = None) -> Path:
    return fx.write_archive(
        tmp_path / "archive", likes=[likes], bookmarks=None if bookmarks is None else [bookmarks]
    )


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    """The stored ``social`` documents keyed by their external id."""
    rows = conn.execute(
        """
        SELECT s.external_id, d.id::text, d.title, d.content, d.content_type, d.sensitivity,
               d.sent_at, d.metadata, d.tags, d.summary
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'social'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "sensitivity", "sent_at", "metadata",
            "tags", "summary")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_ids(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute("SELECT id::text FROM chunks ORDER BY id").fetchall()
    return [str(r[0]) for r in rows]


# --- scope ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [[], ["--format", "x-archive"], ["somewhere"]])
def test_without_format_and_archive_the_run_is_refused(args: list[str], home: Path) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert cli_ingest_bookmarks.SCOPE_REFUSAL in result.output


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--format", "y-archive", "dir"], "--format must be x-archive"),
        (["--format", "x-archive", "dir", "--since", "2026/10/01"], "--since must be YYYY-MM-DD"),
    ],
)
def test_a_malformed_scope_is_refused(args: list[str], reason: str, home: Path) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert f"ingest-bookmarks: {reason}" in result.output


def test_a_future_since_is_refused(tmp_path: Path, home: Path) -> None:
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()

    result = _ingest(_archive(tmp_path, [LIKE]), "--since", tomorrow)

    assert result.exit_code == 2
    assert "ingest-bookmarks: --since is in the future" in result.output


@pytest.mark.parametrize("name", ["export.zip", "EXPORT.ZIP"])
def test_a_zip_is_refused_with_unzip_advice(tmp_path: Path, home: Path, name: str) -> None:
    archive = tmp_path / name
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    result = _ingest(archive)

    assert result.exit_code == 2
    assert cli_ingest_bookmarks.ZIP_REFUSAL in result.output


def test_a_bad_sensitivity_is_refused(tmp_path: Path, home: Path) -> None:
    result = _ingest(_archive(tmp_path, [LIKE]), "--sensitivity", "secret")

    assert result.exit_code != 0
    assert "secret" in result.output


def test_a_refused_archive_exits_1_without_touching_the_database(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    (root / "data").mkdir(parents=True)
    (root / "data" / "like.js").write_text("alert(1)", encoding="utf-8")

    def no_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused archive must not open the database")

    monkeypatch.setattr(cli_ingest_bookmarks, "connect", no_connect)

    result = _ingest(root)

    assert result.exit_code == 1
    assert "data/like.js is refused: the prefix is not window.YTD.like.part0 =" in result.output


def test_a_missing_folder_exits_1(tmp_path: Path, home: Path) -> None:
    result = _ingest(tmp_path / "nowhere")

    assert result.exit_code == 1
    assert "not a folder" in result.output


# --- the run (AC2, AC3, AC4, AC5) ---------------------------------------------------------------


def test_each_post_becomes_one_normal_document(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE, OLD_LIKE, fx.fields(UNDATED_PID, text=fx.OTHER_TEXT)])

    result = _ingest(root, "--tag", "Reading List")

    assert result.exit_code == 0, result.output
    assert "found 3 posts (3 liked, 0 bookmarked)" in result.output
    assert "bookmarks file not present" in result.output
    assert "3 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 failed" in result.output
    docs = _docs(test_db)
    assert set(docs) == {f"x-{PID}", f"x-{OLD_PID}", f"x-{UNDATED_PID}"}
    doc = docs[f"x-{PID}"]
    assert doc["content"] == f"@{fx.AUTHOR}\n\n{fx.TEXT}\n\n{URL}"
    assert doc["content_type"] == "social_post"
    assert doc["sensitivity"] == "normal"
    assert doc["summary"] is None
    assert doc["tags"] == ["reading-list"]
    assert doc["sent_at"] == RECENT
    assert doc["metadata"]["collections"] == ["like"]
    assert "SyntheticToken123" not in str(doc) and "s=20" not in str(doc)
    assert f"  ingested {URL}" in result.output
    undated = docs[f"x-{UNDATED_PID}"]
    assert undated["sent_at"] is None
    assert "handle" not in undated["metadata"]


def test_a_rerun_is_a_no_op(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE, OLD_LIKE], bookmarks=[LIKE])
    assert _ingest(root).exit_code == 0
    before = _docs(test_db)
    chunks = _chunk_ids(test_db)
    embeds = embedder.embed_calls

    result = _ingest(root)

    assert result.exit_code == 0, result.output
    assert "0 ingested, 0 updated, 0 refreshed, 2 unchanged, 0 failed" in result.output
    assert embedder.embed_calls == embeds
    assert _chunk_ids(test_db) == chunks
    assert _docs(test_db) == before


def test_liked_and_bookmarked_is_one_document(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE], bookmarks=[fx.fields(PID), OLD_LIKE])

    result = _ingest(root)

    assert "found 2 posts (1 liked, 2 bookmarked)" in result.output
    docs = _docs(test_db)
    assert len(docs) == 2
    assert docs[f"x-{PID}"]["metadata"]["collections"] == ["bookmark", "like"]


def test_a_later_bookmark_patches_metadata_only_and_is_never_removed(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE])
    assert _ingest(root).exit_code == 0
    chunks = _chunk_ids(test_db)
    embeds = embedder.embed_calls
    _archive(tmp_path, [LIKE], bookmarks=[LIKE])

    added = _ingest(root)

    assert "0 ingested, 0 updated, 1 refreshed, 0 unchanged" in added.output
    assert f"  refreshed {URL}" in added.output
    assert _docs(test_db)[f"x-{PID}"]["metadata"]["collections"] == ["bookmark", "like"]
    assert embedder.embed_calls == embeds
    assert _chunk_ids(test_db) == chunks

    (root / "data" / "bookmark.js").unlink()
    gone = _ingest(root)

    assert gone.exit_code == 0
    assert "bookmarks file not present" in gone.output
    assert "1 unchanged" in gone.output
    assert _docs(test_db)[f"x-{PID}"]["metadata"]["collections"] == ["bookmark", "like"]


def test_skipped_entries_are_counted(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE, fx.fields(OLD_PID, text=None), fx.fields("x1")])

    result = _ingest(root)

    assert "1 entry without text skipped" in result.output
    assert "1 malformed entry skipped" in result.output
    assert len(_docs(test_db)) == 1


# --- --since, --max and --dry-run (AC6) -------------------------------------------------------


def test_since_keeps_posts_written_on_or_after_the_day(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE, OLD_LIKE, fx.fields(UNDATED_PID)])
    since = (datetime.now() - timedelta(days=10)).date().isoformat()

    result = _ingest(root, "--since", since)

    assert result.exit_code == 0, result.output
    assert "1 post before --since skipped" in result.output
    assert "1 undated post skipped (--since needs a date)" in result.output
    assert set(_docs(test_db)) == {f"x-{PID}"}


def test_max_keeps_the_newest_and_reports_the_rest(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [OLD_LIKE, LIKE])

    result = _ingest(root, "--max", "1")

    assert "stopped at the --max cap of 1: 1 more post qualifies" in result.output
    assert set(_docs(test_db)) == {f"x-{PID}"}


def test_dry_run_lists_and_writes_nothing(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = fx.fields(OLD_PID, text="evil \x1b]0;title\x07 text")
    root = _archive(tmp_path, [LIKE, hostile])

    def no_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("--dry-run must not open the database")

    monkeypatch.setattr(cli_ingest_bookmarks, "connect", no_connect)

    result = _ingest(root, "--dry-run")

    assert result.exit_code == 0, result.output
    assert f"  {RECENT.date().isoformat()}  @{fx.AUTHOR}: {fx.TEXT}" in result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert embedder.embed_calls == 0


# --- sensitivity (C2) and failures ----------------------------------------------------------------


def test_confidential_new_posts_and_escalate_only_reruns(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _archive(tmp_path, [LIKE])
    assert _ingest(root).exit_code == 0
    assert _docs(test_db)[f"x-{PID}"]["sensitivity"] == "normal"

    raised = _ingest(root, "--sensitivity", "confidential")

    assert f"  marked confidential {URL}" in raised.output
    assert "1 refreshed" in raised.output
    assert _docs(test_db)[f"x-{PID}"]["sensitivity"] == "confidential"

    lowered = _ingest(root, "--sensitivity", "normal")

    assert f"  kept confidential {URL} (lower it with: brain mark-normal " in lowered.output
    assert _docs(test_db)[f"x-{PID}"]["sensitivity"] == "confidential"

    plain = _ingest(root)

    assert "kept confidential" not in plain.output
    assert "1 unchanged" in plain.output


def test_a_new_post_can_be_stored_confidential(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _ingest(_archive(tmp_path, [LIKE]), "--sensitivity", "confidential")

    assert result.exit_code == 0, result.output
    assert _docs(test_db)[f"x-{PID}"]["sensitivity"] == "confidential"


def test_a_post_with_a_secret_is_refused_alone(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    leaky = fx.fields(OLD_PID, text=f"key {SYNTHETIC_AWS_KEY} here")
    root = _archive(tmp_path, [LIKE, leaky])

    result = _ingest(root)

    assert f"  refused https://x.com/i/web/status/{OLD_PID}" in result.output
    # The shared guard notice masks the finding (it quotes the title, as for every source).
    assert "AKIA****" in result.output
    assert "1 ingested, 0 updated, 0 refreshed, 0 unchanged, 1 failed" in result.output
    assert set(_docs(test_db)) == {f"x-{PID}"}

    allowed = _ingest(root, "--allow-secrets")

    assert "1 ingested, 0 updated, 0 refreshed, 1 unchanged, 0 failed" in allowed.output
    assert set(_docs(test_db)) == {f"x-{PID}", f"x-{OLD_PID}"}


def test_under_the_default_guard_a_secret_is_ingested_with_a_notice(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    leaky = fx.fields(OLD_PID, text=f"key {SYNTHETIC_AWS_KEY} here")

    result = _ingest(_archive(tmp_path, [leaky]))

    assert "1 ingested" in result.output
    assert "AKIA****" in result.output
    assert set(_docs(test_db)) == {f"x-{OLD_PID}"}


def test_a_failing_post_is_counted_and_the_run_continues(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = cli_ingest_bookmarks.ingest_document

    def flaky(conn: Any, **kwargs: Any) -> Any:
        if kwargs["source_external_id"] == f"x-{OLD_PID}":
            raise ValueError("synthetic failure with private detail")
        return real(conn, **kwargs)

    monkeypatch.setattr(cli_ingest_bookmarks, "ingest_document", flaky)

    result = _ingest(_archive(tmp_path, [LIKE, OLD_LIKE]))

    assert result.exit_code == 0
    assert f"  failed https://x.com/i/web/status/{OLD_PID}: ValueError" in result.output
    assert "private detail" not in result.output
    assert "1 ingested, 0 updated, 0 refreshed, 0 unchanged, 1 failed" in result.output


def test_the_command_is_registered_through_the_extension_seam() -> None:
    from brain import cli_registry

    assert ("ingest-bookmarks", cli_ingest_bookmarks.register) in (
        cli_registry.EXTENSION_REGISTRARS
    )
