"""CLI tests for the ``brain owner`` subcommand group (Phase 1.5).

The owner subcommands manage ``BRAIN_OWNER_PARTICIPANTS`` in ``.env`` so the
operator never has to hand-edit the file. Tests redirect the writer at a
``tmp_path`` ``.env`` by patching ``brain.config._project_dotenv`` — same
pattern ``tests/test_config.py::isolated_dotenv`` uses. Owner identifiers
are normalised at write time (trim → lowercase → dedupe) to match
``Config.load()``'s parser; assertions verify the on-disk shape rather
than the input casing.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain import config as config_module
from brain.cli import app


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """Redirect both ``Config.load()`` and the owner writer at ``tmp_path/.env``.

    - ``monkeypatch.chdir(tmp_path)`` neutralises the cwd-walk-up branch of
      ``Config.load()``'s dotenv discovery.
    - Patching ``config._project_dotenv`` redirects both the project-fallback
      load AND the owner-subcommand writer (which goes through the same helper).
    - Strips any inherited ``BRAIN_OWNER_PARTICIPANTS`` so each test starts
      from a known state. ``DATABASE_URL`` is left to the session-scope
      fixture in ``conftest.py``.
    """
    monkeypatch.chdir(tmp_path)
    fake_env = tmp_path / ".env"
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: fake_env)
    monkeypatch.delenv("BRAIN_OWNER_PARTICIPANTS", raising=False)
    yield fake_env


_RELINK_HINT_FRAGMENT = "Run `brain vault relink-derived`"


def test_owner_show_empty_when_unset(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``show`` with env unset and no ``.env`` line prints a none-marker."""
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "show"])
    assert result.exit_code == 0, result.output
    assert "BRAIN_OWNER_PARTICIPANTS unset" in result.output


def test_owner_show_lists_current_entries(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``show`` prints lowercased entries, one per line, sorted for stability."""
    monkeypatch.setenv(
        "BRAIN_OWNER_PARTICIPANTS",
        "Ali Sarkis,redacted@example.com",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "show"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines == ["ali sarkis", "redacted@example.com"]


def test_owner_set_writes_env_file(isolated_env: Path) -> None:
    """``set "Ali,fixture@example.com"`` writes a normalised + quoted line to ``.env``.

    Lowercase-in-list is the chosen contract (matches ``Config.load()``);
    a comma in the value triggers double-quoting per the writer rule.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "set", "Ali,fixture@example.com"])
    assert result.exit_code == 0, result.output
    text = isolated_env.read_text()
    assert 'BRAIN_OWNER_PARTICIPANTS="ali,fixture@example.com"' in text


def test_owner_set_replaces_existing_line(isolated_env: Path) -> None:
    """A pre-existing ``BRAIN_OWNER_PARTICIPANTS=`` line is replaced in place.

    Other lines (incl. comments, blank lines, unrelated keys) are preserved
    verbatim — the writer must not duplicate or reorder.
    """
    isolated_env.write_text(
        "# header comment\n"
        "DATABASE_URL=postgresql://x:y@h:5432/d\n"
        "BRAIN_OWNER_PARTICIPANTS=old@example.com\n"
        "\n"
        "# trailing comment\n"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "set", "new@example.com"])
    assert result.exit_code == 0, result.output
    text = isolated_env.read_text()
    # Replaced, not duplicated.
    assert text.count("BRAIN_OWNER_PARTICIPANTS=") == 1
    assert "BRAIN_OWNER_PARTICIPANTS=new@example.com" in text
    assert "old@example.com" not in text
    # Surrounding context preserved.
    assert "# header comment" in text
    assert "DATABASE_URL=postgresql://x:y@h:5432/d" in text
    assert "# trailing comment" in text


def test_owner_add_is_idempotent(isolated_env: Path) -> None:
    """Adding an already-present identifier is a no-op (case-insensitive)."""
    isolated_env.write_text(
        'BRAIN_OWNER_PARTICIPANTS="ali,fixture@example.com"\n'
    )
    before = isolated_env.read_text()
    runner = CliRunner()
    # Mixed-case input still matches the lowercase-stored entry.
    result = runner.invoke(app, ["owner", "add", "FIXTURE@Example.com"])
    assert result.exit_code == 0, result.output
    assert "no change" in result.output.lower()
    # File untouched — including byte-for-byte (no trailing-newline drift).
    assert isolated_env.read_text() == before
    # Idempotent path skips the relink hint (nothing to relink).
    assert _RELINK_HINT_FRAGMENT not in result.output


