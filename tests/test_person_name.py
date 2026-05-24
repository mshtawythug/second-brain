"""Unit tests for ``brain.wiki._person_name`` — the shared person-name normalizer.

Pure-function tests (no DB) covering every Phase-1 data-quality pattern:
mailing-list ``via X`` decoration, ``Last, First (Org`` flipping, separator
collapse (so handle-style and spaced forms merge to one canonical key),
email-as-name humanization, automated/org-sender detection, and owner-variant
expansion. All names are synthetic (Jane Doe / John Smith / Acme Corp); no PII.
"""
from brain.wiki._person_name import (
    NormalizedName,
    expand_owner_keys,
    humanize_person_name,
    is_automated_sender,
    normalize_person_name,
)

# --------------------------------------------------------------------------
# normalize_person_name — mailing-list "via X" decoration (A.3)
# --------------------------------------------------------------------------


class TestViaDecoration:
    def test_plain_via_stripped(self) -> None:
        result = normalize_person_name("Jane Doe via Acme Members")
        assert result == NormalizedName("jane doe", "Jane Doe")

    def test_via_with_surrounding_quote_stripped(self) -> None:
        # Google Groups often leaves a stray apostrophe: ``'Jane Doe' via …``.
        result = normalize_person_name("'Jane Doe' via Acme Members")
        assert result is not None
        assert result.canonical_key == "jane doe"

    def test_via_is_case_insensitive(self) -> None:
        result = normalize_person_name("Jane Doe VIA Acme")
        assert result is not None
        assert result.canonical_key == "jane doe"

    def test_via_only_matches_as_a_word(self) -> None:
        # "Olivia" contains "via" but not as a delimited word — must survive.
        result = normalize_person_name("Olivia Stone")
        assert result is not None
        assert result.canonical_key == "olivia stone"


# --------------------------------------------------------------------------
# normalize_person_name — "Last, First (Org" flipping (A.4)
# --------------------------------------------------------------------------


class TestLastFirstFlip:
    def test_last_first_with_org_fragment(self) -> None:
        result = normalize_person_name("Smith, John (Acme Tech")
        assert result == NormalizedName("john smith", "John Smith")

    def test_last_first_without_org(self) -> None:
        result = normalize_person_name("Smith, John")
        assert result is not None
        assert result.canonical_key == "john smith"

    def test_org_fragment_without_comma_dropped(self) -> None:
        result = normalize_person_name("John Smith (Acme Corp")
        assert result is not None
        assert result.canonical_key == "john smith"

    def test_trailing_credential_after_first_dropped(self) -> None:
        # A second comma (", PhD") is not part of the first name.
        result = normalize_person_name("Smith, John, PhD")
        assert result is not None
        assert result.canonical_key == "john smith"

    def test_degenerate_comma_left_unflipped(self) -> None:
        # A comma with an empty half ("Smith,") can't flip — the canonicalizer
        # just strips the trailing punctuation.
        result = normalize_person_name("Smith,")
        assert result is not None
        assert result.canonical_key == "smith"


# --------------------------------------------------------------------------
# normalize_person_name — separator collapse / merge identity (A.5)
# --------------------------------------------------------------------------


class TestSeparatorCollapse:
    def test_dot_underscore_space_share_one_canonical_key(self) -> None:
        keys = {
            normalize_person_name(raw).canonical_key  # type: ignore[union-attr]
            for raw in ("Jane.Doe", "jane_doe", "Jane Doe", "Jane  Doe")
        }
        assert keys == {"jane doe"}

    def test_hyphen_is_preserved(self) -> None:
        result = normalize_person_name("Anne-Marie")
        assert result is not None
        assert result.canonical_key == "anne-marie"

    def test_apostrophe_is_preserved(self) -> None:
        result = normalize_person_name("O'Brien")
        assert result is not None
        assert result.canonical_key == "o'brien"


# --------------------------------------------------------------------------
# normalize_person_name — email-as-name (A.6)
# --------------------------------------------------------------------------


class TestEmailAsName:
    def test_email_humanized_from_local_part(self) -> None:
        result = normalize_person_name("jane.doe@example.com")
        assert result == NormalizedName("jane doe", "Jane Doe")

    def test_email_never_titlecases_full_address(self) -> None:
        result = normalize_person_name("jane.doe@example.com")
        assert result is not None
        assert "@" not in result.canonical_key
        assert "@" not in result.display_name
        assert "example" not in result.canonical_key

    def test_email_underscore_local_part(self) -> None:
        result = normalize_person_name("john_smith@example.com")
        assert result is not None
        assert result.canonical_key == "john smith"

    def test_single_token_email_local_part(self) -> None:
        result = normalize_person_name("jdoe@example.com")
        assert result is not None
        assert result.canonical_key == "jdoe"


# --------------------------------------------------------------------------
# normalize_person_name — outer junk + rejection cases
# --------------------------------------------------------------------------


class TestNormalizeEdgeCases:
    def test_outer_angle_brackets_stripped(self) -> None:
        result = normalize_person_name("<Jane Doe>")
        assert result is not None
        assert result.canonical_key == "jane doe"

    def test_outer_square_brackets_stripped(self) -> None:
        result = normalize_person_name("[Jane Doe]")
        assert result is not None
        assert result.canonical_key == "jane doe"

    def test_empty_returns_none(self) -> None:
        assert normalize_person_name("") is None
        assert normalize_person_name("   ") is None

    def test_sub_two_char_returns_none(self) -> None:
        assert normalize_person_name("a") is None
        assert normalize_person_name("j.") is None

    def test_display_name_is_title_cased(self) -> None:
        result = normalize_person_name("jane doe")
        assert result is not None
        assert result.display_name == "Jane Doe"


