"""The Ingest tab's HTTP contract (Ingest spec §6): config, paste and the jobs routes.

Status codes, envelopes, and ``GET /api/ingest/config`` in each mode. Jobs are run
with ``run_next()`` on the test thread; the client is never entered as a context
manager, so the app lifespan never starts the worker here.
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.ui.app import create_app
from brain.ui.ingest_limits import (
    MAX_PASTE_BODY_BYTES,
    MAX_PASTE_WIRE_BYTES,
    MAX_QUEUED_JOBS,
    PASTE_CONTENT_TYPES,
)
from brain.ui.jobs import JobHandle, JobRegistry
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import ORIGIN, WRITE_HEADERS, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_PASTE = {"title": "Synthetic Paste", "body": "A short synthetic body.\n", "enrich": False}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return make_vault(tmp_path)


@pytest.fixture
def registry() -> JobRegistry:
    return make_registry()


def _client(
    conn: psycopg.Connection[Any], vault: Path, registry: JobRegistry | None, **kw: Any
) -> TestClient:
    ctx = make_context(conn, vault, embedder=FakeEmbedder(), jobs=registry, **kw)
    if registry is None:
        from dataclasses import replace

        ctx = replace(ctx, jobs=None)
    return TestClient(create_app(ctx), base_url=ORIGIN)


@pytest.fixture
def client(test_db: psycopg.Connection[Any], vault: Path, registry: JobRegistry) -> TestClient:
    return _client(test_db, vault, registry)


def _paste(client: TestClient, payload: Mapping[str, Any] | None = None) -> Any:
    return client.post("/api/ingest/paste", json=payload or _PASTE, headers=WRITE_HEADERS)


# ------------------------------------------------------------------ config --


def test_config_on_a_loopback_writable_server(client: TestClient) -> None:
    payload = client.get("/api/ingest/config").json()

    assert payload["enabled"] is True
    assert payload["reason"] is None
    assert payload["content_types"] == list(PASTE_CONTENT_TYPES)
    assert payload["sensitivity_levels"] == ["confidential", "normal"]
    assert payload["default_sensitivity"] == "normal"
    assert payload["limits"] == {
        "paste_body_bytes": MAX_PASTE_BODY_BYTES,
        "max_queued_jobs": MAX_QUEUED_JOBS,
    }
    assert payload["extensions"] == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"read_only": True}, "read_only"),
        ({"read_only": True, "ingest_enabled": False}, "read_only"),
        ({"ingest_enabled": False}, "requires_loopback"),
    ],
)
def test_config_names_why_ingest_is_off(
    test_db: psycopg.Connection[Any],
    vault: Path,
    registry: JobRegistry,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    client = _client(test_db, vault, registry, **overrides)

    payload = client.get("/api/ingest/config").json()
    health = client.get("/api/health").json()

    assert (payload["enabled"], payload["reason"]) == (False, reason)
    assert health["ingest_enabled"] is False


def test_config_without_a_registry_is_jobs_unavailable(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    client = _client(test_db, vault, None)

    assert client.get("/api/ingest/config").json()["reason"] == "jobs_unavailable"
    assert client.get("/api/health").json()["ingest_enabled"] is False


def test_health_reports_ingest_enabled(client: TestClient) -> None:
    assert client.get("/api/health").json()["ingest_enabled"] is True


# ------------------------------------------------------------------- paste --


def test_paste_queues_a_job_and_the_poll_returns_its_outcome(
    client: TestClient, registry: JobRegistry
) -> None:
    response = _paste(client)

    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["state"] == "queued"
    assert queued["kind"] == "ingest.paste"
    assert queued["cancellable"] is True
    assert queued["result"] is None
    assert [e["phase"] for e in queued["events"]] == ["queued"]
    assert registry.run_next()

    done = client.get(f"/api/jobs/{queued['id']}").json()
    assert done["state"] == "succeeded"
    assert done["result"]["outcome"] == "ingested"
    assert done["result"]["possible_duplicates"] == []
    assert done["cancellable"] is False


def test_the_poll_returns_only_later_events(client: TestClient, registry: JobRegistry) -> None:
    job_id = _paste(client).json()["id"]
    registry.run_next()

    events = client.get(f"/api/jobs/{job_id}?after=1").json()["events"]

    assert events and all(e["seq"] > 1 for e in events)
    assert set(events[0]) == {"seq", "at", "phase", "done", "total", "code"}


@pytest.mark.parametrize("after", ["-1", "x", "1.5"])
def test_a_bad_after_is_a_400(client: TestClient, after: str) -> None:
    job_id = _paste(client).json()["id"]

    response = client.get(f"/api/jobs/{job_id}?after={after}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_after"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({**_PASTE, "title": ""}, "invalid_title"),
        ({**_PASTE, "title": "x" * 201}, "invalid_title"),
        ({k: v for k, v in _PASTE.items() if k != "title"}, "invalid_title"),
        ({**_PASTE, "body": "   \n "}, "invalid_body"),
        ({**_PASTE, "body": 7}, "invalid_body"),
        ({**_PASTE, "content_type": "krisp_action_items"}, "invalid_content_type"),
        ({**_PASTE, "sensitivity": "secret"}, "invalid_sensitivity"),
        ({**_PASTE, "sensitivity": 1}, "invalid_sensitivity"),
        ({**_PASTE, "tags": "vendors"}, "invalid_tags"),
        ({**_PASTE, "tags": [1]}, "invalid_tags"),
        ({**_PASTE, "enrich": "yes"}, "invalid_enrich"),
        ({**_PASTE, "force": True}, "unknown_field"),
        ({**_PASTE, "allow_secrets": True}, "unknown_field"),
    ],
)
def test_a_bad_paste_is_a_400_and_nothing_is_queued(
    client: TestClient, registry: JobRegistry, payload: dict[str, Any], code: str
) -> None:
    response = _paste(client, payload)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert registry.list_recent() == ()


def test_a_200_character_title_is_accepted(client: TestClient) -> None:
    assert _paste(client, {**_PASTE, "title": "t" * 200}).status_code == 202


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_a_malformed_paste_body_is_a_400(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/api/ingest/paste",
        content=body,
        headers={**WRITE_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"invalid_json", "invalid_body"}


def test_the_decoded_body_cap_is_two_mebibytes(client: TestClient) -> None:
    at_cap = _paste(client, {**_PASTE, "body": "a" * MAX_PASTE_BODY_BYTES})
    over = _paste(client, {**_PASTE, "body": "a" * (MAX_PASTE_BODY_BYTES + 1)})

    assert at_cap.status_code == 202
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "body_too_large"


def test_a_heavily_escaped_body_under_the_cap_passes_the_wire_cap(client: TestClient) -> None:
    """Newlines escape to two bytes on the wire; the wire cap must allow it."""
    body = "x\n" * (MAX_PASTE_BODY_BYTES // 2)

    assert _paste(client, {**_PASTE, "body": body}).status_code == 202


def test_an_honest_oversize_content_length_is_refused_before_reading(
    client: TestClient,
) -> None:
    raw = json.dumps({**_PASTE, "body": "a"}).encode() + b" " * MAX_PASTE_WIRE_BYTES

    response = client.post(
        "/api/ingest/paste",
        content=raw,
        headers={**WRITE_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_a_chunked_oversize_body_is_refused_on_the_stream(client: TestClient) -> None:
    def pieces() -> Iterator[bytes]:
        yield b'{"title": "t", "body": "'
        for _ in range(MAX_PASTE_WIRE_BYTES // (1024 * 1024) + 2):
            yield b"a" * (1024 * 1024)
        yield b'"}'

    response = client.post(
        "/api/ingest/paste",
        content=pieces(),
        headers={**WRITE_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_a_fifth_queued_paste_is_a_429(client: TestClient) -> None:
    for i in range(MAX_QUEUED_JOBS):
        assert _paste(client, {**_PASTE, "body": f"body {i}"}).status_code == 202

    response = _paste(client)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_full"


def test_a_paste_during_shutdown_is_a_503(client: TestClient, registry: JobRegistry) -> None:
    registry.stop(timeout=1.0)

    response = _paste(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "shutting_down"


def test_without_a_registry_ingest_routes_are_503(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    client = _client(test_db, vault, None)

    for response in (
        _paste(client),
        client.get("/api/jobs"),
        client.get(f"/api/jobs/{'a' * 32}"),
        client.post(f"/api/jobs/{'a' * 32}/cancel", json={}, headers=WRITE_HEADERS),
    ):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "jobs_unavailable"


# -------------------------------------------------------------------- jobs --


def test_the_job_list_carries_no_results_or_events(
    client: TestClient, registry: JobRegistry
) -> None:
    first = _paste(client).json()["id"]
    registry.run_next()
    second = _paste(client, {**_PASTE, "body": "another body"}).json()["id"]

    jobs = client.get("/api/jobs").json()["jobs"]

    assert [j["id"] for j in jobs] == [second, first]
    assert all("result" not in j and "events" not in j for j in jobs)
    assert jobs[1]["state"] == "succeeded"


def test_an_unknown_job_is_job_unknown(client: TestClient) -> None:
    response = client.get(f"/api/jobs/{'0' * 32}")
    cancel = client.post(f"/api/jobs/{'0' * 32}/cancel", json={}, headers=WRITE_HEADERS)

    assert (response.status_code, cancel.status_code) == (404, 404)
    assert response.json()["error"]["code"] == "job_unknown"
    assert "Re-submitting is safe" in response.json()["error"]["message"]


def test_cancel_a_queued_job_is_200_cancelled(client: TestClient, registry: JobRegistry) -> None:
    job_id = _paste(client).json()["id"]

    response = client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=WRITE_HEADERS)

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert registry.run_next() is False


def test_cancel_a_running_job_is_202_then_cancelled(
    client: TestClient, registry: JobRegistry
) -> None:
    seen: list[Any] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        seen.append(
            client.post(f"/api/jobs/{handle.job_id}/cancel", json={}, headers=WRITE_HEADERS)
        )
        handle.check_cancelled()
        return {}

    job = registry.submit("test.kind", runner)
    registry.run_next()

    assert seen[0].status_code == 202
    assert seen[0].json()["cancel_requested"] is True
    assert client.get(f"/api/jobs/{job.id}").json()["state"] == "cancelled"


def test_cancel_after_enter_commit_is_409(client: TestClient, registry: JobRegistry) -> None:
    seen: list[Any] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        handle.enter_commit()
        seen.append(
            client.post(f"/api/jobs/{handle.job_id}/cancel", json={}, headers=WRITE_HEADERS)
        )
        return {}

    registry.submit("test.kind", runner)
    registry.run_next()

    assert seen[0].status_code == 409
    assert seen[0].json()["error"]["code"] == "not_cancellable"


def test_cancel_a_finished_job_is_200_unchanged(
    client: TestClient, registry: JobRegistry
) -> None:
    job_id = _paste(client).json()["id"]
    registry.run_next()
    before = client.get(f"/api/jobs/{job_id}").json()

    response = client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=WRITE_HEADERS)

    assert response.status_code == 200
    assert response.json() == before


def test_a_failed_job_carries_its_fixed_error(client: TestClient, registry: JobRegistry) -> None:
    def runner(handle: JobHandle) -> Mapping[str, Any]:
        raise RuntimeError("synthetic document text")

    job = registry.submit("test.kind", runner)
    registry.run_next()

    payload = client.get(f"/api/jobs/{job.id}").json()
    assert payload["state"] == "failed"
    assert payload["error"]["code"] == "internal_error"
    assert "synthetic document text" not in json.dumps(payload)


def test_without_a_runtime_ingest_is_jobs_unavailable(
    test_db: psycopg.Connection[Any], vault: Path, registry: JobRegistry
) -> None:
    from dataclasses import replace

    ctx = replace(
        make_context(test_db, vault, embedder=FakeEmbedder(), jobs=registry),
        ingest_runtime=None,
    )
    client = TestClient(create_app(ctx), base_url=ORIGIN)

    response = _paste(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "jobs_unavailable"
    assert client.get("/api/ingest/config").json()["reason"] == "jobs_unavailable"
    assert registry.list_recent() == ()


@pytest.mark.nodb
def test_an_honest_content_length_over_the_cap_is_refused_without_reading() -> None:
    """The precheck runs BEFORE the stream is touched (Ingest spec §7.3).

    Through ``TestClient`` the stream counter alone would also answer 413, so the
    precheck is pinned here directly: ``receive`` fails the test if it is called.
    """
    import asyncio

    from starlette.requests import Request

    from brain.ui._http import read_body_capped
    from brain.ui.errors import UiPayloadTooLarge

    async def receive() -> dict[str, Any]:
        raise AssertionError("the body was read")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ingest/paste",
        "headers": [(b"content-length", str(MAX_PASTE_WIRE_BYTES + 1).encode())],
    }

    with pytest.raises(UiPayloadTooLarge):
        asyncio.run(read_body_capped(Request(scope, receive), MAX_PASTE_WIRE_BYTES))
