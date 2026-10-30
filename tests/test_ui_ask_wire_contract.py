"""The Agent tab's wire contract, end to end on the real server (RM-18b test plan).

The browser suite (``tests/test_ui_browser_agent.py``) is hermetic: it stubs the
job routes with ``tests/ui_ask_harness.py``. This is the route test the ticket
pairs with it. A fake ``ChatJson`` is injected through ``UiContext.ask_runtime``,
the job runs on ``run_next()`` against the 5434 database, and the REAL job payload
is checked for the three things the front end depends on:

1. its keys are exactly the stub's (:data:`SNAPSHOT_KEYS`, :data:`RESULT_KEYS`,
   :data:`CITATION_KEYS`), so the hermetic stub cannot drift from the wire;
2. a hostile answer comes back VERBATIM (HTML intact, no escaping), so the browser
   is the layer that must keep it text, and an out-of-range ``[99]`` is already
   dropped (``ask._sanitize_answer``), which the client does not rely on;
3. a real cited document id passes the UUID pattern ``ask_view.js`` checks before
   it builds a link, read out of the shipped JS, so the two cannot disagree;
4. ``GET /api/jobs``, which a queued ask reads to decide whether an ingest is AHEAD
   of it, gives each job the keys the stub's :func:`listed_job` gives, with a
   ``created_at`` in the same form as the ask's own, so the FIFO comparison the tab
   makes on the stub is the comparison it makes on the wire.

No Ollama; all data synthetic.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.ingest import ExtractedDoc, ingest_document
from brain.ui.app import create_app, static_dir
from brain.ui.ask_service import AskRuntime
from brain.ui.context import UiContext
from tests.conftest import FakeEmbedder
from tests.ui_ask_harness import (
    AFTER,
    BEFORE,
    CITATION_KEYS,
    LISTED_KEYS,
    RESULT_KEYS,
    SNAPSHOT_KEYS,
    listed_job,
)
from tests.ui_ingest_support import ORIGIN, WRITE_HEADERS, make_context, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_HOSTILE = (
    'It ships <img src=x onerror="alert(1)"> <script>alert(2)</script> '
    "javascript:alert(3) on Fridays [1], not [99]."
)


class _SchemaChat:
    """Answers each ask step by the schema it asks for, so the loop's shape is free."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        if "sub_queries" in schema:
            self.steps.append("plan")
            return {"sub_queries": ["quillbeam"]}
        if "sufficient" in schema:
            self.steps.append("reflect")
            return {"sufficient": True, "follow_up_queries": []}
        self.steps.append("synthesize")
        return {"answer": _HOSTILE}


def _ctx(conn: psycopg.Connection[Any], tmp_path: Path, chat: Any) -> UiContext:
    ctx = make_context(conn, make_vault(tmp_path), embedder=FakeEmbedder(),
                       ask_runtime=AskRuntime(chat=chat))
    return dataclasses.replace(ctx, serve_confidential_bodies=False)


def _seed(conn: psycopg.Connection[Any]) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title="Quillbeam ledger", content="Quillbeam ships on Fridays. quillbeam body",
            content_type="note", source_path=None, metadata={},
        ),
        source_kind="manual",
        sensitivity="normal",
    )
    assert result.document_id is not None
    return result.document_id


def _js_uuid_pattern() -> re.Pattern[str]:
    """``UUID_RE`` exactly as ``ask_view.js`` ships it."""
    source = (static_dir() / "js" / "ask_view.js").read_text(encoding="utf-8")
    (body, flags) = re.findall(r"export const UUID_RE = /(.+)/([a-z]*);", source)[0]
    return re.compile(body, re.IGNORECASE if "i" in flags else 0)


def _run(test_db: psycopg.Connection[Any], tmp_path: Path) -> tuple[dict[str, Any], str]:
    doc_id = _seed(test_db)
    chat = _SchemaChat()
    ctx = _ctx(test_db, tmp_path, chat)
    client = TestClient(create_app(ctx), base_url=ORIGIN)
    submitted = client.post("/api/ask", json={"question": "When does quillbeam ship?"},
                            headers=WRITE_HEADERS)
    assert submitted.status_code == 202
    assert set(submitted.json()) == SNAPSHOT_KEYS
    assert ctx.jobs is not None and ctx.jobs.run_next()
    snapshot = client.get(f"/api/jobs/{submitted.json()['id']}").json()
    assert snapshot["state"] == "succeeded", snapshot["error"]
    assert chat.steps[0] == "plan" and chat.steps[-1] == "synthesize"
    return snapshot, doc_id


def test_the_real_payload_has_exactly_the_stubs_keys(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    snapshot, _ = _run(test_db, tmp_path)

    assert set(snapshot) == SNAPSHOT_KEYS
    assert set(snapshot["result"]) == RESULT_KEYS
    assert snapshot["result"]["citations"], "nothing was cited, so nothing was compared"
    for citation in snapshot["result"]["citations"]:
        assert set(citation) == CITATION_KEYS
    phases = [e["phase"] for e in snapshot["events"]]
    assert phases[:3] == ["queued", "plan", "retrieve"] and phases[-1] == "synthesize"


def test_a_hostile_answer_arrives_verbatim_and_its_id_passes_the_js_check(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    snapshot, doc_id = _run(test_db, tmp_path)
    result = snapshot["result"]

    # Verbatim but for the out-of-range marker the server drops: the HTML is NOT
    # escaped or stripped, so the client's textContent is what keeps it inert.
    assert result["answer"] == _HOSTILE.replace(" [99]", "")
    assert "<img src=x onerror=" in result["answer"]
    (cited,) = result["citations"]
    assert (cited["ref"], cited["document_id"], cited["withheld"]) == (1, doc_id, False)
    pattern = _js_uuid_pattern()
    # ``search``, not ``fullmatch``: JS ``RegExp.test`` finds a match ANYWHERE, so
    # only the pattern's own anchors keep a UUID-plus-payload out. ``fullmatch``
    # would add anchors the shipped pattern may not have, and pass while it lacked them.
    assert pattern.search(doc_id), doc_id
    # And the pattern is not vacuous: what the hostile payload would put there fails.
    for bad in ("javascript:alert(1)", f"{doc_id}?x=1", f"x{doc_id}",
                doc_id.replace("-", ""), ""):
        assert not pattern.search(bad), bad


def test_the_job_listing_has_the_stubs_keys_and_comparable_times(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path, _SchemaChat())
    client = TestClient(create_app(ctx), base_url=ORIGIN)
    assert ctx.jobs is not None
    ingest = ctx.jobs.submit("ingest.paste", lambda handle: {})
    ask = client.post("/api/ask", json={"question": "q"}, headers=WRITE_HEADERS).json()

    (listed,) = client.get("/api/jobs").json()["jobs"]   # the ask is never listed

    assert set(listed) == LISTED_KEYS
    assert set(listed_job("queued", BEFORE)) == LISTED_KEYS
    assert (listed["id"], listed["state"]) == (ingest.id, "queued")
    # The tab compares the two with Date.parse. Same ISO form, same clock: the
    # ingest submitted first is earlier, exactly as BEFORE/AFTER bracket STAMP.
    when = datetime.fromisoformat(listed["created_at"])
    asked = datetime.fromisoformat(ask["created_at"])
    assert when.tzinfo is not None and asked.tzinfo is not None
    assert when <= asked
    for stub in (BEFORE, AFTER):
        assert datetime.fromisoformat(stub).tzinfo is not None
