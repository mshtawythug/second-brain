"""Proactive daily-digest assembler (``brain brief``).

Single responsibility: assemble :class:`BriefData` from the DB and optionally
synthesize next-step suggestions. No CLI, no MCP, no terminal formatting — the
callers (CLI + MCP) format the result. Reads recent captures (via
:mod:`brain.activity`), open action items (via :mod:`brain.todo`), and pinned
docs from the interactions log, plus the saved-search ("watch") hits first seen
inside the capture window (RM-15, :mod:`brain.saved_search`), and the people, orgs and
projects new this week (RM-17, :mod:`brain.brief_entities`).

The LLM leg goes through :func:`brain.chat.chat_json` — NOT
:mod:`brain.enrichment` — so this module never imports the enricher (which would
create an ``enrichment → brief → enrichment`` import cycle). Only titles + todo
texts are forwarded to the model; document bodies never leave the DB.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg

from . import brief_entities, chat, near_dup_store
from .activity import recent_captures
from .config import Config
from .errors import EnrichmentError, describe_failure
from .queries import DocumentRow
from .saved_search import (
    WatchHit,
    WatchNote,
    hits_since,
    inline_code,
    watch_tables_present,
)
from .sensitivity import not_confidential_sql
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
    #: Saved-search hits (RM-15). The terminal payload holds every hit first seen in the
    #: capture window; the published page's payload holds the unnotified ones, capped per watch
    #: (``brief_publish.build_brief`` swaps them in).
    watch_hits: list[WatchHit] = field(default_factory=list)
    #: Per-watch "and N more" counts and skipped checks, for the same section.
    watch_notes: list[WatchNote] = field(default_factory=list)
    #: Entities first seen this week (RM-17), under this payload's confidential lens.
    new_entities: list[brief_entities.NewEntity] = field(default_factory=list)
    #: Open near-duplicate pairs (RM-19); gated, no pair with a confidential or draft end.
    possible_duplicates: int = 0

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
            "watch_hits": [
                {
                    "watch": hit.watch,
                    "id": hit.document_id,
                    "title": hit.title,
                    "source": hit.source_kind,
                    "first_seen_at": hit.first_seen_at.isoformat(),
                    "notified": hit.notified,
                }
                for hit in self.watch_hits
            ],
            "watch_notes": [
                {
                    "watch": note.watch,
                    "more": note.more,
                    "skipped": note.skipped,
                    "limit": note.limit,
                }
                for note in self.watch_notes
            ],
            "new_entities": [entity.to_dict() for entity in self.new_entities],
            "possible_duplicates": self.possible_duplicates,
        }


def assemble_brief(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    since_hours: int,
    todo_since_days: int,
    on_date: date,
    exclude_confidential: bool = False,
) -> BriefData:
    """Assemble the brief for ``on_date`` (no LLM — ``suggestions`` is empty).

    ``since_hours`` bounds the recent-captures window; ``todo_since_days`` bounds
    the open-action-item window; ``on_date`` is the header date (the caller
    passes today so this stays deterministic under test). Suggestions are filled
    separately by :func:`suggest_next_steps`.

    ``exclude_confidential`` (F6) is threaded to ALL SIX reads below, and it
    has to be all six: they are independent queries against ``documents`` — a
    time window, a body scan, an interaction join, the watch hits, the new
    entities' aggregate and the duplicate count — so closing one closes none of the others.

    The watch hits are the TERMINAL set (every hit first seen in the last
    ``since_hours``, notified or not); no check runs here, so MCP ``brain_brief``
    only reads. They are empty when migration 029 is not applied, so a launchd
    reinstall without ``brain init`` cannot break the morning brief. The brief names
    documents the caller never asked for (that is what a digest IS), and through
    ``iter_action_item_docs`` it republishes confidential BODY text, which
    :func:`suggest_next_steps` then puts in a prompt.

    DEFAULTS FALSE (include) — ``brain brief`` at a terminal is inside the trust
    boundary; :func:`brain.mcp_server.brain_brief` is the boundary and passes
    ``exclude_confidential=not include_confidential``.
    """
    captures = recent_captures(
        conn,
        since_hours=since_hours,
        limit=cfg.brief_capture_limit,
        exclude_confidential=exclude_confidential,
    )
    open_todos = list(
        iter_action_item_docs(
            conn,
            since_days=todo_since_days,
            include_closed=False,
            exclude_confidential=exclude_confidential,
        )
    )
    pinned = _pinned_docs(
        conn,
        limit=cfg.brief_pin_limit,
        exclude_confidential=exclude_confidential,
    )
    watch_hits = (
        hits_since(conn, hours=since_hours, exclude_confidential=exclude_confidential)
        if watch_tables_present(conn)
        else []
    )
    return BriefData(
        date=on_date,
        captures=captures,
        open_todos=open_todos,
        pinned=pinned,
        suggestions=[],
        watch_hits=watch_hits,
        new_entities=brief_entities.for_brief(
            conn, cfg, exclude_confidential=exclude_confidential
        ),
        possible_duplicates=near_dup_store.count_open(
            conn, exclude_confidential=exclude_confidential
        ),
    )


def _pinned_docs(
    conn: psycopg.Connection[Any],
    *,
    limit: int,
    exclude_confidential: bool = False,
) -> list[PinnedDoc]:
    """Return the most-recently pinned docs (one row per doc, newest first).

    A doc may be pinned more than once; ``MAX(at)`` collapses to the latest pin
    timestamp per doc. ``DISTINCT ON`` is invalid alongside ``GROUP BY``, so the
    grouped subquery feeds the title join.

    ``exclude_confidential`` (F6) filters on the OUTER query, where ``documents``
    is in scope. It cannot go in the inner subquery — that one selects from
    ``interactions`` alone and has no ``sensitivity`` column to test. Placing it
    outside costs one subtlety worth stating: ``LIMIT`` applies to the inner
    grouped set, so a window whose newest pins are all confidential returns fewer
    than ``limit`` rows rather than back-filling with older ones. That is the
    correct trade — back-filling would let a caller infer, from the count, that
    something was withheld, which is the membership oracle
    :func:`brain.mcp_server._confidential_lens` describes.
    """
    where = ["TRUE"]
    if exclude_confidential:
        where.append(not_confidential_sql("d"))
    rows = conn.execute(
        f"""
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
        WHERE {' AND '.join(where)}
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
    :func:`brain.chat.chat_json`. Best-effort: on any
    :class:`~brain.errors.EnrichmentError` (Ollama unavailable OR two malformed
    JSON / schema-violating responses — ``OllamaUnavailable`` is a subclass) it
    logs a warning and returns ``[]`` so the brief always renders. Non-string
    entries from the model are dropped; the list is capped at five.
    """
    prompt = _build_suggest_prompt(brief)
    try:
        body = chat.chat_json(prompt, schema={"suggestions": "list"}, cfg=cfg)
    except EnrichmentError as exc:
        # OllamaUnavailable is an EnrichmentError subclass, so this single clause
        # covers transport failure AND malformed-JSON-twice — never fail the brief.
        _logger.warning(
            "suggest_next_steps: synthesis failed (%s); returning no suggestions",
            describe_failure(exc),
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

    watch_lines = watch_section_lines(brief)
    if watch_lines:
        lines += ["", "## Watch hits", "", *[f"- {line}" for line in watch_lines]]
    if entity_lines := brief_entities.section_lines(brief.new_entities):
        lines += ["", f"## {brief_entities.SECTION_TITLE}", "", *[f"- {e}" for e in entity_lines]]
    if dup_lines := near_dup_store.brief_lines(n_dups := brief.possible_duplicates):
        lines += ["", f"## {near_dup_store.section_title(n_dups)}", "", f"- {dup_lines[0]}"]

    if brief.suggestions:
        lines += ["", "## Suggested next steps", ""]
        for i, suggestion in enumerate(brief.suggestions, 1):
            lines.append(f"{i}. {suggestion}")

    return "\n".join(lines) + "\n"


def terminal_sections(brief: BriefData) -> list[tuple[str, list[str]]]:
    """``cli._print_brief``'s optional sections as ``(heading, lines)``; empty ones are left out."""
    sections = [
        ("👀  Watch hits", watch_section_lines(brief)),
        (f"🆕  {brief_entities.SECTION_TITLE}", brief_entities.section_lines(brief.new_entities)),
        (f"🔁  {near_dup_store.section_title(n := brief.possible_duplicates)}",
         near_dup_store.brief_lines(n)),
    ]
    return [(heading, lines) for heading, lines in sections if lines]


def watch_section_lines(brief: BriefData) -> list[str]:
    """The watch section's lines, without bullets; empty when there is nothing to say.

    Shared by the terminal (``cli._print_brief``) and the page, so the two cannot word a hit
    differently. A watch with no hits and nothing skipped prints nothing: someone with no
    watches never sees the section.
    """
    lines = [f"[{hit.watch}] {hit.title}" for hit in brief.watch_hits]
    for note in brief.watch_notes:
        if note.more:
            lines.append(
                f"[{note.watch}] and {note.more} more "
                f"({inline_code(f'brain watch hits {shlex.quote(note.watch)}')})"
            )
        if note.skipped:
            lines.append(f"[{note.watch}] {note.skipped}")
        if note.limit:
            lines.append(f"[{note.watch}] {note.limit}")
    return lines
