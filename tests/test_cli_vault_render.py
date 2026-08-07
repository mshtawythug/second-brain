"""Tests for the ``brain vault render`` CLI command.

Quartz integration is the substance of this command, but Quartz itself
is a Node.js project — we never actually shell out to ``npx`` during a
test run. Every test mocks ``subprocess.run`` (and ``shutil.which``
where relevant) so the assertions cover the brain-side logic only:

- arg construction (``--directory``, ``--output``, cwd)
- workspace validation (vault is a directory, vault has .md files,
  Quartz workspace exists with package.json + quartz.config.ts)
- ``--no-build`` short-circuit
- exit code propagation from a non-zero npx
- friendly error when ``npx`` itself isn't on PATH
- timeout handling
- output-path resolution (vault-derived default, explicit ``--to``
  override) and the guard that refuses output dirs whose contents the
  Quartz build would ``rm -rf``

The tests use ``CliRunner`` and assert on exit codes + stdout. They
never touch the database — render is a pure file-tree operation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import _assert_render_output_safe, _resolve_render_to, app
from brain.wiki.build_swap import DEFAULT_BUILD_TIMEOUT_S


def _make_vault(tmp_path: Path, with_md: bool = True) -> Path:
    """Create a minimal vault directory.

    A real vault has many subdirectories and a frontmatter README; the
    tests only need ``.is_dir()`` to succeed and (optionally) at least
    one ``.md`` file for the ``_vault_has_markdown`` check to return
    True. We keep the scaffolding minimal so each test is explicit
    about what it's testing.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    if with_md:
        (vault / "note.md").write_text("# hello\n", encoding="utf-8")
    return vault


def _make_quartz_workspace(vault: Path) -> Path:
    """Create a ``.quartz/`` directory with the files render checks for.

    Returns the workspace path. Real Quartz workspaces have hundreds of
    files; we only need ``package.json`` and ``quartz.config.ts`` for
    ``_check_quartz_workspace`` to accept the dir.
    """
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / "quartz.config.ts").write_text(
        "// stub config\nexport default {}\n", encoding="utf-8"
    )
    return workspace


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """A fresh ``CompletedProcess`` for ``subprocess.run`` mocks."""
    return subprocess.CompletedProcess(
        args=["npx", "quartz", "build"], returncode=returncode, stdout="", stderr=""
    )


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common env wiring — DATABASE_URL is required for Config.load()."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5434/second_brain_test",
        ),
    )


# ---------------------------------------------------------------------------
# Happy path + arg construction
# ---------------------------------------------------------------------------


def test_render_invokes_npx_with_correct_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """The default invocation calls ``npx quartz build`` with the right
    flags, cwd at the Quartz workspace, and the expected output dir."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    output_dir = tmp_path / "dist"
    monkeypatch.chdir(tmp_path)  # cwd matters for `--to` resolution

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        [
            "vault",
            "render",
            "--vault",
            str(vault),
            "--to",
            str(output_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    # Single subprocess call: npx quartz build --directory <vault> --output <to>
    assert run_mock.call_count == 1
    call_args, call_kwargs = run_mock.call_args
    cmd = call_args[0]
    assert cmd[0] == "npx"
    # `--no` before the `--` separator, then the Quartz command line. See
    # test_render_never_lets_npx_fetch_from_the_registry for why this exact
    # shape matters.
    assert cmd[1] == "--no"
    assert cmd[2] == "--"
    assert cmd[3] == "quartz"
    assert cmd[4] == "build"
    assert "--directory" in cmd
    assert str(vault) in cmd
    assert "--output" in cmd
    # `--to` is resolved through expanduser+resolve before being passed.
    assert str(output_dir.resolve()) in cmd
    # cwd is the Quartz workspace, not the vault.
    assert call_kwargs["cwd"] == str(workspace)
    # check=False so we can read the returncode ourselves.
    assert call_kwargs["check"] is False
    # Build ceiling comes from the ONE shared constant, not a local one.
    # This used to pin 300 while `brain.wiki.build_swap` enforced 600 on the
    # identical operation, so a build's allowed wall-clock depended purely on
    # which entrypoint you came through. 300 was also simply wrong for a real
    # vault: four measured *successful* builds on the live 1,392-file corpus
    # took 2-5 min, i.e. this path was killing builds that were working.
    assert call_kwargs["timeout"] == DEFAULT_BUILD_TIMEOUT_S

    # Final success message points the user at the output.
    assert "rendered to" in result.output
    assert str(output_dir.resolve()) in result.output


def test_render_never_lets_npx_fetch_from_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``npx`` must be forbidden from installing, and its flags terminated.

    Bare ``npx quartz build`` falls back to FETCHING a package named "quartz"
    from the npm registry whenever npm cannot resolve one locally — and a
    workspace in that state still passes ``_check_quartz_workspace``, which
    only requires package.json + quartz.config.ts. Measured 2026-08-07: in such
    a directory ``npm exec --no -- quartz`` reports "could not determine
    executable to run", i.e. without ``--no`` the real thing would go to the
    network and build the vault with whatever it found. That is a silent
    substitution of an unpinned package for the overlaid local Quartz.

    ``--`` then ends npx's own option parsing. It matters: ``npx --no quartz
    --version`` prints *npm's* version (10.9.2), not Quartz's — npx swallowed
    the flag. With the separator, ``npx --no -- quartz --version`` correctly
    yields 4.5.2.
    """
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    cmd = run_mock.call_args[0][0]
    # No-install must come before the separator, or npx treats it as Quartz's.
    assert cmd.index("--no") < cmd.index("--")
    # Everything Quartz needs must come after the separator.
    assert cmd.index("--") < cmd.index("quartz") < cmd.index("build")
    assert cmd.index("build") < cmd.index("--directory")
    assert cmd.index("build") < cmd.index("--output")


