"""Tests for OllamaEnricher.draft_rule (Task 2.1) and GapDrafter (Task 2.2)."""
from __future__ import annotations

import json

import httpx
import pytest

from brain.elicit.drafter import GapDrafter
from brain.elicit.schema import Gap
from brain.enrichment import OllamaEnricher, RuleDraft
from brain.errors import EnrichmentError

# ---------------------------------------------------------------------------
# Helpers — mirror the pattern in test_enrichment.py exactly so the mock
# response shape matches what _chat_once / _chat_with_retry expect.
# ---------------------------------------------------------------------------


def _mock_transport(payload: dict) -> httpx.MockTransport:
    """Return a MockTransport that always replies with the given JSON payload
    nested inside the Ollama /api/chat envelope shape:
      {"message": {"content": "<json-encoded payload>"}, "done": True}
    """
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload),
                },
                "done": True,
            },
        )
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Task 2.1 — OllamaEnricher.draft_rule
# ---------------------------------------------------------------------------


def test_draft_rule_returns_title_and_rule() -> None:
    """draft_rule parses the LLM's {"title": ..., "rule": ...} response."""
    enr = OllamaEnricher(
        host="http://x",
        model="m",
        client=httpx.Client(
            base_url="http://x",
            transport=_mock_transport(
                {
                    "title": "Protect recovery time",
                    "rule": "I never schedule deep work near interruptions.",
                }
            ),
        ),
    )
    out = enr.draft_rule(subject="seating", evidence_texts=["t1", "t2", "t3"])
    assert isinstance(out, RuleDraft)
    assert out.title == "Protect recovery time"
    assert "deep work" in out.rule_text
    assert out.model == "m"


def test_draft_rule_strips_whitespace() -> None:
    """Trailing/leading whitespace in the model response is stripped."""
    enr = OllamaEnricher(
        host="http://x",
        model="m",
        client=httpx.Client(
            base_url="http://x",
            transport=_mock_transport(
                {"title": "  Focus blocks  ", "rule": "  I block mornings.  "}
            ),
        ),
    )
    out = enr.draft_rule(subject="scheduling", evidence_texts=["e1"])
    assert out.title == "Focus blocks"
    assert out.rule_text == "I block mornings."


def test_draft_rule_includes_subject_in_user_prompt() -> None:
    """The composed user prompt must include the subject."""
    captured: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.read())
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"title": "T", "rule": "R"}),
                },
                "done": True,
            },
        )

    enr = OllamaEnricher(
        host="http://x",
        model="m",
        client=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)),
    )
    enr.draft_rule(subject="priority-setting", evidence_texts=["evidence text"])
    body = json.loads(captured[0])
    user_content = body["messages"][1]["content"]
    assert "priority-setting" in user_content
    assert "evidence text" in user_content


# ---------------------------------------------------------------------------
# Task 2.2 — GapDrafter
# ---------------------------------------------------------------------------


class _FakeEnricher:
    """Duck-typed stand-in for OllamaEnricher in drafter tests."""

    model = "fake"

    def draft_rule(self, *, subject: str, evidence_texts: list[str]) -> RuleDraft:
        return RuleDraft(
            title=f"Rule about {subject}",
            rule_text="drafted",
            model="fake",
        )


def test_gapdrafter_builds_elicitdraft(test_db) -> None:  # type: ignore[no-untyped-def]
    """GapDrafter.draft fetches evidence summaries and returns an ElicitDraft."""
    did = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, kind, summary) "
        "VALUES ('D', 'b', 'h1', 'note', 'ingested', 'a meaningful summary') RETURNING id"
    ).fetchone()[0]

    gap = Gap(
        gap_id="g1",
        signal_kind="delta",
        target_type="org",
        target_id="Acme",
        score=0.9,
        evidence_ids=[str(did)],
        evidence_texts=[],
        rationale="Acme is referenced a lot",
    )

    draft = GapDrafter(_FakeEnricher()).draft(test_db, gap, tenant_id="default")  # type: ignore[arg-type]

    assert draft.gap_id == "g1"
    assert draft.draft_text == "drafted"
    assert draft.evidence_texts  # fetched from documents.summary
    assert "a meaningful summary" in draft.evidence_texts
    assert draft.title.startswith("Rule about")


def test_gapdrafter_falls_back_to_content_when_no_summary(test_db) -> None:  # type: ignore[no-untyped-def]
    """When summary IS NULL, GapDrafter uses the first 500 chars of content."""
    did = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, kind) "
        "VALUES ('D2', 'fallback content here', 'h2', 'note', 'ingested') RETURNING id"
    ).fetchone()[0]

    gap = Gap(
        gap_id="g2",
        signal_kind="delta",
        target_type="org",
        target_id="Beta",
        score=0.5,
        evidence_ids=[str(did)],
        evidence_texts=[],
        rationale="",
    )

    draft = GapDrafter(_FakeEnricher()).draft(test_db, gap, tenant_id="default")  # type: ignore[arg-type]

    assert draft.evidence_texts
    assert "fallback content here" in draft.evidence_texts[0]


def test_gapdrafter_empty_evidence_ids(test_db) -> None:  # type: ignore[no-untyped-def]
    """Gap with no evidence_ids produces an ElicitDraft with empty evidence_texts."""
    gap = Gap(
        gap_id="g3",
        signal_kind="orphan",
        target_type="topic",
        target_id="planning",
        score=0.3,
        evidence_ids=[],
        evidence_texts=[],
        rationale="planning came up often",
    )

    draft = GapDrafter(_FakeEnricher()).draft(test_db, gap, tenant_id="default")  # type: ignore[arg-type]

    assert draft.gap_id == "g3"
    assert draft.evidence_texts == []


# ---------------------------------------------------------------------------
# Task 2.1 — draft_rule validation parity with summarize()
# ---------------------------------------------------------------------------


def _enricher_with_response(payload: dict) -> OllamaEnricher:
    return OllamaEnricher(
        host="http://x",
        model="m",
        client=httpx.Client(base_url="http://x", transport=_mock_transport(payload)),
    )


def test_draft_rule_raises_on_empty_title() -> None:
    """Whitespace-only title raises EnrichmentError (mirrors summarize behaviour)."""
    enr = _enricher_with_response({"title": "   ", "rule": "I do something."})
    with pytest.raises(EnrichmentError, match="empty title"):
        enr.draft_rule(subject="s", evidence_texts=[])


def test_draft_rule_raises_on_non_string_title() -> None:
    """Non-string title raises EnrichmentError."""
    enr = _enricher_with_response({"title": 42, "rule": "I do something."})
    with pytest.raises(EnrichmentError, match="non-string"):
        enr.draft_rule(subject="s", evidence_texts=[])


def test_draft_rule_raises_on_empty_rule() -> None:
    """Whitespace-only rule raises EnrichmentError."""
    enr = _enricher_with_response({"title": "Good title", "rule": "  "})
    with pytest.raises(EnrichmentError, match="empty rule"):
        enr.draft_rule(subject="s", evidence_texts=[])
