"""Local transcription for voice memos: finding whisper.cpp and running the audio tools (RM-38).

:func:`resolve_whisper` turns the ``BRAIN_WHISPER_*`` knobs (:mod:`brain.settings.voice`)
into a :class:`WhisperSetup`: ready, or ``unavailable`` with the one-line fix. Voice memos
need four things, checked in this order, and the first one missing is the hint:

1. ``BRAIN_WHISPER_MODEL`` set (there is no default model, and nothing is downloaded);
2. macOS's ``/usr/bin/afinfo`` and ``/usr/bin/afconvert`` (built in);
3. the model file itself, which must also be safe to load (:func:`model_file_problem`);
4. ``whisper-cli`` (Homebrew ``whisper-cpp``) on ``PATH``, found by an absolute path.

**The model is code-adjacent input** (sec-107 F6): whisper loads it into its process, so
a model file someone else can replace is refused. It must be a regular file owned by
this user (or root), with no group or world write bit, and not inside a configured drop
folder (``$BRAIN_HOME/drop-folders.toml``), where files arrive from elsewhere. Whisper
opens it by path, after this check, so **every directory above it** (above the path as
given and above the file it resolves to) must also be owned by this user or root and
have no group or world write bit (sec-107b N1): then nobody else can swap the file in
between. Passing whisper an fd (``-m /dev/fd/N``) was ruled out: whisper.cpp builds with
Core ML derive the encoder's path from the model's (``…/ggml-base.en-encoder.mlmodelc``),
so every memo would fail on such a build. **A
relative ``whisper-cli``** (``shutil.which`` returns one for a relative ``PATH`` entry,
sec-107 F3) is refused too: it would resolve against whatever the working directory is.

**The runner** (:data:`AudioRunner`) runs all three tools, and is the one seam tests
replace, so no test needs a binary. The production :func:`run_audio_tool` goes through
:func:`brain.ingest.capped_process.run_capped`: the binary by absolute path, no stdin,
stderr discarded unread (whisper prints the model's details and progress there),
stdout capped, the whole process group killed at the timeout, and only
:data:`ENV_KEYS` inherited, since the rest of the environment can hold API keys. The
runner also takes ``pass_fds`` (the memo's fd, read as ``/dev/fd/N``) and
``max_file_bytes`` (``afconvert``'s output bound), both forwarded to ``run_capped``.
Nothing leaves the machine.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
HINT_RELATIVE_BINARY = (
    "whisper-cli was found through a relative PATH entry: make every PATH entry absolute"
)
HINT_MODEL_OWNER = "BRAIN_WHISPER_MODEL names a file owned by another user: use your own copy"
HINT_MODEL_WRITABLE = (
    "BRAIN_WHISPER_MODEL names a group- or world-writable file: chmod go-w it"
)
HINT_MODEL_IN_DROP_FOLDER = (
    "BRAIN_WHISPER_MODEL names a file inside a drop folder: keep the model elsewhere"
)
HINT_MODEL_DIR_UNSAFE = (
    "BRAIN_WHISPER_MODEL is in a directory another user can change: "
    "chmod go-w it, or keep the model in a directory of your own"
)


class AudioRunner(Protocol):
    """Runs one audio tool and returns its :class:`CappedRun`; raises only ``OSError``."""

    def __call__(
        self,
        argv: list[str],
        timeout: float,
        max_stdout_bytes: int,
        *,
        pass_fds: tuple[int, ...] = (),
        max_file_bytes: int | None = None,
    ) -> CappedRun: ...


Which = Callable[[str], "str | None"]
IsFile = Callable[[Path], bool]
#: ``model -> hint`` when the model file must not be loaded, else ``None``.
ModelCheck = Callable[[Path], "str | None"]


def minimal_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the audio tools get: :data:`ENV_KEYS` that are set."""
    source = os.environ if environ is None else environ
    return {key: source[key] for key in ENV_KEYS if key in source}


def run_audio_tool(
    argv: list[str],
    timeout: float,
    max_stdout_bytes: int,
    *,
    pass_fds: tuple[int, ...] = (),
    max_file_bytes: int | None = None,
    popen: Popen = subprocess.Popen,
) -> CappedRun:
    """The production :class:`AudioRunner` (module docstring). ``popen`` is a test seam."""
    return run_capped(
        argv,
        timeout=timeout,
        max_stdout_bytes=max_stdout_bytes,
        env=minimal_env(),
        popen=popen,
        pass_fds=pass_fds,
        max_file_bytes=max_file_bytes,
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


def configured_drop_folder_paths() -> list[Path]:
    """The folders in ``$BRAIN_HOME/drop-folders.toml`` (resolved); none if it is broken.

    A broken file is not this check's to report: ``brain ingest-folder`` and
    ``brain doctor`` already name it.
    """
    from brain.config import ConfigError, _brain_home_root
    from brain.settings.drop_folders import load_drop_folders

    try:
        return [folder.path for folder in load_drop_folders(_brain_home_root()).folders]
    except ConfigError:
        return []


def model_file_problem(
    path: Path,
    *,
    uid: int | None = None,
    drop_folders: Callable[[], Sequence[Path]] = configured_drop_folder_paths,
) -> str | None:
    """Why the model file at ``path`` must not be loaded, or ``None`` (module docstring).

    ``uid`` (default: this process's) and ``drop_folders`` are test seams.
    """
    owners = (os.getuid() if uid is None else uid, 0)
    try:
        resolved = path.resolve(strict=True)
        st = os.lstat(resolved)
    except OSError:
        return HINT_MISSING_MODEL
    if not stat.S_ISREG(st.st_mode):
        return HINT_MISSING_MODEL
    if st.st_uid not in owners:
        return HINT_MODEL_OWNER
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return HINT_MODEL_WRITABLE
    if not _dirs_safe([*path.absolute().parents, *resolved.parents], owners):
        return HINT_MODEL_DIR_UNSAFE
    if any(resolved.is_relative_to(folder) for folder in drop_folders()):
        return HINT_MODEL_IN_DROP_FOLDER
    return None


def _dirs_safe(dirs: Sequence[Path], owners: tuple[int, int]) -> bool:
    """Every directory owned by one of ``owners`` with no group or world write bit.

    A sticky world-writable directory (``/tmp``) is refused too: its other users cannot
    replace the model, but they can create the names around it first.
    """
    for directory in dirs:
        try:
            st = os.stat(directory)
        except OSError:
            return False
        if st.st_uid not in owners or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    return True


def resolve_whisper(
    env: Mapping[str, str],
    *,
    which: Which = shutil.which,
    is_file: IsFile = _is_regular_file,
    runner: AudioRunner | None = None,
    model_problem: ModelCheck = model_file_problem,
) -> WhisperSetup:
    """Read the knobs from ``env`` and find the tools (module docstring).

    Raises ``ConfigError`` for a bad knob. ``which``, ``is_file``, ``runner`` and
    ``model_problem`` are seams.
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
    problem = model_problem(settings.model)
    if problem is not None:
        return unavailable(problem)
    binary = which(WHISPER_CLI)
    if binary is None:
        return unavailable(HINT_MISSING_BINARY)
    if not os.path.isabs(binary):
        return unavailable(HINT_RELATIVE_BINARY)
    return WhisperSetup(settings=settings, binary=binary, unavailable=None, runner=chosen)
