"""CLI surface of `brain claude install-hooks` / `capture-hook`.

Every test drives a throwaway Typer app built by
``cli_claude.register_claude_commands`` and points ``--target`` at ``tmp_path``,
so the developer's real ``~/.claude`` is never read or written. ``BRAIN_HOME`` is
redirected too, so no sentinel escapes into the real brain home. No database.
"""
from __future__ import annotations

import json
import os
import stat
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from brain import cli_claude

runner = CliRunner()

HOOK_FILENAME = "brain-capture-hook.sh"
BACKUP_GLOB = "settings.json.brain-backup-*"


@pytest.fixture
def hooks_app() -> typer.Typer:
    """A throwaway `claude` sub-app carrying only the two commands under test.

    ``cli.py`` owns the real ``claude_app`` object and calls the registrar once;
    building a fresh app here keeps these tests independent of that wiring.
    """
    app = typer.Typer()
    cli_claude.register_claude_commands(app)
    return app


def _script(root: Path) -> Path:
    return root / "hooks" / HOOK_FILENAME


def _settings(root: Path) -> Path:
    return root / "settings.json"


def _packaged_bytes() -> bytes:
    return (resource_files("brain.templates.claude") / HOOK_FILENAME).read_bytes()


def _stop_entries(root: Path) -> list[dict[str, Any]]:
    document = json.loads(_settings(root).read_text(encoding="utf-8"))
    return [entry for group in document["hooks"]["Stop"] for entry in group.get("hooks", [])]


def _install(app: typer.Typer, root: Path, *args: str) -> Any:
    return runner.invoke(app, ["install-hooks", "--target", str(root), *args])


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def test_fresh_install_writes_script_and_settings(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 0, result.output
    assert _script(tmp_path).read_bytes() == _packaged_bytes()
    assert stat.S_IMODE(_script(tmp_path).stat().st_mode) & 0o111
    entries = _stop_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0] == {
        "type": "command",
        "command": str(_script(tmp_path)),
        "timeout": 10,
    }
    assert "hook script installed" in result.output
    assert "Stop hook added" in result.output


def test_install_reports_how_to_disable(hooks_app: typer.Typer, tmp_path: Path) -> None:
    """A hook the user cannot turn off is a hook they uninstall in anger."""
    result = _install(hooks_app, tmp_path)

    assert "BRAIN_HOOK_ENABLED=false" in result.output
    assert "--uninstall" in result.output


def test_reinstall_is_idempotent(hooks_app: typer.Typer, tmp_path: Path) -> None:
    _install(hooks_app, tmp_path)
    before = _script(tmp_path).stat().st_mtime_ns

    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 0
    assert "hook script up to date" in result.output
    assert "Stop hook already present" in result.output
    assert _script(tmp_path).stat().st_mtime_ns == before
    assert len(_stop_entries(tmp_path)) == 1


def test_reinstall_creates_no_second_backup(hooks_app: typer.Typer, tmp_path: Path) -> None:
    """An unchanged merge writes nothing, so it takes no backup either."""
    _install(hooks_app, tmp_path)
    after_first = list(tmp_path.glob(BACKUP_GLOB))

    _install(hooks_app, tmp_path)

    assert list(tmp_path.glob(BACKUP_GLOB)) == after_first


def test_force_overwrites_drifted_script(hooks_app: typer.Typer, tmp_path: Path) -> None:
    _install(hooks_app, tmp_path)
    _script(tmp_path).write_bytes(b"#!/bin/sh\n# hand-edited\n")

    result = _install(hooks_app, tmp_path, "--force")

    assert result.exit_code == 0
    assert _script(tmp_path).read_bytes() == _packaged_bytes()


