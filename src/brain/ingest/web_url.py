"""URL normalization for `brain ingest-web` (RM-41): the one canonical form a page is stored under.

A page is deduplicated by ``(web, <normalized URL>)``, so this function decides
both what is stored and which visits count as the same page. It keeps the
scheme (http or https only), the host and a non-default port, and the path. It
drops everything else, because everything else is where secrets live (ticket
protocol point 6):

- the **query string** and the **fragment** (session tokens, search terms);
- the **userinfo** (``user:password@``), which a browser can record verbatim;
- every ``;`` **path parameter** (``/a;jsessionid=…/b`` → ``/a/b``): servlet and SSO
  stacks put a session id there when cookies are off (sec-114 F3);
- the default port, and a trailing dot on the host.

The host is lowercased and IDNA-encoded, so ``Bücher.Example.com`` and
``xn--bcher-kva.example.com`` are one host for the allowlist and for dedup.
Anything this module cannot put in that form normalizes to ``None`` and is
never stored: another scheme (``file:``, ``chrome:``, ``javascript:`` …), a
control or whitespace character anywhere, a malformed host or port, or a result
longer than :data:`MAX_URL_CHARS`.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

__all__ = ["MAX_URL_CHARS", "host_of", "is_numeric_host", "normalize_host", "normalize_url"]

#: The longest normalized URL stored; a longer one is not a page anybody revisits.
MAX_URL_CHARS = 2048
_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_HOST_CHARS = 253
_HEX = frozenset("0123456789abcdef")


def normalize_host(host: str) -> str | None:
    """``host`` lowercased, IDNA-encoded and without a trailing dot; ``None`` if malformed.

    An IP literal is returned in its compressed form (``2001:db8::1``).
    """
    name = host.strip()
    if name.endswith("."):
        name = name[:-1]
    if not name:
        return None
    try:
        return ipaddress.ip_address(name).compressed
    except ValueError:
        pass
    try:
        encoded = name.lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(encoded) > _MAX_HOST_CHARS:
        return None
    if not all(_LABEL.match(label) for label in encoded.split(".")):
        return None
    return encoded


def normalize_url(raw: str) -> str | None:
    """The canonical form of ``raw`` (module docstring), or ``None`` when it has none."""
    if len(raw) > 4 * MAX_URL_CHARS or _has_unsafe_char(raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES or not parts.hostname:
        return None
    host = normalize_host(parts.hostname)
    if host is None:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = "/".join(segment.split(";", 1)[0] for segment in parts.path.split("/"))
    url = f"{scheme}://{netloc}{path or '/'}"
    return url if len(url) <= MAX_URL_CHARS else None


def is_numeric_host(host: str) -> bool:
    """Whether ``host`` is an address rather than a name (sec-114 F8).

    An IP literal in any form ``ipaddress`` reads, or a host whose last label is a
    number, which is WHATWG's "ends in a number" rule: ``127.1``, ``2130706433`` and
    ``0x7f.1`` are all IPv4 addresses to a browser and a resolver.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    last = host.rstrip(".").rsplit(".", 1)[-1].lower()
    if last.isascii() and last.isdigit():
        return True
    return last.startswith("0x") and all(ch in _HEX for ch in last[2:])


def host_of(url: str) -> str:
    """The normalized host of ``url``; ``""`` when it has none."""
    hostname = urlsplit(url).hostname
    return (normalize_host(hostname) or "") if hostname else ""


def _has_unsafe_char(text: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in text)
