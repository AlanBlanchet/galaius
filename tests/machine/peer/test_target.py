"""The PC a run is placed on: it builds the run's own fence, refuses when its owner switched peer runs off, and runs unfenced only on the owner's press."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from galaius_core import AgentRefused, PlacedBy
from galaius.machine import agents as agents_module
from galaius.machine.agents import MachineAgents
from galaius.machine.peer.switch import PeerSwitch


def start(**more) -> SimpleNamespace:
    """What `_peer_fenced` reads of a start request (the signed envelope around it is the server's)."""
    return SimpleNamespace(**{"placed_by": None, "require_fence": False, **more})


def placed(unfenced_ok: bool = False) -> dict:
    return {"placed_by": PlacedBy(dispatch_id=uuid4(), machine_id=uuid4(), machine_name="pc-a", conversation_id=uuid4(), unfenced_ok=unfenced_ok)}


@pytest.fixture(autouse=True)
def config_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(PeerSwitch, "path", staticmethod(lambda: tmp_path / "peer-off"))


def fenced(monkeypatch: pytest.MonkeyPatch, ready: bool, request: SimpleNamespace) -> bool:
    monkeypatch.setattr(agents_module, "available", lambda: (ready, "" if ready else "no bubblewrap"))
    return MachineAgents._peer_fenced(None, request)


def test_a_web_start_is_not_a_peer_start_and_is_left_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fenced(monkeypatch, False, start()) is False and fenced(monkeypatch, True, start()) is False


def test_a_placed_run_gets_its_own_fence_where_one_can_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fenced(monkeypatch, True, start(**placed())) is True and fenced(monkeypatch, True, start(require_fence=True)) is True


def test_without_a_fence_a_placed_run_is_refused_unless_the_owner_allowed_it(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentRefused):
        fenced(monkeypatch, False, start(**placed()))
    with pytest.raises(AgentRefused):
        fenced(monkeypatch, False, start(require_fence=True))
    assert fenced(monkeypatch, False, start(**placed(unfenced_ok=True))) is False


def test_the_local_off_refuses_and_only_the_servers_word_turns_it_back_on(monkeypatch: pytest.MonkeyPatch) -> None:
    PeerSwitch.off()
    with pytest.raises(AgentRefused):
        fenced(monkeypatch, True, start(**placed()))
    PeerSwitch.on(said_by_server=False)
    assert PeerSwitch.is_off()
    PeerSwitch.on(said_by_server=True)
    assert fenced(monkeypatch, True, start(**placed())) is True
