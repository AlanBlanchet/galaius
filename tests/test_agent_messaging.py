"""Delivering a message to a running agent.

Two surfaces send messages — the `agent_send` MCP tool (an agent addressing a teammate) and the
CLI (`interact agents send`, which is how the VS Code panel lets the OPERATOR join in). They must
agree on every refusal, so the checks live here once rather than being written twice and drifting.
"""

import asyncio
import json
import sys
import threading

import pytest

from interact.agents import agent_queue, messaging
from interact.agents import registry as reg
from interact.agents.providers import PermissionMode
from tests.support import ScriptedProvider, install_provider, register_run, use_policy


def test_an_unknown_run_id_says_how_to_find_the_real_ones():
    error = messaging.check_deliverable("nope")[1]
    assert error and "nope" in error and "agent_list" in error


def test_it_refuses_to_type_into_one_of_the_users_own_editor_sessions(monkeypatch):
    """A foreign run is a session the user is driving — interact watches it, never types in it.
    It has no stored record: only the provider's own session listing knows it."""
    monkeypatch.setattr(reg, "list_runs", lambda **kw: [_Foreign()])
    error = messaging.check_deliverable("r1")[1]
    assert error and "must not type into it" in error


class _Foreign:
    run_id, name, provider, foreign, cwd = "r1", "my editor", "claude", True, "."


