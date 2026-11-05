"""``brain search --pick`` with the real ``brain``, the real ``fzf`` and a real pty (RM-52).

Nothing is faked: the command runs as a subprocess on the test database with stdout piped, exactly
like ``id=$(brain search --pick q)``, and the test presses real keys. Faked tools have hidden real
failures here before. CI installs ``fzf``, so a missing binary FAILS under ``GITHUB_ACTIONS``
instead of skipping: a skip there would turn this file off without anyone noticing.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.gaps import top_search_failures
from brain.ingest import Embedder, ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
TERM = "fjordquill"
BODY_MARKER = "preview-body-marker"
OSC_MARKER = "PWNEDTITLE"
KEY_UP_ENTER = b"\x1b[A\r"
KEY_ESC = b"\x1b"
#: Every wait below is bounded; the process group is killed on the way out regardless.
DEADLINE_SECONDS = 60.0


def require_fzf(
    *, env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> str:
    """The ``fzf`` path; a missing binary fails on CI and skips on a developer machine."""
    found = which("fzf")
    if found is not None:
        return found
    if env.get("GITHUB_ACTIONS"):
        pytest.fail("fzf must be installed on CI: the real picker test may not skip there")
    pytest.skip("fzf is not installed")


@pytest.mark.nodb
def test_a_missing_fzf_fails_rather_than_skips_on_ci() -> None:
    with pytest.raises(pytest.fail.Exception, match="may not skip"):
        require_fzf(env={"GITHUB_ACTIONS": "true"}, which=lambda _name: None)


@pytest.mark.nodb
def test_a_missing_fzf_skips_on_a_developer_machine() -> None:
    with pytest.raises(pytest.skip.Exception):
        require_fzf(env={}, which=lambda _name: None)


@pytest.mark.nodb
def test_a_present_fzf_is_returned_on_ci() -> None:
    assert require_fzf(env={"GITHUB_ACTIONS": "true"}, which=lambda _name: "/f") == "/f"


@dataclass(frozen=True)
class PtyRun:
    returncode: int
    stdout: str
    screen: bytes


def _brain_env(tmp_path: Path, fzf: str, **extra: str) -> dict[str, str]:
    """A minimal environment: the test database, this venv's ``brain``, and the real ``fzf``."""
    bin_dirs = [str(Path(sys.executable).parent), str(Path(fzf).parent), "/usr/bin", "/bin"]
    return {
        "PATH": os.pathsep.join(bin_dirs),
        "HOME": str(tmp_path),
        "BRAIN_HOME": str(tmp_path / "brain-home"),
        "DATABASE_URL": TEST_DATABASE_URL,
        "BRAIN_EMBEDDER": "none",
        "SHELL": "/bin/sh",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        **extra,
    }


