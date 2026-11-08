"""Runs past this computer's limit wait in line and start in order, never refused
(`galaius.agents.spawn_line`). 2026-10-13: three conversations ran 20+ runs at once and the agents'
memory cap killed builds, browsers and whole runs."""

import asyncio
import contextlib
import inspect
import json
import os
import subprocess
import sys
import time
import uuid

import pytest
from galaius_core import AgentRelation, AgentRevisionRef

from galaius.account.caller import Caller
from galaius.agents import registry as reg
from galaius.agents import spawn_line
from galaius.agents.catalog.community import CommunityAgent
from galaius.agents.messaging import Asker
from galaius.agents.policy import Policy
from galaius.agents.providers import ClaudeCodeProvider
from galaius.agents.run import ChildLaunch, TaskNotHanded, run_agent
from galaius.agents.spawn_line import ASIDE, STUCK, Admission, Load, Spawn, SpawnLine
from galaius.cli.app_commands import agents_require_role, agents_spawn
from galaius.host.processes import process_started
from galaius.machine.web_runs import WebRun, WebRuns
from galaius.host.paths import UserPaths
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
    line = SpawnLine(limit=2, waiting_flag_minutes=5, waiting_gap_seconds=60)
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
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
    at_work("lead")
    critic = line.enter(_spawn(parent="lead"))
    assert critic.ahead is None
    at_work(critic.run_id, parent_run_id="lead")  # its start registered it: its place is the run now
    line.leave(critic.run_id)
    second_child, grandchild = line.enter(_spawn(parent="lead")), line.enter(_spawn(parent=critic.run_id))
    assert (second_child.ahead, grandchild.ahead) == (0, 1)
    assert "2 agents au travail sur ce PC, 1 places prises sur 1" in grandchild.sentence()
    assert line.describe(grandchild.run_id).endswith(STUCK)  # the one place's team waits on its own grandchild


def test_runs_waiting_on_their_agents_give_their_place_back_until_they_wake(at_work, started):
    """2026-10-17: 6 places of 6 held, half by runs only waiting on their children; critic verdicts queued behind them."""
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60)
    for run_id in ("w1", "w2", "w3", "x1", "x2", "x3"):
        at_work(run_id)
    queued = line.enter(_spawn())
    assert queued.ahead == 0
    with contextlib.ExitStack() as waiting:
        for run_id in ("w1", "w2", "w3"):
            waiting.enter_context(line.aside(run_id))
        assert line.set_aside() == frozenset({"w1", "w2", "w3"})
        assert line._turn(queued.run_id) != "wait"  # the 4th worker starts at its waiter's next look
        with line._places() as places:
            assert (line.load(places).places, line.load(places).aside) == (4, 3)
        line.leave(queued.run_id)  # never started here: a real start makes it a run at work, and the waiters wait for it to end
    assert line.set_aside() == frozenset()
    assert Admission(run_id="r", ahead=0, load=Load(runs=7, places=6, limit=6, aside=3)).sentence() == \
        "en file : 0 avant elle (7 agents au travail sur ce PC, 6 places prises sur 6, 3 rendues par des runs qui attendent leurs agents)"


@pytest.mark.parametrize(("waits_on", "said"), [
    pytest.param(["83da9bab-0000-4000-8000-000000000000", "5c0ffee1-0000-4000-8000-000000000000", "ended"], "attend 83da9bab, 5c0ffee1 depuis 14 min", id="names-the-runs-still-to-end"),
    pytest.param([], "attend ses agents depuis 14 min", id="a-wait-naming-none"),
    pytest.param([f"{n}0000000-0000-4000-8000-000000000000" for n in range(1, 6)], "attend 10000000, 20000000, 30000000 +2 depuis 14 min",
                 id="a-wave-past-three-counted-so-the-cli-keeps-its-duration"),
])
def test_a_run_only_waiting_on_other_runs_past_the_flag_says_which_and_since_when(at_work, started, monkeypatch, waits_on, said):
    """2026-10-17 « still wasting time / inference / usage on stupid things »: a run whose only activity is waiting on other
    runs, in one wait or a loop of short ones, is flagged in the run list once past `waiting_flag_minutes`; before it its
    place given back, and once it writes again, nothing."""
    line, clock = SpawnLine(limit=2, waiting_flag_minutes=5, waiting_gap_seconds=60), time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    at_work("lead")
    register_run("ended", agent="code-reviewer", parent_run_id="lead")
    reg.finish("ended", exit_code=0, first=0)
    with line.aside("lead", waits_on=waits_on):
        assert line.waiting_said() == {"lead": ASIDE}
        clock += 7 * 60
    clock += 30  # its agent reads the wait's answer and waits again
    with line.aside("lead", waits_on=waits_on):
        clock += 7 * 60 - 30
        assert line.waiting_said() == {"lead": said}
        reg.raw_events_path("lead").write_text("{}\n")
        os.utime(reg.raw_events_path("lead"), (clock, clock))
        assert line.waiting_said() == {}


