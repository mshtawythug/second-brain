"""Baseline tests for T3.1-T3.4 brain setup scaffold.

Three cases:
    1. dry_run=True — no filesystem side-effects, no subprocess.run called.
    2. --reset with wrong confirmation — aborts; existing data untouched.
    3. preflight fails when docker missing — exits non-zero with docker +
       Remediation in output.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit_code(exc: BaseException) -> int:
    """Extract the exit code from a typer.Exit or SystemExit."""
    if isinstance(exc, typer.Exit):
        return exc.exit_code
    # SystemExit
    code = getattr(exc, "code", 1)
    return code if isinstance(code, int) else 1


# ---------------------------------------------------------------------------
# Test 1 — dry-run produces no filesystem side-effects and no subprocess calls
# ---------------------------------------------------------------------------


def test_setup_dry_run_no_side_effects(tmp_path: Path) -> None:
    """dry_run=True must not write any files and must not invoke subprocess.run."""
    brain_home = tmp_path / ".brain"

    subprocess_calls: list[Any] = []

    def _fake_run(*args: Any, **kwargs: Any) -> MagicMock:
        subprocess_calls.append(args)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", side_effect=_fake_run),
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            # Use port 0 so the socket.bind probe always succeeds regardless of
            # whether the developer's local Postgres is already running on 5433.
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    # No directories or files should have been created under tmp.
    assert not brain_home.exists(), (
        "dry_run=True must not create brain_home or any subdirectories"
    )

    # No subprocess.run calls should have been made (preflight docker check
    # is skipped in dry_run, and all T3.4 startup steps are guarded by
    # _perform_action which no-ops in dry_run mode).
    assert subprocess_calls == [], (
        f"subprocess.run was called unexpectedly: {subprocess_calls}"
    )


# ---------------------------------------------------------------------------
# Test 2 — --reset with wrong confirmation leaves data intact
# ---------------------------------------------------------------------------


def test_setup_reset_requires_typed_confirmation(tmp_path: Path) -> None:
    """--reset with the wrong confirmation phrase must abort without deleting data."""
    brain_home = tmp_path / ".brain"
    pg_marker = brain_home / "data" / "postgres" / "marker"
    pg_marker.parent.mkdir(parents=True)
    pg_marker.write_text("precious postgres data")

    with (
        pytest.raises((typer.Exit, SystemExit)) as exc_info,
        patch("typer.prompt", return_value="no thanks"),
    ):
        from brain.setup import run_setup

        run_setup(
            reset=True,
            non_interactive=True,
            brain_home_override=brain_home,
            skip_wiki=True,
            skip_skill=True,
        )

    # Must exit non-zero.
    assert _exit_code(exc_info.value) != 0, (
        "setup with wrong reset confirmation must exit non-zero"
    )

    # Precious data must still exist — nothing was deleted.
    assert pg_marker.exists(), (
        "pg_marker was unexpectedly deleted despite wrong confirmation"
    )


# ---------------------------------------------------------------------------
# Test 3 — preflight fails with clear error when docker is missing
# ---------------------------------------------------------------------------


def test_setup_preflight_fails_when_docker_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preflight must exit non-zero and mention 'docker' + 'Remediation' when docker absent."""
    brain_home = tmp_path / ".brain"

    def _fake_which(name: str) -> str | None:
        if name == "docker":
            return None  # simulate docker missing
        return f"/usr/bin/{name}"

    with (
        pytest.raises((typer.Exit, SystemExit)) as exc_info,
        patch("shutil.which", side_effect=_fake_which),
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            # Use port 0 so the socket.bind probe always succeeds (OS assigns
            # an ephemeral port; there is no risk of collision with real Postgres).
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    # Must exit non-zero.
    assert _exit_code(exc_info.value) != 0, (
        "setup must exit non-zero when docker preflight fails"
    )

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()

    assert "docker" in combined, (
        "output must mention 'docker' so the user knows which check failed"
    )
    assert "remediation" in combined, (
        "output must contain a Remediation hint"
    )


# ---------------------------------------------------------------------------
# Test 4 — --vault is consumed: written into fresh .env and appended to existing
# ---------------------------------------------------------------------------


def _run_through_state_creation(
    brain_home: Path,
    vault_override: Path,
    *,
    pre_create_env: bool = False,
) -> None:
    """Helper: run setup past T3.3 with all external calls mocked out."""
    if pre_create_env:
        # Write a minimal existing .env (no BRAIN_VAULT_PATH line).
        brain_home.mkdir(parents=True, exist_ok=True)
        (brain_home / ".env").write_text("DATABASE_URL=postgresql://x\n")

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),  # skip real shim install
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=vault_override,
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )


