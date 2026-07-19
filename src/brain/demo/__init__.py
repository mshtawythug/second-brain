"""`brain demo` — zero-Ollama taste test over a synthetic Larkspur corpus.

Orchestration core for the demo experience: load the packaged synthetic
corpus, provision an isolated throwaway Postgres, seed it deterministically
with :class:`~brain.demo.embedder.DemoEmbedder` (no Ollama), run FTS-only
hybrid search, and tear it all down. This module owns the provision / seed /
query / status / teardown primitives; the Typer surface lives in
:mod:`brain.cli_demo`.
"""
from __future__ import annotations

import importlib.resources
import json
from typing import Any

from brain.errors import BrainError


def load_corpus() -> list[dict[str, Any]]:
    """Load the packaged synthetic corpus manifest (22 docs).

    Resolved via :func:`importlib.resources.files` so it works in both editable
    checkouts and pipx/wheel installs. Returns the raw record list exactly as
    authored in ``corpus/manifest.json``.
    """
    resource = importlib.resources.files("brain.demo") / "corpus" / "manifest.json"
    text = resource.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise BrainError("demo corpus manifest must be a JSON list of records")
    return data
