"""Drift guards for the README demo/usage GIFs, their VHS tapes, and regenerators.

Two GIFs ship in the README:

* ``docs/assets/demo.gif`` — the hero, recorded from ``docs/assets/demo.tape``
  via ``bin/brain-demo-gif`` (the ``brain demo`` sandbox flow).
* ``docs/assets/usage.gif`` — the daily-workflow clip, recorded from
  ``docs/assets/usage.tape`` via ``bin/brain-usage-gif`` (the regular CLI
  against a throwaway :55440 Postgres sandbox).

These tests keep the assets, their tapes, their regenerators, and the docs that
reference them from silently drifting apart, and — for the usage recorder, which
drives the *real* CLI rather than a self-managed sandbox — assert the safety
invariants that keep it off the prod / demo / test databases.
"""
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "assets"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

DEMO_GIF = ASSETS / "demo.gif"
DEMO_TAPE = ASSETS / "demo.tape"
DEMO_SCRIPT = REPO_ROOT / "bin" / "brain-demo-gif"

USAGE_GIF = ASSETS / "usage.gif"
USAGE_TAPE = ASSETS / "usage.tape"
USAGE_SCRIPT = REPO_ROOT / "bin" / "brain-usage-gif"

# README size budgets (bytes). Mirror the ceilings each regenerator enforces:
# demo hero 3 MB, daily-workflow clip 2.5 MB.
DEMO_MAX_BYTES = 3 * 1024 * 1024
USAGE_MAX_BYTES = 2560 * 1024

# The isolated sandbox the usage recorder is allowed to touch, and the databases
# it must NEVER connect to (prod / demo / test). The forbidden checks look for a
# connection form (``:PORT/`` in a DB URL) rather than the bare number, so the
# tape/script may still name those ports in a comment explaining what they avoid.
USAGE_SANDBOX_PORT = "55440"
FORBIDDEN_DB_URLS = (":55432/", ":55433/", ":5434/")


# --------------------------------------------------------------------------- #
# Existence + budget                                                          #
# --------------------------------------------------------------------------- #
def test_both_tapes_exist() -> None:
    assert DEMO_TAPE.is_file(), f"missing hero tape: {DEMO_TAPE}"
    assert USAGE_TAPE.is_file(), f"missing usage tape: {USAGE_TAPE}"


def test_both_gifs_exist_and_are_nonempty() -> None:
    for gif in (DEMO_GIF, USAGE_GIF):
        assert gif.is_file(), f"missing GIF: {gif}"
        assert gif.stat().st_size > 0, f"empty GIF: {gif}"


def test_gifs_within_readme_budget() -> None:
    assert DEMO_GIF.stat().st_size <= DEMO_MAX_BYTES, (
        f"{DEMO_GIF.name} exceeds the {DEMO_MAX_BYTES} byte README budget"
    )
    assert USAGE_GIF.stat().st_size <= USAGE_MAX_BYTES, (
        f"{USAGE_GIF.name} exceeds the {USAGE_MAX_BYTES} byte README budget"
    )


def test_both_regenerators_are_executable() -> None:
    for script in (DEMO_SCRIPT, USAGE_SCRIPT):
        assert script.is_file(), f"missing regenerator: {script}"
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"regenerator not executable: {script}"
        assert os.access(script, os.X_OK), f"regenerator not executable: {script}"


# --------------------------------------------------------------------------- #
# Tape <-> GIF self-consistency                                               #
# --------------------------------------------------------------------------- #
def test_tapes_output_their_own_gifs() -> None:
    assert "Output docs/assets/demo.gif" in DEMO_TAPE.read_text(encoding="utf-8")
    assert "Output docs/assets/usage.gif" in USAGE_TAPE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# README + CONTRIBUTING reference both assets                                 #
# --------------------------------------------------------------------------- #
def test_readme_embeds_both_gifs() -> None:
    text = README.read_text(encoding="utf-8")
    assert "](docs/assets/demo.gif)" in text, "README no longer embeds the demo GIF"
    assert "](docs/assets/usage.gif)" in text, "README does not embed the usage GIF"


def test_contributing_documents_all_regenerators() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    # Every GIF regenerator under bin/ must be named in the "Docs assets" section
    # so contributors know which script rebuilds which asset. Substring checks on
    # the seven script names stay robust to prose/formatting changes.
    for script in (
        "bin/brain-demo-gif",
        "bin/brain-usage-gif",
        "bin/brain-mcp-gif",
        "bin/brain-graphrag-gif",
        "bin/brain-ask-gif",
        "bin/brain-wiki-gif",
        "bin/brain-proactivity-gif",
    ):
        assert script in text, (
            f"CONTRIBUTING 'Docs assets' does not mention regenerator {script}"
        )


# --------------------------------------------------------------------------- #
# Usage recorder safety invariants (it drives the REAL CLI)                   #
# --------------------------------------------------------------------------- #
def test_usage_tape_targets_only_the_sandbox_port() -> None:
    text = USAGE_TAPE.read_text(encoding="utf-8")
    assert f":{USAGE_SANDBOX_PORT}/" in text, "usage tape lost its :55440 sandbox guard"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"usage tape connects to a forbidden DB ({db_url}) — prod/demo/test"
        )


def test_usage_tape_fails_closed_on_wrong_database() -> None:
    """The tape must hard-abort unless DATABASE_URL is the :55440 sandbox."""
    text = USAGE_TAPE.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text, "usage tape does not assert on DATABASE_URL"
    assert "exit 1" in text, "usage tape has no fail-closed abort"


def test_usage_script_scopes_teardown_and_uses_named_volume() -> None:
    text = USAGE_SCRIPT.read_text(encoding="utf-8")
    # Isolated compose project + its own port, never prod/demo/test.
    assert "brain-usage-gif" in text, "usage script lost its isolated project name"
    assert USAGE_SANDBOX_PORT in text, "usage script no longer pins port 55440"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"usage script connects to a forbidden DB ({db_url}) — prod/demo/test"
        )
    # Teardown scoped to our own project, with a named volume (never a bind mount).
    assert "down -v" in text, "usage script does not tear its sandbox volume down"
    assert "-p brain-usage-gif" in text or '-p "$PROJECT"' in text, (
        "usage script teardown is not scoped to the brain-usage-gif project"
    )
    assert "/var/lib/postgresql/data" in text, "usage script lost its pg data volume"
    assert "./data/postgres" not in text, (
        "usage script must not bind-mount the prod-style ./data/postgres path"
    )


def test_usage_script_overrides_database_url_via_env() -> None:
    """The CLI must be pointed at the sandbox purely through the environment."""
    text = USAGE_SCRIPT.read_text(encoding="utf-8")
    assert "export DATABASE_URL=" in text, (
        "usage script must export DATABASE_URL so it wins over any repo-root .env"
    )
    assert "export BRAIN_VAULT_PATH=" in text, (
        "usage script must redirect BRAIN_VAULT_PATH away from the real vault"
    )
