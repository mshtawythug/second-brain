"""Unit tests for the T5 ``build_partial`` Python wrapper.

All tests mock ``subprocess.run`` (and ``shutil.which`` where needed) via
``unittest.mock.patch`` — no real Node.js process is spawned.  This keeps
the suite fast, deterministic, and DB-free even without a live Quartz
workspace.

Coverage targets per T5 spec:
  - Success path with elapsed_ms parse
  - Stdout parse failure → elapsed_ms=-1 sentinel
  - Each exit code 1-6 → correct PartialBuildFailureKind
  - Unknown nonzero (exit 99) → UNKNOWN_NONZERO
  - Missing node binary → MISSING_NODE
  - Missing bootstrap-cli.mjs → MISSING_BOOTSTRAP
  - Subprocess timeout → SUBPROCESS_TIMEOUT
  - Stderr surfacing: error message contains slug + stderr tail
  - Argv construction: subprocess.run called with exact argument list
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain.wiki.build_partial import (
    _STDERR_TAIL_LIMIT,
    BrainWikiPartialBuildError,
    PartialBuildFailureKind,
    PartialBuildResult,
    run_build_partial,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Return a fake CompletedProcess to return from a mocked subprocess.run."""
    result: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace ROOT with quartz/bootstrap-cli.mjs present.

    Mirrors the real Quartz convention: workspace root = ``<vault>/.quartz``,
    bootstrap located at ``<workspace_root>/quartz/bootstrap-cli.mjs``.
    """
    ws = tmp_path / "workspace"
    (ws / "quartz").mkdir(parents=True)
    (ws / "quartz" / "bootstrap-cli.mjs").write_text("// stub\n", encoding="utf-8")
    return ws


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """A minimal vault root directory."""
    vd = tmp_path / "vault"
    vd.mkdir()
    return vd


@pytest.fixture()
def build_dir(tmp_path: Path) -> Path:
    """A build output directory."""
    bd = tmp_path / "current"
    bd.mkdir()
    return bd


# ---------------------------------------------------------------------------
# 1. Success path — subprocess returns exit 0 with elapsed_ms line
# ---------------------------------------------------------------------------


def test_success_returns_partial_build_result(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """Exit 0 + valid stdout → PartialBuildResult with parsed elapsed_ms."""
    slug = "my-note"
    stdout = f"wiki: build-partial slug={slug} elapsed=120ms\n"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(0, stdout=stdout),
        ),
    ):
        result = run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    assert isinstance(result, PartialBuildResult)
    assert result.slug == slug
    assert result.elapsed_ms == 120
    assert result.stdout == stdout
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# 2. Stdout parse failure → elapsed_ms sentinel -1
# ---------------------------------------------------------------------------


def test_success_unrecognised_stdout_yields_sentinel(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """Exit 0 but no 'elapsed=Nms' pattern in stdout → elapsed_ms=-1."""
    slug = "my-note"
    stdout = "some unexpected output from node\n"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(0, stdout=stdout),
        ),
    ):
        result = run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    assert result.slug == slug
    assert result.elapsed_ms == -1  # sentinel — parse failed but exit was 0


# ---------------------------------------------------------------------------
# 3-8. Exit codes 1-6 → correct PartialBuildFailureKind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "expected_kind", "stderr"),
    [
        (
            1,
            PartialBuildFailureKind.MANIFEST_OR_CONTENTMAP_UNREADABLE,
            "manifest not found — full build required\n",
        ),
        (
            2,
            PartialBuildFailureKind.ENVELOPE_MISMATCH,
            "envelope mismatch: manifest=1/abc contentmap=1/def\n",
        ),
        (
            3,
            PartialBuildFailureKind.SLUG_NOT_IN_MANIFEST,
            "slug not in manifest — full build required: my-note\n",
        ),
        (
            4,
            PartialBuildFailureKind.SLUG_NOT_IN_CONTENTMAP,
            "slug not in contentmap — full build required: my-note\n",
        ),
        (
            5,
            PartialBuildFailureKind.EMITTER_FAILED,
            "partial emit failed in ContentPage: TypeError: cannot read null\n",
        ),
        (
            6,
            PartialBuildFailureKind.UNSUPPORTED_SLUG_SCOPE,
            "scope: full build required for slug=my-note\n",
        ),
    ],
)
def test_exit_code_maps_to_correct_kind(
    workspace: Path,
    vault_dir: Path,
    build_dir: Path,
    exit_code: int,
    expected_kind: PartialBuildFailureKind,
    stderr: str,
) -> None:
    """Each exit code 1-6 raises BrainWikiPartialBuildError with the right kind."""
    slug = "my-note"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(exit_code, stderr=stderr),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    err = exc_info.value
    assert err.kind == expected_kind, (
        f"exit {exit_code}: expected kind={expected_kind.value}, got {err.kind.value}"
    )
    assert err.slug == slug


# ---------------------------------------------------------------------------
# 9. Unknown nonzero (exit 99) → UNKNOWN_NONZERO
# ---------------------------------------------------------------------------


def test_unknown_nonzero_exit_code(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """Exit code not in 1-6 (e.g. 99) maps to UNKNOWN_NONZERO."""
    slug = "my-note"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(99, stderr="segfault\n"),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    assert exc_info.value.kind == PartialBuildFailureKind.UNKNOWN_NONZERO
    assert exc_info.value.slug == slug


# ---------------------------------------------------------------------------
# 10. Missing node binary → MISSING_NODE
# ---------------------------------------------------------------------------


def test_missing_node_raises_missing_node_kind(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """shutil.which('node') returning None raises MISSING_NODE."""
    with (
        patch("brain.wiki.build_partial.shutil.which", return_value=None),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug="my-note",
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    err = exc_info.value
    assert err.kind == PartialBuildFailureKind.MISSING_NODE
    assert err.slug == "my-note"
    assert "node" in str(err).lower()


# ---------------------------------------------------------------------------
# 11. Missing bootstrap-cli.mjs → MISSING_BOOTSTRAP
# ---------------------------------------------------------------------------


def test_missing_bootstrap_raises_missing_bootstrap_kind(
    tmp_path: Path, vault_dir: Path, build_dir: Path
) -> None:
    """workspace_dir without bootstrap-cli.mjs raises MISSING_BOOTSTRAP."""
    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug="my-note",
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=empty_workspace,
        )

    err = exc_info.value
    assert err.kind == PartialBuildFailureKind.MISSING_BOOTSTRAP
    assert err.slug == "my-note"
    assert "bootstrap-cli.mjs" in str(err)


# ---------------------------------------------------------------------------
# 12. Subprocess timeout → SUBPROCESS_TIMEOUT
# ---------------------------------------------------------------------------


def test_subprocess_timeout_raises_timeout_kind(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """subprocess.TimeoutExpired raises SUBPROCESS_TIMEOUT."""
    slug = "my-note"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["node"], timeout=30.0),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
            timeout_s=30.0,
        )

    err = exc_info.value
    assert err.kind == PartialBuildFailureKind.SUBPROCESS_TIMEOUT
    assert err.slug == slug
    assert "timed out" in str(err).lower()


# ---------------------------------------------------------------------------
# 13. Stderr surfacing — message contains slug name + stderr tail
# ---------------------------------------------------------------------------


def test_stderr_surfaced_in_error_message(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """Error message on nonzero exit includes the slug and a tail of stderr."""
    slug = "important-note"
    long_stderr = "x" * 1000 + "IMPORTANT_TAIL"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(5, stderr=long_stderr),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    error_str = str(exc_info.value)
    assert slug in error_str, f"slug '{slug}' missing from error message: {error_str!r}"
    assert "IMPORTANT_TAIL" in error_str, (
        f"stderr tail missing from error message: {error_str!r}"
    )
    # Confirm the full stderr is NOT included (tail limit enforced).
    assert len(error_str) < len(long_stderr), (
        "error message appears to contain the full stderr instead of a truncated tail"
    )


def test_stderr_tail_limit_applied(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """Stderr is trimmed to _STDERR_TAIL_LIMIT chars in the error message."""
    slug = "my-note"
    # Construct stderr exactly twice the limit to verify trimming.
    long_stderr = "A" * _STDERR_TAIL_LIMIT + "B" * _STDERR_TAIL_LIMIT

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(1, stderr=long_stderr),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    error_str = str(exc_info.value)
    # The tail of stderr (all Bs) must be present; the leading As must be absent.
    assert "B" * _STDERR_TAIL_LIMIT in error_str, (
        "expected stderr tail (Bs) in error message"
    )
    assert "A" * _STDERR_TAIL_LIMIT not in error_str, (
        "full leading-A section of stderr should be trimmed from error message"
    )


# ---------------------------------------------------------------------------
# 14. Argv construction — subprocess.run called with exact argument list
# ---------------------------------------------------------------------------


def test_argv_construction(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """subprocess.run is called with the exact argv + kwargs contract."""
    slug = "my-note"
    node_bin = "/usr/local/bin/node"
    stdout = f"wiki: build-partial slug={slug} elapsed=42ms\n"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value=node_bin),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(0, stdout=stdout),
        ) as mock_run,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
            timeout_s=45.0,
        )

    # workspace fixture creates quartz/bootstrap-cli.mjs one level inside the root.
    expected_argv = [
        node_bin,
        str(workspace / "quartz" / "bootstrap-cli.mjs"),
        "build-partial",
        "--directory",
        str(vault_dir),
        "--output",
        str(build_dir),
        "--slug",
        slug,
    ]
    mock_run.assert_called_once_with(
        expected_argv,
        cwd=str(workspace),  # cwd = workspace ROOT, not bootstrap-containing subdir
        capture_output=True,
        text=True,
        timeout=45.0,
        env=os.environ,
        check=False,
    )


# ---------------------------------------------------------------------------
# 15. Relative workspace input — resolved to absolute before subprocess call
# ---------------------------------------------------------------------------


def test_relative_workspace_resolved_to_absolute(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """A relative workspace_dir Path is resolved to absolute before subprocess call.

    Regression: if workspace_dir is not resolved, cwd passed to subprocess.run
    would be a relative path, which is silently wrong when the caller's cwd
    differs from the intended workspace root.
    """
    slug = "my-note"
    node_bin = "/usr/local/bin/node"
    stdout = f"wiki: build-partial slug={slug} elapsed=10ms\n"

    # We pass an already-absolute path via the fixture but spy on what cwd
    # subprocess.run actually receives.  The key invariant is that cwd must be
    # an absolute path string (os.path.isabs), regardless of what was passed in.
    with (
        patch("brain.wiki.build_partial.shutil.which", return_value=node_bin),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            return_value=_make_completed(0, stdout=stdout),
        ) as mock_run,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    _, call_kwargs = mock_run.call_args
    cwd_used = call_kwargs["cwd"]
    assert os.path.isabs(cwd_used), (
        f"subprocess cwd must be absolute, got: {cwd_used!r}"
    )
    # Also verify the argv bootstrap path is absolute.
    argv_used = mock_run.call_args[0][0]
    assert os.path.isabs(argv_used[1]), (
        f"bootstrap-cli.mjs path in argv must be absolute, got: {argv_used[1]!r}"
    )


# ---------------------------------------------------------------------------
# 16. OSError on subprocess launch → UNKNOWN_NONZERO
# ---------------------------------------------------------------------------


def test_subprocess_oserror_raises_unknown_nonzero(
    workspace: Path, vault_dir: Path, build_dir: Path
) -> None:
    """OSError raised by subprocess.run (e.g. cwd vanished) maps to UNKNOWN_NONZERO.

    Regression: before Fix #3, an OSError would propagate uncaught rather than
    being wrapped in a BrainWikiPartialBuildError with a structured kind.
    """
    slug = "my-note"

    with (
        patch("brain.wiki.build_partial.shutil.which", return_value="/usr/bin/node"),
        patch(
            "brain.wiki.build_partial.subprocess.run",
            side_effect=OSError("No such file or directory: '/vanished/cwd'"),
        ),
        pytest.raises(BrainWikiPartialBuildError) as exc_info,
    ):
        run_build_partial(
            slug=slug,
            vault_dir=vault_dir,
            build_dir=build_dir,
            workspace_dir=workspace,
        )

    err = exc_info.value
    assert err.kind == PartialBuildFailureKind.UNKNOWN_NONZERO
    assert err.slug == slug
    assert "failed to launch" in str(err).lower()
