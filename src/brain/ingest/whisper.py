"""Local transcription for voice memos: finding whisper.cpp and running the audio tools (RM-38).

:func:`resolve_whisper` turns the ``BRAIN_WHISPER_*`` knobs (:mod:`brain.settings.voice`)
into a :class:`WhisperSetup`: ready, or ``unavailable`` with the one-line fix. Voice memos
need four things, checked in this order, and the first one missing is the hint:

1. ``BRAIN_WHISPER_MODEL`` set (there is no default model, and nothing is downloaded);
2. macOS's ``/usr/bin/afinfo`` and ``/usr/bin/afconvert`` (built in);
3. the model file itself;
4. ``whisper-cli`` (Homebrew ``whisper-cpp``) on ``PATH``.

**The runner** (:data:`AudioRunner`) runs all three tools, and is the one seam tests
replace, so no test needs a binary. The production :func:`run_audio_tool` goes through
:func:`brain.ingest.capped_process.run_capped`: the binary by absolute path, no stdin,
stderr discarded unread (whisper prints the model's details and progress there),
stdout capped, the whole process group killed at the timeout, and only
:data:`ENV_KEYS` inherited, since the rest of the environment can hold API keys.
Nothing leaves the machine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from brain.settings.voice import VoiceSettings, load_voice_settings

from .capped_process import CappedRun, Popen, run_capped

WHISPER_CLI = "whisper-cli"
AFINFO_PATH = "/usr/bin/afinfo"
AFCONVERT_PATH = "/usr/bin/afconvert"
#: The only variables the audio tools inherit.
ENV_KEYS = ("PATH", "HOME", "LANG", "TMPDIR")

GUIDE = "docs/guides/drop-folders.md"
HINT_OFF = "voice memos are off: set BRAIN_WHISPER_MODEL to a whisper.cpp model file"
HINT_NOT_MACOS = "voice memos need macOS's /usr/bin/afinfo and /usr/bin/afconvert"
HINT_MISSING_MODEL = f"BRAIN_WHISPER_MODEL names no model file: download one (see {GUIDE})"
HINT_MISSING_BINARY = "whisper-cli is not on PATH: brew install whisper-cpp"

#: ``(argv, timeout, max_stdout_bytes) -> CappedRun``; raises only ``OSError``.
AudioRunner = Callable[[list[str], float, int], CappedRun]
Which = Callable[[str], "str | None"]
IsFile = Callable[[Path], bool]


def minimal_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the audio tools get: :data:`ENV_KEYS` that are set."""
    source = os.environ if environ is None else environ
    return {key: source[key] for key in ENV_KEYS if key in source}


def run_audio_tool(
    argv: list[str],
    timeout: float,
    max_stdout_bytes: int,
    *,
    popen: Popen = subprocess.Popen,
) -> CappedRun:
    """The production :data:`AudioRunner` (module docstring). ``popen`` is a test seam."""
    return run_capped(
        argv,
        timeout=timeout,
        max_stdout_bytes=max_stdout_bytes,
        env=minimal_env(),
        popen=popen,
    )


@dataclass(frozen=True)
class WhisperSetup:
    """The resolved knobs, ``whisper-cli``'s path, and why voice memos cannot run (if so)."""

    settings: VoiceSettings
    binary: str | None
    unavailable: str | None
    runner: AudioRunner = run_audio_tool

    @property
    def ready(self) -> bool:
        return self.unavailable is None


def _is_regular_file(path: Path) -> bool:
    return path.is_file()


def resolve_whisper(
    env: Mapping[str, str],
    *,
    which: Which = shutil.which,
    is_file: IsFile = _is_regular_file,
    runner: AudioRunner | None = None,
) -> WhisperSetup:
    """Read the knobs from ``env`` and find the tools (module docstring).

    Raises ``ConfigError`` for a bad knob. ``which``, ``is_file`` and ``runner`` are seams.
    """
    settings = load_voice_settings(env)
    chosen = runner if runner is not None else run_audio_tool

    def unavailable(hint: str) -> WhisperSetup:
        return WhisperSetup(settings=settings, binary=None, unavailable=hint, runner=chosen)

    if settings.model is None:
        return unavailable(HINT_OFF)
    if not (is_file(Path(AFINFO_PATH)) and is_file(Path(AFCONVERT_PATH))):
        return unavailable(HINT_NOT_MACOS)
    if not is_file(settings.model):
        return unavailable(HINT_MISSING_MODEL)
    binary = which(WHISPER_CLI)
    if binary is None:
        return unavailable(HINT_MISSING_BINARY)
    return WhisperSetup(settings=settings, binary=binary, unavailable=None, runner=chosen)
