"""``--agent`` on the CLI surfaces, and ``BRAIN_AGENT_ID`` (F10).

Attribution answers a question ``source`` cannot: two agents both driving the
CLI share ``source='cli'``, so without ``agent_id`` "is the research agent's
hit rate worse than the capture agent's" is unanswerable.

Precedence is flag > ``BRAIN_AGENT_ID`` > unattributed, and it is tested at
every surface rather than once at the helper, because the failure mode is a
surface that forgets to call ``resolve_agent_id`` at all — which no amount of
helper-level testing would catch.

The ``ingest-stdin`` case has one extra rule worth stating: the flag ASSIGNS
over a same-named ``--metadata`` key rather than deferring to it. An explicit
``--agent`` is the more specific and more recent statement of intent, so a
stale ``agent_id`` inside a hand-built metadata blob must not outrank it.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app


@pytest.fixture
def wired(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the CLI at the test DB with a working fake embedder."""
    patch_embedder(fake_embedder)
    monkeypatch.delenv("BRAIN_AGENT_ID", raising=False)


def _agent_of(conn: psycopg.Connection[Any], query: str) -> str | None:
    row = conn.execute(
        "SELECT agent_id FROM search_queries WHERE query = %s", (query,)
    ).fetchone()
    assert row is not None, f"no search_queries row logged for {query!r}"
    return row[0]


# ---------------------------------------------------------------------------
# brain search --agent
# ---------------------------------------------------------------------------


