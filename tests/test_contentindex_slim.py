"""Static + behavioural tests for the slim ``contentIndex.json`` transform.

Background
----------

The Phase 3.1 slim transform lives in
``quartz_overrides/quartz/plugins/emitters/contentIndex.ts``. After the
existing draft / tier / source / linkRecords graft, the post-processor
captures the full body, writes it to ``static/contentBodies/<slug>.json``,
and overwrites ``details.content`` with a 240-char snippet (also exposed
under the new ``details.snippet`` key).

CLAUDE.md / project policy: this repo does not run ``npx quartz build``
in CI (no JS toolchain on the test image). The closest existing pattern
is ``tests/test_quartz_contentindex_draft_filter.py`` — strict static
checks against the emitter source. We follow that pattern AND add a
Python port of the slim transform that exercises the same shape against
a synthetic ``parsed`` dict, so the behavioural contract has direct test
coverage even without a real build.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EMITTER_PATH = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "plugins"
    / "emitters"
    / "contentIndex.ts"
)

# Mirror of the SNIPPET_LENGTH constant in the TS emitter. Pinned here
# (instead of imported) because the Python tests can't read TS — the
# value is asserted by ``test_emitter_pins_snippet_length_constant``
# below, which fails loudly if the two ever drift.
SNIPPET_LENGTH = 240


# ---------------------------------------------------------------------------
# Source-level static checks
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def emitter_source() -> str:
    """Read the contentIndex emitter source once per test module."""
    assert EMITTER_PATH.is_file(), f"missing emitter at {EMITTER_PATH}"
    return EMITTER_PATH.read_text(encoding="utf-8")


def test_emitter_pins_content_bodies_reldir(emitter_source: str) -> None:
    """Bodies are written under ``static/contentBodies``.

    The path is pinned as a top-level ``CONTENT_BODIES_RELDIR`` constant
    so that a future Quartz reshuffle (e.g. moving the static dir) is a
    one-line fix, and so the Search component can hard-code the same
    relative URL when lazy-fetching.
    """
    assert 'CONTENT_BODIES_RELDIR = path.join("static", "contentBodies")' in emitter_source, (
        "expected CONTENT_BODIES_RELDIR constant pinned to static/contentBodies"
    )


def test_emitter_pins_snippet_length_constant(emitter_source: str) -> None:
    """Snippet budget is 240 chars and is exposed as a named constant.

    240 chars ≈ 2-3 lines in the search popover. Anchoring the value in
    a named constant (vs. a magic number on the slice() call) makes the
    intent obvious and gives the test a stable anchor.
    """
    assert f"const SNIPPET_LENGTH = {SNIPPET_LENGTH}" in emitter_source, (
        f"expected `const SNIPPET_LENGTH = {SNIPPET_LENGTH}` in emitter"
    )


def test_emitter_writes_per_slug_body_file(emitter_source: str) -> None:
    """Post-processor writes ``contentBodies/<slug>.json`` per surviving entry.

    We anchor on the ``${slug}.json`` template literal — the unique
    on-disk file shape — plus the ``mkdir`` recursive call that nested
    slugs (e.g. ``_ingested/gmail/<id>``) require.
    """
    assert "${slug}.json" in emitter_source, (
        "expected per-slug body filename template in slim transform"
    )
    assert "fs.mkdir(path.dirname(bodyTarget), { recursive: true })" in emitter_source, (
        "expected recursive mkdir before per-slug body write"
    )


def test_emitter_overwrites_content_with_snippet(emitter_source: str) -> None:
    """``details.content`` is rewritten to the snippet (backwards-compat fallback).

    The plan keeps the ``content`` key populated with the snippet so any
    consumer that hasn't been taught about lazy-fetching still sees a
    usable preview. The new ``details.snippet`` field is the canonical
    forward-looking name.
    """
    assert "details.content = snippet" in emitter_source, (
        "slim transform must overwrite details.content with the snippet"
    )
    assert "details.snippet = snippet" in emitter_source, (
        "slim transform must expose a `snippet` field for forward-looking consumers"
    )


def test_emitter_slim_runs_after_linkrecords_graft(emitter_source: str) -> None:
    """Slim transform comes after the linkRecords graft.

    Ordering: tier/source/linkRecords graft → slim split. The slim step
    is independent but must happen inside the same per-entry loop so the
    snippet is written for every surviving entry (post draft filter).
    """
    graft_marker = "details.linkRecords ="
    slim_marker = "details.content = snippet"
    graft_idx = emitter_source.find(graft_marker)
    slim_idx = emitter_source.find(slim_marker)
    assert graft_idx >= 0, "linkRecords graft marker not found"
    assert slim_idx >= 0, "slim transform marker not found"
    assert graft_idx < slim_idx, (
        "slim transform must run after linkRecords graft so every "
        "surviving entry gets its snippet/body split"
    )


def test_emitter_lifts_date_from_frontmatter(emitter_source: str) -> None:
    """The post-processor surfaces ``details.date`` from frontmatter (P3.3 Part B).

    Lookup order mirrors the brain frontmatter writer in
    ``src/brain/vault/export.py`` (``date`` > ``created`` >
    ``published`` > ``updated``). The P3.6 fix routes every candidate
    through ``liftDate()`` so both string and `Date`-typed frontmatter
    values resolve correctly — the fallback chain itself is anchored
    by checking the literal call sequence inside the assignment.
    """
    # P3.6 fix-1: the lookup chain now flows through ``liftDate(...)``
    # so both YAML strings AND js-yaml-parsed Date objects resolve. Each
    # candidate field is named explicitly so a reorder still trips the
    # test (preferring ``updated`` over ``created`` would be a UX
    # deviation — every doc would look "fresh" after a touch).
    expected_branches = (
        "liftDate(fm.date)",
        "liftDate(fm.created)",
        "liftDate(fm.published)",
        "liftDate(fm.updated)",
    )
    for marker in expected_branches:
        assert marker in emitter_source, (
            f"date-lift branch missing in emitter: `{marker}` — "
            "P3.3 Part B requires the four-field fallback chain"
        )
    # The fallback chain is preserved as a `??` cascade so order is
    # observable in source.
    assert (
        "liftDate(fm.date) ??\n            liftDate(fm.created) ??\n"
        "            liftDate(fm.published) ??\n            liftDate(fm.updated)"
        in emitter_source
    ), "expected `liftDate(...) ?? liftDate(...) ?? ...` cascade preserving the priority order"


def test_emitter_lift_date_helper_accepts_strings_and_dates(
    emitter_source: str,
) -> None:
    """``liftDate`` accepts both YAML strings and js-yaml-parsed `Date` objects.

    P3.6 fix-1: gray-matter / js-yaml parses bare YAML dates
    (``date: 2026-04-12``) into JS ``Date`` instances rather than
    strings. The original ``typeof X === "string"`` checks silently
    dropped Date instances. Anchoring on the helper signature + the
    two type branches inside it.
    """
    assert "export function liftDate(v: unknown): string | undefined" in emitter_source, (
        "expected exported `liftDate(v: unknown): string | undefined` helper"
    )
    assert 'if (typeof v === "string" && v.length > 0) return v' in emitter_source, (
        "liftDate must accept string values"
    )
    assert "v instanceof Date" in emitter_source, (
        "liftDate must accept `Date` instances (js-yaml parses YAML dates as Date)"
    )
    assert "v.toISOString().slice(0, 10)" in emitter_source, (
        "liftDate must normalise Date instances to a `YYYY-MM-DD` slice"
    )


def test_emitter_date_lift_runs_after_tier_source(emitter_source: str) -> None:
    """Date lift comes after the tier/source graft, before linkRecords.

    Ordering keeps the per-entry block readable: frontmatter pulls
    cluster together, then the link-classification pass, then the
    slim transform writes the body file. The P3.6 fix-1 routes the
    date lift through ``liftDate`` so we anchor on the new helper
    invocation rather than the old `details.date = fm.date` literal.
    """
    tier_marker = "details.tier = fm.kind"
    date_marker = "liftDate(fm.date)"
    link_marker = "details.linkRecords ="
    tier_idx = emitter_source.find(tier_marker)
    date_idx = emitter_source.find(date_marker)
    link_idx = emitter_source.find(link_marker)
    assert tier_idx >= 0 and date_idx >= 0 and link_idx >= 0
    assert tier_idx < date_idx < link_idx, (
        "expected order: tier/source pulls → date pull → linkRecords graft"
    )


def test_emitter_brain_content_details_declares_date_field(emitter_source: str) -> None:
    """``BrainContentDetails`` type carries the optional ``date`` field.

    Without the type declaration the date assignment trips a TS error
    at build time (``Property 'date' does not exist on type
    'BrainContentDetails'``). Anchored on the literal ``date?: string``
    line so a rename to ``date?: number`` (epoch millis) would force a
    test update + a downstream Search.tsx update together.
    """
    assert "date?: string" in emitter_source, (
        "BrainContentDetails must declare `date?: string` for the date lift"
    )


def test_emitter_slim_io_inside_existing_try_catch(emitter_source: str) -> None:
    """Slim writes are inside the post-processor's existing try/catch.

    Without this, a missing output dir / EACCES / disk-full surfaces as a
    bare ``ENOENT`` instead of a brain-attributable
    ``brain contentIndex post-processor failed at <path>: ...`` error.
    Anchored by checking the order: try-open marker → slim marker →
    catch-handler marker.
    """
    try_marker = "try {"
    slim_marker = "details.content = snippet"
    catch_marker = "brain contentIndex post-processor failed at"
    try_idx = emitter_source.find(try_marker)
    slim_idx = emitter_source.find(slim_marker)
    catch_idx = emitter_source.find(catch_marker)
    assert try_idx >= 0 and slim_idx >= 0 and catch_idx >= 0
    assert try_idx < slim_idx < catch_idx, (
        "slim transform must sit between the post-processor's `try {` and "
        "the wrapped catch handler"
    )


# ---------------------------------------------------------------------------
# Behavioural shape — Python port of the slim transform
# ---------------------------------------------------------------------------


def _python_port_slim_transform(
    parsed: dict[str, dict[str, Any]],
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Mirror of the TS slim step in pure Python.

    The TS emitter:
        const body = typeof details.content === "string" ? details.content : ""
        const snippet = body.slice(0, SNIPPET_LENGTH)
        await fs.mkdir(path.dirname(bodyTarget), { recursive: true })
        await fs.writeFile(bodyTarget, JSON.stringify({slug, content: body}))
        details.content = snippet
        details.snippet = snippet

    This port walks ``parsed`` in the same way and writes the same files.
    Any drift between this port and the TS source is a sign the test
    needs updating — the static checks above guard the contract on the
    JS side; this helper guards behavioural shape.
    """
    bodies_root = output_dir / "static" / "contentBodies"
    for slug, details in parsed.items():
        raw = details.get("content")
        body = raw if isinstance(raw, str) else ""
        snippet = body[:SNIPPET_LENGTH]
        body_target = bodies_root / f"{slug}.json"
        body_target.parent.mkdir(parents=True, exist_ok=True)
        body_target.write_text(
            json.dumps({"slug": slug, "content": body}),
            encoding="utf-8",
        )
        details["content"] = snippet
        details["snippet"] = snippet
    return parsed


