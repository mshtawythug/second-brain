"""The paste ingest job against a real Postgres (Ingest spec §9.2, paste rows).

Every job is driven through ``JobRegistry.run_next()`` on the test thread: no
second thread, no sleeps. Cancels are raised from INSIDE the fake embedder's own
``embed()`` (or ``count_tokens``) calls, which is the only place a real cancel can
land mid-job. All content is synthetic.
"""
from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.enrichment import SummaryResult
from brain.errors import EmbedError, SecretGuardError
from brain.ui.context import UiContext
from brain.ui.ingest_schemas import PasteSpec
from brain.ui.ingest_service import ingest_failure, submit_paste
from brain.ui.jobs import JobRegistry, JobSnapshot
from brain.ui.watcher_probe import WatcherProbe
from brain.vault.sync import sync_one_file
from tests.conftest import FakeEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY
from tests.ui_ingest_support import body_with_chunks, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_BODY = "Quarterly vendor review.\n\nThree open threads remain on the shortlist.\n"


def _spec(**overrides: Any) -> PasteSpec:
    fields: dict[str, Any] = {
        "title": "Vendor Review Notes",
        "body": _BODY,
        "tags": ("vendors",),
        "content_type": "note",
        "sensitivity": "normal",
        "enrich": False,
    }
    fields.update(overrides)
    return PasteSpec(**fields)


def _run(ctx: UiContext, spec: PasteSpec) -> JobSnapshot:
    assert ctx.jobs is not None and ctx.ingest_runtime is not None
    job = submit_paste(ctx, ctx.jobs, ctx.ingest_runtime, spec)
    assert ctx.jobs.run_next()
    return ctx.jobs.get(job.id)


def _counts(conn: psycopg.Connection[Any]) -> tuple[int, int, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks),"
        " (SELECT count(*) FROM sources)"
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _mirror_files(vault: Path) -> list[Path]:
    """The UI's mirror files: everything under ``_ingested/manual/``."""
    manual = vault / "_ingested" / "manual"
    return sorted(manual.rglob("*.md")) if manual.exists() else []


class _CancellingEmbedder(FakeEmbedder):
    """Cancels its own job from inside its ``cancel_on``-th ``embed()`` call."""

    def __init__(self, registry: JobRegistry, *, cancel_on: int) -> None:
        super().__init__()
        self.registry = registry
        self.cancel_on = cancel_on
        self.calls = 0
        self.job_id = ""

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls += 1
        if self.calls == self.cancel_on:
            self.registry.cancel(self.job_id)
        return super().embed(texts, input_type)


class _CountingEmbedder(FakeEmbedder):
    def __init__(self, **flags: bool) -> None:
        super().__init__()
        self.calls = 0
        for name, value in flags.items():
            setattr(self, name, value)

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls += 1
        return super().embed(texts, input_type)


class _Enricher:
    model = "fake-summary-model"

    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.raise_exc = raise_exc

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        if self.raise_exc is not None:
            raise self.raise_exc
        return SummaryResult(summary="A canned two-sentence summary.", model=self.model)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return make_vault(tmp_path)


# ------------------------------------------------------------- happy paths --


