"""F25: an installed ``brain`` keeps ``brain owner`` and ``brain eval`` files under ``$BRAIN_HOME``.

Under a non-editable install the package sits in ``site-packages``, so every
``__file__``-relative default landed inside the virtualenv: ``brain owner set``
wrote ``<venv>/lib/python3.11/.env`` and ``brain eval``'s corpus and baselines
pointed at ``<venv>/lib/python3.11/tests/eval``. A source checkout keeps its
old paths byte-for-byte; the pin tests below hold that.

No test here reaches Ollama or a database: the answer-corpus regression goes
through ``load_answer_corpus`` directly, never ``brain eval --answer``.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from brain import config as config_module
from brain import install_paths
from brain.cli import app
from brain.eval import answer_eval, concept_extraction, corpus
from brain.eval.errors import EvalCorpusError
from tests.conftest import isolate_config_dotenv
from tests.wheel_install import WheelInstall, simulate_wheel_install

_LINE = 'BRAIN_OWNER_PARTICIPANTS="pat example,fixture@example.com"'


@pytest.fixture
def wheel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WheelInstall:
    return simulate_wheel_install(monkeypatch, tmp_path)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


# --------------------------------------------------------------------------
# Detection: the one heuristic, extracted from _brain_home_root.
# --------------------------------------------------------------------------


def test_checkout_root_for_repo_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").touch()
    assert install_paths.checkout_root_for(tmp_path / "src" / "brain" / "m.py") == tmp_path


def test_checkout_root_for_site_packages_layout_is_none(tmp_path: Path) -> None:
    module = tmp_path / "venv" / "lib" / "python3.11" / "site-packages" / "brain" / "m.py"
    assert install_paths.checkout_root_for(module) is None


def test_this_checkout_is_detected() -> None:
    """The suite runs from an editable checkout, so the dev branch is what it exercises."""
    expected = Path(config_module.__file__).resolve().parent.parent.parent
    assert install_paths.source_checkout_root() == expected
    assert (expected / "pyproject.toml").is_file()


def test_install_paths_imports_cold() -> None:
    """``config`` imports from ``install_paths``; neither may close an import cycle."""
    for module in ("brain.install_paths", "brain.config"):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, check=False
        )
        assert proc.returncode == 0, proc.stderr.decode()


# --------------------------------------------------------------------------
# brain owner under a wheel install.
# --------------------------------------------------------------------------


def test_owner_set_writes_brain_home_dotenv_on_wheel_install(wheel: WheelInstall) -> None:
    before = wheel.venv_files()
    result = _invoke("owner", "set", "Pat Example,fixture@example.com")
    assert result.exit_code == 0, result.output
    assert _LINE in (wheel.home / ".env").read_text(encoding="utf-8")
    assert not wheel.package_dotenv.exists()
    assert wheel.venv_files() == before


def test_owner_add_remove_then_show_on_wheel_install(
    wheel: WheelInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _invoke("owner", "add", "Pat Example").exit_code == 0
    assert _invoke("owner", "add", "fixture@example.com").exit_code == 0
    assert _invoke("owner", "add", "Other Example").exit_code == 0
    assert _invoke("owner", "remove", "other example").exit_code == 0
    assert _LINE in (wheel.home / ".env").read_text(encoding="utf-8")
    assert not wheel.package_dotenv.exists()

    shown = _invoke("owner", "show")
    assert shown.exit_code == 0, shown.output
    lines = [line for line in shown.output.splitlines() if line.strip()]
    assert lines == ["fixture@example.com", "pat example"]


def test_owner_write_keeps_brain_home_symlink(wheel: WheelInstall, tmp_path: Path) -> None:
    """A symlinked ``$BRAIN_HOME/.env`` (``brain setup``'s dev shape) is written THROUGH.

    Replacing the link with a regular file would silently fork the config the
    link exists to share. The target may live anywhere (here: outside
    ``$BRAIN_HOME``); that is the point of the link.
    """
    target = tmp_path / "dotfiles" / "brain.env"
    target.parent.mkdir()
    target.write_text("DATABASE_URL=postgresql://x:y@localhost:1/d\n", encoding="utf-8")
    link = wheel.home / ".env"
    link.symlink_to(target)

    result = _invoke("owner", "set", "Pat Example,fixture@example.com")
    assert result.exit_code == 0, result.output
    assert link.is_symlink() and link.resolve() == target.resolve()
    text = target.read_text(encoding="utf-8")
    assert _LINE in text and "DATABASE_URL=postgresql://x:y@localhost:1/d" in text
    assert sorted(p.name for p in wheel.home.iterdir()) == [".env"]
    assert sorted(p.name for p in target.parent.iterdir()) == ["brain.env"]
    assert not wheel.package_dotenv.exists()


def test_owner_refuses_dangling_brain_home_symlink(wheel: WheelInstall, tmp_path: Path) -> None:
    """A dangling ``$BRAIN_HOME/.env`` link is refused: exit 1, nothing written anywhere.

    The user's real config presumably moved (``brain setup`` reports the same
    shape as ``dangling`` rather than replacing it), so neither re-creating the
    old target nor replacing the link with a fresh file is safe to do silently.
    """
    target = tmp_path / "moved_checkout" / ".env"
    link = wheel.home / ".env"
    link.symlink_to(target)
    before = wheel.venv_files()

    result = _invoke("owner", "set", "Pat Example")
    assert result.exit_code == 1, result.output
    assert "symlink" in result.output and str(link) in result.output
    assert "Traceback" not in result.output
    assert link.is_symlink() and not target.exists()
    assert not target.parent.exists()
    assert sorted(p.name for p in wheel.home.iterdir()) == [".env"]
    assert wheel.venv_files() == before


def test_owner_set_in_checkout_writes_project_dotenv_even_with_brain_home_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pin: a source checkout still writes the repo ``.env``, whatever ``$BRAIN_HOME`` is."""
    project = isolate_config_dotenv(
        monkeypatch, tmp_path, clear=("BRAIN_OWNER_PARTICIPANTS",),
        project_env=tmp_path / "project.env",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(home))

    result = _invoke("owner", "set", "Pat Example,fixture@example.com")
    assert result.exit_code == 0, result.output
    assert _LINE in project.read_text(encoding="utf-8")
    assert not (home / ".env").exists()


