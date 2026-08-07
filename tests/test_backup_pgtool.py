"""pg_dump / pg_restore version resolution — container first, host fallback (F3 §5.3)."""
from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from brain.backup.pgtool import PgToolPlan, password_env_file, resolve_pg_tool
from brain.errors import PgToolUnavailable
from tests.backup_fakes import (
    RecordingRunner,
    repo_root_guard,  # noqa: F401 — autouse fixture
)

CONTAINER = "second-brain-postgres"

#: The brain's own stack: the DSN and the container's published port agree.
MATCHING_DSN = "postgresql://brain:brain@localhost:55432/second_brain"
MATCHING_PORTS = "0.0.0.0:55432\n[::]:55432\n"

# Real `--version` output captured from this machine, so the parser is tested
# against the exact strings it will meet in production.
CONTAINER_V16 = "pg_dump (PostgreSQL) 16.14 (Debian 16.14-1.pgdg12+1)\n"
HOST_V14 = "pg_dump (PostgreSQL) 14.23 (Homebrew)\n"
HOST_V17 = "pg_dump (PostgreSQL) 17.2\n"

# Synthetic throughout — never a real credential.
SECRET = "s3cr3t-synthetic-password"  # noqa: S105

#: A database name for argv-shape assertions. Deliberately NOT derived from
#: TEST_DATABASE_URL: this asserts on the argv string, not on any live
#: server, and a per-agent scratch name here is what broke CI.
SYNTHETIC_DB = "synthetic_brain_db"


#: A stand-in PATH result. Never executed and never touched on disk — it is
#: only ever handed back by the `which` double — so it must NOT be a real
#: machine's path, or the test reads as environment-dependent when it isn't.
HOST_PG_DUMP = "/synthetic/bin/pg_dump"


def _container_down() -> subprocess.CalledProcessError:
    """The failure a stopped container produces: `docker exec` exits non-zero."""
    return subprocess.CalledProcessError(
        1, ["docker", "exec"], stderr="Error: No such container: second-brain-postgres"
    )


def _which_finds(path: str | None) -> Callable[[str], str | None]:
    """PATH-lookup double, so these tests never depend on the host's own pg_dump."""
    return lambda _tool: path


def test_container_pg_dump_preferred_when_major_matches() -> None:
    runner = RecordingRunner(
        responses={"docker port": MATCHING_PORTS, "--version": CONTAINER_V16}
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=MATCHING_DSN,
    )

    assert plan.source == "container"
    assert plan.major == 16
    assert plan.version == "16.14"
    assert plan.container == CONTAINER
    assert plan.tool == "pg_dump"


def test_host_pg_dump_rejected_when_older_than_server() -> None:
    """The empirically verified 14.23-vs-16.14 abort, encoded as a refusal.

    Running it would produce `aborting because of server version mismatch`;
    resolving it here means the dump is never attempted at all.
    """
    runner = RecordingRunner(
        responses={"--version": HOST_V14},
        raises={"docker": _container_down()},
    )

    with pytest.raises(PgToolUnavailable) as excinfo:
        resolve_pg_tool(
            "pg_dump",
            server_major=16,
            container=CONTAINER,
            runner=runner,
            database_url=MATCHING_DSN,
            which=_which_finds(HOST_PG_DUMP),
        )

    message = str(excinfo.value)
    assert "14.23" in message
    assert "16" in message
    assert "brain-up" in message
    assert "postgresql@16" in message


def test_host_pg_dump_accepted_when_newer_than_server() -> None:
    """The rule is `>=`, not `==` — a 17 client dumps a 16 server fine."""
    runner = RecordingRunner(
        responses={"--version": HOST_V17},
        raises={"docker": _container_down()},
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=MATCHING_DSN,
        which=_which_finds(HOST_PG_DUMP),
    )

    assert plan.source == "host"
    assert plan.major == 17
    assert plan.container is None


def test_version_parse_rejects_garbage() -> None:
    """Unparseable --version output raises PgToolUnavailable, never IndexError."""
    runner = RecordingRunner(
        responses={"--version": "command not found\n"},
        raises={"docker": _container_down()},
    )

    with pytest.raises(PgToolUnavailable) as excinfo:
        resolve_pg_tool(
            "pg_dump",
            server_major=16,
            container=CONTAINER,
            runner=runner,
            database_url=MATCHING_DSN,
            which=_which_finds(HOST_PG_DUMP),
        )

    assert "version" in str(excinfo.value).lower()


def test_missing_host_binary_raises_pg_tool_unavailable() -> None:
    runner = RecordingRunner(raises={"docker": _container_down()})

    with pytest.raises(PgToolUnavailable, match="not on PATH"):
        resolve_pg_tool(
            "pg_dump",
            server_major=16,
            container=CONTAINER,
            runner=runner,
            database_url=MATCHING_DSN,
            which=_which_finds(None),
        )


