"""T7's UI half — the Source dropdown's fifth value, ``none``.

``tests/test_search_predicate_source_missing.py`` proves the predicate. This
module proves the three UI-side facts that predicate cannot: that ``none``
parses to ``source_missing`` rather than to a ``source_kind`` no ingest path can
write, that it is offered by ``/api/facets`` so the dropdown can show it, and
that it actually reaches the ranker.

The last of those was blocked and is not any more — recorded here because the
mechanism is worth reusing, not because anything is still outstanding.
``hybrid_search`` names all thirteen ``build_predicate`` kwargs explicitly
rather than splatting, so for a while an additive kwarg on ``build_predicate``
was unreachable from the UI (spec defect S16). While the two-line forwarding
awaited a ruling, the end-to-end test below was marked ``xfail(strict=True)``
rather than skipped: ``strict=True`` reports an unexpected PASS as a FAILURE, so
the day the forwarding landed the run went red until the marker was removed. A
plain ``skip`` would have gone on passing silently and could have outlived the
blocker indefinitely. **The forwarding has landed and the marker is gone**; the
test below is a live assertion.

No PII: synthetic notes only.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.search import hybrid_search
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.schemas import (
    SOURCE_FILTER_VALUES,
    SOURCE_NONE,
    VALID_SOURCE_KINDS,
    parse_search_params,
)
from brain.vault import init_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
TERM = "quixotry"


# ------------------------------------------------------------- parse only --


def test_none_is_not_smuggled_into_the_real_source_kinds() -> None:
    """``none`` is a view over the corpus, not a kind of source.

    ``VALID_SOURCE_KINDS`` mirrors ``cli._VALID_SOURCE_KINDS`` and
    ``tests/test_ui_schemas.py`` asserts the two sets are equal. Adding the
    pseudo-value to it would either break that guard or push a value into
    ``sources.kind`` territory that no ingest path can ever write — and a
    ``source_kind='none'`` lookup returns an empty set silently, which is the
    failure this separation exists to prevent.
    """
    assert SOURCE_NONE not in VALID_SOURCE_KINDS
    assert VALID_SOURCE_KINDS | {SOURCE_NONE} == SOURCE_FILTER_VALUES


def test_source_none_parses_to_source_missing_not_to_source_kind() -> None:
    spec = parse_search_params({"q": "anything", "source": SOURCE_NONE})

    assert spec.source_missing is True
    assert spec.source_kind is None, (
        "'none' leaked through as a source_kind; build_predicate would look up "
        "a sources row that cannot exist and silently return nothing"
    )
    assert spec.filter_kwargs()["source_missing"] is True


@pytest.mark.parametrize("kind", sorted(VALID_SOURCE_KINDS))
def test_a_real_source_kind_does_not_set_source_missing(kind: str) -> None:
    """The two are disjoint at the parse boundary, not just in the SQL.

    Without this, a bug that set both would produce a contradictory predicate
    (``source_id`` both NULL and matching) whose symptom is an empty result set
    — indistinguishable from "you have no Slack documents yet".
    """
    spec = parse_search_params({"q": "anything", "source": kind})

    assert spec.source_kind == kind
    assert spec.source_missing is False
    assert "source_missing" not in spec.filter_kwargs(), (
        "the opt-in kwarg is emitted for a caller that did not ask for it; the "
        "whole eval-neutrality argument is that the call site is unchanged"
    )


def test_an_unknown_source_still_names_every_legal_value() -> None:
    """The 400 must list ``none`` too, or the value is undiscoverable."""
    from brain.ui.errors import UiBadRequest

    with pytest.raises(UiBadRequest) as excinfo:
        parse_search_params({"q": "anything", "source": "notion"})

    message = str(excinfo.value)
    for value in SOURCE_FILTER_VALUES:
        assert value in message, f"the 400 does not mention {value!r}: {message}"


def test_the_facet_predicate_carries_source_missing_too() -> None:
    """Facets must be scoped by the SAME filter the ranked legs used.

    ``_facets_for`` builds its own ``build_predicate`` call rather than reusing
    the search's, and its docstring promises the buckets "can never describe a
    different match set than the results they annotate". A filter added to the
    ranked path and forgotten here breaks exactly that promise, and the symptom
    is a plausible-looking number rather than an error — which is why this is
    checked instead of trusted.

    Both database calls are patched out (``mock.patch``, not monkey-patching a
    production module): the claim is about which kwargs reach
    ``build_predicate``, and the real ``build_predicate`` is deliberately left
    unpatched so its actual output is what gets asserted.
    """
    from unittest import mock

    from brain.ui import routes_search

    spec = parse_search_params({"q": "anything", "source": SOURCE_NONE})
    captured: dict[str, object] = {}

    with (
        mock.patch.object(routes_search, "build_tsquery", return_value="tsq"),
        mock.patch.object(
            routes_search,
            "compute_facets",
            side_effect=lambda conn, *, predicate, tsquery: captured.update(
                where_sql=predicate.where_sql
            ),
        ),
    ):
        routes_search._facets_for(mock.MagicMock(), spec, sensitivity=None)

    assert "d.source_id IS NULL" in captured["where_sql"], (
        "the facet predicate does not carry source_missing, so ?source=none "
        f"would count a different match set than it returns: {captured}"
    )


# ------------------------------------------------------------------ routes --


@pytest.fixture
def ui_cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
    )


@pytest.fixture
def client(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = UiContext(
        cfg=ui_cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def test_facets_offers_none_with_a_real_count(
    client: TestClient,
    test_db: psycopg.Connection,
    mixed_source_corpus: dict[str, str],
) -> None:
    """The dropdown renders the value AND a number, and the number is right.

    This test previously asserted ``count is None``. The reasoning for ``null``
    was that no statement in ``ui/queries.py`` could produce the number and
    ``source_kind_buckets`` structurally cannot (it starts ``FROM sources``, so
    a source-less document is unreachable there). The second half stays true;
    the first was a gap, not a fact about the corpus, and
    ``queries.sourceless_document_count`` closes it.

    THE SECOND SOURCED DOCUMENT IS LOAD-BEARING, and it is here because a
    mutation survived without it. ``mixed_source_corpus`` is symmetric — one
    document with a source, one without — so ``count(*) … WHERE source_id IS
    NULL`` and its exact inverse, ``… IS NOT NULL``, both return 1. Inverting
    the predicate reddened NOTHING in the whole suite. One extra sourced row
    makes the two answers 1 and 2, so the assertion below now distinguishes
    "counts the right rows" from "returns a plausible number".
    """
    test_db.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, "
        "content_type) VALUES ("
        "  (SELECT id FROM sources WHERE external_id = %s), %s, %s, %s, %s)",
        ("t7-ui-gmail", "T7 UI second sourced", "unrelated body", "t7-ui-2nd", "note"),
    )

    payload = client.get("/api/facets").json()
    by_value = {row["value"]: row for row in payload["sources"]}

    assert SOURCE_NONE in by_value, (
        f"/api/facets does not offer {SOURCE_NONE!r}: {sorted(by_value)}"
    )
    assert by_value[SOURCE_NONE]["count"] == 1, (
        "expected exactly the one source-less document; 2 means the predicate "
        "is inverted and 0 means it is counting the wrong table"
    )
    assert by_value["gmail"]["count"] == 2, (
        "the sourced rows are missing, so the count above could be right by "
        "coincidence rather than by predicate"
    )
    # Anti-vacuity: the four real kinds must still be there with real counts.
    assert set(by_value) >= VALID_SOURCE_KINDS
    for kind in VALID_SOURCE_KINDS:
        assert isinstance(by_value[kind]["count"], int)


def test_the_none_count_matches_the_rows_the_none_filter_returns(
    client: TestClient, mixed_source_corpus: dict[str, str]
) -> None:
    """The count and the click-through describe the same set — the whole point.

    A count is only worth shipping if selecting the value produces that many
    rows. ``mixed_source_corpus`` makes both documents lexically match ``TERM``,
    so the ONLY thing separating them is the source filter: the facet's number
    and the filtered result set are computed by two entirely different
    statements (``count(*) … WHERE source_id IS NULL`` versus
    ``build_predicate(source_missing=True)``) and this is what pins them
    together.
    """
    counted = {
        row["value"]: row["count"]
        for row in client.get("/api/facets").json()["sources"]
    }[SOURCE_NONE]
    returned = client.get(
        f"/api/search?q={TERM}&fts_only=1&source={SOURCE_NONE}"
    ).json()["results"]

    assert counted == len(returned) == 1, (
        f"/api/facets counts {counted} source-less documents but ?source=none "
        f"returns {len(returned)}"
    )


def test_the_none_count_does_not_inflate_manual(
    client: TestClient, mixed_source_corpus: dict[str, str]
) -> None:
    """The count came OUT of somewhere, and it must not still be in ``manual``.

    ``/api/facets``' source counts come from ``source_kind_buckets``, which
    starts ``FROM sources`` — so the source-less document was never in
    ``manual`` on THIS surface, and this test passes without the change. It is
    here as the counterfactual's other half: the same document IS misfiled under
    ``manual`` by ``compute_facets`` on the search response, which is what
    ``test_search_facets`` covers. Stated rather than implied, because "the
    count is elsewhere too" is the assumption a reader would otherwise carry
    from one module to the other.
    """
    by_value = {
        row["value"]: row["count"]
        for row in client.get("/api/facets").json()["sources"]
    }
    assert by_value["manual"] == 0, (
        "the fixture writes no manual source row, so any manual count here is "
        "the source-less document leaking into a real kind"
    )
    assert by_value["gmail"] == 1


@pytest.fixture
def mixed_source_corpus(test_db: psycopg.Connection) -> dict[str, str]:
    """One searchable document with a source row, one with none.

    Both carry ``TERM`` so both are in the match set and only the source filter
    can tell them apart. Written through raw SQL because every ingest path calls
    ``_upsert_source`` and therefore cannot produce a NULL ``source_id`` — the
    row shape under test is real (876 of them in the reference corpus, written
    by the vault sync path) but unreachable from the fixture factories.

    ``chunks.tsv`` is a GENERATED column (migration 009) and ``embedding`` is
    nullable, so a bare content insert is lexically findable with no further
    work — and the search below runs ``fts_only=1``, so the vector leg (and any
    embedding-dimension coupling) is out of the picture entirely.
    """
    ids: dict[str, str] = {}
    source_id = test_db.execute(
        "INSERT INTO sources (kind, external_id) VALUES (%s, %s) RETURNING id",
        ("gmail", "t7-ui-gmail"),
    ).fetchone()[0]

    for label, src in [("with_source", source_id), ("sourceless", None)]:
        body = f"A synthetic note about {TERM} and nothing else."
        doc_id = test_db.execute(
            "INSERT INTO documents (source_id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (src, f"T7 UI {label}", body, f"t7-ui-{label}", "note"),
        ).fetchone()[0]
        test_db.execute(
            "INSERT INTO chunks (document_id, chunk_index, content) "
            "VALUES (%s, %s, %s)",
            (doc_id, 0, body),
        )
        ids[label] = str(doc_id)
    return ids


def test_source_none_returns_the_documents_no_other_value_can(
    client: TestClient, mixed_source_corpus: dict[str, str]
) -> None:
    """End to end: ``?source=none`` reaches the ranker and filters on it.

    This is the test that proves the WIRING, not the predicate. It was
    ``xfail(strict=True)`` while ``hybrid_search`` still did not forward the
    kwarg — chosen over a plain skip precisely because an XPASS is reported as a
    failure, so the marker could not survive the fix by being ignored. The
    forwarding has since landed and the marker is gone.

    Both halves are load-bearing. That the source-less document comes back
    proves the filter reached the ranker; that nothing else does proves it
    filtered rather than merely being accepted and dropped.
    """
    response = client.get(f"/api/search?q={TERM}&fts_only=1&source={SOURCE_NONE}")
    assert response.status_code == 200, response.text
    returned = {row["id"] for row in response.json()["results"]}

    assert returned == {mixed_source_corpus["sourceless"]}, (
        "?source=none did not return exactly the source-less document — the "
        "kwarg is either not reaching hybrid_search or not filtering there"
    )


def test_the_facet_counts_describe_the_same_match_set_as_the_results(
    client: TestClient, mixed_source_corpus: dict[str, str]
) -> None:
    """Facets annotate the results they ship with, under ``?source=none`` too.

    ``_facets_for`` builds its OWN ``build_predicate`` call, so a filter can
    reach the ranked legs and not the facet rollup. The symptom is not an error
    — it is a plausible number beside a smaller result list, on a surface whose
    docstring promises the two can never disagree.

    Compared against the response's own ``total_documents`` rather than against
    ``len(results)``: that key is produced by the RANKED path's predicate (via
    ``_count_matching_documents``), so this asserts the two predicates agree,
    which is the actual claim. ``len(results)`` is capped by ``limit`` and would
    make the test pass for the wrong reason on any larger corpus.

    Summing the ``source`` leg is exact by construction — ``facets.py:163``
    records that every matched document contributes exactly one source row.
    """
    payload = client.get(
        f"/api/search?q={TERM}&fts_only=1&source={SOURCE_NONE}"
    ).json()
    facets = payload["facets"]
    assert facets is not None, "facets degraded to null; the assertion below is vacuous"
    assert payload["total_documents"], (
        "nothing matched, so a facet total of 0 would agree with it trivially"
    )

    counted = sum(bucket["count"] for bucket in facets["source"])
    assert counted == payload["total_documents"], (
        "the facet rollup counts a different match set than the ranked legs: "
        f"facets {counted}, ranked {payload['total_documents']} — ?source=none "
        "reached one predicate and not the other"
    )
