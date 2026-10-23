"""CLI integration tests for ``brain vault sync --watch``.

These run the full Typer entrypoint with a fake watchdog Observer, so
we exercise argument parsing + the wiring into ``run_watcher`` without
relying on the OS filesystem-events subsystem.
"""
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner
from watchdog.events import FileCreatedEvent

from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter


def _write(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


# Reuse the FakeObserver from the unit tests to keep the test surface DRY.
# ``_wait_for`` comes from the shared helper, not through that test module, so
# the call-site guard in tests/test_thread_wait.py can see this file.
from tests.test_vault_watcher import (  # noqa: E402
    _FakeObserver,
    _wait_for_observer,
)
from tests.thread_wait import wait_for as _wait_for  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_fake_observers_for_cli() -> None:
    """Clear the per-test FakeObserver registry before each CLI test.

    The autouse fixture in ``test_vault_watcher.py`` only fires inside
    that file; without this twin we'd carry stale observer instances
    across files and ``_wait_for_observer`` would race on the wrong one.
    """
    _FakeObserver.instances.clear()


def test_watch_and_dry_run_are_mutually_exclusive(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, patch_embedder: Any
) -> None:
    """``--watch --dry-run`` must fail loudly via Typer's BadParameter."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["vault", "sync", "--vault", str(vault), "--watch", "--dry-run"],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "--watch" in combined
    assert "--dry-run" in combined


def test_watch_smoke(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Any,
    mocker: Any,
) -> None:
    """End-to-end: ``brain vault sync --watch`` runs the watcher.

    We patch the Observer factory so the watcher uses our fake; we then
    inject one created event in a background thread, wait for the DB row
    to appear, and trigger shutdown via the stop_event.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "smoke.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Smoke"}, "hello\n")

    # Patch the watchdog Observer at its import site inside ``watch.py``.
    mocker.patch("brain.vault.watch.Observer", _FakeObserver)
    # Skip signal handler installation — CliRunner runs in a non-main
    # thread context where signal.signal() would fail.
    runner = CliRunner()

    # Schedule a background driver: wait for the observer, inject one
    # event, then stop the watcher.
    driver_done = threading.Event()

    def _driver() -> None:
        try:
            observer = _wait_for_observer()
            new_note = vault / "after-watch.md"
            new_id = str(uuid.uuid4())
            _write(new_note, {"id": new_id, "title": "After"}, "y\n")
            observer.inject(FileCreatedEvent(str(new_note)))
            _wait_for(
                lambda: test_db.execute(
                    "SELECT count(*) FROM documents WHERE id = %s", (new_id,)
                ).fetchone()[0]
                == 1,
            )
            # Trigger graceful shutdown.
            state = observer.handler._state
            state.stop_event.set()
        finally:
            driver_done.set()

    # Patch signal.signal to a no-op for this CliRunner test — Typer
    # runs the command on a worker thread (not the main thread) and
    # signal.signal() raises ValueError there. The watcher is robust to
    # missing handler installation, so a no-op is safe.
    import signal

    def _no_op(_sig: int, _handler: Any) -> Any:
        return signal.SIG_DFL

    mocker.patch("brain.vault.watch.signal.signal", _no_op)

    thread = threading.Thread(target=_driver, daemon=True)
    thread.start()

    result = runner.invoke(
        app,
        ["vault", "sync", "--vault", str(vault), "--watch"],
        catch_exceptions=False,
    )

    # Wait for our driver to finish (defensive — should already be done
    # by the time the watcher exits).
    driver_done.wait(timeout=5.0)
    thread.join(timeout=5.0)

    assert result.exit_code == 0, result.stdout
    assert "watching" in result.stdout
    assert "initial sync" in result.stdout


def test_watch_prints_initial_sync_summary(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Any,
    mocker: Any,
) -> None:
    """The initial-sync line surfaces created/updated/skipped/etc."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "a.md", {"id": str(uuid.uuid4()), "title": "A"}, "x\n")

    mocker.patch("brain.vault.watch.Observer", _FakeObserver)

    import signal

    def _no_op(_sig: int, _handler: Any) -> Any:
        return signal.SIG_DFL

    mocker.patch("brain.vault.watch.signal.signal", _no_op)

    def _stopper() -> None:
        try:
            obs = _wait_for_observer()
            time.sleep(0.05)
            obs.handler._state.stop_event.set()
        except AssertionError:
            pass  # pragma: no cover

    threading.Thread(target=_stopper, daemon=True).start()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["vault", "sync", "--vault", str(vault), "--watch"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout
    assert "created 1" in result.stdout
    assert "links_resolved 0" in result.stdout


def test_watch_connects_with_the_watcher_connect_timeout(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Any,
    mocker: Any,
) -> None:
    """Every watcher connection, and so every reconnect, uses WATCHER_CONNECT_TIMEOUT_S."""
    from brain.vault.sync import SyncReport
    from brain.vault.watch import WATCHER_CONNECT_TIMEOUT_S

    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault.mkdir()
    factories: list[Any] = []

    def _capture(conn_factory: Any, **_kwargs: Any) -> SyncReport:
        factories.append(conn_factory)
        return SyncReport()

    mocker.patch("brain.cli.run_watcher", _capture)
    connect = mocker.patch("brain.cli.connect_raw")
    result = CliRunner().invoke(
        app, ["vault", "sync", "--vault", str(vault), "--watch"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.stdout
    factories[0]()

    assert connect.call_args.kwargs.get("connect_timeout") == WATCHER_CONNECT_TIMEOUT_S, (
        f"the watcher's connections are not bounded: {connect.call_args!r}"
    )
