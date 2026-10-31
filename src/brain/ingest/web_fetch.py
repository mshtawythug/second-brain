"""The opt-in page fetch of `brain ingest-web --fetch` (RM-41): the one network egress.

A page's text is downloaded only when the owner passes ``--fetch``, and only
under these rules, each checked at **every** hop (the first request and each
redirect):

1. **https only.** An http URL or redirect is refused.
2. **Allowlisted host.** The hop's normalized host must be on the allowlist; a
   redirect off it is refused (acceptance criterion 5).
3. **Public addresses only** (amendment A1). The host is resolved through the
   injected :data:`Resolver`, and the hop is refused when ANY address fails the
   injected :data:`AddressPolicy`. The default, :func:`public_only`, refuses
   loopback, private (RFC 1918, ULA), link-local, shared (CGNAT), multicast,
   unspecified and reserved addresses, and IPv4-mapped forms of them.
4. **Connect to what was validated.** The request goes to the IP literal; the
   name travels in the ``Host`` header and in httpcore's ``sni_hostname``
   request extension, which sets the TLS server name and so the certificate
   check. There is no second DNS lookup between the check and the connect, so
   DNS rebinding cannot swap in another address.

And per fetch: at most :data:`MAX_REDIRECTS` redirects; :data:`TIMEOUT_SECONDS`
per network operation and overall; :data:`MAX_BODY_BYTES` read, counted while
streaming (``Accept-Encoding: identity``, so a compressed body cannot expand past
the count inside one chunk); ``text/html`` or ``text/plain`` only. The client
(:func:`make_client`) ignores the environment (``trust_env=False``: no proxy
variables and no ``.netrc`` credentials), follows no redirect by itself, and
carries no cookie from one hop to the next.

HTML becomes text through :func:`brain.ingest.notes_html.html_to_markdown`
(stdlib ``html.parser``, bounded; ``script``, ``style`` and ``img`` dropped).

Every failure is a :class:`WebFetchError` with a fixed message; a transport
error is named by class only (``describe_failure``), never by its text.

**Residual.** :func:`system_resolver` uses ``socket.getaddrinfo``, which has no
timeout of its own; the operating system's resolver timeout bounds it.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..errors import BrainError, describe_failure
from .notes_html import html_to_markdown
from .web_url import normalize_host

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "TIMEOUT_SECONDS",
    "USER_AGENT",
    "AddressPolicy",
    "Resolver",
    "WebFetchError",
    "fetch_page",
    "make_client",
    "public_only",
    "system_resolver",
]

#: Per network operation, and for the whole fetch.
TIMEOUT_SECONDS = 10.0
#: The most body bytes read; a bigger page keeps its title-and-URL body.
MAX_BODY_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "brain-ingest-web (local personal knowledge base)"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset({"text/html", "text/plain"})
_HTTPS_PORT = 443

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
#: ``(host, port)`` → the host's addresses as strings.
Resolver = Callable[[str, int], Sequence[str]]
#: ``True`` when a request may connect to the address.
AddressPolicy = Callable[[IPAddress], bool]

#: The fetch's clock (a seam, so a test can run the deadline without waiting).
_clock: Callable[[], float] = time.monotonic


class WebFetchError(BrainError):
    """A page could not be fetched; the message is fixed and quotes no response."""


class _Allowlist(Protocol):
    def allows(self, host: str) -> bool: ...


def public_only(address: IPAddress) -> bool:
    """Whether ``address`` is a public unicast address (module docstring, rule 3)."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def system_resolver(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, in the resolver's order, without repeats."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    addresses: list[str] = []
    for *_, sockaddr in answers:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The fetch client (module docstring); ``transport`` is the tests' seam."""
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


def fetch_page(
    url: str,
    allowlist: _Allowlist,
    *,
    client: httpx.Client,
    resolver: Resolver = system_resolver,
    policy: AddressPolicy = public_only,
) -> str:
    """The text of the page at ``url``; raises :class:`WebFetchError` (module docstring)."""
    deadline = _clock() + TIMEOUT_SECONDS
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        request = _pinned_request(client, current, allowlist, resolver, policy)
        try:
            response = client.send(request, stream=True)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise WebFetchError(f"fetch failed ({describe_failure(exc)})") from None
        try:
            if response.status_code in _REDIRECTS:
                location = response.headers.get("location")
                if not location:
                    raise WebFetchError("redirect without a location")
                current = urljoin(current, location)
                continue
            return _read_text(response, deadline)
        finally:
            response.close()
            client.cookies.clear()
    raise WebFetchError(f"too many redirects (more than {MAX_REDIRECTS})")


def _pinned_request(
    client: httpx.Client,
    url: str,
    allowlist: _Allowlist,
    resolver: Resolver,
    policy: AddressPolicy,
) -> httpx.Request:
    """A request for ``url`` that connects to a validated address (rules 1–4)."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise WebFetchError("refused: not https")
    try:
        port = parts.port or _HTTPS_PORT
    except ValueError:
        raise WebFetchError("refused: malformed port") from None
    host = normalize_host(parts.hostname or "")
    if host is None or not allowlist.allows(host):
        raise WebFetchError("refused: host not on the allowlist")
    answers = resolver(host, port)
    if not answers:
        raise WebFetchError("host did not resolve")
    address = _validated(answers, policy)
    literal = f"[{address}]" if address.version == 6 else str(address)
    suffix = "" if port == _HTTPS_PORT else f":{port}"
    target = urlunsplit(("https", f"{literal}{suffix}", parts.path or "/", parts.query, ""))
    return client.build_request(
        "GET",
        target,
        headers={
            "Host": f"{host}{suffix}",
            "Accept": "text/html, text/plain",
            "Accept-Encoding": "identity",
        },
        extensions={"sni_hostname": host},
    )


def _validated(answers: Sequence[str], policy: AddressPolicy) -> IPAddress:
    """The first address, once every address passes ``policy``."""
    addresses: list[IPAddress] = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer)
        except ValueError:
            raise WebFetchError("refused: address not public") from None
        if not policy(address):
            raise WebFetchError("refused: address not public")
        addresses.append(address)
    return addresses[0]


def _read_text(response: httpx.Response, deadline: float) -> str:
    if response.status_code != 200:
        raise WebFetchError(f"HTTP {response.status_code}")
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media not in _TEXT_TYPES:
        raise WebFetchError("unsupported content type")
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise WebFetchError(f"response larger than {MAX_BODY_BYTES} bytes")
            if _clock() > deadline:
                raise WebFetchError(f"fetch took longer than {TIMEOUT_SECONDS:g} s")
    except httpx.HTTPError as exc:
        raise WebFetchError(f"fetch failed ({describe_failure(exc)})") from None
    text = _decode(bytes(body), response.charset_encoding)
    return html_to_markdown(text).strip() if media == "text/html" else text.strip()


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
