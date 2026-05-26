"""Generate precomputed semantic-related JSON files for the Quartz wiki.

Phase F of ``docs/plans/2026-05-06-related-docs-rebuild.md``. For each
browseable, non-draft document with at least one embedded chunk, write
``<vault>/static/related/<slug>.json`` containing the top-K most-related
documents under the **same hybrid signal as runtime search**: weighted
multi-field FTS (title weight A, tags B, content C) per-document-capped to
``PER_DOC_CHUNK_CAP`` chunks, blended via Reciprocal Rank Fusion with a
vector leg gated by ``cfg.vector_sim_floor``. Quartz's stock Static emitter
copies the vault ``static/`` tree into the build output, making the files
fetchable as ``/static/related/<slug>.json``.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg

from ..config import Config
from ..db import connect
from ..rank_fusion import rrf_contribution
from ..search import CANDIDATE_LIMIT, PER_DOC_CHUNK_CAP, RRF_K
from ..vault._atomic import atomic_write_text

DEFAULT_RELATED_LIMIT = 10
SNIPPET_LENGTH = 240

# Minimum RRF score a candidate document must reach to appear in the
# related-docs list. RRF scores are bounded by 1/(RRF_K+1) ≈ 0.016 for
# a rank-1 match in a single leg. A threshold of 0.020 requires the
# candidate to appear in BOTH legs (FTS + vector) or rank very high in
# one leg alone, filtering docs that are only weakly related via a
# single commodity token.
_MIN_RRF_SCORE = 0.020

# Token regex mirrors :data:`brain.search._TOKEN_RE`. Used to count
# *meaningful* (non-stop-word) tokens in a source doc's title to decide
# whether the title alone is a strong enough self-query or whether we need
# to augment it with body-derived keywords. Postgres's English text-search
# config still removes stop-words from the final tsquery — we only use this
# regex + ``_STOP_TOKENS`` to gate the title-only-vs-fallback decision.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# Inline stop-token set for the title-vs-fallback gate. Intentionally tiny:
# Postgres handles real stop-word removal at tsquery time. The plan
# (docs/plans/2026-05-06-related-docs-rebuild.md, "Title fallback") only
# needs us to recognise titles like "Notes" or "On the bus" as too thin to
# carry the FTS leg by themselves.
_STOP_TOKENS = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with", "from"}
)

# How many body-derived lexemes the fallback path appends to the title query.
# Five is the value called out in the plan; lexemes are ranked by document
# frequency over this doc's chunks.
_BODY_FALLBACK_LEXEME_LIMIT = 5

# Minimum number of meaningful (non-stop) title tokens required to skip the
# body-fallback augmentation. Plan: "<3 alphabetic tokens after stop-words".
_MIN_TITLE_TOKENS_FOR_TITLE_ONLY = 3

# Fraction of documents a title lexeme may appear in before it is
# considered a corpus-level commodity and dropped from the OR clause
# of the self-tsquery. Only the OR leg is filtered; the AND form
# (plainto_tsquery) is kept as-is.
#
# Empirically tuned on the live ~1,076-doc corpus (Phase F.D). At 0.025
# (ndoc > ~27) the cut catches commodity tokens such as ``pat`` (ndoc 94),
# ``sarki`` (53) and ``meet`` (31) — which lacked any IDF correction
# under ``ts_rank`` and were outranking distinctive lexemes like
# ``topic-ih`` (4) and ``person-b`` (1). The plan's original 0.30 was too
# loose for this small personal corpus (top lexeme tops out near 13%);
# tuning lower restores the spec's two failing acceptance criteria
# (person-x + COMPANY_REDACTED spot-checks) without filtering anything we want to
# keep — see Phase F.D notes in
# ``docs/plans/2026-05-06-related-docs-rebuild.md``.
_CORPUS_FREQ_THRESHOLD = 0.025

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelatedSummary:
    """Outcome counts for a related-docs refresh."""

    written: int = 0
    skipped: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RelatedEntry:
    """One related-doc JSON row."""

    slug: str
    title: str
    score: float
    source: str
    snippet: str

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "score": self.score,
            "source": self.source,
            "snippet": self.snippet,
        }


def refresh_related(
    cfg: Config, *, k: int = DEFAULT_RELATED_LIMIT
) -> RelatedSummary:
    """Refresh related-doc JSON using ``cfg``'s DB and vault path.

    Plumbs ``cfg.vector_sim_floor`` through to the hybrid neighbor query so
    the precompute and runtime ``brain search`` share the same cosine floor
    (single source of truth — see plan ``docs/plans/2026-05-06-related-docs-rebuild.md``,
    "Cosine-floor reuse").

    Failures are logged and returned in ``summary.errors`` rather than raised:
    related docs are a wiki enhancement and must not block the build.
    """
    try:
        with connect(cfg.database_url) as conn:
            return regenerate_related_json(
                conn,
                vault_path=cfg.vault_path,
                k=k,
                vector_sim_floor=cfg.vector_sim_floor,
            )
    except (OSError, psycopg.Error) as exc:
        _logger.warning("wiki related docs: refresh failed: %s", exc)
        return RelatedSummary(errors=[str(exc)])


def regenerate_related_json(
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path,
    k: int = DEFAULT_RELATED_LIMIT,
    vector_sim_floor: float,
) -> RelatedSummary:
    """Write ``static/related/<slug>.json`` files for eligible documents.

    ``vector_sim_floor`` is required (no default — callers must pass an
    explicit value or wire ``cfg.vector_sim_floor`` through). Mirrors the
    runtime ``brain search`` cosine floor so the precompute can't silently
    diverge from the user-facing ranking.
    """
    if k < 1:
        raise ValueError("k must be >= 1")

    grouped: dict[str, list[RelatedEntry]] = defaultdict(list)
    source_slugs: set[str] = set()
    for row in _iter_hybrid_neighbors(conn, k=k, vector_sim_floor=vector_sim_floor):
        source_vault_path = row[0]
        if not isinstance(source_vault_path, str):
            continue
        source_slug = _slug_from_vault_path(source_vault_path)
        if source_slug is None:
            continue
        source_slugs.add(source_slug)

        related_vault_path = row[1]
        if related_vault_path is None:
            continue
        if not isinstance(related_vault_path, str):
            continue
        related_slug = _slug_from_vault_path(related_vault_path)
        if related_slug is None:
            continue

        title = str(row[2])
        source = str(row[3] or "vault")
        score = round(float(row[4]), 6)
        snippet = str(row[5] or "")
        grouped[source_slug].append(
            RelatedEntry(
                slug=related_slug,
                title=title,
                score=score,
                source=source,
                snippet=snippet,
            )
        )

    related_root = vault_path / "static" / "related"
    written = skipped = 0
    expected_paths: set[Path] = set()

    for slug in sorted(source_slugs):
        target = _target_path_for_slug(related_root, slug)
        if target is None:
            continue
        expected_paths.add(target)
        payload = [entry.to_json() for entry in grouped.get(slug, [])[:k]]
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if target.is_file():
            try:
                if target.read_text(encoding="utf-8") == rendered:
                    skipped += 1
                    continue
            except OSError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, rendered)
        written += 1

    pruned = _prune_stale_related_files(related_root, expected=expected_paths)
    return RelatedSummary(written=written, skipped=skipped, pruned=pruned)


def _iter_hybrid_neighbors(
    conn: psycopg.Connection[Any],
    *,
    k: int,
    vector_sim_floor: float,
) -> list[tuple[Any, ...]]:
    """Yield (source_vault_path, related_vault_path, related_title,
    related_source, related_score, related_snippet) rows under the new
    hybrid Related-docs signal.

    Mirrors :func:`brain.search.hybrid_search` structurally — the source
    document acts as the query. Per the plan's "Algorithm in pseudocode"
    section (``docs/plans/2026-05-06-related-docs-rebuild.md``):

    1. Compute ``tsquery_str`` via :func:`_build_self_tsquery` (title with
       optional body-keyword fallback for short/generic titles).
    2. Compute ``src_embedding = avg(c.embedding)`` over the source doc's
       embedded chunks.
    3. **FTS leg** (when ``tsquery_str`` is non-empty): per-doc-cap CTE
       (``PER_DOC_CHUNK_CAP=3`` chunks per candidate doc, ranked by
       ``ts_rank``) filtered to non-draft docs with non-NULL ``vault_path``
       and excluding self; global LIMIT ``CANDIDATE_LIMIT``.
    4. **Vector leg** (when ``src_embedding`` is non-NULL): cosine distance
       gated by ``vector_sim_floor``; ORDER BY cosine distance LIMIT
       ``CANDIDATE_LIMIT``; same self/draft/vault_path exclusions.
    5. **RRF blend.** ``1 / (RRF_K + rank)`` per leg, summed per chunk.
       Group by ``document_id`` taking the max chunk score; that chunk's
       content becomes the snippet.
    6. **Top-K.** Highest ``k`` documents by RRF score.

    Implementation choice: a Python loop over eligible source docs is the
    plan's pseudocode shape. Each iteration is independent; per-doc cost is
    comparable to a single ``brain search`` (~200ms) so a ~1k-doc corpus
    finishes well under the plan's 60s ideal / 3min ceiling.
    """
    eligible = _eligible_source_docs(conn)
    corpus_common = _corpus_common_lexemes(conn)
    rows: list[tuple[Any, ...]] = []
    for source in eligible:
        neighbors = _neighbors_for_source(
            conn,
            source=source,
            k=k,
            vector_sim_floor=vector_sim_floor,
            corpus_common=corpus_common,
        )
        for neighbor in neighbors:
            rows.append(
                (
                    source.vault_path,
                    neighbor.vault_path,
                    neighbor.title,
                    neighbor.source,
                    neighbor.score,
                    neighbor.snippet,
                )
            )
    return rows


@dataclass(frozen=True)
class _SourceDoc:
    """One eligible source document for the related-docs precompute."""

    id: str
    title: str
    vault_path: str


@dataclass(frozen=True)
class _Neighbor:
    """One ranked neighbor of a source document."""

    document_id: str
    vault_path: str
    title: str
    source: str
    score: float
    snippet: str


def _eligible_source_docs(conn: psycopg.Connection[Any]) -> list[_SourceDoc]:
    """Return the set of docs the precompute generates JSON for.

    Eligibility mirrors the pre-rewrite query: non-draft, non-NULL
    ``vault_path``, at least one chunk with a non-NULL embedding.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT d.id::text, d.title, d.vault_path
        FROM documents d
        JOIN chunks c ON c.document_id = d.id
        WHERE d.draft = FALSE
          AND d.vault_path IS NOT NULL
          AND c.embedding IS NOT NULL
        ORDER BY d.vault_path
        """
    ).fetchall()
    return [
        _SourceDoc(id=str(row[0]), title=str(row[1] or ""), vault_path=str(row[2]))
        for row in rows
    ]


def _avg_embedding(conn: psycopg.Connection[Any], doc_id: str) -> Any:
    """Return ``avg(c.embedding)`` for ``doc_id`` or ``None`` if all NULL."""
    row = conn.execute(
        "SELECT avg(c.embedding) "
        "FROM chunks c WHERE c.document_id = %s::uuid AND c.embedding IS NOT NULL",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _fts_candidates(
    conn: psycopg.Connection[Any], *, tsquery: str, exclude_doc_id: str
) -> list[tuple[str, str, str]]:
    """Return up to ``CANDIDATE_LIMIT`` (chunk_id, document_id, content) rows
    from the per-doc-capped FTS leg. Empty list when ``tsquery`` is empty.

    Mirrors the per-doc-cap CTE in :func:`brain.search.hybrid_search` but
    with the eligibility filters from the related-docs pipeline (non-draft,
    non-NULL ``vault_path``) and an explicit self-exclusion.
    """
    if not tsquery:
        return []
    # ts_rank weights array (Postgres float4[] order is ``{D, C, B, A}``).
    # Override the runtime defaults ``{0.1, 0.2, 0.4, 1.0}`` to de-emphasize
    # title (A) and promote body content (C). Migration 009's chunks.tsv
    # weighting is A=title_text, B=tags_text, C=content+search_extras,
    # so A=0.1 keeps short commodity-token title matches like "min" or
    # "refer" from outranking distinctive body matches like "person-b" or
    # "topic-ih"; C=0.8 ensures title-form distinctive tokens reach the
    # candidate set even when they only appear in body content.
    # Phase F.D — see ``docs/plans/2026-05-06-related-docs-rebuild.md``.
    sql = f"""
        WITH ranked AS (
            SELECT c.id::text AS id,
                   c.document_id::text AS document_id,
                   c.content AS content,
                   ts_rank(
                       '{{0.05, 0.8, 0.3, 0.1}}'::float4[],
                       c.tsv,
                       to_tsquery('english', %s)
                   ) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.document_id
                       ORDER BY ts_rank(
                           '{{0.05, 0.8, 0.3, 0.1}}'::float4[],
                           c.tsv,
                           to_tsquery('english', %s)
                       ) DESC
                   ) AS rn
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ to_tsquery('english', %s)
              AND d.draft = FALSE
              AND d.vault_path IS NOT NULL
              AND d.id <> %s::uuid
        )
        SELECT id, document_id, content
        FROM ranked
        WHERE rn <= {PER_DOC_CHUNK_CAP}
        ORDER BY score DESC
        LIMIT {CANDIDATE_LIMIT}
    """
    rows = conn.execute(sql, (tsquery, tsquery, tsquery, exclude_doc_id)).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]


def _vector_candidates(
    conn: psycopg.Connection[Any],
    *,
    src_embedding: Any,
    exclude_doc_id: str,
    vector_sim_floor: float,
) -> list[tuple[str, str, str]]:
    """Return up to ``CANDIDATE_LIMIT`` (chunk_id, document_id, content) rows
    from the cosine-floor-gated vector leg. Empty list when
    ``src_embedding`` is ``None``.
    """
    if src_embedding is None:
        return []
    sql = f"""
        SELECT c.id::text, c.document_id::text, c.content
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.draft = FALSE
          AND d.vault_path IS NOT NULL
          AND d.id <> %s::uuid
          AND c.embedding IS NOT NULL
          AND 1 - (c.embedding <=> %s::vector) >= %s
        ORDER BY c.embedding <=> %s::vector
        LIMIT {CANDIDATE_LIMIT}
    """
    rows = conn.execute(
        sql,
        (exclude_doc_id, src_embedding, vector_sim_floor, src_embedding),
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]


def _neighbors_for_source(
    conn: psycopg.Connection[Any],
    *,
    source: _SourceDoc,
    k: int,
    vector_sim_floor: float,
    corpus_common: frozenset[str],
) -> list[_Neighbor]:
    """RRF-blend the FTS + vector legs for one source doc and return top-K.

    Per-chunk RRF score: ``1 / (RRF_K + rank)`` from each leg the chunk
    appears in. Per-doc score: max chunk RRF for that document. Snippet:
    the highest-RRF chunk's content for the winning doc, whitespace-
    collapsed and truncated to ``SNIPPET_LENGTH``.
    """
    tsquery = _build_self_tsquery(
        conn, source.id, title=source.title, corpus_common=corpus_common
    )
    src_embedding = _avg_embedding(conn, source.id)

    fts_rows = _fts_candidates(conn, tsquery=tsquery, exclude_doc_id=source.id)
    vec_rows = _vector_candidates(
        conn,
        src_embedding=src_embedding,
        exclude_doc_id=source.id,
        vector_sim_floor=vector_sim_floor,
    )

    rrf: dict[str, float] = {}
    chunk_meta: dict[str, tuple[str, str]] = {}  # chunk_id → (document_id, content)
    for rank, (cid, doc_id, content) in enumerate(fts_rows):
        rrf[cid] = rrf.get(cid, 0.0) + rrf_contribution(rank, k=RRF_K)
        chunk_meta[cid] = (doc_id, content)
    for rank, (cid, doc_id, content) in enumerate(vec_rows):
        rrf[cid] = rrf.get(cid, 0.0) + rrf_contribution(rank, k=RRF_K)
        chunk_meta[cid] = (doc_id, content)

    if not rrf:
        return []

    # Per-document max chunk score + remember the winning chunk's content
    # so the JSON snippet shows the actual matching context, not a generic
    # head-of-document slice (plan: snippet is rendered next to the source
    # icon in ``RelatedDocs.tsx``; the matching chunk is the right text).
    by_doc: dict[str, tuple[float, str]] = {}
    for cid, score in rrf.items():
        doc_id, content = chunk_meta[cid]
        prev = by_doc.get(doc_id)
        if prev is None or score > prev[0]:
            by_doc[doc_id] = (score, content)

    # Drop candidates below the minimum score — prevents generic-title
    # docs (e.g. Krisp calls with "activ | call" in OR clause) from
    # filling the list with noise-level matches.
    by_doc = {doc_id: val for doc_id, val in by_doc.items() if val[0] >= _MIN_RRF_SCORE}

    if not by_doc:
        return []

    doc_ids = list(by_doc.keys())
    meta_rows = conn.execute(
        """
        SELECT d.id::text, d.title, d.vault_path, COALESCE(s.kind, 'vault')
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.id = ANY(%s::uuid[])
        """,
        (doc_ids,),
    ).fetchall()
    meta = {
        str(row[0]): (str(row[1] or ""), str(row[2] or ""), str(row[3] or "vault"))
        for row in meta_rows
    }

    neighbors: list[_Neighbor] = []
    for doc_id, (score, raw_snippet) in by_doc.items():
        if doc_id not in meta:
            continue
        title, vault_path, source_kind = meta[doc_id]
        if not vault_path:
            continue
        snippet = _collapse_whitespace(raw_snippet)[:SNIPPET_LENGTH]
        neighbors.append(
            _Neighbor(
                document_id=doc_id,
                vault_path=vault_path,
                title=title,
                source=source_kind,
                score=score,
                snippet=snippet,
            )
        )

    neighbors.sort(key=lambda n: (-n.score, n.title, n.document_id))
    return neighbors[:k]


_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    """Mirror the snippet-formatter regex used by the old centroid query.

    The pre-rewrite ``_iter_related_rows`` collapsed whitespace inside SQL
    via ``regexp_replace(d.content, '\\s+', ' ', 'g')``. The new pipeline
    builds the snippet from the matching chunk's content in Python; we
    apply the same whitespace rule here so the rendered JSON keeps the
    same shape and ``RelatedDocs.tsx`` doesn't need to learn anything
    about line breaks.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _corpus_common_lexemes(conn: psycopg.Connection[Any]) -> frozenset[str]:
    """Return title lexemes appearing in > _CORPUS_FREQ_THRESHOLD of docs.

    Corpus common-lexemes are dropped from the OR leg of _build_self_tsquery
    so commodity tokens ("meet", "note", "reference") don't outrank rare
    distinctive ones ("person-b", "topic-ih"). Computed once per regen run and
    passed down so the corpus scan runs exactly once.
    """
    total_row = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE draft = FALSE"
        " AND title IS NOT NULL AND title <> ''"
    ).fetchone()
    total = int(total_row[0]) if total_row else 0
    if total == 0:
        return frozenset()
    threshold_ndoc = total * _CORPUS_FREQ_THRESHOLD
    rows = conn.execute(
        """
        SELECT word FROM ts_stat(
            'SELECT to_tsvector(''english'', title)
             FROM documents
             WHERE draft = FALSE AND title IS NOT NULL AND title <> '''''
        ) WHERE ndoc > %s
        """,
        (threshold_ndoc,),
    ).fetchall()
    return frozenset(str(r[0]) for r in rows if r[0] is not None)


