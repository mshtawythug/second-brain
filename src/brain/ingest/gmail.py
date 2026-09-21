"""Gmail ingester — shells out to the `gws` CLI.

Uses the real ``gws gmail users messages list/get`` surface. Each operation
accepts a JSON-encoded ``--params`` blob that mirrors the underlying Gmail
REST API. Message bodies returned by the ``get`` call are base64url-encoded
and may live either on ``payload.body.data`` or inside ``payload.parts[]``
(multi-part messages) — this module normalises both shapes into plain text.
"""
import base64
import html
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
    """Compose the Gmail search ``q`` string from the CLI scope flags.

    Drafts are now **included** by default (wave Q1-A, 2026-05-11). The
    per-message extractor (:func:`to_extracted_doc`) and per-thread
    extractor (:func:`to_extracted_thread`) stamp
    ``metadata["_is_draft"] = True`` on all-draft documents so the ingest
    pipeline can set ``documents.draft = TRUE``. The P1.6 wiki quarantine
    (``contentIndex.ts:397``) hides those rows from the Quartz build;
    ``brain search`` / ``brain show`` still surface them so "what was I
    going to email person-x about?" is answerable.
    """
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


def _is_draft(msg: dict[str, Any]) -> bool:
    """Return True when the Gmail Message resource carries the ``DRAFT`` label.

    Gmail's draft state is communicated via ``labelIds`` on the Message
    resource. Drafts are unsent — the user typed them but never sent —
    and ingesting them pollutes the searchable corpus. Ingest paths use
    this helper to short-circuit on drafts before doing any work.
    """
    label_ids = msg.get("labelIds") or []
    return "DRAFT" in label_ids


