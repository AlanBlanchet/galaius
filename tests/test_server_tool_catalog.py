"""Every interact tool declares a category and publishes it in MCP `_meta`; the packaged snapshot
(`interact/data/mcp_tools.json`, what the web server groups an agent's tools by) is the live list.

Regenerate after adding or re-categorising a tool:
    python -c "import json; from interact.server import core; from interact.data import PackageData; PackageData.path(PackageData.MCP_TOOLS).write_text(json.dumps(core.tool_catalog(), indent=1) + '\\n')"
"""

import asyncio
import json
from typing import get_args

from interact.data import PackageData
from interact.server import core


def test_every_tool_publishes_its_category_and_the_snapshot_is_current():
    listed = asyncio.run(core.mcp.list_tools())
    assert listed and all((tool.meta or {}).get("category") in get_args(core.ToolCategory) for tool in listed)
    assert json.loads(PackageData.read(PackageData.MCP_TOOLS)) == core.tool_catalog()