def test_a_waiters_first_child_takes_its_place_and_the_woken_waiter_counts_at_once(at_work, started):
    """Its first child takes the place it gave back ahead of the line; woken, it counts again before any spawn; one whose
    agent writes again (its wait in the background) counts."""
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
    at_work("lead")
    waiting = line.aside("lead")
    waiting.__enter__()
    critic = line.enter(_spawn(parent="lead"))
    assert critic.ahead is None
    busy = at_work(critic.run_id, parent_run_id="lead")
    line.leave(critic.run_id)
    fresh = line.enter(_spawn())
    assert fresh.ahead == 0
    waiting.__exit__(None, None, None)
    busy.kill()
    busy.wait()
    assert line._turn(fresh.run_id) == "wait"  # the lead counts again at once
    with line.aside("lead"):
        reg.raw_events_path("lead").write_text("{}\n")
        later = time.time() + 60
        os.utime(reg.raw_events_path("lead"), (later, later))
        assert line.set_aside() == frozenset()


def test_a_waiter_whose_child_works_frees_no_place_and_says_none(at_work, started):
    """Its child at work takes over the place it shared: none is free, none is said free; one with no child frees one,
    its first child takes a free place ahead of the line, never one past the limit."""
    line = SpawnLine(limit=4, waiting_flag_minutes=5, waiting_gap_seconds=60)
    for lead in ("w1", "w2", "w3"):
        at_work(lead)
        at_work(f"{lead}-child", parent_run_id=lead)
    at_work("x")
    with contextlib.ExitStack() as waiting:
        for run_id in ("w1", "w2", "w3", "x"):
            waiting.enter_context(line.aside(run_id))
        assert line.set_aside() == frozenset({"x"})
        fresh = line.enter(_spawn())
        assert fresh.ahead is None  # x's place
        second = line.enter(_spawn(parent="w1"))
        assert second.ahead == 0 and "4 places prises sur 4, 1 rendue par des runs qui attendent leurs agents)" in second.sentence()
        x_child = line.enter(_spawn(parent="x"))
        assert x_child.ahead == 1  # the place x gave back is taken: it waits
        line.leave(fresh.run_id)
        assert line._turn(x_child.run_id) != "wait" and line._turn(second.run_id) == "wait"


def test_a_hand_off_chain_waiting_holds_only_its_working_leafs_place(at_work, started):
    """A run handing off to a fresh run and waiting on it, three deep: the waiting ancestors hold no place."""
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60)
    at_work("r1")
    at_work("r2", parent_run_id="r1")
    at_work("r3", parent_run_id="r2")
    at_work("leaf", parent_run_id="r3")
    with line._places() as places:
        assert line.load(places).places == 3
    with contextlib.ExitStack() as waiting:
        for run_id in ("r1", "r2", "r3"):
            waiting.enter_context(line.aside(run_id))
        with line._places() as places:
            assert (line.load(places).places, line.load(places).aside) == (1, 2)


def test_the_owners_own_web_runs_are_not_counted(at_work, started):
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
    owners = str(uuid.uuid4())
    at_work(owners)
    UserPaths.config().mkdir(parents=True)
    WebRuns.in_folder(UserPaths.config()).add(WebRun(run_id=uuid.UUID(owners), root="/w"))
    assert line.enter(_spawn()).ahead is None  # counted, it would fill the one place


def test_a_resumed_turn_takes_its_turn_in_the_same_line_and_says_so(at_work, started):
    """A message to an ended run starts a process like a spawn: it waits its turn, and its sender is told."""
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
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
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
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
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
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


def test_a_new_run_started_for_a_run_lists_it_among_its_askers():
    """A job queue's wake starting a new worker (`spawn --for` the run that queued the job): that run is its first asker."""
    maker, started = str(uuid.uuid4()), str(uuid.uuid4())
    reg.register(run_id=maker, pid=999999, provider="claude", name="maker", task="t", agent="tester")

    async def runner(provider, task, *, run_id, **rest):
        reg.register(run_id=run_id, pid=999999, provider="claude", name="deployer", task=task, agent="infra-engineer",
                     asked_by=rest["asked_by"], inherited_asks=rest["inherited_asks"])

    asyncio.run(Spawn(task="ship it", cwd=".", agent="infra-engineer", for_run=maker).start(started, runner))
    assert reg.get_run(started).asked_by == (maker,)


