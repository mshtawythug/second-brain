"""Routing tests for brain.wiki.build_watcher fast-path / full-build dispatch.

Exercises the T6a routing layer introduced in ``build_watcher._run_build``:
- ``_FASTPATH_ENABLED`` env gate
- single-file vs. multi-file batch detection
- unsupported-slug pre-check (index / tags/* / */index)
- classifier dispatch (TRIVIAL → partial, NON_TRIVIAL → full)
- ``BrainWikiPartialBuildError`` recovery (all PartialBuildFailureKind values)
- ``.git/`` event filtering (existing watcher behaviour still intact)
- ``_is_single_md_file`` helper unit tests (bonus coverage)

Mocking strategy — per CLAUDE.md item 13:
    ``mocker.patch`` / ``unittest.mock.patch`` with automatic cleanup.
    Patch targets are the names inside ``brain.wiki.build_watcher``
    (i.e. ``brain.wiki.build_watcher.build_and_swap``, etc.) because
    those are the references the module under test resolves at call time.
    NO monkey-patching, NO direct attribute assignment on imported modules.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from watchdog.events import FileModifiedEvent

from brain.wiki.build_partial import (
    BrainWikiPartialBuildError,
    PartialBuildFailureKind,
    PartialBuildResult,
)
from brain.wiki.build_swap import BuildResult
from brain.wiki.build_watcher import _Handler, _is_single_md_file, _WatcherState
from brain.wiki.edit_classifier import ClassificationResult, EditClassification

# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _make_full_result(build_id: str = "20260509-120000-abc123") -> BuildResult:
    """Synthetic :class:`BuildResult` for tests that don't run a real build."""
    return BuildResult(
        build_dir=Path(f"/tmp/builds/{build_id}"),
        build_id=build_id,
        elapsed_seconds=0.5,
        pruned=[],
        method="output-flag",
    )


def _trivial_result(slug: str) -> ClassificationResult:
    return ClassificationResult(
        classification=EditClassification.TRIVIAL,
        reason="fingerprint unchanged",
        slug=slug,
        old_fingerprint="abc123",
        new_fingerprint="abc123",
    )


