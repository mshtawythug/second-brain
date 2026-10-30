"""Every daily-loop surface points at `brain ui`, and none at the deleted wiki.

Phase 7 (R1, "Demoted") repointed each surface the phase-7 inventory
(``docs/plans/2026-10-12-wiki-to-ui-phase7.md`` §2) named at `brain ui`; phase 8
(R2) made the wiki dormant; phase 9 (R3) deleted it. The doc pins below were
"points at brain ui / calls the wiki legacy or dormant" until R3; they are now
"points at brain ui and names no removed wiki surface", so a doc that regresses
to documenting the wiki fails here. ``brain-up``'s pins live with the other
bin-script tests in ``tests/test_bin_scripts.py``.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tests.test_brain_skills_sync import _skill_reference_files

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"
PHONE_GUIDE = REPO_ROOT / "docs" / "guides" / "brain-ui-on-your-phone.md"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
VAULT_DOC = REPO_ROOT / "docs" / "vault.md"
CONFIGURATION = REPO_ROOT / "docs" / "configuration.md"

#: The recipe the docs shipped for reading from another device. It cannot work:
#: a wildcard bind gets the loopback Host allowlist (pinned in
#: ``tests/test_ui_security.py``), so a phone is refused with 400.
_WILDCARD_RECIPE = re.compile(
    r"brain ui[^\n`]*--host\s+(?:0\.0\.0\.0|\[?::\]?)(?![0-9A-Za-z.:\]])"
)


# --------------------------------------------------------------------------
# E4 — `brain setup`'s closing report
# --------------------------------------------------------------------------


def _setup_report(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> list[str]:
    from brain.setup import _print_final_report

    _print_final_report(
        brain_home=tmp_path,
        vault=tmp_path / "vault",
        skill_installed=False,
        effective_embedder="arctic",
        ollama_fell_back=False,
    )
    return capsys.readouterr().out.splitlines()


def _read_line(lines: list[str]) -> int:
    hits = [i for i, line in enumerate(lines) if line.strip().startswith("read:")]
    assert hits and "brain ui" in lines[hits[0]], lines
    return hits[0]


def test_setup_report_names_brain_ui_as_the_reading_surface(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _read_line(_setup_report(capsys, tmp_path))


def test_setup_report_names_no_wiki(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """R3 (phase 9): the legacy wiki URL and its ``caddy run`` hint are gone.

    Phase 7 (M-2) pinned ``brain ui`` ABOVE the legacy wiki URL; R3 deleted the
    wiki, so the report names no wiki and no Caddy at all.
    """
    lines = _setup_report(capsys, tmp_path)
    text = "\n".join(lines).lower()
    assert "localhost:8080" not in text and "caddy" not in text and "wiki" not in text, lines


@pytest.mark.parametrize(
    "line",
    [
        "brain ui --host 0.0.0.0 --token s",
        "brain ui --host :: --token s",
        "brain ui --host [::] --token s",
        "`brain ui --host 0.0.0.0`",
    ],
)
def test_wildcard_recipe_pattern_catches_every_wildcard(line: str) -> None:
    """L-2: the pattern missed ``--host ::`` (``\\b`` needs a word character)."""
    assert _WILDCARD_RECIPE.search(line), line


@pytest.mark.parametrize(
    "line",
    [
        "brain ui --host ::1 --token s",
        "brain ui --host 0.0.0.01 --token s",
        "brain ui --host 192.0.2.10 --token s",
        "brain ui --lan",
    ],
)
def test_wildcard_recipe_pattern_spares_real_addresses(line: str) -> None:
    """L-2: ``::1`` is loopback, not a wildcard — the old pattern flagged it."""
    assert not _WILDCARD_RECIPE.search(line), line


# --------------------------------------------------------------------------
# E5 — the MCP `brain_capture` description, and its CLI docstring twin
# --------------------------------------------------------------------------


def _capture_docstring() -> str:
    """The `capture` callback's docstring — NOT its `--help` text.

    ``capture_app`` passes ``help=`` to ``typer.Typer``, which overrides the
    callback docstring, so this sentence never reaches a terminal (measured:
    ``brain capture --help`` contains no "Quartz"). It is pinned anyway because
    it is the MCP description's twin, and the two drifting apart is how one of
    them ends up wrong.
    """
    from brain._capture_command import capture

    return capture.__doc__ or ""


def _mcp_capture_description() -> str:
    from brain import mcp_server

    tools = asyncio.run(mcp_server.mcp_app.list_tools())
    (tool,) = [tool for tool in tools if tool.name == "brain_capture"]
    return tool.description or ""


def test_capture_docstring_names_brain_ui_not_quartz() -> None:
    text = _capture_docstring()
    assert "brain ui" in text, text
    assert "Quartz" not in text, text


def test_mcp_capture_description_names_brain_ui_not_quartz() -> None:
    text = _mcp_capture_description()
    assert "brain ui" in text, text
    assert "Quartz" not in text, text


# --------------------------------------------------------------------------
# E6 — the docs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [README, CLI_REFERENCE], ids=lambda p: p.name)
def test_docs_do_not_ship_the_wildcard_phone_recipe(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert not _WILDCARD_RECIPE.search(text), (
        f"{doc.name} documents `brain ui --host 0.0.0.0`, which refuses every "
        "other device — document `--lan` or `--host <LAN IP>` instead"
    )


@pytest.mark.parametrize("doc", [README, CLI_REFERENCE], ids=lambda p: p.name)
def test_docs_point_at_the_phone_guide_and_lan(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert "brain-ui-on-your-phone.md" in text
    assert "brain ui --lan" in text


def test_phone_guide_covers_the_q1_contract() -> None:
    """Spec Q1: how the token is supplied and the caveats.

    Q1 also required the guide to say the wiki stays; R3 deleted it, so that
    needle moved to :func:`test_phone_guide_warns_about_a_leftover_wiki_caddy`.
    """
    text = PHONE_GUIDE.read_text(encoding="utf-8")
    for needle in (
        "brain ui --lan",
        "--host",
        "--token",
        "#t=",
        "0.0.0.0",
        "--read-only",
        "--include-confidential",
        "trust",
        "it is the token that keeps other clients out",
        "judged by the address it will bind",
        "Zones are refused even on link-local addresses",
        "in memory only",
        "page itself never saves the token",
        "the URL is the token",
        "Treat the printed URL as a password",
    ):
        assert needle in text, f"phone guide is missing {needle!r}"
    # The guide NAMES the wildcard bind, as the trap — so it must say why.
    assert "400" in text, "the guide must explain why 0.0.0.0 refuses a phone"


def test_docs_index_lists_the_phone_guide() -> None:
    assert "brain-ui-on-your-phone.md" in DOCS_INDEX.read_text(encoding="utf-8")


def test_readme_names_brain_ui_as_the_reading_surface() -> None:
    text = README.read_text(encoding="utf-8")
    section = text[text.index("## Local web UI"):]
    section = section[: section.index("\n## ", 1)]
    assert "primary" in section.lower(), section


#: Every user-facing surface R3 (phase 9) deleted. A doc naming one of these is
#: documenting something that no longer exists.
_REMOVED_WIKI_SURFACES = (
    "brain vault render",
    "brain wiki install",
    "--only quartz",
    "--clean-cache",
    "--skip-wiki",
    "--wiki-port",
    "BRAIN_OPEN_BROWSER",
    "BRAIN_WIKI_BUILD",
    "BRAIN_WIKI_PORT",
    "BRAIN_WIKI_KEEP_BUILDS",
    "BRAIN_WIKI_RELOAD",
    "BRAIN_NO_OVERLAY",
    "BRAIN_FASTPATH_ENABLED",
    "brain-verify-fastpath",
    "quartz.config.ts",
    "brain.test",
)


def _removed_surfaces_in(text: str) -> list[str]:
    return [needle for needle in _REMOVED_WIKI_SURFACES if needle in text]


def _vault_doc_body() -> str:
    """The vault doc after its intro — the intro's retirement callout may NAME
    the removed commands to say they are gone; nothing after it may."""
    text = VAULT_DOC.read_text(encoding="utf-8")
    return text[text.index("## Vault model"):]


def test_readme_intro_names_no_rendered_wiki() -> None:
    """F16 (phase-7 audit) pinned the intro calling the wiki legacy; R3 deleted
    it, so the intro must not offer it as a reading surface at all."""
    intro = README.read_text(encoding="utf-8").split("\n## ", 1)[0]
    assert "rendered wiki" not in intro, intro
    assert "brain ui" in intro or "(#local-web-ui)" in intro, intro
    assert "(docs/vault.md)" in intro, intro


@pytest.mark.parametrize(
    "doc",
    [README, CLI_REFERENCE, PHONE_GUIDE, DOCS_INDEX, CONFIGURATION],
    ids=lambda p: p.name,
)
def test_reading_docs_name_no_removed_wiki_surface(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    assert not _removed_surfaces_in(text), (doc.name, _removed_surfaces_in(text))


@pytest.mark.parametrize(
    "skill",
    [*sorted((REPO_ROOT / "skills").glob("*/SKILL.md")), *_skill_reference_files()],
    ids=lambda p: p.relative_to(REPO_ROOT / "skills").as_posix(),
)
def test_skills_name_no_removed_wiki_surface(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    where = skill.relative_to(REPO_ROOT).as_posix()
    assert not _removed_surfaces_in(text), (where, _removed_surfaces_in(text))


def test_cli_reference_setup_and_doctor_name_no_wiki_dependency() -> None:
    """R3 dropped the setup wiki offer and doctor's ``quartz/npx`` line; the
    reference said "optionally wires the wiki (Caddy + Quartz)" and listed npx."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    assert "Quartz" not in text, "cli-reference still describes the Quartz wiki"
    assert "npx" not in text, "cli-reference still lists the deleted npx doctor check"


