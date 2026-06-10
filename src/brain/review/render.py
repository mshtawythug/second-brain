"""Pure renderers for a :class:`~brain.review.weekly.WeeklyReport`.

``render_weekly_md`` produces the vault page (YAML frontmatter + Markdown body);
``render_weekly_rich`` produces a compact terminal summary. Both are pure: the
same ``WeeklyReport`` always yields byte-identical output, which is what makes
the vault write idempotent (re-running a week overwrites with identical bytes).
The only collaborators are the deterministic
:func:`brain.vault.frontmatter.dump_frontmatter` and the wiki-link path helpers.
"""
from __future__ import annotations

from typing import Any

from ..vault.frontmatter import dump_frontmatter
from ..vault.paths import safe_wikilink_alias, strip_md_extension
from .weekly import ThemeBlock, WeeklyReport

_EMPTY_THEMES = "_No themes detected this week._"
_EMPTY_ACTIVITY = "_No documents interacted with this week._"
_EMPTY_LOOPS = "_No open loops._"
_EMPTY_INGESTED = "_No new captures this week._"
_EMPTY_PEOPLE = "_No key people identified._"


def _doc_link(doc_id: str, title: str, vault_paths: dict[str, str]) -> str:
    """Render a doc reference as a wiki-link when it has a vault path, else plain.

    ``[[<vault-path-no-md>|<alias>]]`` for vault-mirrored docs (alias has its
    ``[`` / ``]`` neutralized so Quartz's wiki-link regex matches); the bare
    title otherwise. Honors the "never guess wiki-link targets" rule — the
    target is always the doc's stored ``vault_path``, never a derived slug.
    """
    vault_path = vault_paths.get(doc_id)
    if vault_path:
        target = strip_md_extension(vault_path)
        return f"[[{target}|{safe_wikilink_alias(title)}]]"
    return title


def _render_theme(block: ThemeBlock, vault_paths: dict[str, str]) -> list[str]:
    """Render one theme block: heading, optional synthesis quote, doc bullets."""
    heading = " & ".join(block.entity_names) if block.entity_names else block.key
    lines = [f"### {heading}"]
    if block.synthesis:
        lines.append(f"> {block.synthesis}")
    for doc_id, title in block.docs:
        lines.append(f"- {_doc_link(doc_id, title, vault_paths)}")
    return lines


def _render_body(report: WeeklyReport) -> str:
    """Build the Markdown body (everything after the frontmatter fence)."""
    lines: list[str] = [
        f"# Weekly review · {report.week}",
        f"_{report.start_date.isoformat()} → {report.end_date.isoformat()}_",
        "",
        "## Themes this week",
        "",
    ]
    if report.themes:
        for i, block in enumerate(report.themes):
            if i > 0:
                lines.append("")
            lines.extend(_render_theme(block, report.vault_paths))
    else:
        lines.append(_EMPTY_THEMES)

    lines += ["", f"## Activity ({len(report.activity)} docs interacted with)", ""]
    if report.activity:
        lines.append("| Doc | Interactions |")
        lines.append("|-----|-------------|")
        for doc in report.activity:
            link = _doc_link(doc.document_id, doc.title, report.vault_paths)
            lines.append(f"| {link} | {doc.interaction_count} |")
    else:
        lines.append(_EMPTY_ACTIVITY)

    lines += ["", "## Open loops", ""]
    if report.open_loops:
        for row in report.open_loops:
            source = _doc_link(
                row.document_id, row.document_title, report.vault_paths
            )
            lines.append(f"- [ ] {row.text}")
            lines.append(f"  _from: {source}_")
    else:
        lines.append(_EMPTY_LOOPS)

    lines += ["", f"## New captures ({len(report.ingested)} docs)", ""]
    if report.ingested:
        for ing in report.ingested:
            link = _doc_link(ing.document_id, ing.title, report.vault_paths)
            kind = ing.source_kind or "manual"
            lines.append(f"- {link} ({kind})")
    else:
        lines.append(_EMPTY_INGESTED)

    lines += ["", "## Key people", ""]
    if report.key_people:
        lines.append(" · ".join(report.key_people))
    else:
        lines.append(_EMPTY_PEOPLE)

    return "\n".join(lines) + "\n"


def render_weekly_md(report: WeeklyReport) -> str:
    """Render the full vault page: YAML frontmatter + Markdown body.

    Deterministic — two calls on the same report return byte-identical strings.
    Frontmatter fields are emitted in a fixed order (``dump_frontmatter`` keeps
    insertion order) so the page round-trips through
    :func:`brain.vault.frontmatter.parse_frontmatter`.
    """
    fields = {
        "kind": "review",
        "week": report.week,
        "date": report.generated_date.isoformat(),
        "tags": ["review", "weekly"],
        "window_start": report.start_date.isoformat(),
        "window_end": report.end_date.isoformat(),
    }
    return dump_frontmatter(fields, _render_body(report))


def render_weekly_json(report: WeeklyReport) -> dict[str, Any]:
    """Render the machine-readable wire shape shared by CLI ``--json`` + MCP.

    ``vault_path`` is the relative page slug (``reviews/<week>``) — present
    whether or not the page was actually emitted, mirroring the MCP contract in
    Plan 10 §3. Theme blocks expose ``community_key`` (the graph community key,
    or the tag name on the fallback path) and ``doc_titles`` (not full doc refs).
    """
    return {
        "week": report.week,
        "start_date": report.start_date.isoformat(),
        "end_date": report.end_date.isoformat(),
        "vault_path": f"reviews/{report.week}",
        "graph_used": report.graph_used,
        "sections": {
            "themes": [
                {
                    "community_key": block.key,
                    "entity_names": block.entity_names,
                    "doc_titles": [title for _id, title in block.docs],
                    "synthesis": block.synthesis,
                }
                for block in report.themes
            ],
            "activity": [
                {
                    "document_id": doc.document_id,
                    "title": doc.title,
                    "interaction_count": doc.interaction_count,
                }
                for doc in report.activity
            ],
            "open_loops": [
                {"document_title": row.document_title, "text": row.text}
                for row in report.open_loops
            ],
            "ingested": [
                {
                    "document_id": ing.document_id,
                    "title": ing.title,
                    "source_kind": ing.source_kind,
                }
                for ing in report.ingested
            ],
            "key_people": report.key_people,
        },
    }


def render_weekly_rich(report: WeeklyReport) -> str:
    """Render a compact terminal summary (plain text, no Rich markup).

    Used by ``brain review weekly --no-emit``. Leads with the report title so
    callers / tests can assert on "Weekly review".
    """
    lines = [
        f"Weekly review · {report.week}",
        f"  {report.start_date.isoformat()} → {report.end_date.isoformat()}",
        f"  themes: {len(report.themes)}  "
        f"activity: {len(report.activity)}  "
        f"open loops: {len(report.open_loops)}  "
        f"captures: {len(report.ingested)}",
    ]
    for block in report.themes:
        heading = " & ".join(block.entity_names) if block.entity_names else block.key
        lines.append(f"  • {heading}")
        if block.synthesis:
            lines.append(f"    {block.synthesis}")
    if report.key_people:
        lines.append(f"  key people: {' · '.join(report.key_people)}")
    return "\n".join(lines)