def _non_trivial_result(slug: str, reason: str = "fingerprint changed") -> ClassificationResult:
    return ClassificationResult(
        classification=EditClassification.NON_TRIVIAL,
        reason=reason,
        slug=slug,
        old_fingerprint="abc123",
        new_fingerprint="def456",
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Bare vault root directory."""
    return tmp_path


@pytest.fixture
def workspace(vault: Path) -> Path:
    """Minimal Quartz workspace directory (``<vault>/.quartz``)."""
    ws = vault / ".quartz"
    ws.mkdir()
    return ws


@pytest.fixture
def current_build_dir(workspace: Path) -> Path:
    """Create a fake build dir and point ``workspace/current`` at it.

    Returns the resolved build directory path so tests can assert the exact
    value passed to ``run_build_partial``.
    """
    builds = workspace / "builds"
    builds.mkdir()
    build_dir = builds / "20260509-120000-abc123"
    build_dir.mkdir()
    # Relative symlink — mirrors what _atomic_swap does in build_swap.py.
    (workspace / "current").symlink_to(Path("builds") / "20260509-120000-abc123")
    return build_dir


@pytest.fixture
def handler(vault: Path) -> _Handler:
    """A :class:`_Handler` with ``quartz_dir=None`` and a no-op refresh runner.

    ``refresh_runner=lambda: None`` prevents the daemon thread from attempting
    the real ``Config.load()`` / ``refresh_related()`` path in any test that
    reaches :meth:`_schedule_refresh_related`.
    """
    state = _WatcherState()
    return _Handler(
        state=state,
        vault=vault,
        quartz_dir=None,
        debounce_seconds=1.5,
        keep=3,
        on_build=None,
        refresh_runner=lambda: None,
    )


# ---------------------------------------------------------------------------
# _is_single_md_file — unit tests (bonus coverage).
# ---------------------------------------------------------------------------


def test_is_single_md_file_single_md_returns_true() -> None:
    """A frozen set with exactly one ``.md`` path is eligible for fast path."""
    assert _is_single_md_file(frozenset({Path("/vault/notes/foo.md")})) is True


def test_is_single_md_file_empty_batch_returns_false() -> None:
    """An empty batch is not a single-md edit."""
    assert _is_single_md_file(frozenset()) is False


def test_is_single_md_file_single_non_md_returns_false() -> None:
    """A single non-Markdown file (image, CSS) is not eligible for fast path."""
    assert _is_single_md_file(frozenset({Path("/vault/image.png")})) is False


def test_is_single_md_file_multi_file_returns_false() -> None:
    """Two ``.md`` files → multi-file batch → not eligible."""
    assert _is_single_md_file(frozenset({Path("/vault/a.md"), Path("/vault/b.md")})) is False


# ---------------------------------------------------------------------------
# T6a routing tests (12 required scenarios).
# ---------------------------------------------------------------------------


# 1. Trivial single-file edit, supported slug → partial called; full NOT called.
def test_trivial_single_md_uses_partial_build(
    vault: Path,
    current_build_dir: Path,  # also creates workspace fixture
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """Trivial fingerprint + supported slug → ``run_build_partial`` is called once."""
    note = vault / "notes" / "foo.md"
    note.parent.mkdir(parents=True)
    note.write_text("body prose only, no structural change")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/foo"),
    )
    partial_mock = mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        return_value=PartialBuildResult(
            slug="notes/foo", elapsed_ms=250, stdout="", stderr=""
        ),
    )
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    partial_mock.assert_called_once()
    kw = partial_mock.call_args.kwargs
    assert kw["slug"] == "notes/foo"
    assert kw["vault_dir"] == vault
    # build_dir is the resolved target of workspace/current.
    expected_build_dir = (vault / ".quartz" / "current").resolve()
    assert kw["build_dir"] == expected_build_dir
    assert kw["workspace_dir"] == vault / ".quartz"
    full_mock.assert_not_called()


# 2. Non-trivial single-file edit → full build called; partial NOT called.
def test_non_trivial_single_md_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """A NON_TRIVIAL classification routes to full build without calling partial."""
    note = vault / "notes" / "bar.md"
    note.parent.mkdir(parents=True)
    note.write_text("## New Heading added — structural change")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_non_trivial_result("notes/bar"),
    )
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    full_mock.assert_called_once()
    partial_mock.assert_not_called()


# 3. Multi-file batch → full build; classifier + partial NOT called.
def test_multi_file_batch_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """Two files in one debounce window → full build; classifier not consulted."""
    note_a = vault / "a.md"
    note_b = vault / "b.md"
    note_a.write_text("A")
    note_b.write_text("B")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note_a, note_b}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 4. Unsupported slug starting with tags/ → full build; classifier + partial NOT called.
def test_unsupported_slug_tags_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """``tags/foo`` slug is unsupported; Python pre-check skips classifier."""
    tags_file = vault / "tags" / "foo.md"
    tags_file.parent.mkdir(parents=True)
    tags_file.write_text("tag page content")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({tags_file}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 5. Unsupported slug ending with /index → full build; classifier + partial NOT called.
def test_unsupported_slug_dir_index_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """``folder/index`` slug ends with ``/index``; pre-check skips classifier."""
    index_file = vault / "folder" / "index.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("folder index content")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({index_file}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 6. Root index slug (slug == "index") → full build; classifier + partial NOT called.
def test_unsupported_slug_root_index_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """``index.md`` slugifies to ``"index"``; pre-check routes to full build."""
    index_file = vault / "index.md"
    index_file.write_text("vault index page")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({index_file}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 7. Slug computation raises ValueError (path outside vault) → full build.
def test_slug_outside_vault_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """Path outside vault → ``slugify_source_path`` raises ValueError → full build."""
    outside = vault.parent / "outside_vault.md"  # sibling of vault; guaranteed outside

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({outside}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 8. Classifier returns NON_TRIVIAL → full build; partial NOT called.
def test_classifier_non_trivial_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """classify_edit returns NON_TRIVIAL → classifier is called; partial is NOT."""
    note = vault / "daily" / "2026-05-09.md"
    note.parent.mkdir(parents=True)
    note.write_text("new section added — structural change")

    classify_mock = mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_non_trivial_result("daily/2026-05-09", reason="fingerprint changed"),
    )
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    classify_mock.assert_called_once()
    full_mock.assert_called_once()
    partial_mock.assert_not_called()


# 9. run_build_partial raises BrainWikiPartialBuildError(EMITTER_FAILED) →
#    full build + warning logged with kind.
def test_partial_build_emitter_failed_falls_back_to_full_build(
    vault: Path,
    current_build_dir: Path,
    handler: _Handler,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EMITTER_FAILED → full build recovery; kind is included in the warning log."""
    note = vault / "notes" / "foo.md"
    note.parent.mkdir(parents=True)
    note.write_text("prose body")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/foo"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        side_effect=BrainWikiPartialBuildError(
            "emitter blew up",
            kind=PartialBuildFailureKind.EMITTER_FAILED,
            slug="notes/foo",
        ),
    )
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with caplog.at_level(logging.WARNING, logger="brain.wiki.build_watcher"):
        handler._run_build(frozenset({note}))

    full_mock.assert_called_once()
    assert "emitter-failed" in caplog.text, (
        "Warning log must include the failure kind value for telemetry"
    )
    assert "emitter blew up" in caplog.text, (
        "Warning log must include str(exc) so stderr tail / exit code are visible"
    )


