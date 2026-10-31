"""The opt-in page fetch of `brain ingest-web --fetch` (RM-41): the one network egress.

A page's text is downloaded only when the owner passes ``--fetch``, and only
under these rules, each checked at **every** hop (the first request and each
redirect):

1. **https only.** An http URL or redirect is refused.
2. **Allowlisted host.** The hop's normalized host must be on the allowlist; a
   redirect off it is refused (acceptance criterion 5).
3. **Public addresses only** (amendment A1, sec-114 F9). The host is resolved
   through the injected :data:`Resolver`, and the hop is refused when ANY address
   fails the injected :data:`AddressPolicy`. The default, :func:`public_only`, is
   an explicit deny list that does not depend on the interpreter's ``ipaddress``
   tables. It checks the IPv4 address embedded in v4-mapped and well-known NAT64
   (``64:ff9b::/96``) addresses, and refuses the deprecated 6to4 (``2002::/16``)
   and Teredo (``2001::/32``) tunnels outright, whatever they embed (team-lead
   ruling on sec-114 F9). Local-use NAT64 (``64:ff9b:1::/48``, whose layout the
   local operator chooses) is never decoded, so it falls outside global unicast
   and is refused too (sec-114 R4).
4. **Connect to what was validated.** The request goes to the IP literal; the
   name travels in the ``Host`` header and in httpcore's ``sni_hostname``
   request extension, which sets the TLS server name and so the certificate
   check. There is no second DNS lookup between the check and the connect, so
   DNS rebinding cannot swap in another address. Each fetch gets a fresh client
   that keeps no connection alive (sec-114 F13), so a second host on the same
   address gets its own TLS handshake and certificate check.

And per fetch:

- **One wall-clock deadline** (sec-114 F4). The whole fetch, every hop's
  connect, headers and body, runs in a worker thread that the caller waits on for
  :data:`TIMEOUT_SECONDS`; each hop also gets the time that remains as its httpx
  timeout. A server that trickles its headers can hold only the abandoned worker
  (a daemon thread, one socket, httpcore's bounded header buffer) until it stops
  or the process exits; the run moves on. Such a timeout raises
  :class:`WebFetchStuckError`, so ``brain ingest-web`` can stop fetching once a
  few workers are stuck (sec-114 R1). The socket is NOT shut down on timeout:
  ``httpx.HTTPTransport`` takes no network backend, so the only way to reach the
  socket is through httpx's and httpcore's private attributes, which this module
  does not touch.
- **The body is read raw** (sec-114 F1). A response with any ``Content-Encoding``
  other than ``identity`` is refused, and the bytes counted against
  :data:`MAX_BODY_BYTES` are the bytes on the wire (``iter_raw``), so no
  decompressor ever runs. ``Accept-Encoding: identity`` asks for that, but a server
  can ignore the request; the refusal is what holds.
- **The server does not choose the decoder** (sec-114 F7). Its charset is used only
  when it names an ordinary text encoding (:data:`_TEXT_CODECS`); anything else,
  ``punycode`` or ``unicode_escape`` included, decodes as UTF-8 with replacement.
- At most :data:`MAX_REDIRECTS` redirects; ``text/html`` or ``text/plain`` only;
  ``trust_env=False`` (no proxy variables, no ``.netrc`` credentials); no cookie
  carried from one hop to the next.

HTML becomes text through :func:`brain.ingest.notes_html.html_to_markdown`
(stdlib ``html.parser``, bounded; ``script``, ``style`` and ``img`` dropped).

Every failure is a :class:`WebFetchError` with a fixed message; a transport
error is named by class only (``describe_failure``), never by its text.

**Residual.** :func:`system_resolver` uses ``socket.getaddrinfo``, which has no
timeout of its own; the operating system's resolver bounds it.
"""
from __future__ import annotations

import codecs
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..errors import BrainError, describe_failure
from .notes_html import html_to_markdown
from .web_url import normalize_host

