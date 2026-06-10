"""Vault I/O for weekly review pages.

One responsibility: where the page lands on disk and how it is written. The
page is rendered by :func:`brain.review.render.render_weekly_md` and written
atomically (sibling tempfile + ``os.replace``), so a re-run for the same week
overwrites byte-for-byte — idempotent.
"""
from __future__ import annotations

from pathlib import Path

from ..vault._atomic import atomic_write_text
from .render import render_weekly_md
from .weekly import WeeklyReport


def emit_weekly_page(vault_root: Path, report: WeeklyReport) -> Path:
    """Write the weekly review page to ``<vault_root>/reviews/<week>.md``.

    Creates the ``reviews/`` directory if absent. Returns the written path.
    Re-running with an equal report overwrites atomically with identical bytes
    (idempotent), because :func:`render_weekly_md` is deterministic.
    """
    target = vault_root / "reviews" / f"{report.week}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, render_weekly_md(report))
    return target
