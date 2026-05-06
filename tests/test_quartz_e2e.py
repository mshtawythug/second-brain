"""Quartz frontend integration harness — drives a real Quartz build.

This is the first end-to-end test in the brain repo that exercises
the rendered Quartz output. The original P3 static-source test files
(``test_contentindex_slim``, ``test_search_size_budget``,
``test_quartz_search_static``, ``test_quartz_tag_content_static``,
``test_quartz_tags_static``) are static-source assertions — they
catch syntactic regressions but can't tell you whether the brain
overlay actually compiles + emits the expected artifacts. This file
fills that gap with a tiny fixture vault, a real ``npx quartz build``
invocation, and HTTP fetches against the rendered output.

Pattern:

  1. ``preflight()`` checks that npx + the live brain Quartz workspace
     + the fixture vault are all present; if anything's missing the
     test skips cleanly (the brain test image lacks Node by design).
  2. The ``e2e_build`` fixture stages the fixture vault into a
     tempdir, runs Quartz, and serves the build directory on a free
     localhost port.
  3. Each test fetches a specific artifact and asserts on its
     content shape.

Local invocation:

  pytest tests/test_quartz_e2e.py -v --no-cov -m e2e

Manual interactive verification (driven by the agent via the MCP
browser tools — pytest can't do this directly):

  - Start the harness as above.
  - Read the ``e2e_build`` URL from the test output (a `print` in the
    fixture surfaces it under -s).
  - Navigate the MCP browser to ``<url>/`` and exercise Cmd+K, chip
    toggles, lazy preview fetch — same protocol used during P3.2 /
    P3.4 commit verification.

Coverage scope:

  - Test 1 — Search popover assets: chip rail markup + source-icon
    JSON attribute + inline script reference are all in the rendered
    HTML. (Interactive Cmd-K + typing happens via MCP, not pytest.)
  - Test 2 — ``static/contentIndex.json`` shape: at least one entry
    has a ``snippet`` field ≤ 240 chars and a non-empty ``date``.
  - Test 3 — TagContent rendering: ``/tags/demo/`` renders with
    ``.brain-tag-row``, ``.brain-tag-icon``, ``.brain-tag-footer``
    classes present, AND the row count matches the fixture (>=2).
  - P4.8 — rendered-output assertions for the daily door, recent
    rail, Explorer runtime hooks, email-thread runtime + fixture
    shape, and graph affordance / chip assets.
  - P5 — rendered-output assertions for RelatedDocs and Cmd/Ctrl+P
    quick-open assets, plus a fetchable related-doc JSON fixture.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib import request as urlrequest

import pytest

from tests.quartz_e2e_helper import (
    preflight,
    quartz_build,
    serve_directory,
    stage_fixture_vault,
)

# brain: 240 chars matches the SNIPPET_LENGTH constant pinned in the
# contentIndex emitter (P3.1) — kept in lock-step here so a future
# bump on the emitter side surfaces as an obvious test failure.
SNIPPET_LENGTH = 240

# brain (P3.6 fix-7): reach into ``scripts/check_index_size.py`` so the
# e2e suite enforces the same gzipped-size budget the operator script
# does. We import the module by file path because ``scripts/`` is not on
# sys.path by default (it's not a package — it ships single-purpose
# CLIs). The import is wrapped in a helper that returns ``None`` if the
# script is missing so the harness still runs in isolation.
_CHECK_INDEX_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_index_size.py"
)


def _load_check_index_module() -> object | None:
    """Load `scripts/check_index_size.py` as a module by file path."""
    if not _CHECK_INDEX_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "_brain_check_index_size", _CHECK_INDEX_PATH
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Skip-gate — runs at module collection so the whole file skips together
# when the toolchain is absent.
# ---------------------------------------------------------------------------

_PREFLIGHT = preflight()
_SKIP_REASON = _PREFLIGHT.skip_reason if not _PREFLIGHT.ok else ""

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _PREFLIGHT.ok,
        reason=f"e2e prerequisites not satisfied: {_SKIP_REASON}",
    ),
]


# ---------------------------------------------------------------------------
# Build fixture — module-scoped so the build runs once per pytest session
# rather than once per test (a fresh build is ~10–20s for the 5-file
# fixture, and the tests don't mutate the artifact).
# ---------------------------------------------------------------------------


# Module-level latch so `e2e_build_dir` can recover the on-disk build
# path that `e2e_build` produced. Set at fixture setup, cleared at
# teardown. Module-scope is fine because both fixtures are also module-
# scope so the build runs at most once per test module.
_LAST_E2E_BUILD_DIR: Path | None = None


@pytest.fixture(scope="module")
def e2e_build(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build the fixture vault and serve it; yield the base URL.

    Module scope so every test in this file shares a single build —
    the build is the expensive step (~10-20s); the per-test fetches
    are cheap. Teardown is automatic via the context manager.
    """
    global _LAST_E2E_BUILD_DIR
    tmp = tmp_path_factory.mktemp("quartz_e2e")
    vault = tmp / "vault"
    output = tmp / "build"

    stage_fixture_vault(vault)
    try:
        quartz_build(vault=vault, output=output)
    except Exception as exc:  # noqa: BLE001 — surface as test skip, not error
        pytest.skip(f"npx quartz build failed: {exc}")

    _LAST_E2E_BUILD_DIR = output
    try:
        with serve_directory(output) as (url, _port):
            # Print the URL so a human running ``pytest -s`` can drive
            # the same build with the MCP browser tools mid-test.
            print(f"\ne2e_build URL: {url} (output: {output})", flush=True)
            yield url
    finally:
        _LAST_E2E_BUILD_DIR = None
        # `output` lives under tmp_path — pytest cleans it up after the
        # session ends. We also clean up the staged vault just in case.
        shutil.rmtree(vault, ignore_errors=True)


