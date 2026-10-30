"""`brain ingest-github` end to end (RM-31), through the CLI and the 5434 database.

The ``gh`` runner is the synthetic ``FakeGh`` (``tests/github_fixtures.py``),
swapped in with ``monkeypatch.setattr`` (a standard test double, rule 13). No
code host is ever read, and the installed ``gh`` is never run: the adversarial
test at the end runs a throwaway script named ``gh`` from ``tmp_path``.
"""
from __future__ import annotations

import functools
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.ingest import github
from brain.ingest.github import GithubError
from tests import github_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY, SYNTHETIC_POSITIVES

TOKEN = SYNTHETIC_POSITIVES["github_pat"]
FINE_TOKEN = SYNTHETIC_POSITIVES["github_pat_fine_grained"]
SINCE = ["--since", "2026-10-01"]
SCOPE_REFUSAL = "ingest-github requires --repo or --mine, and --since"
PR_ID = f"{fx.PUBLIC_REPO}#{fx.PR_NUMBER}"
ISSUE_ID = f"{fx.PUBLIC_REPO}#{fx.ISSUE_NUMBER}"
PRIVATE_ID = f"{fx.PRIVATE_REPO}#{fx.PRIVATE_PR_NUMBER}"


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> fx.FakeGh:
    fake = fx.FakeGh(
        prs=[fx.pr_hit(), fx.private_pr_hit(), fx.search_hit(fx.OWN_REPO, 3, "Add a script")],
        issues=[fx.issue_hit()],
        api={
            **fx.standard_api(),
            f"repos/{fx.OWN_REPO}/pulls/3": json.dumps(fx.pr_detail()),
            f"repos/{fx.OWN_REPO}/pulls/3/reviews": fx.pages([]),
            f"repos/{fx.OWN_REPO}/pulls/3/comments": fx.pages([]),
            f"repos/{fx.OWN_REPO}/issues/3/comments": fx.pages([]),
        },
    )
    monkeypatch.setattr("brain.cli_ingest_github.github_gh_runner", fake)
    return fake


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-github", *args])


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.external_id, d.id::text, d.title, d.content, d.content_type,
               d.sensitivity, d.sent_at, d.metadata, d.tags
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'github'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "sensitivity", "sent_at", "metadata",
            "tags")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_ids(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM chunks WHERE document_id = %s ORDER BY id", (document_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


# --- scope (AC1) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [[], ["--mine"], ["--repo", fx.PUBLIC_REPO], SINCE, ["--no-readmes", *SINCE]]
)
def test_missing_scope_is_refused(args: list[str], gh: fx.FakeGh) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert SCOPE_REFUSAL in result.output
    assert gh.calls == []


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--mine", "--since", "2026/10/01"], "--since must be YYYY-MM-DD"),
        (["--repo", "example-org", *SINCE], "--repo must be OWNER/REPO"),
        (["--repo", "a/b/c", *SINCE], "--repo must be OWNER/REPO"),
    ],
)
def test_malformed_scope_is_refused(args: list[str], reason: str, gh: fx.FakeGh) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert f"{SCOPE_REFUSAL}: {reason}" in result.output
    assert gh.calls == []


def test_a_future_since_is_refused(gh: fx.FakeGh) -> None:
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()

    result = _run("--mine", "--since", tomorrow)

    assert result.exit_code == 2
    assert "--since is in the future" in result.output
    assert gh.calls == []


# --- gh failures ------------------------------------------------------------------------


def test_a_missing_gh_exits_1_with_the_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = functools.partial(github.gh_runner, which=lambda _n: None)
    monkeypatch.setattr("brain.cli_ingest_github.github_gh_runner", runner)

    result = _run("--mine", *SINCE)

    assert result.exit_code == 1
    assert "gh CLI not found on PATH" in result.output
    assert "gh auth login" in result.output


def test_a_search_failure_exits_1(gh: fx.FakeGh) -> None:
    gh.fail = {"search": GithubError("`gh search` failed (exit 1)")}

    result = _run("--mine", *SINCE)

    assert result.exit_code == 1
    assert "`gh search` failed (exit 1)" in result.output


# --- dry run ------------------------------------------------------------------------------


