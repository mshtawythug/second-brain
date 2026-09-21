"""Corpus manifest + PII-guard tests for the `brain demo` synthetic world.

The demo corpus is a MARKETING ASSET shipped in the wheel and seeded on a
stranger's machine, so it must be 100% synthetic. These tests fail the build if
the manifest drifts from its 22-doc contract OR if any real personal data
(non-``.example.com`` email, off-cast person name, phone number) sneaks in.

The PII guard uses SYNTHETIC-ONLY assertions (an allowlist of the invented
cast + a ``.example.com`` email rule), never a denylist of real names — a
denylist would itself embed the very PII it purports to block.
"""
import json
import re

from brain.demo import load_corpus
from brain.source_kinds import VALID_SOURCE_KINDS

# The complete invented cast (CLAUDE.md PII rule). Every person named in a
# structured ``participants`` field must be one of these — nobody real.
CAST = frozenset(
    {
        "Sam Rivera",
        "Priya Okafor",
        "Marcus Chen",
        "Dana Whitfield",
        "Jordan Alvarez",
        "Riley Nakamura",
    }
)

# The four ingest source kinds the demo must showcase.
#
# The canonical object, not a restatement of its four members. As a hardcoded
# literal this guard was an unguarded copy: ``brain.source_kinds`` is the single
# definition every write boundary validates against, and a fifth kind added
# there would leave this set silently stale — the demo would stop covering the
# real enum while the test kept reporting that it did. ``ui/schemas.py`` takes
# the same re-export route and is pinned by identity in
# ``tests/test_ui_schemas.py``; there is no reason for the demo guard to be the
# one copy that can drift.
EXPECTED_SOURCES = VALID_SOURCE_KINDS

# Real-provider email domains that must NEVER appear (generic provider domains,
# not PII — safe to name). All demo emails are ``…@…​.example.com``.
_REAL_PROVIDER_DOMAINS = (
    "gmail.com",
    "outlook.com",
    "yahoo.com",
    "hotmail.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "live.com",
    "proton.me",
    "protonmail.com",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phone-number shapes: US ``NNN-NNN-NNNN`` (any of - . space separators) and a
# ``+`` international run of 10+ digits. Deliberately NOT a loose digit run, so
# ISO dates (``2026-06-02`` = 4-2-2) and durations never false-positive.
_PHONE_RES = (
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    re.compile(r"\+\d{10,}"),
)


def _manifest_text() -> str:
    """The full manifest serialized back to text for whole-corpus scans."""
    return json.dumps(load_corpus())


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_corpus_has_exactly_22_docs() -> None:
    assert len(load_corpus()) == 22


def test_corpus_covers_all_four_source_kinds() -> None:
    sources = {record["source"] for record in load_corpus()}
    assert sources >= EXPECTED_SOURCES, f"missing source kinds: {EXPECTED_SOURCES - sources}"
    # No stray/unknown source kinds either.
    assert sources <= EXPECTED_SOURCES, f"unexpected source kinds: {sources - EXPECTED_SOURCES}"


def test_external_ids_are_unique() -> None:
    ids = [record["external_id"] for record in load_corpus()]
    assert len(ids) == len(set(ids)), "duplicate external_id in demo corpus"


def test_every_record_has_the_required_shape() -> None:
    required = {
        "external_id",
        "source",
        "title",
        "date",
        "content_type",
        "tags",
        "body",
    }
    for record in load_corpus():
        missing = required - record.keys()
        assert not missing, f"{record.get('external_id')!r} missing keys: {missing}"
        assert isinstance(record["tags"], list)
        assert isinstance(record["title"], str) and record["title"].strip()


def test_dates_parse_as_iso() -> None:
    from datetime import date

    for record in load_corpus():
        # Raises ValueError on a non-ISO date → test failure.
        date.fromisoformat(record["date"])


def test_bodies_are_marketing_length() -> None:
    """Bodies should be substantive prose (target 120-250 words)."""
    for record in load_corpus():
        words = len(record["body"].split())
        assert 100 <= words <= 300, (
            f"{record['external_id']!r} body is {words} words (want ~120-250)"
        )


# ---------------------------------------------------------------------------
# PII guard — synthetic-only assertions
# ---------------------------------------------------------------------------


def test_all_emails_are_example_com() -> None:
    # RFC 2606 reserves example.com for synthetic use. Accept both the bare
    # domain and any ``*.example.com`` subdomain (the repo's pre-commit PII
    # gate only recognizes the bare form, so the corpus ships bare addresses).
    for email in _EMAIL_RE.findall(_manifest_text()):
        domain = email.rsplit("@", 1)[-1]
        assert domain == "example.com" or domain.endswith(".example.com"), (
            f"non-synthetic email in demo corpus: {email!r}"
        )


def test_no_real_provider_email_domains() -> None:
    text = _manifest_text().lower()
    for domain in _REAL_PROVIDER_DOMAINS:
        assert f"@{domain}" not in text, f"real provider email domain present: {domain}"


def test_participants_are_all_cast_members() -> None:
    for record in load_corpus():
        for person in record.get("participants") or []:
            assert person in CAST, (
                f"{record['external_id']!r} names a non-cast person: {person!r}"
            )


def test_no_phone_number_patterns() -> None:
    text = _manifest_text()
    for pattern in _PHONE_RES:
        match = pattern.search(text)
        assert match is None, f"phone-number-like pattern in demo corpus: {match!r}"
