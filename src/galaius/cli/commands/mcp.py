"""Deferred MCP-server command boundary."""

from galaius.cli.command_bootstrap import apply_command_environment
from galaius.server import main as serve


def mcp() -> None:
    """Run the MCP server over stdio. Clients launch this; register it with `galaius install`."""
    apply_command_environment()
    serve()


__all__ = ["mcp"]
