"""Tacit-knowledge elicitation — gap detection, drafting, and lifecycle management."""

from brain.elicit.detectors import DETECTOR_REGISTRY, GapDetector
from brain.elicit.queue import build_queue
from brain.elicit.schema import ElicitDraft, ElicitOutcome, Gap
from brain.elicit.session import run_session

__all__ = [
    "Gap",
    "ElicitDraft",
    "ElicitOutcome",
    "build_queue",
    "run_session",
    "GapDetector",
    "DETECTOR_REGISTRY",
]
