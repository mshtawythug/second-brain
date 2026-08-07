"""Agent identity — who is asking, as opposed to which surface asked (F10).

``source`` on ``search_queries`` / ``interactions`` already records the
SURFACE (``cli`` / ``mcp`` / ``wiki``). This module supplies the other axis:
the ACTOR. Two agents both talking over MCP are indistinguishable by
``source``, and "is the research agent's hit rate worse than the capture
agent's" is the question ``brain usage`` exists to answer.

Precedence is flag > environment > unattributed, and **unattributed is a
first-class answer**, not a failure. Every pre-027 row is genuinely
unattributed; a default of ``'cli'`` would duplicate the surface into the
actor field and make "which agent" unanswerable for exactly the rows that
claim to answer it.

The grammar lives in :data:`brain.config.AGENT_ID_PATTERN` and is *imported*
rather than re-declared here. ``config.py`` inlines the pattern because it
must stay importable before this module exists, and its comment names this
module as the sibling validator against the same constant — importing is what
makes that promise unbreakable instead of merely documented.
"""
from __future__ import annotations

import re

from .config import AGENT_ID_PATTERN, Config
from .errors import AgentIdInvalid

_AGENT_ID_RE = re.compile(AGENT_ID_PATTERN)


def normalize_agent_id(raw: str | None) -> str | None:
    """Strip and validate an agent identifier.

    ``None``, empty, or whitespace-only input yields ``None`` — "no agent"
    rather than an error, so an unset ``--agent`` flag and an unset
    ``BRAIN_AGENT_ID`` both mean the same thing and neither is a usage error.

    Anything else must match :data:`~brain.config.AGENT_ID_PATTERN`: one
    alphanumeric character followed by up to 63 more alphanumerics, dots,
    underscores, colons or hyphens. Leading punctuation is rejected so an id
    can never be mistaken for a CLI flag.

    Raises :class:`~brain.errors.AgentIdInvalid` on a malformed id. Failing
    loudly is deliberate: silently dropping a typo'd id would attribute the
    row to nobody, and the user would discover it only as a permanently empty
    ``brain usage`` bucket.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if not _AGENT_ID_RE.match(candidate):
        raise AgentIdInvalid(
            f"agent id must match {AGENT_ID_PATTERN} (got {raw!r})"
        )
    return candidate


def resolve_agent_id(explicit: str | None, cfg: Config) -> str | None:
    """Pick the effective agent id: ``explicit`` > ``cfg.agent_id`` > ``None``.

    ``explicit`` is the per-invocation ``--agent`` flag or MCP ``agent_id``
    parameter and is normalized here, so every caller gets the same validation
    without repeating it. ``cfg.agent_id`` was already normalized at
    :meth:`Config.load` time (it raises ``ConfigError`` on a bad
    ``BRAIN_AGENT_ID``), so it is trusted as-is.

    A blank explicit value falls through to the config rather than forcing
    ``None`` — ``--agent ""`` is indistinguishable from an unset flag at the
    Typer boundary, so treating it as "unset" is the only coherent reading.
    """
    normalized = normalize_agent_id(explicit)
    if normalized is not None:
        return normalized
    return cfg.agent_id
