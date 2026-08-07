"""Tests for brain.bin.launchd — plist generator + install/uninstall helpers.

These tests have zero DB dependency. Run with:
    .venv/bin/pytest --no-cov --noconftest -q tests/test_bin_launchd.py -v
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from brain.bin.launchd import (
    _LABELS,
    install_main,
    install_plists,
    render_plist,
    resolve_brain_py,
    resolve_pipx_bin_dir,
    uninstall_plists,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WATCHER = "com.brain.watcher"
_BUILD = "com.brain.build"
_BRIEF = "com.brain.brief"

_FAKE_BRAIN_HOME = Path("/fake/brain/home")
_FAKE_VAULT = Path("/fake/vault")
_FAKE_PIPX_BIN = Path("/fake/pipx/bin")
_FAKE_BRAIN_PY = Path("/fake/python")


def _render(label: str = _WATCHER, **overrides: Path) -> str:
    kwargs: dict[str, Path] = {
        "brain_home": _FAKE_BRAIN_HOME,
        "vault_path": _FAKE_VAULT,
        "pipx_bin_dir": _FAKE_PIPX_BIN,
        "brain_py": _FAKE_BRAIN_PY,
    }
    kwargs.update(overrides)
    return render_plist(label, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 1 — render_plist substitutes all variables
# ---------------------------------------------------------------------------


def test_render_plist_substitutes_all_variables() -> None:
    """render_plist replaces every {{ key }} marker with the supplied value."""
    text = _render()

    assert "/fake/brain/home" in text
    assert "/fake/vault" in text
    assert "/fake/pipx/bin" in text
    assert "/fake/python" in text
    # No leftover markers.
    assert "{{ " not in text


# ---------------------------------------------------------------------------
# Test 2 — render_plist XML-escapes values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,escaped",
    [
        ("/te&st/home", "/te&amp;st/home"),
        ("/te<st/home", "/te&lt;st/home"),
        ("/te>st/home", "/te&gt;st/home"),
    ],
    ids=["ampersand", "less-than", "greater-than"],
)
def test_render_plist_xml_escapes_values(raw: str, escaped: str) -> None:
    """Values containing XML metacharacters are escaped so the plist stays valid XML.

    xml.sax.saxutils.escape covers & / < / > by default. Parametrising over
    all three locks in the contract — without escaping, a path containing
    any of these characters would produce invalid plist XML and
    launchctl bootstrap would fail with a parse error.
    """
    text = _render(brain_home=Path(raw))

    # Raw form must NOT appear anywhere in the output.
    assert raw not in text
    # Escaped form must be present.
    assert escaped in text
    # Full document must parse.
    ET.fromstring(text)


# ---------------------------------------------------------------------------
# Test 3 — render_plist produces valid XML for both labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", [_WATCHER, _BUILD, _BRIEF])
def test_render_plist_is_valid_xml(label: str) -> None:
    """Each rendered plist is well-formed XML."""
    text = _render(
        label=label,
        brain_home=Path("/home/user/.brain"),
        vault_path=Path("/home/user/brain-vault"),
        pipx_bin_dir=Path("/home/user/.local/bin"),
        brain_py=Path("/home/user/.local/pipx/venvs/secondbrain-py/bin/python"),
    )
    ET.fromstring(text)  # raises ET.ParseError on invalid XML


# ---------------------------------------------------------------------------
# Test 4 — install_plists writes both plist files
# ---------------------------------------------------------------------------


def test_install_plists_writes_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install_plists creates both plist files in launchd_dir."""
    monkeypatch.setenv("BRAIN_LAUNCHCTL", "/usr/bin/true")

    launchd_dir = tmp_path / "agents"
    install_plists(
        brain_home=tmp_path / "brain",
        launchd_dir=launchd_dir,
        launchctl="/usr/bin/true",
    )

    for label in _LABELS:
        plist = launchd_dir / f"{label}.plist"
        assert plist.exists(), f"expected {plist} to exist"
        assert plist.stat().st_size > 0


# ---------------------------------------------------------------------------
# Test 5 — install_plists calls launchctl bootstrap for both labels
# ---------------------------------------------------------------------------


