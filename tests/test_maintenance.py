"""Unit + integration tests for the brain-rebuild full-corpus orchestrator."""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import maintenance as m


def test_build_stages_canonical_order_and_ids() -> None:
    stages = m.build_stages(vault_path=Path("/tmp/vault"), keep=3, clean_cache=False)
    assert [s.stage_id for s in stages] == [
        "embeddings", "summaries", "search",
        "graph", "graph-weights", "communities", "wiki",
    ]
    assert stages[0].steps[0].argv == ("brain", "reembed")
    assert stages[0].steps[0].fatal is True
    assert stages[1].steps[0].argv == ("brain", "enrich", "--backfill")
    assert stages[3].steps[0].argv == ("brain", "graphrag", "build", "--backfill")
    assert stages[4].steps[0].argv == ("brain", "graphrag", "refresh")
    assert stages[5].steps[0].argv == ("brain", "graphrag", "communities", "refresh")


# ---------------------------------------------------------------------------
# Task 2: Stage selection
# ---------------------------------------------------------------------------


def _ids(stages: list[m.Stage]) -> list[str]:
    return [s.stage_id for s in stages]


def _stages() -> list[m.Stage]:
    return m.build_stages(vault_path=Path("/tmp/v"), keep=3, clean_cache=False)


def test_select_default_runs_all() -> None:
    assert _ids(m.select_stages(_stages(), only=None, skip=None, wiki_only=False)) == list(
        m.ALL_STAGE_IDS
    )


def test_select_only_subset_preserves_registry_order() -> None:
    got = m.select_stages(_stages(), only=["communities", "graph"], skip=None, wiki_only=False)
    assert _ids(got) == ["graph", "communities"]


def test_select_skip_removes() -> None:
    got = m.select_stages(_stages(), only=None, skip=["summaries", "wiki"], wiki_only=False)
    assert _ids(got) == ["embeddings", "search", "graph", "graph-weights", "communities"]


def test_select_wiki_only() -> None:
    assert _ids(m.select_stages(_stages(), only=None, skip=None, wiki_only=True)) == ["wiki"]


def test_only_and_skip_mutually_exclusive() -> None:
    with pytest.raises(m.SelectionError):
        m.select_stages(_stages(), only=["graph"], skip=["wiki"], wiki_only=False)


def test_wiki_only_with_only_errors() -> None:
    with pytest.raises(m.SelectionError):
        m.select_stages(_stages(), only=["graph"], skip=None, wiki_only=True)


def test_unknown_stage_id_errors_with_valid_list() -> None:
    with pytest.raises(m.SelectionError) as exc:
        m.select_stages(_stages(), only=["bogus"], skip=None, wiki_only=False)
    assert "bogus" in str(exc.value)
    assert "embeddings" in str(exc.value)


# ---------------------------------------------------------------------------
# Task 3: In-flight ingest guard
# ---------------------------------------------------------------------------


def test_ingest_in_flight_detects_ingest() -> None:
    procs = [
        "/usr/bin/python /x/bin/brain ingest-stdin --source krisp --title foo",
        "/usr/bin/python -m brain.wiki.build_watcher --vault /v --keep 3",
    ]
    assert m.ingest_in_flight(procs) is True


def test_ingest_in_flight_ignores_watchers_and_search() -> None:
    procs = [
        "/usr/bin/python /x/bin/brain vault sync --watch --vault /v",
        "/usr/bin/python -m brain.wiki.build_watcher --vault /v --keep 3",
        "/usr/bin/python /x/bin/brain search 'ingest pipeline'",
    ]
    assert m.ingest_in_flight(procs) is False


def test_ingest_in_flight_matches_all_ingest_variants() -> None:
    for cmd in ("ingest", "ingest-dir", "ingest-stdin", "ingest-gmail"):
        assert m.ingest_in_flight(
            [f"/usr/bin/python /x/bin/brain {cmd} /some/path"]
        ) is True, cmd
