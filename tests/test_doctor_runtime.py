"""Regression tests for the runtime-health doctor checks (:mod:`brain.doctor_runtime`).

These cover the twelve-day outage in which a missing ``$BRAIN_HOME/.env`` broke
all three launchd daemons while `brain doctor` reported everything green and
exited 0. Each fault below MUST move the exit code; the all-healthy case is the
positive control that stops the failure tests from passing vacuously.

Every seam is injected: no test shells out to the real ``launchctl``, reads the
real ``~/.brain`` or ``~/brain-vault``, or touches ``~/Library/LaunchAgents``.
All fixture values are synthetic (CLAUDE.md Rule 15).
"""
import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

from brain.doctor_runtime import (
    _default_launchd_dir,
    daemon_code_doctor_check,
    daemon_doctor_check,
    dotenv_doctor_check,
    runtime_doctor_checks,
)

# The labels the daemon checks cover (phase 8: com.brain.build is retired and
# reported only by its own check, never by these).
_LABELS = ("com.brain.watcher", "com.brain.brief")


def _statuses(*, running: bool = True, **by_label: int) -> str:
    """Render a fake ``launchctl list`` table (header + tab-separated rows).

    ``running`` controls the PID column for the brain labels: launchctl prints
    ``-`` for a job that is not currently running, which is how the check tells
    "restarted after a signal" from "dead".
    """
    pid = "4242" if running else "-"
    rows = ["PID\tStatus\tLabel", "501\t0\tcom.apple.example"]
    rows.extend(f"{pid}\t{status}\t{label}" for label, status in by_label.items())
    return "\n".join(rows) + "\n"


class _FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess`` from ``launchctl list``."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _install_plists(
    launchd_dir: Path,
    *labels: str,
    brain_py: str | None = None,
    keep_alive: bool = False,
) -> None:
    """Write minimal, valid LaunchAgent plists so the check sees them installed.

    ``keep_alive`` mirrors the real templates: watcher/build are resident
    daemons, brief is a one-shot StartCalendarInterval job.
    """
    for label in labels:
        payload: dict[str, object] = {
            "Label": label,
            "StandardErrorPath": f"/synthetic/logs/{label}.err.log",
        }
        if keep_alive:
            payload["KeepAlive"] = True
        if brain_py is not None:
            payload["EnvironmentVariables"] = {"BRAIN_PY": brain_py}
        (launchd_dir / f"{label}.plist").write_bytes(plistlib.dumps(payload))


def _daemons(launchd_dir: Path, stdout: str, *, returncode: int = 0):
    """Run the daemon check on darwin with ``launchctl list`` stubbed out."""
    with patch(
        "brain.doctor_runtime.subprocess.run",
        return_value=_FakeCompleted(stdout, returncode),
    ):
        return daemon_doctor_check(
            launchd_dir=launchd_dir,
            launchctl="launchctl",
            platform="darwin",
        )


# ---------------------------------------------------------------------------
# config — $BRAIN_HOME/.env
# ---------------------------------------------------------------------------