def test_install_plists_calls_launchctl_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_plists calls launchctl bootstrap once for each label."""
    # Build a tiny shell stub that logs its argv.
    stub = tmp_path / "launchctl-stub"
    log_path = tmp_path / "calls.log"
    stub.write_text("#!/usr/bin/env bash\necho \"$@\" >> \"$LOG\"\n")
    stub.chmod(0o755)
    monkeypatch.setenv("LOG", str(log_path))

    launchd_dir = tmp_path / "agents"
    install_plists(
        brain_home=tmp_path / "brain",
        launchd_dir=launchd_dir,
        launchctl=str(stub),
    )

    log_content = log_path.read_text()
    # bootstrap must be present for both labels.
    for label in _LABELS:
        assert "bootstrap" in log_content, "expected 'bootstrap' in stub log"
        assert label in log_content, f"expected {label} in stub log"


# ---------------------------------------------------------------------------
# Test 6 — uninstall_plists removes both plist files
# ---------------------------------------------------------------------------


def test_uninstall_plists_removes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After install+uninstall the plist files are gone."""
    launchd_dir = tmp_path / "agents"

    # Install first (use /usr/bin/true so launchctl is a no-op).
    install_plists(
        brain_home=tmp_path / "brain",
        launchd_dir=launchd_dir,
        launchctl="/usr/bin/true",
    )
    for label in _LABELS:
        assert (launchd_dir / f"{label}.plist").exists()

    # Uninstall — /usr/bin/true returns 0 for the "print" probe, so each label
    # is considered loaded; bootout is called (also /usr/bin/true).
    uninstall_plists(launchd_dir=launchd_dir, launchctl="/usr/bin/true")

    for label in _LABELS:
        assert not (launchd_dir / f"{label}.plist").exists(), (
            f"{label}.plist should have been removed"
        )


# ---------------------------------------------------------------------------
# Test 7 — uninstall_plists is idempotent when nothing is loaded/present
# ---------------------------------------------------------------------------


def test_uninstall_plists_idempotent_when_nothing_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling uninstall against an empty launchd_dir does not raise."""
    launchd_dir = tmp_path / "agents"
    launchd_dir.mkdir(parents=True)

    # Use a stub that always returns non-zero (service not loaded).
    stub = tmp_path / "launchctl-stub"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    stub.chmod(0o755)

    # Should not raise.
    uninstall_plists(launchd_dir=launchd_dir, launchctl=str(stub))


# ---------------------------------------------------------------------------
# Test 8 — install_main uses BRAIN_LAUNCHD_DIR + BRAIN_LAUNCHCTL env overrides
# ---------------------------------------------------------------------------


def test_install_main_uses_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_main reads BRAIN_LAUNCHD_DIR and BRAIN_LAUNCHCTL from env."""
    agents_dir = tmp_path / "agents"
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(agents_dir))
    monkeypatch.setenv("BRAIN_LAUNCHCTL", "/usr/bin/true")
    # Override BRAIN_HOME so _brain_home_root() doesn't resolve to the live checkout.
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "brain"))

    install_main()

    for label in _LABELS:
        plist = agents_dir / f"{label}.plist"
        assert plist.exists(), f"expected {plist} to land in {agents_dir}"


# ---------------------------------------------------------------------------
# Test 9 — resolve_pipx_bin_dir fallback when pipx is absent
# ---------------------------------------------------------------------------


def test_resolve_pipx_bin_dir_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pipx is not on PATH, resolve_pipx_bin_dir returns ~/.local/bin."""
    with patch(
        "brain.bin.launchd.subprocess.run", side_effect=FileNotFoundError("pipx not found")
    ):
        result = resolve_pipx_bin_dir()

    assert result == Path.home() / ".local" / "bin"


# ---------------------------------------------------------------------------
# Test 10 — install_plists ensures the foreground wrapper shims are installed
# ---------------------------------------------------------------------------


def test_install_plists_installs_foreground_wrappers(
    tmp_path: Path,
) -> None:
    """install_plists writes _brain-watcher-fg and _brain-build-fg into brain_home/bin/."""
    brain_home = tmp_path / "brain"
    launchd_dir = tmp_path / "agents"

    install_plists(
        brain_home=brain_home,
        launchd_dir=launchd_dir,
        launchctl="/usr/bin/true",
    )

    for wrapper in ("_brain-watcher-fg", "_brain-build-fg", "_brain-brief-fg"):
        shim = brain_home / ".shims" / wrapper
        assert shim.exists(), f"expected shim {shim} to be installed"
        assert shim.stat().st_mode & 0o111, f"expected {shim} to be executable"


# ---------------------------------------------------------------------------
# Test 11 — generated plists must not depend on the ambient environment
#
# Regression for the 12-day silent outage (2026-07-26 -> 2026-08-07): launchd
# starts agents with a minimal environment and no useful cwd. The plists pinned
# HOME/PATH/BRAIN_VAULT_PATH/BRAIN_PY but never BRAIN_HOME, so _brain_home_root()
# fell through to a repo-root walk-up from the *installed* package location and
# landed on ~/.brain only by accident of the install layout. Every leg of the
# dotenv chain then missed, Config.load() raised, and all three daemons died.
# ---------------------------------------------------------------------------


def _top_level_nodes(text: str) -> list[ET.Element]:
    """Return the children of the plist's top-level <dict>."""
    root = ET.fromstring(text)
    top = root.find("dict")
    assert top is not None, "plist has no top-level <dict>"
    return list(top)


