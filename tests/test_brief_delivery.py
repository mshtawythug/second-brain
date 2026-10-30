"""``brain.brief_delivery``: rendering, the two channels and ``deliver_brief`` (RM-16).

No test here sends anything: the email channel gets an injected runner or a fake ``gws`` script
on a tmp path, and the Slack channel an ``httpx.MockTransport``. Addresses, titles and the webhook
URL are synthetic.
"""
from __future__ import annotations

import json
import logging
import stat
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from brain.brief import BriefData, PinnedDoc
from brain.brief_delivery import (
    EMAIL_TIMEOUT_S,
    SLACK_TIMEOUT_S,
    DeliveryAuthError,
    DeliveryError,
    GwsEmailChannel,
    SlackWebhookChannel,
    deliver_brief,
    make_channel,
    render_delivery,
    run_gws,
    without_drafts,
)
from brain.queries import DocumentRow
from brain.saved_search import WatchHit, WatchNote
from brain.settings.brief_delivery import BriefDeliverySettings
from brain.todo import TodoRow

OWNER = "owner@example.invalid"
#: The secret part of the synthetic webhook, searched for in every log record.
SECRET_PATH = "T0SENTINEL/B0SENTINEL/sentinel-webhook-secret-42"
WEBHOOK = f"https://hooks.slack.com/services/{SECRET_PATH}"
ON = date(2026, 6, 9)
AT = datetime(2026, 6, 9, 7, 0, tzinfo=UTC)


def _brief(**overrides: object) -> BriefData:
    fields: dict[str, object] = {
        "date": ON,
        "captures": [
            DocumentRow(
                id="d1", title="Capture alpha", content_type="note", tags=[],
                source_kind="krisp", ingested_at=AT,
            ),
            DocumentRow(
                id="d2", title="Capture beta", content_type="note", tags=[],
                source_kind=None, ingested_at=AT,
            ),
        ],
        "open_todos": [
            TodoRow(
                document_id="d1", document_title="Capture alpha", ingested_at=AT,
                state="open", text="todo body text one",
            ),
        ],
        "pinned": [PinnedDoc(document_id="d3", title="Pinned gamma", pinned_at=AT)],
        "suggestions": ["suggestion derived from todo text"],
        "watch_hits": [
            WatchHit(
                watch="rollout", document_id="d4", title="Watched delta",
                source_kind="slack", first_seen_at=AT, notified=False,
            )
        ],
        "watch_notes": [WatchNote(watch="rollout", more=2)],
    }
    fields.update(overrides)
    return BriefData(**fields)  # type: ignore[arg-type]


def _settings(channel: str = "email", *, include_todos: bool = False) -> BriefDeliverySettings:
    return BriefDeliverySettings(
        channel=channel,
        include_todos=include_todos,
        recipient=OWNER,
        slack_webhook_url=WEBHOOK,
    )


class _FakeChannel:
    name = "fake"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def send(self, *, subject: str, body: str) -> None:
        self.sent.append((subject, body))
        if self.error is not None:
            raise self.error


# --- render_delivery -------------------------------------------------------------------------


def test_the_subject_is_fixed_and_dated() -> None:
    subject, _ = render_delivery(_brief(), include_todos=False)

    assert subject == "[brain brief] 2026-06-09"


def test_the_body_carries_titles_counts_and_the_watch_section() -> None:
    _, body = render_delivery(_brief(), include_todos=False)

    assert "- [krisp] Capture alpha" in body
    assert "- [manual] Capture beta" in body
    assert "Recent captures (2)" in body
    assert "- Pinned gamma" in body
    assert "- [rollout] Watched delta" in body
    assert "[rollout] and 2 more" in body
    assert "Open action items: 1" in body
    assert body.rstrip().endswith("Open `brain brief` for detail.")


def test_todo_text_and_suggestions_are_not_sent_by_default() -> None:
    _, body = render_delivery(_brief(), include_todos=False)

    assert "todo body text one" not in body
    assert "suggestion derived from todo text" not in body


def test_todo_text_and_suggestions_are_sent_with_the_opt_in() -> None:
    _, body = render_delivery(_brief(), include_todos=True)

    assert "- [ ] todo body text one" in body
    assert "1. suggestion derived from todo text" in body


