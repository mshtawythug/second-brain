"""Vault-emit tests for the weekly review page (``brain.review.emit``).

Writes go to ``tmp_path`` — never the real vault. Covers path convention,
directory creation, and idempotent overwrite.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from brain.review.emit import emit_weekly_page
from brain.review.weekly import WeeklyReport


def _report() -> WeeklyReport:
    return WeeklyReport(
        week="2026-W23",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
        generated_date=date(2026, 6, 9),
        themes=[],
        activity=[],
        open_loops=[],
        ingested=[],
        key_people=[],
        graph_used=False,
        vault_paths={},
    )


def test_emit_weekly_page_writes_expected_path(tmp_path: Path) -> None:
    target = emit_weekly_page(tmp_path, _report())
    assert target == tmp_path / "reviews" / "2026-W23.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("---\n")


def test_emit_weekly_page_creates_reviews_dir(tmp_path: Path) -> None:
    assert not (tmp_path / "reviews").exists()
    emit_weekly_page(tmp_path, _report())
    assert (tmp_path / "reviews").is_dir()


def test_emit_weekly_page_is_idempotent(tmp_path: Path) -> None:
    first = emit_weekly_page(tmp_path, _report())
    bytes_first = first.read_bytes()
    second = emit_weekly_page(tmp_path, _report())
    assert second == first
    assert second.read_bytes() == bytes_first
