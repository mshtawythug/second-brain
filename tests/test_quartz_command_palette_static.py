"""Static checks for the Phase 5.3 command palette finish."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_ROOT = REPO_ROOT / "quartz_overrides" / "quartz"
COMPONENTS_DIR = OVERRIDES_ROOT / "components"
COMMAND_TSX = COMPONENTS_DIR / "CommandPalette.tsx"
COMMAND_INLINE = COMPONENTS_DIR / "scripts" / "commandPalette.inline.ts"
SEARCH_INLINE = COMPONENTS_DIR / "scripts" / "search.inline.ts"
COMMAND_SCSS = OVERRIDES_ROOT / "styles" / "brain" / "_command_palette.scss"
CMDK_SCSS = OVERRIDES_ROOT / "styles" / "brain" / "_cmdk.scss"
CUSTOM_SCSS = OVERRIDES_ROOT / "styles" / "custom.scss"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing expected file: {path}"
    return path.read_text(encoding="utf-8")


def test_command_palette_component_renders_source_chip_rail() -> None:
    text = _read(COMMAND_TSX)

    assert 'from "../util/sourceIcons"' in text
    assert "SOURCE_ICONS" in text
    assert "SOURCE_CHIP_ORDER" in text
    assert 'class="brain-cmdk-chips"' in text
    assert 'data-brain-source="__all__"' in text
    assert "data-brain-source={value}" in text
    assert "CommandPalette.afterDOMLoaded = script" in text
    assert 'aria-pressed="true"' in text


def test_command_palette_runtime_uses_cmd_p_not_cmd_k() -> None:
    text = _read(COMMAND_INLINE)

    assert 'event.key.toLowerCase() === "p"' in text
    assert 'event.key.toLowerCase() === "k"' not in text
    assert "isCmdP" in text
    assert "isCmdK" not in text
    assert "window.fetchData" not in text
    assert "const data = await fetchData" in text


def test_search_runtime_keeps_cmd_k_full_search() -> None:
    text = _read(SEARCH_INLINE)

    assert 'e.key.toLowerCase() === "k"' in text
    assert "(e.ctrlKey || e.metaKey) && !e.shiftKey" in text


def test_command_palette_runtime_filters_by_shared_sources() -> None:
    text = _read(COMMAND_INLINE)

    assert 'ACTIVE_SOURCES_KEY = "brain.commandPalette.activeSources"' in text
    assert "localStorage.setItem(ACTIVE_SOURCES_KEY" in text
    assert "localStorage.getItem(ACTIVE_SOURCES_KEY)" in text
    assert 'from "../../util/sourceIcons"' in text
    assert "SOURCE_CHIP_ORDER" in text
    assert "inferSource" in text
    assert "sourceIconFor" in text
    assert "passesSourceFilter" in text
    assert ".brain-cmdk-chip" in text
    assert "aria-pressed" in text
    assert 'data-brain-source="__all__"' not in text


def test_command_palette_styles_include_chip_rail() -> None:
    text = _read(COMMAND_SCSS)
    custom = _read(CUSTOM_SCSS)

    for selector in (
        ".brain-cmdk-chips",
        ".brain-cmdk-chip",
        ".brain-cmdk-chip-icon",
        ".brain-cmdk-chip-label",
    ):
        assert selector in text
    assert '@use "./brain/command_palette"' in custom
    assert '@use "./command_palette"' in _read(CMDK_SCSS)