class _Asked(Exception):
    """Raised by a stub `Spawn.asked`: the spawn stops once its arguments are resolved."""


@pytest.mark.parametrize(("given", "resolved"), [("full", True), ("short", True), ("unknown", False)])
def test_spawn_for_takes_the_short_id_agents_list_prints(monkeypatch, given, resolved):
    """A job queue's wake passes the asker as its operator typed it, often the 8-character id `agents list`
    prints: refusing it started the worker for nobody."""
    maker = str(uuid.uuid4())
    reg.register(run_id=maker, pid=999999, provider="claude", name="maker", task="t", agent="tester")
    asked = {}

    def stop(**arguments):
        asked.update(arguments)
        raise _Asked

    monkeypatch.setattr(Spawn, "asked", stop)
    named = {"full": maker, "short": maker[:8], "unknown": str(uuid.uuid4())[:8]}[given]
    with pytest.raises(_Asked if resolved else SystemExit):
        agents_spawn("ship it", agent="infra-engineer", for_run=named)
    assert asked.get("for_run") == (maker if resolved else None)


def test_every_kept_argument_of_a_spawn_is_one_run_agent_takes():
    """A waiter starts it long after it was asked: an argument `run_agent` renamed fails there, out of sight."""
    assert set(Spawn.model_fields) - Spawn.NOT_RUN_ARGUMENTS <= set(inspect.signature(run_agent).parameters)


def _ended(run_id: str, **rest) -> None:
    run = register_run(run_id=run_id, **rest)
    reg.finish(run_id, exit_code=0, first=0, expected_pid=run.pid, expected_lifecycle_token=run.lifecycle_token)


#: Another revision of the role's prompt (the spawn resolves revision 3 of agent 1).
OLDER = {"id": str(uuid.UUID(int=1)), "revision": str(uuid.UUID(int=2))}


