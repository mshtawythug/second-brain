"""Tests for brain.vault.frontmatter — YAML frontmatter writer + reader."""
import pytest
import yaml

from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter


def test_round_trip_preserves_fields_and_body() -> None:
    fields = {
        "id": "7c2a8b9f-3d4e-4f5a-9b8c-1d2e3f4a5b6c",
        "title": "person-x conversation",
        "tags": ["career", "company-id"],
    }
    body = "First paragraph.\n\nSecond paragraph.\n"
    text = dump_frontmatter(fields, body)
    parsed_fields, parsed_body = parse_frontmatter(text)
    assert parsed_fields == fields
    assert parsed_body == body


def test_writer_preserves_field_ordering() -> None:
    fields = {"z_last": 1, "a_first": 2, "m_middle": 3}
    text = dump_frontmatter(fields, "")
    # YAML lines after the opening fence; insertion order must be preserved.
    yaml_lines = text.split("---\n", 2)[1].splitlines()
    keys_in_order = [line.split(":", 1)[0] for line in yaml_lines if ":" in line]
    assert keys_in_order == ["z_last", "a_first", "m_middle"]


def test_writer_starts_and_ends_frontmatter_with_fence() -> None:
    text = dump_frontmatter({"id": "abc"}, "body")
    assert text.startswith("---\n")
    # Fence followed by body (single blank line in between).
    assert "\n---\n\nbody" in text


def test_writer_handles_unicode_titles() -> None:
    fields = {"id": "x", "title": "person-x Q1 réview — 中文"}
    text = dump_frontmatter(fields, "")
    parsed_fields, _ = parse_frontmatter(text)
    assert parsed_fields["title"] == "person-x Q1 réview — 中文"


def test_writer_handles_empty_body() -> None:
    fields = {"id": "x", "title": "t"}
    text = dump_frontmatter(fields, "")
    parsed_fields, parsed_body = parse_frontmatter(text)
    assert parsed_fields == fields
    assert parsed_body == ""


def test_parser_handles_file_without_frontmatter() -> None:
    fields, body = parse_frontmatter("Just plain text, no fences.\n")
    assert fields == {}
    assert body == "Just plain text, no fences.\n"


def test_parser_handles_unclosed_frontmatter() -> None:
    # Opening fence but no closing — treat as bare body.
    fields, body = parse_frontmatter("---\nid: x\nstill no closing fence\n")
    assert fields == {}
    assert body == "---\nid: x\nstill no closing fence\n"


def test_parser_rejects_non_mapping_frontmatter() -> None:
    text = "---\n- bare\n- list\n---\nbody\n"
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_frontmatter(text)


def test_parser_propagates_yaml_errors() -> None:
    text = "---\nfoo: [unclosed\n---\nbody\n"
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter(text)


def test_writer_preserves_list_values() -> None:
    fields = {"id": "x", "tags": ["a", "b", "c"]}
    text = dump_frontmatter(fields, "body")
    parsed_fields, _ = parse_frontmatter(text)
    assert parsed_fields["tags"] == ["a", "b", "c"]


def test_parser_strips_one_leading_blank_line_from_body() -> None:
    # Writer convention: blank line between closing fence and body. Parser
    # peels exactly that one blank back so the round-trip is identity.
    text = "---\nid: x\n---\n\nactual body line\n"
    _, body = parse_frontmatter(text)
    assert body == "actual body line\n"


def test_parser_keeps_intentional_double_blank_after_fence() -> None:
    # If the user actually wants a blank line as the first line of the body,
    # we'd see two blanks after the fence; we only strip one.
    text = "---\nid: x\n---\n\n\nthe body\n"
    _, body = parse_frontmatter(text)
    assert body == "\nthe body\n"


def test_empty_yaml_body_yields_empty_dict() -> None:
    fields, body = parse_frontmatter("---\n---\nbody\n")
    assert fields == {}
    assert body == "body\n"


def test_parser_handles_crlf_line_endings() -> None:
    """Files saved on Windows / by older editors come in with ``\\r\\n``."""
    text = "---\r\nid: abc\r\ntitle: hi\r\n---\r\n\r\nbody line\r\n"
    fields, body = parse_frontmatter(text)
    assert fields == {"id": "abc", "title": "hi"}
    # The single ``\r\n`` separator after the closing fence is consumed; the
    # body keeps whatever line endings the user wrote.
    assert body == "body line\r\n"


def test_parser_handles_empty_input() -> None:
    fields, body = parse_frontmatter("")
    assert fields == {}
    assert body == ""
