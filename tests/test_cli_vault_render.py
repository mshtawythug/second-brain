"""Tests for the ``brain vault render`` CLI command.

Quartz integration is the substance of this command, but Quartz itself
is a Node.js project — we never actually shell out to ``npx`` during a
test run. Every test mocks ``subprocess.run`` (and ``shutil.which``
where relevant) so the assertions cover the brain-side logic only:

- arg construction (``--directory``, ``--output``, cwd)
- workspace validation (vault is a directory, vault has .md files,
  Quartz workspace exists with package.json + quartz.config.ts)
- ``--no-build`` short-circuit
- exit code propagation from a non-zero npx
- friendly error when ``npx`` itself isn't on PATH
- timeout handling
- ``--to`` path-traversal guard

The tests use ``CliRunner`` and assert on exit codes + stdout. They
never touch the database — render is a pure file-tree operation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import _resolve_render_to, app


def _make_vault(tmp_path: Path, with_md: bool = True) -> Path:
    """Create a minimal vault directory.

    A real vault has many subdirectories and a frontmatter README; the
    tests only need ``.is_dir()`` to succeed and (optionally) at least
    one ``.md`` file for the ``_vault_has_markdown`` check to return
    True. We keep the scaffolding minimal so each test is explicit
    about what it's testing.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    if with_md:
        (vault / "note.md").write_text("# hello\n", encoding="utf-8")
    return vault


def _make_quartz_workspace(vault: Path) -> Path:
    """Create a ``.quartz/`` directory with the files render checks for.

    Returns the workspace path. Real Quartz workspaces have hundreds of
    files; we only need ``package.json`` and ``quartz.config.ts`` for
    ``_check_quartz_workspace`` to accept the dir.
    """
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text(
        "// stub config\nexport default {}\n", encoding="utf-8"
    )
    return workspace


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """A fresh ``CompletedProcess`` for ``subprocess.run`` mocks."""
    return subprocess.CompletedProcess(
        args=["npx", "quartz", "build"], returncode=returncode, stdout="", stderr=""
    )


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common env wiring — DATABASE_URL is required for Config.load()."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5434/second_brain_test",
        ),
    )


# ---------------------------------------------------------------------------
# Happy path + arg construction
# ---------------------------------------------------------------------------


def test_render_invokes_npx_with_correct_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """The default invocation calls ``npx quartz build`` with the right
    flags, cwd at the Quartz workspace, and the expected output dir."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    output_dir = tmp_path / "dist"
    monkeypatch.chdir(tmp_path)  # cwd matters for `--to` resolution

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        [
            "vault",
            "render",
            "--vault",
            str(vault),
            "--to",
            str(output_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    # Single subprocess call: npx quartz build --directory <vault> --output <to>
    assert run_mock.call_count == 1
    call_args, call_kwargs = run_mock.call_args
    cmd = call_args[0]
    assert cmd[0] == "npx"
    assert cmd[1] == "quartz"
    assert cmd[2] == "build"
    assert "--directory" in cmd
    assert str(vault) in cmd
    assert "--output" in cmd
    # `--to` is resolved through expanduser+resolve before being passed.
    assert str(output_dir.resolve()) in cmd
    # cwd is the Quartz workspace, not the vault.
    assert call_kwargs["cwd"] == str(workspace)
    # check=False so we can read the returncode ourselves.
    assert call_kwargs["check"] is False
    # Five-minute ceiling.
    assert call_kwargs["timeout"] == 300

    # Final success message points the user at the output.
    assert "rendered to" in result.output
    assert str(output_dir.resolve()) in result.output


def test_render_default_to_is_dist_under_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """With ``--to`` omitted, output goes to ``./dist`` in the cwd."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    cmd = run_mock.call_args[0][0]
    # The output flag's value should resolve to ``<cwd>/dist``.
    assert str((tmp_path / "dist").resolve()) in cmd


def test_render_uses_default_quartz_dir_under_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Without ``--quartz-dir``, render looks at ``<vault>/.quartz``."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert run_mock.call_args[1]["cwd"] == str(workspace)


def test_render_honors_explicit_quartz_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--quartz-dir`` overrides the default ``<vault>/.quartz`` location."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    # Place the workspace OUTSIDE the vault — a real-world setup where
    # the user keeps their Quartz scaffolding in a separate folder.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "package.json").write_text("{}", encoding="utf-8")
    (elsewhere / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        [
            "vault",
            "render",
            "--vault",
            str(vault),
            "--quartz-dir",
            str(elsewhere),
        ],
    )
    assert result.exit_code == 0, result.output
    assert run_mock.call_args[1]["cwd"] == str(elsewhere)


# ---------------------------------------------------------------------------
# --no-build short-circuit
# ---------------------------------------------------------------------------


def test_render_no_build_just_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-build`` exits 0 after printing OK without invoking npx."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--no-build"],
    )
    assert result.exit_code == 0, result.output
    assert run_mock.call_count == 0
    assert "OK" in result.output
    assert str(workspace) in result.output


