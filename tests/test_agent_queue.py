"""Durable continuation queue tests against real child processes."""

import asyncio
import json
import os
import subprocess
import sys

import uuid

import pytest

from galaius import inbox_hook
from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.policy import Policy
from galaius.agents.providers import ClaudeCodeProvider
from galaius.fence import FenceSpec
from tests.support.agents import ScriptedProvider, install_provider, use_policy


class _Provider(ScriptedProvider):
    name = "queue-fake"
    can_resume = True

    def __init__(self, script: str | None = None):
        self.script = script or (
            "import json; "
            "print(json.dumps({'type':'assistant','session_id':'vendor',"
            "'message':{'content':[{'type':'text','text':'reply'}]}})); "
            "print(json.dumps({'type':'result','is_error':False,'session_id':'vendor'}))"
        )
        self.messages: list[str] = []

    def command(self, *args, **kwargs):
        return [sys.executable, "-c", "pass"]

    def resume_command(self, session_id, message, **kwargs):
        self.messages.append(message)
        return [sys.executable, "-c", self.script]

    def valid_definition(self, agent):
        return True

    def permission_modes(self):
        return []


def _setup(monkeypatch, provider=None):
    provider = provider or _Provider()
    install_provider(monkeypatch, provider)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    use_policy(monkeypatch, messaging, agents={"tester": "queue-model"}, providers={provider.name: True})
    # Delivery tests run the same dispatcher entry point in-process. The child process itself is
    # still real: launch_continuation uses Popen and its reaper owns waitpid/finish.
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    reg.register(
        run_id="r1", pid=999999, provider=provider.name, name="worker", task="t",
        agent="tester", provider_session_id="vendor",
    )
    return provider


@pytest.mark.parametrize(("working", "ahead", "mid_turn", "fenced", "reads_at"), [
    (False, False, True, False, "now"),         # between turns: a turn starts on it
    (False, True, True, False, "next_step"),    # a turn starts on the message ahead, which reads it after a tool call
    (True, False, True, False, "next_step"),    # the turn running reads it after its current tool call
    (True, False, True, True, "turn_end"),      # fenced: the hook cannot claim it from inside the fence
    (True, False, False, False, "turn_end"),    # a CLI that reads nothing mid-turn
])
def test_a_queued_message_says_when_its_agent_reads_it(monkeypatch, tmp_path, working, ahead, mid_turn, fenced, reads_at):
    provider = _setup(monkeypatch)
    provider.reads_mid_turn = mid_turn
    fence = FenceSpec(working_directory=tmp_path, levels={"work": "write"}, start=tmp_path / "work", state=tmp_path / "state") if fenced else None
    reg.save_run(reg.get_run("r1").model_copy(update={"pid": os.getpid() if working else 999999, "fence": fence}))
    if ahead:
        messaging.queue_message("r1", "first", sender="operator")
    assert messaging.queue_message("r1", "second", sender="operator").reads_at == reads_at


def test_ordered_items_run_once_in_real_subprocesses(monkeypatch):
    provider = _setup(monkeypatch)
    first = messaging.deliver_message("r1", "one", sender="operator")
    second = messaging.deliver_message("r1", "two", sender="operator")

    agent_queue.dispatch("r1")

    assert first.state == second.state == "queued"
    assert [item.state for item in agent_queue.items("r1")] == ["replied", "replied"]
    assert provider.messages == ["one", "two"]
    # A turn that started (here: already replied) is no refusal for the sender to report.
    assert messaging.wait_for_start(first, timeout=1) is None


def test_stop_cancels_queued_work_before_any_resume(monkeypatch):
    provider = _setup(monkeypatch)
    messaging.deliver_message("r1", "do not start", sender="operator")

    assert reg.stop("r1") is True
    agent_queue.dispatch("r1")

    assert provider.messages == []
    assert [item.state for item in agent_queue.items("r1")] == ["cancelled"]


def test_policy_is_resolved_again_when_dispatch_starts(monkeypatch):
    provider = _setup(monkeypatch)
    policies = iter([
        Policy(agents={"tester": "queued-model"}, providers={provider.name: True}),
        Policy(agents={"tester": "started-model"}, providers={provider.name: True}),
    ])
    monkeypatch.setattr(messaging, "load_policy", lambda: next(policies))
    messaging.deliver_message("r1", "fresh policy", sender="operator")

    agent_queue.dispatch("r1")

    run = reg.get_run("r1")
    assert run is not None and run.model == "started-model"


