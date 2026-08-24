"""The derived-edges fence must not name a confidential document (F6).

`` _ingested/`` mirrors are an EGRESS boundary: the directory appears in
NEITHER Quartz config's ``ignorePatterns``, and a mirror whose own frontmatter
says ``sensitivity: normal`` is published. ``render_fenced_section`` joined
``documents`` for each partner with no sensitivity predicate, so a normal host's
published page carried, as a visible anchor::

    - [[2026-06-02-conf|Wind-down memo (synthetic confidential)]] *(shared_thread)*

— the confidential document's TITLE as the link text, its SLUG as the target,
its EXISTENCE, and the fact that it is related to this one. The host's own
``sensitivity`` key is what keeps the page published, so ``RemoveConfidential``
never looks at the partner named inside it. Before this module,
``grep -rn 'sensitivity\\|confidential' src/brain/vault/derived_links/`` returned
nothing at all: the package had no gate to be inconsistent with.

**Two downstream surfaces, one upstream.** The fence is not stripped at publish
time — ``quartz_overrides/quartz/plugins/transformers/derivedFenceMark.ts``
STAMPS ``data-brain-*`` attributes onto those links and leaves them in place,
and ``plugins/emitters/contentIndex.ts`` reads the attributes back to emit link
records into ``static/contentIndex.json``. So the leak reaches both the rendered
HTML (via the anchor TEXT) and the public JSON index (via the link TARGET).

*What this module can and cannot measure, stated rather than implied:* there is
no JavaScript test harness in this repo (``find src/brain/quartz_overrides -name
'*.test.ts'`` is empty), so contentIndex.json cannot be asserted from pytest.
What is asserted instead is the sole INPUT to both surfaces — the markdown
bullet — and both of its halves separately: the title (which becomes the anchor
text) and the vault-path stem (which becomes the link target and the
contentIndex record). A gate that scrubbed only the alias and left the stem
would still feed the JSON index, and the stem assertions are what catch that.
The JS half remains unpinned and is named here so it is not mistaken for
covered.

All fixture data is synthetic.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.sensitivity import CONFIDENTIAL
from brain.vault.derived_links.fence import (
    render_fenced_section,
    rewrite_derived_fences,
)

#: Distinctive synthetic strings — a substring hit is unambiguous evidence.
CONF_TITLE = "Wind-down memo (synthetic confidential)"
CONF_STEM = "2026-06-02-conf"
NORMAL_TITLE = "Roadmap notes (synthetic normal)"
CTRL_TITLE = "Control partner (synthetic normal)"
CTRL_STEM = "2026-06-03-ctrl"

_HOST_REL = "_ingested/krisp/2026-06-01-host.md"
_CONF_REL = f"_ingested/krisp/{CONF_STEM}.md"
_CTRL_REL = f"_ingested/krisp/{CTRL_STEM}.md"


def _doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str,
    confidential: bool = False,
) -> str:
    """An ingested-tier document with a mirror path. Content salted for the hash."""
    source_id = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('krisp', %s, '{}'::jsonb) RETURNING id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    salted = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    doc_id = str(
        conn.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type,
                 tags, metadata, vault_path, kind)
            VALUES (%s, %s, %s, %s, 'transcript', '{}', '{}'::jsonb, %s,
                    'ingested')
            RETURNING id::text
            """,
            (
                source_id,
                title,
                salted,
                hashlib.sha256(salted.encode("utf-8")).hexdigest(),
                vault_path,
            ),
        ).fetchone()[0]
    )
    if confidential:
        conn.execute(
            "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
            (CONFIDENTIAL, doc_id),
        )
    return doc_id


def _link(conn: psycopg.Connection[Any], src: str, dst: str) -> None:
    """Insert one ``shared_thread`` edge in EXACTLY the given column order.

    Deliberately not the pass-runner's ``(LEAST, GREATEST)`` canonicalization.
    Which column a document lands in is the variable
    :func:`test_confidential_partner_dropped_from_either_column` is controlling,
    and ids are random UUIDs, so deriving the order from them means the fixture
    picks its own orientation and the test cannot ask for one. (Measured: the
    first draft did derive it, and its own straddle check caught both edges
    landing in the same column.)
    """
    conn.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, "
        "evidence, weight) VALUES (%s, %s, 'shared_thread', '{}'::jsonb, 1.0)",
        (src, dst),
    )


