"""Tests for ``scripts/token_payload_report.py`` (Wave 0 measurement harness).

The script lives outside ``src/brain``, so it isn't auto-importable; it is
loaded via :mod:`importlib.util`, the same way
``tests/test_embedding_smoke.py`` loads its script. ``scripts/`` sits outside
BOTH the coverage (``--cov=brain``) and type (``mypy src/``) gates
``bin/brain-ci`` runs, but every later wave's before/after proof is computed by
this code, so it is tested like production code.

Four things matter here and each has a test:

1. Tokens are counted over the SERIALIZED payload an agent receives, via
   ``embedder.count_tokens``, not over the dataclasses behind it — a chars/4
   estimate or a ``len(results)`` proxy would make every later wave's savings
   claim fiction. Both tests for this use :class:`_CountTokensSpy` rather than
   the shared ``fake_embedder``, because ``FakeEmbedder.count_tokens`` **is**
   ``len(text) // 4``: an assertion pinned to it would be satisfied by the very
   estimate the rule forbids, which is a test that cannot fail.
2. The script never writes. It runs against the live corpus by design, and a
   measurement run that silently logged telemetry would pollute
   ``brain gaps``.
3. The production opt-in guard actually refuses.
4. ``main()`` produces the ``--json`` envelope Wave 5 reads its ``totals`` out
   of, end to end.

All fixture data is synthetic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.format_search import search_results_json
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import set_document_sensitivity
from brain.recall import recall
from brain.search import hybrid_search
from tests.conftest import TEST_DATABASE_URL

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "token_payload_report.py"

# The script imports its sibling ``scripts/query_files``. Running the script
# directly puts ``scripts/`` on ``sys.path`` for free; loading it via
# ``importlib`` does not, so put it there — the pattern
# ``tests/test_collapse_gmail_threads.py`` established for the same reason.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

#: Tables a stray write would land in. Module constants, never user input —
#: table names cannot be bound as SQL parameters.
_WATCHED_TABLES = ("documents", "chunks", "sources", "search_queries")

#: The spy's tokens-per-text function. Chosen to be provably NOT ``len // 4``
#: (the shape the headline rule forbids) and not a constant either, so an
#: assertion against it pins delegation rather than coincidence. Non-integer
#: multiplier + odd offset means no payload length can make ``len // 4`` and
#: this agree.
def _spy_token_count(text: str) -> int:
    """A tokenizer no chars/4 estimate can imitate."""
    return len(text) * 3 + 7


class _CountTokensSpy:
    """Record every string handed to ``count_tokens`` and price it distinctively.

    A local test double, not a monkey-patch of production code (CLAUDE.md rule
    13): it wraps a real embedder and delegates ``embed`` / ``dim`` untouched,
    so it satisfies the :class:`brain.ingest.Embedder` Protocol and can be
    passed anywhere the real one goes.

    It exists because ``FakeEmbedder.count_tokens`` **is** ``len(text) // 4``
    (``tests/conftest.py``), which makes any assertion of the form
    ``tokens == fake_embedder.count_tokens(payload)`` equally true of a
    hard-coded chars/4 estimate — the exact estimate the script's headline rule
    forbids. This double breaks that tie.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dim = inner.dim
        self.counted: list[str] = []

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        self.counted.append(text)
        return _spy_token_count(text)


@pytest.fixture
def report() -> ModuleType:
    """Load ``scripts/token_payload_report.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("token_payload_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(report: ModuleType, **overrides: Any) -> Any:
    """A Config pinned to the test DB with the ranking knobs made explicit."""
    base: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "vector_sim_floor": 0.0,
        "recency_halflife_days": None,
        "snippet_context_tokens": 0,
        "recall_passage_tokens": 120,
        "recall_max_candidates": 25,
    }
    base.update(overrides)
    return report.Config(**base)


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic documents all matching the word 'quarterly'.

    Bodies differ per document: ``documents.content_hash`` is UNIQUE, so
    identical bodies would dedup into one row.
    """
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Quarterly note {i}",
                content=f"The quarterly review covered budget and hiring {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"token-report-{i}",
            tags=["planning"],
        )


