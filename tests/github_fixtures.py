"""Synthetic `gh` output for the RM-31 suites, and the fake runner that serves it.

Every value is invented: ``example-org`` / ``example-user`` are placeholder
accounts, the logins are synthetic, and no item exists on any code host. The
shapes follow ``gh search prs --json …`` (field list from ``gh search prs
--help``, gh 2.92) and the REST resources ``gh api`` returns. The fake never
runs a process; it answers by the arguments it is called with and records them.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from brain.ingest.github import GithubError

OWNER = "example-user"
OTHER = "example-reviewer"
BOT = "example-bot"

PUBLIC_REPO = "example-org/example-repo"
PRIVATE_REPO = "example-org/example-private"
OWN_REPO = f"{OWNER}/example-tools"

PR_NUMBER = 1
ISSUE_NUMBER = 2
PRIVATE_PR_NUMBER = 7

PR_TITLE = "Add the widget cache"
PR_DESCRIPTION = "The cache keeps rendered widgets for one minute."
OWNER_REVIEW = "Looks right; the expiry test covers the edge."
OWNER_COMMENT = "Rebased on the main branch."
OWNER_INLINE = "Name this constant for its unit."
OTHER_COMMENT = "Could we cache for five minutes instead?"
OTHER_REVIEW = "Please split this into two commits."
ISSUE_TITLE = "Widget cache misses after restart"
ISSUE_DESCRIPTION = "After a restart every widget renders again."
README_TEXT = "# example-repo\n\nA synthetic repository for tests.\n"


def search_hit(
    repo: str,
    number: int,
    title: str,
    *,
    body: str = "",
    author: str = OWNER,
    state: str = "open",
    updated: str = "2026-10-20T10:00:00Z",
    created: str = "2026-10-01T09:00:00Z",
    comments: int = 0,
    labels: Sequence[str] = (),
    is_pr: bool = True,
) -> dict[str, Any]:
    """One ``gh search prs|issues --json`` result."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "repository": {"name": repo.rsplit("/", 1)[-1], "nameWithOwner": repo},
        "url": f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number}",
        "state": state,
        "updatedAt": updated,
        "createdAt": created,
        "author": {"login": author},
        "isDraft": False,
        "isPullRequest": is_pr,
        "labels": [{"name": name} for name in labels],
        "commentsCount": comments,
    }


def pr_hit(**overrides: Any) -> dict[str, Any]:
    """The public PR, authored by the owner."""
    kwargs: dict[str, Any] = {"body": PR_DESCRIPTION, "comments": 2, "labels": ("cache",)}
    kwargs.update(overrides)
    return search_hit(PUBLIC_REPO, PR_NUMBER, PR_TITLE, **kwargs)


def issue_hit(**overrides: Any) -> dict[str, Any]:
    """The public issue, authored by someone else and assigned to the owner."""
    kwargs: dict[str, Any] = {"body": ISSUE_DESCRIPTION, "author": OTHER, "is_pr": False}
    kwargs.update(overrides)
    return search_hit(PUBLIC_REPO, ISSUE_NUMBER, ISSUE_TITLE, **kwargs)


def private_pr_hit(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"body": "Rotate the internal job schedule."}
    kwargs.update(overrides)
    return search_hit(PRIVATE_REPO, PRIVATE_PR_NUMBER, "Move the nightly job", **kwargs)


def repo(name: str, *, private: bool, visibility: str | None = None) -> dict[str, Any]:
    """A ``gh api repos/O/R`` resource, with fields brain must never copy."""
    owner = name.split("/")[0]
    return {
        "full_name": name,
        "private": private,
        "visibility": visibility or ("private" if private else "public"),
        "owner": {"login": owner, "type": "Organization"},
        "html_url": f"https://github.com/{name}",
        # A field brain never reads; the adversarial test plants a token here.
        "temp_clone_token": "",
    }


def pr_detail(*, merged: bool = False, state: str = "open", draft: bool = False) -> dict[str, Any]:
    return {
        "number": PR_NUMBER,
        "state": state,
        "draft": draft,
        "merged": merged,
        "merged_at": "2026-10-21T08:00:00Z" if merged else None,
        "closed_at": "2026-10-21T08:00:00Z" if state == "closed" else None,
    }


def comment(login: str, body: str, created: str, **extra: Any) -> dict[str, Any]:
    return {"user": {"login": login}, "body": body, "created_at": created, **extra}


def review(login: str, body: str, state: str, submitted: str) -> dict[str, Any]:
    return {"user": {"login": login}, "body": body, "state": state, "submitted_at": submitted}


