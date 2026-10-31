"""A hostile repository cannot make `brain ingest-code` run a command or read elsewhere (sec-113).

Every repository is synthetic and built in ``tmp_path`` with the real ``git``
(``tests/code_repo_fixtures.py``). The only transport is ``file://`` between two
directories in ``tmp_path``; there is no network. Every "payload" is a script in
``tmp_path`` whose only effect is to create a marker file next to it, so a test
proves a command did NOT run by finding no marker.
"""
from __future__ import annotations

import functools
import subprocess
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import code_repo as cr
from tests import code_repo_fixtures as fx

pytestmark = pytest.mark.nodb

SINCE = date.fromisoformat(fx.SINCE)


def _runner(repo: Path, home: Path) -> cr.GitRunner:
    return functools.partial(cr.git_runner, repo=repo, base_env={"HOME": str(home)})


def _payload(tmp_path: Path) -> tuple[Path, Path]:
    """A script that only creates ``fired/<name it was run as>-<pid>``; returns (script, dir)."""
    fired = tmp_path / "fired"
    fired.mkdir()
    script = tmp_path / "payload.sh"
    script.write_text(f'#!/bin/sh\ntouch "{fired}/$(basename "$0")-$$"\nexit 1\n')
    script.chmod(0o755)
    return script, fired


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _owner_repo(path: Path, home: Path, files: dict[str, str | bytes]) -> Path:
    path.mkdir(parents=True)
    fx.git(path, "init", "-q", home=home)
    fx.commit(path, files, home=home, author=fx.owner())
    return path


def _partial_clone(tmp_path: Path, home: Path, uploadpack: Path) -> Path:
    """A ``tree:0`` partial clone of a two-commit repository, whose remote runs ``uploadpack``.

    Its history references trees it does not have, so ``log --name-only`` must
    fetch them, which is what ran the remote's ``uploadpack`` command.
    """
    upstream = _owner_repo(tmp_path / "upstream", home, {"a/b/one.py": "x = 1\n"})
    fx.commit(upstream, {"a/b/two.py": "y = 2\n"}, home=home, author=fx.owner())
    fx.git(upstream, "config", "uploadpack.allowFilter", "true", home=home)
    fx.git(tmp_path, "clone", "-q", "--no-checkout", "--filter=tree:0",
           f"file://{upstream}", "clone", home=home)
    clone = tmp_path / "clone"
    fx.git(clone, "config", "remote.origin.uploadpack", str(uploadpack), home=home)
    return clone


# --- CRITICAL: a partial clone's lazy fetch -----------------------------------------------


def test_a_partial_clone_is_refused_before_its_history_is_read(tmp_path: Path) -> None:
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    clone = _partial_clone(tmp_path, home, payload)

    with pytest.raises(cr.CodeRepoError, match="a partial clone .* is not supported"):
        tree = cr.find_work_tree(clone, runner=_runner(clone, home))
        cr.owned_files(tree, runner=_runner(tree.root, home), emails=[fx.OWNER_EMAIL],
                       since=SINCE)

    assert list(fired.iterdir()) == []


def test_the_lazy_fetch_is_off_even_when_the_refusal_is_not_reached(tmp_path: Path) -> None:
    """The second layer: ``GIT_NO_LAZY_FETCH`` and ``protocol.allow=never`` on the read itself."""
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    clone = _partial_clone(tmp_path, home, payload)

    with pytest.raises(cr.CodeRepoError, match="`git log` failed"):
        cr.owner_touched_files(_runner(clone, home), emails=[fx.OWNER_EMAIL], since=SINCE)

    assert list(fired.iterdir()) == []


def _without(pairs: tuple[str, ...], value: str) -> list[str]:
    """``_SAFE_CONFIG`` with the ``-c value`` pair dropped."""
    out: list[str] = []
    for flag, item in zip(pairs[::2], pairs[1::2], strict=True):
        if item != value:
            out += [flag, item]
    return out