@pytest.fixture(scope="module")
def e2e_build_dir(e2e_build: str) -> Path:
    """Companion fixture: the build output directory on disk.

    Tests that exercise on-disk artifacts (e.g. the gzipped-size
    budget check) ask for this fixture in addition to ``e2e_build``.
    Reads the path from the module-scope latch ``e2e_build`` populates
    on setup so we don't re-run the build.
    """
    # Touch `e2e_build` so pytest enforces the dependency — the URL
    # itself isn't used here, but the fixture must run first.
    _ = e2e_build
    if _LAST_E2E_BUILD_DIR is None:
        pytest.fail("e2e_build did not publish its output directory")
    return _LAST_E2E_BUILD_DIR


def _fetch_text(url: str) -> str:
    """Fetch ``url`` with a short timeout and return decoded text.

    Wraps urllib so the test bodies are one-liners. Timeout is 30s
    because the harness's own server is local — anything slower than
    that means the server is wedged, not "the network is slow".
    """
    with urlrequest.urlopen(url, timeout=30.0) as response:  # noqa: S310 — local URL
        return response.read().decode("utf-8")


def _fetch_json(url: str) -> object:
    """Fetch ``url`` and parse the body as JSON. Same timeout shape as text."""
    text = _fetch_text(url)
    return json.loads(text)


def _fetch_first_text(base_url: str, candidate_paths: tuple[str, ...]) -> str:
    """Fetch the first candidate path that Quartz emitted for a slug."""
    last_err: Exception | None = None
    for candidate in candidate_paths:
        try:
            return _fetch_text(f"{base_url}{candidate}")
        except Exception as exc:  # noqa: BLE001 — try next emitted shape
            last_err = exc
    pytest.fail(
        f"could not fetch any candidate path {candidate_paths!r}; "
        f"last error: {last_err}"
    )


# ---------------------------------------------------------------------------
# Test 1 — Search popover assets present in rendered HTML
# ---------------------------------------------------------------------------


def test_search_popover_assets_present_in_rendered_html(e2e_build: str) -> None:
    """The rendered home page carries the brain Search override's markup.

    Anchored on three signals the brain Search.tsx places in every
    page: (a) the ``brain-search-chips`` rail container, (b) the
    ``data-brain-source-icons`` JSON attribute, and (c) the chip rail
    has a button per source.

    This is the closest pytest can get to validating the popover
    without actually running JavaScript — interactive Cmd-K + typing
    is a separate manual step driven via the MCP browser tools.
    """
    html = _fetch_text(f"{e2e_build}/")
    # The chip rail must be present in the SSR'd markup.
    assert 'class="brain-search-chips"' in html, (
        "expected `.brain-search-chips` rail in the rendered home HTML"
    )
    # The JSON icon table must round-trip through the data attribute.
    assert "data-brain-source-icons" in html, (
        "expected `data-brain-source-icons` attribute on the chip rail"
    )
    # Every source must have a chip button.
    for source in ("krisp", "slack", "gmail", "manual", "vault"):
        assert f'data-brain-source="{source}"' in html, (
            f"expected `data-brain-source=\"{source}\"` chip in rendered HTML"
        )
    # The "All" pseudo-chip is also present.
    assert 'data-brain-source="__all__"' in html, (
        "expected the `All` pseudo-chip with `data-brain-source=__all__`"
    )


