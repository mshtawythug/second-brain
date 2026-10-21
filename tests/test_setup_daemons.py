"""Phase 8 T3 (Q8 / R8-1): ``brain setup`` offers the launchd daemons whether or not
the wiki is installed.

"Unconditionally" means not conditional on the wiki. The macOS gate, the
``--non-interactive``-never-installs contract and the default-No prompt all stay
(``tests/test_setup.py`` pins those). The daemons are the vault-sync watcher and
the daily brief; the Quartz build daemon is retired (T2), so setup no longer
writes its wrapper shim either. ``install_plists`` is doubled throughout; the
conftest launchctl stub is the backstop.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from brain import setup


def _interactive_full_dry_run(tmp_path: Path, *, answer: bool) -> str:
    """An interactive full-profile dry run on macOS.

    Every confirm answers ``answer``.
    """
    buf = io.StringIO()
    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("typer.confirm", return_value=answer),
        patch("sys.platform", "darwin"),
        redirect_stdout(buf),
    ):
        setup.run_setup(
            profile="full",
            dry_run=True,
            non_interactive=False,
            brain_home_override=tmp_path / ".brain",
            vault_override=tmp_path / "vault",
            pg_port=0,
            skip_skill=True,
        )
    return buf.getvalue()


def _brief_time() -> str:
    """The brief's StartCalendarInterval as HH:MM, read from the plist template it installs."""
    template = (
        resource_files("brain.templates.launchd") / "com.brain.brief.plist.j2"
    ).read_text(encoding="utf-8")
    schedule = template.split("<key>StartCalendarInterval</key>", 1)[1]
    hour = re.search(r"<key>Hour</key>\s*<integer>(\d+)</integer>", schedule)
    minute = re.search(r"<key>Minute</key>\s*<integer>(\d+)</integer>", schedule)
    assert hour and minute, "the brief plist template lost its schedule"
    return f"{int(hour[1]):02d}:{int(minute[1]):02d}"


def test_a_wikiless_interactive_setup_offers_and_installs_the_daemons(tmp_path: Path) -> None:
    """E7: with the wiki skipped, accepting the prompt reaches the daemon install."""
    out = _interactive_full_dry_run(tmp_path, answer=True)

    assert "launchd install (wiki not installed)" not in out
    assert "[dry-run] would: brain-install-launchd" in out


def test_declining_the_daemons_still_skips_them(tmp_path: Path) -> None:
    """The prompt stays, and still defaults to an honoured No."""
    out = _interactive_full_dry_run(tmp_path, answer=False)

    assert "[skipped] launchd background daemons (declined)" in out
    assert "would: brain-install-launchd" not in out


def test_the_daemon_prompt_names_the_vault_watcher_and_the_brief(tmp_path: Path) -> None:
    """E7: the prompt names what it installs. There is no Postgres agent and no wiki watcher."""
    prompts: list[str] = []

    def _confirm(text: str, **_: Any) -> bool:
        prompts.append(text)
        return False

    with patch("typer.confirm", side_effect=_confirm), patch("sys.platform", "darwin"):
        setup._maybe_install_launchd(
            vault_path=tmp_path / "vault", brain_home=tmp_path / ".brain", dry_run=False
        )

    (prompt,) = prompts
    assert "vault-sync watcher" in prompt
    assert f"{_brief_time()} daily brief" in prompt
    assert "loaded on login" in prompt, "the brief runs on a schedule; login only loads it"
    assert "wiki" not in prompt.lower()
    assert "postgres" not in prompt.lower()


def test_setup_no_longer_installs_the_build_wrapper_shim(tmp_path: Path) -> None:
    """Nothing installed at R2 runs ``_brain-build-fg``; the template stays until R3."""
    out = _interactive_full_dry_run(tmp_path, answer=False)

    assert "_brain-build-fg" not in setup._SHIM_NAMES
    assert "install shim _brain-watcher-fg" in out
    assert "_brain-build-fg" not in out
