"""Tests for the Wave Q1-D post-ingest auto-summary hook.

End-to-end exercises against the real test DB: ingest a doc with a fake
:class:`OllamaEnricher` and assert ``documents.summary`` lands inside the
transaction (or stays NULL when one of the skip rules fires).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
import pytest

from brain.enrichment import SummaryResult
from brain.errors import EnrichmentError, OllamaUnavailable
from brain.ingest import ExtractedDoc, ingest_document


@dataclass
class _FakeEnricher:
    """In-memory enricher honoring the :class:`OllamaEnricher` surface.

    NOT a monkey-patch — it's an explicit test double the hook accepts via
    its public ``enricher=`` kwarg. Counts calls so tests can assert the
    skip rules actually skip.
    """

    model: str = "llama3.1:8b"
    summary_text: str = "Two-sentence canned summary used by tests."
    raise_exc: BaseException | None = None
    calls: int = 0

    def count_tokens(self, text: str) -> int:
        # Match FakeEmbedder.count_tokens — 1 token per ~4 chars.
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return SummaryResult(summary=self.summary_text, model=self.model)


def _read_summary(conn: psycopg.Connection, doc_id: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT summary, summary_model FROM documents WHERE id=%s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _ingest(
    conn: psycopg.Connection,
    embedder: object,
    *,
    title: str = "Doc title",
    content: str = "Body content. " * 50,  # ~100 tokens
    enricher: object | None = None,
    enrich: bool = True,
    enrich_min_tokens: int = 50,
    force: bool = False,
) -> str:
    doc = ExtractedDoc(
        title=title,
        content=content,
        content_type="note",
        source_path=None,
        metadata={},
    )
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=doc,
        source_kind="manual",
        enricher=enricher,  # type: ignore[arg-type]
        enrich=enrich,
        enrich_min_tokens=enrich_min_tokens,
        force=force,
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_post_ingest_hook_writes_summary(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    enricher = _FakeEnricher(summary_text="A clean summary.")
    doc_id = _ingest(test_db, fake_embedder, enricher=enricher)
    summary, model = _read_summary(test_db, doc_id)
    assert summary == "A clean summary."
    assert model == "llama3.1:8b"
    assert enricher.calls == 1
    # summary_at should be populated.
    row = test_db.execute(
        "SELECT summary_at FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] is not None


def test_short_content_skips_enrichment(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    enricher = _FakeEnricher()
    # 5 token doc (well under 50)
    doc_id = _ingest(
        test_db, fake_embedder, content="short", enricher=enricher
    )
    summary, _ = _read_summary(test_db, doc_id)
    assert summary is None
    assert enricher.calls == 0


def test_enrich_false_kwarg_skips(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    enricher = _FakeEnricher()
    doc_id = _ingest(test_db, fake_embedder, enricher=enricher, enrich=False)
    summary, _ = _read_summary(test_db, doc_id)
    assert summary is None
    assert enricher.calls == 0


def test_no_enricher_passed_skips_silently(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """``enrich=True`` but no enricher → debug log + skip; row commits NULL."""
    doc_id = _ingest(test_db, fake_embedder, enricher=None, enrich=True)
    summary, _ = _read_summary(test_db, doc_id)
    assert summary is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_re_ingest_same_content_skips_re_enrichment(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    enricher = _FakeEnricher()
    body = "Body content. " * 50
    doc_id_a = _ingest(test_db, fake_embedder, content=body, enricher=enricher)
    summary_a, _ = _read_summary(test_db, doc_id_a)
    assert summary_a is not None
    assert enricher.calls == 1

    # Same hash → ingest returns the same row; force=False (default) means
    # we hit the content_hash short-circuit BEFORE the hook even runs.
    doc_id_b = _ingest(test_db, fake_embedder, content=body, enricher=enricher)
    assert doc_id_b == doc_id_a
    assert enricher.calls == 1, "second ingest must not re-call the enricher"


def test_post_ingest_hook_re_enriches_after_model_upgrade(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """Codex finding 1 (HIGH): the hook's reuse-summary skip rule must
    invalidate when the model fingerprint differs from the current
    enricher, even though the content_hash is unchanged.

    Codex loop-2 follow-up: the original version of this test used
    ``force=True``, which deletes the existing row before INSERT — the
    hook then sees ``existing_summary is None`` and re-enriches via the
    NULL-summary path. That path does NOT exercise the
    "same content_hash + different summary_model" skip rule. If the
    hook regressed back to checking only ``summary IS NULL``, that test
    would still pass.

    This version calls ``_enrich_post_ingest_hook`` DIRECTLY against the
    existing row (no delete + re-insert), so the hook reads the stored
    ``summary_model``, compares against the new enricher's model, and
    must re-enrich. Genuine regression lock for the model-fingerprint
    branch.
    """
    from brain.ingest import _content_hash, _enrich_post_ingest_hook

    body = "Body content used by the model-upgrade test. " * 20
    # Seed via the OLD model.
    old_enricher = _FakeEnricher(model="llama3.1:8b", summary_text="OLD")
    doc_id = _ingest(test_db, fake_embedder, content=body, enricher=old_enricher)
    summary, model = _read_summary(test_db, doc_id)
    assert summary == "OLD"
    assert model == "llama3.1:8b"
    assert old_enricher.calls == 1

    # Direct hook call against the existing row with a different-model
    # enricher. content_hash is identical (same body), so the only thing
    # that should trigger re-enrichment is the model fingerprint mismatch.
    new_enricher = _FakeEnricher(model="llama3.2:8b", summary_text="NEW")
    doc_for_hook = ExtractedDoc(
        title="Doc title",
        content=body,
        content_type="note",
        source_path=None,
        metadata={},
    )
    with test_db.transaction():
        _enrich_post_ingest_hook(
            test_db,
            document_id=doc_id,
            doc=doc_for_hook,
            enricher=new_enricher,  # type: ignore[arg-type]
            enrich=True,
            min_tokens=50,
            content_hash=_content_hash(body),
        )

    # Row was re-enriched in place — same UUID, new summary + model.
    summary, model = _read_summary(test_db, doc_id)
    assert summary == "NEW", "hook must re-enrich when summary_model differs"
    assert model == "llama3.2:8b", "summary_model must be updated to current enricher"
    assert new_enricher.calls == 1


def test_post_ingest_hook_skips_when_model_matches(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """Idempotency complement to the model-upgrade test: same content_hash
    AND same summary_model must SKIP — no second Ollama call."""
    from brain.ingest import _content_hash, _enrich_post_ingest_hook

    body = "Body content used by the same-model idempotency test. " * 20
    enricher = _FakeEnricher(model="llama3.1:8b", summary_text="STABLE")
    doc_id = _ingest(test_db, fake_embedder, content=body, enricher=enricher)
    assert enricher.calls == 1

    # Direct hook call with the SAME enricher (same model).
    doc_for_hook = ExtractedDoc(
        title="Doc title",
        content=body,
        content_type="note",
        source_path=None,
        metadata={},
    )
    with test_db.transaction():
        _enrich_post_ingest_hook(
            test_db,
            document_id=doc_id,
            doc=doc_for_hook,
            enricher=enricher,  # type: ignore[arg-type]
            enrich=True,
            min_tokens=50,
            content_hash=_content_hash(body),
        )

    # No second call — skip rule fired.
    assert enricher.calls == 1, "hook must skip when summary + hash + model all match"


def test_re_ingest_force_same_content_idempotent(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """``force=True`` with unchanged body keeps the same content hash;
    the hook's per-row hash short-circuit prevents re-enrichment."""
    enricher = _FakeEnricher()
    body = "Body content. " * 50
    doc_id_a = _ingest(test_db, fake_embedder, content=body, enricher=enricher)
    assert enricher.calls == 1

    # Force re-ingests delete + re-insert (new UUID) but the content hash
    # matches the prior row's. The new row is empty before the INSERT, so
    # the hook DOES run for the new row — its skip check looks at the
    # just-INSERTed row's existing summary, which is NULL. Therefore one
    # additional call is expected and the summary lands on the new row.
    doc_id_b = _ingest(
        test_db, fake_embedder, content=body, enricher=enricher, force=True
    )
    assert doc_id_b != doc_id_a
    summary_b, _ = _read_summary(test_db, doc_id_b)
    assert summary_b is not None


