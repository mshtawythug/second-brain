"""The ``--json`` emit guard in ``brain search`` — must survive ``python -O``.

``brain search --json`` builds its result projection once, prices it into
``search_queries.payload_tokens``, and emits that same object. The invariant
"the emitted payload IS the priced payload" was protected by a bare ``assert``,
which ``python -O`` removes from the bytecode entirely — so the guard was absent
in exactly the runs that skip assertions. These tests pin the replacement AND
prove it still fires under ``-O``.
"""
import subprocess
import sys

import pytest

from brain.cli_search import _require_priced_projection
from brain.errors import BrainError


def test_require_priced_projection_returns_the_same_object() -> None:
    """The happy path returns the priced list itself, not a copy.

    Identity matters: a copy would be a second projection, which is the drift
    the single-build discipline exists to prevent.
    """
    projected = [{"id": "0" * 8, "title": "synthetic doc"}]

    assert _require_priced_projection(projected) is projected


def test_require_priced_projection_raises_instead_of_emitting_null() -> None:
    """``None`` must raise, not reach ``emit_json`` as a bare ``null``."""
    with pytest.raises(BrainError) as excinfo:
        _require_priced_projection(None)

    # The message has to name the invariant, not just say "internal error" —
    # the next reader's first question is *which* pairing broke.
    assert "payload_tokens" in str(excinfo.value)


def test_the_guard_still_fires_under_python_dash_O() -> None:
    """The whole reason this is not an ``assert``.

    Runs a fresh interpreter with ``-O`` (assertions stripped) and asserts the
    guard still raises. Mutating ``_require_priced_projection``'s ``raise`` back
    into a bare ``assert`` reddens THIS test and no other, because it is the
    only one that runs with assertions disabled.
    """
    program = (
        "from brain.cli_search import _require_priced_projection\n"
        "from brain.errors import BrainError\n"
        # Prove -O really is in effect for this child, so the test cannot pass
        # vacuously if the flag is ever dropped from the argv below.
        "assert False, 'assertions are enabled: -O did not take effect'\n"
        "try:\n"
        "    _require_priced_projection(None)\n"
        "except BrainError:\n"
        "    print('RAISED')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-O", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "RAISED", proc.stdout
