"""``ingest_document``'s docstring says when the summary is written (RM-07, BL-12).

The summary has run AFTER the commit, as its own autocommit ``UPDATE``, since
Wave perf-T3 (``_ingest_within_transaction``'s docstring, point 4, and the
post-commit hook call). The outer docstring said "inside the same transaction"
until RM-07. A text test, so the stale sentence cannot come back.
"""
from __future__ import annotations

import inspect
import re

from brain.ingest import _ingest_within_transaction, ingest_document


def _flat(doc: str | None) -> str:
    assert doc is not None
    return re.sub(r"\s+", " ", doc)


def test_ingest_document_docstring_says_summary_runs_after_commit() -> None:
    # Arrange
    doc = _flat(inspect.getdoc(ingest_document))

    # Act
    enrichment = doc[doc.index("Wave Q1-D enrichment") :]
    enrichment = enrichment[: enrichment.index("Wave G1-c graph sync")]

    # Assert
    assert "inside the same transaction" not in doc
    assert "after the commit" in enrichment
    assert "autocommit" in enrichment
    assert "_ingest_within_transaction" in enrichment


def test_inner_docstring_still_states_the_post_commit_order() -> None:
    # The outer docstring now cites this one; pin that it still says so.
    doc = _flat(inspect.getdoc(_ingest_within_transaction))
    assert "Auto-summary enrichment runs AFTER the commit, in autocommit mode" in doc