def test_setup_vault_override_written_into_fresh_env(tmp_path: Path) -> None:
    """--vault must appear as BRAIN_VAULT_PATH=... in a freshly rendered .env."""
    brain_home = tmp_path / ".brain"
    vault = tmp_path / "my-vault"

    _run_through_state_creation(brain_home, vault)

    env_text = (brain_home / ".env").read_text()
    assert f"BRAIN_VAULT_PATH={vault}" in env_text, (
        f"BRAIN_VAULT_PATH={vault} not found in rendered .env:\n{env_text}"
    )
    # Commented-out placeholder must be gone (replaced by the live value).
    assert "# BRAIN_VAULT_PATH=" not in env_text


def test_setup_vault_override_appended_to_existing_env(tmp_path: Path) -> None:
    """--vault must be appended to an existing .env that lacks BRAIN_VAULT_PATH."""
    brain_home = tmp_path / ".brain"
    vault = tmp_path / "my-vault"

    _run_through_state_creation(brain_home, vault, pre_create_env=True)

    env_text = (brain_home / ".env").read_text()
    assert f"BRAIN_VAULT_PATH={vault}" in env_text, (
        f"BRAIN_VAULT_PATH={vault} not appended to existing .env:\n{env_text}"
    )


# ---------------------------------------------------------------------------
# T3.9 tests — T3.5-T3.8 behaviour
# ---------------------------------------------------------------------------


def _dry_run_output(
    tmp_path: Path,
    *,
    profile: str = "standard",
    skip_wiki: bool = True,
    skip_skill: bool = True,
    vault_override: Path | None = None,
    pg_port: int = 0,
    wiki_port: int = 0,
    which_returns: str | None = "/usr/bin/fake",
) -> str:
    """Run setup in dry-run + non-interactive mode; return stdout as a string.

    Uses redirect_stdout so typer.echo() output is captured directly without
    patching click internals (which are already bound at import time).
    Stderr (typer.secho(..., err=True)) is NOT captured — tests that need stderr
    should use capsys directly instead of this helper.

    Both pg_port and wiki_port default to 0 so the socket.bind probe always
    succeeds regardless of what's running on the developer's machine.
    """
    brain_home = tmp_path / ".brain"
    _vault = vault_override or (tmp_path / "vault")
    buf = io.StringIO()

    with (
        patch("shutil.which", return_value=which_returns),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        redirect_stdout(buf),
    ):
        from brain.setup import run_setup

        run_setup(
            profile=profile,
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=_vault,
            pg_port=pg_port,
            wiki_port=wiki_port,
            skip_wiki=skip_wiki,
            skip_skill=skip_skill,
        )

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test 6 — idempotent second run (dry-run, no state changes)
# ---------------------------------------------------------------------------


def test_setup_idempotent_second_run(tmp_path: Path) -> None:
    """Calling run_setup twice with dry_run=True must not error and produce matching output."""
    vault = tmp_path / "vault"

    def _one_run() -> str:
        return _dry_run_output(tmp_path / "run", vault_override=vault)

    out1 = _one_run()
    out2 = _one_run()

    # Neither call should raise.  If they both returned, they didn't error.
    # The output doesn't have to be byte-for-byte identical (e.g. paths could
    # differ on tmp_path), but both must contain the closing 🧠 banner line.
    assert "brain setup complete" in out1, f"first run missing banner:\n{out1}"
    assert "brain setup complete" in out2, f"second run missing banner:\n{out2}"


# ---------------------------------------------------------------------------
# Test 7 — caddy preflight only fails when wiki is enabled
# ---------------------------------------------------------------------------


