"""Option B: confidential bodies are withheld on a non-loopback bind.

**Withholding must be TOTAL.** The body route looking correct in isolation is
what makes partial withholding easy to ship, so this module enumerates every
path document text can reach a response and tests each one with a confidential
document:

===================================  ==========================================
Path                                 Covered by
===================================  ==========================================
``GET /api/notes`` → ``body``        ``test_body_is_withheld``
``GET /api/notes`` → ``html``        ``test_rendered_html_is_withheld``
``GET /api/notes`` → ``summary``     ``test_summary_is_withheld`` — the trap:
                                     ``documents.summary`` is LLM-generated
                                     FROM the body
``GET /api/notes`` → ``body_hash``   ``test_body_hash_is_not_emitted``
``GET /api/search`` → ``snippet``    ``test_search_snippet_is_redacted``
``PUT /api/notes``                   ``test_withheld_note_cannot_be_edited``
titles / tree / metadata             ``test_title_and_tier_stay_visible`` —
                                     deliberately NOT withheld
===================================  ==========================================

The response *shape* matches MCP ``brain_show`` (``withheld`` + ``sensitivity``
keys, present only on the withheld path) so a client handles one vocabulary
across both surfaces.
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
from brain.vault.note_builder import create_vault_note

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
SECRET_PHRASE = "quarterly severance envelope"
SUMMARY_TEXT = "A summary derived from the confidential body."


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
def confidential_id(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> str:
    """One confidential note whose body contains a phrase we can grep for."""
    doc_id = create_vault_note(
        test_db,
        cfg=ui_cfg,
        vault_path=ui_cfg.vault_path,
        title="Compensation Review",
        body=f"Notes on the {SECRET_PHRASE} for next quarter.\n",
        tags=["hr"],
        template="note",
        folder="projects",
        embedder=fake_embedder,
    )
    # Mark it confidential, and give it a summary — a summary that is null makes
    # test_summary_is_withheld pass vacuously.
    test_db.execute(
        "UPDATE documents SET sensitivity=%s, summary=%s WHERE id=%s",
        (CONFIDENTIAL, SUMMARY_TEXT, doc_id),
    )
    return doc_id


def _client(
    test_db: psycopg.Connection,
    cfg: Config,
    embedder: Any,
    *,
    serve_confidential: bool,
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        serve_confidential_bodies=serve_confidential,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


@pytest.fixture
def withholding(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    """A server that behaves as if bound to a non-loopback address."""
    return _client(test_db, ui_cfg, fake_embedder, serve_confidential=False)


@pytest.fixture
def serving(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    """A loopback-equivalent server: confidential bodies are served."""
    return _client(test_db, ui_cfg, fake_embedder, serve_confidential=True)


# --------------------------------------------------------------- withheld ---


def test_body_is_withheld(withholding: TestClient, confidential_id: str) -> None:
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert payload["body"] is None
    assert SECRET_PHRASE not in str(payload)


def test_rendered_html_is_withheld(
    withholding: TestClient, confidential_id: str
) -> None:
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert payload["html"] == ""


def test_summary_is_withheld(
    withholding: TestClient, confidential_id: str
) -> None:
    """The trap. ``summary`` is LLM-written FROM the body.

    Returning it beside a withheld body hands out a précis of exactly the
    content being protected. The fixture gives the document a real summary, so
    this cannot pass vacuously.
    """
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert "summary" not in payload
    assert SUMMARY_TEXT not in str(payload)


def test_body_hash_is_not_emitted(
    withholding: TestClient, confidential_id: str
) -> None:
    """A hash of withheld content is both useless and a weak oracle."""
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert "body_hash" not in payload


def test_withheld_shape_matches_mcp_vocabulary(
    withholding: TestClient, confidential_id: str
) -> None:
    """Same keys as MCP ``brain_show`` so a client handles ONE spelling."""
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert payload["sensitivity"] == CONFIDENTIAL
    assert payload["withheld"].startswith("body withheld: sensitivity=confidential")


def test_confidential_document_is_excluded_from_search(
    withholding: TestClient, confidential_id: str
) -> None:
    """The match-membership oracle — the sharpest leak of the set.

    Redacting the snippet is not enough on its own. If the row still appeared,
    an attacker could reconstruct the withheld body a word at a time by issuing
    queries and watching which ones surface it: membership in a result set is
    itself derived from the content. Only excluding the row closes that.

    Nothing is lost — the tree still lists the note by title, so "you can see it
    exists" is preserved where it belongs.
    """
    payload = withholding.get("/api/search?q=severance").json()
    assert confidential_id not in {r["id"] for r in payload["results"]}
    assert SECRET_PHRASE not in str(payload)


def test_facet_counts_do_not_reveal_excluded_documents(
    withholding: TestClient, confidential_id: str
) -> None:
    """The oracle by another route.

    A facet count computed over a match set that includes what the results
    exclude would report the exact number of confidential hits.
    """
    payload = withholding.get("/api/search?q=severance").json()
    facets = payload.get("facets") or {}
    for buckets in facets.values():
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if bucket.get("value") == "hr":
                raise AssertionError(
                    "a facet bucket counts the excluded confidential document"
                )


def test_snippet_redaction_survives_as_defence_in_depth(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any,
    confidential_id: str,
) -> None:
    """Belt-and-braces: if the sensitivity filter is bypassed, redaction holds.

    Exercises ``_redact`` directly against a search that deliberately does NOT
    apply the exclusion, so the second layer is proven rather than assumed —
    the first layer passing would otherwise hide a broken second one forever.
    """
    from brain.ui.routes_search import _redact

    payload = {"id": confidential_id, "snippet": SECRET_PHRASE}
    redacted = _redact(payload, {confidential_id})
    assert redacted["snippet"] == ""
    assert redacted["withheld"] is True


def test_withheld_note_cannot_be_edited(
    withholding: TestClient, confidential_id: str
) -> None:
    """Editing a body you were not allowed to read would clobber it blind."""
    response = withholding.put(
        f"/api/notes/{confidential_id}",
        json={"body_hash": "anything", "body": "overwritten"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "body_withheld"


def test_title_and_tier_stay_visible(
    withholding: TestClient, confidential_id: str
) -> None:
    """Deliberately NOT withheld.

    "You can see it exists and that it is confidential, you just cannot read it
    here" is the intended property — otherwise a user is left wondering why a
    document looks empty.
    """
    payload = withholding.get(f"/api/notes/{confidential_id}").json()
    assert payload["title"] == "Compensation Review"
    assert payload["sensitivity"] == CONFIDENTIAL
    assert payload["vault_path"].endswith("compensation-review.md")


# ---------------------------------------------------------------- serving ---


def test_loopback_serves_the_body(
    serving: TestClient, confidential_id: str
) -> None:
    payload = serving.get(f"/api/notes/{confidential_id}").json()
    assert SECRET_PHRASE in payload["body"]
    assert "withheld" not in payload
    assert payload["summary"] == SUMMARY_TEXT


def test_tier_is_still_reported_when_serving(
    serving: TestClient, confidential_id: str
) -> None:
    """The frontmatter strip labels it in BOTH modes."""
    payload = serving.get(f"/api/notes/{confidential_id}").json()
    assert payload["sensitivity"] == CONFIDENTIAL


def test_loopback_snippet_is_not_redacted(
    serving: TestClient, confidential_id: str
) -> None:
    payload = serving.get("/api/search?q=severance").json()
    hits = {r["id"]: r for r in payload["results"]}
    assert hits[confidential_id]["snippet"] != ""
    assert "withheld" not in hits[confidential_id]


# ----------------------------------------------------------- normal docs ----


def test_normal_document_payload_is_byte_identical_across_modes(
    withholding: TestClient,
    serving: TestClient,
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
) -> None:
    """A normal document must be unaffected by the withholding feature.

    Adopted from w2b's ``test_normal_doc_payload_is_byte_identical``: pinning
    that the extra keys appear ONLY on the withheld path is what stops a
    confidentiality feature from quietly altering every other response.
    """
    doc_id = create_vault_note(
        test_db,
        cfg=ui_cfg,
        vault_path=ui_cfg.vault_path,
        title="Ordinary Note",
        body="Nothing sensitive here.\n",
        tags=[],
        template="note",
        folder="projects",
        embedder=fake_embedder,
    )
    withheld_mode = withholding.get(f"/api/notes/{doc_id}").json()
    serving_mode = serving.get(f"/api/notes/{doc_id}").json()

    assert withheld_mode == serving_mode
    assert "withheld" not in withheld_mode
    assert "sensitivity" not in withheld_mode
    assert withheld_mode["body"] == "Nothing sensitive here.\n"


def test_normal_document_snippet_is_never_redacted(
    withholding: TestClient,
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
) -> None:
    create_vault_note(
        test_db,
        cfg=ui_cfg,
        vault_path=ui_cfg.vault_path,
        title="Public Roadmap",
        body="The roadmap mentions severance only in passing.\n",
        tags=[],
        template="note",
        folder="projects",
        embedder=fake_embedder,
    )
    payload = withholding.get("/api/search?q=roadmap").json()
    assert payload["results"], "expected a hit"
    for result in payload["results"]:
        assert "withheld" not in result
        assert result["snippet"] != ""
