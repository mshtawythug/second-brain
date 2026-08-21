"""Agentic multi-hop cited answer synthesis (`brain ask`, Plan 06).

Orchestrates a plan -> retrieve -> reflect -> synthesize loop over the existing
hybrid-search (and optional graph) retrieval primitives, then composes a single
cited answer. This module owns ONLY the loop + the LLM prompt strings; it has no
CLI or MCP knowledge. The three LLM steps go through an injected
:data:`ChatJson` callable (the public :func:`brain.chat.chat_json`) so the whole
loop is unit-testable with a fake chat and never needs a live Ollama.

Privacy: the prompts receive document ``title + snippet`` only (snippet capped at
:data:`_SYNTH_SNIPPET_CHARS`), never full bodies -- mirroring the enricher's
head-only truncation policy so no transcript/email body is sent to the LLM.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from .chat import coerce_bool
from .search import SearchResult, hybrid_search

if TYPE_CHECKING:
    from .config import Config
    from .graph_rag.backends.base import GraphBackend
    from .graph_rag.schema import GraphContext
    from .ingest import Embedder

_logger = logging.getLogger(__name__)

# Retrieval mode vocabulary accepted by `brain ask`. ``hybrid`` (the default) is
# vector/FTS only; the other three add a graph leg via ``graph_rag_search``.
HYBRID_MODE = "hybrid"
ASK_MODES: frozenset[str] = frozenset({"hybrid", "auto", "fuse", "local"})

# Fallback default when a caller omits ``max_iterations``; the CLI/MCP layers
# pass ``cfg.ask_max_iterations`` explicitly (this mirrors the plan signature
# default of 3 without importing Config at module load time).
_DEFAULT_MAX_ITERATIONS = 3

# Per-LLM-step completion-length budgets (tokens). Plan/reflect are tiny JSON
# objects; synthesize needs room for a few sentences of cited prose.
_PLAN_NUM_PREDICT = 128
_REFLECT_NUM_PREDICT = 128
_SYNTH_NUM_PREDICT = 512

# Sub-query count bounds returned by the plan step.
_MAX_SUB_QUERIES = 3
# Follow-up sub-query count bound returned by the reflect step.
_MAX_FOLLOW_UPS = 2

# Snippet truncation for the reflect step (titles + short snippets only).
_REFLECT_SNIPPET_CHARS = 120
# Snippet truncation for the synthesize step (privacy / context-window safety).
_SYNTH_SNIPPET_CHARS = 400

# Matches a ``[N]`` citation marker (1+ digits) in a synthesized answer.
_CITATION_RE = re.compile(r"\[(\d+)\]")

# Matches a ``[N]`` marker plus any leading whitespace, for sanitizing dangling
# (out-of-range) markers out of the final answer text — the marker AND the space
# that precedes it are removed so " foo [99] bar" collapses cleanly to "foo bar".
_MARKER_WITH_LEAD_RE = re.compile(r"\s*\[(\d+)\]")


class ChatJson(Protocol):
    """Structural type of :func:`brain.chat.chat_json` (the injected LLM seam).

    Production passes the real ``chat_json``; tests pass a closure with the same
    signature. Keeping it a ``Protocol`` (not a hard import of the function) lets
    the loop stay decoupled from the transport and trivially mockable.
    """

    def __call__(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = ...,
        num_predict: int | None = ...,
        timeout: float | None = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Citation:
    """One cited source backing an answer, mapped from a ``[N]`` marker.

    ``ref`` is the 1-indexed number that appears as ``[ref]`` in the answer text
    (NOT renumbered, so the marker always resolves to a real source).
    """

    ref: int
    document_id: str
    title: str
    source_kind: str | None
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        """JSON projection (CLI ``--json`` / MCP wire shape)."""
        return {
            "ref": self.ref,
            "document_id": self.document_id,
            "title": self.title,
            "source_kind": self.source_kind,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class AskResult:
    """The synthesized answer plus its provenance.

    ``citations`` are ordered by first appearance of their ``[ref]`` marker in
    ``answer`` and contain ONLY sources the answer actually cites.
    ``fallback_used`` is ``True`` when the agentic planning loop was skipped or
    degraded: the ``no_loop`` fast path, or a plan step that yielded no usable
    sub-queries (so the raw question was used as the single sub-query).
    """

    answer: str
    citations: list[Citation]
    iterations_used: int
    sub_queries: list[str]
    fallback_used: bool
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        """JSON projection shared by the CLI ``--json`` output and the MCP tool."""
        return {
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "iterations_used": self.iterations_used,
            "sub_queries": list(self.sub_queries),
            "fallback_used": self.fallback_used,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class _ReflectVerdict:
    """Parsed result of the reflect step."""

    sufficient: bool
    follow_up_queries: list[str]


# ---------------------------------------------------------------------------
# LLM step prompts + parsers (pure-logic; mockable via the injected ``chat``)
# ---------------------------------------------------------------------------


def _plan_prompt(question: str) -> str:
    """Build the plan-step prompt: decompose ``question`` into sub-queries."""
    return (
        "You are a retrieval planner for a personal knowledge base. Decompose "
        "the user's question into 1 to 3 focused search sub-queries that, "
        "together, would retrieve the documents needed to answer it. Each "
        "sub-query should be a short keyword phrase, not a full sentence. "
        "Respond ONLY with a JSON object of the form "
        '{"sub_queries": ["...", "..."]}.\n\n'
        f"Question: {question}"
    )


def _call_plan_step(chat: ChatJson, cfg: Config, question: str) -> list[str]:
    """Run the plan step and return 1-3 sub-queries (clamped + de-duplicated).

    Returns an empty list only if the model produced no usable string
    sub-queries; the caller then falls back to ``[question]``.
    """
    body = chat(
        _plan_prompt(question),
        schema={"sub_queries": "list"},
        cfg=cfg,
        model=cfg.ask_model,
        num_predict=_PLAN_NUM_PREDICT,
        timeout=cfg.ask_timeout_seconds,
    )
    return _clean_query_list(body.get("sub_queries"), limit=_MAX_SUB_QUERIES)


def _reflect_prompt(question: str, docs: list[SearchResult]) -> str:
    """Build the reflect-step prompt: is coverage sufficient for ``question``?"""
    lines = [
        "You are assessing whether the retrieved documents are sufficient to "
        "answer the user's question. If they are sufficient, respond with "
        '{"sufficient": true, "follow_up_queries": []}. If not, respond with '
        '{"sufficient": false, "follow_up_queries": ["...", "..."]} providing '
        "up to 2 NEW short search sub-queries that would fill the gaps. "
        "Respond ONLY with that JSON object.\n",
        f"Question: {question}\n",
        "Retrieved documents:",
    ]
    if docs:
        for i, doc in enumerate(docs, start=1):
            snippet = _truncate(doc.snippet, _REFLECT_SNIPPET_CHARS)
            lines.append(f"[{i}] {doc.title} — {snippet}")
    else:
        lines.append("(none retrieved)")
    return "\n".join(lines)


def _call_reflect_step(
    chat: ChatJson, cfg: Config, question: str, docs: list[SearchResult]
) -> _ReflectVerdict:
    """Run the reflect step and return the sufficiency verdict + follow-ups."""
    body = chat(
        _reflect_prompt(question, docs),
        schema={"sufficient": "bool", "follow_up_queries": "list"},
        cfg=cfg,
        model=cfg.ask_model,
        num_predict=_REFLECT_NUM_PREDICT,
        timeout=cfg.ask_timeout_seconds,
    )
    # Coerce defensively: a local model that returns ``"sufficient": "false"``
    # would be treated as True by a bare ``bool(...)`` (non-empty string is
    # truthy), stopping the loop early. ``coerce_bool`` maps stringified/int
    # booleans correctly and defaults unparseable verdicts to False (insufficient)
    # so the loop keeps retrieving rather than truncating on garbage.
    sufficient = coerce_bool(body.get("sufficient"))
    follow_ups = _clean_query_list(
        body.get("follow_up_queries"), limit=_MAX_FOLLOW_UPS
    )
    return _ReflectVerdict(sufficient=sufficient, follow_up_queries=follow_ups)


def _synthesize_prompt(
    question: str, docs: list[SearchResult], graph_summary: str
) -> str:
    """Build the synthesize-step prompt over numbered title+snippet docs."""
    lines = [
        "You are answering the user's question using ONLY the numbered source "
        "documents below. Write a direct, concise answer. After each claim, cite "
        "the source it came from using its bracketed number exactly as shown "
        "below — for example [1] or [2] (use the real digits, never a literal "
        "letter). If the documents do not contain enough information to answer, "
        "say so plainly and do not invent facts. Respond ONLY with a JSON object "
        'of the form {"answer": "your answer with [1]-style citations"}.\n',
        f"Question: {question}\n",
    ]
    if graph_summary:
        lines.append(f"Graph context: {graph_summary}\n")
    lines.append("Sources:")
    if docs:
        for i, doc in enumerate(docs, start=1):
            snippet = _truncate(doc.snippet, _SYNTH_SNIPPET_CHARS)
            lines.append(f"[{i}] {doc.title} — {snippet}")
    else:
        lines.append("(no documents found)")
    return "\n".join(lines)


def _call_synthesize_step(
    chat: ChatJson,
    cfg: Config,
    question: str,
    docs: list[SearchResult],
    graph_summary: str,
) -> str:
    """Run the synthesize step and return the raw answer string."""
    body = chat(
        _synthesize_prompt(question, docs, graph_summary),
        schema={"answer": "str"},
        cfg=cfg,
        model=cfg.ask_model,
        num_predict=_SYNTH_NUM_PREDICT,
        timeout=cfg.ask_timeout_seconds,
    )
    answer = body.get("answer")
    return answer if isinstance(answer, str) else ""


# ---------------------------------------------------------------------------
# Citation tracking
# ---------------------------------------------------------------------------


def _sanitize_answer(answer: str, doc_count: int) -> str:
    """Strip out-of-range ``[N]`` markers from the answer text.

    Citation integrity: a marker whose number has no backing source (``N`` >
    ``doc_count`` or ``N`` < 1) is a dangling citation. Removing it — along with
    its leading whitespace — keeps the displayed/returned answer free of
    references that resolve to nothing, while valid in-range markers are kept
    verbatim so :func:`_build_citations` still maps them.
    """

    def _replace(match: re.Match[str]) -> str:
        ref = int(match.group(1))
        if 1 <= ref <= doc_count:
            return match.group(0)  # keep the valid marker (and its lead space)
        return ""

    return _MARKER_WITH_LEAD_RE.sub(_replace, answer).strip()


def _build_citations(answer: str, docs: list[SearchResult]) -> list[Citation]:
    """Map ``[N]`` markers in ``answer`` to the numbered ``docs`` list.

    Keeps only in-range references (1..len(docs)), de-duplicated, ordered by
    first appearance in the answer text. ``ref`` preserves the original marker
    number so ``[ref]`` in the prose always resolves to a real source — and a
    hallucinated marker (out of range) is silently dropped, never emitted as a
    citation (citation integrity).
    """
    citations: list[Citation] = []
    seen: set[int] = set()
    for match in _CITATION_RE.finditer(answer):
        ref = int(match.group(1))
        if ref in seen:
            continue
        if ref < 1 or ref > len(docs):
            continue
        seen.add(ref)
        doc = docs[ref - 1]
        citations.append(
            Citation(
                ref=ref,
                document_id=doc.document_id,
                title=doc.title,
                source_kind=doc.source_kind,
                snippet=_truncate(doc.snippet, _SYNTH_SNIPPET_CHARS),
            )
        )
    return citations


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _graph_summary(ctx: GraphContext) -> str:
    """Build a one-line graph-context label list from a ``GraphContext``.

    For ``themes`` mode uses each :class:`ThemeGroup`'s ``summary`` (when the
    opt-in synthesis ran) else its top entity names; for ``global`` mode each
    :class:`CommunityGroup`'s ``summary`` else its ``community_key``; for
    ``local`` mode the reached entity names. Empty string when the graph leg
    produced nothing — keeps the synthesize prompt clean.
    """
    labels: list[str] = []
    for theme in ctx.themes:
        if theme.summary:
            labels.append(theme.summary)
        else:
            labels.extend(e.name for e in theme.entities[:3] if e.name)
    for community in ctx.communities:
        labels.append(community.summary or community.community_key)
    for entity in ctx.entities:
        if entity.name:
            labels.append(entity.name)
    # De-duplicate preserving order, cap to keep the prompt bounded.
    deduped: list[str] = []
    for label in labels:
        if label and label not in deduped:
            deduped.append(label)
    return ", ".join(deduped[:10])


def _retrieve_hybrid(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    limit: int,
    exclude_confidential: bool = False,
) -> list[SearchResult]:
    """Single hybrid-search pass for one sub-query (config-driven knobs).

    ``exclude_confidential`` maps onto ``hybrid_search``'s own F6 lens. ``ask``
    is the highest-consequence retrieval surface here: its citations carry
    ``snippet`` (raw body), and its ``answer`` is an LLM SYNTHESIS over the
    retrieved bodies — so a confidential document reaching this leg leaks
    twice, once verbatim and once paraphrased into prose that no marker-based
    check downstream could recognise as derived from it.
    """
    return hybrid_search(
        conn,
        embedder=embedder,
        query=query,
        limit=limit,
        vector_sim_floor=cfg.vector_sim_floor,
        recency_halflife_days=cfg.recency_halflife_days,
        snippet_context_tokens=cfg.snippet_context_tokens,
        sensitivity="normal" if exclude_confidential else None,
    )


def _retrieve(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    limit: int,
    mode: str,
    backend: GraphBackend | None,
    exclude_confidential: bool = False,
) -> tuple[list[SearchResult], str]:
    """Retrieve documents for one sub-query; returns (results, graph_summary).

    The hybrid leg always runs. For ``mode != hybrid`` the graph leg runs too
    (requires ``backend``) and its docs are appended after the hybrid docs; the
    returned ``graph_summary`` carries the theme/community/entity labels for the
    synthesize prompt. A graph leg that yields nothing degrades silently to the
    hybrid docs (never-raise on empty, matching the graph layer's contract).
    """
    # Validate the graph precondition BEFORE the (DB-touching) hybrid leg so a
    # missing backend fails fast as a clean caller error.
    if mode != HYBRID_MODE and backend is None:
        raise ValueError(
            f"mode={mode!r} requires a graph backend; pass backend=AgeBackend()"
        )

    results = _retrieve_hybrid(
        conn,
        cfg,
        embedder=embedder,
        query=query,
        limit=limit,
        exclude_confidential=exclude_confidential,
    )
    if mode == HYBRID_MODE:
        return results, ""

    assert backend is not None  # guarded above
    from .graph_rag import graph_rag_search

    ctx = graph_rag_search(
        conn,
        cfg,
        query,
        backend=backend,
        mode=mode,
        limit=limit,
        embedder=embedder,
        exclude_confidential=exclude_confidential,
    )
    combined = list(results)
    seen_ids = {r.document_id for r in combined}
    for doc in ctx.docs:
        if doc.document_id not in seen_ids:
            seen_ids.add(doc.document_id)
            combined.append(doc)
    return combined, _graph_summary(ctx)


def _clean_query_list(raw: Any, *, limit: int) -> list[str]:
    """Coerce a model's list field into 0..limit non-empty unique strings."""
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if stripped and stripped not in cleaned:
            cleaned.append(stripped)
        if len(cleaned) >= limit:
            break
    return cleaned


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars (collapsing trailing whitespace)."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def ask_no_loop(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    chat: ChatJson,
    question: str,
    mode: str = HYBRID_MODE,
    limit: int = 5,
    backend: GraphBackend | None = None,
    exclude_confidential: bool = False,
) -> AskResult:
    """Single retrieve + synthesize pass (the ``--no-loop`` fast path).

    Skips the plan/reflect LLM steps: retrieves once for ``question`` and
    synthesizes a cited answer. ``fallback_used`` is always ``True`` here (no
    agentic planning was performed).
    """
    _validate_mode(mode)
    session_id = uuid.uuid4().hex
    docs, graph_summary = _retrieve(
        conn,
        cfg,
        embedder=embedder,
        query=question,
        limit=limit,
        mode=mode,
        backend=backend,
        exclude_confidential=exclude_confidential,
    )
    raw_answer = _call_synthesize_step(chat, cfg, question, docs, graph_summary)
    answer = _sanitize_answer(raw_answer, len(docs))
    citations = _build_citations(answer, docs)
    return AskResult(
        answer=answer,
        citations=citations,
        iterations_used=1,
        sub_queries=[question],
        fallback_used=True,
        session_id=session_id,
    )


