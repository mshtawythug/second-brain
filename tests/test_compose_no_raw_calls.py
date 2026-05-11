"""AST-walking regression test that bans raw `docker compose` calls.

Every `docker compose` invocation must go through `brain._compose.compose_cmd`,
which prepends -f and --project-name flags. This test fails if any other file
constructs a raw ``["docker", "compose", ...]`` argv passed to subprocess.
"""
import ast
from pathlib import Path

import pytest

_BRAIN_SRC = Path(__file__).resolve().parents[1] / "src" / "brain"
_ALLOWED_EXCEPTION = _BRAIN_SRC / "_compose.py"
_SKIP_DIRS = {"__pycache__", "quartz_overrides"}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in _BRAIN_SRC.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def _is_subprocess_call(node: ast.AST) -> bool:
    """Return True if node is a call to subprocess.{run,Popen,check_call,check_output}."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        return fn.value.id == "subprocess" and fn.attr in {
            "run",
            "Popen",
            "check_call",
            "check_output",
        }
    return False


def _first_arg_starts_with_docker_compose(call: ast.Call) -> bool:
    """Return True if the first positional arg is [\"docker\", \"compose\", ...]."""
    if not call.args:
        return False
    arg = call.args[0]
    if not isinstance(arg, ast.List) or len(arg.elts) < 2:
        return False
    first, second = arg.elts[0], arg.elts[1]
    return (
        isinstance(first, ast.Constant)
        and first.value == "docker"
        and isinstance(second, ast.Constant)
        and second.value == "compose"
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(_BRAIN_SRC)))
def test_no_raw_docker_compose_call(path: Path) -> None:
    """Every `docker compose` call must go through brain._compose.compose_cmd."""
    if path == _ALLOWED_EXCEPTION:
        pytest.skip("brain._compose is the canonical helper, allowed")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if _is_subprocess_call(node) and _first_arg_starts_with_docker_compose(node):
            offenders.append(node.lineno)  # type: ignore[attr-defined]
    if offenders:
        rel = path.relative_to(_BRAIN_SRC)
        pytest.fail(
            f"{rel} has raw `docker compose` subprocess call(s) at lines: {offenders}\n"
            f"  Route every docker-compose invocation through brain._compose.compose_cmd "
            f"so the -f and --project-name flags are always present."
        )
