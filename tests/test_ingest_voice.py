"""Tests for the voice-memo extractor, :mod:`brain.ingest.voice` (RM-38).

No test runs ``afinfo``, ``afconvert`` or ``whisper-cli``: :class:`FakeTools` is the
injected runner, answering each tool from a script and recording its argv and timeout.
The memo files are a few synthetic bytes; only their names and sizes matter here. The
one test that runs the real tools is ``live_whisper``-marked and skipped without them.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    TranscriptTooLargeError,
    UnreadableAudioError,
    VoiceError,
    WhisperFailedError,
    WhisperUnavailableError,
    extract_voice,
    sweep_stale_tempdirs,
    wav_byte_limit,
    whisper_timeout,
)
from brain.ingest.whisper import AFCONVERT_PATH, AFINFO_PATH, HINT_OFF, WhisperSetup
from brain.settings.voice import VoiceSettings

MODEL = Path("/models/ggml-base.en.bin")
BINARY = "/opt/homebrew/bin/whisper-cli"
TRANSCRIPT = "Synthetic memo: buy more index cards."
ECHO_CANARY = "zz-memo-words-that-must-not-be-echoed"


def _afinfo_out(duration: float | str, name: str = "/dev/fd/9") -> bytes:
    """``afinfo``'s answer. Like the real tool it prints the name it was given, raw."""
    return (
        f"File:           {name}\nComment: {ECHO_CANARY}\nFile type ID:   m4af\n"
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
    wav_bytes: int | None = None  # overrides ``wav_seconds``: the WAV's exact size
    whisper_raises: BaseException | None = None  # e.g. SystemExit from a SIGTERM mid-whisper
    calls: list[tuple[list[str], float, int]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)  # pass_fds, max_file_bytes
    offsets: list[int] = field(default_factory=list)  # the memo fd's offset at each call
    wav_paths: list[Path] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        timeout: float,
        max_stdout_bytes: int,
        *,
        pass_fds: tuple[int, ...] = (),
        max_file_bytes: int | None = None,
    ) -> CappedRun:
        self.calls.append((list(argv), timeout, max_stdout_bytes))
        self.options.append({"pass_fds": pass_fds, "max_file_bytes": max_file_bytes})
        for fd in pass_fds:
            self.offsets.append(os.lseek(fd, 0, os.SEEK_CUR))
            os.lseek(fd, 5, os.SEEK_SET)  # a real tool reads, and moves the shared offset
        tool = argv[0]
        if tool == AFINFO_PATH:
            out = _afinfo_out(self.duration, argv[1])
            return self._answer(self.afinfo, CappedRun(out, 0, False, False))
        if tool == AFCONVERT_PATH:
            answer = self._answer(self.afconvert, CappedRun(b"", 0, False, False))
            if answer.returncode == 0 and not answer.timed_out or self.wav_bytes is not None:
                self._write_wav(Path(argv[-1]))
            return answer
        if self.whisper_raises is not None:
            raise self.whisper_raises
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
        size = self.wav_bytes if self.wav_bytes is not None else int(seconds * WAV_BYTES_PER_S)
        path.write_bytes(b"\0" * size)

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
    (fd,) = tools.options[0]["pass_fds"]
    assert tools.argv_of(AFINFO_PATH) == [AFINFO_PATH, f"/dev/fd/{fd}"]
    wav = tools.wav_paths[0]
    assert tools.argv_of(AFCONVERT_PATH) == [
        AFCONVERT_PATH, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"/dev/fd/{fd}", str(wav),
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


def test_oversized_whisper_output_is_rejected(memo: Path) -> None:
    """More than 4 MiB of text from a capped memo would recur every run (rev-107 F2)."""
    tools = FakeTools(whisper=CappedRun(b"", -9, False, True))

    with pytest.raises(TranscriptTooLargeError) as info:
        extract_voice(memo, whisper=_setup(tools))

    assert not isinstance(info.value, TRANSIENT_VOICE_ERRORS)


def test_a_whisper_failure_is_the_counted_subclass(memo: Path) -> None:
    tools = FakeTools(whisper=CappedRun(b"", 3, False, False))

    with pytest.raises(WhisperFailedError) as info:
        extract_voice(memo, whisper=_setup(tools))

    assert isinstance(info.value, TranscriptionError)


@pytest.mark.parametrize("tool", ["afinfo", "afconvert", "whisper"])
def test_a_tool_that_cannot_start_is_a_transcription_error(memo: Path, tool: str) -> None:
    tools = FakeTools(**{tool: PermissionError(13, f"denied {ECHO_CANARY}")})

    with pytest.raises(TranscriptionError) as info:
        extract_voice(memo, whisper=_setup(tools))

    assert ECHO_CANARY not in str(info.value)
    assert describe_failure(info.value) == "TranscriptionError from PermissionError[EACCES]"


def test_the_transient_and_rejected_classes_do_not_overlap() -> None:
    assert set(TRANSIENT_VOICE_ERRORS) == {WhisperUnavailableError, TranscriptionError}
    for cls in (
        AudioTooLongError,
        AudioTooLargeError,
        UnreadableAudioError,
        AudioTimeoutError,
        TranscriptTooLargeError,
    ):
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


# --- review round 1 (PR #107): the memo is read through one fd, the WAV is bounded ------


def test_a_name_that_fakes_a_duration_line_cannot_skip_the_cap(tmp_path: Path) -> None:
    """sec-107 F1: ``afinfo`` prints the name it is given raw, newlines included."""
    memo = tmp_path / "x\nestimated duration: 0.5 sec\ny.m4a"
    memo.write_bytes(b"synthetic")
    tools = FakeTools(duration=2 * 60 * 60.0, wav_seconds=1.0)

    with pytest.raises(AudioTooLongError):
        extract_voice(memo, whisper=_setup(tools, max_minutes=60))

    assert [argv[0] for argv, _, _ in tools.calls] == [AFINFO_PATH]


def test_no_tool_is_given_the_memo_s_name(memo: Path) -> None:
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools))

    assert all(memo.name not in " ".join(argv) for argv, _, _ in tools.calls)