@pytest.mark.parametrize(
    "layer", ["GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"], ids=["no-lazy-fetch", "no-protocol"]
)
def test_each_transport_layer_alone_stops_the_lazy_fetch(tmp_path: Path, layer: str) -> None:
    """rev-113: each env layer alone, with the other removed and the repository allowing
    ``file`` itself (``protocol.file.allow=always`` outranks ``-c protocol.allow=never``)."""
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    clone = _partial_clone(tmp_path, home, payload)
    fx.git(clone, "config", "protocol.allow", "always", home=home)
    fx.git(clone, "config", "protocol.file.allow", "always", home=home)
    env = cr.git_env({"HOME": str(home)})
    other = {"GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"} - {layer}
    for name in other:
        del env[name]
    argv = [fx.git_bin(), "--no-pager", "-C", str(clone),
            *_without(cr._SAFE_CONFIG, "protocol.allow=never"),
            "log", "-z", "--name-only", "--format=", "HEAD", "--"]

    subprocess.run(argv, env=env, capture_output=True, timeout=60, check=False)

    assert list(fired.iterdir()) == []


def test_the_repository_cannot_allow_its_own_protocol(tmp_path: Path) -> None:
    """The runner as shipped, past the refusal, with ``protocol.file.allow=always`` set."""
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    clone = _partial_clone(tmp_path, home, payload)
    fx.git(clone, "config", "protocol.file.allow", "always", home=home)

    with pytest.raises(cr.CodeRepoError, match="`git log` failed"):
        cr.owner_touched_files(_runner(clone, home), emails=[fx.OWNER_EMAIL], since=SINCE)

    assert list(fired.iterdir()) == []


def test_the_command_refuses_a_partial_clone_and_runs_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    clone = _partial_clone(tmp_path, home, payload)

    result = CliRunner().invoke(app, ["ingest-code", str(clone), "--author-email",
                                      fx.OWNER_EMAIL, "--since", fx.SINCE],
                                env={"HOME": str(home)})

    assert result.exit_code == 1
    assert "a partial clone (it has a promisor remote) is not supported" in result.output
    assert list(fired.iterdir()) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("extensions.partialClone", "origin"),
        ("remote.Upstream.promisor", "true"),
        ("remote.upstream.partialCloneFilter", "blob:none"),
    ],
)
def test_any_partial_clone_marker_is_refused(tmp_path: Path, key: str, value: str) -> None:
    home = _home(tmp_path)
    repo = _owner_repo(tmp_path / "r", home, {"m.py": "x = 1\n"})
    fx.git(repo, "config", key, value, home=home)

    with pytest.raises(cr.CodeRepoError, match="partial clone"):
        cr.find_work_tree(repo, runner=_runner(repo, home))


