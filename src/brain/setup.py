"""brain setup — interactive one-command installer for the second-brain runtime."""
import enum
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path

import typer

from ._compose import DEFAULT_COMPOSE_PROJECT, compose_cmd, compose_project_name
from .bin._launcher import ensure_shim
from .config import DEFAULT_VAULT_PATH, _brain_home_root
from .errors import BrainError
from .vault._atomic import atomic_write_text
from .wiki import QUARTZ_PINNED_COMMIT, QUARTZ_REPO_URL


class SetupError(BrainError):
    """Raised when `brain setup` cannot proceed."""


@dataclass
class PreflightResult:
    """Result of a single preflight check."""

    name: str
    ok: bool
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class SetupProfile:
    """The dependency matrix for one `brain setup --profile` choice.

    Every gating decision in :func:`run_setup` derives from this object — no
    scattered string compares on the profile name. Three profiles ship:

    * **minimal** — Postgres + FTS only. Stock compose, ``BRAIN_EMBEDDER=none``,
      graph off, no Ollama, no wiki/daemons/skill. A user with only Docker +
      Git + Python gets a working brain.
    * **standard** — + Ollama hybrid search. Stock compose,
      ``BRAIN_EMBEDDER=arctic`` (or ``--embedder``), graph off, Ollama preflight
      + pull, soft skill install (only if ``~/.claude`` exists), no wiki/daemons.
    * **full** — today's behaviour: the AGE compose + Dockerfile, graph on,
      Ollama pull, and the interactive wiki / daemons / skill offers.
    """

    name: str
    needs_ollama: bool
    wants_graph: bool
    compose_template: str
    offer_wiki: bool
    offer_daemons: bool
    offer_skill: bool
    soft_skill: bool
    env_embedder_default: str
    env_graph_enabled: bool


class ProfileName(enum.StrEnum):
    """Valid ``brain setup --profile`` choices.

    Used directly as the Typer option type so Typer renders it as a
    ``click.Choice`` — earlier validation (exit 2 on a bad value), shell
    completion, and a richer ``--help`` — with :func:`resolve_profile`'s
    ``SetupError`` as the backstop for direct ``run_setup`` callers.
    """

    minimal = "minimal"
    standard = "standard"
    full = "full"


_STOCK_COMPOSE = "docker-compose.stock.yml.j2"
_AGE_COMPOSE = "docker-compose.yml.j2"

_PROFILES: dict[str, SetupProfile] = {
    "minimal": SetupProfile(
        name="minimal",
        needs_ollama=False,
        wants_graph=False,
        compose_template=_STOCK_COMPOSE,
        offer_wiki=False,
        offer_daemons=False,
        offer_skill=False,
        soft_skill=False,
        env_embedder_default="none",
        env_graph_enabled=False,
    ),
    "standard": SetupProfile(
        name="standard",
        needs_ollama=True,
        wants_graph=False,
        compose_template=_STOCK_COMPOSE,
        offer_wiki=False,
        offer_daemons=False,
        offer_skill=True,
        soft_skill=True,
        env_embedder_default="arctic",
        env_graph_enabled=False,
    ),
    "full": SetupProfile(
        name="full",
        needs_ollama=True,
        wants_graph=True,
        compose_template=_AGE_COMPOSE,
        offer_wiki=True,
        offer_daemons=True,
        offer_skill=True,
        soft_skill=False,
        env_embedder_default="arctic",
        env_graph_enabled=True,
    ),
}

# Public tuple of valid profile names, in install-weight order (for --help + validation).
PROFILE_NAMES: tuple[str, ...] = tuple(p.value for p in ProfileName)


def resolve_profile(name: str) -> SetupProfile:
    """Return the :class:`SetupProfile` for *name*, or raise on an unknown choice."""
    try:
        return _PROFILES[name]
    except KeyError as exc:
        valid = ", ".join(PROFILE_NAMES)
        raise SetupError(
            f"unknown profile {name!r} — choose one of: {valid}"
        ) from exc


# Printed when the standard profile auto-detects a MISSING Ollama and falls back
# to the FTS-only backend (the surprise case — leads with "Ollama not found").
# Shown at fallback time and reiterated in the final report.
OLLAMA_FALLBACK_HINT = (
    "Ollama not found — installing FTS-only (BRAIN_EMBEDDER=none). To enable "
    "semantic search later: install Ollama, set BRAIN_EMBEDDER=arctic in .env, "
    "then run 'brain init' and 'brain reembed'."
)

# Printed in the final report when the brain is FTS-only BY CHOICE (minimal
# profile, or an explicit --embedder none) — a truthful variant that does NOT
# claim Ollama was missing.
FTS_ONLY_NOTE = (
    "FTS-only brain (BRAIN_EMBEDDER=none). To enable semantic search later: "
    "install Ollama, set BRAIN_EMBEDDER=arctic in .env, then run 'brain init' "
    "and 'brain reembed'."
)


