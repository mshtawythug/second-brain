"""Snapshot GitHub repo-traffic into a committed, permanent history file.

GitHub only retains repository traffic (clones / views) for a rolling **14
days** and exposes it solely to accounts with push access. This script pulls
the current window from the REST API and merges it into ``stats/traffic.json``,
upserting per-day records so the history accumulates forever instead of
scrolling off after two weeks. Run daily by ``.github/workflows/traffic-stats.yml``.

Stdlib only (``urllib`` / ``json`` / ``os`` / ``pathlib``) — no third-party deps.

``stats/traffic.json`` schema (stable, deterministic — sorted keys, sorted
dates, trailing newline)::

    {
      "clones": {                     # permanent per-day clone history
        "YYYY-MM-DD": {"count": int, "uniques": int},
        ...
      },
      "views": {                      # permanent per-day view history
        "YYYY-MM-DD": {"count": int, "uniques": int},
        ...
      },
      "summaries": {                  # rolling 14-day totals, refreshed each run
        "clones_14d": {"as_of": "YYYY-MM-DD", "count": int, "uniques": int},
        "views_14d":  {"as_of": "YYYY-MM-DD", "count": int, "uniques": int}
      },
      "snapshots": [                  # point-in-time stars/forks, upserted by date
        {"date": "YYYY-MM-DD", "stars": int, "forks": int},
        ...
      ],
      "latest": {"date": "YYYY-MM-DD", "stars": int, "forks": int}
    }

Seed note: the committed seed's ``views`` map is intentionally EMPTY — only
14-day view *totals* were available when the history was first seeded (kept in
``summaries.views_14d``). Daily ``views`` records backfill automatically once
the workflow runs with a token that has traffic access, so an empty ``views``
map is expected at seed time, not a bug.

The merge (:func:`merge_traffic`) is a pure function with NO network / file
IO, so it is unit-testable in isolation; :func:`main` does the HTTP + file IO
and delegates the actual merge to it.

Auth: reads ``TRAFFIC_TOKEN`` if set, else ``GITHUB_TOKEN`` (the workflow
supplies one). The traffic endpoints require push access, so a bare
``GITHUB_TOKEN`` may be rejected — in that case the script prints a clear
message about creating a ``TRAFFIC_TOKEN`` repo secret and EXITS 0.

Snapshotting is best-effort and must NEVER fail the workflow, so every
*transient / access* failure is swallowed (print one reason line, exit 0
without writing): HTTP 401/403/404 (no push access / bad token), 429 (rate
limit), any 5xx (server/gateway blip), and network-level failures
(``URLError`` / socket timeout). Genuinely unexpected errors — a
``JSONDecodeError``, a ``KeyError``, other programming bugs — stay fail-loud
(non-zero) so real breakage still surfaces. The token is never printed or
logged (no skip message contains it).

Usage::

    python scripts/snapshot_traffic.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATS_PATH = REPO_ROOT / "stats" / "traffic.json"
DEFAULT_REPO = "mshtawythug/second-brain"

API_BASE = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "second-brain-traffic-snapshot"

# GitHub returns these when the token lacks push access (traffic endpoints) or
# is invalid — the "best-effort skip" path rather than a hard failure.
_INSUFFICIENT_ACCESS_CODES = frozenset({401, 403, 404})

_ACCESS_HINT = (
    "GitHub traffic snapshot skipped (best-effort): the clones/views endpoints "
    "require push access, which the default GITHUB_TOKEN may not have. To enable "
    "permanent traffic history, add a repo secret named TRAFFIC_TOKEN — either a "
    "fine-grained PAT with 'Administration: read' on this repo, or a classic PAT "
    "with the 'repo' scope. Exiting 0 without writing."
)
_RATE_LIMIT_HINT = (
    "GitHub traffic snapshot skipped (best-effort): rate-limited (HTTP 429). "
    "Exiting 0 without writing — the next daily run will retry."
)
_NETWORK_HINT = (
    "GitHub traffic snapshot skipped (best-effort): could not reach the GitHub "
    "API (network error / timeout). Exiting 0 without writing — the next daily "
    "run will retry."
)


def _iso_date(timestamp: str) -> str:
    """Slice an API ISO timestamp (``2026-07-20T00:00:00Z``) to ``YYYY-MM-DD``."""
    return timestamp[:10]


def _index_daily(entries: Any) -> dict[str, dict[str, int]]:
    """Turn an API ``[{timestamp,count,uniques}, ...]`` list into a date->record map."""
    daily: dict[str, dict[str, int]] = {}
    for entry in entries or []:
        date = _iso_date(str(entry["timestamp"]))
        daily[date] = {"count": int(entry["count"]), "uniques": int(entry["uniques"])}
    return daily


def merge_traffic(
    existing: dict[str, Any],
    clones: dict[str, Any],
    views: dict[str, Any],
    repo_meta: dict[str, Any],
    run_date: str,
) -> dict[str, Any]:
    """Merge one API pull into the persistent history — pure, no network / IO.

    Per-day clone/view records are *upserted* keyed by date: days already in
    ``existing`` are preserved, and any day the API still reports (the last 14)
    is overwritten with the freshest value. The rolling 14-day totals in
    ``summaries`` are refreshed from the API's top-level count/uniques, and the
    stars/forks ``snapshot`` for ``run_date`` is upserted into ``snapshots``
    (idempotent within a day) with ``latest`` pointing at the newest snapshot
    *by date* — a backfill / out-of-order run with an older ``run_date`` cannot
    make ``latest`` regress.

    Does not mutate ``existing`` — carried-over records are deep-copied.
    """
    clones_daily = deepcopy(cast("dict[str, Any]", existing.get("clones", {})))
    clones_daily.update(_index_daily(clones.get("clones")))

    views_daily = deepcopy(cast("dict[str, Any]", existing.get("views", {})))
    views_daily.update(_index_daily(views.get("views")))

    summaries = deepcopy(cast("dict[str, Any]", existing.get("summaries", {})))
    summaries["clones_14d"] = {
        "as_of": run_date,
        "count": int(clones.get("count", 0)),
        "uniques": int(clones.get("uniques", 0)),
    }
    summaries["views_14d"] = {
        "as_of": run_date,
        "count": int(views.get("count", 0)),
        "uniques": int(views.get("uniques", 0)),
    }

    snapshot = {
        "date": run_date,
        "stars": int(repo_meta.get("stargazers_count", 0)),
        "forks": int(repo_meta.get("forks_count", 0)),
    }
    by_date: dict[str, dict[str, Any]] = {
        str(snap["date"]): snap for snap in existing.get("snapshots", [])
    }
    by_date[run_date] = snapshot
    snapshots = [by_date[date] for date in sorted(by_date)]

    return {
        "clones": clones_daily,
        "views": views_daily,
        "summaries": summaries,
        "snapshots": snapshots,
        # Newest snapshot by date (snapshots is date-sorted and non-empty) — an
        # out-of-order backfill run cannot regress ``latest``.
        "latest": snapshots[-1],
    }


def serialize(data: dict[str, Any]) -> str:
    """Deterministic JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _fetch_json(url: str, token: str) -> dict[str, Any]:
    """GET ``url`` as JSON with GitHub auth headers and an explicit timeout."""
    request = urllib.request.Request(  # noqa: S310 — fixed https api.github.com URLs
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
        return cast("dict[str, Any]", json.load(response))


def _load_existing(path: Path) -> dict[str, Any]:
    """Load the current history file, or an empty dict when absent."""
    if path.is_file():
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    return {}


def classify_fetch_error(exc: BaseException) -> str | None:
    """Classify a fetch failure as a best-effort skip vs a fatal error.

    Returns a short one-line reason (safe to print — never contains the token)
    when ``exc`` is a *transient / access* failure that should be swallowed so
    the daily cron stays green, or ``None`` when the caller should re-raise it.

    Swallowed (returns a reason):
      * HTTP 401 / 403 / 404 — no push access or an invalid token;
      * HTTP 429 — rate limited;
      * HTTP 5xx — a GitHub server / gateway blip;
      * ``URLError`` / ``TimeoutError`` (socket timeout) — network trouble.

    Fatal (returns ``None`` → re-raise): every other ``HTTPError`` code (e.g. a
    400 / 422 signalling a real request bug) and any non-network exception such
    as ``JSONDecodeError`` or ``KeyError`` — those are programming bugs that
    must surface loudly.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in _INSUFFICIENT_ACCESS_CODES:
            return _ACCESS_HINT
        if exc.code == 429:
            return _RATE_LIMIT_HINT
        if exc.code >= 500:
            return _NETWORK_HINT
        return None
    # HTTPError subclasses URLError, so this branch only sees genuine network
    # errors (DNS/connection) and socket timeouts (TimeoutError since 3.10).
    if isinstance(exc, urllib.error.URLError | TimeoutError):
        return _NETWORK_HINT
    return None


def main() -> int:
    """CLI entrypoint. Returns a process exit code (0 = ok or best-effort skip)."""
    token = os.environ.get("TRAFFIC_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO

    if not token:
        print(_ACCESS_HINT)
        return 0

    clones_url = f"{API_BASE}/repos/{repo}/traffic/clones"
    views_url = f"{API_BASE}/repos/{repo}/traffic/views"
    repo_url = f"{API_BASE}/repos/{repo}"

    try:
        clones = _fetch_json(clones_url, token)
        views = _fetch_json(views_url, token)
        repo_meta = _fetch_json(repo_url, token)
    except (urllib.error.URLError, TimeoutError) as exc:
        # HTTPError (a URLError subclass), 429/5xx, and network/timeout failures
        # are best-effort skips (exit 0). A fatal HTTPError code re-raises;
        # JSONDecodeError / KeyError aren't caught here at all → fail loud.
        reason = classify_fetch_error(exc)
        if reason is None:
            raise
        print(reason)
        return 0

    # UTC "today" marks this snapshot's stars/forks and the summaries' as_of.
    run_date = datetime.now(UTC).strftime("%Y-%m-%d")

    stats_path = DEFAULT_STATS_PATH
    merged = merge_traffic(_load_existing(stats_path), clones, views, repo_meta, run_date)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(serialize(merged), encoding="utf-8")

    print(
        f"Wrote {stats_path.relative_to(REPO_ROOT)}: "
        f"{len(merged['clones'])} clone-days, {len(merged['views'])} view-days, "
        f"{len(merged['snapshots'])} snapshots (stars={merged['latest']['stars']}, "
        f"forks={merged['latest']['forks']})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