@pytest.mark.parametrize("peers, asked, continued, why", [
    pytest.param([("eng1", {})], {}, "eng1", "", id="idle-same-role-continued"),
    pytest.param([("eng1", {"parent_run_id": "lead"}), ("eng2", {"parent_run_id": "mid"})], {}, "eng1", "", id="own-child-first"),
    # One run of the role at a time: one at work takes it behind its turn, never one started beside it.
    pytest.param([("eng1", {"busy": True})], {}, "eng1", "", id="busy-queued-behind-its-turn"),
    # 2026-10-16: the coordinator's shell sat in the workspace, its engineers in a checkout below: every spawn started anew.
    pytest.param([("eng1", {"cwd": "/elsewhere"})], {}, "eng1", "", id="other-folder-read-only-continued"),
    pytest.param([("eng1", {"cwd": "/elsewhere", "permission_mode": "full_access"})], {"permission_mode": "full_access"}, "eng1", "",
                 id="other-folder-full-access-continued"),
    pytest.param([("eng1", {"cwd": "/elsewhere", "permission_mode": "workspace_write"})], {"permission_mode": "workspace_write"}, None,
                 "eng1 works in another folder it may not write in", id="other-folder-may-not-write"),
    pytest.param([("eng1", {"permission_mode": "read_only"})], {}, "eng1", "", id="recorded-default-permission-continued"),
    pytest.param([("eng1", {"permission_mode": "danger_full_access"})], {}, None, "eng1 was given danger_full_access permissions",
                 id="wider-permissions-never-reused"),
    # 2026-10-17: 10 of 63 extra runs of one conversation were started beside an idle peer on the role's previous prompt revision.
    pytest.param([("eng1", {"agent_ref": OLDER})], {}, "eng1", "", id="older-revision-continued-on-the-new-one"),
    pytest.param([("eng1", {"agent_ref": {"id": str(uuid.UUID(int=9)), "revision": str(uuid.UUID(int=2))}})], {}, None,
                 "eng1 runs another agent of that name", id="other-agent-same-name"),
    pytest.param([("eng1", {})], {"model": "other-model", "name": "eng-3"}, "eng1", "", id="asked-model-and-name-ignored"),
    pytest.param([("eng1", {"refuses": True}), ("eng2", {"parent_run_id": "mid"})], {}, "eng2", "", id="refused-start-falls-through"),
    pytest.param([("eng1", {"refuses": True, "read": True}), ("eng2", {"parent_run_id": "mid"})], {}, "eng1", "",
                 id="uncertain-start-never-run-twice"),
    pytest.param([("eng1", {"replaced_by": {"run_id": "eng9-relaunch", "provider": "claude"}})], {}, None,
                 "eng1 was refused at launch, relaunched as eng9-rel", id="taken-over-launch-never-resumed"),
    pytest.param([("eng1", {"stream": [{"type": "turn.failed", "error": {"message": "not supported"}}]})], {}, None,
                 "eng1 never got a first turn", id="never-spoke-not-resumed"),
    pytest.param([("eng1", {"agent": "code-reviewer"})], {}, None, "", id="other-role"),
    # `per_project`: the project's run of the role, whatever conversation or session started it.
    pytest.param([("eng1", {"parent_run_id": "other-lead"}), ("other-lead", {"agent": "main", "parent_run_id": None})], {}, "eng1", "",
                 id="other-conversation-same-project-continued"),
    pytest.param([("eng1", {"session_id": "s2", "parent_run_id": None})], {"parent_run_id": None}, "eng1", "", id="no-parent-other-session-continued"),
    pytest.param([("eng1", {"session_id": "s2", "parent_run_id": None, "cwd": "/elsewhere"})], {}, None, "", id="other-project-not-a-peer"),
    # `one`: the PC's run of the role, whatever project it works for.
    pytest.param([("eng1", {"session_id": "s2", "parent_run_id": None, "cwd": "/elsewhere"})], {"instances": "one"}, "eng1", "",
                 id="one-crosses-projects"),
    # Owner 2026-11-03 « we should be able to call as many "ingenieur backend" and other agents as we want »: `many` never
    # hands a task to a run already there, idle or at work.
    pytest.param([("eng1", {})], {"instances": "many"}, None, "", id="many-starts-new-beside-an-idle-one"),
    pytest.param([("eng1", {"busy": True}), ("eng2", {})], {"instances": "many"}, None, "", id="many-starts-new-beside-a-busy-one"),
    pytest.param([("eng1", {"parent_run_id": None})], {"parent_run_id": None}, "eng1", "", id="no-parent-same-session-continued"),
    # The effort a person set on its conversation stays with that conversation: the task handed on runs at its role's.
    pytest.param([("eng1", {"reasoning_set": "max"})], {}, "eng1", "", id="reused-run-drops-the-effort-set-on-its-conversation"),
    pytest.param([("eng1", {})], {"task": "x" * 5000}, "eng1", "", id="long-brief-continued"),
    pytest.param([("eng1", {"context": (150_000, 233_000)})], {}, "eng1", "", id="context-at-reuse-ceiling-continued"),
    # 2026-10-26: runs at 500k-905k of a 1M window took new tasks and billed half a day's subscription in hours.
    pytest.param([("eng1", {"context": (905_000, 1_000_000)})], {}, None, "eng1 holds 905000 tokens of context, past the 150000 a new task may inherit",
                 id="context-past-reuse-ceiling-starts-fresh"),
    pytest.param([("eng1", {"context": (90_000, 200_000)})], {}, "eng1", "", id="context-with-room-continued"),
    # Owner 2026-10-17: « it should be 1 person for this project to be able to deploy, not 8 ».
    pytest.param([("eng1", {"busy": True}), ("eng2", {})], {"single": True}, "eng1", "", id="single-queues-the-one-at-work"),
    # One at a time: a busy one of another revision, agent or a full context still takes it behind its turn, never one beside it.
    pytest.param([("eng1", {"busy": True, "agent_ref": OLDER})], {"single": True}, "eng1", "", id="single-busy-other-revision-queued"),
    pytest.param([("eng1", {"busy": True, "context": (170_000, 233_000)})], {"single": True}, "eng1", "", id="single-busy-context-past-ceiling-queued"),
    pytest.param([("eng1", {"busy": True, "replaced_by": {"run_id": "eng9-relaunch", "provider": "claude"}})], {"single": True}, None,
                 "eng1 was refused at launch, relaunched as eng9-rel", id="single-busy-taken-over-starts-new"),
    pytest.param([("eng1", {})], {"instances": "many", "single": True}, "eng1", "", id="single-many-continued-in-its-conversation"),
    pytest.param([("eng1", {"parent_run_id": "other-lead"}), ("other-lead", {"agent": "main", "parent_run_id": None})],
                 {"instances": "many", "single": True}, None, "", id="single-many-other-conversation-not-a-peer"),
    # Owner 2026-11-08 « many to many »: a run of an `N-N` role (a critic shared by makers) takes another caller's task.
    pytest.param([("eng1", {})], {"instances": "many", "callers": "many"}, "eng1", "", id="n-n-continued-in-its-conversation"),
    pytest.param([("eng1", {})], {"instances": "many", "callers": "many", "fresh": True}, None, "", id="n-n-fresh-starts-new"),
])
def test_a_spawn_goes_where_its_roles_instances_setting_says(at_work, monkeypatch, tmp_path, peers, asked, continued, why):
    """A role keeping one run at a time (`per_project` here, unless the case names another) hands each task to that
    run; `many` starts one per task (owner 2026-11-03)."""
    instances = asked.pop("instances", "per_project")
    callers = {"callers": asked.pop("callers")} if "callers" in asked else {}
    monkeypatch.setattr(spawn_line, "load_policy",
                        lambda: Policy(agents={"backend-engineer": "m"}, providers={"claude": True}, defaults={"touch": "read_only"},
                                       relations={"backend-engineer": (AgentRelation(callees=instances, **callers),)}))
    current = AgentRevisionRef(id=uuid.UUID(int=1), revision=uuid.UUID(int=3))
    monkeypatch.setattr(spawn_line, "resolve_role", lambda policy, agent, **_: (policy, agent, current))
    refusing: set[str] = set()
    read: set[str] = set()
    sent: list[str] = []
    texts: list[str] = []
    revisions: list[AgentRevisionRef | None] = []
    _ended("lead", agent="main", session_id="s1", parent_run_id=None, cwd=str(tmp_path))
    _ended("mid", agent="tech-lead", session_id="s1", parent_run_id="lead", cwd=str(tmp_path))

    def deliver(run_id, task, *, only_if_idle, turn, for_run, across_conversations):
        assert only_if_idle != reg.get_run(run_id).working() and turn.refusal(task, None) is None
        assert across_conversations == (instances != "many")
        sent.append(run_id)
        texts.append(task)
        revisions.append(turn.agent_ref)
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
        replaced_by, stream, context = fields.pop("replaced_by", None), fields.pop("stream", None), fields.pop("context", None)
        if fields.pop("busy", False):
            at_work(run_id, **fields)
        else:
            _ended(run_id, **fields)
        if replaced_by is not None:
            reg._merge_record_locked(run_id, {"replaced_by": replaced_by})
        if context is not None:
            reg._merge_record_locked(run_id, dict(zip(("context_tokens", "context_window"), context)))
        if stream is not None:
            reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in stream))
    spawn = Spawn(task="add the endpoint", cwd=str(tmp_path), agent="backend-engineer", parent_run_id="lead", session_id="s1") \
        .model_copy(update=asked)
    handover = spawn.hand_over()
    assert (handover.run.run_id if isinstance(handover, spawn_line.Continued) else None) == continued
    # The turn handed over runs on the revision the spawn resolved (`MessageTurn.begin_locked`, as it starts).
    assert revisions[-1:] == ([current] if continued else [])
    assert sent[-1:] == ([continued] if continued else [])
    if continued:
        # A run of another folder is told the task's: its session still starts in its own.
        elsewhere = reg.get_run(continued).cwd != spawn.cwd
        assert texts[-1].endswith(spawn.task) and (str(tmp_path) in texts[-1].removesuffix(spawn.task)) == elsewhere
        assert reg.get_run(continued).reasoning_set is None
    if not continued:
        assert handover.why == (f"backend-engineer already here, not continued: {why}" if why else "")