def test_render_no_build_still_validates_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-build`` still runs the workspace check — that's its purpose."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    # No .quartz/ — workspace check must fail.
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--no-build"],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "Quartz workspace not found" in combined


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_render_rejects_missing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A non-existent ``--vault`` path returns exit 2 with a clear message."""
    _env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    bogus = tmp_path / "no-such-vault"
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(bogus)])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "vault path is not a directory" in combined
    assert run_mock.call_count == 0


def test_render_rejects_vault_with_no_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Empty vault (no ``.md`` files) returns exit 2 — nothing to render."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path, with_md=False)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "no .md files" in combined
    assert run_mock.call_count == 0


def test_render_rejects_missing_quartz_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """No ``.quartz/`` dir → BadParameter with the ``npx quartz create`` hint."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "Quartz workspace not found" in combined
    assert "npx quartz create" in combined
    assert run_mock.call_count == 0


def test_render_rejects_quartz_dir_missing_package_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Existing ``.quartz/`` but no package.json → tell the user exactly what's missing."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    # quartz.config.ts present, package.json absent.
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "package.json" in combined


def test_render_rejects_quartz_dir_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Existing ``.quartz/`` but no quartz.config.ts → clear error mentioning it."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "quartz.config.ts" in combined


# ---------------------------------------------------------------------------
# --to path traversal
# ---------------------------------------------------------------------------


def test_resolve_render_to_rejects_traversal(tmp_path: Path) -> None:
    """``_resolve_render_to`` rejects paths that resolve outside the cwd."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # `../escape` from cwd goes to tmp_path/escape, which is outside cwd.
    with pytest.raises(Exception) as excinfo:
        _resolve_render_to(Path("../escape"), cwd)
    assert "stay within the current working directory" in str(excinfo.value)


def test_resolve_render_to_accepts_relative(tmp_path: Path) -> None:
    """A plain relative output path resolves under the cwd."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    out = _resolve_render_to(Path("dist"), cwd)
    assert out == (cwd / "dist").resolve()


def test_resolve_render_to_accepts_absolute_inside_cwd(tmp_path: Path) -> None:
    """An absolute path that happens to live under the cwd is accepted."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "subdir" / "site"
    out = _resolve_render_to(target, cwd)
    assert out == target.resolve()


def test_render_rejects_traversal_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A `--to ../escape` path is rejected with a BadParameter."""
    _env(monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    vault = _make_vault(cwd)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(cwd)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app,
        [
            "vault",
            "render",
            "--vault",
            str(vault),
            "--to",
            "../escape",
        ],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "stay within" in combined
    assert run_mock.call_count == 0


# ---------------------------------------------------------------------------
# subprocess error handling
# ---------------------------------------------------------------------------


def test_render_propagates_npx_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A non-zero npx exit code causes brain to exit 1 with a clear line."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch("brain.cli.subprocess.run", return_value=_completed(returncode=2))

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "quartz build failed" in combined
    assert "exit code 2" in combined


def test_render_handles_npx_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``subprocess.run`` raising FileNotFoundError → friendly hint, exit 1."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=FileNotFoundError("npx not found"),
    )

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "npx not found" in combined
    assert "Node.js" in combined


def test_render_handles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A 5-min timeout is surfaced as exit 1 with a config-issue hint."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["npx"], timeout=300),
    )

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "exceeded" in combined or "timed out" in combined.lower()


# ---------------------------------------------------------------------------
# Sample quartz.config.ts smoke test
# ---------------------------------------------------------------------------


def test_sample_quartz_config_exists_and_parses_minimally() -> None:
    """The sample at the repo root must exist, be valid TypeScript shape.

    We don't run a TS compiler — we just sanity-check that the file
    parses as well-formed TS by string-matching the canonical Quartz
    config landmarks (the `import { QuartzConfig }` line, a default
    export, plugin lists). If a future Quartz version renames any of
    these, this test fails loudly and prompts a refresh.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config = repo_root / "quartz.config.ts"
    assert config.is_file(), f"sample quartz.config.ts missing at {config}"
    text = config.read_text(encoding="utf-8")
    # Quartz v4 import shape.
    assert "import { QuartzConfig }" in text
    assert 'from "./quartz/cfg"' in text
    assert "import * as Plugin from" in text
    # The three plugin buckets every Quartz config carries.
    assert "transformers:" in text
    assert "filters:" in text
    assert "emitters:" in text
    # Critical brain-flagged plugins. ``Plugin.Graph()`` was previously
    # asserted here; it was removed from the template by commit 885644a
    # ("fix(quartz): drop nonexistent Plugin.Graph()") because the symbol
    # doesn't exist in stock Quartz v4.5.x. ``Plugin.GitHubFlavoredMarkdown``
    # is a stable substitute — present in the template and required for
    # the brain vault's GFM-flavored content.
    assert "Plugin.ObsidianFlavoredMarkdown" in text
    assert "Plugin.GitHubFlavoredMarkdown" in text
    assert "Plugin.ContentIndex" in text
    # Default export is what `npx quartz build` reads.
    assert "export default config" in text
