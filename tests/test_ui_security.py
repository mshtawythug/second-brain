"""The security properties of `brain ui`, proven against a real ASGI app.

These tests deliberately exercise ``create_app`` end-to-end through
``TestClient`` rather than calling the middleware classes directly. A green unit
test on ``RequestGuardMiddleware._check`` would prove the helper works while
saying nothing about whether it is *wired into the app* — and "the guard exists
but is not installed" is precisely the failure worth catching.

No monkey-patching: ``UiContext`` is the injection seam, so a read-only server,
a token-protected server, and a fake database are all just different contexts.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.security import CSP, is_loopback

# starlette 1.3.1 + httpx 0.28 emit a deprecation warning from TestClient
# itself. Scoped to this module rather than added to pyproject's global
# filterwarnings, because pyproject is not this feature's file to edit.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"


class _FakeConn:
    """A connection that fails loudly if a security test ever reaches SQL."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("a security test must not reach the database")


def _context(tmp_path: Path, **overrides: Any) -> UiContext:
    """Build a context with no live dependencies."""

    class _Cfg:
        vault_path = tmp_path
        database_url = "postgresql://user:pw@localhost:5432/nowhere"
        embedder = "none"
        vector_sim_floor = 0.25
        recency_halflife_days = 180.0
        snippet_context_tokens = 0
        owner_participants: frozenset[str] = frozenset()
        # Read by ``routes_meta.health``; ``None`` is the real default
        # (``Config.user_email``, unset ``BRAIN_USER_EMAIL``).
        user_email: str | None = None

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield _FakeConn()

    defaults: dict[str, Any] = {
        "cfg": _Cfg(),
        "conn_factory": conn_factory,
        "embedder": object(),
        "search_fn": lambda *a, **k: [],
        "allowed_origin": ORIGIN,
    }
    defaults.update(overrides)
    return UiContext(**defaults)


def _client(context: UiContext) -> TestClient:
    return TestClient(create_app(context), base_url=ORIGIN)


# --------------------------------------------------------------------------
# CSP and hardening headers
# --------------------------------------------------------------------------


