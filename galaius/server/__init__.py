"""The ``galaius`` MCP server. ``core`` owns the shared ``FastMCP`` instance, lifespan, instructions
and the browser session registry; ``vlm`` / ``browser_sandbox`` / ``targets`` / ``capture`` hold
helpers; the ``tools`` package holds the ``@mcp.tool`` surfaces. Importing this package registers
every tool; callers and tests import names from the module that defines them."""

from galaius.server import browser_sandbox, capture, core, targets, tools, vlm  # noqa: F401 — registration order
