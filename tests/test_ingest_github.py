"""The `gh` reads behind `brain ingest-github` (RM-31): runner, parsing, search shapes.

Pure: no database, and no real ``gh``. The runner tests inject ``which`` and
``run``; one test runs a real subprocess, but against a throwaway script named
``gh`` in ``tmp_path``, never the installed CLI.
"""
from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from brain.ingest import github
from brain.ingest.github import GithubError, SearchScope
from tests import github_fixtures as fx
from tests.secret_fixtures import SYNTHETIC_POSITIVES

TOKEN = SYNTHETIC_POSITIVES["github_pat"]
FINE_TOKEN = SYNTHETIC_POSITIVES["github_pat_fine_grained"]
SINCE = date(2026, 10, 1)


# --- parse_pages ----------------------------------------------------------------


def test_parse_pages_joins_concatenated_array_pages() -> None:
    raw = fx.pages([{"a": 1}, {"a": 2}], [{"a": 3}])

    assert github.parse_pages(raw) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_parse_pages_keeps_object_pages_and_tolerates_whitespace() -> None:
    raw = '\n{"a": 1}\n  [{"a": 2}]\n'

    assert github.parse_pages(raw) == [{"a": 1}, {"a": 2}]


def test_parse_pages_of_nothing_is_empty() -> None:
    assert github.parse_pages("  \n") == []


def test_parse_pages_refuses_garbage_without_quoting_it() -> None:
    with pytest.raises(GithubError) as info:
        github.parse_pages(f'[{{"a": 1}}] {TOKEN}')

    assert TOKEN not in str(info.value)


# --- gh_runner --------------------------------------------------------------------


