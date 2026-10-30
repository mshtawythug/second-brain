"""Who may ingest, and how (Ingest spec §7.1, §7.2, §9.3; paste rows, and RM-13's upload rows).

A sibling of ``tests/test_ui_security.py`` (already near the file-size ceiling).
Mutation routes are ENUMERATED from ``app.routes`` rather than listed by hand, so
a route added later is covered the moment it exists.

- Cross-origin, missing-Origin and ``Sec-Fetch-Site: cross-site`` mutations → 403.
- ``--read-only`` → 403 on every ingest/jobs mutation; the GETs answer.
- Non-loopback → 403 ``ingest_requires_loopback`` on every ingest/jobs mutation,
  even with a valid token; ``config`` and the job GETs still answer.
- The GET-cannot-mutate table, extended with the new routes.
- The ONE Content-Type exception (§7.2): over EVERY mutating route in the app,
  read from ``app.routes``, ``application/octet-stream`` is 415 except on
  ``POST /api/ingest/uploads``, where JSON is 415.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from brain.ui.app import create_app
from brain.ui.jobs import JobRegistry
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import ORIGIN, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_PREFIXES = ("/api/ingest/", "/api/jobs")
_JOB_ID = "a" * 32
_UPLOADS = "/api/ingest/uploads"
_BODIES: dict[str, dict[str, Any]] = {
    "/api/ingest/paste": {"title": "Synthetic", "body": "Synthetic body.", "enrich": False},
    "/api/ingest/file": {"upload_id": _JOB_ID, "enrich": False},
}
_OCTET = {"Content-Type": "application/octet-stream", "X-Brain-Filename": "synthetic.txt"}


def _client(
    conn: psycopg.Connection[Any], tmp_path: Path, registry: JobRegistry, **kw: Any
) -> TestClient:
    ctx = make_context(conn, make_vault(tmp_path), embedder=FakeEmbedder(), jobs=registry, **kw)
    return TestClient(create_app(ctx), base_url=ORIGIN)


def _ingest_routes(client: TestClient, *, mutating: bool) -> list[tuple[str, str]]:
    """``(method, path)`` for every ingest/jobs route on the REAL app."""
    found = []
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, Route) or not route.path.startswith(_PREFIXES):
            continue
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            if (method != "GET") is mutating:
                found.append((method, route.path))
    return found


def _url(path: str) -> str:
    for param in ("{job_id}", "{upload_id}", "{id_prefix}", "{tag}"):
        path = path.replace(param, _JOB_ID)
    return path


def _send(client: TestClient, method: str, path: str, headers: dict[str, str]) -> Any:
    """A WELL-FORMED request for the route: raw bytes for the upload, JSON elsewhere."""
    if (method, path) == ("POST", _UPLOADS):
        return client.request(method, path, content=b"Synthetic.", headers={**_OCTET, **headers})
    return client.request(method, _url(path), json=_BODIES.get(path, {}), headers=headers)


def test_the_enumeration_finds_the_mutation_routes(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Anti-vacuity: the parametrized checks below iterate this list."""
    client = _client(test_db, tmp_path, make_registry())

    assert _ingest_routes(client, mutating=True) == [
        ("POST", "/api/ingest/paste"),
        ("POST", "/api/ingest/uploads"),
        ("DELETE", "/api/ingest/uploads/{upload_id}"),
        ("POST", "/api/ingest/file"),
        ("POST", "/api/jobs/{job_id}/cancel"),
    ]
    assert len(_ingest_routes(client, mutating=False)) == 3


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Origin": "http://evil.example"}, "origin_mismatch"),
        ({}, "origin_missing"),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, "cross_site"),
    ],
)
def test_cross_site_mutations_are_refused(
    test_db: psycopg.Connection[Any], tmp_path: Path, headers: dict[str, str], code: str
) -> None:
    registry = make_registry()
    client = _client(test_db, tmp_path, registry)

    for method, path in _ingest_routes(client, mutating=True):
        response = _send(client, method, path, headers)
        assert response.status_code == 403, (method, path)
        assert response.json()["error"]["code"] == code
    assert registry.list_recent() == ()


