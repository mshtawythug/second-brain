"""The brief daemon script runs ``brain dedup scan`` on its own line, before the brief (RM-19).

RM-19.D3: the scan is a separate process whose failure cannot stop ``brain brief --wiki``. The
script runs under ``set -euo pipefail``, so the ``|| true`` is what keeps a failing scan from
ending it (AC7). The script is run for real, with ``BRAIN_PY`` pointing at a fake interpreter that
records its arguments and fails the scan; nothing here reaches the real ``brain``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import brain

SCRIPT = Path(brain.__file__).parent / "templates" / "bin" / "_brain-brief-fg.sh"
SCAN_LINE = '"$PY" -m brain dedup scan --limit 500 || true'
BRIEF_LINE = 'exec "$PY" -m brain brief --wiki'


def _fake_python(tmp_path: Path, *, scan_exit: int) -> tuple[Path, Path]:
    """A fake interpreter: logs each call's arguments, exits ``scan_exit`` for ``dedup``."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        f'if [[ "$*" == *"brain dedup scan"* ]]; then exit {scan_exit}; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _run(tmp_path: Path, *, scan_exit: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake, log = _fake_python(tmp_path, scan_exit=scan_exit)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BRAIN_PY": str(fake)}
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_the_scan_line_precedes_the_brief_line() -> None:
    lines = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()]

    assert SCAN_LINE in lines
    assert BRIEF_LINE in lines
    assert lines.index(SCAN_LINE) < lines.index(BRIEF_LINE)


def test_a_failing_scan_does_not_stop_the_brief(tmp_path: Path) -> None:
    # AC7.
    result, calls = _run(tmp_path, scan_exit=1)

    assert result.returncode == 0, result.stderr
    assert calls[-2:] == ["-m brain dedup scan --limit 500", "-m brain brief --wiki"]


def test_a_passing_scan_then_runs_the_brief(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, scan_exit=0)

    assert result.returncode == 0, result.stderr
    assert calls[-2:] == ["-m brain dedup scan --limit 500", "-m brain brief --wiki"]
