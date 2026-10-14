"""The `brain ui` Typer surface.

Nothing here binds a socket or blocks: ``serve`` and the browser opener are
injected, so the command's argument handling, gating and panel output are tested
without a server ever starting.

The sub-app is registered into a throwaway ``typer.Typer`` rather than imported
from ``brain.cli``. That is deliberate — ``cli.py`` is coordinator-owned and the
two-line registration has not landed yet, so testing through a local app proves
``register_ui_commands`` works and keeps these tests green either way.

Review round 11 split this module at the 800-line ceiling: `--host` validation
and resolution live in ``tests/test_cli_ui_host.py``, the port and socket
binding in ``tests/test_ui_server_bind.py``, and the doubles all three share in
``tests/ui_cli_support.py``.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from pathlib import Path

import pytest
import typer

from brain.cli_ui import PHONE_LINE
from brain.ui.context import UiContext
from brain.ui.lan import WILDCARD_NOTICE
from brain.ui.security import is_loopback
from brain.ui.server import DEFAULT_PORT, bind_listeners, serve
from tests.ui_cli_support import (
    LAN_IP,
    Started,
    install_started,
    make_app,
    runner,
)


@pytest.fixture
def app() -> typer.Typer:
    return make_app()


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Started:
    return install_started(tmp_path, monkeypatch)


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


def test_include_confidential_help_names_all_three_modes() -> None:
    """Round 16c (audit of `e0d6067`): the help said the flag was "Ignored on
    loopback, where they are always served", but since round 16 a loopback
    ALIAS withholds bodies unless the flag is passed — it widens egress there.
    Read from the option itself, so Rich's wrapping cannot split a phrase."""
    from brain.cli_ui import ui_app

    command = typer.main.get_command(ui_app)
    # By `opts`, not `isinstance(click.Option)`: Typer vendors its own click,
    # so the top-level click's Option is a different class (§10.2).
    option = next(
        p for p in command.params if "--include-confidential" in getattr(p, "opts", ())
    )
    text = getattr(option, "help", None) or ""
    assert "Ignored on loopback" not in text, text
    assert "a loopback alias" in text, text
    assert "No effect on a loopback address or localhost" in text, text


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