#: A synthetic summary short enough that substituting it for a 400-char chunk
#: snippet is a NET saving even after brief mode adds ``"snippet_source":
#: "chunk"`` (~25 bytes) to every OTHER result in the payload.
_SHORT_SUMMARY = "Quarterly planning notes."

#: Body long enough that ``hybrid_search`` returns a snippet at its
#: ``SNIPPET_LENGTH`` cap (400 chars), so the saving above is real and not an
#: artifact of a short fixture. Repeated sentences keep it deterministic.
_LONG_QUARTERLY_BODY = (
    "The quarterly review covered budget, hiring, roadmap and staffing. " * 12
)


def _seed_long_doc_with_short_summary(
    conn: psycopg.Connection[Any], embedder: Any
) -> str:
    """Ingest one long 'quarterly' doc and give it a much shorter summary.

    ``_seed`` above calls ``ingest_document`` with **no enricher**, and
    ``enricher=None`` makes enrichment a documented no-op — so every document
    it seeds has ``summary IS NULL``. Under the brief projection a NULL summary
    falls back to the chunk snippet AND still emits ``snippet_source:
    "chunk"``, which makes the brief arm strictly LARGER on a ``_seed``-only
    fixture. Any test asserting brief < full must therefore put a real summary
    in the corpus first.

    The summary is written with a parameterized ``UPDATE`` rather than by
    calling the enricher: this is a size measurement, not an Ollama test, and
    a live LLM would make the fixture non-deterministic.

    Returns the document id so callers can assert the fixture is not vacuous.
    """
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Quarterly note with summary",
            content=_LONG_QUARTERLY_BODY,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="token-report-summarized",
        tags=["planning"],
    )
    doc_id = result.document_id
    assert doc_id is not None
    conn.execute(
        "UPDATE documents SET summary = %s WHERE id = %s",
        (_SHORT_SUMMARY, doc_id),
    )
    return str(doc_id)