def test_a_provider_that_cannot_resume_is_refused_with_the_alternative(monkeypatch):
    """Without resume the message would arrive with no context, which is worse than refusing."""
    register_run("r1", name="reviewer", provider="codex", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")

    class _NoResume:
        can_resume = False

    monkeypatch.setattr(messaging, "provider_for", lambda name: _NoResume())
    error = messaging.check_deliverable("r1")[1]
    assert error and "agent_spawn" in error


def test_a_deliverable_run_returns_no_error(monkeypatch):
    register_run("r1", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")

    class _Ok:
        can_resume = True

    monkeypatch.setattr(messaging, "provider_for", lambda name: _Ok())
    run, error = messaging.check_deliverable("r1")
    assert error is None and run.run_id == "r1"


# ── Cross-session delivery: a run id is not a bearer token ─────────────────────────────────
# `run_agent` already refuses a spawn whose parent's owning conversation is unrecorded (#server
# revision guard); `agent_send` had no equivalent — any caller that already held a run id, however
# it got it, could resume ANY known run regardless of which conversation started either side.


def test_it_refuses_to_message_a_run_recorded_in_a_different_session(monkeypatch):
    register_run("sender", name="me", provider="claude", task="t", pid=1, session_id="conversation-a")
    register_run("r1", name="reviewer", provider="claude", task="t", pid=1234, agent="tester",
                provider_session_id="vendor-r1", session_id="conversation-b")

    class _Ok:
        can_resume = True

    monkeypatch.setattr(messaging, "provider_for", lambda name: _Ok())
    run, error = messaging.check_deliverable("r1", sender="sender")
    assert run is None and error and "different session" in error


def test_same_session_delivery_is_allowed(monkeypatch):
    register_run("sender", name="me", provider="claude", task="t", pid=1, session_id="conversation-a")
    register_run("r1", name="reviewer", provider="claude", task="t", pid=1234, agent="tester",
                provider_session_id="vendor-r1", session_id="conversation-a")

    class _Ok:
        can_resume = True

    monkeypatch.setattr(messaging, "provider_for", lambda name: _Ok())
    run, error = messaging.check_deliverable("r1", sender="sender")
    assert error is None and run.run_id == "r1"


def test_unknown_session_ownership_on_either_side_is_never_treated_as_a_mismatch(monkeypatch):
    """The operator (CLI/panel) has no recorded run of its own, and an old run predating session
    tracking has none either — unknown must never be read as a refusal, only a genuine conflict."""
    register_run("r1", name="reviewer", provider="claude", task="t", pid=1234, agent="tester",
                provider_session_id="vendor-r1", session_id="conversation-a")

    class _Ok:
        can_resume = True

    monkeypatch.setattr(messaging, "provider_for", lambda name: _Ok())
    run, error = messaging.check_deliverable("r1", sender="operator")
    assert error is None and run.run_id == "r1"


# ── Run ids: what the tool PRINTS must be what it ACCEPTS ───────────────────────────────────
# `interact agents list` shows 8-character ids, and every command that takes one demanded the full
# uuid — so copying an id straight off the tool's own output failed with "no agent run". The panel
# passes full ids, but a human reading the list cannot.


def test_a_unique_prefix_resolves_to_the_full_id():
    register_run("abcd1234-0000-0000-0000-000000000000", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    assert reg.resolve_run_id("abcd1234") == "abcd1234-0000-0000-0000-000000000000"


def test_an_exact_id_still_resolves_to_itself():
    register_run("abcd1234-0000-0000-0000-000000000000", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    full = "abcd1234-0000-0000-0000-000000000000"
    assert reg.resolve_run_id(full) == full


def test_an_ambiguous_prefix_resolves_to_nothing_rather_than_guessing():
    """Picking one at random could stop or message the WRONG agent — refuse instead."""
    register_run("abcd1111-0000-0000-0000-000000000000", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    register_run("abcd2222-0000-0000-0000-000000000000", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    assert reg.resolve_run_id("abcd") is None


def test_an_unknown_prefix_resolves_to_nothing():
    assert reg.resolve_run_id("zzzz") is None


def test_resolving_a_stored_prefix_never_derives_the_whole_history(monkeypatch):
    """Deriving every run re-parses every transcript: at 1.4 GB of history `agents send` sat for
    minutes before queuing a single message. A prefix naming a stored record is a filename lookup."""
    register_run("abcd1234-0000-0000-0000-000000000000", name="reviewer", provider="codex", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    monkeypatch.setattr(reg, "list_runs", lambda **_: pytest.fail("delivery derived every run"))
    monkeypatch.setattr(messaging, "provider_for", lambda name: type("_Ok", (), {"can_resume": True})())
    assert reg.resolve_run_id("abcd1234") == "abcd1234-0000-0000-0000-000000000000"
    run, error = messaging.check_deliverable("abcd1234")
    assert error is None and run.run_id == "abcd1234-0000-0000-0000-000000000000"


def test_delivery_accepts_the_id_the_list_printed(monkeypatch):
    register_run("abcd1234-0000-0000-0000-000000000000", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")

    class _Ok:
        can_resume = True

    monkeypatch.setattr(messaging, "provider_for", lambda name: _Ok())
    run, error = messaging.check_deliverable("abcd1234")
    assert error is None and run.run_id.startswith("abcd1234")


class _DeliveryProvider(ScriptedProvider):
    """The delivery side of the shared scripted provider: it records the queue/resume argv it was
    asked for instead of running one."""

    name = "fake"
    can_resume = True
    can_queue = True
    calls: list = []

    def validate_tool_policy(self, allowed_tools, denied_tools, *, coarse_accepted=False):
        assert not allowed_tools and not denied_tools

    def valid_definition(self, agent):
        return True

    def definition_path(self, agent):
        return None

    def discover(self):
        return []

    def permission_modes(self):
        return [PermissionMode("workspace-write", "write", "write")]

    def queue_command(self, session_id, message):
        self.calls.append(("queue", session_id, message))
        return ["fake", "queue", session_id, message]

    def resume_command(self, session_id, message, **kwargs):
        self.calls.append(("resume", session_id, message, kwargs))
        return ["fake", "resume", session_id, message]


class _ResumeProvider(_DeliveryProvider):
    name = "resume-fake"
    can_queue = False
    script = (
        "import json,sys,time\n"
        "print(json.dumps({'type':'assistant','session_id':'vendor-thread',"
        "'message':{'content':[{'type':'text','text':'resumed reply'}]}}), flush=True)\n"
        "print(json.dumps({'type':'result','is_error':False,'stop_reason':'end_turn',"
        "'session_id':'vendor-thread'}), flush=True)\n"
    )

    def resume_command(self, session_id, message, **kwargs):
        self.calls.append(("resume", session_id, message, kwargs))
        return [sys.executable, "-c", self.script]

def _continuation_policy(monkeypatch):
    provider = _DeliveryProvider()
    provider.calls = []
    monkeypatch.setattr(messaging, "provider_for", lambda name: provider)
    use_policy(monkeypatch, messaging, agents={"tester": "fresh-model"},
               reasoning={"tester": "high"}, providers={"fake": True})
    return provider


def _resume_policy(monkeypatch):
    use_policy(monkeypatch, messaging, agents={"tester": "fresh-model"},
               reasoning={"tester": "high"}, providers={"resume-fake": True})


def test_delivery_queues_against_vendor_session_not_interact_id(monkeypatch):
    from interact.agents import agent_queue
    provider = _continuation_policy(monkeypatch)
    register_run("interact-run", name="reviewer", provider="fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("interact-run", "ping", sender="operator")

    assert delivery.state == "queued" and delivery.queue_id
    assert provider.calls == []
    assert agent_queue.items("interact-run")[0].message_id


def test_peer_message_carries_registry_model_context_without_inventing_capability(monkeypatch):
    _continuation_policy(monkeypatch)
    register_run("recipient", name="reviewer", provider="fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    reg.register(
        run_id="sender", pid=None, provider="fake", name="source reviewer", agent="tester",
        model="fixture/reviewer", requested_criterion="aa.intelligence >= 35", reasoning="low",
    )
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("recipient", "Verified files are attached.", sender="sender")
    queued = agent_queue.items("recipient")[0]
    message = reg.message_for("recipient", queued.message_id)

    assert delivery.state == "queued"
    header = json.loads(message.text.splitlines()[1])
    assert header["model"] == "fixture/reviewer"
    assert header["reasoning"] == "low"
    assert header["requested_criterion"] == "aa.intelligence >= 35"
    assert header["benchmark_evidence"].startswith("not recorded")
    assert message.text.endswith("Verified files are attached.")


def test_stopped_delivery_resumes_with_fresh_policy_and_tracks_new_pid(monkeypatch):
    from interact.agents import agent_queue
    provider = _continuation_policy(monkeypatch)
    register_run("interact-run", name="reviewer", provider="fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [])

    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    delivery = messaging.deliver_message("interact-run", "continue", sender="operator")

    assert delivery.state == "queued" and delivery.queue_id
    assert provider.calls == []
    tracked = reg.get_run("interact-run")
    assert tracked is not None and tracked.pid == 1234


# ── A crashed or finished run resumes exactly like a stopped one ───────────────────────────
# `interact agents send` never checked "running" — the dispatcher only asks whether the recorded
# pid is ALIVE (`_active`). A machine restart kills the pid without ever calling `finish()`, so a
# genuinely crashed run's on-disk record is indistinguishable from `status="running"` until
# something re-derives it; a normally finished run's `status="done"` is exactly the same shape the
# dispatcher already re-activates. Both are exercised here through a REAL child process
# (`_ResumeProvider`), not a call-recording double, so the whole
# spawn → persist → classify-the-reply path runs, not just argv construction.


@pytest.mark.parametrize("recorded_status,exit_code", [("running", None), ("done", 0), ("crashed", None)])
def test_a_crashed_or_finished_run_resumes_with_full_context(monkeypatch, recorded_status, exit_code):
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=99999,
                agent="tester", provider_session_id="vendor-thread")
    # A dead pid the supervisor never reaped — the exact shape a machine restart leaves behind:
    # no `finish()` ever ran, so `exit_code` stays whatever it last was, and `status` is whatever
    # was last written, never derived fresh unless something calls `_derive`.
    reg._merge_record_locked("r1", {"status": recorded_status, "exit_code": exit_code})
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "keep going", sender="operator")
    agent_queue.dispatch("r1")
    reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "replied" and "resumed reply" in reply
    assert reg.get_run("r1").status == "done"


def test_a_vendor_session_that_no_longer_exists_fails_with_its_own_message_not_silently(monkeypatch):
    """The provider's own refusal (an expired/deleted vendor session, an unknown id) must reach
    the caller as a named failure — never swallowed, never reported as a successful reply."""
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    provider.script = "import sys; sys.stderr.write('Error: no conversation found for that id\\n'); sys.exit(1)"
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=99999,
                agent="tester", provider_session_id="vendor-thread-long-gone")
    reg._merge_record_locked("r1", {"status": "crashed", "exit_code": None})
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "keep going", sender="operator")
    agent_queue.dispatch("r1")
    reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "error" and "1" in reply
    assert reg.get_run("r1").status == "failed"


# ── A resumed turn re-resolves against the policy AND the cooldown memory ──────────────────
# "the resumed turn launched on that same dead model and died immediately with 'reached your
# Fable limit'" — a fresh spawn passes a quota-cooled candidate over BEFORE it ever runs
# (`run_agent`'s own `cooled` filter); resume replayed `run.model` untouched instead. Fixed by
# routing continuation through `resolve_continuable_model`, which walks the SAME ranked list
# scoped to the run's own provider (a vendor session cannot switch CLI mid-conversation) and
# skips anything `quota.blocked_until` still remembers.


class _FixtureCatalogProvider(_ResumeProvider):
    """Runs models from a throwaway `fixture` catalog instead of any real vendor's, so the test
    controls exactly which candidate ranks first without touching the real bundled catalog."""

    native_providers = frozenset({"fixture"})


def _cooldown_policy(monkeypatch):
    use_policy(monkeypatch, messaging, agents={"tester": "cap.llm"},
               reasoning={"tester": "high"}, providers={"resume-fake": True})


def test_a_resumed_turn_skips_a_cooled_model_and_uses_the_next_ranked_one(monkeypatch):
    from interact.agents import agent_queue, quota
    from interact.models import ModelCapability
    from tests.support import catalog_of, model as fixture_model

    provider = _FixtureCatalogProvider()
    provider.calls = []
    _cooldown_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=99999,
                agent="tester", provider_session_id="vendor-thread", model="fable")
    reg._merge_record_locked("r1", {"status": "crashed", "exit_code": None})
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    with catalog_of(
        fixture_model("fable", provider="fixture", caps={ModelCapability.LLM}, input_cost=0.1, output_cost=0.1),
        fixture_model("opus", provider="fixture", caps={ModelCapability.LLM}, input_cost=5.0, output_cost=5.0),
    ):
        # "fable" is the cheaper, normally-preferred candidate for "cap.llm" — exactly the
        # dead-model-gets-replayed shape from the bug report.
        quota.record_refusal("resume-fake", "fable", cooldown=3600)
        delivery = messaging.deliver_message("r1", "keep going", sender="operator")
        agent_queue.dispatch("r1")
        reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "replied" and "resumed reply" in reply
    resume_calls = [call for call in provider.calls if call[0] == "resume"]
    assert resume_calls[-1][3]["model"] == "opus"


def test_a_resumed_turn_refuses_when_every_candidate_for_its_provider_is_cooled(monkeypatch):
    """When the ONLY models this session's provider can run are all cooled, resume must REFUSE
    naming what the user can do — never spawn on a model that already refused for quota once
    (the exact bug), and never silently pick a different provider (a vendor session cannot
    switch CLI mid-conversation; that would abandon the conversation history)."""
    from interact.agents import agent_queue, quota
    from interact.models import ModelCapability
    from tests.support import catalog_of, model as fixture_model

    provider = _FixtureCatalogProvider()
    provider.calls = []
    _cooldown_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=99999,
                agent="tester", provider_session_id="vendor-thread", model="fable")
    reg._merge_record_locked("r1", {"status": "crashed", "exit_code": None})
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    with catalog_of(
        fixture_model("fable", provider="fixture", caps={ModelCapability.LLM}, input_cost=0.1, output_cost=0.1),
        fixture_model("opus", provider="fixture", caps={ModelCapability.LLM}, input_cost=5.0, output_cost=5.0),
    ):
        quota.record_refusal("resume-fake", "fable", cooldown=3600)
        quota.record_refusal("resume-fake", "opus", cooldown=3600)
        # Refused eagerly at delivery time — never even enqueued, so nothing here should reach
        # a "resend explicitly" or "uncertain" outcome; the caller learns the reason immediately.
        delivery = messaging.deliver_message("r1", "keep going", sender="operator")

    assert delivery.state == "error" and delivery.queue_id is None
    assert "cooldown" in delivery.text and "agent_spawn" in delivery.text
    assert not any(call[0] == "resume" for call in provider.calls)


def test_delivery_is_a_validated_model_not_a_dataclass():
    from dataclasses import is_dataclass
    from pydantic import BaseModel

    delivery = messaging.Delivery(state="queued", text="queued", run_id="r1")
    assert isinstance(delivery, BaseModel)
    assert not is_dataclass(delivery)


def test_concurrent_resumes_have_one_writer_and_one_busy_result(monkeypatch):
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    provider.script = "import time; time.sleep(1)\n" + provider.script
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    use_policy(monkeypatch, messaging, agents={"tester": "fresh-model"},
               reasoning={"tester": "high"}, providers={"resume-fake": True})
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: pid != 1234)

    results = []
    barrier = threading.Barrier(2)

    def send():
        barrier.wait()
        results.append(messaging.deliver_message("r1", "continue", sender="operator"))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result.state == "queued" for result in results)
    assert len(agent_queue.items("r1")) == 2


def test_wait_records_nonzero_exit_and_bounded_redacted_stderr(monkeypatch):
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    provider.script = "import sys; sys.stderr.write('api_key=test-secret-value\\n'); sys.exit(7)"
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "fail", sender="operator")
    agent_queue.dispatch("r1")
    reply = asyncio.run(messaging.wait_for_reply(delivery))
    assert delivery.state == "error" and "7" in reply
    assert "test-secret-value" not in reply
    assert "test-secret-value" not in reg.read_stderr("r1")
    assert reg.get_run("r1").status == "failed"


def test_wait_rejects_a_clean_process_that_emits_no_resume_event(monkeypatch):
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    provider.script = "pass"
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "silent", sender="operator")
    agent_queue.dispatch("r1")
    reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "error"
    assert "acceptance was not confirmed" in reply
    assert agent_queue.items("r1")[0].state == "uncertain"
    assert reg.get_run("r1").status == "done"


