"""Voice-memo extractor: transcribe an ``.m4a``, ``.mp3`` or ``.wav`` on this machine (RM-38).

Registered in :mod:`brain.ingest.dispatch` with ``in_directory_walks=False`` and a
``confidential`` default, so ``brain ingest-dir``'s walk is unchanged and a memo is
confidential on every path unless the caller says otherwise. Memos normally arrive
through the drop-folder runner (:mod:`brain.drop_folder`); the tools are found by
:mod:`brain.ingest.whisper`, and every one runs through its injectable runner.

**The steps, each bounded:**

1. Whisper unavailable (off, not macOS, no model file, no ``whisper-cli``):
   :class:`WhisperUnavailableError` before the file is touched.
2. The memo is opened once, ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``, and checked with
   ``fstat``: a regular file (a symlink or FIFO is refused, unread, and a FIFO never
   blocks), at most :data:`MAX_FILE_BYTES`. Both audio tools then read that fd as
   ``/dev/fd/N`` (``pass_fds``), rewound to its start first since the tools share its
   offset. Swapping the path after the open changes nothing (sec-107 F2), and neither
   tool is ever given the memo's name, which ``afinfo`` would print back raw (sec-107 F1:
   a name holding a newline and a fake ``estimated duration`` line used to set the
   duration).
3. ``afinfo`` (:data:`AFINFO_TIMEOUT_S`) reads the duration; only its
   ``estimated duration: N sec`` line is used, and its output is never logged or
   quoted. Over ``BRAIN_WHISPER_MAX_MINUTES``: :class:`AudioTooLongError`, before
   anything is converted.
4. ``afconvert`` (:data:`AFCONVERT_TIMEOUT_S`) writes a 16 kHz mono 16-bit WAV into a
   private temporary directory (prefix :data:`TEMP_PREFIX`), removed afterwards: the
   audio is never copied into the brain. The header's duration is only a claim, so
   ``afconvert`` runs under an ``RLIMIT_FSIZE`` of :func:`wav_byte_limit` (the cap plus
   :data:`WAV_HEADER_SLACK_S` of audio plus :data:`WAV_HEADER_BYTES`): a lying header
   cannot fill the disk (about 95 MB a second, uncapped). The WAV's size is its true
   length (:data:`WAV_BYTES_PER_S`), checked against the cap before anything else about
   the conversion. Hitting the limit is :class:`AudioTooLongError` both ways it shows: the
   tool killed by SIGXFSZ, or a WAV cut off at the limit with exit 0 (what macOS's
   ``afconvert`` does). The limit is past the cap's longest legitimate WAV (the cap plus
   the slack), so a memo of exactly the cap is never rejected. The WAV's length, not the
   header's, is the stored duration.
5. ``whisper-cli -m MODEL -f WAV -nt -np``, with :func:`whisper_timeout`
   (``2 × duration + 60 s``, on the longer of the two durations). This timeout ignores
   the drop-folder run's own deadline (RM-38.D1): that deadline stops new files from
   starting, never a memo already being transcribed.
6. The transcript: whisper's ``[BLANK_AUDIO]`` marker dropped, segments joined. Silence
   is an empty document, which ingest skips.

**Rejected or transient.** :class:`AudioTooLongError`, :class:`AudioTooLargeError`,
:class:`UnreadableAudioError`, :class:`AudioTimeoutError` and
:class:`TranscriptTooLargeError` would fail again on every run, so the drop folder keeps
the file in ``imported/`` (``rejected``). The two :data:`TRANSIENT_VOICE_ERRORS` point at
the machine, not the memo: whisper unavailable, and :class:`TranscriptionError` (a tool
that cannot start, or :class:`WhisperFailedError`, whisper failing on the WAV this module
just made, RM-38.D4), so the drop folder moves the file back for the next run. A memo
whisper keeps failing on is rejected after three tries by the drop folder's ledger
(:mod:`brain.ingest.voice_failures`, sec-107 F5).

**Interrupted runs.** A ``SystemExit`` (SIGTERM, under
:func:`~brain.ingest.capped_process.sigterm_as_system_exit`) or ``KeyboardInterrupt`` mid-
tool kills the tool's process group, closes the fd and removes the temporary directory
on its way out. A process killed outright (SIGKILL, a power cut) cannot clean up, so
:func:`sweep_stale_tempdirs`, run when a drop-folder run starts, removes this user's
:data:`TEMP_PREFIX` directories older than :data:`STALE_TEMPDIR_S` (sec-107 F4).

**Errors carry no content.** Every class is a :class:`ValueError` with a fixed message; a
cause is kept only as ``__cause__``, printed by class by
:func:`brain.errors.describe_failure`. The WARNING logged when a memo is not transcribed
names the class only.
"""
from __future__ import annotations

