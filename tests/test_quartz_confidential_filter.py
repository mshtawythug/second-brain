"""Confidential notes must not be PUBLISHED at all (F6 publish boundary).

Two layers of test, because they catch different regressions:

* **Static checks** (always run) pin that the ``RemoveConfidential`` filter
  exists and is *registered in the config*. Registration is the half that
  silently rots: the plugin can be present and correct while a config edit
  drops it from ``filters``, and nothing else in the suite would notice.
* **A real ``npx quartz build``** (marked ``e2e``, skips cleanly without the JS
  toolchain) asserts the property that actually matters — the canary phrase
  appears NOWHERE in the built output.

**Why this module exists at all.** The first version of this boundary filtered
the ``contentIndex`` emitter, on the assumption that this was how ``draft``
quarantines a note. A real build disproved it: the index entry was correctly
dropped and the body was still published in three other places — the rendered
page at its slug, ``index.xml`` (the RSS feed), and ``tags/<tag>.html``.
``draft`` never had that problem because upstream's ``RemoveDrafts`` filters at
``shouldPublish``, which runs before ANY emitter.

That is why the e2e test greps the **entire build tree** rather than the files
it expects to be risky. The enumeration of risky files was already wrong once —
RSS and tag pages were not on anyone's list — and a test written from the same
enumeration would have passed while the leak stood.

All fixtures are synthetic; ``ZEPHYRQUUX`` is an invented canary.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = REPO_ROOT / "src" / "brain" / "quartz_overrides"
FILTER_PATH = OVERRIDES / "quartz" / "plugins" / "filters" / "sensitivity.ts"
CONFIG_PATH = OVERRIDES / "quartz.config.ts"

#: Distinctive string planted in the confidential fixture's body. Must not
#: survive into any built artifact.
CANARY = "ZEPHYRQUUX"

_CONFIDENTIAL_NOTE = f"""---
id: 11111111-2222-3333-4444-555555555555
title: Synthetic confidential probe
tags: [ops]
kind: vault
content_type: markdown
summary: A body-derived precis mentioning {CANARY} banding.
sensitivity: confidential
---

# Synthetic confidential probe

The distinctive canary phrase is {CANARY} and it must not reach the built site.
"""


#: Canary in a `draft: true` note. Adding a filter alongside `RemoveDrafts`
#: could disable it, so every build here proves drafts are still filtered.
DRAFT_CANARY = "WOMBATRIX"

_DRAFT_NOTE = f"""---
id: 66666666-7777-8888-9999-000000000000
title: Synthetic draft probe
kind: vault
content_type: markdown
draft: true
---

# Synthetic draft probe

Draft canary {DRAFT_CANARY}, used as the control for the draft filter.
"""

#: A published note linking to the confidential one, so each build exercises a
#: dangling wikilink to a filtered page.
_LINKER_NOTE = """---
id: 77777777-8888-9999-aaaa-bbbbbbbbbbbb
title: Synthetic linker probe
kind: vault
content_type: markdown
---

# Synthetic linker probe

