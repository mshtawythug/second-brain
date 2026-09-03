"""The review queue must not enumerate confidential documents over MCP (F6).

``brain_review_findings_list`` and ``brain_review_scan`` were the last two
document-naming MCP surfaces without the F6 lens. They are the same shape as the
four listing tools closed alongside them — neither takes a document id — but they
leak through two fields that look like metadata and are not:

* ``evidence_ids`` is a list of DOCUMENT IDS. A finding is an enumeration: it
  names documents the caller never asked for, and an id is enough to fetch one.
* ``rationale`` is BODY-DERIVED. For a ``stale`` finding it is built by
  ``review.scans`` as ``f"Age: {n} days. Superseded by: '{title}' …"`` — a
  document TITLE, interpolated directly. For a ``contradiction`` finding it is
  ``verdict.rationale``, the output of an LLM given two documents' SUMMARIES,
  which are themselves generated from their content.

**Two gates are required, and they are not redundant.** They cover different
producers, which is why closing either one alone leaves a live path:

1. **Read** (``list_review_queue``) — a finding produced by ``brain review scan``
   at the TERMINAL is written to ``elicitation_gaps`` by a caller inside the
   trust boundary, and stays there. Nothing about the MCP layer un-writes it, so
   the read must filter it on the way out.
2. **Scan input** (the candidate + evidence queries) — ``brain_review_scan``
   returns the findings the scan ITSELF just produced, from the scan's own return
   value and not from ``list_review_queue``. A read-side gate never sees them.

Gate 2 has a second effect worth stating: it means an MCP-triggered scan does not
WRITE a confidential-derived rationale into the queue in the first place. That
matters because a read gate cannot un-write one — it can only hide it, and the
row would still be sitting in the table for any future surface that forgets.

Assertions serialize the WHOLE response rather than the fields we thought of, and
every fixture is proved non-vacuous before it is trusted.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.sensitivity import CONFIDENTIAL

from .test_mcp_review_scan import _install_state
from .test_review import _insert_doc, _insert_entity

_TENANT = "default"

#: The confidential document's title. For a ``stale`` finding this is the string
#: that gets interpolated into ``rationale``, so finding it in a payload is
#: unambiguous evidence the gate failed — not a near-miss on some id.
CONF_TITLE = "Severance bands — synthetic wind-down"

#: The ordinary documents. Their presence is what proves a gate excluded one tier
#: rather than simply emptying the queue.
OLD_TITLE = "Compensation ranges — synthetic role"
NEW_TITLE = "Updated salary bands — synthetic"


def _blob(payload: Any) -> str:
    """Serialize a whole MCP response for substring assertions.

    ``ensure_ascii=False`` is load-bearing, not tidiness. The default escapes
    every non-ASCII character, so a title containing an em-dash serializes as
    ``Severance bands \\u2014 synthetic`` and a substring check for the title
    silently fails to match. On a ``not in`` assertion that is worse than a bug:
    it PASSES, reporting "no leak" about a payload that contains the title in
    escaped form. Measured — every scan assertion in this module failed this way
    before the flag was added, and the withholding ones would have passed.
    """
    return json.dumps(payload, default=str, ensure_ascii=False)


def _mark_confidential(conn: psycopg.Connection, doc_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
        (CONFIDENTIAL, doc_id),
    )


@pytest.fixture
def review_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> mcp_server._State:
    """An MCP state driving the review tools against the test DB (no Ollama)."""
    return _install_state(monkeypatch, fake_embedder, tmp_path)


# ---------------------------------------------------------------------------
# brain_review_scan — the scan-input gate
# ---------------------------------------------------------------------------


def _seed_stale_pair_with_confidential_superseder(
    conn: psycopg.Connection,
) -> dict[str, str]:
    """An aged doc superseded by a CONFIDENTIAL one, plus a wholly-normal pair.

    The confidential document is the SUPERSEDER, not the candidate, deliberately:
    its title is what ``review.scans`` interpolates into the ``stale`` rationale.
    A gate that filtered only the candidate list would leave that interpolation
    intact and this test would still fail.

    The second, wholly-normal pair exists so "no confidential title in the
    payload" cannot be satisfied by an empty payload — see the anti-vacuity test.
    """
    old = _insert_doc(
        conn,
        title=OLD_TITLE,
        summary="old comp",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    sealed = _insert_doc(
        conn,
        title=CONF_TITLE,
        summary="sealed comp",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _mark_confidential(conn, sealed)
    _insert_entity(conn, canonical_key="comp", name="Compensation", doc_ids=[old, sealed])

    # A second pair, entirely normal, on its own entity.
    plain_old = _insert_doc(
        conn,
        title="Vendor list — synthetic",
        summary="old vendors",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    plain_new = _insert_doc(
        conn,
        title=NEW_TITLE,
        summary="new vendors",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _insert_entity(
        conn, canonical_key="vend", name="Vendors", doc_ids=[plain_old, plain_new]
    )
    return {"old": old, "sealed": sealed, "plain_old": plain_old}


def _scan_fixture_is_not_vacuous(
    conn: psycopg.Connection, ids: dict[str, str]
) -> None:
    """The sealed doc is really confidential AND really reachable by the scan."""
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE id=%s::uuid", (ids["sealed"],)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL, "fixture must be confidential"
    permissive = mcp_server.brain_review_scan(
        scan_type="stale", dry_run=True, include_confidential=True
    )
    assert CONF_TITLE in _blob(permissive), (
        "the scan cannot reach the confidential document even when permitted, "
        "so the strict assertions below would pass vacuously"
    )


def test_review_scan_rationale_never_names_a_confidential_superseder(
    review_state: mcp_server._State,  # noqa: ARG001 — installs the state
    test_db: psycopg.Connection,
) -> None:
    """The sharp case: a TITLE interpolated into body-derived rationale text.

    ``rationale`` reads as a computed metadata string, which is exactly why this
    was missed — but ``review.scans`` builds it by interpolating the superseding
    document's title verbatim.
    """
    ids = _seed_stale_pair_with_confidential_superseder(test_db)
    _scan_fixture_is_not_vacuous(test_db, ids)

    blob = _blob(mcp_server.brain_review_scan(scan_type="stale", dry_run=True))

    assert CONF_TITLE not in blob
    assert ids["sealed"] not in blob


def test_review_scan_still_finds_the_wholly_normal_pair(
    review_state: mcp_server._State,  # noqa: ARG001 — installs the state
    test_db: psycopg.Connection,
) -> None:
    """Anti-vacuity: the gate excludes one tier, it does not disable the scan."""
    ids = _seed_stale_pair_with_confidential_superseder(test_db)
    _scan_fixture_is_not_vacuous(test_db, ids)

    payload = mcp_server.brain_review_scan(scan_type="stale", dry_run=True)

    targets = {f["target_id"] for f in payload["findings"]}
    assert ids["plain_old"] in targets, (
        f"the normal stale pair produced no finding, so the assertion above "
        f"passes for the wrong reason: {payload}"
    )


def test_review_scan_include_confidential_opts_back_in(
    review_state: mcp_server._State,  # noqa: ARG001 — installs the state
    test_db: psycopg.Connection,
) -> None:
    """The permissive direction. Without it the bridge could be inverted.

    A control pairs with an ASSERTION, not with a test. The strict test above
    makes TWO withholding claims over one blob — the title and the sealed
    document's id — and they travel by different routes: the title through
    ``rationale``, the id through ``evidence_ids``. One ``in`` assertion cannot
    be evidence for both, so there are two here.
    """
    ids = _seed_stale_pair_with_confidential_superseder(test_db)
    _scan_fixture_is_not_vacuous(test_db, ids)

    blob = _blob(
        mcp_server.brain_review_scan(
            scan_type="stale", dry_run=True, include_confidential=True
        )
    )

    assert CONF_TITLE in blob
    # Pins the second claim of ``…never_names_a_confidential_superseder``. The
    # id reaches the payload by exactly one route — the ``evidence_ids`` list
    # serialized out of ``brain_review_scan`` — so without this the strict
    # ``ids["sealed"] not in blob`` beside it would pass on an empty route.
    assert ids["sealed"] in blob


def test_review_scan_skips_a_confidential_candidate(
    review_state: mcp_server._State,  # noqa: ARG001 — installs the state
    test_db: psycopg.Connection,
) -> None:
    """The other direction of the same pair: the AGED doc is the confidential one.

    ``target_id`` is then the confidential document's own id, and it reaches the
    payload through a different field than the superseder's title does. Asserted
    separately because the candidate query and the superseder query are two
    different SELECTs and gating one does not gate the other.

    **The id claim is the evidence here; the title claim below is inert.** In
    THIS fixture the confidential document is the candidate, and a scan finding
    carries only ``kind``/``target_type``/``target_id``/``score``/``rationale``/
    ``evidence_ids`` — the candidate's own title is in none of them, and
    ``rationale`` names the SUPERSEDER, which here is the ordinary
    ``NEW_TITLE``. Measured 2026-08-21 against the permissive payload: the id
    is present, ``CONF_TITLE`` is NOT, so ``CONF_TITLE not in blob`` cannot
    fail whatever the gate does and no control can pin it. It is kept as a
    forward guard against a future payload that starts carrying candidate
    titles — labelled so nobody counts it as evidence. The title route is
    covered for real by
    ``test_review_scan_rationale_never_names_a_confidential_superseder``, whose
    fixture makes the confidential document the superseder.
    """
    old = _insert_doc(
        conn := test_db,
        title=CONF_TITLE,
        summary="sealed comp",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    _mark_confidential(conn, old)
    new = _insert_doc(
        conn,
        title=NEW_TITLE,
        summary="new comp",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _insert_entity(conn, canonical_key="comp", name="Compensation", doc_ids=[old, new])

    permissive = mcp_server.brain_review_scan(
        scan_type="stale", dry_run=True, include_confidential=True
    )
    assert old in _blob(permissive), "fixture unreachable — test would be vacuous"

    blob = _blob(mcp_server.brain_review_scan(scan_type="stale", dry_run=True))

    assert old not in blob
    # Inert by construction — see the docstring. Not evidence; a forward guard.
    assert CONF_TITLE not in blob


# ---------------------------------------------------------------------------
# brain_review_findings_list — the read gate
# ---------------------------------------------------------------------------


def _insert_finding(
    conn: psycopg.Connection,
    *,
    target_id: str,
    evidence_ids: list[str],
    rationale: str,
) -> str:
    """Insert one surfaced ``stale`` finding directly.

    Written straight to the table rather than produced by a scan, because this
    test is about the READ path and must hold for a finding the MCP layer did not
    create — which is the case the read gate exists for: ``brain review scan`` at
    a terminal is inside the trust boundary and legitimately writes findings
    about confidential documents.
    """
    row = conn.execute(
        """
        INSERT INTO elicitation_gaps
            (tenant_id, signal_kind, target_type, target_id, score,
             evidence_ids, rationale, status)
        VALUES (%s, 'stale', 'doc', %s, 0.9, %s, %s, 'surfaced')
        RETURNING id::text
        """,
        (_TENANT, target_id, evidence_ids, rationale),
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def queued_findings(
    test_db: psycopg.Connection,
    review_state: mcp_server._State,  # noqa: ARG001 — installs the state
) -> dict[str, str]:
    """Two queued findings: one touching a confidential doc, one wholly normal."""
    sealed = _insert_doc(
        test_db, title=CONF_TITLE, summary="sealed", ingested_days_ago=400
    )
    _mark_confidential(test_db, sealed)
    normal_a = _insert_doc(
        test_db, title=OLD_TITLE, summary="old", ingested_days_ago=400
    )
    normal_b = _insert_doc(
        test_db, title=NEW_TITLE, summary="new", ingested_days_ago=15
    )
    _insert_finding(
        test_db,
        target_id=normal_a,
        evidence_ids=[normal_a, sealed],
        rationale=f"Age: 400 days. Superseded by: '{CONF_TITLE}' (similarity 0.90)",
    )
    # A DIFFERENT target: ``elicitation_gaps`` is unique on
    # ``(tenant_id, signal_kind, target_id)``, which is the idempotency the scan
    # relies on — two findings of one kind cannot share a target.
    _insert_finding(
        test_db,
        target_id=normal_b,
        evidence_ids=[normal_b, normal_a],
        rationale=f"Age: 400 days. Superseded by: '{OLD_TITLE}' (similarity 0.90)",
    )
    return {"sealed": sealed, "normal_a": normal_a, "normal_b": normal_b}


def _queue_fixture_is_not_vacuous(
    conn: psycopg.Connection, ids: dict[str, str]
) -> None:
    n = conn.execute("SELECT count(*) FROM elicitation_gaps").fetchone()
    assert n is not None and n[0] == 2, "fixture must hold both findings"
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE id=%s::uuid", (ids["sealed"],)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL


def test_findings_list_hides_a_finding_whose_evidence_is_confidential(
    test_db: psycopg.Connection, queued_findings: dict[str, str]
) -> None:
    """A finding is withheld whole — id, rationale and evidence together.

    Not redacted: a finding whose ``rationale`` were blanked but whose row still
    appeared would prove a confidential document exists, is stale, and was
    superseded — the membership oracle ``_confidential_lens`` describes.
    """
    _queue_fixture_is_not_vacuous(test_db, queued_findings)

    blob = _blob(mcp_server.brain_review_findings_list())

    assert CONF_TITLE not in blob
    assert queued_findings["sealed"] not in blob


def test_findings_list_still_returns_the_wholly_normal_finding(
    test_db: psycopg.Connection, queued_findings: dict[str, str]
) -> None:
    """Anti-vacuity: exactly one of the two findings survives."""
    _queue_fixture_is_not_vacuous(test_db, queued_findings)

    payload = mcp_server.brain_review_findings_list()

    assert len(payload["findings"]) == 1, payload
    assert OLD_TITLE in payload["findings"][0]["rationale"]


def test_findings_list_include_confidential_opts_back_in(
    test_db: psycopg.Connection, queued_findings: dict[str, str]
) -> None:
    """The permissive direction.

    Two ``in`` assertions for the two withholding claims the strict test makes
    over one blob. ``len(...) == 2`` establishes that the sealed finding's ROW
    survives; it says nothing about whether the sealed document's ID string
    reaches the serialized payload, which is the second claim.
    """
    _queue_fixture_is_not_vacuous(test_db, queued_findings)

    payload = mcp_server.brain_review_findings_list(include_confidential=True)
    blob = _blob(payload)

    assert len(payload["findings"]) == 2
    assert CONF_TITLE in blob
    # Pins the second claim of ``…hides_a_finding_whose_evidence_is_confidential``.
    # The sealed id reaches the payload only through the withheld finding's
    # ``evidence_ids``; the surviving normal finding does not reference it.
    assert queued_findings["sealed"] in blob
