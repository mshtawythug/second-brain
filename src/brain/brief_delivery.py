"""Send the published daily brief to its owner: email through ``gws``, or a Slack webhook (RM-16).

:func:`brain.brief_publish.build_brief` calls :func:`deliver_brief` with the payload it has just
written to the vault page, the GATED one (``exclude_confidential=True``), never the terminal one:
the message leaves the machine (spec §2.5). By default it carries titles and counts only. Todo
text, and the suggestions the model derives from it, are body-derived and are sent only with
``BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS=true``.

**Failure never fails the brief.** Every channel turns its failures into :class:`DeliveryError`
with a fixed message and a content-free ``log_detail``; :func:`deliver_brief` logs ONE class-only
WARNING and returns a ``failed`` outcome. Nothing here logs or raises the message body, ``gws``'s
stderr, or the webhook URL.

**The webhook URL is a secret, and httpx would log it.** ``httpx.Client`` logs every request at
INFO with its full URL. :class:`SlackWebhookChannel` therefore calls the transport's
``handle_request`` directly, which no httpx logger sees, and carries the timeout on the request's
``timeout`` extension (a transport has no default timeout of its own). A transport also never
follows a redirect. The timeout does not cover the DNS lookup (``getaddrinfo``), which the OS
resolver bounds (plan, Deviations).

**Drafts never leave** (sec-82 1): the published payload still lists draft documents on the local
page, so :func:`without_drafts` removes them from what is sent (plan D13).

**The email body is in ``gws``'s argv** (visible to a local ``ps`` for at most the 30 s timeout):
``gws gmail +send`` takes the body only as ``--body`` (plan, Deviations).

Plan: ``docs/plans/2026-10-30-rm-16-brief-delivery.md``.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import httpx

from .brief import BriefData, watch_section_lines

# Fixed, so a mail filter can label it and ``ingest-gmail`` can skip the owner's copy.
from .brief_mail import SUBJECT_PREFIX
from .errors import BrainError, describe_failure
from .settings.brief_delivery import (
    CHANNEL_EMAIL,
    CHANNEL_SLACK,
    BriefDeliverySettings,
)

_logger = logging.getLogger(__name__)

#: The ticket's timeouts: one ``gws`` send, one webhook POST.
EMAIL_TIMEOUT_S = 30.0
SLACK_TIMEOUT_S = 10.0


#: Markers of a ``gws`` authorization failure in its stderr (the cases ``bin/gws-reauth`` fixes).
#: The stderr is read only to classify; it is never kept, logged or raised.
_GWS_AUTH_FAILURE = re.compile(
    r"invalid_grant|invalid_rapt|token_error|insufficient authentication scopes"
    r"|unauthenticated|unauthorized|\b401\b|\b403\b",
    re.IGNORECASE,
)
#: A run of control characters or whitespace inside one title: collapsed so it cannot forge a line.
_LINE_BREAKERS = re.compile(r"[\x00-\x1f\x7f\s]+")

#: Runs one ``gws`` command and returns its stdout; raises :class:`DeliveryError`.
GwsRunner = Callable[[list[str]], str]


class DeliveryError(BrainError):
    """A channel could not deliver. The message is fixed text; ``log_detail`` is content-free."""

    def __init__(self, message: str = "brief delivery failed", *, log_detail: str = "") -> None:
        super().__init__(message)
        self.log_detail = log_detail


class DeliveryAuthError(DeliveryError):
    """``gws`` is not authorized to send mail. ``bin/gws-reauth`` is the fix."""

    def __init__(self) -> None:
        super().__init__("gws is not authorized to send", log_detail="authorization failed")


class DeliveryChannel(Protocol):
    """Where a rendered brief goes. ``send`` raises :class:`DeliveryError` and nothing else."""

    name: str

    def send(self, *, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class DeliveryOutcome:
    """What :func:`deliver_brief` did. ``failure`` is the class-only description, if it failed."""

    status: Literal["off", "sent", "failed"]
    failure: str | None = None


def run_gws(
    argv: list[str],
    *,
    timeout_s: float = EMAIL_TIMEOUT_S,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Run one ``gws`` command; return its stdout, or raise :class:`DeliveryError`.

    The argv holds the message body, so no message here quotes it, and a timeout's own text
    (which does) is only the cause. ``which`` is the test seam for the binary's path.
    """
    executable = which(argv[0])
    if executable is None:
        raise DeliveryError("gws CLI not found", log_detail="gws CLI not found on PATH")
    try:
        proc = subprocess.run(
            [executable, *argv[1:]],
            capture_output=True,
            text=True,
            errors="replace",  # aud-82 4: gws output that is not UTF-8 is not a crash
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(
            "gws timed out", log_detail=f"timed out after {timeout_s:g}s"
        ) from exc
    except OSError as exc:
        raise DeliveryError("could not run gws", log_detail="could not run gws") from exc
    if proc.returncode != 0:
        if _GWS_AUTH_FAILURE.search(proc.stderr or ""):
            raise DeliveryAuthError()
        raise DeliveryError("gws send failed", log_detail=f"exit {proc.returncode}")
    return proc.stdout


class GwsEmailChannel:
    """Email to the owner through ``gws gmail +send``. ``recipient`` is ``BRAIN_USER_EMAIL``."""

    name = CHANNEL_EMAIL

    def __init__(self, *, recipient: str, runner: GwsRunner = run_gws) -> None:
        self._recipient = recipient
        self._runner = runner

    def __repr__(self) -> str:
        return "GwsEmailChannel()"

    def send(self, *, subject: str, body: str) -> None:
        # Each value glued to its flag (sec-82 3): no value can be read as an option.
        self._runner(
            [
                "gws", "gmail", "+send",
                f"--to={self._recipient}",
                f"--subject={subject}",
                f"--body={body}",
            ]
        )


class SlackWebhookChannel:
    """A POST of ``{"text": body}`` to an incoming webhook, through the transport (module doc)."""

    name = CHANNEL_SLACK

    def __init__(
        self,
        webhook_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = SLACK_TIMEOUT_S,
    ) -> None:
        self._url = webhook_url
        self._transport = transport
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return "SlackWebhookChannel(<webhook hidden>)"

    def send(self, *, subject: str, body: str) -> None:
        # Everything that can raise is inside the guard (rev-82 H2): ``httpx.Request`` raises
        # ``InvalidURL`` (not an ``HTTPError``) for a control character, and building the real
        # transport raises an ``OSError`` for a bad ``SSL_CERT_FILE``.
        built: httpx.BaseTransport | None = None
        try:
            request = httpx.Request(
                "POST",
                self._url,
                json={"text": slack_escape(body), "unfurl_links": False, "unfurl_media": False},
                extensions={"timeout": httpx.Timeout(self._timeout_s).as_dict()},
            )
            transport = self._transport
            if transport is None:
                built = transport = httpx.HTTPTransport()
            response = transport.handle_request(request)
            try:
                response.read()
            finally:
                response.close()
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            raise DeliveryError(
                "Slack webhook request failed", log_detail=type(exc).__name__
            ) from exc
        finally:
            if built is not None:
                built.close()
        if not response.is_success:
            raise DeliveryError(
                "Slack webhook refused the message", log_detail=f"HTTP {response.status_code}"
            )


def make_channel(
    settings: BriefDeliverySettings,
    *,
    runner: GwsRunner = run_gws,
    transport: httpx.BaseTransport | None = None,
) -> DeliveryChannel | None:
    """The configured channel, or ``None`` when delivery is off.

    ``runner`` and ``transport`` are the test seams; production passes neither.
    """
    if settings.channel == CHANNEL_EMAIL and settings.recipient is not None:
        return GwsEmailChannel(recipient=settings.recipient, runner=runner)
    if settings.channel == CHANNEL_SLACK and settings.slack_webhook_url is not None:
        return SlackWebhookChannel(settings.slack_webhook_url, transport=transport)
    return None


def without_drafts(brief: BriefData, draft_ids: Collection[str]) -> BriefData:
    """``brief`` without any capture, pin, todo or watch hit from a draft document (plan D13).

    The suggestions were derived from every todo and cannot be traced to one, so when a draft's
    todo is removed they go too; re-deriving them would put an LLM call on the delivery path.
    """
    if not draft_ids:
        return brief
    todos = [row for row in brief.open_todos if row.document_id not in draft_ids]
    return replace(
        brief,
        captures=[doc for doc in brief.captures if doc.id not in draft_ids],
        pinned=[pin for pin in brief.pinned if pin.document_id not in draft_ids],
        open_todos=todos,
        watch_hits=[hit for hit in brief.watch_hits if hit.document_id not in draft_ids],
        suggestions=brief.suggestions if len(todos) == len(brief.open_todos) else [],
    )


def slack_escape(text: str) -> str:
    """Escape Slack's three control characters, so a title cannot become a link or a mention."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(text: str) -> str:
    return _LINE_BREAKERS.sub(" ", text).strip()


def render_delivery(brief: BriefData, *, include_todos: bool) -> tuple[str, str]:
    """``(subject, body)`` for ``brief``, the published payload, as plain text.

    Titles and counts by default; todo text and suggestions only with ``include_todos``. The
    watch section is the page's (:func:`brain.brief.watch_section_lines`) without the fetch-limit
    line, which the page never carries either (RM-15 D14).
    """
    date_text = brief.date.isoformat()
    lines = [f"Brain brief · {date_text}", "", f"Recent captures ({len(brief.captures)})"]
    lines += [f"- [{doc.source_kind or 'manual'}] {_one_line(doc.title)}" for doc in brief.captures]
    lines += ["", f"Pinned ({len(brief.pinned)})"]
    lines += [f"- {_one_line(pin.title)}" for pin in brief.pinned]
    watch_lines = watch_section_lines(
        replace(brief, watch_notes=[replace(note, limit=None) for note in brief.watch_notes])
    )
    if watch_lines:
        lines += ["", "Watch hits", *[f"- {_one_line(line)}" for line in watch_lines]]
    lines += ["", f"Open action items: {len(brief.open_todos)}"]
    if include_todos:
        lines += [f"- [ ] {_one_line(row.text)}" for row in brief.open_todos]
        if brief.suggestions:
            lines += ["", "Suggested next steps"]
            lines += [f"{i}. {_one_line(s)}" for i, s in enumerate(brief.suggestions, 1)]
    lines += ["", "Open `brain brief` for detail."]
    return f"{SUBJECT_PREFIX} {date_text}", "\n".join(lines) + "\n"


def deliver_brief(
    brief: BriefData,
    settings: BriefDeliverySettings,
    channel: DeliveryChannel | None,
) -> DeliveryOutcome:
    """Send ``brief`` (the published payload) through ``channel``, or the configured one.

    Off: nothing is sent and nothing is logged. A :class:`DeliveryError` is one WARNING, never a
    raise; an authorization failure's names ``bin/gws-reauth``.
    """
    if not settings.enabled:
        return DeliveryOutcome("off")
    target = channel if channel is not None else make_channel(settings)
    if target is None:
        return DeliveryOutcome("off")
    subject, body = render_delivery(brief, include_todos=settings.include_todos)
    try:
        target.send(subject=subject, body=body)
    except DeliveryError as exc:
        failure = describe_failure(exc)
        if isinstance(exc, DeliveryAuthError):
            _logger.warning(
                "brief delivery (%s) failed (%s): gws cannot send mail; run bin/gws-reauth",
                settings.channel,
                failure,
            )
        else:
            _logger.warning(
                "brief delivery (%s) failed (%s); the page is written", settings.channel, failure
            )
        return DeliveryOutcome("failed", failure)
    _logger.info("brief delivered (%s)", settings.channel)
    return DeliveryOutcome("sent")
