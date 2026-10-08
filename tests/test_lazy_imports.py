"""The MCP server every agent's CLI waits on before its first turn must not pay for litellm at boot."""

import subprocess
import sys


def test_mcp_server_import_leaves_litellm_unloaded() -> None:
    probe = "import sys, galaius.cli.mcp_command, galaius.server; print('litellm.main' in sys.modules, 'openai.types' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=True)
    assert done.stdout.split() == ["False", "False"]
