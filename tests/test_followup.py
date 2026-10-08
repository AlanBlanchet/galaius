"""A message to a run between its turns, started by the launcher that holds the run's next child
(`FollowUps`), and what a launcher that stopped left queued or in flight, recovered by the run's
dispatcher with no message lost or sent twice. Real child processes: the next turn's child reads
its message from its stdin, as Claude's stream-json input does."""

import asyncio
import os
import sys

import pytest

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents import run as run_module
from galaius.agents.followup import FollowUps
from galaius.agents.warm import WarmStart
from galaius.agents.providers import ClaudeCodeProvider
from tests.support.agents import ScriptedProvider, install_provider, use_policy

#: The next turn's child: waits for its message, says it read it (as `--replay-user-messages` does), answers it.
READER = (
    "import json, sys\n"
    "said = json.loads(sys.stdin.readline())['message']['content']\n"
    "print(json.dumps({'type':'user','isReplay':True,'message':{'role':'user','content':said}}), flush=True)\n"
    "print(json.dumps({'type':'assistant','session_id':'vendor','message':{'content':[{'type':'text','text':'got ' + said}]}}), flush=True)\n"
    "print(json.dumps({'type':'result','is_error':False,'session_id':'vendor'}), flush=True)\n"
)


class _Provider(ScriptedProvider):
    name = "followup-fake"
    can_resume = True
    starts_ahead = True

    def __init__(self) -> None:
        self.resumed: list[str] = []

    def resume_command(self, session_id, message, **kwargs):
        self.resumed.append(message)  # the dispatcher's own resume: the message on its command line, no echo
        script = READER.replace("json.loads(sys.stdin.readline())['message']['content']", repr(message))
        return [sys.executable, "-c", "\n".join(line for line in script.splitlines() if "isReplay" not in line)]

    def resume_ahead_command(self, session_id, **kwargs):
        return [sys.executable, "-c", READER]

    def task_message(self, task):
        return ClaudeCodeProvider().task_message(task)

    def took_message(self, line):
        return ClaudeCodeProvider().took_message(line)

    def valid_definition(self, agent):
        return True

    def permission_modes(self):
        return []


RUNS = ("r1", "r2", "r3", "r4")


@pytest.fixture
def provider(monkeypatch):
    provider = _Provider()
    install_provider(monkeypatch, provider)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    use_policy(monkeypatch, messaging, run_module, agents={"tester": "followup-model"}, providers={provider.name: True})
    dispatchers: list[str] = []
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda run_id, **_: dispatchers.append(run_id) or 1)
    provider.dispatchers = dispatchers
    for run_id in RUNS:  # between turns: the last turn's process is gone
        reg.register(run_id=run_id, pid=999999, provider=provider.name, name="worker", task="t", agent="tester", provider_session_id="vendor")
    return provider


async def _until(check, what: str) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(what)


def _replies(run_id: str = "r1") -> list[str]:
    return [event.text for event in reg.read_events(run_id) if event.kind == "text"]


@pytest.mark.asyncio
async def test_a_follow_up_goes_to_the_child_started_when_the_turn_ended(provider):
    followups = FollowUps(warm=WarmStart(delay=0, check_every=0.02, hold=60))
    try:
        await followups.ready("r1", environment=dict(os.environ))
        await _until(lambda: followups.warm.holding == ("r1",), "no child started for the next turn")
        held = followups.warm._held["r1"].process.pid

        delivery = await followups.send("r1", "hello", environment=dict(os.environ))

        assert delivery.state == "queued" and reg.get_run("r1").pid == held
        await _until(lambda: agent_queue.items("r1")[-1].state == "replied", "the turn was not settled")
        await _until(lambda: reg.get_run("r1").status == "done", "the run's turn did not end")
        assert _replies() == ["got hello"]
        assert provider.resumed == [] and provider.dispatchers == [], "no dispatcher, no second resume"
        await _until(lambda: followups.warm.holding == ("r1",), "no child started for the turn after")
    finally:
        followups.close()


@pytest.mark.asyncio
async def test_a_hand_over_that_fails_leaves_the_message_to_the_dispatcher_once(provider, monkeypatch):
    async def broken(self, process, text):
        raise ConnectionResetError("the child's pipe broke")

    monkeypatch.setattr(run_module.ResumeLaunch, "hand", broken)
    followups = FollowUps(warm=WarmStart(capacity=0))

    delivery = await followups.send("r1", "hello", environment=dict(os.environ))
    agent_queue.dispatch("r1")  # what the dispatcher it started runs

    assert delivery.state == "queued" and provider.dispatchers == ["r1"]
    assert [item.state for item in agent_queue.items("r1")] == ["replied"]
    assert provider.resumed == ["hello"] and _replies() == ["got hello"]


@pytest.mark.asyncio
async def test_a_runner_that_stopped_loses_no_message_and_sends_none_twice(provider):
    """A runner restart (every client upgrade) during a turn, between the claim and the hand-over,
    before the claim, and after a dispatcher's turn it had not settled yet: the next runner's
    `recover`, then each run's dispatcher."""
    followups, env = FollowUps(warm=WarmStart(capacity=0)), dict(os.environ)
    # r1, during a turn: the runner dies once the child has its message; the child ends on its own.
    in_flight = await followups.send("r1", "in flight", environment=env)
    for watcher in list(followups._tasks):
        watcher.cancel()
    await _until(lambda: not reg.get_run("r1").process_running(), "the turn's child did not end")
    # r2, claimed, never handed: the child sees its stdin close with the runner.
    claimed = messaging.queue_message("r2", "never read", sender="operator")
    launch = FollowUps._launch(reg.get_run("r2"), env)
    child = await followups._started(launch)
    assert FollowUps._claim(claimed, child.pid, launch) is not None
    child.stdin.close()
    await child.wait()
    # r3, queued, never claimed.
    queued = messaging.queue_message("r3", "queued", sender="operator")
    # r4, a dispatcher's turn (the message on the command line, no echo) answered; the dispatcher died before settling it.
    dispatched = messaging.queue_message("r4", "dispatched", sender="operator")
    with reg.record_lock("r4"):
        item = agent_queue.claim_next_locked("r4")
        turn = run_module.launch_continuation(provider, reg.get_run("r4"), "vendor", "dispatched", model=None, criterion=None, reasoning=None,
                                              raw_index=item.raw_index or 0, record_locked=True)
    await asyncio.to_thread(turn.wait)

    assert followups.recover([reg.get_run(run_id) for run_id in RUNS], environment=env) == RUNS
    for run_id in RUNS:
        agent_queue.dispatch(run_id)  # what each recovered dispatcher runs

    assert [(item.id, item.state) for run_id in RUNS for item in agent_queue.items(run_id)] == [
        (in_flight.queue_id, "replied"), (claimed.queue_id, "replied"), (queued.queue_id, "replied"), (dispatched.queue_id, "replied")]
    assert provider.resumed == ["dispatched", "never read", "queued"], "a message a child took is settled from its stream, never sent again"
    assert [_replies(run_id) for run_id in RUNS] == [["got in flight"], ["got never read"], ["got queued"], ["got dispatched"]]
