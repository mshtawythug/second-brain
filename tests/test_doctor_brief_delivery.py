"""``brain doctor``'s ``brief delivery`` row (RM-16): the channel and its prerequisite.

It never prints the webhook URL, never fails doctor, and reads only the ``env`` and ``which`` it
is given. Values are synthetic.
"""
from __future__ import annotations

from brain.doctor_delivery import brief_delivery_doctor_check

SECRET_PATH = "T0SENTINEL/B0SENTINEL/doctor-webhook-secret-42"
WEBHOOK = f"https://hooks.slack.com/services/{SECRET_PATH}"


def _row(env: dict[str, str], *, gws: str | None = "/opt/fake/gws") -> tuple[str, str, str]:
    (check,) = brief_delivery_doctor_check(env=env, which=lambda _name: gws)
    text = "\n".join(line.text for line in check.lines)
    return check.status, check.detail, text


def test_off_is_ok() -> None:
    status, detail, text = _row({})

    assert (status, detail) == ("ok", "off")
    assert text.startswith("brief delivery")


def test_email_with_gws_on_path_is_ok() -> None:
    status, detail, text = _row(
        {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": "owner@example.invalid"}
    )

    assert status == "ok"
    assert "email" in detail and "gws" in detail
    assert "owner@example.invalid" not in text


def test_email_without_gws_warns_with_a_remedy() -> None:
    (check,) = brief_delivery_doctor_check(
        env={"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": "owner@example.invalid"},
        which=lambda _name: None,
    )

    assert check.status == "warn"
    assert "gws" in check.detail
    assert check.remedy


def test_slack_reports_the_webhook_set_never_the_url() -> None:
    status, detail, text = _row(
        {"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": WEBHOOK}
    )

    assert status == "ok"
    assert "slack" in detail and "webhook set" in detail
    assert SECRET_PATH not in text and SECRET_PATH not in detail


def test_a_config_error_warns_and_names_the_variable() -> None:
    (check,) = brief_delivery_doctor_check(
        env={"BRAIN_BRIEF_DELIVERY": "slack",
             "BRAIN_BRIEF_SLACK_WEBHOOK_URL": "https://hooks.example.invalid/x"},
        which=lambda _name: None,
    )

    assert check.status == "warn"
    assert "BRAIN_BRIEF_SLACK_WEBHOOK_URL" in check.detail
    assert "hooks.example.invalid" not in check.detail
    assert check.remedy


def test_the_row_is_part_of_the_runtime_checks() -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    names = [c.check for c in runtime_doctor_checks(platform="linux", processes=[])]

    assert names.count("brief delivery") == 1