def test_setup_preflight_caddy_missing_only_when_wiki_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """caddy check must be skipped with --skip-wiki and fail without it."""
    brain_home = tmp_path / ".brain"

    def _which_no_caddy(name: str) -> str | None:
        if name == "caddy":
            return None
        return f"/usr/bin/{name}"

    # --- skip_wiki=True: caddy check is bypassed; setup succeeds ---
    # (caddy is a full-profile-only preflight, so exercise the full profile.)
    with (
        patch("shutil.which", side_effect=_which_no_caddy),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
    ):
        from brain.setup import run_setup

        # Should NOT raise
        run_setup(
            profile="full",
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=tmp_path / "vault",
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    # --- skip_wiki=False: caddy check runs and fails ---
    with (
        pytest.raises((typer.Exit, SystemExit)) as exc_info,
        patch("shutil.which", side_effect=_which_no_caddy),
    ):
        run_setup(
            profile="full",
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=tmp_path / "vault",
            pg_port=0,
            wiki_port=0,
            skip_wiki=False,
            skip_skill=True,
        )

    assert _exit_code(exc_info.value) != 0, (
        "setup must exit non-zero when caddy is missing and --skip-wiki is not set"
    )
    captured = capsys.readouterr()
    assert "caddy" in (captured.out + captured.err).lower(), (
        "output must mention 'caddy' when the caddy check fails"
    )
    assert "remediation" in (captured.out + captured.err).lower(), (
        "output must include remediation hint for missing caddy"
    )


# ---------------------------------------------------------------------------
# Test 8 — launchd gating by --skip-wiki
# ---------------------------------------------------------------------------


def test_setup_launchd_skipped_when_skip_wiki(tmp_path: Path) -> None:
    """Full profile: --skip-wiki suppresses launchd; daemons stay opt-in otherwise.

    launchd is a full-profile-only component; standard/minimal never reach it.
    ``_dry_run_output`` runs --non-interactive, and daemons are opt-in only, so
    neither run may install them.
    """
    # skip_wiki=True → wiki not installed → launchd skipped
    out_skipped = _dry_run_output(tmp_path / "skip", profile="full", skip_wiki=True)
    assert "launchd" in out_skipped.lower(), (
        "expected launchd mention when --skip-wiki"
    )
    assert "wiki not installed" in out_skipped or "wiki" in out_skipped.lower(), (
        "expected wiki-not-installed rationale in launchd skip message"
    )

    # skip_wiki=False + --non-interactive → daemons NEVER auto-install.
    out_enabled = _dry_run_output(tmp_path / "enabled", profile="full", skip_wiki=False)
    assert "launchd" in out_enabled.lower() or "daemons" in out_enabled.lower(), (
        "expected launchd/daemons mention when wiki is enabled"
    )
    # The dry-run *install* line must be absent — daemons are opt-in only. (The
    # skip message legitimately mentions `brain-install-launchd` as the opt-in
    # command, so assert on the "would:" install action specifically.)
    assert "would: brain-install-launchd" not in out_enabled, (
        "--non-interactive must NEVER install launchd daemons"
    )
    if sys.platform != "darwin":
        assert "not macos" in out_enabled.lower(), (
            "expected 'not macOS' skip message on Linux"
        )


def test_setup_noninteractive_never_installs_launchd(tmp_path: Path) -> None:
    """D1d: --non-interactive (full profile) must NOT invoke install_plists.

    Regression for the default-on bug: previously wiki auto-installed in
    --non-interactive mode and launchd registration rode along unconditionally.
    """
    from brain.setup import PreflightResult

    install_plists_calls: list[Any] = []

    def _quartz_ok(*, dry_run: bool = False) -> PreflightResult:
        return PreflightResult(name="quartz-sha", ok=True, message="ok (patched)")

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
        # Avoid the real `git ls-remote` network call in the full preflight.
        patch("brain.setup._check_quartz_sha", side_effect=_quartz_ok),
        patch("brain.wiki.install.wiki_install"),
        patch("brain.cli_claude.install_skill"),
        patch(
            "brain.bin.launchd.install_plists",
            side_effect=lambda *a, **k: install_plists_calls.append((a, k)),
        ),
    ):
        from brain.setup import run_setup

        run_setup(
            profile="full",
            dry_run=False,
            non_interactive=True,
            brain_home_override=tmp_path / ".brain",
            vault_override=tmp_path / "vault",
            pg_port=0,
            wiki_port=0,
            skip_wiki=False,
            skip_skill=True,
        )

    assert install_plists_calls == [], (
        "install_plists must NOT run in --non-interactive mode"
    )


def test_setup_daemons_flag_installs_when_interactive(tmp_path: Path) -> None:
    """--daemons pre-answers the opt-in prompt so launchd installs (interactive)."""
    install_plists_calls: list[Any] = []

    with (
        patch("brain.bin.launchd.install_plists",
              side_effect=lambda *a, **k: install_plists_calls.append((a, k))),
        patch("sys.platform", "darwin"),
    ):
        from brain.setup import _maybe_install_launchd

        _maybe_install_launchd(
            wiki_installed=True,
            vault_path=tmp_path / "vault",
            brain_home=tmp_path / ".brain",
            dry_run=False,
            non_interactive=False,
            daemons=True,
        )

    assert len(install_plists_calls) == 1, (
        "--daemons must install launchd without prompting"
    )


def test_setup_launchd_interactive_default_no(tmp_path: Path) -> None:
    """Interactively, the launchd prompt defaults to No — declining skips install."""
    install_plists_calls: list[Any] = []

    with (
        patch("typer.confirm", return_value=False),  # user accepts the No default
        patch("brain.bin.launchd.install_plists",
              side_effect=lambda *a, **k: install_plists_calls.append((a, k))),
        patch("sys.platform", "darwin"),
    ):
        from brain.setup import _maybe_install_launchd

        _maybe_install_launchd(
            wiki_installed=True,
            vault_path=tmp_path / "vault",
            brain_home=tmp_path / ".brain",
            dry_run=False,
            non_interactive=False,
            daemons=False,
        )

    assert install_plists_calls == [], (
        "declining the default-No launchd prompt must skip install"
    )


