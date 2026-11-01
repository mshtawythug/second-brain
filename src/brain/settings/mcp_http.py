"""The MCP HTTP transport's knobs (roadmap ticket RM-46, spec §2.3).

``BRAIN_MCP_TOKEN`` supplies the bearer token directly, for a container that
cannot mount a file; ``BRAIN_MCP_TOKEN_PATH`` moves the token file away from
``$BRAIN_HOME/mcp_token``. The file is the documented path (R1 Q2): an
environment variable is copied into every child process and cannot be
permission-checked, so it is the override, not the default.

A token from the environment must look like one ``brain mcp token --rotate``
mints, so a guessable value cannot slip in. The refusal never quotes it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from brain.config import ConfigError
from brain.settings._env import env_str

#: What a bearer token may be: ``secrets.token_urlsafe(32)`` is 43 characters of
#: this alphabet. The ceiling keeps a header comparison bounded.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,256}")


@dataclass(frozen=True)
class McpHttpSettings:
    """The token knobs. ``token`` is a secret, so it is left out of ``repr``."""

    token: str | None = field(default=None, repr=False)
    token_path: Path | None = None


def load_mcp_http_settings(env: Mapping[str, str]) -> McpHttpSettings:
    """Read ``BRAIN_MCP_TOKEN`` and ``BRAIN_MCP_TOKEN_PATH`` from ``env``."""
    token = env_str(env, "BRAIN_MCP_TOKEN", None)
    if token is not None and not TOKEN_PATTERN.fullmatch(token):
        raise ConfigError(
            "BRAIN_MCP_TOKEN must be 43 to 256 characters of A-Z, a-z, 0-9, "
            "'_' and '-' (mint one with: brain mcp token --rotate --show)"
        )
    raw_path = env_str(env, "BRAIN_MCP_TOKEN_PATH", None)
    token_path = Path(raw_path).expanduser() if raw_path is not None else None
    return McpHttpSettings(token=token, token_path=token_path)
