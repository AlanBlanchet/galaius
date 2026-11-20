"""`report_write`: every role with a tool list can leave its report, and writes nothing but its own ledger file."""

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from galaius.agents import messaging
from galaius.agents.tool_gateway import AgentToolList
from galaius.server.tools import agents

RUN = "1e917136-8d78-45ed-b73e-1791b2acd837"


@pytest.fixture
def out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / "out" / "agents"))
    monkeypatch.setattr(messaging, "asking_run", lambda: RUN)
    return tmp_path / "out"


def test_a_run_writes_its_ledger_file_whole_each_time_and_nothing_else(out: Path) -> None:
    said = asyncio.run(agents.report_write("# first\n"))
    asyncio.run(agents.report_write("# second\n"))
    file = out / "ledgers" / "1e917136.md"
    assert file.read_text() == "# second\n" and str(file) in said
    assert sorted(path.name for path in (out / "ledgers").iterdir()) == ["1e917136.md"]
    if sys.platform != "win32":  # a protected DACL there, no POSIX mode
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


@pytest.mark.parametrize("text", ["", "  \n", "x" * (agents.REPORT_MAX + 1)], ids=["empty", "blank", "oversized"])  # a 200,001-character id made the test folder's name too long on Windows
def test_an_empty_or_oversized_report_is_refused_and_leaves_the_file_alone(out: Path, text: str) -> None:
    asyncio.run(agents.report_write("kept"))
    with pytest.raises(ValueError):
        asyncio.run(agents.report_write(text))
    assert (out / "ledgers" / "1e917136.md").read_text() == "kept"


def test_nobody_asking_writes_nothing(out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messaging, "asking_run", lambda: None)
    with pytest.raises(ValueError):
        asyncio.run(agents.report_write("report"))
    assert not (out / "ledgers").exists()


@pytest.mark.parametrize("tools", [["Read"], ["Write"], ["Bash", "Edit"]])
def test_every_role_with_a_tool_list_is_granted_it_and_a_role_with_none_stays_with_none(tools: list[str]) -> None:
    listing = AgentToolList()
    assert "mcp__galaius__report_write" in listing.granted("any", tools, "mcp__galaius__")
    assert listing.granted("any", [], "mcp__galaius__") == [] and listing.granted("any", None, "mcp__galaius__") is None


@pytest.mark.parametrize("run_id", ["../../../etc/pwn", "1e917136/../x", "", "not-a-run"])
def test_an_id_that_is_no_run_id_names_no_file(out: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> None:
    monkeypatch.setattr(messaging, "asking_run", lambda: run_id)
    with pytest.raises(ValueError):
        asyncio.run(agents.report_write("report"))
    assert not list(out.rglob("*.md"))
