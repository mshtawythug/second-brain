"""Enable ``python -m brain [command]`` invocation of the brain CLI.

This module is the standard entry-point for Python's ``-m`` flag:

    python -m brain init
    python -m brain doctor
    python -m brain search "..."

It delegates directly to the Typer app in :mod:`brain.cli`, which
processes ``sys.argv`` and dispatches to the appropriate subcommand.
``brain setup`` uses this path (via ``sys.executable``) to invoke ``brain
init`` and ``brain doctor`` inside the same virtualenv that is running the
installer, without relying on the ``brain`` console-script being on ``PATH``.
"""

from brain.cli import app

app()
