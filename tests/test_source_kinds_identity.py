"""One source-kind definition in Python (RM-07).

Every module that needs the closed set of ``sources.kind`` values binds the one
object in :mod:`brain.source_kinds`. The assertions are **identity** (``is``),
not equality: the failure mode left once a name is a re-export is somebody
restating the literal, which produces an equal-but-separate object that ``==``
passes and ``is`` catches (the same argument as ``tests/test_ui_schemas.py``).
"""
from __future__ import annotations

from pathlib import Path

from brain import cli, source_kinds
from brain.ui import schemas
from brain.vault import links

# The set as it was before RM-07, restated here on purpose: this pin is what
# shows RM-07 changed no kind.
PRE_RM07_KINDS = frozenset({"manual", "krisp", "gmail", "slack"})


def test_source_kind_set_unchanged() -> None:
    assert source_kinds.VALID_SOURCE_KINDS == PRE_RM07_KINDS


def test_vault_links_source_kinds_is_canonical() -> None:
    assert links._SOURCE_KINDS is source_kinds.VALID_SOURCE_KINDS


def test_every_python_reexport_is_the_canonical_object() -> None:
    canonical = source_kinds.VALID_SOURCE_KINDS
    assert schemas.VALID_SOURCE_KINDS is canonical
    assert cli._VALID_SOURCE_KINDS is canonical
    assert links._SOURCE_KINDS is canonical


def test_source_kinds_docstring_no_longer_claims_a_separate_copy() -> None:
    text = Path(source_kinds.__file__).read_text(encoding="utf-8")
    assert "genuine separate copy" not in text