# 10. Every PartialBuildFailureKind → full build recovery (parametrized).
@pytest.mark.parametrize("kind", list(PartialBuildFailureKind))
def test_partial_build_all_failure_kinds_fall_back_to_full_build(
    vault: Path,
    current_build_dir: Path,
    handler: _Handler,
    mocker: MockerFixture,
    kind: PartialBuildFailureKind,
) -> None:
    """Any :class:`BrainWikiPartialBuildError` kind triggers immediate full build."""
    note = vault / "notes" / "doc.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/doc"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        side_effect=BrainWikiPartialBuildError(
            f"failure kind={kind.value}",
            kind=kind,
            slug="notes/doc",
        ),
    )
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    full_mock.assert_called_once()


# 11. BRAIN_FASTPATH_ENABLED=false → always full build; classifier + partial NOT called.
def test_fastpath_disabled_always_uses_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """Patching ``_FASTPATH_ENABLED=False`` forces all edits through full build."""
    mocker.patch("brain.wiki.build_watcher._FASTPATH_ENABLED", new=False)

    note = vault / "notes" / "foo.md"
    note.parent.mkdir(parents=True)
    note.write_text("body")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# 13. classify_edit raises an unexpected exception → catch-all logs ERROR and falls back
#     to full build (regression for the silent-loss bug in T6a).
def test_classify_edit_raises_unexpected_falls_back_to_full_build(
    vault: Path,
    handler: _Handler,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RuntimeError from classify_edit must be caught by _run_build's catch-all.

    Before the T6a fix, an unexpected exception inside _try_fast_path would
    escape the timer thread, causing _drain_pending to clear ``running`` without
    rebuilding — the edit was silently lost.  The fix wraps the fast-path
    dispatch in a bare ``except Exception`` that logs and falls back to full build.
    """
    note = vault / "notes" / "crash.md"
    note.parent.mkdir(parents=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        side_effect=RuntimeError("boom"),
    )
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with caplog.at_level(logging.ERROR, logger="brain.wiki.build_watcher"):
        handler._run_build(frozenset({note}))

    full_mock.assert_called_once()
    assert "fast-path routing failed unexpectedly" in caplog.text


# 14. run_build_partial raises a plain RuntimeError (not BrainWikiPartialBuildError) →
#     catch-all in _run_build logs ERROR and falls back to full build.
def test_run_build_partial_raises_plain_runtime_error_falls_back(
    vault: Path,
    current_build_dir: Path,
    handler: _Handler,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected RuntimeError from run_build_partial must reach the _run_build catch-all.

    This is distinct from BrainWikiPartialBuildError (expected failure, handled
    inside _try_fast_path).  An unexpected exception must NOT silently drop the
    edit — it must be caught, logged at ERROR level with traceback, and cause a
    full build fallback.
    """
    note = vault / "notes" / "crash.md"
    note.parent.mkdir(parents=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/crash"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        side_effect=RuntimeError("boom"),
    )
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with caplog.at_level(logging.ERROR, logger="brain.wiki.build_watcher"):
        handler._run_build(frozenset({note}))

    full_mock.assert_called_once()
    assert "fast-path routing failed unexpectedly" in caplog.text


# 12. Single .git/ file event → IGNORED entirely; changed_paths stays empty.
def test_git_event_does_not_populate_changed_paths(
    vault: Path,
    handler: _Handler,
) -> None:
    """Verify T6a path accumulation doesn't break existing ``.git/`` filtering.

    Events under ``<vault>/.git/`` must be dropped by ``_should_trigger``
    before they reach ``changed_paths``.  The debounce timer must also not
    be scheduled (no build at all — existing watcher behaviour preserved).
    """
    git_head_path = vault / ".git" / "HEAD"
    event = FileModifiedEvent(str(git_head_path))

    handler.on_any_event(event)

    with handler._state.lock:
        assert not handler._state.current_batch, (
            ".git/ events must not be accumulated in current_batch"
        )
        assert not handler._state.pending_batch, (
            ".git/ events must not be accumulated in pending_batch"
        )
        assert handler._state.timer is None, (
            ".git/ events must not schedule the debounce timer"
        )