def test_password_never_appears_in_argv() -> None:
    """The password reaches the container via --env-file, never via argv (§7)."""
    runner = RecordingRunner(
        responses={"docker port": MATCHING_PORTS, "--version": CONTAINER_V16}
    )
    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=MATCHING_DSN,
    )

    with password_env_file(SECRET) as env_file:
        assert env_file is not None
        argv = plan.argv("-Fc", "-d", SYNTHETIC_DB, env_file=env_file)
        assert env_file.read_text(encoding="utf-8") == f"PGPASSWORD={SECRET}\n"
        assert (env_file.stat().st_mode & 0o777) == 0o600

    assert not any(SECRET in element for element in argv)
    assert "--env-file" in argv
    assert str(env_file) in argv
    # The temp file is removed once the context exits.
    assert not env_file.exists()


def test_password_env_file_removed_even_on_error() -> None:
    with pytest.raises(RuntimeError), password_env_file(SECRET) as env_file:
        assert env_file is not None
        leaked = env_file
        raise RuntimeError("boom")

    assert not leaked.exists()


def test_password_env_file_is_none_when_no_password() -> None:
    with password_env_file(None) as env_file:
        assert env_file is None


def test_host_plan_passes_password_by_env_not_argv() -> None:
    """The host leg has no --env-file; it must still keep the password off argv."""
    runner = RecordingRunner(
        responses={"--version": HOST_V17},
        raises={"docker": _container_down()},
    )
    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=MATCHING_DSN,
        which=_which_finds(HOST_PG_DUMP),
    )

    argv = plan.argv("-Fc", env_file=None)
    env = plan.env(SECRET)

    assert not any(SECRET in element for element in argv)
    assert env["PGPASSWORD"] == SECRET


def test_container_name_follows_compose_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BRAIN_COMPOSE_PROJECT` isolation reaches the container the tool runs in."""
    from brain._compose import postgres_container_name

    monkeypatch.setenv("BRAIN_COMPOSE_PROJECT", "qa")
    assert postgres_container_name() == "qa-postgres"

    monkeypatch.delenv("BRAIN_COMPOSE_PROJECT", raising=False)
    assert postgres_container_name() == "second-brain-postgres"


def test_plan_is_immutable() -> None:
    runner = RecordingRunner(
        responses={"docker port": MATCHING_PORTS, "--version": CONTAINER_V16}
    )
    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=MATCHING_DSN,
    )

    assert isinstance(plan, PgToolPlan)
    with pytest.raises(AttributeError):
        plan.source = "host"  # type: ignore[misc]


def test_unknown_tool_is_rejected() -> None:
    runner = RecordingRunner(
        responses={"docker port": MATCHING_PORTS, "--version": CONTAINER_V16}
    )

    with pytest.raises(ValueError, match="pg_dump"):
        resolve_pg_tool(
            "rm",
            server_major=16,
            container=CONTAINER,
            runner=runner,
            database_url=MATCHING_DSN,
        )


# ---------------------------------------------------------------------------
# Regression: the leg must follow the DSN, not the tool version.
#
# QA hit this as a hard failure — DATABASE_URL on port 5434 still `docker
# exec`'d into the production container (published on 55432) and looked for the
# database on THAT server's local socket. The visible symptom was a clean
# abort; the unshipped one is a backup that succeeds holding the wrong data.
# ---------------------------------------------------------------------------

PROD_CONTAINER_PORTS = "0.0.0.0:55432\n[::]:55432\n"
QA_DSN = "postgresql://brain:brain@localhost:5434/second_brain_qa"
DEFAULT_DSN = "postgresql://brain:brain@localhost:55432/second_brain"


def _ports_runner(published: str, **kwargs: object) -> RecordingRunner:
    """A runner answering `docker port` with ``published``.

    Overrides go in FIRST: RecordingRunner matches on the first key that is a
    substring of the invocation, so the specific ``"pg_dump --version"`` must
    precede the catch-all ``"--version"`` or the host probe would be answered
    with the container's banner.
    """
    responses: dict[str, str] = dict(kwargs.pop("responses", {}))  # type: ignore[arg-type]
    responses.setdefault("docker port", published)
    responses.setdefault("--version", CONTAINER_V16)
    return RecordingRunner(responses=responses, **kwargs)  # type: ignore[arg-type]


def test_container_leg_refused_when_dsn_port_is_not_published() -> None:
    """The exact QA failure: DSN on 5434, container publishes 55432."""
    runner = _ports_runner(
        PROD_CONTAINER_PORTS, responses={"pg_dump --version": HOST_V17}
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=QA_DSN,
        which=_which_finds(HOST_PG_DUMP),
    )

    assert plan.source == "host", "a DSN on another port must not use the container"
    assert plan.container is None
    # And it never even asked the container for its version.
    assert not any("exec" in call for call in runner.flat_calls)


