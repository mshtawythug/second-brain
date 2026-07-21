"""Drift guards for the docs/graphrag.md GraphRAG GIF, its VHS tape, and regenerator.

``docs/assets/graphrag.gif`` is the entity-graph-retrieval clip embedded at the
top of ``docs/graphrag.md`` — recorded from ``docs/assets/graphrag.tape`` via
``bin/brain-graphrag-gif`` (the ``brain graphrag …`` CLI — "themes with a
person" then a fused graph+vector search — against a throwaway :55442
Apache-AGE Postgres sandbox).

These tests keep the asset, its tape, its regenerator, and the doc that embeds
it from silently drifting apart, and — because the recorder drives the *real*
``brain`` CLI against a live database — assert the safety invariants that keep
it off the prod / demo / test / usage-gif databases (it must only ever touch its
own isolated :55442 sandbox on the custom Apache-AGE image).

Mirrors ``tests/test_docs_assets.py`` (the demo/usage GIFs).
"""
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "assets"
GRAPHRAG_DOC = REPO_ROOT / "docs" / "graphrag.md"

GRAPHRAG_GIF = ASSETS / "graphrag.gif"
GRAPHRAG_TAPE = ASSETS / "graphrag.tape"
GRAPHRAG_SCRIPT = REPO_ROOT / "bin" / "brain-graphrag-gif"

# docs budget (bytes). Mirrors the ceiling the regenerator enforces: 2.5 MB.
GRAPHRAG_MAX_BYTES = 2560 * 1024

# The isolated sandbox the recorder is allowed to touch, and the databases it
# must NEVER connect to (prod / demo / test / usage-gif). The forbidden checks
# look for a connection form (``:PORT/`` in a DB URL) rather than the bare
# number, so the tape/script may still name those ports in a comment explaining
# what they avoid.
GRAPHRAG_SANDBOX_PORT = "55442"
FORBIDDEN_DB_URLS = (":55432/", ":55433/", ":5434/", ":55440/")

# GraphRAG needs the custom Apache-AGE image, never the stock pgvector prod one.
AGE_IMAGE_MARKER = "second-brain-age:pg16"


# --------------------------------------------------------------------------- #
# Existence + budget                                                          #
# --------------------------------------------------------------------------- #
def test_graphrag_tape_exists() -> None:
    assert GRAPHRAG_TAPE.is_file(), f"missing graphrag tape: {GRAPHRAG_TAPE}"


def test_graphrag_gif_exists_and_is_nonempty() -> None:
    assert GRAPHRAG_GIF.is_file(), f"missing GIF: {GRAPHRAG_GIF}"
    assert GRAPHRAG_GIF.stat().st_size > 0, f"empty GIF: {GRAPHRAG_GIF}"


def test_graphrag_gif_within_docs_budget() -> None:
    assert GRAPHRAG_GIF.stat().st_size <= GRAPHRAG_MAX_BYTES, (
        f"{GRAPHRAG_GIF.name} exceeds the {GRAPHRAG_MAX_BYTES} byte docs budget"
    )


def test_graphrag_regenerator_is_executable() -> None:
    assert GRAPHRAG_SCRIPT.is_file(), f"missing regenerator: {GRAPHRAG_SCRIPT}"
    mode = GRAPHRAG_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"regenerator not executable: {GRAPHRAG_SCRIPT}"
    assert os.access(GRAPHRAG_SCRIPT, os.X_OK), (
        f"regenerator not executable: {GRAPHRAG_SCRIPT}"
    )


# --------------------------------------------------------------------------- #
# Tape <-> GIF self-consistency + doc embed                                    #
# --------------------------------------------------------------------------- #
def test_tape_outputs_its_own_gif() -> None:
    assert "Output docs/assets/graphrag.gif" in GRAPHRAG_TAPE.read_text(
        encoding="utf-8"
    )


def test_graphrag_doc_embeds_the_gif() -> None:
    text = GRAPHRAG_DOC.read_text(encoding="utf-8")
    # graphrag.md lives in docs/, so the embed path is relative to that dir.
    assert "](assets/graphrag.gif)" in text, (
        "docs/graphrag.md no longer embeds the GraphRAG GIF"
    )


# --------------------------------------------------------------------------- #
# Recorder safety invariants (it drives the REAL CLI against a live DB)        #
# --------------------------------------------------------------------------- #
def test_graphrag_tape_targets_only_the_sandbox_port() -> None:
    text = GRAPHRAG_TAPE.read_text(encoding="utf-8")
    assert f":{GRAPHRAG_SANDBOX_PORT}/" in text, (
        "graphrag tape lost its :55442 sandbox guard"
    )
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"graphrag tape connects to a forbidden DB ({db_url}) — "
            "prod/demo/test/usage-gif"
        )


def test_graphrag_tape_fails_closed_on_wrong_database() -> None:
    """The tape must hard-abort unless DATABASE_URL is the :55442 sandbox."""
    text = GRAPHRAG_TAPE.read_text(encoding="utf-8")
    assert "DATABASE_URL" in text, "graphrag tape does not assert on DATABASE_URL"
    assert "exit 1" in text, "graphrag tape has no fail-closed abort"


def test_graphrag_script_scopes_teardown_and_uses_named_volume() -> None:
    text = GRAPHRAG_SCRIPT.read_text(encoding="utf-8")
    # Isolated compose project + its own port, never prod/demo/test/usage-gif.
    assert "brain-graphrag-gif" in text, (
        "graphrag script lost its isolated project name"
    )
    assert GRAPHRAG_SANDBOX_PORT in text, "graphrag script no longer pins port 55442"
    for db_url in FORBIDDEN_DB_URLS:
        assert db_url not in text, (
            f"graphrag script connects to a forbidden DB ({db_url}) — "
            "prod/demo/test/usage-gif"
        )
    # Teardown scoped to our own project, with a named volume (never a bind mount).
    assert "down -v" in text, "graphrag script does not tear its sandbox volume down"
    assert "-p brain-graphrag-gif" in text or '-p "$PROJECT"' in text, (
        "graphrag script teardown is not scoped to the brain-graphrag-gif project"
    )
    assert "/var/lib/postgresql/data" in text, "graphrag script lost its pg data volume"
    assert "./data/postgres" not in text, (
        "graphrag script must not bind-mount the prod-style ./data/postgres path"
    )


def test_graphrag_script_uses_the_age_image() -> None:
    """GraphRAG needs Apache AGE — never the stock pgvector prod image."""
    text = GRAPHRAG_SCRIPT.read_text(encoding="utf-8")
    assert AGE_IMAGE_MARKER in text, (
        "graphrag script must provision the custom Apache-AGE image (GraphRAG "
        "needs the graph extension)"
    )


def test_graphrag_script_overrides_database_url_via_env() -> None:
    """The CLI must be pointed at the sandbox purely through the environment."""
    text = GRAPHRAG_SCRIPT.read_text(encoding="utf-8")
    assert "export DATABASE_URL=" in text, (
        "graphrag script must export DATABASE_URL so it wins over any repo-root .env"
    )
    assert "export BRAIN_VAULT_PATH=" in text, (
        "graphrag script must redirect BRAIN_VAULT_PATH away from the real vault"
    )
    assert "export BRAIN_GRAPH_ENABLED=" in text, (
        "graphrag script must enable the graph layer for the recording"
    )