# ---------------------------------------------------------------------------
# Test 9 — --skip-skill prevents skill install
# ---------------------------------------------------------------------------


def test_setup_skill_install_skipped_via_flag(tmp_path: Path) -> None:
    """--skip-skill must print the skip message and never call install_skill."""
    install_skill_calls: list[Any] = []

    with patch(
        "brain.cli_claude.install_skill",
        side_effect=lambda **kw: install_skill_calls.append(kw),
    ):
        out = _dry_run_output(tmp_path, skip_skill=True, skip_wiki=True)

    assert "skip" in out.lower() and "skill" in out.lower(), (
        f"expected skill-skip message in output:\n{out}"
    )
    assert install_skill_calls == [], (
        "install_skill must not be called when --skip-skill is set"
    )


# ---------------------------------------------------------------------------
# Test 10 — brain init failure aborts with SetupError
# ---------------------------------------------------------------------------


def test_setup_init_failure_aborts(tmp_path: Path) -> None:
    """If brain init exits non-zero, run_setup must raise SetupError."""
    from brain.setup import SetupError

    brain_home = tmp_path / ".brain"

    def _smart_subprocess_run(*args: Any, **kwargs: Any) -> MagicMock:
        cmd = args[0] if args else kwargs.get("args", [])
        m = MagicMock()
        # Detect the `brain init` invocation: [sys.executable, "-m", "brain", "init"]
        is_brain_init = (
            isinstance(cmd, list)
            and len(cmd) >= 4
            and cmd[-1] == "init"
            and "-m" in cmd
            and "brain" in cmd
        )
        m.returncode = 1 if is_brain_init else 0
        m.stdout = ""
        m.stderr = ""
        return m

    with (
        pytest.raises(SetupError, match="brain init failed"),
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", side_effect=_smart_subprocess_run),
        patch("brain.setup.ensure_shim"),
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=tmp_path / "vault",
            # embedder_choice=None → defaults to arctic; ollama pull is also
            # intercepted by _smart_subprocess_run (returns 0).
            # pg_port=0 makes the socket.bind probe always succeed.
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )


# ---------------------------------------------------------------------------
# Regression: Bug 1 — launchd must pass the resolved vault_path, not re-derive
# ---------------------------------------------------------------------------


def test_launchd_receives_resolved_vault_path(tmp_path: Path) -> None:
    """Regression: install_plists must receive the caller-supplied vault_path.

    Before the fix, _maybe_install_launchd called install_main() which
    re-resolved brain_home from the env and hardcoded ~/brain-vault as the
    vault path.  A --vault override was silently discarded, so launchd would
    supervise the wrong vault.
    """
    from brain.setup import _maybe_install_launchd

    custom_vault = tmp_path / "custom-vault"
    brain_home = tmp_path / ".brain"
    captured: dict[str, object] = {}

    def _fake_install_plists(
        bh: Path,
        launchd_dir: Path,
        launchctl: str = "launchctl",
        *,
        vault_path: Path | None = None,
    ) -> None:
        captured["vault_path"] = vault_path
        captured["brain_home"] = bh

    with (
        patch("brain.bin.launchd.install_plists", side_effect=_fake_install_plists),
        patch("sys.platform", "darwin"),
    ):
        _maybe_install_launchd(
            wiki_installed=True,
            vault_path=custom_vault,
            brain_home=brain_home,
            dry_run=False,
            # daemons=True pre-answers the now-default-No opt-in prompt.
            daemons=True,
        )

    assert captured.get("vault_path") == custom_vault, (
        f"install_plists must receive vault_path={custom_vault}, "
        f"got {captured.get('vault_path')}"
    )
    assert captured.get("brain_home") == brain_home, (
        f"install_plists must receive brain_home={brain_home}, "
        f"got {captured.get('brain_home')}"
    )


# ---------------------------------------------------------------------------
# Task 2.4 — brain setup must render the chosen --port into the generated .env
# ---------------------------------------------------------------------------


def _packaged_env_template() -> str:
    """Read the shipped env.example template the same way run_setup does."""
    from importlib.resources import files as resource_files

    return (resource_files("brain.templates") / "env.example").read_text(
        encoding="utf-8"
    )


