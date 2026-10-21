"""Score and rank the most-related documents for a source document.

The hybrid Related-docs signal, extracted from the wiki emitter so the
scoring can be reused (and tested) without importing the wiki package.

For a source document, :func:`_neighbors_for_source` mirrors
:func:`brain.search.hybrid_search` structurally — the source document acts
as the query. Weighted multi-field FTS (title weight A, tags B, content C)
is per-document-capped to ``PER_DOC_CHUNK_CAP`` chunks and blended via
Reciprocal Rank Fusion with a vector leg gated by ``vector_sim_floor``,
so related-docs ranking can never silently diverge from the user-facing one.

Pure ranking only: this module reads the database and returns ranked rows.
:func:`compute_related` is the entry point (``brain ui``'s related-notes rail);
:mod:`brain.connect` reuses the eligibility + embedding helpers for its own
auto-link scoring. The corpus-wide driver that fed the Quartz
``static/related/<slug>.json`` emitter (``_iter_hybrid_neighbors``) had no
production caller once the emitter was deleted with the wiki at R3 (phase 9);
it moved to ``tests/related_harness.py``, which composes the same helpers.

**File-size ceiling (CLAUDE.md): back UNDER, by extraction.** Re-derive with
``wc -l src/brain/related.py``. The trail is 714 (``3b16527``, where the wiki/ui
split created it) -> 720 (``0473b5f``) -> 726 (``7b5579e``) -> 851 (``b7fd0e8``,
the F6 gate, which crossed the ceiling: a recorded rule violation) -> 851
(``f0abf64``, the merge; no line changed) -> 851 (``0af7353``) -> 851
(``7536c4f``, promoted 2026-10-15) -> 730 (``7dfa9e8``, which moved both
candidate legs to brain.related_candidates; promoted 2026-10-21) -> **the
phase-9 commit that moved ``_iter_hybrid_neighbors`` to the test harness**,
descriptive, no delta: a hop cannot name its own SHA (see ``connect.py``).
Read it as authoritative only THROUGH THE LAST SHA IT NAMES; for anything after
it use
``git log --oneline f8c76c0..HEAD -- src/brain/related.py``. Being under is a
live condition, not a settled one: a commit that pushes it back over needs its
CLAUDE.md row back. The next clean seam, if one is needed, is the lexeme/tsquery
machinery at the tail (``_build_self_tsquery``, ``_corpus_common_lexemes``,
``_top_body_lexemes``, the tsquery-escaping helpers), already covered by
``tests/test_build_related_signal.py``.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .rank_fusion import rrf_contribution
from .related_candidates import confidential_clause, fts_candidates, vector_candidates
from .search import RRF_K

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
    exclude_confidential: bool = True,
) -> list[RelatedDoc]:
    """Return the ``limit`` documents most related to ``doc_id``, best first.

    The per-document entry point into the ranking. It delegates to
    :func:`_neighbors_for_source`, the same function the corpus-wide driver in
    ``tests/related_harness.py`` loops over, so the two rank identically for
    the same doc.

    ``exclude_confidential`` DEFAULTS TO ``True`` -- fail-closed, and gated now
    rather than in phase 5 because :class:`RelatedDoc` carries ``snippet``, a
    raw slice of a candidate's chunk ``content``: this is a BODY egress, not a
    title one. Deferring would make the phase-5 panel's author responsible for
    noticing that, at the moment they are least likely to. (Written before
    that panel existed; it now does -- see below.) Polarity: ``exclude_confidential``
    (``True`` = exclude) here vs MCP's ``include_confidential`` (``False`` =
    exclude); both exclude by default, bridge is ``not``. See
    :func:`brain.mcp_server._confidential_lens`.

    **Its consumer is :mod:`brain.ui.routes_related`** (``GET
    /api/notes/{id}/related``, phase 5), behind the related-notes rail in the
    ``brain ui`` inspector; ``tests/test_related_compute.py`` pins it directly.
    History: it was authored ahead of that consumer (design spec §9.2 and
    Appendix B-17 record it as a deposit), so the gating above was written for
    a panel that did not exist yet -- and that panel now ships. The route also
    gates the SOURCE, which this function does not (see the comment below).

    ``vector_sim_floor`` is required, with no default: the cosine floor is
    shared with runtime ``brain search`` and must not silently diverge from
    it. Callers plumb ``cfg.vector_sim_floor`` through.

    **Source eligibility is deliberately wider here.**
    :func:`_eligible_source_docs` restricts the corpus-wide source set to
    non-draft docs with a ``vault_path`` and at least one embedded chunk (the
    docs the deleted JSON emitter wrote files for; ``brain connect`` scores
    the same set). A reader can open a document that meets none
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
    # Gates CANDIDATES only; gating the SOURCE is the caller's job. A related
    # list for a confidential root is a précis of what is being withheld, so
    # ``brain.ui.routes_related`` checks the root's tier FIRST (403
    # ``related_withheld`` under a strict lens) and only then calls here.
    neighbors = _neighbors_for_source(
        conn,
        source=source,
        k=limit,
        vector_sim_floor=vector_sim_floor,
        corpus_common=_corpus_common_lexemes(conn),
        exclude_confidential=exclude_confidential,
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


@dataclass(frozen=True)
class _SourceDoc:
    """One eligible source document for the corpus-wide related-docs ranking."""

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


def _eligible_source_docs(
    conn: psycopg.Connection[Any], *, exclude_confidential: bool = True
) -> list[_SourceDoc]:
    """Return the eligible source documents for corpus-wide related-docs scoring.

    Eligibility mirrors the pre-rewrite query: non-draft, non-NULL
    ``vault_path``, at least one chunk with a non-NULL embedding.

    ``exclude_confidential`` defaults fail-closed, and gating the SOURCE is a
    SEPARATE leak from gating candidates. It was written for the Quartz
    ``static/related/<slug>.json`` emitter (deleted with the wiki at R3, phase
    9), where a confidential source that survived got its own published file,
    disclosing the note's existence and its neighbours' titles and body
    snippets. The fail-closed default stays: a future corpus-wide consumer
    inherits the safe polarity rather than having to remember it.

    Note the local default embedder (``arctic``) is not hosted, so F6's
    hosted-embedder boundary does NOT null a confidential doc's embeddings --
    it stays eligible here on the ``c.embedding IS NOT NULL`` clause. The
    hosted-embedder path is not a substitute for this gate.

    :mod:`brain.connect` passes ``False`` deliberately -- see its call site.
    """
    rows = conn.execute(
        f"""
        SELECT DISTINCT d.id::text, d.title, d.vault_path
        FROM documents d
        JOIN chunks c ON c.document_id = d.id
        WHERE d.draft = FALSE
          AND d.vault_path IS NOT NULL
          AND c.embedding IS NOT NULL
          {confidential_clause(exclude_confidential)}
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


def _neighbors_for_source(
    conn: psycopg.Connection[Any],
    *,
    source: _SourceDoc,
    k: int,
    vector_sim_floor: float,
    corpus_common: frozenset[str],
    exclude_confidential: bool = True,
) -> list[_Neighbor]:
    """RRF-blend the FTS + vector legs for one source doc and return top-K.

    Per-chunk RRF score: ``1 / (RRF_K + rank)`` from each leg the chunk
    appears in. Per-doc score: max chunk RRF for that document. Snippet:
    the highest-RRF chunk's content for the winning doc, whitespace-
    collapsed and truncated to ``SNIPPET_LENGTH``.

    ``exclude_confidential`` defaults fail-closed. Both production callers pass
    it explicitly, so it governs only new callers -- tests reaching in here
    included -- where the safe direction costs nothing to be wrong about.
    """
    tsquery = _build_self_tsquery(
        conn, source.id, title=source.title, corpus_common=corpus_common
    )
    src_embedding = _avg_embedding(conn, source.id)

    # The gate goes in the PREDICATE of BOTH legs, never the projection: a
    # confidential doc surviving either leg reaches ``by_doc`` with its chunk
    # ``content`` as the snippet, and dropping only the snippet afterwards
    # would still publish the id, title and vault_path. Same ruling as
    # ``timeline._compose_doc_filter``.
    fts_rows = fts_candidates(
        conn,
        tsquery=tsquery,
        exclude_doc_id=source.id,
        exclude_confidential=exclude_confidential,
    )
    vec_rows = vector_candidates(
        conn,
        src_embedding=src_embedding,
        exclude_doc_id=source.id,
        vector_sim_floor=vector_sim_floor,
        exclude_confidential=exclude_confidential,
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