def test_python_port_truncates_content_to_240_chars(tmp_path: Path) -> None:
    """A ≥240-char body is truncated to a 240-char snippet."""
    # Setup
    long_body = "x" * 1000
    parsed = {"note-a": {"slug": "note-a", "title": "A", "content": long_body}}

    # Exercise
    out = _python_port_slim_transform(parsed, tmp_path)

    # Verify
    assert out["note-a"]["snippet"] == "x" * 240
    assert out["note-a"]["content"] == "x" * 240
    assert len(out["note-a"]["snippet"]) == SNIPPET_LENGTH


def test_python_port_short_content_passes_through(tmp_path: Path) -> None:
    """A <240-char body is preserved verbatim in the snippet."""
    # Setup
    short_body = "hello world"
    parsed = {"short": {"slug": "short", "content": short_body}}

    # Exercise
    out = _python_port_slim_transform(parsed, tmp_path)

    # Verify
    assert out["short"]["snippet"] == short_body
    assert out["short"]["content"] == short_body


def test_python_port_writes_per_slug_body_file(tmp_path: Path) -> None:
    """Each surviving entry produces a ``contentBodies/<slug>.json`` file with the full body."""
    # Setup
    body = "the quick brown fox " * 50  # >240 chars, ensure truncation occurs
    parsed = {"slug-1": {"slug": "slug-1", "content": body}}

    # Exercise
    _python_port_slim_transform(parsed, tmp_path)

    # Verify
    body_file = tmp_path / "static" / "contentBodies" / "slug-1.json"
    assert body_file.is_file(), f"expected body file at {body_file}"
    payload = json.loads(body_file.read_text(encoding="utf-8"))
    assert payload == {"slug": "slug-1", "content": body}


