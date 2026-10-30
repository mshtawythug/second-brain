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
from brain.ui.security import CSP, TOKEN_HEADER, is_loopback

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


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
def test_a_wildcard_bind_refuses_another_devices_host_header(
    tmp_path: Path, wildcard: str
) -> None:
    """Why the phone recipe is ``--host <LAN IP>`` (or ``--lan``), never a wildcard.

    Every wildcard bind gets the LOOPBACK allowlist (phase-7 ruling W1), so a
    phone that reaches the machine by its LAN address is refused before any
    route runs. That is the DNS-rebinding defence working as designed; the docs
    used to ship ``0.0.0.0`` as the remote-reading recipe. Pinned so the guide
    cannot drift from it.
    """
    from brain.ui.context import host_allowlist

    context = _context(tmp_path, token="t", allowed_hosts=host_allowlist(wildcard))
    with _client(context) as client:
        response = client.get(
            "/api/health", headers={"Host": "192.0.2.10:8765", TOKEN_HEADER: "t"}
        )
    assert response.status_code == 400


@pytest.mark.parametrize("wildcard", ["::", "[::]"])
def test_an_ipv6_wildcard_gets_exactly_the_ipv4_wildcards_allowlist(wildcard: str) -> None:
    """Ruling W1 (phase 7): ``::`` behaves like ``0.0.0.0``, and no wider.

    Before W1, ``host_allowlist("::")`` was ``{"::"}`` — a Host value no browser
    can send (IPv6 literals arrive bracketed), so a ``--host ::`` server refused
    every request, this machine's included. Equality, not superset: the IPv6
    wildcard must not accept anything the IPv4 one does not.
    """
    from brain.ui.context import LOOPBACK_HOSTS, host_allowlist

    assert host_allowlist(wildcard) == host_allowlist("0.0.0.0") == LOOPBACK_HOSTS


@pytest.mark.parametrize(
    ("wildcard", "local_host"), [("0.0.0.0", "127.0.0.1"), ("::", "[::1]")]
)
def test_a_wildcard_bind_accepts_a_write_from_the_url_it_opens(
    tmp_path: Path, wildcard: str, local_host: str
) -> None:
    """Ruling W2: the opened URL and the expected Origin come from one helper.

    Before W2 the Origin a wildcard bind expected was ``http://0.0.0.0:<port>``,
    which no browser ever sends, so every write from the opened page was refused
    as cross-origin. The request is a POST to an unrouted path so the guard is
    the only thing that can refuse it: passing it yields 404, not 403.
    """
    from brain.ui.context import bind_origin, host_allowlist

    origin = bind_origin(wildcard, 8765)
    assert origin == f"http://{local_host}:8765"
    context = _context(
        tmp_path, allowed_origin=origin, allowed_hosts=host_allowlist(wildcard)
    )
    with TestClient(create_app(context), base_url=origin) as client:
        response = client.post(
            "/api/no-such-route", json={}, headers={"Origin": origin}
        )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_a_wildcard_bind_still_refuses_a_foreign_origin_on_writes(
    tmp_path: Path, wildcard: str
) -> None:
    """W2 must not loosen CSRF: only the derived origin passes, nothing else."""
    from brain.ui.context import bind_origin, host_allowlist

    origin = bind_origin(wildcard, 8765)
    context = _context(
        tmp_path, allowed_origin=origin, allowed_hosts=host_allowlist(wildcard)
    )
    with TestClient(create_app(context), base_url=origin) as client:
        response = client.post(
            "/api/no-such-route",
            json={},
            headers={"Origin": "http://192.0.2.10:8765"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_mismatch"


def test_bind_origin_brackets_ipv6_and_passes_other_hosts_through() -> None:
    from brain.ui.context import bind_origin

    assert bind_origin("127.0.0.1", 8765) == "http://127.0.0.1:8765"
    assert bind_origin("192.0.2.10", 9000) == "http://192.0.2.10:9000"
    assert bind_origin("::1", 8765) == "http://[::1]:8765"
    assert bind_origin("[::]", 8765) == "http://[::1]:8765"
    # Round 5: non-canonical spellings become the origin a browser sends.
    assert bind_origin("0:0:0:0:0:0:0:1", 8765) == "http://[::1]:8765"
    assert bind_origin("FD00:0:0::5", 8765) == "http://[fd00::5]:8765"


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        (("0.0.0.0",), "http://127.0.0.1:8765"),
        (("::",), "http://[::1]:8765"),
        (("0.0.0.0", "::"), "http://127.0.0.1:8765"),
        # Round 11: the IPv4 address here is a SPECIFIC one, so 127.0.0.1 is
        # not bound at all — only `::` makes this a wildcard, and only its
        # loopback answers. "Any IPv4 address" picked 127.0.0.1 and opened a
        # page on a port nobody was listening on.
        (("192.0.2.5", "::"), "http://[::1]:8765"),
        (("127.0.0.1", "::"), "http://[::1]:8765"),
        (("::1", "0.0.0.0"), "http://127.0.0.1:8765"),
    ],
)
def test_a_wildcard_name_opens_the_loopback_of_its_unspecified_family(
    resolved: tuple[str, ...], expected: str
) -> None:
    import ipaddress

    from brain.ui.context import bind_origin

    addresses = tuple(ipaddress.ip_address(a) for a in resolved)
    assert bind_origin("brain-1.lan", 8765, addresses) == expected


