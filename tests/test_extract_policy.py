"""Unit tests for :mod:`brain.graph_rag.extract_policy` (RM-30)."""
from __future__ import annotations

import pytest

from brain.graph_rag import extract_policy
from brain.graph_rag.extract_policy import (
    CONCEPT_SKIP_CONTENT_TYPES,
    CONCEPT_SKIP_POLICY_VERSION,
    SKIP_EXTRACTOR_VER,
    skip_watermark,
)


def test_calendar_event_is_a_skipped_type() -> None:
    assert "calendar_event" in CONCEPT_SKIP_CONTENT_TYPES


def test_screenshot_is_a_skipped_type() -> None:
    """RM-33: OCR text from a screenshot is short and noisy; its concept aspect is skipped."""
    assert "screenshot" in CONCEPT_SKIP_CONTENT_TYPES


def test_code_is_a_skipped_type() -> None:
    """RM-43: a code file's definition-level chunks are its value; concepts add little."""
    assert "code" in CONCEPT_SKIP_CONTENT_TYPES


def test_web_page_is_a_skipped_type() -> None:
    """RM-41: a page body is its title, URL and host; its concept aspect is skipped."""
    assert "web_page" in CONCEPT_SKIP_CONTENT_TYPES


def test_the_policy_version_was_bumped_for_web_page() -> None:
    """The set changed at RM-33 (v2), RM-43 (v3) and RM-41 (v4), so the version did too."""
    assert CONCEPT_SKIP_POLICY_VERSION == "v4"


def test_skip_watermark_for_a_skipped_type() -> None:
    assert skip_watermark("calendar_event") == (
        f"skip:calendar_event:{CONCEPT_SKIP_POLICY_VERSION}",
        "skipped-by-policy",
    )
    assert SKIP_EXTRACTOR_VER == "skipped-by-policy"


@pytest.mark.parametrize("content_type", ["email_thread", "transcript", "note", ""])
def test_skip_watermark_is_none_for_other_types(content_type: str) -> None:
    assert skip_watermark(content_type) is None


def test_skip_watermark_reads_the_policy_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy bump must reach the helper without re-importing it.

    ``reconcile_plan`` binds the function, not the constants, so the set and the
    version are read on every call.
    """
    monkeypatch.setattr(
        extract_policy, "CONCEPT_SKIP_CONTENT_TYPES", frozenset({"note"})
    )
    monkeypatch.setattr(extract_policy, "CONCEPT_SKIP_POLICY_VERSION", "v99")

    assert skip_watermark("note") == ("skip:note:v99", "skipped-by-policy")
    assert skip_watermark("calendar_event") is None
