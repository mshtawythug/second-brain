"""Phase 7 (R1, "Demoted"): every daily-loop surface points at `brain ui`.

Spec §1.3: at R1 "`brain ui` is primary. The wiki still builds and serves;
nothing in the daily loop points at it." Each test below pins one surface the
phase-7 inventory (``docs/plans/2026-10-12-wiki-to-ui-phase7.md`` §2) repointed,
so a revert to the wiki fails here. ``brain-up``'s pins live with the other
bin-script tests in ``tests/test_bin_scripts.py``.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"
PHONE_GUIDE = REPO_ROOT / "docs" / "guides" / "brain-ui-on-your-phone.md"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
VAULT_AND_WIKI = REPO_ROOT / "docs" / "vault-and-wiki.md"

#: The recipe the docs shipped for reading from another device. It cannot work:
#: a wildcard bind gets the loopback Host allowlist (pinned in
#: ``tests/test_ui_security.py``), so a phone is refused with 400.
_WILDCARD_RECIPE = re.compile(
    r"brain ui[^\n`]*--host\s+(?:0\.0\.0\.0|\[?::\]?)(?![0-9A-Za-z.:\]])"
)


# --------------------------------------------------------------------------
# E4 — `brain setup`'s closing report
# --------------------------------------------------------------------------


def _setup_report(capsys: pytest.CaptureFixture[str], tmp_path: Path, *, wiki: bool) -> list[str]:
    from brain.setup import _print_final_report

    _print_final_report(
        brain_home=tmp_path,
        vault=tmp_path / "vault",
        wiki_port=8080,
        wiki_installed=wiki,
        skill_installed=False,
        effective_embedder="arctic",
        ollama_fell_back=False,
    )
    return capsys.readouterr().out.splitlines()


def _read_line(lines: list[str]) -> int:
    hits = [i for i, line in enumerate(lines) if line.strip().startswith("read:")]
    assert hits and "brain ui" in lines[hits[0]], lines
    return hits[0]


@pytest.mark.parametrize("wiki_installed", [True, False])
def test_setup_report_names_brain_ui_as_the_reading_surface(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, wiki_installed: bool
) -> None:
    _read_line(_setup_report(capsys, tmp_path, wiki=wiki_installed))


def test_setup_report_names_brain_ui_before_the_legacy_wiki_url(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """M-2 (phase-7 review): the ORDER, not just the label.

    The first version of this test was named for the order and asserted only
    that the wiki line said "legacy", while the report printed the wiki URL
    above everything else. Both halves are asserted now.
    """
    lines = _setup_report(capsys, tmp_path, wiki=True)
    wiki = [i for i, line in enumerate(lines) if "http://localhost:8080" in line]
    assert wiki and "legacy" in lines[wiki[0]], lines
    assert _read_line(lines) < wiki[0], lines


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
    """Spec Q1: how the token is supplied, the caveats, and that the wiki stays."""
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
        "wiki",
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


def test_readme_intro_names_the_wiki_legacy_not_an_equal_reader() -> None:
    """F16 (phase-7 audit): inventory row #5 repointed README's opening sentence,
    and nothing pinned it — reverting to "the same corpus through the CLI, a
    local web UI, or a rendered wiki" stayed green."""
    text = README.read_text(encoding="utf-8")
    assert "or a [rendered wiki]" not in text, (
        "README's intro lists the wiki as an equal reading surface again"
    )
    assert re.search(r"\[rendered wiki\]\([^)]*\)[^\n.]*legacy", text), (
        "README's intro must call the rendered wiki legacy"
    )


def test_vault_and_wiki_marks_the_wiki_as_legacy() -> None:
    text = VAULT_AND_WIKI.read_text(encoding="utf-8")
    intro = text[: text.index("## Vault model")]
    assert "brain ui" in intro and "legacy" in intro.lower(), intro


def test_vault_and_wiki_documents_the_brain_up_default_truthfully() -> None:
    """M-4 (phase-7 audit): the default flipped to 0, and the doc said so in one
    place while two others still described brain-up opening the browser.

    The table row is pinned exactly, because a row stating the old default
    reads as authoritative and survived the first round unpinned.
    """
    text = VAULT_AND_WIKI.read_text(encoding="utf-8")
    assert re.search(r"^\| `BRAIN_OPEN_BROWSER` \| `0` \|", text, re.MULTILINE), (
        "the env table must state BRAIN_OPEN_BROWSER's default as `0`"
    )
    assert "and opens the browser" not in text
    assert "→ open browser" not in text


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


def test_docs_index_calls_the_rendered_wiki_legacy() -> None:
    """S20 (phase-7 audit): the docs index row for vault-and-wiki.md was
    repointed to call the wiki legacy, and nothing pinned it."""
    row = next(
        line for line in DOCS_INDEX.read_text(encoding="utf-8").splitlines()
        if line.startswith("| [vault-and-wiki.md]")
    )
    assert "the legacy Quartz + Caddy rendered wiki" in row, row
    assert "`brain ui` is the primary reading surface" in row, row