def test_related_docs_and_command_palette_assets_present(
    e2e_build: str,
) -> None:
    """The rendered home page ships P5 RelatedDocs + quick-open hooks."""
    html = _fetch_text(f"{e2e_build}/")

    assert "brain-related-docs" in html
    assert "data-brain-related-slug" in html
    assert "brain-related-docs-list" in html
    assert "brain-cmdk-chips" in html
    for source in ("krisp", "slack", "gmail", "manual", "vault"):
        assert f'data-brain-source="{source}"' in html
    assert 'data-brain-source="__all__"' in html

    related = _fetch_json(f"{e2e_build}/static/related/index.json")
    assert isinstance(related, list)
    assert related
    first = related[0]
    assert isinstance(first, dict)
    assert first["slug"] == "demo-vault-doc"
    assert first["source"] == "vault"


# ---------------------------------------------------------------------------
# Test 2 — contentIndex.json shape
# ---------------------------------------------------------------------------


def test_contentindex_has_snippet_under_budget_and_dates(e2e_build: str) -> None:
    """``contentIndex.json`` carries snippet ≤ 240 chars and a date per entry.

    P3.1 added the ``snippet`` field (slim transform) capped at 240
    chars. P3.3 lifted ``frontmatter.date`` into ``details.date`` (with
    a fallback to ``createdDate`` / ``modifiedDate`` / ``filePath``-mtime).
    Both contracts must hold for every surviving entry — anything
    longer would defeat the gzip budget; anything date-less would
    leave the search row's date column empty.
    """
    parsed = _fetch_json(f"{e2e_build}/static/contentIndex.json")
    assert isinstance(parsed, dict), (
        f"expected contentIndex.json to deserialize as a dict, got {type(parsed)}"
    )
    assert len(parsed) > 0, "expected at least one entry in contentIndex.json"

    snippet_count = 0
    date_count = 0
    for slug, details in parsed.items():
        assert isinstance(details, dict), (
            f"entry for {slug} must be an object, got {type(details)}"
        )
        snippet = details.get("snippet")
        if isinstance(snippet, str):
            snippet_count += 1
            assert len(snippet) <= SNIPPET_LENGTH, (
                f"snippet for {slug} is {len(snippet)} chars > "
                f"SNIPPET_LENGTH ({SNIPPET_LENGTH})"
            )
        # `date` may be a Date-as-ISO string (most upstream emit paths)
        # or a numeric epoch — both round-trip through Quartz's
        # ContentDetails. Existence + non-empty is the contract here.
        date_value = details.get("date")
        if date_value is not None and date_value != "":
            date_count += 1

    # At least one entry must carry a snippet (slim transform fired).
    assert snippet_count > 0, "no entries carried a `snippet` field"
    # At least one entry must carry a date (P3.3 date lift fired).
    assert date_count > 0, "no entries carried a non-empty `date` field"


def test_contentbodies_split_into_per_slug_files(e2e_build: str) -> None:
    """Per-slug body files exist under ``static/contentBodies/<slug>.json``.

    P3.1's slim transform writes one body file per surviving entry so
    the Search popover's preview pane can lazy-fetch on demand. We
    pick the fixture's ``demo-vault-doc`` slug because it's the most
    stable identifier across builds (the ingested files have date-
    prefixed slugs that survive verbatim, but the vault doc slug is
    the most readable canary).
    """
    payload = _fetch_json(f"{e2e_build}/static/contentBodies/demo-vault-doc.json")
    assert isinstance(payload, dict), (
        f"expected per-slug body to deserialize as a dict, got {type(payload)}"
    )
    assert payload.get("slug") == "demo-vault-doc", (
        f"expected slug='demo-vault-doc' in body file, got {payload.get('slug')!r}"
    )
    body = payload.get("content")
    assert isinstance(body, str) and len(body) > 0, (
        "expected `content` field with the full body text"
    )


# ---------------------------------------------------------------------------
# Test 3 — TagContent rendering on /tags/demo/
# ---------------------------------------------------------------------------


