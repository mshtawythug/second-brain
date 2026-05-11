"""Tests for the Quartz overlay step (helper layer + render CLI integration).

Two layers covered here:

1. ``brain.vault.quartz_overlay`` — pure planning + apply functions.
   Tests use ``tmp_path`` to stand up a fake overlay tree and a fake
   Quartz workspace, then exercise ``plan_overlay`` / ``apply_overlay``
   directly. ``_overlay_source_root`` is monkeypatched so the tests
   don't depend on the real ``brain/quartz_overrides/`` package contents.
2. ``brain vault render`` CLI — the overlay/no-overlay/print-overlay
   flag wiring + the order-of-operations contract that the overlay
   apply step runs strictly before ``subprocess.run``.

We never invoke ``npx`` or any real Quartz process — all subprocess
calls are mocked. The four-phase test pattern (setup → exercise →
verify → teardown) is followed throughout; ``tmp_path`` and pytest
fixtures handle teardown automatically.
"""

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


def _make_fake_overlay_root(
    tmp_path: Path, files: dict[str, str] | None = None
) -> Path:
    """Build a fake overlay tree rooted at ``tmp_path`` and return the root.

    ``files`` maps relative paths (mirroring the ``<quartz_dir>/`` layout) to
    content strings.  Defaults cover both placement classes plus the four
    shapes seen in the real repo (components/, plugins/, styles/, util/,
    scripts/).
    """
    if files is None:
        files = {
            "quartz.layout.ts": "// stub layout\nexport default {}\n",
            "quartz/components/Graph.tsx": "// stub Graph\nexport default null\n",
            "quartz/components/scripts/graph.inline.ts": "// stub inline\n",
            "quartz/plugins/emitters/contentIndex.ts": "// stub emitter\n",
            "quartz/plugins/transformers/derivedFenceMark.ts": "// stub xform\n",
            "quartz/plugins/transformers/reloadSignal.ts": "// stub reload xform\n",
            "quartz/static/reload.js": "// stub reload client\n",
            "quartz/styles/graph.scss": "// stub scss\n",
            "quartz/util/path.ts": "// stub util\n",
        }
    overlay_root = tmp_path
    for rel, content in files.items():
        dest = overlay_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return overlay_root


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


def test_overlay_copies_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file under the overlay root lands at the right place."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)
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
        (
            "quartz/plugins/transformers/reloadSignal.ts",
            "quartz/plugins/transformers/reloadSignal.ts",
        ),
        ("quartz/static/reload.js", "quartz/static/reload.js"),
        ("quartz/styles/graph.scss", "quartz/styles/graph.scss"),
        ("quartz/util/path.ts", "quartz/util/path.ts"),
    }
    actual_pairs = {
        (
            str(src.relative_to(overlay_root)),
            str(dest.relative_to(workspace.resolve())),
        )
        for src, dest in copied
    }
    assert actual_pairs == expected_pairs

    # Verify content survives the copy intact.
    for src, dest in copied:
        assert dest.is_file(), f"missing dest after copy: {dest}"
        assert dest.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_overlay_overwrites_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-populated destinations are overwritten by the overlay copy."""
    # Setup
    overlay_root = _make_fake_overlay_root(
        tmp_path / "overlay",
        files={"quartz/components/Graph.tsx": "// brain Graph (new)\n"},
    )
    workspace = _make_quartz_workspace(tmp_path)
    stub_dest = workspace / "quartz" / "components" / "Graph.tsx"
    stub_dest.parent.mkdir(parents=True, exist_ok=True)
    stub_dest.write_text("// stale stub Graph (old)\n", encoding="utf-8")
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)
    apply_overlay(plan)

    # Verify
    assert stub_dest.read_text(encoding="utf-8") == "// brain Graph (new)\n"


def test_overlay_renames_upstream_contentindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stock ``contentIndex.tsx`` is present, it gets renamed first."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=True)
    upstream = workspace / "quartz" / "plugins" / "emitters" / "contentIndex.tsx"
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    assert upstream.is_file() and not renamed.is_file()
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)
    assert plan.rename is not None
    assert plan.rename_state == "needed"
    apply_overlay(plan)

    # Verify — the rename happened, and the original is gone.
    assert not upstream.is_file()
    assert renamed.is_file()
    assert renamed.read_text(encoding="utf-8") == "// upstream stock emitter\n"


def test_overlay_skips_rename_when_already_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If only ``_upstreamContentIndex.tsx`` exists, no rename is needed."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=False)
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    renamed.write_text("// already renamed\n", encoding="utf-8")
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)

    # Verify
    assert plan.rename is None
    assert plan.rename_state == "already_applied"


def test_overlay_rename_missing_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither contentIndex variant present → ``missing_both`` state."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=False)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)

    # Verify
    assert plan.rename is None
    assert plan.rename_state == "missing_both"


def test_overlay_raises_when_both_contentindex_files_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both upstream and renamed files at once → refuse to auto-resolve."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=True)
    renamed = workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    renamed.write_text("// also already renamed\n", encoding="utf-8")
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(workspace)
    assert "both upstream and renamed contentIndex files exist" in str(excinfo.value)


def test_overlay_raises_when_overrides_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing overlay source dir is a hard failure (broken brain package)."""
    # Setup — point _overlay_source_root at a path that doesn't exist.
    nonexistent = tmp_path / "does_not_exist"
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: nonexistent
    )

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(workspace)
    assert "overlay source directory not found" in str(excinfo.value)


