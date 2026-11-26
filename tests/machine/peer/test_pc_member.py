"""One prompt, one other PC of his: the CLI member of the class (`galaius agents spawn --machine <name|auto> "<task>"` reaches his other PC and, when it cannot, says the real cause).

Owner 2026-11-25: « be able to prompt and for galaius to send it to the other PC »."""
from uuid import uuid4

import pytest

from galaius_core import PcFleet, PcState
from galaius.cli.commands.agents.spawn import _spawn_on_machine
from galaius.machine.peer.words import pick

CANNOT_ISOLATE = "it cannot isolate a run: « Takes runs » on its page, turned on, allows runs without isolation, or leave it off"


def laptop() -> PcState:
    return PcState(id=uuid4(), name="Laptop", state="online", dispatchable=False, why_not=CANNOT_ISOLATE)


def test_auto_names_why_the_only_online_pc_takes_nothing() -> None:
    """« online » is no reason: the sentence carries the server's `why_not`."""
    fleet = PcFleet(pcs=(PcState(id=uuid4(), name="this", this=True, state="online", dispatchable=True), laptop()))
    with pytest.raises(ValueError, match="isolate"):
        pick(fleet, "auto")


def test_machine_with_only_a_task_needs_no_project_and_no_agent(capsys: pytest.CaptureFixture[str]) -> None:
    """`--machine auto "<task>"` asks for neither flag: the project and the role have defaults (here no runner answers)."""
    with pytest.raises(SystemExit):
        _spawn_on_machine("auto", None, None, "hostname", agent=None, provider=None, model=None, permission="read_only")
    said = capsys.readouterr().err
    assert "needs --project" not in said and "needs --agent" not in said
