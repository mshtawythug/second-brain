"""Every MCP tool that can name a document must accept the F6 lens — discovered.

``tests/test_mcp_confidential_egress.py`` exercises five tools BY HAND. That is
what let four ungated listing tools ship: a tool nobody wrote a line for is
invisible to it, and adding a fifth leaking tool changes nothing about whether it
passes. The HTTP surface already solved this — ``test_ui_confidential_titles_gate``
discovers its roster from the live route table, so a route added later is caught —
and MCP had no equivalent.

This module is that equivalent, and it inverts the failure mode that produced the
finding. **The roster is the COMPLEMENT of the exemptions.** Every tool on the
live registry is required to carry ``include_confidential`` unless it lands in one
of four explicitly-justified exemption sets. A tool added next week is in none of
them, so it is required to be gated and this module goes RED naming it. Two
hand-maintained rosters on this branch passed while silently omitting entries;
a list of what must be gated can be incomplete, a list of what need not be cannot
be — omitting an entry there is a failure, not a silent pass.

``test_the_gate_catches_a_newly_added_ungated_tool`` plants one on the real
registry and proves the gate reddens, so the guard itself is not taken on faith.

WHY A STRUCTURAL GATE RATHER THAN A BEHAVIOURAL ONE. The HTTP module can GET every
route and read the bytes back. Its MCP counterpart cannot: this registry holds
``brain_rm``, ``brain_edit``, ``brain_note_move`` and five graph rebuilders, so
"call everything both ways and diff" would delete documents and rebuild the graph
to find out. The behavioural proof lives in
``tests/test_mcp_listing_confidential.py`` and ``test_mcp_confidential_egress.py``,
per tool, with fixtures; what belongs HERE is the property those per-tool tests
cannot state — that the set of gated tools has no holes. Presence of the parameter
is necessary, not sufficient, and this module claims only the necessary half.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain import mcp_server

#: The parameter every document-returning MCP tool must accept.
LENS_PARAM = "include_confidential"


# ---------------------------------------------------------------------------
# The four exemption sets. Each entry is a RULING and carries its reason.
# ---------------------------------------------------------------------------

#: WRITES. A mutation names the document it is about — the caller passed the id
#: or the content — so it discloses nothing the caller did not already hold. They
#: are exempt from the LENS, not from authorization; ``_require_confirm`` is the
#: control that applies to them.
_WRITE_TOOLS = frozenset(
    {
        "brain_ingest_stdin",
        "brain_capture",
        "brain_tag",
        "brain_edit",
        "brain_rm",
        "brain_note_rename",
        "brain_note_move",
        "brain_note_new",
        "brain_daily",
        "brain_rate",
        "brain_connect_accept",
        "brain_connect_reject",
        "brain_graphrag_build",
        "brain_graphrag_refresh",
        "brain_graphrag_communities_build",
        "brain_graphrag_communities_refresh",
        "brain_graphrag_aliases_apply",
    }
)

#: PROMPTED READS. The caller named every document in the payload, so the tool
#: cannot tell it about one it had not already identified. This is the settled
#: ruling ``_PROMPTED`` records for ``/api/notes/{id_prefix}`` on the HTTP side.
#:
#: ``brain_link_proposal`` takes BOTH endpoints (``src_id_prefix`` and
#: ``dst_id_or_title``) and returns nothing else.
_PROMPTED = frozenset({"brain_link_proposal"})

#: NO DOCUMENT DATA. The payload contains no document title, body, summary or id
#: — verified against the return statement, not assumed from the name.
#:
#: - ``brain_status`` / ``brain_graphrag_stats`` — integer counts only.
#: - ``brain_graphrag_entities`` — entity names, which are a separate tier the
#:   branch already treats as ungated (an entity is not a document, and no
#:   document is identified through it).
#: - ``brain_gaps`` — the caller's own FAILED QUERIES (``{query, count, kind}``).
#:   Never a document; the text is the caller's own input echoed back.
_NO_DOCUMENT_DATA = frozenset(
    {
        "brain_status",
        "brain_graphrag_stats",
        "brain_graphrag_entities",
        "brain_gaps",
    }
)

#: KNOWN-UNGATED REMAINDER — **currently empty, and that is the claim.**
#:
#: This set exists so an accepted leak is recorded EXECUTABLY rather than in
#: prose, for the reason the branch's own strict-xfail block gives: a prose
#: remainder gets lost between passes, and "a gate that stopped one query short
#: of a sibling path" is the class that produced this finding twice.
#:
#: It held three entries when this module was written, all found BY building the
#: gate rather than by reading — ``brain_review_findings_list`` and
#: ``brain_review_scan`` (``evidence_ids`` enumerate documents; ``rationale`` is
#: body-derived), and ``brain_graphrag_communities`` (``summary`` is generated
#: from document titles). All three were closed in the same pass, so the set is
#: empty and every tool on the registry is now either gated or exempt on a
#: substantive ruling.
#:
#: It may only SHRINK (asserted below), so it cannot become a parking space for
#: a new leak: adding an entry requires editing this literal, writing the reason,
#: and changing the size assertion — three deliberate acts, none of them silent.
_KNOWN_UNGATED: frozenset[str] = frozenset()

#: The tools this module was written against. A FLOOR under the discovered set,
#: mirroring ``_KNOWN_GATED`` in the HTTP module: if a refactor drops the lens
#: from one of these, the requirement set shrinks and every assertion over it
#: gets weaker while staying green. Listing them turns that into a failure.
_KNOWN_GATED = frozenset(
    {
        "brain_search",
        "brain_recall",
        "brain_show",
        "brain_list",
        "brain_resurface",
        "brain_backlinks",
        "brain_links",
        "brain_orphans",
        "brain_ask",
        "brain_graphrag_search",
        "brain_graphrag_themes",
        "brain_graphrag_entity",
        # Closed by this pass.
        "brain_connect_list",
        "brain_timeline",
        "brain_brief",
        "brain_review_weekly",
        # Closed after the gate found them — they were the entire contents of
        # _KNOWN_UNGATED, and the gate is what surfaced them.
        "brain_review_findings_list",
        "brain_review_scan",
        "brain_graphrag_communities",
    }
)


def _registered_tools() -> dict[str, dict[str, Any]]:
    """Every tool on the LIVE registry → its published JSON-schema properties.

    Read from ``mcp_app._tool_manager`` rather than by parsing the module, which
    is the whole point: this is the registry the server actually serves, so a
    tool registered by any means — a decorator, a later ``add_tool``, an import
    side effect — is discovered. An AST pass sees only what it knows to look for,
    and a regex pass over triple-quoted docstrings mis-pairs quotes and reports
    gated functions as ungated.
    """
    return {
        tool.name: tool.parameters.get("properties", {})
        for tool in mcp_server.mcp_app._tool_manager.list_tools()
    }


def _exempt() -> frozenset[str]:
    return _WRITE_TOOLS | _PROMPTED | _NO_DOCUMENT_DATA | _KNOWN_UNGATED


def _ungated(tools: dict[str, dict[str, Any]]) -> set[str]:
    """Tools REQUIRED to carry the lens that do not — the gate's core claim.

    Factored out of the test so ``test_the_gate_catches_a_newly_added_ungated_tool``
    can call the same code path it is proving, rather than a paraphrase of it.
    """
    return {
        name
        for name, params in tools.items()
        if name not in _exempt() and LENS_PARAM not in params
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_document_returning_tool_accepts_the_lens() -> None:
    """THE ruling: gated, or explicitly and reasonably exempt. No third state."""
    tools = _registered_tools()
    assert tools, "the registry is empty — this module would pass vacuously"

    missing = _ungated(tools)

    assert not missing, (
        "these MCP tools can name documents the caller never asked for and take "
        f"no {LENS_PARAM!r} parameter: {sorted(missing)}.\n"
        "Either gate the tool (add the parameter and bridge it to the query with "
        "exclude_confidential=not include_confidential), or add it to exactly one "
        "exemption set in this module WITH a written reason."
    )


def test_the_lens_defaults_to_excluding() -> None:
    """Presence is not enough — the default has to point at "exclude".

    ``include_confidential: bool = True`` satisfies the test above while leaving
    the surface wide open, and it is a one-character edit away from correct. The
    default is the entire policy for every caller that does not pass the flag,
    which is every caller.
    """
    tools = _registered_tools()
    wrong = {
        name: params[LENS_PARAM].get("default")
        for name, params in tools.items()
        if LENS_PARAM in params and params[LENS_PARAM].get("default") is not False
    }

    assert not wrong, (
        f"{LENS_PARAM!r} must default to False (= exclude confidential): {wrong}"
    )


def test_the_registry_is_fully_partitioned() -> None:
    """Every tool is gated or in exactly one exemption set.

    Totality is what makes the discovery worth more than a list. The overlap
    check matters as much as the coverage check: a name in two sets is a ruling
    nobody made on purpose, and it would silently survive because both readings
    exempt it.
    """
    tools = _registered_tools()
    names = set(tools)

    sets = {
        "_WRITE_TOOLS": _WRITE_TOOLS,
        "_PROMPTED": _PROMPTED,
        "_NO_DOCUMENT_DATA": _NO_DOCUMENT_DATA,
        "_KNOWN_UNGATED": _KNOWN_UNGATED,
    }
    overlaps = {
        f"{a}&{b}": sorted(sets[a] & sets[b])
        for i, a in enumerate(sets)
        for b in list(sets)[i + 1 :]
        if sets[a] & sets[b]
    }
    assert not overlaps, f"a tool is exempted twice, under two rulings: {overlaps}"

    stale = _exempt() - names
    assert not stale, (
        f"exemptions naming tools that no longer exist, so they protect nothing "
        f"and hide nothing: {sorted(stale)}"
    )

    gated = {name for name, params in tools.items() if LENS_PARAM in params}
    assert gated | _exempt() == names, (
        f"tools neither gated nor exempted: {sorted(names - gated - _exempt())}"
    )
    # A gated tool must not ALSO be exempt — that combination means an exemption
    # is masking a surface that is in fact protected, and the next reader will
    # trust the exemption's reason instead of the code.
    assert not (gated & _exempt()), (
        f"tools both gated and exempt — delete the stale exemption: "
        f"{sorted(gated & _exempt())}"
    )


def test_the_gated_floor_only_grows() -> None:
    """A tool that used to carry the lens must not quietly lose it.

    Without this, deleting a parameter also deletes the assertion that covered
    it: the tool moves out of the discovered gated set and nothing notices.
    """
    gated = {name for name, params in _registered_tools().items() if LENS_PARAM in params}

    assert gated >= _KNOWN_GATED, (
        f"these tools used to accept {LENS_PARAM!r} and no longer do: "
        f"{sorted(_KNOWN_GATED - gated)}"
    )


def test_the_known_ungated_remainder_only_shrinks() -> None:
    """The remainder is a debt register, not a parking space.

    Pinned by SIZE as well as by content, the way ``_PROMPTED``'s size is pinned
    in the HTTP module and for the same reason: each entry silences this module
    for one tool, so each needs a deliberate edit and a written ruling. Closing
    one means deleting its entry here in the same change — and a tool that gets
    gated while still listed fails ``test_the_registry_is_fully_partitioned``'s
    gated-and-exempt check, so the register cannot rot in the other direction
    either.
    """
    assert len(_KNOWN_UNGATED) == 0, (
        f"the known-ungated remainder changed size. It is currently EMPTY — "
        f"every registered tool is gated or substantively exempt — so any entry "
        f"here is a NEW accepted leak and needs a written ruling beside it: "
        f"{sorted(_KNOWN_UNGATED)}"
    )


# ---------------------------------------------------------------------------
# Proof the gate works — plant an ungated tool and watch it fail
# ---------------------------------------------------------------------------


def test_the_gate_catches_a_newly_added_ungated_tool() -> None:
    """Register a leaking tool on the REAL registry; the gate must name it.

    This is the claim the whole module rests on, so it is proved rather than
    asserted. Everything above passes today on a registry that happens to be
    clean; none of it demonstrates that a tool added TOMORROW would be caught,
    which is the only property that distinguishes this from the hand-maintained
    roster it replaces.

    The plant goes on the live ``mcp_app`` — not a copy — because a copy would
    prove the gate works against a fixture rather than against the thing it
    guards. Removal is in ``finally`` so a failing assertion cannot leave the
    server carrying a synthetic tool into the rest of the session.
    """
    tools_before = _registered_tools()
    assert "brain_leaky_planted" not in tools_before

    @mcp_server.mcp_app.tool()
    def brain_leaky_planted(limit: int = 10) -> dict[str, Any]:
        """A synthetic listing tool that names documents and takes no lens."""
        return {"titles": ["Confidential Wind-Down Memo"]}

    try:
        caught = _ungated(_registered_tools())
        assert "brain_leaky_planted" in caught, (
            "the discovery gate did not notice a newly-registered ungated tool — "
            "it is INERT and proves nothing about tools added after it was "
            f"written. Reported: {sorted(caught)}"
        )
        # And the partition check must fail too, independently: the two
        # assertions rest on different set arithmetic, so a plant that trips only
        # one of them would mean the other is not doing the work its docstring
        # claims.
        with pytest.raises(AssertionError, match="neither gated nor exempted"):
            test_the_registry_is_fully_partitioned()
    finally:
        mcp_server.mcp_app.remove_tool("brain_leaky_planted")

    assert "brain_leaky_planted" not in _registered_tools(), (
        "the planted tool outlived the test and will corrupt later assertions"
    )
    # The registry is byte-identical to what it was, not merely missing the
    # plant — ``remove_tool`` could in principle drop more than it added.
    assert _registered_tools().keys() == tools_before.keys()


def test_the_gate_accepts_a_newly_added_GATED_tool() -> None:
    """The negative control for the test above.

    A gate that reported EVERY new tool would also pass the plant test, while
    being useless — it would fail on the next legitimately-gated tool anyone
    adds. So the same plant is repeated with the lens present and must NOT be
    reported.
    """
    @mcp_server.mcp_app.tool()
    def brain_clean_planted(
        limit: int = 10, include_confidential: bool = False
    ) -> dict[str, Any]:
        """A synthetic listing tool that DOES take the lens."""
        return {"titles": []}

    try:
        assert "brain_clean_planted" not in _ungated(_registered_tools()), (
            "the gate reports a correctly-gated tool as ungated — it is not "
            "measuring the parameter, it is just listing new tools"
        )
    finally:
        mcp_server.mcp_app.remove_tool("brain_clean_planted")
