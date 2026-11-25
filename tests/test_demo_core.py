"""Unit tests for `brain demo` core helpers: the prod-safety guard, port
resolution, the compose file renderer, and the doc-count probe.

The prod guard is a BINDING isolation-contract requirement — the demo must be
structurally incapable of touching the prod database (port 55432 / db
``second_brain``), so these tests assert every entry point refuses a
prod-looking URL before opening any connection.
"""
import socket
import subprocess
from pathlib import Path

import psycopg
import pytest
from pytest_mock import MockerFixture

from brain import demo as demo_mod
from brain.db import connect as _real_connect
from brain.errors import BrainError, DemoError
from tests.conftest import TEST_DATABASE_URL

#: libpq reads these when a DSN leaves a value out. Cleared for every test, so
#: neither the developer's shell nor CI can change what the guard resolves.
_LIBPQ_ENV = ("PGSERVICE", "PGPORT", "PGDATABASE", "PGUSER")


@pytest.fixture(autouse=True)
def _clear_libpq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LIBPQ_ENV:
        monkeypatch.delenv(key, raising=False)


class _RecordingConnect:
    """The module-wide ``brain.demo.connect``: records, and connects to ONE DSN only.

    Only the exact ``TEST_DATABASE_URL`` string is passed to the real
    :func:`brain.db.connect` — ``conftest`` refuses a prod DSN there at import,
    so that one is the 5434 test server. Every other DSN is recorded in
    ``attempts`` and refused with an ``AssertionError``, never opened. A guard
    that is removed or weakened therefore fails the refusal tests here instead
    of reaching a server: these DSNs name the production port.

    Autouse for the whole module (rev-139 incident: the older refusal tests
    called the real ``connect``, so a mutation that removed the guard reached
    prod on 55432).
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __call__(self, database_url: str) -> object:
        if database_url == TEST_DATABASE_URL:
            return _real_connect(database_url)
        self.attempts.append(database_url)
        raise AssertionError("brain demo attempted a connection to a non-test DSN")


@pytest.fixture(autouse=True)
def refusing_connect(monkeypatch: pytest.MonkeyPatch) -> _RecordingConnect:
    recorder = _RecordingConnect()
    monkeypatch.setattr("brain.demo.connect", recorder)
    return recorder


# URLs that must be refused by every demo primitive (mirror conftest's prod
# fingerprints: current + historical ports, and the exact prod db name).
_PROD_URLS = [
    "postgresql://brain:brain@localhost:55432/second_brain_demo",
    "postgresql://brain:brain@localhost:5433/second_brain_demo",
    "postgresql://brain:brain@0.0.0.0:55432/second_brain_demo",  # 0.0.0.0 = local
    "postgresql://brain:brain@localhost:55432/second_brain",
    "postgresql://brain:brain@example.com:5432/second_brain",  # prod name, any host
]


@pytest.mark.parametrize("url", _PROD_URLS)
def test_guard_refuses_prod_urls(url: str, refusing_connect: _RecordingConnect) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod._assert_not_demo_prod_db(url)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize("url", _PROD_URLS)
def test_seed_refuses_prod_urls(url: str, refusing_connect: _RecordingConnect) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.seed_demo(url)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize("url", _PROD_URLS)
def test_query_refuses_prod_urls(url: str, refusing_connect: _RecordingConnect) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.query_demo(url, "anything")
    assert refusing_connect.attempts == []


def test_provision_refuses_prod_port(
    mocker: MockerFixture, refusing_connect: _RecordingConnect
) -> None:
    # provision(55432) resolves to the prod host port and must abort BEFORE any
    # Docker call or compose-file write. Both are faked, so a removed guard
    # fails the assertions below instead of running `docker compose up` or
    # writing into the real ~/.brain-demo.
    write = mocker.patch("brain.demo._write_compose_file")
    docker = mocker.patch("brain.demo._run_docker")
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.provision(55432)
    write.assert_not_called()
    docker.assert_not_called()
    assert refusing_connect.attempts == []


def test_demo_database_url_shape() -> None:
    url = demo_mod.demo_database_url(55444)
    assert url == "postgresql://brain:brain@localhost:55444/second_brain_demo"


def test_resolve_port_returns_start_when_free() -> None:
    # Bind then release a port to find one that is currently free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert demo_mod.resolve_port(free_port) == free_port


def test_resolve_port_bumps_off_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        resolved = demo_mod.resolve_port(busy_port)
        assert resolved != busy_port
        assert resolved > busy_port


def test_compose_file_text_has_isolation_contract() -> None:
    text = demo_mod._compose_file_text(55499)
    # Stock image, named volume, container name, and the mapped port.
    assert demo_mod.DEMO_IMAGE in text
    assert f"{demo_mod.DEMO_VOLUME}:/var/lib/postgresql/data" in text
    assert demo_mod.CONTAINER_NAME in text
    assert '"55499:5432"' in text
    # A Docker NAMED volume — never a host bind-mount ("./" path).
    assert "./" not in text


def test_count_documents_unreachable_returns_none(
    refusing_connect: _RecordingConnect,
) -> None:
    # Any connect failure → best-effort probe returns None; the recorder stands
    # in for an unreachable server (it refuses every non-test DSN).
    dsn = "postgresql://brain:brain@localhost:1/x"
    assert demo_mod._count_documents(dsn) is None
    assert refusing_connect.attempts == [dsn]


@pytest.mark.fresh_schema
def test_count_documents_counts_seeded_rows(test_db: psycopg.Connection) -> None:
    from tests.conftest import TEST_DATABASE_URL

    demo_mod.seed_demo(TEST_DATABASE_URL)
    assert demo_mod._count_documents(TEST_DATABASE_URL) == 22


def test_load_corpus_rejects_non_list(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo.json.loads", return_value={"not": "a list"})
    with pytest.raises(BrainError, match="JSON list"):
        demo_mod.load_corpus()


def test_record_to_doc_promotes_metadata() -> None:
    doc = demo_mod._record_to_doc(
        {
            "title": "Sync",
            "body": "a body",
            "content_type": "transcript",
            "date": "2026-01-02",
            "participants": ["Sam Rivera"],
            "duration_min": 30,
            "thread_id": "thr-123",
        }
    )
    assert doc.source_path is None
    assert doc.metadata["date"] == "2026-01-02"
    assert doc.metadata["participants"] == ["Sam Rivera"]
    assert doc.metadata["duration_min"] == 30
    assert doc.metadata["thread_id"] == "thr-123"


def test_run_executes_a_real_subprocess() -> None:
    result = demo_mod._run(["printf", "hello"])
    assert result.returncode == 0
    assert result.stdout == "hello"


def test_write_compose_file_materializes_isolated_config(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("brain.demo.DEMO_HOME", tmp_path)
    path = demo_mod._write_compose_file(55510)
    assert path == tmp_path / demo_mod.COMPOSE_FILENAME
    text = path.read_text(encoding="utf-8")
    assert demo_mod.DEMO_IMAGE in text
    assert '"55510:5432"' in text


def test_container_running_swallows_subprocess_error(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo._run", side_effect=OSError("docker missing"))
    assert demo_mod._container_running() is False


def test_teardown_passes_compose_file_when_present(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("brain.demo.DEMO_HOME", tmp_path)
    (tmp_path / demo_mod.COMPOSE_FILENAME).write_text("services: {}\n", encoding="utf-8")
    run_mock = mocker.patch("brain.demo._run")

    demo_mod.teardown()

    called = run_mock.call_args.args[0]
    assert "-f" in called
    assert str(tmp_path / demo_mod.COMPOSE_FILENAME) in called
    assert called[-2:] == ["down", "-v"]


def test_teardown_translates_docker_unavailable(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch("brain.demo.DEMO_HOME", tmp_path)
    mocker.patch("brain.demo._run", side_effect=FileNotFoundError("docker"))
    with pytest.raises(DemoError, match="Docker"):
        demo_mod.teardown()


def test_run_docker_translates_daemon_down(mocker: MockerFixture) -> None:
    # CalledProcessError → DemoError carrying docker's own stderr.
    err = subprocess.CalledProcessError(1, ["docker"], stderr="daemon is not running")
    mocker.patch("brain.demo._run", side_effect=err)
    with pytest.raises(DemoError, match="daemon is not running"):
        demo_mod._run_docker(["docker", "compose", "up", "-d"])


def test_run_docker_translates_missing_binary(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo._run", side_effect=FileNotFoundError("docker"))
    with pytest.raises(DemoError, match="Docker CLI not found"):
        demo_mod._run_docker(["docker", "compose", "up", "-d"])


def test_run_docker_translates_timeout(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo._run", side_effect=subprocess.TimeoutExpired(["docker"], 120))
    with pytest.raises(DemoError, match="timed out"):
        demo_mod._run_docker(["docker", "compose", "up", "-d"])


# --- rev-139: the demo prod guard, hardened ---------------------------------
# Each DSN below reaches the prod database (port 55432/5433, or db
# ``second_brain``) and each slipped past the old guard, which matched the host
# against five literal local spellings and read the URL with ``urlparse``.


_BYPASS_DSNS = [
    "postgresql://brain:brain@127.1:55432/second_brain_demo",
    "postgresql://brain:brain@[::ffff:127.0.0.1]:55432/second_brain_demo",
    "postgresql://brain:brain@localhost.:55432/second_brain_demo",
    "postgresql://brain:brain@127.0.0.2:55432/second_brain_demo",
    "host=host.docker.internal port=55432 dbname=second_brain_demo",
    "host=host.docker.internal port=5433 dbname=second_brain_demo",
    "postgresql://brain:brain@localhost:5434/second%5Fbrain",
    "host=localhost port=55432 dbname=second_brain",
    "host=localhost port=55432 dbname=second_brain_demo user=brain",
    "postgresql://brain:brain@localhost/second_brain_demo?port=55432",
    "postgresql://brain:brain@localhost/?port=55432&dbname=second_brain",
    "host=db-a,db-b port=5434,55432 dbname=second_brain_demo",
    "postgresql://brain:brain@db-a:5434,db-b:55432/second_brain_demo",
]


@pytest.mark.parametrize("dsn", _BYPASS_DSNS)
def test_guard_refuses_every_spelling_of_prod(
    dsn: str, refusing_connect: _RecordingConnect
) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod._assert_not_demo_prod_db(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize("dsn", _BYPASS_DSNS)
def test_seed_refuses_before_connecting(
    dsn: str, refusing_connect: _RecordingConnect
) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.seed_demo(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize("dsn", _BYPASS_DSNS)
def test_query_refuses_before_connecting(
    dsn: str, refusing_connect: _RecordingConnect
) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.query_demo(dsn, "anything")
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    ("env", "dsn"),
    [
        ({"PGPORT": "55432"}, "postgresql://brain:brain@localhost/second_brain_demo"),
        ({"PGPORT": "55432"}, "host=localhost dbname=second_brain_demo"),
        ({"PGDATABASE": "second_brain"}, "postgresql://brain:brain@localhost:5434"),
        ({"PGUSER": "second_brain"}, "host=localhost port=5434"),
    ],
    ids=["pgport-url", "pgport-keyword", "pgdatabase", "user-as-default-dbname"],
)
def test_guard_applies_the_libpq_environment_defaults(
    monkeypatch: pytest.MonkeyPatch,
    refusing_connect: _RecordingConnect,
    env: dict[str, str],
    dsn: str,
) -> None:
    """A DSN without a port or db name gets libpq's: PGPORT, PGDATABASE, then the user."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(BrainError, match="PROD"):
        demo_mod.seed_demo(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    "dsn",
    ["host=localhost port", "postgresql://brain@localhost:notaport/second_brain_demo"],
    ids=["malformed-keyword", "non-numeric-port"],
)
def test_guard_refuses_an_unparseable_dsn(
    dsn: str, refusing_connect: _RecordingConnect
) -> None:
    with pytest.raises(BrainError):
        demo_mod.seed_demo(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    ("env", "dsn"),
    [
        ({}, "service=synthetic_demo"),
        ({}, "service=synthetic_demo port=5434"),
        ({}, "service=synthetic_demo dbname=synthetic_scratch_test"),
        ({}, "service=synthetic_demo host=a,b port=5434, dbname=synthetic_scratch_test"),
        ({"PGSERVICE": "synthetic_demo"}, "host=localhost dbname=synthetic_scratch_test"),
        ({"PGSERVICE": "synthetic_demo"}, "postgresql://brain:brain@localhost:5434"),
    ],
    ids=["bare", "no-dbname", "no-port", "empty-port-entry", "env-no-port", "env-no-dbname"],
)
def test_guard_refuses_a_service_that_supplies_port_or_dbname(
    monkeypatch: pytest.MonkeyPatch,
    refusing_connect: _RecordingConnect,
    env: dict[str, str],
    dsn: str,
) -> None:
    """``service=`` fills what the DSN omits from pg_service.conf, out of the guard's sight."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(BrainError, match="service"):
        demo_mod.seed_demo(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    ("env", "dsn"),
    [
        ({}, "service=synthetic_demo port=55432 dbname=synthetic_scratch_test"),
        ({}, "service=synthetic_demo port=5434 dbname=second_brain"),
        ({"PGSERVICE": "synthetic_demo"}, "host=localhost port=5433 dbname=synthetic_demo"),
    ],
    ids=["prod-port", "prod-dbname", "env-prod-port"],
)
def test_guard_still_refuses_prod_values_given_alongside_a_service(
    monkeypatch: pytest.MonkeyPatch,
    refusing_connect: _RecordingConnect,
    env: dict[str, str],
    dsn: str,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(BrainError, match="PROD"):
        demo_mod.seed_demo(dsn)
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    ("env", "dsn"),
    [
        ({}, "service=synthetic_demo port=5434 dbname=synthetic_scratch_test"),
        ({}, "service=synthetic_demo host=a,b port=5434,55433 dbname=synthetic_scratch_test"),
        ({"PGSERVICE": "synthetic_demo"}, "postgresql://brain:brain@localhost:5434/synthetic_scratch_test"),
    ],
    ids=["dsn-service", "dsn-service-multi-host", "env-service"],
)
def test_guard_allows_a_service_when_port_and_dbname_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    refusing_connect: _RecordingConnect,
    env: dict[str, str],
    dsn: str,
) -> None:
    """libpq lets explicit DSN values override the service file, so these are decided here."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    demo_mod._assert_not_demo_prod_db(dsn)  # must not raise
    assert refusing_connect.attempts == []


@pytest.mark.parametrize(
    "dsn",
    [
        demo_mod.demo_database_url(55433),
        "postgresql://brain:brain@localhost:5434/synthetic_scratch_test",
        "host=localhost port=5434 dbname=synthetic_scratch_test user=brain",
    ],
)
def test_guard_still_allows_the_demo_and_test_servers(
    dsn: str, refusing_connect: _RecordingConnect
) -> None:
    demo_mod._assert_not_demo_prod_db(dsn)  # must not raise
    assert refusing_connect.attempts == []
