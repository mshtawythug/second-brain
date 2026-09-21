"""``connect accept --write`` must not publish a confidential document (F6).

``iter_suggestions`` was recorded as F6-gated and is — it is the LIST. **Neither
accept path goes through it.** ``cli_connect._write_wikilink`` and
``mcp_server._connect_write_wikilink`` both reach ``connect.load_action_context``,
whose SQL joined ``sd``/``td`` with no sensitivity predicate, so the recorded
status was true of the surface it named and silently not of the WRITE. Accepting
a suggestion whose target was confidential appended, into a source page that
publishes because its OWN frontmatter says ``normal``::

    - [[_ingested/krisp/2026-07-02-conf|Wind-down memo (synthetic confidential)]]

— the confidential title as anchor text and its slug as the link target.

**One command, two audiences, and the split lands between its two steps.**
Flipping the row's status is local DB state inside the trust boundary; the
``--write`` is a file in ``cfg.vault_path`` that Quartz serves. So the status
flip stays ungated and the write refuses — and the status flip staying ungated
is asserted here, not assumed, because a gate that made the row unactionable
would be its own bug.

**Why a refusal rather than a second gated payload** (the ``review weekly``
fix): there is no gated variant of this wikilink. Its entire content IS the
confidential document. The choice is publish it or do not, so it does not, and
says so out loud.

Both endpoints are gated with honestly unequal arguments — a confidential TARGET
is the reproduced disclosure; a confidential SOURCE is defence in depth, since
that page is unpublished only by Quartz's frontmatter filter. Both are asserted.

Every refusal assertion is paired with a control proving the same fixture DOES
write the bullet when the tier is normal, so no "absent" here can be satisfied by
a writeback that silently does nothing. All fixture data is synthetic.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli, mcp_server
from brain import connect as connect_mod
from brain.sensitivity import CONFIDENTIAL

from .test_mcp_review_scan import _install_state

runner = CliRunner()

CONF_TITLE = "Wind-down memo (synthetic confidential)"
CONF_STEM = "2026-07-02-conf"
NORMAL_TITLE = "Roadmap notes (synthetic normal)"
PLAIN_TARGET_TITLE = "Vendor list (synthetic normal)"

_SOURCE_REL = "_ingested/krisp/2026-07-01-source.md"
_CONF_REL = f"_ingested/krisp/{CONF_STEM}.md"
_PLAIN_REL = "_ingested/krisp/2026-07-03-plain.md"


def _doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str,
    confidential: bool = False,
) -> str:
    source_id = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('krisp', %s, '{}'::jsonb) RETURNING id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    salted = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    doc_id = str(
        conn.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type,
                 tags, metadata, vault_path, kind)
            VALUES (%s, %s, %s, %s, 'transcript', '{}', '{}'::jsonb, %s,
                    'ingested')
            RETURNING id::text
            """,
            (
                source_id,
                title,
                salted,
                hashlib.sha256(salted.encode("utf-8")).hexdigest(),
                vault_path,
            ),
        ).fetchone()[0]
    )
    if confidential:
        conn.execute(
            "UPDATE documents SET sensitivity=%s WHERE id=%s::uuid",
            (CONFIDENTIAL, doc_id),
        )
    return doc_id


def _suggestion(conn: psycopg.Connection[Any], source: str, target: str) -> str:
    return str(
        conn.execute(
            "INSERT INTO link_suggestions (source_doc_id, target_doc_id, score) "
            "VALUES (%s, %s, 0.9) RETURNING id::text",
            (source, target),
        ).fetchone()[0]
    )


