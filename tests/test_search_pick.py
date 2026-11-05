"""Pure logic of ``brain search --pick`` (RM-52): lines, argv, parsing, probes, child env.

All data is synthetic. Every guard here has a "refuses" and a "keeps ordinary behaviour" test.
"""
from __future__ import annotations

import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brain.search import SearchResult
from brain.search_pick import (
    MIN_FZF_VERSION,
    NO_FZF_MESSAGE,
    NO_TERMINAL_MESSAGE,
    OLD_FZF_MESSAGE,
    FzfRun,
    child_env,
    fzf_argv,
    has_terminal,
    installed_fzf_version,
    interpret_run,
    parse_selection,
    pick_refusal,
    result_lines,
    run_fzf,
)

pytestmark = pytest.mark.nodb

ID_A = "0a1b2c3d-1111-4222-8333-444455556666"
ID_B = "9f8e7d6c-1111-4222-8333-444455556666"


def _hit(document_id: str, title: str, source: str | None = "gmail") -> SearchResult:
    return SearchResult(
        document_id=document_id,
        title=title,
        source_kind=source,
        snippet="SNIPPET-MUST-NOT-REACH-FZF",
        score=1.0,
        content_type="note",
        tags=[],
    )


# --- result_lines -----------------------------------------------------------


def test_a_line_is_id_title_source_separated_by_tabs() -> None:
    assert result_lines([_hit(ID_A, "Quarterly plan", "krisp")]) == (
        f"{ID_A}\tQuarterly plan\tkrisp\n"
    )


def test_a_missing_source_reads_manual_as_brain_show_does() -> None:
    assert result_lines([_hit(ID_A, "Note", None)]) == f"{ID_A}\tNote\tmanual\n"


@pytest.mark.parametrize(
    "hostile",
    [
        "Before\tAfter",
        "Before\nAfter",
        "Before\r\nAfter",
        "Before After",
        "Before After",
        "Before\x85After",
    ],
)
def test_a_separator_in_a_title_cannot_add_a_field_or_a_line(hostile: str) -> None:
    lines = result_lines([_hit(ID_A, hostile), _hit(ID_B, "Plain")]).splitlines()
    assert len(lines) == 2
    assert lines[0].split("\t") == [ID_A, "Before After", "gmail"]


@pytest.mark.parametrize(
    ("hostile", "clean"),
    [
        ("Hi\x1b[31mred\x1b[0m", "Hired"),
        ("Hi\x1b]0;PWNED\x07there", "Hithere"),
        ("Hi\x1b]8;;http://example.invalid\x1b\\there", "Hithere"),
        ("Hi\x9b31mred", "Hi31mred"),
        ("Hi\x1b(Bthere", "Hithere"),
        ("Hi\x07\x08\x7fthere", "Hithere"),
        ("Hi‮reversed‬", "Hireversed"),
        ("Hi⁦isolated⁩", "Hiisolated"),
    ],
)
def test_control_ansi_and_bidi_characters_never_reach_fzf(hostile: str, clean: str) -> None:
    assert result_lines([_hit(ID_A, hostile)]) == f"{ID_A}\t{clean}\tgmail\n"


def test_a_hostile_source_cannot_add_a_field() -> None:
    assert result_lines([_hit(ID_A, "T", "gm\tail\x1b[1m")]) == f"{ID_A}\tT\tgm ail\n"


@pytest.mark.parametrize(
    "title",
    ["Résumé draft", "季度计划", "Planning 🚀 notes", "A title with  spaces".replace("  ", " ")],
)
def test_ordinary_titles_survive(title: str) -> None:
    assert result_lines([_hit(ID_A, title)]) == f"{ID_A}\t{title}\tgmail\n"


@pytest.mark.parametrize("empty", ["", "   ", "\x1b[0m", "\t\n"])
def test_a_title_with_nothing_left_reads_untitled(empty: str) -> None:
    assert result_lines([_hit(ID_A, empty)]) == f"{ID_A}\t(untitled)\tgmail\n"


def test_a_long_title_is_capped_and_a_short_one_is_not() -> None:
    capped = result_lines([_hit(ID_A, "x" * 5000)]).rstrip("\n").split("\t")[1]
    assert capped == "x" * 200
    assert result_lines([_hit(ID_A, "x" * 200)]).rstrip("\n").split("\t")[1] == "x" * 200


def test_only_id_title_and_source_reach_fzf() -> None:
    assert "SNIPPET" not in result_lines([_hit(ID_A, "T")])


# --- fzf_argv ---------------------------------------------------------------


def test_argv_hides_the_id_and_previews_by_field_index_only() -> None:
    argv = fzf_argv("/fake/fzf")
    assert argv == [
        "/fake/fzf",
        "--delimiter",
        "\t",
        "--with-nth",
        "2..",
        "--no-multi",
        "--preview",
        f"brain show {{1}} 2>&1 | {shlex.quote(sys.executable)} -I -m brain.preview_filter",
    ]


