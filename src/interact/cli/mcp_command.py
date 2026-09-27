"""Deferred MCP-server command boundary."""

from interact.cli.command_bootstrap import apply_command_environment
from interact.server import main as serve


def mcp() -> None:
    """Run the MCP server over stdio. Clients launch this; register it with `interact install`."""
    apply_command_environment()
    serve()


__all__ = ["mcp"]
