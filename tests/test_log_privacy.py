"""No ``src/brain`` log line carries an exception's MESSAGE (Y1-Y6, H1-H2, F1).

Plan: ``docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md``. An exception's
text can quote what it failed on:

- a PyYAML ``MarkedYAMLError`` built from a STRING quotes the offending line;
- a psycopg DETAIL quotes row values;
- an ``EnrichmentError`` can embed the model's reply.

So every logger call in ``src/brain`` names a caught exception through
``brain.errors.describe_failure``. That gives the class plus content-free detail:
an errno symbol, a YAML line:col, or a raise-site ``log_detail``.

- **G1-G3** and **T1-T3** are the guard, an AST scan of ``src/brain``, and its
  self-tests: ``tests/test_log_privacy_rules.py`` (no database), with the rules in
  ``tests/log_privacy_rules.py``.
- **Y1-Y6** reproduce the leak at the sites that read a vault file.
- **H1-H2** check the gws refresh WARNINGs.

All data is synthetic.
"""

from __future__ import annotations

import logging
import stat
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter

#: A line of YAML that does not parse, and that PyYAML's error text quotes back.
_SECRET = "synthetic-secret-value-42"
_MALFORMED = f"---\ntitle: a: {_SECRET}\n---\nbody\n"


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(r.getMessage() for r in caplog.records)


def test_a_malformed_people_yml_is_logged_by_class_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Y1: ``load_people_yml`` (people names and emails live in that file)."""
    from brain.vault.derived_links.directory import load_people_yml

    (tmp_path / "_people.yml").write_text(f"name: a: {_SECRET}\n")
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert load_people_yml(tmp_path) == {}

    logged = _logged(caplog)
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"a malformed _people.yml was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_logged_by_class_only_in_link_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Y2: ``rewrite_vault_links``. It skips before any DB use, so no connection is needed."""
    from brain.vault.link_rewrite import rewrite_vault_links

    note = tmp_path / "note.md"
    note.write_text(_MALFORMED)
    with caplog.at_level(logging.WARNING, logger="brain.vault.link_rewrite"):
        changed = rewrite_vault_links(
            note,
            document_id=str(uuid.uuid4()),
            conn=None,  # type: ignore[arg-type]
        )

    logged = _logged(caplog)
    assert changed is False, "fixture: a malformed file must be skipped"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"malformed frontmatter was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_logged_by_class_only_in_the_fence_rewrite(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Y3: ``rewrite_derived_fences``, on a synced note whose file was then broken."""
    from brain.vault.derived_links.fence import rewrite_derived_fences
    from brain.vault.sync import sync_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = str(uuid.uuid4())
    note = vault / "note.md"
    note.write_text(dump_frontmatter({"id": doc_id, "title": "Note"}, "body\n"))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    # The fence rewrite only visits ingested mirrors; mark the synced row as one.
    test_db.execute("UPDATE documents SET kind = 'ingested' WHERE id = %s", (doc_id,))
    note.write_text(_MALFORMED)

    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.fence"):
        written = rewrite_derived_fences(test_db, {doc_id}, vault_path=vault)

    logged = _logged(caplog)
    assert written == 0, "fixture: a malformed file must be skipped"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"malformed frontmatter was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_reported_by_class_only_in_sync_summaries(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Y4: ``sync_summaries`` built its WARNING (and ``report.errors``) from ``f"...({exc})"``.

    PR #58 audit F1: the alias form, which the first G1 missed. ``report.errors`` is
    printed by ``brain vault sync-summaries`` and can land in a daemon's log, so it
    is class-only too (ruling in the plan); it keeps the path and the line:col.
    """
    from brain.vault.sync import sync_vault
    from brain.vault.sync_summaries import sync_summaries

    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = str(uuid.uuid4())
    note = vault / "note.md"
    note.write_text(dump_frontmatter({"id": doc_id, "title": "Note"}, "body\n"))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    test_db.execute("UPDATE documents SET summary = 'A summary.' WHERE id = %s", (doc_id,))
    note.write_text(_MALFORMED)

    with caplog.at_level(logging.WARNING, logger="brain.vault.sync_summaries"):
        report = sync_summaries(test_db, vault_root=vault)

    logged = _logged(caplog)
    assert report.errored == 1, "fixture: the malformed file must be counted as errored"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"sync-summaries did not log by class only: {logged!r}"
    )
    assert all(_SECRET not in e for e in report.errors), (
        f"sync-summaries' report.errors quotes the file: {report.errors!r}"
    )


def _gws_path(tmp_path: Path, script: str | None) -> str:
    """A PATH holding only a fake ``gws`` (or nothing), for ``real_gws_runner``."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if script is not None:
        gws = bindir / "gws"
        gws.write_text(script)
        gws.chmod(gws.stat().st_mode | stat.S_IXUSR)
    return str(bindir)


