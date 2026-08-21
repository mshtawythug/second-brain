"""Score and rank the most-related documents for a source document.

The hybrid Related-docs signal, extracted from the wiki emitter so the
scoring can be reused (and tested) without importing the wiki package.

For a source document, :func:`_iter_hybrid_neighbors` mirrors
:func:`brain.search.hybrid_search` structurally — the source document acts
as the query. Weighted multi-field FTS (title weight A, tags B, content C)
is per-document-capped to ``PER_DOC_CHUNK_CAP`` chunks and blended via
Reciprocal Rank Fusion with a vector leg gated by ``vector_sim_floor``,
so the precompute can never silently diverge from the user-facing ranking.

Pure ranking only: this module reads the database and returns ranked rows.
Writing those rows out as ``<vault>/static/related/<slug>.json`` is the
emitter's job and lives in :mod:`brain.wiki.build_related`, which imports
from here. :mod:`brain.connect` reuses the eligibility + embedding helpers
for its own auto-link scoring.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .rank_fusion import rrf_contribution
from .search import CANDIDATE_LIMIT, PER_DOC_CHUNK_CAP, RRF_K

__all__ = [
    "DEFAULT_RELATED_LIMIT",
    "SNIPPET_LENGTH",
    "RelatedDoc",
    "compute_related",
]

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
# Empirically tuned on a ~1,076-doc personal corpus (Phase F.D). At 0.025
# (ndoc > ~27) the cut catches high-frequency commodity lexemes — recurring
# person-name and meeting-boilerplate stems appearing in 30-90 documents —
# which lacked any IDF correction under ``ts_rank`` and were outranking
# far more distinctive lexemes appearing in only 1-4 documents. The plan's
# original 0.30 was too loose for a corpus this small (the top lexeme tops
# out near 13%); tuning lower restores the spec's two failing acceptance
# criteria without filtering anything worth keeping — see Phase F.D notes
# in ``docs/plans/2026-05-06-related-docs-rebuild.md``.
#
# The original comment named the actual offending lexemes from the live
# corpus. They were real personal-data derivatives and are deliberately
# not reproduced here (CLAUDE.md rule 15).
_CORPUS_FREQ_THRESHOLD = 0.025


@dataclass(frozen=True)
class RelatedDoc:
    """One related document, as returned by :func:`compute_related`.

    The public counterpart of :class:`_Neighbor` — the same six fields, named
    for a consumer rather than for the ranking internals (``id`` rather than
    ``document_id``). ``score`` is the blended RRF score: comparable *within*
    one result list, not across source documents.
    """

    id: str
    title: str
    vault_path: str
    source: str
    score: float
    snippet: str


def compute_related(
    conn: psycopg.Connection[Any],
    doc_id: str,
    *,
    limit: int = DEFAULT_RELATED_LIMIT,
    vector_sim_floor: float,
) -> list[RelatedDoc]:
    """Return the ``limit`` documents most related to ``doc_id``, best first.

    The per-document entry point into the same signal
    :func:`_iter_hybrid_neighbors` runs corpus-wide. Both delegate to
    :func:`_neighbors_for_source`, so a live call here and a precomputed
    ``static/related/<slug>.json`` row rank identically for the same doc.

    **Nothing in ``src/`` calls this yet, and that is deliberate.** The
    related-docs panel it exists for is phase-5 work (design spec §9.2, whose
    verified block records this function as new code authored ahead of its
    consumer rather than moved from the emitter); the ``brain ui`` routes carry
    no such panel today, and only ``tests/test_related_compute.py`` exercises
    it. It is a deposit, not a live caller — do not delete it as dead code, and
    do not read the paragraph above as describing a panel that ships.

    ``vector_sim_floor`` is required, with no default, for the reason
    :func:`brain.wiki.build_related.regenerate_related_json` states: the
    cosine floor is shared with runtime ``brain search`` and must not
    silently diverge from it. Callers plumb ``cfg.vector_sim_floor`` through.

    **Source eligibility is deliberately wider here.**
    :func:`_eligible_source_docs` restricts the *precompute* to non-draft docs
    with a ``vault_path`` and at least one embedded chunk, because those are
    the docs it writes files for. A reader can open a document that meets none
    of those conditions, so this function accepts any document row and lets
    the legs degrade on their own — no embedded chunks means vector-only drops
    out (:func:`_avg_embedding` returns ``None``), and an empty self-tsquery
    drops the FTS leg. *Candidate* eligibility is unchanged: drafts and
    ``vault_path``-less docs are still never returned as neighbors.

    Returns ``[]`` — rather than raising — when ``doc_id`` is not a valid
    UUID or matches no document. The UUID guard is load-bearing, not
    defensive padding: an unparseable id reaching ``%s::uuid`` raises and
    leaves the caller's transaction aborted, so every later query in the same
    request would fail too. Same idiom as :func:`_top_body_lexemes`.

    Cost: one :func:`_corpus_common_lexemes` scan per call, on top of
    :func:`_neighbors_for_source`'s per-doc queries. The corpus-wide driver
    hoists that scan out of its loop; a single-document caller cannot, so
    treat this as roughly one ``brain search``'s worth of work.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    try:
        validated = uuid.UUID(str(doc_id))
    except (ValueError, AttributeError, TypeError):
        return []

    row = conn.execute(
        "SELECT d.title, d.vault_path FROM documents d WHERE d.id = %s::uuid",
        (str(validated),),
    ).fetchone()
    if row is None:
        return []

    source = _SourceDoc(
        id=str(validated),
        title=str(row[0] or ""),
        vault_path=str(row[1] or ""),
    )
    neighbors = _neighbors_for_source(
        conn,
        source=source,
        k=limit,
        vector_sim_floor=vector_sim_floor,
        corpus_common=_corpus_common_lexemes(conn),
    )
    return [
        RelatedDoc(
            id=neighbor.document_id,
            title=neighbor.title,
            vault_path=neighbor.vault_path,
            source=neighbor.source,
            score=neighbor.score,
            snippet=neighbor.snippet,
        )
        for neighbor in neighbors
    ]


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
                       %s::tsquery
                   ) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.document_id
                       ORDER BY ts_rank(
                           '{{0.05, 0.8, 0.3, 0.1}}'::float4[],
                           c.tsv,
                           %s::tsquery
                       ) DESC
                   ) AS rn
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ %s::tsquery
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
        # ``_lexeme_to_tsquery_text``, NOT ``_to_tsquery_text``: these came from
        # ``ts_stat`` over ``chunks.tsv`` and are ALREADY stemmed. Re-stemming
        # them (provis -> provi) stops them matching the column they came from.
        # The title path above deliberately keeps ``_to_tsquery_text`` because
        # its input is RAW tokens, which must be stemmed.
        lex_tsq = _lexeme_to_tsquery_text(conn, lex)
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