def test_drifted_script_without_force_prompts_and_aborts(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    _install(hooks_app, tmp_path)
    drifted = b"#!/bin/sh\n# hand-edited\n"
    _script(tmp_path).write_bytes(drifted)

    result = runner.invoke(
        hooks_app, ["install-hooks", "--target", str(tmp_path)], input="n\n"
    )

    assert result.exit_code != 0
    assert _script(tmp_path).read_bytes() == drifted


def test_backup_created_before_rewrite(hooks_app: typer.Typer, tmp_path: Path) -> None:
    original = json.dumps({"env": {"SYNTHETIC": "1"}, "editorMode": "vim"}, indent=4)
    _settings(tmp_path).write_text(original, encoding="utf-8")

    _install(hooks_app, tmp_path)

    backups = list(tmp_path.glob(BACKUP_GLOB))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    # And the user's keys survived into the rewritten file.
    document = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert document["env"] == {"SYNTHETIC": "1"}
    assert document["editorMode"] == "vim"


def test_no_backup_when_settings_absent(hooks_app: typer.Typer, tmp_path: Path) -> None:
    _install(hooks_app, tmp_path)

    assert list(tmp_path.glob(BACKUP_GLOB)) == []


def test_no_backup_when_settings_is_whitespace_only(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    """An empty file holds nothing worth preserving — a backup would be noise."""
    _settings(tmp_path).write_text("\n  \n", encoding="utf-8")

    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 0, result.output
    assert list(tmp_path.glob(BACKUP_GLOB)) == []
    assert len(_stop_entries(tmp_path)) == 1


def test_relocated_script_entry_is_updated_not_duplicated(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    """A stale command containing the marker is corrected in place."""
    stale = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/tmp/old-home/.claude/hooks/brain-capture-hook.sh",
                            "timeout": 99,
                        }
                    ]
                }
            ]
        }
    }
    _settings(tmp_path).write_text(json.dumps(stale), encoding="utf-8")

    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 0, result.output
    assert "updated Stop hook entry" in result.output
    entries = _stop_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["command"] == str(_script(tmp_path))
    assert entries[0]["timeout"] == 10


def test_dry_run_reports_a_pending_update(hooks_app: typer.Typer, tmp_path: Path) -> None:
    stale = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "brain claude capture-hook"}]}
            ]
        }
    }
    body = json.dumps(stale)
    _settings(tmp_path).write_text(body, encoding="utf-8")

    result = _install(hooks_app, tmp_path, "--dry-run")

    assert result.exit_code == 0
    assert "would update Stop hook entry" in result.output
    assert _settings(tmp_path).read_text(encoding="utf-8") == body


def test_existing_user_stop_hook_survives_install(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    foreign = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "/tmp/synthetic/other.sh"}]}]
        }
    }
    _settings(tmp_path).write_text(json.dumps(foreign), encoding="utf-8")

    _install(hooks_app, tmp_path)

    entries = _stop_entries(tmp_path)
    assert entries[0]["command"] == "/tmp/synthetic/other.sh"
    assert entries[1]["command"] == str(_script(tmp_path))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_malformed_settings_exits_1_and_writes_nothing(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    broken = '{\n  "env": {}\n  "editorMode": "vim"\n}'
    _settings(tmp_path).write_text(broken, encoding="utf-8")

    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert _settings(tmp_path).read_text(encoding="utf-8") == broken
    assert list(tmp_path.glob(BACKUP_GLOB)) == []
    assert not _script(tmp_path).exists()


def test_force_does_not_override_a_malformed_settings_refusal(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    """`--force` means "overwrite a stale script", never "discard my config"."""
    broken = "{ not json at all"
    _settings(tmp_path).write_text(broken, encoding="utf-8")

    result = _install(hooks_app, tmp_path, "--force")

    assert result.exit_code == 1
    assert _settings(tmp_path).read_text(encoding="utf-8") == broken
    assert not _script(tmp_path).exists()


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        ({"hooks": []}, "hooks"),
        ({"hooks": {"Stop": {}}}, "Stop"),
    ],
)
def test_unexpected_shape_is_refused(
    hooks_app: typer.Typer, tmp_path: Path, document: dict[str, Any], needle: str
) -> None:
    body = json.dumps(document)
    _settings(tmp_path).write_text(body, encoding="utf-8")

    result = _install(hooks_app, tmp_path)

    assert result.exit_code == 1
    assert needle in result.output
    assert _settings(tmp_path).read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(hooks_app: typer.Typer, tmp_path: Path) -> None:
    result = _install(hooks_app, tmp_path, "--dry-run")

    assert result.exit_code == 0
    assert "would install hook script" in result.output
    assert "would add Stop hook entry" in result.output
    assert "(dry run" in result.output
    assert not _script(tmp_path).exists()
    assert not _settings(tmp_path).exists()
    assert list(tmp_path.glob(BACKUP_GLOB)) == []


