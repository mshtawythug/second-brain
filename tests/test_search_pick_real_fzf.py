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
ORDINARY_TERM = "marrowlight"
MARKER_PREFIX = b"preview-marker-"
#: Everything that reached a pty raw on real fzf 0.72.0 (sec-121 F1), plus the C1 forms.
CANARY = "cHduZWQ"
HOSTILE = (
    f"\x1b]1337;Copy=:{CANARY}=\x07"
    f"\x1b]1337;SetUserVar=x={CANARY}\x1b\\"
    f"\x1bPtmux;\x1b\x1b]52;c;{CANARY}=\x07\x1b\\"
    f"\x1b]52;c;{CANARY}=\x07"
    f"\x1bPq#0;2;0;0;0#0~~{CANARY}\x1b\\"
    f"\x1b_Gf=100,a=T;{CANARY}\x1b\\"
    f"\x1b]8;;https://{CANARY}.invalid\x1b\\"
    # Kept short: a long leftover would push the marker past the pane's right edge.
    "\u009d0\u009c\u009b31m"
)
FORBIDDEN = (
    b"\x1b]1337",
    b"\x1bPtmux",
    b"\x1b]52",
    b"\x1bPq",
    b"\x1b_G",
    b"\x1b]8;",
    "\u009b".encode(),
    "\u009d".encode(),
    CANARY.encode(),
)
KEY_UP_ENTER = b"\x1b[A\r"
KEY_UP = b"\x1b[A"
KEY_ESC = b"\x1b"
#: A non-zero pixel size opens more of fzf's pass-through path (sec-121 F1).
WINSIZE = struct.pack("HHHH", 40, 160, 1600, 800)
#: Every wait below is bounded; the process group is killed on the way out regardless.
DEADLINE_SECONDS = 30.0
BOUNDED_SECONDS = 30.0

Action = Callable[["subprocess.Popen[bytes]", int], None]
Step = tuple[tuple[bytes, ...], Action]


def press(keys: bytes) -> Action:
    """An action that types ``keys`` on the terminal."""
    return lambda _proc, master: _write(master, keys)


def _write(master: int, keys: bytes) -> None:
    os.write(master, keys)


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
    # Both outcomes are caught on purpose. Catching only the failure let a skip escape, which
    # turned this very test into a skip: mutation-tested, and it survived until this was fixed.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        require_fzf(env={"GITHUB_ACTIONS": "true"}, which=lambda _name: None)
    assert caught.type is pytest.fail.Exception
    assert "may not skip" in str(caught.value)


@pytest.mark.nodb
def test_a_missing_fzf_skips_on_a_developer_machine() -> None:
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        require_fzf(env={}, which=lambda _name: None)
    assert caught.type is pytest.skip.Exception


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


def _run_bounded(
    args: list[str], env: dict[str, str], cwd: Path, *, stderr: int = subprocess.PIPE
) -> subprocess.CompletedProcess[bytes]:
    """Run ``brain`` in its own session with no terminal, and kill the whole group at the deadline.

    A plain ``subprocess.run(timeout=...)`` kills only ``brain``: if a regression ever reached real
    ``fzf`` here, it would be orphaned, hung, for the life of the machine.
    """
    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "brain"), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=BOUNDED_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail(f"brain {' '.join(args)} did not finish in {BOUNDED_SECONDS}s")
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _drain(master: int) -> bytes:
    """Whatever is left on the pty. The caller's slave stays open: macOS drops it on close."""
    chunks = b""
    while select.select([master], [], [], 0.3)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks += chunk
    return chunks


