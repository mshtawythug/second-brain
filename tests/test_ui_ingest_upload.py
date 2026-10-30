"""The upload routes over HTTP (Ingest spec §5.2, §6, §7.3, §9.2 upload rows; RM-13).

``POST /api/ingest/uploads`` (raw body), ``DELETE /api/ingest/uploads/{id}`` and
``POST /api/ingest/file``, through ``create_app`` against the test DB. The staging
root lives under ``tmp_path``; jobs are driven with ``run_next()``. The job itself
(each extension, cancel, failure, cleanup) is ``tests/test_ui_ingest_file_job.py``.

All content is synthetic.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.ingest.dispatch import supported_extensions
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.ingest_limits import (
    MAX_BINARY_UPLOAD_BYTES,
    MAX_STAGED_BYTES,
    MAX_TEXT_UPLOAD_BYTES,
    STAGING_BLOCK_BYTES,
    UPLOAD_EXTENSIONS,
    UPLOAD_TTL,
)
from brain.ui.jobs import JobRegistry
from brain.ui.upload_staging import UploadStaging
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import ORIGIN, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TEXT = b"Synthetic upload body.\n\nA second paragraph about quarkles.\n"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _ctx(
    conn: psycopg.Connection[Any],
    tmp_path: Path,
    *,
    registry: JobRegistry | None = None,
    staging: UploadStaging | None = None,
    **kw: Any,
) -> UiContext:
    staging = staging if staging is not None else UploadStaging(
        clock=_Clock(), parent=tmp_path / "tmp"
    )
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return make_context(
        conn, make_vault(tmp_path), embedder=FakeEmbedder(),
        jobs=registry if registry is not None else make_registry(), uploads=staging, **kw,
    )


def _client(ctx: UiContext) -> TestClient:
    return TestClient(create_app(ctx), base_url=ORIGIN)


def _upload(
    client: TestClient, name: str, data: Any, *, headers: dict[str, str] | None = None
) -> Any:
    sent = {
        "Origin": ORIGIN,
        "Content-Type": "application/octet-stream",
        "X-Brain-Filename": quote(name),
    }
    sent.update(headers or {})
    return client.post("/api/ingest/uploads", content=data, headers=sent)


def _submit(client: TestClient, upload_id: str, **fields: Any) -> Any:
    return client.post(
        "/api/ingest/file",
        json={"upload_id": upload_id, "enrich": False, **fields},
        headers={"Origin": ORIGIN},
    )


def _staged(ctx: UiContext) -> list[Path]:
    assert ctx.uploads is not None
    root = ctx.uploads._root
    return sorted(p for p in root.rglob("*") if p.is_file()) if root else []


def _chunked(data: bytes, step: int = 64 * 1024) -> Iterator[bytes]:
    """A generator body: httpx sends it with no Content-Length."""
    for i in range(0, len(data), step):
        yield data[i : i + step]


# --- POST /api/ingest/uploads ------------------------------------------------


def test_a_text_upload_is_staged_and_answered_201(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), "notes.txt", _TEXT)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"upload_id", "size", "kind"}
    assert body["size"] == len(_TEXT) and body["kind"] == "text"
    assert [p.name for p in _staged(ctx)] == ["notes.txt"]


@pytest.mark.parametrize(
    ("name", "kind"),
    [("sample.pdf", "pdf"), ("sample.docx", "docx"), ("sample.md", "markdown"),
     ("sample.txt", "text")],
)
def test_every_fixture_type_stages(
    test_db: psycopg.Connection[Any], tmp_path: Path, fixtures_dir: Path, name: str, kind: str
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), name, (fixtures_dir / name).read_bytes())

    assert response.status_code == 201, response.json()
    assert response.json()["kind"] == kind


def test_a_traversal_filename_lands_inside_the_staging_root(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    response = _upload(_client(ctx), "../../../escape.txt", _TEXT)

    assert response.status_code == 201
    new = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert ctx.uploads is not None and ctx.uploads._root is not None
    assert [p.name for p in new] == ["escape.txt"]
    assert all(ctx.uploads._root in p.parents for p in new)


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"X-Brain-Filename": ""}, 400, "bad_filename"),
        ({"X-Brain-Filename": "..%2F"}, 400, "bad_filename"),
        ({"X-Brain-Filename": "archive.zip"}, 415, "unsupported_file"),
        ({"X-Brain-Filename": "shot.png"}, 415, "unsupported_file"),
    ],
)
def test_a_bad_name_is_refused_before_anything_is_staged(
    test_db: psycopg.Connection[Any], tmp_path: Path,
    headers: dict[str, str], status: int, code: str,
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), "x", _TEXT, headers=headers)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert _staged(ctx) == []


def test_a_missing_filename_header_is_bad_filename(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)
    response = _client(ctx).post(
        "/api/ingest/uploads", content=_TEXT,
        headers={"Origin": ORIGIN, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_filename"


@pytest.mark.parametrize(
    ("name", "data"),
    [("paper.pdf", b"plain text, not a pdf"), ("memo.docx", b"%PDF-1.7"),
     ("notes.txt", b"\x00\x01binary"), ("notes.md", b"caf\xe9")],
)
def test_a_sniff_mismatch_is_415_and_leaves_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, name: str, data: bytes
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), name, data)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file"
    assert _staged(ctx) == []


@pytest.mark.parametrize(
    ("name", "cap"), [("big.txt", MAX_TEXT_UPLOAD_BYTES), ("big.pdf", MAX_BINARY_UPLOAD_BYTES)]
)
def test_an_honest_length_over_the_cap_is_413(
    test_db: psycopg.Connection[Any], tmp_path: Path, name: str, cap: int
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), name, b"%PDF-" + b"x" * cap)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert _staged(ctx) == []


def test_a_chunked_body_over_the_cap_is_413_mid_stream(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), "big.txt", _chunked(b"x" * (MAX_TEXT_UPLOAD_BYTES + 1)))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert _staged(ctx) == []
    assert ctx.uploads is not None and ctx.uploads.used_bytes() == 0


def test_a_huge_declared_length_is_413_not_a_crash(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), "x.txt", _TEXT, headers={"Content-Length": "9" * 5000})

    assert response.status_code == 413


def test_the_staging_budget_is_429(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    staging = UploadStaging(clock=_Clock(), parent=tmp_path, budget=STAGING_BLOCK_BYTES)
    ctx = _ctx(test_db, tmp_path, staging=staging)
    client = _client(ctx)
    assert _upload(client, "a.txt", _TEXT).status_code == 201

    response = _upload(client, "b.txt", _TEXT)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "staging_full"
    assert len(_staged(ctx)) == 1


def test_the_default_budget_is_64_mib() -> None:
    assert MAX_STAGED_BYTES == 64 * 1024 * 1024
    assert timedelta(minutes=15) == UPLOAD_TTL


def test_without_a_staging_area_the_upload_routes_are_503(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = make_context(test_db, make_vault(tmp_path), embedder=FakeEmbedder())
    client = _client(ctx)

    assert _upload(client, "a.txt", _TEXT).status_code == 503
    assert _submit(client, "a" * 32).status_code == 503
    deleted = client.request(
        "DELETE", f"/api/ingest/uploads/{'a' * 32}", json={}, headers={"Origin": ORIGIN}
    )
    assert deleted.status_code == 503
    assert client.get("/api/ingest/config").json()["extensions"] == []


# --- DELETE /api/ingest/uploads/{id} --------------------------------------------


def test_delete_discards_a_staged_upload_with_a_json_answer(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)
    client = _client(ctx)
    upload_id = _upload(client, "a.txt", _TEXT).json()["upload_id"]

    response = client.request(
        "DELETE", f"/api/ingest/uploads/{upload_id}", json={}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 200
    assert response.json() == {"upload_id": upload_id, "deleted": True}
    assert _staged(ctx) == []
    again = client.request(
        "DELETE", f"/api/ingest/uploads/{upload_id}", json={}, headers={"Origin": ORIGIN}
    )
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "upload_unknown"


@pytest.mark.parametrize("upload_id", ["a" * 32, "nothex", "a" * 31, "A" * 32])
def test_delete_of_an_unknown_id_is_404(
    test_db: psycopg.Connection[Any], tmp_path: Path, upload_id: str
) -> None:
    client = _client(_ctx(test_db, tmp_path))

    response = client.request(
        "DELETE", f"/api/ingest/uploads/{upload_id}", json={}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "upload_unknown"


# --- POST /api/ingest/file -------------------------------------------------------


def test_submit_queues_a_file_job_and_the_upload_cannot_be_reused(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry()
    ctx = _ctx(test_db, tmp_path, registry=registry)
    client = _client(ctx)
    upload_id = _upload(client, "notes.txt", _TEXT).json()["upload_id"]

    response = _submit(client, upload_id, tags=["Vendors"])

    assert response.status_code == 202
    assert response.json()["kind"] == "ingest.file"
    assert response.json()["state"] == "queued"
    for again in (
        _submit(client, upload_id),
        client.request("DELETE", f"/api/ingest/uploads/{upload_id}", json={},
                       headers={"Origin": ORIGIN}),
    ):
        assert again.status_code == 404
    assert registry.run_next()
    assert registry.get(response.json()["id"]).state == "succeeded"
    assert _staged(ctx) == []


def test_submit_of_an_unknown_upload_is_404(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    response = _submit(_client(_ctx(test_db, tmp_path)), "a" * 32)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "upload_unknown"


def test_an_expired_upload_is_404_and_deleted(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    clock = _Clock()
    staging = UploadStaging(clock=clock, parent=tmp_path)
    ctx = _ctx(test_db, tmp_path, staging=staging)
    client = _client(ctx)
    upload_id = _upload(client, "a.txt", _TEXT).json()["upload_id"]
    clock.now += UPLOAD_TTL + timedelta(seconds=1)

    response = _submit(client, upload_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "upload_unknown"
    assert _staged(ctx) == []


def test_a_full_queue_is_429_and_the_upload_survives_for_a_retry(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry(max_queued=1)
    ctx = _ctx(test_db, tmp_path, registry=registry)
    client = _client(ctx)
    registry.submit("test.kind", lambda handle: {})
    upload_id = _upload(client, "a.txt", _TEXT).json()["upload_id"]

    refused = _submit(client, upload_id)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "queue_full"
    assert len(_staged(ctx)) == 1
    assert registry.run_next()
    assert _submit(client, upload_id).status_code == 202


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "invalid_upload_id"),
        ({"upload_id": 7}, "invalid_upload_id"),
        ({"upload_id": "a" * 32, "force": True}, "unknown_field"),
        ({"upload_id": "a" * 32, "content_type": "note"}, "unknown_field"),
        ({"upload_id": "a" * 32, "sensitivity": "secret"}, "invalid_sensitivity"),
        ({"upload_id": "a" * 32, "title": "x" * 201}, "invalid_title"),
        ({"upload_id": "a" * 32, "enrich": "yes"}, "invalid_enrich"),
    ],
)
def test_submit_validation_is_400(
    test_db: psycopg.Connection[Any], tmp_path: Path, body: dict[str, Any], code: str
) -> None:
    client = _client(_ctx(test_db, tmp_path))

    response = client.post("/api/ingest/file", json=body, headers={"Origin": ORIGIN})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_submit_json_is_capped_on_the_wire(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    client = _client(_ctx(test_db, tmp_path))

    response = client.post(
        "/api/ingest/file", json={"upload_id": "a" * 32, "tags": ["x" * 20_000]},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 413


# --- GET /api/ingest/config, the lifespan, logging -----------------------------------


def test_config_offers_the_uploadable_extensions_and_their_caps(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    config = _client(_ctx(test_db, tmp_path)).get("/api/ingest/config").json()

    assert config["extensions"] == [e for e in supported_extensions() if e in UPLOAD_EXTENSIONS]
    assert set(config["extensions"]) == UPLOAD_EXTENSIONS
    assert config["limits"]["upload_bytes"] == {
        ".txt": MAX_TEXT_UPLOAD_BYTES, ".md": MAX_TEXT_UPLOAD_BYTES,
        ".markdown": MAX_TEXT_UPLOAD_BYTES, ".pdf": MAX_BINARY_UPLOAD_BYTES,
        ".docx": MAX_BINARY_UPLOAD_BYTES,
    }
    assert config["limits"]["staged_bytes"] == MAX_STAGED_BYTES


def test_the_lifespan_sweeps_dead_roots_and_removes_its_own(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    parent = tmp_path / "tmp"
    parent.mkdir()
    dead = parent / f"brain-ui-ingest-{2**22 - 1}-old"
    (dead / "u").mkdir(parents=True)
    staging = UploadStaging(clock=_Clock(), parent=parent)
    ctx = _ctx(test_db, tmp_path, staging=staging)

    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        assert not dead.exists()
        assert _upload(client, "a.txt", _TEXT).status_code == 201
        assert staging._root is not None and staging._root.exists()
        root = staging._root

    assert not root.exists()
    assert list(parent.iterdir()) == []


def test_no_log_line_carries_the_filename(
    test_db: psycopg.Connection[Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = make_registry()
    ctx = _ctx(test_db, tmp_path, registry=registry)
    client = _client(ctx)
    with caplog.at_level(logging.DEBUG):
        upload_id = _upload(client, "zorblax-quarterly.txt", _TEXT).json()["upload_id"]
        _upload(client, "zorblax-refused.pdf", b"not a pdf")
        _submit(client, upload_id)
        registry.run_next()

    assert "zorblax" not in caplog.text
    assert upload_id in caplog.text


# --- sec-77 (PR #77 security review) regressions over HTTP ------------------------


def test_a_long_multibyte_filename_stages_instead_of_a_500(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)

    response = _upload(_client(ctx), "😀" * 120 + ".txt", _TEXT)

    assert response.status_code == 201, response.json()
    (staged,) = _staged(ctx)
    assert len(staged.name.encode("utf-8")) <= 200


def test_a_filesystem_refusal_is_503_and_no_log_names_the_file(
    test_db: psycopg.Connection[Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The unhandled-error handler logs a traceback; a staging OSError must never get
    there, or its message would print the staged path (sec-77 #2)."""
    ctx = _ctx(test_db, tmp_path)
    assert ctx.uploads is not None
    root = ctx.uploads.root
    root.chmod(0o500)
    try:
        with caplog.at_level(logging.DEBUG):
            response = _upload(_client(ctx), "zorblax-private.txt", _TEXT)
    finally:
        root.chmod(0o700)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "staging_failed"
    assert "zorblax" not in caplog.text and "zorblax" not in response.text
    assert "Traceback" not in caplog.text