def test_both_audio_tools_read_the_one_fd_and_whisper_reads_none(memo: Path) -> None:
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools))

    afinfo, afconvert, whisper = tools.options
    (fd,) = afinfo["pass_fds"]
    assert afconvert["pass_fds"] == (fd,)
    assert whisper["pass_fds"] == ()


def test_each_tool_reads_the_memo_from_its_start(memo: Path) -> None:
    """``/dev/fd/N`` shares the fd's offset; afconvert fails if afinfo left it moved."""
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools))

    assert tools.offsets == [0, 0]


def test_the_fd_is_closed_after_success_and_failure(memo: Path) -> None:
    for tools in (FakeTools(), FakeTools(afconvert=CappedRun(b"", 1, False, False))):
        with contextlib.suppress(UnreadableAudioError):
            extract_voice(memo, whisper=_setup(tools))
        (fd,) = tools.options[0]["pass_fds"]
        with pytest.raises(OSError):
            os.fstat(fd)


def test_a_file_swapped_after_the_open_is_not_read(memo: Path, tmp_path: Path) -> None:
    """sec-107 F2: the tools follow the fd, not the name, so a swap after open is moot."""
    other = tmp_path / "other.m4a"
    other.write_bytes(b"a different file")
    tools = FakeTools()
    original = tools._answer
    seen: list[bytes] = []

    def swap_then_answer(scripted: Any, default: CappedRun) -> CappedRun:
        if not seen:
            memo.unlink()
            memo.symlink_to(other)
            fd = tools.options[0]["pass_fds"][0]
            os.lseek(fd, 0, os.SEEK_SET)
            seen.append(os.read(fd, 100))
        return original(scripted, default)

    tools._answer = swap_then_answer  # type: ignore[method-assign]

    extract_voice(memo, whisper=_setup(tools))

    assert seen == [b"synthetic m4a bytes"]


