"""``brain mcp serve``'s bind policy and option checks (RM-46, T1).

Classification is by the ADDRESS a host resolves to, once (``brain ui``'s
review rounds 10-16): the resolver is injected, so no test touches DNS and no
socket is opened. Addresses are from the documentation ranges.
"""
from __future__ import annotations

import socket
from dataclasses import replace
from typing import Any

import pytest

from brain.errors import McpTransportError
from brain.mcp_auth import TokenInfo, fingerprint
from brain.mcp_http import HttpServeOptions, validate_options

pytestmark = pytest.mark.nodb

SECRET = "A" * 43
TOKEN = TokenInfo(
    path=None, secret=SECRET, mode_octal="", mode_ok=True, fingerprint=fingerprint(SECRET)
)


def _resolves_to(*addresses: str) -> Any:
    def resolver(host: str, *_: Any) -> list[tuple[Any, ...]]:
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    return resolver


def _opts(**overrides: Any) -> HttpServeOptions:
    return replace(HttpServeOptions(token=TOKEN), **overrides)


def test_defaults_are_loopback_read_only_and_confidential_excluded() -> None:
    opts = HttpServeOptions()

    assert opts.host == "127.0.0.1"
    assert opts.port == 8787
    assert opts.path == "/mcp"
    assert opts.allow_writes is False
    assert opts.include_confidential is False
    assert opts.allow_remote is False
    assert opts.no_auth is False


def test_loopback_with_a_token_is_accepted() -> None:
    plan = validate_options(_opts())

    assert plan.kind == "loopback"
    assert plan.host == "127.0.0.1"
    assert {"127.0.0.1", "localhost", "[::1]"} <= plan.allowed_hosts


def test_loopback_with_no_auth_is_accepted() -> None:
    plan = validate_options(_opts(token=None, no_auth=True))

    assert plan.kind == "loopback"


def test_loopback_with_no_token_and_no_no_auth_is_refused() -> None:
    with pytest.raises(McpTransportError, match="brain mcp token --rotate"):
        validate_options(_opts(token=None))


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10"])
def test_refuses_non_loopback_without_allow_remote(host: str) -> None:
    with pytest.raises(McpTransportError) as excinfo:
        validate_options(_opts(host=host))

    message = str(excinfo.value)
    assert f"refusing to bind {host}" in message
    assert "--allow-remote" in message
    assert "brain mcp token --rotate" in message
    assert "port-forward" in message


def test_refuses_non_loopback_with_allow_remote_but_no_token() -> None:
    with pytest.raises(McpTransportError, match="token is mandatory"):
        validate_options(_opts(host="0.0.0.0", allow_remote=True, token=None))


def test_refuses_no_auth_off_loopback_even_with_allow_remote() -> None:
    with pytest.raises(McpTransportError, match="--no-auth"):
        validate_options(_opts(host="0.0.0.0", allow_remote=True, no_auth=True))


def test_accepts_non_loopback_with_allow_remote_and_a_token() -> None:
    plan = validate_options(_opts(host="192.0.2.10", allow_remote=True))

    assert plan.kind == "other"
    assert plan.allowed_hosts == frozenset({"192.0.2.10"})


def test_a_wildcard_bind_only_trusts_loopback_hosts_unless_told() -> None:
    plan = validate_options(
        _opts(
            host="0.0.0.0",
            allow_remote=True,
            allowed_hosts=("brain.tailnet.example.com", "2001:db8::5"),
        )
    )

    assert plan.kind == "wildcard"
    assert "brain.tailnet.example.com" in plan.allowed_hosts
    assert "[2001:db8::5]" in plan.allowed_hosts
    assert "127.0.0.1" in plan.allowed_hosts


def test_a_localhost_that_resolves_elsewhere_is_not_loopback() -> None:
    with pytest.raises(McpTransportError, match="--allow-remote"):
        validate_options(_opts(host="localhost"), resolver=_resolves_to("192.0.2.1"))