def test_missing_brain_home_dotenv_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression: a missing $BRAIN_HOME/.env must FAIL, never pass quietly.

    This is the exact twelve-day outage — the daemons got no environment and
    died, while doctor stayed green.
    """
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    (check,) = dotenv_doctor_check()

    assert check.status == "fail"
    assert "MISSING" in check.detail
    assert check.remedy == "brain setup"


def test_dangling_dotenv_symlink_is_not_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relocated dev checkout leaves $BRAIN_HOME/.env dangling.

    ``Path.exists()`` follows symlinks, so this looks identical to "missing"
    unless the check probes ``is_symlink()`` — but the remedies differ, so the
    two must be reported differently.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    (home / ".env").symlink_to(tmp_path / "moved-away" / ".env")

    (check,) = dotenv_doctor_check()

    assert check.status == "fail"
    assert "DANGLING SYMLINK" in check.detail
    assert "MISSING" not in check.detail


def test_unreadable_dotenv_is_distinct_from_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env that is present but unparseable (here: a directory) FAILs its own way."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    (check,) = dotenv_doctor_check()

    assert check.status == "fail"
    assert "could NOT be read" in check.detail


def test_dotenv_lacking_required_keys_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present + parseable but missing DATABASE_URL still kills the daemons.

    The interactive CLI can survive this via an exported shell var; launchd
    cannot. Doctor must not be fooled by the ambient environment.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("BRAIN_EMBEDDER=arctic\n", encoding="utf-8")
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic/exported-in-shell")
    monkeypatch.chdir(tmp_path)

    (check,) = dotenv_doctor_check()

    assert check.status == "fail"
    assert "DATABASE_URL" in check.detail


@pytest.mark.parametrize("ignore_cwd", ["", "1"])
def test_dotenv_lookup_survives_a_shorter_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore_cwd: str
) -> None:
    """``BRAIN_IGNORE_CWD_DOTENV=1`` REMOVES the cwd entry from the chain.

    The check must keep resolving ``$BRAIN_HOME/.env`` by PATH, never by
    position: a 3-entry chain becomes 2 entries under that flag (and collapses
    further in a dev checkout, where ``$BRAIN_HOME`` IS the repo root), so an
    index-based lookup would silently read a different file and report a false
    OK for a missing daemon config.
    """
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    # A cwd .env that WOULD satisfy a positional lookup by mistake.
    (work / ".env").write_text(
        "DATABASE_URL=postgresql://synthetic/cwd\n", encoding="utf-8"
    )
    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", ignore_cwd)
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.chdir(work)

    (check,) = dotenv_doctor_check()

    assert check.status == "fail", (
        "the cwd .env must never stand in for a missing $BRAIN_HOME/.env — "
        "the daemons do not read the cwd"
    )
    assert str(home / ".env") in check.detail


def test_healthy_dotenv_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "DATABASE_URL=postgresql://synthetic:synthetic@localhost:5999/synthetic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    (check,) = dotenv_doctor_check()

    assert check.status == "ok"


# ---------------------------------------------------------------------------
# daemons
# ---------------------------------------------------------------------------


def test_daemon_last_exit_one_fails(tmp_path: Path) -> None:
    """A LaunchAgent whose last exit was non-zero must FAIL, not warn.

    Measured live during the outage: two of three agents sat at last-exit-1
    while doctor exited 0.
    """
    _install_plists(tmp_path, *_LABELS)
    stdout = _statuses(
        **{"com.brain.watcher": 1, "com.brain.brief": 1}
    )

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "fail"
    assert "com.brain.watcher last exit 1" in check.detail
    assert "com.brain.brief last exit 1" in check.detail


def test_daemon_error_log_path_comes_from_the_installed_plist(tmp_path: Path) -> None:
    """The remedy must point at the log launchd ACTUALLY writes.

    Recomputing it from ``$BRAIN_HOME`` sends the user to a nonexistent path
    whenever doctor runs from a dev checkout, because the plists were rendered
    with a different ``$BRAIN_HOME`` at install time.
    """
    _install_plists(tmp_path, *_LABELS)
    stdout = _statuses(**{lbl: (1 if lbl == "com.brain.brief" else 0) for lbl in _LABELS})

    (check,) = _daemons(tmp_path, stdout)

    rendered = "\n".join(line.text for line in check.lines)
    assert "/synthetic/logs/com.brain.brief.err.log" in rendered


def test_exit_3_is_an_ordinary_failure_now_that_build_swap_is_gone(tmp_path: Path) -> None:
    """R9-4 (phase 9): exit 3 gets no "MISCONFIGURED" special case any more.

    Exit 3 was the deleted ``brain.wiki.build_swap``'s "a human must act"
    contract, and the check used to word it specially and withhold the reload
    remedy. No surviving daemon emits it — the watcher's ``ConfigError`` exits 2
    through the Typer group — so a 3 from one is a plain non-zero exit: FAIL,
    named with its code, with the ordinary reload remedy.
    """
    _install_plists(tmp_path, *_LABELS)
    stdout = _statuses(**{"com.brain.watcher": 3, "com.brain.brief": 0})

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "fail"
    assert "com.brain.watcher last exit 3" in check.detail
    assert "MISCONFIGURED" not in check.detail
    assert "brain-install-launchd" in (check.remedy or "")


def test_nonzero_exit_suggests_a_reload(tmp_path: Path) -> None:
    """A plain non-zero exit is retryable — the reload remedy is right there."""
    _install_plists(tmp_path, *_LABELS)
    stdout = _statuses(**dict.fromkeys(_LABELS, 1))

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "fail"
    assert "brain-install-launchd" in (check.remedy or "")
    assert "MISCONFIGURED" not in check.detail


def test_signal_death_that_recovered_warns_but_does_not_fail(tmp_path: Path) -> None:
    """Restarted after SIGTERM and running again → nothing is down. WARN.

    Before phase 8 this was composed with wiki freshness (a stale wiki turned it
    into a FAIL). At R2 the wiki is dormant, so its age says nothing about a
    daemon; ``tests/test_doctor_watcher.py`` pins the decoupling.
    """
    _install_plists(tmp_path, *_LABELS, keep_alive=True)
    stdout = _statuses(
        **{"com.brain.watcher": -15, "com.brain.brief": 0}
    )

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "warn"
    assert "signal 15" in check.detail


def test_signal_killed_keepalive_daemon_that_is_down_fails(tmp_path: Path) -> None:
    """A resident daemon that is NOT RUNNING is down, whatever killed it.

    Regression for a real hole: an earlier parser discarded the PID column and
    so reported a dead KeepAlive daemon as a routine restart.
    """
    _install_plists(tmp_path, *_LABELS, keep_alive=True)
    stdout = _statuses(
        running=False,
        **{"com.brain.watcher": -9, "com.brain.brief": 0},
    )

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "fail"
    assert "NOT RUNNING" in check.detail


def test_one_shot_brief_job_not_running_is_normal(tmp_path: Path) -> None:
    """`com.brain.brief` is StartCalendarInterval — idle is its resting state."""
    _install_plists(tmp_path, "com.brain.brief", keep_alive=False)
    stdout = _statuses(running=False, **{"com.brain.brief": -15})

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "warn", "a one-shot job at rest must not read as down"


def test_installed_but_not_loaded_fails(tmp_path: Path) -> None:
    """Plist on disk but absent from launchctl: nothing is running."""
    _install_plists(tmp_path, *_LABELS)
    stdout = _statuses(**{"com.brain.watcher": 0})

    (check,) = _daemons(tmp_path, stdout)

    assert check.status == "fail"
    assert "com.brain.brief installed but NOT LOADED" in check.detail


def test_no_agents_installed_is_not_broken(tmp_path: Path) -> None:
    """A fresh install that never opted into daemons must not cry wolf."""
    (check,) = _daemons(tmp_path, _statuses())

    assert check.status == "ok"
    assert "not installed" in check.detail


def test_non_macos_degrades_gracefully(tmp_path: Path) -> None:
    """launchd is macOS-only — a Linux user gets no red line for it."""
    _install_plists(tmp_path, *_LABELS)

    (check,) = daemon_doctor_check(
        launchd_dir=tmp_path, launchctl="launchctl", platform="linux"
    )

    assert check.status == "ok"
    assert "macOS" in check.detail


def test_all_daemons_healthy_passes(tmp_path: Path) -> None:
    _install_plists(tmp_path, *_LABELS)

    (check,) = _daemons(tmp_path, _statuses(**dict.fromkeys(_LABELS, 0)))

    assert check.status == "ok"


def test_unusable_launchctl_warns_rather_than_failing(tmp_path: Path) -> None:
    """A broken probe is doctor's problem, not the user's — never a false FAIL."""
    _install_plists(tmp_path, *_LABELS)

    with patch(
        "brain.doctor_runtime.subprocess.run", side_effect=OSError("no launchctl")
    ):
        (check,) = daemon_doctor_check(
            launchd_dir=tmp_path, launchctl="launchctl", platform="darwin"
        )

    assert check.status == "warn"


# ---------------------------------------------------------------------------
# hermeticity — the suite must never read THIS machine's launchd state
# ---------------------------------------------------------------------------


def test_suite_never_reads_host_launchd_state() -> None:
    """The daemons check must be blind to the developer's real LaunchAgents.

    Without the ``_force_test_runtime_home`` fixture in conftest, this check
    reads ``~/Library/LaunchAgents`` and inherits whatever exit status the
    host's daemons last had — so a developer whose watcher last exited 1 gets a
    block of unrelated red tests, while Linux CI stays green because the
    non-darwin branch returns early. That split (green CI, red macOS) is the
    worst failure mode available, and it is how a suite stops being believed.

    House rule: a detector needs a test proving it still detects, and a
    hermetic test needs a test proving it is still hermetic. This is the
    second half; :func:`test_hermetic_default_still_detects_a_failing_daemon`
    is the first, and the two must be read together — hermeticity achieved by
    neutering the check would be worthless.
    """
    resolved = _default_launchd_dir()

    assert resolved != Path.home() / "Library" / "LaunchAgents", (
        "the doctor tests are pointed at the REAL LaunchAgents directory; "
        "_force_test_runtime_home in conftest is not in effect"
    )
    assert not list(resolved.glob("com.brain.*.plist")), (
        f"{resolved} unexpectedly contains brain plists"
    )

    # With no plists the check short-circuits to the fresh-install shape and
    # never even spawns launchctl — an intentionally unrunnable binary proves
    # no subprocess is attempted.
    (check,) = daemon_doctor_check(
        launchd_dir=resolved,
        launchctl="/nonexistent/launchctl",
        platform="darwin",
    )
    assert check.status == "ok"
    assert "not installed" in check.detail


def test_hermetic_default_still_detects_a_failing_daemon(tmp_path: Path) -> None:
    """Isolation must not be achieved by making the check toothless.

    The paired half of :func:`test_suite_never_reads_host_launchd_state`: given
    a launchd dir that DOES hold a plist and a launchctl reporting last-exit-1,
    the very same code path still FAILs.
    """
    _install_plists(tmp_path, *_LABELS, keep_alive=True)

    (check,) = _daemons(tmp_path, _statuses(**dict.fromkeys(_LABELS, 1)))

    assert check.status == "fail"
    assert "last exit 1" in check.detail


def test_doctor_cli_daemons_line_is_host_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the production `brain doctor` call site honours the seam.

    Asserting on ``runtime_doctor_checks`` alone would not prove the CLI wiring
    resolves ``launchd_dir`` through ``BRAIN_LAUNCHD_DIR`` — and the wiring is
    exactly where a host-reading regression would reappear.
    """
    from brain.doctor_runtime import runtime_doctor_checks

    checks = {c.check: c for c in runtime_doctor_checks(platform="darwin")}

    assert checks["daemons"].status == "ok"
    assert "not installed" in checks["daemons"].detail