def test_a_missing_gws_is_logged_with_its_cause(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H1: class-only must not lose the operator's diagnosis: name the missing CLI."""
    from brain.vault.derived_links.directory import refresh_contacts
    from brain.vault.derived_links.gws import real_gws_runner

    monkeypatch.setenv("PATH", _gws_path(tmp_path, None))
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert refresh_contacts(test_db, runner=real_gws_runner) == 0

    logged = _logged(caplog)
    assert "gws CLI not found on PATH" in logged, f"the missing gws was not named: {logged!r}"


def test_a_failing_gws_is_logged_with_its_exit_code_not_its_stderr(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H2: the exit code is content-free; gws's stderr is not (it can echo account data)."""
    from brain.vault.derived_links.directory import refresh_contacts
    from brain.vault.derived_links.gws import real_gws_runner

    script = f"#!/bin/sh\necho '{_SECRET}' >&2\nexit 2\n"
    monkeypatch.setenv("PATH", _gws_path(tmp_path, script))
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert refresh_contacts(test_db, runner=real_gws_runner) == 0

    logged = _logged(caplog)
    assert "exit 2" in logged and _SECRET not in logged, (
        f"a failing gws was not logged by exit code alone: {logged!r}"
    )


def test_a_malformed_frontmatter_is_reported_by_class_only_in_sync_vault(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Y5: ``sync_vault``'s ``report.errors`` (printed by the watcher and sent over MCP).

    PR #58 review of ``ad67f43``: ``_sync_one`` raised
    ``_SyncError(f"malformed frontmatter: {e}")``, and the reason reaches the launchd
    watcher's stderr log (``cli.py``) and the MCP wire (``mcp_server.py``).
    """
    from brain.vault.sync import sync_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(_MALFORMED)

    report = sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    reasons = " ".join(str(reason) for _path, reason in report.errors)
    assert "ScannerError[" in reasons and _SECRET not in reasons, (
        f"sync_vault's report.errors quotes the file: {reasons!r}"
    )


def test_a_frontmatter_yaml_error_names_the_file_line() -> None:
    """F1: the mark counts from the top of the FILE, not of the YAML block.

    The YAML block starts after the opening ``---`` fence, so PyYAML's mark is one
    line short. ``parse_frontmatter`` shifts it, so ``title: a: …`` on file line 2
    is reported as ``[2:9]``.
    """
    from brain.errors import describe_failure
    from brain.vault.frontmatter import parse_frontmatter

    with pytest.raises(Exception) as caught:
        parse_frontmatter(_MALFORMED)

    assert describe_failure(caught.value) == "ScannerError[2:9]"


def test_an_unreadable_note_is_reported_by_class_and_errno_in_sync_vault(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Y6: the "could not read file" reason names ``PermissionError[EACCES]``, not the OS text."""
    from brain.vault.sync import sync_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "locked.md"
    note.write_text(dump_frontmatter({"id": str(uuid.uuid4()), "title": "Locked"}, "body\n"))
    note.chmod(0)
    try:
        report = sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    finally:
        note.chmod(stat.S_IRUSR | stat.S_IWUSR)

    reasons = " ".join(str(reason) for _path, reason in report.errors)
    assert "could not read file: PermissionError[EACCES]" in reasons, (
        f"an unreadable note was not reported by class and errno: {reasons!r}"
    )
