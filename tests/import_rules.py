"""Shared helper for the spec §6.5 import rule: a module's imports, as absolute names.

Imported by ``tests/test_ui_routes_graph.py``, which holds the one
parametrised §6.5 test over every phase-5 surface (``brain.ui.routes_graph``,
``brain.ui.graph_layout``, ``brain.ui.routes_related``). It used to be two
copies — one per route test module — that resolved relative imports in two
different ways; this is the one.

**Deliberately not a ``test_*`` module.** It holds no tests, so pytest never
collects it. Same shape as ``tests/ui_graph_harness.py`` and
``tests/backup_fakes.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

#: Packages a read-only UI surface must never import (spec §6.5): each one is a
#: build or maintenance path, and the UI is the reader, not the builder.
FORBIDDEN_PACKAGES = ("brain.maintenance", "brain.graph_rag", "brain.wiki")


def absolute_imports(module: ModuleType) -> set[str]:
    """Every module name ``module``'s source imports, relative imports resolved.

    Reads the file on disk (``module.__file__``) rather than the imported
    object, so a mutation written to the file is seen without a re-import. For
    ``from X import y`` both ``X`` and ``X.y`` are reported, because ``y`` may
    itself be a module.
    """
    if module.__file__ is None:
        raise ValueError(f"{module.__name__} has no source file to read")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    package_parts = module.__name__.split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = ".".join(package_parts[: len(package_parts) - node.level + 1])
                base = f"{prefix}.{node.module}" if node.module else prefix
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def forbidden_imports(module: ModuleType) -> list[str]:
    """The names in :func:`absolute_imports` that fall under a forbidden package."""
    return sorted(
        name
        for name in absolute_imports(module)
        for package in FORBIDDEN_PACKAGES
        if name == package or name.startswith(f"{package}.")
    )
