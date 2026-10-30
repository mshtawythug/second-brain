"""`brain ingest-apple-notes` end to end (RM-36), through the CLI and the 5434 database.

The ``osascript`` runner is the synthetic ``FakeOsascript``
(``tests/apple_notes_fixtures.py``) and the host platform is pinned to
``darwin``, both swapped in with ``monkeypatch.setattr`` (standard test doubles,
rule 13), so the suite runs on Linux CI. The real ``osascript`` is never run: the
end-to-end test at the end hands the production runner a throwaway script
through its ``binary`` seam (the command never searches ``PATH``).
"""
from __future__ import annotations

import functools
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.ingest import IngestResult
from brain.ingest import apple_notes as an
from brain.ingest.apple_notes import JXA_SCRIPT, AppleNotesError
from tests import apple_notes_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

SCOPE_REFUSAL = 'ingest-apple-notes requires --folder NAME (repeatable); "all notes" is not offered'
FOLDER = ["--folder", fx.FOLDER]


@pytest.fixture(autouse=True)
def on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.cli_ingest_apple_notes.host_platform", "darwin")


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> fx.FakeOsascript:
    fake = fx.FakeOsascript(notes=[fx.note(2), fx.note(1)])
    monkeypatch.setattr("brain.cli_ingest_apple_notes.apple_notes_runner", fake)
    return fake


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-apple-notes", *args])


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.external_id, d.id::text, d.title, d.content, d.content_type,
               d.sensitivity, d.sent_at, d.metadata, d.tags
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'applenotes'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "sensitivity", "sent_at", "metadata",
            "tags")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_ids(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM chunks WHERE document_id = %s ORDER BY id", (document_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


# --- scope (AC1) and host (AC4) ---------------------------------------------------------------


@pytest.mark.parametrize("args", [[], ["--since", "2026-10-01"], ["--max", "5"]])
def test_without_a_folder_the_run_is_refused(args: list[str], notes: fx.FakeOsascript) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert SCOPE_REFUSAL in result.output
    assert notes.calls == []


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--folder", "  "], "--folder must not be blank"),
        ([*FOLDER, "--since", "2026/10/01"], "--since must be YYYY-MM-DD"),
    ],
)
def test_a_malformed_scope_is_refused(
    args: list[str], reason: str, notes: fx.FakeOsascript
) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert f"ingest-apple-notes: {reason}" in result.output
    assert SCOPE_REFUSAL not in result.output  # a folder was given: the precise reason only
    assert notes.calls == []


def test_a_future_since_is_refused(notes: fx.FakeOsascript) -> None:
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()

    result = _run(*FOLDER, "--since", tomorrow)

    assert result.exit_code == 2
    assert "ingest-apple-notes: --since is in the future" in result.output
    assert SCOPE_REFUSAL not in result.output
    assert notes.calls == []


