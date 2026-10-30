"""Document building for `brain ingest-github` (RM-31): bodies, metadata, sensitivity.

Pure: synthetic ``RawItem`` values from ``tests/github_fixtures.py``, no runner.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from brain.ingest import github, github_docs
from brain.ingest.github import RawItem, Readme, SearchHit
from tests import github_fixtures as fx


def _hit(raw: dict[str, Any]) -> SearchHit:
    hit = github._hit("pr" if raw["isPullRequest"] else "issue", raw)
    assert hit is not None
    return hit


def _pr_item(**detail: Any) -> RawItem:
    return RawItem(
        hit=_hit(fx.pr_hit()),
        pr=fx.pr_detail(**detail),
        reviews=tuple(fx.default_reviews()),
        review_comments=tuple(fx.default_review_comments()),
        issue_comments=tuple(fx.default_issue_comments()),
    )


def _issue_item() -> RawItem:
    return RawItem(
        hit=_hit(fx.issue_hit()),
        pr=None,
        reviews=(),
        review_comments=(),
        issue_comments=(fx.comment(fx.OTHER, fx.OTHER_COMMENT, "2026-10-02T09:00:00Z"),),
    )


def _build(item: RawItem, *, confidential: bool = False, with_thread: bool = False) -> Any:
    return github_docs.build_item_document(
        item, owner_login=fx.OWNER, confidential=confidential, with_thread=with_thread
    )


# --- pull requests ------------------------------------------------------------------


def test_a_pr_keeps_the_description_and_only_the_owners_comments() -> None:
    gd = _build(_pr_item())

    body = gd.doc.content
    assert gd.external_id == f"{fx.PUBLIC_REPO}#{fx.PR_NUMBER}"
    assert gd.doc.title == f"{fx.PUBLIC_REPO}#{fx.PR_NUMBER} — {fx.PR_TITLE}"
    assert gd.doc.content_type == "github_pr"
    assert gd.doc.source_path is None
    assert body.startswith(f"{fx.PUBLIC_REPO}#{fx.PR_NUMBER}: {fx.PR_TITLE}\nPull request\n")
    assert fx.PR_DESCRIPTION in body
    for own in (fx.OWNER_REVIEW, fx.OWNER_COMMENT, fx.OWNER_INLINE):
        assert own in body
    for other in (fx.OTHER_COMMENT, fx.OTHER_REVIEW, "Coverage: 91%"):
        assert other not in body
    # No third party's login enters the default body.
    assert fx.OTHER not in body and fx.BOT not in body
    assert "Visibility" not in body
    assert gd.confidential is False


def test_owner_comments_are_in_time_order_with_their_kind() -> None:
    body = _build(_pr_item()).doc.content

    review = body.index(f"### Review by {fx.OWNER} (approved), 2026-10-03")
    inline = body.index(f"### Review comment by {fx.OWNER} on src/cache.py, 2026-10-03")
    comment = body.index(f"### Comment by {fx.OWNER}, 2026-10-04")
    assert review < inline < comment


def test_with_thread_keeps_everyones_comments_and_drops_empty_reviews() -> None:
    body = _build(_pr_item(), with_thread=True).doc.content

    for text in (fx.OTHER_COMMENT, fx.OTHER_REVIEW, "Coverage: 91%", fx.OWNER_COMMENT):
        assert text in body
    assert f"### Review by {fx.OTHER} (changes requested), 2026-10-02" in body
    assert body.count(f"### Review by {fx.OTHER}") == 1  # the empty review is not a section


def test_pr_metadata_holds_the_volatile_fields() -> None:
    meta = _build(_pr_item(merged=True, state="closed")).doc.metadata

    assert meta == {
        "repo": fx.PUBLIC_REPO,
        "number": fx.PR_NUMBER,
        "item_kind": "pr",
        "url": f"https://github.com/{fx.PUBLIC_REPO}/pull/{fx.PR_NUMBER}",
        "author": fx.OWNER,
        "date": "2026-10-01T09:00:00Z",
        "state": "merged",
        "draft": False,
        "review_count": 3,
        "comment_count": 4,
        "labels": ["cache"],
        "updated_at": "2026-10-20T10:00:00Z",
        "closed_at": "2026-10-21T08:00:00Z",
        "merged_at": "2026-10-21T08:00:00Z",
        "with_thread": False,
        "repo_private": False,
    }
    assert set(github_docs.VOLATILE_KEYS) <= set(meta)


def test_a_state_change_changes_metadata_not_the_body() -> None:
    open_doc = _build(_pr_item()).doc
    merged_doc = _build(_pr_item(merged=True, state="closed")).doc

    assert open_doc.content == merged_doc.content
    assert open_doc.metadata["state"] == "open"
    assert merged_doc.metadata["state"] == "merged"


def test_a_closed_unmerged_pr_is_closed_and_a_draft_is_flagged() -> None:
    assert _build(_pr_item(state="closed")).doc.metadata["state"] == "closed"
    assert _build(_pr_item(draft=True)).doc.metadata["draft"] is True


def test_a_confidential_pr_says_so_in_the_body() -> None:
    gd = _build(_pr_item(), confidential=True)

    assert gd.confidential is True
    assert "Pull request · Visibility: private\n" in gd.doc.content
    assert gd.doc.metadata["repo_private"] is True


# --- issues -------------------------------------------------------------------------


def test_an_issue_by_someone_else_keeps_its_description_but_no_login() -> None:
    gd = _build(_issue_item())

    assert gd.doc.content_type == "github_issue"
    assert fx.ISSUE_DESCRIPTION in gd.doc.content
    assert fx.OTHER_COMMENT not in gd.doc.content
    assert fx.OTHER not in gd.doc.content
    assert gd.doc.metadata["author"] == fx.OTHER
    assert gd.doc.metadata["item_kind"] == "issue"
    assert gd.doc.metadata["state"] == "open"
    assert gd.doc.metadata["review_count"] == 0
    assert "merged_at" not in gd.doc.metadata
    assert "\nIssue\n" in gd.doc.content


def test_missing_fields_degrade_to_placeholders() -> None:
    raw = fx.search_hit(fx.PUBLIC_REPO, 9, "   ", is_pr=False)
    for key in ("body", "author", "labels", "createdAt", "url", "state", "commentsCount"):
        raw.pop(key)
    raw["labels"] = ["junk", {"name": 3}, {"name": "kept"}]
    weird_comments = (
        "junk",
        {"user": None, "body": "anonymous"},
        {"user": {"login": fx.OWNER}, "body": "   "},
        {"user": {"login": fx.OWNER}, "body": "dateless"},
    )
    item = RawItem(_hit(raw), None, (), (), weird_comments)  # type: ignore[arg-type]

    gd = _build(item)

    assert gd.doc.title == f"{fx.PUBLIC_REPO}#9 — (untitled)"
    assert "(no description)" in gd.doc.content
    assert "anonymous" not in gd.doc.content
    assert f"### Comment by {fx.OWNER}\ndateless" in gd.doc.content
    meta = gd.doc.metadata
    assert meta["author"] is None and meta["date"] is None and meta["url"] is None
    assert meta["labels"] == ["kept"]
    assert meta["state"] == "unknown"
    assert meta["comment_count"] == 4


# --- READMEs ------------------------------------------------------------------------


def test_a_readme_document() -> None:
    gd = github_docs.build_readme_document(
        fx.PUBLIC_REPO, Readme(fx.README_TEXT, "README.md", "a1b2c3"), confidential=False
    )

    assert gd.external_id == f"{fx.PUBLIC_REPO}:README"
    assert gd.doc.title == f"{fx.PUBLIC_REPO} — README"
    assert gd.doc.content_type == "github_readme"
    assert gd.doc.content == f"{fx.PUBLIC_REPO} README (README.md)\n\n{fx.README_TEXT}"
    assert gd.doc.metadata == {
        "repo": fx.PUBLIC_REPO,
        "path": "README.md",
        "sha": "a1b2c3",
        "url": f"https://github.com/{fx.PUBLIC_REPO}",
        "repo_private": False,
    }
    assert "date" not in gd.doc.metadata


def test_a_private_readme_is_confidential_and_says_so() -> None:
    gd = github_docs.build_readme_document(
        fx.PRIVATE_REPO, Readme("x", "README.md", ""), confidential=True
    )

    assert gd.confidential is True
    assert gd.doc.content.startswith(f"{fx.PRIVATE_REPO} README (README.md)\nVisibility: private\n")


# --- the fixtures never carry real-looking data --------------------------------------


@pytest.mark.parametrize("value", [fx.OWNER, fx.OTHER, fx.BOT, fx.PUBLIC_REPO, fx.PRIVATE_REPO])
def test_fixture_identities_are_placeholders(value: str) -> None:
    assert "example" in json.dumps(value)


def test_labels_that_are_not_a_list_are_dropped() -> None:
    raw = {**fx.pr_hit(), "labels": "cache"}
    item = RawItem(_hit(raw), fx.pr_detail(), (), (), ())

    assert _build(item).doc.metadata["labels"] == []
