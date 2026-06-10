"""``brain review`` — periodic synthesis over the corpus.

Plan 10 contributes the ``weekly`` leg (this package's :mod:`weekly` /
:mod:`render` / :mod:`emit`). The package is the shared home for the unified
``brain review`` subcommand tree; Plan 03 later adds ``scan`` / ``list`` /
``dismiss`` alongside (``review/scans.py`` + ``review/queries.py``) without
restructuring what lands here.
"""
from __future__ import annotations

from .emit import emit_weekly_page
from .render import render_weekly_json, render_weekly_md, render_weekly_rich
from .weekly import ThemeBlock, WeeklyReport, build_weekly_report

__all__ = [
    "ThemeBlock",
    "WeeklyReport",
    "build_weekly_report",
    "emit_weekly_page",
    "render_weekly_json",
    "render_weekly_md",
    "render_weekly_rich",
]
