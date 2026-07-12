"""Shared ``--since`` duration parsing for CLI lookback windows.

Every ``brain`` command with a ``--since`` window accepts either a bare number
— interpreted in that command's *native* unit (days for most commands, hours
for ``brain brief --since``) — or an explicit suffix that is unit-independent:
``7d`` (days), ``24h`` (hours), ``90m`` (minutes). Centralising the grammar
here keeps every command on one set of accepted forms and one set of
rejections, and lets bare numbers keep their historical per-command meaning
(zero breaking changes).
"""
import re
from datetime import timedelta
from typing import Literal

import typer

# Bare numbers are read in the command's native unit; suffixes override it.
_BareUnit = Literal["days", "hours"]

# ``<digits>`` with an optional single ``d``/``h``/``m`` suffix. ``fullmatch``
# rejects a leading sign ("-3d"), a leading letter ("d7"), the empty string,
# decimals ("7.5"), and doubled suffixes ("7dd").
_SINCE_RE = re.compile(r"(?P<value>\d+)(?P<unit>[dhm])?")

_SECONDS_PER: dict[_BareUnit, int] = {"days": 86_400, "hours": 3_600}


def parse_since(value: str, *, bare_unit: _BareUnit) -> timedelta:
    """Parse a ``--since`` argument into a :class:`~datetime.timedelta`.

    A bare number (``"7"``) is interpreted in ``bare_unit``; a suffixed value
    (``"7d"`` / ``"24h"`` / ``"90m"``) is unit-independent and always wins.

    Raises:
        typer.BadParameter: When ``value`` is empty, negative, non-integer, or
            otherwise not ``<digits>[d|h|m]`` (e.g. ``""``, ``"d7"``, ``"-3d"``).
    """
    text = value.strip()
    match = _SINCE_RE.fullmatch(text)
    if match is None:
        raise typer.BadParameter(
            f"invalid duration {value!r}; expected a bare number (interpreted "
            f"as {bare_unit}) or a suffixed value like '7d', '24h', '90m'"
        )
    magnitude = int(match.group("value"))
    unit = match.group("unit")
    if unit == "d":
        return timedelta(days=magnitude)
    if unit == "h":
        return timedelta(hours=magnitude)
    if unit == "m":
        return timedelta(minutes=magnitude)
    # Bare number — read in the command's native unit.
    if bare_unit == "days":
        return timedelta(days=magnitude)
    return timedelta(hours=magnitude)


def whole_units(duration: timedelta, *, unit: _BareUnit) -> int:
    """Round ``duration`` UP to whole ``unit``s (days or hours).

    Downstream consumers take an integer day/hour window bound straight into
    SQL, so a fractional window has to become a whole number. Rounding *up*
    keeps a positive sub-unit window (e.g. ``90m`` in a days-unit command) from
    collapsing to ``0`` — which every consumer treats as "no window". A value
    already sitting on a whole-``unit`` boundary (every bare number, and any
    coarser-or-equal suffix) converts exactly.
    """
    # ``parse_since`` only ever yields whole days/hours/minutes, so microseconds
    # are always zero — integer arithmetic here is exact (no float rounding).
    total_seconds = duration.days * 86_400 + duration.seconds
    per = _SECONDS_PER[unit]
    whole, remainder = divmod(total_seconds, per)
    return whole + (1 if remainder else 0)


def since_window(value: str, *, unit: _BareUnit) -> int:
    """Parse a ``--since`` value and return its whole-``unit`` window.

    The one-call CLI convenience: ``since_window("24h", unit="days") == 1``.
    ``unit`` is both the bare-number interpretation and the output unit (they
    are always the same for a given command).
    """
    return whole_units(parse_since(value, bare_unit=unit), unit=unit)
