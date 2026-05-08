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


def test_brain_up_bootstraps_launchd_when_supervisor_not_skipped(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — used for cleanup side effect
    tmp_path: Path,
) -> None:
    """brain-up calls install-launchd → launchctl bootstrap fires for both labels.

    This is the "single source of truth for daemon lifecycle" regression
    guard: the pre-2026-05-08 brain-up used `nohup ... &` to spawn the
    daemons, which silently bypassed launchd supervision when the agents
    happened to be unloaded (e.g. after a brain-down booted them out).
    Now brain-up unconditionally delegates to install-launchd, which is
    idempotent (rewrites + reboots the plists every run).

    The test stubs launchctl into a tmp dir, points BRAIN_LAUNCHD_DIR at
    a tmp directory (so we don't drop plists into the developer's real
    ~/Library/LaunchAgents), seeds a healthy current/ to short-circuit
    cold-start, and verifies launchctl was called with `bootstrap` for
    both com.brain.watcher AND com.brain.build.
    """
    _seed_healthy_current(vault_dir)
    _write_stub(stub_dir, "brain")
    _write_stub(stub_dir, "python")
    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=False))

    launchd_dir = tmp_path / "LaunchAgents"
    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        # NOTE: deliberately omit BRAIN_NO_BUILD_WATCHER so install-launchd
        # actually runs. Override the install-launchd-internal knobs so
        # the bootstrap calls land against our launchctl stub + tmp dir.
        extras={
            "BRAIN_NO_OVERLAY": "1",
            "BRAIN_LAUNCHD_DIR": str(launchd_dir),
            "BRAIN_LAUNCHCTL": str(stub_dir / "launchctl"),
        },
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-up")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    # brain-up may exit non-zero from wait_for_url's 30s timeout warning
    # (no Caddy in the test environment) — that's fine; we assert on the
    # observable side effect of install-launchd having fired.
    launchctl_calls = _read_log(stub_dir / "launchctl.calls")
    bootstrap_calls = [c for c in launchctl_calls if c.startswith("bootstrap ")]
    assert len(bootstrap_calls) == 2, (
        f"brain-up must bootstrap BOTH watcher and build LaunchAgents; "
        f"saw {bootstrap_calls!r} (full launchctl call log: {launchctl_calls!r}; "
        f"brain-up stdout: {result.stdout!r}; stderr: {result.stderr!r})"
    )
    assert any("com.brain.watcher.plist" in c for c in bootstrap_calls), bootstrap_calls
    assert any("com.brain.build.plist" in c for c in bootstrap_calls), bootstrap_calls

    # Both plists landed on disk in the tmp dir, not the developer's
    # real ~/Library/LaunchAgents.
    assert (launchd_dir / "com.brain.watcher.plist").is_file()
    assert (launchd_dir / "com.brain.build.plist").is_file()

    # Report line surfaces the new "supervisor: launchd (KeepAlive)" tag
    # so the user immediately knows what's supervising.
    assert "launchd" in result.stdout, result.stdout


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
    stub_dir: Path,
    isolated_pid_files: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Three live ``sleep 60`` processes are killed and pid files removed.

    Spawns three real ``sleep`` processes, writes their pids into the
    watch / build / wiki pid files (``wiki`` is the legacy file the
    plan asks us to clean up if it still exists), runs ``brain-down``,
    and asserts:

    1. All three processes are no longer running (``kill -0`` fails).
    2. All three pid files are gone.
    3. The ``caddy left running`` reassurance line is in stdout.

    Stubs `launchctl` (and points BRAIN_LAUNCHD_DIR at a tmp dir) so the
    test doesn't accidentally bootout real LaunchAgents from the
    developer's launchd state — the bootout-when-loaded path has its own
    dedicated test, this one is just the kill-loop.
    """
    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=False))
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for key in ("watch_pid", "build_pid", "wiki_pid"):
            p = subprocess.Popen(["/bin/sleep", "60"])  # noqa: S603,S607 — fixed args
            procs.append(p)
            isolated_pid_files[key].write_text(f"{p.pid}\n")

        env = os.environ.copy()
        env["BRAIN_LAUNCHD_DIR"] = str(tmp_path / "LaunchAgents")
        env["BRAIN_LAUNCHCTL"] = str(stub_dir / "launchctl")
        result = subprocess.run(  # noqa: S603 — list-form, no shell
            [str(BIN / "brain-down")],
            env=env,
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
    "name",
    [
        "brain-up",
        "brain-down",
        "brain-status",
        "brain-rebuild",
        # Phase: launchd supervision (2026-05-08). The wrappers are
        # foreground entrypoints that the LaunchAgent plists exec; the
        # install/uninstall scripts manage the plists in
        # ~/Library/LaunchAgents/. All four ship under bin/ and must
        # parse + be executable.
        "_brain-watcher-fg",
        "_brain-build-fg",
        "brain-install-launchd",
        "brain-uninstall-launchd",
    ],
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


# ---------------------------------------------------------------------------
# Tests — launchd-friendly foreground wrappers (`_brain-{watcher,build}-fg`).
# ---------------------------------------------------------------------------
#
# These wrappers exist so a LaunchAgent can supervise the daemons end-to-end
# (KeepAlive=true). They write the pid file so `brain-status` keeps working
# unchanged, then `exec` the underlying watcher. Tests use the same stub
# pattern as the rest of this file: a stub `brain` / `python` on PATH (with
# BRAIN_PY for the build wrapper, mirroring brain-up) records argv to a log
# file we can assert on. Because exec replaces the wrapper's process image
# with the stub's, the wrapper still completes cleanly when the stub exits 0.


def test_brain_watcher_fg_writes_pid_and_execs_brain_sync(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],
) -> None:
    """Wrapper writes /tmp/brain-watch.pid then execs `brain vault sync --watch`.

    Verifies the pid-file contract upstream from launchd supervision: the
    file `brain-status` reads must contain the wrapper's pid (which becomes
    the watcher's pid post-exec) and the watcher must actually be invoked
    with the expected argv.

    `BRAIN_SKIP_VENV_AUTOLOAD=1` keeps the wrapper from prepending the
    developer's real `.venv/bin` to PATH (which would mask the stub
    `brain` and run the actual watcher against the test vault, hanging
    the test). Mirrors the BRAIN_PY override pattern used elsewhere.
    """
    _write_stub(stub_dir, "brain")
    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={"BRAIN_SKIP_VENV_AUTOLOAD": "1"},
    )

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "_brain-watcher-fg")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert isolated_pid_files["watch_pid"].exists(), "wrapper must write the pid file"
    pid_content = isolated_pid_files["watch_pid"].read_text().strip()
    assert pid_content.isdigit(), f"pid file should contain an integer, got {pid_content!r}"

    brain_calls = _read_log(stub_dir / "brain.calls")
    assert any("vault sync --watch" in c for c in brain_calls), brain_calls
    assert any(str(vault_dir) in c for c in brain_calls), brain_calls


def test_brain_watcher_fg_aborts_when_brain_cli_missing(
    stub_dir: Path,  # noqa: ARG001 — fixture forces test isolation
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],
) -> None:
    """No `brain` on PATH → wrapper exits non-zero and never writes pid file.

    Guards against the failure mode where launchd respawns a wrapper that
    can't find the project venv (e.g. PATH not set in the plist's
    EnvironmentVariables block). The wrapper must surface the error
    instead of silently writing a pid file pointing at no real process.
    """
    # PATH points at an empty stub dir — no `brain`, no `python`. The
    # BRAIN_SKIP_VENV_AUTOLOAD knob keeps the wrapper from silently
    # prepending the developer's real `.venv/bin` (which would resolve
    # `brain` and defeat the test).
    empty_dir = vault_dir.parent / "empty-stubs"
    empty_dir.mkdir()
    env = {
        "PATH": f"{empty_dir}:/usr/bin:/bin",
        "HOME": str(vault_dir.parent),
        "BRAIN_VAULT_PATH": str(vault_dir),
        "BRAIN_SKIP_VENV_AUTOLOAD": "1",
    }

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "_brain-watcher-fg")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "brain CLI not on PATH" in result.stderr, result.stderr
    assert not isolated_pid_files["watch_pid"].exists(), (
        "wrapper must NOT write a pid file when it can't actually run the watcher"
    )


def test_brain_build_fg_writes_pid_and_execs_build_watcher(
    stub_dir: Path,
    vault_dir: Path,
    isolated_pid_files: dict[str, Path],
) -> None:
    """Wrapper writes /tmp/brain-build.pid then execs `python -m brain.wiki.build_watcher`.

    Mirrors the watcher-fg test but for the build daemon. Asserts the
    invocation includes the env-driven `--vault` and `--keep` args that
    `bin/brain-up` historically passed by hand.
    """
    _write_stub(stub_dir, "python")
    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={"BRAIN_WIKI_KEEP_BUILDS": "5"},
    )

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "_brain-build-fg")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert isolated_pid_files["build_pid"].exists(), "wrapper must write the pid file"
    pid_content = isolated_pid_files["build_pid"].read_text().strip()
    assert pid_content.isdigit(), pid_content

    python_calls = _read_log(stub_dir / "python.calls")
    build_lines = [c for c in python_calls if "brain.wiki.build_watcher" in c]
    assert len(build_lines) == 1, python_calls
    assert "--vault" in build_lines[0]
    assert str(vault_dir) in build_lines[0]
    assert "--keep 5" in build_lines[0], build_lines[0]


# ---------------------------------------------------------------------------
# Tests — `bin/brain-install-launchd` + `bin/brain-uninstall-launchd`.
# ---------------------------------------------------------------------------
#
# These exercise the install/uninstall lifecycle without touching the
# developer's real launchd domain. We point BRAIN_LAUNCHD_DIR at a tmp
# directory and pass a stub `launchctl` via BRAIN_LAUNCHCTL so every
# `bootout` / `bootstrap` / `print` call lands in a log file we can assert on.


def _launchctl_stub_body(stub_dir: Path, *, print_succeeds: bool) -> str:
    """Build a launchctl stub: logs argv, controls `print` exit code.

    `print` is the only subcommand whose return value drives behavior in
    our scripts (it's the "is this label loaded?" probe). Tests pass
    `print_succeeds=True` to simulate "loaded", `False` for "not loaded".
    All other subcommands (`bootout`, `bootstrap`) just exit 0.
    """
    return (
        "#!/usr/bin/env bash\n"
        f"echo \"$@\" >> {stub_dir}/launchctl.calls\n"
        'if [[ "${1:-}" == "print" ]]; then\n'
        f"  exit {0 if print_succeeds else 1}\n"
        "fi\n"
        "exit 0\n"
    )


def test_install_launchd_writes_plists_and_bootstraps(
    stub_dir: Path,
    vault_dir: Path,
    tmp_path: Path,
) -> None:
    """install-launchd writes both plists with KeepAlive=true and bootstraps them.

    Plist content is checked structurally (ProgramArguments line points at
    the wrapper, KeepAlive present, Label correct). The stub launchctl
    captures calls so we can assert the bootstrap step actually fired for
    both labels.
    """
    launchd_dir = tmp_path / "LaunchAgents"
    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=False))

    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={
            "BRAIN_LAUNCHD_DIR": str(launchd_dir),
            "BRAIN_LAUNCHCTL": str(stub_dir / "launchctl"),
        },
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-install-launchd")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    watcher_plist = launchd_dir / "com.brain.watcher.plist"
    build_plist = launchd_dir / "com.brain.build.plist"
    assert watcher_plist.is_file(), watcher_plist
    assert build_plist.is_file(), build_plist

    watcher_text = watcher_plist.read_text()
    assert "<string>com.brain.watcher</string>" in watcher_text
    assert "_brain-watcher-fg" in watcher_text
    assert "<key>KeepAlive</key>" in watcher_text
    assert "<true/>" in watcher_text  # KeepAlive=true
    assert "<key>RunAtLoad</key>" in watcher_text
    assert "/tmp/brain-watch.log" in watcher_text

    build_text = build_plist.read_text()
    assert "<string>com.brain.build</string>" in build_text
    assert "_brain-build-fg" in build_text
    assert "<key>KeepAlive</key>" in build_text
    assert "/tmp/brain-build.log" in build_text

    launchctl_calls = _read_log(stub_dir / "launchctl.calls")
    bootstrap_calls = [c for c in launchctl_calls if c.startswith("bootstrap ")]
    assert len(bootstrap_calls) == 2, launchctl_calls
    assert any("com.brain.watcher.plist" in c for c in bootstrap_calls)
    assert any("com.brain.build.plist" in c for c in bootstrap_calls)


def test_uninstall_launchd_boots_out_and_removes_plists(
    stub_dir: Path,
    vault_dir: Path,
    tmp_path: Path,
) -> None:
    """uninstall removes both plist files and `bootout`s their labels.

    Pre-seeds the tmp LaunchAgents dir with two plists (so the rm path
    runs), points the script at a launchctl stub whose `print` succeeds
    (= "label is loaded"), and asserts both bootout calls land + both
    plists are gone afterward.
    """
    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    (launchd_dir / "com.brain.watcher.plist").write_text("<plist/>", encoding="utf-8")
    (launchd_dir / "com.brain.build.plist").write_text("<plist/>", encoding="utf-8")

    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=True))

    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={
            "BRAIN_LAUNCHD_DIR": str(launchd_dir),
            "BRAIN_LAUNCHCTL": str(stub_dir / "launchctl"),
        },
    )
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-uninstall-launchd")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert not (launchd_dir / "com.brain.watcher.plist").exists()
    assert not (launchd_dir / "com.brain.build.plist").exists()

    launchctl_calls = _read_log(stub_dir / "launchctl.calls")
    bootout_calls = [c for c in launchctl_calls if c.startswith("bootout ")]
    assert any("com.brain.watcher" in c for c in bootout_calls), launchctl_calls
    assert any("com.brain.build" in c for c in bootout_calls), launchctl_calls


def test_brain_down_boots_out_launchd_when_loaded(
    stub_dir: Path,
    isolated_pid_files: dict[str, Path],
    tmp_path: Path,
) -> None:
    """brain-down bootouts both labels when launchctl reports them loaded.

    Ensures that when the user has installed the LaunchAgents, plain
    `kill` is no longer enough — brain-down must also bootout so launchd
    stops respawning the daemons within ThrottleInterval seconds.
    """
    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=True))

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
    env["BRAIN_LAUNCHD_DIR"] = str(launchd_dir)
    env["BRAIN_LAUNCHCTL"] = str(stub_dir / "launchctl")

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-down")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    launchctl_calls = _read_log(stub_dir / "launchctl.calls")
    bootout_calls = [c for c in launchctl_calls if c.startswith("bootout ")]
    assert any("com.brain.watcher" in c for c in bootout_calls), launchctl_calls
    assert any("com.brain.build" in c for c in bootout_calls), launchctl_calls
    assert "unloaded launchd" in result.stdout, result.stdout


def test_brain_down_skips_bootout_when_not_loaded(
    stub_dir: Path,
    isolated_pid_files: dict[str, Path],  # noqa: ARG001 — fixture isolation
    tmp_path: Path,
) -> None:
    """brain-down does NOT bootout when launchctl `print` reports not loaded.

    Backwards compat: users who never installed the LaunchAgents must see
    the same brain-down behavior as before — no bootout call, no
    "unloaded launchd" line, just the normal kill loop.
    """
    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    _write_stub(stub_dir, "launchctl", body=_launchctl_stub_body(stub_dir, print_succeeds=False))

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
    env["BRAIN_LAUNCHD_DIR"] = str(launchd_dir)
    env["BRAIN_LAUNCHCTL"] = str(stub_dir / "launchctl")

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-down")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    launchctl_calls = _read_log(stub_dir / "launchctl.calls")
    bootout_calls = [c for c in launchctl_calls if c.startswith("bootout ")]
    assert bootout_calls == [], (
        "brain-down must not bootout labels that aren't loaded — "
        f"saw {bootout_calls}"
    )
    assert "unloaded launchd" not in result.stdout, result.stdout
