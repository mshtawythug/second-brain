"""The `brain ui` Typer surface.

Nothing here binds a socket or blocks: ``serve`` and the browser opener are
injected, so the command's argument handling, gating and panel output are tested
without a server ever starting.

The sub-app is registered into a throwaway ``typer.Typer`` rather than imported
from ``brain.cli``. That is deliberate — ``cli.py`` is coordinator-owned and the
two-line registration has not landed yet, so testing through a local app proves
``register_ui_commands`` works and keeps these tests green either way.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from brain.cli_ui import register_ui_commands
from brain.ui.security import is_loopback
from brain.ui.server import DEFAULT_PORT, resolve_port

runner = CliRunner()


@pytest.fixture
def app() -> typer.Typer:
    root = typer.Typer()
    register_ui_commands(root)
    return root


def test_ui_command_is_registered(app: typer.Typer) -> None:
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--port",
        "--host",
        "--read-only",
        "--token",
        "--auto-port",
        "--include-confidential",
    ):
        assert flag in result.output


def test_help_does_not_need_a_database(app: typer.Typer) -> None:
    """`--help` must work on a machine with no Postgres running at all."""
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0


def test_non_loopback_without_a_token_is_a_usage_error(app: typer.Typer) -> None:
    """Exit 2, before any config load or bind.

    The corpus has no authentication; binding it to every interface without a
    shared secret would publish the user's entire brain to the local network.
    """
    result = runner.invoke(app, ["ui", "--host", "0.0.0.0", "--no-open"])
    assert result.exit_code == 2


def test_default_bind_is_loopback() -> None:
    from brain.cli_ui import ui

    defaults = ui.__defaults__ or ()
    assert any(
        getattr(d, "default", None) == "127.0.0.1" for d in defaults
    ), "the --host default must be 127.0.0.1"


def test_default_port_avoids_every_port_the_repo_uses() -> None:
    """55432 prod, 5434 test, 55433 demo, 8080 wiki/Caddy, 11434 Ollama."""
    assert DEFAULT_PORT == 8765
    assert DEFAULT_PORT not in {55432, 5434, 55433, 8080, 11434}


def test_loopback_detection_rejects_all_interfaces() -> None:
    assert is_loopback("127.0.0.1")
    assert not is_loopback("0.0.0.0")


def test_resolve_port_returns_a_free_port() -> None:
    assert resolve_port(DEFAULT_PORT + 4000, auto=True) >= DEFAULT_PORT + 4000


def test_resolve_port_bumps_past_a_busy_one() -> None:
    import socket

    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        assert resolve_port(busy, auto=True) != busy


def test_no_auto_port_refuses_with_the_remediation_text() -> None:
    import socket

    from brain.errors import BrainError

    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        with pytest.raises(BrainError) as excinfo:
            resolve_port(busy, auto=False)
    message = str(excinfo.value)
    assert "already in use" in message
    assert "--no-auto-port" in message
    assert "--port" in message


def test_missing_vault_exits_one(
    app: typer.Typer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing vault is a config problem, reported before anything binds."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "does-not-exist"))
    result = runner.invoke(app, ["ui", "--no-open"])
    assert result.exit_code == 1


def test_describe_db_never_leaks_credentials() -> None:
    """The panel prints dbname @ host:port — never the URL, never the password."""
    from brain.cli_ui import _describe_db

    described = _describe_db("postgresql://brain:sup3rs3cret@localhost:55432/second_brain")
    assert described == "second_brain @ localhost:55432"
    assert "sup3rs3cret" not in described
    assert "postgresql://" not in described


def test_importing_brain_cli_does_not_pull_starlette_or_uvicorn() -> None:
    """Import-cost discipline, asserted in a clean interpreter.

    ``cli_ui`` imports ``brain.ui.server`` — and through it starlette and
    uvicorn — only inside the command body. A module-scope import would add
    that cost to EVERY ``brain`` invocation, including ``brain --help``.
    Run in a subprocess because this test session has already imported both.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import brain.cli, sys;"
            "print('starlette' in sys.modules, 'uvicorn' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False", (
        f"brain.cli now imports a web dependency at module scope: {result.stdout!r}"
    )


