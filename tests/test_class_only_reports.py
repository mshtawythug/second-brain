"""RM-29 (H-6): terminal error reports name the failure's class, never its message.

These reports used to quote an exception's text straight to the terminal:

- ``brain vault export``: ``summary.errors`` carried ``f"{relative}: {e}"``;
- ``brain backfill scan-secrets --action redact``: ``_apply_redact`` recorded
  ``str(exc)``.
- ``brain backfill scan-secrets --action mark-confidential``: three of
  ``vault/sensitivity_propagate.py``'s ``PropagationFailure`` messages, which that
  action writes into the same report, interpolated ``{exc}``.

An exception message can carry document-derived text, so each report now goes
through :func:`brain.errors.describe_failure`. That keeps the content-free
detail an operator needs: an ``OSError`` still shows its errno (``ENOSPC``).
The hash collision that ``_apply_redact`` documents gets a fixed sentence.

Every message below is a SENTINEL: a string that no code path would produce
by itself. If it shows up in a report, the report quoted the exception.
"""
from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app, backfill_app
from brain.cli_registry import register_backfill_sensitivity
from brain.cli_sensitivity import REDACT_COLLISION_MESSAGE
from brain.ingest import ExtractedDoc, ingest_document
from brain.ingest.guard import redact_secrets
from brain.vault.export import export_vault
from tests.conftest import FakeEmbedder

_SENTINEL = "RM29-SENTINEL-do-not-print-7f3c"

# Synthetic credential shape: valid form, invalid value. This is the same
# convention as tests/test_backfill_scan_secrets.py.
_FAKE_AWS_KEY = "AKIA" + "B" * 16


def _seed(conn: psycopg.Connection[Any], *, title: str, content: str) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=title,
        # Store the body verbatim, as the pre-F4 corpus that the sweep cleans
        # up was stored.
        secret_guard="off",
    )
    assert result.document_id is not None
    return result.document_id


