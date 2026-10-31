"""`brain ingest-web --fetch` (RM-41): the egress rules, against ``httpx.MockTransport`` only.

No test here touches the network: the transport is a mock and the resolver is a
fake mapping. The rules (ticket point 6, amendment A1): https only; every hop's
host allowlisted; every address the host resolves to public (one private address
refuses the hop); the request connects to the validated address, with the name
carried in ``Host`` and in the TLS ``sni_hostname`` extension, so a second DNS
answer cannot be swapped in between the check and the connect.
"""
from __future__ import annotations

import gzip
import ipaddress
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from brain.ingest import web_fetch as wf
from brain.ingest.web_fetch import WebFetchError, fetch_page, make_client, public_only
from brain.settings.web_allowlist import WebAllowlist
from tests.tls_fixtures import KeepAliveServer, make_ca_and_server_cert

ALLOW = WebAllowlist(exact=frozenset({"example.com", "docs.example.com"}),
                     suffixes=frozenset({"example.org"}))
PUBLIC = {"example.com": ["93.184.215.14"], "docs.example.com": ["93.184.215.15"],
          "a.example.org": ["2606:2800:21f:cb07::1"], "example.net": ["93.184.215.16"]}
HTML = "text/html; charset=utf-8"


def resolver(table: dict[str, list[str]]) -> Callable[[str, int], Sequence[str]]:
    def resolve(host: str, port: int) -> Sequence[str]:
        return table.get(host, [])
    return resolve