def test_dry_run_lists_and_writes_nothing(gh: fx.FakeGh, test_db: psycopg.Connection) -> None:
    result = _run("--mine", *SINCE, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "found 4 item(s) and 1 README(s)" in result.output
    assert f"pr     {PR_ID}  {fx.PR_TITLE}" in result.output
    assert f"readme {fx.OWN_REPO}" in result.output
    assert gh.api_calls() == ["user"]
    assert _docs(test_db) == {}


def test_dry_run_strips_control_characters_from_titles(gh: fx.FakeGh) -> None:
    gh.prs = [fx.search_hit(fx.PUBLIC_REPO, fx.PR_NUMBER,
                            "Fix \x1b[2J\x1b]0;owned\x07 the\u202e cache\u200b")]
    gh.issues = []

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--dry-run")

    assert result.exit_code == 0, result.output
    for ch in ("\x1b", "\x07", "\u202e", "\u200b"):
        assert ch not in result.output
    assert f"{PR_ID}  Fix [2J]0;owned the cache" in result.output


# --- ingest (AC2, AC3) ----------------------------------------------------------------------


def test_mine_ingests_items_with_owner_comments_and_private_as_confidential(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run("--mine", *SINCE, "--tag", "Code Review")

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    assert set(docs) == {PR_ID, ISSUE_ID, PRIVATE_ID, f"{fx.OWN_REPO}#3", f"{fx.OWN_REPO}:README"}
    pr = docs[PR_ID]
    assert pr["content_type"] == "github_pr"
    assert fx.OWNER_REVIEW in pr["content"] and fx.OWNER_COMMENT in pr["content"]
    assert fx.OTHER_COMMENT not in pr["content"] and fx.OTHER not in pr["content"]
    assert pr["sensitivity"] == "normal"
    assert pr["sent_at"] == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert pr["metadata"]["state"] == "open"
    assert pr["tags"] == ["code-review"]
    assert docs[PRIVATE_ID]["sensitivity"] == "confidential"
    assert "Visibility: private" in docs[PRIVATE_ID]["content"]
    assert docs[ISSUE_ID]["content_type"] == "github_issue"
    # R1: under --mine only the owner's own repository's README; never the
    # README of a repository the owner merely worked in.
    assert docs[f"{fx.OWN_REPO}:README"]["content_type"] == "github_readme"
    assert f"repos/{fx.PUBLIC_REPO}/readme" not in gh.api_calls()
    assert "5 ingested, 0 updated, 0 refreshed, 0 unchanged, 0 failed" in result.output


def test_repo_scope_reads_that_repositorys_readme(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE)

    assert result.exit_code == 0, result.output
    assert set(_docs(test_db)) == {PR_ID, f"{fx.PUBLIC_REPO}:README"}


def test_repo_scope_never_ingests_another_persons_item(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    """The #100 ruling: ``--repo`` narrows the owner's activity, it does not widen to a repo's."""
    theirs = fx.search_hit(fx.PUBLIC_REPO, 5, "Their unrelated change", author=fx.OTHER)
    reviewed = fx.search_hit(fx.PUBLIC_REPO, 6, "A change the owner reviewed", author=fx.OTHER)
    gh.prs, gh.issues = [fx.pr_hit(), theirs, reviewed], []
    gh.reviewed = {f"{fx.PUBLIC_REPO}#6"}
    # #5 is fully readable, so if a search ever returned it, it WOULD be ingested.
    for n in (5, 6):
        base = f"repos/{fx.PUBLIC_REPO}"
        gh.api[f"{base}/pulls/{n}"] = json.dumps(fx.pr_detail())
        for path in (f"{base}/pulls/{n}/reviews", f"{base}/pulls/{n}/comments",
                     f"{base}/issues/{n}/comments"):
            gh.api[path] = fx.pages([])

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert result.exit_code == 0, result.output
    assert set(_docs(test_db)) == {PR_ID, f"{fx.PUBLIC_REPO}#6"}
    assert f"{fx.PUBLIC_REPO}#5" not in result.output
    assert "0 failed" in result.output
    assert f"repos/{fx.PUBLIC_REPO}/pulls/5" not in gh.api_calls()


def test_no_readmes_and_a_repo_without_one(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []
    del gh.api[f"repos/{fx.PUBLIC_REPO}/readme"]

    without = _run("--repo", fx.PUBLIC_REPO, *SINCE)
    skipped = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert "1 README(s) could not be read (none, or the read failed)" in without.output
    assert "found 1 item(s) and 0 README(s)" in skipped.output
    assert "could not be read" not in skipped.output
    assert set(_docs(test_db)) == {PR_ID}


def test_with_thread_keeps_other_peoples_comments(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []

    _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")
    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes", "--with-thread")

    assert "1 updated" in result.output
    assert fx.OTHER_COMMENT in _docs(test_db)[PR_ID]["content"]


def test_an_unreadable_repository_is_stored_confidential(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []
    gh.fail = {f"repos/{fx.PUBLIC_REPO}": GithubError("`gh api` failed (exit 1)")}

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert f"could not read {fx.PUBLIC_REPO} (`gh api` failed (exit 1)); " in result.output
    assert _docs(test_db)[PR_ID]["sensitivity"] == "confidential"


# --- re-runs (AC4) --------------------------------------------------------------------------


def test_a_rerun_is_a_no_op_that_fetches_nothing_per_item(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    _run("--mine", *SINCE)
    embeds = embedder.embed_calls
    gh.calls.clear()

    result = _run("--mine", *SINCE)

    assert result.exit_code == 0, result.output
    assert "0 ingested, 0 updated, 0 refreshed, 5 unchanged, 0 failed" in result.output
    per_item = [p for p in gh.api_calls() if "/pulls/" in p or "/issues/" in p]
    assert per_item == []
    assert embedder.embed_calls == embeds


def test_a_repo_that_turns_private_is_reingested_confidential(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    """The short-circuit must not hold a document at ``normal`` once its repo is private."""
    gh.prs, gh.issues = [fx.pr_hit()], []
    _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")
    assert _docs(test_db)[PR_ID]["sensitivity"] == "normal"
    gh.api[f"repos/{fx.PUBLIC_REPO}"] = json.dumps(fx.repo(fx.PUBLIC_REPO, private=True))

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")  # same updatedAt

    assert "1 updated" in result.output
    doc = _docs(test_db)[PR_ID]
    assert doc["sensitivity"] == "confidential"
    assert "Visibility: private" in doc["content"]
    assert doc["metadata"]["repo_private"] is True


def test_a_repo_whose_visibility_can_no_longer_be_read_is_reingested_confidential(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []
    _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")
    assert _docs(test_db)[PR_ID]["sensitivity"] == "normal"
    gh.fail = {f"repos/{fx.PUBLIC_REPO}": GithubError("`gh api` failed (exit 1)")}

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")  # same updatedAt

    assert "its documents are stored confidential" in result.output
    assert _docs(test_db)[PR_ID]["sensitivity"] == "confidential"


def test_a_state_change_touches_metadata_only(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []
    _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")
    before = _docs(test_db)[PR_ID]
    chunks = _chunk_ids(test_db, before["id"])
    embeds = embedder.embed_calls
    gh.prs = [fx.pr_hit(updated="2026-10-21T08:00:00Z", state="closed")]
    gh.api[f"repos/{fx.PUBLIC_REPO}/pulls/{fx.PR_NUMBER}"] = json.dumps(
        fx.pr_detail(merged=True, state="closed")
    )

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert "1 refreshed" in result.output
    after = _docs(test_db)[PR_ID]
    assert after["id"] == before["id"] and after["content"] == before["content"]
    assert after["metadata"]["state"] == "merged"
    assert after["metadata"]["updated_at"] == "2026-10-21T08:00:00Z"
    assert _chunk_ids(test_db, after["id"]) == chunks
    assert embedder.embed_calls == embeds


def test_a_new_owner_comment_updates_the_body_in_place(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    gh.prs, gh.issues = [fx.pr_hit()], []
    _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")
    first = _docs(test_db)[PR_ID]["id"]
    gh.prs = [fx.pr_hit(updated="2026-10-22T08:00:00Z")]
    gh.api[f"repos/{fx.PUBLIC_REPO}/issues/{fx.PR_NUMBER}/comments"] = fx.pages(
        [*fx.default_issue_comments(), fx.comment(fx.OWNER, "Merging now.", "2026-10-22T08:00:00Z")]
    )

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert "1 updated" in result.output
    doc = _docs(test_db)[PR_ID]
    assert doc["id"] == first and "Merging now." in doc["content"]


# --- failures and the guard ---------------------------------------------------------------


def test_a_per_item_failure_does_not_stop_the_run(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    failing = f"repos/{fx.PUBLIC_REPO}/pulls/{fx.PR_NUMBER}"
    gh.fail = {failing: GithubError("`gh api` failed (exit 1)")}

    result = _run("--mine", *SINCE, "--no-readmes")

    assert result.exit_code == 0, result.output
    assert f"failed {PR_ID}: `gh api` failed (exit 1)" in result.output
    assert "3 ingested, 0 updated, 0 refreshed, 0 unchanged, 1 failed" in result.output
    assert PR_ID not in _docs(test_db)


def test_a_database_error_is_reported_by_class(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise psycopg.errors.CheckViolation("row value SECRETROW")

    monkeypatch.setattr("brain.cli_ingest_github.ingest_document", _boom)
    gh.prs, gh.issues = [fx.pr_hit()], []

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert f"failed {PR_ID}: CheckViolation" in result.output
    assert "SECRETROW" not in result.output


def test_the_secret_guard_runs_on_every_body(
    gh: fx.FakeGh, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    gh.prs = [fx.pr_hit(body=f"The key is {SYNTHETIC_AWS_KEY}.")]
    gh.issues = [fx.issue_hit()]

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--no-readmes")

    assert f"refused {PR_ID}" in result.output
    assert set(_docs(test_db)) == {ISSUE_ID}
    assert SYNTHETIC_AWS_KEY not in result.output


# --- the cap (AC6) ---------------------------------------------------------------------------


def test_a_run_stops_at_500_items_and_says_so(gh: fx.FakeGh) -> None:
    gh.prs = [
        fx.search_hit(
            fx.PUBLIC_REPO, n, f"Change {n}", updated=f"2026-10-{1 + n % 28:02d}T00:00:00Z"
        )
        for n in range(1, 503)
    ]
    gh.issues = []

    result = _run("--repo", fx.PUBLIC_REPO, *SINCE, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "stopped at the 500-item cap: more than 500 matched; narrow --since or --repo" in (
        result.output
    )
    assert "found 500 item(s) and 0 README(s)" in result.output
    assert result.output.count("\n  pr     ") == 500
    limits = {c[c.index("--limit") + 1] for c in gh.search_calls()}
    assert limits == {"501"}


def test_readmes_fill_only_the_budget_left(gh: fx.FakeGh) -> None:
    gh.prs = [fx.search_hit(fx.PUBLIC_REPO, n, f"Change {n}") for n in range(1, 500)]
    gh.issues = []

    result = _run("--repo", fx.PUBLIC_REPO, "--repo", fx.OWN_REPO, *SINCE, "--dry-run")

    assert "found 499 item(s) and 1 README(s)" in result.output
    assert "stopped at the 500-item cap: 2 README(s) are due" in result.output


# --- adversarial: no credential from gh reaches a document or the terminal --------------------


_FAKE_GH = """#!{python}
import json, os, sys
responses = json.load(open(os.environ["RM31_FAKE_GH"]))
key = sys.argv[1] if sys.argv[1] == "search" else sys.argv[-1].split("?")[0]
if key == "search":
    key = "search " + sys.argv[2]
if key not in responses:
    sys.stderr.write("HTTP 401: Bad credentials {fine} {token}\\n")
    sys.stdout.write("partial {token}\\n")
    sys.exit(1)
sys.stderr.write("warning: token {token} in use\\n")
sys.stdout.write(responses[key])
"""


def test_no_token_from_gh_output_or_stderr_reaches_documents_or_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: CountingEmbedder,
    test_db: psycopg.Connection,
) -> None:
    script = tmp_path / "gh"
    script.write_text(_FAKE_GH.format(python=sys.executable, token=TOKEN, fine=FINE_TOKEN))
    script.chmod(0o755)
    responses = {
        "search prs": json.dumps([fx.pr_hit()]),
        "search issues": json.dumps([]),
        # Fields brain never reads carry tokens: they must not be copied anywhere.
        "user": json.dumps({"login": fx.OWNER, "token": TOKEN}),
        f"repos/{fx.PUBLIC_REPO}": json.dumps(
            {**fx.repo(fx.PUBLIC_REPO, private=False), "temp_clone_token": FINE_TOKEN}
        ),
        f"repos/{fx.PUBLIC_REPO}/pulls/{fx.PR_NUMBER}": json.dumps(
            {**fx.pr_detail(), "head": {"repo": {"clone_url": f"https://{TOKEN}@example.com"}}}
        ),
        f"repos/{fx.PUBLIC_REPO}/pulls/{fx.PR_NUMBER}/reviews": fx.pages(fx.default_reviews()),
        f"repos/{fx.PUBLIC_REPO}/pulls/{fx.PR_NUMBER}/comments": fx.pages(
            [fx.comment(fx.OWNER, fx.OWNER_INLINE, "2026-10-03T10:05:00Z", token=TOKEN)]
        ),
        # issues/1/comments and the README are missing: those calls fail with the
        # token on stderr and on stdout.
    }
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    monkeypatch.setenv("RM31_FAKE_GH", str(tmp_path / "responses.json"))
    runner = functools.partial(github.gh_runner, which=lambda _n: str(script))
    monkeypatch.setattr("brain.cli_ingest_github.github_gh_runner", runner)

    failing = _run("--repo", fx.PUBLIC_REPO, *SINCE)
    responses[f"repos/{fx.PUBLIC_REPO}/issues/{fx.PR_NUMBER}/comments"] = fx.pages([])
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    passing = _run("--repo", fx.PUBLIC_REPO, *SINCE)

    assert "`gh api` failed (exit 1)" in failing.output
    assert "1 ingested" in passing.output, passing.output
    stored = json.dumps(
        [list(r) for r in test_db.execute(
            "SELECT d.title, d.content, d.metadata::text, s.metadata::text, d.summary "
            "FROM documents d JOIN sources s ON s.id = d.source_id"
        ).fetchall()],
        default=str,
    )
    chunks = json.dumps([r[0] for r in test_db.execute("SELECT content FROM chunks").fetchall()])
    for secret in (TOKEN, FINE_TOKEN):
        for where in (failing.output, passing.output, stored, chunks):
            assert secret not in where
