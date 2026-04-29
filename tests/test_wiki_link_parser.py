"""Unit tests for brain.vault.links.parse_wiki_links.

Pure parser — no DB, no fixtures. Each test pins one row of the spec's
resolution table or one skip rule.
"""
from brain.vault.links import ParsedLink, parse_wiki_links


def _kinds(text: str) -> list[tuple[str, str, str]]:
    """Compact projection used in many of the tests below."""
    return [(p.target_type, p.target_value, p.kind) for p in parse_wiki_links(text)]


# ---------------------------------------------------------------------------
# Pattern table coverage.
# ---------------------------------------------------------------------------


def test_plain_title_link() -> None:
    [link] = parse_wiki_links("Hello [[person-x conversation]] world")
    assert link == ParsedLink(
        raw="[[person-x conversation]]",
        kind="wiki",
        target_type="title",
        target_value="person-x conversation",
        target_source=None,
        display_text=None,
        heading=None,
    )


def test_pipe_alias() -> None:
    [link] = parse_wiki_links("[[person-x conversation|person-x]]")
    assert link.target_value == "person-x conversation"
    assert link.display_text == "person-x"
    assert link.kind == "wiki"


def test_heading_anchor() -> None:
    [link] = parse_wiki_links("[[person-x#March meeting]]")
    assert link.target_value == "person-x"
    assert link.heading == "March meeting"
    assert link.display_text is None


def test_heading_and_alias_combined() -> None:
    [link] = parse_wiki_links("[[person-x#March meeting|that one]]")
    assert link.target_value == "person-x"
    assert link.heading == "March meeting"
    assert link.display_text == "that one"


def test_embed_marker() -> None:
    [link] = parse_wiki_links("![[person-a]]")
    assert link.kind == "embed"
    assert link.target_value == "person-a"
    assert link.target_type == "title"


def test_embed_with_alias_and_heading() -> None:
    [link] = parse_wiki_links("![[person-x#section|alt]]")
    assert link.kind == "embed"
    assert link.target_value == "person-x"
    assert link.heading == "section"
    assert link.display_text == "alt"


def test_brain_id_prefix() -> None:
    [link] = parse_wiki_links("see [[brain:7c2a8b]] for details")
    assert link == ParsedLink(
        raw="[[brain:7c2a8b]]",
        kind="wiki",
        target_type="doc-id",
        target_value="7c2a8b",
        target_source=None,
        display_text=None,
        heading=None,
    )


def test_krisp_external_id() -> None:
    [link] = parse_wiki_links("see [[krisp:abc123]]")
    assert link.target_type == "source-external"
    assert link.target_source == "krisp"
    assert link.target_value == "abc123"


def test_slack_dotted_external_id() -> None:
    [link] = parse_wiki_links("[[slack:1234.5678]]")
    assert link.target_source == "slack"
    assert link.target_value == "1234.5678"


def test_gmail_external_id() -> None:
    [link] = parse_wiki_links("[[gmail:msg-id-with-dashes]]")
    assert link.target_source == "gmail"
    assert link.target_value == "msg-id-with-dashes"


def test_manual_external_id() -> None:
    [link] = parse_wiki_links("[[manual:foo]]")
    assert link.target_source == "manual"
    assert link.target_value == "foo"


def test_unknown_prefix_falls_back_to_title() -> None:
    """Obsidian allows colons inside titles. Unknown prefixes stay as titles."""
    [link] = parse_wiki_links("[[notion:something]]")
    assert link.target_type == "title"
    assert link.target_value == "notion:something"
    assert link.target_source is None


# ---------------------------------------------------------------------------
# Skip rules — code fences, inline code, escapes.
# ---------------------------------------------------------------------------


def test_links_inside_fenced_code_blocks_are_skipped() -> None:
    text = """
Outside [[Visible]] link.
```
inside [[Hidden]] code
```
After [[AlsoVisible]].
"""
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert "Visible" in titles
    assert "AlsoVisible" in titles
    assert "Hidden" not in titles


def test_links_inside_tilde_fenced_code_blocks_are_skipped() -> None:
    text = """
~~~
[[Hidden]]
~~~
[[Visible]]
"""
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["Visible"]


