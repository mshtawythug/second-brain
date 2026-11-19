"""``host:port`` of a DSN for an error line, only when no part of a password can be in it.

Used by the ingest commands' "cannot reach DATABASE_URL" line (0.4.0 QA F2). libpq's
own parse is not enough. A malformed DSN parses *successfully* with the password, or a
fragment of it, in the host or port slot. Examples: an unescaped ``/`` or ``@`` in a URI
password, an ``&`` in a URI query, an unquoted space in a ``key=value`` password. Review
rev-137 found those one probe at a time, so the rule here is structural.

A password's text can only extend to the right of where it starts. The address is
printed only when the host and port provably come from somewhere the password cannot
reach. Otherwise the address is ``(unparseable)``.

**URI** (``postgresql://`` or ``postgres://``, libpq's prefixes):

- The authority is the text after ``://``, up to the first ``/``, ``?`` or ``#``. It
  holds at most one ``@``, and no ``@`` appears after it. The userinfo, and so the
  password, then ends at that ``@``, and the host and port after it are clean.
- The query sets neither ``host``, ``hostaddr`` nor ``port``, compared after
  percent-decoding, as libpq decodes keys. A query comes after the userinfo, so an
  unescaped ``&`` in a query password could inject them.

**key=value:**

- ``host`` and ``port`` each appear at most once.
- Both appear before any ``password`` keyword. An unquoted space in the password value
  injects keywords only after it.

**Both forms:**

- libpq's parser (``conninfo_to_dict``) accepts the DSN.
- The port, when given, is an integer from 1 to 65535.
- Every host in the list is a host name, an IPv4 address, a bare IPv6 address (an
  optional ``%zone``) or an absolute socket directory.

The checks are conservative. A DSN they refuse may be valid, and it is only printed as
``(unparseable)``.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

import psycopg

from .backup.pgtool import dsn_target

UNPARSEABLE = "(unparseable)"

_URI_PREFIXES = ("postgresql://", "postgres://")
_HOST_RE = re.compile(
    r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?"
    r"|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?:%[A-Za-z0-9]+)?"
    r"|/[A-Za-z0-9._/-]*"
)
#: A ``key=value`` keyword: libpq separates keywords with whitespace and allows
#: whitespace around ``=``. A match inside a quoted value only makes the check refuse.
_KEYWORD_RE = re.compile(r"(?:^|(?<=\s))(host|port|password)\s*=")
_AUTHORITY_END_RE = re.compile(r"[/?#]")
_ADDRESS_QUERY_KEYS = frozenset({"host", "hostaddr", "port"})
_MAX_PORT = 65535


def _uri_is_clean(dsn: str) -> bool:
    rest = dsn.split("://", 1)[1]
    end = _AUTHORITY_END_RE.search(rest)
    authority, tail = (rest[: end.start()], rest[end.start() :]) if end else (rest, "")
    if authority.count("@") > 1 or "@" in tail:
        return False
    query = tail.partition("?")[2].partition("#")[0]
    # libpq percent-decodes query KEYS too: ``ho%73t=`` sets the host.
    keys = {unquote(pair.partition("=")[0]) for pair in query.split("&") if pair}
    return not keys & _ADDRESS_QUERY_KEYS


def _keywords_are_clean(dsn: str) -> bool:
    keywords = [m.group(1) for m in _KEYWORD_RE.finditer(dsn)]
    if keywords.count("host") > 1 or keywords.count("port") > 1:
        return False
    if "password" not in keywords:
        return True
    before_password = keywords[: keywords.index("password")]
    return all(k in before_password for k in ("host", "port") if k in keywords)


def db_address(database_url: str) -> str:
    """``host:port`` of ``database_url``, or ``(unparseable)`` (module docstring)."""
    is_uri = database_url.startswith(_URI_PREFIXES)
    if not (_uri_is_clean(database_url) if is_uri else _keywords_are_clean(database_url)):
        return UNPARSEABLE
    try:
        target = dsn_target(database_url)
    except (psycopg.Error, ValueError):
        return UNPARSEABLE
    if target.port is not None and not 1 <= target.port <= _MAX_PORT:
        return UNPARSEABLE
    if target.host is not None and not all(
        _HOST_RE.fullmatch(h) for h in target.host.split(",")
    ):
        return UNPARSEABLE
    return target.address
