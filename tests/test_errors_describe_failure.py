"""``brain.errors.describe_failure``: class, plus content-free detail (D1-D7).

Plan: ``docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md``. Class-only
logging must not cost the operator the diagnosis. ENOSPC versus EROFS, the
line of a broken YAML file, and the reason gws failed all survive. Each one is
chosen so it can never carry the text the exception failed on.
"""

from __future__ import annotations

import errno
import json

import pytest
import yaml

from brain.errors import DirectoryRefreshError, EnrichmentError, describe_failure

_SECRET = "synthetic-secret-value-42"


def test_a_plain_exception_is_its_class_only() -> None:
    """D1."""
    assert describe_failure(ValueError(_SECRET)) == "ValueError"


def test_a_cause_is_named_by_class() -> None:
    """D2."""
    try:
        try:
            json.loads(_SECRET)
        except json.JSONDecodeError as inner:
            raise EnrichmentError(_SECRET) from inner
    except EnrichmentError as exc:
        described = describe_failure(exc)

    assert described == "EnrichmentError from JSONDecodeError"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OSError(errno.ENOSPC, "No space left", f"/{_SECRET}"), "OSError[ENOSPC]"),
        (OSError(errno.EROFS, "Read-only", f"/{_SECRET}"), "OSError[EROFS]"),
        (FileNotFoundError(errno.ENOENT, "missing", f"/{_SECRET}"), "FileNotFoundError[ENOENT]"),
        (OSError(_SECRET), "OSError"),
    ],
    ids=["enospc", "erofs", "subclass", "no-errno"],
)
def test_an_oserror_carries_its_errno_symbol(exc: OSError, expected: str) -> None:
    """D3: the symbolic errno is content-free; the path and the strerror are not logged."""
    assert describe_failure(exc) == expected


def test_an_unknown_errno_is_named_by_number() -> None:
    """D4."""
    assert describe_failure(OSError(99999, _SECRET)) == "OSError[errno 99999]"


def test_a_yaml_error_carries_its_line_and_column_not_the_line() -> None:
    """D5: PyYAML's text quotes the offending line; line:col (1-based) does not."""
    try:
        yaml.safe_load(f"name: ok\ntitle: a: {_SECRET}\n")
    except yaml.YAMLError as exc:
        described = describe_failure(exc)

    assert described == "ScannerError[2:9]"


def test_a_raise_site_log_detail_is_appended() -> None:
    """D6: ``log_detail`` is a content-free phrase chosen where the error is raised."""
    exc = DirectoryRefreshError(f"gws failed: {_SECRET}", log_detail="exit 2")

    assert describe_failure(exc) == "DirectoryRefreshError[exit 2]"


def test_a_cause_carries_its_detail_too() -> None:
    """D7."""
    try:
        try:
            raise OSError(errno.ENOSPC, _SECRET)
        except OSError as inner:
            raise DirectoryRefreshError(_SECRET, log_detail="could not run gws") from inner
    except DirectoryRefreshError as exc:
        described = describe_failure(exc)

    assert described == "DirectoryRefreshError[could not run gws] from OSError[ENOSPC]"