def test_python_port_handles_nested_slugs(tmp_path: Path) -> None:
    """Slugs containing ``/`` (e.g. ``_ingested/gmail/<id>``) get nested dirs."""
    # Setup
    parsed = {
        "_ingested/gmail/abc123": {
            "slug": "_ingested/gmail/abc123",
            "content": "email body",
        },
        "README": {"slug": "README", "content": "readme"},
    }

    # Exercise
    _python_port_slim_transform(parsed, tmp_path)

    # Verify
    nested = tmp_path / "static" / "contentBodies" / "_ingested" / "gmail" / "abc123.json"
    flat = tmp_path / "static" / "contentBodies" / "README.json"
    assert nested.is_file(), f"missing nested body file at {nested}"
    assert flat.is_file(), f"missing flat body file at {flat}"


def test_python_port_handles_missing_content(tmp_path: Path) -> None:
    """Entries with no ``content`` field default to empty body + empty snippet.

    Mirrors the TS guard ``typeof details.content === "string" ? ... : ""`` —
    a frontmatter-only stub doc shouldn't crash the emitter.
    """
    # Setup
    parsed: dict[str, dict[str, Any]] = {"empty": {"slug": "empty", "title": "stub"}}

    # Exercise
    out = _python_port_slim_transform(parsed, tmp_path)

    # Verify
    assert out["empty"]["snippet"] == ""
    assert out["empty"]["content"] == ""
    body_file = tmp_path / "static" / "contentBodies" / "empty.json"
    assert body_file.is_file()
    assert json.loads(body_file.read_text(encoding="utf-8")) == {
        "slug": "empty",
        "content": "",
    }


