"""The daily brief's delivery knobs (roadmap ticket RM-16, decision G8).

``BRAIN_BRIEF_DELIVERY`` picks the channel: ``none`` (the default, so nothing leaves the
machine), ``email`` (``gws gmail +send`` to ``BRAIN_USER_EMAIL``, the owner, and nobody else) or
``slack`` (an incoming webhook the owner binds to a private channel or a DM).
``BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS`` is the second opt-in spec §2.5 asks for: todo text and the
suggestions built from it are body-derived, so by default only their count is sent.

``BRAIN_BRIEF_SLACK_WEBHOOK_URL`` is a secret: the URL's path is the credential. It is a
``repr=False`` field, it must be an ``https://hooks.slack.com/`` URL, and no message here quotes it.
It is validated even while the channel is not ``slack``, as the other leaves validate their knobs.

``BRAIN_USER_EMAIL`` is the variable ``Config.user_email`` reads; it is read here too so the
recipient is checked where it is loaded. It must be ONE address: ``gws``'s ``--to`` takes a
comma-separated list, and a second address would send the brief to someone else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from brain.config import ConfigError
from brain.settings._env import env_bool, env_choice, env_str

CHANNEL_NONE = "none"
CHANNEL_EMAIL = "email"
CHANNEL_SLACK = "slack"
CHANNELS = frozenset({CHANNEL_NONE, CHANNEL_EMAIL, CHANNEL_SLACK})

#: Slack's incoming-webhook host. The URL is checked by parts, not by prefix, so
#: ``https://hooks.slack.com.example.invalid/`` is refused.
SLACK_WEBHOOK_HOST = "hooks.slack.com"

#: One bare address: no list separator, no display name, no whitespace, exactly one ``@``.
_ONE_ADDRESS = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+")


@dataclass(frozen=True)
class BriefDeliverySettings:
    """Where the published brief is sent. ``channel == "none"`` means it is not."""

    channel: str
    include_todos: bool
    recipient: str | None = field(default=None, repr=False)
    slack_webhook_url: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.channel != CHANNEL_NONE


def _slack_webhook_url(env: Mapping[str, str]) -> str | None:
    """``BRAIN_BRIEF_SLACK_WEBHOOK_URL``, refused unless it is ``https://hooks.slack.com/…``."""
    raw = env_str(env, "BRAIN_BRIEF_SLACK_WEBHOOK_URL", None)
    if raw is None:
        return None
    message = (
        "BRAIN_BRIEF_SLACK_WEBHOOK_URL must be a Slack incoming-webhook URL "
        f"(https://{SLACK_WEBHOOK_HOST}/...)"
    )
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise ConfigError(message) from None
    if (
        parts.scheme != "https"
        or parts.hostname != SLACK_WEBHOOK_HOST
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or not parts.path.strip("/")
    ):
        raise ConfigError(message)
    return raw


def _recipient(env: Mapping[str, str], *, required: bool) -> str | None:
    """``BRAIN_USER_EMAIL`` as one address; required only by the ``email`` channel."""
    raw = env_str(env, "BRAIN_USER_EMAIL", None)
    if raw is None:
        if required:
            raise ConfigError("BRAIN_BRIEF_DELIVERY=email needs BRAIN_USER_EMAIL to be set")
        return None
    if required and not _ONE_ADDRESS.fullmatch(raw):
        raise ConfigError(
            "BRAIN_USER_EMAIL must be one email address (no list, no display name) "
            "when BRAIN_BRIEF_DELIVERY=email"
        )
    return raw


def load_brief_delivery_settings(env: Mapping[str, str]) -> BriefDeliverySettings:
    """Read the delivery knobs from ``env`` (``os.environ`` after ``Config.load()``)."""
    channel = env_choice(env, "BRAIN_BRIEF_DELIVERY", CHANNELS, CHANNEL_NONE)
    webhook = _slack_webhook_url(env)
    if channel == CHANNEL_SLACK and webhook is None:
        raise ConfigError(
            "BRAIN_BRIEF_DELIVERY=slack needs BRAIN_BRIEF_SLACK_WEBHOOK_URL to be set"
        )
    return BriefDeliverySettings(
        channel=channel,
        include_todos=env_bool(env, "BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS", False),
        recipient=_recipient(env, required=channel == CHANNEL_EMAIL),
        slack_webhook_url=webhook,
    )
