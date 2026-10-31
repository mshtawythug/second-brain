"""Tests for ``brain.ingest.attachment_text`` — the receipt-text rules (RM-45).

Pure unit tests. Every value is synthetic: ``4111 1111 1111 1111`` is the
documented test card number, merchants are ``Example …`` names.
"""
from __future__ import annotations

from pathlib import Path

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
    "spelled", [TEST_PAN, "4111 1111 1111 1111", "4111-1111-1111-1111", "4111.1111.1111.1111"]
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


@pytest.mark.parametrize(
    "digits", ["411111111111", "41111111111111111111", "4111111111111111111100"]
)
def test_runs_shorter_than_thirteen_or_longer_than_nineteen_are_kept(digits: str) -> None:
    assert redact_card_numbers(digits) == digits


def test_a_card_followed_by_more_digit_groups_is_still_redacted() -> None:
    """sec-117 F4: a card printed before more digits (a quantity, a reference) is found by
    its group-aligned window; the groups after it are kept."""
    out = redact_card_numbers(f"{TEST_PAN} 2222 3333")

    assert out == f"{REDACTED_CARD} 2222 3333"


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


def test_three_spaces_in_a_row_end_a_digit_group_run() -> None:
    text = "4111 1111    1111 1111"

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



# --- sec-117 F4: layouts the first cut missed ---------------------------------


@pytest.mark.parametrize(
    ("layout", "kept"),
    [
        ("Card 4111 1111 1111 1111 12/28", "12/28"),
        ("4111 1111 1111 1111-1", "-1"),
        ("4111\u00ad1111\u00ad1111\u00ad1111", ""),
        ("4111\u200b1111\u200b1111\u200b1111", ""),
        ("4111\u00a01111\u00a01111\u00a01111", ""),
        ("4111\u202f1111\u20071111\u20091111", ""),
        ("4111  1111  1111  1111", ""),
        ("4111.1111.1111.1111", ""),
        ("4111 1111\n1111 1111", ""),
        ("4111 1111 \n 1111 1111", ""),
        ("Card: 4111111111111111 exp 12/28", "exp 12/28"),
        ("Order 1234 4111 1111 1111 1111", "Order 1234"),
        # rev-117's cases: an amount, a count or a reference printed next to the card
        ("4111111111111111 10.00", "10.00"),
        ("Visa 4111111111111111 1 item", "1 item"),
        ("Qty 2 4111111111111111", "Qty 2"),
        ("ref 4111 1111 1111 1111 000 total", "000 total"),
    ],
)
def test_every_card_layout_in_the_review_is_redacted(layout: str, kept: str) -> None:
    out = render_section("statement.pdf", layout, max_chars=1000)

    assert out is not None
    assert "4111" not in out
    assert "1111" not in out
    assert REDACTED_CARD.replace("[", "\\[") in out
    assert kept.replace("-", "\\-") in out or kept in out


@pytest.mark.parametrize(
    "kept",
    ["**** **** **** 0000", "Visa ending 0000", "ghp_EXAMPLE4111111111111111", "Total 12.34"],
)
def test_masks_tokens_and_amounts_are_left_alone(kept: str) -> None:
    out = render_section("r.pdf", kept, max_chars=1000)

    assert out is not None
    assert "REDACTED" not in out


def test_two_line_wraps_end_a_digit_group_run() -> None:
    text = "4111 1111\n\n1111 1111"

    assert redact_card_numbers(text) == text


# --- sec-117 F7: Unicode line separators ---------------------------------------


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c"])
def test_unicode_line_separators_become_newlines_before_escaping(separator: str) -> None:
    raw = separator.join(["Total 1.00", "## See Also", "## 2026-01-01 — boss@example.com", "```"])

    out = render_section("r.pdf", raw, max_chars=1000)

    assert out is not None
    body = out.split("\n\n", 1)[1]
    assert not any(ch in body for ch in "\u2028\u2029\x85\x0b\x0c")
    for line in body.splitlines():
        assert not line.startswith(("#", "`")), line


def test_a_line_separator_cannot_plant_a_see_also_heading(tmp_path: Path) -> None:
    """``connect.append_see_also_link`` splits with ``splitlines``, which honours U+2028."""
    from brain.connect import append_see_also_link

    section = render_section("r.pdf", "Total 1.00\u2028## See Also\u2028- [[planted]]",
                             max_chars=1000)
    assert section is not None
    note = tmp_path / "thread.md"
    note.write_text(f"## 2026-01-01 00:00 — shop@example.com\n\nThanks\n\n{section}\n",
                    encoding="utf-8")

    assert append_see_also_link(note, "[[brain:abcd1234]]")

    text = note.read_text(encoding="utf-8")
    assert text.endswith("## See Also\n\n- [[brain:abcd1234]]\n")
    assert [line for line in text.splitlines() if line.strip() == "## See Also"] == [
        "## See Also"
    ]



