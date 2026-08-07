"""The hosted-embedder egress boundary for confidential documents (F6).

Facts for the pre-write gate:
1. Called by: pytest collection only (leaf test module, nothing imports it).
   Consumes ``test_db`` (tests/conftest.py:432) and ``FakeEmbedder``
   (tests/conftest.py:478).
2. No existing module covers this: ``ls tests/ | grep -i 'sensitiv|egress'``
   returns only ``test_migration_026_sensitivity.py`` (the schema half). The
   nearest neighbour, ``test_ingest_guard_pipeline.py``, covers F4's content
   guard — a different boundary.
3. Writes synthetic ``documents`` / ``chunks`` rows to the port-5434 test DB:
   ``title='Synthetic ...'``, ``content`` = invented prose, ``sensitivity`` in
   {'normal','confidential'}. No PII, no dates, no production data.
4. Instruction: "RED: tests/test_sensitivity_egress.py::
   test_confidential_doc_is_not_sent_to_hosted_embedder — a FakeVoyageEmbedder
   recording double (a fake conforming to the Embedder Protocol that
   is_hosted_embedder recognizes, not a monkeypatch of VoyageEmbedder) ingests a
   doc with sensitivity='confidential'; assert fake.embed_calls == [] and every
   chunk's embedding is NULL. Fails today on both counts."

---

This is the one F6 boundary that stops bytes from leaving the machine. The other
two (MCP ``brain_show``, the published wiki) keep bytes inside a process or off
an index; this one is about a live HTTPS POST to a third party:
``VoyageEmbedder.embed`` sends raw chunk text to Voyage AI, and before 026 the
ingest pipeline had no per-document veto over it.

**Why NULL embeddings rather than a refusal or a local fallback.** Refusing the
ingest loses the note entirely, forcing a choice between the brain and privacy.
Falling back to a local embedder is the option that sounds best and is the
worst: it writes a 1024-dim Arctic vector into a column whose other rows hold
Voyage vectors, and cosine distance between two different models' embedding
spaces is meaningless — every confidential document would sit at a garbage rank
forever, silently. NULL is the only choice that is correct at every layer, and
the machinery already exists for the FTS-only ``none`` backend.

The double here is a FAKE conforming to the ``Embedder`` Protocol that
``is_hosted_embedder`` recognizes via the duck-typed ``hosted_egress`` flag —
NOT a monkeypatch of ``VoyageEmbedder`` (CLAUDE.md rule 13), and not a subclass
of it either, since constructing one requires an API key and a live client.

All documents are synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.embeddings import is_hosted_embedder
from brain.errors import SensitivityError
from brain.ingest import ExtractedDoc, ingest_document
from brain.sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from tests.conftest import FakeEmbedder

# A body long enough to produce chunks, with no credential-shaped substrings so
# the F4 secret guard stays silent and cannot confound the assertions here.
_BODY = (
    "Quarterly platform review notes.\n\n"
    "We discussed the migration sequencing for the billing service and agreed "
    "to stage the cutover behind a feature flag. The rollback plan is a single "
    "configuration revert.\n\n"
    "Follow-up owners were assigned for the load test and the runbook update.\n"
)


class RecordingHostedEmbedder(FakeEmbedder):
    """A hosted backend double that records every ``embed`` call.

    Declares the duck-typed ``hosted_egress`` flag, which is the whole seam
    :func:`brain.embeddings.is_hosted_embedder` reads. Mirrors how
    ``NullEmbedder.produces_embeddings`` is recognized — a flag, not an
    ``isinstance`` check, so a test double can stand in for a hosted backend
    without an API key and without patching production classes.

    The hosted-ness is the flag, not the dim.
    """

    hosted_egress: bool = True

    def __init__(self) -> None:
        # Inherit FakeEmbedder's default dim (4096) so the vectors match the
        # test schema's declared ``chunks.embedding vector(4096)``. Pinning 1024
        # here to mirror the real Voyage schema would make every non-vetoed
        # insert fail on a dimension mismatch — and would test psycopg's type
        # checking rather than the egress boundary.
        super().__init__()
        self.embed_calls: list[list[str]] = []

    def embed(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return super().embed(texts, input_type)


class RecordingLocalEmbedder(FakeEmbedder):
    """A local backend double — same recording, but NOT hosted.

    Proves the veto is about hosted *egress*, not about sensitivity as such: a
    confidential document under a local embedder must still get real vectors,
    or marking a note confidential would silently cripple its retrieval for no
    privacy gain.
    """

    def __init__(self) -> None:
        # Inherit FakeEmbedder's default dim (4096) so the vectors match the
        # test schema's declared ``chunks.embedding vector(4096)``. Pinning 1024
        # here to mirror the real Voyage schema would make every non-vetoed
        # insert fail on a dimension mismatch — and would test psycopg's type
        # checking rather than the egress boundary.
        super().__init__()
        self.embed_calls: list[list[str]] = []

    def embed(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return super().embed(texts, input_type)


def _doc(title: str) -> ExtractedDoc:
    """A synthetic stdin-shaped note (``source_path=None``, empty metadata).

    ``title`` is folded into the body so every document in this module hashes
    differently. Without that, the content-hash dedup ladder (``ingest_document``
    rule 4, which applies precisely when ``source_path is None``) would treat the
    second ingest of an identical body as a no-op ``skip`` and the assertions
    would silently describe the FIRST document.
    """
    return ExtractedDoc(
        title=title,
        content=f"{title}\n\n{_BODY}",
        content_type="note",
        source_path=None,
        metadata={},
    )


def _ingest(
    conn: psycopg.Connection[Any],
    *,
    embedder: Any,
    title: str,
    sensitivity: str | None = None,
) -> Any:
    """Ingest one synthetic stdin-shaped note.

    Centralizes the ``source_kind`` / ``source_external_id`` pair that the stdin
    path requires, so each test states only what it is actually about: the
    embedder, and the sensitivity level. Passing ``sensitivity=None`` omits the
    keyword entirely, which is how the "unmarked ingest is unchanged" test proves
    the parameter is genuinely defaulted rather than merely defaulting to the
    same value.
    """
    kwargs: dict[str, Any] = {}
    if sensitivity is not None:
        kwargs["sensitivity"] = sensitivity
    return ingest_document(
        conn,
        embedder=embedder,
        doc=_doc(title),
        source_kind="manual",
        source_external_id=title,
        **kwargs,
    )


def _chunk_embeddings(conn: psycopg.Connection[Any], document_id: str) -> list[Any]:
    rows = conn.execute(
        "SELECT embedding FROM chunks WHERE document_id = %s ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# is_hosted_embedder — the seam itself
# --------------------------------------------------------------------------


def test_hosted_flag_recognized_on_a_double() -> None:
    """The seam is duck-typed, so a fake can be hosted without an API key."""
    assert is_hosted_embedder(RecordingHostedEmbedder()) is True


def test_local_embedders_are_not_hosted() -> None:
    """A backend that declares nothing is local — the safe default for the veto.

    Defaulting to "not hosted" is correct because the veto's cost is a NULL
    embedding: mislabelling a local backend as hosted would silently degrade
    retrieval for every confidential document, while the reverse (a genuinely
    hosted backend forgetting the flag) is caught by the parity test below.
    """
    assert is_hosted_embedder(RecordingLocalEmbedder()) is False
    assert is_hosted_embedder(FakeEmbedder()) is False


def test_voyage_is_the_only_hosted_production_backend() -> None:
    """Parity guard: every production backend's hosted status is asserted here.

    Adding a hosted backend without declaring ``hosted_egress`` would silently
    reopen the egress hole. This test names each backend explicitly so a new one
    cannot be added without a deliberate decision recorded here.
    """
    from brain.embeddings import (
        ArcticEmbedder,
        NullEmbedder,
        Qwen3Embedder,
        VoyageEmbedder,
    )

    assert getattr(VoyageEmbedder, "hosted_egress", False) is True, (
        "VoyageEmbedder POSTs chunk text off-machine and MUST declare "
        "hosted_egress = True"
    )
    for local in (ArcticEmbedder, Qwen3Embedder, NullEmbedder):
        assert getattr(local, "hosted_egress", False) is False, (
            f"{local.__name__} runs locally and must not claim hosted egress"
        )


# --------------------------------------------------------------------------
# The veto, end to end through ingest_document
# --------------------------------------------------------------------------


def test_confidential_doc_is_not_sent_to_hosted_embedder(
    test_db: psycopg.Connection[Any],
) -> None:
    """RED-FIRST: a confidential body never reaches a hosted embedder.

    Two independent assertions, because either one alone is satisfiable by a
    wrong implementation: no ``embed`` call proves nothing left the machine, and
    all-NULL embeddings prove the pipeline stored the FTS-only shape rather than
    quietly substituting vectors from somewhere else.
    """
    embedder = RecordingHostedEmbedder()

    result = _ingest(
        test_db,
        embedder=embedder,
        title="Synthetic confidential runbook",
        sensitivity=CONFIDENTIAL,
    )

    assert result.document_id is not None
    assert embedder.embed_calls == [], (
        "a confidential document's chunk text must never be handed to a hosted "
        f"embedder; got {len(embedder.embed_calls)} embed call(s)"
    )
    embeddings = _chunk_embeddings(test_db, result.document_id)
    assert embeddings, "the document must still be stored and chunked"
    assert all(
        e is None for e in embeddings
    ), "chunks must carry SQL NULL embeddings, not substituted vectors"


def test_normal_doc_is_still_sent_to_hosted_embedder(
    test_db: psycopg.Connection[Any],
) -> None:
    """The veto is SCOPED: a normal document under the same backend embeds.

    Without this, a veto that simply disabled the hosted embedder outright would
    pass the test above while breaking the other ~1,376 documents.
    """
    embedder = RecordingHostedEmbedder()

    result = _ingest(
        test_db,
        embedder=embedder,
        title="Synthetic normal runbook",
        sensitivity=DEFAULT_SENSITIVITY,
    )

    assert result.document_id is not None
    assert embedder.embed_calls, "a normal document must still be embedded"
    embeddings = _chunk_embeddings(test_db, result.document_id)
    assert embeddings and all(e is not None for e in embeddings)


def test_default_sensitivity_is_normal_so_ingest_is_unchanged(
    test_db: psycopg.Connection[Any],
) -> None:
    """Omitting ``sensitivity`` entirely behaves exactly as it did pre-026.

    This is the backward-compatibility assertion for every existing call site:
    the parameter is defaulted, so no caller has to pass it and none changes
    behaviour.
    """
    embedder = RecordingHostedEmbedder()

    result = _ingest(
        test_db, embedder=embedder, title="Synthetic default runbook"
    )

    assert result.document_id is not None
    assert embedder.embed_calls, "an unmarked document must embed as before"
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (result.document_id,)
    ).fetchone()
    assert row is not None and row[0] == DEFAULT_SENSITIVITY


def test_confidential_doc_under_local_embedder_is_embedded(
    test_db: psycopg.Connection[Any],
) -> None:
    """A confidential document under a LOCAL backend gets real vectors.

    The boundary is about egress off the machine, not about sensitivity as
    such. Gating local embedding too would degrade retrieval on exactly the
    documents the user cares most about, buying no confidentiality — there is no
    network hop to protect.
    """
    embedder = RecordingLocalEmbedder()

    result = _ingest(
        test_db,
        embedder=embedder,
        title="Synthetic local confidential note",
        sensitivity=CONFIDENTIAL,
    )

    assert result.document_id is not None
    assert (
        embedder.embed_calls
    ), "a local embedder has no egress to veto — the body must be embedded"
    embeddings = _chunk_embeddings(test_db, result.document_id)
    assert embeddings and all(e is not None for e in embeddings)


def test_confidential_column_is_persisted(
    test_db: psycopg.Connection[Any],
) -> None:
    """``sensitivity`` reaches the column on the INSERT path."""
    result = _ingest(
        test_db,
        embedder=FakeEmbedder(),
        title="Synthetic persisted marking",
        sensitivity=CONFIDENTIAL,
    )

    assert result.document_id is not None
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (result.document_id,)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL


def test_reingest_without_the_flag_does_not_downgrade_a_confidential_doc(
    test_db: psycopg.Connection[Any],
) -> None:
    """REGRESSION: a plain re-ingest must not silently un-protect a document.

    The bug this guards against is the most dangerous one in F6, and it is
    invisible without this test. ``sensitivity`` defaults to ``normal``, so at the
    UPDATE-in-place layer an incoming ``normal`` is ambiguous: it means either
    "the user explicitly asked for normal" or — far more often — "no
    ``--sensitivity`` flag was passed at all". Nothing in the signature can tell
    them apart.

    If the re-ingest path treated that ambiguous ``normal`` as authoritative (the
    way ``draft`` legitimately does), then ANY later re-ingest — a repeated
    ``brain ingest``, or a ``brain vault sync --watch`` pass that re-reads the
    note — would quietly reset the column, and the next ingest under a hosted
    embedder would ship the body to Voyage. The user would have marked the note
    confidential, watched it succeed, and lost the protection to an unrelated
    background job, with no message anywhere.

    So the write escalates only. Clearing the tier is an explicit act and belongs
    to ``brain mark-normal``.
    """
    # Arrange — ingest once as confidential.
    first = _ingest(
        test_db,
        embedder=FakeEmbedder(),
        title="Synthetic re-ingest target",
        sensitivity=CONFIDENTIAL,
    )
    assert first.document_id is not None

    # Act — re-ingest the SAME source with changed content and NO sensitivity
    # flag, which is exactly what a watcher pass or a plain re-run looks like.
    again = ingest_document(
        test_db,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title="Synthetic re-ingest target",
            content=f"Revised body.\n\n{_BODY}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="Synthetic re-ingest target",
    )

    # Assert — same row, body updated, tier PRESERVED.
    assert again.document_id == first.document_id, "must be an in-place update"
    row = test_db.execute(
        "SELECT sensitivity, content FROM documents WHERE id = %s",
        (first.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == CONFIDENTIAL, (
        "a re-ingest that passed no --sensitivity must NOT downgrade the tier; "
        "silently un-protecting a document is the worst failure mode of this "
        "feature"
    )
    assert "Revised body." in row[1], "the body should still have been updated"


def test_reingest_can_escalate_a_normal_doc_to_confidential(
    test_db: psycopg.Connection[Any],
) -> None:
    """The other half of escalate-only: marking up on re-ingest DOES apply.

    Without this, "never downgrade" could be implemented as "never write on the
    update path at all", which would silently ignore a user explicitly passing
    ``--sensitivity confidential`` on a re-ingest.
    """
    first = _ingest(
        test_db,
        embedder=FakeEmbedder(),
        title="Synthetic escalation target",
        sensitivity=DEFAULT_SENSITIVITY,
    )
    assert first.document_id is not None

    again = ingest_document(
        test_db,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title="Synthetic escalation target",
            content=f"Revised body.\n\n{_BODY}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="Synthetic escalation target",
        sensitivity=CONFIDENTIAL,
    )

    assert again.document_id == first.document_id
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (first.document_id,)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL


def test_invalid_sensitivity_is_refused_before_any_write(
    test_db: psycopg.Connection[Any],
) -> None:
    """A typo'd level raises and stores nothing.

    Fails CLOSED on the write path: a user who typed ``--sensitivity
    confidental`` must not end up with a document they believe is protected and
    which is not. The CHECK constraint would also catch this, but raising in
    Python keeps the message actionable and leaves no partial row behind.
    """
    before = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert before is not None

    with pytest.raises(SensitivityError):
        _ingest(
            test_db,
            embedder=FakeEmbedder(),
            title="Synthetic typo level",
            sensitivity="confidental",
        )

    after = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert after is not None
    assert after[0] == before[0], "a refused ingest must write nothing"
