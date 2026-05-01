"""Tests for brain.vault.frontmatter — YAML frontmatter writer + reader."""
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from brain.vault.frontmatter import (
    dump_frontmatter,
    parse_frontmatter,
    rewrite_tags,
)


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


# ---------------------------------------------------------------------------
# rewrite_tags — Phase 0 of the brain-tag-frontmatter plan
# ---------------------------------------------------------------------------


def _seed_vault_file(
    tmp_path: Path,
    fields: dict[str, object],
    body: str,
    *,
    name: str = "note.md",
) -> Path:
    """Helper: write a vault file with ``fields`` + ``body`` and return its path."""
    target = tmp_path / name
    target.write_text(dump_frontmatter(fields, body), encoding="utf-8")
    return target


def test_rewrite_tags_replaces_empty_list_with_populated_list(tmp_path: Path) -> None:
    # Setup: file with tags: [] (the post-sync "never tagged" state).
    path = _seed_vault_file(
        tmp_path,
        {
            "id": "abc",
            "title": "Sample",
            "tags": [],
            "updated": "2020-01-01T00:00:00+00:00",
        },
        "body line\n",
    )

    # Exercise.
    changed = rewrite_tags(path, ["career", "company-id"])

    # Verify: returns True, file now has the new tags.
    assert changed is True
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields["tags"] == ["career", "company-id"]
    assert body == "body line\n"


def test_rewrite_tags_replaces_existing_list_with_new_list(tmp_path: Path) -> None:
    # Setup.
    path = _seed_vault_file(
        tmp_path,
        {"id": "abc", "title": "S", "tags": ["old1", "old2"]},
        "b\n",
    )

    # Exercise.
    changed = rewrite_tags(path, ["new1", "new2", "new3"])

    # Verify.
    assert changed is True
    fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields["tags"] == ["new1", "new2", "new3"]


def test_rewrite_tags_adds_tags_when_key_missing(tmp_path: Path) -> None:
    # Setup: no tags: key at all.
    path = _seed_vault_file(
        tmp_path,
        {"id": "abc", "title": "S"},
        "b\n",
    )
    assert "tags:" not in path.read_text(encoding="utf-8")

    # Exercise.
    changed = rewrite_tags(path, ["career"])

    # Verify.
    assert changed is True
    fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields["tags"] == ["career"]


def test_rewrite_tags_is_idempotent_when_tags_already_match(tmp_path: Path) -> None:
    # Setup: seed once with the desired tags, then read the on-disk bytes.
    path = _seed_vault_file(
        tmp_path,
        {
            "id": "abc",
            "title": "S",
            "tags": ["career", "company-id"],
            "updated": "2020-01-01T00:00:00+00:00",
        },
        "b\n",
    )
    before_bytes = path.read_bytes()

    # Exercise.
    changed = rewrite_tags(path, ["career", "company-id"])

    # Verify: returns False, file is byte-for-byte identical.
    assert changed is False
    assert path.read_bytes() == before_bytes


def test_rewrite_tags_bumps_updated_only_when_change_occurs(tmp_path: Path) -> None:
    # Setup: file has a known-old `updated:` timestamp.
    old_updated = "2020-01-01T00:00:00+00:00"
    path = _seed_vault_file(
        tmp_path,
        {
            "id": "abc",
            "title": "S",
            "tags": ["original"],
            "updated": old_updated,
        },
        "b\n",
    )
    before = datetime.now(UTC)

    # Exercise (1): a real change.
    changed = rewrite_tags(path, ["different"])

    # Verify: updated bumped to a fresh UTC timestamp >= before.
    assert changed is True
    fields_after_change, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    new_updated = fields_after_change["updated"]
    assert new_updated != old_updated
    parsed_new = datetime.fromisoformat(new_updated)
    assert parsed_new >= before

    # Exercise (2): a no-op call with the same tags.
    changed_again = rewrite_tags(path, ["different"])

    # Verify: updated is unchanged from after the first call.
    assert changed_again is False
    fields_after_noop, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields_after_noop["updated"] == new_updated


def test_rewrite_tags_preserves_other_frontmatter_keys(tmp_path: Path) -> None:
    # Setup: realistic vault-tier frontmatter with the canonical key set.
    fields = {
        "id": "11111111-2222-3333-4444-555555555555",
        "title": "person-x conversation",
        "created": "2026-04-01T10:00:00+00:00",
        "kind": "vault",
        "content_type": "note",
        "tags": [],
    }
    path = _seed_vault_file(tmp_path, fields, "Some body.\n")

    # Exercise.
    changed = rewrite_tags(path, ["career", "company-id"])

    # Verify: all other keys retain identical values.
    assert changed is True
    fields_after, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    for key in ("id", "title", "created", "kind", "content_type"):
        assert fields_after[key] == fields[key], (
            f"{key} unexpectedly changed: {fields_after[key]!r} != {fields[key]!r}"
        )
    assert fields_after["tags"] == ["career", "company-id"]


def test_rewrite_tags_raises_filenotfounderror_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(FileNotFoundError):
        rewrite_tags(missing, ["foo"])


def test_rewrite_tags_round_trips_unicode_titles(tmp_path: Path) -> None:
    # Setup: title contains accented + CJK characters; allow_unicode
    # must keep them intact through the rewrite.
    title = "person-x Q1 réview"
    path = _seed_vault_file(
        tmp_path,
        {"id": "abc", "title": title, "tags": []},
        "body\n",
    )

    # Exercise.
    changed = rewrite_tags(path, ["réview"])

    # Verify: title and the tag with a unicode char both survive.
    assert changed is True
    fields_after, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields_after["title"] == title
    assert fields_after["tags"] == ["réview"]