__all__ = [
    "CONNECTION_LIMITS",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "TIMEOUT_SECONDS",
    "USER_AGENT",
    "AddressPolicy",
    "ClientFactory",
    "Resolver",
    "WebFetchError",
    "WebFetchStuckError",
    "fetch_page",
    "make_client",
    "public_only",
    "system_resolver",
]

#: The whole fetch, every hop included.
TIMEOUT_SECONDS = 10.0
#: The most body bytes read, as sent; a bigger page keeps its title-and-URL body.
MAX_BODY_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "brain-ingest-web (local personal knowledge base)"
#: No connection outlives its request, so every host is verified on its own (F13).
CONNECTION_LIMITS = httpx.Limits(max_connections=1, max_keepalive_connections=0)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset({"text/html", "text/plain"})
_HTTPS_PORT = 443

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
#: ``(host, port)`` → the host's addresses as strings.
Resolver = Callable[[str, int], Sequence[str]]
#: ``True`` when a request may connect to the address.
AddressPolicy = Callable[[IPAddress], bool]
#: A new client for one fetch; tests pass one built on ``httpx.MockTransport``.
ClientFactory = Callable[[], httpx.Client]

#: The fetch's clock (a seam, so a test can expire the deadline between hops).
_clock: Callable[[], float] = time.monotonic

#: Every IPv4 range that is not a public unicast destination (sec-114 F9).
_DENIED_V4 = tuple(
    ipaddress.IPv4Network(net)
    for net in (
        "0.0.0.0/8",  # this network
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # shared address space (CGNAT)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local (cloud metadata)
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.88.99.0/24",  # 6to4 relay anycast (deprecated)
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, and the broadcast address
    )
)
#: Global unicast IPv6; everything outside it is refused (loopback, ULA, link-local,
#: site-local ``fec0::/10``, multicast, unspecified, discard ``100::/64``, ``::/96``).
_GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")
#: Ranges inside global unicast that are still not public destinations.
_DENIED_V6 = tuple(
    ipaddress.IPv6Network(net)
    for net in (
        "2001::/32",  # Teredo (deprecated tunnel): refused whatever it embeds
        "2001:2::/48",  # benchmarking
        "2001:10::/28",  # ORCHID (deprecated)
        "2001:20::/28",  # ORCHIDv2
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (deprecated tunnel): refused whatever it embeds
        "3fff::/20",  # documentation (RFC 9637)
    )
)
_V4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

#: The charsets a server may name (after ``codecs.lookup`` normalization).
_TEXT_CODECS = frozenset(
    {
        "ascii", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32",
        "utf-32-le", "utf-32-be", "shift_jis", "cp932", "euc_jp", "euc_kr",
        "gb2312", "gbk", "gb18030", "big5", "koi8-r", "koi8-u",
    }
)
_TEXT_CODEC_FAMILIES = re.compile(r"^(iso8859-\d+|cp125\d)$")


class WebFetchError(BrainError):
    """A page could not be fetched; the message is fixed and quotes no response."""


class WebFetchStuckError(WebFetchError):
    """The fetch passed its deadline and its worker is still blocked on the server."""


class _Allowlist(Protocol):
    def allows(self, host: str) -> bool: ...


def public_only(address: IPAddress) -> bool:
    """Whether ``address`` is a public unicast destination (module docstring, rule 3)."""
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in net for net in _DENIED_V4)
    embedded = _embedded_v4(address)
    if embedded is not None:
        return all(public_only(v4) for v4 in embedded)
    return address in _GLOBAL_V6 and not any(address in net for net in _DENIED_V6)


def _embedded_v4(address: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...] | None:
    """The IPv4 address a v4-mapped or well-known NAT64 address carries, else ``None``.

    Only ``64:ff9b::/96``: a local-use ``64:ff9b:1::/48`` address has no fixed layout
    (RFC 8215), so it is not decoded and :func:`public_only` refuses it.
    """
    value = int(address)
    if address in _V4_MAPPED or address in _NAT64:
        return (ipaddress.IPv4Address(value & 0xFFFFFFFF),)
    return None


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


