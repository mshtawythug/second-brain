"""Integration tests for brain.eval.runner — run_eval() and _normalize_ids()."""

import hashlib
import unittest.mock
from datetime import UTC, date, datetime, timedelta

import httpx
import psycopg
import pytest
from voyageai import error as voyage_error

from brain.embeddings import OllamaEmbedContextOverflow, OllamaEmbedError, VoyageEmbedError
from brain.errors import EmbedError
from brain.eval.corpus import EvalQuery
from brain.eval.errors import EvalCorpusError
from brain.eval.runner import (
    EvalReport,
    _normalize_ids,
    _utc_midnight,
    degraded_results,
    run_eval,
)
from brain.ingest import Embedder, ExtractedDoc, ingest_document
from tests.embed_failure_doubles import OLLAMA_OVERFLOW, arctic_failing_on, voyage_failing_on

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(
    test_db: psycopg.Connection, embedder, title: str, content: str, source_kind: str = "manual"
) -> str:
    """Ingest one document and return its full UUID string."""
    ingest_document(
        test_db,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=f"{title}: {content}",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=f"{source_kind}:{title}",
    )
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = %s",
        (title,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _make_query(query: str, expected_id: str, category: str = "semantic", **kwargs) -> EvalQuery:
    return EvalQuery(query=query, expected_doc_ids=[expected_id], category=category, **kwargs)


# ---------------------------------------------------------------------------
# _normalize_ids — unit tests (direct calls, no run_eval)
# ---------------------------------------------------------------------------


def test_normalize_ids_passes_full_uuid_through(test_db, fake_embedder):
    """Full UUIDs (32+ chars or containing hyphens) are returned unchanged."""
    full_uuid = "12345678-1234-1234-1234-123456789abc"
    # No DB lookup for full UUIDs — a missing doc is not an error at this layer.
    result = _normalize_ids(test_db, [full_uuid])
    assert result == [full_uuid]


def test_normalize_ids_resolves_8char_prefix(test_db, fake_embedder):
    """An 8-char hex prefix is resolved to the full UUID via a LIKE query."""
    doc_id = _seed(test_db, fake_embedder, "Prefix Doc", "prefix resolution test content")
    prefix = doc_id[:8]
    resolved = _normalize_ids(test_db, [prefix])
    assert resolved == [doc_id]


def test_normalize_ids_drops_stale_prefix(test_db, fake_embedder):
    """A prefix that matches zero documents is silently dropped (stale corpus)."""
    result = _normalize_ids(test_db, ["00000000"])
    assert result == []


def test_normalize_ids_ambiguous_prefix_raises(test_db, fake_embedder):
    """A prefix matching multiple documents raises EvalCorpusError."""
    # Insert two documents with UUIDs sharing the same 4-char prefix.
    # We control the ID by inserting directly; the tsv column is generated.
    uid1 = "abcd0000-0000-0000-0000-000000000001"
    uid2 = "abcd1111-0000-0000-0000-000000000002"
    pairs = [
        (uid1, "Ambig Doc One", "content one alpha"),
        (uid2, "Ambig Doc Two", "content two beta"),
    ]
    for uid, title, extra in pairs:
        h = hashlib.md5(uid.encode()).hexdigest()
        test_db.execute(
            """INSERT INTO documents (id, title, content, content_hash, content_type)
               VALUES (%s::uuid, %s, %s, %s, 'txt')""",
            (uid, title, extra, h),
        )

    with pytest.raises(EvalCorpusError, match="ambiguous"):
        _normalize_ids(test_db, ["abcd"])


# ---------------------------------------------------------------------------
# run_eval — integration tests
# ---------------------------------------------------------------------------


def test_run_eval_returns_one_result_per_query(test_db, fake_embedder):
    """Three seeded docs + three queries → three EvalResult entries in input order."""
    id1 = _seed(test_db, fake_embedder, "Alpha Doc", "alpha topic discussion")
    id2 = _seed(test_db, fake_embedder, "Beta Doc", "beta topic discussion")
    id3 = _seed(test_db, fake_embedder, "Gamma Doc", "gamma topic discussion")

    queries = [
        _make_query("alpha topic", id1),
        _make_query("beta topic", id2),
        _make_query("gamma topic", id3),
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert isinstance(report, EvalReport)
    assert len(report.results) == 3
    assert [r.query for r in report.results] == ["alpha topic", "beta topic", "gamma topic"]


def test_run_eval_threads_source_filter_through(test_db, fake_embedder):
    """A query with source_filter='krisp' ignores manual docs."""
    _seed(test_db, fake_embedder, "Manual Note", "krisp notes manual content", source_kind="manual")
    _seed(
        test_db, fake_embedder, "Krisp Meeting", "krisp notes meeting content", source_kind="krisp"
    )

    queries = [
        EvalQuery(
            query="krisp notes",
            expected_doc_ids=["00000000"],  # nil UUID — we just check source filtering
            category="meeting",
            source_filter="krisp",
        )
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)
    assert len(report.results) == 1
    # All returned docs must be krisp docs (the source filter was applied).
    for doc_id in report.results[0].actual_doc_ids:
        row = test_db.execute(
            """SELECT s.kind FROM documents d
               JOIN sources s ON s.id = d.source_id
               WHERE d.id::text = %s""",
            (doc_id,),
        ).fetchone()
        if row is not None:
            assert row[0] == "krisp"


def test_run_eval_resolves_8char_prefixes(test_db, fake_embedder):
    """Expected IDs given as 8-char prefixes are resolved to full UUIDs in the result."""
    doc_id = _seed(test_db, fake_embedder, "Prefix Test Doc", "prefix resolution data here")
    prefix = doc_id[:8]

    queries = [_make_query("prefix resolution data", prefix)]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert len(report.results) == 1
    # The stored expected_doc_ids should be the resolved full UUID, not the prefix.
    assert report.results[0].expected_doc_ids == [doc_id]


def test_run_eval_handles_ollama_failure_gracefully(test_db, fake_embedder):
    """When the embedder raises OllamaEmbedError, actual_doc_ids=[] and metrics=0.0."""
    _seed(test_db, fake_embedder, "Embed Fail Doc", "embedding failure test document")
    queries = [_make_query("embedding failure test", "00000000")]

    # Patch embed on the fake_embedder to raise a TRANSIENT OllamaEmbedError (a
    # timeout) for this test; a permanent one aborts the run instead.
    timeout = OllamaEmbedError("test", transient=True)
    with unittest.mock.patch.object(fake_embedder, "embed", side_effect=timeout):
        report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert len(report.results) == 1
    r = report.results[0]
    assert r.actual_doc_ids == []
    assert r.ndcg_at_5 == 0.0
    assert r.mrr == 0.0
    assert r.recall_at_20 == 0.0
    assert r.degraded is True


def test_run_eval_flags_only_the_query_whose_embed_failed(
    test_db, fake_embedder, make_flaky_embedder
):
    """A failed embed marks THAT row degraded (metrics 0.0); a healthy row is not.

    The flag is what lets ``--record-baseline`` refuse and ``--diff`` exclude:
    without it a timed-out embed is indistinguishable from a genuine 0.0 miss.
    """
    healthy_id = _seed(test_db, fake_embedder, "Flag Healthy Doc", "flagrunner healthy content")
    _seed(test_db, fake_embedder, "Flag Broken Doc", "flagrunner broken content")
    failing = "flagrunner broken query"
    queries = [
        _make_query("flagrunner healthy content", healthy_id),
        _make_query(failing, healthy_id),
    ]

    report = run_eval(test_db, embedder=make_flaky_embedder({failing}), queries=queries)

    healthy, broken = report.results
    assert healthy.degraded is False
    assert healthy.actual_doc_ids  # it really ran
    assert broken.degraded is True
    assert (broken.ndcg_at_5, broken.mrr, broken.recall_at_20) == (0.0, 0.0, 0.0)
    assert degraded_results(report) == [broken]


def test_degraded_flag_does_not_stick_after_a_failed_query(
    test_db, fake_embedder, make_flaky_embedder
):
    """The flag is per query: a healthy query AFTER a failed one is not degraded.

    Ordered [healthy, failing, healthy] so a flag initialised once outside the
    loop (and never reset) would mark the third row too.
    """
    doc_id = _seed(test_db, fake_embedder, "Sticky Flag Doc", "stickyflag content")
    failing = "stickyflag broken query"
    queries = [
        _make_query("stickyflag content first", doc_id),
        _make_query(failing, doc_id),
        _make_query("stickyflag content last", doc_id),
    ]

    report = run_eval(test_db, embedder=make_flaky_embedder({failing}), queries=queries)

    assert [r.degraded for r in report.results] == [False, True, False]


# ---------------------------------------------------------------------------
# Every backend, transient vs permanent
# (spec docs/specs/2026-10-16-eval-degraded-all-embedders-design.md)
#
# Real ArcticEmbedder / VoyageEmbedder over fake transports, so the backend's
# own classification and run_eval's handling are exercised together. Failing
# texts are unique per test: brain.search caches query embeds per embedder.
# ---------------------------------------------------------------------------


def test_run_eval_degrades_a_voyage_rate_limited_query(test_db, fake_embedder):
    """Rule-12 regression (PR #33 gap 1): run_eval caught only OllamaEmbedError,
    so under BRAIN_EMBEDDER=voyage ONE rate-limited query escaped as a
    VoyageEmbedError and the whole run was lost."""
    doc_id = _seed(test_db, fake_embedder, "Voyage Degrade Doc", "voyagedeg healthy content")
    failing = "voyagedeg rate limited query"
    embedder = voyage_failing_on(
        fake_embedder, {failing: lambda: voyage_error.RateLimitError("synthetic 429")}
    )
    queries = [_make_query("voyagedeg healthy content", doc_id), _make_query(failing, doc_id)]

    report = run_eval(test_db, embedder=embedder, queries=queries)

    healthy, broken = report.results
    assert healthy.degraded is False
    assert healthy.actual_doc_ids
    assert broken.degraded is True
    assert (broken.ndcg_at_5, broken.mrr, broken.recall_at_20) == (0.0, 0.0, 0.0)
    assert degraded_results(report) == [broken]


def test_run_eval_aborts_on_a_permanent_voyage_failure(test_db, fake_embedder):
    """A bad API key fails every query the same way: abort, do not degrade."""
    doc_id = _seed(test_db, fake_embedder, "Voyage Auth Doc", "voyageauth content")
    failing = "voyageauth rejected key query"
    embedder = voyage_failing_on(
        fake_embedder, {failing: lambda: voyage_error.AuthenticationError("synthetic 401")}
    )

    with pytest.raises(VoyageEmbedError) as caught:
        run_eval(test_db, embedder=embedder, queries=[_make_query(failing, doc_id)])
    assert caught.value.transient is False


def test_run_eval_degrades_an_ollama_timeout(test_db, fake_embedder):
    """PR #33's observed case, now at the real-backend level: a read timeout is
    classified transient by the Ollama transport and degrades only its row."""
    doc_id = _seed(test_db, fake_embedder, "Ollama Timeout Doc", "ollamatimeout content")
    failing = "ollamatimeout slow query"
    embedder = arctic_failing_on(fake_embedder, {failing: httpx.ReadTimeout})
    queries = [_make_query("ollamatimeout content", doc_id), _make_query(failing, doc_id)]

    report = run_eval(test_db, embedder=embedder, queries=queries)

    assert [r.degraded for r in report.results] == [False, True]


def test_run_eval_aborts_on_an_ollama_model_not_found(test_db, fake_embedder):
    """Rule-12 regression (PR #33 gap 2): HTTP 404 (model not pulled) degraded
    every row and advised "retry", exiting 0 with every metric 0.0. No retry
    fixes a missing model; the run aborts with the backend's error instead."""
    doc_id = _seed(test_db, fake_embedder, "Ollama Missing Doc", "ollamamissing content")
    failing = "ollamamissing model query"
    embedder = arctic_failing_on(fake_embedder, {failing: 404})

    with pytest.raises(OllamaEmbedError) as caught:
        run_eval(test_db, embedder=embedder, queries=[_make_query(failing, doc_id)])
    assert caught.value.transient is False
    assert "404" in str(caught.value)


def test_run_eval_aborts_on_an_embed_context_overflow(test_db, fake_embedder):
    """An over-window query (PR #36) overflows the same way on every run, so a
    degraded row would block --record-baseline forever while advising a retry.
    run_eval re-raises it (spec 2026-10-16-eval-degraded-all-embedders §3.6)."""
    doc_id = _seed(test_db, fake_embedder, "Overflow Abort Doc", "overflowabort content")
    failing = "overflowabort oversize query"
    embedder = arctic_failing_on(fake_embedder, {failing: OLLAMA_OVERFLOW})
    queries = [_make_query("overflowabort content", doc_id), _make_query(failing, doc_id)]

    with pytest.raises(OllamaEmbedContextOverflow) as caught:
        run_eval(test_db, embedder=embedder, queries=queries)
    assert caught.value.transient is False


class _UnclassifiedEmbedError(EmbedError):
    """An embed error from code that never set ``transient``."""


class _UnclassifiedFailingEmbedder:
    """Protocol-conforming double whose query embeds raise an unclassified error."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.dim = inner.dim

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if input_type == "query":
            raise _UnclassifiedEmbedError("synthetic unclassified failure")
        return self._inner.embed(texts, input_type=input_type)

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)


def test_run_eval_aborts_on_an_unclassified_embed_error(test_db, fake_embedder):
    """Degrading is a tolerance and needs positive evidence: an embed error whose
    raiser never said ``transient=True`` aborts the run."""
    doc_id = _seed(test_db, fake_embedder, "Unclassified Doc", "unclassified content")
    embedder = _UnclassifiedFailingEmbedder(fake_embedder)

    with pytest.raises(_UnclassifiedEmbedError):
        run_eval(
            test_db, embedder=embedder, queries=[_make_query("unclassified query", doc_id)]
        )


def test_embed_failure_message_wording_by_kind():
    """Names the class and carries the backend's text; the advice follows the
    kind. Built without any query text — the function never receives one."""
    from brain.eval.runner import embed_failure_message

    retry = embed_failure_message(VoyageEmbedError("synthetic 429", transient=True), command="eval")
    abort = embed_failure_message(OllamaEmbedError("HTTP 404: gone"), command="eval --answer")

    assert retry.startswith("eval: ")
    assert "VoyageEmbedError" in retry and "synthetic 429" in retry
    assert "retry" in retry and "a retry will not fix" not in retry
    assert abort.startswith("eval --answer: ")
    assert "OllamaEmbedError" in abort and "HTTP 404: gone" in abort
    assert "a retry will not fix" in abort


def test_degraded_results_empty_for_a_clean_run(test_db, fake_embedder):
    doc_id = _seed(test_db, fake_embedder, "Clean Run Doc", "cleanrun content")
    report = run_eval(
        test_db, embedder=fake_embedder, queries=[_make_query("cleanrun content", doc_id)]
    )
    assert degraded_results(report) == []


def test_run_eval_aggregates_per_category(test_db, fake_embedder):
    """Two 'people' queries + one 'email' → per_category has correct counts."""
    id1 = _seed(test_db, fake_embedder, "Person A", "people category content alpha")
    id2 = _seed(test_db, fake_embedder, "Person B", "people category content beta")
    id3 = _seed(test_db, fake_embedder, "Email C", "email category content gamma")

    queries = [
        EvalQuery(query="people content alpha", expected_doc_ids=[id1], category="people"),
        EvalQuery(query="people content beta", expected_doc_ids=[id2], category="people"),
        EvalQuery(query="email content gamma", expected_doc_ids=[id3], category="email"),
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert "people" in report.per_category
    assert "email" in report.per_category
    assert report.per_category["people"].count == 2
    assert report.per_category["email"].count == 1


def test_run_eval_config_signature_captures_kwargs(test_db, fake_embedder):
    """EvalReport.config_signature records all four kwarg fields."""
    _seed(test_db, fake_embedder, "Config Sig Doc", "config signature test content")
    queries = [_make_query("config signature test", "00000000")]

    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=queries,
        recency_halflife_days=90.0,
        snippet_context_tokens=100,
        vector_sim_floor=0.3,
        embedder_name="arctic",
    )

    sig = report.config_signature
    assert sig["recency_halflife_days"] == 90.0
    assert sig["snippet_context_tokens"] == 100
    assert sig["vector_sim_floor"] == 0.3
    assert sig["embedder"] == "arctic"


def test_run_eval_mean_aggregates_match_per_query(test_db, fake_embedder):
    """Report-level means equal the arithmetic mean of per-query values."""
    ids = [
        _seed(test_db, fake_embedder, f"Mean Doc {i}", f"mean aggregate test content {i}")
        for i in range(3)
    ]
    queries = [
        _make_query(f"mean aggregate test content {i}", ids[i])
        for i in range(3)
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    n = len(report.results)
    assert report.mean_ndcg_at_5 == pytest.approx(
        sum(r.ndcg_at_5 for r in report.results) / n, abs=1e-9
    )
    assert report.mean_mrr == pytest.approx(
        sum(r.mrr for r in report.results) / n, abs=1e-9
    )
    assert report.mean_recall_at_20 == pytest.approx(
        sum(r.recall_at_20 for r in report.results) / n, abs=1e-9
    )


# ---------------------------------------------------------------------------
# Clock determinism — spec docs/specs/2026-10-15-eval-recency-determinism-design.md.
# ``since_days`` compiles to ``ingested_at >= NOW() - N days``, so any newly
# ingested matching document enters the window at once and moves the ranked
# list (the daily-brief drift that tripped ``--fail-below``). A closed
# ``after``/``before`` range does not move.
# ---------------------------------------------------------------------------


def _set_ingested_at(test_db: psycopg.Connection, doc_id: str, ts: datetime) -> None:
    """Pin one document's ``ingested_at`` (``sent_at`` stays NULL, so the
    ``coalesce(sent_at, ingested_at)`` the bounds read is this value)."""
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s::uuid", (ts, doc_id)
    )


def test_run_eval_since_days_query_drifts_under_new_ingest(test_db, fake_embedder):
    """The bug, reproduced: a ``since_days`` query's ranked list changes when an
    unjudged matching document is ingested, with nothing about search changed."""
    judged = _seed(test_db, fake_embedder, "Digest Judged", "daily digest of captures")
    _set_ingested_at(test_db, judged, datetime.now(tz=UTC) - timedelta(days=10))
    query = EvalQuery(
        query="daily digest", expected_doc_ids=[judged], category="recency", since_days=40
    )

    before_ingest = run_eval(test_db, embedder=fake_embedder, queries=[query])
    newcomer = _seed(test_db, fake_embedder, "Digest Newcomer", "daily digest of captures too")
    after_ingest = run_eval(test_db, embedder=fake_embedder, queries=[query])

    assert newcomer not in before_ingest.results[0].actual_doc_ids
    assert newcomer in after_ingest.results[0].actual_doc_ids


def test_run_eval_pinned_window_is_stable_under_new_ingest(test_db, fake_embedder):
    """The fix: the same scenario with a closed ``after``/``before`` range — the
    newcomer is dated after ``before``, so the ranked list is unchanged."""
    judged = _seed(test_db, fake_embedder, "Digest Judged", "daily digest of captures")
    _set_ingested_at(test_db, judged, datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    query = EvalQuery(
        query="daily digest",
        expected_doc_ids=[judged],
        category="recency",
        after=date(2026, 1, 1),
        before=date(2026, 2, 1),
    )

    before_ingest = run_eval(test_db, embedder=fake_embedder, queries=[query])
    _seed(test_db, fake_embedder, "Digest Newcomer", "daily digest of captures too")
    after_ingest = run_eval(test_db, embedder=fake_embedder, queries=[query])

    assert before_ingest.results[0].actual_doc_ids == [judged]
    assert after_ingest.results[0].actual_doc_ids == [judged]


def test_run_eval_pinned_bounds_are_half_open(test_db, fake_embedder):
    """``after`` is inclusive and ``before`` exclusive, each at UTC midnight —
    the same contract as ``hybrid_search``'s ``after``/``before``."""
    on_after = _seed(test_db, fake_embedder, "Bound On After", "bounded digest entry one")
    last_inside = _seed(test_db, fake_embedder, "Bound Last Inside", "bounded digest entry two")
    on_before = _seed(test_db, fake_embedder, "Bound On Before", "bounded digest entry three")
    _set_ingested_at(test_db, on_after, datetime(2026, 1, 1, tzinfo=UTC))
    _set_ingested_at(test_db, last_inside, datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC))
    _set_ingested_at(test_db, on_before, datetime(2026, 2, 1, tzinfo=UTC))
    query = EvalQuery(
        query="bounded digest entry",
        expected_doc_ids=[on_after],
        category="recency",
        after=date(2026, 1, 1),
        before=date(2026, 2, 1),
    )

    report = run_eval(test_db, embedder=fake_embedder, queries=[query])

    assert set(report.results[0].actual_doc_ids) == {on_after, last_inside}


def test_utc_midnight_is_utc_aware() -> None:
    """The bound is built UTC-aware here, so it never depends on the session
    ``TimeZone`` or on a later ``_ensure_utc`` call."""
    assert _utc_midnight(date(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
    stamped = _utc_midnight(date(2026, 1, 1))
    assert stamped is not None and stamped.tzinfo is UTC
    assert _utc_midnight(None) is None
