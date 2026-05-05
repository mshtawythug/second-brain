"""Static smoke tests for the P4.4 email-thread reading mode.

The brain repo's test image does not run a JS toolchain, so we cannot
build the Quartz output and exercise the runtime end-to-end here. The
closest existing pattern is ``tests/test_quartz_search_static.py`` —
regex / substring assertions against TS / SCSS / JS source files. This
file follows the same flavor, scoped to the P4.4 contract:

- A ``Plugin.EmailThreadReader()`` transformer exists at
  ``quartz_overrides/quartz/plugins/transformers/emailThread.ts``,
  is exported from the transformers barrel, and is wired into
  ``quartz.config.ts``'s plugin list.
- The runtime ships at ``quartz_overrides/quartz/static/emailThread
  .js``, gates on ``/_ingested/gmail/`` URL, parses the From address
  from the ``YYYY-MM-DD HH:MM — <from>`` heading shape emitted by
  ``brain.ingest.gmail.to_extracted_thread``, stamps ``data-brain-
  thread-from`` + ``data-brain-is-mine`` on each section, renders the
  ``.brain-email-replies-only-toggle`` button, and persists the
  toggle state under the ``brain.email.repliesOnly`` localStorage
  key.
- A new SCSS partial ``_email_thread.scss`` exists, declares the
  expected class hooks (``.brain-thread-message``, the toggle button,
  the ``[data-brain-is-mine="false"]`` filter rule), and is wired
  into ``custom.scss`` so the build picks it up.
- ``Config.user_email`` exists in ``brain.config`` and defaults to
  ``None`` when ``BRAIN_USER_EMAIL`` is unset.

Limitations: this file only asserts the SOURCE shape. A full end-to-
end test would invoke ``npx quartz build`` against a fixture vault
and drive the toggle with a Playwright browser. That needs a JS
toolchain not on the test image.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_DIR = REPO_ROOT / "quartz_overrides"
TRANSFORMERS_DIR = OVERRIDES_DIR / "quartz" / "plugins" / "transformers"
EMAIL_TRANSFORMER = TRANSFORMERS_DIR / "emailThread.ts"
TRANSFORMERS_INDEX = TRANSFORMERS_DIR / "index.ts"
QUARTZ_CONFIG = OVERRIDES_DIR / "quartz.config.ts"
STATIC_DIR = OVERRIDES_DIR / "quartz" / "static"
EMAIL_RUNTIME = STATIC_DIR / "emailThread.js"
STYLES_DIR = OVERRIDES_DIR / "quartz" / "styles"
EMAIL_SCSS = STYLES_DIR / "brain" / "_email_thread.scss"
CUSTOM_SCSS = STYLES_DIR / "custom.scss"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def transformer_source() -> str:
    """Read the EmailThreadReader transformer source once per module."""
    assert EMAIL_TRANSFORMER.is_file(), f"missing transformer at {EMAIL_TRANSFORMER}"
    return EMAIL_TRANSFORMER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def transformers_index_source() -> str:
    """Read the transformers barrel once per module."""
    assert TRANSFORMERS_INDEX.is_file(), f"missing barrel at {TRANSFORMERS_INDEX}"
    return TRANSFORMERS_INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def quartz_config_source() -> str:
    """Read quartz.config.ts once per module."""
    assert QUARTZ_CONFIG.is_file(), f"missing config at {QUARTZ_CONFIG}"
    return QUARTZ_CONFIG.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runtime_source() -> str:
    """Read the runtime script once per module."""
    assert EMAIL_RUNTIME.is_file(), f"missing runtime at {EMAIL_RUNTIME}"
    return EMAIL_RUNTIME.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def email_scss_source() -> str:
    """Read the SCSS partial once per module."""
    assert EMAIL_SCSS.is_file(), f"missing SCSS partial at {EMAIL_SCSS}"
    return EMAIL_SCSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def custom_scss_source() -> str:
    """Read the SCSS entry point once per module."""
    assert CUSTOM_SCSS.is_file(), f"missing custom.scss at {CUSTOM_SCSS}"
    return CUSTOM_SCSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Transformer — shape + barrel + wiring
# ---------------------------------------------------------------------------


def test_transformer_exports_email_thread_reader(transformer_source: str) -> None:
    """Transformer file exports the ``EmailThreadReader`` plugin symbol.

    The barrel re-exports by name; without the export the import
    chain breaks at build time. Anchor on the literal ``export const
    EmailThreadReader``.
    """
    assert (
        "export const EmailThreadReader: QuartzTransformerPlugin"
        in transformer_source
    ), "expected `export const EmailThreadReader: QuartzTransformerPlugin` declaration"


def test_transformer_reads_user_email_env_var(transformer_source: str) -> None:
    """Transformer reads ``process.env.BRAIN_USER_EMAIL`` at build time.

    The runtime expects ``window.BRAIN_USER_EMAIL`` to be set before
    its ``DOMContentLoaded`` listener fires; the transformer is the
    only thing that bakes the env var into the page. Anchor on the
    literal env-var name + a ``process.env`` access.
    """
    assert (
        'USER_EMAIL_ENV_VAR = "BRAIN_USER_EMAIL"' in transformer_source
    ), "expected `BRAIN_USER_EMAIL` env-var name pinned as a constant"
    assert (
        "process.env[USER_EMAIL_ENV_VAR]" in transformer_source
    ), "expected `process.env[USER_EMAIL_ENV_VAR]` read inside externalResources()"


def test_transformer_emits_inline_window_global(transformer_source: str) -> None:
    """Transformer emits an inline ``<script>window.BRAIN_USER_EMAIL = "...";</script>``.

    The runtime reads ``window.BRAIN_USER_EMAIL`` at boot. Without
    the inline script, the runtime sees ``undefined`` and the filter
    can never match. Pin the contract: a JS resource with
    ``contentType: "inline"`` whose script body sets the global.
    """
    assert (
        'contentType: "inline"' in transformer_source
    ), "expected at least one inline JS resource emitted"
    assert (
        "window.BRAIN_USER_EMAIL" in transformer_source
    ), "expected the inline script body to set `window.BRAIN_USER_EMAIL`"
    assert (
        'loadTime: "beforeDOMReady"' in transformer_source
    ), "expected the inline global to load BEFORE DOM ready (so the runtime can read it)"


def test_transformer_emits_external_runtime(transformer_source: str) -> None:
    """Transformer emits an external JS resource pointing at ``/static/emailThread.js``.

    Quartz's ``Plugin.Static()`` mirrors ``quartz/static/`` into
    ``<build>/static/`` automatically, so we just need to point a
    ``<script src=...>`` at the absolute path. Anchor on the literal
    ``/static/emailThread.js`` URL.
    """
    assert (
        'SCRIPT_SRC = "/static/emailThread.js"' in transformer_source
    ), "expected SCRIPT_SRC pinned to `/static/emailThread.js`"
    assert (
        'contentType: "external"' in transformer_source
    ), "expected the runtime emitted as an external JS resource"


def test_transformer_escapes_user_email_value(transformer_source: str) -> None:
    """Transformer escapes the user email before interpolating into JS.

    Even though the email comes from the user's own ``.env`` and is
    not adversarial, a stray ``</script>`` would break the inline
    script tag boundary. The escape pass also handles ``\\``, ``"``,
    and ``\\r``/``\\n`` to keep the inline string parseable.
    """
    assert "escapeForJsString" in transformer_source, (
        "expected `escapeForJsString` helper to escape the email "
        "before interpolation"
    )


def test_transformer_index_reexports_email_thread_reader(
    transformers_index_source: str,
) -> None:
    """Transformers barrel re-exports ``EmailThreadReader``.

    The Quartz config imports plugins via ``import * as Plugin from
    "./quartz/plugins"`` which resolves to the barrel. Without the
    re-export, ``Plugin.EmailThreadReader`` is undefined.
    """
    assert (
        'export { EmailThreadReader } from "./emailThread"'
        in transformers_index_source
    ), "expected `export { EmailThreadReader } from './emailThread'` line"


def test_quartz_config_wires_email_thread_reader(quartz_config_source: str) -> None:
    """``quartz.config.ts`` calls ``Plugin.EmailThreadReader()`` in the transformer list.

    Without the call, the plugin never loads regardless of barrel
    exports.
    """
    assert "Plugin.EmailThreadReader()" in quartz_config_source, (
        "expected `Plugin.EmailThreadReader()` call in the transformers list"
    )


# ---------------------------------------------------------------------------
# Runtime — gating, parsing, annotation, button render
# ---------------------------------------------------------------------------


def test_runtime_gates_on_gmail_pathname(runtime_source: str) -> None:
    """Runtime only activates on ``/_ingested/gmail/`` URLs.

    Two-part gate (URL + DOM heuristic) — pin the URL prefix literal
    so a future rename trips the test.
    """
    assert "/_ingested/gmail/" in runtime_source, (
        "expected `/_ingested/gmail/` pathname gate in the runtime"
    )


def test_runtime_parses_from_address_separator(runtime_source: str) -> None:
    """Runtime parses ``YYYY-MM-DD HH:MM — <from>`` headings.

    The separator is the em-dash (``—``) padded with single spaces,
    matching ``brain.ingest.gmail._format_thread_section``. Pin the
    literal so a markdown-shape change in the gmail extractor surfaces
    here.
    """
    assert 'FROM_SEPARATOR = " — "' in runtime_source, (
        "expected ` — ` (space-em-dash-space) as the FROM_SEPARATOR constant"
    )
    assert "parseFromAddress" in runtime_source, (
        "expected `parseFromAddress` helper in the runtime"
    )


def test_runtime_recognises_thread_heading_pattern(runtime_source: str) -> None:
    """Runtime detects per-message headings via the date-stamped pattern.

    Anchor on the regex. A stray ``## Conclusion`` H2 should NOT be
    mistaken for a thread message — the regex's leading
    ``YYYY-MM-DD HH:MM`` block is what filters that out.
    """
    assert (
        "THREAD_HEADING_RE = /^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}\\s+—\\s+/"
        in runtime_source
    ), "expected `THREAD_HEADING_RE` regex pinned to the date-stamped heading shape"


def test_runtime_stamps_data_attributes(runtime_source: str) -> None:
    """Runtime stamps ``data-brain-thread-from`` + ``data-brain-is-mine``.

    These attributes are the SCSS visibility-rule anchor — without
    them the toggle has nothing to filter on.
    """
    assert 'FROM_ATTR = "data-brain-thread-from"' in runtime_source, (
        "expected FROM_ATTR pinned to `data-brain-thread-from`"
    )
    assert 'IS_MINE_ATTR = "data-brain-is-mine"' in runtime_source, (
        "expected IS_MINE_ATTR pinned to `data-brain-is-mine`"
    )


def test_runtime_wraps_latest_message_section(runtime_source: str) -> None:
    """Runtime wraps the leading H2 + body in a ``brain-thread-latest`` section.

    ``to_extracted_thread`` emits the latest message as ``## H2 +
    body`` (test-pinned). The runtime synthesises a section wrapper
    so the SCSS / filter has a uniform shape across latest + older
    messages.
    """
    assert "wrapLatestMessage" in runtime_source, (
        "expected `wrapLatestMessage` helper in the runtime"
    )
    assert 'LATEST_CLASS = "brain-thread-latest"' in runtime_source, (
        "expected LATEST_CLASS pinned to `brain-thread-latest`"
    )
    assert 'SECTION_CLASS = "brain-thread-message"' in runtime_source, (
        "expected SECTION_CLASS pinned to `brain-thread-message`"
    )


def test_runtime_renders_toggle_button_class(runtime_source: str) -> None:
    """Runtime renders a ``.brain-email-replies-only-toggle`` button.

    The class literal comes verbatim from the spec; pinning it here
    keeps the SCSS selector + the JS injection in lock-step.
    """
    assert (
        'TOGGLE_CLASS = "brain-email-replies-only-toggle"' in runtime_source
    ), "expected TOGGLE_CLASS pinned to `brain-email-replies-only-toggle`"
    assert "renderToggle" in runtime_source, (
        "expected `renderToggle` helper that injects the button"
    )


def test_runtime_persists_toggle_state(runtime_source: str) -> None:
    """Runtime persists the toggle state in ``localStorage[brain.email.repliesOnly]``.

    Without persistence, every page nav would reset the filter.
    Anchor on the literal storage key + getItem/setItem calls.
    """
    assert (
        'REPLIES_ONLY_KEY = "brain.email.repliesOnly"' in runtime_source
    ), "expected `brain.email.repliesOnly` localStorage key constant"
    assert (
        "localStorage.getItem(REPLIES_ONLY_KEY)" in runtime_source
    ), "expected `localStorage.getItem(REPLIES_ONLY_KEY)` read"
    assert (
        "localStorage.setItem(REPLIES_ONLY_KEY" in runtime_source
    ), "expected `localStorage.setItem(REPLIES_ONLY_KEY, ...)` write"


def test_runtime_toggles_body_class(runtime_source: str) -> None:
    """Runtime flips ``body.brain-replies-only`` to drive the SCSS visibility rule.

    Anchor on the literal class. The SCSS `body.brain-replies-only
    article [data-brain-is-mine="false"]` rule does the actual
    hiding — the JS just toggles the master switch.
    """
    assert (
        'REPLIES_ONLY_CLASS = "brain-replies-only"' in runtime_source
    ), "expected `brain-replies-only` body-class constant"
    assert "document.body.classList.toggle(REPLIES_ONLY_CLASS" in runtime_source, (
        "expected `document.body.classList.toggle(REPLIES_ONLY_CLASS, ...)` call"
    )


def test_runtime_reads_window_user_email(runtime_source: str) -> None:
    """Runtime reads the user email from ``window.BRAIN_USER_EMAIL``.

    The transformer bakes this in via an inline script tag; this
    asserts the runtime side actually reads it.
    """
    assert "window.BRAIN_USER_EMAIL" in runtime_source, (
        "expected `window.BRAIN_USER_EMAIL` read in the runtime"
    )
    assert "getUserEmail" in runtime_source, (
        "expected `getUserEmail` helper that reads the global"
    )


def test_runtime_subscribes_to_spa_nav(runtime_source: str) -> None:
    """Runtime re-runs on Quartz SPA ``nav`` events.

    Without the listener, navigating from a thread page to another
    thread page would skip the wiring.
    """
    assert 'document.addEventListener("nav", init)' in runtime_source, (
        "expected `document.addEventListener('nav', init)` listener"
    )


def test_runtime_idempotent_via_wired_attribute(runtime_source: str) -> None:
    """Runtime guards against double-wiring with a ``data-brain-thread-wired`` flag.

    SPA back/forward can re-fire ``nav`` on the same article element.
    Without an idempotency guard, the latest-message wrap step would
    re-wrap on each fire and stack <section> elements.
    """
    assert 'WIRED_ATTR = "data-brain-thread-wired"' in runtime_source, (
        "expected `data-brain-thread-wired` idempotency marker"
    )


# ---------------------------------------------------------------------------
# SCSS — partial exists + class hooks declared + import wired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        ".brain-thread-message",
        ".brain-thread-latest",
        ".brain-email-replies-only-toggle",
        '[data-brain-is-mine="false"]',
    ],
)
def test_email_scss_declares_expected_classes(
    email_scss_source: str, selector: str
) -> None:
    """Each expected class hook / attribute appears in the SCSS partial."""
    assert selector in email_scss_source, (
        f"expected selector `{selector}` declared in _email_thread.scss"
    )


def test_email_scss_styles_details_chrome(email_scss_source: str) -> None:
    """SCSS paints ``<details>`` and ``<summary>`` (intentional disclosure UI).

    Spec: ``<details>`` should look intentional, not browser-default.
    Anchor on the selector and a body-styling property so a bare
    selector with no rules trips the test.
    """
    assert "details" in email_scss_source, (
        "expected `details` selector in the SCSS partial"
    )
    assert "summary" in email_scss_source, (
        "expected `summary` selector in the SCSS partial"
    )
    # Hide the OEM disclosure marker; we paint our own glyph.
    assert "::-webkit-details-marker" in email_scss_source, (
        "expected `::-webkit-details-marker` reset (OEM widget hidden)"
    )


def test_email_scss_uses_brain_tokens(email_scss_source: str) -> None:
    """SCSS pulls colours from the brain token palette.

    Spec: solid border-left accent (from ``--secondary`` or
    ``--surface-1``) — anchor on the brain token CSS variables so a
    refactor that swaps in a hex literal trips the test.
    """
    assert "var(--accent-soft)" in email_scss_source, (
        "expected `var(--accent-soft)` reference in the SCSS partial"
    )
    assert "var(--accent-strong)" in email_scss_source, (
        "expected `var(--accent-strong)` reference in the SCSS partial"
    )


def test_email_scss_has_visibility_rule(email_scss_source: str) -> None:
    """SCSS hides non-user messages when ``body.brain-replies-only`` is on.

    The visibility rule is THE filter — without it the toggle button
    just flips a class that does nothing.
    """
    assert (
        'body.brain-replies-only article [data-brain-is-mine="false"]'
        in email_scss_source
    ), (
        "expected `body.brain-replies-only article [data-brain-is-mine=\"false\"]` "
        "selector"
    )
    assert "display: none" in email_scss_source, (
        "expected `display: none` rule on the hidden-section selector"
    )


def test_custom_scss_imports_email_thread_partial(custom_scss_source: str) -> None:
    """The new ``_email_thread.scss`` partial is imported from the SCSS entry point.

    Without this `@use` line, the partial sits on disk but never
    reaches the rendered CSS.
    """
    assert '@use "./brain/email_thread"' in custom_scss_source, (
        "expected `@use \"./brain/email_thread\";` line in custom.scss"
    )


# ---------------------------------------------------------------------------
# Config — Python side
# ---------------------------------------------------------------------------


def test_config_defaults_user_email_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Config.load()`` defaults ``user_email`` to ``None`` when env is unset.

    BRAIN_USER_EMAIL is optional — the wiki transformer accepts an
    empty value (renders the toggle, filter no-ops). Use the existing
    DATABASE_URL fixture pattern: the ``Config.load`` path requires
    DATABASE_URL, which we stub here with monkeypatch + a minimal env.
    """
    monkeypatch.delenv("BRAIN_USER_EMAIL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://test:test@localhost/test"
    )
    cfg = Config.load()
    assert cfg.user_email is None


