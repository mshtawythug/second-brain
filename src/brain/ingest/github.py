"""Code-host reader for `brain ingest-github` (RM-31): the `gh` CLI calls and their parsing.

Path (b) of the new-source protocol: a shell-out to the already-authenticated
``gh`` (``gh auth login``), behind an injectable :data:`GhRunner` so tests never
run it. This module only reads; :mod:`brain.ingest.github_docs` turns what it
returns into documents, and :mod:`brain.cli_ingest_github` writes them.

**No credential and no ``gh`` text leaves this module.** ``brain`` never asks
``gh`` for its token and never requests response headers. Every
:class:`GithubError` message is written here and names only the subcommand, an
exit code, a timeout or an exception class, never ``gh``'s stdout or stderr
(which can quote an account, a URL with a token, or a server message), so the
CLI may print it as it is. Raised ``from None``, so no traceback chains the
original either.

**Pagination.** ``gh api --paginate`` prints each page as its own JSON value,
one after another; :func:`parse_pages` reads that stream, which ``--slurp``
would do only on newer ``gh`` releases.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

SOURCE_KIND = "github"

#: One ``gh`` call: argv in, stdout out; raises :class:`GithubError`.
GhRunner = Callable[[list[str]], str]

#: Per call. A search of up to 501 results, or one paginated comment list, is far below it.
GH_TIMEOUT_SECONDS = 60
#: Refuse a larger answer rather than hold it (one search page of 501 bodies is a few MB).
GH_MAX_STDOUT_BYTES = 32 * 1024 * 1024
#: ``OWNER/REPO`` as the code host spells them.
REPO_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

_SEARCH_FIELDS = (
    "number,title,body,repository,url,state,updatedAt,createdAt,author,"
    "isDraft,isPullRequest,labels,commentsCount"
)
#: The roles ``--mine`` means, per search type (the ticket: author, assignee or reviewer).
_MINE_ROLES: Mapping[str, tuple[tuple[str, str], ...]] = {
    "prs": (("--author", "@me"), ("--assignee", "@me"), ("--reviewed-by", "@me")),
    "issues": (("--author", "@me"), ("--assignee", "@me")),
}
#: ``gh`` must never wait on a prompt or print an upgrade banner into stdout.
_QUIET_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
_INSTALL_HINT = (
    "gh CLI not found on PATH: install it (https://cli.github.com), then `gh auth login`"
)


class GithubError(RuntimeError):
    """A ``gh`` call failed or answered something unusable. The message quotes no ``gh`` output."""


@dataclass(frozen=True)
class SearchScope:
    """What one run reads: ``--repo`` repositories, ``--mine``, and ``--since``."""

    repos: tuple[str, ...]
    mine: bool
    since: date


@dataclass(frozen=True)
class SearchHit:
    """One pull request or issue from ``gh search``."""

    kind: Literal["pr", "issue"]
    repo: str
    number: int
    updated_at: str
    raw: Mapping[str, Any]

    @property
    def external_id(self) -> str:
        """The dedup key, ``<owner>/<repo>#<number>`` (PRs and issues share the numbers)."""
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class RawItem:
    """A hit and what its per-item reads returned (``pr`` and reviews are PR-only)."""

    hit: SearchHit
    pr: Mapping[str, Any] | None
    reviews: tuple[Mapping[str, Any], ...]
    review_comments: tuple[Mapping[str, Any], ...]
    issue_comments: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Readme:
    text: str
    path: str
    sha: str


