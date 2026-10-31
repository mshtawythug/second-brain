"""The hosts `brain ingest-web` may ingest, read from ``$BRAIN_HOME/web-allowlist.txt`` (RM-41).

One entry per line; a line starting with ``#`` is a comment (a comment always takes a
whole line), and blank lines are ignored::

    # pages from these hosts may become documents
    docs.example.com
    # every subdomain of example.org, never example.org itself
    *.example.org

An entry is an exact host or ``*.`` followed by a domain of at least two labels.
It is matched against the page's normalized host (:func:`brain.ingest.web_url.normalize_host`:
lowercase, IDNA, no trailing dot), so ``Bücher.example.com`` and its punycode form are one
entry. A wildcard covers proper subdomains only (RM-41.D1): the apex needs its own line.

**Default-deny.** :func:`load_web_allowlist` returns ``None`` when the file does not
exist and an empty :class:`WebAllowlist` when it has no entries; ``brain ingest-web``
refuses both, so nothing is ever allowlisted implicitly.

**Fail closed (RM-41.D8).** Any malformed line fails the whole load: a scheme, a path,
a port, userinfo, whitespace or an inline comment, a ``*`` anywhere but a leading
``*.``, a wildcard over one label (``*.com``), or an IP literal (which also keeps
``--fetch`` off loopback and metadata addresses). The :class:`~brain.config.ConfigError`
names the line number only, never its text.

The file is a list rather than ``BRAIN_*`` variables, like ``drop-folders.toml``; the
settings-leaf rule (§2.3) still holds: one loader, one frozen value type.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

from brain.config import ConfigError
from brain.ingest.web_url import normalize_host

__all__ = [
    "CONFIG_NAME",
    "MAX_FILE_BYTES",
    "WebAllowlist",
    "allowlist_path",
    "load_web_allowlist",
]

CONFIG_NAME = "web-allowlist.txt"
#: The largest allowlist read; a hand-written host list is far smaller.
MAX_FILE_BYTES = 1024 * 1024
_WILDCARD = "*."
_FORBIDDEN = frozenset("/:@#[]")


@dataclass(frozen=True)
class WebAllowlist:
    """Exact hosts, and the domains whose proper subdomains are allowed."""

    exact: frozenset[str]
    suffixes: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.exact and not self.suffixes

    def allows(self, host: str) -> bool:
        """Whether the normalized ``host`` is allowlisted."""
        if not host:
            return False
        if host in self.exact:
            return True
        return any(host.endswith(f".{suffix}") for suffix in self.suffixes)


def allowlist_path(brain_home: Path) -> Path:
    """Where the allowlist lives: ``$BRAIN_HOME/web-allowlist.txt``."""
    return brain_home / CONFIG_NAME


def load_web_allowlist(brain_home: Path) -> WebAllowlist | None:
    """The allowlist; ``None`` when the file does not exist (module docstring)."""
    path = allowlist_path(brain_home)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError(f"{CONFIG_NAME} could not be read") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigError(f"{CONFIG_NAME} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{CONFIG_NAME} could not be read (not UTF-8)") from None
    exact: set[str] = set()
    suffixes: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        parsed = _parse_entry(entry)
        if parsed is None:
            raise ConfigError(
                f"{CONFIG_NAME}: line {number} is not a host or wildcard-domain entry"
            )
        is_wildcard, host = parsed
        (suffixes if is_wildcard else exact).add(host)
    return WebAllowlist(exact=frozenset(exact), suffixes=frozenset(suffixes))


def _parse_entry(entry: str) -> tuple[bool, str] | None:
    """``(is_wildcard, normalized host)``, or ``None`` when the entry is malformed."""
    if any(ch.isspace() or ch in _FORBIDDEN for ch in entry):
        return None
    is_wildcard = entry.startswith(_WILDCARD)
    name = entry[len(_WILDCARD):] if is_wildcard else entry
    if "*" in name:
        return None
    host = normalize_host(name)
    if host is None or _is_ip(host):
        return None
    if is_wildcard and "." not in host:
        return None
    return is_wildcard, host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