import errno
import logging
import math
import os
import re
import shutil
import signal
import stat
import tempfile
import time
from pathlib import Path

from . import ExtractedDoc
from .capped_process import CappedRun
from .whisper import AFCONVERT_PATH, AFINFO_PATH, WhisperSetup, resolve_whisper

logger = logging.getLogger(__name__)

#: ``documents.content_type`` of a transcribed memo.
CONTENT_TYPE = "voice_memo"
ENGINE = "whisper.cpp"
#: A file larger than this is refused unread. A 60-minute 44.1 kHz stereo WAV is ~635 MB.
MAX_FILE_BYTES = 1 << 30
AFINFO_TIMEOUT_S = 10.0
AFCONVERT_TIMEOUT_S = 60.0
#: Whisper's timeout is ``2 × duration`` plus this.
WHISPER_TIMEOUT_SLACK_S = 60.0
#: ``afinfo`` and ``afconvert`` answer in a few lines; more is not an audio file.
TOOL_MAX_STDOUT_BYTES = 64 * 1024
#: An hour of speech is about 60 KB of text; this is far above any capped memo.
WHISPER_MAX_STDOUT_BYTES = 4 * 1024 * 1024
#: 16,000 samples a second × 2 bytes × 1 channel: the converted WAV's byte rate.
WAV_BYTES_PER_S = 32_000
#: The WAV's header (and any padding chunk) counts toward its size; allow this much.
WAV_HEADER_SLACK_S = 1.0
#: Added to ``afconvert``'s file-size limit on top of the slack, so a WAV cut off at the
#: limit is always longer than the cap check allows (``afconvert``'s header is 4 KiB).
WAV_HEADER_BYTES = 4096
WAV_NAME = "memo.wav"
#: The temporary directories' prefix; :func:`sweep_stale_tempdirs` removes old ones.
TEMP_PREFIX = "brain-voice-"
#: A temporary directory this old is a crashed run's (sec-107 F4), unless the cap allows
#: a memo to run longer: :func:`sweep_stale_tempdirs` waits out the longest one.
STALE_TEMPDIR_S = 3 * 60 * 60.0
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
BLANK_AUDIO = "[BLANK_AUDIO]"

_DURATION = re.compile(rb"^estimated duration:\s*(\S+)\s*sec\s*$", re.MULTILINE)


class VoiceError(ValueError):
    """A memo could not be transcribed. The message never carries content."""


class WhisperUnavailableError(VoiceError):
    """Voice memos cannot run here; the message is the fix. Transient: configuration."""


class TranscriptionError(VoiceError):
    """A tool could not start, or whisper failed on the converted WAV. Transient (D4)."""


class WhisperFailedError(TranscriptionError):
    """Whisper exited non-zero on the converted WAV. Transient, and counted (sec-107 F5)."""


class TranscriptTooLargeError(VoiceError):
    """Whisper printed more than :data:`WHISPER_MAX_STDOUT_BYTES`; it would every run."""


class UnreadableAudioError(VoiceError):
    """Not a regular file, or ``afinfo``/``afconvert`` cannot read it as audio."""


class AudioTooLargeError(VoiceError):
    """The file is over :data:`MAX_FILE_BYTES`."""