def test_ui_tree_and_render_import_without_starlette_or_uvicorn() -> None:
    """``brain.ui.tree`` and ``brain.ui.render`` stay pure modules.

    ``brain/ui/__init__.py`` deliberately does not re-export ``create_app`` so
    that importing either module never drags in :mod:`brain.ui.app` — and
    through it ``starlette``. Same clean-interpreter shape as the test above:
    this session has already imported both web dependencies.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import brain.ui.tree, brain.ui.render, sys;"
            "print('starlette' in sys.modules, 'uvicorn' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False", (
        "brain.ui.tree / brain.ui.render now import a web dependency: "
        f"{result.stdout!r}"
    )


# --------------------------------------------------------------------------
# Phase 7 / Q1 — `--lan`, the wildcard-bind notice, and the opened URL
# --------------------------------------------------------------------------

LAN_IP = "192.0.2.44"  # TEST-NET-1 (RFC 5737): synthetic


class _Started:
    """What the command handed to the (replaced) server layer."""

    def __init__(self) -> None:
        self.context_kwargs: dict[str, object] = {}
        self.serve_kwargs: dict[str, object] = {}


@pytest.fixture
def started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Started:
    """Run `brain ui` up to the bind without binding anything.

    ``resolve_port``, ``build_context`` and ``serve`` are replaced with pytest
    ``monkeypatch`` (standard test doubles, CLAUDE.md rule 13). The command
    imports them from ``brain.ui.server`` inside its body, so replacing the
    module attributes is what it sees.
    """
    from types import SimpleNamespace

    import brain.ui.server as server

    record = _Started()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr(
        "brain.cli_ui._build_embedder", lambda cfg: object()
    )
    monkeypatch.setattr(server, "resolve_port", lambda port, **kw: port)

    def fake_build_context(cfg: object, **kwargs: object) -> object:
        record.context_kwargs = kwargs
        return SimpleNamespace(
            serve_confidential_bodies=False, notices=(), token=kwargs["token"]
        )

    def fake_serve(context: object, **kwargs: object) -> None:
        record.serve_kwargs = kwargs

    monkeypatch.setattr(server, "build_context", fake_build_context)
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(
        "brain.ui.lan.detect_lan_address", lambda *a, **k: LAN_IP
    )
    return record


def test_lan_binds_the_lan_address_with_a_minted_token(
    app: typer.Typer, started: _Started
) -> None:
    """E7: the convenience never skips the token rule — it mints one."""
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])

    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == LAN_IP
    token = started.context_kwargs["token"]
    assert isinstance(token, str) and len(token) >= 32, (
        "--lan must always run with a token; an empty one would bind the LAN "
        "unauthenticated"
    )
    assert f"http://{LAN_IP}:8765/#t={token}" in result.output
    assert started.serve_kwargs["host"] == LAN_IP


def test_lan_uses_a_supplied_token(app: typer.Typer, started: _Started) -> None:
    result = runner.invoke(
        app, ["ui", "--lan", "--token", "a-stable-secret", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == "a-stable-secret"


def test_lan_tells_the_user_to_open_the_url_on_another_device(
    app: typer.Typer, started: _Started
) -> None:
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert "on your phone" in result.output


def test_lan_and_host_together_is_a_usage_error(
    app: typer.Typer, started: _Started
) -> None:
    """Exit 2 before anything is resolved — two answers to one question."""
    result = runner.invoke(
        app, ["ui", "--lan", "--host", "192.0.2.9", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 2
    assert started.context_kwargs == {}


def test_lan_without_a_network_exits_one_with_the_override(
    app: typer.Typer, started: _Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain.ui.lan import LanAddressError

    def no_route(*args: object, **kwargs: object) -> str:
        raise LanAddressError("--lan found no network route\n  Pass --host …")

    monkeypatch.setattr("brain.ui.lan.detect_lan_address", no_route)
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert result.exit_code == 1
    assert started.context_kwargs == {}
    # M-3 (phase-7 audit): the remedy reaches the user, as a message, not a
    # traceback. Exit code 1 alone was satisfied by an uncaught exception too.
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Pass --host" in result.output, result.output


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
def test_wildcard_bind_warns_that_other_devices_are_refused(
    app: typer.Typer, started: _Started, wildcard: str
) -> None:
    """E9: the recipe the docs used to ship serves no phone; say so at startup.

    Every wildcard, F8 (phase-7 audit): with only ``0.0.0.0`` here, narrowing
    the check to ``== "0.0.0.0"`` survived. ``[::]`` proves the notice still
    fires after the CLI strips the brackets.
    """
    result = runner.invoke(
        app, ["ui", "--host", wildcard, "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert "--lan" in result.output
    assert "400" in result.output


def test_wildcard_bind_prints_a_loopback_url_not_the_bind_address(
    app: typer.Typer, started: _Started
) -> None:
    """L-6, at the panel: the printed URL is the one that works on this machine."""
    result = runner.invoke(
        app, ["ui", "--host", "0.0.0.0", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8765/#t=x" in result.output
    assert "http://0.0.0.0" not in result.output


def test_loopback_with_a_token_prints_the_tokened_url(
    app: typer.Typer, started: _Started
) -> None:
    """M-2 / rule 12 (phase-7 audit): the panel half of the ``#t=`` fix.

    Before ``5742c91`` the panel printed the fragment only for non-loopback
    binds, so ``--token`` on loopback printed a URL whose page 403'd. The
    serve half has its own test; this pins the panel.
    """
    result = runner.invoke(app, ["ui", "--token", "s3cret", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8765/#t=s3cret" in result.output


def test_the_phone_line_is_printed_only_for_lan(
    app: typer.Typer, started: _Started
) -> None:
    """L-13 (phase-7 audit): "open … on your phone" is wrong advice on loopback."""
    loopback = runner.invoke(app, ["ui", "--no-open"])
    lan = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert loopback.exit_code == 0 and lan.exit_code == 0
    assert "Phone" not in loopback.output, loopback.output
    assert "Phone" in lan.output, lan.output


def test_a_lan_bind_does_not_print_the_wildcard_warning(
    app: typer.Typer, started: _Started
) -> None:
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert "Host allowlist" not in result.output


def test_browser_url_carries_the_token_whenever_one_is_set() -> None:
    """The opened tab needs the fragment too, or every API call is a 403.

    Regression: ``serve`` opened ``http://host:port/`` with no ``#t=``, so a
    tokened server opened a page that could not load a single note, and the
    panel printed the fragment only for non-loopback binds.
    """
    from brain.ui.server import browser_url

    assert browser_url("127.0.0.1", 8765, "") == "http://127.0.0.1:8765/"
    assert browser_url(LAN_IP, 8766, "s3cret") == f"http://{LAN_IP}:8766/#t=s3cret"
    assert browser_url("127.0.0.1", 8765, "s3cret").endswith("/#t=s3cret")


@pytest.mark.parametrize(
    ("wildcard", "expected"),
    [("0.0.0.0", "http://127.0.0.1:8765/"), ("::", "http://[::1]:8765/")],
)
def test_browser_url_for_a_wildcard_bind_is_a_loopback_url(
    wildcard: str, expected: str
) -> None:
    """L-6: ``http://0.0.0.0:<port>/`` is not a URL a browser can use here.

    The wildcard is a bind address, not a destination; its Host allowlist is
    loopback names (ruling W1), so the opened page must be a loopback URL.
    """
    from brain.ui.server import browser_url

    assert browser_url(wildcard, 8765, "") == expected


def test_browser_url_brackets_an_ipv6_literal() -> None:
    """L-12 (phase-7 audit): ``http://::1:8765/`` is not a URL."""
    from brain.ui.server import browser_url

    assert browser_url("::1", 8765, "") == "http://[::1]:8765/"


