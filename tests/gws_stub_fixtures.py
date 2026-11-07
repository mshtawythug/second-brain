"""Throwaway ``gws`` stand-ins for the BL-39 tests: a ``/bin/sh`` script on a PATH of its own.

PATH is set to the stub's directory alone, so the installed ``gws`` can never be found. Commands
inside a stub are absolute (``/bin/sleep``, ``/usr/bin/head``); ``printf`` and ``echo`` are shell
builtins. Every stub is bounded: no loops, a sleep of at most :data:`HANG_SECONDS`, and a fixed
byte count. All data is synthetic.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

#: How long a "hung" stub sleeps. A test that waits for it instead of the timeout fails its
#: :data:`FAST_SECONDS` bound, which is how the regression tests go red on master.
HANG_SECONDS = 20
#: The bound every refusal test asserts it finished within.
FAST_SECONDS = 10.0
SYNTH_ADDRESS = "sender@example.test"
SYNTH_STDERR = "SYNTH-STDERR-MARKER"

#: Sleeps in the foreground while a background grandchild holds stdout open: only a
#: process-group kill ends the read before :data:`HANG_SECONDS`.
HUNG = f"/bin/sleep {HANG_SECONDS} &\n/bin/sleep {HANG_SECONDS}"
#: Writes 300,000 bytes, then hangs: only a cap enforced while reading refuses it early.
FLOOD = f"/usr/bin/head -c 300000 /dev/zero\n/bin/sleep {HANG_SECONDS}"
#: Quotes a synthetic secret on stderr and exits 3.
FAILING = f"echo {SYNTH_STDERR} >&2\nexit 3"


def answering(list_json: str, get_json: str) -> str:
    """A stub answering ``messages list`` / ``messages get`` (its ``$4``) with fixed JSON."""
    return (
        'case "$4" in\n'
        f"  list) printf '%s' '{list_json}' ;;\n"
        f"  get) printf '%s' '{get_json}' ;;\n"
        "  *) exit 9 ;;\n"
        "esac"
    )


def gws_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | None) -> Path:
    """Put a stub ``gws`` running ``body`` (or none at all) on a PATH holding nothing else."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if body is not None:
        gws = bindir / "gws"
        gws.write_text(f"#!/bin/sh\n{body}\n")
        gws.chmod(gws.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir
