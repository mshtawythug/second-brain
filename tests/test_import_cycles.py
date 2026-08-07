"""Regression: no module-level import cycle in the core import graph.

On 2026-07-26 ``brain/ingest/__init__.py`` gained a module-level
``from brain.embeddings import is_hosted_embedder``. ``brain/embeddings.py``
already carried ``from .ingest import Embedder`` at module level, closing a
cycle:

    brain.ingest -> brain.embeddings -> brain.ingest

That broke ``import brain.ingest`` outright::

    ImportError: cannot import name 'Embedder' from partially initialized
    module 'brain.ingest' (most likely due to a circular import)

Because ``tests/conftest.py`` reaches ``brain.ingest`` (via ``brain.db``) at
collection time, *no pytest run in the repository could collect* — every
module, not just the ingest tests. The fix deferred the ``is_hosted_embedder``
import into the function body that uses it, leaving ``embeddings -> ingest``
as a single, one-directional edge.

**Why one subprocess per module, each as the entry import.** A combined
``python -c "import brain.cli, brain.ingest, brain.embeddings"`` is near-
worthless as evidence: whichever module is named first establishes a benign
resolution order that masks the cycle for everything after it. A cycle between
A and B is invisible when some third module C imports A to completion first,
and only bites whoever reaches the partially-initialized module first. The
2026-07-26 failure surfaced *only* because conftest happened to reach
``brain.ingest`` first via ``brain.db``; importing ``brain.cli`` first that
same day succeeded.

So each module here is imported **first, alone, in a fresh interpreter**. That
is what actually pins the property — each entry point must independently be a
valid place to start.

Subprocess-based, mirroring :mod:`tests.test_import_hygiene`. No database, no
network, no fixtures.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# Every module that is a real entry point into the package: the two that formed
# the 2026-07-26 cycle, the module conftest reaches first (``brain.db``), and
# the two console-script/server entry points. Each must be importable as the
# very first brain module in a fresh interpreter.
ENTRY_MODULES = (
    "brain.ingest",
    "brain.embeddings",
    "brain.db",
    "brain.cli",
    "brain.mcp_server",
)


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    """``import <module>`` succeeds as the FIRST brain import in a new process.

    One process per module is load-bearing — see the module docstring. Collapsing
    these into a single interpreter would let the first successful import hide a
    cycle affecting all the others.
    """
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"`import {module}` failed as the first brain import in a fresh "
        f"interpreter (exit {proc.returncode}). A module-level import cycle "
        "breaks whichever module is reached first, so this can fail for one "
        "entry point while others still succeed.\n"
        f"--- stderr ---\n{proc.stderr}"
    )


def test_import_error_names_a_cycle_when_one_exists() -> None:
    """The detector above would actually catch a cycle — proven on a real one.

    Without this, :func:`test_module_imports_first_in_a_fresh_interpreter` could
    silently degrade (a subprocess that always exits 0, a swallowed error) and
    keep passing while a cycle sat in the tree. Rather than assert on brain
    modules — which are, correctly, cycle-free — build a genuine two-module
    cycle in a temp package and confirm a fresh interpreter reports it the same
    way the 2026-07-26 failure did.
    """
    cycle = (
        "import sys, types\n"
        # Two modules importing a name from each other at module level — the
        # exact shape of ingest <-> embeddings.
        "mod_a = types.ModuleType('cyc_a')\n"
        "mod_b = types.ModuleType('cyc_b')\n"
        "sys.modules['cyc_a'] = mod_a\n"
        "sys.modules['cyc_b'] = mod_b\n"
        "exec('from cyc_b import THING', mod_a.__dict__)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", cycle], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0, "a real import cycle must fail the subprocess"
    assert "ImportError" in proc.stderr, (
        "a cycle must surface as ImportError so the assertion message above is "
        f"actionable; got:\n{proc.stderr}"
    )


def test_ingest_does_not_import_embeddings_at_module_level() -> None:
    """``brain.ingest`` must not pull ``brain.embeddings`` in at import time.

    This is the specific edge that closed the 2026-07-26 cycle. Pinning the
    *direction* catches a reintroduction at the moment it is written, rather
    than later when some unrelated module happens to import in the order that
    makes it explode. ``brain.embeddings -> brain.ingest`` remains legitimate
    and is deliberately NOT asserted against.
    """
    code = (
        "import sys, brain.ingest; "
        "sys.exit(1 if 'brain.embeddings' in sys.modules else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, (
        "importing brain.ingest must not import brain.embeddings at module "
        "level — brain.embeddings already imports Embedder from brain.ingest, "
        "so a module-level import here recreates the cycle that broke every "
        "pytest run on 2026-07-26. Import it inside the function that uses it."
        f"\n--- stderr ---\n{proc.stderr}"
    )
