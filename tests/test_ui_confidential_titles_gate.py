"""One flag, every listing surface that can name a confidential document.

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
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from brain.config import Config, ConfigError
from brain.sensitivity import CONFIDENTIAL
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
#: A third, ordinary document, linked to the open note. It exists so the
#: links rail still renders SOMETHING once the confidential neighbour is
#: withheld — without it that surface is empty under the strict flag and its
#: half of the ruling passes vacuously. (Measured: the anti-vacuity test
#: named ``/api/notes/{id_prefix}/links`` before this was added.)
OPEN_NEIGHBOUR_TITLE = "Budget Review"


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

    The confidential note carries a tag NO other document carries, so every
    gated surface can reach it in its own way: as a tree leaf, as a recent row,
    as a tag name in the index and its facet dropdown, as the sole row of its
    tag page, and — via the edges below — as a neighbour on the open note's
    links rail.

    THE EDGES ARE PART OF THE FIXTURE, not of one test. ``/api/notes/{id}/links``
    can only be *observed* to leak if the confidential document is somebody's
    neighbour, and a surface that cannot leak in the fixture is a surface this
    module silently stops covering. Both directions plus a derived edge, because
    the route projects three separate queries.
    """
    ids = {
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
    ids["neighbour"] = _make_doc(
        test_db,
        doc_id="b2222222-0000-4000-8000-000000000003",
        title=OPEN_NEIGHBOUR_TITLE,
        vault_path="projects/budget-review.md",
        tags=[OPEN_TAG],
        sensitivity="normal",
    )
    for src_id, dst_id, text in (
        (ids["open"], ids["sealed"], f"[[{SEALED_TITLE}]]"),
        (ids["sealed"], ids["open"], f"[[{OPEN_TITLE}]]"),
        (ids["open"], ids["neighbour"], f"[[{OPEN_NEIGHBOUR_TITLE}]]"),
        (ids["neighbour"], ids["open"], f"[[{OPEN_TITLE}]]"),
    ):
        test_db.execute(
            "INSERT INTO links (src_document_id, dst_document_id, link_text, "
            "link_kind, display_text) VALUES (%s, %s, %s, %s, %s)",
            (src_id, dst_id, text, "wiki", None),
        )
    lo, hi = sorted((ids["open"], ids["sealed"]))
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, "
        "evidence, weight) VALUES (%s, %s, %s, %s::jsonb, %s)",
        (lo, hi, "shared_thread", "{}", 0.5),
    )
    return ids


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
    return TestClient(create_app(context), base_url=ORIGIN)


#: How to *call* each GET route — path-parameter values only, plus a query
#: string where one is required. Hand-maintained, and that is safe in a way the
#: old roster was not: this map says how to reach a route, never whether the
#: route is allowed to leak. A route added without an entry here raises
#: :class:`_UncoveredRoute` rather than being skipped, so the failure mode of
#: forgetting is a red test naming the route, not a silent gap.
_PATH_ARGS_FIXED = {"tag": SEALED_TAG}
_QUERY_STRINGS = {"/api/search": "q=severance"}

#: Routes that may name a confidential document, with the ruling for each.
#: The only hand-maintained EXEMPTION here (``_KNOWN_GATED`` below is also
#: hand-maintained, but it only ever tightens). Kept short on purpose, and it
#: fails closed: a new leaking route is in neither this set nor the exempt path,
#: so it lands in the discovered gated set and must hide.
#:
#: - ``/api/search`` — PROMPTED. The reader typed a query. ``routes_search``
#:   returns confidential hits with their titles and blanks only the snippet
#:   (``_redact``), gated on ``serve_confidential_bodies``. That is the settled
#:   ruling for a surface the reader asked for, and the titles flag governs the
#:   ones they did not.
#: - ``/api/notes/{id_prefix}`` — PROMPTED, and it is the document itself. A
#:   reader who opened a note by id is not being *told* it exists.
_PROMPTED = frozenset({"/api/search", "/api/notes/{id_prefix}"})

