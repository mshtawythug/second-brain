"""Proactive daily-digest assembler (``brain brief``).

Single responsibility: assemble :class:`BriefData` from the DB and optionally
synthesize next-step suggestions. No CLI, no MCP, no terminal formatting — the
callers (CLI + MCP) format the result. Reads recent captures (via
:mod:`brain.activity`), open action items (via :mod:`brain.todo`), and pinned
docs from the interactions log.

The LLM leg goes through :func:`brain.chat.chat_json` — NOT
:mod:`brain.enrichment` — so this module never imports the enricher (which would
create an ``enrichment → brief → enrichment`` import cycle). Only titles + todo
texts are forwarded to the model; document bodies never leave the DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

from . import chat
from .activity import recent_captures
from .config import Config
from .errors import OllamaUnavailable
from .queries import DocumentRow
from .todo import TodoRow, iter_action_item_docs
from .vault.frontmatter import dump_frontmatter

_logger = logging.getLogger(__name__)

# Cap on suggestions requested from the model.
_MAX_SUGGESTIONS = 5
# Cap on prompt context lines (titles / todo texts) to keep the prompt bounded.
_MAX_PROMPT_ITEMS = 20

_SUGGEST_SYSTEM = (
    "You are a focused assistant proposing the user's next concrete steps for "
    "the day. You are given recent capture titles and open action items (titles "
    "and text only — no document bodies). Propose up to 5 short, specific, "
    "actionable next steps grounded in that context. Return ONLY valid JSON:\n"
    '{"suggestions": ["step 1", "step 2"]}\n'
    "Each suggestion is one imperative sentence. Never invent facts not implied "
    "by the inputs; return fewer suggestions (or an empty list) rather than "
    "padding."
)


@dataclass(frozen=True)
class PinnedDoc:
    """One pinned document (most-recent ``action='pinned'`` interaction)."""

    document_id: str
    title: str
    pinned_at: datetime


@dataclass(frozen=True)
class BriefData:
    """Assembled daily-brief payload. Pure value object; callers format it."""

    date: date
    captures: list[DocumentRow]
    open_todos: list[TodoRow]
    pinned: list[PinnedDoc]
    suggestions: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire shape shared by ``brain brief --json`` + MCP."""
        return {
            "date": self.date.isoformat(),
            "captures": [
                {
                    "id": doc.id,
                    "title": doc.title,
                    "source": doc.source_kind,
                    "ingested_at": (
                        doc.ingested_at.isoformat() if doc.ingested_at else None
                    ),
                }
                for doc in self.captures
            ],
            "open_todos": [
                {
                    "document_id": row.document_id,
                    "text": row.text,
                    "document_title": row.document_title,
                    "ingested_at": (
                        row.ingested_at.isoformat() if row.ingested_at else None
                    ),
                }
                for row in self.open_todos
            ],
            "pinned": [
                {
                    "id": pin.document_id,
                    "title": pin.title,
                    "pinned_at": pin.pinned_at.isoformat(),
                }
                for pin in self.pinned
            ],
            "suggestions": self.suggestions,
        }


def assemble_brief(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    since_hours: int,
    todo_since_days: int,
    on_date: date,
) -> BriefData:
    """Assemble the brief for ``on_date`` (no LLM — ``suggestions`` is empty).

    ``since_hours`` bounds the recent-captures window; ``todo_since_days`` bounds
    the open-action-item window; ``on_date`` is the header date (the caller
    passes today so this stays deterministic under test). Suggestions are filled
    separately by :func:`suggest_next_steps`.
    """
    captures = recent_captures(
        conn, since_hours=since_hours, limit=cfg.brief_capture_limit
    )
    open_todos = list(
        iter_action_item_docs(
            conn, since_days=todo_since_days, include_closed=False
        )
    )
    pinned = _pinned_docs(conn, limit=cfg.brief_pin_limit)
    return BriefData(
        date=on_date,
        captures=captures,
        open_todos=open_todos,
        pinned=pinned,
        suggestions=[],
    )


