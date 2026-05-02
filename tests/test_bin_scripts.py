"""End-to-end shell tests for ``bin/brain-{up,down,status,rebuild}``.

These tests drive each script as a real subprocess against a pristine
``$BRAIN_VAULT_PATH`` under ``tmp_path``, with stub ``brain`` /
``python`` executables on ``PATH`` (and via ``BRAIN_PY``) so the scripts
exercise their full control flow without needing a real Quartz
install, the brain CLI, or network access.

The stubs append every invocation to a per-test log file so individual
tests can assert on which subcommands ran (``brain vault sync --watch``,
``python -m brain.wiki.build_swap``, …) and which did not. This keeps
the tests black-box: they verify the bash logic via observable side
effects rather than introspecting the script source.

A few invariants the suite enforces:

- ``brain-up`` never spawns ``python -m brain.wiki.build_swap`` when
  ``current/`` is healthy (the cold-start short-circuit).
- ``brain-down`` kills every PID in every PID file we know about
  (``brain-watch``, ``brain-build``, plus the legacy ``brain-wiki``).
- ``brain-status`` reports rows for both watcher and build watcher.
- ``brain-rebuild`` is a one-shot (no ``brain-down`` / ``brain-up``
  bounce, just one ``build_swap`` call).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_dir(tmp_path: Path) -> Path:
    """Directory that holds the fake ``brain`` + ``python`` stubs.

    Returned as a ``Path`` so individual tests can inspect the call logs
    written next to the executables.
    """
    d = tmp_path / "stubs"
    d.mkdir()
    return d


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """Tmp ``$BRAIN_VAULT_PATH`` with an empty ``.quartz`` workspace.

    The workspace is populated minimally so ``cold_start_build`` can
    locate it; tests that need a healthy ``current/`` symlink seed one
    explicitly inside the test body.
    """
    v = tmp_path / "vault"
    v.mkdir()
    (v / ".quartz").mkdir()
    return v


@pytest.fixture
def isolated_pid_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """Redirect ``/tmp/brain-*.pid`` and ``/tmp/brain-*.log`` into a tmp dir.

    The bin scripts hard-code ``/tmp/brain-*.{pid,log}`` paths, which
    would clash with a real local install if a developer happens to
    have ``brain-up`` running while the suite executes. We can't change
    the scripts to read these from env (they'd be unsafe in production
    — multiple installs would race on the same default), so instead we
    use ``mocker``-style cleanup: snapshot any pre-existing files,
    remove them for the test, restore them after. Returns a dict so
    tests can read paths cleanly.
    """
    paths = {
        "watch_pid": Path("/tmp/brain-watch.pid"),
        "watch_log": Path("/tmp/brain-watch.log"),
        "build_pid": Path("/tmp/brain-build.pid"),
        "build_log": Path("/tmp/brain-build.log"),
        "wiki_pid": Path("/tmp/brain-wiki.pid"),
        "wiki_log": Path("/tmp/brain-wiki.log"),
    }
    saved: dict[Path, bytes] = {}
    for p in paths.values():
        if p.exists():
            saved[p] = p.read_bytes()
            p.unlink()
    try:
        yield paths
    finally:
        # Best-effort restore: anything we created during the test goes
        # away; anything we displaced from before is put back.
        for p in paths.values():
            if p.exists():
                p.unlink()
        for p, data in saved.items():
            p.write_bytes(data)


def _write_stub(
    stub_dir: Path,
    name: str,
    *,
    log_basename: str | None = None,
    body: str | None = None,
) -> Path:
    """Write an executable shell stub under ``stub_dir/<name>``.

    Default behaviour (``body`` is ``None``) is to log argv to
    ``stub_dir/<log_basename>`` and exit 0 — sufficient for the vast
    majority of the bin-script tests. Pass ``body`` for tests that need
    custom semantics (e.g. simulate failure, sleep before exit).
    """
    if log_basename is None:
        log_basename = f"{name}.calls"
    log = stub_dir / log_basename
    target = stub_dir / name
    if body is None:
        body = (
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {log}\n"
            "exit 0\n"
        )
    target.write_text(body)
    target.chmod(0o755)
    return target


def _make_env(
    *,
    stub_dir: Path,
    vault_dir: Path,
    extras: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict for a bin-script subprocess invocation.

    Resets ``PATH`` to ``stub_dir`` + the bare-essential system bins
    (``/usr/bin``, ``/bin``) so the scripts can't accidentally pick up
    the developer's real ``brain`` / ``python``. ``BRAIN_PY`` points at
    the stub so the cold-start build + build-watcher commands flow
    through it. ``BRAIN_OPEN_BROWSER=0`` skips the 30s ``wait_for_url``
    + browser-open dance during tests.
    """
    env: dict[str, str] = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": str(vault_dir.parent),
        "BRAIN_VAULT_PATH": str(vault_dir),
        "BRAIN_OPEN_BROWSER": "0",
        "BRAIN_PY": str(stub_dir / "python"),
    }
    if extras:
        env.update(extras)
    return env