def _build_self_tsquery(
    conn: psycopg.Connection[Any],
    doc_id: str,
    *,
    title: str,
    corpus_common: frozenset[str],
) -> str:
    """Build a ``to_tsquery``-compatible string for the source doc.

    The new hybrid Related-docs signal treats each source document as its
    own self-query (see ``docs/plans/2026-05-06-related-docs-rebuild.md``,
    "The replacement signal"). The FTS leg's "query" is the source doc's
    title — but a title like "Notes" or "Meeting" can't carry the leg on
    its own, so we fall back to augmenting the title with the doc's
    top-frequency body lexemes when fewer than three meaningful title
    tokens remain.

    Title-only path (≥3 meaningful tokens) returns ``(<AND-form>) | <OR-of-tokens>``.
    The AND clause from ``plainto_tsquery`` boosts ``ts_rank`` when a
    candidate doc contains every title lexeme (still the strongest signal),
    while the OR clause lets long, distinctive titles such as
    ``COMPANY_REDACTED — SVP of Engineering — Internal Job Spec`` retrieve
    partial-overlap neighbors instead of returning an empty FTS leg.
    This mirrors the OR shape ``brain.search._build_tsquery`` uses to
    catch compact-token variants. Phase F.C tuning — see
    ``docs/plans/2026-05-06-related-docs-rebuild.md``, "Acceptance criteria".

    Returns ``""`` only when the doc has neither a usable title nor any
    body lexemes. The caller (Phase F.B's hybrid CTE) treats empty as
    "skip the FTS leg, use vector-only".

    Body-keyword extraction (option A from the plan): we use ``ts_stat``
    to rank lexemes from this doc's chunks by document frequency. This
    works regardless of whether the doc has tags — option B (rely on
    ``chunks.tags_text``) was rejected because untagged docs would get
    no fallback signal at all.
    """
    tokens = _TOKEN_RE.findall(title)
    meaningful_tokens = [tok for tok in tokens if tok.lower() not in _STOP_TOKENS]
    meaningful_count = len(meaningful_tokens)

    title_tsq = _plainto_tsquery_text(conn, title)

    if meaningful_count >= _MIN_TITLE_TOKENS_FOR_TITLE_ONLY:
        # OR each meaningful token alongside the AND-form, dropping
        # corpus-level commodity lexemes ("meet", "note", "reference")
        # so distinctive ones ("person-b", "topic-ih") aren't outranked by
        # commodity tokens that appear in many docs. Plan: F.D fix.
        # ``_to_tsquery_text`` rejects tokens that don't normalise to a
        # safe ``[a-z][a-z0-9]*`` lexeme (digits-first, multi-word
        # fragments left over from punctuation-heavy titles), so the
        # filter below silently drops those — same defensive shape the
        # body-fallback path uses.
        token_tsqs: list[str] = []
        for tok in meaningful_tokens:
            stemmed = _to_tsquery_text(conn, tok.lower())
            if not stemmed:
                continue
            # ``_to_tsquery_text`` returns the lexeme wrapped in single
            # quotes (Postgres tsquery::text format, e.g. ``'meet'``);
            # ``corpus_common`` from ``ts_stat`` is bare words. Strip the
            # quotes for the membership check.
            if stemmed.strip("'") in corpus_common:
                continue
            token_tsqs.append(stemmed)
        token_or = " | ".join(token_tsqs)
        if title_tsq and token_or:
            return f"({title_tsq}) | {token_or}"
        return title_tsq or token_or

    body_lexemes = _top_body_lexemes(conn, doc_id, limit=_BODY_FALLBACK_LEXEME_LIMIT)

    parts: list[str] = []
    if title_tsq:
        parts.append(f"({title_tsq})")
    for lex in body_lexemes:
        lex_tsq = _to_tsquery_text(conn, lex)
        if lex_tsq:
            parts.append(f"({lex_tsq})")

    if not parts:
        return ""
    return " | ".join(parts)


