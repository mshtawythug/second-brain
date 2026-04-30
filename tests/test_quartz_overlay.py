"""Tests for the Quartz overlay step (G.6 helper + G.6 CLI integration).

Two layers covered here:

1. ``brain.vault.quartz_overlay`` — pure planning + apply functions.
   Tests use ``tmp_path`` to stand up a fake brain repo (with a tiny
   ``quartz_overrides/`` tree) and a fake Quartz workspace, then
   exercise ``plan_overlay`` / ``apply_overlay`` directly. No CLI
   involvement at this layer.
2. ``brain vault render`` CLI — the overlay/no-overlay/print-overlay
   flag wiring + the order-of-operations contract that the overlay
   apply step runs strictly before ``subprocess.run``.

We never invoke ``npx`` or any real Quartz process — all subprocess
calls are mocked. The four-phase test pattern (setup → exercise →
verify → teardown) is followed throughout; ``tmp_path`` and pytest
fixtures handle teardown automatically.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.vault.quartz_overlay import (
    OverlayError,
    apply_overlay,
    plan_overlay,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_repo(
    tmp_path: Path, files: dict[str, str] | None = None
) -> Path:
    """Build a fake brain repo with a tiny ``quartz_overrides/`` tree.

    ``files`` maps relative paths under ``quartz_overrides/`` to content.
    The overlay's source tree mirrors ``<quartz_dir>/`` 1:1 — top-level
    workspace configs (``quartz.layout.ts``) live at the root, and
    Quartz source files live under ``quartz/``. Defaults cover both
    placement classes plus the four shapes seen in the real repo
    (components/, plugins/, styles/, util/, scripts/).
    """
    if files is None:
        files = {
            "quartz.layout.ts": "// stub layout\nexport default {}\n",
            "quartz/components/Graph.tsx": "// stub Graph\nexport default null\n",
            "quartz/components/scripts/graph.inline.ts": "// stub inline\n",
            "quartz/plugins/emitters/contentIndex.ts": "// stub emitter\n",
            "quartz/plugins/transformers/derivedFenceMark.ts": "// stub xform\n",
            "quartz/styles/graph.scss": "// stub scss\n",
            "quartz/util/path.ts": "// stub util\n",
        }
    overrides = tmp_path / "quartz_overrides"
    for rel, content in files.items():
        dest = overrides / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return tmp_path


def _make_quartz_workspace(
    tmp_path: Path, *, with_upstream_contentindex: bool = True
) -> Path:
    """Create a fake ``.quartz/`` workspace with the canonical files.

    ``package.json`` and ``quartz.config.ts`` are required for the CLI's
    workspace check. ``quartz/plugins/emitters/contentIndex.tsx`` is
    optional — controls which rename branch fires.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text(
        "// stub\nexport default {}\n", encoding="utf-8"
    )
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    if with_upstream_contentindex:
        (emitters / "contentIndex.tsx").write_text(
            "// upstream stock emitter\n", encoding="utf-8"
        )
    return workspace


# ---------------------------------------------------------------------------
# Helper-layer tests (plan_overlay + apply_overlay)
# ---------------------------------------------------------------------------


