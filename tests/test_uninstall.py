"""Tests for brain uninstall (T4.3).

Tests cover:
1. Default run keeps data/postgres intact while removing runtime files.
2. --remove-db requires typed confirmation "yes, delete my data"; wrong answer aborts.
3. --remove-vault removes the vault; absence of flag keeps it.
4. launchd uninstall_main is called on macOS.
5. F16: --remove-vault resolves the configured vault (env, then $BRAIN_HOME/.env,
   then the default), prints it with its source, demands the typed path even
   under --yes, and refuses unsafe targets. A decoy default vault is never touched.
6. F41: "docker compose down" reports [ok] only on exit 0.
"""
from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

import brain.config as config_module
import brain.uninstall as uninstall_module

# Pure filesystem tests: no database connection, so no suite-wide DB lock.
pytestmark = pytest.mark.nodb

# ---------------------------------------------------------------------------
# Isolation — no test in this file can reach a real home or a real vault
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def decoy_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` and every ``DEFAULT_VAULT_PATH`` binding at ``tmp_path``.

    The returned path is the DECOY default vault (``<fake home>/brain-vault``).
    Before the F16 fix, ``brain.uninstall`` bound ``DEFAULT_VAULT_PATH`` (the
    real ``~/brain-vault``) at import and rmtree'd it, so both bindings are
    redirected here; ``raising=False`` because the fixed module no longer
    imports the name. Only pytest's ``monkeypatch`` is used, so all of it is
    undone after each test.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    decoy = fake_home / "brain-vault"
    monkeypatch.setenv("HOME", str(fake_home))
    # A shell's (or the session fixture's) value must not choose the vault.
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_VAULT_PATH", decoy)
    monkeypatch.setattr(uninstall_module, "DEFAULT_VAULT_PATH", decoy, raising=False)
    assert decoy == config_module.DEFAULT_VAULT_PATH
    assert fake_home == Path.home()
    return decoy


# ---------------------------------------------------------------------------
# Helper — run run_uninstall with pre-populated directories
# ---------------------------------------------------------------------------


def _build_brain_home(tmp_path: Path) -> Path:
    """Create a typical $BRAIN_HOME skeleton under tmp_path."""
    home = tmp_path / ".brain"
    # data/postgres (should survive by default)
    (home / "data" / "postgres").mkdir(parents=True)
    (home / "data" / "postgres" / "PG_VERSION").write_text("16")
    # safe-to-remove entries
    (home / ".env").write_text("DATABASE_URL=postgresql://localhost/brain\n")
    (home / ".shims").mkdir()
    (home / ".shims" / "brain-up").write_text("#!/usr/bin/env bash\n")
    (home / "Caddyfile").write_text("# caddy\n")
    (home / "docker-compose.yml").write_text("version: '3'\n")
    return home


def _subprocess_noop(*args: object, **kwargs: object) -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    return m


# ---------------------------------------------------------------------------
# Test 1 — default uninstall removes runtime files but keeps data/postgres
# ---------------------------------------------------------------------------


def test_uninstall_removes_brain_home_keeps_data_postgres(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_marker = home / "data" / "postgres" / "PG_VERSION"
    assert pg_marker.exists()

    with patch("subprocess.run", side_effect=_subprocess_noop):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    # Runtime files must be gone.
    assert not (home / ".env").exists(), ".env should be removed"
    assert not (home / ".shims").exists(), ".shims/ should be removed"
    assert not (home / "Caddyfile").exists(), "Caddyfile should be removed"

    # Database data must survive.
    assert pg_marker.exists(), "data/postgres must NOT be removed without --remove-db"


# ---------------------------------------------------------------------------
# Test 2 — --remove-db requires the exact typed phrase; wrong answer aborts
# ---------------------------------------------------------------------------


def test_uninstall_remove_db_requires_typed_confirmation(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_marker = home / "data" / "postgres" / "PG_VERSION"

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="no"),  # wrong confirmation phrase
        pytest.raises((typer.Abort, SystemExit)),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=True,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    # Database data must survive the failed confirmation.
    assert pg_marker.exists(), "data/postgres must NOT be removed when confirmation fails"


def test_uninstall_remove_db_correct_phrase_removes_postgres(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_dir = home / "data" / "postgres"
    assert pg_dir.exists()

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="yes, delete my data"),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=True,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    assert not pg_dir.exists(), "data/postgres must be removed when correct phrase given"


# ---------------------------------------------------------------------------
# Test 3 — --remove-vault removes vault; default keeps it
# ---------------------------------------------------------------------------


def test_uninstall_remove_vault_takes_explicit_flag(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# My note\n")

    # Without flag: vault survives.
    with patch("subprocess.run", side_effect=_subprocess_noop):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=vault,
            _launchd_uninstall=MagicMock(),
        )

    assert (vault / "note.md").exists(), "vault must NOT be removed without --remove-vault"

    # Rebuild brain home for the second call.
    home2 = _build_brain_home(tmp_path / "run2")

    # With flag: vault is removed once its resolved path is typed.
    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value=str(vault.resolve())),
    ):
        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=True,
            brain_home=home2,
            vault_path=vault,
            _launchd_uninstall=MagicMock(),
        )

    assert not vault.exists(), "vault must be removed when --remove-vault is given"


# ---------------------------------------------------------------------------
# Test 4 — launchd uninstall_main is called on macOS
# ---------------------------------------------------------------------------


def test_uninstall_runs_launchd_uninstall_on_macos(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    called: list[int] = []

    def _fake_launchd() -> None:
        called.append(1)

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch.object(sys, "platform", "darwin"),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=_fake_launchd,
        )

    assert called, "uninstall_main (launchd) must be called on macOS"


# ---------------------------------------------------------------------------
# Test 5 — the final removal hint names the current distribution (secondbrain-py)
# ---------------------------------------------------------------------------


def test_uninstall_hint_uses_current_distribution_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: `brain uninstall`'s final hint must print the real pipx name.

    The distribution was renamed to ``secondbrain-py``, so the printed
    ``pipx uninstall …`` command MUST name ``secondbrain-py`` — the old
    hyphenated target errors (no such package). Note the old name is not a
    substring of ``secondbrain-py`` (no hyphen after "second"), so the negative
    assertion below is meaningful.
    """
    home = _build_brain_home(tmp_path)

    with patch("subprocess.run", side_effect=_subprocess_noop):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    out = capsys.readouterr().out
    assert "pipx uninstall secondbrain-py" in out, (
        f"uninstall hint must name the current distribution 'secondbrain-py'; got:\n{out}"
    )
    assert "second-brain" not in out, (
        f"uninstall hint must not reference the OLD distribution name 'second-brain'; got:\n{out}"
    )


# ---------------------------------------------------------------------------
# F16 — --remove-vault targets the CONFIGURED vault, never a decoy default
# ---------------------------------------------------------------------------


def _make_vault(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "note.md").write_text("# synthetic note\n")
    return path


def _launchd_recorder() -> tuple[list[int], Callable[[], None]]:
    calls: list[int] = []

    def _fake() -> None:
        calls.append(1)

    return calls, _fake


def test_remove_vault_targets_brain_vault_path_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoy_vault: Path
) -> None:
    home = _build_brain_home(tmp_path)
    _make_vault(decoy_vault)
    configured = _make_vault(tmp_path / "elsewhere" / "notes")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(configured))

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value=str(configured.resolve())),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=MagicMock()
        )

    assert not configured.exists(), "the vault BRAIN_VAULT_PATH names must be removed"
    assert (decoy_vault / "note.md").exists(), "the default ~/brain-vault must be untouched"


def test_remove_vault_targets_brain_vault_path_from_brain_home_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoy_vault: Path
) -> None:
    home = _build_brain_home(tmp_path)
    _make_vault(decoy_vault)
    configured = _make_vault(tmp_path / "elsewhere" / "notes")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    (home / ".env").write_text(f"BRAIN_VAULT_PATH={configured}\n")

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value=str(configured.resolve())),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=MagicMock()
        )

    # Step 6 deleted the .env, so the path must have been read before it.
    assert not (home / ".env").exists()
    assert not configured.exists(), "the vault $BRAIN_HOME/.env names must be removed"
    assert (decoy_vault / "note.md").exists(), "the default ~/brain-vault must be untouched"


def test_summary_and_kept_line_print_resolved_absolute_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", "rel-vault")
    configured = _make_vault(tmp_path / "rel-vault")
    absolute = configured.resolve()

    with patch("subprocess.run", side_effect=_subprocess_noop):
        uninstall_module.run_uninstall(
            yes=True,
            brain_home=_build_brain_home(tmp_path / "run1"),
            _launchd_uninstall=MagicMock(),
        )
    assert f"vault kept at {absolute}" in capsys.readouterr().out

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="not the path"),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True,
            remove_vault=True,
            brain_home=_build_brain_home(tmp_path / "run2"),
            _launchd_uninstall=MagicMock(),
        )
    assert f"  • {absolute}  (from BRAIN_VAULT_PATH env)  ← YOUR NOTES" in (
        capsys.readouterr().out
    )


def _wrong_answer(case: str, tmp_path: Path) -> tuple[Path, str]:
    """Build a vault and a near-miss answer; return ``(vault, answer)``."""
    if case == "unresolved_symlinked_parent":
        real_parent = tmp_path / "real-parent"
        vault = _make_vault(real_parent / "vault")
        link_parent = tmp_path / "link-parent"
        link_parent.symlink_to(real_parent, target_is_directory=True)
        configured = link_parent / "vault"
        # The configured spelling names the same directory, but it is not
        # the resolved path the prompt printed, so it must not be accepted.
        assert str(configured) != str(vault.resolve())
        return configured, str(configured)
    vault = _make_vault(tmp_path / "vault")
    target = str(vault.resolve())
    answers = {"y": "y", "trailing_space": f"{target} ", "trailing_slash": f"{target}/"}
    return vault, answers[case]


@pytest.mark.parametrize(
    "case", ["y", "trailing_space", "trailing_slash", "unresolved_symlinked_parent"]
)
def test_remove_vault_wrong_typed_path_removes_nothing(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _build_brain_home(tmp_path)
    vault, answer = _wrong_answer(case, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    launchd_calls, fake_launchd = _launchd_recorder()
    run = MagicMock(side_effect=_subprocess_noop)

    with (
        patch("subprocess.run", run),
        patch("typer.prompt", return_value=answer),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=fake_launchd
        )

    assert (vault / "note.md").exists(), "a wrong typed path must leave the vault intact"
    assert (home / ".env").exists() and (home / ".shims").exists(), "nothing may be removed"
    assert not launchd_calls, "launchd must not be touched after a failed confirmation"
    run.assert_not_called()


def test_remove_db_then_wrong_vault_path_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--remove-db with --remove-vault: a correct DB phrase does not excuse the vault path."""
    home = _build_brain_home(tmp_path)
    pg_marker = home / "data" / "postgres" / "PG_VERSION"
    vault = _make_vault(tmp_path / "vault")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    launchd_calls, fake_launchd = _launchd_recorder()
    run = MagicMock(side_effect=_subprocess_noop)

    with (
        patch("subprocess.run", run),
        patch("typer.prompt", side_effect=["yes, delete my data", "wrong"]),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True,
            remove_db=True,
            remove_vault=True,
            brain_home=home,
            _launchd_uninstall=fake_launchd,
        )

    assert pg_marker.exists(), "data/postgres must survive a failed vault confirmation"
    assert (vault / "note.md").exists()
    assert (home / ".env").exists() and (home / ".shims").exists()
    assert not launchd_calls
    run.assert_not_called()


def test_yes_does_not_bypass_vault_confirmation_at_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes plus no input (a script, </dev/null): the REAL prompt hits EOF and aborts."""
    home = _build_brain_home(tmp_path)
    vault = _make_vault(tmp_path / "vault")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    launchd_calls, fake_launchd = _launchd_recorder()

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=fake_launchd
        )

    assert (vault / "note.md").exists(), "--yes must not bypass the typed vault path"
    assert (home / ".env").exists(), "nothing may be removed when the prompt aborts"
    assert not launchd_calls


def _unsafe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    return _build_brain_home(tmp_path), Path.home()


def _unsafe_parent_of_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    nested_home = tmp_path / "users" / "me"
    nested_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(nested_home))
    return _build_brain_home(tmp_path), tmp_path / "users"


def _unsafe_brain_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, home


def _unsafe_parent_of_brain_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    return _build_brain_home(tmp_path / "outer"), tmp_path / "outer"


def _unsafe_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    real = _make_vault(tmp_path / "real-vault")
    link = tmp_path / "vault-link"
    link.symlink_to(real, target_is_directory=True)
    return _build_brain_home(tmp_path), link


def _unsafe_not_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    file_vault = tmp_path / "vault.txt"
    file_vault.write_text("not a directory\n")
    return _build_brain_home(tmp_path), file_vault


def _unsafe_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, home / "data" / "postgres"


def _unsafe_parent_of_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, home / "data"


def _unsafe_inside_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, _make_vault(home / "data" / "postgres" / "base")


def _unsafe_runtime_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, home / ".shims"


def _unsafe_inside_runtime_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = _build_brain_home(tmp_path)
    return home, _make_vault(home / ".shims" / "vault")


def _unsafe_parent_of_symlinked_brain_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """$BRAIN_HOME is a symlink; the vault is the REAL parent of the directory it names."""
    real_home = _build_brain_home(tmp_path / "real" / "outer")
    link = tmp_path / "brain-home-link"
    link.symlink_to(real_home, target_is_directory=True)
    return link, tmp_path / "real" / "outer"


def _unsafe_symlinked_home_real_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """HOME is a symlink; the vault is the real parent of the home directory."""
    real_home = tmp_path / "real-homes" / "me"
    real_home.mkdir(parents=True)
    home_link = tmp_path / "home-link"
    home_link.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home_link))
    return _build_brain_home(tmp_path), tmp_path / "real-homes"


def _unsafe_symlinked_brain_home_real_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A PARENT of $BRAIN_HOME is a symlink; the vault is an ancestor of its real target."""
    _build_brain_home(tmp_path / "real-tree" / "outer")
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(tmp_path / "real-tree" / "outer", target_is_directory=True)
    return parent_link / ".brain", tmp_path / "real-tree"


_HOME_REASON = "is your home directory or a parent of it"
_BRAIN_HOME_REASON = "is $BRAIN_HOME or a parent of it"
_DATABASE_REASON = "is the database directory, inside it, or a parent of it"
_RUNTIME_REASON = "$BRAIN_HOME/.shims or inside it"

_UnsafeBuilder = Callable[[Path, pytest.MonkeyPatch], tuple[Path, Path]]

#: Unsafe-vault scenarios: a builder returning ``(brain_home, configured vault)``
#: and the refusal reason THAT case must print. Asserting each case's own reason
#: is what pins every guard: with only ``Abort`` checked, a later guard could
#: cover for a disabled one.
_UNSAFE_TARGETS: dict[str, tuple[_UnsafeBuilder, str]] = {
    "home": (_unsafe_home, _HOME_REASON),
    "parent_of_home": (_unsafe_parent_of_home, _HOME_REASON),
    "symlinked_home_real_parent": (_unsafe_symlinked_home_real_parent, _HOME_REASON),
    "brain_home": (_unsafe_brain_home, _BRAIN_HOME_REASON),
    "parent_of_brain_home": (_unsafe_parent_of_brain_home, _BRAIN_HOME_REASON),
    "parent_of_symlinked_brain_home": (
        _unsafe_parent_of_symlinked_brain_home,
        _BRAIN_HOME_REASON,
    ),
    "symlinked_brain_home_real_parent": (
        _unsafe_symlinked_brain_home_real_parent,
        _BRAIN_HOME_REASON,
    ),
    "symlink": (_unsafe_symlink, "is a symlink"),
    "not_a_directory": (_unsafe_not_a_directory, "is not a directory"),
    "database": (_unsafe_database, _DATABASE_REASON),
    "parent_of_database": (_unsafe_parent_of_database, _DATABASE_REASON),
    "inside_database": (_unsafe_inside_database, _DATABASE_REASON),
    "runtime_entry": (_unsafe_runtime_entry, _RUNTIME_REASON),
    "inside_runtime_entry": (_unsafe_inside_runtime_entry, _RUNTIME_REASON),
}


@pytest.mark.parametrize("case", sorted(_UNSAFE_TARGETS))
def test_remove_vault_refuses_unsafe_target(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build, reason = _UNSAFE_TARGETS[case]
    home, configured = build(tmp_path, monkeypatch)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(configured))
    launchd_calls, fake_launchd = _launchd_recorder()

    # The correct typed answer, so the guard is the only thing that can refuse.
    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value=str(configured.resolve())),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=fake_launchd
        )

    assert configured.exists() or configured.is_symlink(), f"{case}: target must survive"
    assert (home / ".env").exists(), f"{case}: nothing may be removed"
    assert not launchd_calls
    out = capsys.readouterr().out
    assert "Refusing --remove-vault" in out
    assert reason in out, f"{case}: expected its own refusal reason {reason!r}; got:\n{out}"


def test_remove_vault_refuses_case_variant_of_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guards compare file identity, not spelling: ``…/HOME`` IS ``…/home`` here."""
    fake_home = Path.home()
    variant = fake_home.with_name(fake_home.name.upper())
    if not variant.exists():
        pytest.skip("tmp filesystem is case-sensitive; the variant names no directory")
    home = _build_brain_home(tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(variant))
    launchd_calls, fake_launchd = _launchd_recorder()

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value=str(variant.resolve())),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=fake_launchd
        )

    assert fake_home.is_dir(), "the home directory must survive"
    assert (home / ".env").exists()
    assert not launchd_calls
    out = capsys.readouterr().out
    assert "Refusing --remove-vault" in out
    assert _HOME_REASON in out


def test_remove_vault_missing_target_skips_without_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "no-such-vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(absent))
    prompt = MagicMock(return_value="unused")

    with patch("subprocess.run", side_effect=_subprocess_noop), patch("typer.prompt", prompt):
        uninstall_module.run_uninstall(
            yes=True,
            remove_vault=True,
            brain_home=_build_brain_home(tmp_path),
            _launchd_uninstall=MagicMock(),
        )

    assert f"vault not found at {absent.resolve()}, nothing to remove" in (
        capsys.readouterr().out
    )
    prompt.assert_not_called()


@pytest.mark.parametrize("source", ["env", "brain_home_dotenv", "default"])
def test_vault_source_label_printed_in_summary_and_prompt(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decoy_vault: Path,
) -> None:
    home = _build_brain_home(tmp_path)
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    if source == "env":
        target = _make_vault(tmp_path / "env-vault")
        monkeypatch.setenv("BRAIN_VAULT_PATH", str(target))
        label = "BRAIN_VAULT_PATH env"
    elif source == "brain_home_dotenv":
        target = _make_vault(tmp_path / "dotenv-vault")
        (home / ".env").write_text(f"BRAIN_VAULT_PATH={target}\n")
        label = "$BRAIN_HOME/.env"
    else:
        # Set nowhere uninstall reads: not in env, not in $BRAIN_HOME/.env.
        target = _make_vault(decoy_vault)
        label = "default ~/brain-vault"

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="decline"),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=MagicMock()
        )

    out = capsys.readouterr().out
    shown = f"{target.resolve()}  (from {label})"
    assert out.count(shown) == 2, f"summary and prompt must both show {shown!r}; got:\n{out}"
    assert (target / "note.md").exists()


# ---------------------------------------------------------------------------
# F41 — "docker compose down" is [ok] only on exit 0
# ---------------------------------------------------------------------------


def _compose_result(returncode: int, stderr: str) -> Callable[..., object]:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout="", stderr=stderr
        )

    return _run


def test_compose_down_nonzero_exit_warns_with_code_and_stderr_tail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stderr = "pulling state\nError: no such project\n"
    with patch("subprocess.run", side_effect=_compose_result(1, stderr)):
        uninstall_module.run_uninstall(
            yes=True, brain_home=_build_brain_home(tmp_path), _launchd_uninstall=MagicMock()
        )

    out = capsys.readouterr().out
    assert "[ok]     docker compose down" not in out
    assert "[warn]   docker compose down exited 1" in out
    assert "no such project" in out


@pytest.mark.parametrize("outcome", ["rc0", "exception"])
def test_compose_down_ok_only_on_zero_and_warns_on_exception(
    outcome: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pin (passes on master too): rc 0 stays [ok]; a missing docker stays [warn]."""
    effect: object = (
        _compose_result(0, "") if outcome == "rc0" else FileNotFoundError("docker")
    )
    with patch("subprocess.run", side_effect=effect):
        uninstall_module.run_uninstall(
            yes=True, brain_home=_build_brain_home(tmp_path), _launchd_uninstall=MagicMock()
        )

    out = capsys.readouterr().out
    if outcome == "rc0":
        assert "[ok]     docker compose down" in out
    else:
        assert "[warn]   docker compose down" in out
        assert "[ok]     docker compose down" not in out