def test_overlay_skips_dotfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Macos metadata + dotfiles are skipped defensively."""
    # Setup
    overlay_root = _make_fake_overlay_root(
        tmp_path / "overlay",
        files={
            "quartz/components/Graph.tsx": "// keep\n",
            ".DS_Store": "macOS metadata\n",
            "quartz/components/.hidden": "should-not-copy\n",
        },
    )
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)

    # Verify — only the non-dotfile is in the plan.
    src_names = {src.name for src, _ in plan.pairs}
    assert "Graph.tsx" in src_names
    assert ".DS_Store" not in src_names
    assert ".hidden" not in src_names


def test_overlay_skips_python_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python package files and bytecode caches are excluded from the overlay."""
    # Setup — put Python packaging artefacts alongside a real overlay file.
    overlay_root = _make_fake_overlay_root(
        tmp_path / "overlay",
        files={
            "quartz/components/Graph.tsx": "// keep\n",
            "__init__.py": "# package marker\n",
        },
    )
    # Also create a simulated __pycache__ entry.
    pycache = overlay_root / "__pycache__"
    pycache.mkdir()
    (pycache / "__init__.cpython-313.pyc").write_bytes(b"fake pyc")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)

    # Verify — only the .tsx file makes it through.
    src_names = {src.name for src, _ in plan.pairs}
    assert "Graph.tsx" in src_names
    assert "__init__.py" not in src_names
    assert "__init__.cpython-313.pyc" not in src_names


def test_overlay_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink under the overlay root pointing outside is rejected."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.tsx").write_text("// evil\n", encoding="utf-8")
    link = overlay_root / "quartz" / "components" / "linked.tsx"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside / "evil.tsx")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise + Verify
    with pytest.raises(OverlayError) as excinfo:
        plan_overlay(workspace)
    assert "escaped" in str(excinfo.value)


def test_overlay_pairs_are_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan pairs come back in deterministic Path-sorted-by-src order."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan = plan_overlay(workspace)

    # Verify — sorted by source ``Path`` (the same order ``rglob`` is
    # piped through inside ``plan_overlay``). Path-sort ≠ str-sort
    # because pathlib normalizes separators, so we sort the Paths
    # directly here too.
    srcs = [src for src, _ in plan.pairs]
    assert srcs == sorted(srcs)


