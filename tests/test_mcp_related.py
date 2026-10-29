"""MCP ``brain_related`` (RM-12): the related-documents panel, for an agent.

What is pinned here is what the TOOL decides; the ranking itself is
:func:`brain.related.compute_related`'s, pinned by ``tests/test_related_compute.py``:

* AC1 — the ids, in order, equal ``GET /api/notes/{id}/related``'s for the same corpus.
* AC2 — a confidential ROOT is refused before ranking unless ``include_confidential``.
* AC3 — a confidential CANDIDATE is never returned unless ``include_confidential``.
* AC5 — ``limit`` above ``RELATED_LIMIT_CEILING`` is ``INVALID_PARAMS``.

The corpus is the UI route test's synthetic three-document seed, reused rather than restated,
so the parity test compares the two surfaces on literally the same rows.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tools import related as related_tool
from brain.related import DEFAULT_RELATED_LIMIT, RelatedDoc
from brain.sensitivity import CONFIDENTIAL
from tests.test_ui_routes_related import (
    AMBIG_1,
    AMBIG_2,
    _client,
    _insert_doc,
    _related_of,
    _seed,
    _vector,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: An off-default floor no other code path would produce by accident.
SENTINEL_FLOOR = 0.0123


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the data fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path, vector_sim_floor=0.0),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _mark_confidential(conn: psycopg.Connection[Any], doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid", (CONFIDENTIAL, doc_id)
    )


def _error_code(excinfo: pytest.ExceptionInfo[BaseException]) -> int:
    return int(excinfo.value.error.code)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ shape --


def test_related_returns_the_ranked_neighbours_and_the_floor(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    # Arrange
    ids = _seed(test_db)

    # Act
    payload = related_tool.brain_related(id=ids["A"])

    # Assert
    assert list(payload) == ["id", "related", "vector_sim_floor"]
    assert payload["id"] == ids["A"]
    assert payload["vector_sim_floor"] == mcp_state.cfg.vector_sim_floor
    related = payload["related"]
    assert [row["id"] for row in related] == [ids["B"], ids["C"]]
    top = related[0]
    assert list(top) == ["id", "title", "vault_path", "source", "score", "snippet"]
    assert top["title"] == "ZZQUARK canonical guide"
    assert top["vault_path"] == "notes/b-title-overlap.md"
    assert top["source"] == "vault"
    assert isinstance(top["score"], float)
    assert "ZZQUARK" in top["snippet"]


def test_a_prefix_resolves_like_a_full_uuid(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    # Arrange
    ids = _seed(test_db)

    # Act
    payload = related_tool.brain_related(id=ids["A"][:8])

    # Assert
    assert payload["id"] == ids["A"]
    assert [row["id"] for row in payload["related"]] == [ids["B"], ids["C"]]


def test_the_configured_floor_and_limit_reach_compute_related(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is ``cfg.vector_sim_floor`` (the one search reads), never a literal."""
    # Arrange
    ids = _seed(test_db)
    monkeypatch.setattr(
        mcp_state, "cfg", dataclasses.replace(mcp_state.cfg, vector_sim_floor=SENTINEL_FLOOR)
    )
    calls: list[dict[str, Any]] = []

    def recording_compute_related(conn: Any, doc_id: str, **kwargs: Any) -> list[RelatedDoc]:
        calls.append({"doc_id": doc_id, **kwargs})
        return []

    monkeypatch.setattr(related_tool, "compute_related", recording_compute_related)

    # Act
    payload = related_tool.brain_related(id=ids["A"], limit=3)

    # Assert
    assert payload["vector_sim_floor"] == SENTINEL_FLOOR
    assert calls == [
        {
            "doc_id": ids["A"],
            "limit": 3,
            "vector_sim_floor": SENTINEL_FLOOR,
            "exclude_confidential": True,
        }
    ]


