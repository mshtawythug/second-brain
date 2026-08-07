"""Tests for build_watcher fault latching — the 496 MB log generator.

The watcher retries on every vault edit. Before latching, a *persistent* fault
emitted one full Rich traceback per event, which is how
``com.brain.watcher.err.log`` reached 506,756,066 bytes (~250k copies of the
same ~2 KB traceback) over 12 days.

Separate module from ``test_build_watcher.py`` so the latch contract stays
readable on its own.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from brain.config import ConfigError
from brain.wiki.build_watcher import (
    _ErrorLatch,
    _fault_key,
    _Handler,
    _run_initial_build,
    _WatcherState,
)
from brain.wiki.errors import BrainWikiBuildError, BrainWikiConfigError

_LOGGER_NAME = "brain.wiki.build_watcher"

# Stands in for the real "DATABASE_URL is not set (see .env.example)" chain.
# Synthetic — no real paths or credentials.
_CONFIG_FAULT = "DATABASE_URL is not set (see .env.example)"


def _make_handler(tmp_path: Path) -> _Handler:
    return _Handler(
        state=_WatcherState(),
        vault=tmp_path,
        quartz_dir=None,
        debounce_seconds=0.01,
        keep=3,
        on_build=None,
        refresh_runner=None,
    )


# ---------------------------------------------------------------------------
# _ErrorLatch unit behaviour
# ---------------------------------------------------------------------------


def test_latch_logs_first_occurrence_then_suppresses() -> None:
    latch = _ErrorLatch()
    assert latch.should_log("boom") is True
    assert latch.should_log("boom") is False
    assert latch.should_log("boom") is False


def test_latch_reports_a_different_fault_immediately() -> None:
    """A new fault must never be hidden behind an older latched one."""
    latch = _ErrorLatch()
    assert latch.should_log("first") is True
    assert latch.should_log("second") is True
    assert latch.should_log("second") is False


def test_latch_rearms_after_clear() -> None:
    """Recovery re-arms, so a recurrence is reported rather than swallowed."""
    latch = _ErrorLatch()
    assert latch.should_log("boom") is True
    latch.clear()
    assert latch.should_log("boom") is True


def test_latch_is_thread_safe() -> None:
    """Exactly one thread may win the first-log race for a given fault.

    _do_full_build (build thread) and _run_refresh_related_once (refresh
    thread) can latch concurrently.
    """
    latch = _ErrorLatch()
    winners: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def contend() -> None:
        barrier.wait()
        won = latch.should_log("same-fault")
        with lock:
            winners.append(won)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sum(winners) == 1, "exactly one thread should log the first occurrence"


def test_fault_key_distinguishes_type_and_message() -> None:
    assert _fault_key(BrainWikiConfigError("a")) != _fault_key(BrainWikiBuildError("a"))
    assert _fault_key(BrainWikiConfigError("a")) != _fault_key(BrainWikiConfigError("b"))
    assert _fault_key(BrainWikiConfigError("a")) == _fault_key(BrainWikiConfigError("a"))


# ---------------------------------------------------------------------------
# _do_full_build — the actual 496 MB regression
# ---------------------------------------------------------------------------


def test_repeated_config_failure_logs_once_without_traceback(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """200 vault edits under one config fault produce ONE bounded line.

    Without the latch this is 200 records; without the dedicated
    BrainWikiConfigError arm each of those carries a full Rich traceback. That
    product is exactly what filled 506,756,066 bytes.
    """
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=BrainWikiConfigError(_CONFIG_FAULT),
    )
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        for _ in range(200):
            handler._do_full_build()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1, f"expected 1 latched record, got {len(records)}"
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is None, "config faults must not carry a traceback"
    assert _CONFIG_FAULT in records[0].getMessage()


def test_repeated_build_failure_is_also_latched(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A persistent build fault floods the log just as effectively.

    Models the live Quartz timeout, which failed identically on every attempt.
    First occurrence keeps its traceback for diagnosis; repeats are suppressed.
    """
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=BrainWikiBuildError("quartz build exceeded 600.0s"),
    )
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        for _ in range(50):
            handler._do_full_build()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].exc_info is not None, "first build failure keeps its traceback"