def test_dry_run_leaves_an_existing_settings_file_untouched(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    original = json.dumps({"env": {"SYNTHETIC": "1"}})
    _settings(tmp_path).write_text(original, encoding="utf-8")

    _install(hooks_app, tmp_path, "--dry-run")

    assert _settings(tmp_path).read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_entry_and_script(hooks_app: typer.Typer, tmp_path: Path) -> None:
    _install(hooks_app, tmp_path)

    result = _install(hooks_app, tmp_path, "--uninstall")

    assert result.exit_code == 0
    document = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert "hooks" not in document
    assert not _script(tmp_path).exists()
    assert not (tmp_path / "hooks").exists()
    assert "removed Stop hook entry" in result.output


def test_uninstall_preserves_user_stop_hook(hooks_app: typer.Typer, tmp_path: Path) -> None:
    foreign_group = {
        "hooks": [{"type": "command", "command": "/tmp/synthetic/other.sh", "timeout": 5}]
    }
    _settings(tmp_path).write_text(
        json.dumps({"hooks": {"Stop": [foreign_group]}}), encoding="utf-8"
    )
    _install(hooks_app, tmp_path)

    _install(hooks_app, tmp_path, "--uninstall")

    document = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert document["hooks"]["Stop"] == [foreign_group]


def test_uninstall_dry_run_deletes_nothing(hooks_app: typer.Typer, tmp_path: Path) -> None:
    """Regression: `--uninstall --dry-run` removed the script for real.

    ``_apply_settings`` honoured ``dry_run`` but ``_remove_hook_script`` ran
    unconditionally, so previewing an uninstall destroyed the hook script while
    leaving settings.json still pointing at it.
    """
    _install(hooks_app, tmp_path)
    settings_before = _settings(tmp_path).read_text(encoding="utf-8")

    result = _install(hooks_app, tmp_path, "--uninstall", "--dry-run")

    assert result.exit_code == 0, result.output
    assert _script(tmp_path).is_file(), "dry-run deleted the hook script"
    assert _settings(tmp_path).read_text(encoding="utf-8") == settings_before
    assert "would remove" in result.output
    assert "(dry run" in result.output


def test_uninstall_when_absent_reports_and_exits_zero(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    result = _install(hooks_app, tmp_path, "--uninstall")

    assert result.exit_code == 0
    assert "no brain Stop hook found" in result.output


def test_uninstall_warns_on_a_non_empty_hooks_dir(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    """Never `rm -rf`: someone else's hook in the directory keeps it alive."""
    _install(hooks_app, tmp_path)
    (tmp_path / "hooks" / "other-tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    result = _install(hooks_app, tmp_path, "--uninstall")

    assert result.exit_code == 0
    assert (tmp_path / "hooks").is_dir()
    assert (tmp_path / "hooks" / "other-tool.sh").exists()
    assert "not empty" in result.output


def test_install_uninstall_round_trip_is_clean(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    original = {"env": {"SYNTHETIC": "1"}, "editorMode": "vim"}
    _settings(tmp_path).write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    _install(hooks_app, tmp_path)
    _install(hooks_app, tmp_path, "--uninstall")

    assert json.loads(_settings(tmp_path).read_text(encoding="utf-8")) == original


# ---------------------------------------------------------------------------
# capture-hook (plumbing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("garbage", ["not json", "", "[]", "\x00\x01"])
def test_capture_hook_command_allows_on_garbage_stdin(
    hooks_app: typer.Typer, garbage: str
) -> None:
    result = runner.invoke(hooks_app, ["capture-hook"], input=garbage)

    assert result.exit_code == 0
    assert result.stdout == ""


def test_capture_hook_command_emits_single_line_json_on_block(
    hooks_app: typer.Typer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one parseable line — guards against Rich soft-wrapping."""
    record = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "/tmp/synthetic/mod.py"},
                }
            ]
        },
    }
    transcript = tmp_path / "session.jsonl"
    transcript.write_text((json.dumps(record) + "\n") * 15, encoding="utf-8")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "brain-home"))
    payload = json.dumps(
        {
            "session_id": "synthetic-session",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }
    )

    result = runner.invoke(hooks_app, ["capture-hook"], input=payload)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    decision = json.loads(lines[0])
    assert decision["decision"] == "block"
    assert "brain capture" in decision["reason"]


def test_capture_hook_is_silent_when_disabled(
    hooks_app: typer.Typer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOOK_ENABLED", "false")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "brain-home"))
    payload = json.dumps(
        {"session_id": "s", "transcript_path": "/nope", "stop_hook_active": False}
    )

    result = runner.invoke(hooks_app, ["capture-hook"], input=payload)

    assert result.exit_code == 0
    assert result.stdout == ""


def test_capture_hook_is_hidden_from_help(hooks_app: typer.Typer) -> None:
    result = runner.invoke(hooks_app, ["--help"])

    assert "install-hooks" in result.output
    assert "capture-hook" not in result.output


def test_run_capture_hook_returns_empty_string_on_garbage() -> None:
    assert cli_claude.run_capture_hook(b"not json") == ""


def test_run_capture_hook_never_raises_on_a_hostile_payload() -> None:
    """The Stop hook path must swallow everything; a raise blocks the session."""
    assert cli_claude.run_capture_hook(b'{"transcript_path": 12345}') == ""


def test_run_capture_hook_swallows_an_exploding_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety guarantee: even a crash in the decision path exits quietly.

    ``decide`` is defensive enough that no realistic input reaches this branch,
    so the failure is injected with a standard test double. Without the guard a
    future bug anywhere under ``decide`` would surface as a traceback in every
    Claude Code session on the machine.
    """

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic failure deep in the decision path")

    monkeypatch.setattr(cli_claude.claude_hook, "decide", _explode)

    assert cli_claude.run_capture_hook(b"{}") == ""


def test_read_hook_stdin_yields_empty_bytes_when_stdin_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable stdin must not fail the Stop hook either."""

    class _BrokenStdin:
        @property
        def buffer(self) -> object:
            raise OSError("synthetic unreadable stdin")

    monkeypatch.setattr("sys.stdin", _BrokenStdin())

    assert cli_claude.read_hook_stdin() == b""


# ---------------------------------------------------------------------------
# The shipped template
# ---------------------------------------------------------------------------


def test_shim_template_is_shipped() -> None:
    content = _packaged_bytes().decode("utf-8")

    assert content.startswith("#!/bin/sh")
    assert "command -v brain" in content
    assert "|| exit 0" in content
    assert "brain claude capture-hook" in content


def test_installed_script_is_executable_by_owner(
    hooks_app: typer.Typer, tmp_path: Path
) -> None:
    _install(hooks_app, tmp_path)

    mode = stat.S_IMODE(_script(tmp_path).stat().st_mode)

    assert mode == 0o755
    assert os.access(_script(tmp_path), os.X_OK)
