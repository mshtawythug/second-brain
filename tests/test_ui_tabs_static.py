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


def _section(html: str, name: str) -> str:
    """The opening tag and body of ``<section id="tab-<name>">``."""
    (body,) = re.findall(rf'(<section[^>]*\bid="tab-{name}"[^>]*>.*?</section>)', html, re.S)
    return body


def test_the_ingest_pane_is_a_real_tab_not_a_deferred_one() -> None:
    """RM-11: ``#tab-ingest`` stops being the "Deferred" placeholder."""
    ingest = _section(_INDEX, "ingest")
    opening = ingest.split(">", 1)[0]

    assert "deferred-tab" not in opening
    assert "Deferred to a later release" not in ingest
    assert '<form class="ingest-form" id="ingest-form"' in ingest
    # Labels on every control: each id the form submits has a <label for=...>.
    for control in ("ingest-title", "ingest-body", "ingest-tags", "ingest-content-type",
                    "ingest-no-enrich", "ingest-mode-paste", "ingest-mode-file",
                    "ingest-file", "ingest-file-title"):
        assert f'id="{control}"' in ingest
        assert f'for="{control}"' in ingest, f"{control} has no <label for>"


def test_the_agent_pane_is_still_deferred() -> None:
    agent = _section(_INDEX, "agent")

    assert 'class="shell deferred-tab"' in agent.split(">", 1)[0]
    assert "Deferred to a later release" in agent


def test_file_mode_has_a_real_button_for_the_drop_zone() -> None:
    """RM-13: dropping is never the only way in; the button opens the file input."""
    ingest = _section(_INDEX, "ingest")

    assert re.search(r'<button[^>]*\bid="ingest-drop"[^>]*type="button"', ingest)
    assert re.search(r'<input type="file"[^>]*\bid="ingest-file"[^>]*tabindex="-1"', ingest)
    # File mode stays unavailable until the config offers an extension.
    assert re.search(r'id="ingest-mode-file"[^>]*\bdisabled', ingest)
    assert re.search(r'id="ingest-file-fields"[^>]*\bhidden', ingest)
