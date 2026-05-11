"""Regression tests for pyproject.toml package-data declarations for brain.templates.

Guards four properties:
1. All four ``__init__.py`` marker files are present so importlib.resources
   can resolve template sub-packages in pipx-installed wheels.
2. Every file extension present in the actual templates tree is covered by a
   declared package-data pattern (catches new file types added without a
   matching pyproject.toml entry).
3. ``importlib.resources.files("brain.templates")`` and its sub-packages are
   loadable end-to-end, and a known file is readable from each.
4. No broad ``'**/*'`` or bare ``'*'`` globs appear in brain.templates*
   package-data patterns (mirrors the quartz_overrides defensive regression).
"""
from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BRAIN_TEMPLATES = REPO_ROOT / "src" / "brain" / "templates"

# Python artefacts that must NOT appear as template assets in the wheel.
_EXCLUDED_SUFFIXES = {".py", ".pyc"}
_EXCLUDED_PARTS = {"__pycache__"}


def _load_pyproject() -> dict:  # type: ignore[type-arg]
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _template_asset_extensions() -> set[str]:
    """Return file extensions in the templates tree (Python artefacts excluded)."""
    exts: set[str] = set()
    for path in BRAIN_TEMPLATES.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        if any(part in _EXCLUDED_PARTS for part in path.parts):
            continue
        # Files with no suffix (like "env.example") — treat the full name as the key.
        # We only need to verify coverage, so we handle them separately below.
        exts.add(path.suffix if path.suffix else path.name)
    return exts


def _all_template_patterns(data: dict) -> list[str]:  # type: ignore[type-arg]
    """Collect every pattern declared under brain.templates* package-data keys."""
    pkg_data: dict[str, list[str]] = data["tool"]["setuptools"]["package-data"]
    patterns: list[str] = []
    for key, pats in pkg_data.items():
        if key.startswith("brain.templates"):
            patterns.extend(pats)
    return patterns


# ---------------------------------------------------------------------------
# Test 1 — __init__.py marker files must exist
# ---------------------------------------------------------------------------


def test_templates_init_markers_present() -> None:
    """All four __init__.py marker files must be present for importlib.resources."""
    expected = [
        BRAIN_TEMPLATES / "__init__.py",
        BRAIN_TEMPLATES / "bin" / "__init__.py",
        BRAIN_TEMPLATES / "launchd" / "__init__.py",
        BRAIN_TEMPLATES / "skill" / "__init__.py",
    ]
    for marker in expected:
        assert marker.exists(), (
            f"Missing __init__.py marker at {marker.relative_to(REPO_ROOT)} — "
            "importlib.resources cannot resolve this sub-package in pipx-installed wheels"
        )


# ---------------------------------------------------------------------------
# Test 2 — package-data covers every extension in the tree
# ---------------------------------------------------------------------------


def test_templates_package_data_covers_all_files() -> None:
    """Every file extension / name in the templates tree has a matching pyproject pattern.

    If a new file type is added to src/brain/templates/ without a corresponding
    pyproject.toml entry, this test will fail at development time rather than
    silently dropping the file from wheel/pipx installs.
    """
    data = _load_pyproject()
    patterns = _all_template_patterns(data)

    # Build the set of covered extensions from the declared patterns.
    # "*.sh" → suffix ".sh"; "env.example" → exact filename "env.example".
    covered: set[str] = set()
    for p in patterns:
        stem = Path(p)
        if stem.suffix:
            covered.add(stem.suffix)
        else:
            # bare filename like "env.example" — no suffix key, add full name
            covered.add(stem.name)

    actual = _template_asset_extensions()
    missing = actual - covered
    assert not missing, (
        f"Templates tree contains extensions/names not covered by package-data patterns: "
        f"{sorted(missing)}\n"
        f"Add matching patterns to [tool.setuptools.package-data] in pyproject.toml."
    )


# ---------------------------------------------------------------------------
# Test 3 — importlib.resources can load files from every sub-package
# ---------------------------------------------------------------------------


def test_templates_loadable_via_importlib_resources() -> None:
    """importlib.resources.files() must resolve all four template sub-packages.

    Reads a known file from each sub-package to confirm the resource is reachable
    in both editable-install and wheel-install modes.
    """
    # Root package: env.example
    root_pkg = importlib.resources.files("brain.templates")
    env_text = (root_pkg / "env.example").read_text(encoding="utf-8")
    assert env_text, "brain.templates/env.example is empty or unreadable"

    # Root package: a .j2 file
    docker_j2 = (root_pkg / "docker-compose.yml.j2").read_text(encoding="utf-8")
    assert docker_j2, "brain.templates/docker-compose.yml.j2 is empty or unreadable"

    # bin sub-package: one of the shell scripts
    bin_pkg = importlib.resources.files("brain.templates.bin")
    brain_up_sh = (bin_pkg / "brain-up.sh").read_text(encoding="utf-8")
    assert brain_up_sh, "brain.templates.bin/brain-up.sh is empty or unreadable"

    # launchd sub-package: one of the plist templates
    launchd_pkg = importlib.resources.files("brain.templates.launchd")
    watcher_j2 = (launchd_pkg / "com.brain.watcher.plist.j2").read_text(encoding="utf-8")
    assert watcher_j2, "brain.templates.launchd/com.brain.watcher.plist.j2 is empty or unreadable"

    # skill sub-package: SKILL.md
    skill_pkg = importlib.resources.files("brain.templates.skill")
    skill_md = (skill_pkg / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md, "brain.templates.skill/SKILL.md is empty or unreadable"


# ---------------------------------------------------------------------------
# Test 4 — no broad glob patterns in brain.templates* package-data
# ---------------------------------------------------------------------------


def test_no_broad_template_glob() -> None:
    """brain.templates* package-data must not use ``'**/*'`` or bare ``'*'``.

    Broad globs would ship .pyc bytecode and __pycache__ artefacts into the
    wheel.  Explicit extension patterns are required instead.
    """
    data = _load_pyproject()
    patterns = _all_template_patterns(data)
    assert "**/*" not in patterns, (
        "brain.templates* package-data must not use '**/*' — "
        "use explicit extension patterns to avoid shipping .pyc bytecode"
    )
    assert "*" not in patterns, (
        "brain.templates* package-data must not use bare '*' — "
        "use explicit extension patterns"
    )