# ---------------------------------------------------------------------------
# daemon code drift — do the daemons import the same `brain` as this CLI?
# ---------------------------------------------------------------------------


def test_daemons_pinned_at_a_different_install_warns(tmp_path: Path) -> None:
    """The observed state: daemons on a uv-tool install, CLI on the checkout.

    Every other check then describes the CLI's world, not the daemons' — the
    same silent drift that let the outage hide.
    """
    _install_plists(tmp_path, *_LABELS, brain_py="/synthetic/other-install/bin/python")

    with patch(
        "brain.doctor_runtime._package_dir_for",
        return_value="/synthetic/other-install/lib/site-packages/brain",
    ):
        (check,) = daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin")

    assert check.status == "warn", "must not FAIL — dev-checkout CLI is legitimate"
    assert "DIFFERENT" in check.detail
    assert check.remedy


def test_daemons_on_the_same_package_pass(tmp_path: Path) -> None:
    _install_plists(tmp_path, *_LABELS, brain_py="/synthetic/other-install/bin/python")
    import brain.doctor_runtime as dr

    ours = str(Path(dr.__file__).resolve().parent)

    with patch("brain.doctor_runtime._package_dir_for", return_value=ours):
        (check,) = daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin")

    assert check.status == "ok"


def test_daemon_interpreter_that_cannot_import_brain_warns(tmp_path: Path) -> None:
    """A BRAIN_PY that cannot import `brain` means every daemon run dies."""
    _install_plists(tmp_path, *_LABELS, brain_py="/synthetic/broken/bin/python")

    with patch("brain.doctor_runtime._package_dir_for", return_value=None):
        (check,) = daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin")

    assert check.status == "warn"
    assert "cannot import" in check.detail


