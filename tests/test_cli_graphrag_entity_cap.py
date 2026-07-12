"""Task 4.3 — `brain graphrag entity` neighbour-cap tests.

A high-degree entity's local neighbourhood otherwise dumps every reached
neighbour (measured 282 lines on the live corpus). ``graphrag entity`` now caps
the *rendered* neighbours at ``--limit/-n`` (default 30; ``-n 0`` shows all) and
prints a "… and N more" footer when it truncated. Seeds are always kept.

These tests stay off the live AGE DB: the cap is a pure render-layer concern, so
they either unit-test the ``_cap_entity_neighbours`` helper directly or drive the
CLI with ``_graphrag_search_or_exit`` mocked to return a synthetic
:class:`GraphContext`. All entity names are synthetic (no PII).
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from brain.cli import _cap_entity_neighbours, app
from brain.graph_rag.schema import GraphContext, GraphEntity, GraphExplanation

_FOOTER_HINT = "use -n 0 for all"


def _entity(idx: int, *, seed: bool = False) -> GraphEntity:
    if seed:
        return GraphEntity(
            id="seed-id",
            entity_type="person",
            name="seed-person",
            canonical_key="seed",
            doc_count=9,
        )
    return GraphEntity(
        id=f"n-{idx:02d}",
        entity_type="person",
        name=f"neighbor-{idx:02d}",
        canonical_key=f"nbr{idx:02d}",
        doc_count=idx,
    )


def _fake_ctx(n_neighbours: int, *, with_explanation: bool = True) -> GraphContext:
    """One seed + ``n_neighbours`` reached neighbours (local mode)."""
    seed = _entity(0, seed=True)
    neighbours = [_entity(i) for i in range(n_neighbours)]
    explanation = (
        GraphExplanation(mode="local", seed_entity_ids=["seed-id"], depth=1)
        if with_explanation
        else None
    )
    return GraphContext(
        session_id="s",
        mode="local",
        query="seed-person",
        entities=[seed, *neighbours],
        explanation=explanation,
    )


# --------------------------------------------------------------------------- #
# Pure helper: _cap_entity_neighbours
# --------------------------------------------------------------------------- #
def test_cap_truncates_neighbours_and_keeps_seed() -> None:
    ctx = _fake_ctx(35)

    capped, hidden = _cap_entity_neighbours(ctx, 30)

    assert hidden == 5
    # Seed + 30 neighbours == 31 rendered entities.
    assert len(capped.entities) == 31
    keys = [e.canonical_key for e in capped.entities]
    assert keys[0] == "seed"  # seed always first / kept
    assert "nbr29" in keys
    assert "nbr30" not in keys


def test_cap_zero_is_unlimited() -> None:
    ctx = _fake_ctx(35)

    capped, hidden = _cap_entity_neighbours(ctx, 0)

    assert hidden == 0
    assert capped is ctx  # untouched
    assert len(capped.entities) == 36


def test_cap_no_truncation_when_under_cap() -> None:
    ctx = _fake_ctx(20)

    capped, hidden = _cap_entity_neighbours(ctx, 30)

    assert hidden == 0
    assert capped is ctx
    assert len(capped.entities) == 21


def test_cap_arbitrary_limit() -> None:
    ctx = _fake_ctx(35)

    capped, hidden = _cap_entity_neighbours(ctx, 10)

    assert hidden == 25
    assert len(capped.entities) == 11  # seed + 10


def test_cap_without_explanation_treats_all_as_neighbours() -> None:
    """No explanation → no seed ids known → every entity is a cappable neighbour."""
    ctx = _fake_ctx(35, with_explanation=False)

    capped, hidden = _cap_entity_neighbours(ctx, 30)

    assert hidden == 6  # 36 total entities, none identified as seed
    assert len(capped.entities) == 30


# --------------------------------------------------------------------------- #
# CLI wiring (mock the retrieval seam; no live DB)
# --------------------------------------------------------------------------- #
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://brain:brain@localhost:5434/second_brain_test"
    )


def test_entity_default_caps_at_30_with_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    with patch("brain.cli._graphrag_search_or_exit", return_value=_fake_ctx(35)):
        res = CliRunner().invoke(
            app, ["graphrag", "entity", "seed-person"], env={"COLUMNS": "200"}
        )
    assert res.exit_code == 0, res.output
    out = res.output
    assert "seed-person" in out  # seed kept
    assert "neighbor-29" in out  # within the 30 cap
    assert "neighbor-30" not in out  # beyond the cap
    assert "and 5 more" in out
    assert _FOOTER_HINT in out


def test_entity_n_zero_shows_all_no_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    with patch("brain.cli._graphrag_search_or_exit", return_value=_fake_ctx(35)):
        res = CliRunner().invoke(
            app,
            ["graphrag", "entity", "seed-person", "-n", "0"],
            env={"COLUMNS": "200"},
        )
    assert res.exit_code == 0, res.output
    out = res.output
    assert "neighbor-34" in out  # last neighbour rendered
    assert "more (" not in out  # no truncation footer


def test_entity_explicit_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    with patch("brain.cli._graphrag_search_or_exit", return_value=_fake_ctx(35)):
        res = CliRunner().invoke(
            app,
            ["graphrag", "entity", "seed-person", "-n", "10"],
            env={"COLUMNS": "200"},
        )
    assert res.exit_code == 0, res.output
    out = res.output
    assert "neighbor-09" in out
    assert "neighbor-10" not in out
    assert "and 25 more" in out


def test_entity_json_is_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """--json returns the full neighbourhood (machine consumer); no footer."""
    _env(monkeypatch)
    with patch("brain.cli._graphrag_search_or_exit", return_value=_fake_ctx(35)):
        res = CliRunner().invoke(
            app, ["graphrag", "entity", "seed-person", "--json"]
        )
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert len(payload["entities"]) == 36  # seed + 35, untouched
    assert "more (" not in res.output


def test_entity_negative_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """min=0 → a negative -n fails at parse time (exit 2), never silent."""
    _env(monkeypatch)
    res = CliRunner().invoke(app, ["graphrag", "entity", "seed-person", "-n", "-1"])
    assert res.exit_code == 2


def test_entity_help_states_default_30() -> None:
    res = CliRunner().invoke(app, ["graphrag", "entity", "--help"])
    assert res.exit_code == 0, res.output
    assert "30" in res.output
    assert "-n 0" in res.output