def test_a_lan_bind_allowlist_is_exactly_the_bound_address() -> None:
    """HIGH-1 (phase-7 audit): a LAN bind accepts its own address and NOTHING else.

    Equality, not membership. The audit's mutation — adding ``"*"`` to the set
    for any dotted host — survived every non-browser test, because the only pin
    was that the bound address was accepted. ``"*"`` disables Host checking in
    ``TrustedHostMiddleware`` entirely, reopening DNS rebinding on the one bind
    that is reachable from the network.
    """
    from brain.ui.context import host_allowlist

    assert host_allowlist("192.0.2.10") == frozenset({"192.0.2.10"})


@pytest.mark.parametrize("bind", ["192.0.2.10", "0.0.0.0", "::", "[::]"])
def test_a_non_loopback_bind_refuses_a_foreign_host_header(
    tmp_path: Path, bind: str
) -> None:
    """HIGH-1: the rebinding defence, through the real app, on every bind kind."""
    from brain.ui.context import host_allowlist

    context = _context(tmp_path, token="t", allowed_hosts=host_allowlist(bind))
    with _client(context) as client:
        response = client.get(
            "/api/health", headers={"Host": "evil.example", TOKEN_HEADER: "t"}
        )
    assert response.status_code == 400


IPV6_LAN = "fd00::5"  # unique-local (RFC 4193): synthetic, never routed


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (IPV6_LAN, frozenset({"[fd00::5]"})),
        ("[fd00::5]", frozenset({"[fd00::5]"})),
        ("FD00:0:0::5", frozenset({"[fd00::5]"})),
        ("192.0.2.10", frozenset({"192.0.2.10"})),
    ],
)
def test_a_non_loopback_allowlist_is_exactly_the_canonical_host_header(
    given: str, expected: frozenset[str]
) -> None:
    """Round 5, item 1: starlette >= 1.7 hands the middleware ``[fd00::5]``.

    The allowlist held the bare ``fd00::5``, so every request to an IPv6 bind
    was refused — its own URL included. The set must hold the form the Host
    header ARRIVES in: bracketed, and canonical, because a browser serialises
    an IPv6 literal compressed and lower-case whatever was typed.
    """
    from brain.ui.context import host_allowlist

    assert host_allowlist(given) == expected


@pytest.mark.parametrize("spelling", ["0:0:0:0:0:0:0:1", "[0:0:0:0:0:0:0:1]"])
def test_a_non_canonical_loopback_is_loopback(spelling: str) -> None:
    from brain.ui.context import LOOPBACK_HOSTS, host_allowlist

    assert host_allowlist(spelling) == LOOPBACK_HOSTS
    assert is_loopback(spelling)


def test_a_non_canonical_wildcard_is_a_wildcard() -> None:
    from brain.ui.context import LOOPBACK_HOSTS, host_allowlist

    assert host_allowlist("0:0:0:0:0:0:0:0") == LOOPBACK_HOSTS


@pytest.mark.parametrize(
    ("host", "status"),
    [("[fd00::5]:8765", 200), ("evil.example", 400), ("[fd00::6]:8765", 400)],
)
def test_an_ipv6_bind_serves_its_own_url_and_refuses_others(
    tmp_path: Path, host: str, status: int
) -> None:
    """Round 5, item 1, through the real middleware: own URL 200, foreign 400."""
    from brain.ui.context import bind_origin, host_allowlist

    origin = bind_origin(IPV6_LAN, 8765)
    assert origin == "http://[fd00::5]:8765"
    context = _context(
        tmp_path, token="t", allowed_origin=origin,
        allowed_hosts=host_allowlist(IPV6_LAN),
    )
    with _client(context) as client:
        response = client.get("/api/health", headers={"Host": host, TOKEN_HEADER: "t"})
    assert response.status_code == status