def _pinned_docs(conn: psycopg.Connection[Any], *, limit: int) -> list[PinnedDoc]:
    """Return the most-recently pinned docs (one row per doc, newest first).

    A doc may be pinned more than once; ``MAX(at)`` collapses to the latest pin
    timestamp per doc. ``DISTINCT ON`` is invalid alongside ``GROUP BY``, so the
    grouped subquery feeds the title join.
    """
    rows = conn.execute(
        """
        SELECT d.id::text, d.title, p.pinned_at
        FROM (
            SELECT document_id, MAX(at) AS pinned_at
            FROM   interactions
            WHERE  action = 'pinned' AND document_id IS NOT NULL
            GROUP  BY document_id
            ORDER  BY MAX(at) DESC
            LIMIT  %s
        ) p
        JOIN documents d ON d.id = p.document_id
        ORDER BY p.pinned_at DESC, d.id
        """,
        (limit,),
    ).fetchall()
    return [
        PinnedDoc(document_id=r[0], title=r[1], pinned_at=r[2]) for r in rows
    ]


def _build_suggest_prompt(brief: BriefData) -> str:
    """Compose the suggestion prompt from titles + todo texts only (no bodies)."""
    parts = [_SUGGEST_SYSTEM, "", "RECENT CAPTURES:"]
    if brief.captures:
        for doc in brief.captures[:_MAX_PROMPT_ITEMS]:
            parts.append(f"- {doc.title}")
    else:
        parts.append("- (none)")
    parts.append("")
    parts.append("OPEN ACTION ITEMS:")
    if brief.open_todos:
        for row in brief.open_todos[:_MAX_PROMPT_ITEMS]:
            parts.append(f"- {row.text}")
    else:
        parts.append("- (none)")
    return "\n".join(parts)


def suggest_next_steps(brief: BriefData, cfg: Config) -> list[str]:
    """Return up to 5 LLM-proposed next steps, or ``[]`` if Ollama is unavailable.

    Assembles the prompt entirely from capture titles + todo texts and calls
    :func:`brain.chat.chat_json`. Best-effort: on :class:`OllamaUnavailable` it
    logs a warning and returns ``[]`` so the brief always renders. Non-string
    entries from the model are dropped; the list is capped at five.
    """
    prompt = _build_suggest_prompt(brief)
    try:
        body = chat.chat_json(prompt, schema={"suggestions": "list"}, cfg=cfg)
    except OllamaUnavailable as exc:
        _logger.warning(
            "suggest_next_steps: Ollama unavailable (%s); returning no suggestions",
            exc,
        )
        return []
    raw = body.get("suggestions")
    if not isinstance(raw, list):
        return []
    suggestions = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    return suggestions[:_MAX_SUGGESTIONS]


def write_brief_to_vault(vault_path: Path, on_date: date, brief: BriefData) -> Path:
    """Write the brief to ``<vault>/daily/<YYYY>/<date>-brief.md`` (read-only note).

    A separate filename from ``brain daily``'s ``<date>.md`` so the two never
    collide. The file is NOT ingested into Postgres — it is a rendered digest,
    not a corpus document, so it creates no embedding churn. Surfaces titles +
    todo texts only; never document bodies.
    """
    year_folder = f"{on_date.year:04d}"
    target = vault_path / "daily" / year_folder / f"{on_date.isoformat()}-brief.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = {
        "title": f"Brain Brief · {on_date.isoformat()}",
        "date": on_date.isoformat(),
        "kind": "brief",
        "tags": ["brief", "daily"],
    }
    target.write_text(
        dump_frontmatter(fields, _render_brief_body(brief)), encoding="utf-8"
    )
    return target


def _render_brief_body(brief: BriefData) -> str:
    """Render the brief's Markdown body (titles + todo texts only)."""
    lines = [
        f"# Brain Brief · {brief.date.isoformat()}",
        "",
        "## Recent captures",
        "",
    ]
    if brief.captures:
        for doc in brief.captures:
            kind = doc.source_kind or "manual"
            lines.append(f"- [{kind}] {doc.title}")
    else:
        lines.append("_No recent captures._")

    lines += ["", "## Open action items", ""]
    if brief.open_todos:
        for row in brief.open_todos:
            lines.append(f"- [ ] {row.text}")
    else:
        lines.append("_No open action items._")

    lines += ["", "## Pinned / follow-up docs", ""]
    if brief.pinned:
        for pin in brief.pinned:
            lines.append(f"- {pin.title}")
    else:
        lines.append("_No pinned docs._")

    if brief.suggestions:
        lines += ["", "## Suggested next steps", ""]
        for i, suggestion in enumerate(brief.suggestions, 1):
            lines.append(f"{i}. {suggestion}")

    return "\n".join(lines) + "\n"