def _run_under_pty(
    args: list[str], env: dict[str, str], cwd: Path, *, steps: list[Step]
) -> PtyRun:
    """Run ``brain`` with stderr and the controlling terminal on a pty, and stdout piped.

    Each step waits for its markers on the screen, then acts. A marker never appearing fails the
    test rather than hanging it: the whole group is killed at the deadline.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, WINSIZE)

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
    screen = b""
    pending = list(steps)
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail(f"--pick did not finish; screen tail: {screen[-600:]!r}")
            if select.select([master], [], [], 0.1)[0]:
                screen += os.read(master, 65536)
            if pending and all(marker in screen for marker in pending[0][0]):
                pending.pop(0)[1](proc, master)
        screen += _drain(master)
        stdout = proc.communicate(timeout=10)[0].decode("utf-8")
        assert proc.returncode is not None
        assert not pending, f"steps never ran: {len(pending)}; tail: {screen[-600:]!r}"
        return PtyRun(proc.returncode, stdout, screen)
    finally:
        os.close(slave)
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
    """Two documents, both hostile in title and body, in the order real search ranks them.

    Both, so that no test here depends on which one ranks first to have rendered a hostile preview.
    """
    for name, marker in (("Alpha plan", "one"), ("Beta plan", "two")):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"{HOSTILE}{name}\tnotes{HOSTILE}",
                content=f"{HOSTILE} {TERM} {HOSTILE} preview-marker-{marker} {HOSTILE} end",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:{name}",
        )
    test_db.commit()
    plain = _run_bounded(
        ["search", TERM, "--fts-only", "--no-meta", "--json"], _brain_env(tmp_path, fzf), tmp_path
    )
    assert plain.returncode == 0, plain.stderr
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
        steps=[((b"Alpha plan", b"Beta plan", MARKER_PREFIX), press(KEY_UP_ENTER))],
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


def test_real_fzf_no_sequence_from_a_hostile_title_or_body_reaches_the_terminal(
    ranked_ids: list[str], fzf: str, tmp_path: Path
) -> None:
    """Both rows previewed, each proved by a marker, on a pty that reports a pixel size."""
    run = _run_under_pty(
        _pick_args(),
        _brain_env(tmp_path, fzf),
        tmp_path,
        steps=[
            ((b"Alpha plan", b"Beta plan", MARKER_PREFIX), press(KEY_UP)),
            ((MARKER_PREFIX + b"one", MARKER_PREFIX + b"two"), press(KEY_ESC)),
        ],
    )
    assert run.returncode == 130
    for forbidden in FORBIDDEN:
        assert forbidden not in run.screen, forbidden


def test_real_fzf_preview_keeps_ordinary_text(
    test_db: psycopg.Connection[Any],
    fake_embedder: Embedder,
    fzf: str,
    tmp_path: Path,
) -> None:
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="季度计划 résumé",
            content=f"{ORDINARY_TERM} Çà et là\tcolumn\n\nsecond-paragraph-marker",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:ordinary",
    )
    test_db.commit()
    wanted = ("季度计划".encode(), "Çà et là".encode(), b"second-paragraph-marker")
    run = _run_under_pty(
        ["search", ORDINARY_TERM, "--fts-only", "--pick"],
        _brain_env(tmp_path, fzf),
        tmp_path,
        steps=[(wanted, press(KEY_ESC))],
    )
    assert run.returncode == 130
    for text in wanted:
        assert text in run.screen, text


def test_real_fzf_previews_under_sh_whatever_shell_the_user_has(
    ranked_ids: list[str], fzf: str, tmp_path: Path
) -> None:
    """fzf runs the preview with ``$SHELL -c``, and the pipe needs a POSIX shell: it gets one."""
    marker = tmp_path / "user-shell-ran"
    user_shell = tmp_path / "not-posix-shell"
    user_shell.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    user_shell.chmod(0o700)
    env = _brain_env(tmp_path, fzf, SHELL=str(user_shell))
    run = _run_under_pty(
        _pick_args(), env, tmp_path, steps=[((MARKER_PREFIX,), press(KEY_ESC))]
    )
    assert run.returncode == 130
    assert not marker.exists()


def _interrupt_brain_only(proc: subprocess.Popen[bytes], _master: int) -> None:
    os.kill(proc.pid, signal.SIGINT)


def test_real_fzf_a_sigint_to_brain_alone_still_restores_the_terminal(
    ranked_ids: list[str],
    test_db: psycopg.Connection[Any],
    fzf: str,
    tmp_path: Path,
) -> None:
    """sec-121 F2: a SIGKILLed fzf leaves the alternate screen up, with a body still on it."""
    run = _run_under_pty(
        _pick_args(),
        _brain_env(tmp_path, fzf),
        tmp_path,
        steps=[((MARKER_PREFIX,), _interrupt_brain_only)],
    )
    assert (run.returncode, run.stdout) == (130, "")
    assert b"\x1b[?1049l" in run.screen
    assert test_db.execute("SELECT count(*) FROM interactions").fetchone() == (0,)


def test_real_fzf_esc_prints_nothing_records_no_click_and_shows_in_gaps(
    ranked_ids: list[str],
    test_db: psycopg.Connection[Any],
    fzf: str,
    tmp_path: Path,
) -> None:
    run = _run_under_pty(
        _pick_args(),
        _brain_env(tmp_path, fzf),
        tmp_path,
        steps=[((b"Alpha plan",), press(KEY_ESC))],
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
    run = _run_under_pty(
        _pick_args(), env, tmp_path, steps=[((b"Alpha plan",), press(KEY_ESC))]
    )
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
    run = _run_under_pty(
        _pick_args(), env, tmp_path, steps=[((b"Alpha plan",), press(b"\r"))]
    )
    assert (run.returncode, run.stdout) == (0, f"{ranked_ids[0]}\n")
    assert not marker.exists()


def test_a_real_pipe_on_stderr_is_refused_at_once_rather_than_hanging(
    fzf: str, tmp_path: Path
) -> None:
    """No terminal at all: real ``fzf`` would hang here, so the preflight must stop it first."""
    done = _run_bounded(_pick_args(), _brain_env(tmp_path, fzf), tmp_path)
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
        done = _run_bounded(_pick_args(), _brain_env(tmp_path, fzf), tmp_path, stderr=slave)
        # Read while this end of the slave is open: macOS drops the buffer once it closes.
        while select.select([master], [], [], 0.2)[0]:
            screen += os.read(master, 65536)
    finally:
        os.close(slave)
        os.close(master)
    assert done.returncode == 1
    assert b"--pick needs a terminal" in screen