def test_wait_false_still_reaps_and_records_the_resumed_process(monkeypatch):
    from interact.agents import agent_queue
    provider = _ResumeProvider()
    provider.script = "import time; time.sleep(0.05)"
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "async", sender="operator")
    agent_queue.dispatch("r1")

    assert delivery.state == "queued"
    assert reg.get_run("r1").status == "done"


def test_queued_reply_lookup_uses_persisted_attempt_anchor(monkeypatch):
    from interact.agents import agent_queue

    provider = _ResumeProvider()
    _resume_policy(monkeypatch)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    install_provider(monkeypatch, provider)
    register_run("r1", name="reviewer", provider="resume-fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    monkeypatch.setattr(reg, "_alive", lambda pid: False)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    delivery = messaging.deliver_message("r1", "anchor", sender="operator")
    agent_queue.dispatch("r1")
    reply = asyncio.run(messaging.wait_for_reply(delivery))

    assert delivery.state == "replied" and "resumed reply" in reply
    assert "[Interact agent provenance]" in reply


def test_record_and_delivery_share_the_transcript_message_limit(monkeypatch):
    register_run("r1", name="reviewer", provider="claude", task="t", pid=1234, agent="tester", provider_session_id="vendor-r1")
    oversized = "x" * 2001

    delivery = messaging.deliver_message("r1", oversized, sender="operator")

    assert delivery.state == "error" and "transcript limit" in delivery.text


def test_queue_preserves_effective_policy_and_records_fresh_policy_separately(monkeypatch):
    from interact.agents import agent_queue
    provider = _DeliveryProvider()
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    use_policy(monkeypatch, messaging, agents={"tester": "fresh-model"},
               reasoning={"tester": "high"}, providers={"fake": True})
    register_run("r1", name="reviewer", provider="fake", task="t", pid=1234, agent="tester", provider_session_id="vendor-thread")
    stored = reg.get_run("r1")
    stored.model = "current-model"
    stored.requested_criterion = "current-criterion"
    stored.reasoning = "low"
    reg.save_run(stored)
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)

    result = messaging.deliver_message("r1", "queued", sender="operator")
    stored = reg.get_run("r1")

    assert result.state == "queued"
    assert stored.model == "current-model" and stored.reasoning == "low"
    assert stored.pending_model == "fresh-model" and stored.pending_reasoning == "high"


