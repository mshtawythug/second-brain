"""Voice memos through `brain ingest-folder` end to end (RM-38), on the 5434 database.

Every folder, ``$BRAIN_HOME`` and vault is in ``tmp_path``; every memo is a few synthetic
bytes. Whisper is the fake :class:`~tests.test_ingest_voice.FakeTools` runner, behind a
fake ``cli_ingest_folder.whisper_resolver`` swapped in with ``monkeypatch.setattr`` (a
standard test double, rule 13), so no audio tool is ever run here.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain import cli_ingest_folder
from brain.cli import app
from brain.drop_folder import IMPORTED_DIR, DropFolder, run
from brain.ingest.capped_process import CappedRun
from brain.ingest.ocr import OcrSetup
from brain.ingest.voice import whisper_timeout
from brain.ingest.whisper import HINT_MISSING_BINARY, WhisperSetup
from brain.settings.ocr import OcrSettings
from brain.settings.voice import VoiceSettings
from tests.conftest import CountingEmbedder
from tests.test_ingest_voice import BINARY, MODEL, TRANSCRIPT, FakeTools

OCR_OFF = OcrSettings(mode="off", lang="eng", max_pages=50)
AUDIO_BYTES = b"synthetic m4a bytes that are not audio"


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    fake = FakeTools(duration=42.0)
    monkeypatch.setattr(
        "brain.cli_ingest_folder.whisper_resolver",
        lambda env: WhisperSetup(
            settings=VoiceSettings(model=MODEL, max_minutes=60),
            binary=BINARY,
            unavailable=None,
            runner=fake,
        ),
    )
    return fake


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    return brain_home


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(path))
    return path


@pytest.fixture
def drop(tmp_path: Path) -> Path:
    folder = tmp_path / "memos"
    folder.mkdir()
    return folder


def _memo(folder: Path, name: str = "New Recording 1.m4a", body: bytes = AUDIO_BYTES) -> Path:
    path = folder / name
    path.write_bytes(body)
    stamp = time.time() - 600.0  # past the runner's settle window
    os.utime(path, (stamp, stamp))
    return path


def _configure(home: Path, drop: Path, extra: str = "") -> None:
    text = f'[[folder]]\npath = "{drop}"\nkind = "voice"\n{extra}\n'
    (home / "drop-folders.toml").write_text(text, encoding="utf-8")


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-folder", "--no-enrich", *args])


def _docs(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.title, d.content, d.content_type, d.sensitivity, d.source_path, s.kind
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'voice'
        ORDER BY d.title, d.source_path
        """
    ).fetchall()
    keys = ("title", "content", "content_type", "sensitivity", "source_path", "kind")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _imported(drop: Path, src_name: str, body: bytes = AUDIO_BYTES) -> Path:
    stem, ext = os.path.splitext(src_name)
    return drop.resolve() / IMPORTED_DIR / f"{stem}-{hashlib.sha256(body).hexdigest()[:8]}{ext}"


# --- AC1 ---------------------------------------------------------------------------------


