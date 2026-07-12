"""Regression tests for Wave perf-T3 — ingest transaction + batching.

Locks in the F1/F2/F4/F5/F6 behavior changes:

- F1 (Theme B): ``embedder.embed`` runs BEFORE the write transaction opens and
  ``enricher.summarize`` runs AFTER the commit, while atomicity of the
  document + chunks write and every dedup / draft guard is preserved.
- F2: chunk rows are bulk-inserted (``executemany``) — the stored rows match
  what the chunker produced.
- F6: ``_upsert_source`` upserts via ``ON CONFLICT`` instead of duplicating.

All inputs are synthetic — no PII.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from brain.enrichment import SummaryResult
from brain.errors import OllamaUnavailable
from brain.ingest import (
    ExtractedDoc,
    _upsert_source,
    ingest_document,
)
from brain.ingest.chunker import chunk_text
from tests.conftest import TEST_DATABASE_URL

# ---------------------------------------------------------------------------
# Test doubles that observe the connection's transaction state at call time.
# NOT monkey-patches — explicit fakes accepted via the public kwargs.
# ---------------------------------------------------------------------------


@dataclass
class _RowCountProbeEmbedder:
    """Wrap ``fake_embedder`` and capture, at the instant ``embed`` is called,
    the COMMITTED row count visible from a SECOND psycopg connection.

    Wave perf-T3 hoists ``embedder.embed`` to BEFORE the write transaction
    opens, so the incoming document MUST NOT yet be visible to any other
    connection at embed time (it hasn't been INSERTed). Using a second
    connection (rather than ``self.conn``) sidesteps psycopg3's open-cursor
    behavior in autocommit mode (a held cursor leaves
    ``info.transaction_status`` at INTRANS even when no explicit
    ``with conn.transaction()`` block is active)."""

    inner: Any
    title: str
    visible_row_counts: list[int] = field(default_factory=list)
    calls: int = 0

    @property
    def dim(self) -> int:
        return int(self.inner.dim)

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.calls += 1
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as observer:
            row = observer.execute(
                "SELECT count(*) FROM documents WHERE title=%s", (self.title,)
            ).fetchone()
            assert row is not None
            self.visible_row_counts.append(int(row[0]))
        return self.inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        return int(self.inner.count_tokens(text))


@dataclass
class _FakeEnricher:
    """Minimal call-counting enricher that records call count and optionally
    raises. NOT a monkey-patch — passed via the public ``enricher`` kwarg."""

    model: str = "llama3.1:8b"
    summary_text: str = "Canned post-commit summary."
    raise_exc: BaseException | None = None
    calls: int = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return SummaryResult(summary=self.summary_text, model=self.model)


_BODY = "Paragraph one about widgets.\n\nParagraph two about gadgets.\n\nThree."


def _doc(content: str = _BODY, *, title: str = "Perf T3 doc") -> ExtractedDoc:
    return ExtractedDoc(
        title=title,
        content=content,
        content_type="note",
        source_path=None,
        metadata={},
    )


# ---------------------------------------------------------------------------
# F1 (a) — embedding computed before the transaction; doc + chunks atomic.
# ---------------------------------------------------------------------------


def test_embedding_computed_before_transaction(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """At embed time, a SECOND connection must see ZERO rows for this doc —
    proving the document INSERT has not yet happened and embed is NOT inside
    the write transaction (Wave perf-T3 / F1).
    """
    title = "EmbedBeforeTxn Probe Doc"
    probe = _RowCountProbeEmbedder(inner=fake_embedder, title=title)
    result = ingest_document(
        test_db,
        embedder=probe,  # type: ignore[arg-type]
        doc=_doc(title=title),
        source_kind="manual",
    )
    assert result.created is True
    assert result.document_id is not None
    assert probe.calls == 1
    # Second connection saw NO row for this doc when embed was called →
    # the INSERT has not happened yet, embed is pre-write.
    assert probe.visible_row_counts == [0], (
        f"embed must run before the document INSERT — saw "
        f"{probe.visible_row_counts}"
    )
    # Atomicity: after ingest_document returns, both the document row AND its
    # chunk rows are committed (visible from the same connection).
    doc_row = test_db.execute(
        "SELECT count(*) FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()
    assert doc_row is not None and doc_row[0] == 1
    chunk_count = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (result.document_id,)
    ).fetchone()
    assert chunk_count is not None and chunk_count[0] >= 1


def test_embed_failure_before_txn_leaves_no_partial_row(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """If embedding raises (before the transaction opens) no document or chunk
    row is created — the write transaction never began."""

    @dataclass
    class _BoomEmbedder:
        inner: Any

        @property
        def dim(self) -> int:
            return int(self.inner.dim)

        def embed(
            self, texts: list[str], *, input_type: str = "document"
        ) -> list[list[float]]:
            raise RuntimeError("embed boom")

        def count_tokens(self, text: str) -> int:
            return int(self.inner.count_tokens(text))

    before = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert before is not None
    with pytest.raises(RuntimeError, match="embed boom"):
        ingest_document(
            test_db,
            embedder=_BoomEmbedder(inner=fake_embedder),  # type: ignore[arg-type]
            doc=_doc(content="Distinct body for the embed-boom test. " * 5),
            source_kind="manual",
        )
    after = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert after is not None
    assert after[0] == before[0], "no document row may be committed on embed failure"


# ---------------------------------------------------------------------------
# F1 (b) — enrichment runs AFTER commit; failure leaves the doc committed.
# ---------------------------------------------------------------------------


def test_enrichment_runs_after_commit_writes_summary(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Happy path: post-commit enrichment writes the summary onto the doc."""
    enricher = _FakeEnricher(summary_text="Lede summary.")
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_doc(content="Body content for the post-commit enrich probe. " * 10),
        source_kind="manual",
        enricher=enricher,  # type: ignore[arg-type]
    )
    assert result.document_id is not None
    assert enricher.calls == 1
    row = test_db.execute(
        "SELECT summary, summary_model FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "Lede summary."
    assert row[1] == "llama3.1:8b"


def test_enrichment_uncaught_exception_does_not_rollback_commit(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """DEFINITIVE proof that enrichment runs AFTER commit (Wave perf-T3 / F1):
    if the enricher raises an uncaught exception, the document is STILL present
    in the DB.

    Under the legacy in-transaction enrich, an uncaught exception would
    propagate through the open ``with conn.transaction()`` block and trigger a
    ROLLBACK — the document and its chunks would never commit. Under the
    post-commit design, the txn has already committed when summarize raises;
    the exception is observable but the row survives.

    ``RuntimeError`` is used because :func:`_enrich_post_ingest_hook` only
    catches ``OllamaUnavailable`` and ``EnrichmentError``; anything else
    propagates and exposes the commit ordering.
    """
    title = "EnrichBoom Doc"
    enricher = _FakeEnricher(raise_exc=RuntimeError("enrich boom"))
    with pytest.raises(RuntimeError, match="enrich boom"):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_doc(content="Body for the uncaught-enrich-boom test. " * 10, title=title),
            source_kind="manual",
            enricher=enricher,  # type: ignore[arg-type]
        )
    # Doc + chunks must survive — only possible if commit happened before the
    # exception was raised by summarize().
    row = test_db.execute(
        "SELECT id FROM documents WHERE title=%s", (title,)
    ).fetchone()
    assert row is not None, (
        "document must be committed before enrichment runs — under in-txn "
        "enrich an uncaught exception would have rolled back the INSERT"
    )
    doc_id = row[0]
    chunks = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchone()
    assert chunks is not None and chunks[0] >= 1
    summary = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary is not None and summary[0] is None


def test_enrichment_failure_post_commit_leaves_doc_with_null_summary(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Caught failure-mode complement: when Ollama is down (``OllamaUnavailable``
    is caught inside the hook), the document still commits with summary NULL
    and a WARN is logged — recoverable via ``brain enrich --backfill``."""
    enricher = _FakeEnricher(raise_exc=OllamaUnavailable("connection refused"))
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_doc(content="Body for the enrich-fails-but-commits test. " * 10),
            source_kind="manual",
            enricher=enricher,  # type: ignore[arg-type]
        )
    assert result.created is True
    assert result.document_id is not None
    assert enricher.calls == 1
    row = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()
    assert row is not None and row[0] is None
    chunks = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (result.document_id,)
    ).fetchone()
    assert chunks is not None and chunks[0] >= 1
    assert any("Ollama unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# (b2) — a raw psycopg.Error from a POST-COMMIT source hook must not fail the
# already-committed ingest (Wave 3, item 3.9).
# ---------------------------------------------------------------------------


def test_post_commit_source_hook_psycopg_error_does_not_fail_committed_ingest(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REGRESSION (Wave 3, item 3.9): a raw ``psycopg.Error`` raised by a
    POST-COMMIT source hook must not fail an already-committed ingest.

    The Krisp calendar/contacts refresh runs AFTER the write transaction commits
    (Task 2.11). If it raises a raw ``psycopg.Error`` (e.g. an OperationalError
    from its ``directory_refresh_state`` SELECT), that must be caught + logged —
    the document is already durably committed — never propagated out of
    ``ingest_document`` (mirroring the ``_enrich_post_ingest_hook`` guard).
    """
    boom = psycopg.OperationalError("simulated post-commit hook failure")
    title = "PostCommitHookBoom Doc"
    with (
        patch("brain.ingest._krisp_post_ingest_hook", side_effect=boom) as hook,
        caplog.at_level(logging.WARNING, logger="brain.ingest"),
    ):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_doc(
                content="Krisp transcript body for the hook-boom test. " * 8,
                title=title,
            ),
            source_kind="krisp",
            source_external_id="meet-hook-boom",
        )

    # The post-commit hook was invoked and raised, but ingest still succeeded.
    assert hook.called
    assert result.created is True
    assert result.document_id is not None
    # The document is durably committed despite the hook failure.
    row = test_db.execute(
        "SELECT id FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()
    assert row is not None
    # The failure was surfaced as a WARNING, not swallowed silently.
    assert any(
        r.levelname == "WARNING" and "post-commit" in r.getMessage().lower()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# (c) — the draft partial-window no-op guard still holds.
# ---------------------------------------------------------------------------


def test_draft_partial_window_noop_preserves_published_body(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """A draft-only re-ingest of a published thread is a no-op: the published
    body is preserved and no re-enrich happens."""
    thread_id = "thread-draft-guard"
    enricher = _FakeEnricher(summary_text="Published summary.")
    published_body = "Full published thread body with sent messages. " * 12
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Subject",
            content=published_body,
            content_type="email_thread",
            source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-1",
        draft=False,
        enricher=enricher,  # type: ignore[arg-type]
    )
    assert r1.document_id is not None
    assert enricher.calls == 1

    # Draft-only incoming on the published thread → must be a no-op.
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Subject",
            content="Just a draft reply, partial window. " * 4,
            content_type="email_thread",
            source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-1",
        draft=True,
        enricher=enricher,  # type: ignore[arg-type]
    )
    assert r2.document_id == r1.document_id
    assert r2.body_changed is False
    # Published body + draft flag preserved, no second enrich call.
    row = test_db.execute(
        "SELECT content, draft FROM documents WHERE id=%s", (r1.document_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == published_body, "published body must survive a draft-only re-ingest"
    assert row[1] is False
    assert enricher.calls == 1, "draft no-op must not re-enrich"


# ---------------------------------------------------------------------------
# F2 (d) — executemany inserts exactly the rows the chunker produced.
# ---------------------------------------------------------------------------


def test_chunk_executemany_inserts_expected_rows(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    body = "\n\n".join(f"Paragraph number {i} with some filler text." for i in range(6))
    expected = chunk_text(body, count_tokens=fake_embedder.count_tokens)
    assert expected, "fixture must produce at least one chunk"

    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_doc(content=body, title="Bulk insert doc"),
        source_kind="manual",
        tags=["alpha", "beta"],
    )
    assert result.document_id is not None

    rows = test_db.execute(
        "SELECT chunk_index, content, title_text, tags_text "
        "FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (result.document_id,),
    ).fetchall()
    # Same count + same per-chunk content/index the chunker emitted.
    assert len(rows) == len(expected)
    for row, chunk in zip(rows, expected, strict=True):
        assert row[0] == chunk.index
        assert row[1] == chunk.content
        assert row[2] == "Bulk insert doc"
        assert row[3] == "alpha beta"
    # Every chunk got an embedding of the right dimension.
    dims = test_db.execute(
        "SELECT DISTINCT vector_dims(embedding) FROM chunks WHERE document_id=%s",
        (result.document_id,),
    ).fetchall()
    assert dims == [(fake_embedder.dim,)]


# ---------------------------------------------------------------------------
# F6 (e) — _upsert_source upserts via ON CONFLICT instead of duplicating.
# ---------------------------------------------------------------------------


def test_upsert_source_on_conflict_reuses_existing(
    test_db: psycopg.Connection,
) -> None:
    first = _upsert_source(
        test_db, kind="krisp", external_id="meet-xyz", metadata={"a": 1}
    )
    second = _upsert_source(
        test_db, kind="krisp", external_id="meet-xyz", metadata={"a": 2}
    )
    assert first is not None
    assert second == first, "same (kind, external_id) must reuse the row, not duplicate"
    count = test_db.execute(
        "SELECT count(*) FROM sources WHERE kind='krisp' AND external_id='meet-xyz'"
    ).fetchone()
    assert count is not None and count[0] == 1
    # Legacy behavior preserved: the existing row's metadata is NOT clobbered
    # by the second call's metadata (no-op DO UPDATE).
    meta = test_db.execute(
        "SELECT metadata FROM sources WHERE id=%s", (first,)
    ).fetchone()
    assert meta is not None and meta[0] == {"a": 1}


def test_upsert_source_null_external_id_reuses_existing(
    test_db: psycopg.Connection,
) -> None:
    """The NULL-external_id branch (ON CONFLICT can't target NULL) still
    reuses an existing NULL row instead of inserting a duplicate."""
    first = _upsert_source(
        test_db, kind="manual", external_id=None, metadata={"k": "v"}
    )
    second = _upsert_source(
        test_db, kind="manual", external_id=None, metadata={"k": "v2"}
    )
    assert first is not None
    assert second == first
    count = test_db.execute(
        "SELECT count(*) FROM sources WHERE kind='manual' AND external_id IS NULL"
    ).fetchone()
    assert count is not None and count[0] == 1


def test_upsert_source_no_external_id_no_metadata_returns_none(
    test_db: psycopg.Connection,
) -> None:
    assert _upsert_source(test_db, kind="manual", external_id=None, metadata={}) is None


def test_reingest_same_source_does_not_duplicate_source_row(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Integration: force re-ingesting the same sourced doc must not create a
    second sources row (ON CONFLICT path through the real pipeline)."""
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_doc(content="Sourced body for the dedup test. " * 8),
        source_kind="krisp",
        source_external_id="meet-dedup",
    )
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_doc(content="Sourced body for the dedup test. " * 8),
        source_kind="krisp",
        source_external_id="meet-dedup",
        force=True,
    )
    assert r1.document_id is not None
    count = test_db.execute(
        "SELECT count(*) FROM sources WHERE kind='krisp' AND external_id='meet-dedup'"
    ).fetchone()
    assert count is not None and count[0] == 1
