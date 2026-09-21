"""The API routes, end-to-end against a real Postgres and a real vault.

Uses the suite's ``test_db`` connection and ``fake_embedder`` fixtures, plus a
``tmp_path`` vault, so nothing here touches Ollama or the production database.

``UiContext`` carries the connection factory, so the app is handed a factory
that yields the *test* connection rather than opening its own — that is what
keeps a route's writes visible to the test's assertions inside one transaction.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault
from brain.vault.note_builder import create_vault_note

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
WRITE_HEADERS = {"Origin": ORIGIN}


@pytest.fixture
def ui_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return vault


@pytest.fixture
def ui_cfg(ui_vault: Path) -> Config:
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=ui_vault,
        embedder="none",
    )


@pytest.fixture
def seeded(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> dict[str, str]:
    """Two synthetic vault notes. No PII anywhere in this corpus."""
    ids = {}
    for title, folder, tags, body in [
        (
            "Q3 Planning Sync",
            "projects",
            ["planning"],
            "Three unresolved threads.\n\nSee [[Vendor Evaluation Notes]].\n",
        ),
        (
            "Vendor Evaluation Notes",
            "projects",
            ["vendors"],
            "Acme Holdings scored highest.\nContact person-b@example.invalid\n",
        ),
    ]:
        ids[title] = create_vault_note(
            test_db,
            cfg=ui_cfg,
            vault_path=ui_cfg.vault_path,
            title=title,
            body=body,
            tags=tags,
            template="note",
            folder=folder,
            embedder=fake_embedder,
        )
    return ids


@pytest.fixture
def client(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    from brain.search import hybrid_search

    context = UiContext(
        cfg=ui_cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


# --------------------------------------------------------------------- read --


def test_health_needs_no_database(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["read_only"] is False


def test_status_reports_counts(client: TestClient, seeded: dict[str, str]) -> None:
    payload = client.get("/api/status").json()
    assert payload["documents"] >= 2


def test_tree_nests_the_vault(client: TestClient, seeded: dict[str, str]) -> None:
    payload = client.get("/api/tree").json()
    assert payload["count"] >= 2
    names = [child["name"] for child in payload["children"]]
    assert "projects" in names


def test_facets_offer_all_known_sources(client: TestClient) -> None:
    payload = client.get("/api/facets").json()
    values = {bucket["value"] for bucket in payload["sources"]}
    # Unioned with the known kinds so a dropdown offers slack before the first
    # slack ingest, rather than the option appearing out of nowhere later.
    assert {"manual", "krisp", "gmail", "slack"} <= values


def test_get_note_by_prefix(client: TestClient, seeded: dict[str, str]) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    payload = client.get(f"/api/notes/{doc_id[:8]}").json()
    assert payload["title"] == "Q3 Planning Sync"
    assert payload["tier"] == "vault"
    assert payload["vault_path"] == "projects/q3-planning-sync.md"
    assert payload["body_hash"]
    assert "<p>" in payload["html"]
    assert payload["editable"] is True


def test_wikilink_in_body_resolves_to_the_other_note(
    client: TestClient, seeded: dict[str, str]
) -> None:
    payload = client.get(f"/api/notes/{seeded['Q3 Planning Sync'][:8]}").json()
    assert f'href="?id={seeded["Vendor Evaluation Notes"]}"' in payload["html"]


def test_headings_describe_the_html_that_was_actually_rendered(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """The TOC walks the SAME string the HTML was rendered from (defect S4).

    ``read_note`` renders ``strip_redundant_title_heading(body, title)``, so a
    TOC built from the *unstripped* body would open with an entry pointing at
    an ``<h1>`` the HTML does not contain — a link that scrolls nowhere. The
    two assertions below are the pair that catches it: the stripped title must
    be ABSENT from ``headings``, and every id that survives must be present in
    ``html``.
    """
    doc_id = seeded["Q3 Planning Sync"]
    current = client.get(f"/api/notes/{doc_id}").json()
    client.put(
        f"/api/notes/{doc_id}",
        json={
            "body_hash": current["body_hash"],
            "body": (
                "# Q3 Planning Sync\n\n"
                "## Scope\n\nWhat this covers.\n\n"
                "### Open Questions\n\nThree unresolved threads.\n"
            ),
        },
        headers=WRITE_HEADERS,
    )
    payload = client.get(f"/api/notes/{doc_id}").json()

    # The premise, asserted rather than assumed: the redundant title heading is
    # really in the stored body, so the strip has something to do. Without this
    # the test passes on a note that never had one and proves nothing.
    assert payload["body"].lstrip().startswith("# Q3 Planning Sync")

    headings = payload["headings"]
    assert [h["text"] for h in headings] == ["Scope", "Open Questions"], (
        "the TOC walked a different body than the renderer did"
    )
    assert [h["level"] for h in headings] == [2, 3]
    for heading in headings:
        assert f'id="{heading["id"]}"' in payload["html"], (
            f"heading id {heading['id']!r} anchors nothing in the rendered HTML"
        )


@pytest.fixture
def read_only_client(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    """The same app as ``client``, served with ``--read-only``.

    Same corpus, same connection — the ONLY difference is
    ``UiContext.read_only``, so any divergence a test observes is attributable
    to that flag and nothing else.
    """

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    from brain.search import hybrid_search

    context = UiContext(
        cfg=ui_cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
        read_only=True,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def test_read_only_server_reports_the_note_as_not_editable(
    read_only_client: TestClient, client: TestClient, seeded: dict[str, str]
) -> None:
    """``editable`` must answer "can this be edited HERE", not "in principle".

    The keyboard path is the reason this is not cosmetic: ``js/keys.js`` binds
    Cmd+E on ``state.note.editable`` ALONE, so an ``editable: true`` payload
    from a read-only server drops the user into an editor whose every save the
    middleware will refuse with a 403.

    MUTATION, RUN 2026-08-20 — the T8 row prescribed this one and nothing had
    ever recorded running it. *Restore the ungated expression* at
    ``src/brain/ui/notes_service.py:172``: drop ``and not ctx.read_only`` from
    ``"editable": (tier == "vault" or bool(vault_path)) and not ctx.read_only``.

        .venv/bin/python -m pytest tests/test_ui_routes.py --no-cov
        -> 1 failed, 40 passed (baseline 41 passed)

    THIS test alone, at ``assert payload["editable"] is False`` — observed
    ``assert True is False``. ``movable`` carries the same ``not ctx.read_only``
    clause and stayed green, which is what says the two flags are gated
    independently rather than by one shared expression: the mutation is
    contained to the field it names. Its sibling below reddens on the other
    mutation and not on this one, so the pair covers two properties rather than
    one property twice.
    """
    doc_id = seeded["Q3 Planning Sync"]
    # The premise, asserted rather than assumed: this note IS editable on a
    # writable server, so a False below is the flag talking and not the note.
    assert client.get(f"/api/notes/{doc_id}").json()["editable"] is True

    payload = read_only_client.get(f"/api/notes/{doc_id}").json()
    assert payload["editable"] is False
    assert payload["movable"] is False


def test_read_only_payload_omits_the_body_but_still_renders(
    read_only_client: TestClient, seeded: dict[str, str]
) -> None:
    """``body`` is the editor's raw source; a read-only server has no editor.

    It is also the single largest field on the wire — 570 KB against ~287 KB of
    ``html`` on the largest document in the corpus — so shipping it to a client
    that can never open an editor is pure waste. ``html`` must survive: reading
    is the entire point of a read-only server.

    MUTATION, RUN 2026-08-20 — the other half of T8's prescribed pair, likewise
    never previously recorded as run. *Re-add ``body``* at
    ``src/brain/ui/notes_service.py:211``: drop the ``if not ctx.read_only:``
    guard so ``payload["body"] = body`` executes unconditionally.

        .venv/bin/python -m pytest tests/test_ui_routes.py --no-cov
        -> 1 failed, 40 passed (baseline 41 passed)

    THIS test alone, at ``assert "body" not in payload`` — "the raw body is
    still on the wire for a client that cannot edit". The ``html`` assertions
    below it stayed green, which is the half that matters: the mutation puts a
    field back rather than breaking rendering, so a run that merely went red
    would not have told them apart.
    """
    payload = read_only_client.get(f"/api/notes/{seeded['Q3 Planning Sync']}").json()

    assert "body" not in payload, (
        "the raw body is still on the wire for a client that cannot edit"
    )
    assert payload["html"], "the rendered body went missing; reads are broken"
    assert "<p>" in payload["html"]
    assert payload["title"] == "Q3 Planning Sync"


def test_note_html_does_not_repeat_the_title_as_a_heading(
    client: TestClient, ui_cfg: Config
) -> None:
    """D2, end to end through the create route that produces the defect.

    ``POST /api/notes`` renders ``vault.templates.NOTE_TEMPLATE``, whose body is
    exactly ``# {{title}}`` — so before this fix every note the UI created
    rendered its title twice: once from js/inspector.js's ``<h1>`` and once from the
    body's own heading.
    """
    created = client.post(
        "/api/notes", json={"title": "Migration Retro"}, headers=WRITE_HEADERS
    ).json()
    fetched = client.get(f"/api/notes/{created['id']}").json()

    # The premise: the stored body really does open with the heading.
    assert fetched["body"].lstrip().startswith("# Migration Retro")
    # The fix: the rendered HTML does not.
    assert "<h1>" not in fetched["html"]
    assert "Migration Retro" not in fetched["html"]


def test_search_response_carries_a_date_per_result(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """D6 end to end, through the real route.

    Coverage either side of this was piecewise — ``test_search.py`` proves
    ``hybrid_search`` populates ``recency_ts``, ``test_ui_schemas.py`` proves the
    projection emits ``date`` — and a payload key can still go missing between
    two green tests. This asserts it survives the actual response body, which is
    what the ledger reads.
    """
    payload = client.get("/api/search?q=planning&fts_only=1").json()
    assert payload["results"], "no results — the assertion below would be vacuous"
    for result in payload["results"]:
        assert "date" in result, (
            f"/api/search returned a result with no date key: {sorted(result)}"
        )
        # Every seeded document is a real row, and documents.ingested_at is
        # NOT NULL, so a None here means the value was dropped in transit.
        assert result["date"] is not None, (
            "date is null for a document that has an ingested_at — the value "
            "was lost between hybrid_search and the response"
        )
        assert len(result["date"]) == len("YYYY-MM-DD")


def test_a_heading_that_is_not_the_title_still_renders(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    """The strip is narrow: only a leading heading matching the title goes."""
    doc_id = seeded["Q3 Planning Sync"]
    current = client.get(f"/api/notes/{doc_id}").json()
    client.put(
        f"/api/notes/{doc_id}",
        json={
            "body_hash": current["body_hash"],
            "body": "# Open questions\n\nThree unresolved threads.\n",
        },
        headers=WRITE_HEADERS,
    )
    fetched = client.get(f"/api/notes/{doc_id}").json()
    # Pinned WITH the anchor rather than loosened to `">Open questions</h1>"`.
    # The obvious loosening drops the `<h1` opening tag out of the assertion
    # entirely, which would stop pinning that this is still a level-1 heading —
    # the very thing "the strip is narrow" is about. Naming the id costs
    # nothing here (the slug is deterministic and separately covered in
    # tests/test_ui_render_toc.py) and buys back the tag plus the anchor T13's
    # table of contents will point at.
    assert '<h1 id="open-questions">Open questions</h1>' in fetched["html"]


def test_rendering_never_rewrites_the_file_on_disk(
    client: TestClient, ui_cfg: Config
) -> None:
    """D2 is a RENDER-time concern and must stay one.

    If the strip ever moved into the write path — or into ``body`` — a round
    trip through the editor would silently delete the user's own heading.
    """
    created = client.post(
        "/api/notes", json={"title": "Vendor Shortlist"}, headers=WRITE_HEADERS
    ).json()
    path = ui_cfg.vault_path / "vendor-shortlist.md"
    before = path.read_bytes()

    for _ in range(3):
        payload = client.get(f"/api/notes/{created['id']}").json()

    assert path.read_bytes() == before, "reading a note rewrote it on disk"
    assert b"# Vendor Shortlist" in before, "the premise died: no heading was written"
    # The served body is the file's body verbatim (``parse_frontmatter`` drops
    # only the blank line after the closing ``---``). The heading is still in it.
    on_disk_body = before.decode("utf-8").split("---\n")[-1]
    assert payload["body"].strip() == on_disk_body.strip(), (
        "the served body diverged from the file; only `html` may be transformed"
    )
    assert payload["body"].lstrip().startswith("# Vendor Shortlist")


def test_body_hash_covers_the_unstripped_body(
    client: TestClient, ui_cfg: Config
) -> None:
    """GAP: nothing pinned ``body_hash`` to the body the strip cannot touch.

    ``read_note`` computes ``body_hash(body)`` and renders
    ``strip_redundant_title_heading(body, title)`` — two different strings, on
    purpose. But BOTH ``seeded`` fixture bodies open with prose, never a
    ``# Title`` heading, so the strip is a no-op on every one of them and
    ``body_hash(strip_redundant_title_heading(body, title))`` passes the entire
    suite unchanged.

    The consequence is not a 409 loop — it is the opposite, and worse. Hashing
    the stripped body makes two bodies that differ *only* by the title heading
    hash IDENTICALLY, so a concurrent edit that adds or removes that heading is
    **not detected as a stale write** and is silently overwritten. ``body_hash``
    is the whole optimistic-concurrency primitive; it has to see the bytes it
    is protecting.

    This note is created through the real route, so it carries
    ``NOTE_TEMPLATE``'s own ``# Title`` — the case the strip exists for, and
    the case the fixtures never produced.
    """
    from brain.ui.notes_service import strip_redundant_title_heading
    from brain.vault.frontmatter import body_hash

    title = "Vendor Shortlist Hashing"
    created = client.post(
        "/api/notes", json={"title": title}, headers=WRITE_HEADERS
    ).json()
    payload = client.get(f"/api/notes/{created['id']}").json()
    body = payload["body"]

    # The premise, asserted rather than assumed: without a leading heading this
    # whole test is vacuous, which is exactly the state the suite was in.
    assert body.lstrip().startswith(f"# {title}"), (
        "the premise died: the template no longer writes a leading title "
        "heading, so this test can no longer distinguish anything"
    )
    stripped = strip_redundant_title_heading(body, title)
    assert stripped != body, "the strip did not fire; the test proves nothing"

    # The hash is over the body as SERVED and STORED...
    assert payload["body_hash"] == body_hash(body)
    # ...and is NOT the stripped body's hash. This is the assertion that fails
    # if the two are ever collapsed.
    assert payload["body_hash"] != body_hash(stripped), (
        "body_hash was computed over the STRIPPED body, so two bodies "
        "differing only by the title heading now hash identically and a "
        "concurrent edit that adds or removes it is not caught as a stale write"
    )

    # And the bytes it hashes are the file's bytes. `parse_frontmatter` drops
    # only the blank line after the closing `---`, hence the strip() on both
    # sides — the same normalisation the on-disk test above uses.
    on_disk_body = (ui_cfg.vault_path / "vendor-shortlist-hashing.md").read_bytes()
    assert body.strip() == on_disk_body.decode("utf-8").split("---\n")[-1].strip()


@pytest.mark.parametrize(
    ("prefix", "status", "code"),
    [
        ("abc", 400, "id_prefix_too_short"),
        ("zzzzzz", 400, "id_prefix_not_hex"),
        ("abcdef123456", 404, "note_not_found"),
    ],
)
def test_bad_prefixes_are_rejected(
    client: TestClient, prefix: str, status: int, code: str
) -> None:
    response = client.get(f"/api/notes/{prefix}")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


# -------------------------------------------------------------------- write --


def test_create_note_writes_file_and_row(
    client: TestClient, ui_cfg: Config
) -> None:
    response = client.post(
        "/api/notes",
        json={"title": "Retro Notes", "folder": "daily", "tags": ["Interview Prep"]},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201
    created = response.json()
    assert (ui_cfg.vault_path / "daily" / "retro-notes.md").is_file()

    fetched = client.get(f"/api/notes/{created['id'][:8]}").json()
    # Tags normalize at the write boundary: "Interview Prep" -> "interview-prep".
    assert "interview-prep" in fetched["tags"]


def test_folder_traversal_is_blocked_and_writes_nothing(
    client: TestClient, ui_cfg: Config, tmp_path: Path
) -> None:
    response = client.post(
        "/api/notes",
        json={"title": "Escape", "folder": "../../etc"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "folder_escapes_vault"
    assert not (tmp_path / "etc").exists()


def test_update_requires_body_hash(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.put(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"body": "rewritten"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_body_hash"


def test_stale_body_hash_is_a_conflict(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """The watcher and brain-mcp are live writers on the same file."""
    response = client.put(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"body_hash": "sha256:stale", "body": "rewritten"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_write"


def test_update_rewrites_the_file_and_keeps_the_uuid(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    current = client.get(f"/api/notes/{doc_id[:8]}").json()
    response = client.put(
        f"/api/notes/{doc_id[:8]}",
        json={"body_hash": current["body_hash"], "body": "# Rewritten\n\nNew body.\n"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["id"] == doc_id          # UUID preserved
    assert response.json()["rechunked"] is True

    on_disk = (ui_cfg.vault_path / "projects" / "q3-planning-sync.md").read_text()
    assert "New body." in on_disk
    assert "id:" in on_disk or "title:" in on_disk   # frontmatter survived


def test_draft_toggle_round_trips(
    client: TestClient, seeded: dict[str, str]
) -> None:
    doc_id = seeded["Q3 Planning Sync"][:8]
    assert client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": True}, headers=WRITE_HEADERS
    ).status_code == 200
    assert client.get(f"/api/notes/{doc_id}").json()["draft"] is True
    # Idempotent: setting the value it already has is a no-op, not an error.
    assert client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": True}, headers=WRITE_HEADERS
    ).status_code == 200
    client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": False}, headers=WRITE_HEADERS
    )
    assert client.get(f"/api/notes/{doc_id}").json()["draft"] is False


# --------------------------------------------------------------- destructive --


def test_delete_without_confirm_is_refused(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.request(
        "DELETE",
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"expected_title": "Q3 Planning Sync"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirm_required"


def test_delete_with_a_wrong_title_deletes_nothing(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """Server-side title check — the 2026-06-09 incident in test form."""
    doc_id = seeded["Q3 Planning Sync"][:8]
    response = client.request(
        "DELETE",
        f"/api/notes/{doc_id}",
        json={"confirm": True, "expected_title": "Something Else"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "title_mismatch"
    assert client.get(f"/api/notes/{doc_id}").status_code == 200   # still there


def test_delete_removes_row_and_mirror(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    mirror = ui_cfg.vault_path / "projects" / "q3-planning-sync.md"
    assert mirror.is_file()

    response = client.request(
        "DELETE",
        f"/api/notes/{doc_id[:8]}",
        json={"confirm": True, "expected_title": "Q3 Planning Sync"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["mirror_unlinked"] is True
    # Without the unlink, the next `brain vault sync` would resurrect it.
    assert not mirror.exists()
    assert client.get(f"/api/notes/{doc_id[:8]}").status_code == 404


def test_move_preserves_the_uuid_and_updates_vault_path(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    response = client.post(
        f"/api/notes/{doc_id[:8]}/move",
        json={"confirm": True, "new_folder": "archive"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["id"] == doc_id
    assert response.json()["vault_path"].startswith("archive/")
    assert (ui_cfg.vault_path / "archive" / "q3-planning-sync.md").is_file()


def test_move_outside_the_vault_is_refused(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}/move",
        json={"confirm": True, "new_folder": "../outside"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400


def test_a_refused_move_leaves_both_notes_exactly_where_they_were(
    client: TestClient,
    seeded: dict[str, str],
    test_db: psycopg.Connection,
    ui_cfg: Config,
) -> None:
    """A rename that collides must change NOTHING — on disk or in the row.

    Provoked for real rather than mocked: renaming one seeded note to the
    other's title produces a slug collision, which ``plan_rename`` rejects with
    a genuine ``RenameError`` (``vault/rename.py:206``). That exercises the
    handler with the exception type it actually has to survive in production.

    **Scope, stated precisely because the first version of this docstring
    overclaimed.** It said this exercised ``apply_rename``'s snapshot-and-restore
    contract. It does not: the collision is raised by ``plan_rename``
    (``vault/rename.py:205``), which runs BEFORE ``apply_rename`` is called at
    all — instrumenting it showed ``apply_rename entered 0 times``. Nothing is
    written, so there is nothing to restore, and the state assertions below pass
    because no mutation was attempted rather than because restoration worked.

    What this DOES pin is still worth having: a **plan-time refusal causes no
    collateral damage**. A 400 alone would not show that — the interesting
    failure is a move that reports refusal while having already renamed the
    file, moved it, or clobbered the note it collided with. So this asserts the
    state, not the status code.

    The restore contract is exercised separately, by
    ``test_a_failure_midway_through_a_rename_restores_every_touched_file``.
    """
    mover_id = seeded["Q3 Planning Sync"]
    target_id = seeded["Vendor Evaluation Notes"]
    mover_path = ui_cfg.vault_path / "projects" / "q3-planning-sync.md"
    target_path = ui_cfg.vault_path / "projects" / "vendor-evaluation-notes.md"
    target_before = target_path.read_text(encoding="utf-8")

    response = client.post(
        f"/api/notes/{mover_id[:8]}/move",
        json={"confirm": True, "new_title": "Vendor Evaluation Notes"},
        headers=WRITE_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "move_failed"

    assert mover_path.is_file(), "the refused move renamed the file anyway"
    assert target_path.is_file(), "the refused move removed the collision target"
    assert target_path.read_text(encoding="utf-8") == target_before, (
        "the refused move overwrote the note it collided with"
    )
    rows = dict(
        test_db.execute(
            "SELECT id::text, vault_path FROM documents WHERE id = ANY(%s)",
            ([mover_id, target_id],),
        ).fetchall()
    )
    assert rows[mover_id] == "projects/q3-planning-sync.md"
    assert rows[target_id] == "projects/vendor-evaluation-notes.md"


def test_a_failure_midway_through_a_rename_restores_every_touched_file(
    client: TestClient,
    seeded: dict[str, str],
    test_db: psycopg.Connection,
    ui_cfg: Config,
    mocker: Any,
) -> None:
    """``apply_rename``'s snapshot-and-restore contract, actually exercised.

    The test above cannot reach this: its collision is refused at PLAN time, so
    ``apply_rename`` never runs and no file is ever written. To exercise restore
    the rename has to fail **after** it has begun writing.

    The seeded corpus makes that natural — "Q3 Planning Sync" contains
    ``[[Vendor Evaluation Notes]]`` — so renaming the second note sends
    ``apply_rename`` through step 1, rewriting that reference in a DIFFERENT
    file, before step 2 touches the source. Failing at
    ``_rewrite_source_frontmatter`` therefore fails with a real write already on
    disk, which is exactly the state the snapshot exists to undo.

    The assertion is byte-exactness, not "the file still mentions the old
    title": the contract says restore is byte-exact (it snapshots ``read_bytes``
    precisely so an unusual encoding survives), and a restore that rewrote the
    file with equivalent content would satisfy anything weaker.
    """
    mover_id = seeded["Vendor Evaluation Notes"]
    referrer = ui_cfg.vault_path / "projects" / "q3-planning-sync.md"
    source = ui_cfg.vault_path / "projects" / "vendor-evaluation-notes.md"
    referrer_before = referrer.read_bytes()
    source_before = source.read_bytes()

    # Fails in step 2, after step 1 has already rewritten the referrer.
    mocker.patch(
        "brain.vault.rename._rewrite_source_frontmatter",
        side_effect=OSError("disk gave out midway through the rename"),
    )

    response = client.post(
        f"/api/notes/{mover_id[:8]}/move",
        json={"confirm": True, "new_title": "Vendor Evaluation Notes Renamed"},
        headers=WRITE_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "move_failed"

    assert referrer.read_bytes() == referrer_before, (
        "the reference rewrite in another file was NOT rolled back: "
        "apply_rename's snapshot-and-restore did not restore it byte-exactly"
    )
    assert source.read_bytes() == source_before, "the source file was left modified"
    assert not (
        ui_cfg.vault_path / "projects" / "vendor-evaluation-notes-renamed.md"
    ).exists(), "the failed rename left the new path behind"

    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (mover_id,)
    ).fetchone()
    assert row is not None and row[0] == "projects/vendor-evaluation-notes.md"


def test_a_patch_that_changes_nothing_is_a_no_op(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """An empty patch short-circuits before any write path is entered.

    The UI sends the whole patch shape on every save, so "the user pressed save
    without editing" arrives as a well-formed request with every field unset.
    Falling through would re-chunk and re-embed a body that did not change —
    and, for the ingested tier, rewrite the vault mirror for nothing.
    """
    doc_id = seeded["Q3 Planning Sync"]
    current = client.get(f"/api/notes/{doc_id[:8]}").json()

    response = client.put(
        f"/api/notes/{doc_id[:8]}",
        json={"body_hash": current["body_hash"]},
        headers=WRITE_HEADERS,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fields_changed"] == []
    assert payload["rechunked"] is False
    # The hash must be the one the client already holds, or the next save from
    # this editor would 409 against a note nobody edited.
    assert payload["body_hash"] == current["body_hash"]


# ------------------------------------------- ingested tier: edit atomicity --


@pytest.fixture
def ingested(
    test_db: psycopg.Connection, fake_embedder: Any
) -> dict[str, str]:
    """One INGESTED-tier document — the tier ``_update_ingested_note`` serves.

    The ``seeded`` fixture above builds VAULT-tier notes, which take the other
    branch of :func:`brain.ui.notes_service.update_note` entirely. Every test
    below needs the ingested branch, so it needs its own corpus.

    Synthetic throughout; no PII.
    """
    from brain.ingest import ingest_document
    from brain.ingest.stdin import make_doc

    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=make_doc(
            content="Original body of the ingested transcript.",
            title="Original Title",
            content_type="transcript",
        ),
        source_kind="krisp",
        source_external_id="meeting-atomicity-1",
        tags=["original-tag"],
        enrich=False,
    )
    return {"id": result.document_id}


def _stored(conn: psycopg.Connection, document_id: str) -> tuple[str, str, list[str]]:
    """Read title, content and tags straight from the row, bypassing the API."""
    row = conn.execute(
        "SELECT title, content, tags FROM documents WHERE id=%s", (document_id,)
    ).fetchone()
    assert row is not None, f"document {document_id} vanished"
    return row[0], row[1], list(row[2] or [])


def _vault_files(vault: Path) -> list[Path]:
    """Every markdown file currently in the vault, mirrors included."""
    return sorted(p for p in vault.rglob("*.md") if p.is_file())


def _mirror_files(vault: Path) -> list[Path]:
    """Only the ingested-tier mirrors under ``_ingested/``.

    Scoped deliberately: ``init_vault`` scaffolds READMEs and templates, so a
    count over the whole vault is six before a single document exists. An
    assertion phrased against that count fails for the wrong reason — it
    reports "the mirror is wrong" when what happened is "the vault has a
    README" — which is the red-for-the-wrong-reason trap this file keeps
    catching.
    """
    return sorted(
        p
        for p in (vault / "_ingested").rglob("*.md")
        if p.is_file() and p.name != "README.md"
    )


def test_a_failing_tag_write_leaves_the_body_and_title_unchanged(
    client: TestClient,
    ingested: dict[str, str],
    test_db: psycopg.Connection,
    ui_cfg: Config,
    mocker: Any,
) -> None:
    """An edit is one transaction, or it is a corruption.

    The UI connection runs with ``autocommit = True`` (``server.py``), so
    ``update_document``'s own ``with conn.transaction()`` used to commit title
    and body the instant it returned, and ``apply_tags`` ran as a SEPARATE
    autocommitted statement. A failure in the second half therefore left the
    first half committed: the user saw a 500 saying the save failed, and the
    note had silently been half-rewritten underneath them.

    Asserting the 500 alone would NOT catch this — the endpoint raises either
    way. What distinguishes the fixed code from the broken code is only visible
    in the row, so that is what this reads: title and content must still hold
    their pre-edit values. This is the assertion that fails without the
    surrounding transaction.
    """
    doc_id = ingested["id"]
    before_title, before_body, _ = _stored(test_db, doc_id)
    current = client.get(f"/api/notes/{doc_id[:8]}").json()

    # Fail the SECOND half of the edit. Patching `brain.ingest.apply_tags`
    # reaches the call site because notes_service imports it inside the
    # function body, so the name is resolved at call time.
    mocker.patch(
        "brain.ingest.apply_tags", side_effect=RuntimeError("tag write exploded")
    )

    with pytest.raises(RuntimeError, match="tag write exploded"):
        client.put(
            f"/api/notes/{doc_id[:8]}",
            json={
                "body_hash": current["body_hash"],
                "title": "Rewritten Title",
                "body": "Rewritten body that must not survive.",
                "tags": ["added-tag"],
            },
            headers=WRITE_HEADERS,
        )

    after_title, after_body, after_tags = _stored(test_db, doc_id)
    assert after_title == before_title, (
        "the title was committed even though the edit failed — body/title and "
        "tags are not sharing a transaction"
    )
    assert after_body == before_body, (
        "the body was committed even though the edit failed — body/title and "
        "tags are not sharing a transaction"
    )
    assert after_tags == ["original-tag"], "tags changed despite the failed write"

    # THE DISK HALF. Rolling the row back is only half of "the edit did not
    # happen": `update_document` writes the vault mirror OUTSIDE its
    # transaction, deliberately, so that a filesystem error cannot roll back a
    # committed DB write. Under the surrounding transaction that ordering
    # inverts — the file is written while the DB work is still uncommitted, so
    # a rollback cannot reach it and the rejected title/body survive on disk.
    #
    # `_ingested/` is what Quartz publishes and what `brain vault sync` reads,
    # and the mirror filename derives from the title, so an orphan here is
    # permanent: the DB never recorded the file, so nothing will ever clean it
    # up. Asserting the row alone is an oracle that cannot see the half of the
    # edit that survived — the same blind spot that hid the bug this test
    # was written to catch.
    # Asserted as "no mirror file exists AT ALL" rather than "no file contains
    # the sentinel". The stronger form is the right one because the orphan is
    # unreclaimable: the rolled-back row keeps `vault_path = NULL`, so the
    # database has no record that the file exists and NOTHING can ever collect
    # it — not `vault sync`, not `vault export`, not a later successful edit,
    # which derives a different filename from a different title. A repair that
    # merely corrected the file's CONTENT while still writing it early would
    # satisfy a sentinel scan and still leave a growing pile of garbage in the
    # directory Quartz publishes.
    #
    # The fixture ingests without a vault_root, so no mirror exists before this
    # edit; after a fully rolled-back edit there must still be none.
    orphans = _mirror_files(ui_cfg.vault_path)
    assert orphans == [], (
        "the rejected edit left a mirror on disk after the DB rolled it back: "
        f"{[str(p) for p in orphans]}. The vault mirror was written before the "
        "outer transaction committed, so the rollback could not reach it — and "
        "the row's vault_path is NULL, so nothing will ever reclaim the file."
    )
    orphan_vault_path = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert orphan_vault_path is not None and orphan_vault_path[0] is None, (
        "the row records a vault_path for an edit that was rolled back"
    )


def test_a_successful_edit_commits_the_body_and_the_tags_together(
    client: TestClient,
    ingested: dict[str, str],
    test_db: psycopg.Connection,
    ui_cfg: Config,
) -> None:
    """The other half of the pair: wrapping the edit must not stop it committing.

    A transaction that rolls back correctly but never commits would satisfy the
    failure-mode test above on its own. This pins the success path so the fix
    cannot be "never write anything".
    """
    doc_id = ingested["id"]
    current = client.get(f"/api/notes/{doc_id[:8]}").json()

    response = client.put(
        f"/api/notes/{doc_id[:8]}",
        json={
            "body_hash": current["body_hash"],
            "title": "Committed Title",
            "body": "Committed body.",
            "tags": ["added-tag"],
        },
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert "tags" in response.json()["fields_changed"]

    after_title, after_body, after_tags = _stored(test_db, doc_id)
    assert after_title == "Committed Title"
    assert "Committed body." in after_body
    # apply_tags UNIONS rather than replaces — the original tag must survive.
    assert set(after_tags) == {"original-tag", "added-tag"}

    # AND THE MIRROR MUST AGREE. "Committed together" is a claim about the
    # whole edit, not about the row: `_ingested/` is what Quartz publishes and
    # what `brain vault sync` reads, so a mirror that disagrees with the row is
    # a user-visible split-brain even though every DB assertion above passes.
    #
    # This is the half the row-only oracle could never see. The mirror used to
    # be written by `update_document` BEFORE `apply_tags` ran, so it captured
    # the tags mid-edit and recorded only `original-tag` while the database
    # committed both — the DB assertions above passed against a file that
    # contradicted them.
    mirrors = _mirror_files(ui_cfg.vault_path)
    assert len(mirrors) == 1, (
        f"expected exactly one ingested mirror, found {[str(p) for p in mirrors]}"
    )
    mirror_text = mirrors[0].read_text(encoding="utf-8")
    assert "Committed body." in mirror_text
    assert "original-tag" in mirror_text and "added-tag" in mirror_text, (
        "the vault mirror does not carry both tags, but the database does. The "
        "mirror was written before apply_tags ran, so disk and row disagree "
        f"about the same committed edit. Mirror was:\n{mirror_text}"
    )


def test_a_tags_only_edit_still_refreshes_the_mirror(
    client: TestClient,
    ingested: dict[str, str],
    test_db: psycopg.Connection,
    ui_cfg: Config,
) -> None:
    """The one edit whose mirror rewrite nothing else would trigger.

    ``update_document`` returns BEFORE ``apply_tags`` runs, so for an edit that
    changes only tags its ``fields_changed`` is empty. Deciding the mirror
    rewrite from that list answers "nothing changed", skips the write, and
    leaves the old tag set on disk permanently — while the row carries the new
    one. The caller therefore decides from its OWN change list, which includes
    the tag write.

    Every other edit hides this: a title or body change makes the mirror stale
    by itself, so the rewrite happens regardless and the mirror — regenerated
    from the committed row — picks the tags up for free. Only the tags-only
    edit can tell the two apart, which is why it needs its own test rather
    than being folded into the success-path case above.
    """
    doc_id = ingested["id"]
    current = client.get(f"/api/notes/{doc_id[:8]}").json()

    response = client.put(
        f"/api/notes/{doc_id[:8]}",
        json={"body_hash": current["body_hash"], "tags": ["late-tag"]},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["fields_changed"] == ["tags"], (
        "this test is only meaningful if tags are the ONLY thing that changed"
    )

    mirrors = _mirror_files(ui_cfg.vault_path)
    assert len(mirrors) == 1, (
        f"expected exactly one ingested mirror, found {[str(p) for p in mirrors]}"
    )
    mirror_text = mirrors[0].read_text(encoding="utf-8")
    assert "late-tag" in mirror_text, (
        "a tags-only edit did not refresh the vault mirror; the row has the new "
        f"tag and disk does not. Mirror was:\n{mirror_text}"
    )


# ------------------------------------------------ phase 4: move_note errors --


def test_a_filesystem_failure_during_a_move_is_still_a_400(
    client: TestClient, seeded: dict[str, str], mocker: Any
) -> None:
    """The narrowing must not turn disk-full into a 500.

    `move_note`'s blanket `except Exception` was narrowed to
    `(RenameError, OSError)`. `RenameError` alone would have been a REGRESSION,
    and the reason is measured rather than argued: `apply_rename`'s contract is
    snapshot, restore, then **re-raise the original error**, so a filesystem
    failure leaves as itself. A real read-only file produces `PermissionError`,
    for which `isinstance(exc, RenameError)` is False.

    Environmental failures are the user's problem and not bugs, so they keep
    their 400.
    """
    mocker.patch(
        "brain.ui.notes_service.apply_rename",
        side_effect=OSError(28, "No space left on device"),
    )

    response = client.post(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}/move",
        json={"confirm": True, "new_title": "Renamed By A Full Disk"},
        headers=WRITE_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "move_failed"


def test_a_programming_error_during_a_move_is_NOT_absorbed(
    client: TestClient, seeded: dict[str, str], mocker: Any
) -> None:
    """The point of the narrowing: a bug must be loud.

    The blanket `except Exception:  # noqa: BLE001` mapped `TypeError` from
    signature drift — and any genuine defect in the rename path — to a tidy
    user-facing 400 `move_failed`, forever, with the `noqa` having already
    silenced the linter. Same precedent as
    `test_warm_up_does_not_swallow_a_real_bug`: only shaped failures are
    absorbed.
    """
    mocker.patch(
        "brain.ui.notes_service.apply_rename",
        side_effect=TypeError("apply_rename() got an unexpected keyword argument"),
    )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        client.post(
            f"/api/notes/{seeded['Q3 Planning Sync'][:8]}/move",
            json={"confirm": True, "new_title": "Renamed By A Bug"},
            headers=WRITE_HEADERS,
        )


def test_a_rename_onto_a_symlinked_destination_is_refused(
    client: TestClient,
    seeded: dict[str, str],
    ui_cfg: Config,
    tmp_path: Path,
) -> None:
    """`plan_rename:199` — the real control, with an honest account of its scope.

    `plan_rename` builds the destination as ``old_relative.with_name(slug)`` and
    guards it with ``assert_within_vault``, **which resolves symlinks**. So a
    perfectly legal stored `vault_path` yields a destination that resolves out
    of bounds whenever a symlink already sits at the new name — no malformed
    row required.

    **WHAT THIS DOES NOT PROVE, measured rather than assumed.** The obvious
    criterion — "with the guard removed, a file appears outside the vault" —
    **cannot fail**, and not for the reason first suspected. It is not that the
    restore deletes the evidence. **The write never escapes at all:**
    `apply_rename` moves with ``Path.replace``, and `os.replace` acts on the
    SYMLINK, clobbering it, so the file lands *inside* the vault:

        os.replace(src, dangling_symlink)
          -> dest is a real file INSIDE the vault
          -> the outside target is never created

    So this test is **weaker than "prevents an escape", and says so.** What it
    pins is that a destination resolving out of bounds is REFUSED, and that the
    symlink is left intact — an observable file-state change, not a status code.

    **The genuinely untested escape** is `rename.py:338`'s EXDEV fallback, which
    writes with ``write_text``. That primitive DOES follow a symlink, making it
    the one place an escape would really occur — and `:199` is its only guard.
    Nobody has shown that path reachable; it is logged, not covered here.

    MUTATION THAT MUST GO RED: remove `assert_within_vault` at
    `plan_rename:199`. The symlink is then destroyed and the note moved onto
    it — which is why the file-state assertions come FIRST. Ordered after the
    status code, the mutation would go red on ``200 != 400`` and this test
    would never have demonstrated anything about the filesystem.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    dangling = outside / "escaped-note.md"          # deliberately NOT created
    link = ui_cfg.vault_path / "projects" / "escaped-note.md"
    link.symlink_to(dangling)

    response = client.post(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}/move",
        json={"confirm": True, "new_title": "Escaped Note"},
        headers=WRITE_HEADERS,
    )

    # FILE STATE FIRST — this is the discrimination; the status code is the
    # exception's spelling.
    assert link.is_symlink(), (
        "the symlink was clobbered — `plan_rename:199` did not refuse a "
        "destination that resolves outside the vault, and the note was moved "
        "onto the link"
    )
    assert link.readlink() == dangling, "the symlink now points somewhere else"
    assert not dangling.exists(), "a file was created at the symlink's target"

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "folder_escapes_vault"


