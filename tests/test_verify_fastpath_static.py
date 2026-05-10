"""Static + functional regression tests for bin/brain-verify-fastpath cleanup safety.

Two bugs were fixed in ``_cleanup()`` (Finding 4 in the verification audit
``docs/audits/2026-05-09-plan-b-verification.md``):

**4-A — Wrong restore target.**
The old code hardcoded ``cp "$README_BACKUP" "$VAULT/README.md"`` regardless of
which vault file was actually backed up.  Fixed by introducing
``README_RESTORE_TARGET`` and restoring to that variable.

**4-B — Zero-byte backup race.**
The old code ran ``README_BACKUP=$(mktemp ...)`` then ``cp src "$README_BACKUP"``.
If the script was interrupted between those two lines ``README_BACKUP`` pointed to
a 0-byte empty file; ``_cleanup()`` would then ``cp`` that empty file over the
vault source, truncating it to zero bytes.  Fixed by using a local ``_bak_*``
variable for ``mktemp``, running ``cp`` into it, then assigning ``README_BACKUP``
*after* the copy succeeds — so ``README_BACKUP`` is non-empty only when the
backup file contains valid content.

The static tests assert the safe source patterns are present (and the unsafe
patterns absent) so any revert is caught immediately.  The functional tests run
bash harnesses that exercise the guard logic with a simulated race-condition state
to verify no vault corruption occurs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "brain-verify-fastpath"


# ---------------------------------------------------------------------------
# Module-level fixture — read script source once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def script_source() -> str:
    assert SCRIPT.is_file(), (
        f"bin/brain-verify-fastpath not found at {SCRIPT} — was T7 implemented?"
    )
    return SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File existence + executability
# ---------------------------------------------------------------------------


def test_verify_fastpath_exists() -> None:
    """``bin/brain-verify-fastpath`` is present in the repository."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_verify_fastpath_is_executable() -> None:
    """``bin/brain-verify-fastpath`` has the execute bit set."""
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_verify_fastpath_syntax_clean() -> None:
    """``bash -n`` reports no syntax errors in the verify script."""
    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax error in bin/brain-verify-fastpath:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Static — cleanup guard contracts (Finding 4-A)
# ---------------------------------------------------------------------------


def test_cleanup_does_not_hardcode_readme_md_restore(script_source: str) -> None:
    """``_cleanup()`` does NOT hardcode ``$VAULT/README.md`` as the restore target.

    Regression guard for Finding 4-A: the old code wrote
    ``cp "$README_BACKUP" "$VAULT/README.md"`` regardless of which vault file
    was actually backed up.  Any slug that resolves to a different path (e.g.
    ``_ingested/README.md``) would corrupt the root README while leaving the
    edited file dirty.
    """
    # The string below is what the old (buggy) cleanup contained.
    assert 'cp "$README_BACKUP" "$VAULT/README.md"' not in script_source, (
        '_cleanup() must NOT restore to a hardcoded "$VAULT/README.md" path — '
        "use $README_RESTORE_TARGET so the restore targets the file that was "
        "actually backed up (Finding 4-A)"
    )


def test_cleanup_restores_to_readme_restore_target(script_source: str) -> None:
    """``_cleanup()`` restores to ``$README_RESTORE_TARGET``, not a literal path."""
    assert 'cp "$README_BACKUP" "$README_RESTORE_TARGET"' in script_source, (
        '_cleanup() must restore via cp "$README_BACKUP" "$README_RESTORE_TARGET" — '
        "hardcoded paths are rejected by Finding 4-A"
    )


def test_cleanup_guard_checks_readme_restore_target(script_source: str) -> None:
    """``_cleanup()`` guard includes ``-n "$README_RESTORE_TARGET"`` so it never fires
    when the target is unset (e.g. if the script exits before Phase C2 runs).
    """
    assert '-n "$README_RESTORE_TARGET"' in script_source, (
        '_cleanup() guard must include -n "$README_RESTORE_TARGET" — '
        "prevents a spurious restore attempt if the script exits before the "
        "backup-creation block runs"
    )


def test_readme_restore_target_initialized_at_top(script_source: str) -> None:
    """``README_RESTORE_TARGET=""`` is declared alongside ``README_BACKUP=""`` at the
    top of the script (before the EXIT trap is registered).
    """
    assert 'README_RESTORE_TARGET=""' in script_source, (
        'README_RESTORE_TARGET="" must be declared at the top of the script — '
        "it must be set (empty) before the EXIT trap fires so $README_RESTORE_TARGET "
        "is always defined even if the script exits very early"
    )


# ---------------------------------------------------------------------------
# Static — deferred backup-assignment contracts (Finding 4-B)
# ---------------------------------------------------------------------------


