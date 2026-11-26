"""`agents spawn --machine <name|auto> "<task>"` and nothing else reaches his other PC: the project of the folder he stands in, the default role, a start still on
its way waited for, and a PC that takes nothing said with its reason."""
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from galaius_core import PcFleet, PcState, PeerProject, PeerProjects, PeerRefusal, PeerStarted
from galaius.machine.peer import words
from galaius.machine.peer.client import PeerError

PLACES, OTHER, MINE = uuid4(), PeerProject(id=uuid4(), name="Other"), PeerProject(id=uuid4(), name="My App")
LAPTOP = PcState(id=uuid4(), name="Laptop", state="online", dispatchable=True, places_free=2)


class Client:
    """The runner's answers: the fleet, the projects, and each start's outcome in turn."""

    def __init__(self, *outcomes, pc: PcState = LAPTOP) -> None:
        self.outcomes, self.pc, self.started = list(outcomes), pc, []

    def fleet(self) -> PcFleet:
        return PcFleet(pcs=(PcState(id=PLACES, name="this", this=True, state="online", dispatchable=True), self.pc))

    def projects(self) -> PeerProjects:
        return PeerProjects(projects=(OTHER, MINE))

    def start(self, target, project, role, task, **rest) -> PeerStarted:
        self.started.append((target, project, role, task))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise PeerError(PeerRefusal(code=outcome, message=f"Laptop: {outcome}", retry_after_s=10))
        return PeerStarted(dispatch_id=uuid4(), run_id=uuid4(), machine_id=target, machine_name=self.pc.name, conversation_id=uuid4(), permission="read_only")


def placing(client: Client, here: Path | None, **over) -> str:
    return words.place(client, "auto", over.pop("project", None), "run hostname", role=over.pop("role", None), path=None, permission=None, provider=None, model=None,
                       here=None if here is None else str(here), **over)


def repository(folder: Path) -> Path:
    (folder / ".git").mkdir(parents=True)
    return folder


@pytest.fixture(autouse=True)
def person(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A person's own terminal (no agent run), and no real wait."""
    monkeypatch.delenv("GALAIUS_RUN_ID", raising=False)
    waited: list[float] = []
    monkeypatch.setattr(words.time, "sleep", waited.append)
    return waited


def test_only_a_task_starts_the_coordinator_in_the_project_named_like_the_folders_repository(tmp_path: Path) -> None:
    here = repository(tmp_path / "my-app") / "tools" / "deep"
    here.mkdir(parents=True)
    client = Client()
    assert "Laptop" in placing(client, here)
    assert client.started == [(LAPTOP.id, MINE.id, "main", "run hostname")]


@pytest.mark.parametrize("address", ["git@github.com:someone/my-app.git", "https://github.com/someone/my-app"])
def test_a_folder_renamed_on_disk_finds_the_project_named_like_its_origin_repository(tmp_path: Path, address: str) -> None:
    folder = tmp_path / "checkout-2"
    folder.mkdir()
    for command in (("init", "-q"), ("remote", "add", "origin", address)):
        subprocess.run(["git", "-C", str(folder), *command], check=True)
    client = Client()
    placing(client, folder)
    assert client.started[0][1] == MINE.id


def test_a_named_project_and_role_win_over_the_defaults(tmp_path: Path) -> None:
    client = Client()
    placing(client, repository(tmp_path / "my-app"), project="other", role="tester")
    assert client.started[0][1:3] == (OTHER.id, "tester")


def test_a_folder_that_names_no_project_says_which_to_pass(tmp_path: Path) -> None:
    client = Client()
    with pytest.raises(ValueError, match=r"named like 'scratch': pass --project .*yours: My App, Other"):
        placing(client, repository(tmp_path / "scratch"))
    assert client.started == []


def test_a_named_pc_that_takes_no_run_says_its_reason_before_asking(tmp_path: Path) -> None:
    client = Client(pc=PcState(id=uuid4(), name="Laptop", state="online", dispatchable=False, why_not="« Takes runs » is off on its page"))
    with pytest.raises(ValueError, match="Laptop takes no run now: « Takes runs » is off"):
        words.place(client, "Laptop", "other", "t", role=None, path=None, permission=None, provider=None, model=None)
    assert client.started == []


def test_a_start_still_on_its_way_is_asked_again_until_its_run_id(tmp_path: Path, person: list[float], capsys: pytest.CaptureFixture[str]) -> None:
    client = Client("pc_slow", "in_progress")
    assert "Laptop" in placing(client, None, project="other")
    assert len(client.started) == 3 and person == [10, 10] and capsys.readouterr().err.count("Waiting for its run id") == 1


@pytest.mark.parametrize(("outcome", "patience"), [("needs_owner", 300.0), ("pc_slow", 0.0)])
def test_another_refusal_or_a_spent_patience_is_said_at_once(outcome: str, patience: float, person: list[float]) -> None:
    client = Client(outcome)
    with pytest.raises(PeerError, match=outcome):
        placing(client, None, project="other", patience=patience)
    assert len(client.started) == 1 and person == []
