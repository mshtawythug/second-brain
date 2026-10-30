"""`brain doctor`'s ``voice memos`` row (RM-38).

``which`` and ``is_file`` are stubs, so neither ``whisper-cli`` nor the model is looked up
on this machine, and no tool is ever run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from brain.doctor_voice import voice_doctor_check
from brain.ingest.whisper import AFCONVERT_PATH, AFINFO_PATH

BINARY = "/fake/bin/whisper-cli"


def _check(
    env: dict[str, str], *, files: set[str] | None = None, binary: str | None = BINARY,
    model_bytes: int = 147_964_211,
) -> Any:
    present = files if files is not None else {AFINFO_PATH, AFCONVERT_PATH, "/m/ggml-base.en.bin"}
    (check,) = voice_doctor_check(
        env=env,
        which=lambda _: binary,
        is_file=lambda p: str(p) in present,
        file_size=lambda p: model_bytes,
    )
    return check


def test_off_is_ok_and_says_how_to_turn_it_on() -> None:
    check = _check({})

    assert check.check == "voice memos"
    assert check.status == "ok"
    assert "off" in check.detail and "BRAIN_WHISPER_MODEL" in check.detail


def test_ready_is_ok_naming_the_binary_the_model_and_the_cap() -> None:
    env = {"BRAIN_WHISPER_MODEL": "/m/ggml-base.en.bin", "BRAIN_WHISPER_MAX_MINUTES": "30"}
    check = _check(env)

    assert check.status == "ok"
    assert check.detail == (
        f"whisper-cli at {BINARY}, model ggml-base.en.bin (148 MB), memos up to 30 min"
    )


def test_a_missing_binary_warns_with_the_install() -> None:
    check = _check({"BRAIN_WHISPER_MODEL": "/m/ggml-base.en.bin"}, binary=None)

    assert check.status == "warn"
    assert "brew install whisper-cpp" in check.detail


def test_a_missing_model_warns_without_naming_its_path() -> None:
    check = _check(
        {"BRAIN_WHISPER_MODEL": "/secret-dir/ggml-base.en.bin"},
        files={AFINFO_PATH, AFCONVERT_PATH},
    )

    assert check.status == "warn"
    assert "names no model file" in check.detail
    assert "secret-dir" not in check.detail


def test_not_macos_warns_when_a_model_is_set() -> None:
    check = _check({"BRAIN_WHISPER_MODEL": "/m/ggml-base.en.bin"}, files={"/m/ggml-base.en.bin"})

    assert check.status == "warn"
    assert "afinfo" in check.detail


def test_a_bad_knob_warns_naming_the_variable_only() -> None:
    check = _check({"BRAIN_WHISPER_MAX_MINUTES": "zz-not-a-number"})

    assert check.status == "warn"
    assert "BRAIN_WHISPER_MAX_MINUTES" in check.detail
    assert "zz-not-a-number" not in check.detail
    assert check.remedy == "fix it in $BRAIN_HOME/.env"


def test_the_default_env_is_the_process_environment(monkeypatch: Any) -> None:
    monkeypatch.delenv("BRAIN_WHISPER_MODEL", raising=False)

    (check,) = voice_doctor_check()

    assert check.status == "ok"
    assert "off" in check.detail


def test_the_default_file_size_reads_the_model(tmp_path: Path) -> None:
    from brain.doctor_voice import _file_size

    model = tmp_path / "m.bin"
    model.write_bytes(b"x" * 2_500_000)

    assert _file_size(model) == 2_500_000
