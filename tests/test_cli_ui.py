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

from brain.cli_ui import PHONE_LINE, register_ui_commands
from brain.ui.lan import WILDCARD_NOTICE
from brain.ui.security import is_loopback
from brain.ui.server import DEFAULT_PORT, resolve_port

#: Rich draws usage errors and `--help` in a box wrapped at COLUMNS, so an
#: assertion on their text depends on the terminal it happens to run in —
#: measured in review round 8: four tests fail at COLUMNS=40 and at 60, all pass
#: at 200. Pinned wide for every invoke in this module, as
#: tests/test_cli_directory.py does with monkeypatch.
runner = CliRunner(env={"COLUMNS": "200"})


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
    import socket

    if host not in FAKE_DNS:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 0, 0, 0)) if ":" in a
        else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
        for a in FAKE_DNS[host]
    ]


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
    import functools

    monkeypatch.setattr(
        server, "resolve_bind_addresses",
        functools.partial(server.resolve_bind_addresses, resolver=fake_resolver),
    )
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
    # SystemExit is the no-traceback check. A `"Traceback" not in output`
    # assertion stood here until round 6 and was vacuous: CliRunner keeps an
    # uncaught exception in `result.exception` and never writes its traceback
    # into `output`, so that line passed whatever happened.
    assert isinstance(result.exception, SystemExit), result.exception
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
    assert WILDCARD_NOTICE in result.output, result.output