#: The surfaces this module was written to cover. NOT the roster — the roster is
#: discovered below — but a floor under it: if a refactor stops one of these
#: from being able to name the confidential document at all, the discovered set
#: shrinks and every assertion over it gets weaker while staying green. This
#: turns that into a failure. Its four original entries were the entire roster
#: before ``/api/facets`` and the links rail were found to leak; both were
#: unprompted listing surfaces the hand-maintained list simply did not contain.
_KNOWN_GATED = frozenset(
    {
        "/api/tree",
        "/api/recent",
        "/api/tags",
        "/api/tags/{tag}",
        "/api/facets",
        "/api/notes/{id_prefix}/links",
    }
)


class _UncoveredRoute(Exception):
    """A GET route this module does not know how to call.

    Raised rather than skipped. The defect this whole module exists to close was
    a surface nobody had listed; a discovery pass that quietly ignored what it
    could not construct would reproduce it exactly.
    """


def _get_paths(client: TestClient) -> list[str]:
    """Every distinct GET route template on the real application."""
    seen: list[str] = []
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, Route) or "GET" not in (route.methods or set()):
            continue
        if route.path not in seen:
            seen.append(route.path)
    return seen


def _url_for(path: str, *, open_id: str) -> str:
    args = {**_PATH_ARGS_FIXED, "id_prefix": open_id}
    url = path
    for name in re.findall(r"{([^}:]+)", path):
        if name not in args:
            raise _UncoveredRoute(
                f"GET {path} takes a path parameter {name!r} this module cannot "
                "supply, so it was never requested and its confidentiality was "
                "never checked. Add a value to _PATH_ARGS_FIXED (or to _url_for "
                "if it must vary), then decide whether the route belongs in "
                "_PROMPTED."
            )
        url = url.replace("{" + name + "}", str(args[name]))
    query = _QUERY_STRINGS.get(path)
    return f"{url}?{query}" if query else url


def _names_the_sealed_document(
    client: TestClient, path: str, *, open_id: str
) -> bool:
    """Does this route put the confidential title or its tag on screen?

    MEASURED FROM THE RESPONSE BODY, which is what makes the roster discovered
    rather than declared. The old version of this module asked four named routes
    four bespoke questions ("is the title among the tree's leaves", "among the
    recent rows"), so a fifth surface was invisible to it by construction. A
    substring test over the raw payload asks the only question the reader cares
    about — did these bytes name it — and asks it of every route equally,
    including routes added after this was written.

    Deliberately not JSON-shape-aware: a leak in a key nobody anticipated is
    still a leak, and shape-awareness is precisely how the previous roster
    limited itself to what it already knew.
    """
    url = _url_for(path, open_id=open_id)
    response = client.get(url)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    # A MARKER THE CALLER PUT IN THE URL IS NOT A DISCLOSURE. ``/api/tags/{tag}``
    # echoes the canonical tag it was asked for, so scanning its body for the
    # sealed TAG reports a leak on a response that correctly returned no
    # documents. Measured: that false positive is what this subtraction fixes,
    # and the sealed TITLE is still scanned for on that route — the half that
    # can actually leak.
    disclosed = {m for m in (SEALED_TITLE, SEALED_TAG) if m not in url}
    return any(marker in response.text for marker in disclosed)


def _names_the_open_document(
    client: TestClient, path: str, *, open_id: str
) -> bool:
    """The same measurement, for the ORDINARY document.

    ``{tag}`` is swapped to the open tag so the tag page is asked about the
    document it can actually return; every other route is corpus-wide or keyed
    on the open note already.
    """
    url = _url_for(path.replace("{tag}", OPEN_TAG), open_id=open_id)
    response = client.get(url)
    assert response.status_code == 200, f"{path}: {response.text}"
    ordinary = (OPEN_TITLE, OPEN_TAG, OPEN_NEIGHBOUR_TITLE)
    return any(marker in response.text for marker in ordinary)


