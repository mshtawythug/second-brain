"""Tests for the repo-traffic snapshotter (``scripts/snapshot_traffic.py``).

These exercise the pure merge core (:func:`merge_traffic`) and the deterministic
serializer — no network, no DB, no ``test_db`` fixture. ``scripts/`` is not a
package on the path, so the module is loaded via ``importlib`` (mirroring
``tests/test_search_size_budget.py``) rather than relying on ``PYTHONPATH``.

Coverage:
  * empty history seeds every reported day (clones + views);
  * per-day upsert overwrites a re-seen date and preserves older out-of-window days;
  * stars/forks snapshot is recorded against ``run_date`` (idempotent within a day);
  * ``latest`` tracks the newest snapshot by date, so a backfill run can't regress it;
  * output is deterministic + sorted (stable diffs, trailing newline);
  * the input history is never mutated;
  * ``classify_fetch_error`` swallows transient/access failures (401/403/404, 429,
    5xx, network/timeout) but re-raises fatal ones (other 4xx, JSONDecodeError, bugs).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "snapshot_traffic.py"


def _load_module() -> ModuleType:
    """Import ``scripts/snapshot_traffic.py`` once, cached in ``sys.modules``."""
    name = "_brain_snapshot_traffic_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snap() -> ModuleType:
    return _load_module()


def _clones_api(days: dict[str, tuple[int, int]], count: int, uniques: int) -> dict[str, Any]:
    return {
        "count": count,
        "uniques": uniques,
        "clones": [
            {"timestamp": f"{date}T00:00:00Z", "count": c, "uniques": u}
            for date, (c, u) in days.items()
        ],
    }


def _views_api(days: dict[str, tuple[int, int]], count: int, uniques: int) -> dict[str, Any]:
    return {
        "count": count,
        "uniques": uniques,
        "views": [
            {"timestamp": f"{date}T00:00:00Z", "count": c, "uniques": u}
            for date, (c, u) in days.items()
        ],
    }


def test_script_file_exists() -> None:
    assert SCRIPT_PATH.is_file()


def test_empty_history_seeds_all_days(snap: ModuleType) -> None:
    """Merging into ``{}`` records every clone/view day and the 14d summaries."""
    clones = _clones_api({"2026-07-20": (275, 20), "2026-07-21": (55, 22)}, count=330, uniques=42)
    views = _views_api({"2026-07-20": (100, 60)}, count=201, uniques=110)
    repo_meta = {"stargazers_count": 9, "forks_count": 1}

    result = snap.merge_traffic({}, clones, views, repo_meta, "2026-07-24")

    assert result["clones"] == {
        "2026-07-20": {"count": 275, "uniques": 20},
        "2026-07-21": {"count": 55, "uniques": 22},
    }
    assert result["views"] == {"2026-07-20": {"count": 100, "uniques": 60}}
    assert result["summaries"]["clones_14d"] == {
        "as_of": "2026-07-24",
        "count": 330,
        "uniques": 42,
    }
    assert result["summaries"]["views_14d"] == {
        "as_of": "2026-07-24",
        "count": 201,
        "uniques": 110,
    }


def test_upsert_overwrites_reseen_and_preserves_out_of_window(snap: ModuleType) -> None:
    """A re-seen date takes the newer value; older out-of-window days survive."""
    existing = {
        "clones": {
            "2026-06-01": {"count": 5, "uniques": 4},  # older than the 14d window
            "2026-07-20": {"count": 275, "uniques": 20},  # will be re-seen (stale)
        },
        "views": {},
        "summaries": {},
        "snapshots": [{"date": "2026-07-23", "stars": 8, "forks": 1}],
    }
    # The API re-reports 2026-07-20 with a corrected/updated value.
    clones = _clones_api({"2026-07-20": (999, 40), "2026-07-24": (3, 3)}, count=1002, uniques=43)
    views = _views_api({}, count=0, uniques=0)
    repo_meta = {"stargazers_count": 9, "forks_count": 1}

    result = snap.merge_traffic(existing, clones, views, repo_meta, "2026-07-24")

    # Out-of-window day preserved.
    assert result["clones"]["2026-06-01"] == {"count": 5, "uniques": 4}
    # Re-seen day overwritten with the newer value.
    assert result["clones"]["2026-07-20"] == {"count": 999, "uniques": 40}
    # Newly reported day added.
    assert result["clones"]["2026-07-24"] == {"count": 3, "uniques": 3}


def test_snapshot_recorded_with_run_date(snap: ModuleType) -> None:
    """Stars/forks are captured against run_date and pointed at by ``latest``."""
    clones = _clones_api({}, count=0, uniques=0)
    views = _views_api({}, count=0, uniques=0)
    repo_meta = {"stargazers_count": 42, "forks_count": 7}

    result = snap.merge_traffic({}, clones, views, repo_meta, "2026-07-24")

    assert result["snapshots"] == [{"date": "2026-07-24", "stars": 42, "forks": 7}]
    assert result["latest"] == {"date": "2026-07-24", "stars": 42, "forks": 7}


def test_snapshot_upsert_is_idempotent_within_a_day(snap: ModuleType) -> None:
    """Two runs on the same run_date collapse to one snapshot (no duplicate)."""
    clones = _clones_api({}, count=0, uniques=0)
    views = _views_api({}, count=0, uniques=0)

    first = snap.merge_traffic(
        {}, clones, views, {"stargazers_count": 9, "forks_count": 1}, "2026-07-24"
    )
    second = snap.merge_traffic(
        first, clones, views, {"stargazers_count": 10, "forks_count": 1}, "2026-07-24"
    )

    assert second["snapshots"] == [{"date": "2026-07-24", "stars": 10, "forks": 1}]


def test_multi_day_snapshots_sorted_by_date(snap: ModuleType) -> None:
    """Snapshots from different days accumulate and stay sorted by date."""
    clones = _clones_api({}, count=0, uniques=0)
    views = _views_api({}, count=0, uniques=0)
    meta = {"stargazers_count": 9, "forks_count": 1}

    day1 = snap.merge_traffic({}, clones, views, meta, "2026-07-24")
    day2 = snap.merge_traffic(
        day1, clones, views, {"stargazers_count": 11, "forks_count": 2}, "2026-07-25"
    )

    assert [s["date"] for s in day2["snapshots"]] == ["2026-07-24", "2026-07-25"]
    assert day2["latest"] == {"date": "2026-07-25", "stars": 11, "forks": 2}


def test_output_is_deterministic_and_sorted(snap: ModuleType) -> None:
    """serialize() sorts keys, sorts dates, and ends with a single newline."""
    clones = _clones_api({"2026-07-21": (55, 22), "2026-07-20": (275, 20)}, count=330, uniques=42)
    views = _views_api({}, count=0, uniques=0)
    result = snap.merge_traffic(
        {}, clones, views, {"stargazers_count": 9, "forks_count": 1}, "2026-07-24"
    )

    text = snap.serialize(result)

    # Same input -> byte-identical output.
    assert snap.serialize(result) == text
    # Trailing newline, exactly one.
    assert text.endswith("}\n")
    assert not text.endswith("}\n\n")
    # Dates appear in chronological (sorted) order regardless of insertion order.
    assert text.index('"2026-07-20"') < text.index('"2026-07-21"')
    # Top-level keys sorted (sort_keys=True): clones before summaries before views.
    reparsed = json.loads(text)
    assert list(reparsed.keys()) == sorted(reparsed.keys())


def test_merge_does_not_mutate_input(snap: ModuleType) -> None:
    """The existing history dict passed in is never mutated (immutable merge)."""
    existing: dict[str, Any] = {
        "clones": {"2026-07-20": {"count": 275, "uniques": 20}},
        "snapshots": [{"date": "2026-07-23", "stars": 8, "forks": 1}],
    }
    before = json.dumps(existing, sort_keys=True)

    clones = _clones_api({"2026-07-20": (999, 40)}, count=999, uniques=40)
    views = _views_api({}, count=0, uniques=0)
    snap.merge_traffic(
        existing, clones, views, {"stargazers_count": 9, "forks_count": 1}, "2026-07-24"
    )

    assert json.dumps(existing, sort_keys=True) == before


def test_latest_tracks_newest_date_not_run_date(snap: ModuleType) -> None:
    """A backfill run with an OLDER run_date must not regress ``latest``."""
    clones = _clones_api({}, count=0, uniques=0)
    views = _views_api({}, count=0, uniques=0)

    newer = snap.merge_traffic(
        {}, clones, views, {"stargazers_count": 11, "forks_count": 2}, "2026-07-25"
    )
    backfilled = snap.merge_traffic(
        newer, clones, views, {"stargazers_count": 5, "forks_count": 1}, "2026-07-20"
    )

    # latest points at the newest snapshot BY DATE, not the run just processed.
    assert backfilled["latest"] == {"date": "2026-07-25", "stars": 11, "forks": 2}
    assert [s["date"] for s in backfilled["snapshots"]] == ["2026-07-20", "2026-07-25"]


# --- classify_fetch_error: transient/access -> skip (exit 0) vs fatal -------


def _http_error(code: int) -> urllib.error.HTTPError:
    """Build an HTTPError carrying ``code`` (only ``.code`` is read)."""
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", hdrs=None, fp=None)


@pytest.mark.parametrize(
    ("exc_factory", "expected_attr"),
    [
        (lambda: _http_error(401), "_ACCESS_HINT"),
        (lambda: _http_error(403), "_ACCESS_HINT"),
        (lambda: _http_error(404), "_ACCESS_HINT"),
        (lambda: _http_error(429), "_RATE_LIMIT_HINT"),
        (lambda: _http_error(500), "_NETWORK_HINT"),
        (lambda: _http_error(503), "_NETWORK_HINT"),
        (lambda: urllib.error.URLError("connection refused"), "_NETWORK_HINT"),
        (lambda: TimeoutError("timed out"), "_NETWORK_HINT"),
    ],
)
def test_classify_swallows_transient_and_access_errors(
    snap: ModuleType, exc_factory: Any, expected_attr: str
) -> None:
    """401/403/404, 429, 5xx, and network/timeout errors return a skip reason."""
    reason = snap.classify_fetch_error(exc_factory())
    assert reason == getattr(snap, expected_attr)
    assert reason is not None
    # Best-effort skip messages must never carry a token.
    assert "Bearer" not in reason


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _http_error(400),
        lambda: _http_error(422),
        lambda: json.JSONDecodeError("bad", "doc", 0),
        lambda: KeyError("count"),
        lambda: ValueError("boom"),
    ],
)
def test_classify_reraises_fatal_errors(snap: ModuleType, exc_factory: Any) -> None:
    """Unexpected HTTP codes and programming bugs are fatal (None -> re-raise)."""
    assert snap.classify_fetch_error(exc_factory()) is None


# --- main() wiring: transient failure -> exit 0 without writing -------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _http_error(429),  # rate limited
        lambda: _http_error(503),  # gateway blip
        lambda: urllib.error.URLError("network down"),
        lambda: TimeoutError("timed out"),
    ],
)
def test_main_skips_transient_failures_exit_0_without_writing(
    snap: ModuleType, exc_factory: Any, mocker: Any, tmp_path: Path
) -> None:
    """A transient fetch failure makes main() exit 0 and never touch the file."""
    mocker.patch.dict("os.environ", {"GITHUB_TOKEN": "unused-in-this-test"}, clear=False)
    mocker.patch.object(snap, "_fetch_json", side_effect=exc_factory())
    # Redirect the output path so a bug that DID write can't clobber the seed.
    sentinel = tmp_path / "traffic.json"
    mocker.patch.object(snap, "DEFAULT_STATS_PATH", sentinel)

    assert snap.main() == 0
    assert not sentinel.exists()  # skip path writes nothing


def test_main_reraises_fatal_fetch_error(snap: ModuleType, mocker: Any, tmp_path: Path) -> None:
    """A fatal error (unexpected HTTP code) is NOT swallowed by main()."""
    mocker.patch.dict("os.environ", {"GITHUB_TOKEN": "unused-in-this-test"}, clear=False)
    mocker.patch.object(snap, "_fetch_json", side_effect=_http_error(400))
    mocker.patch.object(snap, "DEFAULT_STATS_PATH", tmp_path / "traffic.json")

    with pytest.raises(urllib.error.HTTPError):
        snap.main()
