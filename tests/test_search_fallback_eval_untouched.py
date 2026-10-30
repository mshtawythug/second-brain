"""``brain eval`` must NOT fall back to text-only search.

Plan: ``docs/plans/2026-10-23-search-fts-fallback.md``. User-facing search falls
back when the query embed fails; eval keeps its degraded-run semantics (spec
``docs/specs/2026-10-16-eval-degraded-all-embedders-design.md``): a transient
failure is a degraded row with NO results, a permanent one aborts. If the
fallback leaked into eval, the degraded row below would carry the FTS hits that
exist for its query, and a baseline would score a text-only ranking as if it
were the hybrid one. All data is synthetic.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.errors import EmbedError
from brain.eval.corpus import EvalQuery
from brain.eval.runner import run_eval
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search
from tests.embed_down import QueryEmbedDownEmbedder

_EVAL_PKG = Path(__file__).resolve().parent.parent / "src" / "brain" / "eval"


def _seed(conn: psycopg.Connection[Any], embedder: Any) -> str:
    """Ingest one synthetic note matching 'orchard'; return its id."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Orchard note",
            content="The orchard pruning schedule moved to early spring.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="manual:orchard-0",
        tags=["garden"],
    )
    assert result.document_id is not None
    return result.document_id


def _query(doc_id: str) -> EvalQuery:
    return EvalQuery(query="orchard", expected_doc_ids=[doc_id], category="semantic")


def test_transient_embed_failure_is_still_a_degraded_row_with_no_results(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    doc_id = _seed(test_db, fake_embedder)
    # Precondition: text-only search WOULD find it, so an empty row below is
    # the absence of a fallback, not the absence of a match.
    fts = hybrid_search(test_db, embedder=fake_embedder, query="orchard", fts_only=True)
    assert [r.document_id for r in fts] == [doc_id]
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)

    # Act
    report = run_eval(test_db, embedder=down, queries=[_query(doc_id)], stages=())

    # Assert
    (row,) = report.results
    assert row.degraded is True
    assert row.actual_doc_ids == []
    assert row.ndcg_at_5 == 0.0


def test_permanent_embed_failure_still_aborts_the_run(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    doc_id = _seed(test_db, fake_embedder)
    down = QueryEmbedDownEmbedder(fake_embedder, transient=False)

    # Act / Assert
    with pytest.raises(EmbedError):
        run_eval(test_db, embedder=down, queries=[_query(doc_id)], stages=())


def test_no_eval_module_imports_the_fallback() -> None:
    """The boundary, structurally: nothing under ``brain.eval`` reaches it."""
    # Arrange
    offenders: list[str] = []

    # Act
    for path in sorted(_EVAL_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(a.name for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("search_fallback" in n for n in names):
                offenders.append(path.name)

    # Assert
    assert list(_EVAL_PKG.rglob("*.py")), "the eval package moved; fix the path"
    assert offenders == []