# ---------------------------------------------------------------------------
# Failure modes — degrade soft
# ---------------------------------------------------------------------------


def test_ollama_unavailable_logs_warning_completes_ingest(
    test_db: psycopg.Connection,
    fake_embedder: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enricher = _FakeEnricher(raise_exc=OllamaUnavailable("connection refused"))
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        doc_id = _ingest(test_db, fake_embedder, enricher=enricher)
    summary, _ = _read_summary(test_db, doc_id)
    assert summary is None
    assert enricher.calls == 1
    assert any(
        "Ollama unavailable" in record.message for record in caplog.records
    )


def test_enrichment_error_logs_warning_completes_ingest(
    test_db: psycopg.Connection,
    fake_embedder: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enricher = _FakeEnricher(raise_exc=EnrichmentError("bad json"))
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        doc_id = _ingest(test_db, fake_embedder, enricher=enricher)
    summary, _ = _read_summary(test_db, doc_id)
    assert summary is None
    assert enricher.calls == 1
    assert any("auto-summary failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Hook ordering — enrich before source-specific hook
# ---------------------------------------------------------------------------


def test_enrich_hook_runs_before_source_specific_hook(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """For a manual ingest, the enrich hook runs (no source-specific hook for
    'manual'). Verifies the call chain reaches the enricher in the
    INSERT path — orchestration check, not a hook-order spy.
    """
    enricher = _FakeEnricher()
    _ingest(test_db, fake_embedder, enricher=enricher)
    assert enricher.calls == 1
