"""Which content types skip concept extraction, and the watermark a skip writes (RM-30).

Concept extraction costs about 8-10 minutes per document on the local model,
so a bulk source of short, structured documents (calendar events) would occupy
the graph lane for days while adding little: an event is about its people, and
the people aspect still runs. The types listed here skip the extractor.
``screenshot`` (RM-33, policy ``v2``) joined for the same reason: OCR text from a
screen grab is short and noisy, and a drop folder can deliver many a day.
``code`` (RM-43, policy ``v3``) joined because a code file's value is its
definition-level chunks, a repository delivers up to ``--max-files`` documents a
run, and a concept list adds little to a function.
``web_page`` (RM-41, policy ``v4``) joined because a page's body is its title, URL
and host (the page text only with ``--fetch``), and one run can write 500 of them.

A skip must still count as done, or ``graph_rag.build``'s backfill would re-plan
the document as stale forever. So :func:`skip_watermark` is the ONE place that
names the concept watermark a skipped document carries:
:func:`brain.graph_rag.reconcile_plan.plan_reconcile` compares the stored row
against it, and :func:`brain.graph_rag.reconcile.reconcile_document` writes it
from the plan. The comparison and the write cannot disagree.

**Bump :data:`CONCEPT_SKIP_POLICY_VERSION` whenever the set changes.** The
version is part of the skip watermark, so a bump makes every document of an
affected type stale. A type newly added is then reconciled with an empty
extraction, which removes its earlier concept mentions. A type removed loses
the skip pair, so its documents no longer match their stored row and are
extracted on the next backfill.

The constants are read at call time, not bound into a default argument, so a
test can swap them with ``monkeypatch.setattr``.
"""
from __future__ import annotations

__all__ = [
    "CONCEPT_SKIP_CONTENT_TYPES",
    "CONCEPT_SKIP_POLICY_VERSION",
    "SKIP_EXTRACTOR_VER",
    "skip_watermark",
]

#: ``documents.content_type`` values whose concept aspect is never extracted.
CONCEPT_SKIP_CONTENT_TYPES: frozenset[str] = frozenset(
    {"calendar_event", "screenshot", "code", "web_page"}
)

#: Bumped whenever :data:`CONCEPT_SKIP_CONTENT_TYPES` changes (module docstring).
#: ``v2`` (RM-33): ``screenshot`` added; every ``calendar_event`` is re-reconciled
#: once, with an empty extraction and no model call.
#: ``v3`` (RM-43): ``code`` added; every ``calendar_event`` and ``screenshot`` is
#: re-reconciled once the same way.
#: ``v4`` (RM-41): ``web_page`` added; every ``calendar_event``, ``screenshot`` and
#: ``code`` document is re-reconciled once the same way.
CONCEPT_SKIP_POLICY_VERSION = "v4"

#: The ``extractor_ver`` a skipped document's concept watermark stores.
SKIP_EXTRACTOR_VER = "skipped-by-policy"


def skip_watermark(content_type: str) -> tuple[str, str] | None:
    """``(inputs_hash, extractor_ver)`` for a skipped type, else ``None``.

    The pair stands in for ``(concept_inputs_hash, extractor.version)`` in the
    concept watermark: ``("skip:<content_type>:<policy version>",
    "skipped-by-policy")``.
    """
    if content_type not in CONCEPT_SKIP_CONTENT_TYPES:
        return None
    return f"skip:{content_type}:{CONCEPT_SKIP_POLICY_VERSION}", SKIP_EXTRACTOR_VER
