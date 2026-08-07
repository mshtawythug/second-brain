"""``brain.agent`` — agent-id normalization and precedence (F10).

Two things matter here beyond the obvious validation cases:

1. **Parity with ``config.py``.** ``Config.load()`` validates
   ``BRAIN_AGENT_ID`` against an inlined pattern (it must stay importable
   before ``brain.agent`` exists), while every runtime caller goes through
   ``normalize_agent_id``. If those two grammars ever diverge, an id accepted
   at startup could be rejected mid-command, or worse. ``brain.agent`` imports
   the same public constant, and the tests below pin both the import identity
   and the observable behaviour.

2. **Blank means unattributed, not invalid.** An unset ``--agent`` flag and an
   unset ``BRAIN_AGENT_ID`` must be indistinguishable, or every un-configured
   invocation becomes a usage error.

All fixture data is synthetic.
"""
from __future__ import annotations

import re

import pytest

from brain import config as config_mod
from brain.agent import normalize_agent_id, resolve_agent_id
from brain.config import AGENT_ID_PATTERN, Config
from brain.errors import AgentIdInvalid, BrainError


def _cfg(agent_id: str | None = None) -> Config:
    """A Config carrying only what these tests read."""
    return Config(
        database_url="postgresql://brain:brain@localhost:5434/unused",
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# Parity with config.py — the drift guard
# ---------------------------------------------------------------------------


def test_agent_module_uses_the_same_pattern_object_as_config() -> None:
    """Not a copy of the regex — literally the same public constant.

    ``config.py``'s comment names ``brain.agent.normalize_agent_id`` as the
    sibling that validates against this same constant. Importing rather than
    re-declaring is what makes that promise unbreakable: there is no second
    string to drift.
    """
    from brain import agent as agent_mod

    assert agent_mod._AGENT_ID_RE.pattern == AGENT_ID_PATTERN
    assert config_mod._AGENT_ID_RE.pattern == AGENT_ID_PATTERN


@pytest.mark.parametrize(
    "candidate",
    [
        "research-agent",
        "capture.bot:v2",
        "a",
        "A0",
        "agent_with_underscores",
        "x" * 64,
        "claude:sonnet-4.6",
    ],
)
def test_config_and_agent_agree_on_valid_ids(candidate: str) -> None:
    """Behavioural parity, not just pattern identity."""
    assert normalize_agent_id(candidate) == candidate
    assert config_mod._AGENT_ID_RE.match(candidate) is not None


@pytest.mark.parametrize(
    "candidate",
    [
        "-leading-hyphen",
        ".leading-dot",
        "_leading-underscore",
        ":leading-colon",
        "has space",
        "has/slash",
        "has\\backslash",
        "x" * 65,
        "emoji🙂",
        "tab\there",
        "new\nline",
    ],
)
def test_config_and_agent_agree_on_invalid_ids(candidate: str) -> None:
    with pytest.raises(AgentIdInvalid):
        normalize_agent_id(candidate)
    assert config_mod._AGENT_ID_RE.match(candidate) is None


def test_pattern_is_anchored_at_both_ends() -> None:
    """An unanchored pattern would accept ``bad id good`` via a substring."""
    assert AGENT_ID_PATTERN.startswith("^")
    assert AGENT_ID_PATTERN.endswith("$")
    assert re.compile(AGENT_ID_PATTERN).match("ok then") is None


# ---------------------------------------------------------------------------
# normalize_agent_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   ", "\t", "\n"])
def test_blank_is_unattributed_not_invalid(blank: str | None) -> None:
    """Unset flag and unset env must be indistinguishable."""
    assert normalize_agent_id(blank) is None


def test_surrounding_whitespace_is_stripped() -> None:
    assert normalize_agent_id("  research-agent  ") == "research-agent"


def test_malformed_id_names_the_grammar_and_the_input() -> None:
    """A typo must be actionable, not just rejected."""
    with pytest.raises(AgentIdInvalid) as excinfo:
        normalize_agent_id("-oops")

    message = str(excinfo.value)
    assert AGENT_ID_PATTERN in message
    assert "-oops" in message


def test_agent_id_invalid_is_a_brain_error() -> None:
    """Callers catch it by the project base class."""
    assert issubclass(AgentIdInvalid, BrainError)


def test_boundary_lengths() -> None:
    """64 characters is the documented maximum; 65 is not."""
    assert normalize_agent_id("x" * 64) == "x" * 64
    with pytest.raises(AgentIdInvalid):
        normalize_agent_id("x" * 65)


# ---------------------------------------------------------------------------
# resolve_agent_id — precedence
# ---------------------------------------------------------------------------


def test_explicit_flag_wins_over_config() -> None:
    assert resolve_agent_id("from-flag", _cfg("from-env")) == "from-flag"


def test_config_is_used_when_no_flag_given() -> None:
    """The path that makes BRAIN_AGENT_ID work with no CLI flag at all."""
    assert resolve_agent_id(None, _cfg("from-env")) == "from-env"


def test_none_when_neither_is_set() -> None:
    assert resolve_agent_id(None, _cfg(None)) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_flag_falls_through_to_config(blank: str) -> None:
    """``--agent ""`` is indistinguishable from an unset flag at the Typer
    boundary, so it must not clobber a configured id."""
    assert resolve_agent_id(blank, _cfg("from-env")) == "from-env"


def test_explicit_flag_is_validated() -> None:
    with pytest.raises(AgentIdInvalid):
        resolve_agent_id("-bad", _cfg(None))


def test_explicit_flag_is_stripped() -> None:
    assert resolve_agent_id("  spaced  ", _cfg(None)) == "spaced"