def _counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Row counts for every table a write could plausibly touch."""
    counts: dict[str, int] = {}
    for table in _WATCHED_TABLES:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        counts[table] = row[0]
    return counts


# ---------------------------------------------------------------------------
# 1. The measurement measures the serialized payload
# ---------------------------------------------------------------------------


def test_measure_delegates_token_counting_to_the_embedder(
    report: ModuleType, fake_embedder: Any
) -> None:
    """``_measure`` counts the SERIALIZED payload, via ``embedder.count_tokens``.

    MUTATION TEST for the script's headline rule ("tokens come from
    ``embedder.count_tokens``, never a ``chars / 4`` estimate"). Rewriting
    ``_measure``'s ``tokens=`` to ``len(serialized) // 4`` must turn this red.

    It is the pure-logic half of the pair, so nothing but ``_measure`` touches
    the spy: ``counted`` is asserted to be EXACTLY the one serialization, which
    pins both the delegation and the payload's identity — a payload built from
    the dataclasses, or a re-serialization with different flags, changes those
    bytes. :func:`test_measure_search_counts_serialized_payload_not_dataclasses`
    is the same claim through the real ``measure_search`` path.
    """
    # Arrange — a payload with a non-ASCII char, so ``ensure_ascii=False`` is
    # load-bearing for the byte count rather than incidental.
    spy = _CountTokensSpy(fake_embedder)
    payload = [{"title": "Quarterly — note", "snippet": "budget and hiring"}]
    serialized = json.dumps(payload, ensure_ascii=False)

    # Act
    measurement = report._measure(
        query="quarterly",
        surface="search",
        payload=payload,
        results=3,
        embedder=spy,
    )

    # Assert
    assert spy.counted == [serialized], (
        "count_tokens must be called exactly once, with the exact bytes the "
        "agent receives"
    )
    assert measurement.tokens == _spy_token_count(serialized)
    assert measurement.chars == len(serialized)
    # The two proxies the headline rule forbids, ruled out by construction
    # rather than by a coincidence of FakeEmbedder's own chars/4 tokenizer.
    assert measurement.tokens != measurement.chars // 4
    assert measurement.tokens != measurement.results


#: A serialization no ``json.dumps`` of any payload could return: it is not
#: valid JSON, its length differs from the fixture payload's real
#: serialization, and it prices differently under :func:`_spy_token_count`.
#: Substituted for the shared helper's return value, it makes ``_measure``'s own
#: numbers name which serializer produced them.
_SENTINEL_SERIALIZATION = "⟪sentinel — the shared serializer's return value⟫"


def test_measure_routes_serialization_through_the_shared_helper(
    report: ModuleType, fake_embedder: Any
) -> None:
    """``_measure`` must SERIALIZE VIA ``brain.token_report.serialize_payload``.

    MUTATION TEST for finding SF1. ``brain.token_report``'s docstring claims
    this harness's numbers stay comparable with the persisted ``payload_tokens``
    column because the harness *imports* the serialization rather than
    re-implementing it. Before the fix the harness duplicated
    ``json.dumps(payload, ensure_ascii=False)`` in two places and nothing
    asserted the two agreed — the claim held only because someone had run it by
    hand once.

    The obvious test — asserting ``measurement.tokens ==
    count_payload_tokens(payload, cost=...)`` — would be VACUOUS: post-fix the
    two are literally the same code path, so it is true by construction and no
    mutation can turn it red. What is actually at stake is the ROUTING, so that
    is what is asserted: the shared helper is replaced with one returning a
    sentinel string, and ``chars`` / ``tokens`` are then required to describe
    that sentinel. Re-inlining the ``dumps`` call in ``_measure`` bypasses the
    patch, so ``call_count`` drops to 0 and this goes red on its first
    assertion.

    ``call_args`` is pinned too, because "calls the helper" is not enough: a
    caller that serialized the payload itself and passed something *else* to
    the helper would still register a call. The payload must arrive unchanged.
    """
    # Arrange — the same non-ASCII payload the sibling test uses, so what the
    # real serializer would have produced is known and provably different.
    spy = _CountTokensSpy(fake_embedder)
    payload = [{"title": "Quarterly — note", "snippet": "budget and hiring"}]
    inlined = json.dumps(payload, ensure_ascii=False)
    assert len(_SENTINEL_SERIALIZATION) != len(inlined), (
        "the sentinel must be distinguishable from the real serialization by "
        "LENGTH, or the chars assertion below could pass by coincidence"
    )
    assert _spy_token_count(_SENTINEL_SERIALIZATION) != _spy_token_count(inlined)

    # Act
    with mock.patch.object(
        report, "serialize_payload", return_value=_SENTINEL_SERIALIZATION
    ) as serializer:
        measurement = report._measure(
            query="quarterly",
            surface="search",
            payload=payload,
            results=3,
            embedder=spy,
        )

    # Assert — THE invariant: the shared helper produced these bytes.
    assert serializer.call_count == 1, (
        "_measure must serialize via brain.token_report.serialize_payload, not "
        "an inlined json.dumps — that duplication IS finding SF1"
    )
    assert serializer.call_args.args == (payload,), (
        "the payload must reach the shared helper unchanged; serializing it "
        "elsewhere and handing the helper something else would still call it"
    )
    # Both measured columns describe the sentinel, so neither can have been
    # computed from a locally-produced string.
    assert measurement.chars == len(_SENTINEL_SERIALIZATION)
    assert spy.counted == [_SENTINEL_SERIALIZATION]
    assert measurement.tokens == _spy_token_count(_SENTINEL_SERIALIZATION)


def test_measure_search_counts_serialized_payload_not_dataclasses(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """``tokens`` is ``count_tokens`` over the exact JSON an agent receives.

    MUTATION TEST, through the real ``measure_search`` path. Uses
    :class:`_CountTokensSpy` rather than ``fake_embedder`` directly for a
    specific reason: ``FakeEmbedder.count_tokens`` *is* ``len(text) // 4``, so
    an assertion pinned to it is equally satisfied by a hard-coded chars/4
    estimate — the estimate this test exists to forbid. Against the spy,
    rewriting ``_measure``'s ``tokens=`` to ``len(serialized) // 4`` goes red.

    ``counted`` is asserted with ``in`` rather than ``==`` because
    ``hybrid_search`` may price chunks through the same embedder; the exact
    call set is pinned by the unit test above.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    cfg = _cfg(report)
    spy = _CountTokensSpy(fake_embedder)
    expected_results = hybrid_search(
        test_db,
        embedder=spy,
        query="quarterly",
        limit=5,
        vector_sim_floor=cfg.vector_sim_floor,
        recency_halflife_days=cfg.recency_halflife_days,
        snippet_context_tokens=cfg.snippet_context_tokens,
        sensitivity=report.AGENT_SENSITIVITY_LENS,
    )
    expected_payload = json.dumps(
        search_results_json(expected_results), ensure_ascii=False
    )

    # Act
    measurements = report.measure_search(
        test_db, cfg, embedder=spy, query="quarterly", limit=5
    )

    # Assert
    assert expected_results, "the seeded corpus must produce at least one hit"
    measurement = measurements[0]
    assert expected_payload in spy.counted, (
        "the serialized agent-facing payload must be what gets counted"
    )
    assert measurement.tokens == _spy_token_count(expected_payload)
    assert measurement.chars == len(expected_payload)
    assert measurement.results == len(expected_results)
    assert measurement.surface == "search"
    # The two proxies this test exists to rule out: a chars/4 estimate, and a
    # result COUNT masquerading as a payload cost.
    assert measurement.tokens != measurement.chars // 4
    assert measurement.tokens != len(expected_results)


