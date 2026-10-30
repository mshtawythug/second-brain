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
import re
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
    resolve_rerank_port,
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


def _render(model: Path, port: int = 8081) -> str:
    return render_plist(
        _RERANKER,
        brain_home=Path("/fake/brain"),
        vault_path=Path("/fake/vault"),
        pipx_bin_dir=Path("/fake/pipx/bin"),
        brain_py=Path("/fake/python"),
        rerank_model=model,
        rerank_port=port,
    )


@pytest.fixture
def brain_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "brain"
    home.mkdir()
    monkeypatch.delenv("BRAIN_RERANK_GGUF", raising=False)
    monkeypatch.delenv("BRAIN_RERANK_URL", raising=False)
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
            rerank_port=8081,
        )


def test_rendering_without_a_port_is_refused() -> None:
    with pytest.raises(LaunchdError, match="rerank_port"):
        render_plist(
            _RERANKER,
            brain_home=Path("/fake/brain"),
            vault_path=Path("/fake/vault"),
            pipx_bin_dir=Path("/fake/pipx/bin"),
            brain_py=Path("/fake/python"),
            rerank_model=Path("/fake/m.gguf"),
        )


# ---------------------------------------------------------------------------
# the port (a deviation from the ticket, ruled 2026-10-30: 8081 was taken by
# LanguageTool on the owner's machine). The host stays a literal 127.0.0.1.
# ---------------------------------------------------------------------------


def test_the_port_is_rendered_and_the_host_stays_loopback() -> None:
    args = plistlib.loads(_render(Path("/fake/m.gguf"), port=8091).encode())["ProgramArguments"]

    assert args[-4:] == ["--host", "127.0.0.1", "--port", "8091"]


def test_the_host_is_never_a_template_variable() -> None:
    from importlib.resources import files

    text = (files("brain.templates.launchd") / f"{_RERANKER}.plist.j2").read_text()

    assert "<string>127.0.0.1</string>" in text
    assert "{{ rerank_port }}" in text
    assert set(re.findall(r"\{\{ *(\w+) *\}\}", text)) == {
        "brain_home", "user_home", "rerank_model_path", "rerank_port"
    }


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, 70000])
def test_render_refuses_a_port_out_of_range(port: int) -> None:
    with pytest.raises(LaunchdError, match="1024"):
        _render(Path("/fake/m.gguf"), port=port)


def test_port_defaults_to_8081(brain_home: Path) -> None:
    assert resolve_rerank_port(brain_home) == 8081


def test_port_from_brain_home_dotenv(brain_home: Path) -> None:
    (brain_home / ".env").write_text(
        "BRAIN_RERANK_URL=http://127.0.0.1:8091\n", encoding="utf-8"
    )

    assert resolve_rerank_port(brain_home) == 8091


