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
from typing import Any

from . import ExtractedDoc


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


def to_extracted_doc(msg: dict[str, Any]) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` from a Gmail ``users.messages.get`` response."""
    payload = msg.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    title = headers.get("subject") or "(no subject)"
    body = _extract_body(payload).strip()
    return ExtractedDoc(
        title=title,
        content=body,
        content_type="email",
        source_path=None,
        metadata={
            "from": headers.get("from"),
            "to": headers.get("to"),
            "date": headers.get("date"),
            "message_id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "label_ids": msg.get("labelIds") or [],
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
