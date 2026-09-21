"""`/api/recent`, `/api/tags`, `/api/tags/{tag}` — the browse surfaces.

The corpus is seeded with direct SQL so each row's ``sent_at`` (and therefore
the generated ``doc_date`` the rail ranks on) is stated outright rather than
inferred from ingest order.

The predicate-by-predicate proof that ``recent_documents`` hides drafts,
``index.md`` and the People Hub lives in ``tests/test_ui_queries_discovery.py``
— T4 owns that query. What is asserted here is what the *route* adds: that it
projects those rows faithfully, ships tag counts rather than nulls, refuses a
tag that is not a tag, and leaks nothing when the database is down.

``brain.ui.app`` is the phase-2 integrator's file, so these routes are appended
to the real app here instead of being registered in ``create_app``.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from brain.config import Config
from brain.sensitivity import CONFIDENTIAL
from brain.ui import routes_discovery
from brain.ui.app import create_app
from brain.ui.context import UiContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    vault_path: str,
    tags: list[str],
    sent_at: datetime,
    draft: bool = False,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path,
           tags, draft, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            title,
            f"body of {title}",
            f"hash-{doc_id}",
            "note",
            "vault",
            vault_path,
            tags,
            draft,
            sent_at,
        ),
    )
    return doc_id


def _at(day: int) -> datetime:
    return datetime(2026, 3, day, 12, 0, tzinfo=UTC)


@pytest.fixture
def corpus(test_db: psycopg.Connection) -> dict[str, str]:
    """Two browseable notes, one draft, one People-Hub page. All synthetic."""
    newer = _make_doc(
        test_db,
        doc_id="11111111-0000-4000-8000-000000000001",
        title="Vendor Evaluation",
        vault_path="notes/vendor-evaluation.md",
        tags=["vendors", "planning"],
        sent_at=_at(9),
    )
    older = _make_doc(
        test_db,
        doc_id="22222222-0000-4000-8000-000000000002",
        title="Budget Review",
        vault_path="notes/budget-review.md",
        tags=["planning"],
        sent_at=_at(2),
    )
    hidden_draft = _make_doc(
        test_db,
        doc_id="33333333-0000-4000-8000-000000000003",
        title="Half Written",
        vault_path="notes/half-written.md",
        tags=["planning"],
        sent_at=_at(28),
        draft=True,
    )
    hub_page = _make_doc(
        test_db,
        doc_id="44444444-0000-4000-8000-000000000004",
        title="Person B",
        vault_path="people/person-b.md",
        tags=["planning"],
        sent_at=_at(27),
    )
    return {
        "newer": newer,
        "older": older,
        "draft": hidden_draft,
        "hub": hub_page,
    }


def _app(
    conn_factory: Any,
    tmp_path: Path,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool = True,
) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    context = UiContext(
        cfg=Config(
            database_url="postgresql://unused/in/these/tests",
            vault_path=vault,
            embedder="none",
        ),
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin=ORIGIN,
        logging_enabled=False,
        # These three routes gate on the TITLES flag, not the bodies one: they
        # are unprompted rails, so what they may name is the title question.
        # Passed explicitly because ``UiContext`` defaults it to False (fail
        # closed) — the permissive fixture has to opt in.
        serve_confidential_titles=serve_confidential_titles,
    )
    app = create_app(context)
    app.routes.extend(
        [
            Route("/api/recent", routes_discovery.recent, methods=["GET"]),
            Route("/api/tags", routes_discovery.tags, methods=["GET"]),
            Route("/api/tags/{tag}", routes_discovery.tag_documents, methods=["GET"]),
        ]
    )
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def client(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    return _app(conn_factory, tmp_path, fake_embedder)


@pytest.fixture
def strict_client(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> TestClient:
    """A client whose session may NOT name confidential documents.

    ``UiContext.serve_confidential_titles`` defaults **False**, so this is the
    shape the dataclass default already produces; the plain ``client`` above is
    the one that has to opt in. Two fixtures rather than one parametrised
    assertion because a route that ignored the flag entirely would still satisfy
    whichever branch matched its hard-coded behaviour.

    Note the default flipped with the flag: ``serve_confidential_bodies``
    defaults True and is computed as ``loopback or include_confidential``, while
    this one defaults False and comes only from
    ``cfg.ui_serve_confidential_titles``. Deliberate — an unprompted title list
    is not the same question as reading a note you opened.
    """

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    return _app(
        conn_factory, tmp_path, fake_embedder, serve_confidential_titles=False
    )


def _json(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# ------------------------------------------------------------- /api/recent --


def test_recent_ranks_by_event_date_newest_first(
    client: TestClient, corpus: dict[str, str]
) -> None:
    payload = _json(client, "/api/recent")

    ids = [row["id"] for row in payload["documents"]]
    assert ids.index(corpus["newer"]) < ids.index(corpus["older"])
    assert payload["documents"][0]["date"].startswith("2026-03-09")


def test_recent_hides_drafts_and_the_people_hub(
    client: TestClient, corpus: dict[str, str]
) -> None:
    """Both would otherwise sort to the top — they carry the newest dates."""
    ids = {row["id"] for row in _json(client, "/api/recent")["documents"]}

    assert corpus["draft"] not in ids
    assert corpus["hub"] not in ids
    assert {corpus["newer"], corpus["older"]} <= ids


def test_recent_carries_no_document_bodies(
    client: TestClient, corpus: dict[str, str]
) -> None:
    """A browse rail is a list of doorways, not a second corpus fetch."""
    raw = client.get("/api/recent").text
    assert "body of" not in raw
    for row in _json(client, "/api/recent")["documents"]:
        assert set(row) == {"id", "title", "vault_path", "source_kind", "date"}


def test_recent_count_matches_the_rows(
    client: TestClient, corpus: dict[str, str]
) -> None:
    payload = _json(client, "/api/recent")
    assert payload["count"] == len(payload["documents"])


# --------------------------------------------------------------- /api/tags --


def test_tags_ship_real_counts(client: TestClient, corpus: dict[str, str]) -> None:
    """``/api/facets`` ships ``count: null``; the tag index must not.

    THE NUMBER CHANGED ON 2026-08-14 AND THE CHANGE IS THE POINT. This asserted
    ``planning == 4`` while ``/api/tags`` served ``tag_counts``, i.e. the
    corpus-wide count over all four seeded documents — including the draft and
    the People-Hub page. The route now serves ``browseable_tag_counts``, so it
    counts the two a reader can actually open, and ``4`` would mean the route
    had regressed to the corpus-wide scope.

    Task #31 predicted this exact test would move ("the route layer already
    depends on corpus-wide counting, so any change moves that test too"), which
    is why the new value is stated with its derivation rather than adjusted to
    whatever the code returned.
    """
    buckets = {row["value"]: row["count"] for row in _json(client, "/api/tags")["tags"]}

    # Four seeded docs carry `planning`; TWO are browseable — `newer` and
    # `older`. The draft and the People-Hub page are excluded by _DISCOVERABLE.
    assert buckets["planning"] == 2
    assert buckets["vendors"] == 1


def test_tags_omits_a_tag_carried_only_by_a_confidential_document(
    strict_client: TestClient,
    corpus: dict[str, str],
    test_db: psycopg.Connection[Any],
) -> None:
    """A TAG NAME IS CONTENT, asserted at the HTTP boundary that serves it.

    The query-layer twin lives in tests/test_ui_queries_discovery.py; this one
    exists because the route is the surface that actually leaks, and because a
    future route could reach for ``tag_counts`` again without any query test
    noticing.

    THE PREMISE IS ASSERTED: ``planning`` — carried by ordinary documents in the
    same corpus — must still be present, so the absence of ``sealed-topic`` is
    evidence about sensitivity rather than about an empty or broken response.
    """
    doc_id = _make_doc(
        test_db,
        doc_id="55555555-0000-4000-8000-000000000005",
        title="Sealed Matter",
        vault_path="notes/sealed-matter.md",
        tags=["sealed-topic"],
        sent_at=_at(15),
    )
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )

    values = {row["value"] for row in _json(strict_client, "/api/tags")["tags"]}

    assert "planning" in values, (
        "the ordinary tags are missing too, so the absence below would hold "
        "even if sensitivity were ignored"
    )
    assert "sealed-topic" not in values, (
        f"/api/tags names a tag carried only by a confidential document: "
        f"{sorted(values)}"
    )


def test_tags_names_the_confidential_only_tag_for_a_session_that_may_see_it(
    client: TestClient, corpus: dict[str, str], test_db: psycopg.Connection[Any]
) -> None:
    """The permissive branch AT THE ROUTE, and its own test by ruling.

    ``client`` opts into ``serve_confidential_titles`` — the operator has set
    ``BRAIN_UI_SERVE_CONFIDENTIAL_TITLES``. The route must then name the tag,
    because that session already sees those titles in the vault tree beside it.

    Two fixtures rather than one parametrised case: a route that ignored the
    flag entirely would still satisfy whichever branch matched its hard-coded
    behaviour, and the parameter would look tested while proving nothing.

    MUTATION, RE-MEASURED 2026-08-20 after the gate moved to
    ``serve_confidential_titles``: pin this route's ``strict = True`` so it
    ignores its context -> **1 failed, 13 passed** in this file — this test
    alone, while the strict twin above stayed green. The counts differ from the
    figure recorded before the move (42 passed) because this file has since been
    run in isolation rather than alongside its neighbours; what is unchanged,
    and what the mutation is actually for, is the CONTAINMENT: the two branches
    fail separately.

    Cross-file, the same mutation also reddens
    ``test_ui_confidential_titles_gate.py::test_every_unprompted_surface_hides_it_or_none_of_them_do``,
    which is the surface-agreement test and is supposed to notice.
    """
    doc_id = _make_doc(
        test_db,
        doc_id="66666666-0000-4000-8000-000000000006",
        title="Sealed Matter",
        vault_path="notes/sealed-matter-2.md",
        tags=["sealed-topic"],
        sent_at=_at(15),
    )
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )

    values = {row["value"] for row in _json(client, "/api/tags")["tags"]}

    assert "sealed-topic" in values, (
        "a loopback session did not get the tag, so the gate is stuck closed "
        "and the rail understates its own corpus"
    )


def test_tags_are_alpha_sorted(client: TestClient, corpus: dict[str, str]) -> None:
    values = [row["value"] for row in _json(client, "/api/tags")["tags"]]
    assert values == sorted(values)


# --------------------------------------------------------- /api/tags/{tag} --


def test_tag_page_lists_only_documents_carrying_that_tag(
    client: TestClient, corpus: dict[str, str]
) -> None:
    payload = _json(client, "/api/tags/vendors")

    assert [row["id"] for row in payload["documents"]] == [corpus["newer"]]
    assert payload["tag"] == "vendors"
    assert corpus["older"] not in {row["id"] for row in payload["documents"]}


def test_tag_page_is_normalized_like_every_other_write_boundary(
    client: TestClient, corpus: dict[str, str]
) -> None:
    """``Vendors`` and ``vendors`` are the same tag; tags are stored casefolded."""
    payload = _json(client, "/api/tags/Vendors")

    assert payload["tag"] == "vendors"
    assert [row["id"] for row in payload["documents"]] == [corpus["newer"]]


def test_tag_page_hides_drafts_and_the_people_hub(
    client: TestClient, corpus: dict[str, str]
) -> None:
    ids = {row["id"] for row in _json(client, "/api/tags/planning")["documents"]}

    assert ids == {corpus["newer"], corpus["older"]}


def test_an_unused_tag_is_an_empty_list_not_an_error(
    client: TestClient, corpus: dict[str, str]
) -> None:
    payload = _json(client, "/api/tags/nothing-uses-this")
    assert payload["documents"] == []
    assert payload["count"] == 0


# ------------------------------------------------------------ fails closed --


def test_a_tag_that_is_not_a_tag_is_a_typed_400(
    client: TestClient, corpus: dict[str, str]
) -> None:
    """``-`` normalizes away to nothing. Answering 200/[] would read as "no
    documents carry this tag", which is a different and untrue statement."""
    response = client.get("/api/tags/-")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_tag"


def test_a_database_failure_is_a_leak_free_503(
    tmp_path: Path, fake_embedder: Any
) -> None:
    @contextlib.contextmanager
    def broken_factory() -> Any:
        raise psycopg.OperationalError("connection refused to 10.0.0.1:5432 as brain")
        yield  # pragma: no cover — unreachable, keeps this a generator

    client = _app(broken_factory, tmp_path, fake_embedder)

    for path in ("/api/recent", "/api/tags", "/api/tags/planning"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == "database_unavailable"
        assert "10.0.0.1" not in response.text
