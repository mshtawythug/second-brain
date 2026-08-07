"""Content contract over the three skills added by Task 1C.

``tests/test_skill_frontmatter.py`` enforces the generic house format on every
``skills/*/`` directory (frontmatter shape, name/directory match, one H1, the
line band, cross-skill trigger uniqueness). This module adds what is specific
to ``brain-proactivity`` / ``brain-ask`` / ``brain-capture``:

* each one carries an explicit safety section — all three document commands
  that delete, mutate a vault file, or write to disk;
* the locked trigger split holds — ``brain-capture`` must not poach
  ``remember this`` (ceded to ``brain-memory``) or ``add this to my brain``
  (already ``ingest-brain``'s), and the capture phrases must have *moved* out
  of ``brain-proactivity`` rather than being duplicated into both;
* **every ``brain`` command and flag shown in a fenced ``bash`` block really
  exists.** A skill naming a command the CLI does not have is worse than no
  skill: the agent will confidently run it and fail in front of the user. The
  Typer app is the oracle here, so this tracks the CLI automatically.

Pure static reads plus in-process Typer introspection: no database, no
network, no fixtures.
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer.main

from brain.cli import app as brain_app

from .test_brain_skills_sync import SRC_SKILLS
from .test_skill_frontmatter import parse_frontmatter

#: The three skills this task owns.
NEW_SKILLS = ("brain-ask", "brain-capture", "brain-proactivity")

#: Locked in the plan's "Skill trigger-phrase split" — ``brain-capture`` owns
#: these and nothing else may claim them.
CAPTURE_TRIGGERS = frozenset(
    {
        "capture this",
        "jot this down",
        "quick note",
        "note to self",
        "add to my inbox",
        "dump this in my brain",
        "capture idea",
        "my inbox",
        "review my inbox",
        "process my inbox",
        "triage my captures",
        "what's in my inbox",
        "brain capture",
    }
)

#: Ceded to other skills by the same locked split. ``remember this`` belongs to
#: ``brain-memory`` (the agent-memory protocol); ``add this to my brain`` has
#: been ``ingest-brain``'s since before this release.
FORBIDDEN_CAPTURE_TRIGGERS = frozenset({"remember this", "add this to my brain"})

SAFETY_HEADINGS = ("## Safety rules", "## Operational notes")

BASH_FENCE_RE = re.compile(r"^```bash\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE)


def _skill_md(name: str) -> Path:
    return SRC_SKILLS / name / "SKILL.md"


def _text(name: str) -> str:
    return _skill_md(name).read_text(encoding="utf-8")


def _triggers(name: str) -> set[str]:
    """The comma-separated phrases after ``MANDATORY TRIGGERS:``, casefolded."""
    description = parse_frontmatter(_skill_md(name))["description"]
    _, _, tail = description.partition("MANDATORY TRIGGERS:")
    return {p.strip().strip(".").casefold() for p in tail.split(",") if p.strip(". \n")}


# ---------------------------------------------------------------------------
# Presence and safety sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", NEW_SKILLS)
def test_new_skill_exists(skill_name: str) -> None:
    assert _skill_md(skill_name).is_file(), f"skills/{skill_name}/SKILL.md is missing"


@pytest.mark.parametrize("skill_name", NEW_SKILLS)
def test_new_skills_declare_safety_rules(skill_name: str) -> None:
    """All three document destructive or disk-writing commands."""
    text = _text(skill_name)

    assert any(heading in text for heading in SAFETY_HEADINGS), (
        f"{skill_name}: no {' / '.join(SAFETY_HEADINGS)} section — every one of "
        "these skills documents a command that deletes, mutates a vault file, "
        "or writes to disk"
    )


def test_capture_skill_carries_the_review_incident_prohibition() -> None:
    """The 2026-06-09 incident: a piped `d`/`y` deleted a real note."""
    text = _text("brain-capture").casefold()

    assert "never pipe" in text, "brain-capture must forbid piping blind responses"
    assert "capture review" in text
    assert "inbox order" in text, (
        "brain-capture must explain WHY piping is unsafe: --limit selects by "
        "inbox order, not relevance"
    )


def test_proactivity_skill_flags_the_vault_write_and_body_rules() -> None:
    text = _text("brain-proactivity").casefold()

    assert "connect accept --write" in text, "the vault-mutating flag must be called out"
    assert "never" in text and "bod" in text, "the brief titles-only rule must be stated"


def test_ask_skill_flags_the_gitignored_audio_directory() -> None:
    text = _text("brain-ask").casefold()

    assert "audio/" in text
    assert "commit" in text, "generated audio artifacts must be marked never-commit"


# ---------------------------------------------------------------------------
# The locked trigger split
# ---------------------------------------------------------------------------


def test_capture_skill_does_not_claim_ingest_triggers() -> None:
    """`remember this` is `brain-memory`'s; `add this to my brain` is `ingest-brain`'s."""
    claimed = _triggers("brain-capture")

    poached = claimed & FORBIDDEN_CAPTURE_TRIGGERS
    assert not poached, (
        f"brain-capture claims {sorted(poached)}, which the locked trigger "
        "split cedes to brain-memory / ingest-brain"
    )


def test_capture_skill_claims_its_locked_triggers() -> None:
    claimed = _triggers("brain-capture")

    missing = CAPTURE_TRIGGERS - claimed
    assert not missing, f"brain-capture is missing locked triggers: {sorted(missing)}"


def test_capture_triggers_moved_out_of_proactivity() -> None:
    """The split must MOVE the phrases, not duplicate them into both skills."""
    leftovers = _triggers("brain-proactivity") & CAPTURE_TRIGGERS

    assert not leftovers, (
        f"brain-proactivity still claims capture triggers {sorted(leftovers)} — "
        "they belong to brain-capture now"
    )


@pytest.mark.parametrize("skill_name", NEW_SKILLS)
def test_new_skill_cross_references_a_sibling(skill_name: str) -> None:
    """Each skill must disclaim the others' territory, as the family does."""
    text = _text(skill_name)
    siblings = [
        other
        for other in ("brain-proactivity", "brain-ask", "brain-capture", "consult-brain")
        if other != skill_name
    ]

    assert any(other in text for other in siblings), (
        f"{skill_name}: names no sibling skill — routing needs an explicit "
        "'use X instead' pointer"
    )