def test_python_port_preserves_other_fields(tmp_path: Path) -> None:
    """Slim step doesn't touch ``title`` / ``tags`` / ``links`` / ``linkRecords`` / ``date``."""
    # Setup
    parsed = {
        "doc": {
            "slug": "doc",
            "title": "Example",
            "tags": ["a", "b"],
            "links": ["other"],
            "linkRecords": [{"target": "other", "kind": "wiki"}],
            "tier": "vault",
            "source": "manual",
            "date": "2026-04-12T00:00:00+00:00",
            "content": "body",
        }
    }

    # Exercise
    out = _python_port_slim_transform(parsed, tmp_path)

    # Verify — only `content` and `snippet` change. Date passthrough
    # confirms the slim step doesn't accidentally drop the field
    # P3.3 Part B added.
    d = out["doc"]
    assert d["title"] == "Example"
    assert d["tags"] == ["a", "b"]
    assert d["links"] == ["other"]
    assert d["linkRecords"] == [{"target": "other", "kind": "wiki"}]
    assert d["tier"] == "vault"
    assert d["source"] == "manual"
    assert d["date"] == "2026-04-12T00:00:00+00:00"
    assert d["content"] == "body"
    assert d["snippet"] == "body"


class _FakeDate:
    """Minimal stand-in for a JS ``Date`` object.

    js-yaml parses bare YAML date literals (``date: 2026-04-12``) into
    ``Date`` instances on the JS side. The Python port can't import a
    real ``Date``, but the contract we care about is "the value carries
    a `toISOString()` method that returns an ISO 8601 string". This
    fake mirrors that contract just well enough to exercise the
    Date-handling branch of `_python_port_lift_date`.
    """

    def __init__(self, iso: str) -> None:
        self._iso = iso

    def toISOString(self) -> str:  # noqa: N802 — match JS API spelling
        return self._iso