def _surfaces(client: TestClient, *, open_id: str) -> dict[str, bool]:
    """Per non-prompted GET route: does it reveal the confidential document?

    Keyed by route template so a disagreement is reported as *which* surface
    disagreed rather than as a bare False.
    """
    return {
        path: _names_the_sealed_document(client, path, open_id=open_id)
        for path in _get_paths(client)
        if path not in _PROMPTED
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
    the flag would look consulted while being ignored. It is also where the
    roster comes from — see ``gated`` below.
    """
    open_id = corpus["open"]
    hiding = _surfaces(
        _client(test_db, tmp_path, fake_embedder, serve_confidential_titles=False),
        open_id=open_id,
    )
    naming = _surfaces(
        _client(test_db, tmp_path, fake_embedder, serve_confidential_titles=True),
        open_id=open_id,
    )

    # THE ROSTER, DISCOVERED: every non-prompted route that CAN name the
    # confidential document, according to the route itself when permitted to.
    gated = {path for path, named in naming.items() if named}

    assert gated >= _KNOWN_GATED, (
        f"a surface that used to be able to name the confidential document no "
        f"longer can, so the assertions below silently stopped covering it: "
        f"{sorted(_KNOWN_GATED - gated)}"
    )
    assert all(hiding[path] is False for path in gated), (
        f"a gated listing surface named a confidential document while its "
        f"neighbours hid it: {sorted(p for p in gated if hiding[p])}"
    )
    # And the class, not just the roster: ANY non-prompted route, including one
    # added after this was written and absent from _KNOWN_GATED, must hide.
    assert hiding == dict.fromkeys(hiding, False), (
        f"an unprompted route named the confidential document or its tag: "
        f"{sorted(p for p, named in hiding.items() if named)}"
    )


def test_the_route_table_is_fully_covered(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    corpus: dict[str, str],
) -> None:
    """Every GET route is either exercised above or explicitly exempt.

    The failure this module exists to close was a surface nobody had listed. A
    discovery pass is only worth more than a list if *not being reachable* is
    itself an error, so this asserts the partition is total: prompted, or
    requested and checked. A new route with an unfamiliar path parameter raises
    :class:`_UncoveredRoute` out of ``_url_for`` and lands here by name.
    """
    client = _client(
        test_db, tmp_path, fake_embedder, serve_confidential_titles=False
    )
    paths = set(_get_paths(client))

    # THE ONE HATCH THE DISCOVERY CANNOT SEE. A route already in _KNOWN_GATED
    # cannot be silenced by exempting it — dropping it from ``_surfaces`` makes
    # ``gated >= _KNOWN_GATED`` fail (MEASURED: mutation M14). A route that never
    # entered _KNOWN_GATED can be, by adding it straight here. That is a ruling,
    # so it is made loud: bump this number in the same edit and write the reason
    # into _PROMPTED's docstring above.
    assert len(_PROMPTED) == 2, (
        f"the prompted-exemption list changed size. Each entry silences a route "
        f"for every assertion in this module, so each needs a written ruling: "
        f"{sorted(_PROMPTED)}"
    )
    assert paths >= _PROMPTED, (
        f"_PROMPTED exempts routes that no longer exist, so the exemption is "
        f"protecting nothing and hiding nothing: {sorted(_PROMPTED - paths)}"
    )
    assert paths >= _KNOWN_GATED, (
        f"_KNOWN_GATED names routes that no longer exist: "
        f"{sorted(_KNOWN_GATED - paths)}"
    )
    checked = set(_surfaces(client, open_id=corpus["open"]))
    assert checked | _PROMPTED == paths, (
        f"GET routes neither requested nor exempted: "
        f"{sorted(paths - checked - _PROMPTED)}"
    )


def test_an_unknown_path_parameter_is_loud(corpus: dict[str, str]) -> None:
    """Omission fails; it does not skip.

    ``_url_for`` is the one place a future route can fall out of this module's
    coverage, and it is the place the previous roster's failure mode lived: a
    surface nobody listed was simply not asked about. Pinned as its own test
    because it is a guard whose absence is invisible — every other assertion
    here stays green when a route is quietly not requested.
    """
    with pytest.raises(_UncoveredRoute, match="slug"):
        _url_for("/api/things/{slug}", open_id=corpus["open"])


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
    blank = sorted(
        path
        for path in _KNOWN_GATED
        if not _names_the_open_document(client, path, open_id=corpus["open"])
    )

    assert not blank, (
        f"these surfaces rendered neither the ordinary title nor its tag, so "
        f"their half of the confidentiality assertions passes vacuously: "
        f"{blank}"
    )


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
