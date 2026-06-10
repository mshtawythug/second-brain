"""``brain review`` — periodic synthesis over the corpus.

Plan 10 contributes the ``weekly`` leg (this package's :mod:`weekly` /
:mod:`render` / :mod:`emit`). Plan 03 adds the ``scan`` / ``list`` / ``dismiss``
leg (:mod:`scans` + :mod:`queries`) for contradiction + staleness detection.
The package is the shared home for the unified ``brain review`` subcommand tree.
"""
from __future__ import annotations

from .emit import emit_weekly_page
from .render import render_weekly_json, render_weekly_md, render_weekly_rich
from .scans import ReviewFinding, run_conflict_scan, run_staleness_scan
from .weekly import ThemeBlock, WeeklyReport, build_weekly_report

__all__ = [
    "ReviewFinding",
    "ThemeBlock",
    "WeeklyReport",
    "build_weekly_report",
    "emit_weekly_page",
    "render_weekly_json",
    "render_weekly_md",
    "render_weekly_rich",
    "run_conflict_scan",
    "run_staleness_scan",
]