def gh_runner(
    args: list[str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = GH_TIMEOUT_SECONDS,
    max_stdout_bytes: int = GH_MAX_STDOUT_BYTES,
) -> str:
    """The production :data:`GhRunner`. ``which`` and ``run`` are test seams."""
    if not args:
        raise GithubError("gh runner invoked with no arguments")
    binary = which(args[0])
    if binary is None:
        raise GithubError(_INSTALL_HINT)
    label = f"`gh {args[1]}`" if len(args) > 1 else "`gh`"
    try:
        proc = run(
            [binary, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, **_QUIET_ENV},
        )
    except subprocess.TimeoutExpired:
        raise GithubError(f"{label} timed out after {timeout:g}s") from None
    except OSError as exc:
        raise GithubError(f"{label} could not run ({type(exc).__name__})") from None
    if proc.returncode != 0:
        raise GithubError(f"{label} failed (exit {proc.returncode})")
    if len(proc.stdout.encode()) > max_stdout_bytes:
        raise GithubError(
            f"{label} output exceeded {max_stdout_bytes} bytes; narrow --since or --repo"
        )
    return proc.stdout


def parse_pages(raw: str) -> list[Any]:
    """Every value in ``gh api --paginate`` output; array pages are flattened."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    pos, end = 0, len(raw)
    while True:
        while pos < end and raw[pos].isspace():
            pos += 1
        if pos == end:
            return values
        try:
            value, pos = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            raise GithubError("could not parse the gh output") from None
        values.extend(value if isinstance(value, list) else [value])


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise GithubError("could not parse the gh output") from None
    if not isinstance(value, dict):
        raise GithubError("the gh output was not a JSON object")
    return value


def _api(path: str, *, runner: GhRunner, paginate: bool = False) -> str:
    return runner(["gh", "api", *(["--paginate"] if paginate else []), path])


def viewer_login(*, runner: GhRunner) -> str:
    """The authenticated account's login (``gh api user``); its other fields are ignored."""
    login = _object(_api("user", runner=runner)).get("login")
    if not isinstance(login, str) or not login:
        raise GithubError("gh api user returned no login")
    return login


def search_items(scope: SearchScope, *, limit: int, runner: GhRunner) -> list[SearchHit]:
    """Every PR and issue in scope, deduplicated, newest ``updatedAt`` first."""
    hits: dict[str, SearchHit] = {}
    for kind in ("prs", "issues"):
        roles: tuple[tuple[str, ...], ...] = _MINE_ROLES[kind] if scope.mine else ((),)
        for role in roles:
            for hit in _search(kind, role, scope, limit=limit, runner=runner):
                hits.setdefault(hit.external_id, hit)
    return sorted(hits.values(), key=lambda h: (h.updated_at, h.external_id), reverse=True)


def _search(
    kind: str, role: tuple[str, ...], scope: SearchScope, *, limit: int, runner: GhRunner
) -> list[SearchHit]:
    args = ["gh", "search", kind, *role]
    for repo in scope.repos:
        args += ["--repo", repo]
    args += [
        "--updated", f">={scope.since.isoformat()}",
        "--sort", "updated", "--order", "desc",
        "--limit", str(limit),
        "--json", _SEARCH_FIELDS,
    ]
    try:
        results = json.loads(runner(args))
    except json.JSONDecodeError:
        raise GithubError(f"could not parse the gh search {kind} output") from None
    if not isinstance(results, list):
        raise GithubError(f"the gh search {kind} output was not a list")
    item_kind: Literal["pr", "issue"] = "pr" if kind == "prs" else "issue"
    return [hit for raw in results if (hit := _hit(item_kind, raw)) is not None]


def _hit(kind: Literal["pr", "issue"], raw: Any) -> SearchHit | None:
    """A :class:`SearchHit`, or ``None`` for a result missing what the key needs."""
    if not isinstance(raw, dict):
        return None
    repository = raw.get("repository")
    repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
    number = raw.get("number")
    if not isinstance(repo, str) or not REPO_PATTERN.match(repo):
        return None
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    updated = raw.get("updatedAt")
    return SearchHit(kind, repo, number, updated if isinstance(updated, str) else "", raw)


def fetch_repo(repo: str, *, runner: GhRunner) -> bool:
    """Whether ``repo`` is not public: ``private``, or a visibility other than ``public``."""
    resource = _object(_api(f"repos/{repo}", runner=runner))
    private = resource.get("private")
    if not isinstance(private, bool):
        raise GithubError("gh api repos returned no privacy flag")
    visibility = resource.get("visibility")
    return private or (isinstance(visibility, str) and visibility != "public")


def fetch_item(hit: SearchHit, *, runner: GhRunner) -> RawItem:
    """The reads one hit needs: for a PR its detail, reviews and inline comments."""
    base = f"repos/{hit.repo}"
    pr: dict[str, Any] | None = None
    reviews: tuple[Mapping[str, Any], ...] = ()
    review_comments: tuple[Mapping[str, Any], ...] = ()
    if hit.kind == "pr":
        pr = _object(_api(f"{base}/pulls/{hit.number}", runner=runner))
        reviews = _list(f"{base}/pulls/{hit.number}/reviews", runner=runner)
        review_comments = _list(f"{base}/pulls/{hit.number}/comments", runner=runner)
    issue_comments = _list(f"{base}/issues/{hit.number}/comments", runner=runner)
    return RawItem(hit, pr, reviews, review_comments, issue_comments)


def _list(path: str, *, runner: GhRunner) -> tuple[Mapping[str, Any], ...]:
    raw = _api(f"{path}?per_page=100", runner=runner, paginate=True)
    return tuple(v for v in parse_pages(raw) if isinstance(v, dict))


def fetch_readme(repo: str, *, runner: GhRunner) -> Readme | None:
    """The repository's README, or ``None`` when it has none or it cannot be read."""
    try:
        resource = _object(_api(f"repos/{repo}/readme", runner=runner))
    except GithubError:
        return None
    content, encoding = resource.get("content"), resource.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        return None
    try:
        # The API wraps the base64 at 60 columns; strict decoding after the
        # newlines are removed rejects anything else.
        text = base64.b64decode("".join(content.split()), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    path, sha = resource.get("path"), resource.get("sha")
    return Readme(
        text, path if isinstance(path, str) else "README", sha if isinstance(sha, str) else ""
    )
