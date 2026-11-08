"""``doc_embeddings.backfill`` against the real test database (RM-20).

Missing-only by default; ``stale=True`` also refreshes a changed text, another
backend's row, and another model's row (D10); the F6 veto under a hosted
embedder (AC3); the failure and refusal paths; and ids-and-counts-only logging.
All text is synthetic.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest

from brain.doc_embeddings import (
    BASIS_TITLE_HEAD,
    BASIS_TITLE_SUMMARY,
    DOC_BASIS_MAX_TOKENS,
    DOC_SUMMARY_CHAR_CAP,
    DocEmbeddingError,
    backfill,
)
from brain.embeddings import NullEmbedder, OllamaEmbedError
from brain.search import embedder_identity
from tests.conftest import CountingEmbedder
from tests.doc_leg_corpus import (
    FIT_SUMMARY,
    FIT_TITLE,
    HostedWordEmbedder,
    ModelWordEmbedder,
    reconcile,
    seed,
)
from tests.saved_search_fakes import WordEmbedder, ingest

#: Placed in a title, a summary and a body; it must never reach a log or the output.
MARKER = "synthetic-docembed-marker-5d1e"


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


def _rows(conn: psycopg.Connection[Any]) -> dict[str, tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT document_id::text, basis, basis_hash, embedder FROM document_embeddings"
    ).fetchall()
    return {str(r[0]): (str(r[1]), str(r[2]), str(r[3])) for r in rows}


class _Failing(WordEmbedder):
    """Embeds the first ``ok_batches`` calls, then fails as a stopped Ollama does."""

    def __init__(self, ok_batches: int) -> None:
        super().__init__()
        self.ok_batches = ok_batches
        self.calls = 0

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls += 1
        if self.calls > self.ok_batches:
            raise OllamaEmbedError(f"connection refused {MARKER}", transient=True)
        return super().embed(texts, input_type)


def test_every_document_gets_one_vector_with_its_basis(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    report = backfill(test_db, word)

    rows = _rows(test_db)
    assert report.embedded == len(ids) == len(rows)
    assert report.remaining == 0
    assert report.stopped_by is None
    assert rows[ids["fit"]][0] == BASIS_TITLE_SUMMARY
    assert rows[ids["other-0"]][0] == BASIS_TITLE_HEAD
    assert {r[2] for r in rows.values()} == {embedder_identity(word)}


def test_a_second_run_embeds_nothing(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)
    counting = CountingEmbedder(word)
    backfill(test_db, counting)  # the rows carry the counting wrapper's identity
    before = counting.embed_calls

    assert backfill(test_db, counting).embedded == 0
    assert backfill(test_db, counting, stale=True).embedded == 0
    assert counting.embed_calls == before


def test_limit_caps_one_run_and_the_rest_remains(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    report = backfill(test_db, word, limit=2, batch_size=2)

    assert report.embedded == 2
    assert report.remaining == len(ids) - 2
    assert len(_rows(test_db)) == 2


def test_the_default_does_not_refresh_a_changed_summary_but_stale_does(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    backfill(test_db, word)
    before = _rows(test_db)[ids["fit"]][1]
    test_db.execute(
        "UPDATE documents SET summary = %s WHERE id = %s", ("a new synthetic summary", ids["fit"])
    )

    assert backfill(test_db, word).embedded == 0
    assert _rows(test_db)[ids["fit"]][1] == before

    report = backfill(test_db, word, stale=True)

    assert report.embedded == 1
    assert _rows(test_db)[ids["fit"]][1] != before


def test_stale_re_embeds_a_row_another_backend_wrote(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # AC4: the row's ``embedder`` names another backend.
    ids = seed(test_db, word)
    backfill(test_db, word)
    test_db.execute(
        "UPDATE document_embeddings SET embedder = 'brain.embeddings.VoyageEmbedder|x|4096' "
        "WHERE document_id = %s",
        (ids["other-1"],),
    )

    report = backfill(test_db, word, stale=True)

    assert report.embedded == 1
    assert _rows(test_db)[ids["other-1"]][2] == embedder_identity(word)


def test_a_change_of_model_alone_makes_every_row_stale(
    test_db: psycopg.Connection[Any],
) -> None:
    # D10: same class, same dim, another model.
    first, second = ModelWordEmbedder("model-one"), ModelWordEmbedder("model-two")
    reconcile(test_db, first)
    ids = seed(test_db, first)
    backfill(test_db, first)
    assert embedder_identity(first) != embedder_identity(second)

    assert backfill(test_db, second, stale=True).embedded == len(ids)
    assert {r[2] for r in _rows(test_db).values()} == {embedder_identity(second)}
    assert backfill(test_db, second, stale=True).embedded == 0


def test_rows_from_another_embedder_count_as_remaining(
    test_db: psycopg.Connection[Any],
) -> None:
    first, second = ModelWordEmbedder("model-one"), ModelWordEmbedder("model-two")
    reconcile(test_db, first)
    ids = seed(test_db, first)
    backfill(test_db, first)

    report = backfill(test_db, second, limit=1)

    # Missing-only: every document already has a row, so nothing is embedded,
    # and all of them are still without a vector from THIS embedder, each
    # because another model wrote it (the CLI's ``--stale`` hint reads this).
    assert report.embedded == 0
    assert report.remaining == len(ids)
    assert report.foreign == len(ids)
    assert backfill(test_db, second, stale=True).foreign == 0


@pytest.mark.parametrize("stale", [False, True], ids=["default", "stale"])
def test_a_hosted_embedder_is_never_sent_a_confidential_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder, stale: bool
) -> None:
    # AC3, first half: assert on what the hosted fake received, on the default
    # (missing-only) path users run and on ``--stale``.
    ids = seed(test_db, word, fit_sensitivity="confidential")
    hosted = HostedWordEmbedder()

    report = backfill(test_db, hosted, stale=stale)

    assert hosted.sent
    assert not any(FIT_TITLE in text for text in hosted.sent)
    assert f"{FIT_TITLE}\n{FIT_SUMMARY}" not in hosted.sent
    assert ids["fit"] not in _rows(test_db)
    assert report.embedded == len(ids) - 1
    assert report.skipped_confidential == 1
    assert report.remaining == 0


@pytest.mark.parametrize("stale", [False, True], ids=["default", "stale"])
def test_a_hosted_embedder_never_reads_a_confidential_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder, stale: bool
) -> None:
    # sec-127b I1: the page statement's own filter, apart from the re-check
    # just before the embed call (which would also keep the text from being
    # SENT). The confidential row must not even be read: no basis is built
    # from it, so ``count_tokens`` never sees its title.
    ids = seed(test_db, word, fit_sensitivity="confidential")
    hosted = HostedWordEmbedder()

    report = backfill(test_db, hosted, stale=stale)

    # Refuses: the confidential document was never measured.
    assert not any(FIT_TITLE in text for text in hosted.measured)
    # Keeps: every other document was read and embedded as usual.
    assert any("Harbour log 0" in text for text in hosted.measured)
    assert report.embedded == len(ids) - 1


def test_a_local_embedder_does_embed_the_confidential_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word, fit_sensitivity="confidential")
    counting = CountingEmbedder(word)

    report = backfill(test_db, counting)

    assert ids["fit"] in _rows(test_db)
    assert report.skipped_confidential == 0
    assert report.embedded == len(ids)


def test_an_embedder_without_vectors_is_refused_before_any_read(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(DocEmbeddingError, match="BRAIN_EMBEDDER=none"):
        backfill(test_db, NullEmbedder())


def test_a_missing_table_is_refused_with_the_init_hint(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    with test_db.transaction():
        test_db.execute("ALTER TABLE document_embeddings RENAME TO document_embeddings_aside")
        try:
            with pytest.raises(DocEmbeddingError, match="brain init"):
                backfill(test_db, word)
        finally:
            test_db.execute("ALTER TABLE document_embeddings_aside RENAME TO document_embeddings")


def test_an_embed_failure_stops_the_run_and_keeps_the_written_batches(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    failing = _Failing(ok_batches=1)

    report = backfill(test_db, failing, batch_size=2)

    assert report.embedded == 2
    assert report.stopped_by == "OllamaEmbedError"
    assert len(_rows(test_db)) == 2
    assert report.remaining == len(ids) - 2
    assert backfill(test_db, word).embedded == len(ids) - 2


def test_a_document_deleted_mid_run_is_skipped_not_fatal(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    class _DeletingFirst(WordEmbedder):
        def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
            test_db.execute("DELETE FROM documents WHERE id = %s", (ids["fit"],))
            return super().embed(texts, input_type)

    report = backfill(test_db, _DeletingFirst())

    assert ids["fit"] not in _rows(test_db)
    assert len(_rows(test_db)) == len(ids) - 1
    assert report.stopped_by is None


def test_logs_carry_counts_and_never_text(
    test_db: psycopg.Connection[Any], word: WordEmbedder, caplog: pytest.LogCaptureFixture
) -> None:
    # (e): the marker is in a title, a summary and a body.
    doc = ingest(test_db, word, title=f"Title {MARKER}", content=f"Body {MARKER}")
    test_db.execute("UPDATE documents SET summary = %s WHERE id = %s", (f"Sum {MARKER}", doc))
    ingest(test_db, word, title="Second", content=f"Head {MARKER}")
    caplog.set_level(logging.DEBUG, logger="brain")

    backfill(test_db, word)
    backfill(test_db, _Failing(ok_batches=0), stale=True)

    assert caplog.records
    assert MARKER not in caplog.text


def test_no_foreign_rows_is_reported_as_zero(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)

    report = backfill(test_db, word, limit=1)

    assert report.foreign == 0
    assert report.remaining > 0


class _FlipsToConfidential(HostedWordEmbedder):
    """Marks ``target`` confidential the first time a basis is measured.

    That is after the page was read and before the batch is sent: the window a
    concurrent ``brain edit`` would hit (sec-127 L1).
    """

    def __init__(self, conn: psycopg.Connection[Any], target: str) -> None:
        super().__init__()
        self._conn = conn
        self._target = target
        self._flipped = False

    def count_tokens(self, text: str) -> int:
        if not self._flipped:
            self._flipped = True
            self._conn.execute(
                "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s",
                (self._target,),
            )
        return super().count_tokens(text)


def test_a_document_marked_confidential_mid_batch_is_not_sent(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    flipping = _FlipsToConfidential(test_db, ids["fit"])

    report = backfill(test_db, flipping)

    # Refuses: the flipped document's text never reached the hosted embedder.
    assert not any(FIT_TITLE in text for text in flipping.sent)
    assert ids["fit"] not in _rows(test_db)
    # Keeps: the rest of the same batch is embedded as usual.
    assert report.embedded == len(ids) - 1
    assert {ids[f"other-{i}"] for i in range(4)} <= set(_rows(test_db))


def test_a_huge_summary_is_cut_and_the_run_succeeds(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    doc = ingest(test_db, word, title="Synthetic long summary", content="short body")
    huge = " ".join(f"word{i}" for i in range(DOC_SUMMARY_CHAR_CAP))  # far past the cap
    test_db.execute("UPDATE documents SET summary = %s WHERE id = %s", (huge, doc))
    hosted = HostedWordEmbedder()

    report = backfill(test_db, hosted)

    assert report.embedded == 1
    (sent,) = hosted.sent
    assert hosted.count_tokens(sent) <= DOC_BASIS_MAX_TOKENS
    assert _rows(test_db)[doc][0] == BASIS_TITLE_SUMMARY