def resolve_effective_embedder(
    profile: SetupProfile, embedder_choice: str | None
) -> tuple[str, bool]:
    """Resolve the embedder to use, applying the standard-profile FTS fallback.

    Returns ``(embedder, fell_back)``. An explicit ``--embedder`` always wins.
    Otherwise the profile default applies — except the **standard** profile
    falls back to the FTS-only ``none`` backend when the ``ollama`` binary is
    absent. Resolved BEFORE preflight so a standard run doesn't hard-fail on the
    very binary it was about to fall back from. Pure except for the
    ``shutil.which`` probe — no printing — so the caller controls when the hint
    is shown (once before preflight, once in the final report).
    """
    if embedder_choice is not None:
        return embedder_choice, False
    if profile.name == "standard" and shutil.which("ollama") is None:
        return "none", True
    return profile.env_embedder_default, False


# Shim names installed to $BRAIN_HOME/.shims/ (sans .sh suffix).
# Each must have a corresponding <name>.sh in brain.templates.bin/.
_SHIM_NAMES: tuple[str, ...] = (
    "brain-up",
    "brain-down",
    "brain-status",
    "_brain-watcher-fg",
    "_brain-build-fg",
)


# ---------------------------------------------------------------------------
# Individual preflight checks
# ---------------------------------------------------------------------------


def _check_python_version() -> PreflightResult:
    """Check 1: Python >= 3.11."""
    v = sys.version_info
    ok = v >= (3, 11)
    if ok:
        return PreflightResult(
            name="python>=3.11",
            ok=True,
            message=f"Python {v.major}.{v.minor}.{v.micro}",
        )
    return PreflightResult(
        name="python>=3.11",
        ok=False,
        message=f"Python 3.11+ required (got {v.major}.{v.minor})",
        remediation="Install Python 3.11+ via pyenv, brew, or the official installer at https://python.org",
    )


def _check_docker(*, dry_run: bool = False) -> PreflightResult:
    """Check 2: docker CLI present and daemon running."""
    if shutil.which("docker") is None:
        return PreflightResult(
            name="docker",
            ok=False,
            message="docker CLI not found on PATH",
            remediation=(
                "macOS: install Docker Desktop from https://docker.com/get-started "
                "or `brew install --cask docker`\n"
                "  Linux: https://docs.docker.com/engine/install/"
            ),
        )
    if dry_run:
        # Skip daemon probe in dry-run — avoid real subprocess calls.
        return PreflightResult(
            name="docker",
            ok=True,
            message="docker found (daemon check skipped in dry-run)",
        )
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok:
        return PreflightResult(name="docker", ok=True, message="docker daemon running")
    return PreflightResult(
        name="docker",
        ok=False,
        message="docker daemon not running (docker info failed)",
        remediation="Start Docker Desktop or run `sudo systemctl start docker` (Linux)",
    )


def _check_ollama() -> PreflightResult:
    """Check 3: ollama CLI present.

    Only invoked when :func:`run_preflight` has already established that the
    resolved embedder needs a local Ollama (:func:`_embedder_uses_ollama`), so
    the SaaS ``voyage`` / FTS-only ``none`` skip lives with that single owner —
    this function no longer second-guesses the gate.
    """
    ok = shutil.which("ollama") is not None
    if ok:
        return PreflightResult(name="ollama", ok=True, message="ollama found")
    return PreflightResult(
        name="ollama",
        ok=False,
        message="ollama CLI not found on PATH",
        remediation=(
            "macOS: brew install ollama && brew services start ollama\n"
            "  Linux: https://ollama.com/install"
        ),
    )


