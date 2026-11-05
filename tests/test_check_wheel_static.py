"""Tests for ``scripts/check_wheel_static.py``: a wheel must carry the whole static tree (RM-50).

The script lives outside ``src/brain``, so it is loaded with :mod:`importlib.util`. The
comparison and the exit codes run on synthetic zips and a fake build. The subprocess
wrapper runs real child processes, because its environment, its output cap and its
group kill are exactly what a fake would hide. The real wheel build runs in CI's ``lint``
job and in ``bin/brain-ci``.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from tests.test_capped_process import _until_exited

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_wheel_static.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_wheel_static", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()

TREE = ["index.html", "css/reading.css", "js/main.js", "fonts/a.woff2", "fonts/OFL.txt"]


def _static_tree(root: Path, names: list[str] = TREE) -> Path:
    static = root / "static"
    for name in names:
        path = static / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return static


def _wheel(path: Path, static_names: list[str], extra: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in static_names:
            archive.writestr(f"{check.WHEEL_STATIC_PREFIX}{name}", b"x")
        for name in extra:
            archive.writestr(name, b"x")
    return path


def test_a_complete_wheel_passes_and_other_wheel_contents_are_ignored(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", TREE, extra=("brain/cli.py", "brain/ui/app.py"))

    assert check.missing_from_wheel(static, wheel) == []


def test_a_wheel_missing_a_font_is_refused_by_name(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", [n for n in TREE if n != "fonts/a.woff2"])

    assert check.missing_from_wheel(static, wheel) == ["fonts/a.woff2"]


def test_the_whole_tree_is_covered_not_only_the_fonts(tmp_path: Path) -> None:
    """The original trap: a nested ``css/`` or ``js/`` file no glob reached."""
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", ["index.html", "fonts/a.woff2", "fonts/OFL.txt"])

    assert check.missing_from_wheel(static, wheel) == ["css/reading.css", "js/main.js"]


def test_local_build_artefacts_are_not_demanded(tmp_path: Path) -> None:
    static = _static_tree(tmp_path, [*TREE, "__pycache__/x.pyc", ".DS_Store", "css/.DS_Store"])
    wheel = _wheel(tmp_path / "w.whl", TREE)

    assert check.missing_from_wheel(static, wheel) == []


def test_an_empty_static_tree_is_a_setup_error_not_a_pass(tmp_path: Path) -> None:
    empty = tmp_path / "static"
    empty.mkdir()
    wheel = _wheel(tmp_path / "w.whl", [])

    with pytest.raises(check.WheelCheckError, match="no static files"):
        check.missing_from_wheel(empty, wheel)


def test_a_file_that_is_not_a_wheel_is_a_setup_error(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    bad = tmp_path / "w.whl"
    bad.write_bytes(b"not a zip")

    with pytest.raises(check.WheelCheckError, match="not a readable wheel"):
        check.missing_from_wheel(static, bad)


def _repo(root: Path) -> Path:
    """The files the build copies, with a small source tree."""
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "README.md").write_text("r\n", encoding="utf-8")
    (root / "LICENSE").write_text("l\n", encoding="utf-8")
    static = root / "src" / "brain" / "ui"
    static.mkdir(parents=True)
    _static_tree(static)
    (root / "src" / "brain" / "ui" / "__pycache__").mkdir()
    (root / "src" / "brain" / "ui" / "__pycache__" / "x.pyc").write_bytes(b"x")
    return root


class _FakeBuild:
    """Stands in for the real build and records what it was given."""

    def __init__(self, *, names: list[str] = TREE, returncode: int = 0) -> None:
        self.names = names
        self.returncode = returncode
        self.argv: list[str] = []
        self.copied: set[str] = set()

    def __call__(self, argv: list[str], log: Path) -> int:
        self.argv = argv
        source = Path(argv[-1])
        self.copied = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
        out = Path(argv[argv.index("--wheel-dir") + 1])
        log.write_text("the tail of a build log\n", encoding="utf-8")
        if self.returncode == 0:
            out.mkdir()
            _wheel(out / "x-1-py3-none-any.whl", self.names)
        return self.returncode


def test_main_passes_on_a_complete_wheel_built_from_a_clean_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copy, because an in-place build reuses a stale ``build/`` and can pass falsely."""
    repo = _repo(tmp_path)
    (repo / "build" / "lib").mkdir(parents=True)
    (repo / "build" / "lib" / "stale.txt").write_text("x", encoding="utf-8")
    build = _FakeBuild()

    assert check.main(repo, build=build) == 0

    assert "5 static files" in capsys.readouterr().out
    assert Path(build.argv[-1]) != repo
    assert "src/brain/ui/static/fonts/a.woff2" in build.copied
    assert "pyproject.toml" in build.copied
    assert not any(name.startswith("build/") for name in build.copied)
    assert not any("__pycache__" in name for name in build.copied)


def test_main_exits_1_and_names_the_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    assert check.main(repo, build=_FakeBuild(names=TREE[:-1])) == 1

    assert "fonts/OFL.txt" in capsys.readouterr().err


