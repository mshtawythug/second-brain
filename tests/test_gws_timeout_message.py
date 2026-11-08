"""``real_gws_runner``'s timeout carries no argv: label and duration only (BL-39, sec-126 I3).

The message used to end in ``' '.join(args)``, which holds the ``--params`` JSON (a Gmail
query and its sender address, a calendar window). ``gmail._run`` raises ``from None``, but the
runner's exception still rides along as ``__context__``, and the Calendar and Contacts callers
hold it directly. Both timeout paths are covered: the capped read (``run_capped``) and the plain
``subprocess.run``. Each runs a throwaway ``gws`` stub on a PATH holding nothing else. No
database: marked ``nodb``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.errors import GwsTimeoutError
from brain.gws import real_gws_runner
from tests.gws_stub_fixtures import HANG_SECONDS, SYNTH_ADDRESS, gws_on_path

pytestmark = pytest.mark.nodb

#: ``exec`` so the plain path's kill reaches the sleep itself: no orphan holds the pipe.
_HANGS = f"exec /bin/sleep {HANG_SECONDS}"
PARAMS = f'{{"q":"from:{SYNTH_ADDRESS}"}}'
ARGS = ["gws", "calendar", "events", "list", "--params", PARAMS, "--format", "json"]


@pytest.mark.parametrize("cap", [1000, None], ids=["capped", "plain"])
def test_a_timeout_names_the_call_and_the_duration_never_the_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int | None
) -> None:
    gws_on_path(tmp_path, monkeypatch, _HANGS)

    with pytest.raises(GwsTimeoutError) as info:
        real_gws_runner(ARGS, timeout=0.3, max_stdout_bytes=cap)

    assert str(info.value) == "`gws calendar events list` timed out after 0.3s"
    assert info.value.log_detail == "timed out after 0.3s"
    for element in ARGS[4:]:
        assert element not in str(info.value) and element not in info.value.log_detail
    assert SYNTH_ADDRESS not in str(info.value)