def test_browser_url_percent_encodes_the_token() -> None:
    """L-3: ``api.js`` runs ``decodeURIComponent`` on the fragment.

    An unencoded ``%`` makes that throw (``URIError``) at module evaluation and
    the page never boots; ``#`` and spaces would truncate or mangle it.
    """
    from brain.ui.server import browser_url

    url = browser_url("127.0.0.1", 8765, "50% off #1")
    assert url == "http://127.0.0.1:8765/#t=50%25%20off%20%231"


@pytest.mark.parametrize("bind", ["127.0.0.1", LAN_IP])
def test_serve_opens_the_tokened_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind: str
) -> None:
    """``serve`` must hand the opener the same URL the panel prints.

    uvicorn's ``Server`` is replaced with a double whose ``run`` performs the
    startup hook and returns, so nothing binds.
    """
    import asyncio
    import contextlib
    from dataclasses import replace

    import uvicorn

    from brain.ui.context import UiContext
    from brain.ui.server import serve

    class _Server:
        def __init__(self, config: object) -> None:
            self.config = config

        async def startup(self, *args: object, **kwargs: object) -> None:
            return None

        def run(self) -> None:
            asyncio.run(self.startup())

    monkeypatch.setattr(uvicorn, "Server", _Server)

    @contextlib.contextmanager
    def conn_factory():  # type: ignore[no-untyped-def]
        yield None

    context = UiContext(
        cfg=object(),  # type: ignore[arg-type]
        conn_factory=conn_factory,
        embedder=object(),
        search_fn=lambda *a, **k: [],
        token="s3cret",
        allowed_origin=f"http://{bind}:8765",
        allowed_hosts=frozenset({bind}),
    )
    opened: list[str] = []
    # FTS-only shape, so the boot-time warm-up is skipped rather than faked.
    context = replace(context, embedder=None)
    serve(context, host=bind, port=8765, open_browser=True, opener=opened.append)

    assert opened == [f"http://{bind}:8765/#t=s3cret"]