def test_search_flag_attributes_the_query(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    result = CliRunner().invoke(
        app, ["search", "flag attributed", "--agent", "research-agent"]
    )

    assert result.exit_code == 0, result.output
    assert _agent_of(test_db, "flag attributed") == "research-agent"


def test_search_without_a_flag_is_unattributed(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    """NULL, not a placeholder — ``brain usage`` renders it as (unattributed)."""
    result = CliRunner().invoke(app, ["search", "no attribution"])

    assert result.exit_code == 0, result.output
    assert _agent_of(test_db, "no attribution") is None


def test_search_uses_brain_agent_id_when_no_flag(
    test_db: psycopg.Connection[Any],
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env path — this is what makes attribution work with zero flags."""
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")

    result = CliRunner().invoke(app, ["search", "env attributed"])

    assert result.exit_code == 0, result.output
    assert _agent_of(test_db, "env attributed") == "env-agent"


def test_search_flag_wins_over_the_environment(
    test_db: psycopg.Connection[Any],
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")

    result = CliRunner().invoke(
        app, ["search", "flag wins", "--agent", "flag-agent"]
    )

    assert result.exit_code == 0, result.output
    assert _agent_of(test_db, "flag wins") == "flag-agent"


def test_search_rejects_a_malformed_agent_id(wired: None) -> None:
    """A typo must fail loudly, not attribute the row to nobody.

    Silently dropping it would leave the user with a permanently empty
    ``brain usage`` bucket and no clue why.
    """
    result = CliRunner().invoke(
        app, ["search", "bad agent", "--agent", "-leading-hyphen"]
    )

    assert result.exit_code != 0


def test_search_output_is_unchanged_by_attribution(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    """``--agent`` is telemetry only — it must not alter results or shape."""
    plain = CliRunner().invoke(app, ["search", "shape check", "--json"])
    attributed = CliRunner().invoke(
        app, ["search", "shape check", "--json", "--agent", "research-agent"]
    )

    assert plain.exit_code == 0 and attributed.exit_code == 0
    assert json.loads(plain.stdout) == json.loads(attributed.stdout)


# ---------------------------------------------------------------------------
# brain ingest-stdin --agent
# ---------------------------------------------------------------------------


def _ingest_stdin(args: list[str], stdin: str) -> Any:
    return CliRunner().invoke(app, ["ingest-stdin", *args], input=stdin)


def test_ingest_stdin_flag_lands_on_the_document(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    result = _ingest_stdin(
        [
            "--source", "krisp",
            "--external-id", "w4-agent-1",
            "--title", "Synthetic Standup",
            "--agent", "capture-bot",
        ],
        "Standup notes about the platform migration runway.\n",
    )

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Synthetic Standup'"
    ).fetchone()
    assert row is not None
    assert row[0] == "capture-bot"


def test_ingest_stdin_without_a_flag_is_unattributed(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    result = _ingest_stdin(
        [
            "--source", "krisp",
            "--external-id", "w4-agent-2",
            "--title", "Unattributed Standup",
        ],
        "Standup notes without attribution.\n",
    )

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Unattributed Standup'"
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_ingest_stdin_flag_overrides_a_metadata_agent_id(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    """The flag ASSIGNS over the metadata key — the documented precedence.

    A stale ``agent_id`` inside a hand-built metadata blob must not outrank an
    explicit ``--agent``, or the document is misattributed.
    """
    result = _ingest_stdin(
        [
            "--source", "slack",
            "--external-id", "w4-agent-3",
            "--title", "Override Standup",
            "--metadata", json.dumps({"agent_id": "stale-agent"}),
            "--agent", "capture-bot",
        ],
        "Thread about the migration runway.\n",
    )

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Override Standup'"
    ).fetchone()
    assert row is not None
    assert row[0] == "capture-bot"


def test_ingest_stdin_metadata_agent_id_alone_still_promotes(
    test_db: psycopg.Connection[Any], wired: None
) -> None:
    """Without the flag, a metadata ``agent_id`` still reaches the column.

    This is the MCP ``brain_ingest_stdin`` path's shape, so it must work.
    """
    result = _ingest_stdin(
        [
            "--source", "slack",
            "--external-id", "w4-agent-4",
            "--title", "Metadata Only Standup",
            "--metadata", json.dumps({"agent_id": "metadata-agent"}),
        ],
        "Another thread about the runway.\n",
    )

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Metadata Only Standup'"
    ).fetchone()
    assert row is not None
    assert row[0] == "metadata-agent"


def test_ingest_stdin_uses_brain_agent_id_when_no_flag(
    test_db: psycopg.Connection[Any],
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")

    result = _ingest_stdin(
        [
            "--source", "krisp",
            "--external-id", "w4-agent-5",
            "--title", "Env Standup",
        ],
        "Standup notes attributed via the environment.\n",
    )

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Env Standup'"
    ).fetchone()
    assert row is not None
    assert row[0] == "env-agent"


def test_ingest_stdin_rejects_a_malformed_agent_id(wired: None) -> None:
    result = _ingest_stdin(
        [
            "--source", "krisp",
            "--external-id", "w4-agent-6",
            "--title", "Bad Agent",
            "--agent", "has space",
        ],
        "Body.\n",
    )

    assert result.exit_code != 0


def test_human_ingest_paths_have_no_agent_flag(
    test_db: psycopg.Connection[Any], wired: None, tmp_path: Any
) -> None:
    """``brain ingest`` is a human path and grows no ``--agent`` FLAG.

    Deliberate, and it survives the #25 ruling: attaching an explicit agent to
    a hand-run file ingest would be a fabricated fact. Asserted so a later
    contributor adds the flag knowingly rather than by reflex.

    The ambient env var is a different claim — see the test below.
    """
    path = tmp_path / "note.md"
    path.write_text("# Synthetic Note\n\nA hand-ingested file.\n")

    result = CliRunner().invoke(app, ["ingest", str(path), "--agent", "x"])

    assert result.exit_code == 2, "--agent must not exist on `brain ingest`"


def test_brain_agent_id_attributes_a_file_ingest(
    test_db: psycopg.Connection[Any],
    wired: None,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BRAIN_AGENT_ID`` attributes file ingests too (task #25 ruling).

    **This overrides F2-F7-F10 §5.2**, which wired attribution to
    ``ingest-stdin`` only. Setting the env var is an affirmative statement —
    "everything from this process is me" — and nobody sets it by accident, so
    recording the document as unattributed is its own fabricated fact.

    The concrete inconsistency it removes: before this, the same variable in
    the same process attributed ``brain search`` and not ``brain ingest``.
    """
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")
    path = tmp_path / "attributed.md"
    path.write_text("# Attributed Note\n\nIngested under an agent id.\n")

    result = CliRunner().invoke(app, ["ingest", str(path)])

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Attributed Note'"
    ).fetchone()
    assert row is not None
    assert row[0] == "env-agent"


def test_a_file_ingest_without_the_env_var_stays_unattributed(
    test_db: psycopg.Connection[Any], wired: None, tmp_path: Any
) -> None:
    """The default is still NULL — attribution is opt-in, never inferred."""
    path = tmp_path / "plain.md"
    path.write_text("# Plain Note\n\nNo agent configured.\n")

    result = CliRunner().invoke(app, ["ingest", str(path)])

    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Plain Note'"
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_ingest_dir_attributes_every_file(
    test_db: psycopg.Connection[Any],
    wired: None,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must not leak one file's attribution into another's metadata.

    ``_attribute_to_agent`` returns a NEW doc rather than mutating in place
    precisely so a shared metadata dict cannot carry across iterations.
    """
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")
    folder = tmp_path / "corpus"
    folder.mkdir()
    for i in range(3):
        (folder / f"note-{i}.md").write_text(f"# Corpus Note {i}\n\nBody {i}.\n")

    result = CliRunner().invoke(app, ["ingest-dir", str(folder)])

    assert result.exit_code == 0, result.output
    rows = test_db.execute(
        "SELECT agent_id FROM documents WHERE title LIKE 'Corpus Note %'"
    ).fetchall()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {"env-agent"}


def test_search_and_ingest_agree_on_the_env_var(
    test_db: psycopg.Connection[Any],
    wired: None,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual defect #25 reported: one variable, one process, one answer.

    Before the ruling, ``search_queries.agent_id`` was set and
    ``documents.agent_id`` was NULL for the same ``BRAIN_AGENT_ID``.
    """
    monkeypatch.setenv("BRAIN_AGENT_ID", "env-agent")
    path = tmp_path / "consistent.md"
    path.write_text("# Consistent Note\n\nplatform migration runway.\n")

    assert CliRunner().invoke(app, ["ingest", str(path)]).exit_code == 0
    assert CliRunner().invoke(app, ["search", "consistency probe"]).exit_code == 0

    doc = test_db.execute(
        "SELECT agent_id FROM documents WHERE title = 'Consistent Note'"
    ).fetchone()
    search = test_db.execute(
        "SELECT agent_id FROM search_queries WHERE query = 'consistency probe'"
    ).fetchone()

    assert doc is not None and search is not None
    assert doc[0] == search[0] == "env-agent"