# ── A message says WHO, and which way ───────────────────────────────────────────────────────
# The row read "reviewer → 2b7642ee: ..." where 2b7642ee is reviewer's OWN id — an arrow pointing
# at itself. It looks exactly like the agent-to-agent messaging the owner asked to SEE, while
# actually showing nothing of the kind. A message needs a direction and a name.


def test_a_message_the_agent_RECEIVED_points_inward_and_names_the_sender():
    from interact.agents.events import AgentEvent

    event = AgentEvent(kind="message", from_run="operator", to_run="r1", text="check the tests")
    assert event.summary(viewer="r1") == "← operator: check the tests"


def test_a_message_the_agent_SENT_points_outward_and_names_the_recipient(tmp_path, monkeypatch):
    from interact.agents.events import AgentEvent

    reg.register(run_id="r2", name="perf", provider="claude", task="t", pid=None)
    event = AgentEvent(kind="message", from_run="r1", to_run="r2", text="numbers look fine")
    assert event.summary(viewer="r1") == "→ perf: numbers look fine"


def test_with_no_viewer_it_still_names_both_ends_rather_than_a_bare_hash(tmp_path, monkeypatch):
    from interact.agents.events import AgentEvent

    reg.register(run_id="r1", name="reviewer", provider="claude", task="t", pid=None)
    reg.register(run_id="r2", name="perf", provider="claude", task="t", pid=None)
    event = AgentEvent(kind="message", from_run="r1", to_run="r2", text="hi")
    assert event.summary() == "reviewer → perf: hi"


