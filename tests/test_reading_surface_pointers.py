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
_WILDCARD_RECIPE = re.compile(r"brain ui[^\n`]*--host\s+(0\.0\.0\.0|::)\b")


# --------------------------------------------------------------------------
# E4 — `brain setup`'s closing report
# --------------------------------------------------------------------------


@pytest.mark.parametrize("wiki_installed", [True, False])
def test_setup_next_steps_name_brain_ui(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, wiki_installed: bool
) -> None:
    from brain.setup import _print_final_report

    _print_final_report(
        brain_home=tmp_path,
        vault=tmp_path / "vault",
        wiki_port=8080,
        wiki_installed=wiki_installed,
        skill_installed=False,
        effective_embedder="arctic",
        ollama_fell_back=False,
    )
    out = capsys.readouterr().out
    steps = out[out.index("next steps:"):]
    assert "brain ui" in steps, out


def test_setup_report_puts_brain_ui_before_the_wiki_url(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """With the wiki installed, its URL is labelled legacy, not presented as THE reader."""
    from brain.setup import _print_final_report

    _print_final_report(
        brain_home=tmp_path,
        vault=tmp_path / "vault",
        wiki_port=8080,
        wiki_installed=True,
        skill_installed=False,
        effective_embedder="arctic",
        ollama_fell_back=False,
    )
    lines = capsys.readouterr().out.splitlines()
    wiki = [line for line in lines if "http://localhost:8080" in line]
    assert wiki and "legacy" in wiki[0], lines


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
        "in memory only",
        "never saved on the device",
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


def test_vault_and_wiki_marks_the_wiki_as_legacy() -> None:
    text = VAULT_AND_WIKI.read_text(encoding="utf-8")
    intro = text[: text.index("## Vault model")]
    assert "brain ui" in intro and "legacy" in intro.lower(), intro
