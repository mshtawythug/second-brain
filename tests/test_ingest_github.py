"""The `gh` reads behind `brain ingest-github` (RM-31): runner, parsing, search shapes.

Pure: no database, and no real ``gh``. The runner tests inject ``which`` and
``run``; one test runs a real subprocess, but against a throwaway script named
``gh`` in ``tmp_path``, never the installed CLI.
"""
from __future__ import annotations

import json
import time
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


def _script(tmp_path: Path, body: str) -> str:
    """A throwaway executable named ``gh`` (never the installed CLI)."""
    script = tmp_path / "gh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_runner_returns_stdout_with_a_quiet_environment(tmp_path: Path) -> None:
    env = "$GH_PROMPT_DISABLED:$GH_NO_UPDATE_NOTIFIER:$NO_COLOR"
    gh = _script(tmp_path, f'printf \'%s\' "{env}:$*"')

    out = github.gh_runner(["gh", "api", "user"], which=lambda _n: gh)

    assert out == "1:1:1:api user"


def test_runner_gives_gh_no_stdin(tmp_path: Path) -> None:
    """``gh`` reads stdin for some prompts; it must see end of file, never wait on a terminal."""
    gh = _script(tmp_path, "cat; printf done")

    assert github.gh_runner(["gh", "api", "user"], which=lambda _n: gh, timeout=5) == "done"


def test_runner_without_gh_on_path_names_the_install() -> None:
    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: None)

    assert "gh CLI not found on PATH" in str(info.value)
    assert "gh auth login" in str(info.value)


def test_runner_refuses_empty_args() -> None:
    with pytest.raises(GithubError):
        github.gh_runner([], which=lambda _n: "/usr/local/bin/gh")


def test_a_real_failing_process_never_surfaces_its_stderr(tmp_path: Path) -> None:
    gh = _script(tmp_path, f"echo 'token {FINE_TOKEN}' >&2\necho 'partial {TOKEN}'\nexit 4")

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: gh)

    assert str(info.value) == "`gh api` failed (exit 4)"
    assert FINE_TOKEN not in str(info.value) and TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_a_hung_process_is_killed_at_the_timeout(tmp_path: Path) -> None:
    gh = _script(tmp_path, f"echo 'token {TOKEN}' >&2\nsleep 30")
    started = time.monotonic()

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "search", "prs"], which=lambda _n: gh, timeout=0.3)

    assert str(info.value) == "`gh search` timed out after 0.3s"
    assert time.monotonic() - started < 10


def test_output_over_the_cap_is_refused_while_reading(tmp_path: Path) -> None:
    """The cap kills ``gh`` mid-stream: the script would otherwise run for 30 s."""
    gh = _script(tmp_path, "head -c 300000 /dev/zero\nsleep 30")
    started = time.monotonic()

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: gh, max_stdout_bytes=1000)

    assert str(info.value) == "`gh api` output exceeded 1000 bytes; narrow --since or --repo"
    assert time.monotonic() - started < 10


def test_output_at_the_cap_is_returned(tmp_path: Path) -> None:
    gh = _script(tmp_path, "printf 12345")

    out = github.gh_runner(["gh", "api", "user"], which=lambda _n: gh, max_stdout_bytes=5)

    assert out == "12345"


def test_runner_os_error_is_a_github_error_by_class() -> None:
    def popen(*_a: Any, **_k: Any) -> Any:
        raise PermissionError(13, f"denied {TOKEN}")

    with pytest.raises(GithubError) as info:
        github.gh_runner(["gh", "api", "user"], which=lambda _n: "/x/gh", popen=popen)

    assert str(info.value) == "`gh api` could not run (PermissionError)"


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


def test_repo_scope_narrows_every_role_search_to_the_named_repos() -> None:
    """``--repo`` narrows the owner's searches; it never reads every contributor's items."""
    fake = fx.FakeGh()
    scope = SearchScope(repos=(fx.PUBLIC_REPO, fx.OWN_REPO), mine=False, since=SINCE)

    github.search_items(scope, limit=10, runner=fake)

    calls = fake.search_calls()
    assert [(c[2], c[3], c[4]) for c in calls] == [
        ("prs", "--author", "@me"), ("prs", "--assignee", "@me"), ("prs", "--reviewed-by", "@me"),
        ("issues", "--author", "@me"), ("issues", "--assignee", "@me"),
    ]
    for call in calls:
        repos = [call[i + 1] for i, a in enumerate(call) if a == "--repo"]
        assert repos == [fx.PUBLIC_REPO, fx.OWN_REPO]


def test_each_search_asks_only_for_fields_its_gh_type_has() -> None:
    """gh 2.92 refuses an unknown ``--json`` field (``issues`` has no ``isDraft``) with exit 1."""
    fake = fx.FakeGh()

    github.search_items(SearchScope(repos=(), mine=True, since=SINCE), limit=1, runner=fake)

    allowed = {"prs": fx.GH_SEARCH_PR_FIELDS, "issues": fx.GH_SEARCH_ISSUE_FIELDS}
    for call in fake.search_calls():
        fields = set(call[call.index("--json") + 1].split(","))
        assert fields <= allowed[call[2]], (call[2], fields - allowed[call[2]])
    pr_fields = fake.search_calls()[0][fake.search_calls()[0].index("--json") + 1]
    assert "isDraft" in pr_fields.split(",")


def test_another_persons_item_is_returned_only_through_an_owner_role() -> None:
    theirs = fx.search_hit(fx.PUBLIC_REPO, 5, "Their change", author=fx.OTHER)
    reviewed = fx.search_hit(fx.PUBLIC_REPO, 6, "Reviewed change", author=fx.OTHER)
    fake = fx.FakeGh(prs=[theirs, reviewed], reviewed={f"{fx.PUBLIC_REPO}#6"})

    hits = github.search_items(
        SearchScope(repos=(fx.PUBLIC_REPO,), mine=False, since=SINCE), limit=10, runner=fake
    )

    assert [h.number for h in hits] == [6]


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("example-org/example-repo", True),
        ("example-org/.github", True),
        ("a/b.c_d-e", True),
        ("a/b\n", False),
        ("a/b/c", False),
        ("owner/.", False),
        ("owner/..", False),
        ("-owner/repo", False),
        ("owner", False),
        (" owner/repo", False),
    ],
)
def test_is_repo_name(name: str, valid: bool) -> None:
    assert github.is_repo_name(name) is valid


def test_deeply_nested_json_is_a_github_error_everywhere() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    fake = fx.FakeGh(api={"user": deep})

    class _Deep(fx.FakeGh):
        def __call__(self, args: list[str]) -> str:
            return deep

    with pytest.raises(GithubError):
        github.parse_pages(deep)
    with pytest.raises(GithubError):
        github.viewer_login(runner=fake)
    with pytest.raises(GithubError):
        github.search_items(SearchScope(repos=(), mine=True, since=SINCE), limit=1,
                            runner=_Deep())


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


def test_killing_a_group_that_is_already_gone_is_quiet(tmp_path: Path) -> None:
    import subprocess

    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
    proc.wait()

    github._kill_group(proc)  # no ProcessLookupError escapes
