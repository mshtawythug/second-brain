"""RM-14b: the opt-in ``com.brain.reranker`` LaunchAgent (``llama-server`` on loopback).

It is installed only by ``brain-install-launchd --with-reranker``. The label is **sticky**: the
flagless install that every ``brain-up`` runs re-renders an installed reranker plist and never
removes it, and only ``brain-uninstall-launchd`` removes it. ``brain doctor``'s ``daemons`` row
checks it only when its plist exists.

Every launchctl here is a recording stub passed explicitly (no real ``launchctl``), as in
``tests/test_bin_launchd_retired.py``. Paths are synthetic.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from brain.bin.launchd import (
    _ALL_LABELS,
    _INSTALLED_LABELS,
    _OPTIONAL_LABELS,
    LaunchdError,
    install_main,
    install_plists,
    render_plist,
    resolve_rerank_model,
    uninstall_plists,
)
from brain.doctor_runtime import daemon_doctor_check
from brain.settings.rerank import DEFAULT_RERANK_GGUF_NAME

_RERANKER = "com.brain.reranker"


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A launchctl stub that records argv; ``print`` says "not loaded"."""
    log = tmp_path / "launchctl.calls"
    stub = tmp_path / "launchctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        "  print) exit 1 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _gguf(tmp_path: Path, name: str = "model.gguf") -> Path:
    model = tmp_path / "models" / name
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"GGUF synthetic")
    return model


def _render(model: Path) -> str:
    return render_plist(
        _RERANKER,
        brain_home=Path("/fake/brain"),
        vault_path=Path("/fake/vault"),
        pipx_bin_dir=Path("/fake/pipx/bin"),
        brain_py=Path("/fake/python"),
        rerank_model=model,
    )


@pytest.fixture
def brain_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "brain"
    home.mkdir()
    monkeypatch.delenv("BRAIN_RERANK_GGUF", raising=False)
    return home


# ---------------------------------------------------------------------------
# labels and render (AC 2)
# ---------------------------------------------------------------------------


def test_the_reranker_is_optional_and_swept() -> None:
    assert _OPTIONAL_LABELS == (_RERANKER,)
    assert _RERANKER not in _INSTALLED_LABELS
    assert _RERANKER in _ALL_LABELS


def test_plist_runs_llama_server_on_loopback_with_the_three_flags() -> None:
    data = plistlib.loads(_render(Path("/fake/models/bge.gguf")).encode())

    assert data["Label"] == _RERANKER
    assert data["ProgramArguments"] == [
        "/usr/bin/env",
        "llama-server",
        "-m",
        "/fake/models/bge.gguf",
        "--embedding",
        "--pooling",
        "rank",
        "--reranking",
        "--host",
        "127.0.0.1",
        "--port",
        "8081",
    ]
    assert data["KeepAlive"] is True
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert data["StandardErrorPath"] == "/fake/brain/logs/com.brain.reranker.err.log"


def test_the_model_path_is_xml_escaped() -> None:
    data = plistlib.loads(_render(Path("/fake/a&b<c>/m.gguf")).encode())

    assert data["ProgramArguments"][3] == "/fake/a&b<c>/m.gguf"


def test_rendering_without_a_model_path_is_refused() -> None:
    with pytest.raises(LaunchdError, match="rerank_model_path"):
        render_plist(
            _RERANKER,
            brain_home=Path("/fake/brain"),
            vault_path=Path("/fake/vault"),
            pipx_bin_dir=Path("/fake/pipx/bin"),
            brain_py=Path("/fake/python"),
        )


# ---------------------------------------------------------------------------
# the model path's precedence
# ---------------------------------------------------------------------------


def test_model_defaults_under_brain_home(brain_home: Path) -> None:
    assert resolve_rerank_model(brain_home) == brain_home / "models" / DEFAULT_RERANK_GGUF_NAME


def test_model_from_brain_home_dotenv(brain_home: Path) -> None:
    (brain_home / ".env").write_text("BRAIN_RERANK_GGUF=/fake/dotenv.gguf\n", encoding="utf-8")

    assert resolve_rerank_model(brain_home) == Path("/fake/dotenv.gguf")