def default_reviews() -> list[dict[str, Any]]:
    return [
        review(OWNER, OWNER_REVIEW, "APPROVED", "2026-10-03T10:00:00Z"),
        review(OTHER, OTHER_REVIEW, "CHANGES_REQUESTED", "2026-10-02T10:00:00Z"),
        review(OTHER, "", "COMMENTED", "2026-10-02T11:00:00Z"),
    ]


def default_issue_comments() -> list[dict[str, Any]]:
    return [
        comment(OTHER, OTHER_COMMENT, "2026-10-02T09:00:00Z"),
        comment(OWNER, OWNER_COMMENT, "2026-10-04T09:00:00Z"),
        comment(BOT, "Coverage: 91%", "2026-10-04T09:30:00Z"),
    ]


def default_review_comments() -> list[dict[str, Any]]:
    return [comment(OWNER, OWNER_INLINE, "2026-10-03T10:05:00Z", path="src/cache.py")]


def readme(text: str = README_TEXT, *, sha: str = "a1b2c3") -> dict[str, Any]:
    return {
        "path": "README.md",
        "sha": sha,
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
        "html_url": "https://github.com/example/readme",
    }


def pages(*items: Sequence[dict[str, Any]]) -> str:
    """``gh api --paginate`` output: each page a JSON array, concatenated."""
    return "".join(json.dumps(list(page)) for page in items)


@dataclass
class FakeGh:
    """A ``GhRunner`` serving canned output, keyed by command shape.

    ``prs`` / ``issues`` are the search results (every role query gets the same
    list; the union step must dedupe them). ``api`` maps an API path (without
    query string) to the raw stdout. ``fail`` maps a path, or ``"search"``, to the
    exception the call raises.
    """

    prs: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    api: dict[str, str] = field(default_factory=dict)
    fail: Mapping[str, BaseException] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        assert args[0] == "gh", args
        if args[1] == "search":
            if "search" in self.fail:
                raise self.fail["search"]
            return json.dumps(self.prs if args[2] == "prs" else self.issues)
        assert args[1] == "api", args
        path = args[-1].split("?")[0]
        if path in self.fail:
            raise self.fail[path]
        if path not in self.api:
            raise GithubError("gh api failed (exit 1)")
        return self.api[path]

    def api_calls(self) -> list[str]:
        return [c[-1].split("?")[0] for c in self.calls if c[1] == "api"]

    def search_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1] == "search"]


def standard_api(*, merged: bool = False, pr_state: str = "open") -> dict[str, str]:
    """API answers for the public PR and issue, the private PR, and the READMEs."""
    return {
        "user": json.dumps({"login": OWNER, "id": 1}),
        f"repos/{PUBLIC_REPO}": json.dumps(repo(PUBLIC_REPO, private=False)),
        f"repos/{PRIVATE_REPO}": json.dumps(repo(PRIVATE_REPO, private=True)),
        f"repos/{OWN_REPO}": json.dumps(repo(OWN_REPO, private=False)),
        f"repos/{PUBLIC_REPO}/pulls/{PR_NUMBER}": json.dumps(
            pr_detail(merged=merged, state=pr_state)
        ),
        f"repos/{PUBLIC_REPO}/pulls/{PR_NUMBER}/reviews": pages(default_reviews()),
        f"repos/{PUBLIC_REPO}/pulls/{PR_NUMBER}/comments": pages(default_review_comments()),
        f"repos/{PUBLIC_REPO}/issues/{PR_NUMBER}/comments": pages(
            default_issue_comments()[:2], default_issue_comments()[2:]
        ),
        f"repos/{PUBLIC_REPO}/issues/{ISSUE_NUMBER}/comments": pages(
            [comment(OWNER, "I can take this one.", "2026-10-05T09:00:00Z")]
        ),
        f"repos/{PRIVATE_REPO}/pulls/{PRIVATE_PR_NUMBER}": json.dumps(pr_detail()),
        f"repos/{PRIVATE_REPO}/pulls/{PRIVATE_PR_NUMBER}/reviews": pages([]),
        f"repos/{PRIVATE_REPO}/pulls/{PRIVATE_PR_NUMBER}/comments": pages([]),
        f"repos/{PRIVATE_REPO}/issues/{PRIVATE_PR_NUMBER}/comments": pages([]),
        f"repos/{PUBLIC_REPO}/readme": json.dumps(readme()),
        f"repos/{PRIVATE_REPO}/readme": json.dumps(readme("# private\n\nInternal notes.\n")),
        f"repos/{OWN_REPO}/readme": json.dumps(readme("# example-tools\n\nSmall scripts.\n")),
    }