def test_a_busy_port_is_named_with_the_one_used_instead(
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 16 (audit finding 3): when claim_port steps past a busy port the
    panel says so — no test executed this line before."""
    import brain.ui.server as server

    monkeypatch.setattr(server, "claim_port", lambda port, **kw: (port + 1, []))
    result = runner.invoke(app, ["ui", "--port", "8765", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "port 8765 was busy — using 8766 instead" in result.output, result.output
    assert started.context_kwargs["port"] == 8766


@pytest.mark.parametrize("serve_confidential", [False, True])
def test_the_panel_never_contradicts_the_confidential_lens_on_an_alias(
    capsys: pytest.CaptureFixture[str], serve_confidential: bool
) -> None:
    """Round 16b (review of `2af3667`): the alias note said "confidential bodies
    withheld" even under --include-confidential, which serves them, and the
    Confidential line called a loopback alias a "non-loopback bind"."""
    from types import SimpleNamespace

    from brain.cli_ui import _panel

    _panel(
        url="http://loopback-alias.example:8765/#t=x",
        cfg=SimpleNamespace(  # type: ignore[arg-type]
            vault_path=Path("/vault"), database_url="postgresql://h:1/db", embedder="none"
        ),
        read_only=False, loopback=False, serve_confidential=serve_confidential,
        notices=(), note_count=None, alias_of="loopback-alias.example",
    )
    out = capsys.readouterr().out
    assert "non-loopback bind" not in out, out
    assert "guarded like a network bind" in out, out
    assert ("withheld" in out) is not serve_confidential, out
    if serve_confidential:
        assert "bodies SERVED on a loopback alias" in out, out
    else:
        assert "bodies withheld (loopback alias, guarded like a network bind)" in out, out


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


def test_lan_binds_the_lan_address_with_a_minted_token(
    app: typer.Typer, started: Started
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


def test_lan_uses_a_supplied_token(app: typer.Typer, started: Started) -> None:
    result = runner.invoke(
        app, ["ui", "--lan", "--token", "a-stable-secret", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == "a-stable-secret"


def test_lan_tells_the_user_to_open_the_url_on_another_device(
    app: typer.Typer, started: Started
) -> None:
    result = runner.invoke(app, ["ui", "--lan", "--no-open"])
    assert "on your phone" in result.output


def test_lan_and_host_together_is_a_usage_error(
    app: typer.Typer, started: Started
) -> None:
    """Exit 2 before anything is resolved — two answers to one question."""
    result = runner.invoke(
        app, ["ui", "--lan", "--host", "192.0.2.9", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 2
    assert started.context_kwargs == {}


def test_lan_without_a_network_exits_one_with_the_override(
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
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
    app: typer.Typer, started: Started, wildcard: str
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
    app: typer.Typer, started: Started
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
    app: typer.Typer, started: Started
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
    app: typer.Typer, started: Started
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
    app: typer.Typer, started: Started, args: list[str]
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


class _Server:
    """uvicorn's ``Server``, replaced: ``run`` performs the startup hook and
    returns, so nothing serves. Records the sockets it was handed, and whether
    each was still open when it ran.

    It calls ``startup(sockets=sockets)`` exactly as uvicorn's ``_serve`` does
    (review round 12). It used to call ``startup()`` with nothing, so a
    ``startup_then_open`` that dropped its arguments passed every test while
    real uvicorn, handed no sockets, bound ``config.host:port`` for itself.
    ``tests/test_cli_ui.py::test_real_uvicorn_serves_only_the_claimed_socket``
    checks the same thing against uvicorn itself, so this double cannot drift
    from it again unnoticed."""

    runs: list[list[socket.socket]] = []
    started: list[list[socket.socket] | None] = []

    def __init__(self, config: object) -> None:
        self.config = config

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        _Server.started.append(sockets)

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        assert sockets is not None, "serve() let uvicorn bind for itself"
        assert all(sock.fileno() != -1 for sock in sockets), "a socket closed early"
        _Server.runs.append(sockets)
        asyncio.run(self.startup(sockets=sockets))


def _ui_context(origin: str, host: str) -> UiContext:
    @contextlib.contextmanager
    def conn_factory():  # type: ignore[no-untyped-def]
        yield None

    # FTS-only shape (embedder=None), so the boot-time warm-up is skipped
    # rather than faked.
    return UiContext(
        cfg=object(),  # type: ignore[arg-type]
        conn_factory=conn_factory,
        embedder=None,  # type: ignore[arg-type]
        search_fn=lambda *a, **k: [],
        token="s3cret",
        allowed_origin=origin,
        allowed_hosts=frozenset({host}),
    )


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> type[_Server]:
    import uvicorn

    monkeypatch.setattr(_Server, "runs", [])
    monkeypatch.setattr(_Server, "started", [])
    monkeypatch.setattr(uvicorn, "Server", _Server)
    return _Server


@pytest.mark.parametrize("bind", ["127.0.0.1", LAN_IP])
def test_serve_opens_the_tokened_url(fake_uvicorn: type[_Server], bind: str) -> None:
    """``serve`` must hand the opener the same URL the panel prints — and, on
    this default ``--open`` path, its startup wrapper must pass the claimed
    sockets through to uvicorn's startup (review round 12)."""
    opened: list[str] = []
    context = _ui_context(f"http://{bind}:8765", bind)
    with socket.socket() as sock:
        serve(
            context, host=bind, port=8765, sockets=[sock],
            open_browser=True, opener=opened.append,
        )

        assert opened == [f"http://{bind}:8765/#t=s3cret"]
        (handed,) = fake_uvicorn.started
        assert handed is not None, "startup ran without the claimed sockets"
        assert handed == [sock] and handed[0] is sock


def test_real_uvicorn_serves_only_the_claimed_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 12, against uvicorn itself rather than a double: with the
    browser opening (the default), the server listens on the claimed socket and
    nothing else. Dropping ``sockets=`` in the startup wrapper made uvicorn bind
    ``config.host:port`` on its own as well — measured on 0.54 — so the config
    port here is 0: that stray bind, if it ever comes back, shows up as a
    second listener instead of colliding with the claimed port.

    The server is real; the subclass only records what it listens on and asks
    it to stop once started."""
    import uvicorn

    listening: list[list[tuple[str, int]]] = []

    class _StopOnceStarted(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            listening.append(sorted(
                sock.getsockname()[:2] for server in self.servers for sock in server.sockets
            ))
            self.should_exit = True

    monkeypatch.setattr(uvicorn, "Server", _StopOnceStarted)
    # Port 0: the OS assigns a free one, so this cannot collide with anything
    # (review round 13).
    sockets = bind_listeners("127.0.0.1", 0, addresses=(ipaddress.ip_address("127.0.0.1"),))
    port = sockets[0].getsockname()[1]
    opened: list[str] = []
    context = _ui_context(f"http://127.0.0.1:{port}", "127.0.0.1")
    serve(
        context, host="127.0.0.1", port=0, sockets=sockets,
        open_browser=True, opener=opened.append,
    )

    assert listening == [[("127.0.0.1", port)]], "uvicorn listened beyond the claimed socket"
    assert opened == [f"http://127.0.0.1:{port}/#t=s3cret"]
    assert all(sock.fileno() == -1 for sock in sockets)


def test_serve_runs_uvicorn_on_the_sockets_it_was_given_and_closes_them(
    fake_uvicorn: type[_Server],
) -> None:
    """Round 11: uvicorn is handed the sockets claim_port bound — it never
    resolves or binds ``host`` itself — and they are closed once it stops."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        context = _ui_context("http://127.0.0.1:8765", "127.0.0.1")
        serve(context, host="127.0.0.1", port=8765, sockets=[sock], open_browser=False)

        assert fake_uvicorn.runs == [[sock]]
        assert sock.fileno() == -1, "serve() left its socket open"


def test_serve_closes_its_sockets_when_startup_fails(
    fake_uvicorn: type[_Server], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sockets are already bound when serve() starts; an embedder that will
    not warm must not leave them open behind it."""
    import brain.ui.server as server
    from brain.errors import BrainError

    def cannot_warm(embedder: object) -> None:
        raise BrainError("the embedder would not load")

    monkeypatch.setattr(server, "warm_embedder", cannot_warm)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        context = _ui_context("http://127.0.0.1:8765", "127.0.0.1")
        with pytest.raises(BrainError):
            serve(context, host="127.0.0.1", port=8765, sockets=[sock], open_browser=False)

        assert fake_uvicorn.runs == []
        assert sock.fileno() == -1, "serve() leaked its socket on a failed start"


def test_the_ipv6_wildcard_notice_says_ipv4_devices_cannot_connect(
    app: typer.Typer, started: Started
) -> None:
    """Review round 3, item 3: ``::`` is IPv6-only, so "refused with 400" is
    not the whole story — an IPv4 phone is refused at connect."""
    result = runner.invoke(app, ["ui", "--host", "::", "--token", "x", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "IPv6-only" in result.output, result.output


@pytest.mark.parametrize("token", ["café", " padded", "padded ", "tab\there"])
def test_a_token_that_cannot_travel_in_a_header_is_a_usage_error(
    app: typer.Typer, started: Started, token: str
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
    app: typer.Typer, started: Started
) -> None:
    result = runner.invoke(app, ["ui", "--token", "50% off #1", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == "50% off #1"


def test_browser_url_for_a_name_resolving_to_the_wildcard_is_loopback() -> None:
    import ipaddress

    from brain.ui.server import browser_url

    v4 = (ipaddress.ip_address("0.0.0.0"),)
    v6 = (ipaddress.ip_address("::"),)
    assert browser_url("0.0.0.0.nip.io", 8765, "", resolved=v4) == "http://127.0.0.1:8765/"
    assert browser_url("wild6.example", 8765, "", resolved=v6) == "http://[::1]:8765/"


def test_serve_opens_the_guards_origin_not_the_bind_spelling(
    fake_uvicorn: type[_Server],
) -> None:
    """Round 10: for a hostname the URL to open is the Origin the write guard
    expects — a name resolving to 0.0.0.0 opens 127.0.0.1, not the name."""
    opened: list[str] = []
    context = _ui_context("http://127.0.0.1:8765", "127.0.0.1")
    serve(
        context, host="0.0.0.0.nip.io", port=8765, sockets=[],
        open_browser=True, opener=opened.append,
    )
    assert opened == ["http://127.0.0.1:8765/#t=s3cret"]