def _python_port_lift_date(v: Any) -> str | None:
    """Mirror of the TS ``liftDate`` helper (P3.6 fix-1).

    The TS helper:
        if (typeof v === "string" && v.length > 0) return v
        if (v instanceof Date && !Number.isNaN(v.getTime()))
            return v.toISOString().slice(0, 10)
        return undefined

    The Python port treats ``_FakeDate`` instances as Date stand-ins
    so we can exercise the Date branch without wiring up a real JS
    runtime.
    """
    if isinstance(v, str) and len(v) > 0:
        return v
    if isinstance(v, _FakeDate):
        return v.toISOString()[:10]
    return None


def _python_port_date_lift(
    fm: dict[str, Any],
) -> str | None:
    """Mirror of the TS date-lift fallback chain (P3.6 fix-1).

    The TS emitter:
        const lifted =
            liftDate(fm.date) ??
            liftDate(fm.created) ??
            liftDate(fm.published) ??
            liftDate(fm.updated)

    Each candidate field flows through ``liftDate`` which now accepts
    BOTH strings AND Date instances (the original `typeof X ===
    "string"` checks silently dropped Date instances — the bug P3.6
    fix-1 closes).
    """
    for key in ("date", "created", "published", "updated"):
        result = _python_port_lift_date(fm.get(key))
        if result is not None:
            return result
    return None


def test_date_lift_prefers_explicit_date_field() -> None:
    """``date`` wins when present (forward-looking authoring override)."""
    fm = {"date": "2026-04-12", "created": "2026-04-01", "updated": "2026-05-01"}
    assert _python_port_date_lift(fm) == "2026-04-12"


def test_date_lift_falls_back_to_created() -> None:
    """``created`` is the primary brain frontmatter field — used when ``date`` is absent."""
    fm = {"created": "2026-04-01T00:00:00+00:00", "updated": "2026-05-01T00:00:00+00:00"}
    assert _python_port_date_lift(fm) == "2026-04-01T00:00:00+00:00"


def test_date_lift_falls_back_to_published() -> None:
    """``published`` covers any legacy authoring tool that wrote that key."""
    fm = {"published": "2025-12-12"}
    assert _python_port_date_lift(fm) == "2025-12-12"


def test_date_lift_falls_back_to_updated() -> None:
    """``updated`` is the last-resort fallback (kept current on every sync)."""
    fm = {"updated": "2026-05-01"}
    assert _python_port_date_lift(fm) == "2026-05-01"


def test_date_lift_returns_none_when_all_missing() -> None:
    """Frontmatter with no recognised date field → ``None`` (consumers handle).

    The TS branch leaves ``details.date`` undefined; the Search row
    renders an empty date column and the tag-content row hides the
    ``<QuartzDate>`` element altogether — both already exercised by
    static checks above.
    """
    assert _python_port_date_lift({}) is None
    # Non-string, non-Date values don't satisfy the type guard.
    assert _python_port_date_lift({"created": 12345}) is None
    assert _python_port_date_lift({"date": None}) is None
    assert _python_port_date_lift({"date": True}) is None


# ---------------------------------------------------------------------------
# P3.6 fix-1 — Date-instance handling
# ---------------------------------------------------------------------------


def test_lift_date_accepts_string_value() -> None:
    """A non-empty string passes through unchanged."""
    assert _python_port_lift_date("2026-04-12") == "2026-04-12"
    # Datetime-form strings (with time + zone) survive verbatim — the
    # consumer (Search.tsx, TagContent.tsx) handles parsing.
    assert (
        _python_port_lift_date("2026-04-12T15:30:00+00:00")
        == "2026-04-12T15:30:00+00:00"
    )