def test_an_unknown_id_falls_back_to_its_short_form():
    from interact.agents.events import AgentEvent

    event = AgentEvent(kind="message", from_run="operator", to_run="deadbeef-1111", text="hi")
    assert event.summary(viewer="deadbeef-1111") == "← operator: hi"


# ── The conversation shows BOTH sides, in order ─────────────────────────────────────────────
# Messages live in their own file and were APPENDED to the end of the event list, and the mirror
# the VS Code panel reads was written WITHOUT them at all. So the chat showed the agent talking to
# nobody: you could not see what you had asked, and an agent-to-agent exchange was invisible in
# the very view built to show it.


def test_a_message_is_placed_where_it_was_sent_not_at_the_end(tmp_path, monkeypatch):
    reg.register(run_id="r1", name="reviewer", provider="claude", task="t", pid=None)
    raw = reg.raw_events_path("r1")
    raw.parent.mkdir(parents=True, exist_ok=True)
    # One turn, then a message arrives, then the reply.
    raw.write_text(_assistant("first answer") + "\n")
    reg.record_message(from_run="operator", to_run="r1", text="now do the other thing")
    with raw.open("a") as f:
        f.write(_assistant("second answer") + "\n")

    kinds = [(e.kind, (e.text or "")[:20]) for e in reg.read_events("r1")]
    assert kinds == [
        ("text", "first answer"),
        ("message", "now do the other thi"),
        ("text", "second answer"),
    ]


