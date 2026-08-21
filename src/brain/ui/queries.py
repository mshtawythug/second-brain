"""The reads `brain ui` needs that :mod:`brain.queries` does not already provide.

Every statement here is parameterized. **This is the only module in the package
allowed to contain SQL** — a route that grows its own query is a review
rejection, because that is exactly how a second, un-eval-gated implementation of
the brain starts (spec §9.3).
"""
from __future__ import annotations

from typing import Any

import psycopg

from ..sensitivity import CONFIDENTIAL

#: One indexed pass over the exported corpus. ``documents_vault_path_idx`` is a
#: partial unique index on precisely ``vault_path WHERE vault_path IS NOT NULL``
#: (migration 003), so this is an index-ordered scan rather than a sort.
#:
#: TWO FROZEN VARIANTS, selected by ``exclude_confidential`` — the same shape
#: the discovery statements below use, for the same reason: a ``%s`` for the
#: sensitivity level would put a positional parameter in a fragment other
#: queries compose, and the tree query binds no parameters at all today, so a
#: placeholder here would also force ``conn.execute`` to start format-escaping
#: a string full of nothing that needs it.
_TREE_SELECT = """
    SELECT id::text, title, vault_path, kind, draft,
           coalesce(sent_at, ingested_at)
    FROM documents
    WHERE vault_path IS NOT NULL
"""
_TREE_ORDER = "    ORDER BY vault_path\n"

#: The permissive variant: every exported row, whatever its sensitivity. This
#: is what the tree query used to be, unconditionally.
_TREE_SQL_ANY = f"{_TREE_SELECT}{_TREE_ORDER}"

#: The default, and the fail-closed one: confidential titles withheld.
#:
#: THE TREE IS AN UNPROMPTED SURFACE. ``boot()`` in ``static/js/main.js`` calls
#: ``loadTree()`` on load, and ``index.html`` puts the rail and the tree in the
#: same viewport on the same paint — so before the reader has typed anything,
#: every confidential title in the vault was on screen. The rail beside it and
#: search below it both gated; the tree did not. Three adjacent surfaces, three
#: behaviours, and the difference had never been decided.
#:
#: A TITLE IS CONTENT, which is the whole argument. ``notes_service`` already
#: withholds the body, the summary and even the HEADINGS of a confidential
#: note, on the grounds that section names leak structure. A filename in a
#: folder tree leaks the same class of thing — that the document exists, what
#: it is about, and where it sits — and it leaks it without being asked.
#:
#: Gated on ``serve_confidential_titles``, NOT ``serve_confidential_bodies``:
#: see :attr:`brain.ui.context.UiContext.serve_confidential_titles`.
#:
#: The level is interpolated from the Python constant rather than bound, for
#: the reasons set out at length on :data:`_DISCOVERABLE` below — it is a
#: module constant, never user input, and a bound parameter inside a composed
#: fragment is the coupling that actually causes defects here.
_TREE_SQL = (
    f"{_TREE_SELECT}      AND sensitivity <> '{CONFIDENTIAL}'\n{_TREE_ORDER}"
)

#: Wiki-link targets are matched on title OR on the extension-stripped vault
#: path, mirroring how ``brain.vault.resolver`` resolves them, so the inspector
#: linkifies exactly what a real vault sync would.
_RESOLVE_LINKS_SQL = """
    SELECT id::text, title, vault_path
    FROM documents
    WHERE lower(title) = ANY(%s)
       OR lower(regexp_replace(coalesce(vault_path, ''), '\\.md$', '')) = ANY(%s)
"""

_DISTINCT_CONTENT_TYPES = (
    "SELECT content_type, count(*) FROM documents "
    "WHERE content_type IS NOT NULL GROUP BY 1 ORDER BY 1"
)

_DISTINCT_SOURCE_KINDS = (
    "SELECT s.kind, count(d.id) FROM sources s "
    "LEFT JOIN documents d ON d.source_id = s.id "
    "GROUP BY 1 ORDER BY 1"
)