def test_overlay_copies_all_files(tmp_path: Path) -> None:
    """Every file under ``quartz_overrides/`` lands at the right place."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise
    plan = plan_overlay(repo, workspace)
    copied = apply_overlay(plan)

    # Verify — every source maps to its 1:1 mirror under <workspace>/.
    expected_pairs = {
        ("quartz.layout.ts", "quartz.layout.ts"),
        ("quartz/components/Graph.tsx", "quartz/components/Graph.tsx"),
        (
            "quartz/components/scripts/graph.inline.ts",
            "quartz/components/scripts/graph.inline.ts",
        ),
        (
            "quartz/plugins/emitters/contentIndex.ts",
            "quartz/plugins/emitters/contentIndex.ts",
        ),
        (
            "quartz/plugins/transformers/derivedFenceMark.ts",
            "quartz/plugins/transformers/derivedFenceMark.ts",
        ),
        ("quartz/styles/graph.scss", "quartz/styles/graph.scss"),
        ("quartz/util/path.ts", "quartz/util/path.ts"),
    }
    actual_pairs = {
        (
            str(src.relative_to(repo / "quartz_overrides")),
            str(dest.relative_to(workspace.resolve())),
        )
        for src, dest in copied
    }
    assert actual_pairs == expected_pairs

    # Verify content survives the copy intact.
    for src, dest in copied:
        assert dest.is_file(), f"missing dest after copy: {dest}"
        assert dest.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_overlay_overwrites_existing(tmp_path: Path) -> None:
    """Pre-populated destinations are overwritten by the overlay copy."""
    # Setup
    repo = _make_fake_repo(
        tmp_path / "repo",
        files={"quartz/components/Graph.tsx": "// brain Graph (new)\n"},
    )
    workspace = _make_quartz_workspace(tmp_path)
    stub_dest = workspace / "quartz" / "components" / "Graph.tsx"
    stub_dest.parent.mkdir(parents=True, exist_ok=True)
    stub_dest.write_text("// stale stub Graph (old)\n", encoding="utf-8")

    # Exercise
    plan = plan_overlay(repo, workspace)
    apply_overlay(plan)

    # Verify
    assert stub_dest.read_text(encoding="utf-8") == "// brain Graph (new)\n"


def test_overlay_renames_upstream_contentindex(tmp_path: Path) -> None:
    """When stock ``contentIndex.tsx`` is present, it gets renamed first."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=True)
    upstream = workspace / "quartz" / "plugins" / "emitters" / "contentIndex.tsx"
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    assert upstream.is_file() and not renamed.is_file()

    # Exercise
    plan = plan_overlay(repo, workspace)
    assert plan.rename is not None
    assert plan.rename_state == "needed"
    apply_overlay(plan)

    # Verify — the rename happened, and the original is gone.
    assert not upstream.is_file()
    assert renamed.is_file()
    assert renamed.read_text(encoding="utf-8") == "// upstream stock emitter\n"


def test_overlay_skips_rename_when_already_applied(tmp_path: Path) -> None:
    """If only ``_upstreamContentIndex.tsx`` exists, no rename is needed."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=False)
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    renamed.write_text("// already renamed\n", encoding="utf-8")

    # Exercise
    plan = plan_overlay(repo, workspace)

    # Verify
    assert plan.rename is None
    assert plan.rename_state == "already_applied"


def test_overlay_rename_missing_both(tmp_path: Path) -> None:
    """Neither contentIndex variant present → ``missing_both`` state."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=False)

    # Exercise
    plan = plan_overlay(repo, workspace)

    # Verify
    assert plan.rename is None
    assert plan.rename_state == "missing_both"


def test_overlay_raises_when_both_contentindex_files_present(tmp_path: Path) -> None:
    """Both upstream and renamed files at once → refuse to auto-resolve."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=True)
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    renamed.write_text("// also already renamed\n", encoding="utf-8")

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(repo, workspace)
    assert "both upstream and renamed contentIndex files exist" in str(excinfo.value)


def test_overlay_raises_when_overrides_dir_missing(tmp_path: Path) -> None:
    """Missing ``quartz_overrides/`` is a hard failure (broken brain repo)."""
    # Setup — repo dir exists but has no quartz_overrides subdir.
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(repo, workspace)
    assert "overlay source directory not found" in str(excinfo.value)


def test_overlay_skips_dotfiles(tmp_path: Path) -> None:
    """Macos metadata + dotfiles are skipped defensively."""
    # Setup
    repo = _make_fake_repo(
        tmp_path / "repo",
        files={
            "quartz/components/Graph.tsx": "// keep\n",
            ".DS_Store": "macOS metadata\n",
            "quartz/components/.hidden": "should-not-copy\n",
        },
    )
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise
    plan = plan_overlay(repo, workspace)

    # Verify — only the non-dotfile is in the plan.
    src_names = {src.name for src, _ in plan.pairs}
    assert "Graph.tsx" in src_names
    assert ".DS_Store" not in src_names
    assert ".hidden" not in src_names


def test_overlay_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink under quartz_overrides pointing outside is rejected."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.tsx").write_text("// evil\n", encoding="utf-8")
    link = repo / "quartz_overrides" / "quartz" / "components" / "linked.tsx"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside / "evil.tsx")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(repo, workspace)
    assert "escaped" in str(excinfo.value)


def test_overlay_pairs_are_sorted(tmp_path: Path) -> None:
    """Plan pairs come back in deterministic Path-sorted-by-src order."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise
    plan = plan_overlay(repo, workspace)

    # Verify — sorted by source ``Path`` (the same order ``rglob`` is
    # piped through inside ``plan_overlay``). Path-sort ≠ str-sort
    # because pathlib normalizes separators, so we sort the Paths
    # directly here too.
    srcs = [src for src, _ in plan.pairs]
    assert srcs == sorted(srcs)


