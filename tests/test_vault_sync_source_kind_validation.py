"""The vault-sync ``source:`` frontmatter boundary is closed over the kind set.

``sources.kind`` is bare ``TEXT NOT NULL`` with no CHECK (``001_init.sql``), so
the database accepts any string, while every *read* surface treats the column as
a closed enum of four values (:data:`brain.source_kinds.VALID_SOURCE_KINDS`).
``brain.vault.sync`` was the third and last write boundary that let an
unvalidated caller-supplied string reach that column — here from a hand-authored
Markdown file's frontmatter rather than from a CLI flag or an MCP argument.

These tests pin BOTH halves of the fix, because either half alone is a defect:

* an unknown kind must NOT produce a ``sources`` row (the count/click divergence
  ``brain.facets.SOURCE_NONE_BUCKET`` documents), and
* the document must still sync (a metadata typo must not cost the user the
  note's body, and must not abort a whole-tree walk).
"""
import uuid
from pathlib import Path

import psycopg
import pytest

from brain.facets import SOURCE_NONE_BUCKET
from brain.source_kinds import VALID_SOURCE_KINDS
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import SyncReport, _source_from_frontmatter, sync_vault

# ---------------------------------------------------------------------------
# Helpers — mirror tests/test_vault_sync.py so the two files read alike.
# ---------------------------------------------------------------------------


def _write(path: Path, frontmatter: dict, body: str) -> None:
    """Write a vault file with the given frontmatter + body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(frontmatter, body))


def _sync(conn: psycopg.Connection, fake_embedder, vault: Path, **kwargs) -> SyncReport:
    return sync_vault(conn, embedder=fake_embedder, vault_path=vault, **kwargs)


def _author_ingested(vault: Path, *, source: str, external_id: str) -> str:
    """Hand-author one ``_ingested/`` file carrying ``source:``. Returns its id."""
    doc_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "misc" / f"{external_id}.md",
        {
            "id": doc_id,
            "title": f"Authored {external_id}",
            "kind": "ingested",
            "source": source,
            "external_id": external_id,
        },
        # Bodies must differ per file: ingested-tier keeps the
        # ``UNIQUE(content_hash)`` constraint (migration 004 relaxed it for
        # vault-tier only), so two identical bodies collide as mirror drift.
        f"synthetic body for {external_id}\n",
    )
    return doc_id


def _source_kinds(conn: psycopg.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT kind FROM sources").fetchall()}


def _source_id_of(conn: psycopg.Connection, doc_id: str) -> str | None:
    row = conn.execute(
        "SELECT source_id::text FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None, f"document {doc_id} was not synced at all"
    return row[0]


# ---------------------------------------------------------------------------
# Unit level — the boundary function itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(VALID_SOURCE_KINDS))
def test_valid_kind_passes_through_unchanged(kind: str) -> None:
    """Every member of the closed set survives the boundary verbatim."""
    assert _source_from_frontmatter({"source": kind}, "ingested", path="f.md") == kind


@pytest.mark.parametrize("kind", ["notion", "none", "Krisp", "", "  ", "manual x"])
def test_unknown_kind_is_dropped_to_none(kind: str) -> None:
    """Anything outside the set degrades to ``None`` — never to a substitute.

    ``'none'`` is called out explicitly: it is the literal string
    :data:`brain.facets.SOURCE_NONE_BUCKET` uses for source-LESS documents, so a
    ``sources`` row with that kind is the exact value that makes the facet count
    and the facet click disagree.
    """
    assert _source_from_frontmatter({"source": kind}, "ingested", path="f.md") is None


def test_the_none_bucket_sentinel_is_specifically_rejected() -> None:
    """Pin the facet-breaking value against a rename of the constant."""
    assert SOURCE_NONE_BUCKET not in VALID_SOURCE_KINDS
    assert (
        _source_from_frontmatter(
            {"source": SOURCE_NONE_BUCKET}, "ingested", path="f.md"
        )
        is None
    )


def test_surrounding_whitespace_is_stripped_before_validation() -> None:
    """``source: manual `` is a legal kind, not an unknown one.

    The ``.strip()`` predates the guard and runs first by design: YAML trailing
    whitespace is a typo in the *file format*, not a claim about provenance, and
    failing it would drop a source the user correctly named. Pinned because the
    ordering is invisible at the call site — swap the two and every quoted
    frontmatter value with a stray space silently loses its source row.
    """
    assert _source_from_frontmatter({"source": " krisp "}, "ingested", path="f.md") == (
        "krisp"
    )


def test_vault_tier_still_ignores_source_entirely() -> None:
    """The pre-existing tier short-circuit is unchanged — even for a valid kind."""
    assert _source_from_frontmatter({"source": "krisp"}, "vault", path="f.md") is None


def test_missing_and_non_string_source_still_return_none() -> None:
    """The pre-existing degradations are untouched by the new guard."""
    assert _source_from_frontmatter({}, "ingested", path="f.md") is None
    assert _source_from_frontmatter({"source": 7}, "ingested", path="f.md") is None
    assert _source_from_frontmatter({"source": None}, "ingested", path="f.md") is None


def test_unknown_kind_is_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A dropped source is loud: the file, the bad value, and the legal set."""
    with caplog.at_level("WARNING", logger="brain.vault.sync"):
        _source_from_frontmatter({"source": "notion"}, "ingested", path="a/b.md")
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "notion" in message
    assert "a/b.md" in message
    for kind in VALID_SOURCE_KINDS:
        assert kind in message


