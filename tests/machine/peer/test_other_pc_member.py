"""One prompt, one other PC of his: the CLI member of the class (`galaius agents spawn --machine <name|auto> "<task>"` reaches his other PC and, when it cannot, says the real cause).

Red today by design (owner 2026-11-25 « be able to prompt and for galaius to send it to the other PC »). Run: pytest tests/machine/peer/test_other_pc_member.py"""
from uuid import uuid4

import pytest

from galaius_core import PcFleet, PcState
from galaius.cli.commands.agents.spawn import _spawn_on_machine
from galaius.machine.peer.words import pick

CANNOT_ISOLATE = "it cannot isolate a run: « Takes runs » on its page, turned on, allows runs without isolation, or leave it off"


def lenovo() -> PcState:
    return PcState(id=uuid4(), name="AlanLenovo", state="online", dispatchable=False, why_not=CANNOT_ISOLATE)


def test_auto_names_why_the_only_online_pc_takes_nothing() -> None:
    """Today: « no other PC of yours takes a run now (AlanLenovo: online, ...) » (words.py:16): « online » is no reason; the server's `why_not` is dropped."""
    fleet = PcFleet(pcs=(PcState(id=uuid4(), name="this", this=True, state="online", dispatchable=True), lenovo()))
    with pytest.raises(ValueError, match="isolate"):
        pick(fleet, "auto")


def test_machine_with_only_a_task_needs_no_project_and_no_agent(capsys: pytest.CaptureFixture[str]) -> None:
    """Today: `--machine auto "<task>"` stops at « --machine needs --project » (spawn.py:31) then « --machine needs --agent » (words.py:35): the sender has both already (his conversation's project, the calling role)."""
    with pytest.raises(SystemExit):
        _spawn_on_machine("auto", None, None, "hostname", agent=None, provider=None, model=None, permission="read_only")
    said = capsys.readouterr().err
    assert "needs --project" not in said and "needs --agent" not in said