def test_distinct_faults_are_each_reported(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Latching must not hide a new, different failure."""
    build = mocker.patch("brain.wiki.build_watcher.build_and_swap")
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        build.side_effect = BrainWikiConfigError(_CONFIG_FAULT)
        handler._do_full_build()
        handler._do_full_build()
        build.side_effect = BrainWikiBuildError("quartz build exceeded 600.0s")
        handler._do_full_build()
        handler._do_full_build()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 2, "one record per distinct fault"


def test_two_config_faults_of_the_same_type_are_both_reported(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Same exception TYPE, different cause -> both must surface.

    BrainWikiConfigError has TWO sources inside build_and_swap: a failed
    Config.load (bad DATABASE_URL) and resolve_build_timeout_s rejecting a
    malformed $BRAIN_WIKI_BUILD_TIMEOUT_S. Because the latch keys on type PLUS
    message, a typo'd timeout is not masked by an earlier config fault.

    Guards against a future "simplification" of the latch to key on type alone,
    which would permanently mute whichever of the two faults arrived second.
    """
    build = mocker.patch("brain.wiki.build_watcher.build_and_swap")
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        build.side_effect = BrainWikiConfigError(_CONFIG_FAULT)
        handler._do_full_build()
        handler._do_full_build()
        build.side_effect = BrainWikiConfigError(
            "BRAIN_WIKI_BUILD_TIMEOUT_S must be a positive number of seconds"
        )
        handler._do_full_build()
        handler._do_full_build()

    messages = [
        r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME
    ]
    assert len(messages) == 2, "each distinct config fault must be reported once"
    assert any("DATABASE_URL" in m for m in messages)
    assert any("BRAIN_WIKI_BUILD_TIMEOUT_S" in m for m in messages)


def test_config_fault_line_does_not_assume_a_cause(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """The bounded line passes the exception through, naming no specific cause.

    The arm must not hardcode "DATABASE_URL": the same exception type is also
    raised for a malformed build-timeout value.
    """
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=BrainWikiConfigError("some other misconfiguration"),
    )
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        handler._do_full_build()

    message = next(r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME)
    assert "some other misconfiguration" in message
    assert "DATABASE_URL" not in message


def test_success_rearms_the_latch(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """After a recovery, the same fault recurring must be reported again."""
    build = mocker.patch("brain.wiki.build_watcher.build_and_swap")
    handler = _make_handler(tmp_path)
    mocker.patch.object(handler, "_update_state_after_full")
    mocker.patch.object(handler, "_schedule_refresh_related")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        build.side_effect = BrainWikiConfigError(_CONFIG_FAULT)
        handler._do_full_build()

        build.side_effect = None
        build.return_value = mocker.Mock(build_id="b1", elapsed_seconds=1.0)
        handler._do_full_build()  # recovery clears the latch

        build.side_effect = BrainWikiConfigError(_CONFIG_FAULT)
        handler._do_full_build()

    errors = [
        r
        for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno == logging.ERROR
    ]
    assert len(errors) == 2, "fault after recovery must not be swallowed"


# ---------------------------------------------------------------------------
# _run_refresh_related_once — the second line from the original bug report
# ---------------------------------------------------------------------------


def test_refresh_related_config_failure_is_error_and_latched(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """Logged at ERROR (was WARNING) and only once.

    At WARNING this read as routine noise while the daemon did nothing useful
    for 12 days. It must NOT raise: this runs on a daemon thread, where raising
    kills the thread silently instead of exiting the process.
    """
    mocker.patch(
        "brain.config.Config.load", side_effect=ConfigError(_CONFIG_FAULT)
    )
    handler = _make_handler(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        for _ in range(100):
            handler._run_refresh_related_once()  # must not raise

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR, "WARNING under-reported a dead daemon"
    assert records[0].exc_info is None


# ---------------------------------------------------------------------------
# _run_initial_build — bounded line per launchd restart
# ---------------------------------------------------------------------------


def test_initial_build_config_failure_has_no_traceback(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """launchd restarts this process on a throttle; each restart must stay cheap."""
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=BrainWikiConfigError(_CONFIG_FAULT),
    )

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        result = _run_initial_build(tmp_path, quartz_dir=None, keep=3)

    assert result is None, "watcher still starts so a later edit gets another shot"
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is None, "a ~2 KB traceback per restart refills the log"