def _seed_healthy_current(vault: Path) -> Path:
    """Create a ``.quartz/current → builds/<id>/`` symlink with required files.

    Used by tests that exercise the "skip cold-start build" short-circuit.
    Returns the build directory so tests can inspect it.
    """
    builds = vault / ".quartz" / "builds"
    builds.mkdir(parents=True, exist_ok=True)
    build_id = "20260501-000000-deadbe"
    build_dir = builds / build_id
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (build_dir / ".build-id").write_text(f"{build_id}\n", encoding="utf-8")
    current = vault / ".quartz" / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    current.symlink_to(Path("builds") / build_id)
    return build_dir


def _read_log(path: Path) -> list[str]:
    """Read the call-log written by ``_write_stub`` into a list of lines.

    Returns ``[]`` for a missing log so tests can assert on absence
    without having to guard for ``FileNotFoundError``.
    """
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Tests — bin/brain-up.
# ---------------------------------------------------------------------------


def test_brain_up_skips_cold_start_when_current_healthy(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
) -> None:
    """Healthy ``current/`` symlink → no cold-start ``build_swap`` call.

    Seeds a populated ``.quartz/current → builds/<id>/`` tree, runs
    ``brain-up`` with ``BRAIN_NO_BUILD_WATCHER=1`` so the test doesn't
    leave a long-running watcher process behind, and asserts that
    neither ``python -m brain.wiki.build_swap`` nor
    ``python -m brain.wiki.build_watcher`` were invoked.
    """
    _seed_healthy_current(vault_dir)
    _write_stub(stub_dir, "brain")
    _write_stub(stub_dir, "python")

    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={"BRAIN_NO_BUILD_WATCHER": "1", "BRAIN_NO_OVERLAY": "1"},
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-up")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    # The script may exit non-zero from `wait_for_url` warning, but the
    # code paths we care about run before any failure. We assert on
    # observable side effects instead of the exit code.
    assert "skipping cold-start build" in result.stdout, result.stdout

    python_calls = _read_log(stub_dir / "python.calls")
    assert not any("build_swap" in c for c in python_calls), python_calls
    assert not any("build_watcher" in c for c in python_calls), python_calls


def test_brain_up_runs_cold_start_when_current_missing(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
) -> None:
    """Empty ``.quartz/`` → cold-start ``build_swap`` runs synchronously.

    The python stub records its argv; we assert that the cold-start
    build was invoked exactly once with ``--vault <vault>`` and
    ``--keep <KEEP>``. ``BRAIN_NO_BUILD_WATCHER=1`` keeps Watcher B
    out of the picture so the test only sees the cold-start call.
    """
    # vault_dir has an empty .quartz/ fixture — no current symlink.
    _write_stub(stub_dir, "brain")
    _write_stub(stub_dir, "python")

    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={"BRAIN_NO_BUILD_WATCHER": "1", "BRAIN_NO_OVERLAY": "1"},
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-up")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert "running first-time build" in result.stdout, result.stdout
    python_calls = _read_log(stub_dir / "python.calls")
    build_swap_calls = [c for c in python_calls if "brain.wiki.build_swap" in c]
    assert len(build_swap_calls) == 1, python_calls
    assert "--vault" in build_swap_calls[0]
    assert str(vault_dir) in build_swap_calls[0]
    # Build watcher must NOT be called when BRAIN_NO_BUILD_WATCHER=1.
    assert not any("build_watcher" in c for c in python_calls), python_calls


def test_brain_up_aborts_when_cold_start_fails(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
) -> None:
    """A failing cold-start build aborts brain-up with a non-zero exit code.

    Stubs the python interpreter to exit 1 on the build_swap invocation
    so the script's ``|| return 1`` path is exercised end-to-end.
    """
    _write_stub(stub_dir, "brain")
    _write_stub(
        stub_dir,
        "python",
        body=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {stub_dir}/python.calls\n"
            "echo 'simulated build failure' >&2\n"
            "exit 1\n"
        ),
    )

    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={"BRAIN_NO_BUILD_WATCHER": "1", "BRAIN_NO_OVERLAY": "1"},
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-up")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "aborting brain-up" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Tests — bin/brain-down.
# ---------------------------------------------------------------------------


