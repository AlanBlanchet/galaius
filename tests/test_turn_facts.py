"""A coordinator's card states what is read at the turn his words start, never a remembered value
(`galaius.agents.turn_facts`; 2026-10-22: three answers in a row quoted a PC's state from memory)."""

import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from galaius_core import MachineSummary

from galaius.account_login import AccountLogin
from galaius.agents import turn_facts
from galaius.agents.turn_facts import Readers
from galaius.config.settings import TurnFacts
from galaius.machines import MachineRunner

HERE, OTHER = uuid.UUID(int=1), uuid.UUID(int=2)


def _slow() -> str:
    time.sleep(2)
    return "an answer that came too late"


def _refused() -> str:
    raise PermissionError("catalog access refused (HTTP 403)")


@pytest.mark.parametrize(("this_pc", "said"), [
    (lambda: "agent folders aino; agents may: workspace_write", "agent folders aino; agents may: workspace_write"),
    (_slow, "not read (no answer within 0.3 s)"),
    (_refused, "not read (catalog access refused (HTTP 403))"),
])
def test_each_fact_is_what_was_read_now_or_says_it_was_not(this_pc, said):
    card = turn_facts.lines(TurnFacts(seconds=0.3), read=Readers(build=lambda: "20261022T134400Z-e079498", pcs=lambda: "- desk (this PC): online",
                                                                 this_pc=this_pc, logins={"claude": lambda: "signed in"}))
    assert "app build 20261022T134400Z-e079498." in card and f": {said}; logins: claude signed in." in card and "too late" not in card


@pytest.mark.parametrize("answer", ["two-pcs", "refused"])
def test_his_pcs_are_read_from_the_server_each_line_or_why_not(monkeypatch, answer):
    monkeypatch.setattr(MachineRunner, "load", lambda self: SimpleNamespace(server_url="https://galaius.example", machine_id=HERE))

    def machines(self, machine):
        if answer == "refused":
            raise ValueError("catalog access refused (HTTP 403)")
        return (MachineSummary(id=HERE, name="desk", state="online", last_seen_at=datetime(2026, 10, 22, 15, 58, tzinfo=UTC)),
                MachineSummary(id=OTHER, name="lenovo", state="offline", last_seen_at=datetime(2026, 10, 22, 10, 31, tzinfo=UTC), file_roots=("aino",)))
    monkeypatch.setattr(AccountLogin, "machines", machines)
    card = turn_facts.lines(TurnFacts(), read=Readers(build=lambda: "b", pcs=turn_facts.pcs, this_pc=lambda: "p"))
    if answer == "refused":
        assert "His PCs, as the server sees them:\nnot read (catalog access refused (HTTP 403))" in card
    else:
        assert "- desk (this PC): online, last seen 2026-10-22 15:58 UTC" in card
        assert "- lenovo, logins not reported: offline, last seen 2026-10-22 10:31 UTC, work folders: aino" in card