This links to [[confidential-probe]], which the build drops.
"""


def _stage_vault(tmp_path: Path) -> Path:
    """Fixture vault plus the confidential, draft, and linking probes."""
    from tests import quartz_e2e_helper as helper

    vault = helper.stage_fixture_vault(tmp_path / "vault")
    (vault / "confidential-probe.md").write_text(_CONFIDENTIAL_NOTE)
    (vault / "draft-probe.md").write_text(_DRAFT_NOTE)
    (vault / "linker-probe.md").write_text(_LINKER_NOTE)
    return vault


def _stage_workspace(tmp_path: Path, *, register_filter: bool = True) -> Path:
    """Copy the live Quartz workspace and apply this repo's overlay onto it.

    A COPY, always: the user's `~/brain-vault/.quartz` is their real wiki
    workspace and a test must never mutate it. Copying also means the build
    exercises THIS repo's overlay rather than whatever overlay happens to be
    deployed — an earlier manual run silently tested the deployed version and
    produced a confidently wrong answer.

    ``register_filter=False`` removes ``RemoveConfidential`` from the config's
    ``filters`` array so the ``contentIndex`` layer can be tested on its own.
    """
    from tests import quartz_e2e_helper as helper

    workspace = tmp_path / f"workspace-{'full' if register_filter else 'indexonly'}"
    shutil.copytree(
        helper.DEFAULT_QUARTZ_WORKSPACE,
        workspace,
        symlinks=True,
        ignore=shutil.ignore_patterns("public", ".git"),
    )
    for rel in (
        Path("quartz.config.ts"),
        Path("quartz/plugins/filters/sensitivity.ts"),
        Path("quartz/plugins/emitters/contentIndex.ts"),
    ):
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OVERRIDES / rel, dest)

    if not register_filter:
        config = workspace / "quartz.config.ts"
        text = config.read_text(encoding="utf-8")
        patched = text.replace(
            "filters: [Plugin.RemoveDrafts(), RemoveConfidential()],",
            "filters: [Plugin.RemoveDrafts()],",
        )
        assert patched != text, (
            "could not unregister RemoveConfidential — the config's filters "
            "line changed shape and this helper needs updating"
        )
        config.write_text(patched, encoding="utf-8")
    return workspace


# --------------------------------------------------------------------------
# Static: the plugin exists and is REGISTERED
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def filter_source() -> str:
    assert FILTER_PATH.is_file(), f"missing filter plugin at {FILTER_PATH}"
    return FILTER_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def config_source() -> str:
    assert CONFIG_PATH.is_file(), f"missing overlay config at {CONFIG_PATH}"
    return CONFIG_PATH.read_text(encoding="utf-8")


def test_filter_gates_on_should_publish(filter_source: str) -> None:
    """The gate is ``shouldPublish`` — the only hook that precedes every emitter.

    An emitter-level check removes a document from ONE output. ``shouldPublish``
    removes it from the build, which is the difference between hiding a note and
    not publishing it.
    """
    assert "shouldPublish" in filter_source, (
        "the confidential gate must be a QuartzFilterPlugin's shouldPublish; "
        "filtering inside an emitter leaves the page, RSS feed and tag pages "
        "published"
    )
    assert 'sensitivity !== "confidential"' in filter_source


def test_filter_is_registered_in_the_config(config_source: str) -> None:
    """THE ROT-PRONE HALF: a correct plugin that nothing calls does nothing.

    The plugin file could stay perfect while a config edit drops it from
    ``filters``, and every other test here would still pass.
    """
    assert "RemoveConfidential" in config_source, (
        "RemoveConfidential must be imported in quartz.config.ts"
    )
    assert "filters: [Plugin.RemoveDrafts(), RemoveConfidential()]" in config_source, (
        "RemoveConfidential must be registered in the `filters` array — an "
        "unregistered filter silently publishes every confidential note"
    )


def test_draft_and_confidential_are_separate_filters(config_source: str) -> None:
    """Two filters, not one branch — they encode different guarantees.

    ``draft`` means "not ready to show"; ``confidential`` means "must not leak".
    Sharing the mechanism is fine and sharing the branch is what allowed a
    publish guarantee to be assumed rather than checked. Keeping them separate
    also keeps us off upstream's ``RemoveDrafts``.
    """
    assert "Plugin.RemoveDrafts()" in config_source, (
        "upstream's draft filter must remain registered"
    )
    assert "RemoveConfidential()" in config_source
    # Asserts the filter does not IMPORT or CALL RemoveDrafts — not that it
    # never names it. Its docstring deliberately explains the relationship, and
    # a bare substring check on the name would forbid documenting the very
    # decision this test exists to protect.
    filter_src = FILTER_PATH.read_text(encoding="utf-8")
    code_lines = [
        line
        for line in filter_src.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//", "*/"))
    ]
    assert not any("RemoveDrafts" in line for line in code_lines), (
        "the confidential filter must not import, wrap, or delegate to "
        "RemoveDrafts — the two encode different guarantees and must be able "
        "to change independently"
    )


# --------------------------------------------------------------------------
# e2e: a real build must not contain the canary ANYWHERE
# --------------------------------------------------------------------------


@pytest.mark.e2e
def test_confidential_note_appears_nowhere_in_a_real_build(tmp_path: Path) -> None:
    """Build the site for real; assert the canary is absent from EVERY file.

    Greps the whole output tree rather than the files we expect to be risky.
    That is deliberate: the previous enumeration of risky files missed the RSS
    feed and the tag listing pages, and a test written from that same list would
    have passed while the body was live on the public wiki.

    Skips cleanly when the JS toolchain or the live Quartz workspace is absent —
    the brain test image has neither by design.
    """
    from tests import quartz_e2e_helper as helper

    pre = helper.preflight()
    if not pre.ok:
        pytest.skip(pre.skip_reason)

    vault = _stage_vault(tmp_path)
    workspace = _stage_workspace(tmp_path)

    out = tmp_path / "build"
    helper.quartz_build(vault=vault, output=out, workspace=workspace)

    built = [p for p in out.rglob("*") if p.is_file()]
    assert built, "the build produced no files — the harness is broken"

    offenders = [
        p.relative_to(out)
        for p in built
        if CANARY in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, (
        f"confidential body leaked into the published site: {offenders}. "
        f"shouldPublish must drop the file before any emitter runs."
    )
    # The FILE must be absent, not merely blank. A page that exists with an
    # empty body still confirms a note lives at that slug, and the slug is
    # derived from the title — the same membership-is-content argument that
    # made snippet redaction insufficient in search, applied to publishing.
    assert not (out / "confidential-probe.html").exists(), (
        "no HTML page may be emitted for a confidential note — a page that is "
        "merely unindexed, or present but empty, still leaks the note's "
        "existence and title to anyone who guesses the URL"
    )

    # The slug/title must not appear in any sidecar either (sitemap, RSS,
    # index shards). A title in a sitemap makes the note's existence public
    # even when the page itself is gone.
    title_leaks = [
        p.relative_to(out)
        for p in built
        if "Synthetic confidential probe" in p.read_text(
            encoding="utf-8", errors="ignore"
        )
    ]
    assert not title_leaks, f"confidential TITLE leaked into: {title_leaks}"

    # Adding a filter alongside RemoveDrafts could disable it through a
    # plugin-ordering or array-construction mistake. Prove drafts still work.
    draft_leaks = [
        p.relative_to(out)
        for p in built
        if DRAFT_CANARY in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not draft_leaks, (
        f"the DRAFT filter regressed while fixing confidential: {draft_leaks}"
    )

    # Controls: the build is not simply empty, and the note that links to the
    # dropped page still published — a dangling wikilink to a filtered note
    # must not fail the build, or a user with one link to a confidential note
    # could never publish at all.
    assert (out / "index.html").exists()
    assert (out / "linker-probe.html").exists(), (
        "a note linking to a filtered note must still publish; a broken-link "
        "warning is acceptable, a failed build is not"
    )


@pytest.mark.e2e
def test_content_index_filter_works_on_its_own(tmp_path: Path) -> None:
    """DEFENCE IN DEPTH, tested in isolation — otherwise it is dead code.

    In a normal build ``RemoveConfidential`` drops the file before any emitter
    runs, so the ``contentIndex`` branch never executes. An untested second
    layer gives false comfort: it could rot to a no-op and every other test here
    would stay green.

    So this builds with the filter deliberately **unregistered**, leaving the
    index branch as the only defence, and asserts it still removes the note from
    ``contentIndex.json``.

    It also pins WHY the second layer is not sufficient alone — the HTML page
    IS emitted in this configuration. That is the measurement that produced the
    filter-plugin fix, kept executable so nobody re-derives it.
    """
    from tests import quartz_e2e_helper as helper

    pre = helper.preflight()
    if not pre.ok:
        pytest.skip(pre.skip_reason)

    vault = _stage_vault(tmp_path)
    workspace = _stage_workspace(tmp_path, register_filter=False)

    out = tmp_path / "build-index-only"
    helper.quartz_build(vault=vault, output=out, workspace=workspace)

    index_json = out / "static" / "contentIndex.json"
    assert index_json.is_file(), "the build must still emit a content index"
    payload = json.loads(index_json.read_text(encoding="utf-8"))
    assert not [k for k in payload if "confidential-probe" in k], (
        "the contentIndex branch must drop the confidential slug on its own — "
        "it is the second layer and must not rot into a no-op"
    )
    assert payload, "control: the index is not simply empty"

    # ...and the reason a second layer alone is NOT enough.
    assert (out / "confidential-probe.html").exists(), (
        "expected the page to still be emitted without the filter plugin; if "
        "this fails, Quartz's behaviour changed and the filter may no longer "
        "be the necessary layer — re-measure before simplifying"
    )
