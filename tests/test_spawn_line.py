"""Runs past this computer's limit wait in line and start in order, never refused
(`galaius.agents.spawn_line`). 2026-10-13: three conversations ran 20+ runs at once and the agents'
memory cap killed builds, browsers and whole runs."""

import inspect
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from galaius.agents import registry as reg
from galaius.agents import spawn_line
from galaius.agents.run import run_agent
from galaius.agents.spawn_line import STUCK, Spawn, SpawnLine
from galaius.machine_agents import WebRun, WebRuns
from galaius.paths import UserPaths
from tests.support import register_run


@pytest.fixture
def at_work():
    """Registers a run whose process really runs (`reg.running_runs` counts it); returns its process."""
    children: list[subprocess.Popen] = []

    def start(run_id: str, **rest) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        children.append(child)
        register_run(run_id=run_id, pid=child.pid, **rest)
        return child

    yield start
    for child in children:
        child.kill()
        child.wait()


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """The run ids the line starts (`run_agent` is not run); this test process holds every waiting place."""
    ids: list[str] = []

    async def run_agent(provider, task, *, run_id, **rest):
        ids.append(run_id)

    monkeypatch.setattr(spawn_line, "run_agent", run_agent)
    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: None)
    return ids


def _spawn(parent: str | None = None) -> Spawn:
    return Spawn(task="review it", cwd=".", agent="code-reviewer", parent_run_id=parent, session_id="s1")


def test_past_the_limit_a_spawn_waits_and_starts_in_order_under_its_id(at_work, started):
    line = SpawnLine(limit=2)
    at_work("a")
    busy = at_work("b")
    first, second = line.enter(_spawn()), line.enter(_spawn())
    assert (first.ahead, second.ahead) == (0, 1) and started == []
    assert second.sentence() == "en file : 1 avant elle (2 agents au travail sur ce PC, 2 places prises sur 2)"
    assert line.describe(second.run_id[:8]) == second.sentence()
    busy.kill()
    busy.wait()
    assert line._turn(second.run_id) == "wait"  # the earlier spawn goes first
    line.wait(first.run_id)
    line.wait(second.run_id)
    assert started == [first.run_id, second.run_id] and line.describe(first.run_id) is None


def test_a_run_shares_its_place_with_its_first_child_one_level_only(at_work, started):
    """A lead waiting for the critic it started never waits behind its own place; the critic's own child does."""
    line = SpawnLine(limit=1)
    at_work("lead")
    critic = line.enter(_spawn(parent="lead"))
    assert critic.ahead is None
    at_work(critic.run_id, parent_run_id="lead")  # its start registered it: its place is the run now
    line.leave(critic.run_id)
    second_child, grandchild = line.enter(_spawn(parent="lead")), line.enter(_spawn(parent=critic.run_id))
    assert (second_child.ahead, grandchild.ahead) == (0, 1)
    assert "2 agents au travail sur ce PC, 1 places prises sur 1" in grandchild.sentence()
    assert line.describe(grandchild.run_id).endswith(STUCK)  # the one place's team waits on its own grandchild


def test_the_owners_own_web_runs_are_not_counted(at_work, started):
    line = SpawnLine(limit=1)
    owners = str(uuid.uuid4())
    at_work(owners)
    UserPaths.config().mkdir(parents=True)
    WebRuns.in_folder(UserPaths.config()).add(WebRun(run_id=uuid.UUID(owners), root="/w"))
    assert line.enter(_spawn()).ahead is None  # counted, it would fill the one place


def test_a_resumed_turn_takes_its_turn_in_the_same_line_and_says_so(at_work, started):
    """A message to an ended run starts a process like a spawn: it waits its turn, and its sender is told."""
    line = SpawnLine(limit=1)
    at_work("lead")
    at_work("critic", parent_run_id="lead")
    ended = register_run(run_id="other", pid=None)
    assert line.waits("other") == "en file : 0 avant elle (2 agents au travail sur ce PC, 1 places prises sur 1)"
    with line.resuming(ended) as admitted:
        assert not admitted
    assert line.enter(_spawn()).ahead == 1  # behind the resumed turn that asked first
    assert line.waits("other").startswith("en file : 0 avant elle")
    lone_child = register_run(run_id="lone", pid=None, parent_run_id="solo")
    at_work("solo")
    with line.resuming(lone_child) as admitted:  # it shares its parent's place: never waits
        assert admitted


@pytest.mark.skipif(sys.platform != "linux", reason="reads the waiter's command line from /proc")
def test_the_waiter_is_its_own_process_and_ends_when_taken_out_of_the_line(at_work):
    """The real waiter: detached, holding the place, gone within one look once `agents stop` cancels it."""
    line = SpawnLine(limit=1)
    at_work("a")
    queued = line.enter(_spawn())
    with line._places() as places:
        holder = places[0].holder
    assert holder != os.getpid() and "galaius.agents.spawn_line" in " ".join(open(f"/proc/{holder}/cmdline").read().split("\0"))
    assert line.cancel(queued.run_id[:8]) == queued.run_id
    deadline = time.monotonic() + 3 * spawn_line.POLL_SECONDS
    while reg._alive(holder) and not spawn_line.process_exited(holder):
        assert time.monotonic() < deadline, "the waiter outlived its place"
        time.sleep(0.2)