def _plist_value(text: str, key: str) -> ET.Element:
    """Return the value element following *key* in the top-level <dict>."""
    nodes = _top_level_nodes(text)
    for index, node in enumerate(nodes):
        if node.tag == "key" and node.text == key:
            return nodes[index + 1]
    raise AssertionError(f"plist has no {key!r} key")


def _environment_variables(text: str) -> dict[str, str]:
    """Parse the plist's EnvironmentVariables block into a plain mapping."""
    env_dict = _plist_value(text, "EnvironmentVariables")
    assert env_dict.tag == "dict"
    children = list(env_dict)
    return {
        (children[i].text or ""): (children[i + 1].text or "")
        for i in range(0, len(children), 2)
    }


@pytest.mark.parametrize("label", _LABELS)
def test_plist_pins_every_var_the_daemons_need(label: str) -> None:
    """Each plist explicitly sets every variable its shim + config chain read.

    Nothing may be inherited from the launching environment, because under
    launchd there effectively isn't one.
    """
    env = _environment_variables(_render(label))

    for required in (
        "HOME",
        "PATH",
        "BRAIN_HOME",
        "BRAIN_VAULT_PATH",
        "BRAIN_PY",
        "BRAIN_IGNORE_CWD_DOTENV",
    ):
        assert required in env, f"{label} does not pin {required}"
        assert env[required].strip(), f"{label} pins {required} to an empty value"


@pytest.mark.parametrize("label", _LABELS)
def test_plist_opts_out_of_the_cwd_dotenv_walkup(label: str) -> None:
    """Daemons must not resolve config through an ambient-cwd read.

    The walk-up climbs to the filesystem root, so a stray .env in any ancestor
    of WorkingDirectory would silently repoint the daemon at another database.
    Today that leg finds nothing only because WorkingDirectory happens to be
    $BRAIN_HOME — incidental, not a guarantee.
    """
    from brain.config import _TRUTHY_ENV_VALUES

    env = _environment_variables(_render(label))
    assert env["BRAIN_IGNORE_CWD_DOTENV"] in _TRUTHY_ENV_VALUES, (
        f"{label} sets BRAIN_IGNORE_CWD_DOTENV to a value config.py "
        f"does not treat as true"
    )


@pytest.mark.parametrize("label", _LABELS)
def test_plist_exports_brain_home(label: str) -> None:
    """BRAIN_HOME is exported as the rendered brain_home — this is the fix."""
    env = _environment_variables(_render(label))
    assert env["BRAIN_HOME"] == str(_FAKE_BRAIN_HOME)


@pytest.mark.parametrize("label", _LABELS)
def test_plist_sets_explicit_working_directory(label: str) -> None:
    """A daemon with no useful cwd must never resolve paths relative to one."""
    working_dir = _plist_value(_render(label), "WorkingDirectory")
    assert working_dir.tag == "string"
    assert working_dir.text == str(_FAKE_BRAIN_HOME)


