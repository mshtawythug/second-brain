"""``brain ask`` must not feed confidential bodies to the model (F6).

``ask`` is the highest-consequence retrieval surface in the codebase and it was
completely ungated. Two things leave through it, and only one of them is
visible in the return value:

1. **``citations[].snippet``** — raw chunk text, in the response.
2. **The PROMPT** — every retrieved snippet is pasted into the synthesize
   prompt and sent to the model. ``answer`` then comes BACK as prose derived
   from that body. A test that only inspects the response would pass while the
   body had already left the process, and the paraphrase in ``answer`` is not
   something a marker check could ever catch.

So the assertions here look at ``chat.prompts`` — what was actually transmitted
— not merely at what was returned. That is the real egress point.

The docstring on ``brain_ask`` used to say "Document snippets only reach the
LLM — never full bodies", which reads as a safety property and is how this went
unnoticed: a snippet IS body text, only less of it.

All fixture data is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.ask import ask_no_loop
from brain.config import Config

#: Present ONLY in the confidential body, never in the question.
BODY_MARKER = "quokkavolt"

QUESTION = "wind-down severance terms"


class _RecordingChat:
    """Fake ``ChatJson`` that records every prompt actually sent to the model.

    A standard injected test double — ``chat`` is a constructor parameter of
    ``ask`` precisely so this needs no monkey-patching of production modules.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        # Cite BOTH slots. Citations are built from the ``[N]`` markers the
        # model emits, so citing only ``[1]`` would make every assertion below
        # depend on which document happened to rank FIRST — a property of the
        # ranker, not of the gate under test. Citing both makes the assertions
        # read the retrieved SET, which is what the gate actually controls.
        return {"answer": "Synthesized answer [1][2]."}


@pytest.fixture
def confidential_corpus(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> str:
    """One confidential doc matching the question + a normal decoy."""
    conf_id = seed_doc(
        title="Confidential Wind-Down Memo",
        content=(
            f"Wind-down severance terms are filed under {BODY_MARKER}. " * 6
        ),
    )
    test_db.execute(
        "UPDATE documents SET sensitivity='confidential' WHERE id=%s", (conf_id,)
    )
    # The decoy must rank for the SAME question, or gating the confidential
    # document leaves zero results and "the gate didn't break ask" would pass
    # by returning nothing at all. It therefore repeats the question's salient
    # terms ("wind-down", "severance", "terms") rather than paraphrasing them.
    seed_doc(
        title="Public Wind-Down Overview",
        content=(
            "Public wind-down severance terms overview for the whole team. " * 6
        ),
    )
    return conf_id


def _fixture_is_not_vacuous(conn: psycopg.Connection[Any], doc_id: str) -> None:
    """Guard: really confidential, and really has body containing the marker."""
    row = conn.execute(
        "SELECT d.sensitivity, "
        "(SELECT string_agg(c.content, ' ') FROM chunks c WHERE c.document_id = d.id) "
        "FROM documents d WHERE d.id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "confidential", "fixture must be confidential"
    assert row[1] and BODY_MARKER in row[1], (
        "fixture must have a NON-EMPTY chunk containing the marker, or these "
        "assertions pass vacuously"
    )


def _run(
    conn: psycopg.Connection[Any], chat: _RecordingChat, **kwargs: Any
) -> Any:
    from tests.conftest import FakeEmbedder

    return ask_no_loop(
        conn,
        Config(database_url="postgresql://unused/none"),
        embedder=FakeEmbedder(),
        chat=chat,
        question=QUESTION,
        limit=5,
        **kwargs,
    )


def test_ask_does_not_transmit_confidential_body_to_the_model(
    test_db: psycopg.Connection[Any], confidential_corpus: str
) -> None:
    """THE assertion that matters: the body never reaches the wire."""
    _fixture_is_not_vacuous(test_db, confidential_corpus)
    chat = _RecordingChat()

    _run(test_db, chat, exclude_confidential=True)

    assert chat.prompts, "the synthesize step must actually have run"
    assert BODY_MARKER not in " ".join(chat.prompts).lower()


def test_ask_does_not_return_confidential_snippet(
    test_db: psycopg.Connection[Any], confidential_corpus: str
) -> None:
    """The response-side half — citations carry raw chunk text."""
    _fixture_is_not_vacuous(test_db, confidential_corpus)
    chat = _RecordingChat()

    result = _run(test_db, chat, exclude_confidential=True)

    blob = " ".join(
        f"{c.title} {c.snippet}" for c in result.citations
    ).lower()
    assert BODY_MARKER not in blob
    assert confidential_corpus not in {c.document_id for c in result.citations}


def test_ask_still_answers_from_normal_documents(
    test_db: psycopg.Connection[Any], confidential_corpus: str
) -> None:
    """The gate must exclude one tier, not break ask."""
    _fixture_is_not_vacuous(test_db, confidential_corpus)
    chat = _RecordingChat()

    result = _run(test_db, chat, exclude_confidential=True)

    assert result.answer
    assert "Public Wind-Down Overview" in {c.title for c in result.citations}


def test_ask_includes_confidential_by_default(
    test_db: psycopg.Connection[Any], confidential_corpus: str
) -> None:
    """``ask`` defaults to INCLUDE — the CLI sits inside the trust boundary.

    Also the non-vacuity proof for the tests above: without this, an empty
    retrieval would make them pass for the wrong reason. The MCP boundary is
    what flips the default, via ``exclude_confidential=not include_confidential``.
    """
    _fixture_is_not_vacuous(test_db, confidential_corpus)
    chat = _RecordingChat()

    result = _run(test_db, chat)

    assert confidential_corpus in {c.document_id for c in result.citations}
    assert BODY_MARKER in " ".join(chat.prompts).lower()
