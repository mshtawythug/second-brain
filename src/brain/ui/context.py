"""`UiContext` — the dependency-injection seam for the whole UI package.

Everything the app needs from the outside world arrives on one frozen
dataclass, built once by :mod:`brain.ui.server` and stashed on
``app.state.ui``. Route handlers read it; nothing reaches for a module-level
global.

That is a deliberate design choice with a specific payoff: **no test in this
package monkey-patches production code** (CLAUDE.md rule 13). A test that wants
a fake embedder, a recording ``search_fn``, a frozen clock, or a read-only
server constructs a ``UiContext`` with those values and calls ``create_app``.
There is no module state left to patch, so there is nothing to forget to
restore.

``conn_factory`` is a *factory*, not a connection: psycopg connections are not
safe to share across threads, and Starlette runs sync endpoints in a
threadpool. Each request opens and closes its own.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..errors import BrainError
from .watcher_probe import WatcherProbe

if TYPE_CHECKING:  # pragma: no cover — typing only
    import psycopg

    from ..config import Config
    from ..ingest import Embedder
    from .ingest_service import IngestRuntime
    from .jobs import JobRegistry

#: The hostnames a browser may legitimately use to reach a loopback bind.
#: ``TrustedHostMiddleware`` matches the Host header against this set, which is
#: what actually defeats DNS rebinding (see :mod:`brain.ui.security`).
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: The only HOSTNAMES that may be treated as loopback, and then only when every
#: address they resolve to is loopback (review round 16). A loopback bind runs
#: with no token and allowlists its own name, so trusting ANY name that happens
#: to resolve to 127.0.0.1 handed the DNS-rebinding defence to whoever controls
#: that name's DNS: `--host 127.0.0.1.nip.io` (a public service) ran tokenless,
#: and a page served from that name, once re-pointed at 127.0.0.1, sent an
#: allowlisted Host and the allowed Origin. Every other name is "other" and
#: needs a token.
#:
#: ``localhost`` only (review round 16b). ``ip6-localhost`` was trusted in
#: round 16 and is not: Debian's /etc/hosts defines it, macOS's does not, and
#: there ``getaddrinfo`` falls through to network DNS and the search domains,
#: so a hostile resolver could point it anywhere — and browsers do not
#: hard-wire it the way they do ``localhost``. Master never trusted it. The
#: literal ``::1`` (``[::1]``) is loopback by address and needs no name. A
#: trailing dot (``localhost.``) is a different Host value from ``localhost``
#: — a browser sends it as typed, and ``LOOPBACK_HOSTS`` does not hold it —
#: so it is "other" too, consistently with the Host check.
LOOPBACK_NAMES: frozenset[str] = frozenset({"localhost"})

#: The wildcard binds: "every interface", a bind address and never a
#: destination. Ruling W1 (phase 7): all three get exactly the loopback
#: ALLOWLIST — never wider than ``0.0.0.0`` already was. That allowlist is a
#: DNS-rebinding defence for BROWSERS, which send the host they were pointed
#: at; it is not access control. Any program on the network that reaches the
#: port can send ``Host: 127.0.0.1`` by hand (measured by the phase-7 audit),
#: so on every non-loopback bind it is the TOKEN that keeps other clients out
#: (review round 7: this said "reachable from this machine and from nowhere
#: else"). The allowlist is the only thing the wildcards share: ``::`` is IPv6-only (asyncio sets
#: ``IPV6_V6ONLY``), so its local URL is ``[::1]`` and ``127.0.0.1`` is refused
#: at connect. W1 also needs ``starlette>=1.7``: older TrustedHostMiddleware
#: split ``[::1]:8765`` on the first colon (see the pin in pyproject.toml).
WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", "[::]"})  # noqa: S104


def _utcnow() -> datetime:
    """Default clock. Injectable via ``UiContext.now_fn`` so tests can freeze it."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class UiContext:
    """Everything a request handler is allowed to depend on.

    :param cfg: the loaded :class:`~brain.config.Config`. Read-only here — the
        UI never rewrites configuration.
    :param conn_factory: called per request; must return a context manager
        yielding an autocommit-capable psycopg connection.
    :param embedder: the configured embedder, passed straight through to
        ``hybrid_search`` and ``sync_one_file``. May be an FTS-only
        ``NullEmbedder``.
    :param search_fn: ``brain.search.hybrid_search`` by default. Injected so a
        test can assert the exact kwargs the route passes without reaching into
        the search module — and so a test never opens a socket to Ollama.
    :param now_fn: the clock, for date-range parsing and telemetry.
    :param read_only: when true the security middleware refuses every non-safe
        method **before routing**. See :mod:`brain.ui.security` for why that
        placement matters.
    :param token: shared secret required on every request when set. Empty on a
        loopback bind, mandatory on any other.
    :param allowed_origin: the exact ``Origin`` value mutations must carry,
        e.g. ``http://127.0.0.1:8765``.
    :param allowed_hosts: the Host-header allowlist.
    :param logging_enabled: false on a pre-024 database, where ``source='ui'``
        trips a CHECK constraint that ``record_search_query`` does not swallow
        and which would therefore 500 every search.
    :param graph_syncer: optional people-graph syncer handed to delete/update.
    :param notices: startup warnings (e.g. telemetry disabled) surfaced by the
        CLI panel and by ``GET /api/health``.
    """

    cfg: Config
    conn_factory: Callable[[], AbstractContextManager[psycopg.Connection[Any]]]
    embedder: Embedder
    search_fn: Callable[..., Any]
    now_fn: Callable[[], datetime] = _utcnow
    read_only: bool = False
    token: str = ""
    allowed_origin: str = "http://127.0.0.1:8765"
    allowed_hosts: frozenset[str] = LOOPBACK_HOSTS
    logging_enabled: bool = False
    graph_syncer: Any | None = None
    notices: tuple[str, ...] = field(default=())
    #: Whether a ``sensitivity='confidential'`` document may have its BODY sent
    #: over the wire. True on a loopback bind (a loopback address, or
    #: ``localhost`` resolving only to loopback; since round 16 a loopback
    #: ALIAS is not one), where the UI is exactly as
    #: inside the trust boundary as ``brain show`` is. False on a non-loopback
    #: bind — there the corpus is crossing a network, which is a different
    #: trust question than "the user is looking at their own machine" — unless
    #: the operator opts in with ``--include-confidential``. The tier itself is
    #: always reported, so the UI can label the note either way; only the body
    #: is withheld. Set by ``server.build_context``.
    serve_confidential_bodies: bool = True
    #: Whether the UNPROMPTED listing surfaces — the vault tree, the recent
    #: rail, the tag index — may name a ``sensitivity='confidential'``
    #: document. **Separate from** :attr:`serve_confidential_bodies` and
    #: defaulting the other way, by ruling.
    #:
    #: The bodies flag answers "may this session read a confidential note it
    #: asked for", and is true on loopback. This one answers "may a list the
    #: user never requested paint confidential titles on load", which is a
    #: different question with a different safe answer: the tree renders in the
    #: same viewport, on the same paint, as everything else, so a confidential
    #: title arrives before any intent to see it does. Reusing the bodies flag
    #: here would gate an unprompted title list on a name that says bodies.
    #:
    #: The three surfaces are gated on THIS flag identically. Explicitly
    #: requested surfaces — a direct note fetch, a typed search query — keep
    #: using :attr:`serve_confidential_bodies`. Set from
    #: ``cfg.ui_serve_confidential_titles`` by ``server.build_context``; the
    #: dataclass default is the fail-closed one so a context built without it
    #: (every test fixture that does not care) hides rather than leaks.
    serve_confidential_titles: bool = False
    #: Answers ``GET /api/watcher`` (A9's UI half): is the vault-sync watcher
    #: running. One probe per app, so its TTL cache is shared by every tab.
    #: Injected so a test can fix the answer instead of forking ``ps``.
    watcher_probe: WatcherProbe = field(default_factory=WatcherProbe)
    #: The job registry behind the Ingest tab (Ingest spec §4.1). A reference to
    #: a mutable object, the same shape as ``watcher_probe``. ``None`` (every test
    #: fixture that does not care) makes the ingest routes answer 503
    #: ``jobs_unavailable``. Built by ``server.build_context``; the app lifespan
    #: starts and stops its worker.
    jobs: JobRegistry | None = None
    #: The job's own embedder and enricher factories (§5.3). ``None`` is treated
    #: like a missing registry.
    ingest_runtime: IngestRuntime | None = None
    #: Whether this bind may ingest at all (D8): loopback-bound and writable.
    #: Fail-closed default, like ``serve_confidential_titles``; set by
    #: ``server.build_context`` as ``loopback and not read_only``.
    ingest_enabled: bool = False

    def connect(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        """Open a per-request connection. Always used as a ``with`` block."""
        return self.conn_factory()


def host_allowlist(host: str, resolved: Resolved | None = None) -> frozenset[str]:
    """The Host-header allowlist for a server bound to ``host``.

    A loopback bind accepts every spelling of loopback, because the browser may
    have been opened at any of them. Any other bind accepts **only** the exact
    hostname it was given — widening that set is precisely what would reopen
    the DNS-rebinding hole this allowlist exists to close.

    ``TrustedHostMiddleware`` strips the port before comparing, so only bare
    hostnames belong here. From starlette 1.7 it keeps the brackets on an IPv6
    literal, which is why ``::`` could never match ``{"::"}`` before ruling W1
    — and why the starlette floor is 1.7: earlier releases cut ``[::1]:8765``
    to ``[`` and refused it.

    The set holds the Host value as it ARRIVES (review round 5): an IPv6
    literal bracketed and canonical (``[fd00::5]``), because a browser
    serialises the URL's address compressed and lower-case. Holding the bare
    ``fd00::5`` refused every request to an IPv6 bind, its own URL included.
    The input is normalised first, so ``0:0:0:0:0:0:0:1`` is loopback and
    ``FD00:0:0::5`` is ``[fd00::5]``.
    """
    address = bind_address(host)
    kind = address_class(address, resolved)
    if kind == "wildcard":
        return LOOPBACK_HOSTS
    own = f"[{address}]" if ":" in address else address
    if kind == "loopback":
        # Loopback by ADDRESS (review round 9): 127.0.0.2 is loopback although
        # no spelling in LOOPBACK_HOSTS names it, and its own URL must work.
        return LOOPBACK_HOSTS | {own}
    return frozenset({own})


def bind_address(host: str) -> str:
    """``host`` as a socket bind address: an IPv6 literal without its brackets.

    Brackets belong in a URL (:func:`bind_origin` adds them back), never in a
    bind — ``--host [::]`` reached uvicorn verbatim and died in ``getaddrinfo``
    (``gaierror 8``) after the panel had already printed (phase-7 review
    round 3). ``brain ui`` normalises once, at the CLI.

    An IP literal is also made canonical (review round 5) — ``FD00:0:0::5``
    becomes ``fd00::5`` and ``0:0:0:0:0:0:0:1`` becomes ``::1`` — so every
    consumer sees one spelling, the one a browser will send. A hostname is
    lower-cased, for the same reason (review round 6).
    """
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        # A hostname: lower-cased, because a browser lower-cases the host it
        # sends and starlette compares Host case-sensitively, so `--host
        # LOCALHOST` refused its own URL (review round 6).
        return bare.lower()


#: An RFC 1123 hostname: dot-separated labels of letters, digits and inner
#: hyphens, optionally fully qualified with a trailing dot.
_HOSTNAME = re.compile(
    r"(?i)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?",
    re.ASCII,
)
#: RFC 1035's limit on a name's text form, without the trailing root dot.
_HOSTNAME_MAX = 253


class HostRefused(BrainError):
    """``--host`` names something ``brain ui`` will not bind; the message says why."""


Resolved = tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


def literal_addresses(host: str) -> Resolved:
    """``(address,)`` for an IP literal, ``()`` for a hostname. No DNS."""
    try:
        return (ipaddress.ip_address(bind_address(host)),)
    except ValueError:
        return ()


def classify_addresses(addresses: Resolved) -> str:
    """``"wildcard"``, ``"loopback"`` or ``"other"`` for a set of bound addresses.

    ``server.claim_port`` binds EVERY address a host resolves to, so ANY
    unspecified address makes the bind a wildcard, and it is loopback only if
    ALL are loopback (review round 10). No addresses — an unresolved name — is
    "other".
    """
    if not addresses:
        return "other"
    if any(address.is_unspecified for address in addresses):
        return "wildcard"
    if all(address.is_loopback for address in addresses):
        return "loopback"
    return "other"


def address_class(host: str, resolved: Resolved | None = None) -> str:
    """``"wildcard"``, ``"loopback"`` or ``"other"`` — judged on the ADDRESS.

    Review round 9 stopped classifying by SPELLING; review round 10 stopped
    classifying a HOSTNAME by its name. A literal is judged on itself; a
    hostname only on ``resolved``, the addresses the bind resolves it to
    (:func:`brain.ui.server.resolve_bind_addresses`). Unresolved, a hostname
    is "other" — ``localhost`` included, which used to be trusted as loopback by
    name alone, so a ``localhost`` mapped elsewhere skipped the token.

    Review round 16: a hostname resolving only to loopback is loopback only if
    it is one of :data:`LOOPBACK_NAMES`; any other such name is "other". Its
    addresses are unchanged — :func:`classify_addresses` still says "loopback"
    for them, which is how :func:`is_loopback_alias` tells the user why.
    """
    if is_loopback_alias(host, resolved):
        return "other"
    return classify_addresses(resolved if resolved is not None else literal_addresses(host))


def is_loopback_alias(host: str, resolved: Resolved | None) -> bool:
    """True for a hostname outside :data:`LOOPBACK_NAMES` that resolves only to loopback.

    Such a name binds loopback but is guarded like a network bind — a token,
    a Host allowlist of the name alone, confidential bodies withheld — because
    whoever controls its DNS could re-point it (review round 16).
    """
    if literal_addresses(host) or bind_address(host) in LOOPBACK_NAMES:
        return False
    return classify_addresses(resolved or ()) == "loopback"


def validate_host(host: str) -> str:
    """Return ``host`` as the canonical address it will bind, or raise :class:`HostRefused`.

    The ONE place a ``--host`` spelling is judged. Review rounds 7–9 each found
    a spelling that bound something unexpected because it was CLASSIFIED BY
    ITS SPELLING; the rule since round 9 is to resolve it the way the bind will
    — numeric-only ``getaddrinfo``, which never touches DNS — and judge the
    result. In order:

    * empty, or surrounding whitespace — asyncio binds ``0.0.0.0`` AND ``::``
      for ``""``; `` 127.0.0.1`` failed later as "no free port";
    * any ``%`` zone, link-local included (ruling N1): a browser cannot open a
      zoned URL, so the printed address could never be used; and an EMPTY zone
      (``::%``) was rejected by :mod:`ipaddress` yet read as ``::`` by the
      resolver — a wildcard with no notice;
    * brackets other than one balanced pair around an IPv6 literal;
    * a numeric spelling the resolver reads differently from :mod:`ipaddress`
      (``0`` is 0.0.0.0; platforms disagree on ``010.0.0.1``) — refused, naming
      what the system would bind;
    * an IPv4-mapped IPv6 address, which the IPv6-only socket cannot bind.

    A spelling that does not resolve numerically is a hostname: lower-cased,
    because a browser lower-cases the host it sends.
    """
    if not host.strip():
        raise HostRefused(
            "--host must not be empty; omit it for 127.0.0.1, or name an address."
        )
    if host != host.strip():
        raise HostRefused(f"--host {host!r} has leading or trailing whitespace.")
    if not host.isascii():
        # Review round 10: the hostname pattern lacked re.ASCII, so `localhoſt`
        # and the Kelvin sign matched, and `İ` was rewritten by lower().
        raise HostRefused(f"--host {host!r} must be ASCII.")
    if "%" in host:
        raise HostRefused(
            f"--host {host}: zone IDs (%…) are not supported — a browser cannot "
            "open a URL that carries one."
        )
    if "[" in host or "]" in host:
        balanced = (
            host.startswith("[") and host.endswith("]")
            and host.count("[") == 1 and host.count("]") == 1
        )
        if not balanced:
            raise HostRefused(f"--host {host}: unbalanced or misplaced brackets.")
        try:
            inner = ipaddress.ip_address(host[1:-1])
        except ValueError:
            inner = None
        if not isinstance(inner, ipaddress.IPv6Address):
            raise HostRefused(
                f"--host {host}: brackets are for IPv6 literals only; "
                "write the address or hostname without them."
            )
    bare = host[1:-1] if host.startswith("[") else host
    not_a_host = HostRefused(
        f"--host {host} is not an IP address or a hostname; pass the "
        "address alone (a port goes in --port)."
    )
    try:
        infos = socket.getaddrinfo(
            bare, None, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST
        )
    except UnicodeError:
        # The idna codec refuses an empty or over-long label (`.`, `a..b`, 64
        # characters); it escaped as a raw traceback (review round 10).
        raise not_a_host from None
    except socket.gaierror:
        # Not numeric, so it must be a HOSTNAME (RFC 1123 labels). Anything
        # else — `127.0.0.1:8080`, a URL, `0.0.0.0/0` — failed at the bind as
        # a resolver error the user could not act on (final round).
        if not _HOSTNAME.fullmatch(bare) or len(bare.rstrip(".")) > _HOSTNAME_MAX:
            raise not_a_host from None
        return bare.lower()
    resolved = {ipaddress.ip_address(info[4][0]) for info in infos}
    try:
        spelled: ipaddress.IPv4Address | ipaddress.IPv6Address | None = (
            ipaddress.ip_address(bare)
        )
    except ValueError:
        spelled = None
    if spelled is None or resolved != {spelled}:
        reading = ", ".join(sorted(str(address) for address in resolved))
        raise HostRefused(
            f"--host {host} is not a canonical IP address (the system reads it "
            f"as {reading}); write the address out in full."
        )
    if isinstance(spelled, ipaddress.IPv6Address) and spelled.is_link_local:
        # A link-local address binds only with a zone, and zones are refused
        # (N1); without this it failed at the bind as "not owned" (final round).
        raise HostRefused(
            f"--host {host}: link-local addresses need a zone ID to bind, and "
            "zone IDs are not supported — use a global or loopback address."
        )
    if isinstance(spelled, ipaddress.IPv6Address) and spelled.ipv4_mapped is not None:
        raise HostRefused(
            f"--host {host}: IPv4-mapped IPv6 addresses cannot be bound (the "
            f"server's IPv6 socket is IPv6-only); write {spelled.ipv4_mapped}."
        )
    return str(spelled)


def bind_origin(host: str, port: int, resolved: Resolved | None = None) -> str:
    """The origin a browser on THIS machine uses to reach a server bound to ``host``.

    Ruling W2 (phase 7): the single source for both the URL ``brain ui`` prints
    and opens (:func:`brain.ui.server.browser_url`) and the ``Origin`` its write
    guard expects (``UiContext.allowed_origin``). Before W2 the guard expected
    ``http://0.0.0.0:<port>`` — an origin no browser sends — so a wildcard bind
    could be read but never written, and its printed URL answered 400.

    A wildcard maps to the matching loopback literal (IPv4 ``127.0.0.1``, IPv6
    ``[::1]``), which :func:`host_allowlist` accepts; an IPv6 literal is
    bracketed, as a URL requires; anything else passes through unchanged.
    """
    address = bind_address(host)
    addresses = resolved if resolved is not None else literal_addresses(address)
    if classify_addresses(addresses) == "wildcard":
        # The loopback of a family the wildcard actually covers; a name that
        # resolves to 0.0.0.0 is IPv4 though it has no colon (round 10). The
        # IPv4 address must be the UNSPECIFIED one: a name resolving to
        # (192.0.2.5, ::) binds no IPv4 loopback at all (round 11).
        v4_wildcard = any(a.is_unspecified and a.version == 4 for a in addresses)
        url_host = "127.0.0.1" if v4_wildcard else "[::1]"
    elif ":" in address:
        url_host = f"[{address}]"
    else:
        url_host = address
    return f"http://{url_host}:{port}"
