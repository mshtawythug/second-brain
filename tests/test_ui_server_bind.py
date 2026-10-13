"""`brain ui`'s bind: one listening socket per classified address, handed to uvicorn.

Split out of ``tests/test_cli_ui.py`` in review round 11, which also changed
what is under test. The port used to be PROBED here and bound again by uvicorn
from the host NAME — a race for the port, and a second DNS lookup that could
bind somewhere the first was not classified as. ``claim_port`` now binds the
classified addresses and the sockets it returns are the ones served. Socket
behaviour is driven through the injected ``socket_factory``; no production
module is patched.
"""
from __future__ import annotations

import errno
import functools
import ipaddress
import socket
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import typer

from brain.errors import BrainError
from brain.ui.server import DEFAULT_PORT, _listen_socket, _open_listener, claim_port
from brain.ui.server import resolve_bind_addresses as _REAL_RESOLVE
from tests.ui_cli_support import (
    Started,
    install_started,
    make_app,
    needs_ipv6,
    runner,
)


@pytest.fixture
def app() -> typer.Typer:
    return make_app()


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Started:
    return install_started(tmp_path, monkeypatch)


def _close(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        sock.close()


def test_claim_port_returns_a_free_port_and_its_live_socket() -> None:
    port, sockets = claim_port(DEFAULT_PORT + 4000, auto=True)
    with closing(sockets[0]):
        assert port >= DEFAULT_PORT + 4000
        assert sockets[0].getsockname() == ("127.0.0.1", port)


def test_claim_port_bumps_past_a_busy_one() -> None:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        port, sockets = claim_port(busy, auto=True)
        _close(sockets)
        assert port != busy


def test_no_auto_port_refuses_with_the_remediation_text() -> None:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        with pytest.raises(BrainError) as excinfo:
            claim_port(busy, auto=False)
    message = str(excinfo.value)
    assert "already in use" in message
    assert "--no-auto-port" in message
    assert "--port" in message


@needs_ipv6
@pytest.mark.parametrize("wildcard", ["::"])
def test_claim_port_binds_a_free_port_on_the_ipv6_wildcard(wildcard: str) -> None:
    """L-8 (phase-7 audit): the port probe bound ``::`` with an IPv4 socket.

    That always raised, so every port looked busy and ``brain ui --host ::``
    died with "no free port found" before the notice could print.
    """
    with closing(socket.socket(socket.AF_INET6, socket.SOCK_STREAM)) as sock:
        sock.bind(("::", 0))
        free = sock.getsockname()[1]
    port, sockets = claim_port(free, host=wildcard, auto=False)
    _close(sockets)
    assert port == free


@needs_ipv6
def test_claim_port_sees_a_busy_ipv6_wildcard_port() -> None:
    """The fix must not make the bind always succeed for IPv6."""
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        port, sockets = claim_port(busy, host="::", auto=True)
        _close(sockets)
        assert port != busy


@needs_ipv6
def test_the_listen_socket_is_ipv6_only_like_asyncio_makes_it() -> None:
    """Review round 3, item 3: asyncio binds ``::`` with ``IPV6_V6ONLY=1``.

    A dual-stack probe (the OS default, ``0``) answers a different question
    from the bind that follows: on Linux it reports a port busy because an IPv4
    socket holds it, although the IPv6-only server would bind it fine. Asserted
    on the socket option itself, because the behavioural difference is
    Linux-only — macOS reports both cases free (measured 2026-10-13).
    """
    with _listen_socket("::") as sock:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    with _listen_socket("127.0.0.1") as sock:
        assert sock.family == socket.AF_INET


def test_claim_port_reports_a_host_without_ipv6_cleanly() -> None:
    """Round 5, item 4: the probe socket was created OUTSIDE the ``try``.

    On a machine without IPv6, ``--host ::`` died in a raw ``OSError``
    traceback. The socket factory is injected — no production module is
    patched — and raises what such a kernel raises.
    """
    def no_ipv6(family: int, kind: int) -> object:
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

    with pytest.raises(BrainError) as excinfo:
        claim_port(8765, host="::", socket_factory=no_ipv6)  # type: ignore[arg-type]
    assert "IPv6" in str(excinfo.value)
    assert "--host" in str(excinfo.value)


class _SocketThatCannotBeConfigured:
    """A socket whose ``setsockopt`` fails, recording whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def setsockopt(self, *args: object) -> None:
        raise OSError(errno.ENOPROTOOPT, "Protocol not available")

    def close(self) -> None:
        self.closed = True


def test_a_socket_that_cannot_be_configured_is_closed_and_reported() -> None:
    """Round 6, item 3: ``setsockopt`` failing after the socket exists must
    neither leak the socket nor be reported as "cannot open"."""
    made: list[_SocketThatCannotBeConfigured] = []

    def factory(family: int, kind: int) -> _SocketThatCannotBeConfigured:
        made.append(_SocketThatCannotBeConfigured())
        return made[-1]

    with pytest.raises(BrainError) as excinfo:
        claim_port(8765, host="::", socket_factory=factory)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "configure" in message, message
    assert "cannot open" not in message, message
    assert made and all(sock.closed for sock in made), "the socket leaked"


class _RecordingSocket:
    """Records what is done to it; ``bind`` fails for any address in ``refuse``."""

    def __init__(self, family: int, refuse: dict[str, OSError] | None = None) -> None:
        self.family = family
        self.refuse = refuse or {}
        self.options: list[tuple[int, int, int]] = []
        self.bound: tuple[str, int] | None = None
        self.listening = False
        self.closed = False

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        if not 0 <= address[1] <= 65535:
            # What a real socket raises — an OverflowError, not an OSError.
            raise OverflowError("bind(): port must be 0-65535.")
        if address[0] in self.refuse:
            raise self.refuse[address[0]]
        self.bound = address

    def listen(self, backlog: int) -> None:
        self.listening = True

    def close(self) -> None:
        self.closed = True


class _Factory:
    """An injected socket factory that keeps every socket it made."""

    def __init__(self, refuse: dict[str, OSError] | None = None) -> None:
        self.refuse = refuse
        self.made: list[_RecordingSocket] = []

    def __call__(self, family: int, kind: int) -> _RecordingSocket:
        self.made.append(_RecordingSocket(family, self.refuse))
        return self.made[-1]


@pytest.mark.parametrize(
    ("address", "ipv6", "reuse"),
    [
        ("127.0.0.1", False, True),
        ("::1", True, True),
        ("192.0.2.5", False, True),
        ("0.0.0.0", False, False),
        ("::", True, False),
    ],
)
def test_the_listen_socket_sets_its_options_by_address(
    address: str, ipv6: bool, reuse: bool
) -> None:
    """Round 9, item 2: dropping ``SO_REUSEADDR`` survived every test — without
    it a port in TIME_WAIT looks busy, so ``--no-auto-port`` refuses a usable
    port. Round 13: but NOT on a wildcard, where on macOS it let the bind
    overlap another process's loopback listener (the real-socket test below).
    Recorded through the injected socket factory; no production module is
    patched.
    """
    factory = _Factory()
    _listen_socket(address, factory)  # type: ignore[arg-type]
    (sock,) = factory.made
    reuseaddr = (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    assert (reuseaddr in sock.options) is reuse, sock.options
    v6only = (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    assert (v6only in sock.options) is ipv6, sock.options


def _bind_with(exc: OSError, *, host: str = "192.0.2.10", auto: bool = False) -> str:
    """Run the REAL claim_port with a socket whose bind fails with ``exc``."""
    factory = _Factory(refuse={host: exc})
    with pytest.raises(BrainError) as excinfo:
        claim_port(8765, host=host, auto=auto, socket_factory=factory)  # type: ignore[arg-type]
    assert factory.made and all(sock.closed for sock in factory.made), "a socket leaked"
    return str(excinfo.value)


def test_only_address_in_use_means_busy() -> None:
    """The final round: ``except OSError: return False`` called EVERY bind
    failure "busy". EADDRINUSE still is — and still says so."""
    assert "already in use" in _bind_with(OSError(errno.EADDRINUSE, "in use"))
    assert "no free port found" in _bind_with(
        OSError(errno.EADDRINUSE, "in use"), auto=True
    )


def test_an_address_this_machine_does_not_own_is_reported_as_such() -> None:
    """EADDRNOTAVAIL — 49 on macOS, 99 on Linux; ``errno`` names it per platform."""
    message = _bind_with(OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"))
    assert "is not an address this machine owns" in message, message
    assert "in use" not in message and "no free port" not in message


def test_a_privileged_port_is_a_permission_error_not_busy() -> None:
    """EACCES is NOT busy (decided in the final round): auto-port moves up by
    one, which stays inside the privileged range below 1024 and hides the
    cause. It says what to do instead."""
    message = _bind_with(OSError(errno.EACCES, "Permission denied"))
    # The remediation, not merely "permission denied": strerror alone already
    # says that, so asserting it could not tell this branch from the generic
    # one — found by the strict harness.
    assert "ports below 1024 need privileges" in message, message
    assert "--port 1024 or higher" in message, message
    assert "in use" not in message


def test_any_other_bind_failure_is_reported_not_called_busy() -> None:
    message = _bind_with(OSError(errno.EINVAL, "Invalid argument"))
    assert "cannot be bound" in message, message
    assert "Invalid argument" in message
    assert "in use" not in message


@needs_ipv6
def test_claim_port_binds_every_resolved_family() -> None:
    """Round 10, item 3: the probe chose its family by ``":" in host``, so a
    name that resolves only to IPv6 was probed as IPv4 and falsely refused."""
    port, sockets = claim_port(
        DEFAULT_PORT + 4100, host="ip6-localhost", auto=True,
        addresses=(ipaddress.ip_address("::1"),),
    )
    _close(sockets)
    assert port >= DEFAULT_PORT + 4100
    assert [sock.family for sock in sockets] == [socket.AF_INET6]


# --------------------------------------------------------------------------
# Review round 11 — bind EXACTLY what was classified, and keep the sockets
# --------------------------------------------------------------------------


def test_claim_port_binds_exactly_the_given_addresses_and_listens() -> None:
    """One listening socket per classified address, in its own family — and
    nothing else. uvicorn used to resolve ``host`` again and bind whatever
    that second answer said."""
    addresses = (ipaddress.ip_address("192.0.2.5"), ipaddress.ip_address("fd00::5"))
    factory = _Factory()
    port, sockets = claim_port(
        8765, host="brain-1.lan", addresses=addresses, socket_factory=factory  # type: ignore[arg-type]
    )
    assert port == 8765
    assert sockets == factory.made  # type: ignore[comparison-overlap]
    assert [sock.bound for sock in factory.made] == [("192.0.2.5", 8765), ("fd00::5", 8765)]
    assert [sock.family for sock in factory.made] == [socket.AF_INET, socket.AF_INET6]
    assert all(sock.listening and not sock.closed for sock in factory.made)


def test_a_failed_address_closes_every_socket_already_bound() -> None:
    """The first address binds, the second does not: the first must not leak
    holding the port."""
    addresses = (ipaddress.ip_address("192.0.2.5"), ipaddress.ip_address("192.0.2.6"))
    factory = _Factory(refuse={"192.0.2.6": OSError(errno.EADDRNOTAVAIL, "not here")})
    with pytest.raises(BrainError, match="is not an address this machine owns"):
        claim_port(
            8765, host="brain-1.lan", addresses=addresses, socket_factory=factory  # type: ignore[arg-type]
        )
    assert len(factory.made) == 2
    assert all(sock.closed for sock in factory.made), "a bound socket leaked"


def test_a_busy_address_frees_the_others_before_the_next_port() -> None:
    """Busy on ONE address is busy: every socket of that attempt is closed and
    the next port tried for all of them."""
    addresses = (ipaddress.ip_address("192.0.2.5"), ipaddress.ip_address("192.0.2.6"))

    class _BusyOnce(_Factory):
        def __call__(self, family: int, kind: int) -> _RecordingSocket:
            sock = super().__call__(family, kind)
            if len(self.made) == 2:
                sock.refuse = {"192.0.2.6": OSError(errno.EADDRINUSE, "in use")}
            return sock

    factory = _BusyOnce()
    port, sockets = claim_port(
        8765, host="brain-1.lan", addresses=addresses, socket_factory=factory  # type: ignore[arg-type]
    )
    assert port == 8766
    assert all(sock.closed for sock in factory.made[:2])
    assert [sock.bound for sock in factory.made[2:]] == [
        ("192.0.2.5", 8766), ("192.0.2.6", 8766),
    ]
    assert sockets == factory.made[2:]  # type: ignore[comparison-overlap]


def test_claim_port_refuses_a_hostname_it_was_not_given_addresses_for() -> None:
    """A hostname is never resolved here: without its classified addresses
    there is nothing safe to bind."""
    with pytest.raises(BrainError, match="has no address to bind"):
        claim_port(8765, host="brain-1.lan", socket_factory=_Factory())  # type: ignore[arg-type]


def test_the_claimed_socket_is_already_listening() -> None:
    """``listen`` in claim_port, not later in uvicorn: on Linux two
    SO_REUSEADDR sockets may both BIND a port nobody listens on, so an unlistened
    claim does not hold it. Proven by connecting, which a socket that is not
    listening refuses (Linux) or leaves to time out (macOS, measured)."""
    port, sockets = claim_port(DEFAULT_PORT + 4200, auto=True)
    with closing(sockets[0]):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError as exc:
            pytest.fail(f"the claimed socket is not listening ({exc!r})")


def test_the_cli_binds_what_it_classified_even_if_dns_changes_its_answer(
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 11, the TOCTOU: the host was classified on one lookup, and uvicorn
    resolved it AGAIN to bind. A name that answered loopback first (no token
    needed) could then bind wherever the second answer pointed — a wildcard,
    say. Here the second answer is ``::1``: different, but still loopback, so
    even a regression that re-resolves never binds a real interface. The bind
    must be on the FIRST answer, from a single lookup."""
    import brain.ui.server as server

    answers = [["127.0.0.1"], ["::1"]]
    calls: list[str] = []

    def shifting(host: str, *args: object) -> list[tuple[object, ...]]:
        calls.append(host)
        addresses = answers[min(len(calls), len(answers)) - 1]
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 0, 0, 0)) if ":" in a
            else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses
        ]

    monkeypatch.setattr(
        server, "resolve_bind_addresses",
        functools.partial(_REAL_RESOLVE, resolver=shifting),
    )
    monkeypatch.setattr(server, "claim_port", claim_port)
    result = runner.invoke(
        app, ["ui", "--host", "brain-1.lan", "--port", str(DEFAULT_PORT + 4300), "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert calls == ["brain-1.lan"], "the host was resolved more than once"
    assert [address for address, _ in started.bound] == ["127.0.0.1"]


def test_the_cli_closes_its_sockets_when_startup_fails(
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claim_port binds BEFORE the context is built; a failure there must not
    leave the port held until the process exits."""
    import brain.ui.server as server

    claimed: list[socket.socket] = []

    def recording_claim(port: int, **kwargs: Any) -> tuple[int, list[socket.socket]]:
        result = claim_port(port, **kwargs)
        claimed.extend(result[1])
        return result

    def failing_context(cfg: object, **kwargs: object) -> object:
        raise BrainError("cannot reach Postgres (OperationalError).")

    monkeypatch.setattr(server, "claim_port", recording_claim)
    monkeypatch.setattr(server, "build_context", failing_context)
    result = runner.invoke(app, ["ui", "--port", str(DEFAULT_PORT + 4400), "--no-open"])
    assert result.exit_code == 1, result.output
    assert claimed, "the real claim_port never ran"
    assert all(sock.fileno() == -1 for sock in claimed), "the CLI leaked its socket"


# --------------------------------------------------------------------------
# Review round 12 — the port range
# --------------------------------------------------------------------------


@pytest.mark.parametrize("port", ["70000", "65536", "-1", "0"])
def test_a_port_outside_1_to_65535_is_a_usage_error(
    app: typer.Typer, started: Started, port: str
) -> None:
    """``bind`` raised a raw OverflowError past 65535, and 0 asked the kernel
    for an ephemeral port — a DIFFERENT one on each address — while the panel
    printed ``:0``. Refused before anything binds."""
    result = runner.invoke(app, ["ui", "--port", port, "--no-open"])
    assert result.exit_code == 2, result.output
    assert "--port" in result.output, result.output
    assert started.context_kwargs == {}


@pytest.mark.parametrize("port", [0, 65536, 70000])
def test_claim_port_refuses_a_port_outside_the_range(port: int) -> None:
    factory = _Factory()
    with pytest.raises(BrainError, match="is not a usable port"):
        claim_port(port, socket_factory=factory)  # type: ignore[arg-type]
    assert factory.made == [], "a socket was opened for an impossible port"


def test_auto_port_stops_at_65535_with_a_message() -> None:
    """Walking up from 65530 used to reach 65536 and raise OverflowError."""

    class _AllBusy(_Factory):
        def __call__(self, family: int, kind: int) -> _RecordingSocket:
            sock = super().__call__(family, kind)
            sock.refuse = {"127.0.0.1": OSError(errno.EADDRINUSE, "in use")}
            return sock

    factory = _AllBusy()
    with pytest.raises(BrainError) as excinfo:
        claim_port(65530, attempts=20, socket_factory=factory)  # type: ignore[arg-type]
    assert "no free port found in 65530..65535" in str(excinfo.value)
    assert len(factory.made) == 6
    assert all(sock.closed for sock in factory.made)


def test_a_bind_that_raises_a_non_oserror_still_closes_its_socket() -> None:
    """A REAL socket and a real bind: past 65535 ``bind`` raises OverflowError,
    which ``except OSError`` let past without closing the socket."""
    made: list[socket.socket] = []

    def factory(family: int, kind: int) -> socket.socket:
        made.append(socket.socket(family, kind))
        return made[-1]

    with pytest.raises(OverflowError):
        _open_listener("127.0.0.1", "127.0.0.1", 70000, factory)
    (sock,) = made
    assert sock.fileno() == -1, "the socket leaked"


# --------------------------------------------------------------------------
# Review round 13 — a wildcard must not share a port with a loopback listener
# --------------------------------------------------------------------------


def _overlap_is_possible(family: int, loopback: str, wildcard: str) -> bool:
    """Can a SO_REUSEADDR wildcard bind over a live loopback listener here?

    macOS: yes (measured). Linux refuses it even with the option, so there is
    nothing for the guard to do and the test below skips, saying why.
    """
    with socket.socket(family, socket.SOCK_STREAM) as held:
        held.bind((loopback, 0))
        held.listen(1)
        port = held.getsockname()[1]
        with socket.socket(family, socket.SOCK_STREAM) as over:
            over.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                over.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                over.bind((wildcard, port))
            except OSError:
                return False
    return True


@pytest.mark.parametrize(
    ("family", "loopback", "wildcard"),
    [
        (socket.AF_INET, "127.0.0.1", "0.0.0.0"),
        pytest.param(socket.AF_INET6, "::1", "::", marks=needs_ipv6),
    ],
    ids=["ipv4", "ipv6"],
)
def test_a_wildcard_never_binds_over_another_process_on_loopback(
    family: int, loopback: str, wildcard: str
) -> None:
    """Review round 13, a token leak: on macOS ``--host 0.0.0.0`` bound port P
    although another process already listened on ``127.0.0.1:P``, because the
    socket set SO_REUSEADDR. The URL ``brain ui`` printed and opened —
    ``http://127.0.0.1:P/#t=<token>`` — then went to that other process.

    A REAL listener and the REAL claim_port: the wildcard must step past P, or
    with ``--no-auto-port`` say P is in use."""
    if not _overlap_is_possible(family, loopback, wildcard):
        pytest.skip("this platform refuses the overlap even with SO_REUSEADDR")
    with socket.socket(family, socket.SOCK_STREAM) as other_process:
        other_process.bind((loopback, 0))
        other_process.listen(1)
        taken = other_process.getsockname()[1]

        with pytest.raises(BrainError, match="already in use"):
            claim_port(taken, host=wildcard, auto=False)
        port, sockets = claim_port(taken, host=wildcard, auto=True)
        _close(sockets)
        assert port != taken, "the wildcard bound over another process's loopback port"