def _check_port_free(port: int, check_name: str) -> PreflightResult:
    """Check: port is not already bound (portable socket probe, no lsof)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return PreflightResult(name=check_name, ok=True, message=f"port {port} is free")
    except OSError:
        return PreflightResult(
            name=check_name,
            ok=False,
            message=f"port {port} is already in use",
            remediation=(
                f"Stop the process using port {port} or pass a different "
                f"--port / --wiki-port value"
            ),
        )


def _check_caddy() -> PreflightResult:
    """Check 6: caddy installed — NOT auto-installed."""
    ok = shutil.which("caddy") is not None
    if ok:
        return PreflightResult(name="caddy", ok=True, message="caddy found")
    return PreflightResult(
        name="caddy",
        ok=False,
        message="caddy CLI not found on PATH",
        remediation=(
            "macOS: brew install caddy\n"
            "  Linux: https://caddyserver.com/docs/install"
        ),
    )


def _check_skills_dir_writable(*, skip: bool) -> PreflightResult:
    """Check 7: ~/.claude/skills/ writable — skipped with --skip-skill."""
    if skip:
        return PreflightResult(
            name="skills-dir",
            ok=True,
            message="~/.claude/skills/ (skipped — --skip-skill)",
        )
    skills_parent = Path.home() / ".claude"
    skills_dir = skills_parent / "skills"
    parent_exists_and_writable = skills_parent.exists() and os.access(skills_parent, os.W_OK)
    dir_ok = skills_dir.exists() or parent_exists_and_writable
    ok = parent_exists_and_writable and dir_ok
    if ok:
        return PreflightResult(name="skills-dir", ok=True, message="~/.claude/skills/ writable")
    return PreflightResult(
        name="skills-dir",
        ok=False,
        message="~/.claude/skills/ not writable or ~/.claude/ missing",
        remediation="mkdir -p ~/.claude/skills && chmod u+w ~/.claude",
    )


def _check_quartz_sha(*, dry_run: bool = False) -> PreflightResult:
    """Check 8: pinned Quartz commit SHA is reachable — skipped in dry-run."""
    if dry_run:
        return PreflightResult(
            name="quartz-sha",
            ok=True,
            message="quartz SHA reachability (skipped in dry-run)",
        )
    try:
        result = subprocess.run(
            ["git", "ls-remote", QUARTZ_REPO_URL, QUARTZ_PINNED_COMMIT],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PreflightResult(
            name="quartz-sha",
            ok=False,
            message=f"git ls-remote error: {exc}",
            remediation=(
                "Ensure git is installed and you have network access to github.com"
            ),
        )
    if result.returncode != 0:
        return PreflightResult(
            name="quartz-sha",
            ok=False,
            message="git ls-remote failed (network error or git not installed)",
            remediation="Check internet connectivity and re-run",
        )
    stdout = result.stdout.strip()
    short = QUARTZ_PINNED_COMMIT[:8]
    ok = bool(stdout) and short in stdout
    if ok:
        return PreflightResult(
            name="quartz-sha",
            ok=True,
            message=f"quartz commit {short} reachable at remote",
        )
    return PreflightResult(
        name="quartz-sha",
        ok=False,
        message=f"quartz commit {short} not found at {QUARTZ_REPO_URL}",
        remediation=(
            "Check network connectivity or update QUARTZ_PINNED_COMMIT in brain.wiki.__init__"
        ),
    )


# ---------------------------------------------------------------------------
# Preflight orchestration
# ---------------------------------------------------------------------------


def _embedder_uses_ollama(embedder: str | None) -> bool:
    """True when the (resolved) embedder needs a local Ollama daemon.

    ``arctic`` and ``qwen3`` embed over Ollama; ``voyage`` is SaaS and ``none``
    is FTS-only. ``None`` defaults to ``arctic`` (the historical default).
    """
    return embedder in (None, "arctic", "qwen3")


def run_preflight(
    pg_port: int,
    wiki_port: int,
    embedder: str | None,
    profile: SetupProfile,
    skip_wiki: bool,
    skip_skill: bool,
    *,
    dry_run: bool = False,
) -> list[PreflightResult]:
    """Run the profile's preflight checks; return the result list.

    Every optional check derives from *profile* + the resolved *embedder*:

    * ollama — only when the profile needs Ollama AND the resolved embedder
      actually embeds over it (an auto-detect fallback to ``none`` drops it).
    * wiki-port / caddy / skills-dir / quartz-sha — only for the full profile
      (``offer_wiki``), which hard-requires the heavy optional components;
      standard/minimal skip them so a missing caddy/quartz/skills-dir is never
      a preflight failure. Order preserved: Python, docker, ollama, PG port,
      wiki port, caddy, skills dir, quartz SHA.
    """
    results: list[PreflightResult] = []
    results.append(_check_python_version())
    results.append(_check_docker(dry_run=dry_run))
    if profile.needs_ollama and _embedder_uses_ollama(embedder):
        results.append(_check_ollama())
    results.append(_check_port_free(pg_port, "postgres-port"))
    if profile.offer_wiki:
        if not skip_wiki:
            results.append(_check_port_free(wiki_port, "wiki-port"))
            results.append(_check_caddy())
        results.append(_check_skills_dir_writable(skip=skip_skill))
        if not skip_wiki:
            results.append(_check_quartz_sha(dry_run=dry_run))
    return results


def _print_preflight(results: list[PreflightResult]) -> None:
    """Print each preflight result; failures go to stderr with Remediation hint."""
    for r in results:
        if r.ok:
            typer.secho(f"  [ok]   {r.name}: {r.message}", fg="green")
        else:
            typer.secho(f"  [fail] {r.name}: {r.message}", fg="red", err=True)
            if r.remediation:
                typer.secho(
                    f"         Remediation: {r.remediation}", fg="yellow", err=True
                )


# ---------------------------------------------------------------------------
# Dry-run action helper
# ---------------------------------------------------------------------------


def _perform_action(label: str, fn: object, dry_run: bool) -> None:
    """Execute *fn* or, in dry-run mode, print what would happen.

    Args:
        label: Human-readable description of the action (shown in dry-run).
        fn:    Callable that performs the mutation.  Only invoked when
               ``dry_run`` is False.
        dry_run: When True, print ``[dry-run] would: <label>`` and return.
    """
    if dry_run:
        typer.echo(f"[dry-run] would: {label}")
        return
    if callable(fn):
        fn()


def materialize_age_dockerfile(brain_home: Path) -> Path:
    """Copy the packaged AGE Dockerfile into ``$BRAIN_HOME/docker/age/Dockerfile``.

    The rendered ``docker-compose.yml`` builds the custom PG16 + pgvector + AGE
    image from ``$BRAIN_HOME/docker/age``; this materializes the canonical packaged
    Dockerfile (``brain.templates/docker/age/Dockerfile``) there so the build
    context resolves on a fresh install — mirroring how the compose/env templates
    and bin shims are shipped from package data. Idempotent (overwrites in place).

    Returns the destination path.
    """
    src = resource_files("brain.templates") / "docker" / "age" / "Dockerfile"
    dest = brain_home / "docker" / "age" / "Dockerfile"
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, src.read_text(encoding="utf-8"))
    return dest


def _container_name_for_project(compose_project: str) -> str:
    """Derive the postgres container name for a compose project.

    The DEFAULT project (``brain``) keeps the exact historical literal
    ``second-brain-postgres`` for backward compatibility — existing users
    re-running ``brain setup`` (a documented upgrade flow) must not have their
    container recreated under a new name. A non-default project (the D5b QA
    isolation seam) derives ``<project>-postgres`` so it can't collide.
    """
    if compose_project == DEFAULT_COMPOSE_PROJECT:
        return "second-brain-postgres"
    return f"{compose_project}-postgres"


def render_compose_from_template(
    template_text: str,
    *,
    brain_home: Path,
    pg_port: int,
    compose_project: str,
) -> str:
    """Render a packaged ``docker-compose*.yml.j2`` into a concrete compose body.

    Substitutes the placeholders every compose template shares
    (``{{ brain_home }}``, ``{{ pg_port }}``, ``{{ compose_project }}``,
    ``{{ container_name }}``). The project name flows into the top-level
    ``name:``; the container name is derived so the default ``brain`` project
    keeps the historical ``second-brain-postgres`` while a non-default
    ``BRAIN_COMPOSE_PROJECT`` gets an isolated, non-colliding name. Pure
    function — unit-testable in isolation.
    """
    return (
        template_text.replace("{{ brain_home }}", str(brain_home))
        .replace("{{ pg_port }}", str(pg_port))
        .replace("{{ container_name }}", _container_name_for_project(compose_project))
        .replace("{{ compose_project }}", compose_project)
    )


def _require_replace(text: str, target: str, replacement: str) -> str:
    """Replace *target* in *text*, raising :class:`SetupError` if it is absent.

    Fails loud at the point of use (not only in CI) so a drifted template — the
    packaged marker renamed or removed — can never silently render a
    partially-substituted ``.env`` that leaves a placeholder or a stale default
    in the live config.
    """
    if target not in text:
        raise SetupError(
            f".env template missing expected marker {target!r} — refusing to "
            "render a silently-wrong .env (has the packaged env.example drifted?)"
        )
    return text.replace(target, replacement)


def render_env_from_template(
    template_text: str,
    *,
    pg_port: int,
    vault_path: Path | None,
    embedder: str | None = None,
    graph_enabled: bool | None = None,
) -> str:
    """Render the packaged ``env.example`` template into a concrete ``.env`` body.

    Substitutes the chosen Postgres host port into ``DATABASE_URL`` via the same
    ``{{ pg_port }}`` placeholder mechanism ``docker-compose.yml.j2`` uses, so a
    non-default ``--port`` produces a live ``.env`` instead of one pinned to the
    template's default port (the bug this fixes: the compose file honoured
    ``--port`` but the ``.env`` was copied verbatim, leaving a dead
    ``DATABASE_URL`` whenever ``--port`` differed). When ``vault_path`` is given,
    the commented-out ``# BRAIN_VAULT_PATH=`` line is activated with the chosen
    vault.

    ``embedder`` and ``graph_enabled`` let a profile pin those two settings
    EXPLICITLY: minimal/standard must write ``BRAIN_GRAPH_ENABLED=false`` because
    :class:`~brain.config.Config` defaults the graph ON, and minimal writes
    ``BRAIN_EMBEDDER=none`` for an FTS-only brain. Left as ``None`` (the default)
    the template's own ``BRAIN_EMBEDDER=arctic`` line and the commented
    ``# BRAIN_GRAPH_ENABLED=true`` line are preserved untouched. Pure function —
    no filesystem or environment access — so the substitution is unit-testable
    in isolation.
    """
    text = _require_replace(template_text, "{{ pg_port }}", str(pg_port))
    if embedder is not None:
        text = _require_replace(text, "BRAIN_EMBEDDER=arctic", f"BRAIN_EMBEDDER={embedder}")
    if graph_enabled is not None:
        value = "true" if graph_enabled else "false"
        text = _require_replace(
            text, "# BRAIN_GRAPH_ENABLED=true", f"BRAIN_GRAPH_ENABLED={value}"
        )
    if vault_path is not None:
        text = _require_replace(
            text, "# BRAIN_VAULT_PATH=", f"BRAIN_VAULT_PATH={vault_path}"
        )
    return text


# ---------------------------------------------------------------------------
# T3.5 — brain init + doctor
# ---------------------------------------------------------------------------


def _run_brain_init(dry_run: bool) -> None:
    """Run `brain init` to apply migrations + reconcile chunks.embedding dim."""
    if dry_run:
        typer.echo("[dry-run] would: brain init")
        return
    typer.echo("Running brain init …")
    result = subprocess.run(
        [sys.executable, "-m", "brain", "init"],
        check=False,
    )
    if result.returncode != 0:
        raise SetupError("brain init failed — see output above")


def _run_brain_doctor(dry_run: bool) -> None:
    """Run `brain doctor` and surface non-zero exit as a setup failure."""
    if dry_run:
        typer.echo("[dry-run] would: brain doctor")
        return
    typer.echo("Running brain doctor …")
    result = subprocess.run(
        [sys.executable, "-m", "brain", "doctor"],
        check=False,
    )
    if result.returncode != 0:
        raise SetupError(
            "brain doctor reported issues — fix them and re-run `brain setup`"
        )


# ---------------------------------------------------------------------------
# T3.6 — interactive wiki + skill installs
# ---------------------------------------------------------------------------


def _maybe_install_wiki(
    skip: bool,
    non_interactive: bool,
    dry_run: bool,
    vault: Path,
    brain_home: Path,
    wiki_port: int,
) -> bool:
    """Prompt for and optionally perform the wiki sub-install.

    Returns:
        True  — wiki was installed (or dry-run would install it).
        False — skipped via flag or user declined interactively.

    The return value is forwarded to ``_maybe_install_launchd`` so the
    watcher supervisors are only registered when there is actually a wiki
    workspace to supervise.
    """
    if skip:
        typer.echo("[skipped] wiki install (--skip-wiki)")
        return False
    if non_interactive:
        do_install = True
    else:
        do_install = typer.confirm(
            "Install the wiki UI? Adds ~150 MB of node_modules.",
            default=True,
        )
    if not do_install:
        typer.echo("[skipped] wiki install (user declined)")
        return False
    if dry_run:
        typer.echo(
            f"[dry-run] would: brain wiki install --vault {vault} --port {wiki_port}"
        )
        return True  # dry-run counts as "would have installed"
    from .wiki.install import wiki_install

    wiki_install(vault=vault, port=wiki_port)
    return True


def _maybe_install_skill(
    skip: bool,
    non_interactive: bool,
    dry_run: bool,
    *,
    soft: bool = False,
) -> bool:
    """Prompt for and optionally perform the Claude Code skill sub-install.

    Returns True if the skill was installed (or dry-run would install it),
    False if skipped via flag or user declined.

    When ``soft`` is set (the standard profile), a missing ``~/.claude``
    directory is a silent skip rather than an install attempt — the user isn't
    a Claude Code user, and this is never a preflight failure. The full profile
    passes ``soft=False`` and keeps the interactive offer.
    """
    if skip:
        typer.echo("[skipped] Claude Code skill (--skip-skill)")
        return False
    if soft and not dry_run and not (Path.home() / ".claude").exists():
        typer.echo("[skipped] Claude Code skill (~/.claude not present)")
        return False
    if non_interactive:
        do_install = True
    else:
        do_install = typer.confirm(
            "Install the Claude Code skill at ~/.claude/skills/brain/SKILL.md?",
            default=True,
        )
    if not do_install:
        typer.echo("[skipped] Claude Code skill (user declined)")
        return False
    if dry_run:
        typer.echo("[dry-run] would: brain claude install-skill")
        return True
    from .cli_claude import install_skill

    install_skill()
    return True


# ---------------------------------------------------------------------------
# T3.7 — launchd install (LAST, after all prereqs pass)
# ---------------------------------------------------------------------------


def _maybe_install_launchd(
    wiki_installed: bool,
    vault_path: Path,
    brain_home: Path,
    dry_run: bool,
    *,
    non_interactive: bool = False,
    daemons: bool = False,
) -> None:
    """Install launchd plists ONLY after every prereq passed — opt-in, default No.

    Runs LAST so DB + wiki are healthy before supervisors start.

    Background daemons are OPT-IN (ticket contract):

    * ``--non-interactive`` NEVER installs daemons — a scripted install must not
      register three KeepAlive LaunchAgents behind the user's back.
    * Interactively the prompt defaults to **No**; pass ``--daemons`` to install
      without being asked.

    Guarded by ``wiki_installed`` (not the raw ``--skip-wiki`` flag) so that an
    interactive decline also suppresses the launchd registration — there would
    be no Quartz workspace for the watcher to supervise.

    Passes ``vault_path`` and ``brain_home`` directly to ``install_plists``
    rather than going through ``install_main()``, which would silently use the
    default vault and brain_home resolved from env/config at call time instead
    of the values chosen for this setup run.
    """
    if not wiki_installed:
        typer.echo("[skipped] launchd install (wiki not installed)")
        return
    if sys.platform != "darwin":
        typer.echo("[skipped] launchd install (not macOS)")
        return
    if non_interactive:
        # Never auto-install daemons in a scripted run — opt in later with
        # `brain-install-launchd` or an interactive `brain setup --daemons`.
        typer.echo(
            "[skipped] launchd background daemons "
            "(--non-interactive; opt in with `brain-install-launchd`)"
        )
        return
    if daemons:
        do_install = True
    else:
        do_install = typer.confirm(
            "Install launchd background daemons "
            "(auto-start Postgres + wiki watcher on login)?",
            default=False,
        )
    if not do_install:
        typer.echo("[skipped] launchd background daemons (declined)")
        return
    if dry_run:
        typer.echo("[dry-run] would: brain-install-launchd")
        return
    from .bin.launchd import install_plists

    launchd_dir = Path(
        os.environ.get("BRAIN_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents"
    )
    launchctl = os.environ.get("BRAIN_LAUNCHCTL") or "launchctl"
    install_plists(brain_home, launchd_dir, launchctl, vault_path=vault_path)
    typer.echo(f"  [ok] brain LaunchAgents installed in {launchd_dir}")


# ---------------------------------------------------------------------------
# T3.8 — final report
# ---------------------------------------------------------------------------


def _print_final_report(
    brain_home: Path,
    vault: Path,
    wiki_port: int,
    wiki_installed: bool,
    skill_installed: bool,
    effective_embedder: str,
    ollama_fell_back: bool,
) -> None:
    """Print the closing banner matching brain-up's visual style.

    Uses the actual install results (not the raw skip flags) so that an
    interactive decline is reflected correctly in the summary. When the brain
    ended up FTS-only (``BRAIN_EMBEDDER=none``), reiterates how to enable
    semantic search later — the exact "Ollama not found" hint only when it was
    an auto-detect fallback; a truthful "FTS-only brain" note when FTS was
    chosen (minimal profile / explicit ``--embedder none``).
    """
    typer.echo("")
    typer.echo("🧠 brain setup complete:")
    typer.echo(f"   brain_home: {brain_home}")
    typer.echo(f"   vault:      {vault}")
    typer.echo(f"   embedder:   {effective_embedder}")
    if wiki_installed:
        typer.echo(f"   wiki url:   http://localhost:{wiki_port}")
        typer.echo(f"   start wiki: caddy run --config {brain_home}/Caddyfile")
    if skill_installed:
        typer.echo("   skill:      ~/.claude/skills/brain/SKILL.md")
    if effective_embedder == "none":
        typer.echo("")
        typer.echo(f"   {OLLAMA_FALLBACK_HINT if ollama_fell_back else FTS_ONLY_NOTE}")
    typer.echo("")
    typer.echo("   next steps:")
    typer.echo("     brain ingest <path>            # ingest a file or directory")
    typer.echo('     brain search "..."             # query the corpus')
    typer.echo("     brain doctor                   # re-check health")


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


def run_setup(
    *,
    profile: str = "standard",
    non_interactive: bool = False,
    dry_run: bool = False,
    brain_home_override: Path | None = None,
    vault_override: Path | None = None,
    pg_port: int = 55432,
    wiki_port: int = 8080,
    embedder_choice: str | None = None,
    skip_wiki: bool = False,
    skip_skill: bool = False,
    daemons: bool = False,
    reset: bool = False,
) -> None:
    """Orchestrate the full setup flow.  Public surface for testing.

    **WARNING — reset is destructive.** When ``reset=True``, this function
    deletes ``$BRAIN_HOME`` and all its contents (Postgres data, logs,
    .env, shims) after explicit typed confirmation.  Even in
    ``--non-interactive`` mode the confirmation prompt cannot be bypassed;
    the only valid input is the exact string ``"yes, delete my data"``.

    """
    # ------------------------------------------------------------------
    # Resolve $BRAIN_HOME and vault path
    # ------------------------------------------------------------------
    if brain_home_override is not None:
        brain_home: Path = brain_home_override
    elif os.environ.get("BRAIN_HOME"):
        brain_home = Path(os.environ["BRAIN_HOME"]).expanduser()
    else:
        brain_home = _brain_home_root()

    # Vault: explicit override > env var > DEFAULT_VAULT_PATH.
    if vault_override is not None:
        vault_path: Path = vault_override
    elif os.environ.get("BRAIN_VAULT_PATH"):
        vault_path = Path(os.environ["BRAIN_VAULT_PATH"]).expanduser()
    else:
        vault_path = DEFAULT_VAULT_PATH

    # ------------------------------------------------------------------
    # Resolve the profile + the effective embedder BEFORE preflight so every
    # downstream gate (preflight checks, compose template, AGE materialize, .env
    # contents, ollama pull, optional installs) derives from one object.
    # ------------------------------------------------------------------
    prof = resolve_profile(profile)
    effective_embedder, ollama_fell_back = resolve_effective_embedder(
        prof, embedder_choice
    )
    if ollama_fell_back:
        typer.echo(OLLAMA_FALLBACK_HINT)

    # Warn loudly when --daemons was asked for but will be ignored, rather than
    # silently dropping the request.
    if daemons and non_interactive:
        typer.secho(
            "--daemons ignored under --non-interactive "
            "(daemons are never auto-installed in a scripted run)",
            fg="yellow",
            err=True,
        )
    elif daemons and not prof.offer_daemons:
        typer.secho(
            f"--daemons ignored (profile {prof.name} has no background daemons; "
            "use --profile full)",
            fg="yellow",
            err=True,
        )

    # ------------------------------------------------------------------
    # T3.1 — Reset handling (DESTRUCTIVE; typed confirmation required)
    # ------------------------------------------------------------------
    if reset:
        typer.secho(
            f"\nWARNING: --reset will permanently DELETE {brain_home} and all its "
            "contents, including your Postgres data.\n"
            "This action is IRREVERSIBLE.",
            fg="red",
            err=True,
        )
        # No auto-bypass: prompt even when --non-interactive is set.
        confirmation = typer.prompt('Type "yes, delete my data" to confirm')
        if confirmation != "yes, delete my data":
            typer.secho(
                'Reset aborted — confirmation did not match "yes, delete my data".',
                fg="red",
                err=True,
            )
            raise typer.Exit(code=1)
        if not dry_run:
            if brain_home.exists():
                shutil.rmtree(brain_home)
                typer.echo(f"[reset] deleted {brain_home}")
            else:
                typer.echo(f"[reset] {brain_home} did not exist; nothing to delete")
        else:
            typer.echo(f"[dry-run] would: shutil.rmtree({brain_home})")

    # ------------------------------------------------------------------
    # T3.2 — Pre-flight checks
    # ------------------------------------------------------------------
    typer.echo("\n── Preflight checks ──────────────────────────────")
    results = run_preflight(
        pg_port=pg_port,
        wiki_port=wiki_port,
        embedder=effective_embedder,
        profile=prof,
        skip_wiki=skip_wiki,
        skip_skill=skip_skill,
        dry_run=dry_run,
    )
    _print_preflight(results)
    failures = [r for r in results if not r.ok]
    if failures:
        typer.secho(
            "\nsetup aborted; fix the items above and re-run",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo("")

    # Expose BRAIN_HOME (and optionally BRAIN_VAULT_PATH) to downstream
    # subprocess calls (idempotent; dry-run prints without mutating).
    if dry_run:
        typer.echo(f"[dry-run] would: export BRAIN_HOME={brain_home}")
        if vault_path is not None:
            typer.echo(f"[dry-run] would: export BRAIN_VAULT_PATH={vault_path}")
    else:
        os.environ["BRAIN_HOME"] = str(brain_home)
        if vault_path is not None:
            os.environ["BRAIN_VAULT_PATH"] = str(vault_path)

    # ------------------------------------------------------------------
    # T3.3 — Idempotent state creation
    # ------------------------------------------------------------------
    typer.echo("── State creation ─────────────────────────────────")

    # 1. mkdir -p $BRAIN_HOME/{data/postgres,logs,bin}
    for subdir in ("data/postgres", "logs", "bin"):
        target_dir = brain_home / subdir
        _perform_action(
            f"mkdir -p {target_dir}",
            lambda d=target_dir: d.mkdir(parents=True, exist_ok=True),
            dry_run,
        )

    # 2. Install bash shims to $BRAIN_HOME/.shims/<name>
    for shim_name in _SHIM_NAMES:
        _perform_action(
            f"install shim {shim_name} → {brain_home / '.shims' / shim_name}",
            lambda n=shim_name: ensure_shim(n, brain_home),
            dry_run,
        )

    # 3. Render docker-compose.yml
    compose_dest = brain_home / "docker-compose.yml"

    def _write_compose_yml() -> None:
        template = resource_files("brain.templates") / prof.compose_template
        text = render_compose_from_template(
            template.read_text(encoding="utf-8"),
            brain_home=brain_home,
            pg_port=pg_port,
            compose_project=compose_project_name(),
        )
        compose_dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(compose_dest, text)
        typer.echo(f"  [ok] wrote {compose_dest} ({prof.compose_template})")

    _perform_action(f"render {compose_dest}", _write_compose_yml, dry_run)

    # 3b. Materialize the packaged AGE Dockerfile so the compose build context
    # ($BRAIN_HOME/docker/age) resolves before `docker compose up` runs. Only the
    # full profile's AGE compose has a build stanza; the stock (minimal/standard)
    # compose has no build context, so the Dockerfile is not materialized.
    if prof.wants_graph:
        dockerfile_dest = brain_home / "docker" / "age" / "Dockerfile"
        _perform_action(
            f"materialize AGE Dockerfile → {dockerfile_dest}",
            lambda: materialize_age_dockerfile(brain_home),
            dry_run,
        )

    # 4. Render .env — only if missing; never overwrite.
    env_dest = brain_home / ".env"
    if not dry_run and env_dest.exists():
        typer.echo(f"  [skipped] {env_dest} (already exists)")
        # If --vault was given and BRAIN_VAULT_PATH isn't already in the file,
        # append it so the running .env reflects the chosen vault location.
        if vault_path is not None:
            existing_env = env_dest.read_text(encoding="utf-8")
            if "BRAIN_VAULT_PATH" not in existing_env:
                with open(env_dest, "a", encoding="utf-8") as fh:
                    fh.write(f"\nBRAIN_VAULT_PATH={vault_path}\n")
                typer.echo(f"  [ok] BRAIN_VAULT_PATH={vault_path} appended to existing .env")
    else:

        def _write_env() -> None:
            template_src = resource_files("brain.templates") / "env.example"
            # Substitute the chosen Postgres port into DATABASE_URL (same
            # {{ pg_port }} mechanism as the compose render), activate the
            # commented-out BRAIN_VAULT_PATH line if --vault was given, and pin
            # BRAIN_EMBEDDER + BRAIN_GRAPH_ENABLED EXPLICITLY to the profile's
            # values (Config defaults the graph ON, so minimal/standard must
            # write it false).
            env_text = render_env_from_template(
                template_src.read_text(encoding="utf-8"),
                pg_port=pg_port,
                vault_path=vault_path,
                embedder=effective_embedder,
                graph_enabled=prof.env_graph_enabled,
            )
            env_dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(env_dest, env_text)
            typer.echo(f"  [ok] wrote {env_dest}")

        _perform_action(f"render {env_dest} from env.example template", _write_env, dry_run)

    # 5. Voyage API key prompt
    if effective_embedder == "voyage" and not os.environ.get("VOYAGE_API_KEY"):
        if non_interactive:
            typer.secho(
                "error: --embedder voyage requires VOYAGE_API_KEY to be set "
                "(export it in the environment before running setup)",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=1)
        if not dry_run:
            api_key = typer.prompt("Voyage API key", hide_input=True)
            with open(env_dest, "a", encoding="utf-8") as fh:
                fh.write(f"\nVOYAGE_API_KEY={api_key}\n")
            typer.echo("  [ok] VOYAGE_API_KEY appended to .env")
        else:
            typer.echo("[dry-run] would: prompt for VOYAGE_API_KEY and append to .env")

    # ------------------------------------------------------------------
    # T3.4 — Service startup
    # ------------------------------------------------------------------
    typer.echo("\n── Service startup ────────────────────────────────")

    # 1. docker compose pull (best-effort) + up -d.
    def _compose_up() -> None:
        # Best-effort prebuilt-image pull BEFORE `up -d`: activates the GHCR
        # prebuilt AGE image for full-profile users (once the package is public)
        # and refreshes the stock pgvector image otherwise. A non-zero pull (the
        # registry image is private / unavailable / offline) is swallowed with a
        # log line so `up -d` still resolves via the local image or build stanza.
        pull = subprocess.run(
            compose_cmd("pull", "postgres", brain_home=brain_home),
            check=False,
        )
        if pull.returncode != 0:
            typer.echo(
                "  [info] docker compose pull failed — falling back to the "
                "local image / build"
            )
        subprocess.run(
            compose_cmd("up", "-d", brain_home=brain_home),
            check=True,
        )

    _perform_action("docker compose pull + up -d (Postgres)", _compose_up, dry_run)

    # 2. Wait for Postgres readiness via pg_isready inside the container.
    def _wait_for_postgres() -> None:
        pg_ready_cmd = compose_cmd(
            "exec", "-T", "postgres", "pg_isready", "-U", "brain",
            brain_home=brain_home,
        )
        typer.echo("  Waiting for Postgres", nl=False)
        deadline = time.monotonic() + 30.0
        last_tick = time.monotonic()
        while time.monotonic() < deadline:
            result = subprocess.run(pg_ready_cmd, capture_output=True)
            if result.returncode == 0:
                typer.echo(" ready!")
                return
            now = time.monotonic()
            if now - last_tick >= 5.0:
                typer.echo(".", nl=False)
                last_tick = now
            time.sleep(1)
        typer.secho(
            "\n  Postgres did not become ready within 30 seconds",
            fg="red",
            err=True,
        )
        raise SetupError("Postgres startup timed out after 30 seconds")

    _perform_action("wait for Postgres (pg_isready inside container)", _wait_for_postgres, dry_run)

    # 3. ollama pull (arctic or qwen3 only; voyage is SaaS, none is FTS-only).
    # Uses the profile-resolved effective_embedder — a minimal profile or a
    # standard run that fell back to FTS-only never pulls a ~1 GB model.
    if effective_embedder in {"arctic", "qwen3"}:
        model = (
            "snowflake-arctic-embed2" if effective_embedder == "arctic" else "qwen3-embedding:8b"
        )

        def _ollama_pull() -> None:
            list_result = subprocess.run(
                ["ollama", "list"], capture_output=True, check=True, text=True
            )
            if model in list_result.stdout:
                typer.echo(f"  [skipped] {model} already present in ollama")
                return
            typer.echo(f"  Pulling {model} (this may take a few minutes)…")
            subprocess.run(["ollama", "pull", model], check=True)

        _perform_action(f"ollama pull {model}", _ollama_pull, dry_run)

    # ------------------------------------------------------------------
    # T3.5 — brain init + doctor
    # ------------------------------------------------------------------
    typer.echo("\n── Database initialisation ────────────────────────")
    _run_brain_init(dry_run)
    _run_brain_doctor(dry_run)

    # ------------------------------------------------------------------
    # T3.6 — optional sub-installs (wiki + Claude Code skill), gated by profile
    # ------------------------------------------------------------------
    typer.echo("\n── Optional components ────────────────────────────")
    if prof.offer_wiki:
        wiki_installed = _maybe_install_wiki(
            skip=skip_wiki,
            non_interactive=non_interactive,
            dry_run=dry_run,
            vault=vault_path,
            brain_home=brain_home,
            wiki_port=wiki_port,
        )
    else:
        wiki_installed = False
        typer.echo(f"[skipped] wiki install (profile {prof.name})")

    if prof.offer_skill:
        # Standard does a SOFT install (only when ~/.claude exists); full offers
        # it interactively. minimal never installs the skill.
        skill_installed = _maybe_install_skill(
            skip=skip_skill,
            non_interactive=non_interactive,
            dry_run=dry_run,
            soft=prof.soft_skill,
        )
    else:
        skill_installed = False
        typer.echo(f"[skipped] Claude Code skill (profile {prof.name})")

    # ------------------------------------------------------------------
    # T3.7 — launchd install LAST (after DB + wiki are healthy); full only
    # ------------------------------------------------------------------
    if prof.offer_daemons:
        _maybe_install_launchd(
            wiki_installed=wiki_installed,
            vault_path=vault_path,
            brain_home=brain_home,
            dry_run=dry_run,
            non_interactive=non_interactive,
            daemons=daemons,
        )
    else:
        typer.echo(f"[skipped] launchd background daemons (profile {prof.name})")

    # ------------------------------------------------------------------
    # T3.8 — final report
    # ------------------------------------------------------------------
    _print_final_report(
        brain_home=brain_home,
        vault=vault_path,
        wiki_port=wiki_port,
        wiki_installed=wiki_installed,
        skill_installed=skill_installed,
        effective_embedder=effective_embedder,
        ollama_fell_back=ollama_fell_back,
    )
