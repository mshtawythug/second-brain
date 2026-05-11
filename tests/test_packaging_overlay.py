"""Regression tests for pyproject.toml package-data declarations for brain.quartz_overrides.

Prevents the broad ``'**/*'`` glob from being re-introduced (which would
ship .pyc bytecode and __pycache__ artefacts as overlay data) and ensures
that every file extension present in the actual overlay tree is covered by
the declared package-data patterns.  Also verifies that the
``exclude-package-data`` guard entry exists so that build-time .pyc
artefacts can be excluded at the setuptools level.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BRAIN_OVERRIDES = REPO_ROOT / "src" / "brain" / "quartz_overrides"

# Python artefacts that must NOT appear as overlay assets in the wheel.
_EXCLUDED_SUFFIXES = {".py", ".pyc"}
_EXCLUDED_PARTS = {"__pycache__"}


def _load_pyproject() -> dict:  # type: ignore[type-arg]
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _overlay_asset_extensions() -> set[str]:
    """Return the set of file extensions in the overlay tree (Python artefacts excluded)."""
    exts: set[str] = set()
    for path in BRAIN_OVERRIDES.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        if any(part in _EXCLUDED_PARTS for part in path.parts):
            continue
        exts.add(path.suffix)
    return exts


def test_package_data_does_not_use_broad_glob() -> None:
    """Regression: brain.quartz_overrides package-data must not use ``'**/*'``.

    The ``'**/*'`` glob matches every file in the overlay directory including
    .pyc bytecode and __pycache__ artefacts, shipping them into the wheel as
    overlay data.  Explicit extension patterns are required instead.
    """
    data = _load_pyproject()
    patterns: list[str] = data["tool"]["setuptools"]["package-data"][
        "brain.quartz_overrides"
    ]
    assert "**/*" not in patterns, (
        "brain.quartz_overrides package-data must not use '**/*' — "
        "use explicit extension patterns to avoid shipping .pyc bytecode"
    )
    assert "*" not in patterns, (
        "brain.quartz_overrides package-data must not use bare '*' — "
        "use explicit extension patterns"
    )


def test_package_data_covers_all_overlay_extensions() -> None:
    """Every file extension in the overlay tree is covered by a package-data pattern.

    If a new file type is added to the overlay (e.g. a .json config) but no
    matching pattern is added to pyproject.toml, that file would be silently
    dropped from wheel installs and pipx deployments.  This test catches
    the drift at development time rather than at install time.
    """
    data = _load_pyproject()
    patterns: list[str] = data["tool"]["setuptools"]["package-data"][
        "brain.quartz_overrides"
    ]
    # Derive the set of extensions that the patterns declare.
    # Each pattern ends in .<ext> (e.g. "**/*.tsx" → suffix ".tsx").
    covered_exts: set[str] = {Path(p).suffix for p in patterns if Path(p).suffix}

    actual_exts = _overlay_asset_extensions()
    missing = actual_exts - covered_exts
    assert not missing, (
        f"Overlay tree contains extensions not covered by package-data patterns: "
        f"{sorted(missing)}\n"
        f"Add matching patterns to [tool.setuptools.package-data] in pyproject.toml."
    )


def test_exclude_package_data_declared_for_overlay() -> None:
    """brain.quartz_overrides has an exclude-package-data entry covering .pyc / __pycache__.

    Setuptools' build-time package scan can create and include
    __pycache__/__init__.cpython-*.pyc even when explicit package-data
    extension patterns are in use (the file comes from setuptools' own package
    import during the build).  The exclude-package-data entry is the
    belt-and-suspenders guard at the setuptools configuration level.
    """
    data = _load_pyproject()
    excludes: dict[str, list[str]] = (
        data.get("tool", {})
        .get("setuptools", {})
        .get("exclude-package-data", {})
    )
    assert "brain.quartz_overrides" in excludes, (
        "expected 'brain.quartz_overrides' in [tool.setuptools.exclude-package-data] — "
        "missing entry means .pyc artefacts may slip through into wheels"
    )
    ex_patterns = excludes["brain.quartz_overrides"]
    assert any("pyc" in p for p in ex_patterns), (
        "expected a *.pyc exclusion pattern in exclude-package-data for "
        "brain.quartz_overrides"
    )
    assert any("__pycache__" in p for p in ex_patterns), (
        "expected a __pycache__ exclusion pattern in exclude-package-data for "
        "brain.quartz_overrides"
    )