def test_a_foreign_host_is_refused(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    client = _client(test_db, tmp_path, make_registry())

    for method, path in _ingest_routes(client, mutating=True):
        response = _send(client, method, path, {"Origin": ORIGIN, "Host": "evil.example"})
        assert response.status_code == 400, (method, path)


def test_a_non_json_paste_is_415(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    client = _client(test_db, tmp_path, make_registry())

    for content_type in ("application/octet-stream", "multipart/form-data", "text/plain"):
        response = client.post(
            "/api/ingest/paste",
            content=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": content_type},
        )
        assert response.status_code == 415, content_type


def test_read_only_refuses_every_ingest_mutation_and_allows_the_gets(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry()
    client = _client(test_db, tmp_path, registry, read_only=True, ingest_enabled=False)

    for method, path in _ingest_routes(client, mutating=True):
        response = _send(client, method, path, {"Origin": ORIGIN})
        assert response.status_code == 403, (method, path)
        assert response.json()["error"]["code"] == "read_only"
    assert client.get("/api/ingest/config").status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_non_loopback_refuses_every_ingest_mutation_even_with_a_token(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry()
    client = _client(test_db, tmp_path, registry, ingest_enabled=False, token="tok-123")
    headers = {"Origin": ORIGIN, "X-Brain-UI-Token": "tok-123"}

    for method, path in _ingest_routes(client, mutating=True):
        response = _send(client, method, path, headers)
        assert response.status_code == 403, (method, path)
        assert response.json()["error"]["code"] == "ingest_requires_loopback"
    assert registry.list_recent() == ()

    config = client.get("/api/ingest/config", headers=headers)
    assert config.status_code == 200
    assert config.json()["reason"] == "requires_loopback"
    assert client.get("/api/jobs", headers=headers).status_code == 200
    assert client.get(f"/api/jobs/{_JOB_ID}", headers=headers).status_code == 404


def test_a_token_server_refuses_ingest_without_the_token(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    client = _client(test_db, tmp_path, make_registry(), token="tok-123")

    for method, path in _ingest_routes(client, mutating=True):
        response = _send(client, method, path, {"Origin": ORIGIN})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_token"


def test_the_ingest_gets_reject_mutating_methods(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    client = _client(test_db, tmp_path, make_registry())

    for _method, path in _ingest_routes(client, mutating=False):
        for verb in ("POST", "PUT", "DELETE"):
            if (verb, path) in _ingest_routes(client, mutating=True):
                continue
            response = client.request(verb, _url(path), json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 405, (verb, path)


@pytest.mark.parametrize(
    "path",
    ["/api/ingest/paste", _UPLOADS, "/api/ingest/file", f"/api/jobs/{_JOB_ID}/cancel"],
)
def test_the_ingest_mutations_reject_get(
    test_db: psycopg.Connection[Any], tmp_path: Path, path: str
) -> None:
    client = _client(test_db, tmp_path, make_registry())

    assert client.get(path).status_code == 405


def test_the_gets_mutate_nothing(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    """A GET is a pure in-memory or config read: the registry is unchanged by one."""
    registry = make_registry()
    client = _client(test_db, tmp_path, registry)
    job = registry.submit("test.kind", lambda handle: {})

    for _method, path in _ingest_routes(client, mutating=False):
        client.get(path.replace("{job_id}", job.id))

    assert registry.get(job.id).state == "queued"
    assert len(registry.list_recent()) == 1


# --- the one Content-Type exception (§7.2) ---------------------------------------


def _all_mutations(client: TestClient) -> list[tuple[str, str]]:
    """Every non-safe ``(method, path)`` in the app, read from ``app.routes``."""
    found = []
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, Route):
            continue
        for method in sorted(route.methods or ()):
            if method not in ("GET", "HEAD", "OPTIONS"):
                found.append((method, route.path))
    return found


def test_the_content_type_matrix_covers_every_mutating_route(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Anti-vacuity for the matrix below: notes, ingest and jobs are all in it."""
    mutations = _all_mutations(_client(test_db, tmp_path, make_registry()))

    assert ("POST", _UPLOADS) in mutations
    assert ("POST", "/api/notes") in mutations
    assert ("PUT", "/api/notes/{id_prefix}") in mutations
    assert ("DELETE", "/api/ingest/uploads/{upload_id}") in mutations
    assert len(mutations) >= 10


@pytest.mark.parametrize(
    "media_type",
    ["application/octet-stream", "application/octet-stream; charset=binary",
     "APPLICATION/OCTET-STREAM"],
)
def test_octet_stream_is_415_on_every_route_but_the_upload(
    test_db: psycopg.Connection[Any], tmp_path: Path, media_type: str
) -> None:
    registry = make_registry()
    client = _client(test_db, tmp_path, registry)

    for method, path in _all_mutations(client):
        response = client.request(
            method, _url(path), content=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": media_type,
                     "X-Brain-Filename": "synthetic.txt"},
        )
        if (method, path) == ("POST", _UPLOADS):
            # Past the media-type gate: the route answers (here, 503: no staging area).
            assert response.status_code != 415, (method, path)
        else:
            assert response.status_code == 415, (method, path)
            assert response.json()["error"]["code"] == "unsupported_media_type"
    assert registry.list_recent() == ()


@pytest.mark.parametrize(
    "media_type", ["application/json", "multipart/form-data; boundary=x", "text/plain",
                   "application/x-www-form-urlencoded", ""],
)
def test_only_octet_stream_reaches_the_upload_route(
    test_db: psycopg.Connection[Any], tmp_path: Path, media_type: str
) -> None:
    client = _client(test_db, tmp_path, make_registry())
    headers = {"Origin": ORIGIN, "X-Brain-Filename": "synthetic.txt"}
    if media_type:
        headers["Content-Type"] = media_type

    response = client.post(_UPLOADS, content=b"{}", headers=headers)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("path", [_UPLOADS + "/", "/api/ingest/uploadsX", "/api/ingest/upload",
                                  "/api/ingest/uploads/" + _JOB_ID])
def test_the_exception_is_a_literal_path_not_a_prefix(
    test_db: psycopg.Connection[Any], tmp_path: Path, path: str
) -> None:
    client = _client(test_db, tmp_path, make_registry())

    for method in ("POST", "PUT", "DELETE"):
        response = client.request(
            method, path, content=b"x", headers={"Origin": ORIGIN, **_OCTET},
            follow_redirects=False,
        )
        assert response.status_code == 415, (method, path)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_the_exception_is_post_only(
    test_db: psycopg.Connection[Any], tmp_path: Path, method: str
) -> None:
    client = _client(test_db, tmp_path, make_registry())

    response = client.request(method, _UPLOADS, content=b"x", headers={"Origin": ORIGIN, **_OCTET})

    assert response.status_code == 415