def test_render_env_substitutes_default_port() -> None:
    """The default 55432 must land in DATABASE_URL with no placeholder left."""
    from brain.setup import render_env_from_template

    out = render_env_from_template(
        _packaged_env_template(), pg_port=55432, vault_path=None
    )

    assert "localhost:55432/second_brain" in out, (
        f"rendered .env missing the 55432 DATABASE_URL:\n{out}"
    )
    assert "{{ pg_port }}" not in out, "placeholder left unrendered in .env"
    # TEST_DATABASE_URL must be untouched (still the 5434 AGE test instance).
    assert "localhost:5434/second_brain_test" in out, (
        "TEST_DATABASE_URL was unexpectedly rewritten by the port substitution"
    )


def test_render_env_nondefault_port_replaces_dead_url() -> None:
    """Regression (Task 2.4): a non-default --port must produce a LIVE .env.

    Before the fix, setup rendered the chosen port into docker-compose.yml but
    copied env.example verbatim, so the generated DATABASE_URL kept the
    template's default port and was dead whenever --port differed.
    """
    from brain.setup import render_env_from_template

    out = render_env_from_template(
        _packaged_env_template(), pg_port=5599, vault_path=None
    )

    assert "localhost:5599/second_brain" in out, (
        f"chosen --port 5599 did not reach DATABASE_URL:\n{out}"
    )
    # The historical dead default must be gone — that was the bug.
    assert "localhost:5433/second_brain" not in out, (
        "stale port 5433 survived into the generated .env"
    )


def test_render_env_activates_vault_path(tmp_path: Path) -> None:
    """A vault override must activate the commented-out BRAIN_VAULT_PATH line."""
    from brain.setup import render_env_from_template

    vault = tmp_path / "my-vault"
    out = render_env_from_template(
        _packaged_env_template(), pg_port=55432, vault_path=vault
    )

    assert f"BRAIN_VAULT_PATH={vault}" in out, (
        f"vault override not activated in rendered .env:\n{out}"
    )
    assert "# BRAIN_VAULT_PATH=" not in out, (
        "commented BRAIN_VAULT_PATH placeholder should be gone once activated"
    )


def test_render_env_no_vault_keeps_placeholder_commented() -> None:
    """With no vault override the BRAIN_VAULT_PATH line stays commented out."""
    from brain.setup import render_env_from_template

    out = render_env_from_template(
        _packaged_env_template(), pg_port=55432, vault_path=None
    )

    assert "# BRAIN_VAULT_PATH=" in out, (
        "BRAIN_VAULT_PATH placeholder must remain commented when no --vault given"
    )


def test_run_setup_default_port_is_canonical_55432() -> None:
    """run_setup's default --port must match the committed compose / prod port."""
    import inspect

    from brain.setup import run_setup

    default = inspect.signature(run_setup).parameters["pg_port"].default
    assert default == 55432, (
        f"run_setup pg_port default is {default}; expected 55432 (matches "
        "docker-compose.yml + prod)"
    )


def test_setup_renders_chosen_port_into_generated_env(tmp_path: Path) -> None:
    """Wiring regression: the full run_setup path writes the chosen port to .env.

    Patches _check_port_free so a real port (55432, occupied by prod on the dev
    box) never trips the preflight, then confirms _write_env actually threads the
    chosen port through render_env_from_template.
    """
    from brain.setup import PreflightResult, run_setup

    brain_home = tmp_path / ".brain"

    def _always_free(port: int, check_name: str) -> PreflightResult:
        return PreflightResult(name=check_name, ok=True, message=f"port {port} free")

    with (
        patch("brain.setup._check_port_free", side_effect=_always_free),
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
    ):
        run_setup(
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=tmp_path / "vault",
            pg_port=55432,
            skip_wiki=True,
            skip_skill=True,
        )

    env_text = (brain_home / ".env").read_text(encoding="utf-8")
    assert "localhost:55432/second_brain" in env_text, (
        f"run_setup did not render pg_port into .env DATABASE_URL:\n{env_text}"
    )
    assert "{{ pg_port }}" not in env_text, "unrendered placeholder in generated .env"


# ---------------------------------------------------------------------------
# D5b — compose project isolation seam (BRAIN_COMPOSE_PROJECT)
# ---------------------------------------------------------------------------


def _packaged_compose_template(name: str) -> str:
    """Read a shipped compose template the same way run_setup does."""
    from importlib.resources import files as resource_files

    return (resource_files("brain.templates") / name).read_text(encoding="utf-8")


def test_default_project_preserves_historical_container_name() -> None:
    """Regression: the default 'brain' project MUST keep container_name
    second-brain-postgres exactly.

    Existing users re-running `brain setup` (documented upgrade flow) would have
    their container recreated under a new name if this drifted. The D5b seam is
    for QA isolation only — the default must not change.
    """
    from brain.setup import render_compose_from_template

    for tpl in ("docker-compose.stock.yml.j2", "docker-compose.yml.j2"):
        out = render_compose_from_template(
            _packaged_compose_template(tpl),
            brain_home=Path("/tmp/bh"),
            pg_port=55432,
            compose_project="brain",
        )
        assert "container_name: second-brain-postgres" in out, (
            f"{tpl}: default project must render the historical container name"
        )
        assert "container_name: brain-postgres" not in out
        assert "name: brain" in out
        assert "{{ " not in out, f"{tpl}: unrendered placeholder left in compose"


