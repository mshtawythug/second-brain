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
    HINT_MODEL_DIR_UNSAFE,
    HINT_MODEL_IN_DROP_FOLDER,
    HINT_MODEL_OWNER,
    HINT_MODEL_WRITABLE,
    HINT_NOT_MACOS,
    HINT_OFF,
    HINT_RELATIVE_BINARY,
    WHISPER_CLI,
    WhisperSetup,
    configured_drop_folder_paths,
    minimal_env,
    model_file_problem,
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


def _model_ok(path: Path) -> str | None:
    return None


def test_off_when_no_model_is_configured() -> None:
    setup = resolve_whisper({}, which=_which(BINARY), is_file=ALL_FILES, model_problem=_model_ok)

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
    setup = resolve_whisper(READY_ENV, which=which, is_file=ALL_FILES, model_problem=_model_ok)

    assert setup.unavailable == HINT_MISSING_BINARY
    assert which.seen == [WHISPER_CLI]


def test_ready_when_everything_is_present() -> None:
    setup = resolve_whisper(
        READY_ENV, which=_which(BINARY), is_file=ALL_FILES, model_problem=_model_ok
    )

    assert setup.ready is True
    assert setup.unavailable is None
    assert setup.binary == BINARY
    assert setup.settings == VoiceSettings(model=Path(MODEL), max_minutes=60)
    assert setup.runner is run_audio_tool


def test_a_given_runner_is_kept() -> None:
    def runner(argv: list[str], timeout: float, max_stdout_bytes: int) -> CappedRun:
        raise AssertionError("not called")

    setup = resolve_whisper(
        READY_ENV, which=_which(BINARY), is_file=ALL_FILES, runner=runner, model_problem=_model_ok
    )

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


# --- review round 1 (PR #107): the binary's path and the model file ---------------------


def test_a_relative_whisper_cli_is_refused() -> None:
    """sec-107 F3: a relative PATH entry resolves against whatever the cwd is."""
    setup = resolve_whisper(
        READY_ENV, which=_which("bin/whisper-cli"), is_file=ALL_FILES, model_problem=_model_ok
    )

    assert setup.unavailable == HINT_RELATIVE_BINARY
    assert setup.binary is None


def test_a_model_problem_makes_whisper_unavailable() -> None:
    setup = resolve_whisper(
        READY_ENV,
        which=_which(BINARY),
        is_file=ALL_FILES,
        model_problem=lambda path: HINT_MODEL_WRITABLE if path == Path(MODEL) else None,
    )

    assert setup.unavailable == HINT_MODEL_WRITABLE


def _model(tmp_path: Path, mode: int = 0o644) -> Path:
    path = tmp_path / "models" / "ggml-base.en.bin"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic model")
    path.chmod(mode)
    return path


def _no_folders() -> list[Path]:
    return []


def test_a_private_model_file_has_no_problem(tmp_path: Path) -> None:
    assert model_file_problem(_model(tmp_path), drop_folders=_no_folders) is None


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_group_or_world_writable_model_is_refused(tmp_path: Path, mode: int) -> None:
    """sec-107 F6: anyone who can write the model can change what whisper runs."""
    problem = model_file_problem(_model(tmp_path, mode), drop_folders=_no_folders)

    assert problem == HINT_MODEL_WRITABLE


def test_a_model_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    import os

    model = _model(tmp_path)

    assert (
        model_file_problem(model, uid=os.getuid() + 1, drop_folders=_no_folders)
        == HINT_MODEL_OWNER
    )


def test_a_root_owned_model_is_accepted(tmp_path: Path) -> None:
    """``/usr/local`` installs are root's; only root could change them."""
    model = Path("/bin/sh").resolve()  # root-owned on macOS and Linux

    assert model_file_problem(model, drop_folders=_no_folders) is None


def test_a_model_inside_a_drop_folder_is_refused(tmp_path: Path) -> None:
    model = _model(tmp_path)

    problem = model_file_problem(model, drop_folders=lambda: [tmp_path.resolve()])

    assert problem == HINT_MODEL_IN_DROP_FOLDER


def test_a_model_beside_a_drop_folder_is_accepted(tmp_path: Path) -> None:
    model = _model(tmp_path)
    folder = tmp_path / "models-drop"
    folder.mkdir()

    assert model_file_problem(model, drop_folders=lambda: [folder.resolve()]) is None


def test_a_missing_or_non_regular_model_is_missing(tmp_path: Path) -> None:
    assert model_file_problem(tmp_path / "gone.bin", drop_folders=_no_folders) == (
        HINT_MISSING_MODEL
    )
    assert model_file_problem(tmp_path, drop_folders=_no_folders) == HINT_MISSING_MODEL


def test_the_drop_folders_come_from_brain_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "drop"
    folder.mkdir()
    (home / "drop-folders.toml").write_text(f'[[folder]]\npath = "{folder}"\n')
    monkeypatch.setenv("BRAIN_HOME", str(home))

    assert configured_drop_folder_paths() == [folder.resolve()]


