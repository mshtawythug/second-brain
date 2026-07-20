"""Drift guards for the README "Claude integrations" MCP GIF, its VHS tape, and regenerator.

``docs/assets/mcp.gif`` shows the ``claude`` CLI answering a question BY CALLING
the bundled ``brain-mcp`` MCP server — the headline pitch ("searchable by any AI
coding agent") shown end-to-end. It is recorded from ``docs/assets/mcp.tape`` via
``bin/brain-mcp-gif``, which drives the REAL agent against a throwaway,
fully-isolated :55441 Postgres sandbox seeded with synthetic Larkspur notes.

These tests keep the asset, its tape, and its regenerator from silently drifting
apart, assert the README embeds the GIF, and — because the recorder spins up a
live agent + ``brain-mcp`` — pin the safety invariants that keep it off the
prod / demo / test databases.
"""
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "assets"
README = REPO_ROOT / "README.md"

MCP_GIF = ASSETS / "mcp.gif"
MCP_TAPE = ASSETS / "mcp.tape"
MCP_SCRIPT = REPO_ROOT / "bin" / "brain-mcp-gif"

# README size budget (bytes). Mirrors the ceiling the regenerator enforces (2.5 MB).
MCP_MAX_BYTES = 2560 * 1024

# The isolated sandbox the recorder is allowed to touch, and the databases it
# must NEVER connect to (prod / demo / test). The forbidden checks look for a
# connection form (``:PORT/`` in a DB URL) rather than the bare number, so the
# tape/script may still name those ports in a comment explaining what they avoid.
MCP_SANDBOX_PORT = "55441"
FORBIDDEN_DB_URLS = (":55432/", ":55433/", ":5434/")


# --------------------------------------------------------------------------- #
# Existence + budget                                                          #
# --------------------------------------------------------------------------- #
def test_mcp_tape_exists() -> None:
    assert MCP_TAPE.is_file(), f"missing MCP tape: {MCP_TAPE}"


def test_mcp_gif_exists_and_is_nonempty() -> None:
    assert MCP_GIF.is_file(), f"missing GIF: {MCP_GIF}"
    assert MCP_GIF.stat().st_size > 0, f"empty GIF: {MCP_GIF}"


def test_mcp_gif_within_readme_budget() -> None:
    assert MCP_GIF.stat().st_size <= MCP_MAX_BYTES, (
        f"{MCP_GIF.name} exceeds the {MCP_MAX_BYTES} byte README budget"
    )


def test_mcp_regenerator_is_executable() -> None:
    assert MCP_SCRIPT.is_file(), f"missing regenerator: {MCP_SCRIPT}"
    mode = MCP_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"regenerator not executable: {MCP_SCRIPT}"
    assert os.access(MCP_SCRIPT, os.X_OK), f"regenerator not executable: {MCP_SCRIPT}"


# --------------------------------------------------------------------------- #
# Tape <-> GIF self-consistency                                               #
# --------------------------------------------------------------------------- #
def test_tape_outputs_its_own_gif() -> None:
    assert "Output docs/assets/mcp.gif" in MCP_TAPE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# README references the asset                                                 #
# --------------------------------------------------------------------------- #
def test_readme_embeds_mcp_gif() -> None:
    text = README.read_text(encoding="utf-8")
    assert "](docs/assets/mcp.gif)" in text, "README does not embed the MCP GIF"


# --------------------------------------------------------------------------- #
# Recorder safety invariants (it drives a live agent + brain-mcp)             #
# --------------------------------------------------------------------------- #
def test_mcp_tape_targets_only_the_sandbox_port() -> None:
    text = MCP_TAPE.read_text(encoding="utf-8")
    assert f":{MCP_SANDBOX_PORT}/" in text, "MCP tape lost its :55441 sandbox guard"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"MCP tape connects to a forbidden DB ({db_url}) — prod/demo/test"
        )


def test_mcp_tape_fails_closed_on_wrong_database() -> None:
    """The tape must hard-abort unless DATABASE_URL is the :55441 sandbox."""
    text = MCP_TAPE.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text, "MCP tape does not assert on DATABASE_URL"
    assert "exit 1" in text, "MCP tape has no fail-closed abort"


def test_mcp_script_scopes_teardown_and_uses_named_volume() -> None:
    text = MCP_SCRIPT.read_text(encoding="utf-8")
    # Isolated compose project + its own port, never prod/demo/test.
    assert "brain-mcp-gif" in text, "MCP script lost its isolated project name"
    assert MCP_SANDBOX_PORT in text, "MCP script no longer pins port 55441"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"MCP script connects to a forbidden DB ({db_url}) — prod/demo/test"
        )
    # Teardown scoped to our own project, with a named volume (never a bind mount).
    assert "down -v" in text, "MCP script does not tear its sandbox volume down"
    assert '-p "$PROJECT"' in text or "-p brain-mcp-gif" in text, (
        "MCP script teardown is not scoped to the brain-mcp-gif project"
    )
    assert "/var/lib/postgresql/data" in text, "MCP script lost its pg data volume"
    assert "./data/postgres" not in text, (
        "MCP script must not bind-mount the prod-style ./data/postgres path"
    )


def test_mcp_script_points_brain_mcp_at_sandbox_via_config_env() -> None:
    """brain-mcp is pointed at the sandbox purely through the MCP config's env
    block, and the recorder fails closed before recording if it is not."""
    text = MCP_SCRIPT.read_text(encoding="utf-8")
    # The seeding CLI is redirected via the environment (wins over a repo-root .env).
    assert "export DATABASE_URL=" in text, (
        "MCP script must export DATABASE_URL so it wins over any repo-root .env"
    )
    assert "export BRAIN_VAULT_PATH=" in text, (
        "MCP script must redirect BRAIN_VAULT_PATH away from the real vault"
    )
    # The generated MCP config carries the sandbox DATABASE_URL into brain-mcp's env.
    assert '"DATABASE_URL"' in text, (
        "MCP script does not set DATABASE_URL in the generated MCP config env"
    )
    # Fail-closed guard before a single frame is recorded.
    assert "refusing to record" in text, (
        "MCP script has no fail-closed pre-record guard"
    )