def test_render_compose_default_project_container_name() -> None:
    """The default 'brain' project keeps the historical second-brain-postgres name."""
    from brain.setup import render_compose_from_template

    out = render_compose_from_template(
        _packaged_compose_template("docker-compose.stock.yml.j2"),
        brain_home=Path("/tmp/bh"),
        pg_port=55432,
        compose_project="brain",
    )
    assert "container_name: second-brain-postgres" in out
    assert "name: brain" in out
    assert "55432:5432" in out
    assert "{{ " not in out, "unrendered placeholder left in compose"


def test_render_compose_project_derives_noncolliding_container_name() -> None:
    """A non-default project derives a distinct container name (no prod collision)."""
    from brain.setup import render_compose_from_template

    out = render_compose_from_template(
        _packaged_compose_template("docker-compose.stock.yml.j2"),
        brain_home=Path("/tmp/bh"),
        pg_port=5599,
        compose_project="brain-qa-x",
    )
    assert "container_name: brain-qa-x-postgres" in out
    assert "container_name: second-brain-postgres" not in out
    assert "name: brain-qa-x" in out


# ---------------------------------------------------------------------------
# D5 — GHCR pull with local-image/build fallback
# ---------------------------------------------------------------------------


def test_compose_pull_failure_still_reaches_up(tmp_path: Path) -> None:
    """D5: a failed `docker compose pull` still proceeds to `docker compose up -d`."""
    calls: list[list[str]] = []

    def _run(*args: Any, **kwargs: Any) -> MagicMock:
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list):
            calls.append(cmd)
        m = MagicMock()
        # Fail ONLY the compose pull; every other subprocess call succeeds.
        is_pull = isinstance(cmd, list) and "pull" in cmd and "postgres" in cmd
        m.returncode = 1 if is_pull else 0
        m.stdout = ""
        return m

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", side_effect=_run),
        patch("brain.setup.ensure_shim"),
    ):
        from brain.setup import run_setup

        run_setup(
            profile="minimal",  # stock compose, no ollama/wiki/skill/launchd noise
            dry_run=False,
            non_interactive=True,
            brain_home_override=tmp_path / ".brain",
            vault_override=tmp_path / "vault",
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    pull_idx = next(
        (i for i, c in enumerate(calls) if "pull" in c and "postgres" in c), None
    )
    up_idx = next((i for i, c in enumerate(calls) if "up" in c and "-d" in c), None)
    assert pull_idx is not None, f"expected a `docker compose pull`; calls={calls}"
    assert up_idx is not None, f"expected a `docker compose up -d`; calls={calls}"
    assert pull_idx < up_idx, (
        f"`up -d` must run even after a failed pull; calls={calls}"
    )


# ---------------------------------------------------------------------------
# D1 — install profiles (minimal | standard | full)
# ---------------------------------------------------------------------------


def _run_profile_setup(
    tmp_path: Path,
    profile: str,
    *,
    skip_wiki: bool = True,
    skip_skill: bool = True,
) -> tuple[Path, list[Path]]:
    """Run a real (non-dry) profile setup with every external call mocked.

    Returns (brain_home, materialize_calls) so a test can assert both the
    rendered artifacts on disk and whether the AGE Dockerfile materializer ran.
    """
    brain_home = tmp_path / ".brain"
    mat_calls: list[Path] = []

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
        patch(
            "brain.setup.materialize_age_dockerfile",
            side_effect=lambda bh: mat_calls.append(bh) or (bh / "df"),
        ),
    ):
        from brain.setup import run_setup

        run_setup(
            profile=profile,
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=tmp_path / "vault",
            pg_port=0,
            skip_wiki=skip_wiki,
            skip_skill=skip_skill,
        )

    return brain_home, mat_calls


def test_minimal_preflight_omits_optional_checks() -> None:
    """D1a: minimal preflight has NO ollama/caddy/quartz/wiki-port/skills-dir checks."""
    from brain.setup import resolve_profile, run_preflight

    results = run_preflight(
        pg_port=0,
        wiki_port=0,
        embedder="none",
        profile=resolve_profile("minimal"),
        skip_wiki=False,
        skip_skill=False,
        dry_run=True,
    )
    names = {r.name for r in results}
    assert names.isdisjoint(
        {"ollama", "caddy", "quartz-sha", "wiki-port", "skills-dir"}
    ), f"minimal preflight leaked an optional check: {names}"
    # The always-on checks must still be present.
    assert {"python>=3.11", "docker", "postgres-port"} <= names


