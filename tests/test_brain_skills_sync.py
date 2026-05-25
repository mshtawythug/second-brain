"""Tests for bin/brain-skills-sync (T1).

The script is a copy-based installer of the brain-family skills into
``~/.claude/skills``.  Every test here invokes it against a *temp* dest dir
(via ``--dest`` AND a sandboxed ``HOME``) so the developer's real
``~/.claude/skills`` is never touched.
"""
from __future__ import annotations

import filecmp
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "bin" / "brain-skills-sync"
SRC_SKILLS = REPO_ROOT / "skills"

# Minimal PATH providing bash built-ins + cp/rm/diff/mkdir/basename but nothing
# project-specific.  Mirrors the install.sh test sandbox.
SANDBOX_PATH = "/usr/bin:/bin"


def _expected_skills() -> set[str]:
    """Brain-family skill names enumerated straight from the repo skills/ dir."""
    return {p.name for p in SRC_SKILLS.iterdir() if p.is_dir()}


def _run(
    args: list[str],
    *,
    home: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the script with a sandboxed PATH + HOME and capture output."""
    env = {
        "PATH": SANDBOX_PATH,
        # Sandboxed HOME — belt-and-suspenders so the default
        # ``$HOME/.claude/skills`` path can never resolve to the real home.
        "HOME": str(home),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# 1. Script exists and is executable
# ---------------------------------------------------------------------------


def test_script_exists_and_executable() -> None:
    assert SCRIPT.exists(), f"brain-skills-sync not found at {SCRIPT}"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "brain-skills-sync must be +x"


# ---------------------------------------------------------------------------
# 2. --help is a single line and exits 0
# ---------------------------------------------------------------------------


def test_help_is_one_line(tmp_path: Path) -> None:
    result = _run(["--help"], home=tmp_path)
    assert result.returncode == 0, result.stderr
    non_empty = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(non_empty) == 1, f"--help must be one line, got:\n{result.stdout}"
    assert "brain-skills-sync" in result.stdout


# ---------------------------------------------------------------------------
# 3. Fresh install copies every brain-family skill, byte-identical to the repo
# ---------------------------------------------------------------------------


def test_fresh_install_copies_all_skills(tmp_path: Path) -> None:
    dest = tmp_path / "skills"

    result = _run(["--dest", str(dest)], home=tmp_path)

    assert result.returncode == 0, result.stderr
    expected = _expected_skills()
    assert expected, "precondition: repo must define at least one skill"
    installed = {p.name for p in dest.iterdir() if p.is_dir()}
    assert installed == expected, f"installed {installed} != repo {expected}"
    # Every line reports 'installed'; summary count matches.
    assert result.stdout.count(": installed") == len(expected)
    # Bytes match the repo source for each skill (recursive compare).
    for name in expected:
        cmp = filecmp.dircmp(SRC_SKILLS / name, dest / name)
        assert not cmp.left_only and not cmp.right_only and not cmp.diff_files, (
            f"{name} copy differs from repo: {cmp.left_only=} "
            f"{cmp.right_only=} {cmp.diff_files=}"
        )


# ---------------------------------------------------------------------------
# 4. Idempotency — a 2nd run reports every skill 'unchanged'
# ---------------------------------------------------------------------------


def test_idempotent_second_run_all_unchanged(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    first = _run(["--dest", str(dest)], home=tmp_path)
    assert first.returncode == 0, first.stderr

    second = _run(["--dest", str(dest)], home=tmp_path)

    assert second.returncode == 0, second.stderr
    n_skills = len(_expected_skills())
    assert second.stdout.count(": unchanged") == n_skills, second.stdout
    assert ": installed" not in second.stdout
    assert ": updated" not in second.stdout


# ---------------------------------------------------------------------------
# 5. Scope — only brain-family copied; a pre-existing unrelated dir is untouched
# ---------------------------------------------------------------------------


def test_scope_leaves_unrelated_dest_dirs_untouched(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    # Simulate a pre-existing non-brain global skill (e.g. open-design).
    unrelated = dest / "open-design-foo"
    unrelated.mkdir(parents=True)
    marker = unrelated / "marker.txt"
    marker.write_text("do not touch", encoding="utf-8")

    result = _run(["--dest", str(dest)], home=tmp_path)

    assert result.returncode == 0, result.stderr
    # Unrelated dir + its contents survive untouched.
    assert marker.read_text(encoding="utf-8") == "do not touch"
    # The unrelated skill is never mentioned in the output.
    assert "open-design-foo" not in result.stdout
    # Only brain-family + the pre-existing unrelated dir exist in dest.
    present = {p.name for p in dest.iterdir() if p.is_dir()}
    assert present == _expected_skills() | {"open-design-foo"}


# ---------------------------------------------------------------------------
# 6. Drift detection — --check exits non-zero when an installed skill differs
# ---------------------------------------------------------------------------


def test_check_detects_drift_in_installed_skill(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    assert _run(["--dest", str(dest)], home=tmp_path).returncode == 0

    # In-sync → exit 0.
    in_sync = _run(["--check", "--dest", str(dest)], home=tmp_path)
    assert in_sync.returncode == 0, in_sync.stdout + in_sync.stderr

    # Mutate one installed skill so it diverges from the repo.
    drifted = next(iter(_expected_skills()))
    (dest / drifted / "SKILL.md").write_text("DRIFTED CONTENT\n", encoding="utf-8")

    result = _run(["--check", "--dest", str(dest)], home=tmp_path)

    assert result.returncode != 0, "drift must make --check exit non-zero"
    assert "STALE" in result.stdout, result.stdout
    assert drifted in result.stdout
    # --check copies nothing — the drifted file is left as-is.
    assert (dest / drifted / "SKILL.md").read_text(encoding="utf-8") == "DRIFTED CONTENT\n"


# ---------------------------------------------------------------------------
# 7. --check on an empty dest reports drift (nothing installed yet), copies nothing
# ---------------------------------------------------------------------------


def test_check_on_empty_dest_is_drift_and_copies_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "skills"

    result = _run(["--check", "--dest", str(dest)], home=tmp_path)

    assert result.returncode != 0, "empty dest is drift → non-zero"
    assert "MISSING" in result.stdout, result.stdout
    # Copied nothing: dest must not have been created/populated.
    assert not dest.exists() or not any(dest.iterdir())


# ---------------------------------------------------------------------------
# 8. --dry-run is an alias of --check
# ---------------------------------------------------------------------------


def test_dry_run_aliases_check(tmp_path: Path) -> None:
    dest = tmp_path / "skills"

    result = _run(["--dry-run", "--dest", str(dest)], home=tmp_path)

    assert result.returncode != 0, "empty dest under --dry-run is drift"
    assert not dest.exists() or not any(dest.iterdir()), "--dry-run must copy nothing"


# ---------------------------------------------------------------------------
# 9. BRAIN_SKILLS_DEST env var is honored when no --dest flag is given
# ---------------------------------------------------------------------------


def test_env_var_dest_override(tmp_path: Path) -> None:
    dest = tmp_path / "skills"

    result = _run([], home=tmp_path, env_overrides={"BRAIN_SKILLS_DEST": str(dest)})

    assert result.returncode == 0, result.stderr
    installed = {p.name for p in dest.iterdir() if p.is_dir()}
    assert installed == _expected_skills()


# ---------------------------------------------------------------------------
# 10. Wholesale-replace — a stray file in an installed skill is removed on resync
# ---------------------------------------------------------------------------


def test_resync_removes_stray_files(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    assert _run(["--dest", str(dest)], home=tmp_path).returncode == 0

    skill = next(iter(_expected_skills()))
    stray = dest / skill / "STRAY.md"
    stray.write_text("orphan", encoding="utf-8")

    result = _run(["--dest", str(dest)], home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not stray.exists(), "stray file must be removed by wholesale replace"


# ---------------------------------------------------------------------------
# 11. Unknown argument is rejected with a non-zero exit
# ---------------------------------------------------------------------------


def test_unknown_arg_rejected(tmp_path: Path) -> None:
    result = _run(["--bogus"], home=tmp_path)
    assert result.returncode != 0
    assert "unknown argument" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# 12. Guard — the real ~/.claude/skills path never appears in any invocation.
#     (Defensive: HOME is always sandboxed to tmp_path.)
# ---------------------------------------------------------------------------


def test_never_targets_real_home(tmp_path: Path) -> None:
    real_home_skills = Path(os.path.expanduser("~/.claude/skills"))
    # Sanity: our sandboxed HOME is the tmp dir, not the developer's home.
    assert tmp_path != real_home_skills.parent.parent
    result = _run(["--check", "--dest", str(tmp_path / "skills")], home=tmp_path)
    # Output must reference the temp dest, never the real home path.
    assert str(real_home_skills) not in (result.stdout + result.stderr)
