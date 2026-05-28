"""Unit + integration tests for the brain-rebuild full-corpus orchestrator."""
from __future__ import annotations

from pathlib import Path

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
