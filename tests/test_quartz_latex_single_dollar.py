"""Static contract test for the Quartz latex overlay (single-dollar math OFF).

Per CLAUDE.md the project ships no JS test harness for the
``quartz_overrides/`` overlay; the established pattern is a static source
check (see ``tests/test_quartz_contentindex_draft_filter.py``). This file is
the same flavor, scoped to one contract.

Background. Upstream Quartz calls ``remarkMath`` with no options, and
micromark-extension-math defaults ``singleDollarTextMath`` to true. A single
``$`` therefore opens an inline math span that, like a code span, runs across
newlines to the next ``$`` in the paragraph. In a knowledge base full of prose
about money and almost devoid of mathematics, that swallowed the text between
any two dollar amounts and rendered it in KaTeX math-italic: 551
``<span class="katex">`` spans across 171 published pages, the longest 278
characters of running prose. The visible build-log symptom was a repeating
``No character metrics for '<c>' in style 'Main-Regular'`` warning, emitted
whenever a swallowed span contained a character missing from KaTeX's
Main-Regular font.

Limitation: this asserts the OPTION IS WIRED in the overlay source. A true
end-to-end check would build a fixture vault with ``npx quartz build`` and
parse the emitted HTML for ``<span class="katex">``, which needs npx plus a
Quartz workspace and lives behind the ``e2e`` marker. The overlay parse-smoke
(``test_quartz_overrides_parse.py``) covers gross syntax on this file
automatically.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_ROOT = REPO_ROOT / "src" / "brain" / "quartz_overrides"
LATEX_PATH = (
    OVERRIDES_ROOT / "quartz" / "plugins" / "transformers" / "latex.ts"
)
CONFIG_PATH = OVERRIDES_ROOT / "quartz.config.ts"


@pytest.fixture(scope="module")
def latex_source() -> str:
    """Read the overlay latex transformer once per module."""
    assert LATEX_PATH.is_file(), (
        f"missing overlay latex transformer at {LATEX_PATH}. Without this file "
        "the build falls back to upstream's, which enables single-dollar math."
    )
    return LATEX_PATH.read_text(encoding="utf-8")


def test_overlay_disables_single_dollar_text_math(latex_source: str) -> None:
    """``singleDollarTextMath`` is explicitly false.

    THE regression. Deleting the overlay file, or letting a Quartz-version bump
    overwrite it, silently restores prose-swallowing inline math -- a defect
    with no error message, only a slow drip of KaTeX font warnings.
    """
    match = re.search(
        r"singleDollarTextMath\s*:\s*(true|false)", latex_source
    )
    assert match is not None, (
        "singleDollarTextMath is not configured; remarkMath defaults it to "
        "true and single-`$` prose gets rendered as math"
    )
    assert match.group(1) == "false"


def test_remark_math_is_invoked_with_its_options(latex_source: str) -> None:
    """The option is actually PASSED to remarkMath, not merely declared.

    Guards the failure mode the previous ``strict: "ignore"`` attempt hit: a
    setting that exists in the source, reads plausibly, and is wired to
    nothing. Upstream's form is a bare ``return [remarkMath]``; a configured
    plugin must be the tuple form instead.
    """
    plugins = re.search(
        r"markdownPlugins\s*\([^)]*\)\s*\{(.*?)\n    \}", latex_source, re.DOTALL
    )
    assert plugins is not None, "could not locate markdownPlugins() in the overlay"
    body = plugins.group(1)

    assert re.search(r"\[\s*remarkMath\s*,", body), (
        "remarkMath is not called in tuple form, so no options reach it -- "
        f"markdownPlugins body was: {body.strip()!r}"
    )
    assert not re.search(r"return\s*\[\s*remarkMath\s*\]", body), (
        "overlay still has upstream's bare `return [remarkMath]`"
    )


def test_block_math_is_not_disabled(latex_source: str) -> None:
    """POSITIVE CONTROL: only INLINE single-dollar math is turned off.

    Two vault notes use ``$$`` block math. A fix that killed all math rendering
    would also make the assertion above pass, so pin that no block-math switch
    was flipped and that rehypeKatex is still wired up.
    """
    assert "singleDollarBlockMath" not in latex_source
    assert re.search(r"\[\s*rehypeKatex\s*,", latex_source), (
        "rehypeKatex is no longer wired; block math would stop rendering"
    )


def test_config_comment_no_longer_claims_strict_suppresses_the_warnings() -> None:
    """The config must not re-assert the debunked ``strict`` explanation.

    The old comment stated that ``strict: "ignore"`` suppressed the
    ``No character metrics`` warnings. It does not -- ``strict`` gates
    ``reportNonstrict`` (``unicodeTextInMathMode``), while the metrics warning
    is an unconditional ``console.warn`` in katex's ``makeSymbol``. That wrong
    comment is why the real cause went unexamined, so keep it from coming back.
    """
    config = CONFIG_PATH.read_text(encoding="utf-8")

    assert "singleDollarTextMath" in config or "latex.ts" in config, (
        "quartz.config.ts should point at the overlay transformer that carries "
        "the real fix"
    )
    latex_comment = config[max(0, config.find("Plugin.Latex(") - 1400) : config.find(
        "Plugin.Latex("
    )]
    assert not re.search(
        r"strict:\s*\"ignore\"\s+suppresses KaTeX strict-mode build warnings",
        latex_comment,
    ), "the debunked `strict` explanation is back in quartz.config.ts"