def test_render_resolves_npx_from_path_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """The Node toolchain is looked up fresh, never pinned at install time.

    Guards against the defect class that bit this project repeatedly in one
    session: a `.env` at a path that no longer existed, a binary pinned to a
    deleted version, daemons pinned to a stale code snapshot. Passing the bare
    name ``npx`` (rather than an absolute path captured earlier) means exec
    resolves it from ``PATH`` on every invocation, so a Node upgrade or a
    version-manager switch takes effect immediately.
    """
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    launcher = run_mock.call_args[0][0][0]
    assert launcher == "npx"
    assert not Path(launcher).is_absolute()


def test_render_default_to_is_dist_under_quartz_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """With ``--to`` omitted, output goes to ``<vault>/.quartz/dist``."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    cmd = run_mock.call_args[0][0]
    # The output flag's value is derived from the vault, not the cwd.
    assert str((workspace / "dist").resolve()) in cmd
    assert str((tmp_path / "dist").resolve()) not in cmd


def test_render_uses_default_quartz_dir_under_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Without ``--quartz-dir``, render looks at ``<vault>/.quartz``."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert run_mock.call_args[1]["cwd"] == str(workspace)


def test_render_honors_explicit_quartz_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--quartz-dir`` overrides the default ``<vault>/.quartz`` location."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    # Place the workspace OUTSIDE the vault — a real-world setup where
    # the user keeps their Quartz scaffolding in a separate folder.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "package.json").write_text("{}", encoding="utf-8")
    (elsewhere / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        [
            "vault",
            "render",
            "--vault",
            str(vault),
            "--quartz-dir",
            str(elsewhere),
        ],
    )
    assert result.exit_code == 0, result.output
    assert run_mock.call_args[1]["cwd"] == str(elsewhere)


# ---------------------------------------------------------------------------
# --no-build short-circuit
# ---------------------------------------------------------------------------


def test_render_no_build_just_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-build`` exits 0 after printing OK without invoking npx."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--no-build"],
    )
    assert result.exit_code == 0, result.output
    assert run_mock.call_count == 0
    assert "OK" in result.output
    assert str(workspace) in result.output


def test_render_no_build_still_validates_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--no-build`` still runs the workspace check — that's its purpose."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    # No .quartz/ — workspace check must fail.
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--no-build"],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "Quartz workspace not found" in combined


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_render_rejects_missing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A non-existent ``--vault`` path returns exit 2 with a clear message."""
    _env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    bogus = tmp_path / "no-such-vault"
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(bogus)])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "vault path is not a directory" in combined
    assert run_mock.call_count == 0