def test_vault_doc_points_at_brain_ui_and_names_the_retirement() -> None:
    text = VAULT_DOC.read_text(encoding="utf-8")
    intro = text[: text.index("## Vault model")]
    assert "brain ui" in intro and "retired" in intro, intro
    # Spec R3: the upgrade leftovers are removed by hand, and the doc says which.
    for leftover in ("<vault>/.quartz/", "$BRAIN_HOME/Caddyfile", "$BRAIN_HOME/build/"):
        assert leftover in intro, leftover
    assert "Wiki (rendered view" not in text
    assert not _removed_surfaces_in(_vault_doc_body()), _removed_surfaces_in(_vault_doc_body())


def test_no_live_doc_links_the_renamed_vault_doc() -> None:
    """Spec §9.5: ``docs/vault-and-wiki.md`` became ``docs/vault.md``. A link to
    the old name is a 404. Historical docs (specs/plans/handoff/audits) keep it."""
    docs = REPO_ROOT / "docs"
    live = [README, *docs.glob("*.md"), *(docs / "guides").glob("*.md")]
    live += [REPO_ROOT / name for name in ("CONTRIBUTING.md", "SECURITY.md", "AGENTS.md")]
    live += sorted((REPO_ROOT / "skills").glob("*/SKILL.md")) + _skill_reference_files()
    stale = re.compile(r"(?:docs/|\]\()vault-and-wiki\.md")
    hits = [
        str(p.relative_to(REPO_ROOT))
        for p in live
        if stale.search(p.read_text(encoding="utf-8"))
    ]
    assert not hits, hits
    assert not (REPO_ROOT / "docs" / "vault-and-wiki.md").exists()