def test_the_mirror_the_panel_reads_contains_the_messages(tmp_path, monkeypatch):
    reg.register(run_id="r1", name="reviewer", provider="claude", task="t", pid=None)
    reg.raw_events_path("r1").parent.mkdir(parents=True, exist_ok=True)
    reg.raw_events_path("r1").write_text(_assistant("hi") + "\n")
    reg.record_message(from_run="operator", to_run="r1", text="hello")
    reg.read_events("r1")  # writes the mirror

    mirrored = reg.events_path("r1").read_text()
    assert '"message"' in mirrored, "the panel reads this file; a missing message is invisible"


def _assistant(text):
    import json

    return json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "usage": {}},
    })


def test_a_message_with_no_anchor_goes_LAST_not_first(tmp_path, monkeypatch):
    """Messages recorded before the anchor existed carry none. We do not know where they belong,
    so they go at the end — `(index or 0)` silently read "unknown" as "the very beginning" and
    dropped every one of them above the run's own first turn."""
    reg.register(run_id="r1", name="reviewer", provider="claude", task="t", pid=None)
    raw = reg.raw_events_path("r1")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_assistant("a turn") + "\n")
    from interact.agents.events import AgentEvent

    reg.messages_path("r1").write_text(
        AgentEvent(kind="message", text="unanchored", from_run="operator",
                   to_run="r1").model_dump_json() + "\n"
    )
    assert [e.kind for e in reg.read_events("r1")] == ["text", "message"]


def test_the_mirror_updates_when_the_CONTENT_changes_not_only_its_length(tmp_path, monkeypatch):
    """The mirror was rewritten only when the event COUNT changed, so a fix to how events are
    ordered or rendered never reached the panel — it kept serving the old shape forever, and the
    only way to notice was that the UI disagreed with the CLI."""
    reg.register(run_id="r1", name="reviewer", provider="claude", task="t", pid=None)
    events_file = reg.events_path("r1")
    events_file.parent.mkdir(parents=True, exist_ok=True)
    from interact.agents.events import AgentEvent

    stale = [AgentEvent(kind="text", text="WRONG"), AgentEvent(kind="text", text="ORDER")]
    events_file.write_text("".join(e.model_dump_json() + "\n" for e in stale))

    fresh = [AgentEvent(kind="text", text="ORDER"), AgentEvent(kind="text", text="RIGHT")]
    reg._mirror_normalised("r1", fresh)  # same LENGTH, different content
    assert "RIGHT" in events_file.read_text()
