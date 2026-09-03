"""Token-budgeted recall — search results shaped for an agent's context (F2).

``brain search`` answers "what matches?" for a human reading a table.
``brain recall`` answers "what should I paste into a context window?", and the
difference is that the second question has a hard size limit the first has
never respected: snippet length is bounded per result, so a five-result search
with generous snippet context can run to thousands of tokens with nothing in
the system aware of it.

Recall packs **expanded chunk windows, one per document**, into an explicit
token budget. That unit is deliberate:

- *Whole documents* would blow the budget on one item — the live corpus has
  documents over 300 chunks — and starve every other source.
- *Bare best chunks* land mid-argument and hand the agent a fragment with no
  surrounding context.
- *Expanded windows* are what ``hybrid_search(snippet_context_tokens=W)``
  already stitches together, and one per document keeps source diversity
  high, which is what a limited budget most wants.

Passages carry ``[N]`` citation markers, the same convention ``brain ask``
teaches models, so an agent that pastes a recall block and cites ``[2]`` is
speaking a vocabulary the rest of the system already understands.

**This module never calls ``embedder.embed()``.** It only ever passes the
embedder through to :func:`~brain.search.hybrid_search`, which auto-degrades
to the lexical leg when the backend produces no vectors. That is what makes
recall work under ``BRAIN_EMBEDDER=none``, and
``tests/test_recall_null_embedder.py`` enforces it with a stub whose
``embed()`` raises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from .config import Config
from .ingest import Embedder
from .search import SearchDiagnostics, SearchResult, hybrid_search
from .token_budget import pack_greedy, truncate_to_token_budget

#: Tokens held back from the caller's budget for the ``# recall:`` header, so
#: the WHOLE emitted block honours the budget rather than just the passages.
_ENVELOPE_TOKENS = 48

#: Floor on how many candidates to ask search for, so a tiny budget still gets
#: a real choice of sources rather than the single top hit.
_MIN_CANDIDATES = 5

#: Rough per-passage size used only to decide how many candidates to fetch.
#: Over-fetching is cheap (one search); under-fetching would silently cap the
#: budget below what the user asked for.
_ASSUMED_PASSAGE_TOKENS = 120


@dataclass(frozen=True)
class RecallPassage:
    """One document's expanded window, sized and cited."""

    ref: int
    document_id: str
    title: str
    date: datetime | None
    source_kind: str | None
    content_type: str
    tags: list[str]
    score: float
    text: str
    tokens: int
    truncated: bool


@dataclass(frozen=True)
class RecallResult:
    """A budgeted recall: the passages that fit, plus what it cost."""

    query: str
    budget_tokens: int
    used_tokens: int
    candidates_considered: int
    dropped: int
    truncated: bool
    fts_count: int | None
    passages: list[RecallPassage]

    def context_block(self) -> str:
        """The pasteable block: a header line, then each cited passage.

        This is the artifact — the thing an agent actually consumes — so it is
        domain logic and lives here rather than in a Rich renderer. It must be
        emitted with plain ``typer.echo``: Rich would parse ``[1]`` as a style
        tag and raise ``MissingStyle``.
        """
        header = (
            f"# recall: {self.query} "
            f"({len(self.passages)} passage(s), ~{self.used_tokens} tokens"
        )
        if self.dropped:
            header += f", {self.dropped} dropped"
        if self.truncated:
            header += ", truncated"
        header += ")"
        return "\n\n".join([header, *(_render_passage(p) for p in self.passages)])

    def to_dict(self) -> dict[str, Any]:
        """JSON projection. Dates are ISO-8601 or ``None``, never ``"unknown"``.

        The ``"unknown"`` placeholder is a *rendering* choice for the human
        block; a machine consumer gets a real null so it can tell "no date"
        from a document literally titled that.
        """
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "candidates_considered": self.candidates_considered,
            "dropped": self.dropped,
            "truncated": self.truncated,
            "fts_count": self.fts_count,
            "passages": [
                {
                    "ref": p.ref,
                    "document_id": p.document_id,
                    "title": p.title,
                    "date": p.date.isoformat() if p.date is not None else None,
                    "source_kind": p.source_kind,
                    "content_type": p.content_type,
                    "tags": p.tags,
                    "score": p.score,
                    "text": p.text,
                    "tokens": p.tokens,
                    "truncated": p.truncated,
                }
                for p in self.passages
            ],
        }


def _passage_header(
    ref: int, document_id: str, date: datetime | None, source_kind: str | None, title: str
) -> str:
    """``[N] <id8> | <YYYY-MM-DD> | <source> | <title>``."""
    stamp = date.strftime("%Y-%m-%d") if date is not None else "unknown"
    return f"[{ref}] {document_id[:8]} | {stamp} | {source_kind or 'manual'} | {title}"


def _render_passage(passage: RecallPassage) -> str:
    """A passage as it appears in the context block: header line, then text."""
    return (
        _passage_header(
            passage.ref,
            passage.document_id,
            passage.date,
            passage.source_kind,
            passage.title,
        )
        + "\n"
        + passage.text
    )


def _render_candidate(
    ref: int, result: SearchResult, date: datetime | None
) -> str:
    """Render a search hit exactly as it will appear, for accurate costing.

    Packing measures these rendered strings rather than the raw snippet, so
    the header line's tokens are inside the budget rather than a surprise on
    top of it.
    """
    return (
        _passage_header(
            ref, result.document_id, date, result.source_kind, result.title
        )
        + "\n"
        + result.snippet
    )


