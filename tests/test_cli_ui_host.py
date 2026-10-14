"""`brain ui --host`: every spelling, validated once and classified by what it resolves to.

Split out of ``tests/test_cli_ui.py`` in review round 11, when that module grew
past the 800-line ceiling. The rules under test live in ONE function,
``brain.ui.context.validate_host`` (round 9), plus the resolution in
``brain.ui.server.resolve_bind_addresses`` (round 10); the one table,
``HOST_SPELLINGS``, is where each newly found spelling goes. Every hostname
resolves through ``tests.ui_cli_support.FAKE_DNS``, never real DNS.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from brain.ui.lan import WILDCARD_NOTICE
from brain.ui.server import claim_port
from tests.ui_cli_support import (
    FAKE_DNS,
    Started,
    fake_resolver,
    install_started,
    ipv6_loopback_available,
    ipv6_wildcard_available,
    make_app,
    runner,
)


@pytest.fixture
def app() -> typer.Typer:
    return make_app()


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Started:
    return install_started(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("given", "bound", "url"),
    [
        ("[::]", "::", "http://[::1]:8765/#t=x"),
        ("[::1]", "::1", "http://[::1]:8765/#t=x"),
    ],
)
def test_a_bracketed_host_is_bound_without_its_brackets(
    app: typer.Typer, started: Started, given: str, bound: str, url: str
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
    app: typer.Typer, started: Started
) -> None:
    """``[::1]`` is loopback once normalised, exactly as ``::1`` is."""
    result = runner.invoke(app, ["ui", "--host", "[::1]", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "::1"


def test_a_non_canonical_loopback_binds_as_its_canonical_form(
    app: typer.Typer, started: Started
) -> None:
    """Round 5, item 1: ``0:0:0:0:0:0:0:1`` IS loopback; it binds as ``::1``."""
    result = runner.invoke(app, ["ui", "--host", "0:0:0:0:0:0:0:1", "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "::1"


@pytest.mark.parametrize("spelling", ["LOCALHOST", "LocalHost"])
def test_an_upper_case_localhost_binds_as_localhost(
    app: typer.Typer, started: Started, spelling: str
) -> None:
    """Round 6, item 2: a browser lower-cases the host, and starlette compares
    Host case-sensitively, so ``--host LOCALHOST`` refused its own URL."""
    result = runner.invoke(app, ["ui", "--host", spelling, "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["host"] == "localhost"
    assert "http://localhost:8765/" in result.output


@pytest.mark.parametrize("spelling", ["::0", "0:0:0:0:0:0:0:0"])
def test_a_non_canonical_ipv6_wildcard_is_the_wildcard(
    app: typer.Typer, started: Started, spelling: str
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
#:   ("unowned", phrase)  exit 1 from the REAL resolution and bind, saying
#:                        `phrase` and never "in use" — the address is not
#:                        this machine's, or the name resolves to none;
#:   ("unowned-on-darwin", phrase)  that on macOS, which owns only 127.0.0.1
#:                        of 127/8; Linux binds the whole block, so exit 0;
#:   ("binds-real", host) exit 0 through the REAL bind, whose live sockets are
#:                        on exactly the addresses `host` resolved to (round 11).
#: "binds"/"wildcard" are decided by the address the system will actually
#: bind (numeric getaddrinfo), not by the spelling — the review-round-9 rule.
#: Their bind is replaced, so they test classification; "unowned" and
#: "binds-real" rows run the real one (the final round: only EADDRINUSE means
#: "busy").
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
    # real bind: addresses this machine does not own, or names that do not resolve
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
    # IPv6-only resolution is bound as IPv6 (round 10, item 3)
    ("ip6-localhost", "binds-real", "ip6-localhost"),
    # Round 16: binds (with the table's --token), guarded like a network bind
    ("loopback-alias.example", "binds", "loopback-alias.example"),
    # Round 16b: a different name from localhost; binds only with the token
    ("localhost.", "binds", "localhost."),
    ("FD00:0:0::5", "binds", "fd00::5"),
]


@pytest.mark.parametrize(("given", "kind", "expected"), HOST_SPELLINGS)
def test_every_host_spelling_does_what_the_table_says(
    app: typer.Typer,
    started: Started,
    monkeypatch: pytest.MonkeyPatch,
    given: str,
    kind: str,
    expected: str,
) -> None:
    """Rounds 7–9 and the final round, one row per spelling. A refusal happens
    in ``context.validate_host`` before any bind; a bind is classified by the
    address it resolves to; an unowned address fails in the REAL bind, never as
    "in use"."""
    import sys

    import brain.ui.server as server

    if kind.startswith("unowned"):
        if ":" in given and not ipv6_wildcard_available():
            pytest.skip("this host cannot open an IPv6 socket")
        # The real bind: `claim_port` was imported at module top, before the
        # fixture replaced the module attribute.
        monkeypatch.setattr(server, "claim_port", claim_port)
        if kind == "unowned-on-darwin" and sys.platform != "darwin":
            kind = "binds-real"
    if kind == "binds-real":
        # Round 15b: what the row binds for real, not whether `::` binds —
        # `ip6-localhost` is ::1, which a host can lack while `::` binds.
        binds_ipv6 = any(":" in a for a in FAKE_DNS.get(given, [given]))
        if binds_ipv6 and not ipv6_loopback_available():
            pytest.skip("this host has no IPv6 loopback (::1)")
        monkeypatch.setattr(server, "claim_port", claim_port)
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
        assert started.context_kwargs["host"] == given
        # Round 11: serve() got live sockets on exactly the resolved addresses.
        assert [address for address, _ in started.bound] == FAKE_DNS.get(given, [given])
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


def test_localhost_that_does_not_resolve_to_loopback_needs_a_token(
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
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
    app: typer.Typer, started: Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loopback only if EVERY resolved address is loopback: uvicorn binds them all."""
    monkeypatch.setitem(FAKE_DNS, "localhost", ["127.0.0.1", "192.0.2.30"])
    result = runner.invoke(app, ["ui", "--host", "localhost", "--no-open"])
    assert result.exit_code == 2, result.output
    assert "pass --token" in result.output, result.output
    # Round 16b, the lead's edge case (b): a TRUSTED name with a mixed answer is
    # plain "other" — the network-bind refusal, not the alias one.
    assert "only localhost is trusted" not in result.output, result.output