def test_render_rejects_vault_with_no_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Empty vault (no ``.md`` files) returns exit 2 — nothing to render."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path, with_md=False)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "no .md files" in combined
    assert run_mock.call_count == 0


def test_render_rejects_missing_quartz_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """No ``.quartz/`` dir → BadParameter with the ``npx quartz create`` hint."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "Quartz workspace not found" in combined
    assert "npx quartz create" in combined
    assert run_mock.call_count == 0


def test_render_rejects_quartz_dir_missing_package_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Existing ``.quartz/`` but no package.json → tell the user exactly what's missing."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    # quartz.config.ts present, package.json absent.
    (workspace / "quartz.config.ts").write_text("// stub\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "package.json" in combined


def test_render_rejects_quartz_dir_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Existing ``.quartz/`` but no quartz.config.ts → clear error mentioning it."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = vault / ".quartz"
    workspace.mkdir()
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr if result.stderr else "")
    assert "quartz.config.ts" in combined


# ---------------------------------------------------------------------------
# Output-path resolution — the default must never come from the cwd.
#
# Regression coverage for the bug where `brain vault render`, run from an
# unrelated project directory, wrote an entire Quartz site into that
# project's source tree. The default output is now derived from the
# vault (via the Quartz workspace); only an explicit `--to` is
# interpreted relative to the shell.
# ---------------------------------------------------------------------------


def _unrelated_project(tmp_path: Path) -> Path:
    """A directory shaped like somebody else's checked-out project.

    Mirrors the real-world failure: the user ran render from a source
    repo, and the site was written into it (with `dist` not gitignored,
    staged for an accidental commit). Contents are synthetic.
    """
    project = tmp_path / "unrelated-project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "package.json").write_text('{"name": "unrelated"}\n', encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "main.ts").write_text("export const x = 1\n", encoding="utf-8")
    return project


def _tree(root: Path) -> set[Path]:
    """Every path under ``root``, relative — for before/after comparison."""
    return {p.relative_to(root) for p in root.rglob("*")}


#: Box-drawing characters Typer/Rich frames error panels with. Stripped
#: before phrase assertions so a border never lands mid-sentence.
_BOX_CHARS = "│╭╮╰╯─┃┏┓┗┛━"


def _flat_output(result: Any) -> str:
    """Combined stdout+stderr, de-boxed and whitespace-collapsed.

    Typer renders ``BadParameter`` inside a panel drawn to the terminal
    width, so the message is hard-wrapped at an unpredictable column and
    every line is fenced by ``│``. Dropping the border glyphs and
    collapsing whitespace makes a phrase assertion independent of how
    wide the runner's terminal happens to be — without it the same test
    passes at ``COLUMNS=80`` and fails at ``COLUMNS=50``.
    """
    combined = result.output + (result.stderr if result.stderr else "")
    for char in _BOX_CHARS:
        combined = combined.replace(char, " ")
    return " ".join(combined.split())


def test_render_from_unrelated_cwd_targets_vault_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Run from an unrelated project: output resolves to the vault build
    dir, and not one byte lands in that project."""
    # Setup — vault in one place, an unrelated project checkout in another.
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    project = _unrelated_project(tmp_path)
    before = _tree(project)
    monkeypatch.chdir(project)
    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    # Exercise
    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])

    # Verify — half one: the constructed argv points at the vault build dir.
    assert result.exit_code == 0, result.output
    cmd = run_mock.call_args[0][0]
    output_idx = cmd.index("--output")
    assert cmd[output_idx + 1] == str((workspace / "dist").resolve())

    # Verify — half two: the unrelated project is byte-for-byte untouched.
    # Asserting only on the argv would still pass if some stray writer
    # scattered files into the cwd on the way there.
    assert not (project / "dist").exists()
    assert _tree(project) == before