def _mirror(root: Path, rel: str, title: str, sensitivity: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\nsensitivity: {sensitivity}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def wired(test_db: psycopg.Connection, tmp_path: Path) -> dict[str, str]:
    """A normal source with two pending suggestions: one confidential target,
    one normal. The pair differs ONLY in ``sensitivity``, so any difference in
    writeback behaviour is attributable to the gate. The normal one is the
    control that keeps every refusal assertion non-vacuous.
    """
    source = _doc(test_db, title=NORMAL_TITLE, vault_path=_SOURCE_REL)
    conf = _doc(test_db, title=CONF_TITLE, vault_path=_CONF_REL, confidential=True)
    plain = _doc(test_db, title=PLAIN_TARGET_TITLE, vault_path=_PLAIN_REL)
    _mirror(tmp_path, _SOURCE_REL, NORMAL_TITLE, "normal")
    _mirror(tmp_path, _CONF_REL, CONF_TITLE, "confidential")
    _mirror(tmp_path, _PLAIN_REL, PLAIN_TARGET_TITLE, "normal")
    return {
        "source": source,
        "conf_suggestion": _suggestion(test_db, source, conf),
        "plain_suggestion": _suggestion(test_db, source, plain),
    }


def _status(conn: psycopg.Connection, suggestion_id: str) -> str:
    return str(
        conn.execute(
            "SELECT status FROM link_suggestions WHERE id = %s::uuid",
            (suggestion_id,),
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# CONTROL — the same command, the same fixture, a normal target: it DOES write
# ---------------------------------------------------------------------------


def test_normal_target_is_still_written(
    wired: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, every refusal below could be a writeback that never works."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["connect", "accept", wired["plain_suggestion"][:8], "--write"]
    )

    assert result.exit_code == 0, result.stdout
    page = (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")
    assert PLAIN_TARGET_TITLE in page
    assert "## See Also" in page
    assert _status(test_db, wired["plain_suggestion"]) == "accepted"


# ---------------------------------------------------------------------------
# The leak — CLI
# ---------------------------------------------------------------------------


def test_cli_refuses_to_write_a_confidential_target(
    wired: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FIX. Disclosure assertions first, bookkeeping after."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    before = (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")

    result = runner.invoke(
        cli.app, ["connect", "accept", wired["conf_suggestion"][:8], "--write"]
    )

    page = (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")
    # Both halves of the bullet: the title becomes the rendered anchor text,
    # the stem becomes the link target and the contentIndex record.
    assert CONF_TITLE not in page
    assert CONF_STEM not in page
    assert page == before
    # Loud, not silent — the operator is told why.
    assert result.exit_code != 0
    assert "confidential" in result.output
    # And the row stays actionable, exactly as the error message promises.
    assert _status(test_db, wired["conf_suggestion"]) == "pending"


def test_cli_refuses_to_write_into_a_confidential_source(
    test_db: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second endpoint. Defence in depth — see the module docstring."""
    source = _doc(
        test_db, title=CONF_TITLE, vault_path=_CONF_REL, confidential=True
    )
    target = _doc(test_db, title=PLAIN_TARGET_TITLE, vault_path=_PLAIN_REL)
    sug = _suggestion(test_db, source, target)
    _mirror(tmp_path, _CONF_REL, CONF_TITLE, "confidential")
    _mirror(tmp_path, _PLAIN_REL, PLAIN_TARGET_TITLE, "normal")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    before = (tmp_path / _CONF_REL).read_text(encoding="utf-8")

    result = runner.invoke(cli.app, ["connect", "accept", sug[:8], "--write"])

    assert (tmp_path / _CONF_REL).read_text(encoding="utf-8") == before
    assert PLAIN_TARGET_TITLE not in before
    assert result.exit_code != 0
    assert "confidential" in result.output


# ---------------------------------------------------------------------------
# The gate is at the WRITE, not the action — asserted, not assumed
# ---------------------------------------------------------------------------


def test_status_flip_without_write_is_ungated(
    wired: dict[str, str],
    test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accepting WITHOUT ``--write`` touches no file, so it is not gated."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["connect", "accept", wired["conf_suggestion"][:8]]
    )

    assert result.exit_code == 0, result.stdout
    assert _status(test_db, wired["conf_suggestion"]) == "accepted"


def test_reject_of_a_confidential_suggestion_still_works(
    wired: dict[str, str],
    test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The reason ``load_action_context`` itself is NOT gated.

    A gate on the shared loader would make a suggestion touching a confidential
    document impossible to reject — permanently pending, and re-proposed on
    every refresh. This test is what that docstring claim is worth.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["connect", "reject", wired["conf_suggestion"][:8]]
    )

    assert result.exit_code == 0, result.stdout
    assert _status(test_db, wired["conf_suggestion"]) == "rejected"


# ---------------------------------------------------------------------------
# The MCP twin — same gate, mapped to INVALID_PARAMS
# ---------------------------------------------------------------------------


def test_mcp_refuses_to_write_a_confidential_target(
    wired: dict[str, str],
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
) -> None:
    """Both writeback call sites share the gate; both are pinned.

    The control below is the same tool on the NORMAL suggestion in the same
    fixture, so "it refused" is about the tier and not about the tool being
    broken.
    """
    _install_state(monkeypatch, fake_embedder, tmp_path)
    before = (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        mcp_server.brain_connect_accept(id=wired["conf_suggestion"][:8], write=True)

    page = (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")
    assert CONF_TITLE not in page
    assert CONF_STEM not in page
    assert page == before
    assert "confidential" in str(excinfo.value)
    assert _status(test_db, wired["conf_suggestion"]) == "pending"

    # CONTROL — the same tool, same run, normal target: writes.
    payload = mcp_server.brain_connect_accept(
        id=wired["plain_suggestion"][:8], write=True
    )
    assert payload["wikilink_written"] is True
    assert PLAIN_TARGET_TITLE in (tmp_path / _SOURCE_REL).read_text(encoding="utf-8")


def test_shared_gate_is_the_one_both_call_sites_use(
    wired: dict[str, str], test_db: psycopg.Connection
) -> None:
    """Pin the policy at the shared function, not only at its two callers.

    A future third writeback caller that forgets the check is the failure this
    guards against, so the unit is asserted directly: the loader carries both
    tiers, and the gate reads them.
    """
    ctx = connect_mod.load_action_context(test_db, wired["conf_suggestion"])
    assert ctx.target_sensitivity == CONFIDENTIAL
    assert ctx.source_sensitivity != CONFIDENTIAL

    with pytest.raises(connect_mod.ConnectError):
        connect_mod.assert_see_also_publishable(ctx)

    plain = connect_mod.load_action_context(test_db, wired["plain_suggestion"])
    connect_mod.assert_see_also_publishable(plain)  # does not raise
