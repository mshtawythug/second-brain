"""Pure participant-extraction helpers — Krisp speaker labels + Gmail headers."""
from typing import Any


def normalize_participant(token: str) -> str | None:
    """Lowercase + strip; return email if `@` present, normalized name otherwise.

    Returns None for empty / Speaker_N / clearly-malformed tokens.
    """
    raise NotImplementedError("Implemented in Task A.3")


def extract_krisp_speakers(body: str) -> set[str]:
    """Parse `**name-or-email | mm:ss**` labels from a Krisp transcript body.

    Drops Speaker_N placeholders. Returns the set of normalized participant
    keys (emails preferred, names where no email is present).
    """
    raise NotImplementedError("Implemented in Task A.3")


def extract_gmail_addresses(metadata: dict[str, Any]) -> list[tuple[str | None, str]]:
    """Use `email.utils.getaddresses` over metadata['from'] + metadata['to'].

    Returns list of (display_name, email) tuples. display_name is None when
    absent. Both elements are normalized (lowercased, stripped).
    """
    raise NotImplementedError("Implemented in Task A.3")