def test_links_inside_inline_code_are_skipped() -> None:
    text = "Plain [[Real]] then `[[Fake]]` then [[AlsoReal]]."
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["Real", "AlsoReal"]


def test_links_inside_indented_code_are_skipped() -> None:
    text = "Outside [[A]]\n\n    [[B]] in indented block\n\nNot indented [[C]]"
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["A", "C"]


def test_escaped_brackets_are_literal() -> None:
    text = r"\[[NotALink]] but [[RealLink]] is."
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["RealLink"]


def test_escaped_embed_brackets_are_literal() -> None:
    text = r"\![[NotEmbed]] [[Real]]"
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["Real"]


def test_double_backslash_does_not_escape() -> None:
    """``\\\\[[X]]`` = literal backslash + real link."""
    text = r"\\[[Real]]"
    [link] = parse_wiki_links(text)
    assert link.target_value == "Real"


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_empty_brackets_silently_ignored() -> None:
    assert parse_wiki_links("[[]]") == []
    assert parse_wiki_links("[[   ]]") == []


def test_first_pipe_is_alias_separator() -> None:
    """``[[X|Y|Z]]`` — only the first ``|`` separates target from alias."""
    [link] = parse_wiki_links("[[X|Y|Z]]")
    assert link.target_value == "X"
    assert link.display_text == "Y|Z"


def test_links_in_document_order() -> None:
    text = "[[A]] then [[B]] then ![[C]] then [[D]]"
    values = [p.target_value for p in parse_wiki_links(text)]
    assert values == ["A", "B", "C", "D"]


def test_multiple_links_on_one_line() -> None:
    text = "[[A]][[B]][[C]]"
    values = [p.target_value for p in parse_wiki_links(text)]
    assert values == ["A", "B", "C"]


def test_no_links_returns_empty_list() -> None:
    assert parse_wiki_links("") == []
    assert parse_wiki_links("Just plain text.") == []


def test_links_at_file_boundaries() -> None:
    """Link at start, link at end, no surrounding context."""
    [start] = parse_wiki_links("[[Start]]")
    assert start.target_value == "Start"
    [end] = parse_wiki_links("text [[End]]")
    assert end.target_value == "End"


def test_link_text_with_whitespace_trimmed() -> None:
    [link] = parse_wiki_links("[[  spaced title  ]]")
    assert link.target_value == "spaced title"


def test_brain_prefix_with_full_uuid() -> None:
    [link] = parse_wiki_links("[[brain:7c2a8b9f-3d4e-4f5a-9b8c-1d2e3f4a5b6c]]")
    assert link.target_type == "doc-id"
    assert link.target_value == "7c2a8b9f-3d4e-4f5a-9b8c-1d2e3f4a5b6c"


def test_only_target_no_pipe_no_heading() -> None:
    [link] = parse_wiki_links("[[just-a-title]]")
    assert link.heading is None
    assert link.display_text is None


def test_lone_pipe_is_skipped() -> None:
    """`[[|Display]]` has empty target — silently ignored."""
    assert parse_wiki_links("[[|Display]]") == []


def test_heading_only_has_no_target() -> None:
    """`[[#heading-only]]` — no title before the ``#``, skipped."""
    assert parse_wiki_links("[[#heading]]") == []


def test_non_link_brackets_ignored() -> None:
    text = "single [brackets] don't count, neither do [[no-end."
    assert parse_wiki_links(text) == []


def test_kinds_helper_smoke() -> None:
    """Compact-projection helper used by other tests works as expected."""
    text = "[[A]] ![[B]] [[brain:abc123]] [[krisp:c1]]"
    assert _kinds(text) == [
        ("title", "A", "wiki"),
        ("title", "B", "embed"),
        ("doc-id", "abc123", "wiki"),
        ("source-external", "c1", "wiki"),
    ]


def test_inline_code_does_not_leak_across_lines() -> None:
    """Unbalanced backtick on one line doesn't disable links on the next."""
    text = "left ` open backtick line\n[[NextLineLink]]"
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert "NextLineLink" in titles


def test_fenced_block_state_resets_at_close() -> None:
    text = """
```
[[Hidden]]
```
[[After]]
"""
    titles = [p.target_value for p in parse_wiki_links(text)]
    assert titles == ["After"]
