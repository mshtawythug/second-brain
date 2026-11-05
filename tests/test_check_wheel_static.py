"""Tests for ``scripts/check_wheel_static.py``: a wheel must carry the whole static tree (RM-50).

The script lives outside ``src/brain``, so it is loaded with :mod:`importlib.util`. These
tests drive it on synthetic zips and a fake build. The real build runs in CI's ``lint``
job and in ``bin/brain-ci``.
"""
from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_wheel_static.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_wheel_static", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()

TREE = ["index.html", "css/reading.css", "js/main.js", "fonts/a.woff2", "fonts/OFL.txt"]


def _static_tree(root: Path, names: list[str] = TREE) -> Path:
    static = root / "static"
    for name in names:
        path = static / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return static


def _wheel(path: Path, static_names: list[str], extra: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in static_names:
            archive.writestr(f"{check.WHEEL_STATIC_PREFIX}{name}", b"x")
        for name in extra:
            archive.writestr(name, b"x")
    return path


def test_a_complete_wheel_passes_and_other_wheel_contents_are_ignored(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", TREE, extra=("brain/cli.py", "brain/ui/app.py"))

    assert check.missing_from_wheel(static, wheel) == []


def test_a_wheel_missing_a_font_is_refused_by_name(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", [n for n in TREE if n != "fonts/a.woff2"])

    assert check.missing_from_wheel(static, wheel) == ["fonts/a.woff2"]


def test_the_whole_tree_is_covered_not_only_the_fonts(tmp_path: Path) -> None:
    """The original trap: a nested ``css/`` or ``js/`` file no glob reached."""
    static = _static_tree(tmp_path)
    wheel = _wheel(tmp_path / "w.whl", ["index.html", "fonts/a.woff2", "fonts/OFL.txt"])

    assert check.missing_from_wheel(static, wheel) == ["css/reading.css", "js/main.js"]


def test_local_build_artefacts_are_not_demanded(tmp_path: Path) -> None:
    static = _static_tree(tmp_path, [*TREE, "__pycache__/x.pyc", ".DS_Store", "css/.DS_Store"])
    wheel = _wheel(tmp_path / "w.whl", TREE)

    assert check.missing_from_wheel(static, wheel) == []


def test_an_empty_static_tree_is_a_setup_error_not_a_pass(tmp_path: Path) -> None:
    empty = tmp_path / "static"
    empty.mkdir()
    wheel = _wheel(tmp_path / "w.whl", [])

    with pytest.raises(check.WheelCheckError, match="no static files"):
        check.missing_from_wheel(empty, wheel)


def test_a_file_that_is_not_a_wheel_is_a_setup_error(tmp_path: Path) -> None:
    static = _static_tree(tmp_path)
    bad = tmp_path / "w.whl"
    bad.write_bytes(b"not a zip")

    with pytest.raises(check.WheelCheckError, match="not a readable wheel"):
        check.missing_from_wheel(static, bad)


def _repo(root: Path) -> Path:
    """The files the build copies, with a small source tree."""
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "README.md").write_text("r\n", encoding="utf-8")
    (root / "LICENSE").write_text("l\n", encoding="utf-8")
    static = root / "src" / "brain" / "ui"
    static.mkdir(parents=True)
    _static_tree(static)
    (root / "src" / "brain" / "ui" / "__pycache__").mkdir()
    (root / "src" / "brain" / "ui" / "__pycache__" / "x.pyc").write_bytes(b"x")
    return root


class _FakePip:
    """Stands in for ``subprocess.run`` and records what the build was given."""

    def __init__(self, *, names: list[str] = TREE, returncode: int = 0) -> None:
        self.names = names
        self.returncode = returncode
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.copied: set[str] = set()

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.argv = argv
        self.kwargs = kwargs
        source = Path(argv[-1])
        self.copied = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
        out = Path(argv[argv.index("--wheel-dir") + 1])
        if self.returncode == 0:
            out.mkdir()
            _wheel(out / "x-1-py3-none-any.whl", self.names)
        return subprocess.CompletedProcess(argv, self.returncode, b"a\nsecret-path\n", b"")


def test_main_passes_on_a_complete_wheel_built_from_a_clean_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copy, because an in-place build reuses a stale ``build/`` and can pass falsely."""
    repo = _repo(tmp_path)
    (repo / "build" / "lib").mkdir(parents=True)
    (repo / "build" / "lib" / "stale.txt").write_text("x", encoding="utf-8")
    pip = _FakePip()

    assert check.main(repo, run=pip) == 0

    assert "5 static files" in capsys.readouterr().out
    assert Path(pip.argv[-1]) != repo
    assert pip.kwargs["timeout"] == check.BUILD_TIMEOUT_S
    assert "src/brain/ui/static/fonts/a.woff2" in pip.copied
    assert "pyproject.toml" in pip.copied
    assert not any(name.startswith("build/") for name in pip.copied)
    assert not any("__pycache__" in name for name in pip.copied)


def test_main_exits_1_and_names_the_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    assert check.main(repo, run=_FakePip(names=TREE[:-1])) == 1

    assert "fonts/OFL.txt" in capsys.readouterr().err


def test_a_failed_build_exits_2_with_a_fixed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tail of a build log may hold paths; the exit message does not repeat it."""
    repo = _repo(tmp_path)

    assert check.main(repo, run=_FakePip(returncode=1)) == 2

    assert "exit 1" in capsys.readouterr().err


def test_a_build_that_times_out_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    def hang(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, check.BUILD_TIMEOUT_S)

    assert check.main(repo, run=hang) == 2

    assert "timed out" in capsys.readouterr().err


def test_a_build_with_no_wheel_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)

    def nothing(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    assert check.main(repo, run=nothing) == 2

    assert "exactly one wheel" in capsys.readouterr().err


def test_a_repo_with_no_static_tree_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check.main(tmp_path, run=_FakePip()) == 2

    assert capsys.readouterr().err