@pytest.mark.parametrize("url", ["http://localhost:9001", "http://127.0.0.1:9001/"])
def test_port_from_a_loopback_url(
    brain_home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_URL", url)

    assert resolve_rerank_port(brain_home) == 9001


@pytest.mark.parametrize(
    "url",
    [
        "http://10.9.8.7:8081",
        "http://0.0.0.0:8081",
        "http://reranker.example.invalid:8081",
        "http://user:pw@127.0.0.1:8081",
        # Loopback, but the job serves plain HTTP on 127.0.0.1 only (rev-98 F2):
        # with these the reranker could never be reached and would be skipped.
        "http://[::1]:8081",
        "https://127.0.0.1:8081",
    ],
)
def test_a_non_loopback_url_is_refused_without_echoing_it(
    brain_home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_URL", url)

    with pytest.raises(LaunchdError, match="BRAIN_RERANK_URL") as exc:
        resolve_rerank_port(brain_home)

    assert url not in str(exc.value)
    assert "10.9.8.7" not in str(exc.value) and "pw" not in str(exc.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1", "http://127.0.0.1:80", "http://127.0.0.1:99999"]
)
def test_a_bad_port_is_refused(
    brain_home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_URL", url)

    with pytest.raises(LaunchdError, match="BRAIN_RERANK_URL"):
        resolve_rerank_port(brain_home)


def test_install_renders_the_configured_port(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(_gguf(tmp_path)))
    monkeypatch.setenv("BRAIN_RERANK_URL", "http://127.0.0.1:8091")
    stub, _log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )

    args = plistlib.loads((agents / f"{_RERANKER}.plist").read_bytes())["ProgramArguments"]
    assert args[-4:] == ["--host", "127.0.0.1", "--port", "8091"]


def test_install_refuses_a_non_loopback_url_before_launchctl(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(_gguf(tmp_path)))
    monkeypatch.setenv("BRAIN_RERANK_URL", "http://10.9.8.7:8081")
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    with pytest.raises(LaunchdError, match="BRAIN_RERANK_URL"):
        install_plists(
            brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
        )

    assert not (agents / f"{_RERANKER}.plist").exists()
    assert _calls(log) == []


def test_a_bad_url_does_not_touch_an_install_without_the_reranker(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL is read only when the reranker job is rendered."""
    monkeypatch.setenv("BRAIN_RERANK_URL", "http://10.9.8.7:8081")
    stub, _log = _recorder(tmp_path)
    agents = tmp_path / "agents"

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    assert (agents / "com.brain.watcher.plist").exists()


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


def _installed_reranker(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, bytes]:
    """A good ``--with-reranker`` install; returns (stub, log, agents, the plist's bytes)."""
    model = _gguf(tmp_path)
    stub, log = _recorder(tmp_path)
    agents = tmp_path / "agents"
    monkeypatch.setenv("BRAIN_RERANK_GGUF", str(model))
    install_plists(
        brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
    )
    log.unlink()
    return stub, log, agents, (agents / f"{_RERANKER}.plist").read_bytes()


def _assert_reranker_untouched_and_others_installed(
    log: Path, agents: Path, before: bytes, stderr: str, variable: str, value: str
) -> None:
    assert (agents / f"{_RERANKER}.plist").read_bytes() == before
    calls = _calls(log)
    assert not any(_RERANKER in c for c in calls), calls
    for label in _INSTALLED_LABELS:
        assert any(c.startswith("bootstrap") and label in c for c in calls), (label, calls)
    (warning,) = [line for line in stderr.splitlines() if line.startswith("WARNING")]
    assert _RERANKER in warning and variable in warning
    assert value not in stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BRAIN_RERANK_URL", "http://10.9.8.7:8091"),
        ("BRAIN_RERANK_URL", "http://127.0.0.1:80"),
        ("BRAIN_RERANK_GGUF", "models/relative.gguf"),
        ("BRAIN_RERANK_GGUF", "/synthetic/moved/away.gguf"),
    ],
    ids=["non-loopback-url", "bad-port", "relative-model", "missing-model"],
)
def test_the_sticky_re_render_warns_and_leaves_the_reranker_untouched(
    tmp_path: Path,
    brain_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
) -> None:
    """aud-98 / the coordinator's F3 ruling: ``brain-up`` must not fail, nor skip the
    watcher and brief, because the reranker's configuration went bad after install."""
    stub, log, agents, before = _installed_reranker(tmp_path, brain_home, monkeypatch)
    capsys.readouterr()
    monkeypatch.setenv(variable, value)

    install_plists(brain_home=brain_home, launchd_dir=agents, launchctl=str(stub))

    _assert_reranker_untouched_and_others_installed(
        log, agents, before, capsys.readouterr().err, variable, value
    )


def test_install_main_exits_normally_when_the_sticky_reranker_is_skipped(
    tmp_path: Path,
    brain_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub, log, agents, before = _installed_reranker(tmp_path, brain_home, monkeypatch)
    capsys.readouterr()
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(agents))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", str(stub))
    monkeypatch.setenv("BRAIN_RERANK_URL", "http://10.9.8.7:8091")

    install_main([])  # returns, so the console script exits 0

    _assert_reranker_untouched_and_others_installed(
        log, agents, before, capsys.readouterr().err, "BRAIN_RERANK_URL", "10.9.8.7"
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [("BRAIN_RERANK_URL", "http://10.9.8.7:8091"), ("BRAIN_RERANK_GGUF", "models/rel.gguf")],
)
def test_with_reranker_still_fails_closed(
    tmp_path: Path,
    brain_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    """The explicit flag still refuses, before any launchctl call."""
    stub, log, agents, _before = _installed_reranker(tmp_path, brain_home, monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(LaunchdError, match=variable):
        install_plists(
            brain_home=brain_home, launchd_dir=agents, launchctl=str(stub), with_reranker=True
        )

    assert _calls(log) == []


def test_a_relative_model_path_is_refused_whatever_the_cwd(
    tmp_path: Path, brain_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rev-98 F1: llama-server runs in $BRAIN_HOME, not the installer's cwd. A
    relative path that exists under the cwd must not pass the check and then
    point at nothing once launchd starts the job."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "models").mkdir(parents=True)
    (elsewhere / "models" / "x.gguf").write_bytes(b"GGUF synthetic")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("BRAIN_RERANK_GGUF", "models/x.gguf")

    with pytest.raises(LaunchdError, match="BRAIN_RERANK_GGUF") as exc:
        resolve_rerank_model(brain_home)

    assert "models/x.gguf" not in str(exc.value)


def test_a_relative_model_path_in_the_dotenv_is_refused(brain_home: Path) -> None:
    (brain_home / ".env").write_text("BRAIN_RERANK_GGUF=x.gguf\n", encoding="utf-8")

    with pytest.raises(LaunchdError, match="absolute"):
        resolve_rerank_model(brain_home)


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