#: ``DocumentRow`` (``brain.queries``) deliberately carries the *show* fields
#: and not the vault placement ones, so the three columns the inspector needs to
#: decide "which tier is this, and can it be edited" come from here.
_NOTE_META_SQL = """
    SELECT vault_path, kind, draft
    FROM documents
    WHERE id = %s
"""


def iter_tree_rows(
    conn: psycopg.Connection[Any], *, exclude_confidential: bool = True
) -> list[tuple[Any, ...]]:
    """Every exported document, ordered by ``vault_path``, for the left rail.

    ``exclude_confidential`` defaults **True** — fail-closed, matching
    :func:`recent_documents` and :func:`documents_for_tag` — so a caller that
    forgets the argument hides confidential titles rather than publishing them.
    ``routes_tree`` computes it as ``not ctx.serve_confidential_titles``.

    Filtering here rather than in :func:`brain.ui.tree.build_tree` is what
    keeps the ROUTE'S COUNT HONEST: ``routes_tree`` sets ``payload["count"]``
    from ``len(rows)`` and every folder's ``note_count`` is folded from the
    same rows, so a hidden document is absent from the listing *and* from every
    number describing it. A post-fold filter would have had to correct counts
    at every node, and the one it missed would be the bug.
    """
    sql = _TREE_SQL if exclude_confidential else _TREE_SQL_ANY
    return list(conn.execute(sql).fetchall())


#: Which of a set of documents are confidential. Used to redact search snippets
#: — snippet text comes straight out of ``chunks``, so it is body text by
#: another name and is the least obvious place a withheld body leaks.
_CONFIDENTIAL_IDS_SQL = """
    SELECT id::text
    FROM documents
    WHERE id = ANY(%s) AND sensitivity = %s
"""


def confidential_document_ids(
    conn: psycopg.Connection[Any], document_ids: list[str]
) -> set[str]:
    """Return the subset of ``document_ids`` marked confidential.

    One round trip for a whole result page rather than one per row. An empty
    input short-circuits without touching the database.
    """
    if not document_ids:
        return set()
    rows = conn.execute(
        _CONFIDENTIAL_IDS_SQL, (list(document_ids), CONFIDENTIAL)
    ).fetchall()
    return {str(row[0]) for row in rows}


def note_meta(
    conn: psycopg.Connection[Any], document_id: str
) -> tuple[str | None, str, bool] | None:
    """``(vault_path, kind, draft)`` for one document, or ``None`` if it is gone.

    ``kind`` is the *tier* — ``'vault'`` or ``'ingested'`` (migration 003) — and
    is emphatically not ``content_type``; ``hybrid_search``'s own docstring
    warns about exactly that confusion.
    """
    row = conn.execute(_NOTE_META_SQL, (document_id,)).fetchone()
    if row is None:
        return None
    return (row[0], str(row[1] or "vault"), bool(row[2]))


def resolve_link_targets(
    conn: psycopg.Connection[Any], targets: list[str]
) -> dict[str, str]:
    """Map wiki-link targets to document ids, case-insensitively.

    One round trip for every link in a note rather than one per link. Returns a
    lowercase-keyed dict; unmatched targets are simply absent, which
    :func:`brain.ui.render.render_markdown` renders as an unresolved link.
    """
    if not targets:
        return {}
    lowered = [t.strip().lower() for t in targets if t.strip()]
    if not lowered:
        return {}
    rows = conn.execute(_RESOLVE_LINKS_SQL, (lowered, lowered)).fetchall()

    resolved: dict[str, str] = {}
    for doc_id, title, vault_path in rows:
        if title:
            resolved.setdefault(str(title).lower(), str(doc_id))
        if vault_path:
            stem = str(vault_path)
            if stem.endswith(".md"):
                stem = stem[:-3]
            resolved.setdefault(stem.lower(), str(doc_id))
    return resolved


