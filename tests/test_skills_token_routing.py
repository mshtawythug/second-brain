"""Guards that the shipped agent skills route to token-bounded retrieval.

The `brain` CLI has one surface with a hard token ceiling — `brain recall
--budget N`. A skill that teaches an agent to retrieve, but never names it,
leaves the agent with only unbounded options: `brain search --limit 20`
followed by a `brain show` per hit is ~91,000 tokens for one question by
arithmetic (20 results x a mean 18,218-char body) and a *measured* mean of
183,940 tokens on this corpus — see
``docs/audits/2026-08-11-wave2-routing-counterfactual.md``.

**"Shipped skills" means two sets, not one.** `skills/*/SKILL.md` is what
`bin/brain-skills-sync` installs for anyone with a checkout;
`src/brain/templates/skill/SKILL.md` is the single packaged cheat-sheet
`brain claude install-skill` writes, and it is the ONLY skill a `pipx` / `uvx`
user ever gets. A guard that covered the first set alone would leave the
packaged audience unrouted and unguarded — the exact hole this file exists to
close — so every routing assertion below runs over the union.

These tests assert the *documentation* routes to the cheap path. They cannot
assert an agent obeys it — nothing in this repo measures that.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SYNC_SCRIPT = REPO_ROOT / "bin" / "brain-skills-sync"
CONSULT_BRAIN = SKILLS_DIR / "consult-brain" / "SKILL.md"

#: The packaged single-file cheat-sheet `brain claude install-skill` writes
#: (``cli_claude.py`` resolves it from package data and copies bytes).
PACKAGED_SKILL = REPO_ROOT / "src" / "brain" / "templates" / "skill" / "SKILL.md"

#: The measured cost of the unbounded procedure. Stated in exactly two files
#: (see :func:`test_measured_ceiling_lives_in_exactly_two_files`) so a
#: correction is a two-file edit and not a five-file scavenger hunt.
MEASURED_MEAN = "183,940"

#: Matches the figure AND its rounded restatements ("~184,000"), which is how
#: it leaked into three siblings before. Any six-figure number in the 180k
#: band inside a skill is this claim wearing a different hat.
_MEAN_VARIANTS = re.compile(r"\b18[0-9],\d{3}\b")

#: Every skill that must name `brain recall`. Pinned explicitly rather than
#: derived, because the derivation (`"brain search" in text`) is evaluated at
#: collection time: a skill that switched to MCP phrasing, or was renamed,
#: would drop out of the parameterization and the suite would stay green with
#: fewer cases. Add a name here when a new skill starts teaching retrieval.
EXPECTED_RETRIEVAL_SKILLS = frozenset(
    {
        "brain-ask",
        "brain-authoring",
        "brain-capture",
        "brain-graph",
        "brain-memory",
        "brain-proactivity",
        "consult-brain",
        "elicit-brain",
        "ingest-brain",
        "templates/skill",  # the packaged cheat-sheet
    }
)

#: Phrases that mark a code block (or the prose introducing it) as iterating
#: over a result set. Matched case-insensitively.
_LOOP_MARKERS = (
    "for each",
    "per doc",
    "per result",
    "per hit",
    "each top",
    "each hit",
    "each result",
    "every doc",
    "every result",
    "repeat for",
    "repeat per",
    "in $ids",
    "; do ",
)

#: Words that flip a loop phrase from an instruction into a prohibition
#: ("Never `brain show` every result"). Without this, the prose that *states*
#: the no-loop rule would be read as teaching it.
#:
#: **Scope is deliberately narrow.** A negation exempts only the unit it shares
#: — its own sentence in prose, its own line in a code block (see
#: :func:`_loop_markers_in_lead` / :func:`_loop_markers_in_block`). Applied to a
#: whole block or a whole paragraph, two of the commonest words in English
#: ("only", "not ") would let a live recipe buy immunity from an unrelated
#: comment nearby: ``brain show <id> --json  # only the ones that matter`` under
#: a ``# for each top doc:`` header evaded this guard entirely before the scope
#: was tightened. The residual hole is a marker and a negation on the *same*
#: line — narrow, and it is the case the exemption exists for.
_NEGATIONS = (
    "never",
    "not ",
    "n't",
    "rather than",
    "instead",
    "at most",
    "ceiling",
    "avoid",
    "only",
)

#: Fenced code block, capturing the body. Language tag optional.
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _repo_skill_files() -> list[Path]:
    """Every SKILL.md under ``skills/`` — what `brain-skills-sync` installs."""
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _routing_files() -> list[Path]:
    """Every skill an agent can end up reading: repo skills + the packaged one."""
    return [*_repo_skill_files(), PACKAGED_SKILL]


def _skill_id(path: Path) -> str:
    """Stable id for a skill file — its directory name, or ``templates/skill``."""
    if path == PACKAGED_SKILL:
        return "templates/skill"
    return path.parent.name


def _retrieval_skills() -> list[Path]:
    """Skills that name `brain search` — i.e. that touch retrieval at all."""
    return [p for p in _routing_files() if "brain search" in p.read_text(encoding="utf-8")]


def _blocks_with_context(text: str) -> list[tuple[str, str]]:
    """Every fenced block paired with the paragraph immediately above it.

    Loop language sitting in the prose *introducing* a fence teaches the same
    recipe as a comment inside it, and leaves the fence itself byte-identical
    to a legitimate single-document example. Both halves are in scope.
    """
    pairs: list[tuple[str, str]] = []
    for match in _FENCE_RE.finditer(text):
        preamble = text[: match.start()].rstrip()
        lead = preamble.split("\n\n")[-1] if preamble else ""
        pairs.append((match.group(1), lead))
    return pairs


def _show_invocations(block: str) -> list[str]:
    """Lines in a code block that invoke `brain show`.

    Matched anywhere in the line, not just at its start, so the one-line shell
    form (``for id in $ids; do brain show $id; done``) is caught. Whole-line
    comments are excluded — a fence that *documents* the prohibition is not
    teaching it.
    """
    return [
        line
        for line in block.splitlines()
        if "brain show" in line and not line.strip().startswith("#")
    ]


def _markers_in_unit(unit: str) -> list[str]:
    """Loop phrases in one prose sentence / one code line, negation-exempted.

    The exemption is evaluated at this granularity and nowhere wider: a
    negation only speaks for the unit it is written in.
    """
    lowered = unit.lower()
    if any(negation in lowered for negation in _NEGATIONS):
        return []
    return [marker for marker in _LOOP_MARKERS if marker in lowered]


def _loop_markers_in_block(block: str) -> list[str]:
    """Loop phrases in a code block. A negation exempts only its own line.

    Line granularity, not block: a comment on one line must not license a
    command on another. Fences are already line-oriented, so a sentence split
    would be the wrong unit here.
    """
    found: list[str] = []
    for line in block.splitlines():
        found.extend(_markers_in_unit(line))
    return sorted(set(found))


def _loop_markers_in_lead(lead: str) -> list[str]:
    """Loop phrases in the prose above a fence. A negation exempts its sentence.

    Prose is hard-wrapped, so a line is the wrong unit — "Never loop `brain
    show`\\nover each result" would split the prohibition from the phrase it
    prohibits and read as an instruction. Newlines are folded first, then the
    paragraph is split into sentences.
    """
    flat = " ".join(lead.split())
    found: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        found.extend(_markers_in_unit(sentence))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# 0. The parameterized set itself — a skill cannot leave it silently
# ---------------------------------------------------------------------------


def test_retrieval_skill_set_is_complete() -> None:
    """The `brain search` filter still selects every skill we expect it to.

    `_retrieval_skills` is a substring filter evaluated at collection time.
    Without this assertion a skill that stopped saying `brain search` — a
    rename, a switch to MCP tool names — would silently vanish from the
    parameterization below and the suite would report green over a smaller set.
    """
    found = {_skill_id(p) for p in _retrieval_skills()}

    assert found == EXPECTED_RETRIEVAL_SKILLS, (
        "the set of retrieval-teaching skills changed. Missing "
        f"{sorted(EXPECTED_RETRIEVAL_SKILLS - found)}; unexpected "
        f"{sorted(found - EXPECTED_RETRIEVAL_SKILLS)}. If this is intended, "
        "update EXPECTED_RETRIEVAL_SKILLS — but a skill that dropped out "
        "silently is a case this guard stopped covering."
    )


# ---------------------------------------------------------------------------
# 1. Routing — every retrieval skill names the token-budgeted surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_path", _retrieval_skills(), ids=_skill_id)
def test_every_retrieval_skill_mentions_recall(skill_path: Path) -> None:
    """A skill that teaches retrieval must name `brain recall`.

    `recall` is the only surface with a hard token ceiling. A skill that
    documents `brain search` without it routes agents exclusively to unbounded
    reads.
    """
    text = skill_path.read_text(encoding="utf-8")

    assert "brain recall" in text, (
        f"{skill_path.relative_to(REPO_ROOT)} documents `brain search` but never "
        "names `brain recall` — the only token-budgeted retrieval surface. An "
        "agent following this skill has no bounded option."
    )


# ---------------------------------------------------------------------------
# 2. The 91k recipe — no per-result `brain show` loop in ANY skill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_path", _routing_files(), ids=_skill_id)
def test_no_skill_teaches_an_unbounded_show_loop(skill_path: Path) -> None:
    """No skill may pair loop language with a `brain show` invocation.

    Scope is every shipped skill, not just `consult-brain`: the recipe is
    cheap to reintroduce anywhere, and a guard covering one file only would
    watch the door the offender already walked through.
    """
    text = skill_path.read_text(encoding="utf-8")

    offenders = []
    for block, lead in _blocks_with_context(text):
        show_lines = _show_invocations(block)
        if not show_lines:
            continue
        markers = _loop_markers_in_block(block) + _loop_markers_in_lead(lead)
        if markers:
            offenders.append((markers, show_lines))

    assert not offenders, (
        f"{skill_path.relative_to(REPO_ROOT)} pairs loop language with a "
        f"`brain show` invocation — the unbounded recipe: {offenders}. Twenty "
        "results at a mean 18,218-char body is ~91,000 tokens by arithmetic and "
        f"a measured mean of {MEASURED_MEAN}. Route to `brain recall --budget N` "
        "or `brain ask` instead."
    )


def test_consult_brain_states_the_ceiling() -> None:
    """The absence of a loop is not enough — the cost must be written down.

    A reader who reaches for `brain show` anyway should meet the number before
    the twentieth call, so both the arithmetic estimate and the measurement are
    pinned here.
    """
    text = CONSULT_BRAIN.read_text(encoding="utf-8")

    assert "NEVER loop `brain show`" in text, (
        "consult-brain must state the no-loop rule verbatim so the guard above "
        "is backed by prose an agent actually reads."
    )
    assert "91,000 tokens" in text, (
        "consult-brain must carry the arithmetic ceiling (~91,000 tokens) — an "
        "unquantified warning is easy to ignore."
    )
    assert MEASURED_MEAN in text, (
        f"consult-brain must carry the MEASURED mean ({MEASURED_MEAN} tokens), "
        "not only the arithmetic estimate it understates by ~2x. Provenance: "
        "docs/audits/2026-08-11-wave2-routing-counterfactual.md"
    )
    assert "18,218" in text, (
        "consult-brain must carry the measured mean body length (18,218 chars) "
        "— the figure the 91,000 arithmetic is derived from."
    )


def test_measured_ceiling_lives_in_exactly_two_files() -> None:
    """The measured figure is stated twice, and the two must not drift apart.

    `docs/agent-skills.md` promises the cost table is "the single file to
    edit". That is only true if siblings reference the table instead of
    restating its numbers. The docs page itself is the one legitimate second
    home — it is the prose that cites the audit artifact — so the allowed set
    is exactly two, and this test is what keeps it that way.
    """
    docs_page = REPO_ROOT / "docs" / "agent-skills.md"
    allowed = {CONSULT_BRAIN, docs_page}

    carriers = {
        path
        for path in [*_routing_files(), docs_page]
        if _MEAN_VARIANTS.search(path.read_text(encoding="utf-8"))
    }

    assert carriers == allowed, (
        f"the measured mean ({MEASURED_MEAN} tokens) must appear in exactly "
        f"{sorted(p.relative_to(REPO_ROOT).as_posix() for p in allowed)}. "
        f"Found in {sorted(p.relative_to(REPO_ROOT).as_posix() for p in carriers)}. "
        "Siblings should reference the cost table in consult-brain, not copy "
        "its numbers — a figure in five files is a figure that will drift."
    )


# ---------------------------------------------------------------------------
# 3. Sync hygiene — edited skills were re-synced to the install destination
# ---------------------------------------------------------------------------


def _install_dest() -> Path:
    return Path(os.environ.get("BRAIN_SKILLS_DEST") or Path.home() / ".claude" / "skills")


def test_skills_sync_is_not_drifted() -> None:
    """`bin/brain-skills-sync --check` reports no drift against the real dest.

    Skipped when the brain skills are not installed on this machine (CI, a
    fresh clone): `--check` reports MISSING and exits 1 there, which is a
    correct answer to a different question than the one this test asks.

    That `--check` genuinely exits non-zero on drift is proven separately and
    against a temp dest by
    ``tests/test_brain_skills_sync.py::test_check_detects_drift_in_installed_skill``
    — this test is not a green no-op resting on an unverified guard.
    """
    dest = _install_dest()
    installed = [p for p in _repo_skill_files() if (dest / p.parent.name).is_dir()]
    if not installed:
        pytest.skip(f"brain skills are not installed at {dest} — nothing to drift")

    result = subprocess.run(
        [str(SYNC_SCRIPT), "--check", "--dest", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "installed brain skills have drifted from the repo — run "
        f"`bin/brain-skills-sync`.\n{result.stdout}\n{result.stderr}"
    )


def test_no_installed_skill_entry_is_a_symlink() -> None:
    """A symlinked install makes the drift check above structurally vacuous.

    `diff -rq` follows symlinks, so an entry that points back at the working
    tree compares the repo against itself and reports "in sync" for every
    future edit — the guard cannot fail for that skill, ever. The script now
    reports such an entry as drift; this test names the offender directly, so
    the diagnosis does not have to be reconstructed from an exit code.

    Repair is `bin/brain-skills-sync`, which replaces the link with a real
    copy (`rm -rf` on a symlink removes the link, not its target).
    """
    dest = _install_dest()
    installed = [
        dest / p.parent.name
        for p in _repo_skill_files()
        if (dest / p.parent.name).exists()
    ]
    if not installed:
        pytest.skip(f"brain skills are not installed at {dest} — nothing to check")

    links = [
        entry
        for root in installed
        for entry in ([root] if root.is_symlink() else [*root.rglob("*")])
        if entry.is_symlink()
    ]

    assert not links, (
        "these installed skill entries are symlinks, not copies: "
        f"{[str(p) for p in links]}. `bin/brain-skills-sync --check` cannot "
        "detect drift for them. Run `bin/brain-skills-sync` to replace each "
        "link with a real copy."
    )