def make_client(
    transport: httpx.BaseTransport | None = None, *, verify: ssl.SSLContext | bool = True
) -> httpx.Client:
    """A client for one fetch (module docstring).

    ``transport`` and ``verify`` are test seams: a MockTransport, or a context that
    trusts a synthetic test CA. Production passes neither, so certificates are
    checked against the system's trust store.
    """
    return httpx.Client(
        verify=verify,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        limits=CONNECTION_LIMITS,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


def fetch_page(
    url: str,
    allowlist: _Allowlist,
    *,
    client_factory: ClientFactory = make_client,
    resolver: Resolver = system_resolver,
    policy: AddressPolicy = public_only,
) -> str:
    """The text of the page at ``url``; raises :class:`WebFetchError` (module docstring).

    The fetch runs in a worker thread that is given :data:`TIMEOUT_SECONDS` in all;
    past it the worker is abandoned and the fetch fails.
    """
    deadline = _clock() + TIMEOUT_SECONDS
    outcome: dict[str, str] = {}

    def work() -> None:
        try:
            outcome["text"] = _fetch(url, allowlist, client_factory, resolver, policy, deadline)
        except WebFetchError as exc:
            outcome["error"] = str(exc)
        except Exception as exc:  # any failure costs this page's text only
            outcome["error"] = f"fetch failed ({describe_failure(exc)})"

    worker = threading.Thread(target=work, name="brain-web-fetch", daemon=True)
    worker.start()
    worker.join(TIMEOUT_SECONDS)
    if worker.is_alive():
        raise WebFetchStuckError(f"fetch took longer than {TIMEOUT_SECONDS:g} s")
    if "text" not in outcome:
        # A worker ended by a BaseException (SystemExit) leaves no outcome at all.
        raise WebFetchError(outcome.get("error", "fetch failed (no result)"))
    return outcome["text"]


def _fetch(
    url: str,
    allowlist: _Allowlist,
    client_factory: ClientFactory,
    resolver: Resolver,
    policy: AddressPolicy,
    deadline: float,
) -> str:
    current = url
    with client_factory() as client:
        for _hop in range(MAX_REDIRECTS + 1):
            remaining = deadline - _clock()
            if remaining <= 0:
                raise WebFetchError(f"fetch took longer than {TIMEOUT_SECONDS:g} s")
            request = _pinned_request(client, current, allowlist, resolver, policy, remaining)
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
    remaining: float,
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
        timeout=httpx.Timeout(min(TIMEOUT_SECONDS, remaining)),
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
    codings = [
        coding.strip().lower()
        for coding in response.headers.get_list("content-encoding", split_commas=True)
        if coding.strip()
    ]
    if any(coding != "identity" for coding in codings):
        raise WebFetchError("unsupported content encoding")
    body = bytearray()
    try:
        for chunk in response.iter_raw():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise WebFetchError(f"response larger than {MAX_BODY_BYTES} bytes")
            if _clock() > deadline:
                raise WebFetchError(f"fetch took longer than {TIMEOUT_SECONDS:g} s")
    except httpx.HTTPError as exc:
        raise WebFetchError(f"fetch failed ({describe_failure(exc)})") from None
    text = bytes(body).decode(_text_codec(response.charset_encoding), errors="replace")
    return html_to_markdown(text).strip() if media == "text/html" else text.strip()


def _text_codec(charset: str | None) -> str:
    """The codec for the server's ``charset`` when it is a text encoding, else UTF-8."""
    if not charset:
        return "utf-8"
    try:
        name = codecs.lookup(charset).name
    except LookupError:
        return "utf-8"
    if name in _TEXT_CODECS or _TEXT_CODEC_FAMILIES.match(name):
        return name
    return "utf-8"
