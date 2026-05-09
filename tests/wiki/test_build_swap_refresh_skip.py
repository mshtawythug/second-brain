"""Tests for the refresh_related_inline skip path in _refresh_pre_build_adornments.

Verifies that ``_refresh_pre_build_adornments(vault, refresh_related_inline=False)``
calls ``refresh_homepage`` but NOT ``refresh_related``, while
``refresh_related_inline=True`` (the default, used by ``bin/brain-rebuild``) calls both.

Uses ``unittest.mock.patch`` as standard test doubles — NOT production monkey-patching.
The patches target module-level symbols in their home modules, which the lazy imports
inside ``_refresh_pre_build_adornments`` pick up at call time.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from brain.wiki.build_swap import _refresh_pre_build_adornments


def test_refresh_pre_build_adornments_skip_when_flag_false(tmp_path: Path) -> None:
    """Pins: refresh_related_inline=False causes refresh_related to be skipped.

    The wiki build watcher passes ``refresh_related_inline=False`` to keep
    edit-to-UI latency fast (~27–30 s measured, instead of ~95 s pre-closeout). This test asserts
    that ``refresh_related`` is NOT called on that code path, while
    ``refresh_homepage`` (fast, <0.1 s) IS still called so the homepage
    rail stays fresh.
    """
    resolved = tmp_path.expanduser().resolve()
    # Mock config whose vault_path matches the test vault so _replace_vault_path
    # is not called (avoids needing a real dataclass instance).
    mock_cfg = MagicMock()
    mock_cfg.vault_path = resolved

    with (
        patch("brain.config.Config.load", return_value=mock_cfg),
        patch("brain.wiki.build_homepage.refresh_homepage") as spy_homepage,
        patch("brain.wiki.build_related.refresh_related") as spy_related,
    ):
        _refresh_pre_build_adornments(tmp_path, refresh_related_inline=False)

    # homepage must be called (fast; always refreshed)
    spy_homepage.assert_called_once()
    # refresh_related must NOT be called (slow ~73 s; deferred to background thread)
    spy_related.assert_not_called()


def test_refresh_pre_build_adornments_calls_both_when_flag_true(tmp_path: Path) -> None:
    """Pins: refresh_related_inline=True (default) causes both helpers to be called.

    ``bin/brain-rebuild`` uses the default (True) so manual rebuilds always
    emit fresh related-docs JSON synchronously before the Quartz build runs.
    """
    resolved = tmp_path.expanduser().resolve()
    mock_cfg = MagicMock()
    mock_cfg.vault_path = resolved

    with (
        patch("brain.config.Config.load", return_value=mock_cfg),
        patch("brain.wiki.build_homepage.refresh_homepage") as spy_homepage,
        patch("brain.wiki.build_related.refresh_related") as spy_related,
    ):
        _refresh_pre_build_adornments(tmp_path, refresh_related_inline=True)

    spy_homepage.assert_called_once()
    spy_related.assert_called_once()