def test_single_forgets_every_other_agent_of_its_role_not_at_work(at_work, monkeypatch, tmp_path):
    """`--single`: the conversation keeps one agent of the role; one at work, another role's or another conversation's stay."""
    monkeypatch.setattr(spawn_line, "load_policy", lambda: None)
    monkeypatch.setattr(spawn_line, "resolve_role", lambda policy, agent, **_: (policy, agent, None))
    for run_id, agent, session in [("old1", "deployer", "q"), ("old2", "deployer", "q"), ("kept", "deployer", "q"),
                                   ("rev", "code-reviewer", "q"), ("else", "deployer", "s2")]:
        _ended(run_id, agent=agent, session_id=session, parent_run_id=None, cwd=str(tmp_path))
    at_work("live", agent="deployer", session_id="q", parent_run_id=None, cwd=str(tmp_path))
    spawn = Spawn(task="ship", cwd=str(tmp_path), agent="deployer", session_id="q")
    assert spawn.forget_peers("kept") == []          # not single: nothing forgotten
    assert sorted(spawn.model_copy(update={"single": True}).forget_peers("kept")) == ["old1", "old2"]
    assert sorted(run.run_id for run in reg.list_runs()) == ["else", "kept", "live", "rev"]


@pytest.mark.parametrize("from_a_run", [True, False], ids=["an-agent-runs-process-refused", "the-persons-terminal-starts-a-root-run"])
def test_a_spawn_from_a_runs_process_never_leaves_its_tree(monkeypatch, capsys, from_a_run):
    """2026-10-17: 282 spawns in 68 transcripts cleared `GALAIUS_PARENT_RUN_ID` (`env -u`) or passed an empty
    --parent-run-id, starting runs whose reports reached no one."""

    monkeypatch.delenv("GALAIUS_PARENT_RUN_ID", raising=False)
    if from_a_run:  # this test's own parent process stands for the run's
        register_run("caller", pid=os.getppid())
        reg._merge_record_locked("caller", {"status": "running", "pid_started": process_started(os.getppid())})
        with pytest.raises(SystemExit) as refused:
            agents_spawn("review it", agent="code-reviewer", parent_run_id="")
        assert refused.value.code == 2 and "caller" in capsys.readouterr().err
    else:
        assert Spawn.asked(task="review it", cwd=".", agent="code-reviewer", parent_run_id="").parent_run_id is None


