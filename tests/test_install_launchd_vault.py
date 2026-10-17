"""``brain-install-launchd`` must bake the CONFIGURED vault into the plists.

``install_main`` used to call ``install_plists`` with no ``vault_path``, and
``install_plists`` fell back to a literal ``~/brain-vault``. Every ``brain-up``
runs ``brain-install-launchd``, so an install with a custom vault had its
daemons repointed at the default on the next ``brain-up``.

Everything runs on ``tmp_path``: ``BRAIN_HOME``, ``HOME``, the vaults, the
LaunchAgents directory (``BRAIN_LAUNCHD_DIR``) and ``launchctl``
(``BRAIN_LAUNCHCTL=/usr/bin/true``), and the default-vault constant. Nothing
reaches the real ``~/Library/LaunchAgents``, ``launchctl``, ``~/.brain`` or the
vault.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from brain.bin.launchd import _INSTALLED_LABELS, install_main, install_plists


@pytest.fixture
def fenced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fence every path ``install_main`` can reach into ``tmp_path``.

    Returns the ``$BRAIN_HOME`` it set. The default vault is patched on
    :mod:`brain.config`, where the resolver reads it, so a regressed resolver
    still lands on a throwaway path.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", "/usr/bin/true")
    monkeypatch.setattr("brain.config.DEFAULT_VAULT_PATH", tmp_path / "default-vault")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    return brain_home


def _baked_vaults(tmp_path: Path) -> dict[str, str]:
    """``BRAIN_VAULT_PATH`` from each installed plist, keyed by label."""
    out = {}
    for label in _INSTALLED_LABELS:
        with (tmp_path / "LaunchAgents" / f"{label}.plist").open("rb") as fh:
            out[label] = plistlib.load(fh)["EnvironmentVariables"]["BRAIN_VAULT_PATH"]
    return out


def test_install_main_takes_vault_from_brain_home_dotenv(fenced: Path, tmp_path: Path) -> None:
    """Regression: the vault named in ``$BRAIN_HOME/.env`` reaches every plist."""
    custom = tmp_path / "custom-vault"
    (fenced / ".env").write_text(f"BRAIN_VAULT_PATH={custom}\n", encoding="utf-8")

    install_main()

    assert _baked_vaults(tmp_path) == dict.fromkeys(_INSTALLED_LABELS, str(custom))


def test_install_main_process_env_beats_brain_home_dotenv(
    fenced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same precedence as ``brain setup``: the process env outranks the file."""
    (fenced / ".env").write_text(f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n", encoding="utf-8")
    from_env = tmp_path / "from-env"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(from_env))

    install_main()

    assert _baked_vaults(tmp_path) == dict.fromkeys(_INSTALLED_LABELS, str(from_env))


def test_install_main_without_any_vault_config_uses_the_default(
    fenced: Path, tmp_path: Path
) -> None:
    """No env and no ``.env`` value: the default vault, as before."""
    install_main()

    assert _baked_vaults(tmp_path) == dict.fromkeys(
        _INSTALLED_LABELS, str(tmp_path / "default-vault")
    )


def test_explicit_vault_path_beats_env_and_dotenv(
    fenced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``brain setup`` passes its own resolved vault; that still wins outright."""
    (fenced / ".env").write_text(f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n", encoding="utf-8")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"

    install_plists(
        fenced,
        tmp_path / "LaunchAgents",
        "/usr/bin/true",
        vault_path=explicit,
    )

    assert _baked_vaults(tmp_path) == dict.fromkeys(_INSTALLED_LABELS, str(explicit))


@pytest.mark.parametrize("leg", ["dotenv", "process-env"])
def test_a_tilde_vault_is_expanded_before_it_reaches_the_plist(
    fenced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leg: str
) -> None:
    """``~/…`` is expanded before it is baked into the plist.

    The plist should carry the absolute path, as ``brain setup`` always baked
    (it did ``Path(env).expanduser()``), so the baked value does not depend on
    every consumer remembering to expand it. The daemons do today (``vault
    sync`` and ``_default_vault_path`` both call ``expanduser``); a raw reader
    such as ``bin/monitor.py``'s ``_vault`` would not. ``HOME`` is fenced, so
    the expansion lands in ``tmp_path``.
    """
    if leg == "dotenv":
        (fenced / ".env").write_text("BRAIN_VAULT_PATH=~/custom-vault\n", encoding="utf-8")
    else:
        monkeypatch.setenv("BRAIN_VAULT_PATH", "~/custom-vault")

    install_main()

    expected = str(tmp_path / "home" / "custom-vault")
    assert _baked_vaults(tmp_path) == dict.fromkeys(_INSTALLED_LABELS, expected)
