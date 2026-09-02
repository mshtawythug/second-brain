"""What "payload tokens" means — one definition, so no two surfaces disagree.

Four places now COMPUTE a token count on a retrieval call: ``brain search``,
MCP ``brain_search``, ``brain recall``, MCP ``brain_recall``. A fifth,
``scripts/token_payload_report.py`` (the Wave-0 harness), measures the same
payloads offline and its numbers must be comparable with the persisted ones.

A sixth surface EMITS the key without computing it: ``brain ui`` projects the
envelope through :func:`brain.format_search.search_meta_json`
(``ui/routes_search.py``), but nothing on that path assigns
``diagnostics.results_tokens``, so the UI reports ``null`` there — always, not
occasionally. That is deliberate rather than an oversight: the browser view is
a local reading surface with no agent paying for the payload, so there is no
cost to attribute. Compute it there only if that stops being true; do not
"fix" the null by counting a payload nobody is billed for.
If any two of them serialized differently — ``indent=2`` here, ``ensure_ascii``
defaulted there — the comparison would silently compare two different things,
which is worse than not measuring at all.

So the serialization lives here, once, in :func:`serialize_payload`, counted
with the ``count_tokens`` half of the :class:`~brain.ingest.Embedder` Protocol.
That half is offline ``tiktoken`` in every backend, so this keeps working under
``BRAIN_EMBEDDER=none`` — the same reason :mod:`brain.token_budget` depends on
the callable rather than the embedder.

**That agreement is structural, not verified.** The Wave-0 harness *imports*
:func:`serialize_payload` and :func:`count_payload_tokens` rather than
re-implementing ``json.dumps(payload, ensure_ascii=False)`` beside them, so the
two cannot drift apart without deleting the import. An earlier revision of this
docstring claimed the harness numbers "are cross-checked against the persisted
ones" while the harness in fact duplicated the serialization in two places and
nothing asserted the two agreed — the claim held only because someone had run
it by hand once. If you are tempted to inline the ``json.dumps`` call back into
a caller for convenience, that is the regression this paragraph exists to stop.

The module depends on **nothing** but ``json`` and that callable: no DB, no
embedder object, no Rich.
"""
from __future__ import annotations

import json
from typing import Any

from .token_budget import TokenCost


def serialize_payload(payload: Any) -> str:
    """The one serialization every token count in this project is taken over.

    ``json.dumps(..., ensure_ascii=False)`` — compact separators, non-ASCII
    left as itself. Exposed rather than inlined so that callers needing the
    string itself (the Wave-0 harness measures ``chars`` as well as ``tokens``)
    size the *same* bytes this module counts, instead of writing an identical
    ``dumps`` call that is free to drift.
    """
    return json.dumps(payload, ensure_ascii=False)


def count_payload_tokens(payload: Any, *, cost: TokenCost) -> int:
    """Exact token cost of ``payload`` in its CANONICAL serialization.

    :func:`serialize_payload`, then counted. The serialization is not
    incidental: it is what puts key names, quoting, braces and separators into
    the count, which is real cost the caller pays and which summing the values
    would omit.
    """
    return cost(serialize_payload(payload))


def count_results_tokens(
    results_json: list[dict[str, Any]], *, cost: TokenCost
) -> int:
    """Exact token cost of the SERIALIZED search results array.

    Counts the canonical serialization of the projected list from
    :func:`brain.format_search.search_results_json` or its brief sibling — not
    the sum of its snippet strings, which would silently omit key names,
    quoting and separators. Canonical, not as-rendered: every JSON-emitting
    surface re-serializes with ``indent=2`` on the way out (Rich for the CLI,
    the MCP text block for the tool), which this count deliberately excludes
    so the surfaces stay comparable — see migration 028's header.

    Deliberately scoped to the RESULTS ARRAY, not the whole envelope: the
    envelope carries this number (``search_meta_json``'s ``results_tokens``),
    so counting the envelope would be self-referential — adding the count
    would change the count. It is also the array, and only the array, that the
    payload-reduction waves move, and the array is what the Wave-0 harness
    measures, so array-scoped is what makes the two comparable.
    """
    return count_payload_tokens(results_json, cost=cost)