def _plainto_tsquery_text(conn: psycopg.Connection[Any], text: str) -> str:
    """Return ``plainto_tsquery('english', text)::text`` or ``""`` on empty."""
    row = conn.execute(
        "SELECT plainto_tsquery('english', %s)::text", (text,)
    ).fetchone()
    if row is None:
        return ""
    return str(row[0]) if row[0] is not None else ""


def _to_tsquery_text(conn: psycopg.Connection[Any], lexeme: str) -> str:
    """Wrap a single ts_stat-derived lexeme in a tsquery, defensively.

    ``ts_stat`` returns already-stemmed lexemes (typically ``[a-z0-9]+``).
    We re-feed them through ``to_tsquery`` so the result is always a
    well-formed tsquery fragment. Returns ``""`` for anything that
    doesn't look like a safe lexeme — e.g. a multi-word phrase or a
    leading digit — so callers can safely ``" | ".join`` the parts.
    """
    if not re.fullmatch(r"[a-z][a-z0-9]*", lexeme):
        return ""
    row = conn.execute("SELECT to_tsquery('english', %s)::text", (lexeme,)).fetchone()
    if row is None:
        return ""
    return str(row[0]) if row[0] is not None else ""


def _top_body_lexemes(
    conn: psycopg.Connection[Any], doc_id: str, *, limit: int
) -> list[str]:
    """Return the source doc's top ``limit`` lexemes by document frequency.

    Uses ``ts_stat`` over a SQL sub-query that selects this doc's
    ``chunks.tsv`` rows. ``ts_stat`` requires a SQL string, so the
    ``document_id`` is interpolated as a UUID literal — guarded by
    :func:`uuid.UUID` to defeat injection. Returns ``[]`` when the id
    isn't a valid UUID or the doc has no chunks (or all chunks have
    empty tsv).
    """
    try:
        validated = uuid.UUID(str(doc_id))
    except (ValueError, AttributeError, TypeError):
        return []
    inner_sql = f"SELECT tsv FROM chunks WHERE document_id = '{validated}'::uuid"
    rows = conn.execute(
        "SELECT word FROM ts_stat(%s) ORDER BY ndoc DESC, nentry DESC, word LIMIT %s",
        (inner_sql, limit),
    ).fetchall()
    return [str(r[0]) for r in rows if r[0] is not None]


