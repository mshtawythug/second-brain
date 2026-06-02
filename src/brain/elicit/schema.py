"""Frozen value objects for tacit-knowledge elicitation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SignalKind = Literal["delta", "orphan", "contradiction", "user_flagged"]
TargetType = Literal["person", "org", "project", "topic", "tool", "doc"]
OutcomeAction = Literal["accepted", "skipped", "snoozed", "dismissed"]


@dataclass(frozen=True)
class Gap:
    gap_id: str
    signal_kind: SignalKind
    target_type: TargetType
    target_id: str
    score: float
    evidence_ids: list[str] = field(default_factory=list)
    evidence_texts: list[str] = field(default_factory=list)
    rationale: str = ""
    target_name: str = ""


@dataclass(frozen=True)
class ElicitDraft:
    gap_id: str
    title: str
    draft_text: str
    evidence_ids: list[str] = field(default_factory=list)
    evidence_texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ElicitOutcome:
    gap_id: str
    action: OutcomeAction
    note_id: str | None = None
    snoozed_days: int | None = None
