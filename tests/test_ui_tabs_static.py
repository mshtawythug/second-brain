"""Phase 8 T6 (Q10, E10): the Publish tab is gone, and the tab strip still adds up.

The user decided Q10 as "delete it": a tab describing a retired subsystem is worse
than no tab. Three things wire a tab, and they must agree. They are the
``.tab`` buttons in ``index.html``, the ``tab-<name>`` sections they reveal, and
the name list ``main.js``'s ``wireTabs`` loops over. A name left in that loop
with no section makes EVERY tab click throw on ``$("tab-<name>")`` being null,
and since "publish" was the last name, the panes would still switch first.
``tests/test_ui_browser_tabs.py`` catches that in a real browser; this module
catches it from source, so a run without Playwright still sees it.
"""

from __future__ import annotations

import re

import brain.ui
from brain.ui import notes_service
from brain.ui.app import static_dir

_INDEX = (static_dir() / "index.html").read_text(encoding="utf-8")
_MAIN_JS = (static_dir() / "js" / "main.js").read_text(encoding="utf-8")


def tab_buttons(html: str) -> list[str]:
    return re.findall(r'<button class="tab[^"]*" data-tab="([a-z]+)"', html)


def tab_sections(html: str) -> list[str]:
    return re.findall(r'<(?:main|section)[^>]*\bid="tab-([a-z]+)"', html)


def wired_tab_names(js: str) -> list[str]:
    """The names ``wireTabs`` toggles: ``for (const name of ["a", "b", ...])``."""
    body = js.split("function wireTabs()", 1)[1]
    (names,) = re.findall(r"for \(const name of \[([^\]]*)\]\)", body.split("\n}\n", 1)[0])
    return re.findall(r'"([a-z]+)"', names)


def test_the_parsers_see_the_tab_strip() -> None:
    """Positive control: each reader finds the surviving tabs, so "absent" means absent."""
    assert tab_buttons(_INDEX) == ["notes", "ingest", "agent"]
    assert tab_sections(_INDEX) == ["notes", "ingest", "agent"]
    assert wired_tab_names(_MAIN_JS) == ["notes", "ingest", "agent"]


def test_there_is_no_publish_tab() -> None:
    assert 'data-tab="publish"' not in _INDEX
    assert 'id="tab-publish"' not in _INDEX
    assert '"publish"' not in _MAIN_JS


def test_every_wired_name_has_a_section_and_every_button_a_pane() -> None:
    sections = set(tab_sections(_INDEX))

    assert set(wired_tab_names(_MAIN_JS)) <= sections, "wireTabs names a missing section"
    assert set(tab_buttons(_INDEX)) <= sections, "a tab button has no pane"
    assert set(tab_buttons(_INDEX)) == set(wired_tab_names(_MAIN_JS))


def test_no_docstring_still_describes_the_publish_tab() -> None:
    assert "Publish" not in (brain.ui.__doc__ or "")
    assert "Publish tab" not in (notes_service.set_draft.__doc__ or "")