def test_process_env_wins_and_is_expanded(
    brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (brain_home / ".env").write_text("BRAIN_RERANK_GGUF=/fake/dotenv.gguf\n", encoding="utf-8")
    monkeypatch.setenv("BRAIN_RERANK_GGUF", "~/m.gguf")

    assert resolve_rerank_model(brain_home) == Path.home() / "m.gguf"


# ---------------------------------------------------------------------------
# install / uninstall (AC 3, AC 4)
# ---------------------------------------------------------------------------


def test_a_flagless_first_install_does_not_install_it(tmp_path: Path, brain_home: Path) -> None:
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert not (agents / f"{_RERANKER}.plist").exists()
    assert not any(_RERANKER in call for call in _calls(log))


def test_with_reranker_installs_and_bootstraps_it(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _gguf(tmp_path)
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(model))
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )

    plist = plistlib.loads((agents / f"{_RERANKER}.plist").read_bytes())
    assert plist["ProgramArguments"][3] == str(model)
    assert any(c.startswith("bootstrap") and _RERANKER in c for c in _calls(log))


def test_with_reranker_refuses_a_missing_model(tmp_path: Path, brain_home: Path) -> None:
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    with pytest.raises(LaunchdError, match="docs/guides/local-reranker.md"):
        install_plists(
            brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
        )

    assert not (agents / f"{_RERANKER}.plist").exists()
    assert _calls(log) == []


def test_a_later_flagless_install_keeps_and_re_renders_it(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _gguf(tmp_path, "first.gguf"), _gguf(tmp_path, "second.gguf")
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(first))
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )
    log.unlink()

    # What brain-up runs on every start: no flag, and here a changed model path.
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(second))
    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    plist = plistlib.loads((agents / f"{_RERANKER}.plist").read_bytes())
    assert plist["ProgramArguments"][3] == str(second)
    calls = _calls(log)
    assert any(c.startswith("bootout") and _RERANKER in c for c in calls)
    assert any(c.startswith("bootstrap") and _RERANKER in c for c in calls)


def test_the_sticky_re_render_does_not_refuse_a_missing_model(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``brain-up`` must not fail because the model moved; doctor reports the dead daemon."""
    model = _gguf(tmp_path)
    stub, _log = _recorder(tmp_path)
    agents = tmp_path / "agents"
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(model))
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )
    model.unlink()

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert (agents / f"{_RERANKER}.plist").exists()


def test_uninstall_removes_it(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(_gguf(tmp_path)))
    stub, _log = _recorder(tmp_path)
    agents = tmp_path / "agents"
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )

    uninstall_plists(launchd_dir=agents, launchctl=str(stub))

    assert sorted(agents.glob("*.plist")) == []


def test_install_main_parses_with_reranker(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = tmp_path / "agents"
    stub, _log = _recorder(tmp_path)
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(agents))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", str(stub))
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(_gguf(tmp_path)))

    install_main([])
    assert not (agents / f"{_RERANKER}.plist").exists()

    install_main(["--with-reranker"])
    assert (agents / f"{_RERANKER}.plist").exists()


def test_install_main_rejects_an_unknown_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        install_main(["--with-everything"])

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# doctor's daemons row checks it only when its plist exists
# ---------------------------------------------------------------------------


def _list_stub(tmp_path: Path, rows: dict[str, int]) -> Path:
    body = "PID\\tStatus\\tLabel\\n" + "".join(f"1\\t{st}\\t{lb}\\n" for lb, st in rows.items())
    stub = tmp_path / "launchctl-list"
    stub.write_text(f"#!/bin/sh\nprintf '{body}'\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_doctor_ignores_a_reranker_that_was_never_installed(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "com.brain.watcher.plist").write_bytes(plistlib.dumps({"KeepAlive": True}))

    (check,) = daemon_doctor_check(
        launchd_dir=agents,
        launchctl=str(_list_stub(tmp_path, {"com.brain.watcher": 0})),
        platform="darwin",
    )

    assert check.status == "ok"
    assert check.detail.startswith("1 agent(s)")


def test_doctor_checks_an_installed_reranker(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / f"{_RERANKER}.plist").write_bytes(plistlib.dumps({"KeepAlive": True}))

    (check,) = daemon_doctor_check(
        launchd_dir=agents,
        launchctl=str(_list_stub(tmp_path, {"com.brain.watcher": 0})),
        platform="darwin",
    )

    assert check.status == "fail"
    assert f"{_RERANKER} installed but NOT LOADED" in check.detail