def test_failed_resume_records_bounded_redacted_stderr(monkeypatch):
    provider = _Provider(
        "import sys; sys.stderr.write('api_key=test-secret-value\\n'); sys.exit(7)"
    )
    _setup(monkeypatch, provider)
    delivery = messaging.deliver_message("r1", "fail", sender="operator")

    agent_queue.dispatch("r1")
    item = agent_queue.items("r1")[0]

    assert delivery.state == "queued"
    assert item.state == "failed" and "7" in item.error
    assert "test-secret-value" not in reg.read_stderr("r1")
    # The RUN failed too, saying why — not only its queue item (the web reads the run).
    run = reg.get_run("r1")
    assert run is not None and run.status == "failed" and "exited 7" in run.last
    assert "test-secret-value" not in run.last
    assert reg.read_events("r1")[-1].kind == "error"
    assert "exited 7" in (messaging.wait_for_start(delivery, timeout=1) or "")


def test_a_resume_the_dispatcher_cannot_start_fails_the_run_and_the_sender_hears_why(monkeypatch):
    """A message to an ENDED run used to be accepted ("Queued", exit 0) while the detached dispatcher's
    policy / catalog / provider step failed in silence: the queue item alone read failed, the run stayed
    done, the web showed the message as delivered forever. Now the run itself fails with the reason and
    the sender, waiting a few seconds for the start, hears it."""
    _setup(monkeypatch)

    def refused(run):
        raise RuntimeError("catalog access refused (HTTP 401); cached access is disabled")

    monkeypatch.setattr(agent_queue, "_fresh_policy", refused)
    delivery = messaging.deliver_message("r1", "wake up", sender="operator")
    assert delivery.state == "queued"

    agent_queue.dispatch("r1")

    run = reg.get_run("r1")
    assert run is not None and run.status == "failed" and run.exit_code == 1
    assert run.last == "resume refused: catalog access refused (HTTP 401); cached access is disabled"
    assert reg.read_events("r1")[-1].text == run.last
    assert agent_queue.items("r1")[0].state == "failed"
    assert "catalog access refused" in (messaging.wait_for_start(delivery, timeout=1) or "")
    # The failure is the run's own now: a later list read (`_derive`) keeps it failed.
    assert next(item for item in reg.list_runs() if item.run_id == "r1").status == "failed"


