"""Tests for ``brain.ingest.attachment_text`` — the receipt-text rules (RM-45).

Pure unit tests. Every value is synthetic: ``4111 1111 1111 1111`` is the
documented test card number, merchants are ``Example …`` names.
"""
from __future__ import annotations

import pytest

from brain.ingest import ExtractedDoc
from brain.ingest.attachment_text import (
    MAX_NAME_CHARS,
    REDACTED_CARD,
    TRUNCATED_LINE,
    UNNAMED,
    neutralize_markup,
    redact_card_numbers,
    render_section,
    safe_name,
    with_sections,
)
from brain.ui.render import render_markdown

TEST_PAN = "4111111111111111"


# --- card numbers ----------------------------------------------------------


@pytest.mark.parametrize(
    "spelled",
    [TEST_PAN, "4111 1111 1111 1111", "4111-1111-1111-1111", "4111 1111-1111 1111"],
)
def test_a_luhn_valid_card_number_is_redacted_however_it_is_spaced(spelled: str) -> None:
    out = redact_card_numbers(f"Card {spelled} charged")

    assert out == f"Card {REDACTED_CARD} charged"


def test_a_thirteen_and_a_nineteen_digit_card_are_redacted() -> None:
    thirteen = "4222222222222"  # Luhn-valid, 13 digits
    luhn_19 = "4111111111111111110"  # Luhn-valid, 19 digits
    not_luhn_19 = "6011000990139424000"  # 19 digits, fails Luhn

    assert redact_card_numbers(thirteen) == REDACTED_CARD
    assert redact_card_numbers(luhn_19) == REDACTED_CARD
    assert redact_card_numbers(not_luhn_19) == not_luhn_19


def test_a_sixteen_digit_order_number_that_fails_luhn_is_kept() -> None:
    order = "Order 1234567812345678"

    assert redact_card_numbers(order) == order


@pytest.mark.parametrize("digits", ["411111111111", "41111111111111111111"])
def test_runs_shorter_than_thirteen_or_longer_than_nineteen_are_kept(digits: str) -> None:
    assert redact_card_numbers(digits) == digits


def test_a_card_inside_a_longer_digit_run_is_not_cut_out_of_it() -> None:
    """The run is matched whole: a valid prefix of a longer number is not a card."""
    longer = f"{TEST_PAN} 2222 3333"

    assert redact_card_numbers(longer) == longer


@pytest.mark.parametrize(
    "token",
    [f"ghp_EXAMPLE{TEST_PAN}", f"{TEST_PAN}abc", f"key_{TEST_PAN}", f"x{TEST_PAN}_"],
)
def test_digits_inside_a_token_are_left_for_the_secret_guard(token: str) -> None:
    assert redact_card_numbers(token) == token


@pytest.mark.parametrize("framed", [f"({TEST_PAN})", f"card:{TEST_PAN}.", f"#{TEST_PAN}"])
def test_a_card_framed_by_punctuation_is_redacted(framed: str) -> None:
    assert TEST_PAN not in redact_card_numbers(framed)


def test_a_masked_card_is_kept() -> None:
    text = "Visa **** 0000"

    assert redact_card_numbers(text) == text


def test_two_separators_in_a_row_end_the_run() -> None:
    text = "4111 1111  1111 1111"

    assert redact_card_numbers(text) == text


# --- markup ----------------------------------------------------------------


def test_html_markup_is_escaped() -> None:
    out = neutralize_markup("</details>\n<summary>x</summary>\n<script>alert(1)</script>")

    assert "<" not in out and ">" not in out
    assert "&lt;/details&gt;" in out


@pytest.mark.parametrize("line", ["# Total", "   ## 2026-01-01 00:00 — boss@example.com"])
def test_a_line_that_would_be_a_heading_is_escaped(line: str) -> None:
    out = neutralize_markup(line)

    assert "<h" not in render_markdown(out)
    assert "\\#" in out


@pytest.mark.parametrize("underline", ["---", "===", "  - - -  ", "~~~", "```"])
def test_setext_rules_and_fences_are_escaped(underline: str) -> None:
    out = neutralize_markup(f"Spoofed heading\n{underline}\nafter")

    html = render_markdown(out)
    assert "<h1" not in html and "<h2" not in html and "<pre" not in html
    assert "<hr" not in html


