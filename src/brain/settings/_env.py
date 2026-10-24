"""Environment parsers shared by every feature settings leaf (RM-08, roadmap spec §2.3).

Each parser reads one variable from ``env`` (``os.environ`` after
``Config.load()``, or a plain ``dict`` in tests):

- **Unset or blank** (empty or whitespace only) yields ``default``, the same rule
  every ``BRAIN_*`` knob in ``config.py`` follows.
- **The value is stripped** before it is parsed.
- **A bad value** raises :class:`brain.config.ConfigError` naming the variable and
  saying what it must be. Unlike ``config.py``'s ``(got {raw!r})``, the message
  **never quotes the value**, because a leaf may hold a secret. The error is
  raised ``from None``: ``int()`` and ``float()`` quote their input in their own
  ``ValueError``, and a chained cause is printed in every traceback.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import overload

from brain.config import _GRAPH_ENABLED_FALSY, _TRUTHY_ENV_VALUES, ConfigError

# The spellings ``config.py`` accepts for its tri-state flags, imported rather
# than restated so a leaf flag and a ``config.py`` flag agree on what "true" is.
TRUTHY = _TRUTHY_ENV_VALUES
FALSY = _GRAPH_ENABLED_FALSY


def _raw(env: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value of ``name``, or ``None`` when it is unset or blank."""
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _bounds_phrase(kind: str, minimum: float, maximum: float | None) -> str:
    if maximum is None:
        return f"{kind} >= {minimum}"
    return f"{kind} in [{minimum}, {maximum}]"


def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Parse ``name`` as ``1/true/yes/on`` or ``0/false/no/off``, case-insensitively."""
    raw = _raw(env, name)
    if raw is None:
        return default
    token = raw.lower()
    if token in TRUTHY:
        return True
    if token in FALSY:
        return False
    raise ConfigError(f"{name} must be one of 1/true/yes/on or 0/false/no/off")


def env_choice(env: Mapping[str, str], name: str, choices: frozenset[str], default: str) -> str:
    """Parse ``name`` as one of ``choices`` (lowercase), compared case-insensitively.

    Returns the lowercased value. A ``default`` outside ``choices``, or a choice
    that is not lowercase, is a bug in the calling leaf and raises ``ValueError``
    whether or not the variable is set.
    """
    if any(choice != choice.lower() for choice in choices):
        raise ValueError(f"choices for {name} must be lowercase")
    if default not in choices:
        raise ValueError(f"default for {name} must be one of its choices")
    raw = _raw(env, name)
    if raw is None:
        return default
    token = raw.lower()
    if token in choices:
        return token
    raise ConfigError(f"{name} must be one of: {', '.join(sorted(choices))}")


def env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Parse ``name`` as an integer in ``[minimum, maximum]`` (``maximum`` optional, inclusive)."""
    raw = _raw(env, name)
    if raw is None:
        return default
    message = f"{name} must be {_bounds_phrase('an integer', minimum, maximum)}"
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(message) from None
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(message)
    return value


def env_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    """Parse ``name`` as a finite float in ``[minimum, maximum]`` (``maximum`` optional, inclusive).

    ``nan`` and ``inf`` are refused: ``float()`` accepts them, and ``nan`` fails
    every comparison, so a bound check alone would let it through.
    """
    raw = _raw(env, name)
    if raw is None:
        return default
    message = f"{name} must be {_bounds_phrase('a finite number', minimum, maximum)}"
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(message) from None
    if not math.isfinite(value):
        raise ConfigError(message)
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(message)
    return value


@overload
def env_str(env: Mapping[str, str], name: str, default: str) -> str: ...


@overload
def env_str(env: Mapping[str, str], name: str, default: None) -> str | None: ...


def env_str(env: Mapping[str, str], name: str, default: str | None) -> str | None:
    """Return the stripped value of ``name``, or ``default`` when it is unset or blank.

    It never refuses. A leaf that needs a shape (a loopback URL, a webhook
    prefix) checks it and raises ``ConfigError`` naming only the variable.
    """
    raw = _raw(env, name)
    return default if raw is None else raw
