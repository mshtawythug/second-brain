"""The Ingest tab's wiring: ``build_context`` (D8) and the app lifespan (§4.6).

``build_context`` really connects (its preflight), so it runs against the test
database. The lifespan is exercised two ways: a real registry whose worker starts
and stops with ``TestClient``'s context, and a recording stand-in that reports a
job still running at the timeout, so the shutdown line is checked without a 10 s
wait.
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.ui.app import create_app
from brain.ui.ingest_limits import STOP_TIMEOUT_S
from brain.ui.ingest_service import IngestRuntime
from brain.ui.jobs import JobHandle, JobRegistry, ShuttingDown
from brain.ui.server import build_context
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.ui_ingest_support import ORIGIN, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _build(host: str, *, read_only: bool = False) -> Any:
    return build_context(
        Config(database_url=TEST_DATABASE_URL, embedder="none"),
        host=host,
        port=8765,
        read_only=read_only,
        token="" if host == "127.0.0.1" else "tok-123",
        include_confidential=False,
        embedder=None,
    )


@pytest.mark.parametrize(
    ("host", "read_only", "enabled"),
    [
        ("127.0.0.1", False, True),
        ("::1", False, True),
        ("127.0.0.1", True, False),
        ("192.0.2.10", False, False),
        ("0.0.0.0", False, False),
    ],
)
def test_build_context_enables_ingest_only_on_a_writable_loopback_bind(
    test_db: psycopg.Connection[Any], host: str, read_only: bool, enabled: bool
) -> None:
    context = _build(host, read_only=read_only)

    assert context.ingest_enabled is enabled
    assert isinstance(context.jobs, JobRegistry)
    assert isinstance(context.ingest_runtime, IngestRuntime)


def test_the_registry_is_new_per_context(test_db: psycopg.Connection[Any]) -> None:
    assert _build("127.0.0.1").jobs is not _build("127.0.0.1").jobs


def test_the_production_runtime_builds_from_config() -> None:
    """``from_config`` wires the real factories; ``none`` builds without a network."""
    runtime = IngestRuntime.from_config(Config(database_url=TEST_DATABASE_URL, embedder="none"))

    embedder = runtime.embedder()

    assert getattr(embedder, "produces_embeddings", True) is False
    assert runtime.embedder() is embedder  # once per worker thread
    assert runtime.enricher() is runtime.enricher()


def test_the_runtime_builds_once_per_thread() -> None:
    built: list[int] = []

    def factory() -> Any:
        built.append(threading.get_ident())
        return FakeEmbedder()

    runtime = IngestRuntime(make_embedder=factory, make_enricher=lambda: None)
    here = runtime.embedder()
    elsewhere: list[Any] = []
    thread = threading.Thread(target=lambda: elsewhere.append(runtime.embedder()))
    thread.start()
    thread.join(timeout=30)

    assert runtime.embedder() is here
    assert elsewhere and elsewhere[0] is not here
    assert len(built) == 2


def test_the_lifespan_starts_the_worker_and_stops_it(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry()
    ran = threading.Event()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        ran.set()
        return {}

    ctx = make_context(test_db, make_vault(tmp_path), embedder=FakeEmbedder(), jobs=registry)
    with TestClient(create_app(ctx), base_url=ORIGIN):
        registry.submit("test.kind", runner)
        assert ran.wait(timeout=30)

    with pytest.raises(ShuttingDown):
        registry.submit("test.kind", runner)


class _StillRunning:
    """A registry stand-in whose running job outlives the shutdown join."""

    def __init__(self) -> None:
        self.started = False
        self.stop_timeouts: list[float] = []

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout: float) -> list[str]:
        self.stop_timeouts.append(timeout)
        return ["0123456789abcdef0123456789abcdef"]


def test_a_job_still_running_at_shutdown_is_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stand_in = _StillRunning()

    @contextlib.contextmanager
    def conn_factory() -> Any:
        raise AssertionError("no database is needed")
        yield

    from brain.ui.context import UiContext

    ctx = UiContext(
        cfg=Config(database_url="postgresql://unused", vault_path=tmp_path, embedder="none"),
        conn_factory=conn_factory,
        embedder=FakeEmbedder(),
        search_fn=lambda *a, **k: [],
        jobs=stand_in,  # type: ignore[arg-type]
    )
    with (
        caplog.at_level("WARNING", logger="brain.ui.app"),
        TestClient(create_app(ctx), base_url=ORIGIN),
    ):
        assert stand_in.started

    assert stand_in.stop_timeouts == [STOP_TIMEOUT_S]
    assert "0123456789abcdef0123456789abcdef" in caplog.text
    assert "Re-submit to be sure" in caplog.text