class AudioTooLongError(VoiceError):
    """The memo is longer than ``BRAIN_WHISPER_MAX_MINUTES``."""


class AudioTimeoutError(VoiceError):
    """``afinfo``, ``afconvert`` or whisper ran past its timeout."""


#: The failures that point at the machine, not the memo: the drop folder retries them.
TRANSIENT_VOICE_ERRORS: tuple[type[VoiceError], ...] = (
    WhisperUnavailableError,
    TranscriptionError,
)


def whisper_timeout(duration_s: float) -> float:
    """Whisper's timeout for a memo of ``duration_s`` seconds: ``2 × duration + 60 s``."""
    return 2 * duration_s + WHISPER_TIMEOUT_SLACK_S


def wav_byte_limit(max_seconds: float) -> int:
    """``afconvert``'s file-size limit: the cap plus :data:`WAV_HEADER_SLACK_S`, in WAV
    bytes, plus :data:`WAV_HEADER_BYTES`."""
    return int((max_seconds + WAV_HEADER_SLACK_S) * WAV_BYTES_PER_S) + WAV_HEADER_BYTES


def sweep_stale_tempdirs(
    *,
    max_seconds: float,
    root: Path | None = None,
    now: float | None = None,
    uid: int | None = None,
) -> int:
    """Remove this user's :data:`TEMP_PREFIX` directories a crashed run left; the count.

    A directory is stale once it is older than :data:`STALE_TEMPDIR_S`, or than the
    longest a memo under ``max_seconds`` can hold it, whichever is longer, so a run still
    transcribing is never swept. Only real directories (``lstat``) owned by ``uid``
    (default: this process's) are removed. ``root`` (default: the temp directory),
    ``now`` and ``uid`` are test seams.
    """
    base = Path(tempfile.gettempdir()) if root is None else root
    longest = AFINFO_TIMEOUT_S + AFCONVERT_TIMEOUT_S + whisper_timeout(max_seconds)
    cutoff = (time.time() if now is None else now) - max(STALE_TEMPDIR_S, longest)
    owner = os.getuid() if uid is None else uid
    try:
        entries = [e for e in os.scandir(base) if e.name.startswith(TEMP_PREFIX)]
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == owner and st.st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed


