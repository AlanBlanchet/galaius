"""A coordinator's card states what is read at the turn his words start, never a remembered value
(`galaius.agents.turns.facts`; 2026-10-22: three answers in a row quoted a PC's state from memory)."""

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from galaius_core import PcFleet, PcState

from galaius.machine.peer.client import PeerClient, PeerUnavailable
from galaius.agents.turns import facts as turn_facts
from galaius.agents.turns.facts import Readers
from galaius.config.settings import TurnFacts
from galaius.machine.connection import MachineRunner

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


@pytest.mark.parametrize(("answer", "name"), [("two-pcs", "desk"), ("two-pcs", "desk (this PC)"), ("down", "desk")])
def test_his_pcs_are_read_through_this_pcs_own_runner_each_line_or_why_not(monkeypatch, answer, name):
    """One credential: the card reads his PCs through the runner holding this PC's link, never asks him to sign in again."""
    def fleet(self):
        if answer == "down":
            raise PeerUnavailable("this PC's galaius is not running (start it: galaius machine service restart)")
        return PcFleet(pcs=(PcState(id=HERE, name=name, this=True, state="online", last_seen_at=datetime(2026, 10, 22, 15, 58, tzinfo=UTC), places_free=2, places_total=3),
                            PcState(id=OTHER, name="lenovo", state="offline", last_seen_at=datetime(2026, 10, 22, 10, 31, tzinfo=UTC))))
    monkeypatch.setattr(PeerClient, "fleet", fleet)
    card = turn_facts.lines(TurnFacts(), read=Readers(build=lambda: "b", pcs=turn_facts.pcs, this_pc=lambda: "p"))
    if answer == "down":
        assert "His PCs, as the server sees them:\nnot read (this PC's galaius is not running" in card and "sign in" not in card
    else:
        assert "- desk (this PC): online, last seen 2026-10-22 15:58 UTC, places free 2/3" in card  # the mark once, even when the server's name carries it
        assert "- lenovo: offline, last seen 2026-10-22 10:31 UTC" in card


@pytest.mark.parametrize(("projects", "roots", "said"), [
    ("/srv/pc/.galaius/projects", (), "projects folder /srv/pc/.galaius/projects (a launch makes each project's folder there); other agent folders none; agents may: workspace_write"),
    ("/srv/pc/.galaius/projects", ("dev",), "projects folder /srv/pc/.galaius/projects (a launch makes each project's folder there); other agent folders dev; agents may: workspace_write"),
    (None, (), "unusable (a link, a junction or a file is there); other agent folders none; agents may: workspace_write"),
])
def test_this_pc_names_where_agents_work_never_no_folder(monkeypatch, projects, roots, said):
    """2026-11-17: a run working on a PC was told « agent folders none »: the projects folder, where every launch
    makes its project's folder, is named first."""
    monkeypatch.setattr(MachineRunner, "load", lambda self: SimpleNamespace(run_agents=True, agent_roots=roots, agent_permission="workspace_write",
                                                                           projects_root=lambda: Path(projects) if projects else None))
    assert turn_facts.this_pc().endswith(said.replace("/srv/pc/.galaius/projects", str(Path("/srv/pc/.galaius/projects"))))   # as this system writes it