def test_search_arms_come_from_one_hybrid_search_call(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Both arms are serialized from ONE retrieval, even with decay on.

    **This test is about the number of retrievals, not about payload size.**
    It was named ``…_even_with_recency_decay_on`` and asserted the two arms
    were byte-identical, which held only while the brief arm was a labelled
    no-op. Wave 1 flipped the seam in ``_search_payload``, so the arms now
    differ **by design** — do not "restore" the equality.

    Why one retrieval matters: the script runs with
    ``BRAIN_RECENCY_HALFLIFE_DAYS`` set (180 by default), so each ``score``
    carries a ``now()``-derived decay term and two separate ``hybrid_search``
    executions can legitimately serialize to different bytes. If the arms came
    from two retrievals, a before/after saving could be pure decay artifact.
    The call-count spy below is what actually pins that: the old
    ``brief.results == full.results`` did NOT — ``measure_search`` sets both
    from ``results=len(results)`` on the SAME list, so it was true by
    construction whether there were one retrieval or ten.

    The inequality at the end depends on the Arrange step seeding a document
    whose summary is materially shorter than its 400-char chunk snippet.
    Without ``_seed_long_doc_with_short_summary`` the brief arm would be
    strictly LARGER (every ``_seed`` document has ``summary IS NULL``, falls
    back to the chunk, and still pays for the ``snippet_source`` key) — which
    is exactly why the naive ``brief.tokens < full.tokens`` amendment fails on
    a ``_seed``-only fixture.
    """
    # Arrange — a summarized document plus the NULL-summary tail from ``_seed``,
    # so one measurement exercises BOTH the substitution and the fallback.
    _seed(test_db, fake_embedder)
    summarized_id = _seed_long_doc_with_short_summary(test_db, fake_embedder)
    cfg = _cfg(report, recency_halflife_days=180.0)

    # Act
    with mock.patch.object(
        report, "hybrid_search", wraps=report.hybrid_search
    ) as spy:
        measurements = report.measure_search(
            test_db, cfg, embedder=fake_embedder, query="quarterly", limit=5
        )

    # Assert — THE invariant: one retrieval, two serializations.
    assert spy.call_count == 1, (
        "both arms must be serialized from a single hybrid_search call, or a "
        "before/after delta could be recency decay rather than the projection"
    )

    full, brief = measurements
    assert full.results > 0, "an empty corpus would make the comparison vacuous"
    assert full.tokens > 0
    assert full.surface == "search"
    assert brief.surface == "search_brief"
    assert brief.results == full.results

    # Fixture guards — without these the inequality could pass for the wrong
    # reason (or fail because the summarized document never ranked).
    row = test_db.execute(
        "SELECT summary FROM documents WHERE id = %s", (summarized_id,)
    ).fetchone()
    assert row is not None and row[0] == _SHORT_SUMMARY
    null_summaries = test_db.execute(
        "SELECT count(*) FROM documents WHERE summary IS NULL"
    ).fetchone()
    assert null_summaries is not None and null_summaries[0] > 0, (
        "the corpus must retain a NULL-summary tail; limit=5 exceeds the 4 "
        "seeded documents, so every one of them is in the measured match set"
    )

    # The projection flip is a real saving, in BOTH units.
    assert brief.tokens < full.tokens
    assert brief.chars < full.chars


def test_measure_recall_counts_the_context_block_too(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Recall is measured as MCP returns it: ``to_dict()`` + ``context_block``
    + ``payload_tokens``.

    MUTATION TEST. Deleting ``payload["context_block"] = ...`` from
    ``measure_recall`` must turn this red — the ``chars`` equality below is
    pinned to the FULL serialization, which is strictly longer than the
    dict-only one. Deleting the ``payload_tokens`` line does the same.

    ``payload_tokens`` is reproduced here in the same ORDER as the tool builds
    it — counted over the payload *before* insertion, inserted last. That is
    the only non-circular definition (a key cannot count itself), and building
    the expectation the same way is what makes the ``chars`` equality a real
    comparison rather than a restatement of whatever the code happened to do.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    cfg = _cfg(report)
    result = recall(
        test_db,
        cfg,
        embedder=fake_embedder,
        query="quarterly",
        budget_tokens=2000,
        max_candidates=cfg.recall_max_candidates,
        sensitivity=report.AGENT_SENSITIVITY_LENS,
    )
    dict_only = json.dumps(result.to_dict(), ensure_ascii=False)
    expected_payload = {**result.to_dict(), "context_block": result.context_block()}
    expected_payload["payload_tokens"] = fake_embedder.count_tokens(
        json.dumps(expected_payload, ensure_ascii=False)
    )
    with_block = json.dumps(expected_payload, ensure_ascii=False)

    # Act
    measurement = report.measure_recall(
        test_db, cfg, embedder=fake_embedder, query="quarterly", budget_tokens=2000
    )

    # Assert
    assert result.passages, "an empty recall would make the comparison vacuous"
    assert result.context_block(), "an empty block would make the comparison vacuous"
    assert measurement.surface == "recall"
    assert measurement.results == len(result.passages)
    assert measurement.tokens > 0
    assert measurement.chars == len(with_block)
    assert measurement.chars > len(dict_only)


def _harness_recall_payload(
    report: ModuleType,
    conn: psycopg.Connection[Any],
    cfg: Any,
    *,
    embedder: Any,
    query: str,
) -> dict[str, Any]:
    """The payload ``measure_recall`` actually built, captured at its seam.

    Captured rather than reconstructed: rebuilding the expected payload here
    would restate the code under test, so instead ``_measure`` — the single
    place the script serializes anything — is intercepted and its ``payload``
    kwarg kept. What comes back is what the harness really measured.
    """
    captured: dict[str, Any] = {}
    real_measure = report._measure

    def spy(**kwargs: Any) -> Any:
        captured["payload"] = kwargs["payload"]
        return real_measure(**kwargs)

    with mock.patch.object(report, "_measure", spy):
        report.measure_recall(
            conn, cfg, embedder=embedder, query=query, budget_tokens=2000
        )
    assert captured, "_measure was never called — the seam moved"
    return captured["payload"]


def test_measure_recall_payload_shape_matches_the_live_mcp_tool(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The harness and ``brain_recall`` must ship the SAME keys. Not "should".

    This is the guard the Wave-0 harness lacked, and its absence cost real
    accuracy: Wave 3 added ``payload_tokens`` to ``brain_recall`` and nothing
    noticed that ``measure_recall`` had not followed, so the script silently
    under-reported the recall surface. A measurement harness that drifts from
    the thing it measures does not fail loudly — it quietly poisons every
    before/after comparison built on it, including Wave 5's telemetry baseline.

    Comparing KEY SETS rather than a hand-copied list is the point: a restated
    list is exactly the drift this test exists to catch, one indirection later.
    Add a key to either side and this goes red naming the difference.
    """
    # Arrange — same DB, same fake embedder, both surfaces.
    _seed(test_db, fake_embedder)
    cfg = _cfg(report)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    # Act
    tool_payload = mcp_server.brain_recall(query="quarterly", budget_tokens=2000)
    harness_payload = _harness_recall_payload(
        report, test_db, cfg, embedder=fake_embedder, query="quarterly"
    )

    # Assert
    assert "payload_tokens" in tool_payload, (
        "the tool lost payload_tokens — this test's premise is gone"
    )
    missing = set(tool_payload) - set(harness_payload)
    extra = set(harness_payload) - set(tool_payload)
    assert not missing, f"measure_recall does not mirror brain_recall keys: {missing}"
    assert not extra, f"measure_recall ships keys brain_recall does not: {extra}"


def test_measure_functions_apply_the_agent_trust_lens(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Both surfaces default to MCP's ``"normal"`` lens, not "both tiers".

    MUTATION TEST. Dropping ``sensitivity=`` from either measure function
    makes the default match BOTH tiers, and the strict inequalities below go
    red. This is not a payload-size nicety: MCP applies
    ``_confidential_lens(include_confidential)`` on both
    ``mcp_server.brain_search`` and ``mcp_server.brain_recall`` (cited by
    symbol: the line numbers this docstring used to carry were invalidated by
    the very wave that added this test), so measuring without it would size a
    MATCH SET no agent ever sees.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    cfg = _cfg(report)
    row = test_db.execute("SELECT id FROM documents ORDER BY title").fetchone()
    assert row is not None
    assert set_document_sensitivity(
        test_db, document_id=str(row[0]), level="confidential"
    )

    # Act
    lensed = report.measure_search(
        test_db, cfg, embedder=fake_embedder, query="quarterly", limit=5
    )[0]
    both_tiers = report.measure_search(
        test_db,
        cfg,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        sensitivity=None,
    )[0]
    lensed_recall = report.measure_recall(
        test_db, cfg, embedder=fake_embedder, query="quarterly", budget_tokens=2000
    )
    both_tiers_recall = report.measure_recall(
        test_db,
        cfg,
        embedder=fake_embedder,
        query="quarterly",
        budget_tokens=2000,
        sensitivity=None,
    )

    # Assert
    assert report.AGENT_SENSITIVITY_LENS == "normal"
    assert lensed.results > 0, "the lens must narrow the match set, not empty it"
    assert lensed.results < both_tiers.results
    assert lensed_recall.results > 0
    assert lensed_recall.results < both_tiers_recall.results


# ---------------------------------------------------------------------------
# 2. The script never writes
# ---------------------------------------------------------------------------


def test_measure_search_is_read_only(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Regression: a measurement run must not log telemetry or ingest.

    ``recall()`` leaves ``record_search_query`` to its surfaces by contract; if
    this script ever grew one, every measurement run would inject synthetic
    queries into ``search_queries`` and poison ``brain gaps``.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    cfg = _cfg(report)
    before = _counts(test_db)

    # Act
    report.measure_search(
        test_db, cfg, embedder=fake_embedder, query="quarterly", limit=5
    )
    report.measure_recall(
        test_db, cfg, embedder=fake_embedder, query="quarterly", budget_tokens=2000
    )

    # Assert
    assert _counts(test_db) == before


def test_enforce_read_only_makes_writes_fail_at_the_server(
    report: ModuleType,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — guarantees a migrated schema
) -> None:
    """The belt, proven: after the call the server rejects a write.

    Uses its own connection, never the shared ``test_db`` one — leaving that
    read-only would break the next test's TRUNCATE reset.
    """
    # Arrange
    conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    try:
        # Act
        report.enforce_read_only(conn)

        # Assert
        with pytest.raises(psycopg.Error):
            conn.execute("CREATE TEMP TABLE token_report_probe (x int)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. The production guard refuses (MUTATION-TESTED)
# ---------------------------------------------------------------------------


def test_prod_guard_refuses_without_optin(
    report: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prod-port DSN without the opt-in exits non-zero and names the env var.

    MUTATION TEST. Deleting the ``if refusal is not None:`` block in
    ``main()`` must turn this red: the run then falls through to
    ``make_embedder`` / ``connect`` and whatever it prints, it is not the
    refusal.

    The PORT is what the guard keys on here, and the database name is
    deliberately one that does not exist: if the guard is ever mutated away,
    the fall-through fails at the connection handshake, so not even a read
    reaches the live corpus. The name also stays outside the ``second_brain*``
    family so ``test_database_url_isolation`` does not (correctly) flag it as a
    pinned brain DSN — that guard caught this exact literal on its first run.
    """
    # Arrange
    monkeypatch.delenv(report.ALLOW_PROD_ENV, raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://brain:brain@localhost:55432/token_report_guard_probe",
    )

    # Act
    exit_code = report.main(["--query", "quarterly"])

    # Assert
    captured = capsys.readouterr()
    assert exit_code != 0
    assert report.ALLOW_PROD_ENV in captured.err
    assert captured.out == "", "stdout stays clean so --json output is pipeable"


def test_prod_guard_lets_the_opt_in_through(
    report: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch works — otherwise the guard would be unusable."""
    # Arrange
    monkeypatch.setenv(report.ALLOW_PROD_ENV, "1")

    # Act / Assert
    assert (
        report.prod_refusal("postgresql://brain:brain@localhost:55432/second_brain")
        is None
    )


def test_prod_guard_ignores_the_test_database(
    report: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-prod DSN is never refused, opt-in or not."""
    # Arrange
    monkeypatch.delenv(report.ALLOW_PROD_ENV, raising=False)

    # Act / Assert
    assert report.prod_refusal(TEST_DATABASE_URL) is None


# ---------------------------------------------------------------------------
# 4. main() end to end — the envelope Wave 5 reads
# ---------------------------------------------------------------------------


def test_main_json_envelope_reports_every_surface(
    report: ModuleType,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The happy path: ``--json`` emits parseable totals for all three surfaces.

    Wave 5's before/after proof reads ``totals`` out of this envelope, so its
    shape is a contract, not an implementation detail. Runs under
    ``BRAIN_EMBEDDER=none`` so no Ollama is needed: the ``NullEmbedder``
    degrades ``hybrid_search`` to its FTS leg and still counts tokens with
    tiktoken, which is the half of the ``Embedder`` Protocol this script uses.
    ``conftest`` already pins ``DATABASE_URL`` to the test DB, which
    ``test_prod_guard_ignores_the_test_database`` proves the guard allows.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv(report.ALLOW_PROD_ENV, raising=False)

    # Act
    exit_code = report.main(["--query", "quarterly", "--json"])

    # Assert
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    envelope = json.loads(captured.out)
    assert envelope["queries"] == 1
    assert envelope["limit"] == 5
    assert envelope["config"]["embedder"] == "none"
    assert envelope["config"]["sensitivity"] == report.AGENT_SENSITIVITY_LENS
    measurements = envelope["measurements"]
    # Non-emptiness FIRST: every assertion below holds on an empty match set,
    # because an empty JSON list still has characters and still costs tokens.
    # Without this the whole test passes against a corpus that matched nothing.
    by_surface = {m["surface"]: m for m in measurements}
    assert by_surface["search"]["results"] > 0, (
        "the seeded corpus must match under the NullEmbedder's FTS leg — "
        "an empty match set makes every assertion below vacuous"
    )
    assert any(m["results"] > 0 for m in measurements)
    totals = envelope["totals"]
    assert set(totals) == {"search", "search_brief", "recall"}
    for surface, bucket in totals.items():
        assert bucket["tokens"] > 0, surface
        assert bucket["chars"] > 0, surface
        assert bucket["queries"] == 1, surface
        assert bucket["mean_tokens"] == pytest.approx(bucket["tokens"])
    assert [m["surface"] for m in measurements] == [
        "search",
        "search_brief",
        "recall",
    ]


def test_main_reports_a_missing_queries_file(
    report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Early exit: a bad ``--queries-file`` never reaches the database."""
    # Act
    exit_code = report.main(["--queries-file", str(tmp_path / "nope.txt")])

    # Assert
    assert exit_code == report.EXIT_ERROR
    assert "queries file not found" in capsys.readouterr().err


def test_main_reports_an_empty_queries_file(
    report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Early exit: a comments-only file measures nothing and says so."""
    # Arrange
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n\n", encoding="utf-8")

    # Act
    exit_code = report.main(["--queries-file", str(empty)])

    # Assert
    assert exit_code == report.EXIT_ERROR
    assert "no queries to measure" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The committed queries file
# ---------------------------------------------------------------------------


def test_default_queries_file_parses_and_is_non_empty(report: ModuleType) -> None:
    """The shipped query list loads — comments and blanks stripped."""
    # Act
    queries = report.load_query_lines(report.DEFAULT_QUERIES_FILE)

    # Assert
    assert queries
    assert all(q and not q.startswith("#") for q in queries)
