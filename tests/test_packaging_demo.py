"""Regression tests for pyproject.toml package-data declarations for brain.demo.

Mirrors ``test_packaging_templates.py``. Guards that the synthetic `brain demo`
corpus manifest ships in the wheel and stays resolvable via
``importlib.resources`` on pipx/wheel installs:

1. Every file extension in the corpus tree is covered by a declared
   package-data pattern (catches a new asset type added without a pyproject entry).
2. ``importlib.resources.files("brain.demo") / "corpus" / "manifest.json"`` is
   loadable end-to-end and parses as the 22-doc corpus.
3. No broad ``'**/*'`` or bare ``'*'`` globs appear in the brain.demo pattern
   (mirrors the templates/quartz defensive regressions — those ship .pyc).
"""
import importlib.resources
import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BRAIN_DEMO = REPO_ROOT / "src" / "brain" / "demo"
CORPUS_DIR = BRAIN_DEMO / "corpus"

# Python artefacts that must NOT appear as demo assets in the wheel.
_EXCLUDED_SUFFIXES = {".py", ".pyc"}
_EXCLUDED_PARTS = {"__pycache__"}


def _load_pyproject() -> dict:  # type: ignore[type-arg]
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _demo_asset_extensions() -> set[str]:
    """Return file extensions in the corpus tree (Python artefacts excluded)."""
    exts: set[str] = set()
    for path in CORPUS_DIR.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        if any(part in _EXCLUDED_PARTS for part in path.parts):
            continue
        exts.add(path.suffix if path.suffix else path.name)
    return exts


def _demo_patterns(data: dict) -> list[str]:  # type: ignore[type-arg]
    """Collect every pattern declared under brain.demo* package-data keys."""
    pkg_data: dict[str, list[str]] = data["tool"]["setuptools"]["package-data"]
    patterns: list[str] = []
    for key, pats in pkg_data.items():
        if key.startswith("brain.demo"):
            patterns.extend(pats)
    return patterns


def test_demo_package_data_covers_all_corpus_files() -> None:
    """Every corpus file extension has a matching pyproject package-data pattern."""
    data = _load_pyproject()
    patterns = _demo_patterns(data)
    assert patterns, "brain.demo has no package-data patterns in pyproject.toml"

    covered: set[str] = set()
    for p in patterns:
        stem = Path(p)
        covered.add(stem.suffix if stem.suffix else stem.name)

    missing = _demo_asset_extensions() - covered
    assert not missing, (
        f"corpus tree has extensions not covered by package-data patterns: "
        f"{sorted(missing)}\nAdd matching patterns to [tool.setuptools.package-data]."
    )


def test_demo_corpus_loadable_via_importlib_resources() -> None:
    """importlib.resources must resolve the corpus manifest in wheel installs."""
    resource = importlib.resources.files("brain.demo") / "corpus" / "manifest.json"
    text = resource.read_text(encoding="utf-8")
    assert text, "brain.demo/corpus/manifest.json is empty or unreadable"
    records = json.loads(text)
    assert isinstance(records, list) and len(records) == 22


def test_no_broad_demo_glob() -> None:
    """brain.demo package-data must not use ``'**/*'`` or bare ``'*'``.

    Broad globs would ship .pyc bytecode + __pycache__ into the wheel.
    """
    data = _load_pyproject()
    patterns = _demo_patterns(data)
    assert "**/*" not in patterns, "brain.demo must not use '**/*' — use explicit patterns"
    assert "*" not in patterns, "brain.demo must not use bare '*' — use explicit patterns"