def test_a_title_cannot_forge_a_line() -> None:
    brief = _brief(
        pinned=[PinnedDoc(document_id="d9", title="evil\r\n- [krisp] forged\tline", pinned_at=AT)]
    )

    _, body = render_delivery(brief, include_todos=False)

    assert "- evil - [krisp] forged line" in body
    assert "\n- [krisp] forged" not in body


def test_the_fetch_limit_line_is_never_sent() -> None:
    # The page never carries it (the F6 1-bit oracle, RM-15 D14), so neither does delivery.
    brief = _brief(watch_notes=[WatchNote(watch="rollout", limit="matched more than 50")])

    _, body = render_delivery(brief, include_todos=False)

    assert "matched more than 50" not in body


def test_an_empty_brief_still_renders() -> None:
    _, body = render_delivery(
        _brief(captures=[], open_todos=[], pinned=[], suggestions=[], watch_hits=[],
               watch_notes=[]),
        include_todos=True,
    )

    assert "Recent captures (0)" in body
    assert "Watch hits" not in body
    assert "Suggested next steps" not in body


# --- the email channel -----------------------------------------------------------------------


def test_email_channel_runs_one_gws_send_to_the_owner() -> None:
    calls: list[list[str]] = []
    channel = GwsEmailChannel(recipient=OWNER, runner=lambda argv: calls.append(argv) or "")

    channel.send(subject="[brain brief] 2026-06-09", body="hello")

    # sec-82 3: each value is glued to its flag, so none can be read as an option.
    assert calls == [
        [
            "gws", "gmail", "+send",
            f"--to={OWNER}",
            "--subject=[brain brief] 2026-06-09",
            "--body=hello",
        ]
    ]


def _fake_gws(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "gws"
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_run_gws_returns_stdout_and_passes_argv_untouched(tmp_path: Path) -> None:
    record = tmp_path / "argv"
    gws = _fake_gws(tmp_path, f'printf "%s\\n" "$@" > "{record}"; echo sent')

    out = run_gws(["gws", "gmail", "+send", "--body", "line one\nline two"],
                  which=lambda _name: str(gws))

    assert out.strip() == "sent"
    assert record.read_text(encoding="utf-8") == "gmail\n+send\n--body\nline one\nline two\n"


def test_run_gws_maps_a_non_zero_exit_without_stderr(tmp_path: Path) -> None:
    gws = _fake_gws(tmp_path, 'echo "quota detail with a body fragment" >&2; exit 3')

    with pytest.raises(DeliveryError) as raised:
        run_gws(["gws", "gmail", "+send"], which=lambda _name: str(gws))

    assert not isinstance(raised.value, DeliveryAuthError)
    assert raised.value.log_detail == "exit 3"
    assert "body fragment" not in str(raised.value)


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: 403 Request had insufficient authentication scopes.",
        "token_error: invalid_rapt",
        "oauth: invalid_grant",
        "HTTP 401 Unauthorized",
    ],
)
def test_run_gws_classifies_an_authorization_failure(tmp_path: Path, stderr: str) -> None:
    gws = _fake_gws(tmp_path, f'echo "{stderr}" >&2; exit 1')

    with pytest.raises(DeliveryAuthError) as raised:
        run_gws(["gws", "gmail", "+send"], which=lambda _name: str(gws))

    assert stderr not in str(raised.value)


def test_run_gws_maps_a_timeout(tmp_path: Path) -> None:
    gws = _fake_gws(tmp_path, "sleep 5")

    with pytest.raises(DeliveryError) as raised:
        run_gws(["gws", "gmail", "+send", "--body", "secret body"],
                which=lambda _name: str(gws), timeout_s=0.2)

    assert raised.value.log_detail == "timed out after 0.2s"
    assert "secret body" not in str(raised.value)


def test_run_gws_maps_a_missing_binary() -> None:
    with pytest.raises(DeliveryError) as raised:
        run_gws(["gws", "gmail", "+send"], which=lambda _name: None)

    assert raised.value.log_detail == "gws CLI not found on PATH"