def extract_voice(
    path: Path,
    *,
    whisper: WhisperSetup | None = None,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> ExtractedDoc:
    """Transcribe one memo into an :class:`ExtractedDoc` (``content_type="voice_memo"``).

    ``whisper`` is resolved from ``os.environ`` when not given (the extractor registry
    passes only a path). Raises the :class:`VoiceError` subclasses the module docstring
    lists, and ``OSError`` when the file cannot be opened (a symlink is
    :class:`UnreadableAudioError`). ``max_file_bytes`` is a seam; the runner is
    ``whisper.runner``.
    """
    setup = whisper if whisper is not None else resolve_whisper(os.environ)
    if not setup.ready or setup.binary is None or setup.settings.model is None:
        raise WhisperUnavailableError(setup.unavailable or "voice memos are unavailable")
    source = Path(path).absolute()
    try:
        duration, text = _transcribe(source, setup, max_file_bytes)
    except VoiceError as exc:
        logger.warning("voice memo not transcribed: %s", type(exc).__name__)
        raise
    return ExtractedDoc(
        title=source.stem,
        content=text,
        content_type=CONTENT_TYPE,
        source_path=str(source.resolve()),
        metadata={
            "audio": {"duration_s": round(duration, 1), "format": source.suffix[1:].lower()},
            "transcription": {"engine": ENGINE, "model": setup.settings.model.name},
        },
    )


def _transcribe(source: Path, setup: WhisperSetup, max_file_bytes: int) -> tuple[float, str]:
    """Steps 2 to 6 of the module docstring; returns the WAV's duration and the transcript."""
    try:
        fd = os.open(source, _OPEN_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            raise UnreadableAudioError("not a regular file") from exc
        raise
    try:
        return _transcribe_fd(fd, setup, max_file_bytes)
    finally:
        os.close(fd)


def _transcribe_fd(fd: int, setup: WhisperSetup, max_file_bytes: int) -> tuple[float, str]:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise UnreadableAudioError("not a regular file")
    if st.st_size > max_file_bytes:
        raise AudioTooLargeError("audio file too large")
    memo = f"/dev/fd/{fd}"
    max_seconds = setup.settings.max_seconds
    duration = _duration(_run(setup, [AFINFO_PATH, memo], AFINFO_TIMEOUT_S, fd=fd))
    if duration > max_seconds:
        raise AudioTooLongError("memo longer than BRAIN_WHISPER_MAX_MINUTES")
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as tmp:
        wav = Path(tmp) / WAV_NAME
        argv = [AFCONVERT_PATH, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", memo, str(wav)]
        limit = wav_byte_limit(max_seconds)
        run = _run(setup, argv, AFCONVERT_TIMEOUT_S, fd=fd, max_file_bytes=limit)
        wav_seconds = wav.stat().st_size / WAV_BYTES_PER_S if wav.is_file() else None
        # First: the limit stops afconvert with SIGXFSZ, or (as macOS's afconvert does) a
        # quiet exit 0 and a WAV cut off at the limit. Either is the cap, never unreadable.
        too_long = wav_seconds is not None and wav_seconds > max_seconds + WAV_HEADER_SLACK_S
        if run.returncode == -signal.SIGXFSZ or too_long:
            raise AudioTooLongError("memo longer than BRAIN_WHISPER_MAX_MINUTES")
        if run.returncode != 0 or run.oversized or wav_seconds is None:
            raise UnreadableAudioError("afconvert could not read the memo as audio")
        timeout = whisper_timeout(max(duration, wav_seconds))
        whisper_argv = [setup.binary or "", "-m", str(setup.settings.model), "-f", str(wav)]
        out = _run(setup, [*whisper_argv, "-nt", "-np"], timeout, WHISPER_MAX_STDOUT_BYTES)
    if out.oversized:
        raise TranscriptTooLargeError("whisper-cli printed more than any memo's transcript")
    if out.returncode != 0:
        raise WhisperFailedError("whisper-cli failed on the converted memo")
    return wav_seconds, _clean(out.stdout)


def _run(
    setup: WhisperSetup,
    argv: list[str],
    timeout: float,
    max_stdout_bytes: int = TOOL_MAX_STDOUT_BYTES,
    *,
    fd: int | None = None,
    max_file_bytes: int | None = None,
) -> CappedRun:
    """One tool call; a start failure is transient, a timeout is not.

    ``fd``, when given, is rewound and handed to the tool: ``/dev/fd/N`` shares its offset,
    and the previous tool left it wherever it stopped reading.
    """
    pass_fds: tuple[int, ...] = ()
    if fd is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        pass_fds = (fd,)
    try:
        run = setup.runner(
            argv, timeout, max_stdout_bytes, pass_fds=pass_fds, max_file_bytes=max_file_bytes
        )
    except OSError as exc:
        raise TranscriptionError("an audio tool could not be started") from exc
    if run.timed_out:
        raise AudioTimeoutError(f"an audio tool ran past {timeout:g}s")
    return run


def _duration(run: CappedRun) -> float:
    """The duration ``afinfo`` printed, in seconds; :class:`UnreadableAudioError` if none."""
    match = _DURATION.search(run.stdout) if run.returncode == 0 and not run.oversized else None
    try:
        duration = float(match.group(1)) if match else math.nan
    except ValueError:
        duration = math.nan
    if not (math.isfinite(duration) and duration > 0):
        raise UnreadableAudioError("afinfo could not read the memo as audio")
    return duration


def _clean(stdout: bytes) -> str:
    """Whisper's segments on one line each, without its non-speech marker, joined."""
    text = stdout.decode("utf-8", errors="replace").replace(BLANK_AUDIO, "")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())