def test_wildcard_bind_prints_a_loopback_url_not_the_bind_address(
    app: typer.Typer, started: _Started
) -> None:
    """L-6, at the panel: the printed URL is the one that works on this machine."""
    result = runner.invoke(
        app, ["ui", "--host", "0.0.0.0", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    # Negative FIRST, so the revert (printing the bind address) goes red here
    # rather than at the positive check below — round 6 found negatives that
    # had only ever been reached after a positive one had already failed.
    assert "http://0.0.0.0" not in result.output, result.output
    assert "http://127.0.0.1:8765/#t=x" in result.output


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
    # The real line, by constant: a negative check must name text the
    # positive path actually prints, or it passes whatever happens (round 6).
    assert PHONE_LINE in lan.output, lan.output
    assert PHONE_LINE not in loopback.output, loopback.output


@pytest.mark.parametrize("args", [["--lan"], []], ids=["lan", "loopback"])
def test_a_non_wildcard_bind_does_not_print_the_wildcard_warning(
    app: typer.Typer, started: _Started, args: list[str]
) -> None:
    """Round 6, item 1: this asserted ``"Host allowlist" not in output`` — a
    phrase the notice had not contained since ``8748b70``, so ``if True:`` on
    the notice check passed every test. It now names the notice itself, which
    the wildcard test above proves IS printed when it should be."""
    result = runner.invoke(app, ["ui", *args, "--no-open"])
    assert result.exit_code == 0, result.output
    assert WILDCARD_NOTICE not in result.output, result.output


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


@needs_ipv6
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


def test_resolve_port_reports_a_host_without_ipv6_cleanly() -> None:
    """Round 5, item 4: the probe socket was created OUTSIDE the ``try``.

    On a machine without IPv6, ``--host ::`` died in a raw ``OSError``
    traceback. The socket factory is injected — no production module is
    patched — and raises what such a kernel raises.
    """
    import errno

    from brain.errors import BrainError

    def no_ipv6(family: int, kind: int) -> object:
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

    with pytest.raises(BrainError) as excinfo:
        resolve_port(8765, host="::", socket_factory=no_ipv6)
    assert "IPv6" in str(excinfo.value)
    assert "--host" in str(excinfo.value)


def test_a_non_canonical_loopback_binds_as_its_canonical_form(
    app: typer.Typer, started: _Started
) -> None:
    """Round 5, item 1: ``0:0:0:0:0:0:0:1`` IS loopback; it binds as ``::1``."""
    result = runner.invoke(app, ["ui", "--host", "0:0:0:0:0:0:0:1", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "::1"


@pytest.mark.parametrize("token", ["café", " padded", "padded ", "tab\there"])
def test_a_token_that_cannot_travel_in_a_header_is_a_usage_error(
    app: typer.Typer, started: _Started, token: str
) -> None:
    """Round 5, item 5: printable ASCII with no edge whitespace, or exit 2.

    Non-ASCII made every API call a 500; edge whitespace is trimmed from a
    header value in transit, so the comparison would fail forever.
    """
    result = runner.invoke(app, ["ui", "--token", token, "--no-open"])
    assert result.exit_code == 2, result.output
    assert "printable ASCII" in result.output
    assert started.context_kwargs == {}


def test_a_token_with_inner_spaces_and_symbols_is_accepted(
    app: typer.Typer, started: _Started
) -> None:
    result = runner.invoke(app, ["ui", "--token", "50% off #1", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == "50% off #1"


@pytest.mark.parametrize("spelling", ["LOCALHOST", "LocalHost"])
def test_an_upper_case_localhost_binds_as_localhost(
    app: typer.Typer, started: _Started, spelling: str
) -> None:
    """Round 6, item 2: a browser lower-cases the host, and starlette compares
    Host case-sensitively, so ``--host LOCALHOST`` refused its own URL."""
    result = runner.invoke(app, ["ui", "--host", spelling, "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "localhost"
    assert "http://localhost:8765/" in result.output


class _SocketThatCannotBeConfigured:
    """A socket whose ``setsockopt`` fails, recording whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def setsockopt(self, *args: object) -> None:
        import errno

        raise OSError(errno.ENOPROTOOPT, "Protocol not available")

    def close(self) -> None:
        self.closed = True


def test_a_socket_that_cannot_be_configured_is_closed_and_reported() -> None:
    """Round 6, item 3: ``setsockopt`` failing after the socket exists must
    neither leak the socket nor be reported as "cannot open"."""
    from brain.errors import BrainError

    made: list[_SocketThatCannotBeConfigured] = []

    def factory(family: int, kind: int) -> _SocketThatCannotBeConfigured:
        made.append(_SocketThatCannotBeConfigured())
        return made[-1]

    with pytest.raises(BrainError) as excinfo:
        resolve_port(8765, host="::", socket_factory=factory)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "configure" in message, message
    assert "cannot open" not in message, message
    assert made and all(sock.closed for sock in made), "the probe socket leaked"


@pytest.mark.parametrize("spelling", ["::0", "0:0:0:0:0:0:0:0"])
def test_a_non_canonical_ipv6_wildcard_is_the_wildcard(
    app: typer.Typer, started: _Started, spelling: str
) -> None:
    """Round 7: ``::0`` and the long form canonicalise to ``::`` (round 5)."""
    result = runner.invoke(app, ["ui", "--host", spelling, "--token", "x", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "::"
    assert WILDCARD_NOTICE in result.output
    assert "http://[::1]:8765/#t=x" in result.output


#: Every --host spelling seen across the phase's review rounds, and what it must
#: do. ONE table (rounds 7–9): each round found a spelling that bound something
#: unexpected, so the next one is a row here. Kinds:
#:   ("refused", phrase)  exit 2, before any probe or bind, saying `phrase`;
#:   ("binds", host)      exit 0, bound as `host`, NO wildcard notice;
#:   ("wildcard", host)   exit 0, bound as `host`, WITH the wildcard notice;
#:   ("unowned", phrase)  exit 1 from the REAL port probe, saying `phrase` and
#:                        never "in use" — the address is not this machine's;
#:   ("unowned-on-darwin", phrase)  that on macOS, which owns only 127.0.0.1
#:                        of 127/8; Linux binds the whole block, so exit 0.
#: "binds"/"wildcard" are decided by the address the system will actually
#: bind (numeric getaddrinfo), not by the spelling — the review-round-9 rule.
#: Their probe is replaced, so they test classification; "unowned" rows run
#: the real probe (the final round: only EADDRINUSE means "busy").
HOST_SPELLINGS: list[tuple[str, str, str]] = [
    ("", "refused", "must not be empty"),
    ("   ", "refused", "must not be empty"),
    (" 127.0.0.1", "refused", "leading or trailing whitespace"),
    ("localhost ", "refused", "leading or trailing whitespace"),
    # zones: browsers cannot open a zoned URL, so none is accepted (ruling N1)
    ("::%", "refused", "zone IDs (%…) are not supported"),
    ("[::%]", "refused", "zone IDs (%…) are not supported"),
    ("0::%", "refused", "zone IDs (%…) are not supported"),
    ("::0%", "refused", "zone IDs (%…) are not supported"),
    ("::1%", "refused", "zone IDs (%…) are not supported"),
    ("::%en0", "refused", "zone IDs (%…) are not supported"),
    ("[::%en0]", "refused", "zone IDs (%…) are not supported"),
    ("::1%lo0", "refused", "zone IDs (%…) are not supported"),
    ("fe80::1%en0", "refused", "zone IDs (%…) are not supported"),
    # brackets: one balanced pair, around an IPv6 literal
    ("[]", "refused", "brackets are for IPv6 literals only"),
    ("[127.0.0.1]", "refused", "brackets are for IPv6 literals only"),
    ("[localhost]", "refused", "brackets are for IPv6 literals only"),
    ("[::1", "refused", "unbalanced or misplaced brackets"),
    ("::1]", "refused", "unbalanced or misplaced brackets"),
    ("[[::1]]", "refused", "unbalanced or misplaced brackets"),
    # numeric spellings the system reads differently from `ipaddress`
    ("0", "refused", "reads it as 0.0.0.0"),
    ("127.1", "refused", "reads it as 127.0.0.1"),
    ("0x7f.1", "refused", "reads it as 127.0.0.1"),
    ("010.0.0.1", "refused", "is not a canonical IP address"),
    # an IPv6-only socket cannot bind an IPv4-mapped address (measured)
    ("::ffff:127.0.0.1", "refused", "IPv4-mapped IPv6 addresses cannot be bound"),
    # idna rejects these inside getaddrinfo — a raw UnicodeError escaped (round 10)
    (".", "refused", "is not an IP address or a hostname"),
    ("..", "refused", "is not an IP address or a hostname"),
    ("a..b", "refused", "is not an IP address or a hostname"),
    ("a" * 64, "refused", "is not an IP address or a hostname"),
    (("a" * 63 + ".") * 3 + "a" * 62, "refused", "is not an IP address or a hostname"),
    # non-ASCII: the regex lacked re.ASCII, so these passed or were rewritten
    ("é" * 64, "refused", "must be ASCII"),
    ("localhoſt", "refused", "must be ASCII"),
    ("\u212a", "refused", "must be ASCII"),
    ("İ", "refused", "must be ASCII"),
    # not an address or a hostname at all
    ("127.0.0.1:8080", "refused", "is not an IP address or a hostname"),
    ("http://127.0.0.1", "refused", "is not an IP address or a hostname"),
    ("0.0.0.0/0", "refused", "is not an IP address or a hostname"),
    # link-local needs a zone to bind, and zones are refused (N1)
    ("fe80::1", "refused", "link-local addresses need a zone ID"),
    # real probe: addresses this machine does not own, or names that do not resolve
    ("1.2.3.4", "unowned", "is not an address this machine owns"),
    ("fd00::5", "unowned", "is not an address this machine owns"),
    ("brain-2.invalid", "unowned", "does not resolve to an address on this machine"),
    ("127.0.0.2", "unowned-on-darwin", "is not an address this machine owns"),
    # accepted, classified by the resolved address
    ("0.0.0.0", "wildcard", "0.0.0.0"),
    ("::", "wildcard", "::"),
    ("::0", "wildcard", "::"),
    ("0:0::0", "wildcard", "::"),
    ("[::]", "wildcard", "::"),
    ("127.0.0.1", "binds", "127.0.0.1"),
    ("127.0.0.2", "binds", "127.0.0.2"),
    ("[::1]", "binds", "::1"),
    ("0:0:0:0:0:0:0:1", "binds", "::1"),
    ("LOCALHOST", "binds", "localhost"),
    ("brain-1.lan", "binds", "brain-1.lan"),
    # a hostname is classified by what it RESOLVES to (round 10, item 2)
    ("0.0.0.0.nip.io", "wildcard", "0.0.0.0.nip.io"),
    # IPv6-only resolution is probed as IPv6 (round 10, item 3)
    ("ip6-localhost", "binds-real", "ip6-localhost"),
    ("FD00:0:0::5", "binds", "fd00::5"),
]


@pytest.mark.parametrize(("given", "kind", "expected"), HOST_SPELLINGS)
def test_every_host_spelling_does_what_the_table_says(
    app: typer.Typer,
    started: _Started,
    monkeypatch: pytest.MonkeyPatch,
    given: str,
    kind: str,
    expected: str,
) -> None:
    """Rounds 7–9 and the final round, one row per spelling. A refusal happens
    in ``context.validate_host`` before any probe or bind; a bind is classified
    by the address it resolves to; an unowned address fails in the REAL probe,
    never as "in use"."""
    import sys

    import brain.ui.server as server

    if kind.startswith("unowned"):
        if ":" in given and not _ipv6_wildcard_available():
            pytest.skip("this host cannot open an IPv6 socket")
        # The real probe: `resolve_port` was imported at module top, before the
        # fixture replaced the module attribute.
        monkeypatch.setattr(server, "resolve_port", resolve_port)
        if kind == "unowned-on-darwin" and sys.platform != "darwin":
            kind = "binds-real"
    if kind == "binds-real":
        if not _ipv6_wildcard_available():
            pytest.skip("this host cannot open an IPv6 socket")
        monkeypatch.setattr(server, "resolve_port", resolve_port)
    result = runner.invoke(app, ["ui", "--host", given, "--token", "x", "--no-open"])
    # Both "busy" phrasings: the catch-all said "already in use" without
    # --auto-port and "no free port found" with it (the default). Checking
    # only "in use" missed the second — found by the strict harness.
    for busy in ("in use", "no free port"):
        assert busy not in result.output, result.output
    if kind.startswith("unowned"):
        assert result.exit_code == 1, result.output
        assert expected in result.output, result.output
        assert started.context_kwargs == {}
        return
    if kind == "binds-real":
        assert result.exit_code == 0, result.output
        if expected != given:
            return
        assert started.context_kwargs["host"] == expected
        return
    if kind == "refused":
        assert result.exit_code == 2, result.output
        assert expected in result.output, result.output
        assert started.context_kwargs == {}
        return
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == expected
    assert (WILDCARD_NOTICE in result.output) is (kind == "wildcard"), result.output


def test_bind_address_strips_only_a_balanced_pair_of_brackets() -> None:
    """S14 (phase-7 audit): ``startswith("[")`` alone survived every test.

    Under it, ``[::1`` lost its first and last characters and became ``::`` —
    the IPv6 WILDCARD. ``validate_host`` refuses that spelling first, so this
    pins the helper itself: an unbalanced bracket is left for the caller to
    refuse, never repaired into a different address.
    """
    from brain.ui.context import bind_address

    assert bind_address("[::1") == "[::1"
    assert bind_address("::1]") == "::1]"
    assert bind_address("[::1]") == "::1"


class _RecordingSocket:
    """Records every ``setsockopt`` call the probe makes."""

    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def close(self) -> None:
        return None


@pytest.mark.parametrize(("address", "ipv6"), [("::", True), ("127.0.0.1", False)])
def test_the_probe_socket_sets_the_options_the_real_bind_uses(
    address: str, ipv6: bool
) -> None:
    """Round 9, item 2: dropping ``SO_REUSEADDR`` survived every test.

    Without it the probe can report a port busy that uvicorn — which sets it —
    would bind, so ``--no-auto-port`` refuses a usable port. Recorded through the
    injected socket factory; no production module is patched.
    """
    import socket

    from brain.ui.server import _probe_socket

    made: list[_RecordingSocket] = []

    def factory(family: int, kind: int) -> _RecordingSocket:
        made.append(_RecordingSocket())
        return made[-1]

    _probe_socket(address, factory)  # type: ignore[arg-type]
    (sock,) = made
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in sock.options, sock.options
    v6only = (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    assert (v6only in sock.options) is ipv6, sock.options


class _BindFails:
    """A probe socket whose ``bind`` raises ``exc``."""

    def __init__(self, exc: OSError) -> None:
        self.exc = exc

    def setsockopt(self, *args: object) -> None:
        return None

    def bind(self, address: object) -> None:
        raise self.exc

    def close(self) -> None:
        return None


def _probe_with(exc: OSError, *, host: str = "192.0.2.10", auto: bool = False) -> str:
    """Run the REAL resolve_port with a probe socket that fails with ``exc``."""
    from brain.errors import BrainError

    def factory(family: int, kind: int) -> _BindFails:
        return _BindFails(exc)

    with pytest.raises(BrainError) as excinfo:
        resolve_port(8765, host=host, auto=auto, socket_factory=factory)  # type: ignore[arg-type]
    return str(excinfo.value)


def test_only_address_in_use_means_busy() -> None:
    """The final round: ``except OSError: return False`` called EVERY bind
    failure "busy". EADDRINUSE still is — and still says so."""
    import errno

    assert "already in use" in _probe_with(OSError(errno.EADDRINUSE, "in use"))
    assert "no free port found" in _probe_with(
        OSError(errno.EADDRINUSE, "in use"), auto=True
    )


def test_an_address_this_machine_does_not_own_is_reported_as_such() -> None:
    """EADDRNOTAVAIL — 49 on macOS, 99 on Linux; ``errno`` names it per platform."""
    import errno

    message = _probe_with(OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"))
    assert "is not an address this machine owns" in message, message
    assert "in use" not in message and "no free port" not in message


def test_a_name_that_does_not_resolve_is_reported_as_such() -> None:
    import socket

    message = _probe_with(socket.gaierror(8, "nodename nor servname provided"))
    assert "does not resolve to an address on this machine" in message, message
    assert "in use" not in message


def test_a_privileged_port_is_a_permission_error_not_busy() -> None:
    """EACCES is NOT busy (decided in the final round): auto-port moves up by
    one, which stays inside the privileged range below 1024 and hides the
    cause. It says what to do instead."""
    import errno

    message = _probe_with(OSError(errno.EACCES, "Permission denied"))
    # The remediation, not merely "permission denied": strerror alone already
    # says that, so asserting it could not tell this branch from the generic
    # one — found by the strict harness.
    assert "ports below 1024 need privileges" in message, message
    assert "--port 1024 or higher" in message, message
    assert "in use" not in message


def test_any_other_bind_failure_is_reported_not_called_busy() -> None:
    import errno

    message = _probe_with(OSError(errno.EINVAL, "Invalid argument"))
    assert "cannot be bound" in message, message
    assert "Invalid argument" in message
    assert "in use" not in message


def test_localhost_that_does_not_resolve_to_loopback_needs_a_token(
    app: typer.Typer, started: _Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 10, item 2 (the mirror case): ``localhost`` was trusted as loopback
    BY NAME. If it resolves elsewhere it is not loopback, so a token is
    required — and without one the bind is refused."""
    monkeypatch.setitem(FAKE_DNS, "localhost", ["192.0.2.30"])
    result = runner.invoke(app, ["ui", "--host", "localhost", "--no-open"])
    assert result.exit_code == 2, result.output
    assert "pass --token" in result.output, result.output
    assert started.context_kwargs == {}


def test_a_name_resolving_to_loopback_and_elsewhere_is_not_loopback(
    app: typer.Typer, started: _Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loopback only if EVERY resolved address is loopback: uvicorn binds them all."""
    monkeypatch.setitem(FAKE_DNS, "localhost", ["127.0.0.1", "192.0.2.30"])
    result = runner.invoke(app, ["ui", "--host", "localhost", "--no-open"])
    assert result.exit_code == 2, result.output
    assert "pass --token" in result.output, result.output


def test_the_cli_threads_the_resolved_addresses_into_the_context(
    app: typer.Typer, started: _Started
) -> None:
    """Round 10: the allowlist, the origin and the confidential lens are built
    from the RESOLVED addresses, so the CLI must hand them to build_context."""
    import ipaddress

    result = runner.invoke(
        app, ["ui", "--host", "0.0.0.0.nip.io", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["resolved"] == (ipaddress.ip_address("0.0.0.0"),)
    assert "http://127.0.0.1:8765/#t=x" in result.output


def test_resolve_bind_addresses_resolves_a_name_passively_like_the_bind() -> None:
    """The resolver sees exactly what uvicorn's bind will ask: AI_PASSIVE."""
    import ipaddress
    import socket

    from brain.ui.server import resolve_bind_addresses

    calls: list[tuple[object, ...]] = []

    def recording(*args: object) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        calls.append(args)
        return fake_resolver(*args)  # type: ignore[arg-type]

    assert resolve_bind_addresses("localhost", resolver=recording) == (
        ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"),
    )
    ((host, _port, family, kind, _proto, flags),) = calls
    assert (host, family, kind, flags) == (
        "localhost", socket.AF_UNSPEC, socket.SOCK_STREAM, socket.AI_PASSIVE,
    )


def test_resolve_bind_addresses_never_resolves_a_literal() -> None:
    import ipaddress

    from brain.ui.server import resolve_bind_addresses

    def refuse(*args: object) -> list[object]:
        raise AssertionError("a literal must not reach the resolver")

    assert resolve_bind_addresses("::1", resolver=refuse) == (ipaddress.ip_address("::1"),)


def test_resolve_bind_addresses_reports_a_name_that_will_not_resolve() -> None:
    from brain.errors import BrainError
    from brain.ui.server import resolve_bind_addresses

    with pytest.raises(BrainError) as excinfo:
        resolve_bind_addresses("brain-2.invalid", resolver=fake_resolver)
    assert "does not resolve to an address on this machine" in str(excinfo.value)


def test_resolve_bind_addresses_reports_a_unicode_failure_cleanly() -> None:
    """Belt and braces: validate_host refuses these first, but the resolver's
    idna codec must never escape as a traceback from here either."""
    from brain.errors import BrainError
    from brain.ui.server import resolve_bind_addresses

    def idna_fails(*args: object) -> list[object]:
        raise UnicodeError("idna: label empty or too long")

    with pytest.raises(BrainError) as excinfo:
        resolve_bind_addresses("odd-name", resolver=idna_fails)
    assert "is not a valid hostname" in str(excinfo.value)


@needs_ipv6
def test_the_probe_binds_every_resolved_family() -> None:
    """Round 10, item 3: the probe chose its family by ``":" in host``, so a
    name that resolves only to IPv6 was probed as IPv4 and falsely refused."""
    import ipaddress

    assert resolve_port(
        DEFAULT_PORT + 4100, host="ip6-localhost", auto=True,
        addresses=(ipaddress.ip_address("::1"),),
    ) >= DEFAULT_PORT + 4100


def test_browser_url_for_a_name_resolving_to_the_wildcard_is_loopback() -> None:
    import ipaddress

    from brain.ui.server import browser_url

    v4 = (ipaddress.ip_address("0.0.0.0"),)
    v6 = (ipaddress.ip_address("::"),)
    assert browser_url("0.0.0.0.nip.io", 8765, "", resolved=v4) == "http://127.0.0.1:8765/"
    assert browser_url("wild6.example", 8765, "", resolved=v6) == "http://[::1]:8765/"