def test_render_explicit_to_override_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """An explicit absolute ``--to`` still wins over the vault default."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    workspace = _make_quartz_workspace(vault)
    target = tmp_path / "custom-site"
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--to", str(target)],
    )
    assert result.exit_code == 0, result.output

    cmd = run_mock.call_args[0][0]
    assert cmd[cmd.index("--output") + 1] == str(target.resolve())
    assert str((workspace / "dist").resolve()) not in cmd


def test_render_explicit_relative_to_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A relative ``--to`` keeps ordinary shell semantics (cwd-relative).

    Pins the deliberate asymmetry: the *default* is cwd-independent, an
    explicit path the user typed is not. ``bin/brain-wiki-gif`` relies on
    this (``cd $WORKDIR && brain vault render --to dist``).
    """
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    run_mock = mocker.patch("brain.cli.subprocess.run", return_value=_completed())

    result = CliRunner().invoke(
        app,
        ["vault", "render", "--vault", str(vault), "--to", "site"],
    )
    assert result.exit_code == 0, result.output

    cmd = run_mock.call_args[0][0]
    assert cmd[cmd.index("--output") + 1] == str((here / "site").resolve())


def test_resolve_render_to_defaults_to_supplied_dir(tmp_path: Path) -> None:
    """``to=None`` yields ``default_dir``, ignoring the cwd entirely."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    default_dir = tmp_path / "vault" / ".quartz" / "dist"
    assert (
        _resolve_render_to(None, cwd, default_dir=default_dir) == default_dir.resolve()
    )


def test_resolve_render_to_accepts_relative(tmp_path: Path) -> None:
    """A plain relative output path resolves under the cwd."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    out = _resolve_render_to(Path("dist"), cwd, default_dir=tmp_path / "unused")
    assert out == (cwd / "dist").resolve()


def test_resolve_render_to_accepts_absolute_outside_cwd(tmp_path: Path) -> None:
    """An explicit absolute path outside the cwd is honored verbatim.

    The old resolver rejected this (``--to`` had to stay under the cwd)
    while happily accepting ``--to .`` — the single most destructive
    value. The containment rule is gone; :func:`_assert_render_output_safe`
    guards the destructive cases instead.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = tmp_path / "elsewhere" / "site"
    out = _resolve_render_to(target, cwd, default_dir=tmp_path / "unused")
    assert out == target.resolve()


# ---------------------------------------------------------------------------
# Destructive-output guard
#
# Quartz rm -rf's its --output directory before emitting (see
# quartz_overrides/quartz/build.ts). Any output path that CONTAINS the
# cwd, the vault, or the Quartz workspace therefore deletes that tree.
# ---------------------------------------------------------------------------


def _anchors(tmp_path: Path) -> dict[str, Path]:
    """The three anchor directories the guard protects.

    Each lives in its own subtree so a test can point the output at the
    parent of exactly one anchor and pin which one is reported.
    """
    cwd = tmp_path / "work" / "cwd"
    vault = tmp_path / "data" / "vault"
    workspace = tmp_path / "ws" / "quartz"
    for path in (cwd, vault, workspace):
        path.mkdir(parents=True)
    return {"cwd": cwd, "vault": vault, "workspace": workspace}


@pytest.mark.parametrize(
    ("anchor_key", "expected_label"),
    [
        ("cwd", "the current working directory"),
        ("vault", "the vault"),
        ("workspace", "the Quartz workspace"),
    ],
)
def test_assert_render_output_safe_rejects_anchor_itself(
    tmp_path: Path, anchor_key: str, expected_label: str
) -> None:
    """Rendering directly into an anchor directory is refused."""
    anchors = _anchors(tmp_path)
    with pytest.raises(Exception) as excinfo:
        _assert_render_output_safe(anchors[anchor_key], **anchors)
    assert "refusing to render into" in str(excinfo.value)
    assert expected_label in str(excinfo.value)


@pytest.mark.parametrize(
    ("anchor_key", "expected_label"),
    [
        ("cwd", "the current working directory"),
        ("vault", "the vault"),
        ("workspace", "the Quartz workspace"),
    ],
)
def test_assert_render_output_safe_rejects_ancestor_of_anchor(
    tmp_path: Path, anchor_key: str, expected_label: str
) -> None:
    """An output dir that CONTAINS an anchor is refused — the build's
    ``rm -rf`` would take the anchor with it."""
    anchors = _anchors(tmp_path)
    with pytest.raises(Exception) as excinfo:
        _assert_render_output_safe(anchors[anchor_key].parent, **anchors)
    assert "refusing to render into" in str(excinfo.value)
    assert expected_label in str(excinfo.value)
    assert str(anchors[anchor_key].resolve()) in str(excinfo.value)


def test_assert_render_output_safe_allows_dedicated_dir(tmp_path: Path) -> None:
    """The default (``<workspace>/dist``) and any sibling output dir pass.

    A dir *under* an anchor is fine — only a dir that would take an
    anchor down with it is refused.
    """
    anchors = _anchors(tmp_path)
    _assert_render_output_safe(anchors["workspace"] / "dist", **anchors)
    _assert_render_output_safe(anchors["cwd"] / "site", **anchors)
    _assert_render_output_safe(tmp_path / "elsewhere" / "site", **anchors)


def test_render_refuses_to_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--to .`` (= rm -rf the working directory) is refused."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    project = _unrelated_project(tmp_path)
    before = _tree(project)
    monkeypatch.chdir(project)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app, ["vault", "render", "--vault", str(vault), "--to", "."]
    )

    assert result.exit_code != 0
    assert "refusing to render into" in _flat_output(result)
    # No build was launched, and the project is untouched.
    assert run_mock.call_count == 0
    assert _tree(project) == before


def test_render_refuses_output_containing_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``--to <parent of the vault>`` would delete the vault — refused."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("brain.cli.subprocess.run")

    result = CliRunner().invoke(
        app, ["vault", "render", "--vault", str(vault), "--to", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "refusing to render into" in _flat_output(result)
    assert run_mock.call_count == 0
    assert (vault / "note.md").exists()


# ---------------------------------------------------------------------------
# subprocess error handling
# ---------------------------------------------------------------------------


def test_render_propagates_npx_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A non-zero npx exit code causes brain to exit 1 with a clear line."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch("brain.cli.subprocess.run", return_value=_completed(returncode=2))

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "quartz build failed" in combined
    assert "exit code 2" in combined


def test_render_handles_npx_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``subprocess.run`` raising FileNotFoundError → friendly hint, exit 1."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=FileNotFoundError("npx not found"),
    )

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "npx not found" in combined
    assert "Node.js" in combined


def test_render_handles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A 5-min timeout is surfaced as exit 1 with a config-issue hint."""
    _env(monkeypatch)
    vault = _make_vault(tmp_path)
    _make_quartz_workspace(vault)
    monkeypatch.chdir(tmp_path)

    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["npx"], timeout=300),
    )

    result = CliRunner().invoke(app, ["vault", "render", "--vault", str(vault)])
    assert result.exit_code == 1
    combined = result.output + (result.stderr if result.stderr else "")
    assert "exceeded" in combined or "timed out" in combined.lower()


