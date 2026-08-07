"""House-format contract over every skills/*/SKILL.md.

A skill is loaded by Claude Code purely on its frontmatter: the ``name`` decides
where ``bin/brain-skills-sync`` installs it, and the ``description`` — including
its ``MANDATORY TRIGGERS:`` list — decides when the agent reaches for it. Two
skills claiming the same trigger phrase route the agent to the wrong one, and
nothing at runtime complains. This module is the mechanical guard.

Every skill directory is discovered dynamically (reusing the enumeration helper
from ``tests/test_brain_skills_sync.py``), so a skill added later is covered the
moment it lands — no list to update here. Pure static reads: no database, no
network, no fixtures.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.test_brain_skills_sync import SRC_SKILLS, _expected_skills

# The observed house band is 140-300 lines: below the floor is a stub, above the
# ceiling is an essay the agent will not read carefully.
MIN_SKILL_LINES = 100
MAX_SKILL_LINES = 300

FRONTMATTER_RE = re.compile(r"\A---\n(?P<frontmatter>.*?)\n---\n", re.DOTALL)
TRIGGER_MARKER = "MANDATORY TRIGGERS:"

SKILL_NAMES = sorted(_expected_skills())


def parse_frontmatter(skill_md: Path) -> dict[str, str]:
    """Parse the leading YAML frontmatter block of a SKILL.md into a dict."""
    match = FRONTMATTER_RE.match(skill_md.read_text(encoding="utf-8"))
    assert match, f"{skill_md} has no leading `---` frontmatter block"

    parsed = yaml.safe_load(match.group("frontmatter"))
    assert isinstance(parsed, dict), f"{skill_md} frontmatter is not a YAML mapping"

    return {str(key): str(value) for key, value in parsed.items()}


def _skill_md(name: str) -> Path:
    return SRC_SKILLS / name / "SKILL.md"


def _body(skill_md: Path) -> str:
    """Everything after the frontmatter block."""
    text = skill_md.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    assert match, f"{skill_md} has no leading `---` frontmatter block"
    return text[match.end() :]


def _headings(body: str, level: str) -> list[str]:
    """Headings at ``level`` that sit outside fenced code blocks.

    Shell comments inside ``` fences start with `# ` too, so a naive scan counts
    `# edit .env to set BRAIN_EMBEDDER=...` as an H1.
    """
    prefix = f"{level} "
    inside_fence = False
    found: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence and line.startswith(prefix):
            found.append(line)
    return found


def _trigger_phrases(description: str) -> set[str]:
    """The comma-separated phrases following ``MANDATORY TRIGGERS:``."""
    index = description.find(TRIGGER_MARKER)
    if index < 0:
        return set()
    tail = description[index + len(TRIGGER_MARKER) :]
    return {
        phrase.strip().strip(".").casefold() for phrase in tail.split(",") if phrase.strip(". \n")
    }


# ---------------------------------------------------------------------------
# Per-skill contract
# ---------------------------------------------------------------------------


def test_at_least_one_skill_is_discovered() -> None:
    """Guard the guard: an empty enumeration would make every test below vacuous."""
    assert SKILL_NAMES, "no skills/*/ directories discovered"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_every_skill_dir_has_a_skill_md(skill_name: str) -> None:
    assert _skill_md(skill_name).is_file(), f"skills/{skill_name}/ has no SKILL.md"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_frontmatter_parses_and_has_name_and_description(skill_name: str) -> None:
    frontmatter = parse_frontmatter(_skill_md(skill_name))

    assert frontmatter.get("name"), f"{skill_name}: frontmatter has no `name`"
    assert frontmatter.get("description"), f"{skill_name}: frontmatter has no `description`"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_name_matches_directory(skill_name: str) -> None:
    """bin/brain-skills-sync installs by directory name; a mismatch misroutes it."""
    frontmatter = parse_frontmatter(_skill_md(skill_name))

    assert frontmatter["name"] == skill_name, (
        f"skills/{skill_name}/SKILL.md declares name={frontmatter['name']!r} — "
        "it must equal the directory name"
    )


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_description_declares_mandatory_triggers(skill_name: str) -> None:
    description = parse_frontmatter(_skill_md(skill_name))["description"]

    assert TRIGGER_MARKER in description, (
        f"{skill_name}: description has no `{TRIGGER_MARKER}` list — the agent has "
        "no phrases to route on"
    )
    assert _trigger_phrases(description), f"{skill_name}: `{TRIGGER_MARKER}` list is empty"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_body_has_an_h1(skill_name: str) -> None:
    body = _body(_skill_md(skill_name))
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    h1s = _headings(body, "#")

    assert first_line.startswith(
        "# "
    ), f"{skill_name}: body must open with an `# ` H1; found {first_line[:60]!r}"
    assert len(h1s) == 1, f"{skill_name}: expected exactly one H1, found {len(h1s)}: {h1s}"


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_length_is_within_house_range(skill_name: str) -> None:
    line_count = len(_skill_md(skill_name).read_text(encoding="utf-8").splitlines())

    assert MIN_SKILL_LINES <= line_count <= MAX_SKILL_LINES, (
        f"{skill_name}: SKILL.md is {line_count} lines; the house band is "
        f"{MIN_SKILL_LINES}-{MAX_SKILL_LINES} (below is a stub, above is an essay)"
    )


# ---------------------------------------------------------------------------
# Cross-skill routing
# ---------------------------------------------------------------------------


def test_trigger_phrases_are_unique_across_skills() -> None:
    """Two skills claiming one phrase route the agent to whichever wins the toss."""
    owners: dict[str, list[str]] = {}
    for skill_name in SKILL_NAMES:
        description = parse_frontmatter(_skill_md(skill_name))["description"]
        # Deduplicate within a skill first (``_trigger_phrases`` returns a set):
        # a phrase repeated inside one description is untidy, but it is not a
        # routing collision.
        for phrase in _trigger_phrases(description):
            owners.setdefault(phrase, []).append(skill_name)

    collisions = {phrase: names for phrase, names in owners.items() if len(names) > 1}

    assert not collisions, (
        "trigger phrases claimed by more than one skill — the agent cannot route "
        f"deterministically: {collisions}"
    )
