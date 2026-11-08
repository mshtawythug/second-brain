"""``brain ingest-gmail`` end to end for BL-39: the list failure and the three-timeout stop.

A separate file because ``tests/test_cli_ingest_gmail.py`` is past 800 lines. The list tests run
a throwaway ``gws`` script (``tests/gws_stub_fixtures.py``) and fail or finish before the command
connects, and a ``--dry-run`` never connects, so those are marked ``nodb``. The ingest's
stop-rule tests need the test database. Both kinds replace the module-level ``_run``, as the
sibling files do. All messages, addresses and bodies are synthetic.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest.gmail import GmailError, GmailTimeoutError
from brain.ingest.gmail_timeouts import STOP_MESSAGE
from tests.gmail_attachment_fixtures import message_with_parts
from tests.gws_stub_fixtures import HUNG, SYNTH_ADDRESS, answering, gws_on_path

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
#: Nothing listens here: a list test that reached ``connect`` would fail loudly.
UNREACHABLE_DATABASE_URL = "postgresql://localhost:1/none"


def _list_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gws: str) -> None:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    gws_on_path(tmp_path, monkeypatch, gws)


@pytest.mark.nodb
def test_a_list_that_times_out_exits_1_with_a_class_only_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout now raises on the list, which was uncaught: a red line and exit 1."""
    _list_env(tmp_path, monkeypatch, HUNG)
    monkeypatch.setattr("brain.ingest.gmail.GWS_TIMEOUT_SECONDS", 0.3)

    result = CliRunner().invoke(app, ["ingest-gmail", "--from", SYNTH_ADDRESS])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert (
        "ingest-gmail: could not list messages: "
        "`gws gmail users messages list` timed out after 0.3s"
    ) in result.output
    assert SYNTH_ADDRESS not in result.output and "Traceback" not in result.output


@pytest.mark.nodb
def test_a_list_that_answers_keeps_its_ordinary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _list_env(tmp_path, monkeypatch, answering('{"resultSizeEstimate":0}', "{}"))
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}

    result = CliRunner().invoke(app, ["ingest-gmail", "--from", SYNTH_ADDRESS])

    assert result.exit_code == 0, result.output
    assert "found 0 message(s)" in result.output and "no messages matched" in result.output
    # sec-126 F1: the run converts SIGTERM/SIGHUP while it lasts, and restores them after.
    assert {s: signal.getsignal(s) for s in handlers} == handlers


# --- the stop rule (test database) ---------------------------------------------------------

THREADS = [f"t-synth-{i}" for i in range(1, 6)]
MESSAGES = {
    f"m-synth-{i}": message_with_parts(
        f"m-synth-{i}", tid, [], text=f"synthetic body {i}", subject=f"Synthetic subject {i}"
    )
    for i, tid in enumerate(THREADS, start=1)
}


def _runner(outcomes: dict[str, GmailError | None], reads: list[str]) -> Any:
    """A fake ``_run``: ``outcomes`` maps a message id to the error its read raises."""

    def run(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        if cmd[4] == "list":
            stubs = [{"id": m, "threadId": r["threadId"]} for m, r in MESSAGES.items()]
            return json.dumps({"messages": stubs, "resultSizeEstimate": len(stubs)})
        mid = json.loads(cmd[6])["id"]
        reads.append(mid)
        error = outcomes.get(mid)
        if error is not None:
            raise error
        return json.dumps(MESSAGES[mid])

    return run


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object | None,
    tmp_path: Path,
    pattern: str,
) -> tuple[Any, list[str]]:
    """Run ``ingest-gmail`` with each thread's read: ``T`` timeout, ``E`` failure, ``R`` returns.

    With no ``fake_embedder`` it is a ``--dry-run``, which never connects: the database URL is
    then the unreachable one.
    """
    timeout = GmailTimeoutError("`gws gmail users messages get` timed out after 60s")
    failure = GmailError("`gws gmail users messages get` exit 3")
    outcomes = {
        mid: {"T": timeout, "E": failure, "R": None}[event]
        for mid, event in zip(MESSAGES, pattern, strict=True)
    }
    reads: list[str] = []
    dry_run = fake_embedder is None
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL if dry_run else TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    if not dry_run:
        monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    monkeypatch.setattr("brain.ingest.gmail._run", _runner(outcomes, reads))
    args = ["ingest-gmail", "--label", "test-label", *(["--dry-run"] if dry_run else [])]
    return CliRunner().invoke(app, args), reads


