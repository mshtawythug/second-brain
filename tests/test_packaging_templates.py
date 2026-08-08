"""Regression tests for pyproject.toml package-data declarations for brain.templates.

Guards five properties:
1. All four ``__init__.py`` marker files are present so importlib.resources
   can resolve template sub-packages in pipx-installed wheels.
2. Every file extension present in the actual templates tree is covered by a
   declared package-data pattern (catches new file types added without a
   matching pyproject.toml entry).
3. ``importlib.resources.files("brain.templates")`` and its sub-packages are
   loadable end-to-end, and a known file is readable from each.
4. No broad ``'**/*'`` or bare ``'*'`` globs appear in brain.templates*
   package-data patterns (mirrors the quartz_overrides defensive regression).
5. The dev-checkout copies of the internal ``_brain-*-fg`` launchd helpers
   stay byte-identical to their packaged templates — the two-source drift
   that previously slipped past T1.8's user-facing-wrapper audit.
"""
import importlib.resources
import re
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


# ---------------------------------------------------------------------------
# Test 5 — internal _brain-*-fg helpers stay in sync with their templates
# ---------------------------------------------------------------------------

# Pairs the dev-checkout launchd helper with its packaged template.  The two
# files MUST be byte-identical: pipx-installed users execute the template
# (materialised into ``$BRAIN_HOME/.shims/``) while dev-checkout users execute
# the ``bin/`` copy directly under launchd.  Any divergence means the two
# install paths run different code — exactly the drift that earlier T1.8/T1.9
# audits missed because these helpers are leading-underscore "internal" and
# weren't on the user-facing wrapper list.
_FG_HELPER_PAIRS = [
    ("_brain-build-fg", "_brain-build-fg.sh"),
    ("_brain-watcher-fg", "_brain-watcher-fg.sh"),
]


def test_fg_helpers_match_packaged_templates() -> None:
    """Every dev-checkout ``bin/_brain-*-fg`` is byte-identical to its template."""
    bin_dir = REPO_ROOT / "bin"
    tpl_dir = BRAIN_TEMPLATES / "bin"
    for dev_name, tpl_name in _FG_HELPER_PAIRS:
        dev_path = bin_dir / dev_name
        tpl_path = tpl_dir / tpl_name
        assert dev_path.is_file(), f"missing dev-checkout helper: {dev_path}"
        assert tpl_path.is_file(), f"missing packaged template: {tpl_path}"
        dev_bytes = dev_path.read_bytes()
        tpl_bytes = tpl_path.read_bytes()
        assert dev_bytes == tpl_bytes, (
            f"bin/{dev_name} has drifted from src/brain/templates/bin/{tpl_name}.\n"
            f"Both files MUST stay byte-identical so pipx-installed and "
            f"dev-checkout launchd flows execute the same code. Re-sync them "
            f"(usually `cp src/brain/templates/bin/{tpl_name} bin/{dev_name}`)."
        )


# ---------------------------------------------------------------------------
# Test 6 — packaged AGE Dockerfile is shipped + loadable via importlib.resources
# ---------------------------------------------------------------------------


def test_age_dockerfile_packaged_and_loadable() -> None:
    """The custom-image Dockerfile must ship as package data and be readable.

    The rendered ``$BRAIN_HOME/docker-compose.yml`` builds the PG16 + pgvector +
    AGE image from a build context the installer materializes; if the Dockerfile
    is not packaged it cannot be materialized on pipx/wheel installs.
    """
    # On disk in the source tree.
    on_disk = BRAIN_TEMPLATES / "docker" / "age" / "Dockerfile"
    assert on_disk.is_file(), (
        "missing src/brain/templates/docker/age/Dockerfile — canonical packaged "
        "source of the custom AGE image"
    )

    # Reachable via importlib.resources (editable + wheel installs).
    root_pkg = importlib.resources.files("brain.templates")
    text = (root_pkg / "docker" / "age" / "Dockerfile").read_text(encoding="utf-8")
    assert text, "brain.templates/docker/age/Dockerfile is empty or unreadable"
    # Pins must be present + honestly labelled (rc0, not GA).
    assert "pgvector/pgvector:0.8.6-pg16" in text
    assert "PG16/v1.5.0-rc0" in text


