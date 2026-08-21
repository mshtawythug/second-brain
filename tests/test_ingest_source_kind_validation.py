"""``--source`` / MCP ``source`` must be a known kind at the WRITE boundary.

``sources.kind`` is bare ``TEXT NOT NULL`` with no CHECK (``001_init.sql:6``),
so any string the ingest paths accept becomes a real, permanent bucket. Every
read surface treats the column as a closed four-value enum, and the mismatch is
not merely cosmetic: ``brain.facets`` documents that clicking a facet selects
precisely the documents it counted, and a document ingested as ``--source none``
breaks that promise in the worst direction -- it is COUNTED in the ``none``
bucket (which the facet panel builds from ``d.source_id IS NULL``... which the
document does NOT satisfy, since it HAS a sources row whose kind is the literal
string "none") and is then absent when that bucket is clicked.

Fixing this on the read side would mean special-casing one sentinel string while
every other string stays accepted, so it is fixed here, on the way in.

These tests need no database: validation runs before any connection is opened,
which is itself part of the contract being asserted.
"""

from __future__ import annotations

import pytest
import typer

from brain.source_kinds import (
    VALID_SOURCE_KINDS,
    InvalidSourceKind,
    validate_source_kind,
)


class TestValidateSourceKind:
    """The shared guard both entry points delegate to."""

    @pytest.mark.parametrize("kind", sorted(VALID_SOURCE_KINDS))
    def test_accepts_and_returns_every_canonical_kind(self, kind: str) -> None:
        assert validate_source_kind(kind) == kind

    @pytest.mark.parametrize(
        "kind",
        ["none", "None", "", "email", "meeting", "Krisp", "krisp ", "manual\n"],
    )
    def test_rejects_anything_outside_the_closed_set(self, kind: str) -> None:
        """Includes the near-misses: case variants and whitespace.

        ``none`` leads the list because it is the one value that is also a
        *facet* value, so it is the one a caller is most likely to copy out of a
        read surface and feed back in as a write.
        """
        with pytest.raises(InvalidSourceKind):
            validate_source_kind(kind)

    def test_error_message_names_the_value_and_every_accepted_kind(self) -> None:
        """Actionable means: what you sent, and what you could have sent."""
        # Act
        with pytest.raises(InvalidSourceKind) as excinfo:
            validate_source_kind("none")

        # Assert
        message = str(excinfo.value)
        assert "none" in message
        for kind in VALID_SOURCE_KINDS:
            assert kind in message, f"{kind!r} missing from {message!r}"


class TestCliIngestStdinBoundary:
    """``brain ingest-stdin --source`` (``cli_ingest.py``)."""

    def test_rejects_unknown_source_before_reading_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails as a BadParameter, and without consuming the piped payload.

        The stdin sentinel makes the ordering claim testable: if validation ever
        moves below ``sys.stdin.read()`` this raises RuntimeError instead of
        BadParameter and the test fails with a different exception type.
        """
        # Arrange
        from brain import cli_ingest

        class ExplodingStdin:
            def read(self) -> str:
                raise RuntimeError("stdin was read before --source was validated")

        monkeypatch.setattr(cli_ingest.sys, "stdin", ExplodingStdin())

        # Act / Assert
        with pytest.raises(typer.BadParameter) as excinfo:
            cli_ingest.ingest_stdin(
                source="none",
                external_id="ext-1",
                title="Synthetic title",
            )
        assert "none" in str(excinfo.value)

    def test_accepts_a_canonical_kind_and_proceeds_to_read_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not reject valid input.

        Reaching the stdin read proves the guard let a real kind through; the
        command then exits 1 on empty stdin, which is pre-existing behaviour and
        needs no database.
        """
        # Arrange
        from brain import cli_ingest

        class EmptyStdin:
            def read(self) -> str:
                return ""

        monkeypatch.setattr(cli_ingest.sys, "stdin", EmptyStdin())

        # Act / Assert — typer.Exit(1) for empty stdin, NOT BadParameter.
        with pytest.raises(typer.Exit) as excinfo:
            cli_ingest.ingest_stdin(
                source="krisp",
                external_id="ext-1",
                title="Synthetic title",
            )
        assert excinfo.value.exit_code == 1


class TestMcpIngestStdinBoundary:
    """``brain_ingest_stdin`` (``mcp_server.py``) -- same hole, model-chosen value."""

    def test_rejects_unknown_source_without_opening_a_connection(self) -> None:
        """Rejected before ``_get_state()``, so no DB or embedder is required.

        That ordering is the reason this test needs no fixtures; if validation
        moved below ``_get_state()`` the test would fail on a connection error
        rather than pass, which is the intended signal.
        """
        # Arrange
        from brain import mcp_server

        # Act / Assert
        with pytest.raises(Exception) as excinfo:
            mcp_server.brain_ingest_stdin(
                content="synthetic body text",
                source="none",
                external_id="ext-1",
                title="Synthetic title",
            )
        assert "none" in str(excinfo.value)

    def test_empty_content_still_wins_over_the_source_check(self) -> None:
        """Ordering guard: the pre-existing empty-content error is unchanged."""
        from brain import mcp_server

        with pytest.raises(Exception) as excinfo:
            mcp_server.brain_ingest_stdin(
                content="   ",
                source="none",
                external_id="ext-1",
                title="Synthetic title",
            )
        assert "content is empty" in str(excinfo.value)


def test_the_kind_set_has_exactly_one_definition_the_others_mirror() -> None:
    """All four copies of the set must agree.

    ``cli`` and ``vault.links`` now import or mirror ``source_kinds``;
    ``ui.schemas`` keeps a deliberate copy (it must not import the Typer CLI)
    which ``tests/test_ui_schemas.py`` guards from the other direction. This
    asserts the whole set at once so a fifth kind cannot be added in one place.
    """
    from brain.cli import _VALID_SOURCE_KINDS
    from brain.ui.schemas import VALID_SOURCE_KINDS as UI_KINDS
    from brain.vault.links import _SOURCE_KINDS

    assert _VALID_SOURCE_KINDS == VALID_SOURCE_KINDS
    assert UI_KINDS == VALID_SOURCE_KINDS
    assert _SOURCE_KINDS == VALID_SOURCE_KINDS