def test_a_localhost_that_resolves_to_loopback_is_loopback() -> None:
    plan = validate_options(_opts(host="localhost"), resolver=_resolves_to("127.0.0.1"))

    assert plan.kind == "loopback"


def test_another_name_that_resolves_to_loopback_needs_the_remote_rules() -> None:
    # Whoever controls that name's DNS could re-point it (DNS rebinding).
    with pytest.raises(McpTransportError, match="--allow-remote"):
        validate_options(_opts(host="alias.example.com"), resolver=_resolves_to("127.0.0.1"))


def test_a_name_resolving_to_a_wildcard_is_a_wildcard() -> None:
    with pytest.raises(McpTransportError, match="--allow-remote"):
        validate_options(_opts(host="any.example.com"), resolver=_resolves_to("0.0.0.0"))


def test_127_0_0_2_is_loopback_by_address() -> None:
    plan = validate_options(_opts(host="127.0.0.2"))

    assert plan.kind == "loopback"
    assert "127.0.0.2" in plan.allowed_hosts


def test_an_unresolvable_host_is_refused() -> None:
    def failing(host: str, *_: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "synthetic")

    with pytest.raises(McpTransportError, match="does not resolve"):
        validate_options(_opts(host="nowhere.example.com"), resolver=failing)


@pytest.mark.parametrize("host", ["", " 127.0.0.1", "http://192.0.2.1", "127.0.0.1:8787", "[::1"])
def test_a_malformed_host_is_refused(host: str) -> None:
    with pytest.raises(McpTransportError):
        validate_options(_opts(host=host))


def test_a_loose_token_file_is_refused_before_binding() -> None:
    loose = replace(TOKEN, mode_ok=False, mode_octal="0644")

    with pytest.raises(McpTransportError, match="chmod 600"):
        validate_options(_opts(token=loose))


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_a_port_out_of_range_is_refused(port: int) -> None:
    with pytest.raises(McpTransportError, match="--port"):
        validate_options(_opts(port=port))


@pytest.mark.parametrize("port", [1, 8787, 65535])
def test_a_port_in_range_is_accepted(port: int) -> None:
    assert validate_options(_opts(port=port)).kind == "loopback"


@pytest.mark.parametrize(
    "path", ["mcp", "/", "/healthz", "/readyz", "/a b", "/" + "a" * 65, "/x?y"]
)
def test_a_bad_endpoint_path_is_refused(path: str) -> None:
    with pytest.raises(McpTransportError, match="--path"):
        validate_options(_opts(path=path))


@pytest.mark.parametrize("path", ["/mcp", "/brain/v1_mcp-x"])
def test_a_good_endpoint_path_is_accepted(path: str) -> None:
    assert validate_options(_opts(path=path)).kind == "loopback"


@pytest.mark.parametrize(
    "extra", ["*", "*.example.com", "https://brain.example.com", "brain.example.com/mcp",
              "0.0.0.0", "brain.example.com:8787", ""]
)
def test_a_bad_allowed_host_is_refused(extra: str) -> None:
    with pytest.raises(McpTransportError, match="--allowed-host"):
        validate_options(_opts(allowed_hosts=(extra,)))


@pytest.mark.parametrize("size", [1023, 16 * 1024 * 1024 + 1])
def test_a_body_cap_out_of_range_is_refused(size: int) -> None:
    with pytest.raises(McpTransportError, match="--max-body-bytes"):
        validate_options(_opts(max_body_bytes=size))


@pytest.mark.parametrize("seconds", [0.5, 3601.0])
def test_a_call_timeout_out_of_range_is_refused(seconds: float) -> None:
    with pytest.raises(McpTransportError, match="--call-timeout"):
        validate_options(_opts(call_timeout_s=seconds))


def test_an_unknown_log_level_is_refused() -> None:
    with pytest.raises(McpTransportError, match="--log-level"):
        validate_options(_opts(log_level="CHATTY"))
