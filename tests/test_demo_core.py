"""Unit tests for `brain demo` core helpers: the prod-safety guard, port
resolution, the compose file renderer, and the doc-count probe.

The prod guard is a BINDING isolation-contract requirement — the demo must be
structurally incapable of touching the prod database (port 55432 / db
``second_brain``), so these tests assert every entry point refuses a
prod-looking URL before opening any connection.
"""
import socket
from pathlib import Path

import psycopg
import pytest
from pytest_mock import MockerFixture

from brain import demo as demo_mod
from brain.errors import BrainError

# URLs that must be refused by every demo primitive (mirror conftest's prod
# fingerprints: current + historical ports, and the exact prod db name).
_PROD_URLS = [
    "postgresql://brain:brain@localhost:55432/second_brain_demo",
    "postgresql://brain:brain@localhost:5433/second_brain_demo",
    "postgresql://brain:brain@localhost:55432/second_brain",
    "postgresql://brain:brain@example.com:5432/second_brain",  # prod name, any host
]


@pytest.mark.parametrize("url", _PROD_URLS)
def test_guard_refuses_prod_urls(url: str) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod._assert_not_demo_prod_db(url)


@pytest.mark.parametrize("url", _PROD_URLS)
def test_seed_refuses_prod_urls(url: str) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.seed_demo(url)


@pytest.mark.parametrize("url", _PROD_URLS)
def test_query_refuses_prod_urls(url: str) -> None:
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.query_demo(url, "anything")


def test_provision_refuses_prod_port() -> None:
    # provision(55432) resolves to the prod host port and must abort BEFORE any
    # Docker call or compose-file write.
    with pytest.raises(BrainError, match="PROD"):
        demo_mod.provision(55432)


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


def test_count_documents_unreachable_returns_none() -> None:
    # Connection refused (nothing listening) → best-effort probe returns None.
    assert demo_mod._count_documents("postgresql://brain:brain@localhost:1/x") is None


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