def test_three_consecutive_timeouts_stop_the_run_with_the_counts_so_far(
    monkeypatch: pytest.MonkeyPatch, test_db: Any, fake_embedder: object, tmp_path: Path
) -> None:
    result, reads = _invoke(monkeypatch, fake_embedder, tmp_path, "RTTTR")

    assert result.exit_code == 1, result.output
    assert STOP_MESSAGE in result.output
    assert "1 ingested, 0 skipped (unchanged), 3 failed" in result.output
    assert reads == ["m-synth-1", "m-synth-2", "m-synth-3", "m-synth-4"]


def test_timeouts_broken_up_by_a_returned_read_keep_going(
    monkeypatch: pytest.MonkeyPatch, test_db: Any, fake_embedder: object, tmp_path: Path
) -> None:
    result, reads = _invoke(monkeypatch, fake_embedder, tmp_path, "TTRTT")

    assert result.exit_code == 0, result.output
    assert STOP_MESSAGE not in result.output
    assert "1 ingested, 0 skipped (unchanged), 4 failed" in result.output
    assert reads == list(MESSAGES)


def test_other_failures_in_a_row_keep_going(
    monkeypatch: pytest.MonkeyPatch, test_db: Any, fake_embedder: object, tmp_path: Path
) -> None:
    """Only timeouts count: three ordinary failures in a row are per-thread, as before."""
    result, reads = _invoke(monkeypatch, fake_embedder, tmp_path, "EEERE")

    assert result.exit_code == 0, result.output
    assert STOP_MESSAGE not in result.output
    assert "1 ingested, 0 skipped (unchanged), 4 failed" in result.output
    assert reads == list(MESSAGES)


# --- the stop rule under --dry-run (no database: a dry run never connects) -----------------


@pytest.mark.nodb
def test_three_consecutive_timeouts_stop_a_dry_run_with_the_counts_so_far(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sec-126 F2: the dry run swallowed every timeout as "(unable to fetch)" and exited 0."""
    result, reads = _invoke(monkeypatch, None, tmp_path, "RTTTR")

    assert result.exit_code == 1, result.output
    assert STOP_MESSAGE in result.output
    assert result.output.count("Subject: (timed out)") == 3
    assert "listed 4 of 5 thread(s) before stopping" in result.output
    assert reads == ["m-synth-1", "m-synth-2", "m-synth-3", "m-synth-4"]


@pytest.mark.nodb
def test_a_dry_run_stopped_on_its_last_thread_still_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, reads = _invoke(monkeypatch, None, tmp_path, "RRTTT")

    assert result.exit_code == 1, result.output
    assert STOP_MESSAGE in result.output
    assert "listed 5 of 5 thread(s) before stopping" in result.output
    assert reads == list(MESSAGES)


@pytest.mark.nodb
def test_dry_run_timeouts_broken_up_by_a_returned_read_keep_going(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, reads = _invoke(monkeypatch, None, tmp_path, "TTRTT")

    assert result.exit_code == 0, result.output
    assert STOP_MESSAGE not in result.output
    assert result.output.count("Subject: (timed out)") == 4
    assert "total: 5 threads, 5 messages" in result.output
    assert reads == list(MESSAGES)


@pytest.mark.nodb
def test_other_dry_run_failures_still_read_unable_to_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, reads = _invoke(monkeypatch, None, tmp_path, "EEERE")

    assert result.exit_code == 0, result.output
    assert STOP_MESSAGE not in result.output
    assert result.output.count("Subject: (unable to fetch)") == 4
    assert reads == list(MESSAGES)
