"""Pure-renderer tests for the weekly review page (``brain.review.render``).

No DB, no Ollama — synthetic ``WeeklyReport`` values exercise determinism,
frontmatter round-trip, section presence, and graceful empty rendering.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

from brain.activity import ActivityDoc, IngestedDoc
from brain.review.render import render_weekly_md, render_weekly_rich
from brain.review.weekly import ThemeBlock, WeeklyReport
from brain.todo import TodoRow
from brain.vault.frontmatter import parse_frontmatter

_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


def _sample_report(*, empty: bool = False) -> WeeklyReport:
    if empty:
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
    activity = [
        ActivityDoc(
            document_id="doc-1",
            title="Synthetic meeting A",
            interaction_count=4,
            last_at=datetime(2026, 6, 3, tzinfo=UTC),
            tags=["topic-alpha"],
        )
    ]
    ingested = [
        IngestedDoc(
            document_id="doc-2",
            title="Synthetic standup",
            ingested_at=datetime(2026, 6, 5, tzinfo=UTC),
            source_kind="krisp",
            tags=[],
        )
    ]
    return WeeklyReport(
        week="2026-W23",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
        generated_date=date(2026, 6, 9),
        themes=[
            ThemeBlock(
                key="comm-1",
                entity_names=["topic-alpha", "project-delta"],
                docs=[("doc-1", "Synthetic meeting A")],
                synthesis="Focus on topic-alpha and project-delta dominated this week.",
            )
        ],
        activity=activity,
        open_loops=[
            TodoRow(
                document_id="doc-1",
                document_title="Synthetic meeting A",
                ingested_at=datetime(2026, 6, 3, tzinfo=UTC),
                state="open",
                text="Follow up on contract draft",
            )
        ],
        ingested=ingested,
        key_people=["person-a", "person-b"],
        graph_used=True,
        vault_paths={
            "doc-1": "_ingested/krisp/2026-06-03-abcd1234-synthetic-meeting-a.md"
        },
    )


def test_render_weekly_md_is_deterministic() -> None:
    report = _sample_report()
    assert render_weekly_md(report) == render_weekly_md(report)


def test_render_weekly_md_frontmatter_round_trips() -> None:
    report = _sample_report()
    fm, _body = parse_frontmatter(render_weekly_md(report))
    assert fm["kind"] == "review"
    assert fm["week"] == "2026-W23"
    assert str(fm["date"]) == "2026-06-09"
    assert fm["tags"] == ["review", "weekly"]


def test_render_weekly_md_week_matches_regex() -> None:
    fm, _ = parse_frontmatter(render_weekly_md(_sample_report()))
    assert _WEEK_RE.match(str(fm["week"]))


def test_render_weekly_md_all_section_headers_present() -> None:
    md = render_weekly_md(_sample_report())
    assert "## Themes this week" in md
    assert "## Activity" in md
    assert "## Open loops" in md
    assert "## New captures" in md
    assert "## Key people" in md


def test_render_weekly_md_emits_wikilink_for_vault_doc() -> None:
    md = render_weekly_md(_sample_report())
    # doc-1 has a vault_path → wiki-link target without .md, with alias.
    assert (
        "[[_ingested/krisp/2026-06-03-abcd1234-synthetic-meeting-a"
        "|Synthetic meeting A]]" in md
    )


def test_render_weekly_md_plain_title_when_no_vault_path() -> None:
    md = render_weekly_md(_sample_report())
    # doc-2 (ingested) has no vault_path → plain title, no wiki-link.
    assert "- Synthetic standup (krisp)" in md


def test_render_weekly_md_empty_sections_render_without_crash() -> None:
    md = render_weekly_md(_sample_report(empty=True))
    assert "_No themes detected this week._" in md
    assert "_No documents interacted with this week._" in md
    assert "_No open loops._" in md
    assert "_No new captures this week._" in md
    assert "_No key people identified._" in md


def test_render_weekly_rich_leads_with_title() -> None:
    out = render_weekly_rich(_sample_report())
    assert out.startswith("Weekly review · 2026-W23")
    assert "person-a · person-b" in out


def test_render_weekly_rich_empty_report() -> None:
    out = render_weekly_rich(_sample_report(empty=True))
    assert "Weekly review · 2026-W23" in out
