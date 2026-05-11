"""Unit tests for brain._compose.compose_cmd output shape.

The AST drift test (``tests/test_compose_no_raw_calls.py``) verifies that
*other* code doesn't bypass this helper. These tests verify the helper
itself produces the right argv — both -f and --project-name brain must
always be present, in order, before any caller-supplied trailing args.
"""
from pathlib import Path

from brain._compose import compose_cmd


def test_default_brain_home(monkeypatch, tmp_path: Path) -> None:
    """compose_cmd reads BRAIN_HOME from env when no kwarg passed."""
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    result = compose_cmd("ps")
    assert result[0:3] == ["docker", "compose", "-f"]
    assert result[3] == str(tmp_path / "docker-compose.yml")
    assert result[4:6] == ["--project-name", "brain"]
    assert result[6:] == ["ps"]


def test_brain_home_kwarg_override(monkeypatch, tmp_path: Path) -> None:
    """The brain_home kwarg wins over the BRAIN_HOME env var."""
    other = tmp_path / "other"
    monkeypatch.setenv("BRAIN_HOME", str(other))
    result = compose_cmd("ps", brain_home=tmp_path)
    assert result[3] == str(tmp_path / "docker-compose.yml")


def test_full_argv_shape(tmp_path: Path) -> None:
    """Full argv shape: docker compose -f <file> --project-name brain <args...>."""
    result = compose_cmd("up", "-d", brain_home=tmp_path)
    assert result == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "docker-compose.yml"),
        "--project-name",
        "brain",
        "up",
        "-d",
    ]