def test_an_ipv6_bind_accepts_a_write_from_its_own_origin(tmp_path: Path) -> None:
    from brain.ui.context import bind_origin, host_allowlist

    # The Origin is the LITERAL a browser sends for this bind — not one
    # computed by the helper under test, which would agree with itself.
    browser_origin = "http://[fd00::5]:8765"
    context = _context(
        tmp_path, token="t", allowed_origin=bind_origin("FD00:0:0::5", 8765),
        allowed_hosts=host_allowlist("FD00:0:0::5"),
    )
    with TestClient(create_app(context), base_url=browser_origin) as client:
        response = client.post(
            "/api/no-such-route", json={},
            headers={"Origin": browser_origin, TOKEN_HEADER: "t"},
        )
    assert response.status_code == 404, response.text


def test_a_non_ascii_token_is_refused_not_a_server_error(tmp_path: Path) -> None:
    """Round 5, item 5: ``secrets.compare_digest`` raises ``TypeError`` on a
    non-ASCII ``str``, so a server started with ``--token café`` answered every
    API call with 500. The comparison is on bytes now; the CLI also refuses
    such a token up front."""
    with _client(_context(tmp_path, token="café")) as client:
        response = client.get("/api/health", headers={TOKEN_HEADER: "wrong"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("bind", "host", "status"),
    [("LOCALHOST", "localhost:8765", 200), ("MyMac.LOCAL", "mymac.local:8765", 200),
     ("MyMac.LOCAL", "evil.example", 400),
     # Round 9: loopback by ADDRESS, not by spelling — and its own URL works.
     ("127.0.0.2", "127.0.0.2:8765", 200), ("127.0.0.2", "evil.example", 400)],
)
def test_a_hostname_bind_is_matched_case_insensitively(
    tmp_path: Path, bind: str, host: str, status: int
) -> None:
    """Round 6, item 2, through the real middleware. A browser lower-cases the
    host before sending it; the allowlist must hold the same spelling."""
    from brain.ui.context import bind_origin, host_allowlist

    context = _context(
        tmp_path, token="t", allowed_origin=bind_origin(bind, 8765),
        allowed_hosts=host_allowlist(bind),
    )
    with _client(context) as client:
        response = client.get("/api/health", headers={"Host": host, TOKEN_HEADER: "t"})
    assert response.status_code == status


def test_a_lan_address_bind_accepts_its_own_host_header(tmp_path: Path) -> None:
    """The working half: bind the LAN address and the phone's Host matches."""
    from brain.ui.context import host_allowlist

    context = _context(
        tmp_path, token="t", allowed_hosts=host_allowlist("192.0.2.10")
    )
    with _client(context) as client:
        response = client.get(
            "/api/health", headers={"Host": "192.0.2.10:8765", TOKEN_HEADER: "t"}
        )
    assert response.status_code == 200


def test_is_loopback_rejects_all_interfaces() -> None:
    """0.0.0.0 is every interface, not loopback. Treating it as safe is the bug."""
    import ipaddress

    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    # Review round 10: `localhost` is loopback because of where it RESOLVES,
    # never by its name — this line asserted the name alone until then.
    assert not is_loopback("localhost")
    assert is_loopback("localhost", (ipaddress.ip_address("127.0.0.1"),))
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


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Origin": "https://evil.example"}, 403, "origin_mismatch"),
        ({}, 403, "origin_missing"),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, 403, "cross_site"),
    ],
)
def test_an_ask_is_guarded_like_every_mutation(
    tmp_path: Path, headers: dict[str, str], status: int, code: str
) -> None:
    """RM-18a: an ask spends minutes of the chat model, so it is a guarded write."""
    with _client(_context(tmp_path)) as client:
        response = client.post("/api/ask", json={"question": "q"}, headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_a_form_cannot_submit_an_ask(tmp_path: Path) -> None:
    with _client(_context(tmp_path)) as client:
        response = client.post(
            "/api/ask",
            content="question=q",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 415


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
        ("POST", "/api/ask"),
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
    ["/api/health", "/api/status", "/api/facets", "/api/search", "/api/tree",
     "/api/ask/config"],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_read_endpoints_reject_mutating_methods(
    tmp_path: Path, path: str, method: str
) -> None:
    with _client(_context(tmp_path)) as client:
        response = client.request(method, path, json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 405


@pytest.mark.parametrize(
    "path", ["/api/notes", "/api/notes/abcdef/draft", "/api/notes/abcdef/move", "/api/ask"]
)
def test_write_endpoints_reject_get(tmp_path: Path, path: str) -> None:
    with _client(_context(tmp_path)) as client:
        assert client.get(path).status_code == 405


@pytest.mark.parametrize(
    ("host", "kind"),
    [("0.0.0.0", "wildcard"), ("::", "wildcard"), ("0:0::0", "wildcard"),
     ("127.0.0.1", "loopback"), ("127.0.0.2", "loopback"), ("::1", "loopback"),
     # Round 10: a hostname is never classified by its NAME — unresolved, it is
     # "other" (and needs a token); see the resolved cases below.
     ("localhost", "other"), ("LOCALHOST", "other"),
     ("192.0.2.10", "other"), ("fd00::5", "other"), ("brain-1.lan", "other")],
)
def test_address_class_is_decided_by_the_address_not_the_spelling(
    host: str, kind: str
) -> None:
    """Round 9 (review): classify by the parsed address. ``127.0.0.2`` is
    loopback although no loopback SET spells it; ``0:0::0`` is the wildcard."""
    from brain.ui.context import address_class

    assert address_class(host) == kind
    assert is_loopback(host) is (kind == "loopback")


@pytest.mark.parametrize(
    ("addresses", "kind"),
    [(["0.0.0.0"], "wildcard"), (["127.0.0.1", "0.0.0.0"], "wildcard"),
     (["127.0.0.1", "::1"], "loopback"), (["127.0.0.1", "192.0.2.30"], "other"),
     (["192.0.2.30"], "other"), (["::"], "wildcard")],
)
def test_a_name_is_classified_by_every_address_it_resolves_to(
    addresses: list[str], kind: str
) -> None:
    """Round 10, item 2: ANY unspecified address makes it a wildcard; it is
    loopback only if ALL are loopback, because uvicorn binds every one.

    Judged for ``localhost`` since round 16: an arbitrary name is never
    loopback (see the next test), so it could not show the "loopback" row."""
    import ipaddress

    from brain.ui.context import address_class

    resolved = tuple(ipaddress.ip_address(a) for a in addresses)
    assert address_class("localhost", resolved) == kind
    assert is_loopback("localhost", resolved) is (kind == "loopback")


@pytest.mark.parametrize(
    ("name", "addresses", "kind", "alias"),
    [
        ("loopback-alias.example", ["127.0.0.1"], "other", True),
        ("loopback-alias.example", ["127.0.0.1", "::1"], "other", True),
        ("127.0.0.1.nip.io", ["127.0.0.1"], "other", True),
        ("localhost", ["127.0.0.1", "::1"], "loopback", False),
        ("LOCALHOST", ["127.0.0.1"], "loopback", False),
        # Round 16b: not trusted — macOS resolves it through network DNS
        ("ip6-localhost", ["::1"], "other", True),
        # Round 16b: the trailing dot is a different name and Host value
        ("localhost.", ["127.0.0.1"], "other", True),
        # Round 16b, edge case (b): a trusted name with a mixed answer
        ("localhost", ["127.0.0.1", "192.0.2.30"], "other", False),
        ("loopback-alias.example", ["0.0.0.0"], "wildcard", False),
    ],
)
def test_only_a_trusted_name_resolving_to_loopback_is_loopback(
    name: str, addresses: list[str], kind: str, alias: bool
) -> None:
    """Round 16 (phase-7 audit of `cb4b6ed`, finding 1). A loopback bind skips
    the token and allowlists its own name, so a name whose DNS someone else
    controls must not get it: rebound onto this server, a page from that name
    sends an allowlisted Host and the allowed Origin. Only LOOPBACK_NAMES may
    be loopback; any other name is "other" — Host allowlist the name alone,
    confidential bodies withheld — and the addresses themselves still classify
    as loopback, which is how the CLI says why."""
    import ipaddress

    from brain.ui.context import (
        address_class,
        classify_addresses,
        host_allowlist,
        is_loopback_alias,
    )

    resolved = tuple(ipaddress.ip_address(a) for a in addresses)
    assert address_class(name, resolved) == kind
    assert is_loopback(name, resolved) is (kind == "loopback")
    assert is_loopback_alias(name, resolved) is alias
    if alias:
        assert classify_addresses(resolved) == "loopback"
        assert host_allowlist(name, resolved) == frozenset({name.lower()})


def test_a_loopback_literal_is_never_an_alias() -> None:
    """Literals are judged on themselves; the name rule is for names only."""
    import ipaddress

    from brain.ui.context import address_class, is_loopback_alias

    for literal in ("127.0.0.1", "127.0.0.2", "::1", "[::1]"):
        resolved = (ipaddress.ip_address(literal.strip("[]")),)
        assert not is_loopback_alias(literal, resolved)
        assert address_class(literal, resolved) == "loopback"


@pytest.mark.parametrize("name", ["localhoſt", "K", "brain-ı.lan"])
def test_the_hostname_pattern_is_ascii_only(name: str) -> None:
    """Round 10, item 4, on the pattern itself: ``validate_host`` refuses
    non-ASCII before the pattern runs, so only a direct test can show the
    pattern would not let ``ſ`` or the Kelvin sign through on its own."""
    from brain.ui.context import _HOSTNAME

    assert not _HOSTNAME.fullmatch(name)
