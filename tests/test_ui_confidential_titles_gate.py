"""One flag, three unprompted listing surfaces, and the plumbing that feeds it.

``tests/test_ui_tree_confidential.py`` proves the tree in isolation and
``tests/test_ui_routes_discovery.py`` proves the two rails in theirs. What
neither can prove is the property the ruling is actually about: that the three
surfaces **agree**. Before this gate existed the tree named every confidential
title while the rail beside it hid them, and each surface's own tests passed —
the defect lived precisely in the gap between two green modules.

So this module asserts the surfaces against each other, and pins the flag's
route from ``BRAIN_UI_SERVE_CONFIDENTIAL_TITLES`` to ``UiContext``.

All fixture data is synthetic.
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

from brain.config import Config, ConfigError
from brain.sensitivity import CONFIDENTIAL
from brain.ui import routes_discovery
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.server import build_context

from .conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
SEALED_TITLE = "Severance Terms"
SEALED_TAG = "sealed-topic"
OPEN_TITLE = "Vendor Shortlist"
OPEN_TAG = "vendors"


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    vault_path: str,
    tags: list[str],
    sensitivity: str,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path,
           tags, draft, sent_at, sensitivity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            False,
            datetime(2026, 3, 4, 12, 0, tzinfo=UTC),
            sensitivity,
        ),
    )
    return doc_id


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    """One ordinary note and one confidential note, both browseable.

    The confidential note carries a tag NO other document carries, so it is
    visible to all three surfaces in three different ways: as a tree leaf, as a
    recent row, and as a tag name in the index.
    """
    return {
        "open": _make_doc(
            test_db,
            doc_id="b2222222-0000-4000-8000-000000000001",
            title=OPEN_TITLE,
            vault_path="projects/vendor-shortlist.md",
            tags=[OPEN_TAG],
            sensitivity="normal",
        ),
        "sealed": _make_doc(
            test_db,
            doc_id="b2222222-0000-4000-8000-000000000002",
            title=SEALED_TITLE,
            vault_path="projects/severance-terms.md",
            tags=[SEALED_TAG],
            sensitivity=CONFIDENTIAL,
        ),
    }


def _client(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool,
) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

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
        serve_confidential_titles=serve_confidential_titles,
    )
    app = create_app(context)
    # ``brain.ui.app`` is the phase-2 integrator's file, so the discovery routes
    # are appended here rather than registered in ``create_app`` — the same
    # arrangement ``test_ui_routes_discovery`` uses.
    app.routes.extend(
        [
            Route("/api/recent", routes_discovery.recent, methods=["GET"]),
            Route("/api/tags", routes_discovery.tags, methods=["GET"]),
            Route("/api/tags/{tag}", routes_discovery.tag_documents, methods=["GET"]),
        ]
    )
    return TestClient(app, base_url=ORIGIN)


def _tree_titles(node: dict[str, Any]) -> set[str]:
    found = {note["title"] for note in node["notes"]}
    for child in node["children"]:
        found |= _tree_titles(child)
    return found


def _surfaces(client: TestClient) -> dict[str, bool]:
    """Does each gated listing surface reveal the confidential document?

    Returns one boolean per surface, keyed by name, so a disagreement is
    reported as *which* surface disagreed rather than as a bare False.

    ``tag_page`` is included and it is the one surface here a reader arguably
    *did* ask for — you have to click a tag to reach it. It is in this dict
    because it shares ``routes_discovery``'s gate, and MEASURED: with only the
    other three, opening the ``tag_documents`` gate alone reddened **nothing in
    the entire suite**. That gate existed and asserted nothing, which is the
    failure mode this repo keeps rediscovering. Its inclusion here is what makes
    it a guard rather than a comment.
    """
    tree = client.get("/api/tree")
    recent = client.get("/api/recent")
    tags = client.get("/api/tags")
    tag_page = client.get(f"/api/tags/{SEALED_TAG}")
    for name, response in [
        ("tree", tree), ("recent", recent), ("tags", tags), ("tag_page", tag_page)
    ]:
        assert response.status_code == 200, f"{name}: {response.text}"
    return {
        "tree": SEALED_TITLE in _tree_titles(tree.json()),
        "recent": SEALED_TITLE in {
            row["title"] for row in recent.json()["documents"]
        },
        "tags": SEALED_TAG in {row["value"] for row in tags.json()["tags"]},
        "tag_page": SEALED_TITLE in {
            row["title"] for row in tag_page.json()["documents"]
        },
    }


def test_every_unprompted_surface_hides_it_or_none_of_them_do(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    corpus: dict[str, str],
) -> None:
    """THE ruling, as one assertion: no surface may differ from its neighbours.

    Both settings are exercised in one test on purpose — the claim is not "they
    hide" or "they show" but "they agree", and a single-setting test cannot
    express that. A surface left gated on ``serve_confidential_bodies`` shows up
    here as a ``True`` among ``False``s in the strict half.

    The permissive half is what keeps the strict half honest: without it, a
    surface that hard-coded "always hide" would pass the strict assertion and
    the flag would look consulted while being ignored.
    """
    hiding = _surfaces(
        _client(test_db, tmp_path, fake_embedder, serve_confidential_titles=False)
    )
    naming = _surfaces(
        _client(test_db, tmp_path, fake_embedder, serve_confidential_titles=True)
    )

    assert hiding == dict.fromkeys(("tree", "recent", "tags", "tag_page"), False), (
        f"a gated listing surface named a confidential document while its "
        f"neighbours hid it: {hiding}"
    )
    assert naming == dict.fromkeys(("tree", "recent", "tags", "tag_page"), True), (
        f"an opted-in session did not get a confidential document from every "
        f"gated listing surface: {naming}"
    )


def test_the_ordinary_document_reaches_every_surface_either_way(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    corpus: dict[str, str],
) -> None:
    """Anti-vacuity for the test above.

    Every assertion up there is about ONE document's presence or absence. If the
    fixture, the routes, or the app wiring were broken such that these surfaces
    returned nothing at all, the strict half would pass and the permissive half
    would fail with a message pointing at confidentiality — which is the wrong
    diagnosis. This test makes "the surfaces work" a separate claim.
    """
    client = _client(
        test_db, tmp_path, fake_embedder, serve_confidential_titles=False
    )
    tree = client.get("/api/tree").json()
    recent = client.get("/api/recent").json()
    tags = client.get("/api/tags").json()
    tag_page = client.get(f"/api/tags/{OPEN_TAG}").json()

    assert OPEN_TITLE in _tree_titles(tree)
    assert OPEN_TITLE in {row["title"] for row in recent["documents"]}
    assert OPEN_TAG in {row["value"] for row in tags["tags"]}
    assert OPEN_TITLE in {row["title"] for row in tag_page["documents"]}


def test_health_reports_both_gates_separately(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
) -> None:
    """A client must be able to tell the two flags apart.

    They now differ in default and in meaning, so reporting only
    ``serve_confidential_bodies`` would let a client conclude the wrong thing
    about what the tree contains. The values are asserted as a pair, and they
    are DIFFERENT here, so a payload that echoed one key into both would fail.
    """
    payload = _client(
        test_db, tmp_path, fake_embedder, serve_confidential_titles=False
    ).get("/api/health").json()

    assert payload["serve_confidential_bodies"] is True
    assert payload["serve_confidential_titles"] is False


# ------------------------------------------------------------------ config --


def _cfg(**kwargs: Any) -> Config:
    return Config(database_url=TEST_DATABASE_URL, embedder="none", **kwargs)


@pytest.mark.parametrize("flag", [True, False])
def test_build_context_carries_the_config_flag_through(flag: bool) -> None:
    """``cfg.ui_serve_confidential_titles`` reaches ``UiContext``, both values.

    Parametrised over both because a ``build_context`` that hard-coded either
    constant would satisfy a single-value test.
    """
    context = build_context(
        _cfg(ui_serve_confidential_titles=flag),
        host="127.0.0.1",
        port=8765,
        read_only=False,
        token="t",
        include_confidential=False,
        embedder=None,
    )
    assert context.serve_confidential_titles is flag


def test_the_titles_gate_is_not_opened_by_loopback_or_include_confidential() -> None:
    """The two flags are independent, which is the substance of "separate".

    ``serve_confidential_bodies`` is ``loopback or include_confidential`` and is
    True here on both counts. If ``serve_confidential_titles`` were quietly
    ``or``-ed with either — the obvious "helpful" edit — this fails. That edit
    would also make the documented default (hide) unreachable in the only
    configuration anyone runs, since ``brain ui`` binds loopback by default.
    """
    context = build_context(
        _cfg(ui_serve_confidential_titles=False),
        host="127.0.0.1",  # loopback
        port=8765,
        read_only=False,
        token="t",
        include_confidential=True,  # and the explicit opt-in
        embedder=None,
    )
    assert context.serve_confidential_bodies is True
    assert context.serve_confidential_titles is False


def test_the_env_var_parses_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset -> hide; a recognised token -> that value; anything else -> error.

    The error branch matters more than usual here. A silently-ignored typo in a
    flag whose default is "hide" is invisible: the operator sets
    ``BRAIN_UI_SERVE_CONFIDENTIAL_TITLES=ture``, sees a tree with no
    confidential notes, and concludes there are none.
    """
    monkeypatch.delenv("BRAIN_UI_SERVE_CONFIDENTIAL_TITLES", raising=False)
    assert Config._load_field_dict(require_db=False)[
        "ui_serve_confidential_titles"
    ] is False

    for token in ("1", "true", "YES", "on"):
        monkeypatch.setenv("BRAIN_UI_SERVE_CONFIDENTIAL_TITLES", token)
        assert Config._load_field_dict(require_db=False)[
            "ui_serve_confidential_titles"
        ] is True, token

    for token in ("0", "false", "NO", "off", ""):
        monkeypatch.setenv("BRAIN_UI_SERVE_CONFIDENTIAL_TITLES", token)
        assert Config._load_field_dict(require_db=False)[
            "ui_serve_confidential_titles"
        ] is False, token

    monkeypatch.setenv("BRAIN_UI_SERVE_CONFIDENTIAL_TITLES", "ture")
    with pytest.raises(ConfigError, match="BRAIN_UI_SERVE_CONFIDENTIAL_TITLES"):
        Config._load_field_dict(require_db=False)