@pytest.mark.parametrize("name", ["localhost", "LOCALHOST"])
def test_a_trusted_loopback_name_needs_no_token(
    app: typer.Typer, started: Started, name: str
) -> None:
    """LOOPBACK_NAMES resolving only to loopback stay tokenless. ``LOCALHOST``
    is lower-cased by validate_host, as a browser lower-cases the Host it sends;
    ``ip6-localhost`` left this list in round 16b (see the refusal test)."""
    result = runner.invoke(app, ["ui", "--host", name, "--no-open"])
    assert result.exit_code == 0, result.output
    assert started.context_kwargs["token"] == ""
    assert "bodies served (loopback bind)" in result.output, result.output


@pytest.mark.parametrize("name", ["loopback-alias.example", "ip6-localhost", "localhost."])
def test_a_loopback_alias_without_a_token_is_refused(
    app: typer.Typer, started: Started, name: str
) -> None:
    """Round 16 (phase-7 audit of `cb4b6ed`, finding 1): a name outside
    LOOPBACK_NAMES that resolves only to 127.0.0.1 ran with NO token and
    allowlisted its own name — `--host 127.0.0.1.nip.io` was measured doing so.
    Whoever controls that name's DNS could rebind it onto this server.

    Round 16b: ``ip6-localhost`` is such a name (macOS resolves it through
    network DNS), and so is ``localhost.`` — the refusal says to drop the dot."""
    result = runner.invoke(app, ["ui", "--host", name, "--no-open"])
    # Rich wraps the usage error in a box; read it as one line of words.
    flat = " ".join(result.output.replace("│", " ").split())
    assert result.exit_code == 2, result.output
    assert "only localhost is trusted as a loopback name" in flat, result.output
    assert "pass --token" in flat, result.output
    assert ("drop the trailing dot to use localhost" in flat) is name.endswith("."), flat
    assert started.context_kwargs == {}


def test_a_loopback_alias_with_a_token_is_guarded_like_a_network_bind(
    app: typer.Typer, started: Started
) -> None:
    """With --token it serves, and the panel says why it is guarded — not the
    false "NOT bound to loopback"."""
    result = runner.invoke(
        app, ["ui", "--host", "loopback-alias.example", "--token", "x", "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert "guarded like a network bind" in result.output, result.output
    assert "NOT bound to loopback" not in result.output, result.output
    assert "bodies withheld (loopback alias" in result.output, result.output
    assert "non-loopback bind" not in result.output, result.output
    assert "http://loopback-alias.example:8765/#t=x" in result.output, result.output


def test_a_loopback_alias_with_include_confidential_never_says_withheld(
    app: typer.Typer, started: Started
) -> None:
    """Round 16b: --include-confidential DOES serve bodies on an alias
    (build_context's lens), so neither the Confidential line nor the alias
    note may claim they are withheld."""
    result = runner.invoke(app, [
        "ui", "--host", "loopback-alias.example", "--token", "x",
        "--include-confidential", "--no-open",
    ])
    assert result.exit_code == 0, result.output
    assert "withheld" not in result.output, result.output
    assert "bodies SERVED on a loopback alias" in result.output, result.output


def test_the_cli_threads_the_resolved_addresses_into_the_context(
    app: typer.Typer, started: Started
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


def test_resolve_bind_addresses_reports_an_empty_answer() -> None:
    """Round 16 (audit finding 3): a resolver answering with NO addresses binds
    nothing — reported, never returned as an empty bind set."""
    from brain.errors import BrainError
    from brain.ui.server import resolve_bind_addresses

    def empty(*args: object) -> list[object]:
        return []

    with pytest.raises(BrainError) as excinfo:
        resolve_bind_addresses("brain-1.lan", resolver=empty)
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