def test_minimal_renders_stock_compose_and_skips_age_materialize(tmp_path: Path) -> None:
    """D1b: minimal renders the stock pgvector compose and never materializes AGE."""
    brain_home, mat_calls = _run_profile_setup(tmp_path, "minimal")

    compose = (brain_home / "docker-compose.yml").read_text(encoding="utf-8")
    assert "pgvector/pgvector:pg16" in compose, "minimal must use the stock image"
    assert "build:" not in compose, "stock compose must have no AGE build stanza"
    assert "ghcr.io" not in compose, "stock compose must not reference the GHCR AGE image"
    assert mat_calls == [], "minimal must NOT materialize the AGE Dockerfile"
    assert not (brain_home / "docker" / "age" / "Dockerfile").exists()


def test_minimal_writes_fts_only_env(tmp_path: Path) -> None:
    """D1c: minimal writes BRAIN_EMBEDDER=none + BRAIN_GRAPH_ENABLED=false to a fresh .env."""
    brain_home, _ = _run_profile_setup(tmp_path, "minimal")

    env_text = (brain_home / ".env").read_text(encoding="utf-8")
    assert "BRAIN_EMBEDDER=none" in env_text
    assert "BRAIN_EMBEDDER=arctic" not in env_text
    # Config defaults the graph ON, so the .env MUST say false explicitly.
    assert "BRAIN_GRAPH_ENABLED=false" in env_text
    assert "# BRAIN_GRAPH_ENABLED=true" not in env_text


def test_standard_and_full_preflight_run_ollama() -> None:
    """D1f: standard + full run the ollama preflight for an Ollama-backed embedder."""
    from brain.setup import resolve_profile, run_preflight

    for name in ("standard", "full"):
        results = run_preflight(
            pg_port=0,
            wiki_port=0,
            embedder="arctic",
            profile=resolve_profile(name),
            skip_wiki=True,
            skip_skill=True,
            dry_run=True,
        )
        assert "ollama" in {r.name for r in results}, (
            f"{name} profile must run the ollama preflight"
        )


def test_full_preflight_includes_wiki_and_skill_checks() -> None:
    """D1f: full keeps today's heavy-component preflight (wiki-port/caddy/skills/quartz)."""
    from brain.setup import resolve_profile, run_preflight

    results = run_preflight(
        pg_port=0,
        wiki_port=0,
        embedder="arctic",
        profile=resolve_profile("full"),
        skip_wiki=False,
        skip_skill=False,
        dry_run=True,
    )
    names = {r.name for r in results}
    assert {"wiki-port", "caddy", "skills-dir", "quartz-sha"} <= names