@pytest.mark.parametrize("label", _LABELS)
def test_plist_bakes_no_secret_values(label: str) -> None:
    """Only paths belong in a plist; secrets stay in $BRAIN_HOME/.env.

    Baking a secret here would both leak it into a world-readable-ish file and
    freeze a snapshot that goes stale silently — the exact failure mode this
    whole fix is about.
    """
    env = _environment_variables(_render(label))

    for forbidden in (
        "DATABASE_URL",
        "VOYAGE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in env, f"{label} bakes {forbidden} into the plist"


def test_plist_brain_home_drives_config_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: the BRAIN_HOME a plist exports is the one Config resolves.

    Ties the plist change to the behaviour it exists to guarantee — with this
    variable exported, $BRAIN_HOME/.env is deterministic instead of depending on
    whether the running package happens to sit under a checkout.
    """
    from brain.config import _brain_home_root

    synthetic_home = tmp_path / "synthetic-brain-home"
    env = _environment_variables(_render(_WATCHER, brain_home=synthetic_home))

    monkeypatch.setenv("BRAIN_HOME", env["BRAIN_HOME"])

    assert _brain_home_root() == synthetic_home


@pytest.mark.parametrize("label", _LABELS)
def test_shim_rotates_logs_before_exec(label: str, tmp_path: Path) -> None:
    """Every shim caps its log before exec'ing the daemon (item #5).

    Rotation has to happen here rather than in a Python logging handler: launchd
    writes fd 1/2 straight to StandardOut/ErrorPath, so Rich tracebacks and
    esbuild's Go goroutine dumps never pass through Python's logging module.
    """
    brain_home = tmp_path / "brain"
    install_plists(
        brain_home=brain_home,
        launchd_dir=tmp_path / "agents",
        launchctl="/usr/bin/true",
    )
    program = _plist_value(_render(label), "ProgramArguments")
    shim_name = Path(str(program[0].text)).name

    body = (brain_home / ".shims" / shim_name).read_text(encoding="utf-8")

    # Match on real command lines, not prose: the surrounding comments mention
    # both "exec" and the module by name.
    lines = body.splitlines()
    rotate_lines = [
        i
        for i, line in enumerate(lines)
        if "-m brain.log_rotation" in line and not line.lstrip().startswith("#")
    ]
    exec_lines = [i for i, line in enumerate(lines) if line.startswith("exec ")]

    assert rotate_lines, f"{shim_name} does not rotate its log"
    assert exec_lines, f"{shim_name} has no exec line"
    assert rotate_lines[0] < exec_lines[0], (
        f"{shim_name} rotates after exec — the exec never returns"
    )


# ---------------------------------------------------------------------------
# Test 12 — resolve_brain_py honours a BRAIN_PY override
#
# Without the override, sys.executable is the only possible answer, so running
# brain-install-launchd from a dev checkout silently repoints the user's live
# LaunchAgents at that checkout — swapping a released install for uncommitted
# code as a side effect of regenerating a plist. This is also the contract
# brain.bin._launcher.exec_shim already documents ("an existing BRAIN_PY env var
# is preserved untouched"), which resolve_brain_py used to disagree with.
# ---------------------------------------------------------------------------


def test_resolve_brain_py_defaults_to_sys_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behaviour is unchanged when BRAIN_PY is unset."""
    monkeypatch.delenv("BRAIN_PY", raising=False)
    assert resolve_brain_py() == Path(sys.executable)


def test_resolve_brain_py_honours_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BRAIN_PY wins, so plists can target an interpreter we are not running."""
    monkeypatch.setenv("BRAIN_PY", "/fake/tools/secondbrain-py/bin/python")
    assert resolve_brain_py() == Path("/fake/tools/secondbrain-py/bin/python")


def test_resolve_brain_py_ignores_blank_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/whitespace BRAIN_PY must not yield an unusable empty path."""
    monkeypatch.setenv("BRAIN_PY", "   ")
    assert resolve_brain_py() == Path(sys.executable)


def test_brain_py_override_reaches_the_rendered_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: the override is what install_plists bakes into BRAIN_PY.

    This is the capability that would have let the live plists be regenerated
    normally instead of hand-rendered.
    """
    target_py = "/fake/tools/secondbrain-py/bin/python"
    monkeypatch.setenv("BRAIN_PY", target_py)
    launchd_dir = tmp_path / "agents"

    install_plists(
        brain_home=tmp_path / "brain",
        launchd_dir=launchd_dir,
        launchctl="/usr/bin/true",
    )

    env = _environment_variables(
        (launchd_dir / f"{_WATCHER}.plist").read_text(encoding="utf-8")
    )
    assert env["BRAIN_PY"] == target_py
    assert str(sys.executable) not in env["BRAIN_PY"]
