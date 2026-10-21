#!/usr/bin/env python3
"""Two-tier acceptance harness for the user's release checklist (checks 1-3).

Checks 4 (a forced Quartz build) and 5 (the Quartz build must fail loudly with no
config) were deleted with the Quartz wiki at R3 (wiki-to-ui phase 9): the code
they measured no longer exists. Checks 1-3 keep their numbers.

WHY TWO TIERS
-------------
Run verbatim, the user's checks measure the WRONG BINARY. ``brain`` on PATH is
the uv-tool install, which lags the repo. w-failloud proved the trap on the
since-deleted check 5: the repo code aborted with nothing published while the
installed copy warned, exited 0 and published anyway -- a green-looking result
that WAS the bug. Tier A and Tier B exist so a result can never be misread:

    TIER A  -- runs against REPO code.  A pass says THE CODE IS RIGHT.
    TIER B  -- runs against the LIVE install. A pass says THE USER'S MACHINE
               IS FIXED. Only meaningful after a redeploy (task #38).

Every check prints the resolved package path (``os.path.dirname(brain.__file__)``)
in its own output, so a result is self-evidencing rather than trusting that the
invocation targeted what you think it did.

SAFETY (these are not incidental -- each one is a bug this harness would
otherwise reproduce):
  * Check 2 uses a TEMP $BRAIN_HOME and never touches the real ~/.brain/.
  * Check 3 restarts launchd agents BY LABEL only, never a pattern kill.
  * Nothing here writes to production Postgres (55432).

USAGE
    python scripts/verify_release.py --tier A
    python scripts/verify_release.py --tier B          # only after a redeploy
    python scripts/verify_release.py --tier A --checks 1,2
    python scripts/verify_release.py --tier B --checks 3 --allow-restart
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_PY = REPO_ROOT / ".venv" / "bin" / "python"
INSTALLED_PY = (
    Path.home() / ".local" / "share" / "uv" / "tools" / "secondbrain-py" / "bin" / "python"
)
# The agents brain-install-launchd installs: brain.bin.launchd._INSTALLED_LABELS.
# com.brain.build is retired at R2 (phase 8) and has nothing left to restart.
LAUNCHD_LABELS = ("com.brain.watcher", "com.brain.brief")
MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    """Outcome of a single check."""

    number: int
    name: str
    status: str
    detail: str
    pkg_path: str = "(not resolved)"
    evidence: list[str] = field(default_factory=list)


def _interpreter(tier: str) -> Path:
    return REPO_PY if tier == "A" else INSTALLED_PY


def _run(
    interpreter: Path,
    code: str,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run *code* under *interpreter* in a controlled environment.

    Always ``env -i``-style: the caller supplies the complete environment, so a
    check can never accidentally pass because of a variable exported in the
    developer's shell. That is the whole point of check 1.
    """
    base = {"HOME": os.environ["HOME"], "PATH": MINIMAL_PATH}
    return subprocess.run(
        [str(interpreter), "-c", code],
        env={**base, **(env or {})},
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# Emitted by every check so the result carries proof of what it measured.
_PKG_PROBE = (
    "import brain, os; print('PKG=' + os.path.dirname(brain.__file__))"
)


def _last_error_line(proc: subprocess.CompletedProcess[str]) -> str:
    """Last stderr line, or an exit-code fallback — compact failure detail."""
    err = proc.stderr.strip()
    return err.splitlines()[-1] if err else f"exit {proc.returncode}"


def _pkg_path(interpreter: Path, env: dict[str, str] | None = None) -> str:
    proc = _run(interpreter, _PKG_PROBE, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("PKG="):
            return line[4:]
    return f"(unresolved: {proc.stderr.strip()[:120]})"


# ---------------------------------------------------------------------------
# Check 1 -- `cd /tmp && brain status` with no brain env exported
# ---------------------------------------------------------------------------


def check_1(tier: str) -> Result:
    """`brain status` must work from an unrelated cwd with no BRAIN_* exported."""
    interpreter = _interpreter(tier)
    code = (
        _PKG_PROBE + "\n"
        "from brain.config import Config, _brain_home_root, _brain_home_dotenv, _project_dotenv\n"
        "from dotenv import find_dotenv\n"
        "print('BRAIN_HOME_RESOLVED=' + str(_brain_home_root()))\n"
        "bh = _brain_home_dotenv()\n"
        "print('LEG_brain_home=%s exists=%s' % (bh, bh.exists()))\n"
        "cwd_leg = find_dotenv(usecwd=True)\n"
        "print('LEG_cwd=%s' % (cwd_leg or '(none)'))\n"
        "print('LEG_project=%s exists=%s' % (_project_dotenv(), _project_dotenv().exists()))\n"
        "cfg = Config.load()\n"
        "print('CONFIG_LOAD=OK db_set=%s' % bool(cfg.database_url))\n"
    )
    with tempfile.TemporaryDirectory() as scratch:
        proc = _run(interpreter, code, cwd=Path(scratch))

    lines = [ln for ln in proc.stdout.splitlines() if "=" in ln]
    pkg = next((ln[4:] for ln in lines if ln.startswith("PKG=")), "(unresolved)")
    if proc.returncode != 0:
        return Result(
            1,
            "brain status with no ambient env",
            FAIL,
            f"Config.load() failed with a minimal environment: {_last_error_line(proc)}",
            pkg,
            lines,
        )
    # Which dotenv leg satisfied it matters: a pass via the REPO .env does not
    # prove the $BRAIN_HOME leg (the one that broke) works.
    satisfied_by_brain_home = any(
        ln.startswith("LEG_brain_home=") and ln.endswith("exists=True") for ln in lines
    )
    detail = "Config.load() succeeded with HOME+PATH only"
    if not satisfied_by_brain_home:
        detail += " -- NOTE: $BRAIN_HOME/.env absent, so a lower-priority leg satisfied it"
    return Result(1, "brain status with no ambient env", PASS, detail, pkg, lines)


# ---------------------------------------------------------------------------
# Check 2 -- fresh install into a temp $BRAIN_HOME (owned by C1)
# ---------------------------------------------------------------------------


def check_2(tier: str) -> Result:
    """`brain setup` must provision a working config under a fresh $BRAIN_HOME.

    Exercises C1's ``provision_brain_home_dotenv`` against a TEMP $BRAIN_HOME.
    Never touches the real ~/.brain/, which currently holds a hand-made symlink
    to the repo .env that must survive; the check asserts that afterwards.
    """
    interpreter = _interpreter(tier)
    real = Path.home() / ".brain" / ".env"
    before = (real.is_symlink(), real.exists())

    with tempfile.TemporaryDirectory() as home:
        code = (
            _PKG_PROBE + "\n"
            "import os\n"
            "from pathlib import Path\n"
            "from brain.config import _brain_home_root, Config\n"
            "from brain.setup import provision_brain_home_dotenv\n"
            "home = Path(os.environ['BRAIN_HOME'])\n"
            "print('BRAIN_HOME_RESOLVED=' + str(_brain_home_root()))\n"
            "res = provision_brain_home_dotenv(home)\n"
            "print('PROVISION_ACTION=' + str(res.action))\n"
            "print('PROVISIONED_PATH=' + str(res.path))\n"
            "print('PROVISIONED_EXISTS=%s' % res.path.exists())\n"
            "try:\n"
            "    cfg = Config.load()\n"
            "    print('CONFIG_LOAD=OK db_set=%s' % bool(cfg.database_url))\n"
            "except Exception as exc:\n"
            "    _m = type(exc).__name__ + ': ' + str(exc).splitlines()[0]\n"
            "    print('CONFIG_LOAD=FAILED ' + _m)\n"
        )
        proc = _run(interpreter, code, env={"BRAIN_HOME": home}, cwd=Path(home))

    after = (real.is_symlink(), real.exists())
    lines = [ln for ln in proc.stdout.splitlines() if "=" in ln]
    pkg = next((ln[4:] for ln in lines if ln.startswith("PKG=")), "(unresolved)")
    evidence = lines + [f"real ~/.brain/.env before={before} after={after}"]

    if before != after:
        return Result(
            2,
            "fresh install provisions working config",
            FAIL,
            "SAFETY VIOLATION: the real ~/.brain/.env changed during a temp-BRAIN_HOME run",
            pkg,
            evidence,
        )
    if proc.returncode != 0:
        return Result(
            2,
            "fresh install provisions working config",
            FAIL,
            f"provisioning raised: {_last_error_line(proc)}",
            pkg,
            evidence,
        )
    provisioned = any(ln == "PROVISIONED_EXISTS=True" for ln in lines)
    loaded = any(ln.startswith("CONFIG_LOAD=OK") for ln in lines)
    if provisioned and loaded:
        return Result(
            2,
            "fresh install provisions working config",
            PASS,
            "setup provisioned $BRAIN_HOME/.env in a fresh home and Config.load() "
            "then succeeded; real ~/.brain untouched",
            pkg,
            evidence,
        )
    return Result(
        2,
        "fresh install provisions working config",
        FAIL,
        f"provisioned={provisioned} config_loaded={loaded} — see evidence",
        pkg,
        evidence,
    )


# ---------------------------------------------------------------------------
# Check 3 -- launchd agents restart clean
# ---------------------------------------------------------------------------


def check_3(tier: str, *, allow_restart: bool) -> Result:
    """Tier A: the GENERATED plists are correct. Tier B: the LIVE agents are clean."""
    if tier == "A":
        code = (
            _PKG_PROBE + "\n"
            "from pathlib import Path\n"
            "from brain.bin.launchd import render_plist, _INSTALLED_LABELS\n"
            "import xml.etree.ElementTree as ET\n"
            "missing = []\n"
            "for label in _INSTALLED_LABELS:\n"
            "    text = render_plist(label, brain_home=Path('/synthetic/home'),\n"
            "        vault_path=Path('/synthetic/vault'), pipx_bin_dir=Path('/synthetic/bin'),\n"
            "        brain_py=Path('/synthetic/python'))\n"
            "    if '<key>BRAIN_HOME</key>' not in text:\n"
            "        missing.append(label + ':BRAIN_HOME')\n"
            "    if '<key>WorkingDirectory</key>' not in text:\n"
            "        missing.append(label + ':WorkingDirectory')\n"
            "    for secret in ('DATABASE_URL', 'VOYAGE_API_KEY'):\n"
            "        if secret in text:\n"
            "            missing.append(label + ':LEAKED_' + secret)\n"
            "print('PLIST_DEFECTS=' + (','.join(missing) if missing else '(none)'))\n"
        )
        proc = _run(_interpreter(tier), code)
        lines = [ln for ln in proc.stdout.splitlines() if "=" in ln]
        pkg = next((ln[4:] for ln in lines if ln.startswith("PKG=")), "(unresolved)")
        ok = proc.returncode == 0 and "PLIST_DEFECTS=(none)" in proc.stdout
        return Result(
            3,
            "generated plists pin env + cwd, leak no secrets",
            PASS if ok else FAIL,
            "every plist exports BRAIN_HOME + WorkingDirectory and bakes no secrets"
            if ok
            else f"defects: {proc.stdout.strip()} {proc.stderr.strip()[:200]}",
            pkg,
            lines,
        )

    # Tier B. The user's criterion is "zero Config.load failed lines AFTER the
    # restart". Scanning whole files would also count HISTORICAL failures from
    # before the fix, so the check could never go green without deleting logs --
    # a check that cannot pass is as useless as one that cannot fail. Instead we
    # record each log's size first and scan only the bytes written afterwards.
    log_dir = Path.home() / ".brain" / "logs"
    logs = sorted(log_dir.glob("com.brain.*.err.log"))

    def _listing() -> list[str]:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, check=False
        )
        return [ln for ln in out.stdout.splitlines() if "com.brain." in ln]

    if not allow_restart:
        historical = []
        for log in logs:
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = text.count("Config.load failed") + text.count(
                "DATABASE_URL is not set"
            )
            historical.append(f"{log.name}: {hits} historical config-failure line(s)")
        return Result(
            3,
            "live launchd agents restart clean",
            SKIP,
            "read-only. 'zero failures AFTER a restart' cannot be evaluated "
            "without restarting; pass --allow-restart for a verdict. Historical "
            "counts below include pre-fix lines and are context, not a result.",
            "(live launchd)",
            _listing() + historical,
        )

    offsets = {}
    for log in logs:
        try:
            offsets[log] = log.stat().st_size
        except OSError:
            offsets[log] = 0

    uid = os.getuid()
    restarted: list[str] = []
    for label in LAUNCHD_LABELS:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        restarted.append(
            f"{label}: kickstart rc={proc.returncode} {proc.stderr.strip()[:60]}"
        )

    offenders: list[str] = []
    for log, offset in offsets.items():
        try:
            with log.open("rb") as handle:
                handle.seek(offset)
                fresh = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        hits = fresh.count("Config.load failed") + fresh.count(
            "DATABASE_URL is not set"
        )
        if hits:
            offenders.append(f"{log.name}: {hits} NEW config-failure line(s)")

    rows = _listing()
    bad_exit = [r for r in rows if r.split("\t")[1] not in ("0", "-")]
    ok = not offenders and not bad_exit
    return Result(
        3,
        "live launchd agents restart clean",
        PASS if ok else FAIL,
        "all agents exit 0 and no config-failure lines since the restart"
        if ok
        else f"nonzero={bad_exit or 'none'}; new failures={offenders or 'none'}",
        "(live launchd)",
        rows + restarted + offenders,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_release.py",
        description="Two-tier acceptance harness for the release checklist (checks 1-3).",
    )
    parser.add_argument("--tier", choices=["A", "B"], required=True)
    parser.add_argument("--checks", default="1,2,3")
    parser.add_argument(
        "--allow-restart",
        action="store_true",
        help="tier B check 3: actually restart the launchd agents (by label)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    wanted = {int(c) for c in args.checks.split(",") if c.strip()}
    interpreter = _interpreter(args.tier)
    if not interpreter.exists():
        print(f"interpreter not found for tier {args.tier}: {interpreter}", file=sys.stderr)
        return 2

    tier_label = (
        "TIER A -- REPO CODE (a pass says THE CODE IS RIGHT, not the user's machine)"
        if args.tier == "A"
        else "TIER B -- LIVE INSTALL (a pass says THE USER'S MACHINE IS FIXED)"
    )
    print("=" * 78)
    print(tier_label)
    print(f"interpreter: {interpreter}")
    print("=" * 78)

    results: list[Result] = []
    if 1 in wanted:
        results.append(check_1(args.tier))
    if 2 in wanted:
        results.append(check_2(args.tier))
    if 3 in wanted:
        results.append(check_3(args.tier, allow_restart=args.allow_restart))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        for r in results:
            print(f"\n[{r.status:7}] check {r.number}: {r.name}")
            print(f"          measured: {r.pkg_path}")
            print(f"          {r.detail}")
            for line in r.evidence:
                print(f"            . {line}")

    failed = [r for r in results if r.status == FAIL]
    print("\n" + "-" * 78)
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP)}
    print(f"tier {args.tier}: " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    if args.tier == "A":
        print("NOTE: tier A says nothing about the user's machine. Run tier B after #38.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
