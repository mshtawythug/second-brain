"""How many times whisper has failed on each memo, so one it always fails on stops (RM-38).

A :class:`~brain.ingest.voice.WhisperFailedError` is transient (RM-38.D4): it usually
points at the model or the binary, so the drop folder moves the memo back and retries it.
A memo whisper fails on every time would then be retried every 10 minutes forever
(sec-107 F5). This ledger counts those failures per memo, keyed by the SHA-256 of its
bytes (so a renamed copy shares the count), in ``<state_dir>/`` :data:`STATE_NAME`
(``$BRAIN_HOME/state``). :func:`record_failure` returns the new count; at
:data:`MAX_FAILURES` the caller rejects the memo and the entry is dropped.
:func:`forget` drops it after a success.

The ledger is a small JSON object, written to a temporary file and renamed into place, in
a directory created ``0o700``. It keeps at most :data:`MAX_ENTRIES` memos, dropping the
oldest. A file that does not parse as ``{digest: positive int}`` is started over: losing
a count costs at most three more tries. Two runs racing can lose one increment, the same
cost. A memo is hashed from one fd opened ``O_NOFOLLOW``, so a symlink is an ``OSError``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

STATE_NAME = "voice-failures.json"
#: The failure that reaches this count rejects the memo.
MAX_FAILURES = 3
MAX_ENTRIES = 1000
CHUNK_BYTES = 1 << 20


def record_failure(state_dir: Path, memo: Path) -> int:
    """Count one more whisper failure on ``memo``; the count, including this one."""
    digest = _digest(memo)
    ledger = _load(state_dir)
    count = ledger.pop(digest, 0) + 1
    if count < MAX_FAILURES:
        ledger[digest] = count  # re-inserted last: the newest entry
        while len(ledger) > MAX_ENTRIES:
            del ledger[next(iter(ledger))]
    _save(state_dir, ledger)
    return count


def forget(state_dir: Path, memo: Path) -> None:
    """Drop ``memo``'s count; reads nothing from ``memo`` when the ledger is empty."""
    ledger = _load(state_dir)
    if not ledger:
        return
    if ledger.pop(_digest(memo), None) is not None:
        _save(state_dir, ledger)


def _digest(memo: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(memo, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        while chunk := os.read(fd, CHUNK_BYTES):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _load(state_dir: Path) -> dict[str, int]:
    try:
        data = json.loads((state_dir / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not all(
        type(count) is int and count > 0 for count in data.values()
    ):
        return {}
    return dict(data)


def _save(state_dir: Path, ledger: dict[str, int]) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{STATE_NAME}.", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(ledger, out)
        os.replace(tmp, state_dir / STATE_NAME)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
