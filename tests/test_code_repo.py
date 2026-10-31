"""Unit tests for :mod:`brain.ingest.code_repo` (RM-43): the runner and the file selection.

The selection tests run the real ``git`` against synthetic repositories built in
``tmp_path`` (``tests/code_repo_fixtures.py``), with a ``HOME`` inside
``tmp_path``. The runner's error paths use tiny fake binaries written into
``tmp_path`` and passed through the ``which`` seam. No real repository is read.
"""
from __future__ import annotations

import functools
import os
from datetime import date
from pathlib import Path

import pytest

from brain.ingest import code_repo as cr
from tests import code_repo_fixtures as fx

pytestmark = pytest.mark.nodb


def _runner(repo: Path, home: Path) -> cr.GitRunner:
    return functools.partial(cr.git_runner, repo=repo, base_env={"HOME": str(home)})


def _fake_binary(tmp_path: Path, body: str, *, executable: bool = True) -> str:
    path = tmp_path / "fake-git"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return str(path)


# --- the runner --------------------------------------------------------------------------


def test_the_runner_hardens_argv_and_passes_a_minimal_env(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "printf '%s\\n' \"$@\"; echo ---; /usr/bin/env")

    out = cr.git_runner(
        ["ls-files"],
        repo=tmp_path / "r",
        which=lambda _name: binary,
        base_env={"HOME": "/synthetic/home", "SENTINEL_VAR": "leak", "PATH": "/evil"},
    ).decode()
    argv_text, env_text = out.split("---\n")

    assert argv_text.splitlines() == [
        "--no-pager", "-C", str(tmp_path / "r"),
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "core.quotePath=false", "-c", "log.showSignature=false",
        "-c", "protocol.allow=never", "-c", "credential.helper=", "ls-files",
    ]
    env = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
    assert env["HOME"] == "/synthetic/home"
    assert env["PATH"] == os.defpath
    assert env["LC_ALL"] == "C"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert "SENTINEL_VAR" not in env


def test_the_env_omits_home_when_there_is_none() -> None:
    assert "HOME" not in cr.git_env({})
    assert "HOME" not in cr.git_env({"HOME": ""})


def test_the_default_env_is_the_process_env(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", "/synthetic/default-home")
    binary = _fake_binary(tmp_path, "/usr/bin/env")

    out = cr.git_runner(["status"], repo=tmp_path, which=lambda _name: binary).decode()

    assert "HOME=/synthetic/default-home" in out.splitlines()


def test_no_arguments_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cr.CodeRepoError, match="no arguments"):
        cr.git_runner([], repo=tmp_path)


def test_a_missing_git_gives_the_install_hint(tmp_path: Path) -> None:
    with pytest.raises(cr.GitNotFoundError, match="git not found on PATH"):
        cr.git_runner(["status"], repo=tmp_path, which=lambda _name: None)


