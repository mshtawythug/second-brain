"""Tests for brain.vault.derived_links.participants — pure parsing helpers."""
import pytest

from brain.vault.derived_links.participants import (
    extract_gmail_addresses,
    extract_krisp_speakers,
    normalize_participant,
)


class TestNormalizeParticipant:
    """Single-token normalization: emails, names, placeholders, malformed input."""

    @pytest.mark.parametrize("token", ["", "   ", "\t", "\n", " \t\n "])
    def test_empty_or_whitespace_returns_none(self, token: str) -> None:
        assert normalize_participant(token) is None

    @pytest.mark.parametrize(
        "token",
        ["Speaker_1", "Speaker_2", "Speaker_42", "speaker_3", "SPEAKER_99", "  Speaker_1  "],
    )
    def test_speaker_placeholder_returns_none(self, token: str) -> None:
        assert normalize_participant(token) is None

    def test_email_lowercased(self) -> None:
        assert normalize_participant("Bob@Example.COM") == "bob@example.com"

    def test_email_with_angle_brackets_stripped(self) -> None:
        assert normalize_participant("<bob@example.com>") == "bob@example.com"

    def test_email_with_angle_brackets_and_whitespace(self) -> None:
        assert normalize_participant("  <Bob@Example.com>  ") == "bob@example.com"

    def test_normalize_participant_strips_open_bracket_only(self) -> None:
        # Defensive: a stray ``<`` with no matching ``>`` was previously
        # surviving into the returned key. Strip it independently.
        assert normalize_participant("<bob@example.com") == "bob@example.com"

    def test_normalize_participant_strips_close_bracket_only(self) -> None:
        # Mirror of the above for a trailing ``>`` with no opener.
        assert normalize_participant("bob@example.com>") == "bob@example.com"

    @pytest.mark.parametrize(
        "token",
        [
            "@",
            "foo@",
            "@bar.com",
            "foo@bar",  # no dot in domain
            "foo@@bar.com",  # two ats
            "foo @ bar.com",  # whitespace inside
            "foo@ bar.com",  # space after @
        ],
    )
    def test_malformed_email_returns_none(self, token: str) -> None:
        assert normalize_participant(token) is None

    def test_simple_name_passthrough(self) -> None:
        assert normalize_participant("Ali Sarkis") == "ali sarkis"

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_participant("Ali   Sarkis") == "ali sarkis"
        assert normalize_participant("Ali\t\tSarkis") == "ali sarkis"

    def test_strips_leading_and_trailing_punctuation(self) -> None:
        assert normalize_participant(", Ali Sarkis.") == "ali sarkis"
        assert normalize_participant("(Ali Sarkis)") == "ali sarkis"
        assert normalize_participant("--Ali Sarkis--") == "ali sarkis"

    @pytest.mark.parametrize("token", ["A", "J ", "  X  ", "."])
    def test_too_short_after_normalization_returns_none(self, token: str) -> None:
        assert normalize_participant(token) is None

    def test_mixed_case_name_lowercased(self) -> None:
        assert normalize_participant("person-x last-a") == "person-a last-a"

    def test_unicode_name_preserved_lowercased(self) -> None:
        # No transliteration here — that's slug's job. We only lowercase + strip.
        assert normalize_participant("José García") == "josé garcía"


