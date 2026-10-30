"""Tests for :mod:`brain.ingest.whisper`: resolving whisper.cpp and running the audio tools (RM-38).

No test runs ``afinfo``, ``afconvert`` or ``whisper-cli``: ``which`` and ``is_file`` are
injected, and the runner is driven through a fake ``popen``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from brain.config import ConfigError
from brain.ingest.capped_process import CappedRun
from brain.ingest.whisper import (
    AFCONVERT_PATH,
    AFINFO_PATH,
    ENV_KEYS,
    HINT_MISSING_BINARY,
    HINT_MISSING_MODEL,
    HINT_NOT_MACOS,
    HINT_OFF,
    WHISPER_CLI,
    WhisperSetup,
    minimal_env,
    resolve_whisper,
    run_audio_tool,
)
from brain.settings.voice import VoiceSettings

MODEL = "/models/ggml-base.en.bin"
BINARY = "/opt/homebrew/bin/whisper-cli"
READY_ENV = {"BRAIN_WHISPER_MODEL": MODEL}


def _which(found: str | None) -> Any:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return found

    which.seen = seen  # type: ignore[attr-defined]
    return which


def _files(*present: str) -> Any:
    return lambda path: str(path) in present


ALL_FILES = _files(MODEL, AFINFO_PATH, AFCONVERT_PATH)


def test_off_when_no_model_is_configured() -> None:
    setup = resolve_whisper({}, which=_which(BINARY), is_file=ALL_FILES)

    assert setup.unavailable == HINT_OFF
    assert setup.ready is False
    assert setup.binary is None


def test_not_macos_when_afinfo_or_afconvert_is_missing() -> None:
    for present in ((MODEL, AFINFO_PATH), (MODEL, AFCONVERT_PATH)):
        setup = resolve_whisper(READY_ENV, which=_which(BINARY), is_file=_files(*present))
        assert setup.unavailable == HINT_NOT_MACOS


def test_model_missing_when_the_file_is_not_there() -> None:
    setup = resolve_whisper(
        READY_ENV, which=_which(BINARY), is_file=_files(AFINFO_PATH, AFCONVERT_PATH)
    )

    assert setup.unavailable == HINT_MISSING_MODEL


def test_binary_missing_when_whisper_cli_is_not_on_path() -> None:
    which = _which(None)
    setup = resolve_whisper(READY_ENV, which=which, is_file=ALL_FILES)

    assert setup.unavailable == HINT_MISSING_BINARY
    assert which.seen == [WHISPER_CLI]


def test_ready_when_everything_is_present() -> None:
    setup = resolve_whisper(READY_ENV, which=_which(BINARY), is_file=ALL_FILES)

    assert setup.ready is True
    assert setup.unavailable is None
    assert setup.binary == BINARY
    assert setup.settings == VoiceSettings(model=Path(MODEL), max_minutes=60)
    assert setup.runner is run_audio_tool


def test_a_given_runner_is_kept() -> None:
    def runner(argv: list[str], timeout: float, max_stdout_bytes: int) -> CappedRun:
        raise AssertionError("not called")

    setup = resolve_whisper(READY_ENV, which=_which(BINARY), is_file=ALL_FILES, runner=runner)

    assert setup.runner is runner


def test_a_bad_knob_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="BRAIN_WHISPER_MAX_MINUTES"):
        resolve_whisper({"BRAIN_WHISPER_MAX_MINUTES": "0"}, which=_which(BINARY))


def test_hints_name_the_fix() -> None:
    assert "BRAIN_WHISPER_MODEL" in HINT_OFF
    assert "brew install whisper-cpp" in HINT_MISSING_BINARY
    assert "BRAIN_WHISPER_MODEL" in HINT_MISSING_MODEL
    assert "afinfo" in HINT_NOT_MACOS


def test_the_default_is_file_checks_a_regular_file(tmp_path: Path) -> None:
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"x")
    env = {"BRAIN_WHISPER_MODEL": str(tmp_path / "ggml.bin")}

    setup = resolve_whisper(env, which=_which(BINARY))
    missing = resolve_whisper({"BRAIN_WHISPER_MODEL": str(tmp_path)}, which=_which(BINARY))

    # On macOS the tools exist; elsewhere the hint is the platform one. Either way a
    # directory is never a model.
    assert setup.unavailable in (None, HINT_NOT_MACOS)
    assert missing.unavailable in (HINT_MISSING_MODEL, HINT_NOT_MACOS)


def test_setup_is_frozen() -> None:
    setup = WhisperSetup(settings=VoiceSettings(None, 60), binary=None, unavailable=HINT_OFF)
    with pytest.raises(AttributeError):
        setup.binary = BINARY  # type: ignore[misc]


# --- the runner ----------------------------------------------------------------------


def test_minimal_env_keeps_only_the_listed_keys() -> None:
    environ = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "TMPDIR": "/t", "VOYAGE_API_KEY": "k"}

    assert minimal_env(environ) == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "TMPDIR": "/t"}
    assert ENV_KEYS == ("PATH", "HOME", "LANG", "TMPDIR")


def test_minimal_env_skips_unset_keys() -> None:
    assert minimal_env({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_minimal_env_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/only/this")
    monkeypatch.setenv("BRAIN_WHISPER_PROBE_SECRET", "nope")

    env = minimal_env()

    assert env["PATH"] == "/only/this"
    assert "BRAIN_WHISPER_PROBE_SECRET" not in env


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int) -> None:
        self.pid = 999_999_999
        self.stdout = _Reader(stdout)
        self._returncode = returncode

    def __enter__(self) -> _FakeProc:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def wait(self) -> int:
        return self._returncode


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def test_run_audio_tool_runs_capped_with_the_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "must-not-reach-the-child")
    calls: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"argv": argv, **kwargs})
        return _FakeProc(b"estimated duration: 2.0 sec\n", 0)

    run = run_audio_tool([AFINFO_PATH, "/x.m4a"], 10.0, 1024, popen=popen)

    assert run == CappedRun(b"estimated duration: 2.0 sec\n", 0, False, False)
    (call,) = calls
    assert call["argv"] == [AFINFO_PATH, "/x.m4a"]
    assert call["stdin"] is subprocess.DEVNULL
    assert call["stderr"] is subprocess.DEVNULL
    assert call["start_new_session"] is True
    assert "VOYAGE_API_KEY" not in call["env"]
    assert set(call["env"]) <= set(ENV_KEYS)


def test_run_audio_tool_reports_an_oversized_answer() -> None:
    def popen(argv: list[str], **kwargs: Any) -> _FakeProc:
        return _FakeProc(b"x" * 100, 0)

    run = run_audio_tool([AFINFO_PATH], 10.0, 10, popen=popen)

    assert run.oversized is True
    assert run.stdout == b""