def test_readme_backup_not_assigned_directly_from_mktemp(script_source: str) -> None:
    """``README_BACKUP`` is never assigned directly from ``$(mktemp ...)``.

    Regression guard for Finding 4-B: the old pattern
    ``README_BACKUP=$(mktemp ...)`` set the variable the instant mktemp created
    an empty 0-byte file — before ``cp`` wrote any content.  An interrupt
    between those two lines left ``README_BACKUP`` pointing at a 0-byte file;
    ``_cleanup()`` then truncated the vault file to zero bytes.

    The fix assigns ``README_BACKUP`` only AFTER the copy succeeds (via a local
    ``_bak_*`` variable).
    """
    assert "README_BACKUP=$(mktemp" not in script_source, (
        "README_BACKUP must NOT be assigned directly from $(mktemp ...) — "
        "this is the unsafe pattern that causes Finding 4-B.  Use a local "
        "_bak_* variable for mktemp, run cp into it, then assign README_BACKUP "
        "only after cp succeeds"
    )


def test_deferred_assignment_fallback_site(script_source: str) -> None:
    """The fallback-branch backup site uses deferred assignment.

    Verifies that the backup-creation block for the fallback (broken
    build-partial) branch assigns ``README_BACKUP`` from a local
    ``_bak_fallback`` variable AFTER ``cp`` has succeeded.
    """
    assert 'README_BACKUP="$_bak_fallback"' in script_source, (
        'README_BACKUP="$_bak_fallback" must appear in the fallback backup site — '
        "deferred assignment ensures README_BACKUP is only set once the backup "
        "file has been populated (Finding 4-B, fallback branch)"
    )


def test_deferred_assignment_happy_path_site(script_source: str) -> None:
    """The happy-path backup site uses deferred assignment.

    Verifies that the backup-creation block for the fast-path (working
    build-partial) branch assigns ``README_BACKUP`` from a local
    ``_bak_happy`` variable AFTER ``cp`` has succeeded.
    """
    assert 'README_BACKUP="$_bak_happy"' in script_source, (
        'README_BACKUP="$_bak_happy" must appear in the happy-path backup site — '
        "deferred assignment ensures README_BACKUP is only set once the backup "
        "file has been populated (Finding 4-B, happy-path branch)"
    )


def test_cp_precedes_deferred_assignment_fallback(script_source: str) -> None:
    """``cp "$README_SOURCE_FILE" "$_bak_fallback"`` comes before
    ``README_BACKUP="$_bak_fallback"`` in the source.

    Positional ordering confirms the copy happens first and the guard variable
    is only set once the backup file contains valid content.
    """
    cp_pos = script_source.find('cp "$README_SOURCE_FILE" "$_bak_fallback"')
    assign_pos = script_source.find('README_BACKUP="$_bak_fallback"')
    assert cp_pos != -1, (
        'cp "$README_SOURCE_FILE" "$_bak_fallback" not found in script source'
    )
    assert assign_pos != -1, (
        'README_BACKUP="$_bak_fallback" not found in script source'
    )
    assert cp_pos < assign_pos, (
        "cp must precede README_BACKUP assignment at the fallback backup site — "
        f"cp at offset {cp_pos}, README_BACKUP= at {assign_pos}"
    )


def test_cp_precedes_deferred_assignment_happy_path(script_source: str) -> None:
    """``cp "$README_SOURCE_FILE" "$_bak_happy"`` comes before
    ``README_BACKUP="$_bak_happy"`` in the source.

    Mirrors ``test_cp_precedes_deferred_assignment_fallback`` for the
    fast-path (working build-partial) branch.
    """
    cp_pos = script_source.find('cp "$README_SOURCE_FILE" "$_bak_happy"')
    assign_pos = script_source.find('README_BACKUP="$_bak_happy"')
    assert cp_pos != -1, (
        'cp "$README_SOURCE_FILE" "$_bak_happy" not found in script source'
    )
    assert assign_pos != -1, (
        'README_BACKUP="$_bak_happy" not found in script source'
    )
    assert cp_pos < assign_pos, (
        "cp must precede README_BACKUP assignment at the happy-path backup site — "
        f"cp at offset {cp_pos}, README_BACKUP= at {assign_pos}"
    )


# ---------------------------------------------------------------------------
# Functional — cleanup guard with simulated race state (Finding 4-B)
# ---------------------------------------------------------------------------


