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
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from brain.config import BOILERPLATE_PATTERNS

# Re-using the shared Re/Fwd-prefix helper from vault.slug rather than
# duplicating the regex — it's marked private (leading underscore) but the
# strip rule is identical between the URL slug (``gmail_slug``) and the
# thread-doc title, and a divergent local copy would invite drift. Single
# source of truth wins over the soft visibility convention here.
from brain.vault.slug import _strip_re_fwd_prefixes

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


# ---------------------------------------------------------------------------
# Thread assembly (P2.1)
#
# A Gmail thread is N messages sharing the same ``threadId``. P2.1 collapses
# them into one ``ExtractedDoc`` so downstream search / vault export operates
# on a single conversational unit instead of N near-duplicate per-message
# rows. The function is pure: no DB, no I/O, no logging at INFO.
# ---------------------------------------------------------------------------

# Cap the assembled-thread title at 200 chars (after Re/Fwd strip) so a
# pathological subject line — e.g. an auto-mailer that crams a multi-line
# log into the subject — doesn't blow out the ``documents.title`` column or
# the wiki UI. Truncation is whole-word-aware; the URL slug uses a separate
# 64-char cap inside ``vault.slug.gmail_slug``.
_THREAD_TITLE_MAX = 200


def _message_sort_key(msg: dict[str, Any]) -> int:
    """Return a milliseconds-since-epoch sort key for a Gmail message.

    Prefer ``internalDate`` (Gmail's canonical receive timestamp, expressed
    as a string of ms-since-epoch). When ``internalDate`` is missing or
    unparseable, fall back to ``Date:`` header parsed via the standard
    library. Returns ``0`` when neither is parseable — the call site keeps
    such messages but their order is undefined; the spec only requires
    defensive handling, not stable tie-breaking.
    """
    raw = msg.get("internalDate")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw)
        except ValueError:
            pass

    payload = msg.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    date_header = headers.get("date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.astimezone(UTC).timestamp() * 1000)
    return 0


def _truncate_title_to_word(title: str, *, limit: int = _THREAD_TITLE_MAX) -> str:
    """Truncate ``title`` to ``limit`` chars at a word boundary, append ``…``.

    Returns the input unchanged when ``len(title) <= limit``. Otherwise:

    1. Take the first ``limit`` characters.
    2. Cut back to the last space within that window so we don't slice
       through a word. If no space exists (single very long token), keep
       the hard slice.
    3. ``rstrip`` trailing whitespace and append the U+2026 ellipsis.

    Final length is at most ``limit + 1`` (one ellipsis char appended).
    """
    if len(title) <= limit:
        return title
    chunk = title[:limit]
    last_space = chunk.rfind(" ")
    if last_space > 0:
        chunk = chunk[:last_space]
    return f"{chunk.rstrip()}…"


def _split_addresses(value: str | None) -> list[str]:
    """Split a comma-separated To/Cc header value into individual entries.

    Simple comma split — does NOT handle the rare RFC 5322 quoted-pair case
    (``"Last, First" <addr>``). Test fixtures and live Gmail traffic stay
    unquoted in practice; revisit with ``email.utils.getaddresses`` only if
    we hit a real-world false-split. Empty / whitespace-only entries are
    dropped.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _format_thread_section(msg: dict[str, Any], *, collapsed: bool) -> str:
    """Render one message as a Markdown section for the assembled thread body.

    The most recent message uses a plain ``## YYYY-MM-DD HH:MM — <from>``
    H2 (always expanded). Older messages wrap in ``<details><summary>``
    so they collapse by default — both Markdown processors and Quartz
    pass HTML through, and ``<details>`` is supported natively by every
    modern browser.

    Date format is ``YYYY-MM-DD HH:MM`` in UTC. The ``from`` value is the
    raw header (``"Name <email>"``) for fidelity. Each message body passes
    through :func:`strip_boilerplate` first.
    """
    payload = msg.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])
    raw_from = headers.get("from") or "(unknown sender)"

    iso_utc = _parse_date_header_to_iso_utc(headers.get("date"))
    if iso_utc is not None:
        date_label = datetime.fromisoformat(iso_utc).astimezone(UTC).strftime(
            "%Y-%m-%d %H:%M"
        )
    else:
        date_label = "(unknown date)"

    body = strip_boilerplate(_extract_body(payload).strip())
    heading = f"{date_label} — {raw_from}"

    if collapsed:
        # Blank lines around the body are required for markdown processors
        # (and Quartz) to render the inner content as markdown rather than
        # a single HTML block.
        return (
            f"<details>\n"
            f"<summary>{heading}</summary>\n"
            f"\n"
            f"{body}\n"
            f"\n"
            f"</details>"
        )
    return f"## {heading}\n\n{body}"


def to_extracted_thread(messages: list[dict[str, Any]]) -> ExtractedDoc:
    """Group N gmail messages from the same thread into one ExtractedDoc.

    Pure function — no DB writes, no file I/O, no logging at INFO level.
    Messages are sorted ascending by ``internalDate`` (defensive fallback to
    ``Date:`` header) and assembled into a single Markdown document, one
    H2 per message. The most recent message renders as a plain H2; older
    ones wrap in ``<details><summary>`` so they collapse by default.

    Title is the FIRST message's subject after stripping leading
    ``Re:`` / ``Fwd:`` / ``Fw:`` prefixes (case-insensitive, repeated).
    Empty subjects fall back to ``"(no subject)"``. Subjects longer than
    200 chars are word-boundary truncated with a trailing ``…``.

    Metadata aggregation — first-vs-latest is asymmetric on purpose:

    - ``thread_id`` is the FIRST message's ``threadId`` (stable across the
      whole thread; first-vs-last is a no-op in practice but the spec
      pins ``first`` so re-ingestion is deterministic if a future Gmail
      bug ever splits a thread mid-conversation).
    - ``rfc_message_id``, ``in_reply_to``, ``from``, ``to``, ``date``,
      ``sent_at`` come from the LATEST message — the thread doc tracks
      the most recent reply so an in-flight conversation surfaces with
      its newest state.
    - ``participants`` is the union of From + To + Cc across ALL
      messages, deduped case-insensitively, sorted case-insensitively for
      stability.
    - ``label_ids`` is the sorted union of ``labelIds`` across all
      messages — a thread inherits IMPORTANT / STARRED if any reply
      carries it.
    - ``message_count`` is N.

    Tags are not produced here — the per-message extractor doesn't tag
    either. Phase 2.4's destructive collapse will preserve tags by
    unioning across the source per-message rows; new threaded ingests
    pick tags up via ``brain tag <id> +foo`` post-ingest.

    Raises:
        ValueError: ``messages`` is empty.
    """
    if not messages:
        raise ValueError("to_extracted_thread requires at least one message")

    sorted_msgs = sorted(messages, key=_message_sort_key)
    first = sorted_msgs[0]
    latest = sorted_msgs[-1]

    first_headers = _headers_to_dict(
        (first.get("payload") or {}).get("headers") or []
    )
    latest_headers = _headers_to_dict(
        (latest.get("payload") or {}).get("headers") or []
    )

    raw_subject = first_headers.get("subject") or ""
    stripped_subject = _strip_re_fwd_prefixes(raw_subject).strip()
    title = stripped_subject or "(no subject)"
    title = _truncate_title_to_word(title)

    last_idx = len(sorted_msgs) - 1
    sections = [
        _format_thread_section(msg, collapsed=(idx != last_idx))
        for idx, msg in enumerate(sorted_msgs)
    ]
    body = "\n\n".join(sections)

    # Participants: From + To + Cc across every message, deduped case-
    # insensitively (first-seen form wins so a "Alice <a@x.com>" sighting
    # is preferred over a later "ALICE <a@x.com>"), then sorted case-
    # insensitively for stability across re-ingests.
    seen: dict[str, str] = {}
    for msg in sorted_msgs:
        headers = _headers_to_dict((msg.get("payload") or {}).get("headers") or [])
        candidates: list[str] = []
        from_hdr = headers.get("from")
        if from_hdr:
            candidates.append(from_hdr)
        candidates.extend(_split_addresses(headers.get("to")))
        candidates.extend(_split_addresses(headers.get("cc")))
        for addr in candidates:
            key = addr.casefold()
            if key not in seen:
                seen[key] = addr
    participants = sorted(seen.values(), key=str.casefold)

    label_ids: set[str] = set()
    for msg in sorted_msgs:
        label_ids.update(msg.get("labelIds") or [])

    metadata: dict[str, Any] = {
        "thread_id": first.get("threadId"),
        "from": latest_headers.get("from"),
        "to": latest_headers.get("to"),
        "date": latest_headers.get("date"),
        "label_ids": sorted(label_ids),
        "participants": participants,
        "message_count": len(sorted_msgs),
    }
    rfc_id = latest_headers.get("message-id")
    if rfc_id:
        metadata["rfc_message_id"] = rfc_id
    in_reply_to = latest_headers.get("in-reply-to")
    if in_reply_to:
        metadata["in_reply_to"] = in_reply_to
    sent_at = _parse_date_header_to_iso_utc(latest_headers.get("date"))
    if sent_at is not None:
        metadata["sent_at"] = sent_at

    return ExtractedDoc(
        title=title,
        content=body,
        content_type="email_thread",
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
