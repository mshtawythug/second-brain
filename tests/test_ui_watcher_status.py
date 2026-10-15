"""A9's UI half: the words the status line uses, EXECUTED under node.

``static/js/watcher_status.js`` maps a ``/api/watcher`` payload to what the top
bar says. It is pure — no DOM, no imports — for the reason
``tests/test_ui_ledger_status.py`` gives: a decision left inside a DOM module
can only be grepped, and this repository has shipped many grep-shaped guards
that could not fail. So the module is copied beside a harness and run.

No PII: payloads are enum strings and junk.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from brain.doctor_watcher import WATCHER_STATES
from brain.ui.app import static_dir

pytestmark = pytest.mark.nodb

WATCHER_STATUS = Path(str(static_dir())) / "js" / "watcher_status.js"

#: A9's sentence, verbatim from the parent spec §8.5.
A9_PHRASE = "external edits will not appear in search"

HARNESS = """\
import { watcherLine, STATES } from "./watcher_status.mjs";

const payloads = {
  running: { state: "running" },
  not_running: { state: "not_running" },
  unavailable: { state: "unavailable" },
  "null": null,
  empty: {},
  junk: { state: "<b>bogus</b>" },
  number: { state: 3 },
  proto: { state: "constructor" },
};
const out = { STATES, lines: {} };
for (const [name, payload] of Object.entries(payloads)) {
  out.lines[name] = watcherLine(payload);
}
console.log(JSON.stringify(out));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover — environment-dependent
        pytest.skip("node is required for the watcher_status.js runtime harness")
    return node


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    tmp = tmp_path_factory.mktemp("watcher_status")
    (tmp / "watcher_status.mjs").write_text(
        WATCHER_STATUS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    harness = tmp / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [_node(), str(harness)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def _line(run: dict[str, object], name: str) -> dict[str, str]:
    lines = run["lines"]
    assert isinstance(lines, dict)
    line = lines[name]
    assert isinstance(line, dict)
    return line


def test_the_javascript_states_are_the_python_states(run: dict[str, object]) -> None:
    """The one fact duplicated across the language boundary, compared by VALUE."""
    assert tuple(run["STATES"]) == WATCHER_STATES  # type: ignore[arg-type]


def test_a_running_watcher_says_nothing(run: dict[str, object]) -> None:
    line = _line(run, "running")

    assert (line["state"], line["text"], line["short"], line["tone"]) == (
        "running", "", "", ""
    )


def test_not_running_says_the_a9_sentence_as_a_warning(run: dict[str, object]) -> None:
    line = _line(run, "not_running")

    assert line["text"] == f"Watcher not running — {A9_PHRASE}"
    assert line["short"] == "Watcher off"
    assert line["tone"] == "warn"


def test_unavailable_says_unknown_quietly(run: dict[str, object]) -> None:
    line = _line(run, "unavailable")

    assert line["text"].startswith("Watcher status unknown")
    assert "may not appear in search" in line["text"]
    assert line["short"] == "Watcher unknown"
    assert line["tone"] == "muted"


@pytest.mark.parametrize("name", ["null", "empty", "junk", "number", "proto"])
def test_anything_unrecognised_is_unknown_never_healthy(
    run: dict[str, object], name: str
) -> None:
    """A payload the line cannot read must not render as the silent "running"."""
    assert _line(run, name) == _line(run, "unavailable")