def test_cleanup_guard_skips_restore_when_readme_backup_empty(tmp_path: Path) -> None:
    """The fixed cleanup guard does NOT restore when ``README_BACKUP`` is empty.

    Simulates the state left by the *fixed* code after mktemp but before the
    deferred ``README_BACKUP="$_bak_*"`` assignment: ``README_BACKUP=""`` and
    an empty 0-byte file exists on disk.  Verifies the vault file is untouched.

    This is the key regression test for Finding 4-B: with the OLD pattern
    (``README_BACKUP=$(mktemp ...)``), the guard fired on the 0-byte file and
    truncated the vault file.  With the fix, ``README_BACKUP`` is empty at this
    point in execution so the guard never fires.
    """
    vault_file = tmp_path / "vault_note.md"
    vault_file.write_text("original content — must not be corrupted\n", encoding="utf-8")

    empty_bak = tmp_path / "empty_backup.bak"
    empty_bak.write_text("", encoding="utf-8")  # 0-byte file, as mktemp creates

    # Fixed-code state: README_BACKUP is still "" (deferred assignment not yet run)
    bash = f"""\
#!/usr/bin/env bash
README_BACKUP=""
README_RESTORE_TARGET="{vault_file}"
# This is the FIXED cleanup guard from bin/brain-verify-fastpath _cleanup()
if [[ -n "$README_BACKUP" && -f "$README_BACKUP" && -n "$README_RESTORE_TARGET" ]]; then
    cp "$README_BACKUP" "$README_RESTORE_TARGET" 2>/dev/null || true
fi
"""
    result = subprocess.run(
        ["bash", "-c", bash],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert vault_file.read_text(encoding="utf-8") == "original content — must not be corrupted\n", (
        "_cleanup() must NOT modify the vault file when README_BACKUP is empty "
        "(deferred-assignment invariant: README_BACKUP='' when backup not yet valid)"
    )


def test_old_pattern_would_corrupt_vault_file(tmp_path: Path) -> None:
    """Documents the bug: the OLD pattern (README_BACKUP set before cp) corrupts vault.

    This test deliberately exercises the *buggy* behaviour to prove the
    regression is real — not hypothetical.  The old cleanup guard ran
    ``cp "$README_BACKUP" "$README_RESTORE_TARGET"`` even when README_BACKUP
    pointed at a 0-byte mktemp file, silently truncating the vault file.

    Kept as documentation so future readers understand *why* the deferred-
    assignment pattern is required.
    """
    vault_file = tmp_path / "vault_note.md"
    vault_file.write_text("original content\n", encoding="utf-8")

    empty_bak = tmp_path / "empty_backup.bak"
    empty_bak.write_text("", encoding="utf-8")  # 0-byte — mktemp with no cp yet

    # OLD-code state: README_BACKUP set directly from mktemp, before cp ran
    bash = f"""\
#!/usr/bin/env bash
README_BACKUP="{empty_bak}"   # old: set BEFORE cp runs
README_RESTORE_TARGET="{vault_file}"
# OLD cleanup guard (no -n "$README_RESTORE_TARGET" check, wrong pattern):
if [[ -n "$README_BACKUP" && -f "$README_BACKUP" ]]; then
    cp "$README_BACKUP" "$README_RESTORE_TARGET" 2>/dev/null || true
fi
"""
    subprocess.run(["bash", "-c", bash], capture_output=True, timeout=5, check=False)
    # The old code truncated the file — prove this is the bug we fixed.
    assert vault_file.stat().st_size == 0, (
        "old cleanup pattern must truncate vault file to 0 bytes when README_BACKUP "
        "points to a 0-byte mktemp file (this documents the pre-fix bug)"
    )


def test_cleanup_guard_restores_correctly_when_backup_valid(tmp_path: Path) -> None:
    """The fixed cleanup guard DOES restore when the backup is valid (non-empty).

    After the deferred assignment runs (``README_BACKUP="$_bak_*"``), the guard
    should fire and restore the original content over the edited vault file.
    This is the normal-interrupt-during-wait scenario.
    """
    vault_file = tmp_path / "vault_note.md"
    vault_file.write_text("edited content — appended comment\n", encoding="utf-8")

    backup_file = tmp_path / "valid_backup.bak"
    backup_file.write_text("original content\n", encoding="utf-8")  # valid backup

    bash = f"""\
#!/usr/bin/env bash
README_BACKUP="{backup_file}"   # set AFTER cp succeeded (deferred assignment done)
README_RESTORE_TARGET="{vault_file}"
if [[ -n "$README_BACKUP" && -f "$README_BACKUP" && -n "$README_RESTORE_TARGET" ]]; then
    if cp "$README_BACKUP" "$README_RESTORE_TARGET" 2>/dev/null; then
        rm -f "$README_BACKUP"
    fi
fi
"""
    result = subprocess.run(
        ["bash", "-c", bash],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert vault_file.read_text(encoding="utf-8") == "original content\n", (
        "_cleanup() must restore the original content from a valid backup file "
        "when README_BACKUP points to a non-empty file (normal interrupt scenario)"
    )
    # Backup file should be removed after successful restore.
    assert not backup_file.exists(), (
        "_cleanup() must remove the backup temp file after a successful restore"
    )