def _fetch_doc_dates(
    conn: psycopg.Connection[Any], document_ids: list[str]
) -> dict[str, datetime | None]:
    """Best-available date per document, in one query.

    ``coalesce(sent_at, ingested_at)`` mirrors what every other date-facing
    surface uses, so a recall passage and a search row date a document
    identically.
    """
    if not document_ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, coalesce(sent_at, ingested_at) FROM documents "
        "WHERE id = ANY(%s)",
        (document_ids,),
    ).fetchall()
    return {str(r[0]): r[1] for r in rows}


def _over_fetch(budget_tokens: int, max_candidates: int) -> int:
    """How many candidates to ask search for.

    Enough that the packer has real choices, capped so a huge budget does not
    turn one recall into a corpus scan.
    """
    wanted = max(
        _MIN_CANDIDATES,
        math.ceil(max(budget_tokens, 0) / _ASSUMED_PASSAGE_TOKENS),
    )
    return max(1, min(max_candidates, wanted))


def recall(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    budget_tokens: int,
    max_candidates: int,
    source_kind: str | None = None,
    source_missing: bool = False,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
    person_keys: list[str] | None = None,
    person_display_name: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    content_type: str | None = None,
    thread_id: str | None = None,
    draft: bool | None = None,
    without_tag: str | None = None,
    sensitivity: str | None = None,
) -> RecallResult:
    """Retrieve and pack passages for ``query`` within ``budget_tokens``.

    Takes ``person_keys``, not a ``--person`` string: resolution raises
    :class:`~brain.errors.PersonNotFound` / ``PersonAmbiguous``, and each
    surface maps those to its own framework's error type — the same division
    of labour ``hybrid_search`` documents.

    Raises nothing of its own; propagates :class:`psycopg.Error` and
    ``EmbedError`` for the caller to map. Telemetry is best-effort by
    ``record_search_query``'s contract — a logging failure must never cost the
    agent its context, which is why this function does not write it (the
    surfaces do, so they can attribute the right ``source`` and ``agent_id``).

    The budget is honoured end-to-end: :data:`_ENVELOPE_TOKENS` is reserved for
    the header before packing, so ``context_block()`` as a whole fits.

    ``sensitivity`` is forwarded straight to ``hybrid_search``. ``None`` (the
    default) means both tiers, which is right for the local CLI — it sits
    inside F6's trust boundary. Surfaces OUTSIDE that boundary, i.e. MCP, must
    pass ``"normal"``: excluding confidential documents from the match set is
    the only way to close the oracle, since returning a row at all reveals
    that its withheld body matched the query.
    """
    diagnostics = SearchDiagnostics()
    results = hybrid_search(
        conn,
        embedder=embedder,
        query=query,
        limit=_over_fetch(budget_tokens, max_candidates),
        snippet_context_tokens=cfg.recall_passage_tokens,
        snippet_max_chars=cfg.snippet_max_chars,
        vector_sim_floor=cfg.vector_sim_floor,
        recency_halflife_days=cfg.recency_halflife_days,
        diagnostics=diagnostics,
        source_kind=source_kind,
        source_missing=source_missing,
        tag=tag,
        since_days=since_days,
        fts_only=fts_only,
        person_keys=person_keys,
        person_display_name=person_display_name,
        after=after,
        before=before,
        content_type=content_type,
        thread_id=thread_id,
        draft=draft,
        without_tag=without_tag,
        sensitivity=sensitivity,
    )

    dates = _fetch_doc_dates(conn, [r.document_id for r in results])
    # Provisional refs (1..N over ALL candidates) purely so the rendered text
    # being measured matches the final shape. Survivors are renumbered below.
    rendered = [
        _render_candidate(i + 1, r, dates.get(r.document_id))
        for i, r in enumerate(results)
    ]

    packable = max(0, budget_tokens - _ENVELOPE_TOKENS)
    packed = pack_greedy(
        rendered, cost=embedder.count_tokens, budget=packable
    )

    truncated = False
    kept_indices = list(packed.indices)
    used_tokens = packed.used_tokens
    truncated_text: str | None = None

    if not kept_indices and results:
        # The budget cannot hold even the best passage whole. Returning
        # nothing would be a worse answer than returning the top passage cut
        # to fit — the agent asked for context, not for silence.
        truncated_text = truncate_to_token_budget(
            rendered[0], cost=embedder.count_tokens, budget=packable
        )
        if truncated_text:
            kept_indices = [0]
            used_tokens = embedder.count_tokens(truncated_text)
            truncated = True

    passages: list[RecallPassage] = []
    for new_ref, index in enumerate(kept_indices, start=1):
        result = results[index]
        if truncated:
            # Strip the header off the truncated render so ``text`` stays a
            # passage body; the header is re-emitted from the fields.
            _, _, body = truncated_text.partition("\n") if truncated_text else ("", "", "")
            text = body
        else:
            text = result.snippet
        passages.append(
            RecallPassage(
                ref=new_ref,
                document_id=result.document_id,
                title=result.title,
                date=dates.get(result.document_id),
                source_kind=result.source_kind,
                content_type=result.content_type,
                tags=result.tags,
                score=result.score,
                text=text,
                tokens=embedder.count_tokens(text),
                truncated=truncated,
            )
        )

    return RecallResult(
        query=query,
        budget_tokens=budget_tokens,
        used_tokens=used_tokens,
        candidates_considered=len(results),
        dropped=len(results) - len(passages),
        truncated=truncated,
        fts_count=diagnostics.fts_count,
        passages=passages,
    )