def test_overlay_pairs_idempotent(tmp_path: Path) -> None:
    """Repeated planning produces an identical plan (deterministic)."""
    # Setup
    repo = _make_fake_repo(tmp_path / "repo")
    workspace = _make_quartz_workspace(tmp_path)

    # Exercise
    plan_a = plan_overlay(repo, workspace)
    plan_b = plan_overlay(repo, workspace)

    # Verify
    assert plan_a.pairs == plan_b.pairs
    assert plan_a.rename == plan_b.rename
    assert plan_a.rename_state == plan_b.rename_state


# ---------------------------------------------------------------------------
# CLI tests — overlay flag wiring
# ---------------------------------------------------------------------------


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire DATABASE_URL the same way the existing render tests do."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5433/second_brain_test",
        ),
    )


def _make_vault(tmp_path: Path) -> Path:
    """Minimal vault — just needs to be a dir with at least one .md."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# hello\n", encoding="utf-8")
    return vault


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["npx", "quartz", "build"], returncode=returncode, stdout="", stderr=""
    )


def test_no_overlay_skips_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-overlay`` does not touch the Quartz workspace files."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    upstream = emitters / "contentIndex.tsx"
    upstream.write_text("// upstream stock\n", encoding="utf-8")
    components = workspace / "quartz" / "components"
    components.mkdir(parents=True)
    pre_existing_graph = components / "Graph.tsx"
    pre_existing_graph.write_text("// pre-existing Graph\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--no-overlay"],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "overlay: skipped (--no-overlay)" in result.output
    # Files in the workspace are untouched.
    assert upstream.is_file()  # rename did NOT happen
    assert not (emitters / "_upstreamContentIndex.tsx").is_file()
    assert pre_existing_graph.read_text(encoding="utf-8") == "// pre-existing Graph\n"


def test_print_overlay_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--print-overlay`` lists the plan and exits without mutating anything."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    upstream = emitters / "contentIndex.tsx"
    upstream_content = "// upstream stock\n"
    upstream.write_text(upstream_content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--print-overlay"],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "overlay plan for" in result.output
    assert "rename:" in result.output
    assert "copy:" in result.output
    # No subprocess invocation.
    assert run_mock.call_count == 0
    # Workspace is unchanged.
    assert upstream.is_file()
    assert upstream.read_text(encoding="utf-8") == upstream_content
    assert not (emitters / "_upstreamContentIndex.tsx").is_file()
    # Real overlay sources land nowhere.
    assert not (workspace / "quartz" / "components" / "Graph.tsx").exists()


def test_print_overlay_already_applied_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """`already_applied` rename state surfaces a distinct message."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    (emitters / "_upstreamContentIndex.tsx").write_text(
        "// already renamed\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--print-overlay"],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "rename: already applied" in result.output


def test_print_overlay_missing_both_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """`missing_both` rename state surfaces a distinct message."""
    # Setup — workspace exists but the emitters dir has neither contentIndex.
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--print-overlay"],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "rename: skipped" in result.output


