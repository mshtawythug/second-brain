"""`brain ingest-web --fetch` (RM-41): the egress rules, against ``httpx.MockTransport`` only.

No test here touches the network: the transport is a mock and the resolver is a
fake mapping. The rules (ticket point 6, amendment A1): https only; every hop's
host allowlisted; every address the host resolves to public (one private address
refuses the hop); the request connects to the validated address, with the name
carried in ``Host`` and in the TLS ``sni_hostname`` extension, so a second DNS
answer cannot be swapped in between the check and the connect.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import pytest

from brain.ingest import web_fetch as wf
from brain.ingest.web_fetch import WebFetchError, fetch_page, make_client, public_only
from brain.settings.web_allowlist import WebAllowlist

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
        return self.routes.get(key, httpx.Response(404))


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler))


def _fetch(routes: dict[str, httpx.Response], url: str = "https://example.com/doc",
           table: dict[str, list[str]] = PUBLIC) -> tuple[str, Recorder]:
    recorder = Recorder(routes)
    with _client(recorder) as client:
        text = fetch_page(url, ALLOW, client=client, resolver=resolver(table))
    return text, recorder


def _html(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": HTML, **headers}, text=body)


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
    with _client(recorder) as client:
        text = fetch_page("https://a.example.org:8443/p", ALLOW, client=client,
                          resolver=resolver(PUBLIC))

    (request,) = recorder.requests
    assert text == "v6"
    assert request.url.host == "2606:2800:21f:cb07::1"
    assert request.url.port == 8443
    assert request.headers["host"] == "a.example.org:8443"


def test_plain_text_is_returned_as_it_is() -> None:
    text, _ = _fetch({"example.com/doc": httpx.Response(
        200, headers={"content-type": "text/plain"}, text="  plain words  ")})

    assert text == "plain words"


def test_an_unknown_charset_falls_back_to_utf8() -> None:
    text, _ = _fetch({"example.com/doc": httpx.Response(
        200, headers={"content-type": "text/plain; charset=no-such-codec"},
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
        "example.com/doc": httpx.Response(
            302, headers={"location": "https://example.com/next", "set-cookie": "s=1"}),
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
    with _client(recorder) as client, pytest.raises(WebFetchError, match=reason):
        fetch_page(url, ALLOW, client=client, resolver=resolver(PUBLIC))

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
    with _client(recorder) as client, pytest.raises(WebFetchError, match="refused"):
        fetch_page("https://example.com/doc", ALLOW, client=client, resolver=resolver(PUBLIC))

    assert len(recorder.requests) == 1


def test_too_many_redirects_are_refused() -> None:
    routes = {f"example.com/{n}": _redirect(f"/{n + 1}") for n in range(10)}

    with pytest.raises(WebFetchError, match="too many redirects"):
        _fetch(routes, url="https://example.com/0")


def test_a_redirect_without_a_location_is_an_error() -> None:
    with pytest.raises(WebFetchError, match="redirect without a location"):
        _fetch({"example.com/doc": httpx.Response(302)})


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
    with _client(recorder) as client, pytest.raises(WebFetchError, match="not public"):
        fetch_page("https://example.com/doc", ALLOW, client=client, resolver=resolver(table))

    assert recorder.requests == []


def test_a_redirect_to_a_host_resolving_privately_is_refused() -> None:
    table = {**PUBLIC, "docs.example.com": ["192.168.0.10"]}
    recorder = Recorder({"example.com/doc": _redirect("https://docs.example.com/x")})
    with _client(recorder) as client, pytest.raises(WebFetchError, match="not public"):
        fetch_page("https://example.com/doc", ALLOW, client=client, resolver=resolver(table))

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
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"),
         "unsupported content type"),
        (httpx.Response(200, content=b"no type"), "unsupported content type"),
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
        _fetch({"example.com/doc": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=endless())})

    assert len(served) < 1000


def test_the_overall_deadline_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.0, wf.TIMEOUT_SECONDS + 1.0])
    monkeypatch.setattr(wf, "_clock", lambda: next(ticks))

    def chunks() -> Iterator[bytes]:
        yield b"a"
        yield b"b"

    with pytest.raises(WebFetchError, match="took longer than"):
        _fetch({"example.com/doc": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=chunks())})


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.ReadError("r")]
)
def test_transport_failures_are_class_only_errors(exc: Exception) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise exc

    with _client(failing) as client, pytest.raises(WebFetchError) as caught:
        fetch_page("https://example.com/doc", ALLOW, client=client, resolver=resolver(PUBLIC))

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
    with _client(recorder) as client, pytest.raises(WebFetchError, match="malformed port"):
        fetch_page("https://example.com:99999/x", ALLOW, client=client,
                   resolver=resolver(PUBLIC))

    assert recorder.requests == []


def test_a_transport_failure_while_streaming_is_a_class_only_error() -> None:
    def broken() -> Iterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("synthetic mid-body failure")

    with pytest.raises(WebFetchError) as caught:
        _fetch({"example.com/doc": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=broken())})

    assert str(caught.value) == "fetch failed (ReadError)"
