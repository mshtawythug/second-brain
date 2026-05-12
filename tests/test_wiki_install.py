"""Tests for brain.wiki.install — wiki_install() entry point.

These tests have zero DB dependency. Run with:
    .venv/bin/pytest --no-cov --noconftest -q tests/test_wiki_install.py -v
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain.wiki import QUARTZ_PINNED_COMMIT, QUARTZ_REPO_URL
from brain.wiki.install import WikiInstallError, wiki_install

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_VAULT = "vault"
_FAKE_BRAIN_HOME = "brain_home"


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    """Return a dict with vault + brain_home paths under tmp_path."""
    vault = tmp_path / _FAKE_VAULT
    vault.mkdir()
    brain_home = tmp_path / _FAKE_BRAIN_HOME
    brain_home.mkdir()
    return {"vault": vault, "brain_home": brain_home, "tmp": tmp_path}


@pytest.fixture()
def mock_config(dirs: dict[str, Path]):
    """Monkeypatch Config.load_minimal() to return a minimal fake config."""
    fake_cfg = MagicMock()
    fake_cfg.vault_path = dirs["vault"]
    fake_cfg.brain_home = dirs["brain_home"]
    with patch("brain.wiki.install.Config") as MockConfig:
        MockConfig.load_minimal.return_value = fake_cfg
        yield MockConfig, dirs


def _make_valid_workspace(quartz_dir: Path) -> None:
    """Create a minimal valid-looking Quartz workspace on disk."""
    quartz_dir.mkdir(exist_ok=True)
    (quartz_dir / "package.json").write_text('{"name": "quartz"}\n')
    (quartz_dir / ".git").mkdir()


def _subprocess_stub_pinned(
    subprocess_calls: list[list[str]],
) -> "MagicMock":
    """Return a subprocess.run side_effect that records calls and returns the
    pinned commit SHA for ``git rev-parse HEAD`` invocations."""

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        subprocess_calls.append(list(cmd))
        result = MagicMock(returncode=0)
        if "rev-parse" in cmd:
            result.stdout = QUARTZ_PINNED_COMMIT + "\n"
        return result

    return _run  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Test 1 — fresh install: clone + checkout + overlay + Caddyfile
# ---------------------------------------------------------------------------


def test_wiki_install_fresh_clone(mock_config: tuple) -> None:
    """Fresh install: git clone + git checkout + overlay applied + Caddyfile rendered."""
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    brain_home: Path = dirs["brain_home"]
    quartz_dir = vault_path / ".quartz"

    subprocess_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
        subprocess_calls.append(list(cmd))
        return MagicMock(returncode=0)

    with (
        patch("brain.wiki.install.subprocess.run", side_effect=_fake_subprocess_run),
        patch("brain.wiki.install.apply_overlay") as mock_apply,
        patch("brain.wiki.install.plan_overlay") as mock_plan,
    ):
        mock_plan.return_value = MagicMock()
        mock_apply.return_value = [("src", "dst")]  # simulate 1 file copied

        wiki_install(vault=vault_path, no_npm=True)

    # --- git clone called with QUARTZ_REPO_URL and the correct destination ---
    clone_calls = [c for c in subprocess_calls if c[0] == "git" and "clone" in c]
    assert len(clone_calls) == 1, f"Expected 1 git clone call, got: {clone_calls}"
    assert QUARTZ_REPO_URL in clone_calls[0]
    assert str(quartz_dir) in clone_calls[0]

    # --- git checkout called with the pinned SHA ---
    checkout_calls = [c for c in subprocess_calls if c[0] == "git" and "checkout" in c]
    assert len(checkout_calls) == 1, f"Expected 1 git checkout call, got: {checkout_calls}"
    assert QUARTZ_PINNED_COMMIT in checkout_calls[0]

    # --- overlay was applied ---
    mock_plan.assert_called_once_with(quartz_dir)
    mock_apply.assert_called_once()

    # --- Caddyfile rendered with correct substitutions ---
    caddyfile = brain_home / "Caddyfile"
    assert caddyfile.exists(), "Caddyfile should have been written"
    content = caddyfile.read_text()
    assert str(vault_path) in content, "vault_path should appear in Caddyfile"
    assert "8080" in content, "default port 8080 should appear in Caddyfile"


# ---------------------------------------------------------------------------
# Test 2 — idempotent refresh: no clone when valid workspace already exists
# ---------------------------------------------------------------------------


def test_wiki_install_idempotent_refresh(mock_config: tuple) -> None:
    """Re-running on a valid workspace skips git clone but re-applies overlay."""
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    brain_home: Path = dirs["brain_home"]
    quartz_dir = vault_path / ".quartz"

    # Pre-create a valid workspace: package.json + .git/ + correct HEAD commit.
    _make_valid_workspace(quartz_dir)

    subprocess_calls: list[list[str]] = []

    with (
        patch(
            "brain.wiki.install.subprocess.run",
            side_effect=_subprocess_stub_pinned(subprocess_calls),
        ),
        patch("brain.wiki.install.apply_overlay") as mock_apply,
        patch("brain.wiki.install.plan_overlay") as mock_plan,
    ):
        mock_plan.return_value = MagicMock()
        mock_apply.return_value = []

        wiki_install(vault=vault_path, no_npm=True)

    # No git clone should have been called.
    clone_calls = [c for c in subprocess_calls if "clone" in c]
    assert clone_calls == [], f"git clone should NOT be called on refresh: {clone_calls}"

    # rev-parse must have been called (commit verification).
    revparse_calls = [c for c in subprocess_calls if "rev-parse" in c]
    assert len(revparse_calls) == 1, "git rev-parse HEAD must be called on refresh"

    # Overlay must still be applied.
    mock_apply.assert_called_once()

    # Caddyfile must still be re-rendered.
    caddyfile = brain_home / "Caddyfile"
    assert caddyfile.exists(), "Caddyfile should be re-rendered on refresh"


# ---------------------------------------------------------------------------
# Test 3 — dry-run: no filesystem mutations, no subprocess calls
# ---------------------------------------------------------------------------


def test_wiki_install_dry_run_no_side_effects(tmp_path: Path) -> None:
    """dry_run=True must not create .quartz/, write Caddyfile, or call subprocess."""
    vault_path = tmp_path / "vault"
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()

    fake_cfg = MagicMock()
    fake_cfg.vault_path = vault_path
    fake_cfg.brain_home = brain_home

    subprocess_calls: list[list[str]] = []

    def _fail_subprocess(*args: object, **kwargs: object) -> None:
        subprocess_calls.append(list(args[0]) if args else [])  # type: ignore[arg-type]

    with (
        patch("brain.wiki.install.Config") as MockConfig,
        patch("brain.wiki.install.subprocess.run", side_effect=_fail_subprocess),
    ):
        MockConfig.load_minimal.return_value = fake_cfg
        wiki_install(vault=vault_path, dry_run=True)

    # .quartz/ must not have been created.
    quartz_dir = vault_path / ".quartz"
    assert not quartz_dir.exists(), ".quartz/ must not be created during dry-run"

    # Caddyfile must not have been written.
    caddyfile = brain_home / "Caddyfile"
    assert not caddyfile.exists(), "Caddyfile must not be written during dry-run"

    # subprocess.run must never have been called.
    assert subprocess_calls == [], (
        f"subprocess.run must not be invoked in dry-run: {subprocess_calls}"
    )


# ---------------------------------------------------------------------------
# Test 4 — --force destroys existing workspace and re-clones
# ---------------------------------------------------------------------------


def test_wiki_install_force_destroys_existing(mock_config: tuple) -> None:
    """--force wipes the existing .quartz/ dir and triggers a fresh git clone."""
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    quartz_dir = vault_path / ".quartz"

    # Pre-create the workspace with a sentinel file.
    quartz_dir.mkdir()
    marker = quartz_dir / "marker.txt"
    marker.write_text("should be deleted by --force\n")

    subprocess_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
        subprocess_calls.append(list(cmd))
        # git clone must recreate the directory (simulate it).
        if "clone" in cmd:
            quartz_dir.mkdir(exist_ok=True)
        return MagicMock(returncode=0)

    with (
        patch("brain.wiki.install.subprocess.run", side_effect=_fake_subprocess_run),
        patch("brain.wiki.install.apply_overlay") as mock_apply,
        patch("brain.wiki.install.plan_overlay") as mock_plan,
    ):
        mock_plan.return_value = MagicMock()
        mock_apply.return_value = []

        wiki_install(vault=vault_path, force=True, no_npm=True)

    # Sentinel marker.txt must be gone (rmtree happened before clone).
    assert not marker.exists(), "marker.txt must be gone after --force"

    # git clone must have been called.
    clone_calls = [c for c in subprocess_calls if "clone" in c]
    assert len(clone_calls) == 1, f"git clone must be called with --force: {clone_calls}"


# ---------------------------------------------------------------------------
# Test 5 — broken workspace: dir exists but no package.json/no .git/
# ---------------------------------------------------------------------------


def test_wiki_install_broken_workspace_no_package_json(mock_config: tuple) -> None:
    """A .quartz/ dir without package.json raises WikiInstallError, not 'refreshed'."""
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    quartz_dir = vault_path / ".quartz"

    # Simulate an interrupted clone: directory exists but has no package.json.
    quartz_dir.mkdir()
    (quartz_dir / "some_other_file.txt").write_text("incomplete\n")

    with (
        patch("brain.wiki.install.subprocess.run"),
        pytest.raises(WikiInstallError, match="incomplete"),
    ):
        wiki_install(vault=vault_path, no_npm=True)


def test_wiki_install_broken_workspace_no_git_dir(mock_config: tuple) -> None:
    """A .quartz/ with package.json but no .git/ raises WikiInstallError."""
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    quartz_dir = vault_path / ".quartz"

    # package.json present but no .git/ — not a git repo (hand-crafted dir).
    quartz_dir.mkdir()
    (quartz_dir / "package.json").write_text('{"name": "quartz"}\n')

    with (
        patch("brain.wiki.install.subprocess.run"),
        pytest.raises(WikiInstallError, match="incomplete"),
    ):
        wiki_install(vault=vault_path, no_npm=True)


# ---------------------------------------------------------------------------
# Test 6 — wrong commit: package.json + .git/ present but HEAD ≠ pinned SHA
# ---------------------------------------------------------------------------


def test_wiki_install_wrong_commit_raises(mock_config: tuple) -> None:
    """A workspace at the wrong commit raises WikiInstallError with --force hint.

    This is the key regression: git clone writes package.json before the
    subsequent git checkout step.  If checkout fails the workspace is at
    the default branch HEAD, not the pinned SHA — the overlay is version-
    specific and must not be applied to the wrong commit.
    """
    _, dirs = mock_config
    vault_path: Path = dirs["vault"]
    quartz_dir = vault_path / ".quartz"

    # Valid structural workspace but at a *different* commit.
    _make_valid_workspace(quartz_dir)
    wrong_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert wrong_sha != QUARTZ_PINNED_COMMIT

    subprocess_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
        subprocess_calls.append(list(cmd))
        result = MagicMock(returncode=0)
        if "rev-parse" in cmd:
            result.stdout = wrong_sha + "\n"
        return result

    with (
        patch("brain.wiki.install.subprocess.run", side_effect=_fake_subprocess_run),
        patch("brain.wiki.install.apply_overlay"),
        patch("brain.wiki.install.plan_overlay"),
        pytest.raises(WikiInstallError, match=wrong_sha[:12]),
    ):
        wiki_install(vault=vault_path, no_npm=True)

    # rev-parse must have been called.
    revparse_calls = [c for c in subprocess_calls if "rev-parse" in c]
    assert len(revparse_calls) == 1, "git rev-parse HEAD must have been invoked"
