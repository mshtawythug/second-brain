"""The UI's telemetry writes must SURVIVE the connection closing.

The defect this pins was silent, fail-closed, and actively contradicted by the
app's own health output: ``brain ui`` recorded **zero rows with source='ui'
after ~215 real searches** while ``/api/status`` reported
``logging_enabled: true``.

Cause: ``build_context``'s ``conn_factory`` returned ``connect(...)`` unmodified,
so psycopg3's default ``autocommit=False`` left an implicit transaction open.
``gaps.record_search_query`` wraps its INSERT in ``conn.transaction()``, which
under an open transaction degrades to a **SAVEPOINT** rather than a real
transaction — and ``conn.close()`` then discarded the lot.

Both ends of the contract already said otherwise in prose:

* ``brain.ui.context.UiContext`` — "must return a context manager yielding an
  autocommit-capable psycopg connection"
* ``brain.gaps.record_search_query`` — "Callers run with ``autocommit=True``"

``brain.cli_recall`` sets the flag; the UI did not. This module is the check
that the *code* now matches the *documentation*, because for the whole of phase
0 only the documentation was true.

The first test asserts the OBSERVABLE consequence — a row written through the
real factory is still there after the connection closes — rather than merely
asserting the flag. A test on the flag alone would pass against any future
refactor that set ``autocommit`` and then broke persistence some other way.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.ui import telemetry
from brain.ui.server import build_context

from .conftest import TEST_DATABASE_URL


@pytest.fixture
def ui_cfg() -> Config:
    return Config(database_url=TEST_DATABASE_URL, embedder="none")


def _context(ui_cfg: Config) -> Any:
    """A real ``UiContext``, built the way ``brain ui`` builds it."""
    return build_context(
        ui_cfg,
        host="127.0.0.1",
        port=8765,
        read_only=False,
        token="t",
        include_confidential=False,
        embedder=None,
    )


def test_a_ui_search_row_survives_the_connection_closing(
    test_db: psycopg.Connection, ui_cfg: Config
) -> None:
    """THE regression test. Without ``autocommit`` this row is rolled back.

    Deliberately end-to-end through the real ``conn_factory``: the write happens
    on one connection, that connection closes, and the row is then read back on
    a *different* connection. That is exactly the sequence that silently
    discarded ~215 real searches.
    """
    context = _context(ui_cfg)
    if not context.logging_enabled:
        pytest.skip("this database's CHECK does not admit source='ui' (migration 024)")

    marker = f"telemetry-persistence-{uuid.uuid4().hex[:12]}"
    session_id = uuid.uuid4()

    with context.conn_factory() as conn:
        # THE STEP THAT MAKES THIS REPRODUCE PRODUCTION — and without which this
        # test passes even with the fix reverted, which is exactly what it did
        # the first time it was mutation-checked.
        #
        # psycopg3's `conn.transaction()` opens a REAL transaction (and commits
        # it on exit) when no transaction is already in progress. On a pristine
        # connection the telemetry INSERT therefore commits itself and survives
        # even under autocommit=False. The route is not pristine: it runs the
        # search on this connection FIRST, which opens the implicit transaction
        # and demotes `conn.transaction()` to a SAVEPOINT that `close()` throws
        # away. One prior read reproduces that ordering.
        conn.execute("SELECT 1").fetchone()
        telemetry.record_ui_search(
            conn,
            enabled=True,
            query=marker,
            result_count=3,
            session_id=session_id,
            fts_count=1,
            duration_ms=42,
        )
    # The factory's context manager has now closed that connection.

    rows = test_db.execute(
        "SELECT source, result_count FROM search_queries WHERE query = %s",
        (marker,),
    ).fetchall()
    assert rows, (
        "the UI's telemetry row did not survive the connection closing. "
        "conn_factory is not autocommit, so record_search_query's "
        "conn.transaction() opened a SAVEPOINT inside an implicit transaction "
        "and conn.close() rolled it back — the exact defect that produced zero "
        "source='ui' rows after ~215 real searches."
    )
    assert rows[0][0] == "ui"
    assert rows[0][1] == 3


def test_conn_factory_yields_an_autocommit_connection(ui_cfg: Config) -> None:
    """The mechanism, asserted directly, so a failure says WHY.

    The test above proves the property; this one names the cause, so a
    regression reports "autocommit is off" instead of only "the row vanished".
    """
    context = _context(ui_cfg)
    with context.conn_factory() as conn:
        assert conn.autocommit is True, (
            "UiContext documents conn_factory as yielding an "
            "autocommit-capable connection and gaps.record_search_query "
            "documents its callers as running with autocommit=True; both were "
            "prose only"
        )


def test_conn_factory_is_a_context_manager_that_closes(ui_cfg: Config) -> None:
    """It is called per request, so it must not leak a connection each time."""
    context = _context(ui_cfg)
    with context.conn_factory() as conn:
        assert conn.closed is False
    assert conn.closed is True, "conn_factory leaked an open connection"


# ------------------------------------------------------- embedder warm-up --


class _RecordingEmbedder:
    """A real-shaped backend that records its calls. No socket, no Ollama."""

    produces_embeddings = True
    dim = 1024

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.calls.append((texts, input_type))
        return [[0.0] * self.dim for _ in texts]


class _DeadOllamaEmbedder(_RecordingEmbedder):
    """A configured backend whose Ollama is down — the common failure."""

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        # OllamaEmbedError lives in brain.embeddings, NOT brain.errors — it
        # subclasses errors.EmbedError but is defined beside its transport.
        from brain.embeddings import OllamaEmbedError

        self.calls.append((texts, input_type))
        raise OllamaEmbedError("Ollama request failed: connection refused")


def test_warm_up_embeds_once_as_a_query() -> None:
    """The cliff is 5,358 ms cold vs ~250 ms warm; one throwaway embed pays it."""
    from brain.ui.server import warm_embedder

    embedder = _RecordingEmbedder()
    assert warm_embedder(embedder) is True
    assert len(embedder.calls) == 1, "warm-up must embed exactly once"
    texts, input_type = embedder.calls[0]
    assert len(texts) == 1
    # `query` matches what a search actually sends, so the warmed path is the
    # path the user's first request will take.
    assert input_type == "query"


def test_warm_up_is_skipped_entirely_for_an_fts_only_install() -> None:
    """``BRAIN_EMBEDDER=none`` must boot unchanged — and make NO call.

    ``NullEmbedder.embed`` raises ``EmbedDisabledError``; reaching it at all
    would mean the FTS-only path depends on an exception handler rather than on
    the ``produces_embeddings`` check every other caller uses.
    """
    from brain.embeddings import NullEmbedder
    from brain.ui.server import warm_embedder

    null = NullEmbedder()
    assert warm_embedder(null) is False


def test_warm_up_never_makes_a_dead_ollama_fatal() -> None:
    """A backend that cannot reach Ollama must not stop the server booting."""
    from brain.ui.server import warm_embedder

    embedder = _DeadOllamaEmbedder()
    assert warm_embedder(embedder) is False, "a failed warm-up must not claim success"
    assert len(embedder.calls) == 1, "it should have tried exactly once"


def test_warm_up_tolerates_no_embedder_at_all() -> None:
    from brain.ui.server import warm_embedder

    assert warm_embedder(None) is False


def test_warm_up_does_not_swallow_a_real_bug() -> None:
    """Only embed-shaped failures are absorbed; a TypeError is a defect.

    A blanket ``except Exception`` here would turn a genuine crash into a silent
    slow first search — the same shape as the telemetry bug above, where a
    failure that should have been loud was quietly discarded.
    """
    from brain.ui.server import warm_embedder

    class Broken(_RecordingEmbedder):
        def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
            raise TypeError("signature drift is a bug, not a degraded backend")

    with pytest.raises(TypeError):
        warm_embedder(Broken())


def test_serve_actually_warms_the_embedder_before_binding(tmp_path: Any) -> None:
    """The CALL SITE, not the function — GAP: five tests, none covering the wiring.

    Every warm-up test above calls ``warm_embedder`` directly. Delete
    ``warm_embedder(context.embedder)`` from ``serve()`` and all five stay
    green while the measured **5,358 ms vs ~250 ms** first-search cliff comes
    straight back. A function that is perfectly tested and never invoked is
    indistinguishable, from the user's side, from one that does not exist.

    So this drives the REAL ``serve()`` with only ``uvicorn.Server.run``
    stubbed — nothing binds a socket — and asserts the embedder was warmed
    through the real call path. It deliberately does NOT patch
    ``warm_embedder``: asserting that a mock was called would prove the mock
    was installed, not that the server warms anything.

    ``run.called`` is asserted first and on purpose. If a future refactor moved
    the uvicorn call behind another indirection, the patch would stop
    intercepting, ``serve`` would try to bind a real port, and the failure
    should name that cause rather than surfacing as a confusing warm-up miss.
    """
    import contextlib
    from unittest import mock

    import uvicorn

    from brain.ui.context import UiContext
    from brain.ui.server import serve

    class _Cfg:
        vault_path = tmp_path
        database_url = "postgresql://user:pw@localhost:5432/nowhere"
        embedder = "arctic"
        vector_sim_floor = 0.25
        recency_halflife_days = 180.0
        snippet_context_tokens = 0
        owner_participants: frozenset[str] = frozenset()

    @contextlib.contextmanager
    def conn_factory() -> Any:
        raise AssertionError("serve() must not open a database connection to boot")
        yield  # pragma: no cover - unreachable, keeps the generator shape

    embedder = _RecordingEmbedder()
    context = UiContext(
        cfg=_Cfg(),
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin="http://127.0.0.1:8765",
    )

    with mock.patch.object(uvicorn.Server, "run", return_value=None) as run:
        serve(context, host="127.0.0.1", port=8765, open_browser=False)

    assert run.called, (
        "serve() never reached uvicorn.Server.run, so this test stubbed the "
        "wrong thing and proves nothing about the warm-up"
    )
    assert len(embedder.calls) == 1, (
        "serve() did not warm the embedder. warm_embedder() itself is well "
        "tested, but its CALL SITE in serve() is what converts a 5,358 ms "
        "first-search stall into startup time — and nothing else asserts it."
    )
    texts, input_type = embedder.calls[0]
    assert texts == ["warm"]
    # `query` is what a real search sends, so the warmed path is the path the
    # user's first request will actually take.
    assert input_type == "query"


# ------------------------------------------------------ the swallowed paths --
#
# Every handler below catches an exception and returns quietly. That is correct
# behaviour — telemetry must never turn a working search into a 500 — but it is
# also precisely the shape that produced this module's headline defect: ~215
# writes discarded while ``/api/status`` reported ``logging_enabled: true``.
#
# So none of these tests assert merely "nothing propagated". A handler that
# silently succeeds at swallowing is indistinguishable from one that was never
# reached, and both look identical from outside. Each test therefore pins
# something OBSERVABLE after the failure: the debug record naming the exception
# (proof the handler ran, not that the call no-opped earlier), the absence of a
# row (proof it did not half-write), and — where the connection survives — that
# it is still usable (proof the swallow did not poison the session for the
# request that continues afterwards).


def _closed_connection() -> psycopg.Connection:
    """A real connection that is closed — a genuine ``psycopg.Error`` source.

    Preferred over a mock: it exercises the real driver raising the real
    exception type the handler claims to catch, so the test cannot pass because
    a stubbed error happened to match an over-broad ``except``.
    """
    conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    conn.close()
    return conn


def test_the_probe_answers_false_when_the_catalogue_cannot_be_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ui_source_supported`` fails CLOSED, and says so.

    A wrong ``True`` here is a 500 on every single search; a wrong ``False`` is
    an empty stats bucket. The module documents that asymmetry as the reason it
    answers ``False`` on any catalogue read failure — this is that promise,
    executed.
    """
    caplog.set_level(logging.DEBUG, logger="brain.ui.telemetry")

    assert telemetry.ui_source_supported(_closed_connection()) is False

    assert any("ui telemetry probe failed" in r.message for r in caplog.records), (
        "the probe returned False without recording why. A False that is "
        "indistinguishable from 'the constraint genuinely omits ui' leaves an "
        "operator no way to tell a broken database from a correct one."
    )


