"""Gmail ingester — shells out to the `gws` CLI."""
import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from . import ExtractedDoc


class GmailError(RuntimeError):
    """Raised when the `gws` CLI is missing or returns a non-zero exit."""


Runner = Callable[[list[str]], str]


def list_messages(
    *,
    query: str | None = None,
    label: str | None = None,
    since: str | None = None,
    until: str | None = None,
    from_addr: str | None = None,
    max_results: int = 50,
    runner: Runner | None = None,
) -> list[dict[str, Any]]:
    """Return a list of ``{id, subject, from, date, snippet}`` dicts via ``gws gmail list``."""
    parts: list[str] = []
    if query:
        parts.append(query)
    if label:
        parts.append(f"label:{label}")
    if from_addr:
        parts.append(f"from:{from_addr}")
    if since:
        parts.append(f"after:{since}")
    if until:
        parts.append(f"before:{until}")
    q = " ".join(parts)
    out = _run(
        [
            "gws", "gmail", "list",
            "--query", q,
            "--max", str(max_results),
            "--format", "json",
        ],
        runner,
    )
    parsed: list[dict[str, Any]] = json.loads(out)
    return parsed


def read_message(message_id: str, *, runner: Runner | None = None) -> dict[str, Any]:
    """Return ``{id, subject, from, to, date, body}`` via ``gws gmail read``."""
    out = _run(
        ["gws", "gmail", "read", "--id", message_id, "--format", "json"],
        runner,
    )
    parsed: dict[str, Any] = json.loads(out)
    return parsed


def to_extracted_doc(msg: dict[str, Any]) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` from a ``gws gmail read`` response."""
    title = msg.get("subject") or "(no subject)"
    body = msg.get("body", "")
    return ExtractedDoc(
        title=title,
        content=body.strip(),
        content_type="email",
        source_path=None,
        metadata={
            "from": msg.get("from"),
            "to": msg.get("to"),
            "date": msg.get("date"),
            "message_id": msg.get("id"),
        },
    )


def _run(cmd: list[str], runner: Runner | None = None) -> str:
    """Execute ``cmd`` and return stdout; delegate to ``runner`` when supplied (tests)."""
    if runner is not None:
        return runner(cmd)
    if not shutil.which(cmd[0]):  # pragma: no cover - requires missing gws on PATH
        raise GmailError(f"`{cmd[0]}` CLI not found on PATH")
    proc = subprocess.run(  # pragma: no cover - exercised only against the real gws CLI
        cmd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:  # pragma: no cover - requires real gws failure
        raise GmailError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout  # pragma: no cover - requires real gws success