# ---------------------------------------------------------------------------
# Every documented command and flag really exists
# ---------------------------------------------------------------------------


def _command_lines(text: str) -> list[str]:
    """Every ``brain …`` invocation inside a fenced ``bash`` block."""
    lines: list[str] = []
    for fence in BASH_FENCE_RE.finditer(text):
        for raw in fence.group("body").splitlines():
            line = raw.split("#", 1)[0].strip().lstrip("$ ").strip()
            # Take the segment after the last pipe, so `echo … | brain capture`
            # is checked as the `brain capture` invocation it is.
            segment = line.rsplit("|", 1)[-1].strip()
            if segment.split(" ", 1)[:1] == ["brain"]:
                lines.append(segment)
    return lines


def _resolve(tokens: list[str]) -> tuple[click.Command, list[str]]:
    """Walk the Typer command tree; return the deepest command plus the rest."""
    node: click.Command = typer.main.get_command(brain_app)
    index = 0
    while index < len(tokens) and isinstance(node, click.Group):
        child = node.commands.get(tokens[index])
        if child is None:
            break
        node = child
        index += 1
    return node, tokens[index:]


def _declared_opts(command: click.Command) -> set[str]:
    opts: set[str] = set()
    for param in command.params:
        opts.update(param.opts)
        opts.update(param.secondary_opts)
    return opts


@pytest.mark.parametrize("skill_name", NEW_SKILLS)
def test_documented_commands_exist(skill_name: str) -> None:
    """A skill naming a command the CLI lacks sends the agent into a dead end."""
    root = typer.main.get_command(brain_app)
    assert isinstance(root, click.Group)

    for line in _command_lines(_text(skill_name)):
        tokens = line.split()[1:]  # drop the leading `brain`
        assert tokens, f"{skill_name}: bare `brain` invocation in an example"
        first = tokens[0]
        if first.startswith("-"):
            continue  # `brain --help` and friends
        assert first in root.commands, (
            f"{skill_name}: documents `brain {first}`, which is not a "
            f"registered command. Line: {line!r}"
        )


@pytest.mark.parametrize("skill_name", NEW_SKILLS)
def test_documented_flags_exist(skill_name: str) -> None:
    """Every long/short flag in an example must be declared by that command."""
    for line in _command_lines(_text(skill_name)):
        tokens = line.split()[1:]
        if not tokens or tokens[0].startswith("-"):
            continue
        command, remainder = _resolve(tokens)
        depth = len(tokens) - len(remainder)
        declared = _declared_opts(command) | {"--help"}
        for token in remainder:
            if not token.startswith("-") or token == "-":
                continue
            flag = token.split("=", 1)[0]
            assert flag in declared, (
                f"{skill_name}: `{flag}` is not a flag of "
                f"`brain {' '.join(tokens[:depth])}`. Line: {line!r}"
            )
