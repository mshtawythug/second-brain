"""`brain doctor`'s ``fzf`` row (RM-52): WARN at worst, and a WARN keeps exit 0."""
from __future__ import annotations

import pytest

from brain.cli import _DoctorReport
from brain.doctor_fzf import fzf_doctor_check
from brain.search_pick import MIN_FZF_VERSION

pytestmark = pytest.mark.nodb


def test_a_current_fzf_is_ok() -> None:
    [check] = fzf_doctor_check(which=lambda _n: "/f", version=lambda _b: (0, 72))
    assert (check.check, check.status) == ("fzf", "ok")
    assert "--pick" in check.detail


def test_an_unreadable_version_is_still_ok() -> None:
    [check] = fzf_doctor_check(which=lambda _n: "/f", version=lambda _b: None)
    assert check.status == "ok"


def test_the_floor_itself_is_ok() -> None:
    [check] = fzf_doctor_check(which=lambda _n: "/f", version=lambda _b: MIN_FZF_VERSION)
    assert check.status == "ok"


def test_a_missing_fzf_warns_with_a_remedy() -> None:
    [check] = fzf_doctor_check(which=lambda _n: None)
    assert check.status == "warn"
    assert "--pick" in check.detail
    assert "fzf" in (check.remedy or "")


def test_an_old_fzf_warns_with_a_remedy() -> None:
    below = (MIN_FZF_VERSION[0], MIN_FZF_VERSION[1] - 1)
    [check] = fzf_doctor_check(which=lambda _n: "/f", version=lambda _b: below)
    assert check.status == "warn"
    assert f"{below[0]}.{below[1]}" in check.detail
    assert "fzf" in (check.remedy or "")


def test_the_missing_fzf_warn_does_not_fail_doctor() -> None:
    report = _DoctorReport(json_output=True)
    report.record_all(fzf_doctor_check(which=lambda _n: None))
    assert report.failed is False


def test_the_default_probe_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.doctor_fzf.shutil.which", lambda _name: None)
    [check] = fzf_doctor_check()
    assert check.status == "warn"
