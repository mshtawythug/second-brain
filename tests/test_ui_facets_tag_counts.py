"""`/api/facets` ships REAL tag counts, not ``count: null``.

``queries.list_existing_tags`` already computed a per-tag document count and
discarded it, so this route shipped ``count: null`` for tags while every other
facet carried a number. T4 added ``ui_queries.tag_counts``; this is the route
side of that fix.

**Why the assertion is on the VALUE and not on the key.** ``count: null``
satisfies "the response has a ``count`` key" perfectly — a key-presence
assertion here is a test that cannot fail, because the exact bug being removed
passes it. The counts below are compared against the number of documents the
test itself seeded.

No PII: two synthetic notes and two invented tag names.
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
from brain.sensitivity import CONFIDENTIAL
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

#: Invented, and deliberately not words any other fixture in the suite seeds —
#: a collision would inflate the expected count and the test would be measuring
#: the whole suite's corpus rather than its own.
TAG_ON_TWO = "zorbtag-pair"
TAG_ON_ONE = "zorbtag-single"


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


@pytest.fixture
def tagged_corpus(seed_doc: Any) -> None:
    """Two documents carrying one tag, one carrying another.

    Two different counts, not two documents with the same tag: a bug that
    returned a constant, or the number of *tags* rather than the number of
    documents, would pass a single-count fixture.
    """
    seed_doc(title="Tagged note A", content="Synthetic body A.", tags=[TAG_ON_TWO])
    seed_doc(title="Tagged note B", content="Synthetic body B.", tags=[TAG_ON_TWO])
    seed_doc(title="Tagged note C", content="Synthetic body C.", tags=[TAG_ON_ONE])


def test_tag_facets_carry_the_real_document_count(
    client: TestClient, tagged_corpus: None
) -> None:
    """The counts are the seeded numbers — 2 and 1, not ``null`` and not equal."""
    payload = client.get("/api/facets").json()
    counts = {row["value"]: row["count"] for row in payload["tags"]}

    assert counts.get(TAG_ON_TWO) == 2, (
        f"expected 2 documents for {TAG_ON_TWO!r}, got {counts.get(TAG_ON_TWO)!r} "
        "— `null` here means the route is still discarding the count the query "
        "already computes"
    )
    assert counts.get(TAG_ON_ONE) == 1, (
        f"expected 1 document for {TAG_ON_ONE!r}, got {counts.get(TAG_ON_ONE)!r}"
    )


def test_no_tag_facet_ships_a_null_count(
    client: TestClient, tagged_corpus: None
) -> None:
    """Not one tag bucket may carry ``null``.

    The test above only inspects two tags it seeded itself. This is the
    corpus-wide claim: a change that counted *some* tags and not others — say,
    by falling back to the old comprehension on one branch — would leave the
    first test green.
    """
    nulls = [row["value"] for row in payload_tags(client) if row["count"] is None]
    assert not nulls, f"tag facets still shipping a null count: {nulls}"


def payload_tags(client: TestClient) -> list[dict[str, Any]]:
    """The ``tags`` buckets from ``/api/facets``."""
    return list(client.get("/api/facets").json()["tags"])


# ------------------------------------------------ the confidentiality gate --

#: A tag no other document in this module's corpus carries, held by a single
#: confidential note. Its NAME is the disclosure under test.
SEALED_TAG = "zorbtag-sealed"

#: A tag carried by one ordinary note AND one confidential note. Its name leaks
#: nothing — the ordinary note publishes it anyway — so what is under test here
#: is the COUNT, i.e. how many confidential documents exist behind it.
SHARED_TAG = "zorbtag-shared"


def _confidential(conn: psycopg.Connection[Any], doc_id: str) -> str:
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )
    return doc_id


def _facet_client(
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool,
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    return TestClient(
        create_app(
            UiContext(
                cfg=ui_cfg,
                conn_factory=conn_factory,
                embedder=fake_embedder,
                search_fn=hybrid_search,
                allowed_origin=ORIGIN,
                logging_enabled=False,
                serve_confidential_titles=serve_confidential_titles,
            )
        ),
        base_url=ORIGIN,
    )


@pytest.fixture
def sealed_corpus(test_db: psycopg.Connection, seed_doc: Any) -> None:
    seed_doc(title="Open note", content="Synthetic open body.", tags=[SHARED_TAG])
    _confidential(
        test_db,
        seed_doc(
            title="Sealed note",
            content="Synthetic sealed body.",
            tags=[SEALED_TAG, SHARED_TAG],
        ),
    )


def _tag_counts(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/facets")
    assert response.status_code == 200, response.text
    return {row["value"]: row["count"] for row in response.json()["tags"]}


def test_the_facet_dropdown_does_not_name_a_confidential_only_tag(
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
    sealed_corpus: None,
) -> None:
    counts = _tag_counts(
        _facet_client(
            test_db, ui_cfg, fake_embedder, serve_confidential_titles=False
        )
    )

    assert SEALED_TAG not in counts, (
        f"/api/facets named a tag carried only by a confidential document: "
        f"{SEALED_TAG!r}"
    )
    assert counts.get(SHARED_TAG) == 1, (
        "anti-vacuity: the dropdown must still be populated — an empty facet "
        f"panel would satisfy the assertion above for the wrong reason: {counts}"
    )


def test_an_opted_in_session_still_gets_the_confidential_tag(
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
    sealed_corpus: None,
) -> None:
    counts = _tag_counts(
        _facet_client(test_db, ui_cfg, fake_embedder, serve_confidential_titles=True)
    )

    assert SEALED_TAG in counts, (
        "an opted-in session lost a tag it is entitled to see; a route that "
        "hard-coded 'always hide' would look correct in the strict test alone"
    )
    assert counts.get(SHARED_TAG) == 2, counts


def test_the_facet_count_excludes_confidential_documents(
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
    sealed_corpus: None,
) -> None:
    strict = _tag_counts(
        _facet_client(
            test_db, ui_cfg, fake_embedder, serve_confidential_titles=False
        )
    )
    permissive = _tag_counts(
        _facet_client(test_db, ui_cfg, fake_embedder, serve_confidential_titles=True)
    )

    assert strict[SHARED_TAG] == 1, strict
    assert permissive[SHARED_TAG] == 2, permissive