def test_lift_date_accepts_date_instance() -> None:
    """A `Date`-shaped object resolves to its ISO date slice (`YYYY-MM-DD`).

    Mirrors the TS branch ``v instanceof Date`` — the bug fix lets
    js-yaml-parsed YAML dates flow through.
    """
    fake = _FakeDate("2026-04-12T00:00:00.000Z")
    assert _python_port_lift_date(fake) == "2026-04-12"


def test_lift_date_returns_none_for_other_types() -> None:
    """Numbers, bools, None, dicts → ``None`` (no falsy date emitted)."""
    for v in (None, 12345, True, [], {}, 0, ""):
        assert _python_port_lift_date(v) is None, f"unexpected lift for {v!r}"


def test_date_lift_chain_resolves_date_field_when_object() -> None:
    """The `Date`-shaped value at `fm.date` wins over a string `created`.

    Anchors the bug-regression scenario: previously a `Date`-typed
    `fm.date` would skip to `created` (because `typeof Date !== "string"`),
    silently demoting the authored date in favour of the export-time
    one. The fix routes both candidates through `liftDate` so the
    explicit authoring override wins as documented.
    """
    fm = {
        "date": _FakeDate("2026-04-12T00:00:00.000Z"),
        "created": "2026-04-01T00:00:00+00:00",
    }
    assert _python_port_date_lift(fm) == "2026-04-12"


# ---------------------------------------------------------------------------
# P3.6 fix-3 — Slug allowlist (path-traversal hardening)
# ---------------------------------------------------------------------------

# Pinned regex mirroring the TS `SAFE_SLUG_RE` constant in the emitter.
# Asserted by `test_emitter_pins_safe_slug_re` below so any drift on
# the TS side trips a test rather than silently bypassing the port.
# Composition rationale lives next to the TS constant — see
# `quartz_overrides/quartz/plugins/emitters/contentIndex.ts`.
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9._/,:-]+$")


def _is_safe_slug(slug: str) -> bool:
    """Mirror of the TS ``isSafeSlug`` helper.

    Char allowlist plus segment-shape rejection. ``../etc/passwd`` is
    rejected even though every individual char is in the allowlist
    (`.`, `/`, alphanumeric); ``/leading/slash`` is rejected because
    its first segment is empty.
    """
    if not _SAFE_SLUG_RE.match(slug):
        return False
    return all(segment not in ("", "..") for segment in slug.split("/"))


def test_emitter_pins_safe_slug_re(emitter_source: str) -> None:
    """Emitter declares the allowlist regex constant + ``isSafeSlug`` helper.

    Defense in depth: a slug like ``../etc/passwd`` would otherwise let
    `fs.writeFile` escape the `static/contentBodies/` directory. The
    char allowlist alone permits `..` (every char is individually safe),
    so the helper additionally rejects any `..` segment.
    """
    assert (
        "export const SAFE_SLUG_RE = /^[a-zA-Z0-9._/,:-]+$/" in emitter_source
    ), (
        "expected exported `SAFE_SLUG_RE` allowlist regex including `,` and "
        "`:` (justified against live-vault slug shapes — see emitter comment)"
    )
    assert "export function isSafeSlug(slug: string): boolean" in emitter_source, (
        "expected exported `isSafeSlug` helper combining char allowlist + .. rejection"
    )
    assert 'slug.split("/")' in emitter_source, (
        "expected `slug.split(\"/\")` to inspect path segments"
    )
    assert 'segment === ".."' in emitter_source, (
        "expected explicit `..`-segment rejection in `isSafeSlug`"
    )
    # The slim transform must consult the helper BEFORE constructing the
    # body file path.
    assert "if (!isSafeSlug(slug))" in emitter_source, (
        "expected `isSafeSlug` guard before per-slug body write"
    )