def test_a_start_cut_off_or_an_unreadable_entry_reads_failed_and_never_refuses(at_work, started):
    line = SpawnLine(limit=1)
    cut = line.enter(_spawn())  # starts now: `starting`, held by this process
    with line._places() as places:
        places[0] = places[0].model_copy(update={"holder_started": -1})
    with reg.shared_state("spawn-line") as (raw, replace, _):
        replace(json.dumps([*json.loads(raw), {"run_id": "from-a-newer-galaius", "state": "parked"}]).encode())
    assert line.describe(cut.run_id).startswith("not started: its start was cut off")
    assert line.describe("from-a-newer").startswith("not started: its place in line is unreadable")
    with reg.shared_state("spawn-line") as (_, replace, _):
        replace(b"{not json")
    assert line.enter(_spawn()).ahead is None
    assert [path.name.startswith("#spawn-line.state.unreadable-") for path in reg.agents_dir().iterdir()].count(True) == 1


def test_every_kept_argument_of_a_spawn_is_one_run_agent_takes():
    """A waiter starts it long after it was asked: an argument `run_agent` renamed fails there, out of sight."""
    assert set(Spawn.model_fields) - {"task", "provider", "session_id"} <= set(inspect.signature(run_agent).parameters)


def _ended(run_id: str, **rest) -> None:
    run = register_run(run_id=run_id, **rest)
    reg.finish(run_id, exit_code=0, expected_pid=run.pid, expected_lifecycle_token=run.lifecycle_token)


@pytest.mark.parametrize("peers, asked, continued, why", [
    pytest.param([("eng1", {})], {}, "eng1", "", id="idle-same-role-continued"),
    pytest.param([("eng1", {"parent_run_id": "lead"}), ("eng2", {"parent_run_id": "other"})], {}, "eng1", "", id="own-child-first"),
    pytest.param([("eng1", {"busy": True})], {}, None, "eng1 is working", id="busy-starts-new"),
    pytest.param([("eng1", {"cwd": "/elsewhere"})], {}, None, "eng1 works in another folder", id="other-folder"),
    pytest.param([("eng1", {"permission_mode": "read_only"})], {}, "eng1", "", id="recorded-default-permission-continued"),
    pytest.param([("eng1", {"permission_mode": "danger_full_access"})], {}, None, "eng1 was given danger_full_access permissions",
                 id="wider-permissions-never-reused"),
    pytest.param([("eng1", {"agent_ref": {"id": str(uuid.UUID(int=1)), "revision": str(uuid.UUID(int=2))}})], {}, None,
                 "eng1 runs another revision of the role", id="other-revision"),
    pytest.param([("eng1", {})], {"model": "other-model", "name": "eng-3"}, "eng1", "", id="asked-model-and-name-ignored"),
    pytest.param([("eng1", {"refuses": True}), ("eng2", {"parent_run_id": "other"})], {}, "eng2", "", id="refused-start-falls-through"),
    pytest.param([("eng1", {"refuses": True, "read": True}), ("eng2", {"parent_run_id": "other"})], {}, "eng1", "",
                 id="uncertain-start-never-run-twice"),
    pytest.param([("eng1", {"agent": "code-reviewer"})], {}, None, "", id="other-role"),
    pytest.param([("eng1", {"session_id": "s2"})], {}, None, "", id="other-conversation"),
])
def test_a_spawn_continues_its_conversations_idle_agent_of_that_role(at_work, monkeypatch, tmp_path, peers, asked, continued, why):
    """Owner 2026-10-15: no « backend engineer three » while engineer one sits idle in the conversation."""
    from galaius_core import AgentRevisionRef

    from galaius.agents.policy import Policy

    monkeypatch.setattr(spawn_line, "load_policy",
                        lambda: Policy(agents={"backend-engineer": "m"}, providers={"claude": True}, defaults={"touch": "read_only"}))
    current = AgentRevisionRef(id=uuid.UUID(int=1), revision=uuid.UUID(int=3))
    monkeypatch.setattr(spawn_line, "resolve_role", lambda policy, agent, **_: (policy, agent, current))
    refusing: set[str] = set()
    read: set[str] = set()
    sent: list[str] = []

    def deliver(run_id, task, *, only_if_idle):
        assert only_if_idle
        sent.append(run_id)
        return spawn_line.messaging.Delivery(state="queued", text="Queued.", run_id=run_id, queue_id="q")

    monkeypatch.setattr(spawn_line.messaging, "deliver_message", deliver)
    monkeypatch.setattr(spawn_line.messaging, "wait_for_start", lambda delivery: "it died at once" if delivery.run_id in refusing else None)
    monkeypatch.setattr(spawn_line.messaging, "read_by_agent", lambda delivery: delivery.run_id in read)
    for run_id, rest in peers:
        fields = {"agent": "backend-engineer", "session_id": "s1", "parent_run_id": "lead", "cwd": str(tmp_path), "model": "m",
                  "agent_ref": current, "permission_mode": "read_only"} | rest
        fields["cwd"] = fields["cwd"] if fields["cwd"] == str(tmp_path) else str(tmp_path / "elsewhere")
        if fields.pop("refuses", False):
            refusing.add(run_id)
        if fields.pop("read", False):
            read.add(run_id)
        if fields.pop("busy", False):
            at_work(run_id, **fields)
        else:
            _ended(run_id, **fields)
    spawn = Spawn(task="add the endpoint", cwd=str(tmp_path), agent="backend-engineer", parent_run_id="lead", session_id="s1") \
        .model_copy(update=asked)
    handover = spawn.hand_over()
    assert (handover.run.run_id if isinstance(handover, spawn_line.Continued) else None) == continued
    assert sent[-1:] == ([continued] if continued else [])
    if not continued:
        assert handover.why == (f"backend-engineer already here, not continued: {why}" if why else "")
