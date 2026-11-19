"""Resolve a `pg_dump` / `pg_restore` that is version-compatible with the server."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg

from ..errors import BackupError, PgToolUnavailable

#: Seconds allowed for a `--version` probe. Generous: a cold `docker exec` on a
#: busy daemon is slow, but a probe that hangs must not wedge the command.
PROBE_TIMEOUT_S = 30.0
#: Seconds allowed for a full `pg_dump`. A 1,200-document corpus dumps in well
#: under a minute; the ceiling exists so a wedged dump fails rather than hangs.
DUMP_TIMEOUT_S = 3600.0
#: Seconds allowed for a full `pg_restore` — twice the dump budget, because a
#: restore also rebuilds every index (including HNSW over `chunks.embedding`).
RESTORE_TIMEOUT_S = 7200.0

#: The only tools this module will ever resolve. Guards against a caller
#: passing an arbitrary binary name that would then be exec'd in the container.
SUPPORTED_TOOLS = ("pg_dump", "pg_restore")

# `pg_dump (PostgreSQL) 16.14 (Debian 16.14-1.pgdg12+1)` -> ("16", "14")
# `pg_dump (PostgreSQL) 14.23 (Homebrew)`                -> ("14", "23")
_VERSION_RE = re.compile(r"^\S+ \(PostgreSQL\) (\d+)\.(\d+)")

_REMEDIES = (
    "  • start the brain container so the matching pg_dump can be used:\n"
    "      brain-up          (or: docker compose --project-name brain up -d)\n"
    "  • or install PostgreSQL 16+ client tools:\n"
    "      brew install postgresql@16"
)

#: Hosts that could plausibly be a container published on this machine. A DSN
#: naming anything else cannot be served by a local `docker exec`.
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Postgres' default port, used when a DSN omits one.
_DEFAULT_PG_PORT = 5432


@dataclass(frozen=True)
class DsnTarget:
    """Where a DSN actually points — the thing a dump must land on.

    Carries no password: this is what error messages print, and a backup must
    never leak a credential into a terminal or a log.
    """

    host: str | None
    port: int | None
    dbname: str

    @property
    def is_local(self) -> bool:
        return (self.host or "").lower() in _LOCAL_HOSTS

    def __str__(self) -> str:
        where = f"{self.host or 'localhost'}:{self.port or _DEFAULT_PG_PORT}"
        return f"{self.dbname} @ {where}"


def dsn_target(database_url: str) -> DsnTarget:
    """Parse a DSN into the (host, port, dbname) triple a dump must match."""
    parsed = psycopg.conninfo.conninfo_to_dict(database_url)
    raw_port = parsed.get("port")
    return DsnTarget(
        host=str(parsed["host"]) if parsed.get("host") else None,
        port=int(str(raw_port)) if raw_port else None,
        dbname=str(parsed.get("dbname") or ""),
    )


class CommandRunner(Protocol):
    """Boundary for every external process this package spawns.

    ``env``, when given, replaces the child environment — it is how the host
    leg passes ``PGPASSWORD`` without putting the password on the command line.
    The container leg uses ``docker exec --env-file`` instead and leaves ``env``
    at ``None``. The design sketch omitted ``env``; it is required, because a
    host-leg dump has no other password channel that keeps the secret out of
    ``ps`` output.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    """The production :class:`CommandRunner` — a checked ``subprocess.run``."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )


@dataclass(frozen=True)
class PgToolPlan:
    """How to invoke a pg_dump/pg_restore that is version-compatible with the server.

    ``argv_prefix`` is a tuple rather than the list the design sketch showed, so
    the frozen dataclass is genuinely immutable — a list field would let callers
    mutate shared state through a nominally frozen object. Build a full command
    line with :meth:`argv`, which splices in the per-invocation ``--env-file``
    the password travels through.
    """

    tool: str
    source: str
    version: str
    major: int
    container: str | None
    argv_prefix: tuple[str, ...]

    def argv(self, *args: str, env_file: Path | None = None) -> list[str]:
        """Full argv for one invocation.

        For the container leg the ``--env-file`` lands between ``docker exec``
        and the container name, which is where Docker requires it. For the host
        leg there is no env-file: the password travels via :meth:`env`.
        """
        if self.source != "container":
            return [*self.argv_prefix, *args]
        head = ["docker", "exec"]
        if env_file is not None:
            head += ["--env-file", str(env_file)]
        # argv_prefix[:2] is the ("docker", "exec") the head just rebuilt.
        return [*head, *self.argv_prefix[2:], *args]

    def env(self, password: str | None) -> dict[str, str]:
        """Child environment for the host leg — ours plus ``PGPASSWORD``."""
        environ = dict(os.environ)
        if password:
            environ["PGPASSWORD"] = password
        return environ


@contextmanager
def password_env_file(password: str | None) -> Iterator[Path | None]:
    """Yield a ``0600`` file holding ``PGPASSWORD=…``, removed on exit.

    The password is never placed in argv, where any user on the machine could
    read it out of ``ps``. ``docker exec --env-file`` consumes this file
    instead. Yields ``None`` when there is no password to pass, so callers need
    no special case for a passwordless DSN.
    """
    if not password:
        yield None
        return
    # `mkstemp` creates the file 0600 from the outset, so the secret is never
    # briefly world-readable the way a create-then-chmod would leave it.
    handle_fd, name = tempfile.mkstemp(prefix="brain-pgpass-", suffix=".env")
    path = Path(name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(f"PGPASSWORD={password}\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def server_version(conn: Any) -> tuple[str, int]:
    """Return ``(display_version, version_num)`` for the connected server.

    ``version_num`` is Postgres' own integer encoding (``160014`` for 16.14);
    the major version is ``version_num // 10000``.
    """
    row = conn.execute("SHOW server_version_num").fetchone()
    if row is None:  # pragma: no cover — SHOW always returns exactly one row
        raise BackupError("could not read server_version_num from the database")
    version_num = int(row[0])
    display_row = conn.execute("SHOW server_version").fetchone()
    display = str(display_row[0]) if display_row is not None else str(version_num)
    return display, version_num


def _parse_version(output: str, *, tool: str, source: str) -> tuple[str, int]:
    """Parse ``<tool> (PostgreSQL) X.Y`` into ``("X.Y", X)``."""
    match = _VERSION_RE.match(output.strip())
    if match is None:
        raise PgToolUnavailable(
            f"Could not parse the version of the {source} {tool}. Expected "
            f"output like '{tool} (PostgreSQL) 16.14' but got: "
            f"{output.strip()!r}"
        )
    major_text, minor_text = match.group(1), match.group(2)
    return f"{major_text}.{minor_text}", int(major_text)


def _probe(runner: CommandRunner, argv: Sequence[str]) -> str | None:
    """Run a ``--version`` probe, returning stdout, or ``None`` if it failed.

    A probe failure is expected and recoverable (Docker down, container
    stopped, binary absent), so it becomes ``None`` rather than an exception —
    the caller decides whether a fallback exists.
    """
    try:
        completed = runner.run(argv, timeout=PROBE_TIMEOUT_S)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None
    return completed.stdout or ""


def container_published_ports(
    container: str, runner: CommandRunner
) -> frozenset[int] | None:
    """Host ports the container publishes for Postgres, or ``None`` if unknown.

    ``docker port <container> 5432/tcp`` prints one ``<addr>:<port>`` per line
    (``0.0.0.0:55432`` and ``[::]:55432`` for the brain's own stack). ``None``
    means the question could not be answered — the daemon is down, or the
    container is not running — which is different from "publishes nothing".
    """
    output = _probe(runner, ["docker", "port", container, "5432/tcp"])
    if output is None:
        return None
    ports = set()
    for line in output.splitlines():
        _, _, tail = line.strip().rpartition(":")
        if tail.isdigit():
            ports.add(int(tail))
    return frozenset(ports) if ports else None


def discover_container(target: DsnTarget, runner: CommandRunner) -> str | None:
    """Find the running container that publishes ``target``'s port to Postgres.

    The configured container is only *one* candidate. A brain pointed at the
    demo sandbox (55433) or a test database is served by a different container
    entirely, and without this the container leg is unavailable to it — leaving
    a machine whose host ``pg_dump`` is too old with no way to back up at all.

    ``docker ps`` renders ports as ``0.0.0.0:5434->5432/tcp, [::]:5434->…``, so
    the published-to-internal arrow is what identifies the mapping.
    """
    if not target.is_local:
        return None
    output = _probe(runner, ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    if output is None:
        return None
    wanted = f":{target.port or _DEFAULT_PG_PORT}->{_DEFAULT_PG_PORT}/tcp"
    for line in output.splitlines():
        name, _, ports = line.partition("\t")
        if wanted in ports and name.strip():
            return name.strip()
    return None


def container_serves(
    container: str, target: DsnTarget, runner: CommandRunner
) -> bool:
    """Is ``target`` actually the database server inside ``container``?

    A `docker exec`'d pg_dump connects over the container's *local socket*, so
    the DSN's host and port are ignored entirely. Choosing that leg for a DSN
    pointing somewhere else does not fail cleanly — it dumps whatever database
    of that name happens to live in the container. Verified against a QA DSN on
    port 5434: the container leg silently targeted the production container's
    server instead.
    """
    if not target.is_local:
        return False
    published = container_published_ports(container, runner)
    if published is None:
        return False
    return (target.port or _DEFAULT_PG_PORT) in published


def resolve_pg_tool(
    tool: str,
    *,
    server_major: int,
    container: str,
    runner: CommandRunner,
    database_url: str,
    which: Callable[[str], str | None] = shutil.which,
) -> PgToolPlan:
    """Pick a version-compatible ``tool`` for the server ``database_url`` names.

    **The leg follows the target, not the other way round.** A `docker exec`'d
    tool talks to the container's own socket and ignores the DSN's host/port, so
    the container leg is used *only* when the DSN provably resolves to that
    container's published port. Otherwise the host leg is used, where ``-h``/
    ``-p`` are honoured. Selecting the container leg on tool-version alone let a
    QA DSN on port 5434 reach the production container instead — and had a
    same-named database existed there, it would have produced a successful
    backup full of the wrong data, discoverable only at restore time.

    PostgreSQL additionally requires the utility to be **>= the server's major
    version**; an older one aborts with ``server version mismatch``. Refusing
    beats a dump that aborts partway and leaves a plausible-looking archive.

    ``which`` is the PATH-lookup seam alongside ``runner``: without it the
    host-leg branch would depend on whether the machine running the tests
    happens to have ``pg_dump`` installed.
    """
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(
            f"unsupported tool {tool!r}; expected one of {', '.join(SUPPORTED_TOOLS)}"
        )

    target = dsn_target(database_url)
    # The configured container first (the overwhelmingly common case), then any
    # other running container that publishes this DSN's port.
    chosen = (
        container
        if container_serves(container, target, runner)
        else discover_container(target, runner)
    )
    if chosen is not None:
        container_output = _probe(runner, ["docker", "exec", chosen, tool, "--version"])
        if container_output:
            version, major = _parse_version(
                container_output, tool=tool, source="container"
            )
            if major >= server_major:
                return PgToolPlan(
                    tool=tool,
                    source="container",
                    version=version,
                    major=major,
                    container=chosen,
                    argv_prefix=("docker", "exec", chosen, tool),
                )

    # Every failure below names BOTH the target and the container, so a
    # host/port mismatch reads as a mismatch rather than a missing database.
    context = (
        f"Target: {target}. The brain container '{container}' was not used "
        "because it does not serve that host and port."
    )

    host_path = which(tool)
    if host_path is None:
        raise PgToolUnavailable(
            f"No usable {tool} for {target}: {tool} is not on PATH, and the "
            f"brain container '{container}' cannot dump it.\n{_REMEDIES}"
        )

    host_output = _probe(runner, [host_path, "--version"])
    if host_output is None:
        raise PgToolUnavailable(
            f"No usable {tool} for {target}: {host_path} could not be executed, "
            f"and the brain container '{container}' cannot dump it.\n{_REMEDIES}"
        )

    version, major = _parse_version(host_output, tool=tool, source="host")
    if major < server_major:
        raise PgToolUnavailable(
            f"{tool} on this machine is {version} but the server for {target} is "
            f"PostgreSQL {server_major}.\n{tool} must be >= the server major "
            f"version.\n{context}\nEither:\n{_REMEDIES}"
        )

    return PgToolPlan(
        tool=tool,
        source="host",
        version=version,
        major=major,
        container=None,
        argv_prefix=(host_path,),
    )
