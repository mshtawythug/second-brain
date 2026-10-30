"""Tests for the voice-memo extractor, :mod:`brain.ingest.voice` (RM-38).

No test runs ``afinfo``, ``afconvert`` or ``whisper-cli``: :class:`FakeTools` is the
injected runner, answering each tool from a script and recording its argv and timeout.
The memo files are a few synthetic bytes; only their names and sizes matter here. The
one test that runs the real tools is ``live_whisper``-marked and skipped without them.
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from brain.errors import describe_failure
from brain.ingest import voice as voice_mod
from brain.ingest.capped_process import CappedRun
from brain.ingest.voice import (
    AFCONVERT_TIMEOUT_S,
    AFINFO_TIMEOUT_S,
    CONTENT_TYPE,
    TOOL_MAX_STDOUT_BYTES,
    TRANSIENT_VOICE_ERRORS,
    WAV_BYTES_PER_S,
    WHISPER_MAX_STDOUT_BYTES,
    WHISPER_TIMEOUT_SLACK_S,
    AudioTimeoutError,
    AudioTooLargeError,
    AudioTooLongError,
    TranscriptionError,
    UnreadableAudioError,
    VoiceError,
    WhisperUnavailableError,
    extract_voice,
    whisper_timeout,
)
from brain.ingest.whisper import AFCONVERT_PATH, AFINFO_PATH, HINT_OFF, WhisperSetup
from brain.settings.voice import VoiceSettings

MODEL = Path("/models/ggml-base.en.bin")
BINARY = "/opt/homebrew/bin/whisper-cli"
TRANSCRIPT = "Synthetic memo: buy more index cards."
ECHO_CANARY = "zz-memo-words-that-must-not-be-echoed"


def _afinfo_out(duration: float | str) -> bytes:
    return (
        f"File:           /somewhere/{ECHO_CANARY}.m4a\nFile type ID:   m4af\n"
        f"Data format:     1 ch,  48000 Hz, aac\nestimated duration: {duration} sec\n"
        "audio bytes: 1234\n"
    ).encode()


@dataclass
class FakeTools:
    """The injected :data:`AudioRunner`: one scripted answer per tool."""

    duration: float | str = 2.0
    afinfo: CappedRun | OSError | None = None
    afconvert: CappedRun | OSError | None = None
    wav_seconds: float | None = None  # the converted WAV's length; defaults to ``duration``
    whisper: CappedRun | OSError | None = None
    calls: list[tuple[list[str], float, int]] = field(default_factory=list)
    wav_paths: list[Path] = field(default_factory=list)

    def __call__(self, argv: list[str], timeout: float, max_stdout_bytes: int) -> CappedRun:
        self.calls.append((list(argv), timeout, max_stdout_bytes))
        tool = argv[0]
        if tool == AFINFO_PATH:
            return self._answer(self.afinfo, CappedRun(_afinfo_out(self.duration), 0, False, False))
        if tool == AFCONVERT_PATH:
            answer = self._answer(self.afconvert, CappedRun(b"", 0, False, False))
            if answer.returncode == 0 and not answer.timed_out:
                self._write_wav(Path(argv[-1]))
            return answer
        assert tool == BINARY, argv
        out = f" {TRANSCRIPT}\n [BLANK_AUDIO]\n".encode()
        return self._answer(self.whisper, CappedRun(out, 0, False, False))

    def _answer(self, scripted: CappedRun | OSError | None, default: CappedRun) -> CappedRun:
        if isinstance(scripted, OSError):
            raise scripted
        return scripted if scripted is not None else default

    def _write_wav(self, path: Path) -> None:
        self.wav_paths.append(path)
        seconds = self.wav_seconds if self.wav_seconds is not None else float(self.duration)
        path.write_bytes(b"\0" * int(seconds * WAV_BYTES_PER_S))

    def argv_of(self, tool: str) -> list[str]:
        return next(argv for argv, _, _ in self.calls if argv[0] == tool)

    def timeout_of(self, tool: str) -> float:
        return next(t for argv, t, _ in self.calls if argv[0] == tool)


def _setup(tools: FakeTools, *, max_minutes: int = 60) -> WhisperSetup:
    return WhisperSetup(
        settings=VoiceSettings(model=MODEL, max_minutes=max_minutes),
        binary=BINARY,
        unavailable=None,
        runner=tools,
    )


@pytest.fixture
def memo(tmp_path: Path) -> Path:
    path = tmp_path / "New Recording 1.m4a"
    path.write_bytes(b"synthetic m4a bytes")
    return path


# --- AC1: a memo becomes its transcript ---------------------------------------------


def test_a_memo_becomes_a_voice_memo_document(memo: Path) -> None:
    tools = FakeTools(duration=2.0)

    doc = extract_voice(memo, whisper=_setup(tools))

    assert doc.content == TRANSCRIPT
    assert doc.content_type == CONTENT_TYPE == "voice_memo"
    assert doc.title == "New Recording 1"
    assert doc.source_path == str(memo.resolve())
    assert doc.metadata == {
        "audio": {"duration_s": 2.0, "format": "m4a"},
        "transcription": {"engine": "whisper.cpp", "model": "ggml-base.en.bin"},
    }


def test_each_tool_gets_its_argv_timeout_and_output_cap(memo: Path) -> None:
    tools = FakeTools(duration=30.0)

    extract_voice(memo, whisper=_setup(tools))

    assert [argv[0] for argv, _, _ in tools.calls] == [AFINFO_PATH, AFCONVERT_PATH, BINARY]
    assert tools.argv_of(AFINFO_PATH) == [AFINFO_PATH, str(memo.absolute())]
    wav = tools.wav_paths[0]
    assert tools.argv_of(AFCONVERT_PATH) == [
        AFCONVERT_PATH, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
        str(memo.absolute()), str(wav),
    ]
    assert tools.argv_of(BINARY) == [BINARY, "-m", str(MODEL), "-f", str(wav), "-nt", "-np"]
    assert [t for _, t, _ in tools.calls] == [AFINFO_TIMEOUT_S, AFCONVERT_TIMEOUT_S, 120.0]
    assert [cap for _, _, cap in tools.calls] == [
        TOOL_MAX_STDOUT_BYTES, TOOL_MAX_STDOUT_BYTES, WHISPER_MAX_STDOUT_BYTES,
    ]
    assert (AFINFO_TIMEOUT_S, AFCONVERT_TIMEOUT_S, WHISPER_TIMEOUT_SLACK_S) == (10.0, 60.0, 60.0)


def test_whisper_timeout_is_twice_the_duration_plus_a_minute() -> None:
    assert whisper_timeout(0.0) == 60.0
    assert whisper_timeout(2400.0) == 4860.0


def test_the_whisper_timeout_follows_the_longer_of_the_two_durations(memo: Path) -> None:
    tools = FakeTools(duration=10.0, wav_seconds=20.0)

    extract_voice(memo, whisper=_setup(tools))

    assert tools.timeout_of(BINARY) == whisper_timeout(20.0)


def test_the_converted_wav_is_removed_after_success(memo: Path) -> None:
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools))

    (wav,) = tools.wav_paths
    assert not wav.exists()
    assert not wav.parent.exists()


def test_the_temporary_directory_is_private(memo: Path) -> None:
    modes: list[int] = []
    tools = FakeTools()
    original = tools._write_wav

    def record(path: Path) -> None:
        modes.append(path.parent.stat().st_mode & 0o777)
        original(path)

    tools._write_wav = record  # type: ignore[method-assign]

    extract_voice(memo, whisper=_setup(tools))

    assert modes == [0o700]


def test_the_transcript_drops_blank_audio_markers_and_joins_segments(memo: Path) -> None:
    out = b" First part.\n\n [BLANK_AUDIO]\n Second part. [BLANK_AUDIO]\n"
    tools = FakeTools(whisper=CappedRun(out, 0, False, False))

    doc = extract_voice(memo, whisper=_setup(tools))

    assert doc.content == "First part. Second part."


def test_silence_is_an_empty_document(memo: Path) -> None:
    tools = FakeTools(whisper=CappedRun(b" [BLANK_AUDIO]\n", 0, False, False))

    assert extract_voice(memo, whisper=_setup(tools)).content == ""


def test_undecodable_bytes_are_replaced(memo: Path) -> None:
    tools = FakeTools(whisper=CappedRun(b"caf\xff memo", 0, False, False))

    assert extract_voice(memo, whisper=_setup(tools)).content == "caf� memo"


@pytest.mark.parametrize("suffix", [".mp3", ".wav", ".M4A"])
def test_the_format_is_the_lowercased_suffix(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"memo{suffix}"
    path.write_bytes(b"x")

    doc = extract_voice(path, whisper=_setup(FakeTools()))

    assert doc.metadata["audio"]["format"] == suffix[1:].lower()


# --- whisper unavailable: before the file is touched --------------------------------


def test_unavailable_whisper_is_raised_before_any_tool_runs(memo: Path) -> None:
    tools = FakeTools()
    setup = WhisperSetup(VoiceSettings(None, 60), binary=None, unavailable=HINT_OFF, runner=tools)

    with pytest.raises(WhisperUnavailableError, match="BRAIN_WHISPER_MODEL"):
        extract_voice(memo, whisper=setup)

    assert tools.calls == []


def test_unavailable_whisper_is_raised_even_for_a_missing_file(tmp_path: Path) -> None:
    setup = WhisperSetup(VoiceSettings(None, 60), binary=None, unavailable=HINT_OFF)

    with pytest.raises(WhisperUnavailableError):
        extract_voice(tmp_path / "gone.m4a", whisper=setup)


def test_whisper_is_resolved_from_the_environment_by_default(
    memo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BRAIN_WHISPER_MODEL", raising=False)

    with pytest.raises(WhisperUnavailableError, match="BRAIN_WHISPER_MODEL"):
        extract_voice(memo)


# --- AC2: the duration cap ----------------------------------------------------------


def test_a_memo_over_the_cap_is_too_long_and_never_converted(memo: Path) -> None:
    tools = FakeTools(duration=60 * 60 + 1)

    with pytest.raises(AudioTooLongError):
        extract_voice(memo, whisper=_setup(tools, max_minutes=60))

    assert [argv[0] for argv, _, _ in tools.calls] == [AFINFO_PATH]


def test_a_memo_exactly_at_the_cap_is_transcribed(memo: Path) -> None:
    tools = FakeTools(duration=60.0)

    assert extract_voice(memo, whisper=_setup(tools, max_minutes=1)).content == TRANSCRIPT


def test_a_header_that_lies_is_caught_by_the_wav_size(memo: Path) -> None:
    tools = FakeTools(duration=5.0, wav_seconds=62.0)

    with pytest.raises(AudioTooLongError):
        extract_voice(memo, whisper=_setup(tools, max_minutes=1))

    assert BINARY not in [argv[0] for argv, _, _ in tools.calls]
    assert not tools.wav_paths[0].exists()


def test_the_wav_check_allows_a_header_second(memo: Path) -> None:
    tools = FakeTools(duration=60.0, wav_seconds=60.9)

    assert extract_voice(memo, whisper=_setup(tools, max_minutes=1)).content == TRANSCRIPT


# --- unreadable files ---------------------------------------------------------------


@pytest.mark.parametrize("duration", ["nan", "inf", "0", "-3", "abc"])
def test_a_duration_that_is_not_a_positive_finite_number_is_unreadable(
    memo: Path, duration: str
) -> None:
    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(FakeTools(duration=duration)))


def test_no_duration_line_is_unreadable(memo: Path) -> None:
    tools = FakeTools(afinfo=CappedRun(b"File: x\nData format: aac\n", 0, False, False))

    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(tools))


def test_an_afinfo_failure_is_unreadable(memo: Path) -> None:
    tools = FakeTools(afinfo=CappedRun(b"", 1, False, False))

    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(tools))


def test_an_oversized_afinfo_answer_is_unreadable(memo: Path) -> None:
    tools = FakeTools(afinfo=CappedRun(b"", -9, False, True))

    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(tools))


def test_an_afconvert_failure_is_unreadable_and_cleans_up(memo: Path) -> None:
    tools = FakeTools(afconvert=CappedRun(b"", 1, False, False))

    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(tools))

    assert BINARY not in [argv[0] for argv, _, _ in tools.calls]


def test_afconvert_that_writes_no_wav_is_unreadable(memo: Path) -> None:
    tools = FakeTools()
    tools._write_wav = lambda path: None  # type: ignore[method-assign]

    with pytest.raises(UnreadableAudioError):
        extract_voice(memo, whisper=_setup(tools))


def test_a_symlink_is_refused_without_running_a_tool(memo: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.m4a"
    link.symlink_to(memo)
    tools = FakeTools()

    with pytest.raises(UnreadableAudioError):
        extract_voice(link, whisper=_setup(tools))

    assert tools.calls == []


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.m4a"
    os.mkfifo(fifo)
    tools = FakeTools()

    with pytest.raises(UnreadableAudioError):
        extract_voice(fifo, whisper=_setup(tools))

    assert tools.calls == []


def test_a_missing_file_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_voice(tmp_path / "gone.m4a", whisper=_setup(FakeTools()))


def test_a_file_over_the_size_cap_is_too_large(memo: Path) -> None:
    tools = FakeTools()

    with pytest.raises(AudioTooLargeError):
        extract_voice(memo, whisper=_setup(tools), max_file_bytes=memo.stat().st_size - 1)

    assert tools.calls == []


def test_a_file_at_the_size_cap_is_read(memo: Path) -> None:
    doc = extract_voice(memo, whisper=_setup(FakeTools()), max_file_bytes=memo.stat().st_size)

    assert doc.content == TRANSCRIPT


def test_the_default_size_cap_is_one_gibibyte() -> None:
    assert voice_mod.MAX_FILE_BYTES == 1 << 30


# --- AC3: timeouts --------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["afinfo", "afconvert", "whisper"])
def test_a_timeout_in_any_tool_is_an_audio_timeout(memo: Path, tool: str) -> None:
    tools = FakeTools(**{tool: CappedRun(b"", -9, True, False)})

    with pytest.raises(AudioTimeoutError):
        extract_voice(memo, whisper=_setup(tools))

    assert all(not p.exists() for p in tools.wav_paths)


def test_a_timeout_logs_a_class_only_warning(memo: Path, caplog: pytest.LogCaptureFixture) -> None:
    tools = FakeTools(whisper=CappedRun(b"", -9, True, False))

    with (
        caplog.at_level(logging.WARNING, logger="brain.ingest.voice"),
        pytest.raises(AudioTimeoutError),
    ):
        extract_voice(memo, whisper=_setup(tools))

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "AudioTimeoutError" in record.getMessage()
    assert memo.name not in record.getMessage()
    assert ECHO_CANARY not in record.getMessage()


# --- transient failures (D4) ---------------------------------------------------------


def test_a_whisper_failure_is_transient(memo: Path) -> None:
    tools = FakeTools(whisper=CappedRun(f"{ECHO_CANARY}".encode(), 3, False, False))

    with pytest.raises(TranscriptionError) as info:
        extract_voice(memo, whisper=_setup(tools))

    assert isinstance(info.value, TRANSIENT_VOICE_ERRORS)
    assert ECHO_CANARY not in str(info.value)


def test_oversized_whisper_output_is_a_transcription_error(memo: Path) -> None:
    tools = FakeTools(whisper=CappedRun(b"", -9, False, True))

    with pytest.raises(TranscriptionError):
        extract_voice(memo, whisper=_setup(tools))


@pytest.mark.parametrize("tool", ["afinfo", "afconvert", "whisper"])
def test_a_tool_that_cannot_start_is_a_transcription_error(memo: Path, tool: str) -> None:
    tools = FakeTools(**{tool: PermissionError(13, f"denied {ECHO_CANARY}")})

    with pytest.raises(TranscriptionError) as info:
        extract_voice(memo, whisper=_setup(tools))

    assert ECHO_CANARY not in str(info.value)
    assert describe_failure(info.value) == "TranscriptionError from PermissionError[EACCES]"


def test_the_transient_and_rejected_classes_do_not_overlap() -> None:
    assert set(TRANSIENT_VOICE_ERRORS) == {WhisperUnavailableError, TranscriptionError}
    for cls in (AudioTooLongError, AudioTooLargeError, UnreadableAudioError, AudioTimeoutError):
        assert issubclass(cls, VoiceError)
        assert not issubclass(cls, TRANSIENT_VOICE_ERRORS)


def test_every_voice_error_is_a_value_error() -> None:
    assert issubclass(VoiceError, ValueError)


@pytest.mark.parametrize(
    "tools",
    [
        FakeTools(duration=ECHO_CANARY),
        FakeTools(duration=99_999.0),
        FakeTools(whisper=CappedRun(b"", -9, True, False)),
    ],
)
def test_messages_never_carry_file_output(memo: Path, tools: FakeTools) -> None:
    with pytest.raises(VoiceError) as info:
        extract_voice(memo, whisper=_setup(tools, max_minutes=1))

    assert ECHO_CANARY not in str(info.value)
    assert memo.name not in str(info.value)


# --- the real tools (skipped unless installed) ---------------------------------------


def _tone_wav(path: Path, seconds: float = 1.0) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"".join(struct.pack("<h", 0) for _ in range(int(16_000 * seconds))))


@pytest.mark.live_whisper
def test_live_whisper_transcribes_a_synthetic_tone(tmp_path: Path) -> None:
    """Runs the real tools on a generated silent WAV; no model is ever downloaded."""
    model = os.environ.get("BRAIN_LIVE_WHISPER_MODEL", "")
    binary = shutil.which("whisper-cli")
    if not model or not Path(model).is_file() or binary is None:
        pytest.skip("needs whisper-cli on PATH and BRAIN_LIVE_WHISPER_MODEL naming a model")
    if not (Path(AFINFO_PATH).is_file() and Path(AFCONVERT_PATH).is_file()):
        pytest.skip("needs macOS's afinfo and afconvert")
    path = tmp_path / "tone.wav"
    _tone_wav(path)
    setup = WhisperSetup(VoiceSettings(Path(model), 60), binary=binary, unavailable=None)

    doc = extract_voice(path, whisper=setup)

    assert doc.content_type == CONTENT_TYPE
    assert doc.metadata["audio"]["duration_s"] == pytest.approx(1.0, abs=0.1)