def test_daemon_code_check_is_silent_when_there_is_nothing_to_compare(
    tmp_path: Path,
) -> None:
    """No plists, no BRAIN_PY, or non-macOS: the `daemons` check already covers it."""
    assert daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin") == []

    _install_plists(tmp_path, *_LABELS)  # installed, but no BRAIN_PY pinned
    assert daemon_code_doctor_check(launchd_dir=tmp_path, platform="darwin") == []
    assert daemon_code_doctor_check(launchd_dir=tmp_path, platform="linux") == []


# ---------------------------------------------------------------------------
# aggregate — the positive control
# ---------------------------------------------------------------------------


def test_all_healthy_yields_no_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a fully healthy runtime records ZERO failing checks.

    Without this, every failure test above could pass vacuously — a check that
    always FAILs would satisfy them all.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "DATABASE_URL=postgresql://synthetic:synthetic@localhost:5999/synthetic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir()
    _install_plists(launchd_dir, *_LABELS)
    monkeypatch.setattr("brain.doctor_github.shutil.which", lambda _name: "/usr/local/bin/gh")
    monkeypatch.setattr(
        "brain.doctor_apple_notes.shutil.which", lambda _name: "/usr/bin/osascript"
    )

    with patch(
        "brain.doctor_runtime.subprocess.run",
        return_value=_FakeCompleted(_statuses(**dict.fromkeys(_LABELS, 0))),
    ):
        checks = runtime_doctor_checks(
            launchd_dir=launchd_dir, launchctl="launchctl", platform="darwin"
        )

    # R3 (phase 9): the dormant wiki's "wiki build" check is gone from the set.
    # RM-16 adds "brief delivery", off (ok) under tests/delivery_isolation_plugin.py.
    # RM-14b adds "reranker", not configured (ok) under tests/rerank_isolation_plugin.py.
    # RM-31 adds "github", ok here because ``gh`` is found (``shutil.which`` is pinned above).
    # RM-36 adds "apple notes", ok here because ``osascript`` is found (pinned above too).
    assert [c.check for c in checks] == [
        "config", "daemons", "vault watcher", "brief delivery", "reranker", "github",
        "apple notes",
    ]
    assert [c.status for c in checks] == ["ok", "ok", "ok", "ok", "ok", "ok", "ok"]


def test_every_failing_check_names_a_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red or yellow line the user cannot act on is only marginally better than silence.

    The leftover ``com.brain.build`` plist adds the retired-daemon WARN, which
    survives the wiki's deletion (R9-1: legacy cleanup stays).
    """
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "absent"))
    monkeypatch.chdir(tmp_path)

    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir()
    _install_plists(launchd_dir, *_LABELS, "com.brain.build")

    with patch(
        "brain.doctor_runtime.subprocess.run",
        return_value=_FakeCompleted(_statuses(**dict.fromkeys(_LABELS, 1))),
    ):
        checks = runtime_doctor_checks(
            launchd_dir=launchd_dir, launchctl="launchctl", platform="darwin"
        )

    flagged = {c.check: c.status for c in checks if c.status != "ok"}
    assert flagged == {"config": "fail", "daemons": "fail", "retired daemon": "warn"}, flagged
    for check in checks:
        if check.status != "ok":
            assert check.remedy, f"{check.check} {check.status} without naming a remedy"