def test_a_broken_drop_folder_file_checks_no_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drop-folder run reports the broken file itself; the model check moves on."""
    (tmp_path / "drop-folders.toml").write_text("[[folder]]\nkind = 3\n")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))

    assert configured_drop_folder_paths() == []


def test_the_default_model_check_reads_the_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    model = _model(tmp_path, 0o666)
    env = {"BRAIN_WHISPER_MODEL": str(model)}

    setup = resolve_whisper(env, which=_which(BINARY), is_file=lambda p: True)

    assert setup.unavailable == HINT_MODEL_WRITABLE


def test_hints_for_the_model_never_name_its_path() -> None:
    for hint in (HINT_MODEL_OWNER, HINT_MODEL_WRITABLE, HINT_MODEL_IN_DROP_FOLDER):
        assert "BRAIN_WHISPER_MODEL" in hint


def test_run_audio_tool_forwards_the_fd_and_the_file_size_limit() -> None:
    calls: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProc:
        calls.append(kwargs)
        return _FakeProc(b"", 0)

    run_audio_tool([AFCONVERT_PATH], 10.0, 10, popen=popen, pass_fds=(7,), max_file_bytes=99)

    (call,) = calls
    assert call["pass_fds"] == (7,)
    assert callable(call["preexec_fn"])


# --- review round 2 (PR #107): the directories above the model (sec-107b N1) ----------


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
def test_a_model_in_a_directory_others_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    """Whisper opens the model by path later; whoever can write its directory can swap it."""
    model = _model(tmp_path)
    model.parent.chmod(mode)
    try:
        problem = model_file_problem(model, drop_folders=_no_folders)
    finally:
        model.parent.chmod(0o755)

    assert problem == HINT_MODEL_DIR_UNSAFE


def test_a_model_below_a_directory_others_can_write_is_refused(tmp_path: Path) -> None:
    model = tmp_path / "shared" / "private" / "ggml.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"synthetic model")
    model.parent.parent.chmod(0o777)
    try:
        problem = model_file_problem(model, drop_folders=_no_folders)
    finally:
        model.parent.parent.chmod(0o755)

    assert problem == HINT_MODEL_DIR_UNSAFE


def test_a_model_directory_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    """A root-owned file reached through a directory the checking uid does not own."""
    import os

    root_owned = Path("/bin/sh").resolve()  # root-owned on macOS and Linux
    link = tmp_path / "ggml.bin"
    link.symlink_to(root_owned)

    problem = model_file_problem(link, uid=os.getuid() + 1, drop_folders=_no_folders)

    assert problem == HINT_MODEL_DIR_UNSAFE


def test_the_directories_of_a_symlink_s_target_are_checked_too(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere" / "ggml.bin"
    real.parent.mkdir()
    real.write_bytes(b"synthetic model")
    real.chmod(0o644)
    link_dir = tmp_path / "models"
    link_dir.mkdir()
    link = link_dir / "ggml.bin"
    link.symlink_to(real)
    real.parent.chmod(0o777)
    try:
        problem = model_file_problem(link, drop_folders=_no_folders)
    finally:
        real.parent.chmod(0o755)

    assert problem == HINT_MODEL_DIR_UNSAFE


def test_the_directories_of_the_link_itself_are_checked_too(tmp_path: Path) -> None:
    real = _model(tmp_path)
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "ggml.bin").symlink_to(real)
    link_dir.chmod(0o777)
    try:
        problem = model_file_problem(link_dir / "ggml.bin", drop_folders=_no_folders)
    finally:
        link_dir.chmod(0o755)

    assert problem == HINT_MODEL_DIR_UNSAFE


def test_a_model_in_a_safe_tree_through_a_symlink_is_accepted(tmp_path: Path) -> None:
    real = _model(tmp_path)
    (tmp_path / "link.bin").symlink_to(real)

    assert model_file_problem(tmp_path / "link.bin", drop_folders=_no_folders) is None


def test_an_unreadable_directory_above_the_model_is_unsafe(tmp_path: Path) -> None:
    """A directory that cannot be stat-ed cannot be shown safe."""
    from brain.ingest.whisper import _dirs_safe

    assert _dirs_safe([tmp_path / "gone"], (0, 0)) is False


def test_a_sticky_shared_directory_above_the_model_is_accepted(tmp_path: Path) -> None:
    """Like ``/tmp``: others write there, but cannot rename or remove this user's entries."""
    model = tmp_path / "sticky" / "mine" / "ggml.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"synthetic model")
    model.parent.parent.chmod(0o1777)
    try:
        problem = model_file_problem(model, drop_folders=_no_folders)
    finally:
        model.parent.parent.chmod(0o755)

    assert problem is None


def test_a_fifo_named_as_the_model_is_missing_without_blocking(tmp_path: Path) -> None:
    import os

    fifo = tmp_path / "ggml.bin"
    os.mkfifo(fifo)

    assert model_file_problem(fifo, drop_folders=_no_folders) == HINT_MISSING_MODEL