def _run_under_pty(
    args: list[str], env: dict[str, str], cwd: Path, *, keys: bytes, wait_for: tuple[bytes, ...]
) -> PtyRun:
    """Run ``brain`` with stderr and the controlling terminal on a pty, and stdout piped.

    Presses ``keys`` once every ``wait_for`` marker has appeared on the screen. A marker never
    appearing fails the test rather than hanging it: the whole group is killed at the deadline.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))

    def take_controlling_terminal() -> None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "brain"), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=slave,
        env=env,
        cwd=cwd,
        start_new_session=True,
        preexec_fn=take_controlling_terminal,  # noqa: PLW1509 - the only way to set a ctty
    )
    os.close(slave)
    screen = b""
    sent = False
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail(f"--pick did not finish; screen tail: {screen[-600:]!r}")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    screen += os.read(master, 65536)
                except OSError:
                    break
            if not sent and all(marker in screen for marker in wait_for):
                os.write(master, keys)
                sent = True
        stdout = proc.communicate(timeout=10)[0].decode("utf-8")
        assert proc.returncode is not None
        return PtyRun(proc.returncode, stdout, screen)
    finally:
        os.close(master)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.fixture
def fzf() -> str:
    return require_fzf(env=os.environ)


@pytest.fixture
def ranked_ids(
    test_db: psycopg.Connection[Any],
    fake_embedder: Embedder,
    fzf: str,
    tmp_path: Path,
) -> list[str]:
    """Two documents with a hostile title and body, in the order real search ranks them."""
    for title, body in (
        ("Alpha plan", f"{TERM} first {BODY_MARKER} one"),
        (
            f"Beta\x1b]0;{OSC_MARKER}\x07\tplan\nnotes",
            f"{TERM} second \x1b]0;{OSC_MARKER}\x07 {BODY_MARKER} two",
        ),
    ):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=title, content=body, content_type="note", source_path=None, metadata={}
            ),
            source_kind="manual",
            source_external_id=f"manual:{title[:5]}",
        )
    test_db.commit()
    plain = subprocess.run(
        [
            str(Path(sys.executable).parent / "brain"),
            "search", TERM, "--fts-only", "--no-meta", "--json",
        ],
        capture_output=True,
        env=_brain_env(tmp_path, fzf),
        cwd=tmp_path,
        timeout=60,
        check=True,
    )
    ids = [row["id"] for row in json.loads(plain.stdout)]
    assert len(ids) == 2
    return ids


def _pick_args() -> list[str]:
    return ["search", TERM, "--fts-only", "--pick"]


def test_real_fzf_up_then_enter_prints_the_second_result_and_records_the_click(
    ranked_ids: list[str],
    test_db: psycopg.Connection[Any],
    fzf: str,
    tmp_path: Path,
) -> None:
    run = _run_under_pty(
        _pick_args(),
        _brain_env(tmp_path, fzf),
        tmp_path,
        keys=KEY_UP_ENTER,
        wait_for=(b"Alpha plan", b"Beta", BODY_MARKER.encode()),
    )
    assert run.returncode == 0, run.screen[-600:]
    assert run.stdout == f"{ranked_ids[1]}\n"
    # The fixture's plain `--json` search logged the NULL row; only the pick minted a session.
    [(plain_session,), (search_session,)] = test_db.execute(
        "SELECT session_id FROM search_queries ORDER BY at"
    ).fetchall()
    assert plain_session is None
    [(doc, query, click_session, source)] = test_db.execute(
        "SELECT document_id::text, query, session_id, source FROM interactions "
        "WHERE action = 'clicked'"
    ).fetchall()
    assert search_session is not None
    assert click_session == search_session
    assert (doc, query, source) == (ranked_ids[1], TERM, "cli")


def test_real_fzf_shows_no_id_and_no_escape_from_a_hostile_title_or_body(
    ranked_ids: list[str], fzf: str, tmp_path: Path
) -> None:
    run = _run_under_pty(
        _pick_args(),
        _brain_env(tmp_path, fzf),
        tmp_path,
        keys=KEY_ESC,
        wait_for=(b"Alpha plan", b"Beta", BODY_MARKER.encode()),
    )
    assert run.returncode == 130
    # The cursor starts on the first result, whose id is in the preview's own `id:` line
    # (`brain show` prints it); the second result's id is on no line of the screen.
    assert ranked_ids[1].encode() not in run.screen
    assert OSC_MARKER.encode() not in run.screen
    assert b"\x1b]0;" not in run.screen


def test_real_fzf_esc_prints_nothing_records_no_click_and_shows_in_gaps(
    ranked_ids: list[str],
    test_db: psycopg.Connection[Any],
    fzf: str,
    tmp_path: Path,
) -> None:
    run = _run_under_pty(
        _pick_args(), _brain_env(tmp_path, fzf), tmp_path, keys=KEY_ESC, wait_for=(b"Alpha plan",)
    )
    assert (run.returncode, run.stdout) == (130, "")
    assert test_db.execute("SELECT count(*) FROM interactions").fetchone() == (0,)
    failures = top_search_failures(test_db, since_days=7, limit=10)
    assert [(f.query, f.kind) for f in failures] == [(TERM, "no_click")]


def test_real_fzf_ignores_a_hostile_environment(
    ranked_ids: list[str],
    test_db: psycopg.Connection[Any],
    fzf: str,
    tmp_path: Path,
) -> None:
    """Honoured, these would pick with no human, or run a command. Neither happens."""
    marker = tmp_path / "hostile-ran"
    env = _brain_env(
        tmp_path,
        fzf,
        FZF_DEFAULT_OPTS=f"--select-1 --filter=plan --bind=enter:execute-silent(touch {marker})",
        FZF_DEFAULT_COMMAND=f"touch {marker}",
    )
    run = _run_under_pty(_pick_args(), env, tmp_path, keys=KEY_ESC, wait_for=(b"Alpha plan",))
    assert (run.returncode, run.stdout) == (130, "")
    assert not marker.exists()
    assert test_db.execute("SELECT count(*) FROM interactions").fetchone() == (0,)


def test_real_fzf_enter_with_a_hostile_bind_still_returns_the_top_result(
    ranked_ids: list[str], fzf: str, tmp_path: Path
) -> None:
    marker = tmp_path / "hostile-ran"
    env = _brain_env(
        tmp_path,
        fzf,
        FZF_DEFAULT_OPTS=f"--bind=enter:execute-silent(touch {marker})+abort",
    )
    run = _run_under_pty(_pick_args(), env, tmp_path, keys=b"\r", wait_for=(b"Alpha plan",))
    assert (run.returncode, run.stdout) == (0, f"{ranked_ids[0]}\n")
    assert not marker.exists()


def test_a_real_pipe_on_stderr_is_refused_at_once_rather_than_hanging(
    fzf: str, tmp_path: Path
) -> None:
    """No terminal at all: real ``fzf`` would hang here, so the preflight must stop it first."""
    done = subprocess.run(
        [str(Path(sys.executable).parent / "brain"), *_pick_args()],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_brain_env(tmp_path, fzf),
        cwd=tmp_path,
        timeout=60,
        start_new_session=True,
        check=False,
    )
    assert done.returncode == 1
    assert b"--pick needs a terminal" in done.stderr
    assert done.stdout == b""


def test_a_terminal_on_stderr_with_no_controlling_tty_is_refused_rather_than_hanging(
    fzf: str, tmp_path: Path
) -> None:
    """stderr is a pty, but ``setsid`` leaves no ``/dev/tty``: the probe's second half."""
    master, slave = pty.openpty()
    screen = b""
    try:
        done = subprocess.run(
            [str(Path(sys.executable).parent / "brain"), *_pick_args()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=slave,
            env=_brain_env(tmp_path, fzf),
            cwd=tmp_path,
            timeout=60,
            start_new_session=True,
            check=False,
        )
        # Read while this end of the slave is open: macOS drops the buffer once it closes.
        while select.select([master], [], [], 0.2)[0]:
            screen += os.read(master, 65536)
    finally:
        os.close(slave)
        os.close(master)
    assert done.returncode == 1
    assert b"--pick needs a terminal" in screen