def _ipv6_wildcard_available() -> bool:
    import socket
    from contextlib import closing

    try:
        with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
            sock.bind(("::", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(
    not _ipv6_wildcard_available(), reason="this host cannot bind the IPv6 wildcard"
)


@needs_ipv6
@pytest.mark.parametrize("wildcard", ["::"])
def test_resolve_port_finds_a_free_port_on_the_ipv6_wildcard(wildcard: str) -> None:
    """L-8 (phase-7 audit): the port probe bound ``::`` with an IPv4 socket.

    That always raised, so every port looked busy and ``brain ui --host ::``
    died with "no free port found" before the notice could print.
    """
    import socket
    from contextlib import closing

    with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
        sock.bind(("::", 0))
        free = sock.getsockname()[1]
    assert resolve_port(free, host=wildcard, auto=False) == free


@needs_ipv6
def test_resolve_port_sees_a_busy_ipv6_wildcard_port() -> None:
    """The fix must not make the probe always succeed for IPv6."""
    import socket

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        assert resolve_port(busy, host="::", auto=True) != busy



def test_the_probe_socket_is_ipv6_only_like_the_real_bind() -> None:
    """Review round 3, item 3: asyncio binds ``::`` with ``IPV6_V6ONLY=1``.

    A dual-stack probe (the OS default, ``0``) answers a different question
    from the bind that follows: on Linux it reports a port busy because an IPv4
    socket holds it, although the IPv6-only server would bind it fine. Asserted
    on the socket option itself, because the behavioural difference is
    Linux-only — macOS reports both cases free (measured 2026-10-13).
    """
    import socket

    from brain.ui.server import _probe_socket

    with _probe_socket("::") as sock:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    with _probe_socket("127.0.0.1") as sock:
        assert sock.family == socket.AF_INET


@pytest.mark.parametrize(
    ("given", "bound", "url"),
    [
        ("[::]", "::", "http://[::1]:8765/#t=x"),
        ("[::1]", "::1", "http://[::1]:8765/#t=x"),
    ],
)
def test_a_bracketed_host_is_bound_without_its_brackets(
    app: typer.Typer, started: _Started, given: str, bound: str, url: str
) -> None:
    """Review round 3, item 2: ``--host [::]`` passed the probe, printed a panel,
    then uvicorn died with ``gaierror 8``: brackets belong in URLs, never in a
    bind address. The host is normalised once, in the CLI; ``bind_origin``
    re-brackets it for the URL."""
    result = runner.invoke(app, ["ui", "--host", given, "--token", "x", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == bound
    assert started.serve_kwargs["host"] == bound
    assert url in result.output


def test_a_bracketed_loopback_needs_no_token(
    app: typer.Typer, started: _Started
) -> None:
    """``[::1]`` is loopback once normalised, exactly as ``::1`` is."""
    result = runner.invoke(app, ["ui", "--host", "[::1]", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "::1"


def test_the_ipv6_wildcard_notice_says_ipv4_devices_cannot_connect(
    app: typer.Typer, started: _Started
) -> None:
    """Review round 3, item 3: ``::`` is IPv6-only, so "refused with 400" is
    not the whole story — an IPv4 phone is refused at connect."""
    result = runner.invoke(app, ["ui", "--host", "::", "--token", "x", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "IPv6-only" in result.output, result.output
