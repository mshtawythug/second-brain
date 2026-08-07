"""The F4 secret guard inside `brain vault sync` — refusals must not kill a walk.

Two defects motivate this module, and they fail in different ways:

1. **A guard refusal must be a per-file skip, not a dead walk.** Before this,
   ``SecretGuardError`` was caught nowhere in ``vault/sync.py``, so under
   ``BRAIN_SECRET_GUARD=reject`` a single offending note would abort
   ``sync_vault`` entirely — the remaining files never sync, and the user gets a
   traceback instead of a report. The guard's own docstring names aborting a
   900-file walk as a worse failure than a loud message, which is why ``warn``
   is its default.

2. **``redact`` must never leave the DB and the file disagreeing.** Vault-tier
   notes are FILE-authoritative (``_sensitivity_from_frontmatter``), so storing a
   redacted body while the user's file keeps the secret means the file wins on
   the next pass and the redaction silently evaporates — while having reported
   success. Sync refuses instead of rewriting the user's authored prose.

A note on the watcher: its worker already has a broad ``except Exception`` that
increments ``state.errors`` and logs, so an escaping ``SecretGuardError`` would
NOT have killed the daemon. The defect there was the quality of the failure — a
stack trace logged as an unexpected error, with nothing in ``report.errors`` —
rather than availability. ``test_watcher_path_reports_refusal_not_traceback``
pins the fixed behaviour at the seam the watcher actually calls.

Secret fixtures are synthetic: correct shape, invalid value, built by
concatenation so no literal credential shape appears in this file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_one_file, sync_vault
from tests.conftest import FakeEmbedder

_FAKE_AWS_KEY = "AKIA" + "A" * 16

_CLEAN_BODY = (
    "Quarterly planning notes. The release workflow and the documentation "
    "backlog were reviewed by the team.\n"
)
_DIRTY_BODY = (
    "Deploy runbook.\n\n"
    f"Set the access key to {_FAKE_AWS_KEY} before running the job.\n"
)


def _write(vault: Path, relative: str, *, title: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter({"title": title}, body))
    return path


def _stored_bodies(conn: psycopg.Connection[Any]) -> dict[str, str]:
    rows = conn.execute("SELECT title, content FROM documents").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


# --------------------------------------------------------------------------
# The walk must survive
# --------------------------------------------------------------------------


def test_reject_skips_one_file_and_syncs_the_rest(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """THE HEADLINE: one refused note must not abort the walk.

    Both halves are asserted. The clean files must be *present* — proving the
    walk continued past the refusal rather than dying at it — and the dirty file
    must be absent with a recorded reason. Asserting only the error would pass
    even if the walk had aborted immediately after recording it.
    """
    vault = tmp_path / "vault"
    _write(vault, "notes/a-clean.md", title="Synthetic clean A", body=_CLEAN_BODY)
    _write(vault, "notes/b-dirty.md", title="Synthetic dirty B", body=_DIRTY_BODY)
    _write(vault, "notes/c-clean.md", title="Synthetic clean C", body=_CLEAN_BODY)

    report = sync_vault(
        test_db,
        embedder=FakeEmbedder(),
        vault_path=vault,
        secret_guard="reject",
    )

    titles = set(_stored_bodies(test_db))
    assert "Synthetic clean A" in titles
    assert "Synthetic clean C" in titles, (
        "the file AFTER the refusal must still sync — if this is missing, the "
        "walk aborted at the refusal instead of continuing"
    )
    assert "Synthetic dirty B" not in titles

    assert report.secrets_refused == 1
    assert len(report.errors) == 1
    bad_path, reason = report.errors[0]
    assert bad_path.name == "b-dirty.md"
    assert "secret guard" in reason


def test_refusal_message_never_contains_the_secret(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The diagnostic must not reproduce what it refused.

    A refusal that echoes the credential moves it into terminal scrollback, CI
    logs, and any tooling that renders ``report.errors`` — turning a guard into
    a disclosure channel.
    """
    vault = tmp_path / "vault"
    _write(vault, "notes/leaky.md", title="Synthetic leaky", body=_DIRTY_BODY)

    report = sync_vault(
        test_db, embedder=FakeEmbedder(), vault_path=vault, secret_guard="reject"
    )

    assert report.secrets_refused == 1
    assert _FAKE_AWS_KEY not in report.errors[0][1]


@pytest.mark.parametrize("mode", ["off", "warn"])
def test_lossless_modes_store_the_body_unchanged(
    test_db: psycopg.Connection[Any], tmp_path: Path, mode: str
) -> None:
    """``off`` and ``warn`` are lossless: the note syncs, body untouched.

    ``warn`` is the guard's default precisely because it is lossless and
    reversible, so a corpus-wide sync under the default must never lose a file.
    """
    vault = tmp_path / "vault"
    _write(vault, "notes/warned.md", title="Synthetic warned", body=_DIRTY_BODY)

    report = sync_vault(
        test_db, embedder=FakeEmbedder(), vault_path=vault, secret_guard=mode
    )

    assert report.secrets_refused == 0
    assert report.errors == []
    assert _FAKE_AWS_KEY in _stored_bodies(test_db)["Synthetic warned"], (
        f"{mode} must store the body verbatim — it is the lossless mode"
    )