def test_the_run_that_takes_a_spawns_task_records_THAT_task(at_work, monkeypatch, tmp_path):
    """A spawn handed to the agent already holding its role in the conversation leaves that run's
    record saying what it was FIRST launched with — weeks ago, for a long-lived coordinator or
    librarian. Everything that reads a run's task then reads the wrong one: its row on the web
    (`machine_agents`), the card its own children start on (`relations`), and the brief a compaction
    hands its agent back as « the task your run was launched with » (`galaius.brief_hook`).
    2026-10-23: a spawn's task went to the librarian of the conversation and its recorded task was
    still a brief it had finished on 2026-10-06."""
    monkeypatch.setattr(spawn_line, "load_policy",
                        lambda: Policy(agents={"backend-engineer": "m"}, providers={"claude": True}, defaults={"touch": "read_only"},
                                       relations={"backend-engineer": (AgentRelation(callees="one"),)}))
    current = AgentRevisionRef(id=uuid.UUID(int=1), revision=uuid.UUID(int=3))
    monkeypatch.setattr(spawn_line, "resolve_role", lambda policy, agent, **_: (policy, agent, current))
    monkeypatch.setattr(spawn_line.messaging, "deliver_message",
                        lambda run_id, task, **_: spawn_line.messaging.Delivery(state="queued", text="Queued.", run_id=run_id, queue_id="q"))
    monkeypatch.setattr(spawn_line.messaging, "wait_for_start", lambda delivery: None)
    monkeypatch.setattr(spawn_line.messaging, "read_by_agent", lambda delivery: False)
    _ended("lead", agent="main", session_id="s1", parent_run_id=None)
    _ended("eng1", agent="backend-engineer", session_id="s1", parent_run_id="lead", cwd=str(tmp_path),
           model="m", agent_ref=current, permission_mode="read_only", task="the task of a fortnight ago")

    spawn = Spawn(task="add the endpoint", cwd=str(tmp_path), agent="backend-engineer", parent_run_id="lead", session_id="s1")
    handover = spawn.hand_over()

    assert isinstance(handover, spawn_line.Continued) and handover.run.run_id == "eng1"
    assert reg.get_run("eng1").task == "add the endpoint"
    assert handover.run.task == "add the endpoint"


def test_a_child_that_never_got_its_task_is_ended_instead_of_run(monkeypatch, tmp_path):
    """Handing a started-ahead child its task can fail on its stdin, and the failure was swallowed: the
    child then ran as if briefed — the role prompt and its card, no task — burning a whole run to answer
    « no task given ». Its launch fails instead, so its caller spawns again."""

    class _Deaf:
        """A child whose stdin is already gone."""

        def __init__(self) -> None:
            self.pid, self.returncode, self.killed = 4242, None, False
            self.stdin = self

        def write(self, _data: bytes) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    deaf = _Deaf()
    launch = ChildLaunch(provider=ClaudeCodeProvider(), cwd=str(tmp_path), environment={}, arguments={},
                         meshed=False, role="backend-engineer")
    monkeypatch.setattr(ChildLaunch, "_ahead", lambda self, run_id, session: _answer(deaf))

    with pytest.raises(TaskNotHanded):
        asyncio.run(launch.launch("r1", "r1", "do the thing"))
    assert deaf.killed