class Recorder:
    """A MockTransport handler that serves ``routes`` and records every request."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        return self.routes.get(key, _response(404))


def _response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    text: str | None = None,
    content: Any = None,
) -> httpx.Response:
    """A STREAMED response, as the network delivers one.

    ``httpx.Response(text=…)`` / ``content=bytes`` is read eagerly, and the fetch reads
    raw (``iter_raw``, sec-114 F1), which a pre-read response refuses. An iterator
    ``content`` streams already.
    """
    if text is not None:
        content = text.encode("utf-8")
    if content is None or isinstance(content, bytes):
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content or b""))
    return httpx.Response(status, headers=headers, content=content)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler))


def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> wf.ClientFactory:
    """A fresh client per fetch, each on a MockTransport serving ``handler``."""
    return lambda: _client(handler)


def _fetch(routes: dict[str, httpx.Response], url: str = "https://example.com/doc",
           table: dict[str, list[str]] = PUBLIC) -> tuple[str, Recorder]:
    recorder = Recorder(routes)
    text = fetch_page(url, ALLOW, client_factory=_factory(recorder), resolver=resolver(table))
    return text, recorder


def _html(body: str, **headers: str) -> httpx.Response:
    return _response(200, headers={"content-type": HTML, **headers}, text=body)


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


# --- the happy path, and the pinned connection --------------------------------------------------


def test_an_allowlisted_https_page_is_fetched_as_text() -> None:
    text, recorder = _fetch({"example.com/doc": _html(
        "<html><head><script>steal()</script></head><body><h1>Title</h1><p>Body text.</p>"
        "</body></html>")})

    assert "Title" in text and "Body text." in text
    assert "steal" not in text
    assert len(recorder.requests) == 1


def test_the_request_connects_to_the_validated_address_with_the_name_in_host_and_sni() -> None:
    _, recorder = _fetch({"example.com/doc": _html("<p>x</p>")})

    (request,) = recorder.requests
    assert request.url.scheme == "https"
    assert request.url.host == "93.184.215.14"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert "cookie" not in request.headers
    assert request.headers["user-agent"] == wf.USER_AGENT


def test_an_ipv6_address_is_bracketed_and_a_port_is_kept() -> None:
    recorder = Recorder({"a.example.org:8443/p": _html("<p>v6</p>")})
    text = fetch_page(
        "https://a.example.org:8443/p", ALLOW, client_factory=_factory(recorder),
        resolver=resolver(PUBLIC),
    )

    (request,) = recorder.requests
    assert text == "v6"
    assert request.url.host == "2606:2800:21f:cb07::1"
    assert request.url.port == 8443
    assert request.headers["host"] == "a.example.org:8443"


def test_plain_text_is_returned_as_it_is() -> None:
    text, _ = _fetch({"example.com/doc": _response(
        200,
        headers={"content-type": "text/plain"},
        text="  plain words  ")})

    assert text == "plain words"


def test_an_unknown_charset_falls_back_to_utf8() -> None:
    text, _ = _fetch({"example.com/doc": _response(
        200,
        headers={"content-type": "text/plain; charset=no-such-codec"},
        content="café".encode())})

    assert text == "café"


def test_a_redirect_within_the_allowlist_is_followed_and_each_hop_is_pinned() -> None:
    text, recorder = _fetch({
        "example.com/doc": _redirect("https://docs.example.com/final?x=1"),
        "docs.example.com/final?x=1": _html("<p>arrived</p>"),
    })

    assert text == "arrived"
    assert [r.url.host for r in recorder.requests] == ["93.184.215.14", "93.184.215.15"]


def test_a_relative_redirect_resolves_against_the_hop_url() -> None:
    text, _ = _fetch({
        "example.com/doc": _redirect("/moved", 301),
        "example.com/moved": _html("<p>here</p>"),
    })

    assert text == "here"


def test_cookies_set_by_one_hop_are_not_sent_to_the_next() -> None:
    _, recorder = _fetch({
        "example.com/doc": _response(
            302,
            headers={"location": "https://example.com/next", "set-cookie": "s=1"}),
        "example.com/next": _html("<p>ok</p>"),
    })

    assert "cookie" not in recorder.requests[1].headers


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/doc", "not https"),
        ("https://example.net/doc", "not on the allowlist"),
        ("https://example.org/doc", "not on the allowlist"),  # wildcard never covers the apex
        ("https://93.184.215.14/doc", "not on the allowlist"),
        ("ftp://example.com/doc", "not https"),
        ("https:///nohost", "not on the allowlist"),
    ],
)
def test_a_url_off_the_rules_is_refused_before_any_request(url: str, reason: str) -> None:
    recorder = Recorder({})
    with pytest.raises(WebFetchError, match=reason):
        fetch_page(url, ALLOW, client_factory=_factory(recorder), resolver=resolver(PUBLIC))

    assert recorder.requests == []


@pytest.mark.parametrize(
    "location",
    [
        "https://example.net/elsewhere",  # off the allowlist (acceptance criterion 5)
        "http://example.com/downgrade",  # off https
    ],
)
def test_a_redirect_off_the_rules_is_refused(location: str) -> None:
    recorder = Recorder({"example.com/doc": _redirect(location)})
    with pytest.raises(WebFetchError, match="refused"):
        fetch_page("https://example.com/doc", ALLOW,
            client_factory=_factory(recorder), resolver=resolver(PUBLIC))

    assert len(recorder.requests) == 1


def test_too_many_redirects_are_refused() -> None:
    routes = {f"example.com/{n}": _redirect(f"/{n + 1}") for n in range(10)}

    with pytest.raises(WebFetchError, match="too many redirects"):
        _fetch(routes, url="https://example.com/0")


def test_a_redirect_without_a_location_is_an_error() -> None:
    with pytest.raises(WebFetchError, match="redirect without a location"):
        _fetch({"example.com/doc": _response(302)})


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["172.16.0.9"],
        ["192.168.1.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["240.0.0.1"],
        ["::1"],
        ["fd00::1"],
        ["fe80::1"],
        ["::"],
        ["ff02::1"],
        ["::ffff:127.0.0.1"],
        ["93.184.215.14", "10.0.0.1"],  # ONE private address refuses the hop
    ],
)
def test_a_host_resolving_to_a_non_public_address_is_refused(addresses: list[str]) -> None:
    recorder = Recorder({})
    table = {**PUBLIC, "example.com": addresses}
    with pytest.raises(WebFetchError, match="not public"):
        fetch_page("https://example.com/doc", ALLOW,
            client_factory=_factory(recorder), resolver=resolver(table))

    assert recorder.requests == []


def test_a_redirect_to_a_host_resolving_privately_is_refused() -> None:
    table = {**PUBLIC, "docs.example.com": ["192.168.0.10"]}
    recorder = Recorder({"example.com/doc": _redirect("https://docs.example.com/x")})
    with pytest.raises(WebFetchError, match="not public"):
        fetch_page("https://example.com/doc", ALLOW,
            client_factory=_factory(recorder), resolver=resolver(table))

    assert len(recorder.requests) == 1


def test_a_host_that_does_not_resolve_is_an_error() -> None:
    with pytest.raises(WebFetchError, match="did not resolve"):
        _fetch({}, table={})


def test_a_resolver_answer_that_is_not_an_address_is_refused() -> None:
    with pytest.raises(WebFetchError, match="not public"):
        _fetch({}, table={"example.com": ["not-an-ip"]})


def test_the_default_policy_is_public_only() -> None:
    import inspect

    default = inspect.signature(fetch_page).parameters["policy"].default
    assert default is public_only
    assert inspect.signature(fetch_page).parameters["resolver"].default is wf.system_resolver
    assert public_only(ipaddress.ip_address("93.184.215.14")) is True
    assert public_only(ipaddress.ip_address("2606:2800:21f:cb07::1")) is True
    for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1", "fe80::1",
                    "224.0.0.1", "0.0.0.0", "::ffff:10.0.0.1", "192.0.2.1"):
        assert public_only(ipaddress.ip_address(private)) is False, private


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_response(404), "HTTP 404"),
        (_response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"),
         "unsupported content type"),
        (_response(200, content=b"no type"), "unsupported content type"),
    ],
)
def test_bad_responses_are_errors(response: httpx.Response, reason: str) -> None:
    with pytest.raises(WebFetchError, match=reason):
        _fetch({"example.com/doc": response})


def test_a_body_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wf, "MAX_BODY_BYTES", 10)

    with pytest.raises(WebFetchError, match="larger than"):
        _fetch({"example.com/doc": _html("<p>" + "x" * 100 + "</p>")})


def test_the_cap_is_enforced_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wf, "MAX_BODY_BYTES", 10)
    served: list[int] = []

    def endless() -> Iterator[bytes]:
        for _ in range(1000):
            served.append(1)
            yield b"x" * 8

    with pytest.raises(WebFetchError, match="larger than"):
        _fetch({"example.com/doc": _response(
            200,
            headers={"content-type": "text/plain"},
            content=endless())})

    assert len(served) < 1000


def test_the_overall_deadline_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.0] + [wf.TIMEOUT_SECONDS + 1.0] * 10)
    monkeypatch.setattr(wf, "_clock", lambda: next(ticks))

    def chunks() -> Iterator[bytes]:
        yield b"a"
        yield b"b"

    with pytest.raises(WebFetchError, match="took longer than"):
        _fetch({"example.com/doc": _response(
            200,
            headers={"content-type": "text/plain"},
            content=chunks())})


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.ReadError("r")]
)
def test_transport_failures_are_class_only_errors(exc: Exception) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(WebFetchError) as caught:
        fetch_page("https://example.com/doc", ALLOW,
            client_factory=_factory(failing), resolver=resolver(PUBLIC))

    assert str(caught.value) == f"fetch failed ({type(exc).__name__})"
    assert caught.value.__cause__ is None


def test_the_client_ignores_the_environment_and_does_not_follow_redirects() -> None:
    with make_client() as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.connect == wf.TIMEOUT_SECONDS
        assert client.timeout.read == wf.TIMEOUT_SECONDS


def test_the_system_resolver_returns_unique_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(args)
        return [
            (2, 1, 6, "", ("93.184.215.14", 443)),
            (2, 1, 6, "", ("93.184.215.14", 443)),
            (30, 1, 6, "", ("2606:2800:21f:cb07::1", 443, 0, 0)),
        ]

    monkeypatch.setattr(wf.socket, "getaddrinfo", fake_getaddrinfo)

    assert wf.system_resolver("example.com", 443) == ["93.184.215.14", "2606:2800:21f:cb07::1"]
    assert calls[0][:2] == ("example.com", 443)


def test_the_system_resolver_maps_a_lookup_failure_to_no_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(*args: Any, **kwargs: Any) -> list[Any]:
        raise wf.socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(wf.socket, "getaddrinfo", failing)

    assert wf.system_resolver("example.com", 443) == []


def test_a_malformed_port_is_refused_before_any_request() -> None:
    recorder = Recorder({})
    with pytest.raises(WebFetchError, match="malformed port"):
        fetch_page("https://example.com:99999/x", ALLOW, client_factory=_factory(recorder),
                   resolver=resolver(PUBLIC))

    assert recorder.requests == []


def test_a_transport_failure_while_streaming_is_a_class_only_error() -> None:
    def broken() -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("synthetic mid-body failure")

    with pytest.raises(WebFetchError) as caught:
        _fetch({"example.com/doc": _response(
            200,
            headers={"content-type": "text/plain"},
            content=broken())})

    assert str(caught.value) == "fetch failed (ReadError)"



# --- sec-114 F1: no decompression, raw bytes counted ----------------------------------------


@pytest.mark.parametrize(
    "coding", ["gzip", "gzip, gzip", "zstd", "br", "deflate", "identity, gzip", "x-custom"]
)
def test_any_content_encoding_but_identity_is_refused_unread(coding: str) -> None:
    served: list[int] = []

    def bomb() -> Iterator[bytes]:
        for _ in range(1000):
            served.append(1)
            yield gzip.compress(b"\0" * 65536)

    with pytest.raises(WebFetchError, match="unsupported content encoding"):
        _fetch({"example.com/doc": _response(
            200,
            headers={"content-type": "text/plain", "content-encoding": coding},
            content=bomb())})

    assert served == []


@pytest.mark.parametrize("coding", ["identity", "Identity", ""])
def test_identity_or_no_encoding_is_read(coding: str) -> None:
    headers = {"content-type": "text/plain"}
    if coding:
        headers["content-encoding"] = coding

    text, _ = _fetch({"example.com/doc": _response(200, headers=headers, text="plain")})

    assert text == "plain"


def test_the_cap_counts_the_bytes_on_the_wire_not_decoded_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compressed body sent WITHOUT a Content-Encoding is never decompressed."""
    payload = gzip.compress(b"A" * 1_000_000)
    monkeypatch.setattr(wf, "MAX_BODY_BYTES", len(payload) + 10)

    text, _ = _fetch({"example.com/doc": _response(
        200,
        headers={"content-type": "text/plain; charset=latin-1"},
        content=payload)})

    assert len(text) < len(payload) + 10
    assert "A" * 1000 not in text


