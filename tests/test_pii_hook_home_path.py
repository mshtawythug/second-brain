"""The PII gate must block an absolute home path that names a real account.

Thirteen occurrences of the repo owner's real home path reached tracked files,
and neither stage of ``scripts/hooks/pre-commit`` could have stopped them:
stage 1 carried no home-path pattern at all, and stage 2's prompt enumerated
names, emails, phones, addresses, employers, codenames, credentials and
transcript bodies — machine layout was a CONCEPT it was never asked about. The
cleanup that followed removed the occurrences but added only a re-derive command
to ``.gitignore``, which documents the class without gating it.

These tests pin the gate that closes it. The one that matters most is
:func:`test_stage_one_blocks_a_home_path_with_an_unknown_account`, because a
gate never shown to *detect* anything is worth nothing — this repo has a dozen
confirmed guards that did exactly that.

**Why no real username appears here.** The check matches the SHAPE of a home
path and allows a small set of known-synthetic account segments; anything else
is blocked by default. That design is what lets this test — and the hook — prove
the capability without either file containing the value it exists to keep out.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "pre-commit"
ALLOWLIST_PATH = REPO_ROOT / ".pii-allowlist.txt"

# The account segments the repo deliberately uses as stand-ins. Kept in sync
# with the hook's _SYNTHETIC_HOME_RE by
# test_hook_allowlist_matches_the_tracked_placeholders below.
SYNTHETIC_SEGMENTS = ("you", "example", "user", "runner")

# A home path whose account segment is NOT allowlisted — an invented name, never
# the real owner's. Built by CONCATENATION so this source file contains no
# literal that matches the hook's own pattern: `/Users/` here is followed by a
# quote, and the pattern requires a path character there. Without this the test
# would need allowlisting in order to exist, which is the circularity it is here
# to disprove. Same technique as tests/test_secret_pattern_hook_parity.py.
_CANARY_HOME = "/Users/" + "dvorak"

# Stage 2 shells out to `claude -p`. Restricting PATH to the system directories
# keeps it out of reach, so these tests exercise stage 1 in isolation: the hook
# prints a "'claude' not found" warning and exits on the deterministic verdict.
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

needs_git_and_isolated_stage_one = pytest.mark.skipif(
    shutil.which("git", path=_MINIMAL_PATH) is None
    or shutil.which("claude", path=_MINIMAL_PATH) is not None,
    reason="needs git on the minimal PATH, and `claude` absent from it",
)


def _hook_text() -> str:
    return HOOK_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural — the pattern exists and is actually consulted.
# ---------------------------------------------------------------------------


def test_stage_one_defines_a_home_path_pattern() -> None:
    """The deterministic stage must carry a home-path pattern at all."""
    assert "_HOME_PATH_RE=" in _hook_text()


def test_stage_one_greps_with_the_home_path_pattern() -> None:
    """A pattern nothing reads would be a green test over a dead gate."""
    assert 'grep -oEi "${_HOME_PATH_RE}"' in _hook_text()


def test_stage_one_subtracts_the_allowlist_for_home_paths() -> None:
    """Exact-line fixed-string subtraction, same as the secret stage.

    A substring subtraction would let a short allowlist entry admit a longer
    real path that merely contains it.
    """
    text = _hook_text()
    assert "allow_paths=" in text
    assert 'grep -vixF -- "${allow_paths}"' in text


def test_semantic_prompt_names_machine_layout() -> None:
    """Stage 2 must ask about the category, not merely about names and emails.

    This is the half that was a concept absent rather than a string absent: the
    model cannot flag a class it was never told to look for.
    """
    text = _hook_text()
    prompt_start = text.index('prompt="')
    prompt = text[prompt_start : text.index('"\n', prompt_start)]
    assert "MACHINE" in prompt.upper()
    assert "/home/" in prompt


def test_hook_does_not_hardcode_any_real_account_name() -> None:
    """The gate must match a shape, never a value (rule 15).

    Committing the username you are trying to block IS the disclosure. The only
    account segments the hook may name are the synthetic stand-ins.
    """
    match = re.search(r"_SYNTHETIC_HOME_RE='([^']*)'", _hook_text())
    assert match is not None, "hook no longer defines _SYNTHETIC_HOME_RE"
    named = set(re.findall(r"[a-z]+", match.group(1).split("(")[-1]))
    assert named <= set(SYNTHETIC_SEGMENTS) | {"Users", "home"}


def test_hook_pattern_does_not_match_the_hook_itself() -> None:
    """Self-avoidance: the gate must not block the commit that ships it."""
    # finditer, not findall: the pattern has a group, so findall would return
    # just that group rather than the whole matched path.
    pattern = re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+")
    allowed = re.compile(rf"(?:/Users|/home)/(?:{'|'.join(SYNTHETIC_SEGMENTS)})")
    offenders = [
        m.group(0)
        for m in pattern.finditer(_hook_text())
        if not allowed.fullmatch(m.group(0))
    ]
    assert offenders == [], f"hook contains non-synthetic home paths: {offenders}"


def test_tracked_tree_carries_only_synthetic_home_paths() -> None:
    """The class stays closed: no tracked file may name a real account.

    Guards the cleanup as well as the gate — the 13 occurrences are gone, and
    this fails if any come back, whatever route they take in.
    """
    out = subprocess.run(
        ["git", "grep", "-hoIE", r"(/Users|/home)/[A-Za-z0-9._-]+"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout
    allowed = {f"/{root}/{seg}" for root in ("Users", "home") for seg in SYNTHETIC_SEGMENTS}
    offenders = sorted({line for line in out.split() if line not in allowed})
    assert offenders == [], f"non-synthetic home paths are tracked: {offenders}"


# ---------------------------------------------------------------------------
# Behavioural — the hook actually RUN against a throwaway repo outside this one.
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_repo(tmp_path: Path) -> Path:
    """A throwaway git repo wired to the REAL hook and the REAL allowlist."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    # copy2 preserves the executable bit, which git requires.
    shutil.copy2(HOOK_PATH, repo / "scripts" / "hooks" / "pre-commit")
    shutil.copy2(ALLOWLIST_PATH, repo / ".pii-allowlist.txt")
    for args in (
        ["git", "init", "-q", "."],
        ["git", "config", "user.email", "tester@example.com"],
        ["git", "config", "user.name", "Tester"],
        ["git", "config", "core.hooksPath", "scripts/hooks"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    return repo


def _stage_and_run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    """Stage everything, then run the hook exactly as ``git commit`` would.

    Output is captured rather than written into ``repo``: a failure message
    quotes the offending path, so a log file left inside the fixture would be
    staged by the next run and re-trip the gate on its own report.
    """
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        [str(repo / "scripts" / "hooks" / "pre-commit")],
        cwd=repo,
        env={"PATH": _MINIMAL_PATH, "HOME": str(repo)},
        capture_output=True,
        text=True,
    )


@needs_git_and_isolated_stage_one
def test_stage_one_blocks_a_home_path_with_an_unknown_account(
    hook_repo: Path,
) -> None:
    """The assertion the whole file exists for: it must DETECT."""
    # --- setup
    (hook_repo / "canary.py").write_text(
        f'CACHE_DIR = "{_CANARY_HOME}/workspace/second-brain/data"\n',
        encoding="utf-8",
    )

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 1, f"gate did not fire: {result.stderr!r}"
    assert "absolute home path" in result.stderr
    # Only the account segment is reported — deeper structure is not sensitive
    # on its own, and quoting it would put more of the machine in the log.
    assert _CANARY_HOME in result.stderr
    assert "workspace/second-brain/data" not in result.stderr


@needs_git_and_isolated_stage_one
def test_stage_one_allows_the_synthetic_placeholders(hook_repo: Path) -> None:
    """It must NOT fire on the stand-ins the repo deliberately uses.

    A gate that blocks the placeholders teaches people to reach for
    ``--no-verify``, which disables every stage at once.
    """
    # --- setup
    (hook_repo / "synthetic.py").write_text(
        "\n".join(
            [
                'VAULT = "/Users/you/workspace/second-brain/vault"',
                'DEMO = "/Users/example/notes"',
                'LINUX = "/home/user/.config/brain"',
                'CI = "/home/runner/work/second-brain"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 0, f"false positive: {result.stderr!r}"
    # Confirms stage 2 really was out of reach, so this asserts stage 1's verdict.
    assert "'claude' not found" in result.stderr