async def _answer(value):
    return value


#: (the role's relations, who asks — a run's role, "person", or None for an agent outside galaius's runs —, runs of the role
#: already there as (parent, at work), spawns of it already in line under `lead`, the run it takes over, the refusal's
#: words or None when it takes a place). Owner 2026-11-08 « the deployer is unique, that, uh, only, it can only be called
#: once. A bit like a database relationship … one to many, many to one, et cetera, many to many ».
RELATION_ROWS = [
    pytest.param(({"callees": "one"},), "backend-engineer", [("other-lead", True)], 0, None, "keeps one run on this PC", id="n-1-second-at-work-refused"),
    pytest.param(({"callees": "one"},), "backend-engineer", [], 1, None, "keeps one run on this PC", id="n-1-second-in-line-refused"),
    pytest.param(({"callees": "one"},), "person", [("other-lead", True)], 0, None, "keeps one run on this PC", id="n-1-the-person-too"),
    pytest.param(({"caller": "main", "callees": "one"},), "person", [("other-lead", True)], 0, None, "keeps one run on this PC", id="n-1-named-only-the-person-too"),
    pytest.param(({"callees": "one"},), "main", [("lead", False)], 0, None, None, id="n-1-none-at-work-starts"),
    pytest.param(({"callees": "one"},), "main", [("lead", True)], 0, "dep0", None, id="n-1-its-own-hand-off-starts"),
    pytest.param((), "main", [("lead", True), ("lead", True)], 0, None, None, id="1-n-third-starts"),
    pytest.param(({"caller": "main", "max_live": 2},), "main", [("lead", True)], 1, None, "may run 2 deployer at once", id="max-live-reached"),
    pytest.param(({"caller": "main", "max_live": 2},), "main", [("other-lead", True), ("lead", False)], 0, None, None, id="max-live-counts-its-callers-at-work"),
    pytest.param(({"caller": "main"},), "tester", [], 0, None, r"run lead \(tester\) may not call deployer: its callers are main", id="unnamed-caller-refused"),
    pytest.param(({"caller": "main"},), None, [], 0, None, "an agent outside galaius's runs may not call deployer", id="unnamed-anonymous-refused"),
    pytest.param(({"caller": "main"},), "person", [], 0, None, None, id="unnamed-person-starts"),
]


def _asker(caller: str | None) -> Asker:
    return Asker(kind="person") if caller == "person" else Asker(kind="anonymous") if caller is None else Asker(kind="run", run_id="lead", role=caller)


def _deployer_policy(monkeypatch, written) -> None:
    relations = {"deployer": tuple(AgentRelation(**relation) for relation in written)} if written else {}
    monkeypatch.setattr(spawn_line, "load_policy", lambda: Policy(agents={"deployer": "m"}, providers={"claude": True}, relations=relations))
    monkeypatch.setattr(spawn_line, "resolve_role", lambda policy, agent, **_: (policy, agent, AgentRevisionRef(id=uuid.UUID(int=1), revision=uuid.UUID(int=3))))


@pytest.mark.parametrize("written, caller, there, lined, continues, refused", RELATION_ROWS)
def test_a_spawn_takes_a_place_only_as_its_roles_relation_allows(at_work, started, monkeypatch, tmp_path, written, caller, there, lined, continues, refused):
    """Who may call a role and how many of its runs answer (`AgentRelation`), checked where a spawn takes its place
    (`SpawnLine.enter`, under the line's lock): a refused spawn takes none and says why in one line."""
    _deployer_policy(monkeypatch, written)
    parent = "lead" if caller not in (None, "person") else None
    if parent is not None:
        _ended("lead", agent=caller, session_id="s1", parent_run_id=None, cwd=str(tmp_path))
    for index, (above, working) in enumerate(there):
        fields = {"agent": "deployer", "session_id": "s1", "parent_run_id": above, "cwd": str(tmp_path)}
        (at_work if working else _ended)(f"dep{index}", **fields)
    spawn = Spawn(task="ship it", cwd=str(tmp_path), agent="deployer", parent_run_id=parent, session_id="s1", continues=continues, asker=_asker(caller))
    line = SpawnLine(limit=8, waiting_flag_minutes=5, waiting_gap_seconds=60)
    for _ in range(lined):
        line.enter(Spawn(task="ship that", cwd=str(tmp_path), agent="deployer", parent_run_id="lead", session_id="s1", asker=_asker("main")))
    if refused is None:
        assert line.enter(spawn).ahead is None   # a place taken, starting now
        return
    with pytest.raises(spawn_line.RelationRefused, match=refused):
        line.enter(spawn)


