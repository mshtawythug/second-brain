"""`brain doctor`'s ``brief delivery`` row (roadmap ticket RM-16).

Which channel the 07:00 brief is sent through (``BRAIN_BRIEF_DELIVERY``) and whether its
prerequisite exists: ``gws`` on ``PATH`` for email, the webhook set for Slack. It never prints the
webhook URL (a secret) or the address, and it is a WARN at worst: a delivery problem never fails
the brief, so it never fails doctor either.

A module of its own, as :mod:`brain.doctor_watcher` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks`` composes
it in. Like that module, it never imports ``brain.cli`` at module scope; ``_check`` defers that.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from .config import ConfigError
from .doctor_runtime import _check
from .settings.brief_delivery import CHANNEL_EMAIL, CHANNEL_NONE, load_brief_delivery_settings

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck


def brief_delivery_doctor_check(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[_DoctorCheck]:
    """The ``brief delivery`` row (RM-16). A WARN at worst: delivery never fails the brief.

    ``env`` defaults to ``os.environ``, which ``Config.load()`` has already filled from the
    dotenv chain. A ``ConfigError``'s message names variables and never a value (the leaf's
    rule), so it is shown, as doctor's ``env`` row shows ``Config.load``'s.
    """
    name = "brief delivery"
    try:
        settings = load_brief_delivery_settings(os.environ if env is None else env)
    except ConfigError as exc:
        return [_check(name, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
    if settings.channel == CHANNEL_NONE:
        return [_check(name, "ok", "off")]
    if settings.channel == CHANNEL_EMAIL:
        if which("gws") is None:
            return [
                _check(name, "warn", "email needs the gws CLI, not on PATH", remedy="install gws")
            ]
        return [_check(name, "ok", "email to BRAIN_USER_EMAIL via gws")]
    return [_check(name, "ok", "slack, webhook set")]