def _mirror(root: Path, rel: str, title: str, sensitivity: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\nsensitivity: {sensitivity}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def wired(test_db: psycopg.Connection, tmp_path: Path) -> dict[str, str]:
    """A normal host edged to one confidential and one normal partner.

    The two partners differ ONLY in ``sensitivity``, so any difference in what
    the fence emits about them is attributable to the gate and to nothing else.
    The normal partner is what makes every ``not in`` below non-vacuous: it
    proves the renderer ran, found edges, and emitted bullets.
    """
    host = _doc(test_db, title=NORMAL_TITLE, vault_path=_HOST_REL)
    conf = _doc(test_db, title=CONF_TITLE, vault_path=_CONF_REL, confidential=True)
    ctrl = _doc(test_db, title=CTRL_TITLE, vault_path=_CTRL_REL)
    _link(test_db, host, conf)
    _link(test_db, host, ctrl)
    _mirror(tmp_path, _HOST_REL, NORMAL_TITLE, "normal")
    _mirror(tmp_path, _CONF_REL, CONF_TITLE, "confidential")
    _mirror(tmp_path, _CTRL_REL, CTRL_TITLE, "normal")
    return {"host": host, "conf": conf, "ctrl": ctrl}


# ---------------------------------------------------------------------------
# Leg 1 — the partner gate. The disclosure that was reproduced.
# ---------------------------------------------------------------------------


def test_published_mirror_never_names_a_confidential_partner(
    wired: dict[str, str], test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """THE FIX, asserted on the file a visitor is served.

    Disclosure assertions come FIRST and the non-vacuity control second, so a
    mutation that reinstates the leak reddens the disclosure rather than being
    short-circuited by some earlier bookkeeping assertion. Both halves of the
    bullet are checked: the TITLE becomes the rendered anchor text, the STEM
    becomes the link target and the ``contentIndex.json`` record.
    """
    rewrite_derived_fences(test_db, set(wired.values()), vault_path=tmp_path)

    page = (tmp_path / _HOST_REL).read_text(encoding="utf-8")
    assert CONF_TITLE not in page
    assert CONF_STEM not in page
    # Non-vacuity: the fence EXISTS and carries the normal partner, so the two
    # absences above are about the gate and not about an empty section.
    assert CTRL_TITLE in page
    assert CTRL_STEM in page
    assert "BRAIN_DERIVED_START" in page
    # The host page is published — this is what makes the absence matter.
    assert "sensitivity: normal" in page


def test_confidential_partner_dropped_from_either_column(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """Both endpoints, pinned — the symmetric ``CASE`` join is an argument, not proof.

    ``iter_suggestions`` needs two predicates because it joins source and target
    separately. Here one predicate on ``partner`` is claimed to cover both, since
    the ``CASE`` resolves whichever end is not the host. That claim is only worth
    what it is measured at, so the same confidential document is placed in the
    ``src`` column for one host and the ``dst`` column for another and both are
    asserted. A gate written against one column would pass half of this.
    """
    host_a = _doc(test_db, title="Host A (synthetic normal)", vault_path=_HOST_REL)
    host_b = _doc(test_db, title="Host B (synthetic normal)", vault_path=_CTRL_REL)
    conf = _doc(test_db, title=CONF_TITLE, vault_path=_CONF_REL, confidential=True)
    _link(test_db, conf, host_a)  # confidential in the SRC column
    _link(test_db, host_b, conf)  # confidential in the DST column

    # Confirm the fixture really did straddle both columns; without this the
    # test could be asserting the same orientation twice.
    columns = {
        (str(src), str(dst))
        for src, dst in test_db.execute(
            "SELECT src_document_id::text, dst_document_id::text FROM derived_links"
        ).fetchall()
    }
    assert any(src == conf for src, _ in columns), columns
    assert any(dst == conf for _, dst in columns), columns

    assert render_fenced_section(test_db, host_a) is None
    assert render_fenced_section(test_db, host_b) is None


def test_renderer_still_emits_normal_partners(
    wired: dict[str, str], test_db: psycopg.Connection
) -> None:
    """The gate narrows one tier; it does not empty the fence."""
    rendered = render_fenced_section(test_db, wired["host"])

    assert rendered is not None
    assert CTRL_TITLE in rendered
    assert CONF_TITLE not in rendered


# ---------------------------------------------------------------------------
# Leg 2 — the host gate. Weaker argument; asserted anyway.
# ---------------------------------------------------------------------------


def test_confidential_host_mirror_gets_no_fence(
    wired: dict[str, str], test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """A confidential mirror is never written a fence, even of normal partners.

    Its own page is unpublished by ``RemoveConfidential``, so this closes nothing
    measured — it removes the pipeline's dependence on that TypeScript filter
    being correct. The control is the sibling test above: the SAME call, in the
    SAME fixture, does write a fence onto the normal host, so "no fence here"
    is a statement about this document's tier and not about the call doing
    nothing.
    """
    # Give the confidential host a NORMAL partner, so a fence would render if
    # the host leg were absent — otherwise the partner gate alone would empty it
    # and this test would pass for the wrong reason.
    _link(test_db, wired["conf"], wired["ctrl"])

    rewrite_derived_fences(test_db, set(wired.values()), vault_path=tmp_path)

    conf_page = (tmp_path / _CONF_REL).read_text(encoding="utf-8")
    assert "BRAIN_DERIVED_START" not in conf_page
    assert CTRL_TITLE not in conf_page
    # Same call, same run: the normal host DID get one.
    assert "BRAIN_DERIVED_START" in (tmp_path / _HOST_REL).read_text(encoding="utf-8")


def test_marking_a_host_confidential_strips_its_existing_fence(
    wired: dict[str, str], test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """STRIP, not SKIP — the pipeline converges instead of freezing.

    Renders a fence onto the host while it is normal, marks it confidential,
    re-runs, and asserts the fence is GONE. A skip-based host gate leaves the
    stale fence on disk forever, which is the failure mode this shape was chosen
    to avoid.
    """
    rewrite_derived_fences(test_db, set(wired.values()), vault_path=tmp_path)
    # Non-vacuity: there is something to strip.
    assert "BRAIN_DERIVED_START" in (tmp_path / _HOST_REL).read_text(encoding="utf-8")

    test_db.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
        (CONFIDENTIAL, wired["host"]),
    )
    rewrite_derived_fences(test_db, set(wired.values()), vault_path=tmp_path)

    page = (tmp_path / _HOST_REL).read_text(encoding="utf-8")
    assert "BRAIN_DERIVED_START" not in page
    assert CTRL_TITLE not in page