def _section(text: str, heading: str) -> str:
    """The body of a ``## heading`` section, up to the next ``## `` heading."""
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_cli_reference_names_brain_ui_the_primary_reading_surface() -> None:
    """S19 (phase-7 audit): reverting the section to "A local, single-user web
    app" — the pre-phase wording — stayed green."""
    section = _section(CLI_REFERENCE.read_text(encoding="utf-8"), "## brain ui")
    assert "The primary reading surface" in section, section


def test_cli_reference_documents_three_confidential_modes() -> None:
    """Round 16c (audit of `e0d6067`): the section said bodies are "always
    served" on loopback and `--include-confidential` "is ignored" — false for a
    loopback alias, which withholds them unless the flag is passed."""
    section = " ".join(_section(CLI_REFERENCE.read_text(encoding="utf-8"), "## brain ui").split())
    assert "`--include-confidential` is ignored" not in section, section
    assert "which of the two you got" not in section, section
    assert "A **loopback alias**" in section, section
    assert "which of the three you got" in section, section
    # Round 16c (review): localhost is loopback only when it resolves only to
    # loopback; a mixed answer is a non-loopback bind.
    assert "or on **`localhost` when it resolves only to loopback**" in section, section


def test_phone_guide_says_a_loopback_alias_withholds_bodies() -> None:
    """Round 16c: "On loopback they are always served" no longer holds for a
    name other than localhost that resolves to loopback."""
    text = " ".join(PHONE_GUIDE.read_text(encoding="utf-8").split())
    assert "On loopback they are always served" not in text, text
    assert (
        "On a loopback address, or on `localhost` when it resolves only to loopback, "
        "they are always served" in text
    ), text
    assert "guarded like a LAN bind" in text, text