# --------------------------------------------------------------------------
# brain eval defaults.
# --------------------------------------------------------------------------


def test_eval_corpus_default_on_wheel_install(wheel: WheelInstall) -> None:
    """``brain eval`` with no ``--corpus`` looks in ``$BRAIN_HOME/eval``; fails before any DB."""
    result = _invoke("eval")
    assert result.exit_code == 1, result.output
    expected = wheel.home / "eval" / "golden_corpus.yaml"
    assert f"corpus file not found: {expected}" in result.output


def test_answer_corpus_default_on_wheel_install(wheel: WheelInstall) -> None:
    """The ``--answer`` default lives under ``$BRAIN_HOME/eval``. Loader only: no Ollama."""
    expected = wheel.home / "eval" / "answer_corpus.yaml"
    with pytest.raises(EvalCorpusError, match="answer corpus file not found") as info:
        answer_eval.load_answer_corpus()
    assert str(expected) in str(info.value)


def test_eval_default_paths_on_wheel_install(wheel: WheelInstall) -> None:
    eval_dir = wheel.home / "eval"
    assert install_paths.eval_data_dir() == eval_dir
    assert install_paths.eval_baselines_dir() == eval_dir / "baselines"
    assert corpus.default_corpus_path() == eval_dir / "golden_corpus.yaml"
    assert answer_eval.default_answer_corpus_path() == eval_dir / "answer_corpus.yaml"
    assert (
        concept_extraction._default_fixture_path()
        == eval_dir / "concept_extraction_fixture.yaml"
    )


def test_eval_defaults_in_checkout_are_unchanged() -> None:
    """Pin: in a checkout every default equals the pre-F25 module constant, byte-for-byte.

    The expressions are the old ones verbatim, so the eval gate (``ci.json``,
    the gitignored ``golden_corpus.yaml``) is read from exactly where it was.
    """
    import brain.cli as cli_module

    old_corpus = (
        Path(corpus.__file__).resolve().parent.parent.parent.parent
        / "tests" / "eval" / "golden_corpus.yaml"
    )
    old_answer = (
        Path(answer_eval.__file__).resolve().parent.parent.parent.parent
        / "tests" / "eval" / "answer_corpus.yaml"
    )
    old_fixture = (
        Path(concept_extraction.__file__).resolve().parent.parent.parent.parent
        / "tests" / "eval" / "concept_extraction_fixture.yaml"
    )
    old_baselines = (
        Path(cli_module.__file__).resolve().parent.parent.parent / "tests" / "eval" / "baselines"
    )
    assert corpus.default_corpus_path() == old_corpus
    assert answer_eval.default_answer_corpus_path() == old_answer
    assert concept_extraction._default_fixture_path() == old_fixture
    assert install_paths.eval_baselines_dir() == old_baselines
    assert (old_baselines / "ci.json").is_file()
    assert old_answer.is_file() and old_fixture.is_file()
    assert install_paths.writable_dotenv() == config_module._project_dotenv()
