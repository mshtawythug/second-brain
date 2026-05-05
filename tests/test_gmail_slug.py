"""Tests for brain.vault.slug.gmail_slug — stable Gmail mirror slugs.

The slug shape is ``YYYY-MM-DD-<thread6>-<subject-slug>``, capped at 64
characters. These tests pin every dimension that matters for URL stability:
the date comes from ``sent_at`` (UTC-normalized), ``thread6`` is exactly the
first 6 hex chars of ``sha1(thread_id)``, the subject is normalized after
stripping reply prefixes, and only the subject portion is ever truncated.
"""
import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from brain.errors import BrainError
from brain.vault.slug import gmail_slug


def _thread6(thread_id: str) -> str:
    """Helper — recompute the expected thread6 the same way production does."""
    return hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:6]


def test_basic_slug() -> None:
    sent = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
    result = gmail_slug("thread123", sent, "Quick question")
    assert result == f"2026-04-28-{_thread6('thread123')}-quick-question"


def test_strips_re_fwd_prefixes() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t1", sent, "Re: Re: Fwd: Re: hello")
    assert result.endswith("-hello")
    # The "re" / "fwd" segments should not appear anywhere in the subject
    # portion — verify by stripping the known-good prefix.
    prefix = f"2026-04-28-{_thread6('t1')}-"
    subject_portion = result[len(prefix):]
    assert subject_portion == "hello"


def test_strips_unicode_emoji() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t2", sent, "🔥 important meeting")
    prefix = f"2026-04-28-{_thread6('t2')}-"
    assert result == f"{prefix}important-meeting"


def test_no_subject_none() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t3", sent, None)
    assert result == f"2026-04-28-{_thread6('t3')}-no-subject"


def test_no_subject_empty_string() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t3", sent, "")
    assert result == f"2026-04-28-{_thread6('t3')}-no-subject"


def test_no_subject_only_re_prefixes() -> None:
    """A subject that consists entirely of stripped prefixes falls back."""
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t3", sent, "Re: Fwd: Re:")
    assert result == f"2026-04-28-{_thread6('t3')}-no-subject"


def test_long_subject_truncated_to_64() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    long_subject = "x" * 200
    result = gmail_slug("t4", sent, long_subject)
    assert len(result) <= 64
    assert not result.endswith("-")
    # Date prefix and thread6 are always preserved in full.
    assert result.startswith(f"2026-04-28-{_thread6('t4')}-")


def test_punctuation_collapses_to_single_hyphen() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t5", sent, "Hello,, World!!")
    assert result == f"2026-04-28-{_thread6('t5')}-hello-world"
    assert "--" not in result


def test_full_punctuation_example() -> None:
    """Spec example: ``Hello, World! How are you?`` → ``hello-world-how-are-you``."""
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t5b", sent, "Hello, World! How are you?")
    assert result == f"2026-04-28-{_thread6('t5b')}-hello-world-how-are-you"


def test_idempotent_same_inputs_same_output() -> None:
    sent = datetime(2026, 4, 28, 9, 0, tzinfo=UTC)
    a = gmail_slug("thread-abc", sent, "Quarterly review")
    b = gmail_slug("thread-abc", sent, "Quarterly review")
    assert a == b


def test_thread6_is_first_6_hex_of_sha1_of_thread_id() -> None:
    """Cryptographically pin the thread6 derivation."""
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    thread = "190abc123def"
    expected = hashlib.sha1(thread.encode("utf-8")).hexdigest()[:6]
    result = gmail_slug(thread, sent, "anything")
    parts = result.split("-")
    # parts: [YYYY, MM, DD, thread6, ...subject]
    assert parts[3] == expected
    assert len(parts[3]) == 6


def test_uses_sent_at_when_provided() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    fallback = datetime(2020, 1, 1, tzinfo=UTC)
    result = gmail_slug("t6", sent, "x", fallback_date=fallback)
    assert result.startswith("2026-04-28-")
    assert "2020-01-01" not in result


def test_uses_fallback_date_when_sent_at_none() -> None:
    fallback = datetime(2020, 1, 1, tzinfo=UTC)
    result = gmail_slug("t7", None, "x", fallback_date=fallback)
    assert result.startswith("2020-01-01-")


def test_raises_when_no_date_available() -> None:
    with pytest.raises(BrainError, match="sent_at"):
        gmail_slug("t8", None, "x")


def test_raises_when_thread_id_empty() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    with pytest.raises(BrainError, match="thread_id"):
        gmail_slug("", sent, "x")


def test_handles_naive_sent_at() -> None:
    """A naive datetime is treated as UTC — no day shift."""
    naive = datetime(2026, 4, 28, 23, 30)  # no tzinfo
    result = gmail_slug("t9", naive, "subj")
    # No TZ shift means the date stays 2026-04-28, not 2026-04-29.
    assert result.startswith("2026-04-28-")


def test_handles_tz_aware_sent_at_normalizes_to_utc() -> None:
    """A non-UTC tz-aware datetime is converted to UTC before extracting Y-M-D.

    23:30 in UTC-5 (e.g. EST) is 04:30 the next day in UTC, so the date
    portion must be ``2026-04-29``.
    """
    est = timezone(timedelta(hours=-5))
    aware = datetime(2026, 4, 28, 23, 30, tzinfo=est)
    result = gmail_slug("t10", aware, "subj")
    assert result.startswith("2026-04-29-")


def test_total_length_capped_for_long_subject() -> None:
    """Specifically pin the 64-char cap for an arbitrarily long subject."""
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t11", sent, "a" * 500)
    assert len(result) <= 64
    # Subject portion has been truncated; result must not end with a stray hyphen.
    assert not result.endswith("-")


def test_subject_with_only_emoji_falls_back_to_no_subject() -> None:
    sent = datetime(2026, 4, 28, tzinfo=UTC)
    result = gmail_slug("t12", sent, "🚀🔥🎉")
    assert result == f"2026-04-28-{_thread6('t12')}-no-subject"
