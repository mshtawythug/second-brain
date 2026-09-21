"""Typed request parsing and response construction.

Every validation rule the UI enforces lives here, so the route modules stay thin
(parse → service → serialize) and so the rules are unit-testable without an HTTP
client. Nothing in this module touches the database or the filesystem.

Validation is **fail-closed and explicit**: an out-of-range limit is a 400, not
a silent clamp, because a silently clamped limit is indistinguishable from a
working one and hides the caller's bug instead of surfacing it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..search import CANDIDATE_LIMIT
from ..source_kinds import VALID_SOURCE_KINDS as _CANONICAL_SOURCE_KINDS
from ..tags import normalize_tags
from .errors import UiBadRequest

#: The four source kinds `brain ingest` recognises — **the canonical object**,
#: re-exported from :data:`brain.source_kinds.VALID_SOURCE_KINDS`, not a copy of
#: it. Spec §2.1 requires exactly this ("have `cli.py` import it"; the review
#: rule rejects copying what can be extracted), and ``brain.cli`` already
#: complies via ``_VALID_SOURCE_KINDS``.
#:
#: This was a second literal until now, justified by import cost: reaching the
#: set meant importing the 9,800-line Typer CLI into every HTTP handler. The
#: extraction to ``brain.source_kinds`` removed that cost, measured here under
#: fresh 3.11 interpreters (re-derive; do not inherit these numbers):
#:
#: * ``import brain.source_kinds`` cold — 28 modules, of which 3 are ``brain.*``
#:   (``brain``, ``brain.errors``, itself); ``typer`` NOT loaded; 18.1 ms.
#: * ``import brain.cli`` cold — 615 modules; ``typer`` loaded; 306.7 ms. That
#:   is the cost the old justification was about, and it is not this import.
#: * **Incremental** cost of this line, the number that actually applies: this
#:   module already loads ``brain`` and ``brain.errors`` via ``..search``, so
#:   adding the import loads **1** further module in **0.18 ms**. For scale,
#:   ``brain.ui.schemas`` itself costs 340 modules / 101.6 ms.
#:
#: Re-exporting *deletes* the drift risk rather than guarding it: there is one
#: frozenset and both names bind it, so the two cannot disagree. What remains
#: possible is a future edit restating the literal here, which
#: ``tests/test_ui_schemas.py`` catches by asserting **identity** — ``is``, not
#: ``==``, because two equal frozenset literals are distinct objects and an
#: equality guard stays green against precisely that regression (verified).
VALID_SOURCE_KINDS: frozenset[str] = _CANONICAL_SOURCE_KINDS

#: The fifth value the Source dropdown offers: documents with **no** ``sources``
#: row at all (T7).
#:
#: Deliberately NOT a member of :data:`VALID_SOURCE_KINDS`. That name now *is*
#: :data:`brain.source_kinds.VALID_SOURCE_KINDS` — the same object, not a mirror
#: of it — so adding a pseudo-kind here would widen the enum the ingest write
#: boundaries validate against, pushing a value into ``sources.kind`` territory
#: that no ingest path can ever write. It is a *view* over the corpus, not a
#: kind of source, and it maps to ``build_predicate(source_missing=True)``
#: rather than to ``source_kind``.
#:
#: ``d.source_id IN (SELECT id FROM sources WHERE kind=%s)`` is false for a NULL
#: ``source_id`` under every one of the four real kinds, so without this value
#: those documents are unreachable from the filter in all of its settings.
SOURCE_NONE = "none"

#: Everything the ``source`` query parameter accepts.
SOURCE_FILTER_VALUES: frozenset[str] = VALID_SOURCE_KINDS | {SOURCE_NONE}

#: Matches MCP's ``_MAX_NOTE_BODY_BYTES`` so the two write surfaces cannot
#: disagree about what is too large.
MAX_BODY_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200
MAX_QUERY_CHARS = 512
MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 25

#: The largest ``offset`` a search may ask for.
#:
#: DERIVED from ``brain.search.CANDIDATE_LIMIT``, never hardcoded. Both ranking
#: legs bound their candidate pools at that many chunks, so at most
#: ``2 * CANDIDATE_LIMIT`` distinct documents can ever reach the RRF merge and
#: an offset past that can only ever return an empty page. A copied literal here
#: would go on advertising the OLD bound the day that constant moves — the same
#: two-sources-of-truth failure ``tests/conftest.py`` records for the Ollama
#: port guard, where only one of the two was redirected and the guard went on
#: guarding a port nothing dialled.
MAX_RANKED_DOCUMENTS = 2 * CANDIDATE_LIMIT
MAX_OFFSET = MAX_RANKED_DOCUMENTS
DEFAULT_OFFSET = 0

#: The four states a search page can end in. ONE enum rather than a pair of
#: booleans (``has_more`` + ``ceiling_reached``): two booleans admit a
#: combination that cannot happen and oblige every reader to learn which one
#: wins: this has exactly four states and no invalid one.
RANKING_MORE = "more"
RANKING_EXHAUSTED = "exhausted"
RANKING_CEILING = "ceiling"
RANKING_UNKNOWN = "unknown"


def ranking_status(
    *, ranked: int, fetch_limit: int, total_documents: int | None
) -> str:
    """Why this page ended, in one word.

    ``ranked`` is the size of the list ``hybrid_search`` returned BEFORE
    :meth:`SearchQuery.page_of` sliced it, and ``fetch_limit`` is what was asked
    for. ``hybrid_search`` applies ``limit`` exactly once, last, as
    ``results[:effective_limit]``, so ``ranked < fetch_limit`` is not a heuristic:
    it means the ranker had nothing more to give.

    * :data:`RANKING_MORE` — the over-fetch came back full. Honest limit: this
      says the ranked set did not end *within this request*, not that the next
      page is non-empty. A ranked set of exactly ``fetch_limit`` reports
      ``more`` and the following request then reports the real reason. The
      overshoot is one page and it is self-correcting, which is why the page
      size is not inflated by one to remove it — doing that would change
      :attr:`SearchQuery.fetch_limit`, the one arithmetic the paging tests
      exist to pin.
    * :data:`RANKING_UNKNOWN` — the ranker ran dry but ``total_documents`` is
      ``None``, so the two endings cannot be told apart. ``SearchDiagnostics``
      requires a caller that asked for the total and got ``None`` to render it
      as unknown, "never as zero"; folding it into ``exhausted`` would
      reintroduce this defect through the error path.
    * :data:`RANKING_CEILING` — the ranker ran dry with matches left behind.
      ``total_documents`` is an exact, uncapped ``count(DISTINCT document_id)``
      over the same lexical predicate, so ``total_documents - ranked`` is a
      real count of documents that match and were never ranked.
    * :data:`RANKING_EXHAUSTED` — the ranker ran dry and nothing lexical is
      left behind it.

    LEXICAL-ONLY, inherited from ``total_documents`` and stated rather than
    hidden. The vector leg may surface near-neighbours the count does not
    include, so a ranked set larger than the lexical total is ordinary and
    reports ``exhausted``; a ranked set truncated purely on the vector side is
    the one case this under-reports. Leg-saturation was the alternative and is
    worse: ``SearchDiagnostics`` records that the vector leg "always returns
    nearest neighbours", so it is saturated on nearly every query and a signal
    keyed on that would fire nearly always.
    """
    if ranked >= fetch_limit:
        return RANKING_MORE
    if total_documents is None:
        return RANKING_UNKNOWN
    if total_documents > ranked:
        return RANKING_CEILING
    return RANKING_EXHAUSTED


def ranking_payload(
    *, ranked: int, fetch_limit: int, total_documents: int | None
) -> dict[str, Any]:
    """The additive ``ranking`` object a search response carries.

    Grouped under one key rather than flattened into three, so a consumer that
    enumerates top-level keys sees a single addition.

    ``max_ranked_documents`` is emitted even though it is a constant: without
    it the ledger would have to hardcode the ceiling in JavaScript to explain
    it, which is the second copy of ``CANDIDATE_LIMIT`` that
    :data:`MAX_RANKED_DOCUMENTS` exists to avoid.

    Deliberately NOT added to ``brain.format_search.search_meta_json``. That
    projection is shared with MCP ``brain_search`` and ``brain search --json
    --meta``; neither pages, both are under active payload-size pressure, and a
    paging concern is not theirs to carry.
    """
    return {
        "status": ranking_status(
            ranked=ranked,
            fetch_limit=fetch_limit,
            total_documents=total_documents,
        ),
        "ranked_documents": ranked,
        "max_ranked_documents": MAX_RANKED_DOCUMENTS,
    }


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise UiBadRequest(f"{field} must be a string", code=f"invalid_{field}")
    return value


def parse_iso_date(raw: str, field: str) -> datetime:
    """Parse an ISO-8601 date or datetime into an aware UTC ``datetime``.

    A bare ``YYYY-MM-DD`` is accepted (the date inputs send exactly that) and is
    read as midnight UTC. Naive datetimes are assumed UTC rather than local
    time, so one query string means one thing regardless of the server's zone.
    """
    text = raw.strip()
    if not text:
        raise UiBadRequest(f"{field} must not be empty", code="invalid_date")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UiBadRequest(
            f"{field} must be an ISO-8601 date (YYYY-MM-DD)", code="invalid_date"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SearchQuery:
    """A validated ``GET /api/search`` request.

    Deliberately a plain object built by :func:`parse_search_params` rather than
    a dataclass with a permissive constructor: there is exactly one way to make
    one, and it validates.
    """

    __slots__ = (
        "after",
        "before",
        "content_type",
        "fts_only",
        "limit",
        "offset",
        "query",
        "session_id",
        "source_kind",
        "source_missing",
        "tag",
    )

    def __init__(
        self,
        *,
        query: str,
        limit: int,
        offset: int,
        source_kind: str | None,
        source_missing: bool,
        tag: str | None,
        content_type: str | None,
        after: datetime | None,
        before: datetime | None,
        fts_only: bool,
        session_id: str | None,
    ) -> None:
        self.query = query
        self.limit = limit
        self.offset = offset
        self.source_kind = source_kind
        self.source_missing = source_missing
        self.tag = tag
        self.content_type = content_type
        self.after = after
        self.before = before
        self.fts_only = fts_only
        self.session_id = session_id

    @property
    def fetch_limit(self) -> int:
        """How many rows to ask ``hybrid_search`` for: this page and every
        page before it.

        ``hybrid_search`` has no ``offset`` / ``cursor`` parameter, and adding
        one would move an **eval-gated** module. So a page is taken by
        over-fetching and slicing (:meth:`page_of`), which is EXACT rather than
        an approximation, for two reasons that are properties of
        :mod:`brain.search` and not of this module:

        * both ranking legs bound their candidate pools by the module constant
          ``CANDIDATE_LIMIT`` — neither ``LIMIT`` mentions the caller's
          ``limit``, so raising it cannot pull a different set of candidates
          into the RRF merge; and
        * ``limit`` is applied exactly once, last, as a truncation of the
          fully-sorted result list.

        Together those make ``search(limit=o + n)[o:]`` the same rows, in the
        same order, that a real ``OFFSET o LIMIT n`` would return.

        **The cost is accepted, not unnoticed:** every page re-pays the whole
        rank leg, measured at ~5.9 s for 545 matches in the phase-0 pass, so
        page 4 is four times that work in total. Paging is bounded at
        :data:`MAX_OFFSET` for the separate reason that nothing beyond it can
        exist.
        """
        return self.offset + self.limit

    def page_of(self, results: list[Any]) -> list[Any]:
        """The slice of an over-fetched ranking that this page shows.

        Deliberately open-ended on the right: the caller asked for exactly
        :attr:`fetch_limit` rows, so everything from ``offset`` on is already
        at most ``limit`` long, and a second bound would be a second place for
        the arithmetic to be wrong.
        """
        return results[self.offset :]

    def filter_kwargs(self) -> dict[str, Any]:
        """The filter kwargs to splat into ``hybrid_search``.

        Only the three mandated dropdown axes and the date range. The
        config-sourced tuning kwargs (``vector_sim_floor`` and friends) are
        added by the route from ``cfg``, so this object never needs to know
        configuration exists.

        ``limit`` is :attr:`fetch_limit`, not :attr:`limit` — see there.

        ``source_missing`` is emitted **only when it is on**. Every filter above
        is passed unconditionally because ``hybrid_search`` has accepted it
        since before this module existed; this one is the opt-in view added by
        T7, and omitting it when off keeps the call byte-identical for every
        search that did not ask for it. That is the same shape as the predicate
        default it feeds: not passing it is indistinguishable from passing its
        default, and the smaller call site is the one that cannot regress an
        eval-gated ranker by accident.
        """
        kwargs: dict[str, Any] = {
            "query": self.query,
            "limit": self.fetch_limit,
            "source_kind": self.source_kind,
            "tag": self.tag,
            "content_type": self.content_type,
            "after": self.after,
            "before": self.before,
            "fts_only": self.fts_only,
        }
        if self.source_missing:
            kwargs["source_missing"] = True
        return kwargs


def _parse_offset(raw: Any) -> int:
    """Validate the ``offset`` query parameter.

    Absent or empty means :data:`DEFAULT_OFFSET` — the ledger shipped before
    paging existed and its requests carry no ``offset``, so any other default
    would silently move every existing caller's first page.

    Out of range is a 400 rather than a clamp, per this module's fail-closed
    contract: a clamped offset is indistinguishable from a working one, so a
    client paging past the end would be handed page 1 forever and read that as
    "no more results".

    The bound is INCLUSIVE of :data:`MAX_OFFSET`, and that is not an
    off-by-one. ``offset == MAX_OFFSET`` can only ever return an empty page —
    at most ``MAX_RANKED_DOCUMENTS`` documents are rankable and
    :meth:`SearchQuery.page_of` slices from there — but an empty page is a
    *supported terminal answer* here, not a failure: it carries
    :data:`RANKING_CEILING`, which tells the reader they hit the ranked ceiling
    rather than exhausted the matches (defect #27, pinned by
    ``test_an_empty_page_past_the_ranked_ceiling_reports_ceiling_not_exhaustion``).
    A client stepping forward by ``limit`` lands exactly on ``MAX_OFFSET``
    whenever ``limit`` divides it, so rejecting that offset would replace the
    one informative terminal state with a 400 on the last legal step.
    """
    if raw in (None, ""):
        return DEFAULT_OFFSET
    try:
        offset = int(raw)
    except (TypeError, ValueError) as exc:
        raise UiBadRequest("offset must be an integer", code="invalid_offset") from exc
    if not 0 <= offset <= MAX_OFFSET:
        raise UiBadRequest(
            f"offset must be between 0 and {MAX_OFFSET}", code="invalid_offset"
        )
    return offset


def parse_search_params(params: Any) -> SearchQuery:
    """Validate a query-string mapping into a :class:`SearchQuery`.

    ``source`` is checked against :data:`VALID_SOURCE_KINDS` rather than passed
    through, mirroring ``cli._validate_source_choice`` — an unknown source
    silently returning zero results is far more confusing than a 400 naming the
    four legal values.
    """
    query = str(params.get("q", "")).strip()
    if not query:
        raise UiBadRequest("q is required", code="missing_query")
    if len(query) > MAX_QUERY_CHARS:
        raise UiBadRequest(
            f"q must be at most {MAX_QUERY_CHARS} characters", code="query_too_long"
        )

    raw_limit = params.get("limit")
    if raw_limit in (None, ""):
        limit = DEFAULT_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise UiBadRequest(
                "limit must be an integer", code="invalid_limit"
            ) from exc
        if not MIN_LIMIT <= limit <= MAX_LIMIT:
            raise UiBadRequest(
                f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}",
                code="invalid_limit",
            )

    offset = _parse_offset(params.get("offset"))

    source = (params.get("source") or "").strip() or None
    if source is not None and source not in SOURCE_FILTER_VALUES:
        raise UiBadRequest(
            f"unknown source {source!r} "
            f"(expected: {'|'.join(sorted(SOURCE_FILTER_VALUES))})",
            code="invalid_source",
        )
    # ``none`` is a view, not a kind: it must reach ``build_predicate`` as
    # ``source_missing=True``, never as ``source_kind="none"`` — the latter
    # would look up a ``sources`` row whose kind no ingest path can write and
    # silently return an empty result set.
    source_missing = source == SOURCE_NONE
    if source_missing:
        source = None

    tag = (params.get("tag") or "").strip() or None
    if tag is not None:
        normalized = normalize_tags([tag])
        tag = normalized[0] if normalized else None

    content_type = (params.get("type") or "").strip() or None
    after_raw = (params.get("after") or "").strip()
    before_raw = (params.get("before") or "").strip()
    after = parse_iso_date(after_raw, "after") if after_raw else None
    before = parse_iso_date(before_raw, "before") if before_raw else None
    if after and before and after > before:
        raise UiBadRequest("after must not be later than before", code="invalid_date")

    return SearchQuery(
        query=query,
        limit=limit,
        offset=offset,
        source_kind=source,
        source_missing=source_missing,
        tag=tag,
        content_type=content_type,
        after=after,
        before=before,
        fts_only=str(params.get("fts_only", "")).lower() in {"1", "true", "yes"},
        session_id=(params.get("session_id") or "").strip() or None,
    )


class NotePatch:
    """A validated ``PUT /api/notes/{id}`` body.

    ``body_hash`` is mandatory, and that is the whole optimistic-concurrency
    story: the vault watcher and ``brain-mcp`` are both live writers on these
    files, so a save without a hash is a save that can silently clobber someone
    else's write.
    """

    __slots__ = ("body", "body_hash", "content_type", "tags", "title")

    def __init__(
        self,
        *,
        body_hash: str,
        body: str | None,
        title: str | None,
        tags: list[str] | None,
        content_type: str | None,
    ) -> None:
        self.body_hash = body_hash
        self.body = body
        self.title = title
        self.tags = tags
        self.content_type = content_type

    def is_empty(self) -> bool:
        """True when the patch would change nothing."""
        return (
            self.body is None
            and self.title is None
            and self.tags is None
            and self.content_type is None
        )


def _parse_tags(payload: dict[str, Any]) -> list[str] | None:
    if payload.get("tags") is None:
        return None
    raw = payload["tags"]
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise UiBadRequest("tags must be a list of strings", code="invalid_tags")
    return normalize_tags(raw)


def _parse_title(payload: dict[str, Any], *, required: bool) -> str | None:
    raw = payload.get("title")
    if raw is None:
        if required:
            raise UiBadRequest("title is required", code="invalid_title")
        return None
    title = _require_str(raw, "title").strip()
    if required and not title:
        raise UiBadRequest("title must not be empty", code="invalid_title")
    if len(title) > MAX_TITLE_CHARS:
        raise UiBadRequest(
            f"title must be at most {MAX_TITLE_CHARS} characters",
            code="invalid_title",
        )
    return title


def _parse_body(payload: dict[str, Any]) -> str | None:
    if payload.get("body") is None:
        return None
    body = _require_str(payload["body"], "body")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise UiBadRequest(
            f"body must be at most {MAX_BODY_BYTES} bytes", code="body_too_large"
        )
    return body


def parse_note_patch(payload: Any) -> NotePatch:
    """Validate a ``PUT /api/notes/{id}`` body."""
    if not isinstance(payload, dict):
        raise UiBadRequest("body must be a JSON object", code="invalid_body")
    body_hash = payload.get("body_hash")
    if not isinstance(body_hash, str) or not body_hash.strip():
        raise UiBadRequest(
            "body_hash is required so a concurrent write cannot be clobbered",
            code="missing_body_hash",
        )
    content_type = payload.get("content_type")
    if content_type is not None:
        content_type = _require_str(content_type, "content_type").strip() or None

    return NotePatch(
        body_hash=body_hash.strip(),
        body=_parse_body(payload),
        title=_parse_title(payload, required=False),
        tags=_parse_tags(payload),
        content_type=content_type,
    )


class NoteCreate:
    """A validated ``POST /api/notes`` body."""

    __slots__ = ("body", "folder", "tags", "template", "title")

    def __init__(
        self,
        *,
        title: str,
        folder: str,
        tags: list[str],
        template: str,
        body: str | None,
    ) -> None:
        self.title = title
        self.folder = folder
        self.tags = tags
        self.template = template
        self.body = body


def parse_note_create(payload: Any) -> NoteCreate:
    """Validate a ``POST /api/notes`` body.

    ``folder`` is only *shape*-checked here; the traversal guard proper is
    :func:`brain.vault.paths.assert_within_vault`, applied in the service layer
    against the real vault root. Rejecting absolute paths and ``..`` segments
    here as well is defence in depth, not the primary control.
    """
    if not isinstance(payload, dict):
        raise UiBadRequest("body must be a JSON object", code="invalid_body")

    title = _parse_title(payload, required=True)
    if title is None:  # pragma: no cover — required=True raises first
        raise UiBadRequest("title is required", code="invalid_title")

    # Check the RAW value before normalizing. Stripping "/" first would make the
    # absolute-path test dead code and silently coerce "/etc" into "etc" — safe,
    # because assert_within_vault still guards, but surprising: the note would be
    # created somewhere the caller did not ask for instead of being refused.
    raw_folder = (payload.get("folder") or "").strip()
    if raw_folder.startswith(("/", "\\")) or ".." in raw_folder.replace(
        "\\", "/"
    ).split("/"):
        raise UiBadRequest(
            "folder must be a relative path inside the vault",
            code="folder_escapes_vault",
        )
    folder = raw_folder.strip("/")

    template = (payload.get("template") or "note").strip() or "note"
    if "/" in template or "\\" in template or template.startswith("."):
        raise UiBadRequest("invalid template name", code="invalid_template")

    return NoteCreate(
        title=title,
        folder=folder,
        tags=_parse_tags(payload) or [],
        template=template,
        body=_parse_body(payload),
    )


def require_confirm(payload: Any) -> None:
    """Reject a destructive request that did not opt in explicitly."""
    if not isinstance(payload, dict) or payload.get("confirm") is not True:
        raise UiBadRequest(
            'this operation requires {"confirm": true}', code="confirm_required"
        )


def result_date(result: Any) -> str | None:
    """The ``YYYY-MM-DD`` a ledger row shows, or ``None`` when there is none.

    ``SearchResult.recency_ts`` is ``coalesce(sent_at, ingested_at)`` — the same
    value the recency boost ranks on — so the date a row displays is the date it
    was ranked by. Only the calendar day is emitted: the gutter is 5.5rem wide,
    and a document's *time* of ingest is noise the reader cannot act on.

    ``None`` is returned rather than a placeholder string. The empty case is the
    ledger's to render (it already prints ``—`` for a missing source kind), and
    a server-side ``"—"`` would be indistinguishable from a real value to any
    other consumer of this payload.
    """
    # Direct attribute access, NOT getattr with a default: a rename of
    # SearchResult.recency_ts must break loudly here rather than silently make
    # every ledger row render "-" forever.
    #
    # The local annotation is load-bearing. ``result`` is ``Any``, so without it
    # the expression below is Any too and mypy rejects returning it as
    # ``str | None``. The previous ``str(...)`` wrapper silenced that — it looked
    # redundant (``isoformat`` returns ``str``) but was doing real work. Naming
    # the expected type is the honest version of the same fix.
    stamp: datetime | None = result.recency_ts
    if stamp is None:
        return None
    return stamp.date().isoformat()


def search_result_payload(result: Any) -> dict[str, Any]:
    """Project one ``SearchResult`` for the ledger."""
    return {
        "id": result.document_id,
        "title": result.title,
        "source_kind": result.source_kind,
        "date": result_date(result),
        "snippet": result.snippet,
        "score": round(float(result.score), 6),
        "content_type": result.content_type,
        "tags": list(result.tags or []),
    }