def test_full_materializes_age_and_writes_graph_on_env(tmp_path: Path) -> None:
    """D1f: full renders the AGE compose, materializes the Dockerfile, graph on."""
    brain_home, mat_calls = _run_profile_setup(
        tmp_path, "full", skip_wiki=True, skip_skill=True
    )

    compose = (brain_home / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ghcr.io" in compose and "build:" in compose, "full must use the AGE compose"
    assert mat_calls == [brain_home], "full must materialize the AGE Dockerfile once"

    env_text = (brain_home / ".env").read_text(encoding="utf-8")
    assert "BRAIN_GRAPH_ENABLED=true" in env_text
    assert "BRAIN_EMBEDDER=arctic" in env_text


def test_resolve_profile_rejects_unknown() -> None:
    """An unknown --profile raises SetupError with the valid choices listed."""
    import pytest as _pytest

    from brain.setup import SetupError, resolve_profile

    with _pytest.raises(SetupError, match="unknown profile"):
        resolve_profile("gigantic")


# ---------------------------------------------------------------------------
# D4 / D1e — Ollama auto-detect fallback to FTS-only
# ---------------------------------------------------------------------------


def _which_no_ollama(name: str) -> str | None:
    return None if name == "ollama" else f"/usr/bin/{name}"


def test_autodetect_falls_back_to_none_when_ollama_missing() -> None:
    """D1e: standard + no --embedder + no ollama → resolve to none (fell_back=True)."""
    from brain.setup import resolve_effective_embedder, resolve_profile

    with patch("shutil.which", side_effect=_which_no_ollama):
        embedder, fell_back = resolve_effective_embedder(
            resolve_profile("standard"), None
        )
    assert embedder == "none"
    assert fell_back is True


def test_autodetect_keeps_arctic_when_ollama_present() -> None:
    """standard + ollama present → arctic, no fallback."""
    from brain.setup import resolve_effective_embedder, resolve_profile

    with patch("shutil.which", return_value="/usr/bin/ollama"):
        embedder, fell_back = resolve_effective_embedder(
            resolve_profile("standard"), None
        )
    assert embedder == "arctic"
    assert fell_back is False


def test_autodetect_explicit_embedder_wins() -> None:
    """An explicit --embedder is never overridden by the ollama probe."""
    from brain.setup import resolve_effective_embedder, resolve_profile

    with patch("shutil.which", return_value=None):
        embedder, fell_back = resolve_effective_embedder(
            resolve_profile("standard"), "voyage"
        )
    assert embedder == "voyage"
    assert fell_back is False


def test_autodetect_full_profile_does_not_fall_back() -> None:
    """full never auto-falls-back — it keeps arctic and hard-requires Ollama."""
    from brain.setup import resolve_effective_embedder, resolve_profile

    with patch("shutil.which", side_effect=_which_no_ollama):
        embedder, fell_back = resolve_effective_embedder(
            resolve_profile("full"), None
        )
    assert embedder == "arctic"
    assert fell_back is False


def test_autodetect_prints_hint_in_setup_narrative(tmp_path: Path) -> None:
    """D1e: a standard run with ollama missing prints the FTS-only hint + no pull."""
    buf = io.StringIO()
    with (
        patch("shutil.which", side_effect=_which_no_ollama),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        redirect_stdout(buf),
    ):
        from brain.setup import run_setup

        run_setup(
            profile="standard",
            dry_run=True,
            non_interactive=True,
            brain_home_override=tmp_path / ".brain",
            vault_override=tmp_path / "vault",
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )
    out = buf.getvalue()
    assert "Ollama not found" in out, f"expected FTS-only fallback hint:\n{out}"
    assert "BRAIN_EMBEDDER=none" in out
    assert "ollama pull" not in out.lower(), "must not pull a model when falling back"


# ---------------------------------------------------------------------------
# D6 — minimal happy-path dry-run narrative (Docker + Git + Python only)
# ---------------------------------------------------------------------------


def test_minimal_dry_run_narrative_reaches_doctor(tmp_path: Path) -> None:
    """D6: minimal previews compose + init + doctor, skips every heavy component.

    A user with only Docker + Git + Python must reach `brain doctor` (the gate
    that Wave 1's none-branch keeps green) with no Ollama, no AGE, no wiki, no
    launchd — proving the FTS-only path is self-contained.
    """
    out = _dry_run_output(tmp_path, profile="minimal", skip_wiki=True, skip_skill=True)

    # Renders a compose file, but never the AGE Dockerfile.
    assert "docker-compose.yml" in out
    assert "materialize AGE Dockerfile" not in out
    # No Ollama model pull in an FTS-only install.
    assert "ollama pull" not in out.lower()
    # Reaches DB init + the doctor gate.
    assert "brain init" in out
    assert "brain doctor" in out
    # Heavy optional components are all skipped for minimal.
    assert "[skipped] wiki install (profile minimal)" in out
    assert "[skipped] Claude Code skill (profile minimal)" in out
    assert "[skipped] launchd background daemons (profile minimal)" in out
    # Closing banner + FTS-only guidance.
    assert "brain setup complete" in out
    assert "embedder:   none" in out
    assert "Ollama not found" in out


# ---------------------------------------------------------------------------
# Regression: Bug 2 — interactive wiki decline must suppress launchd
# ---------------------------------------------------------------------------


def test_launchd_skipped_when_wiki_interactively_declined(tmp_path: Path) -> None:
    """Regression: user declining wiki via confirm must prevent launchd install.

    Before the fix, _maybe_install_launchd was gated on the --skip-wiki flag,
    not on whether the wiki was actually installed.  A user answering 'no' to
    the typer.confirm prompt would still trigger launchd registration against a
    Quartz workspace that was never created.
    """
    from brain.setup import _maybe_install_launchd, _maybe_install_wiki

    install_plists_calls: list[Any] = []

    def _record_install_plists(*args: Any, **kwargs: Any) -> None:
        install_plists_calls.append((args, kwargs))

    with (
        patch("typer.confirm", return_value=False),  # user declines wiki
        patch("brain.bin.launchd.install_plists", side_effect=_record_install_plists),
        patch("sys.platform", "darwin"),
    ):
        wiki_installed = _maybe_install_wiki(
            skip=False,
            non_interactive=False,
            dry_run=False,
            vault=tmp_path / "vault",
            brain_home=tmp_path / ".brain",
            wiki_port=8080,
        )
        assert not wiki_installed, (
            "_maybe_install_wiki must return False when the user declines"
        )

        _maybe_install_launchd(
            wiki_installed=wiki_installed,
            vault_path=tmp_path / "vault",
            brain_home=tmp_path / ".brain",
            dry_run=False,
        )

    assert install_plists_calls == [], (
        "install_plists must NOT be called when wiki was interactively declined"
    )