def test_a_failed_build_exits_2_and_prints_the_tail_of_its_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A red CI step must be diagnosable from its own output."""
    repo = _repo(tmp_path)

    assert check.main(repo, build=_FakeBuild(returncode=1)) == 2

    err = capsys.readouterr().err
    assert "the wheel build failed (exit 1)" in err
    assert "the tail of a build log" in err


def test_a_build_with_no_wheel_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    assert check.main(repo, build=lambda argv, log: 0) == 2

    assert "exactly one wheel" in capsys.readouterr().err


def test_a_repo_it_cannot_build_exits_2_with_a_neutral_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check.main(tmp_path, build=_FakeBuild()) == 2

    err = capsys.readouterr().err
    assert "the build could not run" in err
    assert str(tmp_path) not in err


def test_a_build_that_cannot_start_exits_2_with_the_same_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    def cannot_start(argv: list[str], log: Path) -> int:
        raise FileNotFoundError("/some/secret/path")

    assert check.main(repo, build=cannot_start) == 2

    err = capsys.readouterr().err
    assert "the build could not run" in err
    assert "/some/secret/path" not in err


# ------------------------------------------------ the real child process --


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_the_child_sees_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build backend runs as code; it gets no developer secret from this script's env."""
    log = tmp_path / "log"
    monkeypatch.setenv("FAKE_API_KEY", "not-for-the-backend")
    monkeypatch.setenv("VOYAGE_API_KEY", "not-for-the-backend")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:not-for-the-backend@example.org/simple")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:not-for-the-backend@example.org:1")
    monkeypatch.setenv("PATH", "/bin:/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    code = "import os; print(sorted(os.environ.items()))"
    assert check.run_build(_python(code), log) == 0

    output = log.read_text(encoding="utf-8")
    assert "not-for-the-backend" not in output
    assert "('PATH', '/bin:/usr/bin')" in output
    assert f"('HOME', '{tmp_path}')" in output
    assert f"('TMPDIR', '{tmp_path}')" in output


def test_the_child_env_keeps_what_pip_and_a_shared_libpython_need(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps: setup-python's interpreter needs ``LD_LIBRARY_PATH``, and pip its own config."""
    for name in ("LD_LIBRARY_PATH", "PIP_CONFIG_FILE", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, "/kept")

    env = check.child_env()

    assert {env["LD_LIBRARY_PATH"], env["PIP_CONFIG_FILE"], env["SSL_CERT_FILE"]} == {"/kept"}


def test_the_child_env_omits_a_name_that_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)

    assert "LD_LIBRARY_PATH" not in check.child_env()


def test_the_exit_status_and_the_output_reach_the_log(tmp_path: Path) -> None:
    log = tmp_path / "log"
    code = "import sys; print('from stdout'); sys.stderr.write('from stderr'); sys.exit(3)"

    assert check.run_build(_python(code), log) == 3

    output = log.read_text(encoding="utf-8")
    assert "from stdout" in output
    assert "from stderr" in output


def test_the_tail_is_bounded_and_keeps_the_end(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_bytes(b"x" * (check.TAIL_BYTES * 3) + b"THE END")

    tail = check.log_tail(log)

    assert len(tail) == check.TAIL_BYTES
    assert tail.endswith("THE END")


def test_the_tail_keeps_ordinary_text_whole(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text("  line one\n\tline two\n", encoding="utf-8")

    assert check.log_tail(log) == "  line one\n\tline two\n"


def test_the_tail_cannot_drive_the_terminal(tmp_path: Path) -> None:
    """Build output is not trusted: no escape sequence reaches the caller's terminal."""
    log = tmp_path / "log"
    log.write_bytes(b"before\x1b[2Jafter\xff\r")

    tail = check.log_tail(log)

    assert "\x1b" not in tail
    assert "\r" not in tail
    assert tail.startswith("before?[2Jafter?")


def test_a_log_that_was_never_written_has_an_empty_tail(tmp_path: Path) -> None:
    assert check.log_tail(tmp_path / "missing") == ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timeout_raises_the_check_error_and_takes_the_build_children_with_it(
    tmp_path: Path,
) -> None:
    """``subprocess.run`` would kill pip alone and leave its build hook running."""
    log = tmp_path / "log"
    pid_file = tmp_path / "grandchild"
    code = (
        "import subprocess, sys, pathlib, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "print('started', flush=True)\n"
        # Shorter than the grandchild's. If the group kill misses, ``wait`` returns when
        # this ends, and the grandchild is still there to be seen.
        "time.sleep(15)\n"
    )

    with pytest.raises(check.WheelCheckError, match="timed out") as raised:
        check.run_build(_python(code), log, timeout=3.0)

    assert "started" in str(raised.value)
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 5.0
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "the build's child survived the timeout"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_group_that_is_already_gone_is_not_an_error() -> None:
    """A timeout racing the group's own exit must still report the timeout."""
    process = subprocess.Popen(_python("pass"), start_new_session=True)
    process.wait()

    check.kill_group(process)

    assert process.returncode == 0


def test_a_group_holding_only_an_unreaped_child_is_not_an_error() -> None:
    """macOS answers ``killpg`` on a zombie-only group with ``EPERM``, not ``ESRCH``.

    A build exiting just as the timeout fires leaves exactly that group, and the
    ``PermissionError`` was then reported as "the build could not run". The child is
    left unreaped on purpose. ``os.waitid`` would do that without polling, but macOS
    has it only from Python 3.13, so this uses the ``ps`` wait the RM-43 test uses.
    """
    process = subprocess.Popen(_python("pass"), start_new_session=True)
    _until_exited(process)
    assert process.returncode is None, "the child was reaped before the call under test"

    check.kill_group(process)

    assert process.returncode == 0
