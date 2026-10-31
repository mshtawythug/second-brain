"""`brain ingest-web` end to end (RM-41), through the CLI and the 5434 database.

Every browser store is a synthetic profile built in ``tmp_path``
(``tests/browser_fixtures.py``) and passed with ``--profile``; the owner's real
profiles are unreachable (``tests/browser_isolation_plugin.py``). ``--fetch`` runs
against ``httpx.MockTransport`` with a fake resolver; no test touches the network.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain import cli_ingest_web
from brain.cli import app
from brain.ingest import web_fetch
from brain.ingest.browser import page_external_id
from tests import browser_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

ALLOWLIST = "# synthetic\nexample.com\n*.example.org\n"
DOC = "https://example.com/doc"
SINCE = ["--since", "2026-10-01"]
NOW = fx.NOW
VISITS = (NOW - 3 * fx.DAY, NOW - 2 * fx.DAY, NOW - fx.DAY, NOW)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "brain-home"
    home.mkdir()
    (home / "web-allowlist.txt").write_text(ALLOWLIST, encoding="utf-8")
    monkeypatch.setenv("BRAIN_HOME", str(home))
    return home


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every store read, still performing it."""
    calls: list[str] = []
    real = cli_ingest_web.read_browser

    def spy(browser: Any, profile: Path, since: datetime) -> Any:
        calls.append(browser)
        return real(browser, profile, since)

    monkeypatch.setattr(cli_ingest_web, "read_browser", spy)
    return calls


def _chrome(tmp_path: Path, pages: list[fx.Page], **kwargs: Any) -> Path:
    return fx.chrome_profile(tmp_path / "chrome", pages, **kwargs)