# --- parse_selection --------------------------------------------------------


def test_a_real_line_returns_its_id() -> None:
    assert parse_selection(f"{ID_B}\tTitle\tkrisp\n", {ID_A, ID_B}) == ID_B


def test_a_leading_blank_line_does_not_hide_the_selection() -> None:
    assert parse_selection(f"\n{ID_B}\tTitle\tkrisp\n", {ID_A, ID_B}) == ID_B


@pytest.mark.parametrize("stdout", [f"{ID_B}\tTitle\tkrisp", f"{ID_B}\n", ID_B])
def test_one_line_with_or_without_a_newline_or_fields_returns_its_id(stdout: str) -> None:
    assert parse_selection(stdout, {ID_A, ID_B}) == ID_B


@pytest.mark.parametrize(
    "stdout",
    [
        f"junk\n{ID_B}\tTitle\n",
        f"junk\x0b{ID_A}\tTitle\n",
        f"{ID_A}\tTitle\n{ID_B}\tTitle\n",
    ],
    ids=["junk-first", "vertical-tab", "two-offered-lines"],
)
def test_more_than_one_line_fails_closed(stdout: str) -> None:
    assert parse_selection(stdout, {ID_A, ID_B}) is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "\n",
        "not-an-offered-id\tTitle\tkrisp\n",
        f"{ID_B}x\tTitle\n",
        f"x{ID_B}\tTitle\n",
        f"{ID_B[:8]}\n",
    ],
)
def test_anything_not_offered_fails_closed(stdout: str) -> None:
    assert parse_selection(stdout, {ID_A, ID_B}) is None


# --- interpret_run ----------------------------------------------------------


def test_a_selection_is_exit_zero_with_no_message() -> None:
    outcome = interpret_run(FzfRun(0, f"{ID_A}\tT\tgmail\n"), {ID_A})
    assert (outcome.document_id, outcome.exit_code, outcome.message) == (ID_A, 0, None)


@pytest.mark.parametrize(
    ("run", "exit_code", "message"),
    [
        (FzfRun(130, ""), 130, None),
        (FzfRun(1, ""), 1, "nothing picked"),
        (FzfRun(2, "ignored"), 1, "fzf failed (exit 2)"),
        (FzfRun(-9, ""), 1, "fzf failed (exit -9)"),
        (FzfRun(None, ""), 1, "fzf could not start"),
        (FzfRun(0, "unoffered\tT\tgmail\n"), 1, "fzf returned a line that was not offered"),
    ],
)
def test_everything_else_picks_nothing(run: FzfRun, exit_code: int, message: str | None) -> None:
    outcome = interpret_run(run, {ID_A})
    assert (outcome.document_id, outcome.exit_code, outcome.message) == (None, exit_code, message)


# --- has_terminal -----------------------------------------------------------


class _Stream:
    def __init__(self, *, tty: bool = True, raises: type[Exception] | None = None) -> None:
        self._tty, self._raises = tty, raises

    def isatty(self) -> bool:
        if self._raises is not None:
            raise self._raises
        return self._tty


def test_a_terminal_on_stderr_with_a_controlling_tty_is_accepted() -> None:
    assert has_terminal(stderr=_Stream(), open_tty=lambda: None) is True


def test_stderr_that_is_not_a_terminal_is_refused() -> None:
    assert has_terminal(stderr=_Stream(tty=False), open_tty=lambda: None) is False


def test_a_closed_stderr_is_refused() -> None:
    assert has_terminal(stderr=_Stream(raises=ValueError), open_tty=lambda: None) is False


def test_no_controlling_terminal_is_refused() -> None:
    def no_tty() -> None:
        raise OSError("no controlling terminal")

    assert has_terminal(stderr=_Stream(), open_tty=no_tty) is False


# --- pick_refusal -----------------------------------------------------------


def _version(found: tuple[int, int] | None) -> object:
    return lambda _binary: found


def test_no_terminal_is_named_first_even_without_fzf() -> None:
    assert (
        pick_refusal(terminal=lambda: False, which=lambda _n: None, version=_version(None))
        == NO_TERMINAL_MESSAGE
    )


def test_a_missing_fzf_gets_the_install_hint() -> None:
    message = pick_refusal(terminal=lambda: True, which=lambda _n: None, version=_version(None))
    assert message == NO_FZF_MESSAGE
    assert "install" in message


def test_an_fzf_below_the_floor_is_refused() -> None:
    below = (MIN_FZF_VERSION[0], MIN_FZF_VERSION[1] - 1)
    assert (
        pick_refusal(terminal=lambda: True, which=lambda _n: "/f", version=_version(below))
        == OLD_FZF_MESSAGE
    )