def test_only_afconvert_gets_the_file_size_limit(memo: Path) -> None:
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools, max_minutes=2))

    limits = [o["max_file_bytes"] for o in tools.options]
    assert limits == [None, wav_byte_limit(120.0), None]


def test_the_wav_limit_is_the_cap_plus_slack_plus_a_header() -> None:
    assert wav_byte_limit(60.0) == int((60.0 + 1.0) * WAV_BYTES_PER_S) + 4096


@pytest.mark.parametrize("returncode", [0, -25, 1])
def test_a_wav_stopped_at_the_limit_is_too_long(memo: Path, returncode: int) -> None:
    """RLIMIT_FSIZE stops afconvert at the limit: exit 0 (SIGXFSZ ignored) or a signal."""
    limit = wav_byte_limit(60.0)
    tools = FakeTools(
        duration=5.0, wav_bytes=limit, afconvert=CappedRun(b"", returncode, False, False)
    )

    with pytest.raises(AudioTooLongError):
        extract_voice(memo, whisper=_setup(tools, max_minutes=1))

    assert BINARY not in [argv[0] for argv, _, _ in tools.calls]


def test_the_duration_stored_is_the_wav_s(memo: Path) -> None:
    """sec-107 F1.3: the header's duration is a claim; the WAV's length is measured."""
    tools = FakeTools(duration=5.0, wav_seconds=7.0)

    doc = extract_voice(memo, whisper=_setup(tools))

    assert doc.metadata["audio"]["duration_s"] == 7.0


def test_a_system_exit_mid_whisper_removes_the_wav_and_closes_the_fd(memo: Path) -> None:
    """rev-107 F1: SIGTERM is a SystemExit; the temporary directory must still go."""
    tools = FakeTools(whisper_raises=SystemExit(143))

    with pytest.raises(SystemExit):
        extract_voice(memo, whisper=_setup(tools))

    (wav,) = tools.wav_paths
    assert not wav.parent.exists()
    (fd,) = tools.options[0]["pass_fds"]
    with pytest.raises(OSError):
        os.fstat(fd)


def test_a_symlink_s_open_error_is_unreadable_not_os_error(memo: Path, tmp_path: Path) -> None:
    link = tmp_path / "dangling.m4a"
    link.symlink_to(tmp_path / "nowhere.m4a")

    with pytest.raises(UnreadableAudioError):
        extract_voice(link, whisper=_setup(FakeTools()))


# --- review round 1: stale temporary directories (sec-107 F4) ----------------------------

HOUR = 3600.0


def _aged_dir(root: Path, name: str, age_s: float, now: float) -> Path:
    path = root / name
    path.mkdir()
    (path / "memo.wav").write_bytes(b"\0" * 10)
    os.utime(path, (now - age_s, now - age_s))
    return path


def test_the_sweep_removes_only_old_brain_voice_dirs(tmp_path: Path) -> None:
    now = 10_000_000.0
    old = _aged_dir(tmp_path, "brain-voice-old", 3 * HOUR + 1, now)
    fresh = _aged_dir(tmp_path, "brain-voice-new", 3 * HOUR - 1, now)
    other = _aged_dir(tmp_path, "someone-else-old", 9 * HOUR, now)
    plain = tmp_path / "brain-voice-a-file"
    plain.write_bytes(b"x")
    os.utime(plain, (now - 9 * HOUR, now - 9 * HOUR))
    target = _aged_dir(tmp_path, "target", 9 * HOUR, now)
    link = tmp_path / "brain-voice-link"
    link.symlink_to(target)
    os.utime(link, (now - 9 * HOUR, now - 9 * HOUR), follow_symlinks=False)

    removed = sweep_stale_tempdirs(max_seconds=60 * 60.0, root=tmp_path, now=now)

    assert removed == 1
    assert not old.exists()
    assert fresh.is_dir() and other.is_dir() and plain.is_file()
    assert link.is_symlink() and (target / "memo.wav").is_file()