def test_a_paste_becomes_an_ingested_document_with_a_mirror(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())

    snap = _run(ctx, _spec(tags=("Vendors", "Q3 Planning")))

    assert snap.state == "succeeded", snap.error
    result = dict(snap.result or {})
    assert result["outcome"] == "ingested"
    assert result["mirror_repaired"] is False
    assert result["possible_duplicates"] == []
    assert result["chunk_count"] >= 1
    assert result["vault_path"].startswith("_ingested/manual/")
    assert (vault / result["vault_path"]).is_file()
    row = test_db.execute(
        "SELECT kind, source_id, source_path, metadata, tags, sensitivity, content_type"
        " FROM documents WHERE id = %s",
        (result["document_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] == "ingested"
    assert row[1] is None and row[2] is None  # no sources row, content-hash dedup
    assert row[3] == {"ingested_via": "ui"}
    assert row[4] == ["vendors", "q3-planning"]
    assert row[5] == "normal"
    assert row[6] == "note"
    assert _counts(test_db)[2] == 0


def test_the_same_paste_again_is_already_in_your_brain(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    first = dict(_run(ctx, _spec()).result or {})
    before = _counts(test_db)

    second = _run(ctx, _spec())

    assert dict(second.result or {})["outcome"] == "skipped"
    assert dict(second.result or {})["document_id"] == first["document_id"]
    assert _counts(test_db) == before


def test_progress_reaches_the_event_log(test_db: psycopg.Connection[Any], vault: Path) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())

    snap = _run(ctx, _spec())

    phases = [e.phase for e in snap.events]
    assert phases[0] == "queued"
    assert "preparing" in phases and "embedding" in phases
    assert phases[-1] == "finishing"


def test_the_info_log_names_ids_never_the_title(
    test_db: psycopg.Connection[Any], vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    with caplog.at_level(logging.INFO, logger="brain.ui.ingest_service"):
        snap = _run(ctx, _spec(title="Distinctive Title Marker"))

    assert snap.id in caplog.text
    assert "Distinctive Title Marker" not in caplog.text
    assert "Three open threads" not in caplog.text


# ---------------------------------------------------------- mid-embed cancel --


@pytest.mark.parametrize(
    ("chunks", "cancel_on", "case"),
    [
        (20, 1, "two batches, cancel in the first: the next check_cancelled raises"),
        (20, 2, "two batches, cancel in the last: enter_commit's check raises"),
        (3, 1, "one batch, cancel in the only call: enter_commit's check raises"),
    ],
)
def test_a_mid_embed_cancel_writes_nothing(
    test_db: psycopg.Connection[Any], vault: Path, chunks: int, cancel_on: int, case: str
) -> None:
    registry = make_registry()
    embedder = _CancellingEmbedder(registry, cancel_on=cancel_on)
    body = body_with_chunks(chunks, embedder.count_tokens)
    ctx = make_context(test_db, vault, embedder=embedder, jobs=registry)

    job = submit_paste(ctx, registry, ctx.ingest_runtime, _spec(body=body))  # type: ignore[arg-type]
    embedder.job_id = job.id
    registry.run_next()

    snap = registry.get(job.id)
    assert snap.state == "cancelled", case
    assert embedder.calls == cancel_on, case
    assert _counts(test_db) == (0, 0, 0), case
    assert _mirror_files(vault) == [], case


def test_a_cancel_on_a_no_embed_path_is_reported_too_late(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    """``produces_embeddings=False``: no cancel point is left after the chunker."""
    registry = make_registry()
    job_id: list[str] = []

    class _NoVectors(FakeEmbedder):
        produces_embeddings = False

        def count_tokens(self, text: str) -> int:
            if job_id:
                registry.cancel(job_id.pop())
            return super().count_tokens(text)

    ctx = make_context(test_db, vault, embedder=_NoVectors(), jobs=registry)
    job = submit_paste(ctx, registry, ctx.ingest_runtime, _spec())  # type: ignore[arg-type]
    job_id.append(job.id)
    registry.run_next()

    snap = registry.get(job.id)
    assert snap.state == "succeeded"
    assert [e.code for e in snap.events].count("cancel_too_late") == 1
    assert _counts(test_db)[0] == 1


# --------------------------------------------------------- repair & failure --


def test_a_skip_over_a_missing_mirror_reports_mirror_repaired(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    first = dict(_run(ctx, _spec()).result or {})
    test_db.execute(
        "UPDATE documents SET vault_path = NULL WHERE id = %s", (first["document_id"],)
    )
    (vault / first["vault_path"]).unlink()

    again = dict(_run(ctx, _spec()).result or {})

    assert again["outcome"] == "mirror_repaired"
    assert again["mirror_repaired"] is True
    assert (vault / again["vault_path"]).is_file()


def test_a_crash_in_the_summary_leaves_a_committed_row_and_a_safe_resubmit(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    """RuntimeError is NOT caught by the summary hook, so it escapes before the mirror."""
    long_body = "Long synthetic paragraph about vendor scoring. " * 40
    crashing = make_context(
        test_db, vault, embedder=FakeEmbedder(), enricher=_Enricher(raise_exc=RuntimeError("x"))
    )

    failed = _run(crashing, _spec(body=long_body, enrich=True))

    assert failed.state == "failed"
    assert failed.error is not None and failed.error.code == "internal_error"
    assert "Nothing was written" not in failed.error.message
    assert "may already have been saved" in failed.error.message
    row = test_db.execute("SELECT summary, vault_path FROM documents").fetchone()
    assert row == (None, None)

    healthy = make_context(test_db, vault, embedder=FakeEmbedder(), enricher=_Enricher())
    again = dict(_run(healthy, _spec(body=long_body, enrich=True)).result or {})

    assert again["outcome"] == "mirror_repaired"
    assert again["summary_status"] == "pending"
    assert test_db.execute("SELECT summary FROM documents").fetchone() == (None,)


def test_a_failed_mirror_still_succeeds_and_a_resubmit_repairs_it(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    ingested = vault / "_ingested" / "manual"
    ingested.mkdir(parents=True, exist_ok=True)
    ingested.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        first = _run(ctx, _spec())
    finally:
        ingested.chmod(stat.S_IRWXU)

    assert first.state == "succeeded"
    assert dict(first.result or {})["vault_path"] is None
    again = dict(_run(ctx, _spec()).result or {})
    assert again["outcome"] == "mirror_repaired"


def test_an_empty_document_ends_empty_without_an_embed(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    """Reachable only past validation (upload, RM-13); the runner still handles it."""
    embedder = _CountingEmbedder()
    ctx = make_context(test_db, vault, embedder=embedder)

    result = dict(_run(ctx, _spec(body="   \n  ")).result or {})

    assert result["outcome"] == "empty"
    assert result["document_id"] is None
    assert result["chunk_count"] == 0
    assert result["summary_status"] == "skipped"
    assert embedder.calls == 0


# ------------------------------------------------------- C5: the flag forwards --


def test_a_confidential_paste_under_a_hosted_embedder_is_never_embedded(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    embedder = _CountingEmbedder(hosted_egress=True)
    ctx = make_context(test_db, vault, embedder=embedder)

    result = dict(_run(ctx, _spec(sensitivity="confidential")).result or {})

    assert embedder.calls == 0
    assert result["egress_notice"]
    nulls = test_db.execute(
        "SELECT count(*) FILTER (WHERE embedding IS NULL), count(*) FROM chunks"
    ).fetchone()
    assert nulls is not None and nulls[0] == nulls[1] > 0


def test_a_backend_without_vectors_stores_null_embeddings(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    embedder = _CountingEmbedder(produces_embeddings=False)
    ctx = make_context(test_db, vault, embedder=embedder)

    snap = _run(ctx, _spec())

    assert snap.state == "succeeded"
    assert embedder.calls == 0
    nulls = test_db.execute(
        "SELECT count(*) FILTER (WHERE embedding IS NULL), count(*) FROM chunks"
    ).fetchone()
    assert nulls is not None and nulls[0] == nulls[1] > 0


# ---------------------------------------------------------- the secret guard --


def test_a_warned_secret_reports_masked_findings(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(
        test_db, vault, embedder=FakeEmbedder(), cfg_overrides={"secret_guard": "warn"}
    )

    snap = _run(ctx, _spec(body=f"Deploy notes.\n\nkey {SYNTHETIC_AWS_KEY}\n"))

    result = dict(snap.result or {})
    assert result["secret_guard"] == "warn"
    assert result["secret_findings"]
    assert all(SYNTHETIC_AWS_KEY not in f["preview"] for f in result["secret_findings"])
    assert SYNTHETIC_AWS_KEY not in repr(snap)
    assert "secret_findings" in [e.code for e in snap.events]


def test_a_rejected_secret_fails_and_writes_nothing(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    ctx = make_context(
        test_db, vault, embedder=FakeEmbedder(), cfg_overrides={"secret_guard": "reject"}
    )

    snap = _run(ctx, _spec(body=f"Deploy notes.\n\nkey {SYNTHETIC_AWS_KEY}\n"))

    assert snap.state == "failed"
    assert snap.error is not None and snap.error.code == "secret_guard_rejected"
    assert SYNTHETIC_AWS_KEY not in snap.error.message
    assert _counts(test_db) == (0, 0, 0)


# ----------------------------------------------------- summary & graph status --


def test_a_written_summary_is_reported(test_db: psycopg.Connection[Any], vault: Path) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder(), enricher=_Enricher())
    long_body = "Long synthetic paragraph about vendor scoring. " * 40

    result = dict(_run(ctx, _spec(body=long_body, enrich=True)).result or {})

    assert result["summary_status"] == "written"


def test_a_short_paste_skips_the_summary(test_db: psycopg.Connection[Any], vault: Path) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder(), enricher=_Enricher())

    result = dict(_run(ctx, _spec(enrich=True)).result or {})

    assert result["summary_status"] == "skipped"


def test_enrich_off_skips_the_summary(test_db: psycopg.Connection[Any], vault: Path) -> None:
    ctx = make_context(test_db, vault, embedder=FakeEmbedder(), enricher=_Enricher())

    result = dict(_run(ctx, _spec(body="Long paragraph. " * 60, enrich=False)).result or {})

    assert result["summary_status"] == "skipped"


@pytest.mark.parametrize(
    ("graph_enabled", "watcher", "expected"),
    [
        (False, "running", "disabled"),
        (True, "running", "queued"),
        (True, "not_running", "not_synced"),
        (True, "unavailable", "not_synced"),
    ],
)
def test_the_graph_state_says_who_will_sync(
    test_db: psycopg.Connection[Any],
    vault: Path,
    graph_enabled: bool,
    watcher: Any,
    expected: str,
) -> None:
    ctx = make_context(
        test_db,
        vault,
        embedder=FakeEmbedder(),
        cfg_overrides={"graph_enabled": graph_enabled},
        watcher_probe=WatcherProbe(detect=lambda: watcher),
    )

    assert dict(_run(ctx, _spec()).result or {})["graph"] == expected


def test_the_watcher_sync_of_the_new_mirror_reaches_reconcile(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    """§5.6, verified rather than inherited: the watcher's lane reconciles the mirror."""
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    result = dict(_run(ctx, _spec()).result or {})

    class _Recording:
        def __init__(self) -> None:
            self.reconciled: list[str] = []

        def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None:
            self.reconciled.append(document_id)

        def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
            raise AssertionError("nothing was removed")

    recorder = _Recording()
    sync_one_file(
        test_db,
        embedder=FakeEmbedder(),
        vault_path=vault,
        file_path=vault / result["vault_path"],
        graph_syncer=recorder,
    )

    assert recorder.reconciled == [result["document_id"]]


# ------------------------------------------------------------ failure mapping --


@pytest.mark.parametrize(
    ("exc", "code", "claims_nothing_written"),
    [
        (SecretGuardError("x"), "secret_guard_rejected", True),
        (EmbedError("x", transient=True), "embedder_busy", True),
        (EmbedError("x", transient=False), "embedder_unavailable", True),
        (psycopg.errors.UniqueViolation("x"), "concurrent_ingest", False),
        (psycopg.OperationalError("x"), "database_error", False),
        (RuntimeError("document text"), "internal_error", False),
    ],
)
def test_every_failure_maps_to_a_fixed_message(
    exc: Exception, code: str, claims_nothing_written: bool
) -> None:
    error = ingest_failure(exc)

    assert error.code == code
    assert "document text" not in error.message
    assert ("Nothing was written" in error.message) is claims_nothing_written



def test_a_document_gone_before_the_read_back_is_reported_from_the_spec(
    test_db: psycopg.Connection[Any],
) -> None:
    """A row deleted between the commit and the read-back must not crash the job."""
    from brain.ui.ingest_service import _read_back

    assert _read_back(test_db, "00000000-0000-4000-8000-000000000000") is None