@pytest.mark.parametrize("found", [MIN_FZF_VERSION, (0, 72), (1, 0), None])
def test_the_floor_itself_newer_and_unknown_versions_proceed(
    found: tuple[int, int] | None,
) -> None:
    assert (
        pick_refusal(terminal=lambda: True, which=lambda _n: "/f", version=_version(found)) is None
    )


# --- real subprocesses against a small script --------------------------------


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fzf"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("0.72.0 (Homebrew)", (0, 72)),
        ("0.44.1 (debian)", (0, 44)),
        ("0.29.0 (dc975e8)", (0, 29)),
        ("1.2", (1, 2)),
        ("not a version", None),
        ("", None),
    ],
)
def test_the_version_is_read_from_the_binary(
    tmp_path: Path, printed: str, expected: tuple[int, int] | None
) -> None:
    assert installed_fzf_version(_script(tmp_path, f"echo '{printed}'")) == expected


def test_a_version_probe_that_cannot_run_is_unknown(tmp_path: Path) -> None:
    assert installed_fzf_version(str(tmp_path / "absent")) is None


def test_a_version_probe_that_hangs_is_unknown_and_bounded(tmp_path: Path) -> None:
    assert installed_fzf_version(_script(tmp_path, "exec sleep 30"), timeout=0.2) is None


def test_run_fzf_feeds_stdin_and_returns_stdout_and_the_exit_code(tmp_path: Path) -> None:
    binary = _script(tmp_path, 'cat; printf "%s\\n" "$#"; exit 7')
    run = run_fzf([binary, "--one", "--two"], f"{ID_A}\tRésumé\tgmail\n")
    assert run == FzfRun(7, f"{ID_A}\tRésumé\tgmail\n2\n")


def test_run_fzf_never_captures_stderr_where_fzf_draws(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    binary = _script(tmp_path, "echo drawn-on-stderr >&2")
    run_fzf([binary], "")
    assert "drawn-on-stderr" in capfd.readouterr().err


def test_run_fzf_reports_a_binary_that_cannot_start(tmp_path: Path) -> None:
    assert run_fzf([str(tmp_path / "absent")], "") == FzfRun(None, "")


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.01)


def test_an_interrupt_stops_fzf_with_sigterm_so_it_can_restore_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready, stopped = tmp_path / "ready", tmp_path / "stopped"
    binary = _script(
        tmp_path,
        f'trap \'touch "{stopped}"; exit 0\' TERM\ntouch "{ready}"\n'
        "while :; do sleep 0.02; done",
    )

    def interrupted(*_args: object, **_kwargs: object) -> None:
        _wait_for(ready)
        raise KeyboardInterrupt

    monkeypatch.setattr("brain.search_pick.subprocess.Popen.communicate", interrupted)
    assert run_fzf([binary], "") == FzfRun(130, "")
    assert stopped.exists(), "SIGTERM was not what stopped it"


def test_an_fzf_that_ignores_sigterm_is_killed_after_a_bounded_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"
    binary = _script(tmp_path, f'trap "" TERM\ntouch "{ready}"\nexec sleep 60')
    seen: list[subprocess.Popen[bytes]] = []

    def interrupted(self: subprocess.Popen[bytes], *_args: object, **_kwargs: object) -> None:
        seen.append(self)
        _wait_for(ready)
        raise KeyboardInterrupt

    monkeypatch.setattr("brain.search_pick.subprocess.Popen.communicate", interrupted)
    monkeypatch.setattr("brain.search_pick._STOP_SECONDS", 0.2)
    started = time.monotonic()
    assert run_fzf([binary], "") == FzfRun(130, "")
    assert time.monotonic() - started < 10, "the wait was not bounded"
    assert seen[0].poll() is not None, "fzf outlived the bounded wait"


def test_run_fzf_clears_every_fzf_variable_from_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FZF_DEFAULT_OPTS", "--bind=start:accept")
    monkeypatch.setenv("FZF_DEFAULT_OPTS_FILE", "/tmp/hostile")
    monkeypatch.setenv("FZF_DEFAULT_COMMAND", "hostile-command")
    monkeypatch.setenv("FZF_API_KEY", "hostile-key")
    monkeypatch.setenv("BRAIN_PICK_TEST_MARKER", "kept")
    run = run_fzf([_script(tmp_path, "env")], "")
    assert "FZF_" not in run.stdout
    assert "BRAIN_PICK_TEST_MARKER=kept" in run.stdout


def test_child_env_forces_a_posix_shell_because_fzf_runs_the_preview_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    assert child_env()["SHELL"] == "/bin/sh"


def test_child_env_keeps_ordinary_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/synthetic/bin")
    monkeypatch.setenv("NOT_FZF_OPTS", "kept")
    env = child_env()
    assert env["PATH"] == "/synthetic/bin"
    assert env["NOT_FZF_OPTS"] == "kept"