def test_a_waiting_spawn_of_a_role_keeping_one_run_is_refused_as_it_starts_once_one_works(at_work, started, monkeypatch, tmp_path):
    """code-reviewer r1 M1: the deployer idle, a `fresh` spawn of it waits in line; the idle one takes a task meanwhile.
    Asked again as the waiting place starts (`SpawnLine._turn`), the relation fails that place: never two at work."""
    _deployer_policy(monkeypatch, ({"callees": "one"},))
    _ended("dep0", agent="deployer", session_id="s1", parent_run_id=None, cwd=str(tmp_path))
    at_work("busy")
    line = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
    waiting = line.enter(Spawn(task="ship it", cwd=str(tmp_path), agent="deployer", session_id="s1", fresh=True, asker=_asker("person")))
    assert waiting.ahead == 0
    at_work("dep1", agent="deployer", session_id="s1", parent_run_id=None, cwd=str(tmp_path))   # the deployer at work again
    monkeypatch.setattr(SpawnLine, "_admits", lambda self, places, parent, first: True)
    assert line._turn(waiting.run_id) == "gone"
    assert "keeps one run on this PC" in line.describe(waiting.run_id[:8])


#: (the run whose scope the process lives in — its role, "community" for a community entry's run —, an agent CLI among its
#: ancestors, its environment, a systemd-run unit, allowed). code-reviewer r1 B1: no variable makes a process the deployer,
#: nor an agent the person.
REQUIRE_ROWS = [
    pytest.param("deployer", False, {}, False, True, id="the-deployer"),
    pytest.param("backend-engineer", False, {}, False, False, id="another-role"),
    pytest.param("community", False, {}, False, False, id="a-community-run-as-deployer"),
    pytest.param(None, False, {"GALAIUS_RUN_ID": "dep", "GALAIUS_PARENT_RUN_ID": "dep"}, False, True, id="the-person-naming-the-deployer-gains-nothing"),
    pytest.param(None, True, {"GALAIUS_RUN_ID": "dep"}, False, False, id="an-editor-agent-naming-the-deployer"),
    pytest.param(None, True, {}, False, False, id="an-editor-agent-with-its-conversation-cleared"),
    pytest.param(None, False, {Caller.THREAD[-1]: "conversation"}, False, False, id="an-agent-conversations-shell"),
    pytest.param(None, False, {}, True, False, id="a-systemd-run-unit"),
    pytest.param(None, False, {}, False, True, id="the-person"),
]


@pytest.mark.parametrize("scope, under_agent, environment, unowned, allowed", REQUIRE_ROWS)
def test_an_act_reserved_to_a_role_passes_its_runs_and_the_person_only(monkeypatch, capsys, tmp_path, scope, under_agent, environment, unowned, allowed):
    """`galaius agents require-role deployer`, run first by `deploy.sh` (owner 2026-11-08 « it's only the service for the agent
    that's supposed to deploy that's deploying »), reads who asks from the process (`Asker.here`): refused, it says who
    asks in one line and exits 3."""
    register_run(run_id="dep", agent="deployer")
    if scope is not None:
        register_run(run_id="asker", agent="deployer" if scope == "community" else scope,
                     **({"community": CommunityAgent(entry=uuid.UUID(int=7), kind="skill", title="Ship", author="someone/skills",
                                                     instructions="Ship.", digest="0" * 64, nonce="n0nce")} if scope == "community" else {}))
    monkeypatch.setattr(Caller, "scope_run", classmethod(lambda cls, pid: "asker" if scope is not None else None))
    monkeypatch.setattr(spawn_line.messaging.reg, "ancestor_run", lambda: None)
    monkeypatch.setattr(spawn_line.messaging, "process_ancestry", lambda pid: ((4242, 1),))
    monkeypatch.setattr(spawn_line.messaging, "process_program", lambda pid: "claude" if under_agent else "bash")
    monkeypatch.setattr(Caller, "unowned", lambda self: unowned)
    monkeypatch.setattr(Caller, "current", classmethod(lambda cls: Caller(environment=environment, cwd=tmp_path, pid=os.getpid())))
    if allowed:
        agents_require_role("deployer")
        return
    with pytest.raises(SystemExit) as refused:
        agents_require_role("deployer")
    assert refused.value.code == 3 and capsys.readouterr().err.startswith("REFUSED: only the deployer does this")
