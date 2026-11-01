"""Import-hygiene regression tests: hot CLI import must not pull heavy graph deps."""
import subprocess
import sys

import pytest


def test_cli_import_does_not_load_networkx() -> None:
    code = "import sys, brain.cli; sys.exit(1 if 'networkx' in sys.modules else 0)"
    proc = subprocess.run([sys.executable, "-c", code])
    assert proc.returncode == 0, "importing brain.cli must not import networkx"


def test_mcp_server_import_does_not_load_networkx() -> None:
    code = "import sys, brain.mcp_server; sys.exit(1 if 'networkx' in sys.modules else 0)"
    proc = subprocess.run([sys.executable, "-c", code])
    assert proc.returncode == 0, "importing brain.mcp_server must not import networkx"


def test_base_imports_do_not_load_document_parsers() -> None:
    """``pypdf`` and ``docx`` load only when a PDF or DOCX is extracted (RM-07).

    The extractor registry holds each parser's error types behind a loader for
    exactly this reason; a tuple built at registration would import both on
    every ``brain`` invocation.
    """
    code = (
        "import sys, brain.cli, brain.mcp_server, brain.ingest.dispatch; "
        "sys.exit(1 if {'pypdf', 'docx', 'pdfplumber'} & set(sys.modules) else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code])
    assert proc.returncode == 0, "importing the CLI must not import pypdf / docx"


def _fresh(code: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)


@pytest.mark.parametrize(
    "module",
    [
        "brain.mcp_state",
        "brain.mcp_auth",
        "brain.mcp_tool_classes",
        "brain.mcp_http_guard",
        "brain.mcp_http_tools",
        "brain.mcp_http_conn",
        "brain.graph_rag.public_entities",
        "brain.settings.mcp_http",
        "brain.cli_mcp",
        "brain.mcp_http",
    ],
)
def test_each_rm46_module_imports_cold(module: str) -> None:
    """RM-46: a new top-level import must not close an import cycle (RM-45's lesson)."""
    proc = _fresh(f"import {module}")
    assert proc.returncode == 0, proc.stderr.decode()


def test_cli_import_does_not_load_the_http_transport() -> None:
    """``brain mcp serve`` imports the server and uvicorn inside its body only."""
    proc = _fresh(
        "import sys, brain.cli; "
        "bad = {'brain.mcp_http', 'brain.mcp_server', 'uvicorn'} & set(sys.modules); "
        "sys.exit(1 if bad else 0)"
    )
    assert proc.returncode == 0, "importing brain.cli must not import the HTTP transport"


def test_the_token_module_needs_no_sdk_or_cli() -> None:
    """``brain mcp token`` stays cheap: ``brain.mcp_auth`` is a stdlib leaf."""
    proc = _fresh(
        "import sys, brain.mcp_auth; "
        "bad = {'mcp', 'starlette', 'typer', 'brain.mcp_server'} & set(sys.modules); "
        "sys.exit(1 if bad else 0)"
    )
    assert proc.returncode == 0, "brain.mcp_auth must not import the SDK, starlette or typer"
