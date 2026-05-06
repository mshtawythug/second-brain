"""Sub-token extractor — surface email/URL/hostname components for FTS."""
import re

# Standard noise TLDs / very common suffix words that add no retrieval value
# when emitted as standalone sub-tokens. Kept lowercase; comparison is
# case-insensitive.
_NOISE_TOKENS: frozenset[str] = frozenset(
    {"com", "org", "net", "io", "co", "gov", "edu"}
)

# Match emails: local@host.tld[.tld...]
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.\w+", re.UNICODE)

# Match URLs (http/https). Stops at whitespace and a few common closing
# punctuation chars so trailing `)`/`]`/`>` don't get folded into the URL.
_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.UNICODE)

# Match bare hostnames: at least two dot-separated word groups, e.g.
# ``example.com/groups``. Restricted to word chars + hyphen so it doesn't
# match every dotted phrase.
_HOST_RE = re.compile(r"\b[\w\-]+(?:\.[\w\-]+)+\b", re.UNICODE)

# Splitter applied to each captured email/URL/host to break it into the
# individual word components. Includes ``@`` so email captures split on
# the local/host boundary, and ``:`` so any leftover scheme markers (e.g.
# port suffixes) don't fold a port number into the host word.
_SPLIT_RE = re.compile(r"[./+_@:?#&=]+", re.UNICODE)


def _is_useful(token: str) -> bool:
    """Filter predicate — drop tokens too short or too noisy to keep."""
    if len(token) <= 1:
        return False
    if token.isdigit():
        return False
    return token.lower() not in _NOISE_TOKENS


def _split_components(value: str) -> list[str]:
    """Split a captured email/URL/hostname into its component words."""
    return [part for part in _SPLIT_RE.split(value) if part]


def extract_sub_tokens(text: str) -> str:
    """Extract sub-tokens from emails, URLs, and bare hostnames in ``text``.

    Returns a single whitespace-joined string of word components in
    *first-seen order* (deduplicated). Returns ``""`` when ``text`` has no
    matches or is empty. Pure function — safe to call twice; idempotent in
    the sense that re-running it on its own output never crashes and
    produces a sane subset (no new sub-tokens to find since the output is
    already plain space-separated words with no `.`/`@`/`/`).

    Filters: tokens of length ≤ 1, digits-only tokens, and the noise
    suffixes in :data:`_NOISE_TOKENS` are dropped.
    """
    if not text:
        return ""

    seen: set[str] = set()
    ordered: list[str] = []

    def _add(token: str) -> None:
        if not _is_useful(token):
            return
        key = token.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(token)

    for match in _EMAIL_RE.finditer(text):
        for component in _split_components(match.group(0)):
            _add(component)

    for match in _URL_RE.finditer(text):
        url = match.group(0)
        # Strip the scheme so we don't emit "https" / "http" as a sub-token.
        if "://" in url:
            url = url.split("://", 1)[1]
        for component in _split_components(url):
            _add(component)

    for match in _HOST_RE.finditer(text):
        host = match.group(0)
        # Skip bare numerics like "1.2.3" (each part already filtered as
        # digits-only, but we'd still touch the dedup table). Cheap guard.
        for component in _split_components(host):
            _add(component)

    return " ".join(ordered)