class TestExtractKrispSpeakers:
    """Parse Krisp inline speaker labels: ``**name | mm:ss**``."""

    def test_empty_body_returns_empty_set(self) -> None:
        assert extract_krisp_speakers("") == set()

    def test_body_with_no_labels_returns_empty_set(self) -> None:
        body = "Just plain prose with no Krisp formatting at all.\n"
        assert extract_krisp_speakers(body) == set()

    def test_single_speaker(self) -> None:
        body = "**Ali Sarkis | 00:29**\nHey there\n"
        assert extract_krisp_speakers(body) == {"ali sarkis"}

    def test_multiple_distinct_speakers(self) -> None:
        body = (
            "**Ali Sarkis | 00:29**\nHello.\n"
            "**person-x last-a | 00:35**\nHi back.\n"
            "**person-erik | 01:02**\nHey.\n"
        )
        assert extract_krisp_speakers(body) == {"ali sarkis", "person-a last-a", "person-erik"}

    def test_repeated_speaker_returns_single_entry(self) -> None:
        body = (
            "**Ali Sarkis | 00:01**\nFirst turn.\n"
            "**person-x last-a | 00:10**\nReply.\n"
            "**Ali Sarkis | 00:20**\nSecond turn.\n"
            "**Ali Sarkis | 00:35**\nThird turn.\n"
        )
        assert extract_krisp_speakers(body) == {"ali sarkis", "person-a last-a"}

    def test_speaker_n_placeholders_dropped(self) -> None:
        body = (
            "**Ali Sarkis | 00:01**\nHi.\n"
            "**Speaker_1 | 00:10**\nUnknown person.\n"
            "**Speaker_2 | 00:25**\nAnother unknown.\n"
            "**person-x last-a | 00:40**\nReply.\n"
        )
        assert extract_krisp_speakers(body) == {"ali sarkis", "person-a last-a"}

    def test_email_speaker_returned_as_email(self) -> None:
        body = "**bob@example.com | 1:23**\nHello from bob.\n"
        assert extract_krisp_speakers(body) == {"bob@example.com"}

    def test_email_speaker_lowercased(self) -> None:
        body = "**Person-Benj@Example.com | 00:31**\nHi.\n"
        assert extract_krisp_speakers(body) == {"person-benj@example.com"}

    @pytest.mark.parametrize("timestamp", ["0:05", "00:05", "12:34", "5:30", "59:59"])
    def test_time_variants_match(self, timestamp: str) -> None:
        body = f"**Bob Jones | {timestamp}**\nstuff\n"
        assert extract_krisp_speakers(body) == {"bob jones"}

    def test_whitespace_variations_inside_label(self) -> None:
        body = "**  Ali Sarkis  |  00:29  **\nHey.\n"
        assert extract_krisp_speakers(body) == {"ali sarkis"}

    def test_realistic_transcript_snippet(self) -> None:
        # 4-Phase: setup the snippet, exercise the parser, verify the set,
        # no teardown needed (pure function).
        snippet = (
            "Meeting transcript — 2026-04-15\n"
            "\n"
            "**Ali Sarkis | 00:00**\n"
            "Thanks for jumping on. Quick agenda check.\n"
            "\n"
            "**person-erik@example.com | 00:08**\n"
            "Sounds good. Ready when you are.\n"
            "\n"
            "**Speaker_1 | 00:14**\n"
            "(unidentified noise)\n"
            "\n"
            "**Ali Sarkis | 00:22**\n"
            "Let's start with the renewal.\n"
        )
        assert extract_krisp_speakers(snippet) == {
            "ali sarkis",
            "person-erik@example.com",
        }

    def test_label_inside_code_block_is_still_parsed(self) -> None:
        # Spec: parser is not Markdown-aware. Anything that matches the regex
        # is captured, even when the surrounding context is fenced code.
        body = "```\n**Ali Sarkis | 00:29**\nfenced content\n```\n"
        assert extract_krisp_speakers(body) == {"ali sarkis"}

    def test_label_must_have_double_asterisks_on_both_sides(self) -> None:
        # Single asterisks aren't bold, so they aren't speaker labels.
        body = "*Ali Sarkis | 00:29*\nNot a label.\n"
        assert extract_krisp_speakers(body) == set()

    def test_two_consecutive_labels_both_parsed(self) -> None:
        # Two adjacent speaker turns each yield a separate match.
        body = "**Pre | 00:05**\nfoo\n**Post | 00:10**\nbar\n"
        assert extract_krisp_speakers(body) == {"pre", "post"}

    def test_hour_minute_second_timestamp_matches(self) -> None:
        # Calls over 60 minutes use H:MM:SS for the timestamp; the regex
        # must accept that form too.
        body = "**Ali Sarkis | 1:23:45**\nLong call.\n"
        assert extract_krisp_speakers(body) == {"ali sarkis"}