def test_a_failed_search_write_is_swallowed_and_writes_nothing(
    test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture, mocker: Any
) -> None:
    """The "second belt" over the startup probe, exercised with the ONLY error
    class that actually reaches it.

    ``CheckViolation`` is chosen deliberately, and the choice is the test.
    ``gaps.record_search_query`` swallows ``OperationalError``,
    ``UndefinedTable`` and a narrowed ``UndefinedColumn`` itself, so provoking
    any of those — a closed connection is the obvious way — never reaches this
    handler at all: ``gaps`` logs its own warning and returns, and this module's
    ``except psycopg.Error`` is dead code for that input. Measured, not assumed:
    the first draft of this test used a genuinely closed connection, passed
    through ``gaps``'s handler, and asserted a debug record that ``telemetry``
    had never been given the chance to emit.

    A ``CheckViolation`` is an ``IntegrityError``, matches no handler in
    ``gaps``, and escapes — which is precisely the scenario in this module's
    docstring: ``'ui'`` joined the ``search_queries`` CHECK only in migration
    024, and a probe that went stale mid-run (a migration rolled back under a
    live server) must degrade to silence rather than to a 500 on every search.
    """
    caplog.set_level(logging.DEBUG, logger="brain.ui.telemetry")
    marker = f"swallowed-search-{uuid.uuid4().hex[:12]}"
    mocker.patch(
        "brain.gaps.record_search_query",
        side_effect=psycopg.errors.CheckViolation(
            'new row violates check constraint "search_queries_source_allowed"'
        ),
    )

    # Must not raise: the caller is a request handler mid-response.
    telemetry.record_ui_search(
        test_db,
        enabled=True,
        query=marker,
        result_count=3,
        session_id=uuid.uuid4(),
    )

    assert any("ui search telemetry skipped" in r.message for r in caplog.records), (
        "the write failed silently with no record at all — the exact condition "
        "under which ~215 searches vanished while the UI reported healthy"
    )
    rows = test_db.execute(
        "SELECT 1 FROM search_queries WHERE query = %s", (marker,)
    ).fetchall()
    assert rows == [], "a failed telemetry write left a partial row behind"
    assert test_db.execute("SELECT 1").fetchone() == (1,), (
        "the swallow left the connection unusable for the rest of the request"
    )