def test_link_and_image_brackets_are_escaped() -> None:
    out = neutralize_markup("![t](https://tracker.example.com/p.png) [[brain:abc]]")

    html = render_markdown(out)
    assert "<img" not in html and "<a" not in html
    assert "\\[\\[" in out


def test_control_and_format_characters_are_removed_but_newlines_and_tabs_kept() -> None:
    raw = "a\x1b[31mb‮c​d\x00e\tf\r\ng\rh"

    out = neutralize_markup(raw)

    assert out == "a[31mbcde\tf\ng\nh".replace("[", "\\[")


def test_blank_line_runs_collapse_and_trailing_space_is_dropped() -> None:
    assert neutralize_markup("a  \n\n\n\n b\n\n") == "a\n\n b"


# --- names -----------------------------------------------------------------


def test_a_name_is_one_escaped_line() -> None:
    assert safe_name("Receipt\n<b>0001</b>\x1b.pdf") == "Receipt &lt;b&gt;0001&lt;/b&gt;.pdf"


@pytest.mark.parametrize("raw", ["", "   ", "\x00\x1b", None, 7])
def test_an_empty_or_non_string_name_reads_unnamed(raw: object) -> None:
    assert safe_name(raw) == UNNAMED


def test_a_long_name_is_capped() -> None:
    out = safe_name("r" * 10_000 + ".pdf")

    assert len(out) == MAX_NAME_CHARS
    assert out.endswith("…")


def test_a_name_is_capped_before_it_is_escaped_so_no_entity_is_cut() -> None:
    out = safe_name("<" * 500)

    assert out.endswith("…")
    assert out[:-1].replace("&lt;", "") == ""


def test_a_name_with_a_leading_hash_cannot_become_a_heading() -> None:
    section = render_section("# x.pdf", "body", max_chars=100)

    assert section.splitlines()[0] == "## Attachment: \\# x.pdf"


# --- sections --------------------------------------------------------------


def test_render_section_heading_and_body() -> None:
    assert render_section("Receipt.pdf", "Total 1.00", max_chars=100) == (
        "## Attachment: Receipt.pdf\n\nTotal 1.00"
    )


def test_render_section_truncates_at_a_line_boundary_with_a_marker() -> None:
    text = "line one\nline two\nline three"

    out = render_section("a.pdf", text, max_chars=12)

    assert out == f"## Attachment: a.pdf\n\nline one\n{TRUNCATED_LINE}"


def test_render_section_hard_cuts_a_single_long_line() -> None:
    out = render_section("a.pdf", "x" * 50, max_chars=10)

    assert out == f"## Attachment: a.pdf\n\n{'x' * 10}\n{TRUNCATED_LINE}"


def test_with_sections_appends_and_merges_metadata_without_mutating_the_input() -> None:
    doc = ExtractedDoc(
        title="Your order",
        content="## 2026-01-01 00:00 — shop@example.com\n\nThanks",
        content_type="email_thread",
        source_path=None,
        metadata={"thread_id": "t1"},
    )

    out = with_sections(doc, ["## Attachment: a.pdf\n\nTotal"], {"attachments": []})

    assert out.content.endswith("Thanks\n\n## Attachment: a.pdf\n\nTotal")
    assert out.metadata == {"thread_id": "t1", "attachments": []}
    assert doc.content.endswith("Thanks")
    assert doc.metadata == {"thread_id": "t1"}


def test_with_sections_and_no_sections_keeps_the_body() -> None:
    doc = ExtractedDoc("t", "body", "email_thread", None, {})

    out = with_sections(doc, [], {"attachments_fingerprint": "f"})

    assert out.content == "body"
    assert out.metadata == {"attachments_fingerprint": "f"}


def test_render_section_of_text_with_nothing_readable_is_none() -> None:
    assert render_section("a.pdf", " \x00\x1b \n\n ", max_chars=100) is None


def test_render_section_redacts_before_it_truncates() -> None:
    """A cut can never leave the visible half of a card number behind."""
    out = render_section("a.pdf", f"Card {TEST_PAN}", max_chars=12)

    assert out is not None
    assert "4111" not in out


def test_a_name_cannot_carry_a_link() -> None:
    out = safe_name("[click](https://evil.example.com)\\.pdf")

    assert out == "\\[click](https://evil.example.com)\\\\.pdf"