def content_type_buckets(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """``content_type`` → document count, for the Type dropdown.

    Used only as the corpus-wide fallback that populates the dropdown before a
    query runs; once a search has run, F5's ``compute_facets`` supplies
    match-scoped counts and this is not consulted.
    """
    return [
        {"value": str(value), "count": int(count)}
        for value, count in conn.execute(_DISTINCT_CONTENT_TYPES).fetchall()
    ]


#: How many documents have no ``sources`` row at all.
#:
#: A SEPARATE STATEMENT BECAUSE :data:`_DISTINCT_SOURCE_KINDS` CANNOT ANSWER
#: IT. That query starts ``FROM sources``, so a document with a NULL
#: ``source_id`` is not merely uncounted there — it is unreachable, and no
#: amount of joining changes that without inverting the query. This one starts
#: from ``documents``, which is the only side that has the rows.
#:
#: Corpus-wide, deliberately: it sits beside the ``sources.kind`` counts in
#: ``/api/facets``, and those are corpus-wide too. A narrower scope here would
#: put one differently-scoped number in a row of otherwise-comparable ones,
#: which is the failure mode the ``none`` bucket exists to fix rather than
#: repeat.
_SOURCELESS_DOCUMENT_COUNT = (
    "SELECT count(*) FROM documents WHERE source_id IS NULL"
)


def sourceless_document_count(conn: psycopg.Connection[Any]) -> int:
    """How many documents have no ``sources`` row — the ``none`` facet's count.

    The Source dropdown's ``none`` value shipped ``count: null`` because no
    query in this module could produce the number. It can now, so it does: a
    filter offering a real, clickable selection while refusing to say how large
    it is asks the reader to guess, and ``null`` rendered beside four real
    counts reads as "zero" more often than as "unknown".
    """
    row = conn.execute(_SOURCELESS_DOCUMENT_COUNT).fetchone()
    return int(row[0]) if row else 0


def source_kind_buckets(
    conn: psycopg.Connection[Any], *, known: frozenset[str]
) -> list[dict[str, Any]]:
    """``sources.kind`` → document count, unioned with the known kinds.

    The union matters: a brain with no Slack rows yet should still offer
    ``slack`` in the dropdown at count 0, rather than the option appearing only
    after the first ingest.
    """
    counts = {
        str(kind): int(count)
        for kind, count in conn.execute(_DISTINCT_SOURCE_KINDS).fetchall()
        if kind
    }
    for kind in known:
        counts.setdefault(kind, 0)
    return [
        {"value": kind, "count": counts[kind]} for kind in sorted(counts)
    ]


# ---------------------------------------------------------------------------
# Discovery — the recent rail and the tag index.
# ---------------------------------------------------------------------------

#: What counts as *browseable* for the discovery surfaces. Ported from
#: ``brain.wiki.build_homepage._fetch_recent_docs``, whose docstring is the
#: authority on why each clause exists — plus ONE clause that is not from the
#: wiki at all (sensitivity, below); in short:
#:
#: - ``draft = FALSE`` — drafts are quarantined from public surfaces (P1.6).
#: - ``vault_path IS NOT NULL`` — un-exported rows have nothing to open.
#:   **Redundant, kept deliberately.** Dropping it alone changes no result and
#:   fails no test (measured): under three-valued logic ``NULL <> 'index.md'``
#:   and ``NULL NOT LIKE 'people/%'`` are both NULL, so either path predicate
#:   already excludes a NULL-path row. It survives as defence in depth — it is
#:   the clause that keeps the guarantee if those two are ever narrowed — and
#:   as the one that states the intent. What *is* proven is the trio: drop all
#:   three and ``…excludes_unexported_documents`` goes red.
#: - ``ingested_at IS NOT NULL`` — defensive parity with the wiki query. It is
#:   *vacuous*: the column is ``NOT NULL`` in ``001_init.sql``, so no mutation
#:   of it can fail a test. ``test_ui_queries_discovery`` pins that constraint
#:   so the day a migration relaxes it — the day this clause starts to matter —
#:   turns red rather than silent.
#: - ``vault_path <> 'index.md'`` — the home note.
#: - ``vault_path NOT LIKE 'people/%'`` — the People-Hub namespace, every page
#:   of which (``people/<slug>.md`` + ``people/index.md``) is machine-generated
#:   and re-stamped on each regeneration, so it would swamp a date-ranked rail.
#:   Path-based rather than a jsonb lookup so it never collides with psycopg
#:   ``%s`` placeholders.
#:
#: - ``sensitivity <> 'confidential'`` — **NOT ported from the wiki; added for
#:   T17, and CONDITIONAL since the 2026-08-14 ruling.** It applies when
#:   ``exclude_confidential`` is true, which all three discovery routes compute
#:   as ``not ctx.serve_confidential_titles`` (``routes_discovery``). That is
#:   the TITLES flag, not the bodies one, because these surfaces list documents
#:   nobody asked to see. ``routes_search.py`` and ``notes_service.py`` stay on
#:   ``serve_confidential_bodies`` — a prompted search and an opened body are a
#:   different question about a different thing; see
#:   :attr:`brain.ui.context.UiContext.serve_confidential_titles`.
#:   IT IS CONDITIONAL RATHER THAN ABSOLUTE, AND THAT WAS DELIBERATED — though
#:   the argument first written down here has since expired. It ran: the VAULT
#:   TREE named every confidential title on the same first paint, in the column
#:   beside this rail, ungated, so an absolute here protected nothing that was
#:   not already on screen. THE TREE NOW GATES TOO (``routes_tree``, on this
#:   same flag), so that premise is void. What survives it is the ruling: one
#:   flag governs all four adjacent surfaces, so they move together. Four
#:   surfaces with three different rules is how "one of these must be a bug"
#:   starts.
#:   Deliberately placed HERE rather than in the two queries, so the recent rail
#:   and the tag click-through cannot drift apart — one predicate, both
#:   surfaces.
#:   THE CLIENT CANNOT DO THIS. ``_discovery_row`` ships
#:   ``{id, title, vault_path, source_kind, date}`` and no proxy for
#:   sensitivity, so a front end can neither filter nor even detect these rows.
#:   That is why it is a predicate and not a UI concern.
#:   THE LEVEL IS INTERPOLATED FROM THE PYTHON CONSTANT, NOT PARAMETERIZED, and
#:   that is a deliberate exception rather than an oversight. ``CONFIDENTIAL``
#:   is a module constant (``brain.sensitivity``), never user input, so the
#:   parameterization rule — which exists to keep *untrusted values* out of SQL
#:   text — is not what is at stake. A placeholder here would put a positional
#:   parameter inside a SHARED fragment, so every present and future query
#:   composing ``_DISCOVERABLE`` would have to bind it first, in order, or bind
#:   the wrong thing silently. Interpolating the constant keeps the fragment
#:   self-contained and keeps one source of truth for the level. The value
#:   contains no ``%``, so it does not interact with the doubling rule below,
#:   and the vocabulary itself is pinned by migration 026's CHECK constraint
#:   against the Python set (``tests/test_migration_026_sensitivity.py``).
#:
#: The literal ``%`` is doubled because psycopg treats the SQL string as a
#: format template **whenever parameters are passed**. Every query below binds
#: at least one parameter, so the doubling is required, not optional; a future
#: consumer that interpolates this fragment into a parameterless statement must
#: undouble it. ``test_recent_documents_excludes_people_hub_pages`` fails if
#: this is ever got wrong in either direction.
#: Everything above EXCEPT sensitivity. Separated so the sensitivity clause can
#: be switched by the caller without any runtime string building: the two
#: variants are frozen at import, and a function picks one. The alternative —
#: a ``%s`` placeholder for the level — would put a positional parameter inside
#: a SHARED fragment, so every query composing it would have to bind that
#: parameter first, in order, or silently bind the wrong thing. Two constants
#: cost two lines; that coupling costs a defect nobody can see in a diff.
_DISCOVERABLE_ANY_SENSITIVITY = """
          d.draft = FALSE
      AND d.vault_path IS NOT NULL
      AND d.ingested_at IS NOT NULL
      AND d.vault_path <> 'index.md'
      AND d.vault_path NOT LIKE 'people/%%'
"""

#: The default scope: browseable AND not confidential.
_DISCOVERABLE = (
    f"{_DISCOVERABLE_ANY_SENSITIVITY}      AND d.sensitivity <> '{CONFIDENTIAL}'\n"
)

#: Event time, not processing time: ``doc_date`` is the generated
#: ``coalesce(sent_at, ingested_at)`` column (migration 021), so a meeting held
#: last month but ingested today ranks by the meeting date. The outer
#: ``coalesce`` mirrors the wiki query and costs nothing.
_DISCOVERY_DATE = "coalesce(d.doc_date, d.ingested_at)"

#: ``d.id`` breaks ties so two docs sharing a timestamp cannot swap places
#: between calls — otherwise a paged client can show or drop the same row twice.
_DISCOVERY_SELECT = f"""
    SELECT d.id::text, d.title, d.vault_path, s.kind, {_DISCOVERY_DATE}
    FROM documents d
    LEFT JOIN sources s ON s.id = d.source_id
"""

_DISCOVERY_ORDER = f"ORDER BY {_DISCOVERY_DATE} DESC, d.id LIMIT %s"

#: Each discovery query exists in TWO frozen variants — strict (confidential
#: excluded) and permissive — selected by ``exclude_confidential``. Composed
#: here rather than at call time so every statement the app can issue is
#: visible in this module as a constant.
_RECENT_SQL = f"{_DISCOVERY_SELECT} WHERE {_DISCOVERABLE} {_DISCOVERY_ORDER}"
_RECENT_SQL_ANY = (
    f"{_DISCOVERY_SELECT} WHERE {_DISCOVERABLE_ANY_SENSITIVITY} {_DISCOVERY_ORDER}"
)

_TAG_DOCS_SQL = (
    f"{_DISCOVERY_SELECT} WHERE {_DISCOVERABLE} AND %s = ANY(d.tags) "
    f"{_DISCOVERY_ORDER}"
)
_TAG_DOCS_SQL_ANY = (
    f"{_DISCOVERY_SELECT} WHERE {_DISCOVERABLE_ANY_SENSITIVITY} "
    f"AND %s = ANY(d.tags) {_DISCOVERY_ORDER}"
)

#: The aggregate ``queries.list_existing_tags`` runs and then throws the count
#: away. ``/api/facets`` keeps it (T4).
#:
#: The PERMISSIVE variant: every document, whatever its sensitivity. Named
#: ``_ANY`` like the four pairs above, and paired with the strict one below for
#: the same reason — a positional parameter inside a shared fragment is a defect
#: nobody can see in a diff.
_TAG_COUNTS_SQL_ANY = """
    SELECT t, count(*)
    FROM documents, unnest(tags) AS t
    GROUP BY t
    HAVING count(*) >= %s
    ORDER BY t
"""

#: The strict variant. ``/api/facets`` issues it on every NON-LOOPBACK request
#: -- and not by default: :func:`tag_counts` defaults
#: ``exclude_confidential=False`` and so selects the ``_ANY`` variant above
#: unless a caller says otherwise. ("on every request" is what this line said
#: until 2026-08-21, which the next two sentences immediately contradicted:
#: they say a loopback bind issues the permissive variant. Both could not be
#: true, and the wrong half was the one a reader meets first.)
#: ``routes_meta.facets`` reaches this SQL only because it passes
#: ``exclude_confidential=strict`` explicitly, and ``strict`` is itself
#: ``not ctx.serve_confidential_titles`` -- so on a loopback bind this route
#: issues the permissive variant instead. Saying "by default" here named the
#: wrong mechanism twice over and made the permissive default look like a
#: protection; the default is argued at length on :func:`tag_counts`, and
#: ``tests/test_ui_queries_confidential_defaults.py`` pins both it and the
#: explicit call site.
#:
#: CORPUS-WIDE MINUS CONFIDENTIAL — a third scope, deliberately neither of the
#: other two. It keeps the drafts and the ``people/`` pages that
#: :data:`_BROWSEABLE_TAG_COUNTS_SQL` drops, because the recorded justification
#: for this route's scope is about those and only those; it drops confidential
#: documents, which that justification never mentioned. Narrowing the facet
#: count all the way to ``_DISCOVERABLE`` would silently decide the drafts
#: question too, and that question was already answered the other way.
_TAG_COUNTS_SQL = f"""
    SELECT t, count(*)
    FROM documents d, unnest(d.tags) AS t
    WHERE d.sensitivity <> '{CONFIDENTIAL}'
    GROUP BY t
    HAVING count(*) >= %s
    ORDER BY t
"""

#: The same aggregate over the BROWSEABLE corpus. Identical to
#: :data:`_TAG_COUNTS_SQL` but for the ``WHERE`` — deliberately, so the two
#: scopes differ in exactly one visible place — and it reuses
#: :data:`_DISCOVERABLE` rather than restating the predicates, so the tag index
#: and :func:`documents_for_tag` cannot drift apart.
#:
#: A TAG NAME IS CONTENT. Without this, a tag carried only by confidential
#: documents appeared BY NAME, with a count, on the idle rail — the unrequested
#: surface the sensitivity predicate was added to protect — and clicking it
#: returned nothing, which is *more* informative about what is being hidden than
#: a tag that simply resolves. See the CONFIDENTIALITY note on
#: :func:`browseable_tag_counts`.
_BROWSEABLE_TAG_COUNTS_SQL = f"""
    SELECT t, count(*)
    FROM documents d, unnest(d.tags) AS t
    WHERE {_DISCOVERABLE}
    GROUP BY t
    HAVING count(*) >= %s
    ORDER BY t
"""

_BROWSEABLE_TAG_COUNTS_SQL_ANY = f"""
    SELECT t, count(*)
    FROM documents d, unnest(d.tags) AS t
    WHERE {_DISCOVERABLE_ANY_SENSITIVITY}
    GROUP BY t
    HAVING count(*) >= %s
    ORDER BY t
"""


def _discovery_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape one discovery row as the JSON the routes hand to the client.

    ``source_kind`` is ``None`` for vault-tier notes (no ``sources`` row) — the
    LEFT JOIN keeps them rather than silently dropping them.
    """
    doc_id, title, vault_path, source_kind, display_date = row
    return {
        "id": str(doc_id),
        "title": str(title),
        "vault_path": str(vault_path),
        "source_kind": str(source_kind) if source_kind is not None else None,
        "date": display_date.isoformat() if display_date is not None else None,
    }


def _checked_limit(limit: int) -> int:
    """Reject a non-positive limit instead of letting SQL decide.

    ``LIMIT 0`` returns nothing and ``LIMIT -1`` is a syntax error, and neither
    is what a caller passing ``0`` means. Failing loudly here keeps a mistaken
    ``limit=0`` from reading as "the corpus is empty".
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    return limit


def recent_documents(
    conn: psycopg.Connection[Any], *, limit: int = 12, exclude_confidential: bool = True
) -> list[dict[str, Any]]:
    """The most recently *dated* browseable documents, newest first.

    A port of the wiki's P4.7 recent rail
    (``brain.wiki.build_homepage._fetch_recent_docs``) that returns document
    **ids**: the wiki links by vault path, the UI navigates by id.

    Filtering is :data:`_DISCOVERABLE` and ranking is :data:`_DISCOVERY_DATE`
    — see both for the reasoning. Read-only.
    """
    sql = _RECENT_SQL if exclude_confidential else _RECENT_SQL_ANY
    rows = conn.execute(sql, (_checked_limit(limit),)).fetchall()
    return [_discovery_row(row) for row in rows]


def documents_for_tag(
    conn: psycopg.Connection[Any],
    tag: str,
    *,
    limit: int = 50,
    exclude_confidential: bool = True,
) -> list[dict[str, Any]]:
    """Browseable documents carrying ``tag``, newest first.

    ``tag`` is canonicalized with :func:`brain.tags.normalize_tag` before the
    membership test, because that is the form every write boundary stores — a
    lookup built from a display label (``"Interview Prep"``) would otherwise
    match nothing. A tag that canonicalizes to the empty string matches
    nothing and short-circuits without touching the database.

    Same corpus as :func:`recent_documents`: this is a browse surface, so it
    hides drafts, un-exported rows, the generated pages and confidential
    documents. That makes it deliberately narrower than :func:`tag_counts` —
    not inconsistently so. The two answer different questions and are scoped to
    what each one annotates; :func:`tag_counts` carries the argument.
    """
    from ..tags import normalize_tag

    normalized = normalize_tag(tag)
    checked = _checked_limit(limit)
    if not normalized:
        return []
    sql = _TAG_DOCS_SQL if exclude_confidential else _TAG_DOCS_SQL_ANY
    rows = conn.execute(sql, (normalized, checked)).fetchall()
    return [_discovery_row(row) for row in rows]


def tag_counts(
    conn: psycopg.Connection[Any],
    *,
    min_doc_count: int = 1,
    exclude_confidential: bool = False,
) -> list[dict[str, Any]]:
    """``tag`` → document count, alpha-sorted, in the facet bucket shape.

    THE COUNT IS CORPUS-WIDE — drafts, ``index.md`` and every generated
    ``people/`` page included — while :func:`documents_for_tag` applies the
    browse predicates in :data:`_DISCOVERABLE`. The two disagree BY DESIGN: a
    tag's count here can legitimately exceed the number of rows that clicking
    it produces.

    THE REASON IS WHAT THIS COUNT ANNOTATES. It backs ``/api/facets``, and
    facets annotate **search** — which legitimately returns drafts and hub
    pages. A browse-filtered count would therefore understate its own result
    set: the panel would offer ``planning (4)`` beside five matching rows the
    same request had just returned. Corpus-wide is the only scope that
    describes the thing the facet is attached to.

    THE PARITY ARGUMENT IS TRUE BUT NOT LOAD-BEARING, and it used to stand here
    alone. This is also a drop-in replacement for ``queries.list_existing_tags``
    — same corpus, same sort, same ``min_doc_count`` bound, differing only in
    that it stops discarding the count that query already computes, so
    ``/api/facets`` can stop shipping ``count: null``. That explains where the
    SHAPE came from; it does not explain why the SCOPE is right. A reader who
    found only the precedent, then met :func:`documents_for_tag` counting a
    narrower corpus two functions above, would reasonably conclude one of them
    was a bug — and a justification that is true but not load-bearing reads as
    coverage it does not provide.

    A BROWSE-SCOPED COUNT WOULD BE A DIFFERENT QUESTION, NOT A CORRECTION.
    "How many documents carry this tag" and "how many of them can you browse
    to" are both answerable and are not the same number. Adding a second
    function for the latter is open; quietly renarrowing THIS one is not —
    ``/api/facets`` and ``test_tags_ship_real_counts`` both depend on the
    corpus-wide answer.

    THIS IS THE SEARCH-SCOPED ANSWER, AND IT IS NOT THE ONE A BROWSE SURFACE
    WANTS. :func:`browseable_tag_counts` is the second function this docstring
    anticipated; ``/api/tags`` uses it, ``/api/facets`` uses this one. The two
    exist because there are two questions, not because one is a bug.

    ``exclude_confidential`` IS A THIRD SCOPE, AND EVERYTHING ABOVE IS ABOUT
    DRAFTS. Re-read the four paragraphs above with sensitivity in mind and none
    of them mention it: every one argues that a *browse-filtered* count would
    understate a result set containing drafts and ``people/`` pages. That
    argument is sound and is untouched. It simply never covered confidential
    documents, and the scope it justified was silently doing two things.

    A TAG NAME IS CONTENT — the same ruling :func:`browseable_tag_counts`
    records, reached here by a different route. ``/api/facets`` is fetched by
    ``main.js``'s ``boot()`` with **no user action**, and the Tag dropdown
    renders ``name (count)``. So a tag carried only by confidential documents
    was named, with its volume, on first paint — beside a rail that hid exactly
    those tags.

    AND THE "IT ANNOTATES SEARCH" DEFENCE DOES NOT REACH THIS CASE. It is true
    that search returns confidential documents (titles kept, snippets redacted),
    so a count including them describes *search's* result set correctly. But
    these values populate the controls **before a query runs**, and once one has
    run the UI replaces them with ``compute_facets``' match-scoped numbers. The
    corpus-wide count is therefore only ever on screen when there is no result
    set for it to agree with — which is precisely the unprompted moment the
    titles flag governs. See ``routes_meta.facets``.

    THE DEFAULT IS PERMISSIVE, WHICH IS NOT THE HOUSE STYLE, AND IS DELIBERATE.
    Its four siblings default ``exclude_confidential=True`` (fail-closed);
    this one defaults ``False`` so that an unflagged call means what it has
    always meant. That keeps ``tag_counts``' recorded search-scoped contract —
    and the test that pins it — true, and confines the behaviour change to the
    one caller that asked for it. The protection does not rest on the default:
    ``routes_meta.facets`` passes the flag on every request, and
    ``tests/test_ui_confidential_titles_gate.py`` fails for **any** route that
    names a confidential title or tag, whichever query it reached for. A default
    guards one function; that test guards the class.
    """
    sql = _TAG_COUNTS_SQL if exclude_confidential else _TAG_COUNTS_SQL_ANY
    return [
        {"value": str(tag), "count": int(count)}
        for tag, count in conn.execute(sql, (min_doc_count,)).fetchall()
    ]


def browseable_tag_counts(
    conn: psycopg.Connection[Any],
    *,
    min_doc_count: int = 1,
    exclude_confidential: bool = True,
) -> list[dict[str, Any]]:
    """``tag`` → count over the BROWSEABLE corpus, in the facet bucket shape.

    :func:`tag_counts`' sibling, scoped to :data:`_DISCOVERABLE` — the same
    predicates :func:`documents_for_tag` applies. So this count and the rows a
    tag click produces describe the same corpus, and the tag index's number
    finally means what a reader takes it to mean.

    CONFIDENTIALITY, which is why this exists rather than being a nicety.
    ``tag_counts`` filters nothing. A tag carried ONLY by confidential
    documents therefore appeared, by name and with a count, on the idle rail —
    a surface that paints before the reader has asked for anything — while
    clicking it returned zero rows, because ``documents_for_tag`` does filter.
    **A tag name is content.** Names like ``severance`` or ``diagnosis`` leak
    the existence and the volume of confidential material without leaking a
    single title, and the empty click-through makes the omission louder rather
    than quieter. Adding the sensitivity predicate to ``_DISCOVERABLE`` without
    this made that *worse*, not better: before it, the tag at least resolved to
    its documents.

    WHY NOT A FLAG ON ``tag_counts``. A boolean that silently changes a
    function's scope puts the choice at every call site and the reasoning at
    none of them, and the next caller passes whichever value the surrounding
    code happened to use. Two names, two docstrings, two questions.

    WHY NOT FILTERED AT THE ROUTE. ``brain.ui.queries`` is the only module in
    the package allowed to hold SQL, and a route deciding which tags are
    confidential-only would need its own query to do it — the rule exists
    precisely to stop that.

    The residual gap between this count and a tag page is now ONE cause, the
    page limit in ``routes_discovery.TAG_PAGE_LIMIT``, rather than a mixture of
    limit, drafts, generated pages and sensitivity that no client could tell
    apart.

    THE TAG-NAME PROTECTION IS CONDITIONAL, AND THAT IS THE RULING, NOT AN
    OVERSIGHT. With ``exclude_confidential`` false — i.e. on loopback, or with
    ``--include-confidential`` — a tag carried only by confidential documents
    REAPPEARS BY NAME here, and its click-through returns those documents. That
    is coherent: such a session already reads those documents in search, in the
    note route and in the vault tree, so withholding only the tag name would
    protect nothing from anyone while making this the one surface that lies
    about the corpus. Off loopback the flag is false and everything stays
    hidden. Recorded here so nobody later reads the conditional exposure as a
    bug and "fixes" it into a fourth, inconsistent rule.
    """
    sql = (
        _BROWSEABLE_TAG_COUNTS_SQL
        if exclude_confidential
        else _BROWSEABLE_TAG_COUNTS_SQL_ANY
    )
    return [
        {"value": str(tag), "count": int(count)}
        for tag, count in conn.execute(sql, (min_doc_count,)).fetchall()
    ]
