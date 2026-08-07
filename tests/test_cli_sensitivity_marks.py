"""``brain mark-confidential`` / ``mark-normal`` / ``list --sensitivity`` (F6, task #17).

``mark-normal`` carries more weight than its size suggests. Re-ingest is
deliberately **escalate-only** — it can raise a document's tier but never
lower it, so a `vault sync --watch` pass cannot silently reset a confidential
document back to normal and let the next hosted ingest ship the body out.
That ratchet makes this command the *only* sanctioned downgrade path: without
it, a document marked confidential by accident could never be un-marked.

Both commands must also regenerate the vault mirror. Skipping that would leave
stale frontmatter on disk that the next sync reads back, silently reverting
the column — the same resurrect-after-delete failure ``brain rm`` guards
against.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import sync_vault


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


@pytest.fixture
def seeded(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    """A synced vault-tier note. Returns (vault, document_id)."""
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(
        vault / "comp-review.md",
        {"title": "Comp Review"},
        "Compensation bands for the platform group.\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = 'Comp Review'"
    ).fetchone()
    assert row is not None
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    return vault, str(row[0])


def _sensitivity(conn: psycopg.Connection[Any], doc_id: str) -> str:
    row = conn.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# mark-confidential
# ---------------------------------------------------------------------------


def test_mark_confidential_sets_the_column(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    _vault, doc_id = seeded

    result = CliRunner().invoke(app, ["mark-confidential", doc_id])

    assert result.exit_code == 0, result.output
    assert f"marked {doc_id[:8]} as confidential" in result.output
    assert _sensitivity(test_db, doc_id) == "confidential"


def test_mark_confidential_is_idempotent(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    _vault, doc_id = seeded
    CliRunner().invoke(app, ["mark-confidential", doc_id])

    result = CliRunner().invoke(app, ["mark-confidential", doc_id])

    assert result.exit_code == 0, result.output
    assert f"{doc_id[:8]} is already confidential" in result.output
    assert _sensitivity(test_db, doc_id) == "confidential"


def test_mark_confidential_bumps_updated_at(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """A trust-tier change IS a change to the user's knowledge — F9 obligation."""
    _vault, doc_id = seeded
    before = test_db.execute(
        "SELECT updated_at FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert before is not None

    result = CliRunner().invoke(app, ["mark-confidential", doc_id])
    assert result.exit_code == 0, result.output

    after = test_db.execute(
        "SELECT updated_at FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert after is not None
    assert after[0] > before[0]


def test_vault_tier_note_gets_the_frontmatter_written_not_regenerated(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """Vault-tier authored notes are file-source-of-truth, in both directions.

    ``regenerate_vault_file`` refuses them (it could discard un-synced edits),
    so the mark rewrites exactly one frontmatter field and leaves the body
    byte-identical. Writing it is mandatory, not cosmetic: sync reads the tier
    back off the frontmatter on every pass, so a DB-only mark would revert.
    """
    vault, doc_id = seeded
    note = vault / "comp-review.md"
    _fields_before, body_before = parse_frontmatter(note.read_text())

    result = CliRunner().invoke(app, ["mark-confidential", doc_id])

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, doc_id) == "confidential"
    fields_after, body_after = parse_frontmatter(note.read_text())
    assert fields_after["sensitivity"] == "confidential"
    assert fields_after["title"] == "Comp Review", "other keys preserved"
    assert body_after == body_before, "the body must be untouched"


def test_ingested_tier_mirror_is_regenerated(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """An ingested doc's mirror IS DB-derived, so its frontmatter must update."""
    vault, _vault_doc_id = seeded
    mirror_rel = "_ingested/manual/synthetic-import.md"
    path = vault / mirror_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, {"title": "Synthetic Import"}, "imported body\n")
    row = test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_type, kind, content_hash, vault_path) "
        "VALUES ('Synthetic Import', 'imported body\n', 'note', 'ingested', "
        "'w4-cli-sens-ing', %s) RETURNING id::text",
        (mirror_rel,),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])

    result = CliRunner().invoke(app, ["mark-confidential", doc_id])

    assert result.exit_code == 0, result.output
    fields, _body = parse_frontmatter(path.read_text())
    assert fields.get("sensitivity") == "confidential"