def test_max_below_one_is_a_usage_error(notes: fx.FakeOsascript) -> None:
    assert _run(*FOLDER, "--max", "0").exit_code == 2
    assert notes.calls == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_host_that_is_not_macos_exits_1(
    platform: str, notes: fx.FakeOsascript, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain.cli_ingest_apple_notes.host_platform", platform)

    result = _run(*FOLDER)

    assert result.exit_code == 1
    assert "ingest-apple-notes needs macOS: Apple Notes is read through osascript" in (
        result.output
    )
    assert notes.calls == []


def test_the_scope_is_checked_before_the_host(
    notes: fx.FakeOsascript, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain.cli_ingest_apple_notes.host_platform", "linux")

    assert _run().exit_code == 2


def test_an_unknown_sensitivity_exits_1(notes: fx.FakeOsascript) -> None:
    result = _run(*FOLDER, "--sensitivity", "secret")

    assert result.exit_code == 1
    assert notes.calls == []


def test_the_request_carries_the_folders_since_and_max(notes: fx.FakeOsascript) -> None:
    result = _run(*FOLDER, "--folder", fx.OTHER_FOLDER, *FOLDER, "--since", "2026-10-01",
                  "--max", "7", "--dry-run")

    assert result.exit_code == 0, result.output
    assert notes.request() == {
        "folders": [fx.FOLDER, fx.OTHER_FOLDER],  # deduplicated, in order
        "since": "2026-10-01T00:00:00",
        "limit": 7,
    }


# --- the read ---------------------------------------------------------------------------------


def test_a_read_failure_exits_1_with_its_message(notes: fx.FakeOsascript) -> None:
    notes.fail = AppleNotesError("`osascript` failed (exit 1); allow it")

    result = _run(*FOLDER)

    assert result.exit_code == 1
    assert "`osascript` failed (exit 1); allow it" in result.output


def test_when_no_named_folder_exists_the_run_exits_1(notes: fx.FakeOsascript) -> None:
    result = _run("--folder", "Nowhere", "--folder", "Elsewhere")

    assert result.exit_code == 1
    assert "no Notes folder named 'Nowhere'" in result.output
    assert "no Notes folder named 'Elsewhere'" in result.output
    assert "none of the named folders exist in Notes" in result.output


def test_a_missing_folder_among_others_is_a_warning(notes: fx.FakeOsascript) -> None:
    result = _run(*FOLDER, "--folder", "Nowhere\x1b[2J", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "no Notes folder named 'Nowhere[2J'" in result.output
    assert "\x1b" not in result.output


def test_dry_run_lists_and_writes_nothing(
    notes: fx.FakeOsascript, test_db: psycopg.Connection
) -> None:
    notes.notes = [fx.note(1, name="Plan\x1b]0;x\x07 ahead"), fx.note(2, name="")]

    result = _run(*FOLDER, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "found 2 note(s)" in result.output
    assert f"  {fx.FOLDER} / Plan]0;x ahead" in result.output
    assert f"  {fx.FOLDER} / Untitled note" in result.output
    assert _docs(test_db) == {}


# --- ingest (AC2) -------------------------------------------------------------------------------


def test_notes_land_confidential_and_locked_notes_are_counted(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    notes.locked = 2
    notes.notes = [
        fx.note(1, body='<div><h1>Groceries</h1></div><ul class="checklist">'
                        '<li class="checked">Bread</li><li>Milk</li></ul>'),
        fx.note(2),
    ]

    result = _run(*FOLDER, "--tag", "Home Life")

    assert result.exit_code == 0, result.output
    assert "2 locked note(s) skipped (locked notes cannot be read)" in result.output
    assert (
        "2 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 empty, 0 skipped, "
        "0 same body as another note, 0 failed"
    ) in result.output
    docs = _docs(test_db)
    assert set(docs) == {fx.note_id(1), fx.note_id(2)}
    first = docs[fx.note_id(1)]
    assert first["title"] == "Synthetic note 1"
    assert first["content"] == "# Groceries\n- [x] Bread\n- [ ] Milk"
    assert first["content_type"] == "apple_note"
    assert first["sensitivity"] == "confidential"
    assert first["sent_at"] == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert first["metadata"]["folder"] == fx.FOLDER
    assert first["metadata"]["modified"] == fx.MODIFIED
    assert first["tags"] == ["home-life"]
    assert all(d["sensitivity"] == "confidential" for d in docs.values())


def test_sensitivity_normal_overrides_the_default(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run(*FOLDER, "--sensitivity", "normal")

    assert result.exit_code == 0, result.output
    assert {d["sensitivity"] for d in _docs(test_db).values()} == {"normal"}


def test_an_unchanged_rerun_writes_nothing_and_an_edit_updates_in_place(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    _run(*FOLDER)
    before = _docs(test_db)

    again = _run(*FOLDER)
    notes.notes = [fx.note(2, body="<div>Rewritten.</div>", modified="2026-10-21T10:00:00Z"),
                   fx.note(1)]
    edited = _run(*FOLDER)

    assert "0 ingested, 0 updated, 0 refreshed, 2 unchanged" in again.output
    assert "0 ingested, 1 updated, 0 refreshed, 1 unchanged" in edited.output
    after = _docs(test_db)
    assert after[fx.note_id(2)]["id"] == before[fx.note_id(2)]["id"]  # dedup by note id
    assert after[fx.note_id(2)]["content"] == "Rewritten."
    assert after[fx.note_id(2)]["metadata"]["modified"] == "2026-10-21T10:00:00Z"


def test_a_moved_note_refreshes_metadata_without_rechunking(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    notes.notes = [fx.note(1)]
    _run(*FOLDER)
    doc_id = _docs(test_db)[fx.note_id(1)]["id"]
    chunks = _chunk_ids(test_db, doc_id)
    embeds = embedder.embed_calls

    notes.notes = [fx.note(1, folder=fx.OTHER_FOLDER, modified="2026-10-22T00:00:00Z")]
    result = _run("--folder", fx.OTHER_FOLDER)

    assert "0 ingested, 0 updated, 1 refreshed, 0 unchanged" in result.output
    assert f"  refreshed {fx.note_id(1)}" in result.output
    meta = _docs(test_db)[fx.note_id(1)]["metadata"]
    assert (meta["folder"], meta["modified"]) == (fx.OTHER_FOLDER, "2026-10-22T00:00:00Z")
    assert _chunk_ids(test_db, doc_id) == chunks
    assert embedder.embed_calls == embeds


def test_an_empty_note_is_counted_and_not_stored(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    notes.notes = [fx.note(1, body="<div><br></div><img src='x'>"), fx.note(2)]

    result = _run(*FOLDER)

    assert (
        "1 ingested, 0 updated, 0 refreshed, 0 unchanged, 1 empty, 0 skipped, "
        "0 same body as another note, 0 failed"
    ) in result.output
    assert f"  empty {fx.note_id(1)}" in result.output  # reported by id
    assert set(_docs(test_db)) == {fx.note_id(2)}


@pytest.mark.parametrize(
    "body", ["<div>   </div>", "<div>&nbsp;\t</div>", '<p><a href="javascript:x"></a></p>']
)
def test_a_whitespace_only_note_is_empty_and_ingest_is_never_called(
    body: str, notes: fx.FakeOsascript, embedder: CountingEmbedder,
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aud-104's second Notion case: an empty body makes no chunks and no document."""
    calls: list[str] = []
    monkeypatch.setattr(
        "brain.cli_ingest_apple_notes.ingest_document",
        lambda *_a, **k: calls.append(k["source_external_id"]),
    )
    notes.notes = [fx.note(1, body=body)]

    result = _run(*FOLDER)

    assert f"  empty {fx.note_id(1)}" in result.output
    assert "0 ingested, 0 updated, 0 refreshed, 0 unchanged, 1 empty" in result.output
    assert calls == []


def test_no_document_from_ingest_is_empty_never_unchanged(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop: ``ingest_document`` returning no row (nothing to chunk) is ``empty``."""
    monkeypatch.setattr(
        "brain.cli_ingest_apple_notes.ingest_document",
        lambda *_a, **_k: IngestResult(document_id=None, created=False),
    )
    notes.notes = [fx.note(1)]

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert f"  empty {fx.note_id(1)}" in result.output
    assert "0 refreshed, 0 unchanged, 1 empty" in result.output


def test_a_row_that_is_not_the_notes_own_is_never_refreshed(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged/refreshed only describe the note's own (applenotes, id) row: a stored note
    for which ingest returns some other document id is reported, and nothing is patched.
    """
    notes.notes = [fx.note(1)]
    _run(*FOLDER)
    own = _docs(test_db)[fx.note_id(1)]
    monkeypatch.setattr(
        "brain.cli_ingest_apple_notes.ingest_document",
        lambda *_a, **_k: IngestResult(
            document_id="00000000-0000-4000-8000-000000000099", created=False
        ),
    )
    notes.notes = [fx.note(1, folder=fx.OTHER_FOLDER, modified="2026-10-26T00:00:00Z")]

    result = _run("--folder", fx.OTHER_FOLDER)

    assert f"same body as another note: {fx.note_id(1)}" in result.output
    assert "0 refreshed, 0 unchanged" in result.output
    assert _docs(test_db)[fx.note_id(1)]["metadata"] == own["metadata"]


def test_an_unreadable_entry_is_reported(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    notes.raw = json.dumps({"notes": [fx.note(1), {"id": 3}], "eligible": 2, "locked": 0,
                            "missing": []})

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert "1 note(s) could not be read" in result.output
    assert set(_docs(test_db)) == {fx.note_id(1)}


def test_a_database_error_is_reported_by_class(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise psycopg.errors.CheckViolation("row value SECRETROW")

    monkeypatch.setattr("brain.cli_ingest_apple_notes.ingest_document", _boom)

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert f"failed {fx.note_id(1)}: CheckViolation" in result.output
    assert "SECRETROW" not in result.output
    assert (
        "0 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 empty, 0 skipped, "
        "0 same body as another note, 2 failed"
    ) in result.output


def test_the_secret_guard_runs_on_every_note(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    notes.notes = [fx.note(1, body=f"<div>The key is {SYNTHETIC_AWS_KEY}.</div>"), fx.note(2)]

    result = _run(*FOLDER)

    assert f"refused {fx.note_id(1)}" in result.output
    assert set(_docs(test_db)) == {fx.note_id(2)}
    assert SYNTHETIC_AWS_KEY not in result.output


def test_a_conversion_failure_costs_one_note_not_the_run(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3 (#103): the conversion runs inside the per-note guard."""
    real = an.build_document

    def build(note: an.Note) -> Any:
        if note.id == fx.note_id(2):
            raise ValueError("SECRETBODY text from the note")
        return real(note)

    monkeypatch.setattr("brain.cli_ingest_apple_notes.an.build_document", build)

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert f"failed {fx.note_id(2)}: ValueError" in result.output
    assert "SECRETBODY" not in result.output
    assert (
        "1 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 empty, 0 skipped, "
        "0 same body as another note, 1 failed"
    ) in result.output
    assert set(_docs(test_db)) == {fx.note_id(1)}


def test_a_huge_character_reference_is_replaced_not_fatal(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    """M3 (#103): a 5,000-digit decimal reference once raised ValueError and ended the run."""
    notes.notes = [fx.note(1, body=f"<div>before &#{'9' * 5000}; after</div>"), fx.note(2)]

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert "2 ingested" in result.output
    assert _docs(test_db)[fx.note_id(1)]["content"] == "before \ufffd after"


def test_an_oversized_body_is_skipped_and_counted(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brain.cli_ingest_apple_notes.an.MAX_BODY_CHARS", 100)
    notes.notes = [fx.note(1, body="<div>" + "x" * 101 + "</div>"), fx.note(2)]

    result = _run(*FOLDER)

    assert result.exit_code == 0, result.output
    assert f"  skipped {fx.note_id(1)}: body over 100 characters" in result.output
    assert (
        "1 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 empty, 1 skipped, "
        "0 same body as another note, 0 failed"
    ) in result.output
    assert set(_docs(test_db)) == {fx.note_id(2)}


def test_a_body_at_the_cap_is_converted(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "<div>" + "x" * 89 + "</div>"
    monkeypatch.setattr("brain.cli_ingest_apple_notes.an.MAX_BODY_CHARS", len(body))
    notes.notes = [fx.note(1, body=body)]

    assert "1 ingested" in _run(*FOLDER).output


def test_the_body_cap_is_two_million_characters() -> None:
    assert an.MAX_BODY_CHARS == 2_000_000


def test_two_notes_with_one_body_never_share_a_row(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    """Regression (#103, rev-104's Notion finding): a new note whose body equals a stored
    note's matched that document through ingest_document's content-hash fallback. It was
    reported ``unchanged`` and never stored, and its folder was patched onto the OTHER
    note's row. Now it is reported by id, and the other row is left alone.
    """
    body = "<div><h1>Weekly template</h1></div><div>Goals:</div>"
    notes.notes = [
        fx.note(1, body=body, folder=fx.FOLDER),
        fx.note(2, body=body, folder=fx.OTHER_FOLDER, modified="2026-10-25T00:00:00Z"),
    ]
    args = ("--folder", fx.FOLDER, "--folder", fx.OTHER_FOLDER)

    first = _run(*args)
    second = _run(*args)

    for result, kept in ((first, "1 ingested"), (second, "1 unchanged")):
        assert result.exit_code == 0, result.output
        assert kept in result.output
        assert f"same body as another note: {fx.note_id(2)}" in result.output
        assert "0 refreshed" in result.output
        assert "1 same body as another note, 0 failed" in result.output
    docs = _docs(test_db)
    assert set(docs) == {fx.note_id(1)}  # never stored under note 2's id, never replaced
    meta = docs[fx.note_id(1)]["metadata"]
    assert (meta["folder"], meta["modified"]) == (fx.FOLDER, fx.MODIFIED)  # not note 2's


# --- the cap (AC5) ------------------------------------------------------------------------------


def test_max_stops_the_run_and_reports_the_remainder(
    notes: fx.FakeOsascript, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    notes.notes = [fx.note(n) for n in range(5)]

    result = _run(*FOLDER, "--max", "2")

    assert result.exit_code == 0, result.output
    assert "found 2 note(s)" in result.output
    assert "stopped at the --max cap of 2: 3 more note(s) match; raise --max or narrow --since" in (
        result.output
    )
    assert set(_docs(test_db)) == {fx.note_id(0), fx.note_id(1)}


def test_the_default_cap_is_200(notes: fx.FakeOsascript) -> None:
    _run(*FOLDER, "--dry-run")

    assert notes.request()["limit"] == 200


# --- end to end: a fake osascript through the binary seam, a hostile folder name ---------------

_FAKE_OSASCRIPT = """#!{python}
import json, os, sys
log = os.environ["RM36_FAKE_LOG"]
with open(log, "w") as fh:
    json.dump(sys.argv[1:], fh)
sys.stderr.write("osascript: private stderr detail\\n")
request = json.loads(sys.argv[5])
answer = json.load(open(os.environ["RM36_FAKE_ANSWER"]))
answer["notes"] = [n for n in answer["notes"] if n["folder"] in request["folders"]]
sys.stdout.write(json.dumps(answer))
"""


def test_end_to_end_through_a_fake_osascript_via_the_binary_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: CountingEmbedder,
    test_db: psycopg.Connection,
) -> None:
    hostile = '"); Application("Finder").delete(Path("/")); ("'
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "osascript"
    script.write_text(_FAKE_OSASCRIPT.format(python=sys.executable))
    script.chmod(0o755)
    answer = {"notes": [fx.note(1, folder=hostile)], "eligible": 1, "locked": 1, "missing": []}
    (tmp_path / "answer.json").write_text(json.dumps(answer))
    monkeypatch.setenv("RM36_FAKE_ANSWER", str(tmp_path / "answer.json"))
    monkeypatch.setenv("RM36_FAKE_LOG", str(tmp_path / "argv.json"))
    # PATH holds nothing runnable: the command must not need it (L1, #103).
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(
        "brain.cli_ingest_apple_notes.apple_notes_runner",
        functools.partial(an.osascript_runner, binary=str(script)),
    )

    result = _run("--folder", hostile)

    assert result.exit_code == 0, result.output
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[:4] == ["-l", "JavaScript", "-e", JXA_SCRIPT]
    assert json.loads(argv[4]) == {"folders": [hostile], "since": None, "limit": 200}
    assert "1 locked note(s) skipped" in result.output
    assert "private stderr detail" not in result.output
    [doc] = _docs(test_db).values()
    assert doc["sensitivity"] == "confidential"
    assert doc["metadata"]["folder"] == hostile