# --- sec-114 F4: one wall-clock deadline over every hop ---------------------------------------


class StallingTransport(httpx.BaseTransport):
    """Never answers until released, like a server trickling its headers."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.entered.set()
        self.release.wait(30)
        raise httpx.ReadTimeout("released", request=request)


def test_a_stalled_server_cannot_hold_the_fetch_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "TIMEOUT_SECONDS", 0.3)
    stall = StallingTransport()
    started = time.monotonic()
    try:
        with pytest.raises(WebFetchError, match="took longer than 0.3 s"):
            fetch_page("https://example.com/doc", ALLOW,
                       client_factory=lambda: make_client(transport=stall),
                       resolver=resolver(PUBLIC))
        elapsed = time.monotonic() - started
    finally:
        stall.release.set()

    assert stall.entered.is_set()
    assert elapsed < 5


def test_the_deadline_is_checked_before_every_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.0] + [wf.TIMEOUT_SECONDS + 1.0] * 10)
    monkeypatch.setattr(wf, "_clock", lambda: next(ticks))

    with pytest.raises(WebFetchError, match="took longer than"):
        _fetch({"example.com/doc": _redirect("/next"), "example.com/next": _html("<p>x</p>")})


def test_each_hop_gets_only_the_time_that_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([100.0, 107.5] + [107.5] * 10)
    monkeypatch.setattr(wf, "_clock", lambda: next(ticks))

    _, recorder = _fetch({"example.com/doc": _html("<p>x</p>")})

    timeout = recorder.requests[0].extensions["timeout"]
    assert timeout["connect"] == timeout["read"] == pytest.approx(2.5)


def test_an_unexpected_failure_in_the_worker_is_a_class_only_error() -> None:
    def broken_factory() -> httpx.Client:
        raise RuntimeError("synthetic detail that must not be printed")

    with pytest.raises(WebFetchError) as caught:
        fetch_page("https://example.com/doc", ALLOW, client_factory=broken_factory,
                   resolver=resolver(PUBLIC))

    assert str(caught.value) == "fetch failed (RuntimeError)"


# --- sec-114 F7: the server does not choose the decoder ---------------------------------------


@pytest.mark.parametrize(
    ("charset", "body", "expected"),
    [
        ("utf-16", "héllo".encode("utf-16"), "héllo"),
        ("windows-1252", "café".encode("cp1252"), "café"),
        ("iso-8859-15", "€uro".encode("iso8859-15"), "€uro"),
        ("shift_jis", "日本".encode("shift_jis"), "日本"),
        ("punycode", b"abc-xyz", "abc-xyz"),
        ("unicode_escape", b"\\x41\\N{BULLET}", "\\x41\\N{BULLET}"),
        ("utf-7", b"+AGEA-", "+AGEA-"),
        ("rot13", b"uryyb", "uryyb"),
    ],
)
def test_only_text_encodings_are_honoured(charset: str, body: bytes, expected: str) -> None:
    text, _ = _fetch({"example.com/doc": _response(
        200,
        headers={"content-type": f"text/plain; charset={charset}"}, content=body)})

    assert text == expected


# --- sec-114 F9: an explicit deny list, embedded IPv4 decoded ---------------------------------


def _teredo(server: str, client: str) -> str:
    value = (0x20010000 << 96) | (int(ipaddress.IPv4Address(server)) << 64)
    value |= ~int(ipaddress.IPv4Address(client)) & 0xFFFFFFFF
    return str(ipaddress.IPv6Address(value))


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07::1", True),
        ("0.1.2.3", False),
        ("10.9.8.7", False),
        ("100.64.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("172.31.255.255", False),
        ("192.0.0.8", False),
        ("192.0.2.1", False),
        ("192.88.99.1", False),
        ("192.168.1.1", False),
        ("198.18.0.1", False),
        ("198.51.100.1", False),
        ("203.0.113.1", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("::", False),
        ("::1", False),
        ("::7f00:1", False),  # v4-compatible (deprecated)
        ("100::1", False),  # discard
        ("fc00::1", False),
        ("fd12:3456::1", False),
        ("fe80::1", False),
        ("fec0::1", False),  # site-local, refused on every Python version
        ("ff02::1", False),
        ("2001:db8::1", False),
        ("2001:2::1", False),
        ("2001:10::1", False),
        ("2001:20::1", False),
        ("3fff::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:93.184.215.14", True),
        ("64:ff9b::7f00:1", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("64:ff9b::5db8:d70e", True),
        ("64:ff9b:1:7f00:0:100::", False),  # RFC 6052 /48: 127.0.0.1
        ("64:ff9b:1:5db8:d7:e00::", True),  # RFC 6052 /48: 93.184.215.14
        # 6to4 and Teredo are refused outright, whatever they embed (team-lead ruling).
        ("2002:7f00:1::1", False),  # 6to4 of 127.0.0.1
        ("2002:a9fe:a9fe::", False),  # 6to4 of 169.254.169.254
        ("2002:5db8:d70e::1", False),  # 6to4 of a public address
        (_teredo("93.184.215.14", "127.0.0.1"), False),
        (_teredo("10.0.0.1", "93.184.215.14"), False),
        (_teredo("93.184.215.14", "93.184.215.15"), False),
    ],
)
def test_public_only_pins_every_form(address: str, allowed: bool) -> None:
    assert public_only(ipaddress.ip_address(address)) is allowed


# --- sec-114 F13: no connection outlives its request ------------------------------------------


def test_no_connection_is_kept_alive_and_each_fetch_gets_a_fresh_client() -> None:
    made: list[httpx.Client] = []

    def factory() -> httpx.Client:
        made.append(_client(lambda request: _html("<p>x</p>")))
        return made[-1]

    for _ in range(2):
        fetch_page("https://example.com/doc", ALLOW, client_factory=factory,
                   resolver=resolver(PUBLIC))

    assert len(made) == 2 and made[0] is not made[1]
    assert wf.CONNECTION_LIMITS.max_keepalive_connections == 0
    with make_client() as client:
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert pool._max_keepalive_connections == 0



# --- rev-114 #1 / sec-114 F13: a second host on the same IP gets its own certificate check ----


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[KeepAliveServer, ssl.SSLContext]]:
    """A keep-alive HTTPS server on 127.0.0.1 holding a certificate for a.example ONLY."""
    ca, cert, key = make_ca_and_server_cert(tmp_path)
    server = KeepAliveServer(cert=cert, key=key, routes={})
    port = server.port
    server.routes.update({
        "a.example/ok": (200, {"Content-Type": "text/plain"}, b"A ok"),
        "a.example/start": (302, {"Location": f"https://b.example:{port}/final"}, b""),
        "b.example/final": (200, {"Content-Type": "text/plain"}, b"B served"),
    })
    trust = ssl.create_default_context(cafile=str(ca))
    try:
        yield server, trust
    finally:
        server.close()


TLS_ALLOW = WebAllowlist(exact=frozenset({"a.example", "b.example"}), suffixes=frozenset())


def _loopback(host: str, port: int) -> Sequence[str]:
    return ["127.0.0.1"]


def _fetch_tls(url: str, trust: ssl.SSLContext) -> str:
    return fetch_page(
        url, TLS_ALLOW,
        client_factory=lambda: make_client(verify=trust),
        resolver=_loopback,
        policy=lambda address: True,  # loopback is refused by default; this test needs it
    )


def test_the_name_on_the_certificate_is_checked(
    tls_server: tuple[KeepAliveServer, ssl.SSLContext],
) -> None:
    server, trust = tls_server

    assert _fetch_tls(f"https://a.example:{server.port}/ok", trust) == "A ok"


class _Shared:
    """Hands the same client to every fetch and never closes it: a client pooled across pages."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def __enter__(self) -> httpx.Client:
        return self.client

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_second_host_on_the_same_address_gets_its_own_certificate_check(
    tls_server: tuple[KeepAliveServer, ssl.SSLContext],
) -> None:
    """rev-114 #1 / sec-114 F13. Fetch a.example, then b.example, on ONE client.

    Both names resolve to 127.0.0.1 and the certificate names a.example only. httpcore
    pools connections by origin, here the IP literal, and reads ``sni_hostname`` only
    on a new connection, so with keep-alive the second fetch rode a.example's verified
    session and b.example was served unchecked (proved on 3484fc4: one connection,
    "B served"). With no keep-alive (``CONNECTION_LIMITS``) the second fetch opens its
    own connection with SNI b.example, and the certificate check refuses it.
    """
    server, trust = tls_server
    shared = make_client(verify=trust)
    try:
        def fetch(host: str, path: str) -> str:
            return fetch_page(
                f"https://{host}:{server.port}/{path}", TLS_ALLOW,
                client_factory=lambda: _Shared(shared),  # type: ignore[arg-type,return-value]
                resolver=_loopback,
                policy=lambda address: True,
            )

        assert fetch("a.example", "ok") == "A ok"
        with pytest.raises(WebFetchError, match=r"^fetch failed \(ConnectError\b"):
            fetch("b.example", "final")
    finally:
        shared.close()

    assert server.served == ["a.example/ok"]
    assert server.connections == 2


def test_a_redirect_to_another_host_on_the_same_address_is_checked_too(
    tls_server: tuple[KeepAliveServer, ssl.SSLContext],
) -> None:
    """The same rule inside one fetch: a.example redirects to b.example on the same IP."""
    server, trust = tls_server

    with pytest.raises(WebFetchError, match=r"^fetch failed \(ConnectError\b"):
        _fetch_tls(f"https://a.example:{server.port}/start", trust)

    assert server.served == ["a.example/start"]