def test_the_mark_survives_a_resync(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str], fake_embedder: Any
) -> None:
    """The escalate-only ratchet, observed end-to-end.

    A `vault sync` pass after marking must not reset the tier — that regression
    would let the next hosted ingest ship a confidential body outward.
    """
    vault, doc_id = seeded
    CliRunner().invoke(app, ["mark-confidential", doc_id])

    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    assert _sensitivity(test_db, doc_id) == "confidential"


# ---------------------------------------------------------------------------
# mark-normal — the only sanctioned downgrade
# ---------------------------------------------------------------------------


def test_mark_normal_downgrades(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    _vault, doc_id = seeded
    CliRunner().invoke(app, ["mark-confidential", doc_id])

    result = CliRunner().invoke(app, ["mark-normal", doc_id])

    assert result.exit_code == 0, result.output
    assert f"marked {doc_id[:8]} as normal" in result.output
    assert _sensitivity(test_db, doc_id) == "normal"


def test_mark_normal_is_the_inverse_and_round_trips(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """Without this, an accidental mark would be permanent."""
    _vault, doc_id = seeded

    CliRunner().invoke(app, ["mark-confidential", doc_id])
    assert _sensitivity(test_db, doc_id) == "confidential"
    CliRunner().invoke(app, ["mark-normal", doc_id])
    assert _sensitivity(test_db, doc_id) == "normal"
    CliRunner().invoke(app, ["mark-confidential", doc_id])
    assert _sensitivity(test_db, doc_id) == "confidential"


def test_mark_normal_is_idempotent(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    _vault, doc_id = seeded

    result = CliRunner().invoke(app, ["mark-normal", doc_id])

    assert result.exit_code == 0, result.output
    assert f"{doc_id[:8]} is already normal" in result.output


def test_mark_normal_writes_the_downgrade_to_disk_too(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """The downgrade must persist to the file, or sync would re-escalate it.

    Written explicitly as ``sensitivity: normal`` rather than by deleting the
    key: both resolve identically today, but an explicit value records the
    user's intent and survives a change to the default.
    """
    vault, doc_id = seeded
    note = vault / "comp-review.md"
    CliRunner().invoke(app, ["mark-confidential", doc_id])

    result = CliRunner().invoke(app, ["mark-normal", doc_id])

    assert result.exit_code == 0, result.output
    assert _sensitivity(test_db, doc_id) == "normal"
    fields, _body = parse_frontmatter(note.read_text())
    assert fields["sensitivity"] == "normal"


def test_the_downgrade_survives_a_resync(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str], fake_embedder: Any
) -> None:
    """Round-trip closure: neither direction is undone by a sync pass."""
    vault, doc_id = seeded
    CliRunner().invoke(app, ["mark-confidential", doc_id])
    CliRunner().invoke(app, ["mark-normal", doc_id])

    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    assert _sensitivity(test_db, doc_id) == "normal"


# ---------------------------------------------------------------------------
# list --sensitivity
# ---------------------------------------------------------------------------


def test_list_filters_by_sensitivity(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    _vault, doc_id = seeded
    test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Ordinary Note', 'body', 'note', 'vault', 'w4-cli-sens-1')"
    )
    CliRunner().invoke(app, ["mark-confidential", doc_id])

    result = CliRunner().invoke(app, ["list", "--sensitivity", "confidential"])

    assert result.exit_code == 0, result.output
    assert "Comp Review" in result.output
    assert "Ordinary Note" not in result.output


def test_list_json_always_carries_sensitivity(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """Always present, always a string — never inferred from a missing key."""
    _vault, _doc_id = seeded

    result = CliRunner().invoke(app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload
    assert all(row["sensitivity"] in {"normal", "confidential"} for row in payload)


def test_list_rejects_an_unknown_sensitivity(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    """A typo must be a usage error, not a silent empty list.

    An empty result reads as "nothing is marked confidential" — the most
    dangerous possible wrong answer for a confidentiality filter.
    """
    result = CliRunner().invoke(app, ["list", "--sensitivity", "confidental"])

    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "confidental" in combined


def test_mark_rejects_an_unknown_id(
    test_db: psycopg.Connection[Any], seeded: tuple[Path, str]
) -> None:
    result = CliRunner().invoke(app, ["mark-confidential", "deadbeefdeadbeef"])

    assert result.exit_code != 0
