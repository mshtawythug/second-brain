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
