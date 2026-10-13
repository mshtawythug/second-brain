"""Shared doubles for the `brain ui` test modules.

Importable as ``from tests.ui_cli_support import ...`` — the same shape as
``tests/backup_fakes.py``. Split out in review round 11, when
``tests/test_cli_ui.py`` grew past the 800-line ceiling and its host-validation
and socket-binding halves moved to ``tests/test_cli_ui_host.py`` and
``tests/test_ui_server_bind.py``. Fixtures stay in each test module (two lines
apiece, over :func:`make_app` and :func:`install_started`), so no module
imports a name that only pytest uses.
"""
from __future__ import annotations

import functools
import socket
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from brain.cli_ui import register_ui_commands

#: Rich draws usage errors and `--help` in a box wrapped at COLUMNS, so an
#: assertion on their text depends on the terminal it happens to run in —
#: measured in review round 8: four tests fail at COLUMNS=40 and at 60, all pass
#: at 200. Pinned wide for every invoke, as tests/test_cli_directory.py does
#: with monkeypatch.
runner = CliRunner(env={"COLUMNS": "200"})

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737): synthetic

#: The ONLY name resolution these tests see (review round 10): every CLI test
#: resolves hostnames through this table, never through real DNS — `.lan`
#: resolves on many home routers, and ISP NXDOMAIN hijacking answers the rest.
FAKE_DNS: dict[str, list[str]] = {
    "localhost": ["127.0.0.1", "::1"],
    "brain-1.lan": ["192.0.2.20"],
    # a public wildcard-DNS service answers this with 0.0.0.0 (round 10)
    "0.0.0.0.nip.io": ["0.0.0.0"],
    # IPv6-only, as Linux's /etc/hosts entry is (round 10, item 3)
    "ip6-localhost": ["::1"],
}


def fake_resolver(
    host: str, port: object, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
    """``socket.getaddrinfo``'s shape, answered from FAKE_DNS."""
    if host not in FAKE_DNS:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 0, 0, 0)) if ":" in a
        else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
        for a in FAKE_DNS[host]
    ]


class Started:
    """What the command handed to the (replaced) server layer."""

    def __init__(self) -> None:
        self.context_kwargs: dict[str, object] = {}
        self.serve_kwargs: dict[str, object] = {}
        #: ``getsockname()`` of each socket serve() was handed, taken while it
        #: was open — the CLI closes them once serve returns (round 11).
        self.bound: list[tuple[str, int]] = []


def make_app() -> typer.Typer:
    """A throwaway app with the `ui` sub-app registered, as ``cli.py`` does."""
    root = typer.Typer()
    register_ui_commands(root)
    return root


def install_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Started:
    """Run `brain ui` up to the bind without binding anything.

    ``claim_port``, ``build_context`` and ``serve`` are replaced with pytest
    ``monkeypatch`` (standard test doubles, CLAUDE.md rule 13). The command
    imports them from ``brain.ui.server`` inside its body, so replacing the
    module attributes is what it sees. A test that needs the REAL bind sets
    ``server.claim_port`` back to the real function.
    """
    import brain.ui.server as server

    record = Started()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr("brain.cli_ui._build_embedder", lambda cfg: object())
    monkeypatch.setattr(server, "claim_port", lambda port, **kw: (port, []))

    def fake_build_context(cfg: object, **kwargs: object) -> object:
        record.context_kwargs = kwargs
        return SimpleNamespace(
            serve_confidential_bodies=False, notices=(), token=kwargs["token"]
        )

    def fake_serve(context: object, **kwargs: object) -> None:
        record.serve_kwargs = kwargs
        sockets = kwargs["sockets"]
        assert isinstance(sockets, list)
        record.bound = [sock.getsockname()[:2] for sock in sockets]

    monkeypatch.setattr(server, "build_context", fake_build_context)
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(
        server, "resolve_bind_addresses",
        functools.partial(server.resolve_bind_addresses, resolver=fake_resolver),
    )
    monkeypatch.setattr("brain.ui.lan.detect_lan_address", lambda *a, **k: LAN_IP)
    return record


def ipv6_wildcard_available() -> bool:
    try:
        with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
            sock.bind(("::", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(
    not ipv6_wildcard_available(), reason="this host cannot bind the IPv6 wildcard"
)


def ipv6_loopback_available() -> bool:
    """Can this host bind ``::1``? Not the same question as the wildcard's.

    Review round 15b: Linux with IPv6 disabled on ``lo``
    (``net.ipv6.conf.lo.disable_ipv6=1``) binds ``::`` but has no ``::1``, so a
    test that binds ``::1`` under ``needs_ipv6`` ERRORED there instead of
    skipping.
    """
    try:
        with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


#: For a test that binds ``::1`` itself (round 15b).
needs_ipv6_loopback = pytest.mark.skipif(
    not ipv6_loopback_available(), reason="this host has no IPv6 loopback (::1)"
)
