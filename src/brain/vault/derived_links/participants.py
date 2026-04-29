"""Pure participant-extraction helpers — Krisp speaker labels + Gmail headers."""
import re
from email.utils import getaddresses
from typing import Any

# Krisp writes unidentified speakers as ``Speaker <digits>`` (real transcripts,
# space separator) or ``Speaker_<digits>`` (legacy / synthetic test data,
# underscore). Either form is dropped because it would over-link unrelated
# calls — every transcript starts numbering at 1, so ``Speaker 2`` from one
# call has nothing to do with ``Speaker 2`` from another.
_SPEAKER_PLACEHOLDER_RE = re.compile(r"^speaker[ _]\d+$")

# Krisp inline speaker label: ``**<name-or-email> | mm:ss**`` (or ``H:MM:SS``
# for calls over an hour). Capture the label text only. The optional
# ``(?:\d{1,2}:)?`` head allows the H prefix without forcing it.
_KRISP_SPEAKER_RE = re.compile(
    r"\*\*([^|*]+?)\s*\|\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s*\*\*",
)

# Internal-whitespace collapse used by name normalization.
_WHITESPACE_RE = re.compile(r"\s+")

# Single-letter "names" (A, J) are too noisy to link on; require at least two
# characters after normalization.
_MIN_NAME_LENGTH = 2


def is_email_like(addr: str) -> bool:
    """Heuristic: exactly one ``@``, both halves non-empty, RHS has a ``.``, no whitespace."""
    if any(ch.isspace() for ch in addr):
        return False
    if addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return False
    return "." in domain


def _normalize_email(addr: str) -> str | None:
    """Strip surrounding angle brackets + whitespace, lowercase, validate.

    Brackets are stripped independently — a token with only a leading ``<`` or
    only a trailing ``>`` still has the stray bracket removed before
    validation. ``is_email_like`` is the final gate.
    """
    cleaned = addr.strip().lower()
    if cleaned.startswith("<"):
        cleaned = cleaned[1:]
    if cleaned.endswith(">"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.strip()
    if not is_email_like(cleaned):
        return None
    return cleaned


def normalize_participant(token: str) -> str | None:
    """Lowercase + strip; return email if `@` present, normalized name otherwise.

    Returns None for empty / Speaker_N / clearly-malformed tokens.
    """
    stripped = token.strip()
    if not stripped:
        return None

    # Drop unidentified Krisp speakers (case-insensitive).
    if _SPEAKER_PLACEHOLDER_RE.match(stripped.lower()):
        return None

    # Email branch: any token containing ``@`` is treated as email.
    if "@" in stripped:
        return _normalize_email(stripped)

    # Name branch: lowercase, collapse whitespace, strip outer punctuation.
    lowered = stripped.lower()
    collapsed = _WHITESPACE_RE.sub(" ", lowered)
    # Strip leading/trailing punctuation (anything that's not alphanumeric or
    # an internal space). We re-strip whitespace afterwards because removing
    # punctuation can leave dangling spaces (``", Ali."`` → ``" ali "``).
    name = collapsed.strip(" \t\n\r\f\v.,;:!?\"'()[]{}<>-_/\\|")
    name = name.strip()
    if len(name) < _MIN_NAME_LENGTH:
        return None
    return name


def extract_krisp_speakers(body: str) -> set[str]:
    """Parse `**name-or-email | mm:ss**` labels from a Krisp transcript body.

    Drops Speaker_N placeholders. Returns the set of normalized participant
    keys (emails preferred, names where no email is present).
    """
    if not body:
        return set()

    speakers: set[str] = set()
    for match in _KRISP_SPEAKER_RE.finditer(body):
        normalized = normalize_participant(match.group(1))
        if normalized is not None:
            speakers.add(normalized)
    return speakers


def extract_gmail_addresses(metadata: dict[str, Any]) -> list[tuple[str | None, str]]:
    """Use `email.utils.getaddresses` over metadata['from'] + metadata['to'].

    Returns list of (display_name, email) tuples. display_name is None when
    absent. Both elements are normalized (lowercased, stripped). Display
    names are run through `normalize_participant` for cross-source matching
    consistency — that drops outer punctuation and rejects sub-2-char or
    Speaker_N values that would never resolve through the directory anyway.
    """
    raw_strings: list[str] = []
    for key in ("from", "to"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            raw_strings.append(value)

    if not raw_strings:
        return []

    seen_emails: set[str] = set()
    pairs: list[tuple[str | None, str]] = []
    for realname, addr in getaddresses(raw_strings):
        email = (addr or "").strip().lower()
        if not email or not is_email_like(email):
            continue
        if email in seen_emails:
            continue

        display_raw = (realname or "").strip()
        display: str | None
        if display_raw:
            collapsed = _WHITESPACE_RE.sub(" ", display_raw.lower())
            display = normalize_participant(collapsed)
        else:
            display = None

        seen_emails.add(email)
        pairs.append((display, email))

    return pairs