def test_default_is_off_so_existing_callers_are_unchanged(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Omitting ``secret_guard`` entirely behaves exactly as it did pre-F4.

    The backward-compatibility assertion for the seven existing call sites
    (``cli.py`` x4, ``mcp_server.py`` x2, the watcher). The parameter defaults to
    ``off`` rather than to ``DEFAULT_SECRET_GUARD`` so none of them changes
    behaviour until deliberately opted in.
    """
    vault = tmp_path / "vault"
    _write(vault, "notes/legacy.md", title="Synthetic legacy", body=_DIRTY_BODY)

    report = sync_vault(test_db, embedder=FakeEmbedder(), vault_path=vault)

    assert report.errors == []
    assert report.secrets_refused == 0
    assert _FAKE_AWS_KEY in _stored_bodies(test_db)["Synthetic legacy"]


# --------------------------------------------------------------------------
# redact must not diverge from the file
# --------------------------------------------------------------------------


def test_redact_refuses_a_vault_tier_note_rather_than_diverging(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """REGRESSION: a redaction that the next sync would undo is refused instead.

    Vault-tier files are file-authoritative. Storing a redacted body here while
    the user's file still holds the secret makes the two disagree, and the file
    wins on the next pass — so the redaction silently evaporates while having
    reported success. Worse than not redacting, because it claims to have
    worked.

    The assertions cover both halves of "never diverge": nothing was stored, AND
    the user's file was not rewritten.
    """
    vault = tmp_path / "vault"
    note = _write(vault, "notes/redactme.md", title="Synthetic redact", body=_DIRTY_BODY)
    before = note.read_text()

    report = sync_vault(
        test_db, embedder=FakeEmbedder(), vault_path=vault, secret_guard="redact"
    )

    assert report.secrets_refused == 1
    assert "Synthetic redact" not in _stored_bodies(test_db), (
        "no divergent body may be stored"
    )
    assert note.read_text() == before, (
        "sync must not rewrite a user's authored prose — refusing is the "
        "contract, and silently editing their note would be a larger act than "
        "the guard is entitled to"
    )
    assert "undo itself" in report.errors[0][1], (
        "the message must explain WHY it refused, or the user will read it as "
        "an arbitrary failure"
    )


def test_allow_secrets_frontmatter_opt_out_still_works(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """``allow_secrets: true`` in a note's YAML bypasses the refusal.

    The documented escape hatch for a note that is legitimately *about*
    credentials — a rotation runbook quoting a key format. It suppresses the
    action, not the evidence.
    """
    vault = tmp_path / "vault"
    path = vault / "notes/runbook.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dump_frontmatter(
            {"title": "Synthetic key-rotation runbook", "allow_secrets": True},
            _DIRTY_BODY,
        )
    )

    report = sync_vault(
        test_db, embedder=FakeEmbedder(), vault_path=vault, secret_guard="reject"
    )

    assert report.secrets_refused == 0
    assert report.errors == []
    assert "Synthetic key-rotation runbook" in _stored_bodies(test_db)


# --------------------------------------------------------------------------
# The watcher seam
# --------------------------------------------------------------------------


def test_watcher_path_reports_refusal_not_traceback(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """``sync_one_file`` — the seam the watcher calls — returns a report, not a raise.

    The watcher worker wraps each job in a broad ``except Exception``, so an
    escaping ``SecretGuardError`` would have been absorbed and the daemon would
    have survived — but the user would see a stack trace logged as an unexpected
    failure, with nothing in ``report.errors`` explaining it. Returning a
    populated report is what turns that into an actionable diagnostic.

    Asserted at ``sync_one_file`` rather than by driving the watcher because
    this is the exact call the worker makes; testing through the thread pool
    would add scheduling flakiness without testing anything more.
    """
    vault = tmp_path / "vault"
    note = _write(vault, "notes/watched.md", title="Synthetic watched", body=_DIRTY_BODY)

    report = sync_one_file(
        test_db,
        embedder=FakeEmbedder(),
        vault_path=vault,
        file_path=note,
        secret_guard="reject",
    )

    assert report.secrets_refused == 1
    assert len(report.errors) == 1
    assert "secret guard" in report.errors[0][1]
    assert report.created == 0 and report.updated == 0
    assert "Synthetic watched" not in _stored_bodies(test_db)


def test_watcher_path_syncs_a_clean_file_normally(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Scope check: the guard does not disturb the ordinary watcher path."""
    vault = tmp_path / "vault"
    note = _write(vault, "notes/fine.md", title="Synthetic fine", body=_CLEAN_BODY)

    report = sync_one_file(
        test_db,
        embedder=FakeEmbedder(),
        vault_path=vault,
        file_path=note,
        secret_guard="reject",
    )

    assert report.errors == []
    assert report.secrets_refused == 0
    assert report.created == 1