def test_config_reads_user_email_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Config.load()`` reads ``BRAIN_USER_EMAIL`` from the env.

    Verifies the round-trip from env to ``Config.user_email`` so a
    rename of the env var or the dataclass field surfaces here.
    """
    monkeypatch.setenv("BRAIN_USER_EMAIL", "owner@example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://test:test@localhost/test"
    )
    cfg = Config.load()
    assert cfg.user_email == "owner@example.com"


def test_config_strips_whitespace_from_user_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace around the env value is stripped before storage.

    A trailing newline from a `.env` quirk would otherwise bleed into
    the runtime's `window.BRAIN_USER_EMAIL` global and break the
    substring match against parsed `From:` headers.
    """
    monkeypatch.setenv("BRAIN_USER_EMAIL", "  owner@example.com\n")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://test:test@localhost/test"
    )
    cfg = Config.load()
    assert cfg.user_email == "owner@example.com"


def test_config_treats_empty_user_email_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty / whitespace-only env value collapses to ``None``.

    Default-falsy semantics — easier for downstream callers to do
    `if cfg.user_email:` without needing to also check `!= ""`.
    """
    monkeypatch.setenv("BRAIN_USER_EMAIL", "   ")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://test:test@localhost/test"
    )
    cfg = Config.load()
    assert cfg.user_email is None
