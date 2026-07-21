"""Drift guards for the CLI-reference ``brain ask`` GIF, its tape, and recorder.

``docs/assets/ask.gif`` is the ``brain ask`` clip embedded in
``docs/cli-reference.md``: one ``brain search`` to show the corpus has material,
then ``brain ask "<question>" --explain`` synthesizing a single CITED answer. It
is recorded from ``docs/assets/ask.tape`` by ``bin/brain-ask-gif`` against a
throwaway, fully-isolated Postgres sandbox (compose project ``brain-ask-gif``,
port 55443, named volume, database ``second_brain_ask_gif``).

These tests keep the asset, its tape, its regenerator, and the doc that
references it from silently drifting apart, and — because the recorder drives the
*real* CLI (plus a live Ollama chat model) rather than a self-managed sandbox —
assert the safety invariants that keep it off the prod / demo / test databases.
The pattern mirrors ``tests/test_docs_assets.py`` (the README demo/usage GIFs).
"""
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "assets"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"

ASK_GIF = ASSETS / "ask.gif"
ASK_TAPE = ASSETS / "ask.tape"
ASK_SCRIPT = REPO_ROOT / "bin" / "brain-ask-gif"

# CLI-reference size budget (bytes). Mirrors the ceiling the regenerator
# enforces: the ask clip stays under 2.5 MB.
ASK_MAX_BYTES = 2560 * 1024

# The isolated sandbox the ask recorder is allowed to touch, and the databases
# it must NEVER connect to (prod / demo / test). The forbidden checks look for a
# connection form (``:PORT/`` in a DB URL) rather than the bare number, so the
# tape/script may still name those ports in a comment explaining what they avoid.
ASK_SANDBOX_PORT = "55443"
FORBIDDEN_DB_URLS = (":55432/", ":55433/", ":5434/")


# --------------------------------------------------------------------------- #
# Existence + budget                                                          #
# --------------------------------------------------------------------------- #
def test_ask_tape_exists() -> None:
    assert ASK_TAPE.is_file(), f"missing ask tape: {ASK_TAPE}"


def test_ask_gif_exists_and_is_nonempty() -> None:
    assert ASK_GIF.is_file(), f"missing GIF: {ASK_GIF}"
    assert ASK_GIF.stat().st_size > 0, f"empty GIF: {ASK_GIF}"


def test_ask_gif_within_cli_reference_budget() -> None:
    assert ASK_GIF.stat().st_size <= ASK_MAX_BYTES, (
        f"{ASK_GIF.name} exceeds the {ASK_MAX_BYTES} byte CLI-reference budget"
    )


def test_ask_regenerator_is_executable() -> None:
    assert ASK_SCRIPT.is_file(), f"missing regenerator: {ASK_SCRIPT}"
    mode = ASK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"regenerator not executable: {ASK_SCRIPT}"
    assert os.access(ASK_SCRIPT, os.X_OK), f"regenerator not executable: {ASK_SCRIPT}"


# --------------------------------------------------------------------------- #
# Tape <-> GIF self-consistency                                               #
# --------------------------------------------------------------------------- #
def test_ask_tape_outputs_its_own_gif() -> None:
    assert "Output docs/assets/ask.gif" in ASK_TAPE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The CLI reference embeds the asset                                          #
# --------------------------------------------------------------------------- #
def test_cli_reference_embeds_the_ask_gif() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    assert "](assets/ask.gif)" in text, (
        "docs/cli-reference.md no longer embeds the ask GIF"
    )


# --------------------------------------------------------------------------- #
# Recorder safety invariants (it drives the REAL CLI + a live Ollama model)   #
# --------------------------------------------------------------------------- #
def test_ask_tape_targets_only_the_sandbox_port() -> None:
    text = ASK_TAPE.read_text(encoding="utf-8")
    assert f":{ASK_SANDBOX_PORT}/" in text, "ask tape lost its :55443 sandbox guard"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"ask tape connects to a forbidden DB ({db_url}) — prod/demo/test"
        )


def test_ask_tape_fails_closed_on_wrong_database() -> None:
    """The tape must hard-abort unless DATABASE_URL is the :55443 sandbox."""
    text = ASK_TAPE.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text, "ask tape does not assert on DATABASE_URL"
    assert "exit 1" in text, "ask tape has no fail-closed abort"


def test_ask_script_scopes_teardown_and_uses_named_volume() -> None:
    text = ASK_SCRIPT.read_text(encoding="utf-8")
    # Isolated compose project + its own port, never prod/demo/test.
    assert "brain-ask-gif" in text, "ask script lost its isolated project name"
    assert ASK_SANDBOX_PORT in text, "ask script no longer pins port 55443"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"ask script connects to a forbidden DB ({db_url}) — prod/demo/test"
        )
    # Teardown scoped to our own project, with a named volume (never a bind mount).
    assert "down -v" in text, "ask script does not tear its sandbox volume down"
    assert "-p brain-ask-gif" in text or '-p "$PROJECT"' in text, (
        "ask script teardown is not scoped to the brain-ask-gif project"
    )
    assert "/var/lib/postgresql/data" in text, "ask script lost its pg data volume"
    assert "./data/postgres" not in text, (
        "ask script must not bind-mount the prod-style ./data/postgres path"
    )


def test_ask_script_overrides_database_url_via_env() -> None:
    """The CLI must be pointed at the sandbox purely through the environment."""
    text = ASK_SCRIPT.read_text(encoding="utf-8")
    assert "export DATABASE_URL=" in text, (
        "ask script must export DATABASE_URL so it wins over any repo-root .env"
    )
    assert "export BRAIN_VAULT_PATH=" in text, (
        "ask script must redirect BRAIN_VAULT_PATH away from the real vault"
    )
