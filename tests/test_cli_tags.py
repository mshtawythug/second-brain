"""``brain tags`` (``brain.cli_tags``): list, rename, merge and suggest-merges, via the real CLI.

Real-DB tests on the 5434 test database, with vault files under ``tmp_path``. Covers RM-21's
acceptance criteria 1 (dry-run writes nothing; ``--apply`` updates the row, ``chunks.tags_text``
and the file of both tiers; a second run is a no-op), 2 (search after a rename), 3 (merge unions
in order, without duplicates), and the saved-search rewrite. All tags, titles and bodies are
synthetic.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

runner = CliRunner()


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(root))
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    return root


def _seed(
    conn: psycopg.Connection[Any],
    embedder: object,
    vault: Path,
    *,
    title: str,
    body: str,
    tags: list[str],
    vault_path: str | None = None,
    kind: str = "vault",
) -> str:
    """Ingest a document with chunks, then point it at a vault file carrying the same tags."""
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title, content=body, content_type="note", source_path=None, metadata={}
        ),
        source_kind="manual",
        tags=tags,
    )
    assert result.document_id is not None
    if vault_path is not None:
        target = vault / vault_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            dump_frontmatter({"title": title, "tags": tags}, f"{body}\n"), encoding="utf-8"
        )
        conn.execute(
            "UPDATE documents SET vault_path = %s, kind = %s WHERE id = %s",
            (vault_path, kind, result.document_id),
        )
    return result.document_id


def _row(conn: psycopg.Connection[Any], doc_id: str) -> tuple[list[str], set[str], Any]:
    """The document's tags, its chunks' distinct ``tags_text``, and its ``updated_at``."""
    tags, updated_at = conn.execute(
        "SELECT tags, updated_at FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    texts = {
        r[0]
        for r in conn.execute(
            "SELECT tags_text FROM chunks WHERE document_id = %s", (doc_id,)
        ).fetchall()
    }
    return list(tags), texts, updated_at


def _file_tags(path: Path) -> list[str]:
    fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return list(fields.get("tags") or [])


def _add_watch(conn: psycopg.Connection[Any], name: str, filters: dict[str, str]) -> None:
    conn.execute(
        "INSERT INTO saved_searches (name, query, filters) VALUES (%s, 'q', %s::jsonb)",
        (name, json.dumps(filters)),
    )


def _watch_filters(conn: psycopg.Connection[Any], name: str) -> dict[str, str]:
    return dict(
        conn.execute("SELECT filters FROM saved_searches WHERE name = %s", (name,)).fetchone()[0]
    )


def test_tags_is_listed_in_brain_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    # The command row itself (Rich's panel prints "│ tags   <help>"), not any "tags" substring.
    assert re.search(r"^│ tags\s", result.stdout, re.MULTILINE), result.stdout


# --- AC1: rename dry-run, apply on both tiers, re-run ---------------------------------------


def test_rename_dry_run_lists_documents_and_writes_nothing(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    doc = _seed(
        test_db, fake_embedder, vault, title="Orbit plan", body="zephyr routine",
        tags=["nebula", "ops"], vault_path="notes/orbit.md",
    )
    _add_watch(test_db, "w", {"tag": "nebula"})
    before = _row(test_db, doc)
    file_before = (vault / "notes/orbit.md").read_bytes()

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar"])

    assert result.exit_code == 0, result.output
    assert "nebula → quasar" in result.stdout
    assert doc[:8] in result.stdout and "Orbit plan" in result.stdout
    assert "watch 'w': tag nebula → quasar" in result.stdout
    assert "nothing written" in result.stdout
    assert _row(test_db, doc) == before
    assert (vault / "notes/orbit.md").read_bytes() == file_before
    assert _watch_filters(test_db, "w") == {"tag": "nebula"}


def test_rename_apply_updates_row_chunks_and_files_of_both_tiers_then_is_a_no_op(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    note = _seed(
        test_db, fake_embedder, vault, title="Vault note", body="zephyr routine",
        tags=["nebula", "ops"], vault_path="notes/vault-note.md", kind="vault",
    )
    mirror = _seed(
        test_db, fake_embedder, vault, title="Ingested mirror", body="zephyr minutes",
        tags=["ops", "nebula"], vault_path="_ingested/manual/mirror.md", kind="ingested",
    )
    other = _seed(
        test_db, fake_embedder, vault, title="Untouched", body="zephyr other",
        tags=["ops"],
    )
    # Raw, un-normalized tags (as an old row may hold): a rename must leave them alone.
    test_db.execute("UPDATE documents SET tags = %s WHERE id = %s", (["Ops Team"], other))
    updated_before = {d: _row(test_db, d)[2] for d in (note, mirror)}

    applied = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])
    rerun = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert applied.exit_code == 0, applied.output
    assert "retagged 2 doc(s), rewrote 2 file(s), 0 file-missing skipped" in applied.stdout
    for doc, expected, rel in (
        (note, ["quasar", "ops"], "notes/vault-note.md"),
        (mirror, ["ops", "quasar"], "_ingested/manual/mirror.md"),
    ):
        tags, texts, updated_at = _row(test_db, doc)
        assert tags == expected
        assert texts == {" ".join(expected)}
        assert _file_tags(vault / rel) == expected
        assert updated_at == updated_before[doc]  # tag maintenance is not new knowledge
    # A rename touches only documents carrying the old tag; it does not normalize others.
    assert _row(test_db, other)[0] == ["Ops Team"]
    assert rerun.exit_code == 0, rerun.output
    assert "retagged 0 doc(s), rewrote 0 file(s)" in rerun.stdout


def test_rename_apply_warns_on_a_missing_file_and_still_updates_the_row(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    doc = _seed(
        test_db, fake_embedder, vault, title="Gone", body="zephyr", tags=["nebula"],
        vault_path="notes/gone.md",
    )
    (vault / "notes/gone.md").unlink()

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 0, result.output
    assert "1 file-missing skipped" in result.stdout
    assert "file missing on disk" in result.stderr
    assert _row(test_db, doc)[0] == ["quasar"]


def test_rename_rewrites_saved_search_filters_and_keeps_other_keys(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    _add_watch(test_db, "inc", {"tag": "nebula", "source": "krisp"})
    _add_watch(test_db, "exc", {"without_tag": "nebula"})
    _add_watch(test_db, "else", {"tag": "ops"})

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])
    rerun = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 0, result.output
    assert "updated 2 watch filter(s)" in result.stdout
    assert _watch_filters(test_db, "inc") == {"tag": "quasar", "source": "krisp"}
    assert _watch_filters(test_db, "exc") == {"without_tag": "quasar"}
    assert _watch_filters(test_db, "else") == {"tag": "ops"}
    assert "updated 0 watch filter(s)" in rerun.stdout


@pytest.mark.parametrize(
    "args",
    [["rename", "nebula", "Nebula"], ["rename", "__", "quasar"], ["merge", "a", "--into", "a"]],
)
def test_a_no_op_or_empty_request_is_refused(
    test_db: psycopg.Connection[Any], vault: Path, args: list[str]
) -> None:
    result = runner.invoke(app, ["tags", *args, "--apply"])

    assert result.exit_code == 1
    assert result.stderr.strip()


# --- AC2: search after a rename ---------------------------------------------------------------


def test_search_finds_the_new_tag_and_not_the_old_after_a_rename(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    _seed(
        test_db, fake_embedder, vault, title="Orbit plan", body="zephyr routine",
        tags=["nebula"],
    )
    runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    by_new_tag = runner.invoke(app, ["search", "zephyr", "--tag", "quasar", "--fts-only"])
    by_old_tag = runner.invoke(app, ["search", "zephyr", "--tag", "nebula", "--fts-only"])
    by_text = runner.invoke(app, ["search", "quasar", "--fts-only"])
    by_old_text = runner.invoke(app, ["search", "nebula", "--fts-only"])

    assert "Orbit plan" in by_new_tag.stdout, by_new_tag.output
    assert "Orbit plan" not in by_old_tag.stdout
    assert "Orbit plan" in by_text.stdout, by_text.output
    assert "Orbit plan" not in by_old_text.stdout


# --- AC3: merge ---------------------------------------------------------------------------------


def test_merge_unions_without_duplicates_and_preserves_order(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    doc = _seed(
        test_db, fake_embedder, vault, title="Mixed", body="zephyr",
        tags=["alpha", "widgets", "beta", "widget", "gizmo"], vault_path="notes/mixed.md",
    )
    lone = _seed(test_db, fake_embedder, vault, title="Lone", body="zephyr", tags=["gizmo"])

    result = runner.invoke(
        app, ["tags", "merge", "widgets", "gizmo", "--into", "widget", "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert "widgets → widget" in result.stdout and "gizmo → widget" in result.stdout
    assert _row(test_db, doc)[0] == ["alpha", "widget", "beta"]
    assert _file_tags(vault / "notes/mixed.md") == ["alpha", "widget", "beta"]
    assert _row(test_db, lone)[0] == ["widget"]


def test_merge_into_one_of_its_sources(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    doc = _seed(test_db, fake_embedder, vault, title="M", body="zephyr", tags=["b", "a"])

    result = runner.invoke(app, ["tags", "merge", "a", "b", "--into", "a", "--apply"])

    assert result.exit_code == 0, result.output
    assert _row(test_db, doc)[0] == ["a"]


# --- list and suggest-merges -------------------------------------------------------------------


def test_list_counts_every_tag_most_used_first(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, vault, title="One", body="zephyr one", tags=["ops", "alpha"])
    _seed(test_db, fake_embedder, vault, title="Two", body="zephyr two", tags=["ops"])

    as_json = runner.invoke(app, ["tags", "list", "--json"])
    as_text = runner.invoke(app, ["tags", "list"])

    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout) == [
        {"tag": "ops", "count": 2},
        {"tag": "alpha", "count": 1},
    ]
    assert as_text.stdout.splitlines() == ["2  ops", "1  alpha"]


def test_suggest_merges_reports_a_plural_and_a_co_occurrence_pair_with_evidence(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    for i in range(3):
        _seed(
            test_db, fake_embedder, vault, title=f"Co {i}", body=f"zephyr co {i}",
            tags=["roadmap", "planning", "widget"] if i < 2 else ["roadmap", "planning"],
        )
    _seed(test_db, fake_embedder, vault, title="Plural", body="zephyr p", tags=["widgets"])

    as_json = runner.invoke(app, ["tags", "suggest-merges", "--json"])
    as_text = runner.invoke(app, ["tags", "suggest-merges"])

    assert as_json.exit_code == 0, as_json.output
    by_pair = {(s["keep"], s["merge"]): s for s in json.loads(as_json.stdout)}
    plural = by_pair[("widget", "widgets")]
    assert "singular-plural" in plural["reasons"]
    assert (plural["keep_count"], plural["merge_count"]) == (2, 1)
    assert plural["ratio"] >= 0.85
    co = by_pair[("roadmap", "planning")]  # tied counts: the shorter tag is kept
    assert co["reasons"] == ["co-occurrence"]
    assert (co["overlap"], co["shared"]) == (1.0, 3)
    assert "brain tags merge widgets --into widget" in as_text.stdout


def test_suggest_merges_with_nothing_to_suggest(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    result = runner.invoke(app, ["tags", "suggest-merges"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "no merge suggestions\n"


def test_a_watch_filter_that_is_not_a_string_is_left_alone(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    test_db.execute(
        "INSERT INTO saved_searches (name, query, filters) VALUES ('odd', 'q', %s::jsonb)",
        ('{"tag": ["nebula"]}',),
    )

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 0, result.output
    assert test_db.execute(
        "SELECT filters FROM saved_searches WHERE name = 'odd'"
    ).fetchone()[0] == {"tag": ["nebula"]}


def test_a_bad_config_is_a_clean_exit_1(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("BRAIN_EMBEDDER", "bogus")

    result = runner.invoke(app, ["tags", "list"])

    assert result.exit_code == 1
    assert "Configuration error" in result.output


@pytest.mark.fresh_schema
def test_rename_without_migration_029_still_retags_documents(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    test_db.execute("DROP TABLE saved_search_hits")
    test_db.execute("DROP TABLE saved_searches")
    doc = _seed(test_db, fake_embedder, vault, title="Old", body="zephyr", tags=["nebula"])

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 0, result.output
    assert "updated 0 watch filter(s)" in result.stdout
    assert _row(test_db, doc)[0] == ["quasar"]


# --- round 1: a bad file is a per-document failure, and paths stay in the vault ----------------

#: A token that appears only inside the broken file. A YAML error message quotes the offending
#: line, so finding it in the output would mean file content leaked into the report.
_CANARY = "canary-77f3"


def _break_frontmatter(path: Path) -> None:
    path.write_text(
        f'---\ntitle: Broken\ntags:\n- nebula\nnote: "unterminated {_CANARY}\n---\nbody\n',
        encoding="utf-8",
    )


def test_a_bad_vault_file_is_reported_class_only_and_everything_else_is_rewritten(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object
) -> None:
    good_a = _seed(
        test_db, fake_embedder, vault, title="Good A", body="zephyr a", tags=["nebula"],
        vault_path="notes/good-a.md",
    )
    bad = _seed(
        test_db, fake_embedder, vault, title="Bad", body="zephyr bad", tags=["nebula"],
        vault_path="notes/bad.md",
    )
    good_c = _seed(
        test_db, fake_embedder, vault, title="Good C", body="zephyr c", tags=["nebula"],
        vault_path="_ingested/manual/good-c.md", kind="ingested",
    )
    _break_frontmatter(vault / "notes/bad.md")
    _add_watch(test_db, "w", {"tag": "nebula"})

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert _CANARY not in result.output
    assert f"{bad[:8]} (notes/bad.md)" in result.stderr
    assert "Error" in result.stderr  # the class, e.g. ScannerError[4:7]
    assert "1 file(s) failed" in result.stdout
    for doc, rel in ((good_a, "notes/good-a.md"), (good_c, "_ingested/manual/good-c.md")):
        assert _row(test_db, doc)[0] == ["quasar"]
        assert _file_tags(vault / rel) == ["quasar"]
    assert _row(test_db, bad)[0] == ["quasar"]
    assert _watch_filters(test_db, "w") == {"tag": "quasar"}


def test_a_vault_path_outside_the_vault_is_refused_and_left_untouched(
    test_db: psycopg.Connection[Any], vault: Path, fake_embedder: object, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere" / "victim.md"
    outside.parent.mkdir()
    outside.write_text(dump_frontmatter({"title": "V", "tags": ["nebula"]}, "v\n"), "utf-8")
    before = outside.read_bytes()
    doc = _seed(test_db, fake_embedder, vault, title="Escapes", body="zephyr", tags=["nebula"])
    test_db.execute("UPDATE documents SET vault_path = %s WHERE id = %s", (str(outside), doc))

    result = runner.invoke(app, ["tags", "rename", "nebula", "quasar", "--apply"])

    assert result.exit_code == 1, result.output
    assert outside.read_bytes() == before
    assert "VaultPathEscape" in result.stderr
    assert "1 file(s) failed" in result.stdout
