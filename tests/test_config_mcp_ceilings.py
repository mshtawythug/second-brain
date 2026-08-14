"""Wave 3 — the six ``BRAIN_*`` MCP payload-ceiling knobs.

Every ceiling in this wave is a judgement call sized off live-corpus
percentiles, so each one is an env var and each one must fail loudly at load
time on a typo. Mirrors ``tests/test_config_new_fields.py``: the
``isolated_dotenv`` fixture blocks every on-disk ``.env`` so only
``os.environ`` reaches ``Config.load()``.

The interesting asymmetry pinned here: ``BRAIN_SHOW_MAX_CONTENT_TOKENS`` is the
ONLY one that accepts ``0`` — it is the operator's "no ceiling" opt-out, parsed
by :func:`brain.config._parse_non_negative_int_env`. The other five use
``_parse_positive_int_env`` and reject ``0``, because their escape hatch is the
per-call parameter rather than a disabled ceiling.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import (
    DEFAULT_GRAPH_COMMUNITIES_LIST_LIMIT,
    DEFAULT_GRAPH_ENTITIES_MAX_LIMIT,
    DEFAULT_MCP_ROWS_MAX_LIMIT,
    DEFAULT_RECALL_MAX_BUDGET_TOKENS,
    DEFAULT_SEARCH_MAX_LIMIT,
    DEFAULT_SHOW_MAX_CONTENT_TOKENS,
    Config,
    ConfigError,
)
from tests.conftest import TEST_DATABASE_URL

_CEILING_ENV_VARS = (
    "BRAIN_SHOW_MAX_CONTENT_TOKENS",
    "BRAIN_SEARCH_MAX_LIMIT",
    "BRAIN_RECALL_MAX_BUDGET_TOKENS",
    "BRAIN_GRAPH_ENTITIES_MAX_LIMIT",
    "BRAIN_MCP_ROWS_MAX_LIMIT",
    "BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT",
)

# The five knobs whose parser is ``_parse_positive_int_env`` (0 rejected),
# paired with the Config field each populates.
_POSITIVE_ONLY = (
    ("BRAIN_SEARCH_MAX_LIMIT", "search_max_limit"),
    ("BRAIN_RECALL_MAX_BUDGET_TOKENS", "recall_max_budget_tokens"),
    ("BRAIN_GRAPH_ENTITIES_MAX_LIMIT", "graph_entities_max_limit"),
    ("BRAIN_MCP_ROWS_MAX_LIMIT", "mcp_rows_max_limit"),
    ("BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT", "graph_communities_list_limit"),
)


#: EVERY committed payload measurement, not just the Wave-0 baseline. Each is
#: allowlisted by name in ``.gitignore`` (``docs/audits/`` is otherwise
#: ignored), so these paths are stable for anyone who checks the repo out.
#:
#: The glob is deliberate. Binding to the Wave-0 file alone derived 2.363 while
#: the repo already held a WORSE committed measurement (Wave 3's 2.367) — the
#: ceiling was being pinned against a figure the repo itself had superseded,
#: loosening the bound by ~23 tokens. A future wave that re-measures and lands
#: another artifact tightens this automatically instead of needing someone to
#: notice. ``wave2-routing-counterfactual.json`` is a different measurement
#: (routing, no ``recall`` rows) and correctly does not match this pattern.
_RECALL_ARTIFACTS = sorted(
    (Path(__file__).resolve().parents[1] / "docs" / "audits").glob(
        "*token-payload*.json"
    )
)

#: Re-derived 2026-08-13, NOT inherited: baseline + after-wave1 + after-wave3.
#: A floor, so landing a fourth artifact does not fail this — but losing one to
#: a rename or a move cannot silently shrink the evidence set back to whichever
#: file happens to be loosest.
_MIN_RECALL_ARTIFACTS = 3


def _measured_recall_overshoot() -> tuple[float, int, Path]:
    """Return ``(worst_case_ratio, sample_size, source)`` across ALL artifacts.

    The ratio is ``max(delivered_tokens) / budget_tokens`` over the ``recall``
    rows — the same arithmetic ``DEFAULT_RECALL_MAX_BUDGET_TOKENS``' comment
    documents, but computed from the artifacts instead of copied out of them.

    The worst case is taken across every committed measurement, so the ceiling
    is bound by the harshest number the repo can show, not by the oldest one.
    """
    assert len(_RECALL_ARTIFACTS) >= _MIN_RECALL_ARTIFACTS, (
        f"expected >= {_MIN_RECALL_ARTIFACTS} committed payload artifacts, found "
        f"{[p.name for p in _RECALL_ARTIFACTS]} — a renamed or un-allowlisted "
        "artifact would silently loosen the recall ceiling's derivation"
    )

    worst: tuple[float, int, Path] | None = None
    shape: tuple[int, int] | None = None
    for path in _RECALL_ARTIFACTS:
        data = json.loads(path.read_text())
        budget = data["budget_tokens"]
        recall_tokens = [
            m["tokens"] for m in data["measurements"] if m["surface"] == "recall"
        ]
        assert recall_tokens, f"no recall measurements in {path}"
        # Taking max() ACROSS artifacts is only meaningful if they measured the
        # same thing. Different query counts or different budgets would make
        # the "worst case" an artefact of the harness, not of the payload.
        assert shape is None or shape == (len(recall_tokens), budget), (
            f"{path.name} measured {len(recall_tokens)} queries at "
            f"budget_tokens={budget}, but a sibling artifact measured {shape} — "
            "the artifacts are not comparable, so the worst case across them is "
            "meaningless; re-record them on one query set before re-deriving"
        )
        shape = (len(recall_tokens), budget)
        candidate = (max(recall_tokens) / budget, len(recall_tokens), path)
        if worst is None or candidate[0] > worst[0]:
            worst = candidate

    assert worst is not None
    return worst


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Block all .env file sources so only os.environ reaches Config.load()."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_module, "_project_dotenv", lambda: tmp_path / "project.env"
    )
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.setattr(
        config_module,
        "_brain_home_root",
        lambda _config_file=None: tmp_path / "brain_home_root",
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for key in _CEILING_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_ceilings_default_when_unset(isolated_dotenv: Path) -> None:
    """The literals are pinned so a silent default change cannot pass review."""
    cfg = Config.load()

    assert cfg.show_max_content_tokens == DEFAULT_SHOW_MAX_CONTENT_TOKENS == 25000
    assert cfg.search_max_limit == DEFAULT_SEARCH_MAX_LIMIT == 50
    assert cfg.recall_max_budget_tokens == DEFAULT_RECALL_MAX_BUDGET_TOKENS == 13000
    assert (
        cfg.graph_entities_max_limit == DEFAULT_GRAPH_ENTITIES_MAX_LIMIT == 500
    )
    assert cfg.mcp_rows_max_limit == DEFAULT_MCP_ROWS_MAX_LIMIT == 200
    assert (
        cfg.graph_communities_list_limit
        == DEFAULT_GRAPH_COMMUNITIES_LIST_LIMIT
        == 25
    )


def test_recall_ceiling_is_derived_from_the_measured_overshoot(
    isolated_dotenv: Path,
) -> None:
    """13000, not 32000 — and the arithmetic is the reason.

    ``brain_recall`` delivers ~2.2x its ``budget_tokens`` because every passage
    ships twice. The intended bound is ~32k DELIVERED tokens, so the accepted
    budget must be ``32000 / worst_case_overshoot``.

    **The overshoot is READ FROM THE ARTIFACTS, not hardcoded.** It previously
    sat here as the literal ``2.36``, which meant a re-measured baseline could
    not move this test — the number would silently keep asserting against a
    corpus that no longer existed. Deriving it from the committed, explicitly
    allowlisted ``docs/audits/*token-payload*.json`` files makes them the single
    source of truth: re-record one and this test re-derives, or fails and tells
    you the ceiling needs re-deriving too.

    **It binds to the WORST committed measurement, not the oldest.** Reading
    only the Wave-0 baseline derived ``2.363`` while the repo already held
    Wave 3's ``2.367`` — a ~23-token loosening, and a live one: it would have
    passed a ``recall_max_budget_tokens`` of 13,542 that the repo's own worst
    measurement rules out at 13,518. Taking ``max()`` across every artifact
    removes the choice of which measurement to trust.

    Both derived values are *stricter* than the ``2.36`` the constant's comment
    rounds to, so this test binds a fraction harder than the documented
    arithmetic — the safe direction.

    MUTATION: change ``candidate[0] > worst[0]`` to ``<`` in
    ``_measured_recall_overshoot`` (take the mildest artifact instead of the
    harshest) and the sample/shape guards still pass — this assertion is what
    holds the ceiling to the harsher figure. To see it bite, raise
    ``DEFAULT_RECALL_MAX_BUDGET_TOKENS`` to 13530: red at 2.367, green at 2.363.
    """
    overshoot, sample_size, source = _measured_recall_overshoot()
    assert sample_size == 11, (
        "the derivation is an 11-query sample; a changed sample size means the "
        "baseline was re-recorded and the ceiling needs re-deriving"
    )

    cfg = Config.load()

    intended_delivered_bound = 32000
    assert cfg.recall_max_budget_tokens <= intended_delivered_bound / overshoot, (
        f"recall_max_budget_tokens={cfg.recall_max_budget_tokens} would deliver "
        f"more than {intended_delivered_bound} tokens at the worst measured "
        f"overshoot ({overshoot}x, from {source.name})"
    )


# ---------------------------------------------------------------------------
# Overrides + eager validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("env_var", "field"), _POSITIVE_ONLY)
def test_positive_ceiling_accepts_an_override(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, env_var: str, field: str
) -> None:
    # Explicit, not a mystery guest: 7 is below the 2000 default recall budget,
    # and the budget<=ceiling cross-validation would (correctly) reject that
    # pair for the BRAIN_RECALL_MAX_BUDGET_TOKENS row. Lowering the budget
    # keeps every row testing the one thing it means to test — that the knob
    # is read — instead of the pair rule, which has its own tests below.
    monkeypatch.setenv("BRAIN_RECALL_BUDGET_TOKENS", "1")
    monkeypatch.setenv(env_var, "7")

    assert getattr(Config.load(), field) == 7


@pytest.mark.parametrize(("env_var", "field"), _POSITIVE_ONLY)
@pytest.mark.parametrize("bad", ["0", "-1", "banana", "12.5"])
def test_positive_ceiling_rejects_bad_values_at_load_time(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: Path,
    env_var: str,
    field: str,  # noqa: ARG001 — parametrized alongside env_var
    bad: str,
) -> None:
    """A typo must surface at startup, not mid-command."""
    monkeypatch.setenv(env_var, bad)

    with pytest.raises(ConfigError, match=env_var):
        Config.load()


def test_show_ceiling_accepts_zero_as_the_operator_opt_out(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """The documented rollback for ``brain_show``: 0 = unlimited.

    This is the ONLY knob in the family for which 0 is legal, which is exactly
    why ``_parse_non_negative_int_env`` exists as a separate function rather
    than a loosened ``_parse_positive_int_env`` (20+ knobs depend on that one
    still rejecting 0).
    """
    monkeypatch.setenv("BRAIN_SHOW_MAX_CONTENT_TOKENS", "0")

    assert Config.load().show_max_content_tokens == 0


@pytest.mark.parametrize("bad", ["-1", "banana", "12.5"])
def test_show_ceiling_still_rejects_negatives_and_junk(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, bad: str
) -> None:
    """Accepting 0 must not mean accepting anything."""
    monkeypatch.setenv("BRAIN_SHOW_MAX_CONTENT_TOKENS", bad)

    with pytest.raises(ConfigError, match="BRAIN_SHOW_MAX_CONTENT_TOKENS"):
        Config.load()


def test_show_ceiling_accepts_a_positive_override(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_SHOW_MAX_CONTENT_TOKENS", "1234")

    assert Config.load().show_max_content_tokens == 1234


# ---------------------------------------------------------------------------
# .env.example coverage — the knobs ARE the rollback mechanism
# ---------------------------------------------------------------------------


def test_every_ceiling_knob_is_documented_in_env_example() -> None:
    """A knob absent from ``.env.example`` is a rollback nobody can find.

    The plan's entire rollback story for this wave is "set the relevant
    ``BRAIN_*`` knob" — which requires the operator to know the knob exists.
    ``.env.example`` documents 50+ knobs and is where they look. Nothing
    enforced its completeness, so this is the enforcement: add a ceiling
    without documenting it and this test says so.
    """
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text()

    missing = [name for name in _CEILING_ENV_VARS if name not in text]
    assert not missing, f"undocumented in .env.example: {', '.join(missing)}"


# ---------------------------------------------------------------------------
# Cross-validation: the default recall budget vs its own ceiling
# ---------------------------------------------------------------------------


def test_recall_budget_above_its_ceiling_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """Each knob is individually valid; the PAIR is not.

    An operator who raises the default budget past the max breaks every
    default ``brain_recall`` call — the omitted ``budget_tokens`` falls back to
    the default, trips the ceiling, and returns ``INVALID_PARAMS`` telling the
    *agent* to re-ask smaller. The error would blame the caller for the
    operator's mistake, so it has to be caught at load instead. Both env vars
    are named because either one is a legitimate place to fix it.
    """
    monkeypatch.setenv("BRAIN_RECALL_BUDGET_TOKENS", "20000")

    with pytest.raises(ConfigError) as excinfo:
        Config.load()
    message = str(excinfo.value)
    assert "BRAIN_RECALL_BUDGET_TOKENS" in message
    assert "BRAIN_RECALL_MAX_BUDGET_TOKENS" in message
    assert "20000" in message
    assert str(DEFAULT_RECALL_MAX_BUDGET_TOKENS) in message


def test_recall_budget_equal_to_its_ceiling_is_accepted(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """The boundary is inclusive — a budget exactly at the ceiling is legal.

    Pins the comparison as ``>`` rather than ``>=``: raising both knobs to the
    same number is a coherent operator choice (every call may use the whole
    ceiling), and rejecting it would be a false positive.
    """
    monkeypatch.setenv("BRAIN_RECALL_BUDGET_TOKENS", "9000")
    monkeypatch.setenv("BRAIN_RECALL_MAX_BUDGET_TOKENS", "9000")

    cfg = Config.load()
    assert cfg.recall_budget_tokens == 9000
    assert cfg.recall_max_budget_tokens == 9000


def test_raising_both_recall_knobs_together_is_accepted(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    """The documented fix must actually work: raise the ceiling too."""
    monkeypatch.setenv("BRAIN_RECALL_BUDGET_TOKENS", "20000")
    monkeypatch.setenv("BRAIN_RECALL_MAX_BUDGET_TOKENS", "30000")

    cfg = Config.load()
    assert cfg.recall_budget_tokens == 20000
    assert cfg.recall_max_budget_tokens == 30000
