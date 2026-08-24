"""``brain mark-confidential`` must take effect on ALREADY-PUBLISHED pages (F6).

The F6 gate in ``render_fenced_section`` decides what a fence may name **at
render time**. It is silent about fences rendered EARLIER — and that is the
whole of this bug. A document marked confidential kept its title and slug in
every partner's already-published ``_ingested/`` page until somebody happened to
run a full ``brain vault relink-derived``:

    $ brain mark-confidential 01662f44
    marked 01662f44 as confidential          # exit 0
    # ...and the partner's published page still says
    # - [[2026-06-02-conf|Wind-down memo (synthetic confidential)]]

So the command reported success while, on the published site, the document was
not confidential — silently, and for an unbounded stretch of time. A user who
marks something confidential has every reason to believe the action took effect.
That is arguably worse than the render-time hole it sits behind, because the
render-time hole at least fails consistently.

**Both callers of ``set_document_sensitivity`` are covered here, deliberately.**
``brain mark-confidential`` / ``mark-normal`` (``cli_docs``) and the
``brain sensitivity --action mark-confidential`` sweep (``cli_sensitivity``).
Fixing only the one that was reported would reproduce, in the fix, the exact
trap this branch has now catalogued three times: a guarantee that holds for the
surface its author was looking at and silently not for its sibling.

**Both DIRECTIONS too.** ``mark-normal`` refreshes as well, so a document
returned to the normal tier reappears in its partners' fences instead of staying
invisible until the next relink. A one-way refresh would fix the alarming case
and leave the quiet one, and the quiet one is a correctness bug the user cannot
see.

Every assertion here is made against a page that DEMONSTRABLY named the document
first — the fixture renders the fence while both documents are normal and asserts
the title is present before touching anything. And no test in this module runs a
relink: the point is that the mark alone is sufficient.

All fixture data is synthetic.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli
from brain.sensitivity import CONFIDENTIAL
from brain.vault.derived_links.fence import rewrite_derived_fences

runner = CliRunner()

CONF_TITLE = "Wind-down memo (synthetic confidential)"
CONF_STEM = "2026-06-02-conf"
HOST_TITLE = "Roadmap notes (synthetic normal)"

_HOST_REL = "_ingested/krisp/2026-06-01-host.md"
_CONF_REL = f"_ingested/krisp/{CONF_STEM}.md"


def _doc(conn: psycopg.Connection[Any], *, title: str, vault_path: str) -> str:
    source_id = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('krisp', %s, '{}'::jsonb) RETURNING id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    salted = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    return str(
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


def _mirror(root: Path, rel: str, title: str, sensitivity: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\nsensitivity: {sensitivity}\n---\n\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def published(test_db: psycopg.Connection, tmp_path: Path) -> dict[str, str]:
    """Two NORMAL linked documents with the host's fence already rendered.

    This is the pre-condition that makes the bug possible and the tests
    meaningful: the fence is rendered while both are normal, so the host page
    genuinely names the other document before anything is marked. The assert
    below is a fixture-level control — if it ever fails, every test in this
    module is measuring nothing.
    """
    host = _doc(test_db, title=HOST_TITLE, vault_path=_HOST_REL)
    partner = _doc(test_db, title=CONF_TITLE, vault_path=_CONF_REL)
    src, dst = (host, partner) if host < partner else (partner, host)
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, "
        "evidence, weight) VALUES (%s, %s, 'shared_thread', '{}'::jsonb, 1.0)",
        (src, dst),
    )
    _mirror(tmp_path, _HOST_REL, HOST_TITLE, "normal")
    _mirror(tmp_path, _CONF_REL, CONF_TITLE, "normal")
    rewrite_derived_fences(test_db, {host, partner}, vault_path=tmp_path)

    page = (tmp_path / _HOST_REL).read_text(encoding="utf-8")
    assert CONF_TITLE in page, "fixture is inert: the host page must name the partner"
    assert CONF_STEM in page
    return {"host": host, "partner": partner}


def _host_page(root: Path) -> str:
    return (root / _HOST_REL).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# `brain mark-confidential` — the reported path
# ---------------------------------------------------------------------------


def test_mark_confidential_scrubs_the_partner_page_without_a_relink(
    published: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FIX. No relink is run anywhere in this test — the mark is enough."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["mark-confidential", published["partner"][:8]]
    )

    assert result.exit_code == 0, result.stdout
    page = _host_page(tmp_path)
    # Both halves: title -> rendered anchor text, stem -> link target and the
    # contentIndex record.
    assert CONF_TITLE not in page
    assert CONF_STEM not in page


def test_the_marked_documents_own_fence_goes_too(
    published: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marked document's own mirror loses its fence as well.

    **This one does NOT pin the refresh, and the docstring said it did until a
    mutation proved otherwise.** Removing ``refresh_fences_naming`` from
    ``cli_docs`` leaves this test green, because ``_set_sensitivity`` already
    regenerates an ingested-tier mirror wholesale via ``regenerate_vault_file``
    and a regenerated mirror has no fence. On THIS path the refresh's inclusion
    of the marked document is redundant.

    It is kept, relabelled, as a characterization test: the end state is part
    of the contract even though a different mechanism delivers it here, and if
    the mirror regeneration is ever narrowed this becomes the thing that
    notices. Where the refresh IS load-bearing for the marked document's own
    fence is the sweep path — ``cli_sensitivity`` performs no mirror
    regeneration at all — and that is asserted in
    :func:`test_the_sensitivity_sweep_also_scrubs_partner_pages`, where mutation
    I does redden it.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    assert "BRAIN_DERIVED_START" in (tmp_path / _CONF_REL).read_text(encoding="utf-8")

    runner.invoke(cli.app, ["mark-confidential", published["partner"][:8]])

    assert "BRAIN_DERIVED_START" not in (
        tmp_path / _CONF_REL
    ).read_text(encoding="utf-8")


def test_mark_normal_restores_the_partner_page_without_a_relink(
    published: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction — the quiet bug a one-way refresh would leave behind.

    Asserted as a round trip from one fixture so this is a statement about
    symmetry rather than two unrelated behaviours: mark, verify it went, unmark,
    verify it came back.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    runner.invoke(cli.app, ["mark-confidential", published["partner"][:8]])
    assert CONF_TITLE not in _host_page(tmp_path)

    result = runner.invoke(cli.app, ["mark-normal", published["partner"][:8]])

    assert result.exit_code == 0, result.stdout
    page = _host_page(tmp_path)
    assert CONF_TITLE in page
    assert CONF_STEM in page


def test_an_idempotent_remark_is_still_a_no_op(
    published: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-marking an already-confidential document must not churn files.

    ``set_document_sensitivity`` returns ``False`` when nothing changed and the
    command returns early, so the refresh never runs — asserted by mtime,
    because "the page is still correct" would pass even if every file had been
    rewritten with identical bytes.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    runner.invoke(cli.app, ["mark-confidential", published["partner"][:8]])
    before = (tmp_path / _HOST_REL).stat().st_mtime_ns

    result = runner.invoke(
        cli.app, ["mark-confidential", published["partner"][:8]]
    )

    assert result.exit_code == 0
    assert "already confidential" in result.output
    assert (tmp_path / _HOST_REL).stat().st_mtime_ns == before


def test_the_refresh_finds_partners_in_either_edge_column(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """``refresh_fences_naming``'s partner query is symmetric — pinned, not argued.

    ``derived_links`` stores one row per pair and the marked document may sit in
    either column, so the query ORs both and projects the other end. A version
    written against one column would still pass most tests here, because the
    ``published`` fixture takes its orientation from UUID ordering and therefore
    picks its own — measured: mutating the query to a single column reddens a
    DIFFERENT set of tests from run to run. This test removes the coin flip by
    inserting each orientation explicitly.

    (Second time this session that a fixture deriving edge order from random ids
    hid a one-sided query. The lesson is that orientation is a variable a test
    must SET, never inherit.)
    """
    from brain.vault.derived_links.fence import refresh_fences_naming

    marked = _doc(test_db, title=CONF_TITLE, vault_path=_CONF_REL)
    as_dst = _doc(test_db, title="Host via DST (synthetic)", vault_path=_HOST_REL)
    other_rel = "_ingested/krisp/2026-06-04-other.md"
    as_src = _doc(test_db, title="Host via SRC (synthetic)", vault_path=other_rel)
    # marked in the DST column for one host, the SRC column for the other.
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, "
        "evidence, weight) VALUES (%s, %s, 'shared_thread', '{}'::jsonb, 1.0)",
        (as_dst, marked),
    )
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, "
        "evidence, weight) VALUES (%s, %s, 'shared_thread', '{}'::jsonb, 1.0)",
        (marked, as_src),
    )
    for rel, title in (
        (_CONF_REL, CONF_TITLE),
        (_HOST_REL, "Host via DST (synthetic)"),
        (other_rel, "Host via SRC (synthetic)"),
    ):
        _mirror(tmp_path, rel, title, "normal")
    rewrite_derived_fences(
        test_db, {marked, as_dst, as_src}, vault_path=tmp_path
    )
    # Non-vacuity: BOTH hosts name the marked document before anything changes.
    assert CONF_TITLE in (tmp_path / _HOST_REL).read_text(encoding="utf-8")
    assert CONF_TITLE in (tmp_path / other_rel).read_text(encoding="utf-8")

    test_db.execute(
        "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
        (CONFIDENTIAL, marked),
    )
    refresh_fences_naming(test_db, marked, vault_path=tmp_path)

    assert CONF_TITLE not in (tmp_path / _HOST_REL).read_text(encoding="utf-8")
    assert CONF_TITLE not in (tmp_path / other_rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The sibling caller — `brain sensitivity --action mark-confidential`
# ---------------------------------------------------------------------------


def test_the_sensitivity_sweep_also_scrubs_partner_pages(
    published: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second ``set_document_sensitivity`` caller (``brain backfill
    scan-secrets --action mark-confidential``), fixed alongside the first.

    Fixing only the reported command would put the guarantee on one surface and
    silently not its sibling — the trap this branch has catalogued three times.
    The sweep marks by SECRET DETECTION, so the partner's body is given a
    synthetic secret to be found by; the assertion is still about the fence.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    # A synthetic AWS-shaped key so the scanner flags this document. Not a real
    # credential — the literal is nonsense and matches only the shape.
    test_db.execute(
        "UPDATE documents SET content = %s WHERE id = %s::uuid",
        ("token AKIAIOSFODNN7EXAMPLE end\n", published["partner"]),
    )

    result = runner.invoke(
        cli.app,
        ["backfill", "scan-secrets", "--action", "mark-confidential", "--apply"],
    )

    assert result.exit_code == 0, result.stdout
    # Non-vacuity: the sweep actually marked it. Without this the fence
    # assertions below could pass because nothing happened at all.
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s::uuid",
        (published["partner"],),
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL, result.stdout

    page = _host_page(tmp_path)
    assert CONF_TITLE not in page
    assert CONF_STEM not in page
    # The marked document's OWN fence, which on this path only the refresh can
    # strip: ``cli_sensitivity`` performs no ``regenerate_vault_file``, unlike
    # ``cli_docs``. This is where that assertion is load-bearing.
    assert "BRAIN_DERIVED_START" not in (
        tmp_path / _CONF_REL
    ).read_text(encoding="utf-8")


def test_the_sweep_leaves_the_mirror_frontmatter_stale_PREEXISTING(
    published: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHARACTERIZATION of a SEPARATE, PRE-EXISTING hole this commit does NOT fix.

    ``brain backfill scan-secrets --action mark-confidential --apply`` flips
    ``documents.sensitivity`` and never writes the tier into the mirror's
    frontmatter — ``cli_sensitivity`` has no ``regenerate_vault_file`` or
    ``rewrite_sensitivity`` call, unlike ``cli_docs._set_sensitivity``. Quartz's
    ``RemoveConfidential`` reads the FILE, so the marked document's own page
    keeps publishing.

    That is a bigger hole than the fence staleness this commit closes, on the
    same command, and it is deliberately left open here rather than folded in:
    it is a different mechanism with a different fix. Pinned as a
    characterization test so it is **recorded and cannot be discovered twice**,
    and so whoever fixes it gets a failing test telling them where to update
    this expectation. Flip the assertion when you fix it.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    test_db.execute(
        "UPDATE documents SET content = %s WHERE id = %s::uuid",
        ("token AKIAIOSFODNN7EXAMPLE end\n", published["partner"]),
    )

    runner.invoke(
        cli.app,
        ["backfill", "scan-secrets", "--action", "mark-confidential", "--apply"],
    )

    db_tier = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s::uuid",
        (published["partner"],),
    ).fetchone()
    assert db_tier is not None and db_tier[0] == CONFIDENTIAL
    frontmatter = (tmp_path / _CONF_REL).read_text(encoding="utf-8")
    # The bug, asserted as it currently behaves.
    assert "sensitivity: normal" in frontmatter
    assert "sensitivity: confidential" not in frontmatter