def ask(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    chat: ChatJson,
    question: str,
    mode: str = HYBRID_MODE,
    no_loop: bool = False,
    limit: int = 5,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    backend: GraphBackend | None = None,
    exclude_confidential: bool = False,
) -> AskResult:
    """Agentic plan -> retrieve -> reflect -> synthesize loop over the corpus.

    Plans 1-3 sub-queries, retrieves (dedup by document id across iterations),
    reflects on coverage, optionally issues follow-up sub-queries, then
    synthesizes a single cited answer. ``no_loop=True`` delegates to
    :func:`ask_no_loop`. ``max_iterations`` hard-caps the loop.

    The loop stops early when the reflect step reports coverage is sufficient,
    when an iteration surfaces no new documents, or when no follow-up sub-queries
    are produced. ``OllamaUnavailable`` / ``EnrichmentError`` from any LLM step
    propagates to the caller (no silent degradation).
    """
    _validate_mode(mode)
    if max_iterations < 1:
        raise ValueError(
            f"max_iterations must be >= 1 (got {max_iterations})"
        )
    if no_loop:
        return ask_no_loop(
            conn,
            cfg,
            embedder=embedder,
            chat=chat,
            question=question,
            mode=mode,
            limit=limit,
            backend=backend,
            exclude_confidential=exclude_confidential,
        )

    session_id = uuid.uuid4().hex

    # Step 1 — Plan. Fall back to the raw question when the planner yields none.
    sub_queries = _call_plan_step(chat, cfg, question)
    fallback_used = False
    if not sub_queries:
        sub_queries = [question]
        fallback_used = True

    all_sub_queries: list[str] = list(sub_queries)
    all_docs: dict[str, SearchResult] = {}
    graph_summaries: list[str] = []
    iterations_used = 0

    for iteration in range(1, max_iterations + 1):
        iterations_used = iteration
        # Step 2 — Retrieve (dedup across iterations).
        new_doc_found = False
        for sub_query in sub_queries:
            results, graph_summary = _retrieve(
                conn,
                cfg,
                embedder=embedder,
                query=sub_query,
                limit=limit,
                mode=mode,
                backend=backend,
                exclude_confidential=exclude_confidential,
            )
            if graph_summary:
                graph_summaries.append(graph_summary)
            for result in results:
                if result.document_id not in all_docs:
                    all_docs[result.document_id] = result
                    new_doc_found = True

        # Step 3 — Reflect (skip on the last iteration or when nothing new).
        if iteration >= max_iterations or not new_doc_found:
            break
        verdict = _call_reflect_step(
            chat, cfg, question, list(all_docs.values())
        )
        if verdict.sufficient or not verdict.follow_up_queries:
            break
        sub_queries = verdict.follow_up_queries
        for follow_up in sub_queries:
            if follow_up not in all_sub_queries:
                all_sub_queries.append(follow_up)

    # Step 4 — Synthesize over the accumulated, deduplicated documents.
    docs = list(all_docs.values())
    combined_graph_summary = _join_graph_summaries(graph_summaries)
    raw_answer = _call_synthesize_step(
        chat, cfg, question, docs, combined_graph_summary
    )
    answer = _sanitize_answer(raw_answer, len(docs))
    citations = _build_citations(answer, docs)
    return AskResult(
        answer=answer,
        citations=citations,
        iterations_used=iterations_used,
        sub_queries=all_sub_queries,
        fallback_used=fallback_used,
        session_id=session_id,
    )


def _join_graph_summaries(summaries: list[str]) -> str:
    """De-duplicate label tokens across iteration graph summaries into one line."""
    labels: list[str] = []
    for summary in summaries:
        for label in summary.split(", "):
            label = label.strip()
            if label and label not in labels:
                labels.append(label)
    return ", ".join(labels[:10])


def _validate_mode(mode: str) -> None:
    """Raise ``ValueError`` for an unrecognized ``--mode`` value."""
    if mode not in ASK_MODES:
        valid = ", ".join(sorted(ASK_MODES))
        raise ValueError(f"unknown mode {mode!r}; valid modes: {valid}")
