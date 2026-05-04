"""Gmail ingester — shells out to the `gws` CLI.

Uses the real ``gws gmail users messages list/get`` surface. Each operation
accepts a JSON-encoded ``--params`` blob that mirrors the underlying Gmail
REST API. Message bodies returned by the ``get`` call are base64url-encoded
and may live either on ``payload.body.data`` or inside ``payload.parts[]``
(multi-part messages) — this module normalises both shapes into plain text.
"""
import base64
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from brain.config import BOILERPLATE_PATTERNS

from . import ExtractedDoc

# Compile boilerplate patterns once at import time. Per-pattern ``(?s)``
# inline flags opt individual entries into DOTALL; see ``BOILERPLATE_PATTERNS``
# in ``brain.config`` for the rationale.
_BOILERPLATE_REGEXES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.MULTILINE | re.IGNORECASE) for p in BOILERPLATE_PATTERNS
)

# Run-collapse threshold: only collapse runs of consecutive identical lines
# longer than this (in characters). Short repeated lines (signoffs, blank
# separators, "ok") stay untouched.
_COLLAPSE_MIN_LINE_LEN = 40


class GmailError(RuntimeError):
    """Raised when the `gws` CLI is missing or returns a non-zero exit."""


Runner = Callable[[list[str]], str]


def _build_query(
    *,
    query: str | None,
    label: str | None,
    since: str | None,
    until: str | None,
    from_addr: str | None,
) -> str:
    """Compose the Gmail search ``q`` string from the CLI scope flags."""
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
    return " ".join(parts)


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
    """Return a list of message stubs: ``[{"id": ..., "threadId": ...}, ...]``.

    Calls ``gws gmail users messages list --params <json> --format json`` and
    returns the ``messages`` array. The Gmail API omits the ``messages`` key
    entirely when there are zero matches, so we coalesce to an empty list.
    """
    q = _build_query(
        query=query, label=label, since=since, until=until, from_addr=from_addr
    )
    params: dict[str, Any] = {"userId": "me", "maxResults": max_results}
    if q:
        params["q"] = q
    out = _run(
        [
            "gws", "gmail", "users", "messages", "list",
            "--params", json.dumps(params),
            "--format", "json",
        ],
        runner,
    )
    parsed = json.loads(out) if out.strip() else {}
    return list(parsed.get("messages") or [])


def read_message(message_id: str, *, runner: Runner | None = None) -> dict[str, Any]:
    """Return the full Gmail Message resource for ``message_id``.

    Calls ``gws gmail users messages get --params <json> --format json`` with
    ``format=full`` so the response includes headers and the message body
    payload (possibly multipart).
    """
    params = {"userId": "me", "id": message_id, "format": "full"}
    out = _run(
        [
            "gws", "gmail", "users", "messages", "get",
            "--params", json.dumps(params),
            "--format", "json",
        ],
        runner,
    )
    parsed: dict[str, Any] = json.loads(out)
    return parsed


def _headers_to_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    """Flatten a Gmail ``headers`` list to a ``{name.lower(): value}`` dict."""
    return {h.get("name", "").lower(): h.get("value", "") for h in headers or []}


def _decode_body_data(data: str) -> str:
    """Decode a Gmail API ``body.data`` base64url string into UTF-8 text."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded)
    return raw.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    """Naive HTML→text fallback: drop tags and collapse whitespace."""
    text = re.sub(r"<style.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_body(payload: dict[str, Any]) -> str:
    """Pull plain text out of a Gmail payload, recursing into parts.

    Strategy: prefer ``text/plain`` parts (aggregated), fall back to
    ``text/html`` (aggregated and HTML-stripped), and if neither is present,
    use whatever data is on the root payload's ``body``.
    """
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        mime = (node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        data = body.get("data") or ""
        if mime == "text/plain" and data:
            plain_chunks.append(_decode_body_data(data))
        elif mime == "text/html" and data:
            html_chunks.append(_decode_body_data(data))
        for child in node.get("parts") or []:
            visit(child)

    visit(payload)
    if plain_chunks:
        return "\n\n".join(plain_chunks)
    if html_chunks:
        return _strip_html("\n\n".join(html_chunks))
    root_data = (payload.get("body") or {}).get("data") or ""
    return _decode_body_data(root_data) if root_data else ""


def _collapse_repeated_long_lines(body: str) -> str:
    """Collapse runs of ≥2 consecutive identical lines longer than the threshold.

    Operates on the line view of ``body`` (split on ``\\n`` so empty trailing
    lines from a trailing newline are preserved). Only lines whose stripped
    length exceeds :data:`_COLLAPSE_MIN_LINE_LEN` are eligible — short
    repetitions (sign-offs, blank lines, single-word pleasantries) are left
    alone.
    """
    if not body:
        return body
    lines = body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for a run of identical lines starting at i.
        j = i + 1
        while j < len(lines) and lines[j] == line:
            j += 1
        run_length = j - i
        if run_length >= 2 and len(line.strip()) > _COLLAPSE_MIN_LINE_LEN:
            out.append(line)
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


def strip_boilerplate(body: str) -> str:
    """Remove repeated email signatures / disclaimers / mobile-app footers.

    1. Collapse runs of identical lines (>40 chars) to one occurrence.
    2. Strip patterns from ``BOILERPLATE_PATTERNS`` (config.py).
    Quoted-reply markers below the most recent message are left alone for
    single-message ingest; thread assembly (Phase 2) will revisit.
    """
    if not body:
        return body
    body = _collapse_repeated_long_lines(body)
    for regex in _BOILERPLATE_REGEXES:
        body = regex.sub("", body)
    return body.strip()


def _parse_date_header_to_iso_utc(raw: str | None) -> str | None:
    """Parse an RFC 2822 ``Date:`` header into an ISO-8601 UTC string.

    Returns ``None`` when ``raw`` is missing/empty or unparseable so the
    caller can omit the field rather than crash the ingest. Naive datetimes
    (rare in real Gmail headers) are treated as UTC.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def to_extracted_doc(msg: dict[str, Any]) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` from a Gmail ``users.messages.get`` response."""
    payload = msg.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    title = headers.get("subject") or "(no subject)"
    body = strip_boilerplate(_extract_body(payload).strip())
    metadata: dict[str, Any] = {
        "from": headers.get("from"),
        "to": headers.get("to"),
        "date": headers.get("date"),
        "message_id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "label_ids": msg.get("labelIds") or [],
    }
    # New typed-column feeders (P1.3). Each key is omitted when the source
    # header is absent so downstream column-promotion stays NULL rather than
    # storing empty strings.
    rfc_id = headers.get("message-id")
    if rfc_id:
        metadata["rfc_message_id"] = rfc_id
    in_reply_to = headers.get("in-reply-to")
    if in_reply_to:
        metadata["in_reply_to"] = in_reply_to
    sent_at = _parse_date_header_to_iso_utc(headers.get("date"))
    if sent_at is not None:
        metadata["sent_at"] = sent_at
    return ExtractedDoc(
        title=title,
        content=body,
        content_type="email",
        source_path=None,
        metadata=metadata,
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
