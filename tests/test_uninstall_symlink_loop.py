"""``brain uninstall`` and a vault path that cannot be resolved (a symlink loop).

Before the F16 fix, uninstall never resolved the vault path. The fix resolves it,
and on Python 3.11 ``Path.resolve`` raises ``RuntimeError`` on a loop, so even a
plain ``brain uninstall`` printed a traceback. These tests pin the two
behaviours:

- **No ``--remove-vault``:** the "vault kept at" line falls back to the
  configured absolute path.
- **``--remove-vault``:** the vault is refused before anything is removed.

They also pin ``_lineage``'s fallback to the spelled chain.

Newer interpreters may resolve a loop without raising. The ``symlink_loop``
fixture therefore makes ``Path.resolve`` raise for the loop's two links on any
interpreter that does not raise natively, so the same branch is exercised
everywhere. On 3.11, which CI runs, the real ``RuntimeError`` is used.

Every path lives under ``tmp_path``. ``HOME`` and the default vault are fenced
there, and launchd and ``docker compose`` are fakes.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

import brain.config as config_module
import brain.uninstall as uninstall_module

# Pure filesystem tests: no database connection, so no suite-wide DB lock.
pytestmark = pytest.mark.nodb


@pytest.fixture(autouse=True)
def _fenced_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``HOME`` and every ``DEFAULT_VAULT_PATH`` binding into ``tmp_path``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    decoy = fake_home / "brain-vault"
    monkeypatch.setattr(config_module, "DEFAULT_VAULT_PATH", decoy)
    monkeypatch.setattr(uninstall_module, "DEFAULT_VAULT_PATH", decoy, raising=False)


@pytest.fixture
def symlink_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``<tmp>/loop/a`` → ``b`` → ``a``; ``Path.resolve`` raises on it on every interpreter."""
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    a, b = loop_dir / "a", loop_dir / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    try:
        a.resolve()
    except (OSError, RuntimeError):
        return a  # Raises natively (Python 3.11): exercise the real thing.

    real_resolve = Path.resolve

    def _resolve(self: Path, strict: bool = False) -> Path:
        if self.absolute() in (a, b):
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    return a


def _brain_home(tmp_path: Path) -> Path:
    home = tmp_path / ".brain"
    (home / "data" / "postgres").mkdir(parents=True)
    (home / ".env").write_text("DATABASE_URL=postgresql://localhost/synthetic\n")
    (home / ".shims").mkdir()
    return home


def _launchd_recorder() -> tuple[list[int], Callable[[], None]]:
    calls: list[int] = []

    def _fake() -> None:
        calls.append(1)

    return calls, _fake


def test_plain_uninstall_keeps_unresolvable_vault_and_prints_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    symlink_loop: Path,
) -> None:
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(symlink_loop))
    run = MagicMock(return_value=MagicMock(returncode=0))

    with patch("subprocess.run", run):
        uninstall_module.run_uninstall(
            yes=True, brain_home=_brain_home(tmp_path), _launchd_uninstall=MagicMock()
        )

    out = capsys.readouterr().out
    assert f"vault kept at {symlink_loop.absolute()}  (from BRAIN_VAULT_PATH env)" in out
    assert symlink_loop.is_symlink(), "the loop itself must be left alone"


def test_remove_vault_refuses_unresolvable_vault_before_removing_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    symlink_loop: Path,
) -> None:
    home = _brain_home(tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(symlink_loop))
    launchd_calls, fake_launchd = _launchd_recorder()
    run = MagicMock(return_value=MagicMock(returncode=0))
    prompt = MagicMock(return_value=str(symlink_loop))

    with (
        patch("subprocess.run", run),
        patch("typer.prompt", prompt),
        pytest.raises(typer.Abort),
    ):
        uninstall_module.run_uninstall(
            yes=True, remove_vault=True, brain_home=home, _launchd_uninstall=fake_launchd
        )

    out = capsys.readouterr().out
    assert f"{symlink_loop.absolute()} cannot be resolved (symlink loop)" in out
    assert "Nothing was removed" in out
    assert (home / ".env").exists() and (home / ".shims").exists()
    assert symlink_loop.is_symlink()
    assert not launchd_calls
    run.assert_not_called()
    prompt.assert_not_called()


def test_lineage_falls_back_to_the_spelled_chain_on_a_symlink_loop(
    symlink_loop: Path,
) -> None:
    """Pins ``_lineage``'s suppress: an unguarded ``resolve()`` raises here (mutant M19)."""
    lineage = uninstall_module._lineage(symlink_loop)

    spelled = symlink_loop.absolute()
    assert lineage[: len(spelled.parents) + 1] == [spelled, *spelled.parents]