def test_a_memo_becomes_a_confidential_voice_transcript(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _memo(drop)
    _configure(home, drop)

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output
    final = _imported(drop, src.name)
    assert final.is_file() and not src.exists()
    assert _docs(test_db) == [
        {
            "title": "New Recording 1",
            "content": TRANSCRIPT,
            "content_type": "voice_memo",
            "sensitivity": "confidential",
            "source_path": str(final),
            "kind": "voice",
        }
    ]
    assert "1 ingested" in result.output


def test_only_the_transcript_enters_the_brain_never_the_audio(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _memo(drop)
    _configure(home, drop)

    assert _run("--configured", "--since", "2d").exit_code == 0

    bodies = test_db.execute("SELECT content FROM documents").fetchall()
    chunks = test_db.execute("SELECT content FROM chunks").fetchall()
    assert all(AUDIO_BYTES.decode() not in row[0] for row in [*bodies, *chunks])
    audio_in_vault = [p for p in vault.rglob("*") if p.suffix in {".m4a", ".mp3", ".wav"}]
    assert audio_in_vault == []
    assert all(not p.exists() for p in tools.wav_paths)


def test_two_memos_with_one_name_become_two_documents(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, drop)
    _memo(drop, body=b"first synthetic memo")
    assert _run("--configured", "--since", "2d").exit_code == 0
    _memo(drop, body=b"second synthetic memo")
    tools.whisper = CappedRun(b" A different synthetic memo entirely.\n", 0, False, False)

    assert _run("--configured", "--since", "2d").exit_code == 0

    docs = _docs(test_db)
    assert [d["title"] for d in docs] == ["New Recording 1", "New Recording 1"]
    assert len({d["source_path"] for d in docs}) == 2


def test_a_folder_sensitivity_override_is_honoured(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _memo(drop)
    _configure(home, drop, 'sensitivity = "normal"')

    assert _run("--configured", "--since", "2d").exit_code == 0

    assert _docs(test_db)[0]["sensitivity"] == "normal"


def test_a_run_sensitivity_override_is_honoured(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _memo(drop)
    _configure(home, drop)

    result = _run("--dir", str(drop), "--since", "2d", "--sensitivity", "normal")

    assert result.exit_code == 0, result.output
    assert _docs(test_db)[0]["sensitivity"] == "normal"


# --- AC2 / AC3: skipped and counted, the file stays in imported/ ------------------------


def test_a_memo_over_the_cap_is_rejected_and_counted(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _memo(drop)
    _configure(home, drop)
    tools.duration = 60 * 60 + 1

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "rejected  New Recording 1.m4a (AudioTooLongError)" in result.output
    assert "1 rejected" in result.output
    assert _imported(drop, src.name).is_file()
    assert _docs(test_db) == []


def test_a_transcription_timeout_is_rejected_by_class_only(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _memo(drop)
    _configure(home, drop)
    tools.whisper = CappedRun(b"", -9, True, False)

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "rejected  New Recording 1.m4a (AudioTimeoutError)" in result.output
    assert TRANSCRIPT not in result.output
    assert _imported(drop, src.name).is_file()
    assert _docs(test_db) == []


def test_a_conversion_timeout_is_rejected(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    _memo(drop)
    _configure(home, drop)
    tools.afconvert = CappedRun(b"", -9, True, False)

    result = _run("--configured", "--since", "2d")

    assert "rejected  New Recording 1.m4a (AudioTimeoutError)" in result.output


# --- D4: a whisper failure goes back, with a warning naming the file --------------------


def test_a_whisper_failure_moves_the_memo_back_with_a_warning(
    home: Path, drop: Path, vault: Path, tools: FakeTools, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _memo(drop)
    _configure(home, drop)
    tools.whisper = CappedRun(b"", 3, False, False)

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert src.is_file()
    assert not _imported(drop, src.name).exists()
    imported_name = _imported(drop, src.name).name
    assert (
        f"warning: {imported_name} was not transcribed (TranscriptionError); "
        "it goes back to the folder and is retried next run"
    ) in result.output
    assert "failed    New Recording 1.m4a (TranscriptionError)" in result.output
    assert _docs(test_db) == []


# --- whisper unavailable: the folder is skipped, nothing moves ---------------------------


def test_unavailable_whisper_skips_the_folder_without_moving_anything(
    home: Path, drop: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _memo(drop)
    _configure(home, drop)
    monkeypatch.setattr(
        "brain.cli_ingest_folder.whisper_resolver",
        lambda env: WhisperSetup(VoiceSettings(MODEL, 60), None, HINT_MISSING_BINARY),
    )

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert f"{HINT_MISSING_BINARY}" in result.output
    assert src.is_file()
    assert not (drop / IMPORTED_DIR).exists()


def test_the_real_resolver_with_no_model_skips_a_voice_folder(
    home: Path, drop: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    src = _memo(drop)
    _configure(home, drop)

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "voice memos are off: set BRAIN_WHISPER_MODEL" in result.output
    assert src.is_file()


def test_a_bad_whisper_knob_skips_only_the_voice_folder(
    home: Path, drop: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _memo(drop)
    _configure(home, drop)
    monkeypatch.setenv("BRAIN_WHISPER_MAX_MINUTES", "0")

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 1
    assert "BRAIN_WHISPER_MAX_MINUTES must be" in result.output


def test_whisper_is_not_resolved_without_an_audio_folder(
    home: Path, tmp_path: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("# Synthetic\n\nA synthetic note body.\n")
    stamp = time.time() - 600.0
    os.utime(notes / "n.md", (stamp, stamp))
    text = f'[[folder]]\npath = "{notes}"\nkind = "manual"\nextensions = [".md"]\n'
    (home / "drop-folders.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("BRAIN_WHISPER_MAX_MINUTES", "0")  # would refuse if it were read

    def refuse(env: Any) -> WhisperSetup:
        raise AssertionError("whisper resolved for a folder without audio")

    monkeypatch.setattr("brain.cli_ingest_folder.whisper_resolver", refuse)

    result = _run("--configured", "--since", "2d")

    assert result.exit_code == 0, result.output


def test_dry_run_lists_memos_without_transcribing(
    home: Path, drop: Path, tools: FakeTools,
) -> None:
    _memo(drop)
    _configure(home, drop)

    result = _run("--configured", "--since", "2d", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "would ingest  New Recording 1.m4a" in result.output
    assert tools.calls == []


# --- D1: a memo runs on whisper's timeout, not the run's remaining deadline --------------


def test_a_long_memo_finishes_past_the_deadline_and_the_next_file_is_deferred(
    tmp_path: Path,
) -> None:
    drop = tmp_path / "memos"
    drop.mkdir()
    first = _memo(drop, "a-first.m4a", b"first memo")
    stamp = first.stat().st_mtime - 10
    os.utime(first, (stamp, stamp))
    _memo(drop, "b-second.m4a", b"second memo")
    clock = [0.0]
    tools = FakeTools(duration=40 * 60.0)
    original = tools.__call__

    def slow_whisper(argv: list[str], timeout: float, cap: int) -> CappedRun:
        if argv[0] == BINARY:
            clock[0] += 900.0  # this memo outlasts the whole 480 s run deadline
        return original(argv, timeout, cap)

    setup = WhisperSetup(VoiceSettings(MODEL, 60), BINARY, None, runner=slow_whisper)
    ctx = cli_ingest_folder._Context(
        cfg=None, embedder=None, enricher=None, graph_syncer=None,  # type: ignore[arg-type]
        ocr=OcrSetup(OCR_OFF, None), enrich=False, allow_secrets=False, whisper=setup,
    )
    seen: list[str] = []

    def ingest(conn: Any, path: Path, *, title: str, timeout: float) -> str:
        doc = cli_ingest_folder._extract(ctx, path, timeout)
        seen.append(doc.content)
        return "ingested"

    folder = DropFolder(drop, "ingest", "voice", frozenset({".m4a"}), "confidential")
    report = run(
        cast(psycopg.Connection[Any], None),
        folder,
        since=datetime.now(UTC) - timedelta(days=1),
        ingest=ingest,
        clock=lambda: clock[0],
    )

    assert [o.status for o in report.outcomes] == ["ingested"]
    assert report.deferred == 1
    assert seen == [TRANSCRIPT]
    whisper_calls = [t for argv, t, _ in tools.calls if argv[0] == BINARY]
    assert whisper_calls == [whisper_timeout(40 * 60.0)]


# --- AC4 and `brain ingest` ----------------------------------------------------------------


def test_ingest_dir_does_not_pick_up_audio(
    tmp_path: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "note.md").write_text("# Synthetic\n\nA synthetic note body.\n")
    for ext in (".m4a", ".mp3", ".wav"):
        (tree / f"memo{ext}").write_bytes(AUDIO_BYTES)

    result = CliRunner().invoke(app, ["ingest-dir", "--no-enrich", str(tree)])

    assert result.exit_code == 0, result.output
    rows = test_db.execute("SELECT source_path FROM documents").fetchall()
    assert [Path(r[0]).name for r in rows] == ["note.md"]
    # Never attempted, not attempted-and-failed: a walked memo would print "failed: memo…".
    assert "memo" not in result.output


def test_brain_ingest_of_a_memo_with_voice_off_is_one_line(
    tmp_path: Path, vault: Path, embedder: CountingEmbedder,
    test_db: psycopg.Connection[Any],
) -> None:
    memo = _memo(tmp_path, "memo.m4a")

    result = CliRunner().invoke(app, ["ingest", "--no-enrich", str(memo)])

    assert result.exit_code == 1
    assert "cannot read memo.m4a: WhisperUnavailableError: voice memos are off" in result.output
    assert "Traceback" not in result.output