# --- AGE image version-pin drift guard -------------------------------------
#
# The published image tag encodes BOTH pins: ``pg16-v<AGE ref>-pgv<pgvector>``.
# ci.yml runs ``docker compose pull || true`` BEFORE ``up``, and only falls back to
# the ``build:`` stanza when the pull fails. So bumping the Dockerfile's FROM without
# bumping the compose ``image:`` tag makes the pull succeed against the OLD image —
# the modified Dockerfile is never built and CI silently validates the previous base
# while every comment and label claims the new one. Same for the publish workflow:
# pushing new base content under a stale ``pgv`` tag mislabels an immutable artifact.
#
# This test is the guard whose absence let a one-line Dependabot FROM bump look
# complete. It reads all three files and asserts a single truthful version.

_AGE_TEST_COMPOSE = REPO_ROOT / "docker-compose.age-test.yml"
_PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-age-image.yml"


def _dockerfile_pins() -> tuple[str, str]:
    """Return ``(pgvector_version, age_ref_version)`` parsed from the AGE Dockerfile."""
    text = (BRAIN_TEMPLATES / "docker" / "age" / "Dockerfile").read_text(encoding="utf-8")

    from_match = re.search(
        r"^FROM\s+pgvector/pgvector:(?P<version>[\w.]+)-pg16\s*$", text, re.MULTILINE
    )
    assert from_match, (
        "could not parse `FROM pgvector/pgvector:<version>-pg16` from the AGE Dockerfile"
    )

    age_match = re.search(r"^ARG\s+AGE_REF=PG16/v(?P<version>[\w.\-]+)\s*$", text, re.MULTILINE)
    assert age_match, "could not parse `ARG AGE_REF=PG16/v<version>` from the AGE Dockerfile"

    return from_match.group("version"), age_match.group("version")


def test_age_image_version_pins_agree_everywhere() -> None:
    """Dockerfile FROM/AGE_REF, the compose ``image:`` tag, and the publish tag agree.

    Failing here means the AGE image version was bumped in one place but not the
    others. The compose tag is what CI actually pulls, so a mismatch is not cosmetic
    drift — it silently runs CI against a different image than the one on disk.
    """
    pgvector_version, age_version = _dockerfile_pins()
    expected_tag = f"pg16-v{age_version}-pgv{pgvector_version}"

    compose = _AGE_TEST_COMPOSE.read_text(encoding="utf-8")
    compose_tag = re.search(
        r"^\s*image:\s*ghcr\.io/[\w\-./]*second-brain-age:(?P<tag>\S+)\s*$", compose, re.MULTILINE
    )
    assert compose_tag, f"no second-brain-age `image:` tag found in {_AGE_TEST_COMPOSE.name}"
    assert compose_tag.group("tag") == expected_tag, (
        f"{_AGE_TEST_COMPOSE.name} pins image tag {compose_tag.group('tag')!r} but the "
        f"Dockerfile builds {expected_tag!r} (pgvector {pgvector_version}, AGE {age_version}). "
        "CI pulls this tag before falling back to `build:`, so a stale tag means CI "
        "silently tests the OLD image. Bump both in the same commit."
    )

    workflow = _PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    publish_tags = re.findall(r"type=raw,value=(?P<tag>pg16-\S+)", workflow)
    assert publish_tags, f"no `type=raw,value=pg16-*` tag found in {_PUBLISH_WORKFLOW.name}"
    for tag in publish_tags:
        assert tag == expected_tag, (
            f"{_PUBLISH_WORKFLOW.name} would publish tag {tag!r} for an image built from "
            f"{expected_tag!r} — that mislabels an immutable artifact."
        )

    # The compose comment and the Dockerfile must not still advertise an older
    # pgvector line in prose while the pins say otherwise.
    stale = re.findall(r"pgvector (?P<version>\d+\.\d+\.\d+)", compose)
    assert all(v == pgvector_version for v in stale), (
        f"{_AGE_TEST_COMPOSE.name} prose mentions pgvector {set(stale)} but pins {pgvector_version}"
    )


def test_age_dockerfile_materialized_into_brain_home(tmp_path: Path) -> None:
    """`materialize_age_dockerfile` copies the packaged Dockerfile into $BRAIN_HOME.

    Asserts the destination is created at ``$BRAIN_HOME/docker/age/Dockerfile`` and
    is byte-identical to the packaged source, so the rendered compose build context
    resolves on a fresh install.
    """
    from brain.setup import materialize_age_dockerfile

    brain_home = tmp_path / "brain_home"
    dest = materialize_age_dockerfile(brain_home)

    assert dest == brain_home / "docker" / "age" / "Dockerfile"
    assert dest.is_file(), "Dockerfile was not materialized into $BRAIN_HOME/docker/age/"

    packaged = (
        importlib.resources.files("brain.templates") / "docker" / "age" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert dest.read_text(encoding="utf-8") == packaged, (
        "materialized Dockerfile drifted from the packaged source"
    )
