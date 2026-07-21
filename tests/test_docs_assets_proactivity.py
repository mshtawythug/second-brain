"""Drift + safety guards for the cli-reference "proactive half" GIF.

One GIF documents the proactivity/assistant commands:

* ``docs/assets/proactivity.gif`` — ``brain brief`` (daily digest) followed by
  ``brain resurface`` (spaced-repetition), recorded from
  ``docs/assets/proactivity.tape`` via ``bin/brain-proactivity-gif`` (the
  regular CLI against a throwaway :55445 Postgres sandbox).

These tests keep the asset, its tape, its regenerator, and the doc that
references it from silently drifting apart, and — because the recorder drives
the *real* CLI rather than a self-managed sandbox — assert the safety
invariants that keep it off the prod / demo / test databases (and off the other
GIF sandboxes).

Mirrors ``tests/test_docs_assets.py`` (the README demo/usage GIFs).
"""
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "assets"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"

PROACTIVITY_GIF = ASSETS / "proactivity.gif"
PROACTIVITY_TAPE = ASSETS / "proactivity.tape"
PROACTIVITY_SCRIPT = REPO_ROOT / "bin" / "brain-proactivity-gif"

# cli-reference size budget (bytes). Mirrors the ceiling the regenerator enforces.
PROACTIVITY_MAX_BYTES = 2560 * 1024

# The isolated sandbox the recorder is allowed to touch, and the databases it
# must NEVER connect to (prod / demo / test). The forbidden checks look for a
# connection form (``:PORT/`` in a DB URL) rather than the bare number, so the
# tape/script may still name those ports in a comment explaining what they avoid.
PROACTIVITY_SANDBOX_PORT = "55445"
FORBIDDEN_DB_URLS = (":55432/", ":55433/", ":5434/")


# --------------------------------------------------------------------------- #
# Existence + budget                                                          #
# --------------------------------------------------------------------------- #
def test_proactivity_tape_exists() -> None:
    assert PROACTIVITY_TAPE.is_file(), f"missing proactivity tape: {PROACTIVITY_TAPE}"


def test_proactivity_gif_exists_and_is_nonempty() -> None:
    assert PROACTIVITY_GIF.is_file(), f"missing GIF: {PROACTIVITY_GIF}"
    assert PROACTIVITY_GIF.stat().st_size > 0, f"empty GIF: {PROACTIVITY_GIF}"


def test_proactivity_gif_within_budget() -> None:
    assert PROACTIVITY_GIF.stat().st_size <= PROACTIVITY_MAX_BYTES, (
        f"{PROACTIVITY_GIF.name} exceeds the {PROACTIVITY_MAX_BYTES} byte "
        "cli-reference budget"
    )


def test_proactivity_regenerator_is_executable() -> None:
    assert PROACTIVITY_SCRIPT.is_file(), f"missing regenerator: {PROACTIVITY_SCRIPT}"
    mode = PROACTIVITY_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"regenerator not executable: {PROACTIVITY_SCRIPT}"
    assert os.access(PROACTIVITY_SCRIPT, os.X_OK), (
        f"regenerator not executable: {PROACTIVITY_SCRIPT}"
    )


# --------------------------------------------------------------------------- #
# Tape <-> GIF self-consistency                                               #
# --------------------------------------------------------------------------- #
def test_tape_outputs_its_own_gif() -> None:
    text = PROACTIVITY_TAPE.read_text(encoding="utf-8")
    assert "Output docs/assets/proactivity.gif" in text, (
        "proactivity tape no longer outputs its own GIF"
    )


def test_tape_records_both_showcased_commands() -> None:
    """The two commands the GIF exists to show must both appear in the tape."""
    text = PROACTIVITY_TAPE.read_text(encoding="utf-8")
    # brief runs with --no-enrich so the recording never waits on the LLM.
    assert "brain brief --no-enrich" in text, "proactivity tape dropped brain brief"
    assert "brain resurface" in text, "proactivity tape dropped brain resurface"


# --------------------------------------------------------------------------- #
# cli-reference references the asset                                          #
# --------------------------------------------------------------------------- #
def test_cli_reference_embeds_the_gif() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    assert "](assets/proactivity.gif)" in text, (
        "cli-reference.md does not embed the proactivity GIF"
    )


def test_cli_reference_documents_the_tape_and_regenerator() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    # The embed sits in the proactivity section, next to the commands it shows.
    assert "## Proactivity and synthesis" in text, (
        "cli-reference.md lost its Proactivity section"
    )


# --------------------------------------------------------------------------- #
# Recorder safety invariants (it drives the REAL CLI)                         #
# --------------------------------------------------------------------------- #
def test_tape_targets_only_the_sandbox_port() -> None:
    text = PROACTIVITY_TAPE.read_text(encoding="utf-8")
    assert f":{PROACTIVITY_SANDBOX_PORT}/" in text, (
        "proactivity tape lost its :55445 sandbox guard"
    )
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"proactivity tape connects to a forbidden DB ({db_url}) — prod/demo/test"
        )


def test_tape_fails_closed_on_wrong_database() -> None:
    """The tape must hard-abort unless DATABASE_URL is the :55445 sandbox."""
    text = PROACTIVITY_TAPE.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text, "proactivity tape does not assert on DATABASE_URL"
    assert "exit 1" in text, "proactivity tape has no fail-closed abort"


def test_script_scopes_teardown_and_uses_named_volume() -> None:
    text = PROACTIVITY_SCRIPT.read_text(encoding="utf-8")
    # Isolated compose project + its own port, never prod/demo/test.
    assert "brain-proactivity-gif" in text, (
        "proactivity script lost its isolated project name"
    )
    assert PROACTIVITY_SANDBOX_PORT in text, (
        "proactivity script no longer pins port 55445"
    )
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"proactivity script connects to a forbidden DB ({db_url}) — prod/demo/test"
        )
    # Teardown scoped to our own project, with a named volume (never a bind mount).
    assert "down -v" in text, "proactivity script does not tear its sandbox volume down"
    assert "-p brain-proactivity-gif" in text or '-p "$PROJECT"' in text, (
        "proactivity script teardown is not scoped to the brain-proactivity-gif project"
    )
    assert "/var/lib/postgresql/data" in text, (
        "proactivity script lost its pg data volume"
    )
    assert "./data/postgres" not in text, (
        "proactivity script must not bind-mount the prod-style ./data/postgres path"
    )


def test_script_overrides_database_url_via_env() -> None:
    """The CLI must be pointed at the sandbox purely through the environment."""
    text = PROACTIVITY_SCRIPT.read_text(encoding="utf-8")
    assert "export DATABASE_URL=" in text, (
        "proactivity script must export DATABASE_URL so it wins over any repo-root .env"
    )
    assert "export BRAIN_VAULT_PATH=" in text, (
        "proactivity script must redirect BRAIN_VAULT_PATH away from the real vault"
    )