def test_csp_is_present_and_default_src_none(tmp_path: Path) -> None:
    """The CSP must be on a REAL response, not merely configured."""
    with _client(_context(tmp_path)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # A nonce or 'unsafe-inline' would mean the no-inline-script rule broke.
    assert "unsafe-inline" not in csp
    assert "nonce-" not in csp
    assert csp == CSP


def test_hardening_headers_on_every_response(tmp_path: Path) -> None:
    with _client(_context(tmp_path)) as client:
        for path in ("/api/health", "/api/does-not-exist"):
            response = client.get(path)
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "content-security-policy" in response.headers


def test_rejected_requests_also_carry_the_csp(tmp_path: Path) -> None:
    """A refusal is the response an attacker sees; it must not be the bare one."""
    with _client(_context(tmp_path, read_only=True)) as client:
        response = client.post(
            "/api/notes", json={"title": "x"}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 403
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_api_responses_are_not_cached(tmp_path: Path) -> None:
    with _client(_context(tmp_path)) as client:
        assert client.get("/api/health").headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------
# DNS rebinding — Host header validation
# --------------------------------------------------------------------------


def test_foreign_host_header_is_rejected(tmp_path: Path) -> None:
    """The defence an Origin check structurally cannot provide.

    An attacker's page can point a hostname at 127.0.0.1 and the browser will
    consider the request same-origin, sending a legitimate-looking Origin. Only
    the Host header distinguishes it.
    """
    with _client(_context(tmp_path)) as client:
        response = client.get("/api/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_loopback_host_spellings_are_accepted(tmp_path: Path) -> None:
    with _client(_context(tmp_path)) as client:
        for host in ("127.0.0.1", "localhost", "127.0.0.1:8765"):
            assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_is_loopback_rejects_all_interfaces() -> None:
    """0.0.0.0 is every interface, not loopback. Treating it as safe is the bug."""
    assert is_loopback("127.0.0.1")
    assert is_loopback("localhost")
    assert is_loopback("::1")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.10")


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_cross_origin_mutation_is_rejected(tmp_path: Path, method: str) -> None:
    with _client(_context(tmp_path)) as client:
        response = client.request(
            method,
            "/api/notes/abcdef",
            json={"body_hash": "x"},
            headers={"Origin": "https://evil.example"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_missing_origin_on_a_mutation_is_rejected(
    tmp_path: Path, method: str
) -> None:
    """Fail closed: some legacy form posts omit Origin entirely."""
    with _client(_context(tmp_path)) as client:
        response = client.request(
            method, "/api/notes/abcdef", json={"body_hash": "x"}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_missing"


def test_cross_site_sec_fetch_is_rejected(tmp_path: Path) -> None:
    with _client(_context(tmp_path)) as client:
        response = client.post(
            "/api/notes",
            json={"title": "x"},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_site"


def test_form_content_type_cannot_reach_a_write_handler(tmp_path: Path) -> None:
    """An HTML form can only send these types, so requiring JSON blocks form CSRF."""
    with _client(_context(tmp_path)) as client:
        response = client.post(
            "/api/notes",
            content="title=x",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    assert response.status_code == 415


# --------------------------------------------------------------------------
# Read-only mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/notes"),
        ("PUT", "/api/notes/abcdef"),
        ("DELETE", "/api/notes/abcdef"),
        ("POST", "/api/notes/abcdef/draft"),
        ("POST", "/api/notes/abcdef/move"),
    ],
)
def test_read_only_blocks_every_write(
    tmp_path: Path, method: str, path: str
) -> None:
    """Refused in middleware, before routing — so no handler can forget to check."""
    with _client(_context(tmp_path, read_only=True)) as client:
        response = client.request(
            method, path, json={"confirm": True}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "read_only"


def test_read_only_refuses_before_routing(tmp_path: Path) -> None:
    """A write to a path that does not exist still 403s, not 404s.

    That difference is the proof the check runs *before* the router: a
    per-handler decorator could not refuse a route that has no handler.
    """
    with _client(_context(tmp_path, read_only=True)) as client:
        response = client.post(
            "/api/no-such-endpoint", json={}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "read_only"


def test_read_only_still_serves_reads(tmp_path: Path) -> None:
    with _client(_context(tmp_path, read_only=True)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["read_only"] is True


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------


def test_token_is_required_on_the_api_when_set(tmp_path: Path) -> None:
    context = _context(tmp_path, token="s3cret")
    with _client(context) as client:
        assert client.get("/api/health").status_code == 403
        assert (
            client.get(
                "/api/health", headers={"X-Brain-UI-Token": "wrong"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/health", headers={"X-Brain-UI-Token": "s3cret"}
            ).status_code
            == 200
        )


def test_token_does_not_gate_the_shell(tmp_path: Path) -> None:
    """The shell must load before any JS exists to send the header.

    It is a constant HTML file with no corpus data in it; every byte of the
    user's brain is behind /api/, which the token does gate.
    """
    with _client(_context(tmp_path, token="s3cret")) as client:
        assert client.get("/").status_code == 200


# --------------------------------------------------------------------------
# GET cannot mutate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/health", "/api/status", "/api/facets", "/api/search", "/api/tree"],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_read_endpoints_reject_mutating_methods(
    tmp_path: Path, path: str, method: str
) -> None:
    with _client(_context(tmp_path)) as client:
        response = client.request(method, path, json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 405


@pytest.mark.parametrize(
    "path", ["/api/notes", "/api/notes/abcdef/draft", "/api/notes/abcdef/move"]
)
def test_write_endpoints_reject_get(tmp_path: Path, path: str) -> None:
    with _client(_context(tmp_path)) as client:
        assert client.get(path).status_code == 405