def test_the_email_timeout_is_the_tickets() -> None:
    assert EMAIL_TIMEOUT_S == 30.0
    assert SLACK_TIMEOUT_S == 10.0


# --- the Slack channel -----------------------------------------------------------------------


def _mock(status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text="ok" if status == 200 else "no_service")

    return httpx.MockTransport(handler)


def test_slack_channel_posts_once() -> None:
    seen: list[httpx.Request] = []
    channel = SlackWebhookChannel(WEBHOOK, transport=_mock(seen=seen))

    channel.send(subject="[brain brief] 2026-06-09", body="a <b> & c")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == WEBHOOK
    assert json.loads(request.content) == {
        "text": "a &lt;b&gt; &amp; c",
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert request.extensions["timeout"] == httpx.Timeout(SLACK_TIMEOUT_S).as_dict()


def test_slack_channel_maps_a_rejection() -> None:
    channel = SlackWebhookChannel(WEBHOOK, transport=_mock(status=404))

    with pytest.raises(DeliveryError) as raised:
        channel.send(subject="s", body="b")

    assert raised.value.log_detail == "HTTP 404"


def test_slack_channel_maps_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    channel = SlackWebhookChannel(WEBHOOK, transport=httpx.MockTransport(handler))

    with pytest.raises(DeliveryError) as raised:
        channel.send(subject="s", body="b")

    assert SECRET_PATH not in str(raised.value)
    assert raised.value.log_detail == "ConnectError"


def test_make_channel_builds_the_configured_channel() -> None:
    assert isinstance(make_channel(_settings("email")), GwsEmailChannel)
    assert isinstance(make_channel(_settings("slack")), SlackWebhookChannel)
    assert make_channel(_settings("none")) is None


# --- deliver_brief ---------------------------------------------------------------------------


def test_delivery_off_sends_nothing_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    channel = _FakeChannel()

    outcome = deliver_brief(_brief(), _settings("none"), channel)

    assert outcome.status == "off"
    assert channel.sent == []
    assert caplog.records == []


def test_deliver_brief_sends_the_rendered_brief(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    channel = _FakeChannel()

    outcome = deliver_brief(_brief(), _settings("email"), channel)

    assert outcome.status == "sent"
    assert channel.sent == [render_delivery(_brief(), include_todos=False)]
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_failing_channel_logs_one_class_only_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error = DeliveryError("gws send failed: body fragment", log_detail="exit 2")

    outcome = deliver_brief(_brief(), _settings("email"), _FakeChannel(error))

    assert outcome.status == "failed"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "DeliveryError[exit 2]" in warnings[0].getMessage()
    assert "body fragment" not in caplog.text


def test_an_auth_failure_warning_names_gws_reauth(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    deliver_brief(_brief(), _settings("email"), _FakeChannel(DeliveryAuthError()))

    assert len(caplog.records) == 1
    assert "bin/gws-reauth" in caplog.records[0].getMessage()


def test_webhook_url_never_reaches_a_log_record_or_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    outcomes = [
        deliver_brief(_brief(), _settings("slack"), SlackWebhookChannel(WEBHOOK, transport=t))
        for t in (_mock(), _mock(status=500))
    ]

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out reading {request.url}", request=request)

    outcomes.append(
        deliver_brief(
            _brief(), _settings("slack"),
            SlackWebhookChannel(WEBHOOK, transport=httpx.MockTransport(boom)),
        )
    )

    assert [o.status for o in outcomes] == ["sent", "failed", "failed"]
    assert SECRET_PATH not in caplog.text
    assert all(SECRET_PATH not in repr(o) for o in outcomes)
    assert SECRET_PATH not in repr(_settings("slack"))
    assert SECRET_PATH not in repr(SlackWebhookChannel(WEBHOOK, transport=_mock()))


def test_the_sentinel_check_can_fail(caplog: pytest.LogCaptureFixture) -> None:
    # The hazard the Slack channel avoids is real, and the capture above would see it:
    # ``httpx.Client`` logs the full URL at INFO.
    caplog.set_level(logging.DEBUG)

    with httpx.Client(transport=_mock()) as client:
        client.post(WEBHOOK, json={"text": "x"})

    assert SECRET_PATH in caplog.text


# --- the production paths: a real subprocess error, a real transport to a loopback stub --------


def test_run_gws_maps_an_os_error(tmp_path: Path) -> None:
    not_executable = tmp_path / "gws"
    not_executable.write_text("not a program", encoding="utf-8")

    with pytest.raises(DeliveryError) as raised:
        run_gws(["gws", "gmail", "+send"], which=lambda _name: str(not_executable))

    assert raised.value.log_detail == "could not run gws"


def test_channel_reprs_hide_the_recipient() -> None:
    assert OWNER not in repr(GwsEmailChannel(recipient=OWNER))


def test_an_enabled_setting_without_a_channel_sends_nothing() -> None:
    settings = BriefDeliverySettings(channel="email", include_todos=False, recipient=None)

    assert deliver_brief(_brief(), settings, None).status == "off"


@pytest.fixture
def loopback_hook() -> Iterator[tuple[str, list[bytes]]]:
    """A one-route HTTP server on 127.0.0.1 standing in for the webhook host."""
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/services/{SECRET_PATH}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_real_transport_posts_and_logs_no_url(
    loopback_hook: tuple[str, list[bytes]], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    url, received = loopback_hook

    SlackWebhookChannel(url).send(subject="s", body="hello")

    assert [json.loads(body)["text"] for body in received] == ["hello"]
    assert SECRET_PATH not in caplog.text


# --- review round 1 (rev-82 / aud-82 / sec-82) ------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com/services/T0/B0/x\x7fy",
        "https://hooks.slack.com/services/T0/B0/x?a=\x01",
    ],
)
def test_a_url_httpx_refuses_is_a_failed_delivery_not_a_raise(
    caplog: pytest.LogCaptureFixture, url: str
) -> None:
    # rev-82 H2: httpx.InvalidURL is not an httpx.HTTPError.
    caplog.set_level(logging.DEBUG)
    channel = SlackWebhookChannel(url, transport=_mock())

    outcome = deliver_brief(_brief(), _settings("slack"), channel)

    assert outcome.status == "failed"
    assert outcome.failure is not None and "InvalidURL" in outcome.failure
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "T0" not in caplog.text


def test_a_bad_ssl_cert_file_is_a_failed_delivery_not_a_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    # aud-82 1 / sec-82 2: building the real transport reads SSL_CERT_FILE (an OSError).
    # Loopback, so even a transport that did build would reach nothing off the machine.
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    channel = SlackWebhookChannel(f"https://127.0.0.1:9/services/{SECRET_PATH}")

    outcome = deliver_brief(_brief(), _settings("slack"), channel)

    assert outcome.status == "failed"
    assert len(caplog.records) == 1
    assert "FileNotFoundError" in caplog.records[0].getMessage()
    assert SECRET_PATH not in caplog.text


def test_run_gws_survives_output_that_is_not_utf8(tmp_path: Path) -> None:
    # aud-82 4: a decode error must not escape run_gws.
    gws = _fake_gws(tmp_path, "printf '\\377\\376 not utf-8'")

    out = run_gws(["gws", "gmail", "+send"], which=lambda _name: str(gws))

    assert "not utf-8" in out


# --- review round 2 ---------------------------------------------------------------------------


def test_a_draft_todo_drops_the_suggestions_derived_from_it() -> None:
    # rev-82 / sec-82 R2-1: suggestions come from ALL todos and cannot be attributed to one, so a
    # removed draft todo takes them with it (no re-derivation: no LLM call on the delivery path).
    brief = _brief()  # its one todo belongs to d1

    _, body = render_delivery(without_drafts(brief, {"d1"}), include_todos=True)

    assert "todo body text one" not in body
    assert "suggestion derived from todo text" not in body
    assert "Suggested next steps" not in body


def test_a_brief_without_draft_todos_keeps_its_suggestions() -> None:
    brief = _brief()

    _, body = render_delivery(without_drafts(brief, {"d3", "d4"}), include_todos=True)

    assert "1. suggestion derived from todo text" in body
    assert "- [ ] todo body text one" in body