# --- rev-117 round 2: strict layouts (team-lead's ruling) ---------------------------


@pytest.mark.parametrize(
    "layout",
    [
        "3782 822463 10005",  # Amex 4-6-5
        "3782-822463-10005",
        "3056 930902 5904",  # Diners 4-6-4
        "4111 1111 1111 1111 110",  # 4-4-4-4-3, 19 digits
        "4111\u20131111\u20131111\u20131111",  # en dash
        "4111\u20141111\u20141111\u20141111",  # em dash
        "4111\u22121111\u22121111\u22121111",  # minus sign
        "4111/1111/1111/1111",
        "4111\t1111\t1111\t1111",
        " ".join(["\u0664" + "\u0661" * 3] + ["\u0661" * 4] * 3),  # Arabic-Indic digits
        " ".join(["\uff14" + "\uff11" * 3] + ["\uff11" * 4] * 3),  # fullwidth digits
        "5555 5555 5555 4444 05/29",
    ],
)
def test_every_strict_card_layout_is_redacted(layout: str) -> None:
    out = render_section("statement.pdf", f"Card {layout} paid", max_chars=1000)

    assert out is not None
    assert "REDACTED:card-number" in out
    assert layout.split()[0] not in out


@pytest.mark.parametrize(
    "text",
    [
        "Qty Price Amount\n2 4.99 9.98\n1 12.50 12.50\n3 1.00 3.00\n",
        "1 1299 12.99\n2 450 9.00\n3 1250 10.00",
        "Tel 555 0100 1234 567",
        "4111 1111-1111 1111",  # mixed separators: not one consistent layout
        "3782 822463\n10005",  # a wrap outside two 4-digit groups
        "4111\n1111\n1111\n1111",  # a column of 4-digit numbers: more than one wrap
        "Order 1234 Ref 5678 Store 9012 Till 3456",
        "Total 1,234.56 Paid 1,234.56",
    ],
)
def test_amounts_phones_and_mixed_layouts_are_not_redacted(text: str) -> None:
    out = render_section("receipt.pdf", text, max_chars=1000)

    assert out is not None
    assert "REDACTED" not in out


def test_no_random_amount_table_loses_text() -> None:
    """rev-117: on 2,000 random four-row tables the windowed rule redacted 1,968."""
    import random

    rng = random.Random(117)
    for _ in range(2000):
        rows = [
            f"{rng.randint(1, 99)} {rng.randint(0, 99)}.{rng.randint(0, 99):02d} "
            f"{rng.randint(0, 999)}.{rng.randint(0, 99):02d}"
            for _ in range(4)
        ]
        table = "\n".join(rows)

        out = render_section("receipt.pdf", table, max_chars=1000)

        assert out is not None and "REDACTED" not in out, table


@pytest.mark.parametrize(
    "text",
    [
        "Qty Price Amount\n2 4.99 9.98\n1 12.50 12.50\n3 1.00 3.00\n",
        "1 1299 12.99\n2 450 9.00\n1 375 3.75\n",
        "Tel 555 0100 1234 567",
    ],
)
def test_rev117_ordinary_numbers_come_back_unchanged(text: str) -> None:
    """rev-117's repros, exactly: the section body is the text itself, nothing redacted."""
    out = render_section("receipt.pdf", text, max_chars=1000)

    assert out == f"## Attachment: receipt.pdf\n\n{text.strip()}"


@pytest.mark.parametrize(
    "layout",
    [
        "4111 1111  1111 1111",  # I-r3-2a: uneven spacing is one kind of separator
        "4111  1111 1111   1111",
        "4111-1111-\n1111-1111",  # I-r3-2b: a hyphen, then the one allowed wrap
    ],
)
def test_uneven_spacing_and_a_hyphen_wrap_are_still_one_layout(layout: str) -> None:
    out = render_section("statement.pdf", f"Card {layout} paid", max_chars=1000)

    assert out is not None
    assert "REDACTED:card-number" in out and "4111" not in out


@pytest.mark.parametrize("mixed", ["4111 1111-1111 1111", "4111 1111.1111 1111"])
def test_space_mixed_with_a_hyphen_or_dot_stays_a_residual(mixed: str) -> None:
    out = render_section("statement.pdf", mixed, max_chars=1000)

    assert out is not None and "REDACTED" not in out