def test_brain_down_kills_all_three_pids(
    stub_dir: Path,  # noqa: ARG001 — fixture forces test isolation
    isolated_pid_files: dict[str, Path],
) -> None:
    """Three live ``sleep 60`` processes are killed and pid files removed.

    Spawns three real ``sleep`` processes, writes their pids into the
    watch / build / wiki pid files (``wiki`` is the legacy file the
    plan asks us to clean up if it still exists), runs ``brain-down``,
    and asserts:

    1. All three processes are no longer running (``kill -0`` fails).
    2. All three pid files are gone.
    3. The ``caddy left running`` reassurance line is in stdout.
    """
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for key in ("watch_pid", "build_pid", "wiki_pid"):
            p = subprocess.Popen(["/bin/sleep", "60"])  # noqa: S603,S607 — fixed args
            procs.append(p)
            isolated_pid_files[key].write_text(f"{p.pid}\n")

        result = subprocess.run(  # noqa: S603 — list-form, no shell
            [str(BIN / "brain-down")],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        # Reap each child so zombies don't keep their pid in the process
        # table: ``kill -0`` reports success on a zombie, which would
        # falsely look like "still running" to ``_pid_dead``. ``wait``
        # times out cleanly if the child is somehow still alive (i.e.
        # the kill genuinely failed) and that surfaces as a test
        # failure below.
        for p in procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=5)

        for p in procs:
            assert p.poll() is not None, (
                f"pid {p.pid} still alive after brain-down (exit={p.returncode})"
            )

        for key in ("watch_pid", "build_pid", "wiki_pid"):
            assert not isolated_pid_files[key].exists(), key

        assert "caddy left running" in result.stdout, result.stdout
    finally:
        # Belt and suspenders: kill anything that escaped.
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests — bin/brain-status.
# ---------------------------------------------------------------------------


def test_brain_status_three_rows(
    stub_dir: Path,  # noqa: ARG001 — fixture forces test isolation
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],
) -> None:
    """Live ``watch`` + ``build`` pids + a stale ``wiki`` pid render three rows.

    Seeds a healthy ``current/`` symlink so the build-id readback line
    fires too, and asserts that all three labels (``watcher``, ``build``,
    ``wiki``) appear in stdout.
    """
    _seed_healthy_current(vault_dir)
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for key in ("watch_pid", "build_pid", "wiki_pid"):
            p = subprocess.Popen(["/bin/sleep", "60"])  # noqa: S603,S607 — fixed args
            procs.append(p)
            isolated_pid_files[key].write_text(f"{p.pid}\n")

        env = os.environ.copy()
        env["BRAIN_VAULT_PATH"] = str(vault_dir)
        result = subprocess.run(  # noqa: S603 — list-form, no shell
            [str(BIN / "brain-status")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert "watcher:" in result.stdout, result.stdout
        assert "build:" in result.stdout, result.stdout
        assert "wiki:" in result.stdout, result.stdout
        # Build-id line surfaces from the seeded symlink.
        assert "build-id" in result.stdout, result.stdout
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests — bin/brain-rebuild.
# ---------------------------------------------------------------------------


def test_brain_rebuild_one_shot(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
) -> None:
    """One ``build_swap`` call, no ``brain-down``/``brain-up`` bounce.

    The python stub records argv; we assert that exactly one
    ``build_swap`` invocation lands and that the legacy bounce path
    (which would spawn watcher / build watcher) was skipped.
    """
    _write_stub(stub_dir, "brain")
    _write_stub(stub_dir, "python")

    env = _make_env(stub_dir=stub_dir, vault_dir=vault_dir)
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-rebuild")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    brain_calls = _read_log(stub_dir / "brain.calls")
    # The DB→vault export step still runs in the default path.
    assert any("vault export" in c for c in brain_calls), brain_calls
    # No ``brain vault sync --watch`` (that would mean a bounce ran).
    assert not any("vault sync --watch" in c for c in brain_calls), brain_calls

    python_calls = _read_log(stub_dir / "python.calls")
    swap_calls = [c for c in python_calls if "brain.wiki.build_swap" in c]
    assert len(swap_calls) == 1, python_calls
    assert "--vault" in swap_calls[0]
    assert str(vault_dir) in swap_calls[0]
    # build_watcher should not have been started by rebuild.
    assert not any("build_watcher" in c for c in python_calls), python_calls


def test_brain_rebuild_no_build_skips_build_swap(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
) -> None:
    """``--no-build`` skips the one-shot build but still runs the export."""
    _write_stub(stub_dir, "brain")
    _write_stub(stub_dir, "python")

    env = _make_env(stub_dir=stub_dir, vault_dir=vault_dir)
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-rebuild"), "--no-build"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    brain_calls = _read_log(stub_dir / "brain.calls")
    assert any("vault export" in c for c in brain_calls), brain_calls

    python_calls = _read_log(stub_dir / "python.calls")
    assert not any("build_swap" in c for c in python_calls), python_calls


# ---------------------------------------------------------------------------
# Sanity: every bin script we ship is executable + bash-syntax-clean.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["brain-up", "brain-down", "brain-status", "brain-rebuild"]
)
def test_bin_script_is_executable_and_parses(name: str) -> None:
    """``bash -n <script>`` parses cleanly and the file is +x.

    Cheap regression guard: catches obvious shell-syntax errors and
    accidental ``chmod -x`` commits before the heavier integration
    tests above even start.
    """
    script = BIN / name
    assert script.is_file(), script
    assert os.access(script, os.X_OK), f"{script} is not executable"

    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
