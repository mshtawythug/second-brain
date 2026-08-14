"""Load a seed-query list from a plain text file (one query per line).

Shared by the two standalone scripts that read a canned query list —
``scripts/embedding_smoke.py`` and ``scripts/token_payload_report.py``. It
lives here rather than in ``src/brain/`` deliberately: nothing the ``brain``
package ships needs it, and a production module that exists only for two
one-shot scripts is a module the package now has to keep. Per CLAUDE.md,
``scripts/`` is where standalone plumbing goes.

Importing it works two ways, both already used in this repo:

* running a script directly (``python scripts/token_payload_report.py``) puts
  ``scripts/`` on ``sys.path`` as ``sys.path[0]``, so a plain
  ``from query_files import ...`` resolves;
* a test loading a sibling script inserts ``scripts/`` into ``sys.path`` first
  — the pattern established by ``tests/test_collapse_gmail_threads.py``.
"""
from pathlib import Path


def load_query_lines(path: Path) -> list[str]:
    """Return non-blank, non-comment lines from ``path`` in order.

    Lines whose stripped form is empty or starts with ``#`` are dropped;
    everything else is returned with surrounding whitespace stripped.
    """
    queries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        queries.append(stripped)
    return queries