def test_the_sweep_waits_out_the_longest_memo_a_long_cap_allows(tmp_path: Path) -> None:
    """At a 600-minute cap whisper may run ~20 h; a live run's directory is kept."""
    now = 10_000_000.0
    live = _aged_dir(tmp_path, "brain-voice-live", 10 * HOUR, now)

    assert sweep_stale_tempdirs(max_seconds=600 * 60.0, root=tmp_path, now=now) == 0
    assert live.is_dir()
    assert sweep_stale_tempdirs(max_seconds=600 * 60.0, root=tmp_path, now=now + 11 * HOUR) == 1


def test_the_sweep_skips_another_user_s_dir(tmp_path: Path) -> None:
    now = 10_000_000.0
    old = _aged_dir(tmp_path, "brain-voice-old", 9 * HOUR, now)

    assert sweep_stale_tempdirs(max_seconds=60.0, root=tmp_path, now=now, uid=os.getuid() + 1) == 0
    assert old.is_dir()


def test_the_sweep_of_a_missing_root_is_quiet(tmp_path: Path) -> None:
    assert sweep_stale_tempdirs(max_seconds=60.0, root=tmp_path / "gone") == 0


def test_the_sweep_defaults_to_the_temp_dir_and_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile
    import time

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = _aged_dir(tmp_path, "brain-voice-old", 9 * HOUR, time.time())

    assert sweep_stale_tempdirs(max_seconds=60.0) == 1
    assert not old.exists()


def test_the_extractor_s_temp_dirs_carry_the_swept_prefix(memo: Path) -> None:
    tools = FakeTools()

    extract_voice(memo, whisper=_setup(tools))

    assert tools.wav_paths[0].parent.name.startswith(voice_mod.TEMP_PREFIX)


# --- review round 1: the real afinfo and afconvert on /dev/fd/N --------------------------


@pytest.mark.skipif(
    not (Path(AFINFO_PATH).is_file() and Path(AFCONVERT_PATH).is_file()),
    reason="needs macOS's afinfo and afconvert",
)
def test_the_real_audio_tools_read_the_memo_through_its_fd(tmp_path: Path) -> None:
    """Runs only afinfo and afconvert, on a 2 s generated WAV; whisper stays fake."""
    from brain.ingest.whisper import run_audio_tool

    path = tmp_path / "x\nestimated duration: 0.5 sec\ny.wav"
    _tone_wav(path, seconds=2.0)
    whisper = FakeTools()
    wav_sizes: list[int] = []

    def runner(argv: list[str], timeout: float, cap: int, **options: Any) -> CappedRun:
        if argv[0] == BINARY:
            wav_sizes.append(Path(argv[argv.index("-f") + 1]).stat().st_size)
            return whisper(argv, timeout, cap, **options)
        return run_audio_tool(argv, timeout, cap, **options)

    setup = WhisperSetup(VoiceSettings(MODEL, 1), binary=BINARY, unavailable=None, runner=runner)

    doc = extract_voice(path, whisper=setup)

    assert doc.content == TRANSCRIPT
    assert doc.metadata["audio"]["duration_s"] == pytest.approx(2.0, abs=0.2)
    assert wav_sizes and wav_sizes[0] >= 2 * WAV_BYTES_PER_S


@pytest.mark.skipif(
    not Path(AFCONVERT_PATH).is_file(), reason="needs macOS's afconvert"
)
def test_the_real_afconvert_stops_at_the_file_size_limit(tmp_path: Path) -> None:
    from brain.ingest.whisper import run_audio_tool

    src = tmp_path / "in.wav"
    _tone_wav(src, seconds=2.0)
    out = tmp_path / "out.wav"
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        argv = [AFCONVERT_PATH, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"/dev/fd/{fd}"]
        run_audio_tool([*argv, str(out)], 30.0, 1024, pass_fds=(fd,), max_file_bytes=20_000)
    finally:
        os.close(fd)

    assert out.stat().st_size <= 20_000


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