def test_safe_slug_regex_accepts_typical_quartz_slugs() -> None:
    """Quartz's slugify output passes the allowlist.

    The live-vault slug list includes shapes with commas (from email
    subject lines that mention dates: ``Tue,-7-Apr-...``) and colons
    (from the ``krisp:`` prefix Krisp transcripts get). These chars
    are URL- and path-safe, so the allowlist accepts them while still
    rejecting HTML metachars and shell metachars.
    """
    for slug in (
        "demo-vault-doc",
        "_ingested/gmail/abc123",
        "_ingested/krisp/2026-04-12-meeting",
        "tags/take-home",
        "README",
        "folder/sub.folder/page",
        # Live-vault shapes — found by running `bin/brain-rebuild`
        # against a real corpus and inspecting the rejected slugs.
        "_ingested/gmail/Tue,-7-Apr-19d68a63-reminder-meeting",
        "_ingested/krisp/2026-05-02-krisp:au-auto",
        "_ingested/gmail/Wed,-04-Ma-19cb8c7c-venwise",
    ):
        assert _is_safe_slug(slug), f"expected `{slug}` to pass allowlist + segment check"


def test_safe_slug_regex_rejects_path_traversal_attempts() -> None:
    """Attacker-shaped slugs are rejected by ``isSafeSlug`` (allowlist + `..` segment).

    The char allowlist alone passes things like ``../etc/passwd`` (every
    char is individually safe), so we use ``isSafeSlug`` here — the
    same helper the emitter and the inline-fetch site call.
    """
    for slug in (
        "../etc/passwd",
        "../../etc/passwd",
        "..",
        "foo/../bar",  # `..` segment in the middle
        "foo bar",  # space (not in allowlist)
        "foo;rm",  # shell metachar
        "foo<script>",  # HTML injection attempt
        "",  # empty
        "/absolute/path",  # leading slash → first segment empty
    ):
        assert not _is_safe_slug(slug), f"expected `{slug}` to be rejected"


def _python_port_slim_transform_with_guard(
    parsed: dict[str, dict[str, Any]],
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Variant of the slim transform that enforces ``SAFE_SLUG_RE``.

    Mirrors the P3.6 fix-3 guard: slugs failing the allowlist skip
    both the body-file write and the slim overwrite, leaving the
    entry in ``parsed`` with its full content.
    """
    bodies_root = output_dir / "static" / "contentBodies"
    for slug, details in parsed.items():
        if not _is_safe_slug(slug):
            continue
        raw = details.get("content")
        body = raw if isinstance(raw, str) else ""
        snippet = body[:SNIPPET_LENGTH]
        body_target = bodies_root / f"{slug}.json"
        body_target.parent.mkdir(parents=True, exist_ok=True)
        body_target.write_text(
            json.dumps({"slug": slug, "content": body}),
            encoding="utf-8",
        )
        details["content"] = snippet
        details["snippet"] = snippet
    return parsed


def test_slim_transform_skips_unsafe_slugs(tmp_path: Path) -> None:
    """Slim transform refuses to write a body file for ``../etc/passwd``.

    Behavioural regression test for P3.6 fix-3: the entry is preserved
    in the parsed dict (with its full content) but no body file is
    written under contentBodies/.
    """
    # Setup
    parsed = {
        "../etc/passwd": {"slug": "../etc/passwd", "content": "secret"},
        "demo-doc": {"slug": "demo-doc", "content": "y" * 500},
    }

    # Exercise
    out = _python_port_slim_transform_with_guard(parsed, tmp_path)

    # Verify — the unsafe slug's entry is untouched (full content stays)
    # and no file written outside contentBodies/.
    assert out["../etc/passwd"]["content"] == "secret"
    assert "snippet" not in out["../etc/passwd"]
    # The traversal target — `tmp_path/etc/passwd` — must not exist.
    assert not (tmp_path / "etc" / "passwd").exists()
    # The safe slug still gets the slim treatment.
    assert out["demo-doc"]["snippet"] == "y" * 240
    assert (tmp_path / "static" / "contentBodies" / "demo-doc.json").is_file()