class TestExtractGmailAddresses:
    """Parse Gmail-style ``from``/``to`` headers via email.utils.getaddresses."""

    def test_empty_metadata_returns_empty_list(self) -> None:
        assert extract_gmail_addresses({}) == []

    def test_metadata_with_none_values_returns_empty_list(self) -> None:
        assert extract_gmail_addresses({"from": None, "to": None}) == []

    def test_metadata_with_empty_strings_returns_empty_list(self) -> None:
        assert extract_gmail_addresses({"from": "", "to": "   "}) == []

    def test_single_from_with_display_name(self) -> None:
        result = extract_gmail_addresses({"from": "Ali Sarkis <redacted@example.com>"})
        assert result == [("ali sarkis", "redacted@example.com")]

    def test_bare_email_no_display_name(self) -> None:
        result = extract_gmail_addresses({"from": "bob@example.com"})
        assert result == [(None, "bob@example.com")]

    def test_multiple_recipients_in_to(self) -> None:
        result = extract_gmail_addresses(
            {"to": "Alice <a@x.com>, Bob <b@x.com>"}
        )
        assert result == [
            ("alice", "a@x.com"),
            ("bob", "b@x.com"),
        ]

    def test_quoted_display_name_with_comma(self) -> None:
        result = extract_gmail_addresses({"from": '"Smith, John" <j@x.com>'})
        assert result == [("smith, john", "j@x.com")]

    def test_malformed_addresses_skipped(self) -> None:
        # ``not-an-email`` has no ``@``; ``foo@`` has no domain.
        result = extract_gmail_addresses({"from": "not-an-email"})
        assert result == []
        result = extract_gmail_addresses({"from": "foo@"})
        assert result == []

    def test_dedup_across_from_and_to(self) -> None:
        # Same email appears in from and to; should be returned exactly once.
        result = extract_gmail_addresses(
            {
                "from": "Ali Sarkis <redacted@example.com>",
                "to": "redacted@example.com, Bob <bob@x.com>",
            }
        )
        # First occurrence (from) wins, then bob from to.
        assert result == [
            ("ali sarkis", "redacted@example.com"),
            ("bob", "bob@x.com"),
        ]

    def test_email_lowercased(self) -> None:
        result = extract_gmail_addresses({"from": "Ali <REDACTED@Example.COM>"})
        assert result == [("ali", "redacted@example.com")]

    def test_display_name_lowercased_and_whitespace_collapsed(self) -> None:
        result = extract_gmail_addresses({"from": "ALI   SARKIS <a@x.com>"})
        assert result == [("ali sarkis", "a@x.com")]

    def test_mixed_valid_and_invalid_in_same_string(self) -> None:
        # email.utils.getaddresses tolerates malformed entries; we drop them.
        result = extract_gmail_addresses(
            {"to": "Alice <a@x.com>, garbage, Bob <b@x.com>"}
        )
        emails = [pair[1] for pair in result]
        assert "a@x.com" in emails
        assert "b@x.com" in emails
        # ``garbage`` has no ``@`` — must not appear.
        assert all("garbage" not in pair[1] for pair in result)

    def test_non_string_metadata_values_ignored(self) -> None:
        # Defensive: if metadata stores something weird (a list, a number),
        # we just skip rather than crash.
        result = extract_gmail_addresses({"from": ["a@x.com"], "to": 42})
        assert result == []

    def test_only_to_provided(self) -> None:
        result = extract_gmail_addresses({"to": "Alice <a@x.com>"})
        assert result == [("alice", "a@x.com")]

    def test_display_name_trailing_punctuation_stripped(self) -> None:
        # Symmetric normalization with `normalize_participant`: Gmail's
        # ``"John Smith." <j@x.com>`` should yield the same display key as
        # Krisp's ``**John Smith. | 00:01**`` would after normalization, so
        # they bridge through directory.resolve_name_to_email.
        result = extract_gmail_addresses({"from": '"John Smith." <j@x.com>'})
        assert result == [("john smith", "j@x.com")]

    def test_display_name_too_short_falls_back_to_none(self) -> None:
        # If the display name fails ``normalize_participant`` (e.g. < 2 chars
        # or a Speaker_N placeholder), drop the display rather than emit a
        # noisy single-letter key that would never resolve anyway.
        result = extract_gmail_addresses({"from": '"A" <a@x.com>'})
        assert result == [(None, "a@x.com")]
