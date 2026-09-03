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

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    import psycopg

    from ..config import Config
    from ..ingest import Embedder

#: The hostnames a browser may legitimately use to reach a loopback bind.
#: ``TrustedHostMiddleware`` matches the Host header against this set, which is
#: what actually defeats DNS rebinding (see :mod:`brain.ui.security`).
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


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
    #: over the wire. True on a loopback bind, where the UI is exactly as
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

    def connect(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        """Open a per-request connection. Always used as a ``with`` block."""
        return self.conn_factory()


def host_allowlist(host: str) -> frozenset[str]:
    """The Host-header allowlist for a server bound to ``host``.

    A loopback bind accepts every spelling of loopback, because the browser may
    have been opened at any of them. Any other bind accepts **only** the exact
    hostname it was given — widening that set is precisely what would reopen
    the DNS-rebinding hole this allowlist exists to close.

    ``TrustedHostMiddleware`` strips the port before comparing, so only bare
    hostnames belong here.
    """
    if host in LOOPBACK_HOSTS or host == "0.0.0.0":  # noqa: S104 — compared, not bound
        return LOOPBACK_HOSTS
    return frozenset({host})
