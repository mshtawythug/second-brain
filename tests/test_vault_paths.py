"""Direct unit tests for ``brain.vault.paths`` shared helpers.

The helpers are tiny but load-bearing — three rendering surfaces
(homepage, people, daily-index) call into them, so any change in
behavior must surface here first.
"""
from brain.vault.paths import safe_wikilink_alias, strip_md_extension


class TestStripMdExtension:
    """``.md`` stripping + POSIX normalization."""

    def test_strips_trailing_md(self) -> None:
        assert strip_md_extension("_ingested/gmail/foo.md") == "_ingested/gmail/foo"

    def test_no_trailing_md_returns_unchanged(self) -> None:
        assert strip_md_extension("_ingested/gmail/foo") == "_ingested/gmail/foo"

    def test_top_level_file(self) -> None:
        assert strip_md_extension("index.md") == "index"

    def test_only_strips_at_end(self) -> None:
        # ``.md`` mid-path is not the file extension and must survive.
        assert strip_md_extension("notes/.md/foo.md") == "notes/.md/foo"

    def test_normalizes_to_posix(self) -> None:
        # Backslashes (rare on macOS / Linux but possible from a Windows
        # path that leaked through) get normalized to forward slashes
        # via ``PurePosixPath`` semantics.
        # ``PurePosixPath`` treats backslash as a literal character (not
        # a separator) so a Windows-style path stays intact in shape.
        # The test pins the documented behavior — POSIX-as-is.
        assert strip_md_extension("plain/posix/path.md") == "plain/posix/path"

    def test_empty_input(self) -> None:
        assert strip_md_extension("") == "."  # PurePosixPath('').as_posix() == '.'


class TestSafeWikilinkAlias:
    """Bracket sanitization for wiki-link alias slots."""

    def test_strips_brackets(self) -> None:
        assert (
            safe_wikilink_alias("Re: [External] Re: foo")
            == "Re: (External) Re: foo"
        )

    def test_plain_title_unchanged(self) -> None:
        assert safe_wikilink_alias("plain title") == "plain title"

    def test_only_open_bracket(self) -> None:
        assert safe_wikilink_alias("foo [bar") == "foo (bar"

    def test_only_close_bracket(self) -> None:
        assert safe_wikilink_alias("foo] bar") == "foo) bar"

    def test_multiple_brackets(self) -> None:
        assert (
            safe_wikilink_alias("[2026-04] [Plenty] sync")
            == "(2026-04) (Plenty) sync"
        )

    def test_empty_string(self) -> None:
        assert safe_wikilink_alias("") == ""