# --------------------------------------------------------------------------
# humanize_person_name
# --------------------------------------------------------------------------


class TestHumanize:
    def test_titlecases_canonical_key(self) -> None:
        assert humanize_person_name("jane doe") == "Jane Doe"

    def test_preserves_internal_hyphen(self) -> None:
        assert humanize_person_name("anne-marie smith") == "Anne-Marie Smith"


# --------------------------------------------------------------------------
# is_automated_sender (A.1)
# --------------------------------------------------------------------------


class TestAutomatedSender:
    def test_no_reply_variants(self) -> None:
        assert is_automated_sender("no-reply@bank.example.com")
        assert is_automated_sender("noreply@example.com")
        assert is_automated_sender("no_reply@example.com")
        assert is_automated_sender("do-not-reply@example.com")
        assert is_automated_sender("donotreply@example.com")

    def test_notification_mailer_bounce_postmaster(self) -> None:
        assert is_automated_sender("notifications@example.com")
        assert is_automated_sender("notification@example.com")
        assert is_automated_sender("mailer-daemon@example.com")
        assert is_automated_sender("bounce@example.com")
        assert is_automated_sender("bounces@example.com")
        assert is_automated_sender("postmaster@example.com")

    def test_prefixed_marker_caught_on_boundary(self) -> None:
        # ``acme.noreply@`` — a marker at the end of the local part after a sep.
        assert is_automated_sender("acme.noreply@example.com")
        # ``mailer-daemon@`` — marker at the start before a sep.
        assert is_automated_sender("mailer-daemon@corp.example.com")

    def test_plus_tag_stripped_before_match(self) -> None:
        # The ``+tag`` suffix is removed before the boundary match.
        assert is_automated_sender("no-reply+unsubscribe@example.com")
        assert not is_automated_sender("john+newsletter@example.com")

    def test_real_humans_with_marker_substring_survive(self) -> None:
        # HIGH #1 regression: markers must NOT substring-match the domain or a
        # longer local-part word. All of these are synthetic REAL people and
        # must survive.
        assert not is_automated_sender(
            "john@mailer-corp.example.com"  # domain contains "mailer"
        )
        assert not is_automated_sender(
            "jane@notifications.acme.com"  # domain contains "notification"
        )
        # "Dana Mailer" → local "dmailer": "mailer" is a substring but NOT on a
        # boundary (no separator before it), so it survives.
        assert not is_automated_sender("dmailer@example.com")
        assert not is_automated_sender("jbounce@example.com")  # "Jane Bounce"
        assert not is_automated_sender("bob@bob.com")  # display==domain rule gone

    def test_ordinary_person_not_flagged(self) -> None:
        assert not is_automated_sender("john.smith@example.com")
        assert not is_automated_sender("jane@acme.com")

    def test_empty_email_never_automated(self) -> None:
        assert not is_automated_sender("")
        assert not is_automated_sender("   ")

    def test_denylist_full_address(self) -> None:
        deny = frozenset({"billing@acme.com"})
        assert is_automated_sender("billing@acme.com", denylist=deny)
        assert not is_automated_sender("jane@acme.com", denylist=deny)

    def test_denylist_substring(self) -> None:
        deny = frozenset({"alerts"})
        assert is_automated_sender("alerts@example.com", denylist=deny)

    def test_denylist_matches_non_email_token(self) -> None:
        # The denylist is checked even for non-email-shaped values.
        assert is_automated_sender("system-bot", denylist=frozenset({"system-bot"}))

    def test_non_email_without_denylist_not_automated(self) -> None:
        assert not is_automated_sender("Jane Doe")


# --------------------------------------------------------------------------
# expand_owner_keys (A.2 owner-variant filtering)
# --------------------------------------------------------------------------


class TestExpandOwnerKeys:
    def test_email_plus_name_owner(self) -> None:
        expanded = expand_owner_keys(frozenset({"pat.owner@example.com", "pat owner"}))
        assert "pat.owner@example.com" in expanded  # raw email
        assert "pat owner" in expanded  # raw display name
        assert "pat" in expanded  # first-name + local-part-first-token variant

    def test_dotted_email_local_part_variants(self) -> None:
        expanded = expand_owner_keys(frozenset({"pat.owner@example.com"}))
        assert "pat.owner@example.com" in expanded
        assert "pat.owner" in expanded  # bare local part
        assert "pat owner" in expanded  # humanized canonical
        assert "pat" in expanded  # first-name-only leak guard

    def test_name_only_owner_first_name_variant(self) -> None:
        expanded = expand_owner_keys(frozenset({"Pat Owner"}))
        assert "pat owner" in expanded
        assert "pat" in expanded

    def test_empty_input(self) -> None:
        assert expand_owner_keys(frozenset()) == frozenset()

    def test_blank_entry_contributes_nothing(self) -> None:
        # A whitespace-only owner key yields no variants.
        assert expand_owner_keys(frozenset({"   "})) == frozenset()

    def test_all_entries_lowercased(self) -> None:
        expanded = expand_owner_keys(frozenset({"Pat@Example.COM"}))
        assert all(entry == entry.lower() for entry in expanded)
