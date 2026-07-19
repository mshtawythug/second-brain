"""Helper that wraps every `docker compose` invocation with the correct flags.

Centralising this prevents call sites from drifting — every invocation MUST
use both ``-f <BRAIN_HOME>/docker-compose.yml`` AND ``--project-name`` (default
``brain``).  Without ``--project-name``, the project name defaults to cwd
basename and shells from different directories can target different compose
projects against the same compose file.
"""
import os
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