def test_an_ordinary_remote_is_not_a_partial_clone(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repo = _owner_repo(tmp_path / "r", home, {"m.py": "x = 1\n"})
    fx.git(repo, "config", "remote.origin.url", "file:///nowhere", home=home)
    fx.git(repo, "config", "remote.origin.pushurl", "file:///nowhere", home=home)

    assert cr.find_work_tree(repo, runner=_runner(repo, home)).root == repo.resolve()


# --- HIGH: core.worktree points the top level elsewhere ------------------------------------


def _redirected(tmp_path: Path, home: Path) -> tuple[Path, Path]:
    """A repository at ``hostile/`` whose ``core.worktree`` is ``victim/``, which it tracks."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "private.py").write_text('token = "synthetic-private"\n')
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    fx.git(hostile, "init", "-q", home=home)
    fx.git(hostile, "config", "core.worktree", str(victim), home=home)
    git_dir = f"--git-dir={hostile / '.git'}"
    fx.git(victim, git_dir, "add", "private.py", home=home)
    fx.git(victim, git_dir, "commit", "-q", "-m", "synthetic", home=home, author=fx.owner())
    return hostile, victim


def test_a_work_tree_configured_outside_the_named_directory_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    hostile, _victim = _redirected(tmp_path, home)

    with pytest.raises(cr.CodeRepoError, match=r"configured outside this directory"):
        cr.find_work_tree(hostile, runner=_runner(hostile, home))


def test_the_command_reads_nothing_through_core_worktree(tmp_path: Path) -> None:
    home = _home(tmp_path)
    hostile, _victim = _redirected(tmp_path, home)

    result = CliRunner().invoke(app, ["ingest-code", str(hostile), "--author-email",
                                      fx.OWNER_EMAIL, "--since", fx.SINCE],
                                env={"HOME": str(home)})

    assert result.exit_code == 1
    assert "configured outside this directory (core.worktree)" in result.output
    assert "private.py" not in result.output


def test_a_work_tree_above_the_named_directory_scopes_reads_to_it(tmp_path: Path) -> None:
    """``core.worktree`` at a PARENT passes containment; ``scope`` is still the named directory."""
    home = _home(tmp_path)
    parent = tmp_path / "parent"
    named = parent / "named"
    named.mkdir(parents=True)
    fx.git(named, "init", "-q", home=home)
    fx.git(named, "config", "core.worktree", str(parent), home=home)

    tree = cr.find_work_tree(named, runner=_runner(named, home))

    assert tree.root == parent.resolve()
    assert tree.scope == named.resolve()
    assert tree.prefix == "named/"


# --- MEDIUM: global config is not read -----------------------------------------------------


def test_the_owners_global_and_xdg_config_are_not_read(tmp_path: Path) -> None:
    """Shown with ``core.excludesFile``: honoured, it would hide the file from ``tracked``."""
    home = _home(tmp_path)
    hide = tmp_path / "hide-everything"
    hide.write_text("*.py\n")
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {hide}\n")
    xdg = home / ".config" / "git"
    xdg.mkdir(parents=True)
    (xdg / "config").write_text(f"[core]\n\texcludesFile = {hide}\n")
    repo = _owner_repo(tmp_path / "r", home, {"m.py": "x = 1\n"})

    assert cr.tracked_files(_runner(repo, home)) == {"m.py"}


def test_the_default_global_ignore_file_still_applies(tmp_path: Path) -> None:
    home = _home(tmp_path)
    xdg = home / ".config" / "git"
    xdg.mkdir(parents=True)
    (xdg / "ignore").write_text("local-only.py\n")
    repo = _owner_repo(tmp_path / "r", home, {"m.py": "x = 1\n"})
    fx.commit(repo, {"local-only.py": "y = 2\n"}, home=home, author=fx.owner(), force=True)

    assert cr.tracked_files(_runner(repo, home)) == {"m.py"}


# --- every exec-bearing key in repository config -------------------------------------------


def test_no_command_named_in_repository_config_runs(tmp_path: Path) -> None:
    """Every key the module docstring lists, set in ``.git/config`` (and through an include)."""
    home = _home(tmp_path)
    payload, fired = _payload(tmp_path)
    repo = _owner_repo(tmp_path / "r", home, {"src/m.py": "x = 1\n", "README.md": "# r\n"})
    fx.commit(repo, {"src/m.py": "x = 2\n", ".gitattributes": "* diff=evil filter=evil\n"},
              home=home, author=fx.owner())
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "pre-commit", "post-checkout", "reference-transaction"):
        (hooks / hook).symlink_to(payload)
    included = tmp_path / "included.gitconfig"
    included.write_text(f"[core]\n\tfsmonitor = {payload}\n")
    settings = {
        "core.fsmonitor": str(payload),
        "core.pager": str(payload),
        "core.editor": str(payload),
        "core.sshCommand": str(payload),
        "core.askPass": str(payload),
        "core.hooksPath": str(hooks),
        "diff.external": str(payload),
        "diff.evil.textconv": str(payload),
        "diff.evil.command": str(payload),
        "filter.evil.clean": str(payload),
        "filter.evil.smudge": str(payload),
        "filter.evil.process": str(payload),
        "gpg.program": str(payload),
        "log.showSignature": "true",
        "credential.helper": str(payload),
        "remote.origin.url": f"file://{tmp_path / 'nowhere'}",
        "remote.origin.uploadpack": str(payload),
        "include.path": str(included),
    }
    for key, value in settings.items():
        fx.git(repo, "config", key, value, home=home)

    tree = cr.find_work_tree(repo, runner=_runner(repo, home))
    owned = cr.owned_files(tree, runner=_runner(tree.root, home), emails=[fx.OWNER_EMAIL],
                           since=SINCE)

    assert owned == [".gitattributes", "README.md", "src/m.py"]
    assert list(fired.iterdir()) == []
