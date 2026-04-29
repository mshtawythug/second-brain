"""Unit tests for ``brain.vault.templates`` — render + listing helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.vault.templates import (
    DAILY_TEMPLATE,
    NOTE_TEMPLATE,
    list_template_names,
    render_template,
)


def test_render_substitutes_known_placeholders() -> None:
    out = render_template("# {{title}}", {"title": "person-x Q1"})
    assert out == "# person-x Q1"


def test_render_substitutes_multiple_placeholders() -> None:
    out = render_template(
        "title: {{title}}\ndate: {{date}}\nslug: {{slug}}",
        {"title": "T", "date": "2026-04-29", "slug": "t"},
    )
    assert out == "title: T\ndate: 2026-04-29\nslug: t"


def test_render_tolerates_inner_whitespace() -> None:
    out = render_template("{{ title }}", {"title": "X"})
    assert out == "X"


def test_render_leaves_unknown_placeholders_alone() -> None:
    """Per spec: unknown placeholders are not an error — left as-is."""
    out = render_template("{{title}} | {{unknown}}", {"title": "T"})
    assert out == "T | {{unknown}}"


def test_render_empty_input() -> None:
    assert render_template("", {"title": "X"}) == ""


def test_render_no_substitutions() -> None:
    """Templates without placeholders pass through unchanged."""
    body = "static body with no markers"
    assert render_template(body, {"title": "X"}) == body


def test_render_bundled_note_template() -> None:
    """Round-trip the bundled note template via the renderer."""
    out = render_template(NOTE_TEMPLATE, {"title": "Hello"})
    assert "# Hello" in out
    assert "title: \"Hello\"" in out


def test_render_bundled_daily_template() -> None:
    out = render_template(DAILY_TEMPLATE, {"date": "2026-04-29"})
    assert "title: \"2026-04-29\"" in out
    assert "# 2026-04-29" in out


def test_render_does_not_perform_double_substitution() -> None:
    """A value that itself contains ``{{x}}`` is not re-rendered."""
    out = render_template("{{a}}", {"a": "{{b}}", "b": "BOOM"})
    assert out == "{{b}}"


def test_list_template_names_returns_sorted_basenames(tmp_path: Path) -> None:
    templates = tmp_path / "_templates"
    templates.mkdir()
    (templates / "note.md").write_text("body")
    (templates / "daily.md").write_text("body")
    (templates / "weekly.md").write_text("body")
    # A non-md file should be ignored.
    (templates / "notes.txt").write_text("ignored")
    # A subdirectory should be ignored.
    (templates / "sub").mkdir()

    # Returns stems (no .md extension) — that's what the CLI's --template
    # arg accepts.
    assert list_template_names(tmp_path) == ["daily", "note", "weekly"]


def test_list_template_names_returns_empty_when_no_templates_dir(
    tmp_path: Path,
) -> None:
    """A vault that hasn't been initialized yet — empty list, no error."""
    assert list_template_names(tmp_path) == []


def test_list_template_names_returns_empty_when_dir_is_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "_templates").mkdir()
    assert list_template_names(tmp_path) == []


@pytest.mark.parametrize(
    "name",
    ["1weird", "with space", "with-dash", "with.dot"],
)
def test_render_invalid_identifier_placeholders_pass_through(name: str) -> None:
    """``{{name}}`` requires a Python identifier; everything else is literal."""
    text = "{{" + name + "}}"
    out = render_template(text, {name: "should-not-substitute"})
    assert out == text