def test_owner_remove_is_idempotent(isolated_env: Path) -> None:
    """Removing an identifier that isn't present is a no-op."""
    isolated_env.write_text(
        'BRAIN_OWNER_PARTICIPANTS="ali,fixture@example.com"\n'
    )
    before = isolated_env.read_text()
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "remove", "notpresent@x.com"])
    assert result.exit_code == 0, result.output
    assert "no change" in result.output.lower()
    assert isolated_env.read_text() == before
    assert _RELINK_HINT_FRAGMENT not in result.output


def test_owner_set_quotes_when_value_contains_spaces(isolated_env: Path) -> None:
    """A value containing spaces is double-quoted on disk.

    Input casing is dropped (lowercase-in-list contract) but the embedded
    space survives, which still triggers the quote rule. Any extra
    formatting (escaped quotes, etc.) round-trips through
    :func:`brain.cli._owner_read_existing`.
    """
    runner = CliRunner()
    result = runner.invoke(
        app, ["owner", "set", "Ali Sarkis,fixture@example.com"]
    )
    assert result.exit_code == 0, result.output
    text = isolated_env.read_text()
    assert 'BRAIN_OWNER_PARTICIPANTS="ali sarkis,fixture@example.com"' in text


def test_owner_set_short_circuits_when_unchanged(isolated_env: Path) -> None:
    """``set`` is a no-op (no rewrite, no hint) when the value matches.

    Mirrors ``add`` / ``remove`` semantics: nothing to relink, so don't
    nudge the user toward a multi-minute rebuild. The on-disk file must
    be byte-for-byte identical (including any trailing-newline quirks)
    so the no-op claim holds even at the OS level.
    """
    isolated_env.write_text('BRAIN_OWNER_PARTICIPANTS="ali,fixture@example.com"\n')
    before = isolated_env.read_text()
    runner = CliRunner()
    # Mixed-case + reordered input that normalises to the existing list.
    result = runner.invoke(app, ["owner", "set", "ALI,fixture@example.com"])
    assert result.exit_code == 0, result.output
    assert "no change" in result.output.lower()
    assert _RELINK_HINT_FRAGMENT not in result.output
    assert isolated_env.read_text() == before


def test_owner_set_rejects_newline_in_value(isolated_env: Path) -> None:
    """An entry containing a newline is rejected before any write happens.

    A literal ``\\n`` in an identifier would corrupt subsequent ``.env``
    parsing once the line was rendered. Reject upfront with a friendly
    error + non-zero exit; ``.env`` stays untouched.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "set", "alice\nbob@example.com"])
    assert result.exit_code != 0
    assert "newline" in result.output.lower()
    # File never created — no ``.env``, no ``.env.tmp`` left behind.
    assert not isolated_env.exists()
    assert not (isolated_env.parent / ".env.tmp").exists()


def test_owner_set_handles_write_failure(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure in the atomic-replace path exits 1 with a friendly msg.

    Simulate the failure with ``mocker.patch``-style swap of ``os.replace``
    inside the ``cli`` module so the temp file is actually written but the
    rename fails — exercises the cleanup branch (``tmp.unlink``) and the
    user-facing ``error: failed to write`` message. Verifies no stale
    ``.env.tmp`` is left behind.
    """
    from brain import cli as cli_module

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(cli_module.os, "replace", _boom)
    runner = CliRunner()
    result = runner.invoke(app, ["owner", "set", "alice@example.com"])
    assert result.exit_code != 0
    assert "failed to write" in result.output.lower()
    # The atomic-write contract: on failure, no half-written sidecar leaks.
    assert not (isolated_env.parent / ".env.tmp").exists()


def test_owner_mutation_prints_relink_hint(isolated_env: Path) -> None:
    """``set``/``add``/``remove`` (when they actually mutate) print the hint."""
    runner = CliRunner()

    # `set` — always mutates (idempotent value-set, but file is rewritten).
    result_set = runner.invoke(app, ["owner", "set", "alice@example.com"])
    assert result_set.exit_code == 0, result_set.output
    assert _RELINK_HINT_FRAGMENT in result_set.output

    # `add` — net-new entry path.
    result_add = runner.invoke(app, ["owner", "add", "bob@example.com"])
    assert result_add.exit_code == 0, result_add.output
    assert _RELINK_HINT_FRAGMENT in result_add.output

    # `remove` — entry exists, so this mutates.
    result_remove = runner.invoke(app, ["owner", "remove", "alice@example.com"])
    assert result_remove.exit_code == 0, result_remove.output
    assert _RELINK_HINT_FRAGMENT in result_remove.output

    # Final state: only bob@example.com remains.
    final = isolated_env.read_text()
    assert "bob@example.com" in final
    assert "alice@example.com" not in final
