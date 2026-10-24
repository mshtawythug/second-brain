"""Import-hygiene regression tests: hot CLI import must not pull heavy graph deps."""
import subprocess
import sys


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
