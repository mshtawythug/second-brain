"""Presence and shape of the standard open-source repository files.

SECURITY.md, CODE_OF_CONDUCT.md, .github/dependabot.yml, .github/CODEOWNERS and
CHANGELOG.md are the surface a first-time contributor meets. Each is easy to add
once and then let rot — a placeholder left in, a link pointing at a tag that was
never cut, a dependency ecosystem quietly dropped. Every assertion here is a
pure static read of a tracked file: no database, no network, no fixtures.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent

SECURITY_MD = Path("SECURITY.md")
CODE_OF_CONDUCT_MD = Path("CODE_OF_CONDUCT.md")
CHANGELOG_MD = Path("CHANGELOG.md")
DEPENDABOT_YML = Path(".github/dependabot.yml")
CODEOWNERS = Path(".github/CODEOWNERS")

# A real address. `@mshtawythug` is a GitHub handle, not an address, and carries
# no dot-suffixed domain, so it cannot match — which is exactly the point: the
# handle is already public in every repo URL, an email would be new PII.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# `[0.2.1]: https://github.com/<owner>/<repo>/releases/tag/v0.2.1`
RELEASE_LINK_RE = re.compile(
    r"(?m)^\[(?P<version>\d+\.\d+\.\d+)\]:.*?/releases/tag/(?P<tag>\S+)\s*$"
)

# `## [0.2.1] - 2026-07-20` or `## [Unreleased]`
VERSION_HEADING_RE = re.compile(r"(?m)^##\s+\[(?P<label>[^\]]+)\]")


def read_repo_file(relative: Path) -> str:
    """Read a repo-root-relative text file, UTF-8."""
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _exists(relative: Path) -> bool:
    return (REPO_ROOT / relative).is_file()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ---------------------------------------------------------------------------
# SECURITY.md
# ---------------------------------------------------------------------------


def test_security_md_exists() -> None:
    assert _exists(SECURITY_MD), "No SECURITY.md — nowhere to report a vulnerability privately"


def test_security_md_has_required_sections() -> None:
    text = read_repo_file(SECURITY_MD).lower()

    assert "## supported versions" in text
    assert "## reporting a vulnerability" in text
    assert "## what to expect" in text


def test_security_md_uses_private_reporting_not_an_email() -> None:
    """No maintainer address may be committed (CLAUDE.md rule 15)."""
    text = read_repo_file(SECURITY_MD)
    found = EMAIL_RE.findall(text)

    assert "private vulnerability reporting" in text.lower()
    assert not found, (
        f"SECURITY.md publishes an email address: {found} — "
        "route reports through GitHub private reporting instead"
    )


def test_security_md_documents_the_local_first_model() -> None:
    """A report is only actionable if the reader knows where the boundaries are."""
    text = read_repo_file(SECURITY_MD).lower()

    assert "55432" in text, "SECURITY.md does not mention the local Postgres port"
    assert "no authentication" in text, "SECURITY.md does not say the wiki is unauthenticated"
    assert "stdio" in text, "SECURITY.md does not describe brain-mcp as a stdio server"


# ---------------------------------------------------------------------------
# CODE_OF_CONDUCT.md
# ---------------------------------------------------------------------------


def test_code_of_conduct_exists_and_is_covenant_21() -> None:
    assert _exists(CODE_OF_CONDUCT_MD), "No CODE_OF_CONDUCT.md"
    text = read_repo_file(CODE_OF_CONDUCT_MD)

    assert "Contributor Covenant" in text
    assert "2.1" in text, "CODE_OF_CONDUCT.md does not name the Covenant version"
    assert (
        "creativecommons.org" in text
    ), "The Contributor Covenant is CC BY 4.0 — the attribution footer must be retained"


def test_code_of_conduct_has_no_placeholder() -> None:
    assert "[INSERT CONTACT METHOD]" not in read_repo_file(CODE_OF_CONDUCT_MD)


def test_code_of_conduct_publishes_no_email() -> None:
    """Same no-PII rule as SECURITY.md: enforcement routes through GitHub."""
    found = EMAIL_RE.findall(read_repo_file(CODE_OF_CONDUCT_MD))

    assert not found, f"CODE_OF_CONDUCT.md publishes an email address: {found}"


# ---------------------------------------------------------------------------
# .github/dependabot.yml
# ---------------------------------------------------------------------------


def _dependabot_updates() -> list[dict[str, object]]:
    parsed = yaml.safe_load(read_repo_file(DEPENDABOT_YML))
    assert isinstance(parsed, dict), "dependabot.yml did not parse as a mapping"
    updates = parsed.get("updates")
    assert isinstance(updates, list), "dependabot.yml has no `updates:` list"
    return [entry for entry in updates if isinstance(entry, dict)]


def test_dependabot_config_covers_all_three_ecosystems() -> None:
    assert _exists(DEPENDABOT_YML), "No .github/dependabot.yml"
    ecosystems = {entry.get("package-ecosystem") for entry in _dependabot_updates()}

    assert {
        "pip",
        "github-actions",
        "docker",
    } <= ecosystems, f"dependabot.yml is missing an ecosystem; found {sorted(map(str, ecosystems))}"


def test_dependabot_docker_directory_points_at_a_real_dockerfile() -> None:
    """The canonical Dockerfile lives inside the package, not at the repo root."""
    docker_entries = [
        entry for entry in _dependabot_updates() if entry.get("package-ecosystem") == "docker"
    ]

    assert docker_entries, "dependabot.yml has no docker entry"
    for entry in docker_entries:
        directory = str(entry.get("directory", "")).lstrip("/")
        assert (
            REPO_ROOT / directory / "Dockerfile"
        ).is_file(), f"dependabot docker directory {entry.get('directory')!r} has no Dockerfile"


def test_dependabot_sets_pr_limits() -> None:
    for entry in _dependabot_updates():
        assert "open-pull-requests-limit" in entry, (
            f"dependabot {entry.get('package-ecosystem')!r} entry has no PR limit — "
            "the queue is unbounded"
        )


# ---------------------------------------------------------------------------
# .github/CODEOWNERS
# ---------------------------------------------------------------------------


def test_codeowners_exists_and_covers_migrations_and_workflows() -> None:
    assert _exists(CODEOWNERS), "No .github/CODEOWNERS"
    rules = [
        line.split()
        for line in read_repo_file(CODEOWNERS).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    patterns = {rule[0] for rule in rules}

    assert all(len(rule) >= 2 for rule in rules), "every CODEOWNERS rule needs at least one owner"
    assert "*" in patterns, "CODEOWNERS has no default `*` rule"
    assert (
        "/src/brain/migrations/" in patterns
    ), "migrations are append-only forever — review must never be optional"
    assert (
        "/.github/workflows/" in patterns
    ), "workflows hold the PyPI Trusted Publishing and GHCR credential paths"


# ---------------------------------------------------------------------------
# CHANGELOG.md
# ---------------------------------------------------------------------------


def _changelog_labels() -> list[str]:
    return [m.group("label") for m in VERSION_HEADING_RE.finditer(read_repo_file(CHANGELOG_MD))]


def test_changelog_has_unreleased_section() -> None:
    labels = _changelog_labels()

    assert labels, "CHANGELOG.md has no `## [...]` version headings"
    assert (
        labels[0] == "Unreleased"
    ), f"`## [Unreleased]` must sit above the newest released version; found {labels[0]!r} first"


def test_changelog_link_definitions_resolve_to_real_tags() -> None:
    """A link to a tag that was never cut 404s for every reader."""
    tags = set(_git("tag", "--list").split())
    dangling = [
        (m.group("version"), m.group("tag"))
        for m in RELEASE_LINK_RE.finditer(read_repo_file(CHANGELOG_MD))
        if m.group("tag") not in tags
    ]

    assert not dangling, (
        f"CHANGELOG.md links release tags that do not exist: {dangling}. "
        "Drop the link definition rather than back-dating a tag."
    )


def test_changelog_unreleased_link_targets_the_newest_tag() -> None:
    text = read_repo_file(CHANGELOG_MD)
    released = [label for label in _changelog_labels() if label[0].isdigit()]

    assert released, "CHANGELOG.md documents no released version"
    assert "[Unreleased]:" in text, "CHANGELOG.md has no `[Unreleased]` link definition"
    assert (
        f"compare/v{released[0]}...HEAD" in text
    ), f"the `[Unreleased]` link should compare against the newest tag v{released[0]}"


# ---------------------------------------------------------------------------
# Docs must actually be trackable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_path",
    sorted(p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "docs").glob("*.md")),
)
def test_no_new_untracked_docs_paths(doc_path: str) -> None:
    """A publishable doc must not be silently swallowed by `.gitignore`.

    ``docs/`` is private-by-subdir: whole directories (``docs/plans/``,
    ``docs/specs/``, ...) are local-only working notes, and a handful of
    individual files are excluded by name. Both of those are deliberate, so the
    assertion is not "nothing under docs/ is ignored" — it is that any ignored
    ``docs/*.md`` is matched by a rule naming **that exact path**. A broad
    pattern reaching a top-level doc is the silent-swallow bug this guards
    against.

    Checked with ``git check-ignore`` rather than ``git ls-files`` on purpose: a
    doc that is merely *uncommitted* is fine, a doc that git would refuse to add
    is not.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", "--", doc_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if ignored.returncode != 0:
        return  # not ignored — tracked, or simply new and uncommitted

    # `<gitignore file>:<line>:<pattern>\t<path>`
    pattern = ignored.stdout.strip().split("\t", 1)[0].rsplit(":", 1)[-1]

    assert pattern == doc_path, (
        f"{doc_path} is swallowed by the broad .gitignore pattern {pattern!r} and can never be "
        "added. Deliberate exclusions must name the file explicitly."
    )