def _run(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-web", *args])


def _web(profile: Path, *args: str, browser: str = "chrome") -> Result:
    return _run("--browser", browser, *SINCE, "--profile", str(profile), *args)


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    """The stored ``web`` documents keyed by their URL (``metadata.url``)."""
    rows = conn.execute(
        """
        SELECT d.metadata->>'url', d.id::text, d.title, d.content, d.content_type,
               d.sensitivity, d.sent_at, d.metadata, d.tags, d.summary
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'web'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "sensitivity", "sent_at", "metadata",
            "tags", "summary")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_ids(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM chunks WHERE document_id = %s ORDER BY id", (document_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


# --- scope, allowlist and host (AC1) ----------------------------------------------------------


@pytest.mark.parametrize(
    "args", [[], ["--browser", "chrome"], SINCE, ["--profile", "/nonexistent", "--max", "3"]]
)
def test_without_browser_and_since_the_run_is_refused(
    args: list[str], home: Path, reads: list[str]
) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert cli_ingest_web.SCOPE_REFUSAL in result.output
    assert reads == []


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--browser", "firefox", *SINCE], "--browser must be chrome or safari"),
        (["--browser", "chrome", "--since", "2026/10/01"], "--since must be YYYY-MM-DD"),
    ],
)
def test_a_malformed_scope_is_refused(
    args: list[str], reason: str, home: Path, reads: list[str]
) -> None:
    result = _run(*args)

    assert result.exit_code == 2
    assert f"ingest-web: {reason}" in result.output
    assert reads == []


def test_a_future_since_is_refused(home: Path, reads: list[str]) -> None:
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()

    result = _run("--browser", "chrome", "--since", tomorrow)

    assert result.exit_code == 2
    assert "ingest-web: --since is in the future" in result.output
    assert reads == []


def test_without_an_allowlist_the_run_is_refused(
    tmp_path: Path, home: Path, reads: list[str]
) -> None:
    (home / "web-allowlist.txt").unlink()
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])

    result = _web(profile)

    assert result.exit_code == 2
    assert cli_ingest_web.NO_ALLOWLIST in result.output
    assert reads == []


def test_an_allowlist_without_hosts_is_refused(
    tmp_path: Path, home: Path, reads: list[str]
) -> None:
    (home / "web-allowlist.txt").write_text("# nothing yet\n", encoding="utf-8")

    result = _web(_chrome(tmp_path, []))

    assert result.exit_code == 2
    assert cli_ingest_web.EMPTY_ALLOWLIST in result.output
    assert reads == []


def test_a_malformed_allowlist_is_refused_by_line_number(
    tmp_path: Path, home: Path, reads: list[str]
) -> None:
    (home / "web-allowlist.txt").write_text("example.com\nhttps://secret.example.net\n",
                                            encoding="utf-8")

    result = _web(_chrome(tmp_path, []))

    assert result.exit_code == 2
    assert "web-allowlist.txt: line 2" in result.output
    assert "secret.example.net" not in result.output
    assert reads == []


def test_off_macos_the_default_profile_is_not_guessed(
    home: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_ingest_web, "host_platform", "linux")

    result = _run("--browser", "chrome", *SINCE)

    assert result.exit_code == 2
    assert cli_ingest_web.NEEDS_PROFILE in result.output
    assert reads == []


def test_on_macos_the_default_profile_is_read_under_home(
    home: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_ingest_web, "host_platform", "darwin")

    result = _run("--browser", "safari", *SINCE)

    # The isolation plugin's home has no profile, so the read fails cleanly.
    assert result.exit_code == 1
    assert "no browser history found" in result.output
    assert reads == ["safari"]


def test_a_bad_sensitivity_is_refused(tmp_path: Path, home: Path, reads: list[str]) -> None:
    result = _web(_chrome(tmp_path, []), "--sensitivity", "secret")

    assert result.exit_code != 0
    assert reads == []


# --- AC2, AC3: what lands ---------------------------------------------------------------------


def test_an_allowlisted_often_visited_page_lands_with_its_query_stripped(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [
        fx.Page(f"{DOC}?session=synthetic-token-123#frag", "Doc page", VISITS),
    ])

    result = _web(profile, "--tag", "Reading")

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    assert list(docs) == [DOC]
    doc = docs[DOC]
    assert doc["title"] == "Doc page"
    assert doc["content"] == f"Doc page\n\n{DOC}\n\nhost: example.com"
    assert "synthetic-token" not in str(doc)
    assert doc["content_type"] == "web_page"
    assert doc["sensitivity"] == "normal"
    assert doc["tags"] == ["reading"]
    assert doc["sent_at"] == VISITS[0]
    assert doc["metadata"]["chrome_visits"] == 4
    assert doc["metadata"]["chrome_last_visit"] == NOW.isoformat()
    assert doc["metadata"]["chrome_bookmarked"] is False
    assert doc["summary"] is None
    assert "1 ingested" in result.output


def test_pages_off_the_allowlist_never_become_documents(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    hot = tuple(NOW - n * timedelta(minutes=5) for n in range(100))
    profile = _chrome(
        tmp_path,
        [fx.Page("https://example.net/hot", "Hot", hot),
         fx.Page("https://example.org/apex", "Apex", VISITS)],
        bookmarks=[("https://www.example.com/sub", "Off-list bookmark"),
                   ("https://docs.example.org/guide", "Guide")],
    )

    result = _web(profile)

    assert result.exit_code == 0, result.output
    assert list(_docs(test_db)) == ["https://docs.example.org/guide"]
    assert "3 entr" in result.output and "off the allowlist" in result.output


def test_the_source_key_is_the_url_hash_and_the_mirror_is_one_flat_file(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a URL external id put ``https:/`` into the mirror filename, a directory."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])

    assert _web(profile).exit_code == 0

    external, vault_path = test_db.execute(
        "SELECT s.external_id, d.vault_path FROM documents d JOIN sources s "
        "ON s.id = d.source_id WHERE s.kind = 'web'"
    ).fetchone()
    assert external == page_external_id(DOC)
    assert vault_path.count("/") == 2 and vault_path.startswith("_ingested/web/")
    mirrored = [p.relative_to(vault) for p in vault.rglob("*.md")]
    assert [str(p.parent) for p in mirrored] == ["_ingested/web"]


def test_a_page_only_in_the_wal_is_ingested(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [], wal_pages=[fx.Page(DOC, "Fresh", VISITS)])

    assert _web(profile).exit_code == 0
    assert list(_docs(test_db)) == [DOC]


def test_confidential_on_request(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])

    assert _web(profile, "--sensitivity", "confidential").exit_code == 0
    assert _docs(test_db)[DOC]["sensitivity"] == "confidential"


# --- AC4: a re-run is metadata only -----------------------------------------------------------


def test_a_rerun_with_new_visits_changes_metadata_only(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS[:3])])
    assert _web(profile).exit_code == 0
    before = _docs(test_db)[DOC]
    chunks = _chunk_ids(test_db, before["id"])
    embeds = embedder.embed_calls
    newer = tmp_path / "newer"
    fx.chrome_profile(newer, [fx.Page(DOC, "Doc (retitled)", VISITS)])

    result = _web(newer)

    assert result.exit_code == 0, result.output
    after = _docs(test_db)[DOC]
    assert embedder.embed_calls == embeds
    assert _chunk_ids(test_db, after["id"]) == chunks
    assert after["id"] == before["id"]
    assert after["content"] == before["content"]  # the title is sticky (D3)
    assert after["metadata"]["chrome_visits"] == 4
    assert "1 refreshed" in result.output

    again = _web(newer)

    assert "1 unchanged" in again.output
    assert embedder.embed_calls == embeds


def test_chrome_and_safari_describe_one_page_without_overwriting_each_other(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    chrome = _chrome(tmp_path, [fx.Page(DOC, "From Chrome", VISITS)])
    safari = fx.safari_profile(tmp_path / "safari", [fx.Page(DOC, "From Safari", VISITS[:3])])

    assert _web(chrome).exit_code == 0
    assert _web(safari, browser="safari").exit_code == 0

    (doc,) = _docs(test_db).values()
    assert doc["metadata"]["chrome_visits"] == 4
    assert doc["metadata"]["safari_visits"] == 3
    assert doc["title"] == "From Chrome"


def test_refresh_rebuilds_the_body_and_keeps_the_other_browser_and_the_first_date(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    safari = fx.safari_profile(tmp_path / "safari", [fx.Page(DOC, "Old", VISITS)])
    assert _web(safari, browser="safari").exit_code == 0
    later = (NOW - fx.DAY, NOW)
    chrome = _chrome(tmp_path, [fx.Page(DOC, "New title", later)], bookmarks=[(DOC, "Saved")])

    result = _web(chrome, "--refresh", "--min-visits", "1")

    assert result.exit_code == 0, result.output
    (doc,) = _docs(test_db).values()
    assert doc["title"] == "Saved"
    assert doc["metadata"]["safari_visits"] == 4
    assert doc["metadata"]["chrome_bookmarked"] is True
    assert doc["sent_at"] == VISITS[0]
    assert "1 updated" in result.output


# --- AC5: --fetch -----------------------------------------------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch, routes: dict[str, httpx.Response]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(f"{request.headers['host']}{request.url.raw_path.decode()}",
                          httpx.Response(404))

    monkeypatch.setattr(
        cli_ingest_web, "make_fetch_client",
        lambda: web_fetch.make_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(cli_ingest_web, "fetch_resolver",
                        lambda host, port: ["93.184.215.14"])


def test_fetch_puts_the_page_text_in_the_body_and_enriches(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {"example.com/doc": httpx.Response(
        200, headers={"content-type": "text/html"}, text="<p>The fetched words.</p>")})
    built: list[int] = []
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: built.append(1))
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])

    result = _web(profile, "--fetch")

    assert result.exit_code == 0, result.output
    assert _docs(test_db)[DOC]["content"].endswith("host: example.com\n\nThe fetched words.")
    assert built == [1]


def test_without_fetch_no_enricher_is_built(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[int] = []
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: built.append(1))

    assert _web(_chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])).exit_code == 0
    assert built == []


def test_fetch_refuses_a_redirect_off_the_allowlist_and_keeps_the_page(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {"example.com/doc": httpx.Response(
        302, headers={"location": "https://example.net/elsewhere"})})

    result = _web(_chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)]), "--fetch", "--no-enrich")

    assert result.exit_code == 0, result.output
    assert "fetch skipped" in result.output
    assert "refused: host not on the allowlist" in result.output
    assert _docs(test_db)[DOC]["content"] == f"Doc\n\n{DOC}\n\nhost: example.com"
    assert "1 fetch failed" in result.output


def test_fetch_never_runs_for_a_page_that_exists_unless_refreshed(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS)])
    assert _web(profile).exit_code == 0
    requested: list[int] = []

    def counting(host: str, port: int) -> list[str]:
        requested.append(1)
        return ["93.184.215.14"]

    monkeypatch.setattr(cli_ingest_web, "fetch_resolver", counting)

    assert _web(profile, "--fetch", "--no-enrich").exit_code == 0
    assert requested == []


# --- AC6 and other failures -------------------------------------------------------------------


def test_safari_without_full_disk_access_exits_1_in_one_line(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = fx.safari_profile(tmp_path / "safari", [fx.Page(DOC, "Doc", VISITS)])

    def denied(path: Path) -> int:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr("brain.ingest.browser_store._stat", denied)

    result = _web(profile, browser="safari")

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Full Disk Access" in result.output
    assert "docs/guides/browser-history.md" in result.output
    assert str(tmp_path) not in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_an_unsupported_schema_exits_1(tmp_path: Path, home: Path) -> None:
    profile = tmp_path / "chrome"
    profile.mkdir()
    import sqlite3

    with sqlite3.connect(profile / "History") as conn:
        conn.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT)")
    conn.close()

    result = _web(profile)

    assert result.exit_code == 1
    assert "unsupported chrome history schema" in result.output


def test_a_secret_in_a_title_refuses_that_page_only(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    profile = _chrome(tmp_path, [
        fx.Page(DOC, f"key {SYNTHETIC_AWS_KEY}", VISITS),
        fx.Page("https://example.com/clean", "Clean", VISITS),
    ])

    result = _web(profile)

    assert result.exit_code == 0, result.output
    assert list(_docs(test_db)) == ["https://example.com/clean"]
    assert "1 failed" in result.output
    # The shared guard notice masks the finding (it quotes the title, as for every source).
    assert "AKIA****" in result.output

    allowed = _web(profile, "--allow-secrets")

    assert allowed.exit_code == 0
    assert DOC in _docs(test_db)


def test_under_the_default_guard_a_secret_is_ingested_with_a_notice(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [fx.Page(DOC, f"key {SYNTHETIC_AWS_KEY}", VISITS)])

    result = _web(profile)

    assert result.exit_code == 0, result.output
    assert DOC in _docs(test_db)
    assert "BRAIN_SECRET_GUARD=warn" in result.output


def test_a_failing_page_is_counted_by_class_and_the_rest_continue(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = cli_ingest_web.ingest_document

    def flaky(conn: Any, **kwargs: Any) -> Any:
        if kwargs["source_external_id"] == page_external_id(DOC):
            raise ValueError("synthetic detail that must not be printed")
        return real(conn, **kwargs)

    monkeypatch.setattr(cli_ingest_web, "ingest_document", flaky)
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc", VISITS),
                                 fx.Page("https://example.com/ok", "Ok", VISITS)])

    result = _web(profile)

    assert result.exit_code == 0
    assert "failed" in result.output and "ValueError" in result.output
    assert "synthetic detail" not in result.output
    assert list(_docs(test_db)) == ["https://example.com/ok"]


# --- reporting --------------------------------------------------------------------------------


def test_dry_run_lists_host_and_title_and_writes_nothing(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    profile = _chrome(tmp_path, [fx.Page(DOC, "Doc \x1b[2J title", VISITS)])

    result = _web(profile, "--dry-run")

    assert result.exit_code == 0
    assert "example.com  Doc [2J title" in result.output
    assert "\x1b" not in result.output
    assert _docs(test_db) == {}
    assert embedder.embed_calls == 0


def test_the_cap_is_reported(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    pages = [fx.Page(f"https://example.com/{n}", str(n), VISITS) for n in range(3)]

    result = _web(_chrome(tmp_path, pages), "--max", "1")

    assert result.exit_code == 0
    assert len(_docs(test_db)) == 1
    assert "stopped at the --max cap of 1: 2 more page(s) qualify" in result.output


def test_the_read_report_names_every_skip_reason(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brain.ingest.browser_store.MAX_ROWS", 3)
    profile = _chrome(tmp_path, [
        fx.Page("https://example.com/rare", "Rare", VISITS[:1]),
        fx.Page("javascript:void(0)", "Js", VISITS),
        fx.Page("https://example.net/off", "Off", VISITS),
        fx.Page(DOC, "Doc", VISITS),
    ])
    import sqlite3

    with sqlite3.connect(profile / "History") as conn:
        conn.execute("UPDATE urls SET title = x'00' WHERE url = 'https://example.net/off'")
    conn.close()

    result = _web(profile)

    assert result.exit_code == 0, result.output
    assert "below --min-visits" in result.output
    assert "not http(s)" in result.output
    assert "could not be read" in result.output
    assert "stopped reading at" in result.output


def test_no_url_or_title_reaches_the_logs(
    tmp_path: Path, home: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = _chrome(tmp_path, [fx.Page(f"{DOC}?q=synthetic-term", "Unique-Title-Marker",
                                         VISITS)])

    with caplog.at_level(logging.DEBUG):
        assert _web(profile).exit_code == 0

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "example.com/doc" not in logged
    assert "Unique-Title-Marker" not in logged
    assert "synthetic-term" not in logged