def test_overlay_pairs_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated planning produces an identical plan (deterministic)."""
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )

    # Exercise
    plan_a = plan_overlay(workspace)
    plan_b = plan_overlay(workspace)

    # Verify
    assert plan_a.pairs == plan_b.pairs
    assert plan_a.rename == plan_b.rename
    assert plan_a.rename_state == plan_b.rename_state


def test_overlay_apply_wraps_oserror_as_overlay_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A ``shutil.copy2`` failure surfaces as ``OverlayError`` with context.

    The CLI only catches :class:`OverlayError` at the apply boundary,
    so any raw ``OSError`` from the copy / rename / mkdir operations
    needs to be wrapped — otherwise an FS-mutation failure (perm denied,
    disk full, …) escapes as an unhandled traceback instead of the
    friendly ``typer.secho(..., fg="red") + Exit(2)`` path.
    """
    # Setup
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )
    plan = plan_overlay(workspace)
    mocker.patch(
        "brain.vault.quartz_overlay.shutil.copy2",
        side_effect=PermissionError("perm denied"),
    )

    # Exercise
    with pytest.raises(OverlayError) as excinfo:
        apply_overlay(plan)

    # Verify — message names the operation + paths so logs are debuggable.
    msg = str(excinfo.value)
    assert "overlay copy failed" in msg
    assert "perm denied" in msg
    # __cause__ preserves the original error for traceback chaining.
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_overlay_apply_wraps_rename_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A ``Path.rename`` failure surfaces as ``OverlayError`` with context.

    Counterpart to the copy-side wrap test — the upstream-emitter rename
    runs strictly before any copy, so its failure mode needs the same
    ``OverlayError`` envelope to keep the CLI's single-catch boundary
    valid.
    """
    # Setup — use a workspace with the upstream contentIndex.tsx in
    # place so the rename branch fires.
    overlay_root = _make_fake_overlay_root(tmp_path / "overlay")
    workspace = _make_quartz_workspace(tmp_path, with_upstream_contentindex=True)
    monkeypatch.setattr(
        "brain.vault.quartz_overlay._overlay_source_root", lambda: overlay_root
    )
    plan = plan_overlay(workspace)
    assert plan.rename is not None  # precondition: rename branch will run
    mocker.patch.object(
        Path, "rename", side_effect=PermissionError("read-only fs")
    )

    # Exercise
    with pytest.raises(OverlayError) as excinfo:
        apply_overlay(plan)

    # Verify — operation label, inner message, and chained cause.
    msg = str(excinfo.value)
    assert "overlay rename failed" in msg
    assert "read-only fs" in msg
    assert isinstance(excinfo.value.__cause__, PermissionError)


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


def _make_vault_with_quartz_workspace(
    tmp_path: Path, *, with_upstream_contentindex: bool = False
) -> tuple[Path, Path]:
    """Build the (vault, workspace) pair the CLI tests expect.

    Returns ``(vault_path, workspace_path)`` where ``workspace_path``
    is ``<vault>/.quartz`` populated with the two files
    ``_check_quartz_workspace`` looks for plus the ``quartz/plugins/
    emitters/`` directory the overlay's rename plan inspects. Pass
    ``with_upstream_contentindex=True`` to seed the stock emitter so
    the rename branch fires; default off so each caller is explicit.
    """
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    emitters = workspace / "quartz" / "plugins" / "emitters"
    emitters.mkdir(parents=True)
    if with_upstream_contentindex:
        (emitters / "contentIndex.tsx").write_text(
            "// upstream stock\n", encoding="utf-8"
        )
    return vault, workspace


def _completed(
    returncode: int = 0, *, vault: Path | None = None, output: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """A ``CompletedProcess`` shaped like the real ``npx quartz build`` call.

    The CLI builds the args list ``["npx", "quartz", "build",
    "--directory", <vault>, "--output", <output>]`` and passes it to
    ``subprocess.run``. Mocks return this stand-in so the CLI's
    ``returncode`` check + downstream assertions exercise the real
    arg shape — important for the rare test that inspects ``.args``.
    """
    args: list[str] = ["npx", "quartz", "build"]
    if vault is not None:
        args.extend(["--directory", str(vault)])
    if output is not None:
        args.extend(["--output", str(output)])
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout="", stderr=""
    )


def test_no_overlay_skips_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-overlay`` does not touch the Quartz workspace files."""
    # Setup
    _env(monkeypatch)
    vault, workspace = _make_vault_with_quartz_workspace(
        tmp_path, with_upstream_contentindex=True
    )
    upstream = workspace / "quartz" / "plugins" / "emitters" / "contentIndex.tsx"
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
    assert not (upstream.parent / "_upstreamContentIndex.tsx").is_file()
    assert pre_existing_graph.read_text(encoding="utf-8") == "// pre-existing Graph\n"


def test_print_overlay_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--print-overlay`` lists the plan and exits without mutating anything."""
    # Setup
    _env(monkeypatch)
    vault, workspace = _make_vault_with_quartz_workspace(
        tmp_path, with_upstream_contentindex=True
    )
    upstream = workspace / "quartz" / "plugins" / "emitters" / "contentIndex.tsx"
    upstream_content = upstream.read_text(encoding="utf-8")
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
    assert not (upstream.parent / "_upstreamContentIndex.tsx").is_file()
    # Real overlay sources land nowhere.
    assert not (workspace / "quartz" / "components" / "Graph.tsx").exists()


def test_print_overlay_already_applied_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """`already_applied` rename state surfaces a distinct message."""
    # Setup
    _env(monkeypatch)
    vault, workspace = _make_vault_with_quartz_workspace(tmp_path)
    (
        workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    ).write_text("// already renamed\n", encoding="utf-8")
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
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
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
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
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
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
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
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
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
    vault, workspace = _make_vault_with_quartz_workspace(
        tmp_path, with_upstream_contentindex=True
    )
    emitters = workspace / "quartz" / "plugins" / "emitters"
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
    vault, workspace = _make_vault_with_quartz_workspace(tmp_path)
    (
        workspace / "quartz" / "plugins" / "emitters" / "_upstreamContentIndex.tsx"
    ).write_text("// already renamed\n", encoding="utf-8")
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


def test_overlay_apply_logs_missing_both_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """The ``missing_both`` rename state surfaces a warn-level apply echo.

    Sibling to the rename + already_applied apply-branch tests; this
    one closes the third branch in the CLI's apply-time rename log
    block at ``cli.py:1607-1611``. With neither contentIndex variant
    present the brain wrapper will fail at build time, so the message
    needs to be loud enough that the user knows.
    """
    # Setup — workspace exists but the emitters dir has neither
    # contentIndex.tsx nor _upstreamContentIndex.tsx.
    _env(monkeypatch)
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    # Exercise
    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault)],
    )

    # Verify — message names the missing file and warns about the
    # downstream build failure; substring assertions tolerate minor
    # rewording without re-pinning the whole sentence.
    assert result.exit_code == 0, result.output
    assert "overlay: rename skipped" in result.output
    assert "upstream contentIndex.tsx not" in result.output
    assert "brain wrapper will fail at build time" in result.output


def test_overlay_plan_error_aborts_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planning-time ``OverlayError`` exits before any apply or build."""
    # Setup
    _env(monkeypatch)
    vault, _workspace = _make_vault_with_quartz_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_plan(qd: Path) -> Any:
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
