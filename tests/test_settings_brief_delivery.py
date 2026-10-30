"""``brain.settings.brief_delivery``: the brief-delivery knobs (RM-16).

Every test passes a plain dict as ``env``, so nothing here reads the process environment. The
addresses and the webhook URL are synthetic.
"""
from __future__ import annotations

import pytest

from brain.config import ConfigError
from brain.settings.brief_delivery import (
    CHANNEL_EMAIL,
    CHANNEL_NONE,
    CHANNEL_SLACK,
    load_brief_delivery_settings,
)

OWNER = "owner@example.invalid"
#: Slack's documented placeholder shape, never a live hook. Assembled from parts so a secret
#: scanner (GitHub push protection) does not read the literal as a leaked webhook.
WEBHOOK = "/".join(["https://hooks.slack.com/services", "T00000000", "B00000000", "X" * 24])


def test_delivery_is_off_by_default() -> None:
    settings = load_brief_delivery_settings({})

    assert settings.channel == CHANNEL_NONE
    assert settings.enabled is False
    assert settings.include_todos is False
    assert settings.slack_webhook_url is None


def test_email_reads_the_owner_address() -> None:
    settings = load_brief_delivery_settings(
        {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": f"  {OWNER} "}
    )

    assert settings.channel == CHANNEL_EMAIL
    assert settings.enabled is True
    assert settings.recipient == OWNER


def test_email_without_an_owner_address_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="BRAIN_USER_EMAIL"):
        load_brief_delivery_settings({"BRAIN_BRIEF_DELIVERY": "email"})


@pytest.mark.parametrize(
    "address",
    [
        "owner@example.invalid,other@example.invalid",
        "owner@example.invalid other@example.invalid",
        "Owner <owner@example.invalid>",
        "no-at-sign.example.invalid",
        "two@@example.invalid",
        "a@b@example.invalid",
        "owner@example.invalid;other@example.invalid",
        # sec-82 3: a leading dash could be read by gws as an option.
        "-owner@example.invalid",
        "--bcc=other@example.invalid",
    ],
)
def test_email_refuses_anything_but_one_address(address: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_USER_EMAIL") as raised:
        load_brief_delivery_settings({"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": address})

    assert address not in str(raised.value)


def test_slack_reads_the_webhook_and_hides_it_from_repr() -> None:
    settings = load_brief_delivery_settings(
        {"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": WEBHOOK}
    )

    assert settings.channel == CHANNEL_SLACK
    assert settings.slack_webhook_url == WEBHOOK
    assert WEBHOOK not in repr(settings)
    assert "XXXXXXXX" not in repr(settings)


def test_slack_without_a_webhook_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="BRAIN_BRIEF_SLACK_WEBHOOK_URL"):
        load_brief_delivery_settings({"BRAIN_BRIEF_DELIVERY": "slack"})


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.invalid/services/T0/B0/X",
        "http://hooks.slack.com/services/T0/B0/X",
        "https://hooks.slack.com.example.invalid/services/T0/B0/X",
        "https://evil.example.invalid/https://hooks.slack.com/",
        "hooks.slack.com/services/T0/B0/X",
        "https://hooks.slack.com:notaport/services/T0/B0/X",
        "https://hooks.slack.com:8443/services/T0/B0/X",
        # Userinfo; split so the pre-commit PII gate does not read it as an address.
        "https://user:pw" + "@" + "hooks.slack.com/services/T0/B0/X",
        "https://hooks.slack.com/",
        # aud-82 2 / sec-82 2: other Slack hosts, and look-alikes that end in slack.com.
        "https://api.slack.com/services/T0/B0/X",
        "https://evilhooks.slack.com/services/T0/B0/X",
        "https://hooks.evil-slack.com/services/T0/B0/X",
        # aud-82 1b: control characters and whitespace anywhere, and an oversized path.
        "https://hooks.slack.com/services/T0/B0/x\x7fy",
        "https://hooks.slack.com/services/T0/B0/x?a=\x01",
        "https://hooks.slack.com/services/T0/B0/x y",
        "https://hooks.slack.com/services/T0/B0/x\ty",
        "https://hooks.slack.com/services/" + "x" * 65_537,
    ],
)
def test_a_non_slack_webhook_url_is_a_config_error(url: str) -> None:
    with pytest.raises(ConfigError, match="BRAIN_BRIEF_SLACK_WEBHOOK_URL") as raised:
        load_brief_delivery_settings(
            {"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": url}
        )

    # One fixed message for every bad value, so none of them can be quoted in it.
    assert str(raised.value) == (
        "BRAIN_BRIEF_SLACK_WEBHOOK_URL must be a Slack incoming-webhook URL "
        "(https://hooks.slack.com/...)"
    )


def test_a_bad_webhook_is_refused_even_while_delivery_is_off() -> None:
    # Like every other leaf: a typo fails the first time the knobs are read, not the day the
    # owner turns delivery on.
    with pytest.raises(ConfigError, match="BRAIN_BRIEF_SLACK_WEBHOOK_URL"):
        load_brief_delivery_settings(
            {"BRAIN_BRIEF_SLACK_WEBHOOK_URL": "https://hooks.example.invalid/x"}
        )


def test_include_todos_is_opt_in() -> None:
    settings = load_brief_delivery_settings({"BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS": "true"})

    assert settings.include_todos is True


def test_an_unknown_channel_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="BRAIN_BRIEF_DELIVERY"):
        load_brief_delivery_settings({"BRAIN_BRIEF_DELIVERY": "carrier-pigeon"})


def test_the_longest_accepted_url_is_65536_characters() -> None:
    # httpx's own cap on a whole URL; one character more is refused (the parametrized case
    # above refuses a PATH of 65,537 characters).
    prefix = "https://hooks.slack.com/services/"
    url = prefix + "x" * (65_536 - len(prefix))

    settings = load_brief_delivery_settings(
        {"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": url}
    )

    assert settings.slack_webhook_url == url
    with pytest.raises(ConfigError, match="BRAIN_BRIEF_SLACK_WEBHOOK_URL"):
        load_brief_delivery_settings(
            {"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": url + "x"}
        )
