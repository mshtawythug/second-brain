"""Helper that wraps every `docker compose` invocation with the correct flags.

Centralising this prevents call sites from drifting — every invocation MUST
use both ``-f <BRAIN_HOME>/docker-compose.yml`` AND ``--project-name`` (default
``brain``).  Without ``--project-name``, the project name defaults to cwd
basename and shells from different directories can target different compose
projects against the same compose file. :func:`compose_failure` words a failed
invocation for the user (F17).
"""
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from .config import _brain_home_root

# Default compose project name.  Overridable via ``BRAIN_COMPOSE_PROJECT`` so a
# second stack (QA, a throwaway ``BRAIN_HOME``) targets an isolated project +
# container name instead of colliding with the real ``brain`` stack.
DEFAULT_COMPOSE_PROJECT = "brain"


def compose_project_name() -> str:
    """Return the active compose project name (``$BRAIN_COMPOSE_PROJECT`` or ``brain``).

    Single source of truth so :func:`compose_cmd` and the setup-time compose
    render (``container_name``) stay in lock-step — a non-default project must
    both switch ``--project-name`` and derive a non-colliding container name.
    """
    return os.environ.get("BRAIN_COMPOSE_PROJECT") or DEFAULT_COMPOSE_PROJECT


def postgres_container_name(project: str | None = None) -> str:
    """Derive the postgres container name for a compose project.

    The DEFAULT project (``brain``) keeps the exact historical literal
    ``second-brain-postgres`` for backward compatibility — existing users
    re-running ``brain setup`` (a documented upgrade flow) must not have their
    container recreated under a new name. A non-default project (the D5b QA
    isolation seam) derives ``<project>-postgres`` so it can't collide.

    ``project`` defaults to :func:`compose_project_name`, so callers that just
    want "the container this brain talks to" — ``brain backup`` / ``brain
    restore``, which run ``pg_dump`` / ``pg_restore`` inside it — need not
    resolve the project themselves. ``brain setup`` passes the project
    explicitly because it renders the name into a compose file before that
    project is necessarily the active one.
    """
    resolved = project if project is not None else compose_project_name()
    if resolved == DEFAULT_COMPOSE_PROJECT:
        return "second-brain-postgres"
    return f"{resolved}-postgres"


def compose_cmd(*args: str, brain_home: Path | None = None) -> list[str]:
    """Build a docker-compose argv list with mandatory -f and --project-name flags.

    Resolves ``$BRAIN_HOME`` via :func:`brain.config._brain_home_root` — so the
    compose file path is consistent with the rest of the runtime.  The optional
    ``brain_home`` kwarg overrides the resolved path and is the recommended seam
    for tests: pass ``tmp_path / ".brain"`` rather than monkeypatching env vars.

    ``--project-name`` comes from :func:`compose_project_name` so the default
    stays ``brain`` for real users while ``BRAIN_COMPOSE_PROJECT`` isolates a
    parallel stack.
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    compose_file = home / "docker-compose.yml"
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--project-name",
        compose_project_name(),
        *args,
    ]


#: How many trailing stderr lines :func:`compose_failure` quotes.
FAILURE_TAIL_LINES = 10


def compose_failure(
    argv: Sequence[str], returncode: int, stderr: str | None, *, pg_port: int
) -> str:
    """The message for a ``docker compose`` call that exited non-zero (F17, 0.4.0 QA).

    Names the exit code, quotes the last :data:`FAILURE_TAIL_LINES` non-empty lines of
    stderr (control characters removed: it is docker's text, printed to a terminal), and
    ends with what to check and the command to re-run by hand for the full output.
    """
    # Lazy: ``brain.ingest`` is heavy, and only a failure needs it here.
    from .ingest.text_safety import one_line

    lines = [one_line(line) for line in (stderr or "").splitlines()]
    tail = [line for line in lines if line][-FAILURE_TAIL_LINES:] or ["(no stderr)"]
    quoted = "\n".join(f"    {line}" for line in tail)
    return (
        f"docker compose failed (exit {returncode}). The last lines of its stderr:\n"
        f"{quoted}\n"
        f"Is Docker running, and is port {pg_port} free? Fix that, then re-run "
        f"`brain setup`. For the full output, run it by hand:\n    {shlex.join(argv)}"
    )
