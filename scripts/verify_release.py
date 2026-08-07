#!/usr/bin/env python3
"""Two-tier acceptance harness for the user's 5-point release checklist.

WHY TWO TIERS
-------------
Run verbatim, all five of the user's checks measure the WRONG BINARY. ``brain``
on PATH is the uv-tool install, which lags the repo. w-failloud proved the trap
on check 5:

    repo code      -> "wiki build aborted", exit 3, NOTHING PUBLISHED
    installed copy -> WARNING "... skipping refresh", exit 0, PUBLISHED ANYWAY

So check 5 as written prints a green-looking "build succeeded" that IS the bug a
user is trying to detect. Tier A and Tier B exist so that result can never be
misread:

    TIER A  -- runs against REPO code.  A pass says THE CODE IS RIGHT.
    TIER B  -- runs against the LIVE install. A pass says THE USER'S MACHINE
               IS FIXED. Only meaningful after a redeploy (task #38).

Every check prints the resolved package path (``os.path.dirname(brain.__file__)``)
in its own output, so a result is self-evidencing rather than trusting that the
invocation targeted what you think it did.

SAFETY (these are not incidental -- each one is a bug this harness would
otherwise reproduce):
  * Check 2 uses a TEMP $BRAIN_HOME and never touches the real ~/.brain/.
  * Check 5 runs against a COPY of src/ outside the tree. ``_project_dotenv()``
    resolves relative to the module, so an IN-TREE run always finds the repo
    .env and the no-config case becomes UNREACHABLE -- a vacuous check that
    cannot fail. The copy is nested so the project-dotenv leg lands inside the
    sandbox, and the check asserts the whole dotenv chain is empty before
    trusting its own result.
  * Check 4 uses a scratch vault, never ~/brain-vault, and does not trigger a
    real cold build (C16: emit phase >5min vs 17-22s).
  * Check 3 restarts launchd agents BY LABEL only, never a pattern kill.
  * Nothing here writes to production Postgres (55432).

USAGE
    python scripts/verify_release.py --tier A
    python scripts/verify_release.py --tier B          # only after a redeploy
    python scripts/verify_release.py --tier A --checks 1,5
    python scripts/verify_release.py --tier B --checks 3 --allow-restart
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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
LAUNCHD_LABELS = ("com.brain.watcher", "com.brain.build", "com.brain.brief")
MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

PASS, FAIL, SKIP, BLOCKED = "PASS", "FAIL", "SKIP", "BLOCKED"


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
            "from brain.bin.launchd import render_plist, _LABELS\n"
            "import xml.etree.ElementTree as ET\n"
            "missing = []\n"
            "for label in _LABELS:\n"
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
# Check 4 -- a forced build emits a new dir containing a newly ingested doc
# ---------------------------------------------------------------------------


def check_4(tier: str, *, include_build: bool) -> Result:
    """Scratch-vault build. Default SKIP: a real build hits C16 on this machine."""
    if not include_build:
        return Result(
            4,
            "forced build emits new dir with new doc",
            SKIP,
            "skipped by default (pass --include-build to run). A real build "
            "currently hits C16 -- emit phase >5min vs 17-22s, four live timeouts "
            "-- and this machine crashed Postgres under load today. Requires a "
            "scratch vault with its own Quartz workspace; never ~/brain-vault.",
            _pkg_path(_interpreter(tier)),
            ["not executed"],
        )
    return Result(
        4,
        "forced build emits new dir with new doc",
        BLOCKED,
        "BLOCKED-ON-C16: a scratch-vault build needs its own Quartz workspace "
        "(npm install) and the emit-phase defect makes the result "
        "uninterpretable until C16 lands.",
        _pkg_path(_interpreter(tier)),
        [],
    )


# ---------------------------------------------------------------------------
# Check 5 -- no config must FAIL LOUDLY, never publish stale
# ---------------------------------------------------------------------------


def check_5(tier: str) -> Result:
    """With no reachable config, the build must abort rather than publish.

    Runs against a COPY of the package outside the tree. In-tree this check is
    VACUOUS: ``_project_dotenv()`` is resolved from the module's own location,
    so an in-tree run always finds the repo .env and the no-config state is
    unreachable. The copy is nested one level (``pkgroot/brain``) so the
    project-dotenv leg lands inside the sandbox, and the probe asserts the whole
    dotenv chain is empty before trusting the outcome.
    """
    if tier == "A":
        source = REPO_ROOT / "src" / "brain"
    else:
        probe = _pkg_path(INSTALLED_PY)
        if not probe.startswith("/"):
            return Result(
                5,
                "no config fails loudly",
                FAIL,
                f"cannot resolve installed package: {probe}",
            )
        source = Path(probe)

    with tempfile.TemporaryDirectory() as sandbox:
        root = Path(sandbox)
        pkgroot = root / "pkgroot"
        pkgroot.mkdir()
        shutil.copytree(source, pkgroot / "brain")
        fake_home = root / "home"
        (fake_home / "logs").mkdir(parents=True)
        vault = root / "vault"
        vault.mkdir()

        code = (
            _PKG_PROBE + "\n"
            "from pathlib import Path\n"
            "from brain.config import _brain_home_dotenv, _project_dotenv\n"
            "from dotenv import find_dotenv\n"
            "reach = []\n"
            "if _brain_home_dotenv().exists(): reach.append(str(_brain_home_dotenv()))\n"
            "if _project_dotenv().exists(): reach.append(str(_project_dotenv()))\n"
            "c = find_dotenv(usecwd=True)\n"
            "if c: reach.append(c)\n"
            "print('REACHABLE_DOTENV=' + (','.join(reach) if reach else '(none)'))\n"
            # Do NOT import BrainWikiConfigError up front: on an install that
            # predates the fix the class does not exist, and a hard import would
            # abort with an ImportError before measuring the behaviour we care
            # about. Catch broadly and classify by class NAME instead, so the
            # same probe works against old and new code alike.
            "import brain.wiki.errors as _err\n"
            "print('HAS_CONFIG_ERROR_CLASS=%s' % hasattr(_err, 'BrainWikiConfigError'))\n"
            "from brain.wiki.build_swap import _refresh_pre_build_adornments\n"
            "try:\n"
            f"    _v = Path({str(vault)!r})\n"
            "    _refresh_pre_build_adornments(_v, refresh_related_inline=False)\n"
            "except Exception as exc:\n"
            "    print('OUTCOME=RAISED')\n"
            "    print('EXC_TYPE=' + type(exc).__name__)\n"
            "    print('MESSAGE=' + str(exc).splitlines()[0])\n"
            "else:\n"
            "    print('OUTCOME=PROCEEDED_WOULD_PUBLISH_STALE')\n"
        )
        proc = _run(
            _interpreter(tier),
            code,
            env={
                "HOME": str(fake_home),
                "PATH": MINIMAL_PATH,
                "PYTHONPATH": str(pkgroot),
                "BRAIN_HOME": str(fake_home),
            },
            cwd=root,
        )

    lines = [ln for ln in proc.stdout.splitlines() if "=" in ln]
    pkg = next((ln[4:] for ln in lines if ln.startswith("PKG=")), "(unresolved)")
    reachable = next((ln for ln in lines if ln.startswith("REACHABLE_DOTENV=")), "")

    if reachable != "REACHABLE_DOTENV=(none)":
        return Result(
            5,
            "no config fails loudly",
            FAIL,
            f"CHECK IS VACUOUS -- config was still reachable ({reachable}). "
            "The sandbox leaked a .env; the result proves nothing.",
            pkg,
            lines + [proc.stderr.strip()[:200]],
        )
    has_class = "HAS_CONFIG_ERROR_CLASS=True" in proc.stdout
    exc_type = next(
        (ln.split("=", 1)[1] for ln in lines if ln.startswith("EXC_TYPE=")), ""
    )
    if exc_type == "BrainWikiConfigError":
        return Result(
            5,
            "no config fails loudly",
            PASS,
            "with no reachable config the build ABORTED instead of publishing stale",
            pkg,
            lines,
        )
    if "OUTCOME=PROCEEDED_WOULD_PUBLISH_STALE" in proc.stdout:
        stale = (
            "build PROCEEDED with no config -- this is the user's original incident: "
            "a green-looking success that publishes a stale wiki."
        )
        if not has_class:
            stale += (
                " This copy PREDATES the fix (brain.wiki.errors has no "
                "BrainWikiConfigError) -- it needs the redeploy in task #38."
            )
        return Result(5, "no config fails loudly", FAIL, stale, pkg, lines)
    if exc_type:
        return Result(
            5,
            "no config fails loudly",
            FAIL,
            f"aborted, but with an unexpected {exc_type} rather than "
            f"BrainWikiConfigError -- the abort may be incidental, not the guard",
            pkg,
            lines,
        )
    return Result(
        5,
        "no config fails loudly",
        FAIL,
        f"indeterminate: {_last_error_line(proc)}",
        pkg,
        lines,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_release.py",
        description="Two-tier acceptance harness for the 5-point release checklist.",
    )
    parser.add_argument("--tier", choices=["A", "B"], required=True)
    parser.add_argument("--checks", default="1,2,3,4,5")
    parser.add_argument(
        "--include-build", action="store_true", help="run check 4's scratch build"
    )
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
    if 4 in wanted:
        results.append(check_4(args.tier, include_build=args.include_build))
    if 5 in wanted:
        results.append(check_5(args.tier))

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
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP, BLOCKED)}
    print(f"tier {args.tier}: " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    if args.tier == "A":
        print("NOTE: tier A says nothing about the user's machine. Run tier B after #38.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
