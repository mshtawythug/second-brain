"""Tests for brain.bin.launchd — plist generator + install/uninstall helpers.

These tests have zero DB dependency. Run with:
    .venv/bin/pytest --no-cov --noconftest -q tests/test_bin_launchd.py -v
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from brain.bin.launchd import (
    _LABELS,
    install_main,
    install_plists,
    render_plist,
    resolve_pipx_bin_dir,
    uninstall_plists,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WATCHER = "com.brain.watcher"
_BUILD = "com.brain.build"

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


@pytest.mark.parametrize("label", [_WATCHER, _BUILD])
def test_render_plist_is_valid_xml(label: str) -> None:
    """Each rendered plist is well-formed XML."""
    text = _render(
        label=label,
        brain_home=Path("/home/user/.brain"),
        vault_path=Path("/home/user/brain-vault"),
        pipx_bin_dir=Path("/home/user/.local/bin"),
        brain_py=Path("/home/user/.local/pipx/venvs/second-brain/bin/python"),
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

    for wrapper in ("_brain-watcher-fg", "_brain-build-fg"):
        shim = brain_home / ".shims" / wrapper
        assert shim.exists(), f"expected shim {shim} to be installed"
        assert shim.stat().st_mode & 0o111, f"expected {shim} to be executable"
