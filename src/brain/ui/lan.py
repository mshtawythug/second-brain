"""`brain ui --lan`: this machine's LAN address, and the token that guards it.

Spec Q1 (answered 2026-10-12) replaced the wiki's zero-config phone reading with
a documented ``brain ui --host <ip> --token <secret>``. ``--lan`` is the
convenience over that recipe, and it exists because the obvious recipe does not
work: ``--host 0.0.0.0`` gets a loopback-only Host allowlist
(:func:`brain.ui.context.host_allowlist`), so a phone's request is refused with
400. Binding the LAN address itself makes the allowlist and the allowed Origin
that address. See ``docs/guides/brain-ui-on-your-phone.md``.

Nothing here widens the security model: ``--lan`` always runs with a token, and
this module never touches the allowlist.
"""
from __future__ import annotations

import ipaddress
import secrets
import socket
from collections.abc import Callable
from contextlib import closing

from ..errors import BrainError

#: The wildcard binds. They are not loopback, so they need a token, but they
#: also cannot serve another device (see the module docstring), which is the
#: trap the startup notice below warns about.
ALL_INTERFACES: frozenset[str] = frozenset({"0.0.0.0", "::", "[::]"})  # noqa: S104

ALL_INTERFACES_NOTICE = (
    "other devices cannot use this bind: the Host allowlist for a wildcard "
    "address is loopback-only, so a phone is refused with 400. Use --lan, or "
    "--host <this machine's LAN address>."
)

#: TEST-NET-1 (RFC 5737). ``connect()`` on a UDP socket only consults the
#: routing table, and sends nothing. The address is reserved, so the probe never
#: names a real host.
_ROUTE_TARGET = ("192.0.2.1", 9)

#: ``secrets.token_urlsafe(24)`` gives 32 characters (192 bits).
_TOKEN_BYTES = 24

RouteProbe = Callable[[tuple[str, int]], str]


class LanAddressError(BrainError):
    """``--lan`` could not find an address another device can reach."""


def udp_route_probe(target: tuple[str, int]) -> str:
    """Return the local address the OS would use to reach ``target``."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        sock.connect(target)
        return str(sock.getsockname()[0])


def detect_lan_address(probe: RouteProbe = udp_route_probe) -> str:
    """The primary IPv4 address of this machine, or :class:`LanAddressError`.

    A loopback or unspecified answer is refused rather than returned: it means
    there is no network, and binding it would serve only this machine while the
    startup panel told the user to open the URL on their phone.
    """
    remedy = "  Pass --host <this machine's LAN address> instead of --lan."
    try:
        found = probe(_ROUTE_TARGET)
    except OSError as exc:
        raise LanAddressError(
            f"--lan found no network route ({exc.strerror or exc})\n{remedy}"
        ) from exc
    try:
        address = ipaddress.ip_address(found)
    except ValueError as exc:
        raise LanAddressError(
            f"--lan got an unusable address {found!r}\n{remedy}"
        ) from exc
    if address.is_loopback or address.is_unspecified:
        raise LanAddressError(
            f"--lan found only {address}, which no other device can reach\n{remedy}"
        )
    return str(address)


def new_token() -> str:
    """A fresh shared secret for one ``brain ui --lan`` run."""
    return secrets.token_urlsafe(_TOKEN_BYTES)
