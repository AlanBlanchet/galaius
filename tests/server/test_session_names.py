"""A browser session name becomes the session's profile folder (``<browser_profile_dir>/<name>``), so a
name that is not one plain path component (``../..``, ``/abs``, ``a/b``) reached any folder the server
can write. Every session tool refuses such a name before its body runs, and ``BrowserManager`` refuses it
at the folder itself."""

import asyncio
import inspect

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from galaius.browser.manager import BrowserManager
from galaius.server import core

from tests.support import browser_config

REFUSED = [
    "",
    ".",
    "..",
    "../..",
    "../../etc",
    "a/b",
    "a\\b",
    "/tmp/x",
    "C:\\x",
    "~",
    "-rf",
    ".hidden",
    "trailing.",
    "a b",
    "a\x00b",
    "café",
    "x" * 65,
    "nul",
    "COM1.txt",
]
ACCEPTED = ["default", "caller-3f9a", "web-test", "v1.2_x", "x" * 64]


@pytest.mark.parametrize("name", REFUSED)
def test_a_session_name_that_is_not_a_plain_slug_is_refused_everywhere(name, tmp_path):
    for tool, arguments in (("session", {"action": "close", "name": name}), ("get_page_state", {"session": name})):
        with pytest.raises(ToolError, match="invalid session name"):
            asyncio.run(core.mcp.call_tool(tool, arguments))
    with pytest.raises(ValueError, match="invalid session name"):
        BrowserManager(browser_config(browser_profile_dir=str(tmp_path / "profiles")), name)
    assert not (tmp_path / "profiles").exists()


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_plain_slug_is_a_session_name(name):
    assert "closed" in str(asyncio.run(core.mcp.call_tool("session", {"action": "close", "name": name})))


def test_every_session_parameter_passes_the_gate():
    """The gate runs on parameters defaulting to the auto-session marker; a tool taking `session` any
    other way would skip it."""
    ungated = [
        tool.name
        for tool in core.mcp._tool_manager.list_tools()
        if (param := inspect.signature(tool.fn).parameters.get("session")) and param.default != core._AUTO_SESSION
    ]
    assert not ungated