def test_the_default_limit_is_the_panels(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    ids = _seed(test_db)
    limits: list[int] = []

    def recording(conn: Any, doc_id: str, **kwargs: Any) -> list[RelatedDoc]:
        limits.append(kwargs["limit"])
        return []

    monkeypatch.setattr(related_tool, "compute_related", recording)

    # Act
    related_tool.brain_related(id=ids["A"])

    # Assert
    assert limits == [DEFAULT_RELATED_LIMIT]


# ------------------------------------------------------- AC1: UI parity --


def test_ids_and_order_equal_the_ui_related_route(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    tmp_path: Path,
    fake_embedder: Any,
) -> None:
    # Arrange: one more neighbour, so the order has three positions to agree on.
    ids = _seed(test_db)
    _insert_doc(
        test_db,
        doc_id="dddddddd-0000-4000-8000-00000000000d",
        title="ZZQUARK appendix",
        vault_path="notes/d-appendix.md",
        chunk_contents=["ZZQUARK appendix paragraph."],
        chunk_vectors=[_vector(0.9, 0.4)],
    )
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    # Act
    ui_ids = [row["id"] for row in _related_of(client, ids["A"])["related"]]
    mcp_ids = [row["id"] for row in related_tool.brain_related(id=ids["A"])["related"]]

    # Assert
    assert len(ui_ids) == 3
    assert mcp_ids == ui_ids


# ------------------------------------------------------ AC2: root gate --


def test_a_confidential_root_is_refused_before_ranking(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    ids = _seed(test_db)
    _mark_confidential(test_db, ids["A"])
    calls: list[str] = []

    def recording(conn: Any, doc_id: str, **kwargs: Any) -> list[RelatedDoc]:
        calls.append(doc_id)
        return []

    monkeypatch.setattr(related_tool, "compute_related", recording)

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_related(id=ids["A"])

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS
    message = str(excinfo.value)
    assert "confidential" in message
    assert "include_confidential=true" in message
    for withheld in ("ZZQUARK", ids["B"], ids["C"]):
        assert withheld not in message
    assert calls == []


def test_a_confidential_root_is_answered_when_opted_in(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    # Arrange
    ids = _seed(test_db)
    _mark_confidential(test_db, ids["A"])

    # Act
    payload = related_tool.brain_related(id=ids["A"], include_confidential=True)

    # Assert
    assert [row["id"] for row in payload["related"]] == [ids["B"], ids["C"]]


def test_a_normal_root_is_answered_under_the_strict_lens(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The other direction of the root gate: only a confidential root is refused."""
    # Arrange
    ids = _seed(test_db)
    _mark_confidential(test_db, ids["C"])

    # Act
    payload = related_tool.brain_related(id=ids["A"])

    # Assert
    assert payload["id"] == ids["A"]


# ------------------------------------------------- AC3: candidate lens --


@pytest.mark.parametrize(("include_confidential", "present"), [(False, False), (True, True)])
def test_a_confidential_candidate_needs_the_opt_in(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    include_confidential: bool,
    present: bool,
) -> None:
    """``C`` is asserted present either way, so an empty list cannot pass for a filtered one."""
    # Arrange
    ids = _seed(test_db)
    _mark_confidential(test_db, ids["B"])

    # Act
    payload = related_tool.brain_related(
        id=ids["A"], include_confidential=include_confidential
    )

    # Assert
    returned = [row["id"] for row in payload["related"]]
    assert (ids["B"] in returned) is present
    assert ids["C"] in returned


# ------------------------------------------------------- AC5: ceilings --


def test_limit_above_the_ceiling_is_invalid_params(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    # Arrange
    ids = _seed(test_db)

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_related(id=ids["A"], limit=related_tool.RELATED_LIMIT_CEILING + 1)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS
    assert str(related_tool.RELATED_LIMIT_CEILING) in str(excinfo.value)


def test_limit_at_the_ceiling_is_accepted(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    # Arrange
    ids = _seed(test_db)

    # Act
    payload = related_tool.brain_related(id=ids["A"], limit=related_tool.RELATED_LIMIT_CEILING)

    # Assert
    assert len(payload["related"]) == 2


def test_the_ceiling_is_twenty_and_above_the_default() -> None:
    # Act / Assert
    assert related_tool.RELATED_LIMIT_CEILING == 20
    assert related_tool.RELATED_LIMIT_CEILING >= DEFAULT_RELATED_LIMIT


@pytest.mark.parametrize("bad_limit", [0, -2])
def test_a_non_positive_limit_is_invalid_params(
    mcp_state: mcp_server._State, bad_limit: int
) -> None:
    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_related(id="aaaaaaaa", limit=bad_limit)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS


# ----------------------------------------------------------------- errors --


@pytest.mark.parametrize("prefix", ["abc", "zzzzzzzz", "deadbeefdead", "abcdef"])
def test_an_unresolvable_prefix_is_invalid_params(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State, prefix: str
) -> None:
    """Too short, not hex, unknown, ambiguous: each is the caller's to fix."""
    # Arrange
    for doc_id, title in ((AMBIG_1, "Ambiguous One"), (AMBIG_2, "Ambiguous Two")):
        _insert_doc(
            test_db,
            doc_id=doc_id,
            title=title,
            vault_path=f"notes/{doc_id}.md",
            chunk_contents=[f"{title} body."],
        )

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_related(id=prefix)

    # Assert
    assert _error_code(excinfo) == INVALID_PARAMS


def test_a_database_error_is_class_only_internal_error(
    test_db: psycopg.Connection[Any],
    mcp_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    ids = _seed(test_db)
    secret = "SELECT secret_detail FROM nowhere"

    def failing(conn: Any, doc_id: str, **kwargs: Any) -> list[RelatedDoc]:
        raise psycopg.errors.UndefinedTable(secret)

    monkeypatch.setattr(related_tool, "compute_related", failing)

    # Act
    with pytest.raises(MCPError) as excinfo:
        related_tool.brain_related(id=ids["A"])

    # Assert
    assert _error_code(excinfo) == INTERNAL_ERROR
    assert "UndefinedTable" in str(excinfo.value)
    assert secret not in str(excinfo.value)


def test_the_tool_module_imports_neither_the_ui_nor_the_cli() -> None:
    """Importing ``brain.ui`` or ``brain.cli`` would load Typer and the doctor into the server."""
    # Arrange
    tree = ast.parse(inspect.getsource(related_tool))

    # Act
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    # Assert
    assert not [name for name in imported if name.lstrip(".").split(".")[0] in {"ui", "cli"}]
    assert not [name for name in imported if name.startswith(("brain.ui", "brain.cli"))]
