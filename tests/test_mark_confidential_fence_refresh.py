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

    **This docstring has now been wrong twice, in opposite directions, and both
    times a mutation is what said so.** It first claimed to pin the fence
    refresh; it did not — ``_set_sensitivity`` regenerated an ingested mirror
    wholesale, so the fence went whether or not the refresh ran, and removing
    the refresh left this green. It was relabelled as characterization on that
    basis. Then the two mechanisms moved behind one call
    (``propagate_sensitivity_to_vault``), and removing THAT call removes both,
    so this test reddens again — the relabelling had gone stale within the same
    branch that wrote it.

    What it pins, stated so it survives the next refactor: **the end state.**
    After a mark, the marked document's own mirror carries no fence. Which
    mechanism delivers that — mirror regeneration, the fence refresh, or both —
    is an implementation detail this test deliberately does not name, because
    naming it is what went stale twice. Both mechanisms currently live behind
    the shared propagation call, and either one alone would satisfy this on the
    ingested path.
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
    # The marked document's OWN fence is back too — and this assertion is what
    # pins the STAGE ORDER inside ``propagate_sensitivity_to_vault``. Stage 1
    # regenerates an ingested mirror wholesale, which produces a file with no
    # fence; stage 2 renders the fence. Reverse them and stage 1 silently
    # discards what stage 2 just wrote. Added because a mutation that swapped
    # the two stages left every other assertion in this module green: the
    # ordering was argued in a docstring and measured nowhere.
    assert "BRAIN_DERIVED_START" in (tmp_path / _CONF_REL).read_text(
        encoding="utf-8"
    )


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


def test_the_sweep_writes_the_tier_into_the_mirror_frontmatter(
    published: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep must make the document confidential ON DISK, not only in the DB.

    **This test was born as a characterization test asserting the opposite.**
    ``brain backfill scan-secrets --action mark-confidential --apply`` flipped
    ``documents.sensitivity`` and never wrote the tier into the mirror, because
    ``cli_sensitivity`` had no ``regenerate_vault_file`` / ``rewrite_sensitivity``
    call while ``cli_docs`` did. Quartz's ``RemoveConfidential`` reads the FILE,
    so the marked document's own page kept publishing: the sweep reported
    ``N written`` and, on the published site, made nothing confidential. That is
    the command someone runs *after finding secrets in their corpus*.

    Both callers now go through ``propagate_sensitivity_to_vault``, so the DB
    flip and the disk work cannot drift apart again.

    The DB assertion is the non-vacuity control: without it, "the frontmatter
    says confidential" could pass on a run where the sweep matched nothing and
    the fixture happened to start that way.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    test_db.execute(
        "UPDATE documents SET content = %s WHERE id = %s::uuid",
        ("token AKIAIOSFODNN7EXAMPLE end\n", published["partner"]),
    )
    # Control: the file starts at ``normal``, so the assertion below is about
    # the sweep and not about a fixture that was already correct.
    assert "sensitivity: normal" in (tmp_path / _CONF_REL).read_text(
        encoding="utf-8"
    )

    result = runner.invoke(
        cli.app,
        ["backfill", "scan-secrets", "--action", "mark-confidential", "--apply"],
    )

    assert result.exit_code == 0, result.stdout
    db_tier = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s::uuid",
        (published["partner"],),
    ).fetchone()
    assert db_tier is not None and db_tier[0] == CONFIDENTIAL, result.stdout

    frontmatter = (tmp_path / _CONF_REL).read_text(encoding="utf-8")
    # The fix: the file agrees with the database, which is the only thing
    # ``RemoveConfidential`` can act on.
    assert "sensitivity: confidential" in frontmatter
    assert "sensitivity: normal" not in frontmatter


def test_the_sweep_marks_a_vault_tier_note_on_disk_too(
    test_db: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other ``kind`` branch — a vault-tier note is edited, not regenerated.

    ``regenerate_vault_file`` refuses vault-tier rows outright (the file is
    authoritative there), so an ingested-only fix would leave exactly the notes
    most likely to hold sensitive material untouched. Worse than untouched:
    ``sync._sensitivity_from_frontmatter`` reads the tier back off the
    frontmatter on every pass, so the column would flip and then silently
    REVERT on the next ``brain vault sync``.
    """
    rel = "notes/2026-06-05-authored.md"
    doc_id = _doc(test_db, title="Authored note (synthetic)", vault_path=rel)
    test_db.execute(
        "UPDATE documents SET kind='vault', content=%s WHERE id=%s::uuid",
        ("token AKIAIOSFODNN7EXAMPLE end\n", doc_id),
    )
    _mirror(tmp_path, rel, "Authored note (synthetic)", "normal")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app,
        ["backfill", "scan-secrets", "--action", "mark-confidential", "--apply"],
    )

    assert result.exit_code == 0, result.stdout
    db_tier = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s::uuid", (doc_id,)
    ).fetchone()
    assert db_tier is not None and db_tier[0] == CONFIDENTIAL, result.stdout
    assert "sensitivity: confidential" in (tmp_path / rel).read_text(
        encoding="utf-8"
    )