def _slug_from_vault_path(vault_path: str) -> str | None:
    """Convert ``foo/bar.md`` to safe fetch slug ``foo/bar``."""
    normalized = vault_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    if path.suffix != ".md":
        return None
    slug_path = path.with_suffix("")
    slug_parts = [_quartz_slugify_segment(part) for part in slug_path.parts]
    if slug_parts[-1].endswith("_index"):
        slug_parts[-1] = slug_parts[-1][: -len("_index")] + "index"
    return PurePosixPath(*slug_parts).as_posix()


def _quartz_slugify_segment(segment: str) -> str:
    """Mirror Quartz's slugifyFilePath segment transform for fetch paths."""
    return (
        re.sub(r"\s", "-", segment)
        .replace("&", "-and-")
        .replace("%", "-percent")
        .replace("?", "")
        .replace("#", "")
    )


def _target_path_for_slug(root: Path, slug: str) -> Path | None:
    """Return the JSON target path for ``slug`` if it is safe."""
    path = PurePosixPath(slug)
    if path.is_absolute():
        return None
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    target = root.joinpath(*path.parts)
    return target.with_name(f"{target.name}.json")


def _prune_stale_related_files(root: Path, *, expected: set[Path]) -> int:
    """Remove stale JSON files from prior related-doc generations."""
    if not root.is_dir():
        return 0
    pruned = 0
    for path in root.rglob("*.json"):
        if path in expected:
            continue
        try:
            path.unlink()
        except OSError as exc:
            _logger.warning("wiki related docs: failed to prune %s: %s", path, exc)
            continue
        pruned += 1
    return pruned