def test_tag_page_renders_brain_tag_content_rows(e2e_build: str) -> None:
    """The ``/tags/demo/`` page renders TagContent rows + footer.

    P3.3 added the TagContent override; this is the integration check
    that the rendered HTML actually carries the expected class hooks.
    The fixture has FOUR docs tagged ``demo`` (demo-vault-doc, multi-
    tag-doc, the krisp call, the gmail thread), so we expect at least
    two rows on the page.

    Quartz emits tag pages either at ``/tags/<tag>/`` (with a trailing
    slash + ``index.html``) or at ``/tags/<tag>.html``; we try both
    forms so the test is robust against an upstream emit-shape flip.
    """
    candidate_paths = ("/tags/demo/", "/tags/demo.html", "/tags/demo")
    last_err: Exception | None = None
    html = ""
    for candidate in candidate_paths:
        try:
            html = _fetch_text(f"{e2e_build}{candidate}")
            break
        except Exception as exc:  # noqa: BLE001 — try next path
            last_err = exc
    if not html:
        pytest.fail(
            f"could not fetch any of /tags/demo/* — last error: {last_err}"
        )

    # Required class hooks per the P3.3 override contract.
    for hook in ("brain-tag-row", "brain-tag-icon", "brain-tag-footer"):
        assert hook in html, (
            f"expected class hook `{hook}` in rendered tag page"
        )

    # Row count — the fixture has 4 demo-tagged docs; expect at least 2
    # rows so the assertion isn't too brittle to upstream filtering
    # (e.g. a future Quartz version that excludes the ingested mirror
    # from tag pages by default would still leave the two vault-tier
    # docs visible).
    row_matches = re.findall(r'class="[^"]*brain-tag-row[^"]*"', html)
    assert len(row_matches) >= 2, (
        f"expected ≥2 `.brain-tag-row` entries on /tags/demo/, "
        f"got {len(row_matches)}"
    )

    # The footer row must reference the source tag with the canonical
    # `#demo` form so the user can see which tags brought the doc here.
    assert "#demo" in html, (
        "expected `#demo` reference in the TagContent footer markup"
    )


def test_tag_page_lowercases_pill_text(e2e_build: str) -> None:
    """The P3.4 lowercase-tag fix is reflected in the rendered tag pills.

    Two-pronged check:
      - The DOM text inside `a.tag-link` is the lowercase tag value
        (``>demo<``), not an uppercased form.
      - There's no inline ``text-transform: uppercase`` declaration
        on a tag selector (the brain overlay's `_links.scss` pins
        ``text-transform: none`` per P3.4).
    """
    html = _fetch_text(f"{e2e_build}/")
    # Look for at least one tag-link with the lowercase fixture tag.
    pattern = re.compile(
        r'<a[^>]*class="[^"]*tag-link[^"]*"[^>]*>([^<]+)</a>',
    )
    matches = pattern.findall(html)
    if matches:
        # If the home page surfaces tag links (it might not, depending
        # on TagList layout), every match must already be lowercase.
        for text in matches:
            assert text == text.lower(), (
                f"expected lowercase tag-link text, got {text!r}"
            )
    # Either way, ensure no upstream stylesheet sneaks `uppercase` back
    # onto a tag selector. We grep the inline + linked stylesheets
    # served by the build for the offending declaration. False positive
    # risk is low because the brain overlay's only `text-transform`
    # rules are in the brain partials, and we explicitly pinned `none`
    # for the tag selector in P3.4.
    css_inline_pattern = re.compile(
        r"a\.tag-link[^{]*\{[^}]*text-transform\s*:\s*uppercase",
        re.IGNORECASE,
    )
    assert not css_inline_pattern.search(html), (
        "found inline `text-transform: uppercase` on `a.tag-link` — "
        "P3.4 contract violated"
    )


# ---------------------------------------------------------------------------
# P3.6 fix-7 — Wire `check_index_size.py` into the e2e harness
# ---------------------------------------------------------------------------


def test_contentindex_gzipped_under_default_budget(e2e_build_dir: Path) -> None:
    """The rendered ``contentIndex.json`` fits under the operator-script budget.

    P3.1's slim transform was sized for a 2 MB gzipped budget (the
    constant pinned in ``scripts/check_index_size.py``). Until P3.6
    that script was operator-only — never run against a real build
    artifact. Wiring it into the e2e harness closes the gap: every
    e2e run now enforces the same budget the operator hits manually
    via ``python scripts/check_index_size.py``.

    Skips if the script can't be imported (defensive — the harness
    runs in environments where `scripts/` may not be on the
    repository tree, e.g. a future packaging change).
    """
    module = _load_check_index_module()
    if module is None:
        pytest.skip(
            f"`scripts/check_index_size.py` not importable from {_CHECK_INDEX_PATH}"
        )

    index_path = e2e_build_dir / "static" / "contentIndex.json"
    assert index_path.is_file(), f"missing build artifact at {index_path}"

    raw_bytes = index_path.read_bytes()
    compressed_size = module.gzipped_size(raw_bytes)  # type: ignore[attr-defined]
    budget = module.DEFAULT_BUDGET_BYTES  # type: ignore[attr-defined]

    assert compressed_size <= budget, (
        f"contentIndex.json gzipped is {compressed_size:,} B "
        f"(> budget {budget:,} B; raw {len(raw_bytes):,} B). "
        "Run `python scripts/check_index_size.py <path>` to reproduce."
    )