def test_too_many_staged_uploads_is_429(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    staging = UploadStaging(clock=_Clock(), parent=tmp_path, max_uploads=2)
    ctx = _ctx(test_db, tmp_path, staging=staging)
    client = _client(ctx)
    for name in ("a.txt", "b.txt"):
        assert _upload(client, name, _TEXT).status_code == 201

    response = _upload(client, "c.txt", _TEXT)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_uploads"


def test_a_failing_startup_sweep_does_not_stop_the_server(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    def exploding(*args: object, **kwargs: object) -> list[Path]:
        raise OverflowError("synthetic")

    staging = UploadStaging(clock=_Clock(), parent=tmp_path, sweeper=exploding)
    ctx = _ctx(test_db, tmp_path, staging=staging)

    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 200
        assert _upload(client, "a.txt", _TEXT).status_code == 201


def test_a_planted_huge_pid_root_does_not_stop_the_server(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The real sweep, end to end through the lifespan (sec-77 #1)."""
    parent = tmp_path / "tmp"
    parent.mkdir()
    (parent / "brain-ui-ingest-9999999999-x").mkdir()
    staging = UploadStaging(clock=_Clock(), parent=parent)
    ctx = _ctx(test_db, tmp_path, staging=staging)

    with TestClient(create_app(ctx), base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 200

    assert (parent / "brain-ui-ingest-9999999999-x").exists()