def test_valid_kind_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The guard is silent on the happy path — no warning fatigue on a big sync."""
    with caplog.at_level("WARNING", logger="brain.vault.sync"):
        _source_from_frontmatter({"source": "krisp"}, "ingested", path="a/b.md")
    assert caplog.records == []


# ---------------------------------------------------------------------------
# Integration — through the real sync engine and the real DB.
# ---------------------------------------------------------------------------


def test_unknown_source_kind_writes_no_sources_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """THE DEFECT. A hand-authored ``source: notion`` must not reach the column."""
    vault = tmp_path / "vault"
    doc_id = _author_ingested(vault, source="notion", external_id="n-1")

    report = _sync(test_db, fake_embedder, vault)

    assert report.created == 1
    assert _source_kinds(test_db) == set()
    assert _source_id_of(test_db, doc_id) is None


def test_source_kind_literally_none_writes_no_sources_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """The facet count/click divergence, closed at its last remaining source.

    With a ``sources`` row of kind ``'none'`` the document is COUNTED in the
    facet's ``none`` bucket (``coalesce(s.kind, 'none')``) but NOT returned by
    the click (``source_missing=True`` → ``d.source_id IS NULL``). Asserting
    ``source_id IS NULL`` is asserting the two agree.
    """
    vault = tmp_path / "vault"
    doc_id = _author_ingested(vault, source=SOURCE_NONE_BUCKET, external_id="x-1")

    _sync(test_db, fake_embedder, vault)

    assert _source_kinds(test_db) == set()
    assert _source_id_of(test_db, doc_id) is None


def test_unknown_source_kind_still_syncs_the_document(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """THE FAILURE MODE. A typo costs the source link, never the note.

    This is the half that rules out the ``_SyncError`` route: that route would
    put the file in ``report.errors``, which ``mcp_server`` turns into an
    ``INTERNAL_ERROR`` and ``cli_note`` into exit code 1 — a hard failure for a
    note whose body indexed perfectly well.
    """
    vault = tmp_path / "vault"
    doc_id = _author_ingested(vault, source="notion", external_id="n-2")

    report = _sync(test_db, fake_embedder, vault)

    assert report.errors == []
    assert report.created == 1
    row = test_db.execute(
        "SELECT title, content FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "Authored n-2"
    assert "synthetic body for n-2" in row[1]


def test_one_bad_file_does_not_stop_the_walk(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Scale argument: a bad note among good ones costs only its own source row."""
    vault = tmp_path / "vault"
    bad_id = _author_ingested(vault, source="notion", external_id="bad-1")
    good_id = _author_ingested(vault, source="krisp", external_id="good-1")

    report = _sync(test_db, fake_embedder, vault)

    assert report.errors == []
    assert report.created == 2
    assert _source_id_of(test_db, bad_id) is None
    assert _source_id_of(test_db, good_id) is not None
    assert _source_kinds(test_db) == {"krisp"}


@pytest.mark.parametrize("kind", sorted(VALID_SOURCE_KINDS))
def test_every_valid_kind_still_creates_its_sources_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, kind: str
) -> None:
    """The guard is not over-broad: all four legal kinds still round-trip."""
    vault = tmp_path / "vault"
    doc_id = _author_ingested(vault, source=kind, external_id=f"{kind}-ok")

    _sync(test_db, fake_embedder, vault)

    assert _source_kinds(test_db) == {kind}
    assert _source_id_of(test_db, doc_id) is not None


def test_unknown_kind_does_not_break_wiki_link_resolution_for_valid_ones(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``[[krisp:ok-1]]`` still resolves while a sibling file carries a bad kind.

    Also pins the converse the docstring's open-set promise could not deliver:
    ``[[notion:bad-3]]`` was never resolvable — ``brain.vault.links._SOURCE_KINDS``
    does not parse it as a source link — so the dropped ``sources`` row costs no
    link that ever worked.
    """
    vault = tmp_path / "vault"
    _author_ingested(vault, source="notion", external_id="bad-3")
    _author_ingested(vault, source="krisp", external_id="ok-1")
    _write(
        vault / "refs.md",
        {"id": str(uuid.uuid4()), "title": "Refs"},
        "see [[krisp:ok-1]] and [[notion:bad-3]]\n",
    )

    report = _sync(test_db, fake_embedder, vault)

    assert report.links_resolved == 1
    assert report.errors == []
