"""``brain backfill scan-secrets`` — the retroactive corpus secret sweep (F4/F6).

The F4 guard runs at INGEST time, so it protects everything ingested after it
shipped and nothing before. This command is the retroactive half.

The assertion that matters most here is
:func:`test_default_run_writes_absolutely_nothing`. This is the one command in
the release that can rewrite document bodies across the whole corpus, and its
safety rests entirely on two gates that must both hold: ``--apply`` is required
to write at all, and the default ``--action report`` cannot write *even with*
``--apply``. A single-flag bypass would be unrecoverable without a backup.

Registration note: ``cli.py`` carries the one line that attaches this command
to ``backfill_app`` (coordinator-applied). These tests register it onto the real
sub-app themselves via :func:`register_backfill_sensitivity`, so they exercise
the true command surface and do not silently pass before that line lands.

Every secret fixture below is SYNTHETIC — correct shape, invalid checksum. They
are the same shapes ``tests/test_ingest_guard.py`` uses.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app, backfill_app
from brain.cli_registry import register_backfill_sensitivity
from brain.ingest import ExtractedDoc, ingest_document
from brain.sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from tests.conftest import FakeEmbedder

# Synthetic credential shapes: valid FORM, invalid value. Same convention as the
# F4 guard suite, and they are why this module's fixtures are allowlisted for
# the repo's own pre-commit secret gate.
_FAKE_AWS_KEY = "AKIA" + "A" * 16
_FAKE_SLACK_TOKEN = "xoxb-" + "0" * 20

_CLEAN_BODY = (
    "Quarterly planning notes. The release workflow and the documentation "
    "backlog were reviewed at length by the team.\n"
)
_DIRTY_BODY = (
    "Deploy runbook.\n\n"
    f"Set the access key to {_FAKE_AWS_KEY} before running the job.\n"
    f"The bot token is {_FAKE_SLACK_TOKEN} for the notifier.\n"
)


@pytest.fixture(autouse=True)
def _register_command() -> None:
    """Attach ``scan-secrets`` to the real ``backfill`` sub-app.

    Idempotent in practice: Typer appends a command per call, and re-registering
    the same name within a session is harmless because invocation resolves by
    name. Doing it here rather than relying on ``cli.py`` means these tests
    describe the command's real surface today.
    """
    register_backfill_sensitivity(backfill_app)


@pytest.fixture(autouse=True)
def _local_embedder(monkeypatch: Any) -> None:
    """Route the redact path's embedder to a deterministic double.

    ``--action redact`` re-embeds through ``update_document``, and the shim in
    ``cli_sensitivity._build_embedder`` deliberately resolves via
    ``brain.cli._build_embedder`` so that patch point stays effective. Without
    this the sweep would try to reach a live Ollama, which the suite forbids.

    ``monkeypatch`` (auto-reverting) rather than reopening a production module —
    permitted by CLAUDE.md rule 13.
    """
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())


def _seed(
    conn: psycopg.Connection[Any], *, title: str, body: str, level: str = DEFAULT_SENSITIVITY
) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            # Fold the title into the body so each seeded document hashes
            # differently. These are stdin-shaped docs (``source_path=None``),
            # so ``ingest_document`` rule 4 dedups them by content hash — four
            # notes sharing one body would collapse into a single row and the
            # assertions would silently describe the first.
            content=f"{title}\n\n{body}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=title,
        sensitivity=level,
        # The guard would otherwise REDACT the dirty body at ingest under a
        # non-default mode, leaving nothing for the sweep to find. `off` stores
        # the body verbatim, which is exactly the pre-F4 corpus this command
        # exists to clean up.
        secret_guard="off",
    )
    assert result.document_id is not None
    return result.document_id


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["backfill", "scan-secrets", *args])


def _sensitivities(conn: psycopg.Connection[Any]) -> dict[str, str]:
    rows = conn.execute("SELECT title, sensitivity FROM documents").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _bodies(conn: psycopg.Connection[Any]) -> dict[str, str]:
    rows = conn.execute("SELECT title, content FROM documents").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


# --------------------------------------------------------------------------
# The safety gates
# --------------------------------------------------------------------------


def test_default_run_writes_absolutely_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    """THE SAFETY ASSERTION: a bare run reports and mutates nothing.

    Asserted against the full before/after state of BOTH mutable columns rather
    than just checking the exit code, because "it printed a report" and "it did
    not write" are independent claims and only the second one is the guarantee.
    """
    _seed(test_db, title="Synthetic dirty one", body=_DIRTY_BODY)
    _seed(test_db, title="Synthetic clean one", body=_CLEAN_BODY)
    before_levels = _sensitivities(test_db)
    before_bodies = _bodies(test_db)

    result = _run()

    assert result.exit_code == 0, result.output
    assert "Synthetic dirty one" in result.output
    assert _sensitivities(test_db) == before_levels
    assert _bodies(test_db) == before_bodies


def test_apply_with_default_action_still_writes_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    """``--apply`` ALONE is not enough — ``--action report`` cannot write.

    Two independent gates, not one. A user reaching for ``--apply`` to "make it
    actually run" must still not mutate the corpus by accident; they have to
    name the action they want.
    """
    _seed(test_db, title="Synthetic dirty two", body=_DIRTY_BODY)
    before_levels = _sensitivities(test_db)
    before_bodies = _bodies(test_db)

    result = _run("--apply")

    assert result.exit_code == 0, result.output
    assert _sensitivities(test_db) == before_levels
    assert _bodies(test_db) == before_bodies


def test_unknown_action_is_a_usage_error(test_db: psycopg.Connection[Any]) -> None:
    """An unrecognized ``--action`` exits 2 before touching the database."""
    _seed(test_db, title="Synthetic dirty three", body=_DIRTY_BODY)

    result = _run("--apply", "--action", "delete-everything")

    assert result.exit_code == 2
    assert _sensitivities(test_db) == {"Synthetic dirty three": DEFAULT_SENSITIVITY}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_only_documents_with_findings_are_flagged(
    test_db: psycopg.Connection[Any],
) -> None:
    """Clean documents are scanned but not reported."""
    _seed(test_db, title="Synthetic dirty four", body=_DIRTY_BODY)
    _seed(test_db, title="Synthetic clean four", body=_CLEAN_BODY)

    result = _run("--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned"] == 2
    assert payload["flagged"] == 1
    assert payload["written"] == 0
    assert payload["applied"] is False
    assert [d["title"] for d in payload["documents"]] == ["Synthetic dirty four"]


def test_json_findings_never_contain_the_raw_secret(
    test_db: psycopg.Connection[Any],
) -> None:
    """THE LEAK GUARD: the report must not reproduce what it is reporting on.

    A sweep whose own output contains the credentials it found would move them
    from one document into the user's terminal scrollback, their shell history
    file, and any CI log that captured the run — turning a detection tool into
    an additional disclosure path.
    """
    _seed(test_db, title="Synthetic dirty five", body=_DIRTY_BODY)

    result = _run("--json")

    assert result.exit_code == 0, result.output
    assert _FAKE_AWS_KEY not in result.output
    assert _FAKE_SLACK_TOKEN not in result.output
    payload = json.loads(result.output)
    findings = payload["documents"][0]["findings"]
    # A control per SHAPE, not per test. This test denies two different
    # credentials over two different containers, and ``assert findings`` pins
    # only that SOME finding exists — so a scanner that detected the AWS key and
    # missed the Slack token entirely would satisfy every ``not in`` below while
    # the token sat unredacted in the corpus. Naming both kinds is what makes
    # the Slack half of each claim an assertion about a string that is really
    # there to be leaked.
    assert {f["kind"] for f in findings} == {"aws_access_key_id", "slack_token"}, (
        f"both seeded credential shapes must be DETECTED, or the Slack "
        f"assertions below pass vacuously: {findings}"
    )
    for finding in findings:
        assert _FAKE_AWS_KEY not in finding["preview"]
        assert _FAKE_SLACK_TOKEN not in finding["preview"]
        assert "*" in finding["preview"], "previews are masked"


def test_limit_bounds_the_scan(test_db: psycopg.Connection[Any]) -> None:
    """``--limit`` stops early, bounding wall-clock on a large corpus."""
    for n in range(4):
        _seed(test_db, title=f"Synthetic limited {n}", body=_DIRTY_BODY)

    result = _run("--json", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["scanned"] == 2


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


def test_mark_confidential_flips_only_the_hit_rows(
    test_db: psycopg.Connection[Any],
) -> None:
    """``--apply --action mark-confidential`` marks hits and leaves clean docs alone.

    The negative half is the point: a sweep that marked everything would be
    indistinguishable from one that worked, and would cripple retrieval on the
    whole corpus under a hosted embedder.
    """
    _seed(test_db, title="Synthetic dirty six", body=_DIRTY_BODY)
    _seed(test_db, title="Synthetic clean six", body=_CLEAN_BODY)

    result = _run("--apply", "--action", "mark-confidential")

    assert result.exit_code == 0, result.output
    assert _sensitivities(test_db) == {
        "Synthetic dirty six": CONFIDENTIAL,
        "Synthetic clean six": DEFAULT_SENSITIVITY,
    }
    # Bodies are untouched — marking is not redaction.
    assert _FAKE_AWS_KEY in _bodies(test_db)["Synthetic dirty six"]


def test_mark_confidential_is_idempotent(
    test_db: psycopg.Connection[Any],
) -> None:
    """A second pass reports ``0 written`` rather than re-counting old hits.

    ``set_document_sensitivity``'s ``WHERE sensitivity <> %s`` guard is what
    makes the count honest; without it the sweep would claim work it did not do
    every time it ran.
    """
    _seed(test_db, title="Synthetic dirty seven", body=_DIRTY_BODY)
    first = _run("--apply", "--action", "mark-confidential", "--json")
    assert json.loads(first.output)["written"] == 1

    second = _run("--apply", "--action", "mark-confidential", "--json")

    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["flagged"] == 1, "it is still a hit — the body still has secrets"
    assert payload["written"] == 0, "but nothing changed, so nothing was written"


def test_redact_removes_secrets_from_body_and_chunks(
    test_db: psycopg.Connection[Any],
) -> None:
    """``--apply --action redact`` rewrites the body AND the chunks.

    Chunks are asserted explicitly because they are the copy that search reads.
    Redacting only ``documents.content`` would leave every secret fully
    retrievable through ``brain search`` while the command reported success —
    which is why this path routes through ``update_document`` rather than a raw
    UPDATE.
    """
    doc_id = _seed(test_db, title="Synthetic dirty eight", body=_DIRTY_BODY)

    result = _run("--apply", "--action", "redact")

    assert result.exit_code == 0, result.output
    body = _bodies(test_db)["Synthetic dirty eight"]
    assert _FAKE_AWS_KEY not in body
    assert _FAKE_SLACK_TOKEN not in body
    assert "[REDACTED:" in body

    chunk_rows = test_db.execute(
        "SELECT content FROM chunks WHERE document_id = %s", (doc_id,)
    ).fetchall()
    assert chunk_rows, "the document must still be chunked after redaction"
    for (content,) in chunk_rows:
        assert _FAKE_AWS_KEY not in str(content)
        assert _FAKE_SLACK_TOKEN not in str(content)


def test_redact_is_idempotent(test_db: psycopg.Connection[Any]) -> None:
    """Re-running redact over an already-clean corpus writes nothing.

    Guards the ``redacted == doc.content`` short-circuit. Without it every run
    would re-write every previously-redacted document, re-embedding the whole
    corpus and bumping ``updated_at`` on rows nobody edited.
    """
    _seed(test_db, title="Synthetic dirty nine", body=_DIRTY_BODY)
    _run("--apply", "--action", "redact")

    second = _run("--apply", "--action", "redact", "--json")

    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["flagged"] == 0, "the redaction marker must not itself match"
    assert payload["written"] == 0