class _Run:
    """A ``subprocess.run`` stand-in that records its call and plays a scripted outcome."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}
        self.args: list[str] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.args, self.kwargs = list(args), kwargs
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def _which(name: str) -> str | None:
    return f"/usr/local/bin/{name}"


def test_runner_returns_stdout_and_bounds_the_call() -> None:
    run = _Run(_done(stdout='{"login": "x"}'))

    out = github.gh_runner(["gh", "api", "user"], which=_which, run=run)

    assert out == '{"login": "x"}'
    assert run.args == ["/usr/local/bin/gh", "api", "user"]
    assert run.kwargs["timeout"] == github.GH_TIMEOUT_SECONDS
    assert run.kwargs["check"] is False
    assert run.kwargs["capture_output"] is True
    env = run.kwargs["env"]
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_runner_without_gh_on_path_names_the_install() -> None:
    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: None, run=_Run(_done()))

    assert "gh CLI not found on PATH" in str(info.value)
    assert "gh auth login" in str(info.value)


def test_runner_failure_reports_the_exit_code_never_stderr() -> None:
    run = _Run(_done(returncode=4, stderr=f"HTTP 401: Bad credentials (token {TOKEN})"))

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=_which, run=run)

    assert str(info.value) == "`gh api` failed (exit 4)"
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None


def test_runner_timeout_is_a_github_error() -> None:
    run = _Run(subprocess.TimeoutExpired(["gh"], 60, stderr=TOKEN.encode()))

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "search", "prs"], which=_which, run=run)

    assert str(info.value) == "`gh search` timed out after 60s"


def test_runner_os_error_is_a_github_error_by_class() -> None:
    run = _Run(PermissionError(13, f"denied {TOKEN}"))

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=_which, run=run)

    assert str(info.value) == "`gh api` could not run (PermissionError)"


def test_runner_refuses_output_over_the_cap() -> None:
    run = _Run(_done(stdout="x" * 11))

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=_which, run=run, max_stdout_bytes=10)

    assert "exceeded 10 bytes" in str(info.value)


def test_runner_refuses_empty_args() -> None:
    with pytest.raises(GithubError):
        github.gh_runner([], which=_which, run=_Run(_done()))


def test_a_real_failing_process_never_surfaces_its_stderr(tmp_path: Path) -> None:
    """A real subprocess (a script named ``gh``, not the CLI) that prints a token and fails."""
    script = tmp_path / "gh"
    script.write_text(
        f"#!/bin/sh\necho 'token {FINE_TOKEN}' >&2\necho 'partial {TOKEN}'\nexit 1\n"
    )
    script.chmod(0o755)

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: str(script))

    assert str(info.value) == "`gh api` failed (exit 1)"
    assert FINE_TOKEN not in str(info.value) and TOKEN not in str(info.value)


def test_a_real_process_success_returns_stdout(tmp_path: Path) -> None:
    script = tmp_path / "gh"
    script.write_text("#!/bin/sh\nprintf '%s' \"$GH_PROMPT_DISABLED:$*\"\n")
    script.chmod(0o755)

    out = github.gh_runner(["gh", "api", "user"], which=lambda _n: str(script))

    assert out == "1:api user"


# --- viewer_login -------------------------------------------------------------------


def test_viewer_login_reads_only_the_login() -> None:
    fake = fx.FakeGh(api={"user": json.dumps({"login": fx.OWNER, "token": TOKEN})})

    assert github.viewer_login(runner=fake) == fx.OWNER
    assert fake.calls == [["gh", "api", "user"]]


@pytest.mark.parametrize("raw", ["not json", "[]", '{"login": ""}', '{"login": 3}'])
def test_viewer_login_refuses_an_unusable_answer(raw: str) -> None:
    with pytest.raises(GithubError):
        github.viewer_login(runner=fx.FakeGh(api={"user": raw}))


# --- search_items -------------------------------------------------------------------


def test_mine_searches_each_role_and_dedupes() -> None:
    fake = fx.FakeGh(prs=[fx.pr_hit()], issues=[fx.issue_hit(updated="2026-10-19T10:00:00Z")])

    hits = github.search_items(SearchScope(repos=(), mine=True, since=SINCE), limit=501,
                               runner=fake)

    roles = [tuple(c[3:5]) for c in fake.search_calls()]
    assert roles == [
        ("--author", "@me"), ("--assignee", "@me"), ("--reviewed-by", "@me"),
        ("--author", "@me"), ("--assignee", "@me"),
    ]
    assert [c[2] for c in fake.search_calls()] == ["prs"] * 3 + ["issues"] * 2
    assert [h.external_id for h in hits] == [
        f"{fx.PUBLIC_REPO}#{fx.PR_NUMBER}", f"{fx.PUBLIC_REPO}#{fx.ISSUE_NUMBER}"
    ]
    assert [h.kind for h in hits] == ["pr", "issue"]


def test_every_search_is_bounded_by_since_sort_and_limit() -> None:
    fake = fx.FakeGh()

    github.search_items(SearchScope(repos=(), mine=True, since=SINCE), limit=501, runner=fake)

    for call in fake.search_calls():
        assert call[call.index("--updated") + 1] == ">=2026-10-01"
        assert call[call.index("--sort") + 1] == "updated"
        assert call[call.index("--order") + 1] == "desc"
        assert call[call.index("--limit") + 1] == "501"
        assert "body" in call[call.index("--json") + 1].split(",")


def test_repo_scope_without_mine_is_one_query_per_type_with_every_repo() -> None:
    fake = fx.FakeGh()
    scope = SearchScope(repos=(fx.PUBLIC_REPO, fx.OWN_REPO), mine=False, since=SINCE)

    github.search_items(scope, limit=10, runner=fake)

    calls = fake.search_calls()
    assert [c[2] for c in calls] == ["prs", "issues"]
    for call in calls:
        assert "--author" not in call
        repos = [call[i + 1] for i, a in enumerate(call) if a == "--repo"]
        assert repos == [fx.PUBLIC_REPO, fx.OWN_REPO]


def test_hits_are_newest_first_and_malformed_ones_are_dropped() -> None:
    older = fx.pr_hit(updated="2026-10-02T00:00:00Z")
    newer = fx.issue_hit(updated="2026-10-09T00:00:00Z")
    bad_repo = fx.search_hit("not a repo", 3, "x")
    bad_number = {**fx.pr_hit(), "number": "4"}
    fake = fx.FakeGh(prs=[older, bad_repo, bad_number, "junk"], issues=[newer])  # type: ignore[list-item]

    hits = github.search_items(SearchScope(repos=(), mine=False, since=SINCE), limit=10,
                               runner=fake)

    assert [h.number for h in hits] == [fx.ISSUE_NUMBER, fx.PR_NUMBER]


def test_search_output_that_is_not_a_list_is_refused() -> None:
    class _Obj(fx.FakeGh):
        def __call__(self, args: list[str]) -> str:
            return "{}"

    with pytest.raises(GithubError):
        github.search_items(SearchScope(repos=(), mine=True, since=SINCE), limit=1,
                            runner=_Obj())


# --- fetch_repo / fetch_item / fetch_readme -------------------------------------------


@pytest.mark.parametrize(
    ("resource", "private"),
    [
        (fx.repo(fx.PUBLIC_REPO, private=False), False),
        (fx.repo(fx.PRIVATE_REPO, private=True), True),
        (fx.repo(fx.PUBLIC_REPO, private=False, visibility="internal"), True),
    ],
)
def test_fetch_repo_reads_privacy(resource: dict[str, Any], private: bool) -> None:
    fake = fx.FakeGh(api={f"repos/{fx.PUBLIC_REPO}": json.dumps(resource)})

    assert github.fetch_repo(fx.PUBLIC_REPO, runner=fake) is private


def test_fetch_repo_without_a_privacy_flag_is_refused() -> None:
    fake = fx.FakeGh(api={f"repos/{fx.PUBLIC_REPO}": json.dumps({"full_name": "x"})})

    with pytest.raises(GithubError):
        github.fetch_repo(fx.PUBLIC_REPO, runner=fake)


def _hit(raw: dict[str, Any]) -> github.SearchHit:
    hits = github.search_items(SearchScope(repos=(), mine=False, since=SINCE), limit=5,
                               runner=fx.FakeGh(prs=[raw] if raw["isPullRequest"] else [],
                                                issues=[] if raw["isPullRequest"] else [raw]))
    return hits[0]


def test_fetch_item_for_a_pr_reads_detail_reviews_and_both_comment_kinds() -> None:
    fake = fx.FakeGh(api=fx.standard_api(merged=True, pr_state="closed"))

    item = github.fetch_item(_hit(fx.pr_hit()), runner=fake)

    base = f"repos/{fx.PUBLIC_REPO}"
    assert fake.api_calls() == [
        f"{base}/pulls/1", f"{base}/pulls/1/reviews", f"{base}/pulls/1/comments",
        f"{base}/issues/1/comments",
    ]
    assert all("--paginate" in c for c in fake.calls[1:])
    assert item.pr is not None and item.pr["merged"] is True
    assert len(item.reviews) == 3
    assert len(item.issue_comments) == 3  # two pages joined
    assert len(item.review_comments) == 1


def test_fetch_item_for_an_issue_reads_only_its_comments() -> None:
    fake = fx.FakeGh(api=fx.standard_api())

    item = github.fetch_item(_hit(fx.issue_hit()), runner=fake)

    assert fake.api_calls() == [f"repos/{fx.PUBLIC_REPO}/issues/2/comments"]
    assert item.pr is None and item.reviews == () and item.review_comments == ()


def test_fetch_item_refuses_a_detail_that_is_not_an_object() -> None:
    api = {**fx.standard_api(), f"repos/{fx.PUBLIC_REPO}/pulls/1": "[]"}

    with pytest.raises(GithubError):
        github.fetch_item(_hit(fx.pr_hit()), runner=fx.FakeGh(api=api))


def test_fetch_readme_decodes_base64() -> None:
    readme = github.fetch_readme(fx.PUBLIC_REPO, runner=fx.FakeGh(api=fx.standard_api()))

    assert readme is not None
    assert readme.text == fx.README_TEXT
    assert (readme.path, readme.sha) == ("README.md", "a1b2c3")


@pytest.mark.parametrize(
    "raw",
    [
        None,  # the call fails (no README: HTTP 404)
        "not json",
        json.dumps({"encoding": "base64", "content": "!!!"}),
        json.dumps({"encoding": "utf-8", "content": "x"}),
        json.dumps({"encoding": "base64", "content": "/w=="}),  # not UTF-8
        json.dumps([]),
    ],
)
def test_fetch_readme_returns_none_when_unreadable(raw: str | None) -> None:
    api = {} if raw is None else {f"repos/{fx.PUBLIC_REPO}/readme": raw}

    assert github.fetch_readme(fx.PUBLIC_REPO, runner=fx.FakeGh(api=api)) is None


def test_search_output_that_is_not_json_is_refused_without_quoting_it() -> None:
    class _Garbage(fx.FakeGh):
        def __call__(self, args: list[str]) -> str:
            return f"oops {TOKEN}"

    with pytest.raises(GithubError) as info:
        github.search_items(SearchScope(repos=(), mine=False, since=SINCE), limit=1,
                            runner=_Garbage())

    assert TOKEN not in str(info.value)


def test_a_blank_readme_is_none() -> None:
    api = {f"repos/{fx.PUBLIC_REPO}/readme": json.dumps(fx.readme("  \n"))}

    assert github.fetch_readme(fx.PUBLIC_REPO, runner=fx.FakeGh(api=api)) is None
