"""Static smoke test for the Quartz contentIndex draft-filter step.

Per CLAUDE.md the project does not yet ship a JS test harness for the
``quartz_overrides/`` overlay. The closest existing pattern is
``tests/test_quartz_overrides_parse.py`` (regex-based static checks). This
file is the same flavor of static check, scoped to the P1.6 contract:

- The contentIndex emitter source contains a filter step that drops the
  entry when ``frontmatter.draft === true``.
- That step lives BEFORE the tier/source/linkRecords graft block (so a
  draft entry never gets the brain-extension fields written into the
  JSON, then deleted later — the cheaper / correct ordering).
- The filter uses ``delete parsed[slug]`` (or equivalent) so the entry
  is gone from ``contentIndex.json`` rather than zero-filled.

Limitation: this file only asserts the FILTER LINE EXISTS in the source.
A full end-to-end test would invoke ``npx quartz build`` against a
fixture vault and parse the emitted ``static/contentIndex.json``. That
needs npx + a Quartz workspace, which is not part of the test image —
flagged in the P1.6 DONE report. The transformer parse-smoke
(``test_quartz_overrides_parse.py``) catches gross syntactic regressions
on every file, including this one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EMITTER_PATH = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "plugins"
    / "emitters"
    / "contentIndex.ts"
)


@pytest.fixture(scope="module")
def emitter_source() -> str:
    """Read the contentIndex emitter source once per test module."""
    assert EMITTER_PATH.is_file(), f"missing emitter at {EMITTER_PATH}"
    return EMITTER_PATH.read_text(encoding="utf-8")


def test_emitter_filters_draft_entries(emitter_source: str) -> None:
    """The post-processor checks ``fm.draft === true`` and skips the entry.

    Strict literal match: the filter must use ``=== true`` (truthy
    coercion would also drop a string ``"draft: true"`` typo, which we
    explicitly do NOT want — only the boolean form should quarantine).
    """
    assert "fm.draft === true" in emitter_source, (
        "draft filter missing or weakened — expected `fm.draft === true` "
        "guard in contentIndex.ts. See P1.6."
    )


def test_emitter_filters_confidential_entries(emitter_source: str) -> None:
    """F6: the same filter drops ``sensitivity: confidential`` entries.

    This is the publish half of F6's trust boundary, and it is load-bearing:
    plan decision Q7 declines to filter ``hybrid_search`` *because the local CLI
    is inside the trust boundary*, which concedes F6's whole security value to
    the egress points. The other one is the hosted-embedder veto at ingest; this
    is the one that governs what reaches a published site.

    Strict string equality, mirroring ``draft``'s ``=== true``. A truthy check
    would drop ``sensitivity: normal`` too — the exact opposite of the intent,
    and it would silently empty the entire wiki.
    """
    assert 'fm.sensitivity === "confidential"' in emitter_source, (
        'sensitivity filter missing or weakened — expected '
        '`fm.sensitivity === "confidential"` in contentIndex.ts. See F6.'
    )


def test_emitter_confidential_filter_shares_the_draft_branch(
    emitter_source: str,
) -> None:
    """Both quarantine rules live in ONE branch, so they cannot diverge.

    Keeping them in the same ``if`` is what guarantees a confidential document
    is dropped everywhere ``draft`` is — Explorer, graph, and search — rather
    than in whichever subset a second, separately-maintained branch happened to
    cover. It also means the ordering guarantee asserted below (filter before
    the graft block) applies to both without a second assertion.
    """
    assert 'fm.draft === true || fm.sensitivity === "confidential"' in emitter_source, (
        "the draft and sensitivity quarantine rules must share a single "
        "branch; splitting them invites one to be updated without the other"
    )


def test_emitter_filter_drops_entry_via_delete(emitter_source: str) -> None:
    """The filter actually removes the entry rather than zero-filling it.

    ``delete parsed[slug]`` is the contract — the consumers (Search,
    Graph, Explorer) all key on ``Object.entries(contentIndex)``, so a
    surviving entry with empty fields would still appear in the
    explorer tree / graph. Removal is the only safe quarantine.
    """
    assert "delete parsed[slug]" in emitter_source, (
        "draft filter must `delete parsed[slug]` to drop the entry from "
        "contentIndex.json. Zero-filling would leave the slug visible in "
        "Explorer / Graph / Search."
    )


def test_emitter_draft_filter_runs_before_grafting(emitter_source: str) -> None:
    """The filter sits before the tier/source/linkRecords graft block.

    Ordering matters for two reasons: (1) running the graft on an entry
    we're about to drop is wasted work, and (2) some graft branches read
    fields we don't want to populate on a draft (the doc shouldn't be
    findable in any flavor of the index).

    The check looks for the filter's ``=== true`` guard appearing
    earlier in the file than the ``// brain-extension: surface the vault
    `tier`...`` comment that introduces the graft block. Both markers
    are pinned in the emitter source as part of this overlay and aren't
    expected to move; if upstream's plugin shape changes such that both
    have to be relocated together, this assertion still holds because
    the filter is logically required to come first.
    """
    filter_marker = "fm.draft === true"
    graft_marker = "// brain-extension: surface the vault"
    filter_idx = emitter_source.find(filter_marker)
    graft_idx = emitter_source.find(graft_marker)
    assert filter_idx >= 0, "draft filter not found"
    assert graft_idx >= 0, "graft block marker not found"
    assert filter_idx < graft_idx, (
        "draft filter must precede the tier/source/linkRecords graft "
        "block — running the graft on a soon-to-be-deleted entry is "
        "wasted work and may surface partial fields."
    )