def test_docs_index_row_points_at_the_vault_doc() -> None:
    """S20 (phase-7 audit) pinned this row calling the wiki legacy; R3 renamed
    the doc and deleted the wiki, so the row must name ``brain ui`` and no wiki."""
    row = next(
        line for line in DOCS_INDEX.read_text(encoding="utf-8").splitlines()
        if line.startswith("| [vault.md](vault.md)")
    )
    assert "`brain ui` is the reading surface" in row, row
    assert "Caddy" not in row and "legacy Quartz" not in row, row


# --------------------------------------------------------------------------
# Phase 8 (R2, "Dormant"), E11: no doc or skill still describes a wiki that
# rebuilds itself. Each pin fails if its sentence reverts.
# --------------------------------------------------------------------------

MAINTENANCE_SKILL = REPO_ROOT / "skills" / "brain-maintenance" / "SKILL.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_REBUILD_ON_REQUEST = "brain-rebuild --only quartz"


def test_vault_doc_documents_the_r3_rebuild_flags() -> None:
    """R2 pinned ``--only quartz`` here; R3 deleted that stage, leaving ``mirror``."""
    section = _section(VAULT_DOC.read_text(encoding="utf-8"), "## Daily use — `bin/` scripts")
    assert "--only mirror" in section, section
    assert "quartz" not in section.lower(), section
    assert "start build watcher" not in section


def test_phone_guide_warns_about_a_leftover_wiki_caddy() -> None:
    """R2 pinned "the wiki is dormant" here. R3 deleted the wiki but not a Caddy
    the user started for it, which serves the LAN with no token — say so."""
    text = PHONE_GUIDE.read_text(encoding="utf-8")
    section = " ".join(_section(text, "## The wiki is gone").split())
    for needle in ("brain ui", "Caddy", ":8080", "no token", "<vault>/.quartz/"):
        assert needle in section, needle
    assert "fallback" not in section, "RA-8 was rejected: the wiki is not a fallback"


def test_maintenance_skill_names_no_wiki_rebuild() -> None:
    text = MAINTENANCE_SKILL.read_text(encoding="utf-8")
    assert "--only mirror" in text
    assert "dormant since R2" not in text
    assert "watcher B (vault→Quartz)" not in text


def test_changelog_warns_that_the_next_brain_up_removes_the_build_daemon() -> None:
    """Plan §3: the migration runs itself on the next ``brain-up``, so it must not surprise."""
    unreleased = CHANGELOG.read_text(encoding="utf-8").split("## [Unreleased]", 1)[1]
    unreleased = unreleased.split("\n## [", 1)[0]
    assert "next `brain-up` after upgrading removes the `com.brain.build`" in unreleased
    assert "**The way back:**" in unreleased and _REBUILD_ON_REQUEST in unreleased


def test_changelog_says_the_way_back_to_the_daemon_is_not_durable() -> None:
    """T7 review (LOW): this release's ``brain-up`` reruns ``brain-install-launchd``, which
    removes ``com.brain.build`` again, so a reinstall from an earlier release does not stick."""
    unreleased = CHANGELOG.read_text(encoding="utf-8").split("## [Unreleased]", 1)[1]
    unreleased = " ".join(unreleased.split("\n## [", 1)[0].split())
    assert "This release's next `brain-up` removes it again" in unreleased
    assert "keeping the daemon means staying on the earlier release" in unreleased
