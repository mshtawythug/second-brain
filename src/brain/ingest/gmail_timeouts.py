"""``ingest-gmail``'s stop rule (BL-39): three ``gws`` timeouts in a row end the run.

A timeout fails its thread and the run goes on, as every per-thread failure does. But a ``gws``
that has stopped answering would cost :data:`brain.ingest.gmail.GWS_TIMEOUT_SECONDS` for every
thread left, so after :data:`MAX_CONSECUTIVE_TIMEOUTS` in a row the run stops. A thread whose
read returned, or that failed for any other reason, resets the count. A leaf, so
``cli_ingest.py`` (over the ceiling) carries only the calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from .gmail import GmailTimeoutError

MAX_CONSECUTIVE_TIMEOUTS = 3
#: Class-only: what stopped the run, never a thread's subject, sender or query.
STOP_MESSAGE = (
    f"ingest-gmail: stopped after {MAX_CONSECUTIVE_TIMEOUTS} gws calls in a row timed out; "
    "check gws, then re-run"
)


@dataclass
class ConsecutiveTimeouts:
    """The run of ``gws`` timeouts since the last thread that did not time out."""

    limit: int = MAX_CONSECUTIVE_TIMEOUTS
    count: int = 0

    def read_returned(self) -> None:
        """A thread's read came back: the run of timeouts is broken."""
        self.count = 0

    def failed(self, exc: BaseException) -> bool:
        """Count a thread's failure; True when the run must stop."""
        self.count = self.count + 1 if isinstance(exc, GmailTimeoutError) else 0
        return self.tripped

    @property
    def tripped(self) -> bool:
        """True once :attr:`limit` timeouts came in a row."""
        return self.count >= self.limit
