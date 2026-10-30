"""Documents from code-host reads (RM-31): pull requests, issues and READMEs.

Pure: :mod:`brain.ingest.github` reads, this module shapes, and
:mod:`brain.cli_ingest_github` writes. What a document holds (spec RM-31,
protocol points 3 and 6):

- **The body** is a ``<owner>/<repo>#<n>: <title>`` line, the item's type, the
  description, and the **owner's own** comments: issue comments, review bodies
  and inline review comments, in time order. Other people's comments, bots'
  included, are kept only with ``with_thread``. No other person's login enters
  the default body; the item's author is ``metadata.author``.
- **Volatile fields** (:data:`VOLATILE_KEYS`: state, draft, review and comment
  counts, labels, the update and close times) are metadata only, because a body
  change re-chunks, re-embeds and re-extracts. A PR that is merged or gets a
  review from someone else therefore changes only its metadata.
- **Sensitivity.** A document from a repository that is not public is
  ``confidential``, and its body says ``Visibility: private``. The line exists so
  that a repository that turns private changes the body, and the re-ingest then
  goes through the pipeline's escalate-only sensitivity update (the RM-30
  precedent for calendar events).
- **Dating.** ``metadata.date`` (promoted to ``sent_at``) is the item's
  ``createdAt``, which never moves. A README has no date.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import ExtractedDoc
from .github import RawItem, Readme

__all__ = [
    "CONTENT_TYPES",
    "VOLATILE_KEYS",
    "GithubDoc",
    "build_item_document",
    "build_readme_document",
]

CONTENT_TYPES = {"pr": "github_pr", "issue": "github_issue", "readme": "github_readme"}
#: Metadata keys that change without the body changing; a re-run patches only these.
VOLATILE_KEYS = (
    "state",
    "draft",
    "review_count",
    "comment_count",
    "labels",
    "updated_at",
    "closed_at",
    "merged_at",
)
_KIND_LINE = {"pr": "Pull request", "issue": "Issue"}
_VISIBILITY = "Visibility: private"


@dataclass(frozen=True)
class GithubDoc:
    """One document to ingest, with its dedup key."""

    external_id: str
    doc: ExtractedDoc
    confidential: bool


@dataclass(frozen=True)
class _Section:
    login: str
    at: str
    heading: str
    body: str


def build_item_document(
    item: RawItem, *, owner_login: str, confidential: bool, with_thread: bool
) -> GithubDoc:
    """The document for one PR or issue."""
    hit, raw = item.hit, item.hit.raw
    title = " ".join(_text(raw.get("title")).split()) or "(untitled)"
    kind_line = _KIND_LINE[hit.kind] + (f" · {_VISIBILITY}" if confidential else "")
    parts = [
        f"{hit.repo}#{hit.number}: {title}\n{kind_line}",
        _text(raw.get("body")).strip() or "(no description)",
    ]
    sections = sorted(
        (s for s in _sections(item) if with_thread or s.login == owner_login),
        key=lambda s: s.at or "~",
    )
    if sections:
        parts.append(
            "## Comments\n\n" + "\n\n".join(f"### {s.heading}\n{s.body}" for s in sections)
        )
    return GithubDoc(
        external_id=hit.external_id,
        doc=ExtractedDoc(
            title=f"{hit.repo}#{hit.number} — {title}",
            content="\n\n".join(parts) + "\n",
            content_type=CONTENT_TYPES[hit.kind],
            source_path=None,
            metadata=_item_metadata(item, confidential=confidential, with_thread=with_thread),
        ),
        confidential=confidential,
    )


def _sections(item: RawItem) -> Iterable[_Section]:
    for review in item.reviews:
        state = _text(review.get("state")).lower().replace("_", " ") or "commented"
        yield from _section(review, "submitted_at", f"Review by {{}} ({state})")
    for comment in item.review_comments:
        path = _text(comment.get("path"))
        on_path = f" on {path}" if path else ""
        yield from _section(comment, "created_at", f"Review comment by {{}}{on_path}")
    for comment in item.issue_comments:
        yield from _section(comment, "created_at", "Comment by {}")


def _section(entry: Any, time_key: str, template: str) -> Iterable[_Section]:
    """Zero or one section: an entry needs a dict shape, a login and a non-empty body."""
    if not isinstance(entry, Mapping):
        return
    user = entry.get("user")
    login = _text(user.get("login")) if isinstance(user, Mapping) else ""
    body = _text(entry.get("body")).strip()
    if not login or not body:
        return
    at = _text(entry.get(time_key))
    heading = template.format(login) + (f", {at[:10]}" if at else "")
    yield _Section(login, at, heading, body)


def _item_metadata(item: RawItem, *, confidential: bool, with_thread: bool) -> dict[str, Any]:
    hit, raw, pr = item.hit, item.hit.raw, item.pr
    author = raw.get("author")
    meta: dict[str, Any] = {
        "repo": hit.repo,
        "number": hit.number,
        "item_kind": hit.kind,
        "url": _or_none(raw.get("url")),
        "author": _or_none(author.get("login")) if isinstance(author, Mapping) else None,
        "date": _or_none(raw.get("createdAt")),
        "state": _state(item),
        "draft": bool((pr or {}).get("draft", raw.get("isDraft", False))),
        "review_count": len(item.reviews),
        "comment_count": len(item.issue_comments) + len(item.review_comments),
        "labels": _labels(raw.get("labels")),
        "updated_at": hit.updated_at or None,
        "closed_at": _or_none((pr or raw).get("closed_at" if pr else "closedAt")),
    }
    if pr is not None:
        meta["merged_at"] = _or_none(pr.get("merged_at"))
    meta["with_thread"] = with_thread
    meta["repo_private"] = confidential
    return meta


def _state(item: RawItem) -> str:
    if item.pr is not None:
        if item.pr.get("merged") is True:
            return "merged"
        return _text(item.pr.get("state")).lower() or "unknown"
    return _text(item.hit.raw.get("state")).lower() or "unknown"


def _labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [
        label["name"]
        for label in raw
        if isinstance(label, Mapping) and isinstance(label.get("name"), str)
    ]


def build_readme_document(repo: str, readme: Readme, *, confidential: bool) -> GithubDoc:
    """The document for one repository's README, keyed ``<owner>/<repo>:README``."""
    header = f"{repo} README ({readme.path})" + (f"\n{_VISIBILITY}" if confidential else "")
    return GithubDoc(
        external_id=f"{repo}:README",
        doc=ExtractedDoc(
            title=f"{repo} — README",
            content=f"{header}\n\n{readme.text}",
            content_type=CONTENT_TYPES["readme"],
            source_path=None,
            metadata={
                "repo": repo,
                "path": readme.path,
                "sha": readme.sha,
                "url": f"https://github.com/{repo}",
                "repo_private": confidential,
            },
        ),
        confidential=confidential,
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
