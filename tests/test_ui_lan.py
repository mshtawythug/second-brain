"""`brain ui --lan`: finding the LAN address and minting the token.

The routing probe is injected, so no test here depends on the machine's real
network. The one test that runs the real probe aims it at loopback, which every
host routes and which sends no packet.
"""
from __future__ import annotations

import pytest

from brain.errors import BrainError
from brain.ui.context import WILDCARD_HOSTS
from brain.ui.lan import (
    LanAddressError,
    detect_lan_address,
    new_token,
    udp_route_probe,
)

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737): synthetic, never a real host


def _probe_returning(address: str):  # type: ignore[no-untyped-def]
    def probe(target: tuple[str, int]) -> str:
        return address

    return probe


def test_detects_the_address_the_route_probe_reports() -> None:
    assert detect_lan_address(_probe_returning(LAN_IP)) == LAN_IP


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "0.0.0.0", "::"])
def test_a_loopback_or_unspecified_answer_is_refused(address: str) -> None:
    """No network: the probe routes via loopback, and that is not a LAN address.

    Binding it would serve only this machine while the banner told the user to
    open it on their phone.
    """
    with pytest.raises(LanAddressError) as excinfo:
        detect_lan_address(_probe_returning(address))
    assert "--host" in str(excinfo.value)


def test_a_probe_that_cannot_route_is_reported_with_the_manual_override() -> None:
    def unroutable(target: tuple[str, int]) -> str:
        raise OSError(51, "Network is unreachable")

    with pytest.raises(LanAddressError) as excinfo:
        detect_lan_address(unroutable)
    message = str(excinfo.value)
    assert "no network route" in message
    assert "--host" in message


def test_a_non_address_answer_is_refused() -> None:
    with pytest.raises(LanAddressError):
        detect_lan_address(_probe_returning("not-an-address"))


def test_lan_address_error_is_a_brain_error() -> None:
    """So `brain ui` reports it with its usual red line and exit 1."""
    assert issubclass(LanAddressError, BrainError)


def test_the_real_probe_reads_the_local_end_of_a_route() -> None:
    """The LOCAL end, which must differ from the target to prove anything.

    F14 (phase-7 audit): aimed at ``127.0.0.1``, local and target are equal, so
    a probe returning its own target instead of ``getsockname()`` passed.
    ``127.0.0.2`` routes over loopback with source ``127.0.0.1`` (measured on
    macOS 2026-10-13; Linux's ``local 127.0.0.0/8`` route also uses
    ``src 127.0.0.1``), and no packet is sent.
    """
    assert udp_route_probe(("127.0.0.2", 9)) == "127.0.0.1"


def test_minted_tokens_are_long_and_distinct() -> None:
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert min(len(token) for token in tokens) >= 32


def test_wildcard_hosts_names_both_wildcards_and_no_loopback() -> None:
    assert {"0.0.0.0", "::", "[::]"} == WILDCARD_HOSTS
    assert "127.0.0.1" not in WILDCARD_HOSTS


def test_the_route_target_is_a_documentation_address_off_the_lan() -> None:
    """Round 7, item 2: every other test injects a probe that ignores its target.

    So ``_ROUTE_TARGET = ("127.0.0.1", 9)`` survived everything, and on a real
    machine ``--lan`` would then always refuse with "found only 127.0.0.1":
    a loopback target routes over loopback. The target must be an address the
    OS routes via the default route — off-machine, off the LAN — and one that
    names no real host: a documentation range (RFC 5737).
    """
    import ipaddress

    seen: list[tuple[str, int]] = []

    def recording(target: tuple[str, int]) -> str:
        seen.append(target)
        return LAN_IP

    detect_lan_address(recording)
    ((host, _port),) = seen
    address = ipaddress.ip_address(host)
    documentation = [
        ipaddress.ip_network(net)
        for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
    ]
    rfc1918 = [
        ipaddress.ip_network(net)
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    assert any(address in net for net in documentation), address
    assert not (address.is_loopback or address.is_link_local or address.is_unspecified)
    assert not any(address in net for net in rfc1918), address


def test_the_wildcard_notice_says_the_token_is_the_guard() -> None:
    """Round 7, item 3: the Host allowlist stops browsers, not programs.

    A LAN client can send ``Host: 127.0.0.1:<port>`` by hand and is served if
    it holds the token (measured by the auditor). The notice must not imply
    the bind keeps others out.
    """
    from brain.ui.lan import WILDCARD_NOTICE

    assert "token" in WILDCARD_NOTICE, WILDCARD_NOTICE
    assert "other devices cannot use this bind" not in WILDCARD_NOTICE