def test_the_global_handler_reports_a_traversal_as_400(
    client: TestClient,
    test_db: psycopg.Connection,
    seeded: dict[str, str],
) -> None:
    """The GLOBAL handler's reporting behaviour — not the deleted arm's.

    HONEST SCOPE, because this test's previous docstring was false. It claimed
    to prove that `move_note`'s `except VaultPathEscape` arm was load-bearing,
    and that deleting it would let a poisoned row write outside the vault.
    **Both claims were wrong.** Measured three ways: coverage shows
    `move_note`'s body after `read_note` is never entered by this test — the
    `try` is not reached at all; `read_note` validates the stored path FIRST
    and raises there; and deleting the arm left the whole suite green.

    What this test actually covers, and the reason it was kept when the arm was
    deleted: `app.py`'s global `VaultPathEscape` handler turning a traversal
    into a **400 with a generic message**. Once the arm is gone that handler is
    the ONLY thing producing that response, and nothing else asserts it.

    The message assertion is not decoration. The deleted arm raised
    `UiBadRequest(str(exc))`, which leaked the ABSOLUTE vault path; the global
    handler is generic precisely to prevent that disclosure.
    """
    doc_id = seeded["Q3 Planning Sync"]
    test_db.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s",
        ("../../evil/poisoned.md", doc_id),
    )

    response = client.post(
        f"/api/notes/{doc_id[:8]}/move",
        json={"confirm": True, "new_title": "Escape Attempt"},
        headers=WRITE_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "folder_escapes_vault"
    assert "/" not in response.json()["error"]["message"].replace("vault", ""), (
        "the error message discloses a filesystem path — the generic handler "
        "exists to prevent exactly that"
    )


def test_a_symlinked_DIRECTORY_destination_is_refused_at_library_level(
    test_db: psycopg.Connection,
    ui_cfg: Config,
    fake_embedder: Any,
    seeded: dict[str, str],
    tmp_path: Path,
    mocker: Any,
) -> None:
    """`plan_rename:199` is the SOLE guard here, and the bytes really do escape.

    Called at LIBRARY level — `plan_rename` / `apply_rename` directly, the way
    MCP calls them — so `move_note`'s own pre-check at `:558` is out of the
    picture and `:199` is the only thing under test.

    **A symlinked DIRECTORY, not a symlinked filename, and the distinction is
    load-bearing.** `rename(2)` exempts only the FINAL path component from
    symlink resolution, so a symlinked *parent* is followed and the file lands
    outside the vault for real.

    A symlinked LEAF was measured NOT to escape: `os.replace` acts on the link
    itself and clobbers it, leaving the file inside the vault. **Do not
    "simplify" this fixture to a symlinked filename** — that substitution looks
    equivalent, and it silently makes this test vacuous. It is the exact
    mistake an earlier version of this suite made.

    **WHY THE ESCAPE IS IN THE DESTINATION AND NOT THE SOURCE.** An earlier
    version poisoned `documents.vault_path` so the *source* sat behind the
    symlink. That fixture cannot demonstrate anything, MEASURED: with `:199`
    removed, `apply_rename` dies in the SNAPSHOT — `_backup_path_for`
    (`rename.py:277` -> `:659`) does `old_path.resolve().relative_to(vault)`,
    and a source outside the vault raises `ValueError` there, **before a single
    write**. So the spy below would record nothing and this test would stay
    GREEN under the mutation it exists to catch. (The docstring that fixture
    carried blamed `_update_vault_path` at `rename.py:408`; the code never
    reaches it.) Routing the escape through `new_folder` instead leaves the
    source a perfectly ordinary in-vault note, the snapshot succeeds, and the
    operation runs all the way to the write.

    **HOW THE ESCAPE IS OBSERVED: during, not after.** Two independent reasons
    a post-hoc `not (outside / ...).exists()` cannot work here, and the first
    is the one that actually bites:

    1. **The exception preempts the assertion.** With the guard gone,
       `_update_vault_path`'s `relative_to` raises `ValueError` (`:408`) and
       the check below never executes — the test goes red on an exception TYPE,
       certifying nothing about the filesystem.
    2. **The evidence is deleted.** `apply_rename` catches, restores, and
       unlinks the escaped file (`rename.py:372`) before returning.

    Recording the destination AT CALL TIME defeats both, because the
    observation happens before anything downstream can raise or clean up.

    **THE SPY COVERS `write_text` AS WELL AS `replace`, and that is not
    belt-and-braces.** `rename.py:338` is the EXDEV fallback, and `write_text`
    FOLLOWS symlinks where `replace` does not — it is the one primitive in this
    module that would escape through a symlinked *leaf*. A spy on `replace`
    alone would miss the case most worth catching. The failure message prints
    the FULL write sequence for the same reason: WHICH primitive escaped says
    whether `:325` did it directly or the `:338` fallback followed a link, and
    those are different defects with different fixes.

    MUTATION THAT MUST GO RED: delete `assert_within_vault(new_abs, vault_path)`
    at `plan_rename:199`. This then fails on `escaped`, naming the primitive and
    the out-of-vault path it was handed — **not** on an exception type and
    **not** on a status code. The escape assertion is therefore FIRST; ordered
    after the exception check it would go red on `ValueError is not
    VaultPathEscape` and would have demonstrated nothing.
    """
    from brain.errors import VaultPathEscape
    from brain.vault.rename import apply_rename, plan_rename

    outside = tmp_path / "outside"
    outside.mkdir()
    # A symlinked PARENT inside the vault. The destination's final component is
    # an ordinary name; the directory above it is the link.
    (ui_cfg.vault_path / "escape-hatch").symlink_to(outside, target_is_directory=True)

    vault = ui_cfg.vault_path.resolve()
    #: (primitive, destination resolved AT CALL TIME) for every write the
    #: operation performs. Resolved eagerly — the symlink and the file are both
    #: gone by the time the call returns.
    recorded: list[tuple[str, Path]] = []
    real_replace = Path.replace
    real_write_text = Path.write_text

    def spy_replace(self: Path, target: Any) -> Any:
        recorded.append(("Path.replace", Path(target).resolve()))
        return real_replace(self, target)

    def spy_write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> Any:
        recorded.append(("Path.write_text", self.resolve()))
        return real_write_text(self, data, *args, **kwargs)

    mocker.patch.object(Path, "replace", spy_replace)
    mocker.patch.object(Path, "write_text", spy_write_text)

    # POSITIVE CONTROL, both directions, before the operation. Unmutated, the
    # guard refuses at PLAN time and no write ever happens — so `recorded` is
    # legitimately empty and "no escape was recorded" is indistinguishable from
    # "the spy never installed". These two probes are what separate them, and
    # the out-of-vault one goes through the very symlink under test, so the
    # classifier is exercised on the exact geometry it must judge.
    (ui_cfg.vault_path / "spy-probe.md").write_text("probe", encoding="utf-8")
    (ui_cfg.vault_path / "escape-hatch" / "spy-probe.md").write_text(
        "probe", encoding="utf-8"
    )
    assert [p for p, dest in recorded if dest.is_relative_to(vault)], (
        "the spy recorded no in-vault write — it is not installed, and every "
        "assertion below is vacuous"
    )
    assert [p for p, dest in recorded if not dest.is_relative_to(vault)], (
        "the spy recorded the symlinked-parent write as INSIDE the vault — it "
        "resolves lazily or not at all, and it cannot detect the escape"
    )
    (ui_cfg.vault_path / "spy-probe.md").unlink()
    (ui_cfg.vault_path / "escape-hatch" / "spy-probe.md").unlink()
    recorded.clear()

    doc_id = seeded["Q3 Planning Sync"]
    raised: BaseException | None = None
    try:
        op = plan_rename(
            test_db,
            vault_path=ui_cfg.vault_path,
            document_id=doc_id,
            new_title="Escaped Via Directory",
            new_folder="escape-hatch",
        )
        apply_rename(
            test_db, embedder=fake_embedder, vault_path=ui_cfg.vault_path, op=op
        )
    except Exception as exc:  # noqa: BLE001 — asserted on below, never swallowed
        raised = exc

    # THE DISCRIMINATION, FIRST: bytes were handed to a path outside the vault.
    escaped = [f"{p} -> {dest}" for p, dest in recorded if not dest.is_relative_to(vault)]
    # The FULL sequence goes in the message alongside it, not just the escaping
    # entries. On a failure the endpoint alone does not say HOW the operation
    # got there, and the two routes need different fixes: `Path.replace`
    # escaping directly is `rename.py:325`, whereas a `Path.write_text` escape
    # means the EXDEV fallback at `:338` took over and followed the link — the
    # one path that also escapes through a symlinked LEAF, and the one nothing
    # currently exercises. Reading that off the failure is the only in-test
    # signal we have about it.
    trace = "\n".join(
        f"    {'ESCAPED' if not dest.is_relative_to(vault) else '     ok'}  {p} -> {dest}"
        for p, dest in recorded
    ) or "    (no writes recorded)"
    assert not escaped, (
        "a write escaped the vault through a symlinked parent directory: "
        f"{escaped} — `assert_within_vault` at plan_rename:199 is the ONLY "
        "guard on this path for library callers (MCP, brain ui).\n"
        f"  full write sequence:\n{trace}"
    )
    # Only then, the spelling of the refusal.
    assert isinstance(raised, VaultPathEscape), (
        f"expected VaultPathEscape, got {raised!r}"
    )


# ------------------------------------------ the email-thread marker, wired --
#
# `render_markdown` only emits thread sections for a document whose
# content_type is the one the gmail assembler stamps, and `read_note` is the
# only production caller that supplies it. The pure tests in
# tests/test_ui_render_email_thread.py hold the RULE; these two hold the WIRE.
# Without them, deleting `content_type=row.content_type` from that call leaves
# every pure test green and un-renders every email thread in the corpus.

#: The assembler's markup, minus its per-message content. Kept to the two
#: marker lines and a body so the assertions are about recognition, not about
#: the fixture. No PII: `@example.test` is RFC 6761 reserved.
_THREAD_BODY = (
    "<details>\n"
    "<summary>2026-03-07 08:00 — Dana Vendor &lt;dana@example.test&gt;</summary>\n"
    "\n"
    "The older message.\n"
    "\n"
    "</details>\n"
    "\n"
    "## 2026-03-09 12:00 — Sam Buyer &lt;sam@example.test&gt;\n"
    "\n"
    "The latest reply.\n"
)


def _seed_typed_doc(
    conn: psycopg.Connection, *, content_type: str, content_hash: str
) -> str:
    """One ingested document whose ONLY variable is its ``content_type``."""
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Widget Order', %s, %s, 'ingested', %s) RETURNING id::text",
        (_THREAD_BODY, content_type, content_hash),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_an_email_thread_document_renders_thread_sections_through_the_route(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    """DIRECTION 1 at the wire: the marker reaches the renderer."""
    doc_id = _seed_typed_doc(
        test_db, content_type="email_thread", content_hash="hash-thread-wired"
    )

    payload = client.get(f"/api/notes/{doc_id}").json()

    assert 'class="thread-message"' in payload["html"], (
        "an email_thread document came back without thread sections — "
        "read_note is no longer passing the document's content_type to "
        "render_markdown, so every ingested thread renders as escaped tag text"
    )


def test_a_note_with_the_same_body_gets_no_thread_sections_through_the_route(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    """DIRECTION 2 at the wire: a non-thread document is left alone.

    Byte-identical body to the test above. Only ``content_type`` differs, so a
    wiring that hardcoded the marker — or dropped the argument and defaulted
    the rule back ON — passes direction 1 and fails here.
    """
    doc_id = _seed_typed_doc(
        test_db, content_type="note", content_hash="hash-note-not-a-thread"
    )

    payload = client.get(f"/api/notes/{doc_id}").json()

    assert "thread-message" not in payload["html"], (
        "a plain note was served with the email-thread class, so the UI mounts "
        "an email-only reply filter on it"
    )
    # Absence needs presence: declining must leave the document readable.
    assert "The older message." in payload["html"]