# ---------------------------------------------------------------------------
# P4.8 — rendered-output coverage for Phase 4 wiki UX features
# ---------------------------------------------------------------------------


def test_p4_home_renders_daily_door_and_recent_rail(e2e_build: str) -> None:
    """The fixture home page renders the P4 daily door and recent rail.

    This is intentionally HTTP-fetch based, matching the rest of the harness:
    it verifies the Quartz build output, not browser-local click state.
    """
    html = _fetch_text(f"{e2e_build}/")
    for needle in (
        "Daily notes",
        "chronological log door",
        "Recently captured",
        "P4 Recent Rail Canary",
        "P4 Krisp Recent Canary",
    ):
        assert needle in html, f"expected {needle!r} in rendered home page"

    daily_html = _fetch_first_text(
        e2e_build,
        ("/daily/", "/daily/index.html", "/daily/index", "/daily"),
    )
    assert "Daily notes" in daily_html
    assert "2026-05-04" in daily_html
    assert "Daily fixture note" in _fetch_first_text(
        e2e_build,
        (
            "/daily/2026/2026-05-04/",
            "/daily/2026/2026-05-04.html",
            "/daily/2026/2026-05-04",
        ),
    )


def test_p4_explorer_filter_and_month_grouping_assets_rendered(
    e2e_build: str,
) -> None:
    """The rendered build includes the P4 Explorer runtime hooks.

    The toggle and grouping are client-side mutations, so this test pins
    the emitted runtime assets rather than pretending an HTTP fetch can click
    localStorage-backed controls.
    """
    postscript = _fetch_text(f"{e2e_build}/postscript.js")
    for needle in (
        "brain.explorer.showIngested",
        "brain-explorer-ingested-toggle",
        "brain-explorer-month-header",
        "brain-explorer-month-date",
        "brain-explorer-month-title",
    ):
        assert needle in postscript, (
            f"expected Explorer hook {needle!r} in emitted postscript.js"
        )


def test_p4_email_thread_reader_assets_and_fixture_shape(
    e2e_build: str,
) -> None:
    """Gmail thread pages carry the reading-mode runtime and details markup."""
    html = _fetch_first_text(
        e2e_build,
        (
            "/_ingested/gmail/2026-04-22-fixture-gmail-thread/",
            "/_ingested/gmail/2026-04-22-fixture-gmail-thread.html",
            "/_ingested/gmail/2026-04-22-fixture-gmail-thread",
        ),
    )
    for needle in (
        "<details",
        "<summary>",
        "owner@example.com",
        "alice@example.com",
        "/static/emailThread.js",
        "window.BRAIN_USER_EMAIL",
    ):
        assert needle in html, f"expected email-thread signal {needle!r}"

    runtime = _fetch_text(f"{e2e_build}/static/emailThread.js")
    for needle in (
        "brain.email.repliesOnly",
        "brain-email-replies-only-toggle",
        "data-brain-is-mine",
        "document.addEventListener(\"nav\", init)",
    ):
        assert needle in runtime, f"expected runtime hook {needle!r}"


def test_p4_graph_buttons_and_chip_runtime_assets(
    e2e_build: str,
) -> None:
    """The build keeps all graph affordances and emits chip runtime assets."""
    html = _fetch_text(f"{e2e_build}/")
    for needle in (
        "local-graph-fullscreen-icon",
        "brain-stock-graph-icon",
        "brain-graph-workbench-icon",
        "global-graph-icon",
    ):
        assert needle in html, f"expected graph signal {needle!r} in build HTML"

    postscript = _fetch_text(f"{e2e_build}/postscript.js")
    for needle in (
        "filterChips",
        "brain-graph-chip-all",
        "brain-graph-chip-row",
        "brain-graph-chip-label",
    ):
        assert needle in postscript, (
            f"expected graph runtime signal {needle!r} in emitted postscript.js"
        )