# ---------------------------------------------------------------------------
# Sample quartz.config.ts smoke test
# ---------------------------------------------------------------------------


def test_sample_quartz_config_exists_and_parses_minimally() -> None:
    """The sample at the repo root must exist, be valid TypeScript shape.

    We don't run a TS compiler — we just sanity-check that the file
    parses as well-formed TS by string-matching the canonical Quartz
    config landmarks (the `import { QuartzConfig }` line, a default
    export, plugin lists). If a future Quartz version renames any of
    these, this test fails loudly and prompts a refresh.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config = repo_root / "quartz.config.ts"
    assert config.is_file(), f"sample quartz.config.ts missing at {config}"
    text = config.read_text(encoding="utf-8")
    # Quartz v4 import shape.
    assert "import { QuartzConfig }" in text
    assert 'from "./quartz/cfg"' in text
    assert "import * as Plugin from" in text
    # The three plugin buckets every Quartz config carries.
    assert "transformers:" in text
    assert "filters:" in text
    assert "emitters:" in text
    # Critical brain-flagged plugins. ``Plugin.Graph()`` was previously
    # asserted here; it was removed from the template by commit 885644a
    # ("fix(quartz): drop nonexistent Plugin.Graph()") because the symbol
    # doesn't exist in stock Quartz v4.5.x. ``Plugin.GitHubFlavoredMarkdown``
    # is a stable substitute — present in the template and required for
    # the brain vault's GFM-flavored content.
    assert "Plugin.ObsidianFlavoredMarkdown" in text
    assert "Plugin.GitHubFlavoredMarkdown" in text
    assert "Plugin.ContentIndex" in text
    # Default export is what `npx quartz build` reads.
    assert "export default config" in text