def _lexeme_to_tsquery_text(conn: psycopg.Connection[Any], lexeme: str) -> str:
    """Wrap an ALREADY-STEMMED lexeme as a tsquery fragment, without re-stemming.

    Counterpart to :func:`_to_tsquery_text`, and the distinction is load-bearing.
    That function takes RAW text and stems it, which is correct for the title
    path (a raw token must become a lexeme to match the stored ``tsv``). This
    one takes a lexeme that ``ts_stat`` already produced from ``chunks.tsv``;
    passing it through ``to_tsquery`` would stem it a SECOND time and the result
    would no longer match the very column it came from — ``provis`` → ``provi``,
    while the stored lexeme stays ``provis``.

    Validation is preserved by *casting* rather than re-parsing: ``::tsquery``
    rejects malformed input exactly as ``to_tsquery`` did, but is
    dictionary-free so lexemes survive intact. The ``[a-z][a-z0-9]*`` guard is
    kept ahead of it, so the quoting below can never be fed a quote character.

    Returns ``""`` for anything unsafe or unparseable, so callers can keep
    ``" | ".join``-ing the parts.
    """
    if not re.fullmatch(r"[a-z][a-z0-9]*", lexeme):
        return ""
    try:
        row = conn.execute("SELECT (%s::tsquery)::text", (f"'{lexeme}'",)).fetchone()
    except psycopg.errors.SyntaxError:
        # Malformed tsquery literal — same outcome the old to_tsquery path gave
        # for input it could not parse. Rolled back so the caller's transaction
        # is not left aborted.
        conn.rollback()
        return ""
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