def test_separate_process_enqueue_calls_are_serialized(monkeypatch, tmp_path):
    _setup(monkeypatch)
    script = (
        "import sys; "
        "from galaius.agents import agent_queue; "
        "item=agent_queue.enqueue('r1', message_id=sys.argv[1], sender='operator'); "
        "raise SystemExit(0 if item else 1)"
    )
    children = [
        subprocess.Popen([
            sys.executable, "-c", script, message_id,
        ], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
        for message_id in ("message-a", "message-b")
    ]
    assert [child.wait(timeout=10) for child in children] == [0, 0]
    assert {item.message_id for item in agent_queue.items("r1")} == {"message-a", "message-b"}


def test_killed_provider_turn_becomes_uncertain_without_automatic_replay(monkeypatch):
    provider = _setup(monkeypatch)
    first_id = reg.record_message_event(from_run="operator", to_run="r1", text="send once")
    second_id = reg.record_message_event(from_run="operator", to_run="r1", text="send twice")
    assert first_id and second_id
    assert agent_queue.enqueue("r1", message_id=first_id, sender="operator")
    assert agent_queue.enqueue("r1", message_id=second_id, sender="operator")
    with reg.record_lock("r1"):
        item = agent_queue.claim_next_locked("r1")
    assert item is not None and item.raw_index is not None and item.attempt_token

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert reg.begin_turn("r1", pid=child.pid) is not None
        child.kill()
        assert child.wait(timeout=10) is not None
        agent_queue.dispatch("r1")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    item = agent_queue.items("r1")[0]
    assert item.state == "uncertain"
    assert "resend explicitly" in item.error
    assert agent_queue.items("r1")[1].state == "replied"
    assert provider.messages == ["send twice"]


def test_recovery_accepts_only_a_completed_raw_turn_after_its_anchor(monkeypatch):
    provider = _setup(monkeypatch)
    message_id = reg.record_message_event(
        from_run="operator", to_run="r1", text="already completed",
    )
    assert message_id
    assert agent_queue.enqueue("r1", message_id=message_id, sender="operator")
    with reg.record_lock("r1"):
        item = agent_queue.claim_next_locked("r1")
    assert item is not None and item.raw_index == 0
    reg.raw_events_path("r1").write_text("\n".join([
        json.dumps({
            "type": "assistant", "session_id": "vendor",
            "message": {"content": [{"type": "text", "text": "reply"}]},
        }),
        json.dumps({"type": "result", "is_error": False, "session_id": "vendor"}),
    ]) + "\n")

    agent_queue.dispatch("r1")

    assert agent_queue.items("r1")[0].state == "replied"
    assert provider.messages == []


def test_crash_after_queue_intent_leaves_recoverable_bounded_failure(monkeypatch):
    provider = _setup(monkeypatch)
    script = """
import os
from galaius.agents import messaging, registry
from galaius.agents.policy import Policy
from tests.support.agents import ScriptedProvider

class Provider(ScriptedProvider):
    name = "queue-fake"
    can_resume = True

    def valid_definition(self, agent):
        return True

messaging.provider_for = lambda _: Provider()
messaging.load_policy = lambda: Policy(
    agents={"tester": "queue-model"}, providers={"queue-fake": True},
)
registry.record_message_event_locked = lambda **kwargs: os._exit(17)
messaging.deliver_message("r1", "crashed intent", sender="operator")
os._exit(19)
"""
    child = subprocess.Popen([
        sys.executable, "-c", script,
    ], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert child.wait(timeout=10) == 17

    assert agent_queue.items("r1")[0].state == "pending"
    agent_queue.dispatch("r1")

    item = agent_queue.items("r1")[0]
    assert item.state == "failed" and "message transcript entry is missing" in item.error
    assert provider.messages == []


def test_wait_for_reply_recovers_a_crashed_dispatcher_with_bounded_error(monkeypatch):
    _setup(monkeypatch)
    delivery = messaging.deliver_message("r1", "wait for me", sender="operator")
    crasher = subprocess.Popen([sys.executable, "-c", "import os; os._exit(17)"])
    assert crasher.wait(timeout=10) == 17
    state = agent_queue._state("r1")
    state.update(dispatcher_pid=crasher.pid, dispatcher_token="crashed-token")
    agent_queue._write_state("r1", state)
    monkeypatch.setattr(
        agent_queue, "ensure_dispatcher",
        lambda run_id: (_ for _ in ()).throw(OSError("dispatcher crashed")),
    )

    reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "error" and "dispatcher recovery failed" in reply
    assert agent_queue.items("r1")[0].state == "failed"


def test_wait_for_item_does_not_timeout_a_healthy_slow_dispatcher(monkeypatch):
    _setup(monkeypatch)
    delivery = messaging.deliver_message("r1", "slow provider", sender="operator")
    state = agent_queue._state("r1")
    state.update(dispatcher_pid=123, dispatcher_token="healthy-token")
    agent_queue._write_state("r1", state)
    monkeypatch.setattr(agent_queue, "_dispatcher_matches", lambda *args: True)
    monkeypatch.setattr(agent_queue, "_DISPATCHER_RECOVERY_TIMEOUT", 0.01)

    async def finish_after_timeout_boundary():
        await asyncio.sleep(0.05)
        agent_queue.mark("r1", delivery.queue_id, "replied")

    async def wait_and_finish():
        waiter = asyncio.create_task(
            agent_queue.wait_for_item("r1", delivery.queue_id)
        )
        await finish_after_timeout_boundary()
        return await waiter

    item = asyncio.run(wait_and_finish())

    assert item is not None and item.state == "replied"


def test_full_queue_refuses_before_recording_transcript_message(monkeypatch):
    monkeypatch.setattr(agent_queue, "MAX_PENDING", 1)
    _setup(monkeypatch)
    first = messaging.deliver_message("r1", "first", sender="operator")
    before = reg.messages_path("r1").read_bytes()

    second = messaging.deliver_message("r1", "second", sender="operator")

    assert first.state == "queued"
    assert second.state == "error" and "queue is full" in second.text
    assert reg.messages_path("r1").read_bytes() == before


def test_corrupt_queue_bytes_are_reported_and_preserved(monkeypatch):
    _setup(monkeypatch)
    queue_path = agent_queue.path("r1")
    queue_path.write_bytes(b"{not-json")

    with pytest.raises(agent_queue.CorruptQueueStateError, match="corrupt"):
        agent_queue.items("r1")

    assert queue_path.read_bytes() == b"{not-json"


def test_reused_dispatcher_pid_does_not_stall_on_unrelated_process(monkeypatch):
    reg.register(
        run_id="r1", pid=None, provider="claude", name="worker", task="t",
        agent="tester", provider_session_id="vendor",
    )
    unrelated = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(30)", "unrelated-token",
    ])
    try:
        agent_queue._write_state("r1", {
            "version": 1, "items": [], "dispatcher_pid": unrelated.pid,
            "dispatcher_token": "queue-token",
        })
        started = {}

        class _Started:
            pid = 424242

        def fake_popen(argv, **kwargs):
            started["argv"] = argv
            return _Started()

        monkeypatch.setattr(agent_queue.subprocess, "Popen", fake_popen)
        assert agent_queue.ensure_dispatcher_locked("r1") == 424242
        assert started["argv"][-1] != "queue-token"
        assert agent_queue._state("r1")["dispatcher_pid"] == 424242
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
            unrelated.wait(timeout=10)