def to_extracted_doc(msg: dict[str, Any]) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` from a Gmail ``users.messages.get`` response.

    Draft messages (``labelIds`` containing ``DRAFT``) are now included
    rather than rejected. ``metadata["_is_draft"]`` (leading underscore
    signals a derived/internal key, mirroring ``_participant_keys``) is
    set to ``True`` so the ingest pipeline can stamp
    ``documents.draft = TRUE`` and the P1.6 wiki quarantine hides the
    doc from Quartz while ``brain search`` / ``brain show`` still
    surface it.
    """
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
        "_is_draft": _is_draft(msg),
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
    header as sent (``"Name <email>"``) for fidelity, HTML-escaped on BOTH
    paths — see the comment on ``escaped_heading`` for why the H2 is not the
    exception it used to be. Each message body passes through
    :func:`strip_boilerplate` first.
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
    # ONE ESCAPE, BOTH HEADINGS — and the "both" is defect #57's fix.
    #
    # Gmail headers routinely carry the From in `Name <email@addr>` form.
    #
    # For the ``<summary>`` (P4.4): markdown processors treat a raw HTML block
    # like ``<details>...</details>`` as opaque pass-through, so the BROWSER
    # parses the inner ``<summary>Name <email@addr></summary>`` and treats
    # ``<email@addr>`` as an unknown tag, silently dropping it from the rendered
    # text. That also broke the "Show only my replies" filter, which reads
    # ``summary.textContent`` and matches it against the owner's address.
    #
    # For the ``## H2``: this used to be emitted RAW, on the stated grounds that
    # "markdown auto-linking handles the angle-bracketed email there". Measured
    # through ``brain.ui.render.render_markdown``, that is not what autolinking
    # does — it REPLACES the address with ``<a href="mailto:…">addr</a>`` and
    # the brackets are gone. So a thread in which the same person sent both an
    # older and the newest message rendered that one address two ways in one
    # document: bracketed text in the summary, a bare mailto anchor in the H2.
    # Under a raw-HTML-passthrough renderer the same rawness is an injection
    # seam rather than a cosmetic one, which is why the raw path is the defect
    # and the escaped path is not "also broken".
    #
    # Hoisted above the branch rather than duplicated inside it: two call sites
    # applying the same escape is exactly how the two drifted apart in the first
    # place.
    #
    # ``quote=False`` because neither heading is ever placed inside a
    # quote-delimited attribute — ``<summary>`` and ``## `` are both content
    # positions.
    #
    # The anchor id ``brain.ui.render`` mints is UNCHANGED by this, measured on
    # both forms: the slugger drops the punctuation the two spellings differ in,
    # so the TOC keeps pointing at the same id (defect S4 stays fixed).
    #
    # NEW THREADS ONLY, UNTIL SOMEONE SWEEPS. This runs at INGEST time and
    # rewrites ``documents.content``; it does not reach a row already stored.
    # Every thread ingested before this change keeps the raw ``Name <addr>``
    # heading, so on that part of the corpus the H2 and the ``<summary>`` stay
    # spelled two different ways and "Show only my replies" keeps missing the
    # H2 — exactly the inconsistency the escape exists to remove.
    #
    # THE SWEEP IS A PLAIN RE-PULL — there is no ``--force`` on
    # ``brain ingest-gmail`` and none is needed. Gmail threads take the
    # "sourced stdin" path in ``ingest_document``: the doc is resolved by
    # ``(source_kind, source_external_id)`` = ``('gmail', thread_id)``, and the
    # escape changes the body, so the recomputed ``content_hash`` differs and
    # the existing row is UPDATEd in place (``body_changed=True``) rather than
    # skipped or duplicated. Re-pull the same scope the threads came from, e.g.
    # ``brain ingest-gmail --label <label> --since <date> --max <n>``.
    #
    # SIZE OF THE UN-SWEPT SET — cited, NOT re-derived: the prod container was
    # down at 2026-08-20 21:46 EDT, so this is ``docs/audits/
    # 2026-08-13-phase2-recon.md`` as of 2026-08-13, and it counts the
    # ``<details>``/``<summary>`` extension rather than the H2 directly: 58
    # documents, of which 57 are ``email_thread`` (89.1% of the 64 there) and 1
    # is ``markdown``. Treat that as a floor for the H2, not a measurement of
    # it — the H2 is emitted for the newest message of every multi-message
    # thread, whether or not the older ones collapsed. Re-derive before acting:
    #
    #   SELECT content_type,
    #          count(*) FILTER (WHERE content ~ '(?m)^## [^<]*<[^ >]+@') AS raw,
    #          count(*) FILTER (WHERE content ~ '(?m)^## .*&lt;')        AS escaped
    #     FROM documents GROUP BY 1;
    escaped_heading = html.escape(heading, quote=False)

    if collapsed:
        # Blank lines around the body are required for markdown processors
        # (and Quartz) to render the inner content as markdown rather than
        # a single HTML block.
        return (
            f"<details>\n"
            f"<summary>{escaped_heading}</summary>\n"
            f"\n"
            f"{body}\n"
            f"\n"
            f"</details>"
        )
    return f"## {escaped_heading}\n\n{body}"


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

    Draft handling is asymmetric on purpose:

    - **All-draft thread**: every message carries the ``DRAFT`` label →
      the full thread is assembled intact and
      ``metadata["_is_draft"] = True`` is set. The ingest pipeline stamps
      ``documents.draft = TRUE``; the P1.6 wiki quarantine hides the doc
      from Quartz while ``brain search`` can still find it ("what was I
      going to email person-x about?").
    - **Mixed thread**: at least one sent message → drafts are dropped
      from the rendered body (body, participants, label_ids, message_count
      all reflect the post-filter sent set) and
      ``metadata["_is_draft"] = False`` is set. This prevents a WIP
      unsent reply from appearing as the visible H2 in a published thread.
    - **Empty input**: raises ``ValueError`` (programmer error — callers
      must supply at least one message).

    Raises:
        ValueError: ``messages`` is empty.
    """
    if not messages:
        raise ValueError("to_extracted_thread requires at least one message")

    all_drafts = all(_is_draft(m) for m in messages)
    if all_drafts:
        # All-draft thread: assemble the full list; the wiki quarantine
        # hides the resulting doc; brain search still surfaces it.
        sorted_msgs = sorted(messages, key=_message_sort_key)
    else:
        # Mixed or fully-sent thread: drop drafts from the rendered body
        # so a WIP unsent reply doesn't appear as the visible H2.
        sorted_msgs = sorted(
            [m for m in messages if not _is_draft(m)], key=_message_sort_key
        )
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
        # Internal flag for the ingest pipeline: True when every message
        # in the original (unfiltered) input was a DRAFT. The pipeline
        # uses this to stamp documents.draft = TRUE and route the doc
        # through the P1.6 wiki quarantine. Leading underscore mirrors
        # the ``_participant_keys`` convention for derived fields.
        "_is_draft": all_drafts,
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
