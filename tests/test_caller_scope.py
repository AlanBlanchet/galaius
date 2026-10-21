"""An agent acts only as its own run (2026-10-21: an agent sent as the person after clearing its run variables, from a unit
`systemd-run` started): the run a process acts as is read from the run scope it lives in, never from its environment."""

import uuid

import pytest

from galaius import caller as caller_module
from galaius.agents import messaging
from galaius.agents import registry as reg
from galaius.agents.spawn_line import Spawn
from galaius.caller import Caller

RUN = str(uuid.UUID(int=11 << 96))
SCOPE = f"galaius-run-{RUN}-0badc0de.scope"


@pytest.mark.parametrize(("unit", "environment", "sender"), [
    (SCOPE, {}, RUN),                                                                    # `env -u GALAIUS_RUN_ID`, `setsid`
    (SCOPE, {"GALAIUS_RUN_ID": str(uuid.uuid4()), "GALAIUS_PARENT_RUN_ID": ""}, RUN),   # another run named, parent cleared
    ("app-gnome-code-922586.scope", {}, reg.PERSON_SENDER),                             # his terminal, his editor
    ("galaius-machine.service", {}, reg.PERSON_SENDER),                                 # the app's own service
    ("run-u3869.service", {}, None),                                                    # `systemd-run --user …`
    ("run-p41-i42.scope", {"GALAIUS_RUN_ID": ""}, None),                                # `systemd-run --scope …`
])
def test_a_process_acts_as_the_run_whose_scope_it_lives_in(monkeypatch, unit, environment, sender):
    monkeypatch.setattr(caller_module, "process_unit", lambda pid: unit)
    with Caller(environment=environment, cwd=".", pid=4242).bound() as asking:
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