def test_search_telemetry_is_skipped_entirely_when_disabled(
    test_db: psycopg.Connection
) -> None:
    """``enabled=False`` must not even reach the database.

    The startup probe exists so a pre-024 database is never asked to accept
    ``source='ui'``. If a disabled writer still issued the INSERT and merely
    swallowed the ``CheckViolation``, the probe would be decorative.
    """
    marker = f"disabled-search-{uuid.uuid4().hex[:12]}"

    telemetry.record_ui_search(
        test_db,
        enabled=False,
        query=marker,
        result_count=1,
        session_id=uuid.uuid4(),
    )

    rows = test_db.execute(
        "SELECT 1 FROM search_queries WHERE query = %s", (marker,)
    ).fetchall()
    assert rows == [], "a disabled writer still wrote a row"


def _seed_document(conn: psycopg.Connection) -> str:
    """One synthetic document to hang an interaction off. No PII."""
    doc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO documents (id, title, content, content_type, kind, "
        "content_hash, source_path) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            doc_id,
            "Telemetry Fixture Note",
            "Synthetic body.",
            "note",
            "ingested",
            uuid.uuid4().hex,
            f"/tmp/telemetry-{doc_id}.md",
        ),
    )
    return doc_id


def test_opening_a_document_records_an_interaction(
    test_db: psycopg.Connection
) -> None:
    """The success path — without it, the swallow tests below prove nothing.

    A ``record_ui_open`` that never wrote anything would satisfy every
    failure-mode assertion in this section.
    """
    doc_id = _seed_document(test_db)
    session_id = uuid.uuid4()

    telemetry.record_ui_open(
        test_db,
        enabled=True,
        document_id=doc_id,
        query="how did the vendor review go",
        session_id=session_id,
    )

    row = test_db.execute(
        "SELECT action, source, query FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None, "opening a document recorded no interaction at all"
    assert row[0] == "opened"
    assert row[1] == "ui"
    assert row[2] == "how did the vendor review go"


def test_a_failed_open_is_swallowed_and_the_connection_stays_usable(
    test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """The observable that matters most: the request can continue.

    A referential-integrity failure is provoked for real — an id no document
    owns — rather than mocked, so the handler faces the genuine
    ``ForeignKeyViolation`` its ``except (psycopg.Error, InteractionError)``
    claims to cover.

    Swallowing the exception is only half the contract. This runs on the SAME
    connection the request is still using, so a handler that absorbed the error
    but left the session in a failed-transaction state would break every
    statement afterwards — a defect strictly worse than the 500 it avoided, and
    completely invisible to a test that only asserts nothing propagated.
    """
    caplog.set_level(logging.DEBUG, logger="brain.ui.telemetry")
    orphan_id = str(uuid.uuid4())

    telemetry.record_ui_open(
        test_db,
        enabled=True,
        document_id=orphan_id,
        query=None,
        session_id=uuid.uuid4(),
    )

    assert any("ui open telemetry skipped" in r.message for r in caplog.records), (
        "the open write failed with no record of it having been attempted"
    )
    assert test_db.execute("SELECT 1").fetchone() == (1,), (
        "the swallow left the connection unusable — the request that continues "
        "after this call would now fail on every statement"
    )
    rows = test_db.execute(
        "SELECT 1 FROM interactions WHERE document_id = %s", (orphan_id,)
    ).fetchall()
    assert rows == [], "a failed interaction write left a row behind"


def test_open_telemetry_is_skipped_without_a_session_id(
    test_db: psycopg.Connection
) -> None:
    """No session means nothing to correlate, so there is nothing worth writing."""
    doc_id = _seed_document(test_db)

    telemetry.record_ui_open(
        test_db, enabled=True, document_id=doc_id, query="q", session_id=None
    )

    rows = test_db.execute(
        "SELECT 1 FROM interactions WHERE document_id = %s", (doc_id,)
    ).fetchall()
    assert rows == [], "an open with no session id still wrote a row"


@pytest.mark.parametrize(
    ("raw", "expected_uuid"),
    [
        ("6f1c8f6e-3f2a-4c1b-9d7e-2a5b8c3d4e5f", True),   # a real one round-trips
        ("not-a-uuid", False),                            # junk degrades, never 400s
        ("", False),                                      # the falsy short-circuit
        (None, False),                                    # header absent entirely
    ],
)
def test_parse_session_id_tolerates_anything_a_client_sends(
    raw: str | None, expected_uuid: bool
) -> None:
    """A malformed session id is a telemetry problem, never a request problem.

    The id arrives from a client header, so it is attacker-controlled in the
    same sense any header is. Raising here would convert junk input into a
    failed search.
    """
    parsed = telemetry.parse_session_id(raw)
    if expected_uuid:
        assert parsed == uuid.UUID(str(raw))
    else:
        assert parsed is None
