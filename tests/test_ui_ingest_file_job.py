"""The file-upload ingest job against a real Postgres (Ingest spec §5.2, §9.2; RM-13).

A staged upload is claimed by ``submit_file`` and its job runs through
``run_next()`` on the test thread. The cases: every extension round-trips (with the
staged file deleted afterwards), the title is the user's filename (C6) unless
overridden, ``source_path`` is never recorded, an identical re-upload is a skip, a
whitespace-only ``.txt`` is ``empty`` without an embed call, an unreadable file is
``unreadable_file``, and the staged file is deleted on EVERY terminal path: success,
failure, a mid-embed cancel, a queued cancel and a ``stop()``.

All content is synthetic: the repo's ``tests/fixtures/sample.*`` files, or bytes here.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.ui.context import UiContext
from brain.ui.ingest_schemas import FileSpec
from brain.ui.ingest_service import FILE_KIND, submit_file
from brain.ui.jobs import JobRegistry, JobSnapshot
from brain.ui.routes_jobs import snapshot_payload
from brain.ui.upload_checks import sanitize_filename
from brain.ui.upload_staging import StagedUpload, UploadStaging
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


class _CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls += 1
        return super().embed(texts, input_type)


class _CancellingEmbedder(FakeEmbedder):
    """Cancels its own job from inside its first ``embed()`` call."""

    def __init__(self, registry: JobRegistry) -> None:
        super().__init__()
        self.registry = registry
        self.job_id = ""

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.registry.cancel(self.job_id)
        return super().embed(texts, input_type)


def _setup(
    conn: psycopg.Connection[Any], tmp_path: Path, *, embedder: Any = None, **kw: Any
) -> tuple[UiContext, UploadStaging, JobRegistry]:
    registry = make_registry()
    staging = UploadStaging(clock=lambda: datetime.now(UTC), parent=tmp_path)
    ctx = make_context(
        conn, make_vault(tmp_path), embedder=embedder or FakeEmbedder(), jobs=registry,
        uploads=staging, **kw,
    )
    return ctx, staging, registry


def _stage(staging: UploadStaging, header: str, data: bytes) -> StagedUpload:
    return asyncio.run(staging.stage(sanitize_filename(header), len(data), _one(data)))


def _spec(upload: StagedUpload, **overrides: Any) -> FileSpec:
    fields: dict[str, Any] = {
        "upload_id": upload.upload_id, "title": None, "tags": ("uploads",),
        "sensitivity": "normal", "enrich": False,
    }
    fields.update(overrides)
    return FileSpec(**fields)


def _submit(ctx: UiContext, staging: UploadStaging, spec: FileSpec) -> JobSnapshot:
    assert ctx.jobs is not None and ctx.ingest_runtime is not None
    return submit_file(ctx, ctx.jobs, ctx.ingest_runtime, staging, spec)


def _run(ctx: UiContext, staging: UploadStaging, spec: FileSpec) -> JobSnapshot:
    job = _submit(ctx, staging, spec)
    assert ctx.jobs is not None and ctx.jobs.run_next()
    return ctx.jobs.get(job.id)


def _doc_count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    ("name", "header"),
    [("sample.pdf", "Synthetic Paper.pdf"), ("sample.docx", "memo.docx"),
     ("sample.md", "notes.md"), ("sample.md", "notes.markdown"),
     ("sample.txt", "Plain Notes.txt")],
)
def test_each_extension_round_trips_and_the_staged_file_is_deleted(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path, name: str, header: str
) -> None:
    ctx, staging, _ = _setup(test_db, tmp_path)
    upload = _stage(staging, header, (fixtures_dir / name).read_bytes())

    snap = _run(ctx, staging, _spec(upload))

    assert snap.state == "succeeded", snap.error
    assert snap.kind == FILE_KIND
    result = dict(snap.result or {})
    assert result["outcome"] == "ingested"
    assert result["original_filename"] == upload.name.basename
    assert (ctx.cfg.vault_path / result["vault_path"]).is_file()
    row = test_db.execute(
        "SELECT title, source_path, metadata, tags, kind FROM documents WHERE id = %s",
        (result["document_id"],),
    ).fetchone()
    assert row is not None
    title, source_path, metadata, tags, kind = row
    assert source_path is None  # never the staging path (§5.2)
    assert metadata["ingested_via"] == "ui"
    assert metadata["original_filename"] == upload.name.basename
    assert tags == ["uploads"] and kind == "ingested"
    if upload.name.ext in (".pdf", ".txt"):
        assert title == upload.name.stem  # C6: the user's filename, not a temp name
    assert not upload.path.exists() and not upload.path.parent.exists()


def test_a_title_override_wins(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    ctx, staging, _ = _setup(test_db, tmp_path)
    upload = _stage(staging, "paper.pdf", (fixtures_dir / "sample.pdf").read_bytes())

    snap = _run(ctx, staging, _spec(upload, title="Chosen Title"))

    assert dict(snap.result or {})["title"] == "Chosen Title"


def test_an_identical_reupload_is_a_skip_on_the_same_document(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    ctx, staging, _ = _setup(test_db, tmp_path)
    data = (fixtures_dir / "sample.txt").read_bytes()
    first = _run(ctx, staging, _spec(_stage(staging, "a.txt", data)))
    count = _doc_count(test_db)

    second = _run(ctx, staging, _spec(_stage(staging, "a.txt", data)))

    assert dict(second.result or {})["outcome"] == "skipped"
    assert dict(second.result or {})["document_id"] == dict(first.result or {})["document_id"]
    assert _doc_count(test_db) == count


def test_a_whitespace_only_txt_is_empty_with_no_embed_call(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    embedder = _CountingEmbedder()
    ctx, staging, _ = _setup(test_db, tmp_path, embedder=embedder)
    upload = _stage(staging, "blank.txt", b"   \n\t\n  ")

    snap = _run(ctx, staging, _spec(upload))

    assert snap.state == "succeeded"
    assert dict(snap.result or {})["outcome"] == "empty"
    assert embedder.calls == 0
    assert not any(e.code == "cancel_too_late" for e in snap.events)
    assert _doc_count(test_db) == 0
    assert not upload.path.parent.exists()


def test_an_unreadable_file_fails_with_a_fixed_message_and_is_deleted(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx, staging, _ = _setup(test_db, tmp_path)
    upload = _stage(staging, "zorblax.pdf", b"%PDF-1.7 then nothing a parser can read")

    snap = _run(ctx, staging, _spec(upload))

    assert snap.state == "failed"
    assert snap.error is not None and snap.error.code == "unreadable_file"
    assert ".pdf" in snap.error.message and "zorblax" not in snap.error.message
    assert "Nothing was written" in snap.error.message
    assert _doc_count(test_db) == 0
    assert not upload.path.parent.exists()


def test_a_mid_embed_cancel_writes_nothing_and_deletes_the_file(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    registry = make_registry()
    embedder = _CancellingEmbedder(registry)
    staging = UploadStaging(clock=lambda: datetime.now(UTC), parent=tmp_path)
    ctx = make_context(
        test_db, make_vault(tmp_path), embedder=embedder, jobs=registry, uploads=staging
    )
    upload = _stage(staging, "notes.md", (fixtures_dir / "sample.md").read_bytes())
    job = _submit(ctx, staging, _spec(upload))
    embedder.job_id = job.id

    assert registry.run_next()

    assert registry.get(job.id).state == "cancelled"
    assert _doc_count(test_db) == 0
    assert not upload.path.parent.exists()


def test_a_queued_cancel_deletes_the_file_without_running(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    ctx, staging, registry = _setup(test_db, tmp_path)
    upload = _stage(staging, "notes.txt", (fixtures_dir / "sample.txt").read_bytes())
    job = _submit(ctx, staging, _spec(upload))
    assert upload.path.exists()

    registry.cancel(job.id)

    assert registry.get(job.id).state == "cancelled"
    assert not upload.path.parent.exists()
    assert staging.used_bytes() == 0
    assert registry.run_next() is False


def test_stop_deletes_the_files_of_queued_jobs(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    ctx, staging, registry = _setup(test_db, tmp_path)
    data = (fixtures_dir / "sample.txt").read_bytes()
    uploads = [_stage(staging, f"n{i}.txt", data + bytes([65 + i])) for i in range(2)]
    for upload in uploads:
        _submit(ctx, staging, _spec(upload))

    registry.stop(timeout=0.1)

    assert all(not u.path.parent.exists() for u in uploads)
    assert staging.used_bytes() == 0


def test_a_confidential_upload_withholds_its_filename_from_the_wire(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path
) -> None:
    ctx, staging, _ = _setup(test_db, tmp_path)
    upload = _stage(staging, "zorblax-private.txt", (fixtures_dir / "sample.txt").read_bytes())

    snap = _run(ctx, staging, _spec(upload, sensitivity="confidential"))
    hidden = snapshot_payload(snap, serve_confidential_titles=False)["result"]
    shown = snapshot_payload(snap, serve_confidential_titles=True)["result"]

    assert hidden["title_withheld"] is True
    for key in ("title", "vault_path", "document_id", "original_filename"):
        assert hidden[key] is None, key
    assert "zorblax" not in repr(hidden)
    assert shown["original_filename"] == "zorblax-private.txt"


def test_a_paste_result_carries_no_filename(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    from brain.ui.ingest_schemas import PasteSpec
    from brain.ui.ingest_service import submit_paste

    ctx, _, registry = _setup(test_db, tmp_path)
    assert ctx.ingest_runtime is not None
    job = submit_paste(ctx, registry, ctx.ingest_runtime, PasteSpec(
        title="Synthetic", body="Synthetic body text.", tags=(), content_type="note",
        sensitivity="normal", enrich=False,
    ))
    registry.run_next()

    assert dict(registry.get(job.id).result or {})["original_filename"] is None