def test_a_dispatcher_is_trusted_only_while_the_recorded_process_lives():
    """Its pid + start time name one process on Linux and Windows alike: a live dispatcher is never
    doubled, and a pid reused by a later process (another start time) or a dead one is not it."""
    from galaius.processes import process_started

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = process_started(child.pid)
        assert started is not None
        recorded = {"dispatcher_pid": child.pid, "dispatcher_token": "queue-token", "dispatcher_started": started}
        assert agent_queue._dispatcher_matches(recorded)
        assert not agent_queue._dispatcher_matches({**recorded, "dispatcher_started": started + 1})
        assert not agent_queue._dispatcher_matches({**recorded, "dispatcher_token": None})
    finally:
        child.kill()
        child.wait(timeout=10)
    assert not agent_queue._dispatcher_matches(recorded)


def test_a_message_to_a_working_run_is_read_after_its_next_own_tool_call_and_settled_by_that_turn(monkeypatch, tmp_path):
    """As in the editor: typed while the agent works, read after its next tool call (never a
    sub-agent's), never replayed as a turn of its own; the turn's reply settles it."""
    provider = _setup(monkeypatch)
    monkeypatch.setattr(agent_queue, "active_interpreter", lambda: sys.executable)
    run = reg.register(run_id="r2", pid=os.getpid(), provider=provider.name, name="worker", task="t", agent="tester", provider_session_id="vendor")
    # The hook runs in the agent's folder: a cloned repository's own json.py must never run.
    (tmp_path / "json.py").write_text(f"open({str(tmp_path / 'shadowed')!r}, 'w')\n")
    hook = agent_queue.inbox_hook("r2")
    idle = subprocess.run(hook, shell=True, input="{}", capture_output=True, text=True, check=True)
    assert idle.stdout == "" and not agent_queue.path("r2").exists()  # no queue yet: the shell answers alone
    sent = str(uuid.uuid4())
    messaging.deliver_message("r2", "also check the footer", sender="operator", message_id=sent)

    def tool_call(**event):
        return subprocess.run(hook, shell=True, input=json.dumps({"hook_event_name": "PostToolUse", **event}), capture_output=True, text=True, check=True, cwd=tmp_path).stdout

    assert tool_call(agent_id="sub-agent") == "" and [item.state for item in agent_queue.items("r2")] == ["pending"]
    said = json.loads(tool_call())["hookSpecificOutput"]
    # Opened by who wrote it, in the exact line the agent was told at launch is the person's own.
    heading = inbox_hook.HEADING.format(who=inbox_hook.OPERATOR)
    assert said["hookEventName"] == "PostToolUse" and said["additionalContext"] == f"{heading}\n\nalso check the footer"
    assert f"\n{heading}\n" in ClaudeCodeProvider.inbox_arguments(hook)[-1] == inbox_hook.NOTE
    assert tool_call() == "" and [item.state for item in agent_queue.items("r2")] == ["running"] and agent_queue.read_ids("r2") == [sent]

    with reg.open_raw_events("r2", append=True) as stream:
        stream.write(json.dumps({"type": "assistant", "session_id": "vendor", "message": {"content": [{"type": "text", "text": "footer checked"}]}}).encode() + b"\n")
        stream.write(json.dumps({"type": "result", "is_error": False, "session_id": "vendor"}).encode() + b"\n")
    reg.finish("r2", exit_code=0, expected_pid=run.pid, expected_lifecycle_token=run.lifecycle_token)
    agent_queue.dispatch("r2")

    assert [item.state for item in agent_queue.items("r2")] == ["replied"] and provider.messages == []
    assert not (tmp_path / "shadowed").exists()