def test_the_default_lookup_is_shutil_which(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    monkeypatch.setattr("brain.ingest.code_repo.shutil.which", lambda _name: None)

    with pytest.raises(cr.CodeRepoError, match="git not found on PATH"):
        cr.git_runner(["status"], repo=tmp_path)


def test_a_failing_git_names_the_subcommand_and_exit_but_never_its_output(
    tmp_path: Path,
) -> None:
    binary = _fake_binary(tmp_path, "echo SYNTHETIC-OUTPUT; echo SYNTHETIC-ERR >&2; exit 3")

    with pytest.raises(cr.CodeRepoError) as caught:
        cr.git_runner(["log"], repo=tmp_path, which=lambda _name: binary)

    assert str(caught.value) == "`git log` failed (exit 3)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_timeout_is_reported(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "sleep 5")

    with pytest.raises(cr.CodeRepoError, match=r"`git log` timed out after 0.2s"):
        cr.git_runner(["log"], repo=tmp_path, which=lambda _name: binary, timeout=0.2)


def test_an_oversized_answer_is_refused(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "yes SYNTHETIC | head -c 4096")

    with pytest.raises(cr.CodeRepoError, match="output exceeded 16 bytes") as caught:
        cr.git_runner(["log"], repo=tmp_path, which=lambda _name: binary, max_stdout_bytes=16)

    assert "SYNTHETIC" not in str(caught.value)


def test_a_binary_that_cannot_start_is_reported_by_class(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "exit 0", executable=False)

    with pytest.raises(cr.CodeRepoError, match=r"`git log` could not run \(PermissionError\)"):
        cr.git_runner(["log"], repo=tmp_path, which=lambda _name: binary)


# --- addresses ---------------------------------------------------------------------------


@pytest.mark.parametrize("email", ["owner@example.test", "a.b+c@sub.example.test"])
def test_valid_addresses(email: str) -> None:
    assert cr.is_valid_author_email(email)


@pytest.mark.parametrize(
    "email", ["", "owner", "a@b@c", "<owner@example.test>", "own er@example.test", "@x", "x@"]
)
def test_invalid_addresses(email: str) -> None:
    assert not cr.is_valid_author_email(email)


# --- the work tree -----------------------------------------------------------------------


def test_the_top_level_has_an_empty_prefix(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)

    tree = cr.find_work_tree(repo, runner=_runner(repo, fx.home_of(tmp_path)))

    assert tree == cr.WorkTree(root=repo.resolve(), prefix="", scope=repo.resolve())
    assert tree.name == "sample-repo"


def test_a_subdirectory_gives_its_prefix(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)

    tree = cr.find_work_tree(repo / "src", runner=_runner(repo / "src", fx.home_of(tmp_path)))

    assert tree == cr.WorkTree(root=repo.resolve(), prefix="src/", scope=(repo / "src").resolve())


def test_a_missing_git_keeps_its_install_hint_through_the_work_tree_check(
    tmp_path: Path,
) -> None:
    """Regression: the hint was reworded to "not inside a git work tree"."""
    def no_git(_args: list[str]) -> bytes:
        return cr.git_runner(_args, repo=tmp_path, which=lambda _name: None)

    with pytest.raises(cr.GitNotFoundError, match="git not found on PATH"):
        cr.find_work_tree(tmp_path, runner=no_git)


def test_a_plain_directory_is_not_a_work_tree(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(cr.CodeRepoError, match="not inside a git work tree"):
        cr.find_work_tree(plain, runner=_runner(plain, tmp_path))


def test_the_git_directory_and_a_bare_repository_are_refused(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)
    home = fx.home_of(tmp_path)
    bare = tmp_path / "bare.git"
    fx.git(tmp_path, "init", "-q", "--bare", str(bare), home=home)

    for directory in (repo / ".git", bare):
        with pytest.raises(cr.CodeRepoError, match="not inside a git work tree"):
            cr.find_work_tree(directory, runner=_runner(directory, home))


def test_a_missing_directory_is_refused_without_running_git(tmp_path: Path) -> None:
    def runner(_args: list[str]) -> bytes:
        pytest.fail("git must not run for a missing directory")

    with pytest.raises(cr.CodeRepoError, match="gone: not a directory"):
        cr.find_work_tree(tmp_path / "gone", runner=runner)


def test_an_unusable_rev_parse_answer_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cr.CodeRepoError, match="unusable"):
        cr.find_work_tree(tmp_path, runner=lambda _args: b"\n\n")
    with pytest.raises(cr.CodeRepoError, match="unusable"):
        cr.find_work_tree(tmp_path, runner=lambda _args: b"/only-one-line")


# --- the file selection ------------------------------------------------------------------


def test_tracked_files_drop_force_added_ignored_files_and_never_list_untracked(
    tmp_path: Path,
) -> None:
    repo = fx.build_repo(tmp_path)

    tracked = cr.tracked_files(_runner(repo, fx.home_of(tmp_path)))

    assert fx.IGNORED_FORCED not in tracked  # tracked, but .gitignore matches it
    assert fx.IGNORED_UNTRACKED not in tracked
    assert {*fx.OWNER_KEPT, fx.OTHER_PATH, fx.SYMLINK, ".gitignore"} <= tracked


def test_owner_touched_files_match_the_whole_address_case_insensitively(
    tmp_path: Path,
) -> None:
    repo = fx.build_repo(tmp_path)
    runner = _runner(repo, fx.home_of(tmp_path))

    touched = cr.owner_touched_files(
        runner, emails=[fx.OWNER_EMAIL.upper()], since=date.fromisoformat(fx.SINCE)
    )

    assert set(fx.OWNER_KEPT) <= touched
    assert fx.OTHER_PATH not in touched
    assert "src/lookalike.py" not in touched  # coowner@ contains owner@
    assert fx.OLD_PATH not in touched  # committed before --since
    assert ".gitignore" not in touched


def test_several_addresses_are_ored(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)

    touched = cr.owner_touched_files(
        _runner(repo, fx.home_of(tmp_path)),
        emails=[fx.OWNER_EMAIL, fx.OTHER_EMAIL],
        since=date.fromisoformat(fx.SINCE),
    )

    assert {fx.OTHER_PATH, *fx.OWNER_KEPT} <= touched


def test_an_address_is_matched_literally_not_as_a_pattern(tmp_path: Path) -> None:
    """``--fixed-strings``: the ``.`` in ``a.b@`` matches only a dot, never ``axb@``."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    fx.git(repo, "init", "-q", home=home)
    fx.commit(repo, {"mine.py": "x = 1\n"}, home=home, author=("Dot Owner", "a.b@example.test"))
    fx.commit(repo, {"theirs.py": "y = 2\n"}, home=home,
              author=("Other Person", "axb@example.test"))

    touched = cr.owner_touched_files(
        _runner(repo, home), emails=["a.b@example.test"], since=date.fromisoformat(fx.SINCE)
    )

    assert touched == {"mine.py"}


def test_since_is_local_midnight_not_the_current_time_of_day(tmp_path: Path) -> None:
    """A commit early on the --since day is inside the window, whatever time it is now."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    fx.git(repo, "init", "-q", home=home)
    fx.commit(repo, {"early.py": "x = 1\n"}, home=home, author=fx.owner(),
              date="2026-06-01T00:00:01")

    touched = cr.owner_touched_files(
        _runner(repo, home), emails=[fx.OWNER_EMAIL], since=date(2026, 6, 1)
    )

    assert touched == {"early.py"}


def test_a_repository_with_no_commit_yet_owns_nothing(tmp_path: Path) -> None:
    """rev-113: files staged, no ``HEAD``; the log cannot answer, so the answer is ``[]``."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    fx.git(repo, "init", "-q", home=home)
    fx.write(repo, "a.py", "x = 1\n")
    fx.git(repo, "add", "a.py", home=home)
    runner = _runner(repo, home)

    assert not cr.has_head(runner)
    assert cr.owned_files(cr.WorkTree(repo, "", repo), runner=runner,
                          emails=[fx.OWNER_EMAIL], since=date.fromisoformat(fx.SINCE)) == []


def test_has_head_is_true_once_there_is_a_commit(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)

    assert cr.has_head(_runner(repo, fx.home_of(tmp_path)))


def test_has_head_raises_any_other_failure() -> None:
    def broken(_args: list[str]) -> bytes:
        raise cr.GitExitError("`git rev-parse` failed (exit 128)", returncode=128)

    with pytest.raises(cr.GitExitError, match="exit 128"):
        cr.has_head(broken)


def test_an_address_list_is_required() -> None:
    with pytest.raises(cr.CodeRepoError, match="at least one author"):
        cr.owner_touched_files(lambda _a: b"", emails=[], since=date(2026, 1, 1))


def test_owned_files_is_the_sorted_intersection_under_the_prefix(tmp_path: Path) -> None:
    repo = fx.build_repo(tmp_path)
    runner = _runner(repo, fx.home_of(tmp_path))
    since = date.fromisoformat(fx.SINCE)

    whole = cr.owned_files(cr.WorkTree(repo, "", repo), runner=runner, emails=[fx.OWNER_EMAIL],
                           since=since)
    src = cr.owned_files(cr.WorkTree(repo, "src/", repo / "src"), runner=runner,
                         emails=[fx.OWNER_EMAIL], since=since)

    assert whole == sorted(whole)
    assert set(fx.OWNER_KEPT) | {fx.SECRET_PATH, fx.SYMLINK} <= set(whole)
    assert fx.OTHER_PATH not in whole
    assert src == ["src/link.py", "src/settings.py", "src/widget.py"]


def test_an_empty_repository_has_no_owned_files_and_reads_no_log(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "empty"
    repo.mkdir()
    fx.git(repo, "init", "-q", home=home)

    owned = cr.owned_files(cr.WorkTree(repo, "", repo), runner=_runner(repo, home),
                           emails=[fx.OWNER_EMAIL], since=date(2026, 1, 1))

    assert owned == []