def test_overlay_runs_before_subprocess_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay copy must complete BEFORE ``npx quartz build`` fires.

    Strategy: install side-effects on ``apply_overlay`` and
    ``subprocess.run`` that append a tag to a shared list. Then assert
    the apply tag precedes the run tag. This is the most direct way to
    verify call order without coupling to internal flow.
    """
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    call_order: list[str] = []

    def fake_apply(plan: Any) -> list[tuple[Path, Path]]:
        call_order.append("apply_overlay")
        return []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        call_order.append("subprocess.run")
        return _completed()

    monkeypatch.setattr("brain.cli.apply_overlay", fake_apply)
    monkeypatch.setattr("brain.cli.subprocess.run", fake_run)

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert call_order == ["apply_overlay", "subprocess.run"], (
        f"expected apply_overlay before subprocess.run, got {call_order}"
    )


def test_overlay_then_build_uses_mock_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same call-order contract, expressed via ``mock.Mock`` + parent.attach_mock.

    The ``parent`` mock records calls across both children in chronological
    order — this is the canonical way to assert "X happens before Y" with
    the stdlib mock library, and complements the simpler list-based check
    in ``test_overlay_runs_before_subprocess_run``.
    """
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    parent = mock.Mock()
    apply_mock = mock.Mock(return_value=[])
    run_mock = mock.Mock(return_value=_completed())
    parent.attach_mock(apply_mock, "apply_overlay")
    parent.attach_mock(run_mock, "subprocess_run")

    monkeypatch.setattr("brain.cli.apply_overlay", apply_mock)
    monkeypatch.setattr("brain.cli.subprocess.run", run_mock)

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 0, result.output
    method_names = [name for name, _, _ in parent.mock_calls]
    assert method_names == ["apply_overlay", "subprocess_run"], (
        f"expected apply before run on parent mock, got {method_names}"
    )
    apply_mock.assert_called_once()
    run_mock.assert_called_once()


def test_overlay_error_during_apply_aborts_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``apply_overlay`` raises ``OverlayError``, the build does not run."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_apply(plan: Any) -> list[tuple[Path, Path]]:
        raise OverlayError("simulated overlay failure")

    run_mock = mock.Mock()
    monkeypatch.setattr("brain.cli.apply_overlay", fake_apply)
    monkeypatch.setattr("brain.cli.subprocess.run", run_mock)

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 2
    combined = result.output + (result.stderr if result.stderr else "")
    assert "simulated overlay failure" in combined
    run_mock.assert_not_called()


def test_overlay_apply_logs_rename_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """When apply renames the upstream emitter, the CLI logs that fact.

    Forces the plan into ``rename != None`` by seeding an upstream
    ``contentIndex.tsx`` in the workspace, then letting the real
    ``apply_overlay`` run with subprocess mocked. The log line is the
    only externally visible signal that the rename happened.
    """
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    (emitters / "contentIndex.tsx").write_text("// upstream\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "overlay: renamed contentIndex.tsx → _upstreamContentIndex.tsx" in result.output
    assert (emitters / "_upstreamContentIndex.tsx").is_file()
    assert not (emitters / "contentIndex.tsx").is_file()


def test_overlay_apply_logs_already_applied_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """The ``already_applied`` rename state surfaces a distinct apply echo."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    (emitters / "_upstreamContentIndex.tsx").write_text(
        "// already renamed\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 0, result.output
    assert "overlay: rename already applied" in result.output


def test_overlay_plan_error_aborts_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planning-time ``OverlayError`` exits before any apply or build."""
    # Setup
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_plan(repo: Path, qd: Path) -> Any:
        raise OverlayError("simulated plan failure")

    apply_mock = mock.Mock()
    run_mock = mock.Mock()
    monkeypatch.setattr("brain.cli.plan_overlay", fake_plan)
    monkeypatch.setattr("brain.cli.apply_overlay", apply_mock)
    monkeypatch.setattr("brain.cli.subprocess.run", run_mock)

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify
    assert result.exit_code == 2
    combined = result.output + (result.stderr if result.stderr else "")
    assert "simulated plan failure" in combined
    apply_mock.assert_not_called()
    run_mock.assert_not_called()