def _sentinel_oserror(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError(errno.ENOSPC, _SENTINEL, f"/tmp/{_SENTINEL}")


def _sentinel_valueerror(*_args: Any, **_kwargs: Any) -> Any:
    raise ValueError(_SENTINEL)


# --------------------------------------------------------------------------
# vault export (AC1, AC3)
# --------------------------------------------------------------------------


def test_export_summary_never_quotes_the_exception_message(
    test_db: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 + AC3: the summary drops the message, and keeps the errno class."""
    _seed(test_db, title="Synthetic export one", content="plain body one")
    monkeypatch.setattr("brain.vault.export._write_doc_file", _sentinel_oserror)

    summary = export_vault(test_db, vault_path=tmp_path / "vault")

    assert len(summary.errors) == 1
    (error,) = summary.errors
    assert _SENTINEL not in error
    assert error.endswith(": OSError[ENOSPC]"), error


def test_cli_vault_export_report_never_quotes_the_exception_message(
    test_db: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 + AC3 through the terminal: the line the user reads, and the exit."""
    _seed(test_db, title="Synthetic export two", content="plain body two")
    monkeypatch.setattr("brain.vault.export._write_doc_file", _sentinel_oserror)

    result = CliRunner().invoke(
        app, ["vault", "export", "--to", str(tmp_path / "vault")]
    )

    assert result.exit_code == 1, result.output
    assert _SENTINEL not in result.output
    assert "OSError[ENOSPC]" in result.output
    assert "errors 1" in result.output


# --------------------------------------------------------------------------
# backfill scan-secrets --action redact (AC2)
# --------------------------------------------------------------------------


@pytest.fixture
def _scan_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach ``scan-secrets`` and route the redact path to a fake embedder.

    This is the same setup ``tests/test_backfill_scan_secrets.py`` uses, and for
    the same reasons.
    """
    register_backfill_sensitivity(backfill_app)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())


def _redact(*extra: str) -> Any:
    return CliRunner().invoke(
        app, ["backfill", "scan-secrets", "--apply", "--action", "redact", *extra]
    )


@pytest.mark.usefixtures("_scan_secrets")
def test_redact_report_never_quotes_the_exception_message(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a ValueError that is not a collision is reported by class only."""
    _seed(test_db, title="Synthetic redact one", content=f"key {_FAKE_AWS_KEY} here\n")
    monkeypatch.setattr("brain.cli_sensitivity.update_document", _sentinel_valueerror)

    text = _redact()
    as_json = _redact("--json")

    assert text.exit_code == 0, text.output
    assert _SENTINEL not in text.output
    assert "ValueError" in text.output
    assert as_json.exit_code == 0, as_json.output
    errors = json.loads(as_json.output)["errors"]
    assert [e["error"] for e in errors] == ["ValueError"]


@pytest.mark.usefixtures("_scan_secrets")
def test_redact_hash_collision_gets_the_fixed_sentence(
    test_db: psycopg.Connection[Any],
) -> None:
    """AC2, the documented case: the collision is a fixed sentence.

    ``update_document``'s message names the other document's id. The report
    replaces it with a sentence that names the cause and quotes nothing.
    """
    dirty = f"Synthetic runbook.\n\nkey {_FAKE_AWS_KEY} here\n"
    redacted, _ = redact_secrets(dirty)
    assert redacted != dirty, "the fixture must contain a secret the sweep redacts"
    dirty_id = _seed(test_db, title="Synthetic redact dirty", content=dirty)
    twin_id = _seed(test_db, title="Synthetic redact twin", content=redacted)

    text = _redact()
    as_json = _redact("--json")

    assert text.exit_code == 0, text.output
    assert REDACT_COLLISION_MESSAGE in text.output
    assert twin_id not in text.output
    assert "collides with existing document" not in text.output
    errors = json.loads(as_json.output)["errors"]
    assert errors == [{"id": dirty_id, "error": REDACT_COLLISION_MESSAGE}]


def test_redact_collision_message_is_the_documented_sentence() -> None:
    """The ticket fixes the wording. A reworded constant should fail here."""
    assert REDACT_COLLISION_MESSAGE == (
        "redacted body collides with another document's content hash"
    )


# --------------------------------------------------------------------------
# backfill scan-secrets --action mark-confidential (AC2, the propagation path)
# --------------------------------------------------------------------------


def _sentinel_psycopg_error(*_args: Any, **_kwargs: Any) -> Any:
    raise psycopg.OperationalError(_SENTINEL)


@pytest.mark.usefixtures("_scan_secrets")
def test_mark_confidential_report_never_quotes_the_exception_message(
    test_db: psycopg.Connection[Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 through the other writer of the same report: every propagation stage.

    ``_apply_mark_confidential`` records each ``PropagationFailure.message``.
    One ingested-tier mirror fails to regenerate, one vault-tier file fails its
    frontmatter rewrite, and both fence refreshes fail. That reaches each of
    ``propagate_sensitivity_to_vault``'s three ``except`` sites.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    ingested = _seed(
        test_db, title="Synthetic mark ingested", content=f"key {_FAKE_AWS_KEY} one\n"
    )
    vault_note = _seed(
        test_db, title="Synthetic mark vault", content=f"key {_FAKE_AWS_KEY} two\n"
    )
    test_db.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s",
        ("_ingested/manual/synthetic-mark-ingested.md", ingested),
    )
    test_db.execute(
        "UPDATE documents SET vault_path = %s, kind = 'vault' WHERE id = %s",
        ("notes/synthetic-mark-vault.md", vault_note),
    )
    target = "brain.vault.sensitivity_propagate"
    monkeypatch.setattr(f"{target}.regenerate_vault_file", _sentinel_oserror)
    monkeypatch.setattr(f"{target}.rewrite_sensitivity", _sentinel_oserror)
    monkeypatch.setattr(f"{target}.refresh_fences_naming", _sentinel_psycopg_error)

    result = CliRunner().invoke(
        app,
        ["backfill", "scan-secrets", "--apply", "--action", "mark-confidential", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert _SENTINEL not in result.output
    errors = sorted(e["error"] for e in json.loads(result.output)["errors"])
    assert len(errors) == 4, errors
    assert sum("could not write sensitivity into" in e for e in errors) == 1
    assert sum("vault mirror write failed" in e for e in errors) == 1
    assert sum("could not refresh derived-link fences" in e for e in errors) == 2
    assert sum(": OSError[ENOSPC]." in e for e in errors) == 2
    assert sum(": OperationalError." in e for e in errors) == 2
