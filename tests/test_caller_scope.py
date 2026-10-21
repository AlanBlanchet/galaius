"""An agent acts only as its own run (2026-10-21: an agent sent as the person after clearing its run variables, from a
unit `systemd-run` started, then from one it named like galaius's own): the run a process acts as is read from the run
scopes of the process and its ancestors, never from its environment or a unit's name."""

import uuid

import pytest

from galaius import caller as caller_module
from galaius import processes
from galaius.agents import messaging
from galaius.agents import registry as reg
from galaius.agents.spawn_line import Spawn
from galaius.caller import Caller

RUN = str(uuid.UUID(int=11 << 96))
SCOPE = f"galaius-run-{RUN}-0badc0de.scope"
ASKING = 4242


@pytest.mark.parametrize(("units", "transient", "environment", "sender"), [
    ([SCOPE], (), {}, RUN),                                                                      # `env -u`, `setsid`
    ([SCOPE], (), {"GALAIUS_RUN_ID": str(uuid.uuid4()), "GALAIUS_PARENT_RUN_ID": ""}, RUN),     # another run named
    (["galaius-task-mine.scope", "user@1000.service", SCOPE], (), {}, RUN),                     # `systemd-run --scope --unit=…`
    (["galaius-task-mine.service"], ("galaius-task-mine.service",), {}, None),                  # `systemd-run --unit=galaius-task-…`
    (["run-u3869.service"], ("run-u3869.service",), {}, None),                                  # `systemd-run --user …`
    (["app-gnome-code-922586.scope", "gnome-shell.service"], (), {}, reg.PERSON_SENDER),                 # his editor (a scope)
    (["galaius-machine.service"], (), {}, reg.PERSON_SENDER),                                    # the app's own service
])
def test_a_process_acts_as_the_run_whose_scope_holds_it_or_an_ancestor(monkeypatch, units, transient, environment, sender):
    """`units`: the unit of the asking process, then of each ancestor; `transient`: the units no unit file defines."""
    chain = {ASKING + depth: unit for depth, unit in enumerate(units)}
    monkeypatch.setattr(processes, "process_unit", chain.get)
    monkeypatch.setattr(processes, "process_ancestry", lambda pid: tuple((other, None) for other in chain if other > pid))
    monkeypatch.setattr(caller_module, "process_unit", chain.get)
    monkeypatch.setattr(caller_module, "run_of_process", processes.run_of_process)
    monkeypatch.setattr(caller_module, "transient_service", lambda unit: unit in transient and unit.endswith(".service"))
    with Caller(environment=environment, cwd=".", pid=ASKING).bound() as asking:
        if sender is None:
            assert asking.unowned() and messaging.send_refusal(RUN) == messaging.UNOWNED
            with pytest.raises(ValueError, match="systemd-run"):
                messaging.sender_id()
            with pytest.raises(ValueError, match="systemd-run"):
                Spawn.asked(task="review it", cwd=".", agent="code-reviewer")
            return
        assert messaging.sender_id() == sender
        if sender == RUN:
            assert asking.environment["GALAIUS_PARENT_RUN_ID"] == RUN
            assert Spawn.asked(task="review it", cwd=".", agent="code-reviewer").parent_run_id == RUN   # never outside its tree