def test_container_leg_used_when_dsn_matches_the_published_port() -> None:
    """The brain's own stack must keep working — this is the default path."""
    runner = _ports_runner(PROD_CONTAINER_PORTS)

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=DEFAULT_DSN,
        which=_which_finds(None),
    )

    assert plan.source == "container"
    assert plan.container == CONTAINER


def test_mismatched_dsn_with_an_old_host_tool_names_both_sides() -> None:
    """A mismatch must read as a mismatch, not as a missing database."""
    runner = _ports_runner(
        PROD_CONTAINER_PORTS, responses={"pg_dump --version": HOST_V14}
    )

    with pytest.raises(PgToolUnavailable) as excinfo:
        resolve_pg_tool(
            "pg_dump",
            server_major=16,
            container=CONTAINER,
            runner=runner,
            database_url=QA_DSN,
            which=_which_finds(HOST_PG_DUMP),
        )

    message = str(excinfo.value)
    assert "second_brain_qa" in message
    assert "5434" in message
    assert CONTAINER in message
    assert "does not serve that host and port" in message
    # The password must never reach the terminal.
    assert "brain:brain" not in message


def test_remote_dsn_never_uses_the_container() -> None:
    runner = _ports_runner(
        PROD_CONTAINER_PORTS, responses={"pg_dump --version": HOST_V17}
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url="postgresql://brain:brain@db.internal.example.com:5432/second_brain",
        which=_which_finds(HOST_PG_DUMP),
    )

    assert plan.source == "host"


def test_dsn_without_a_port_defaults_to_5432() -> None:
    from brain.backup.pgtool import container_serves, dsn_target

    target = dsn_target("postgresql://brain:brain@localhost/second_brain")
    runner = _ports_runner("0.0.0.0:5432\n")

    assert target.port is None
    assert container_serves(CONTAINER, target, runner) is True


def test_stopped_container_falls_back_to_the_host_leg() -> None:
    """`docker port` failing means 'unknown', which must not select the container."""
    runner = RecordingRunner(
        responses={"--version": HOST_V17},
        raises={"docker port": _container_down()},
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=DEFAULT_DSN,
        which=_which_finds(HOST_PG_DUMP),
    )

    assert plan.source == "host"


def test_dsn_target_never_renders_the_password() -> None:
    from brain.backup.pgtool import dsn_target

    rendered = str(dsn_target(QA_DSN))

    assert "second_brain_qa" in rendered
    assert "5434" in rendered
    assert "brain:brain" not in rendered


DOCKER_PS = (
    "second-brain-age-test\t0.0.0.0:5434->5432/tcp, [::]:5434->5432/tcp\n"
    "second-brain-postgres\t0.0.0.0:55432->5432/tcp, [::]:55432->5432/tcp\n"
)


def test_discovers_the_container_that_actually_serves_the_dsn() -> None:
    """A non-default DSN must reach ITS container, not the configured one.

    Without this, `brain backup` is unusable for the demo sandbox or any test
    database on a machine whose host pg_dump is too old — which is exactly the
    situation on this machine (14.23 vs a 16 server).
    """
    runner = RecordingRunner(
        responses={
            "docker port": "0.0.0.0:55432\n",  # configured container: wrong port
            "docker ps": DOCKER_PS,
            "--version": CONTAINER_V16,
        }
    )

    plan = resolve_pg_tool(
        "pg_dump",
        server_major=16,
        container=CONTAINER,
        runner=runner,
        database_url=QA_DSN,
        which=_which_finds(HOST_PG_DUMP),
    )

    assert plan.source == "container"
    assert plan.container == "second-brain-age-test", (
        "must dump inside the container serving 5434, never the production one"
    )
    assert CONTAINER not in (plan.argv_prefix or ())


def test_discovery_ignores_containers_on_other_ports() -> None:
    from brain.backup.pgtool import discover_container, dsn_target

    runner = RecordingRunner(responses={"docker ps": DOCKER_PS})

    found = discover_container(
        dsn_target("postgresql://brain:brain@localhost:59999/nope"), runner
    )

    assert found is None


def test_discovery_skips_remote_hosts() -> None:
    from brain.backup.pgtool import discover_container, dsn_target

    runner = RecordingRunner(responses={"docker ps": DOCKER_PS})

    found = discover_container(
        dsn_target("postgresql://brain:brain@db.example.com:5434/x"), runner
    )

    assert found is None
    assert runner.calls == [], "a remote DSN must not even ask Docker"
