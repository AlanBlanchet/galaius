"""`ask_owner`: a critical question reaches the owner from his conversation's coordinator only, never pauses the asker,
and closes on the one message of his that carries its answer id (owner 2026-11-02 « only ask for critical things »;
15:24 « a sub-agent shoudl'nt be allowed to ask question to the user, only the coordinator »)."""

import pytest
from galaius_core import OPEN_MAX, OwnerQuestion, QuestionAnswer

import galaius.server.tools.agents as tools_agents
from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from tests.support import ScriptedProvider, install_provider, register_run, use_policy

OPTIONS = [{"label": "Supprimer", "consequence": "la base preprod repart vide"},
           {"label": "Garder", "consequence": "rien ne change, la migration attend"}]


class _Resumable(ScriptedProvider):
    name = "fake"
    can_resume = True
    can_queue = True

    def validate_tool_policy(self, allowed_tools, denied_tools, *, coarse_accepted=False):
        pass


def _asking(monkeypatch, run_id: str | None) -> None:
    """The run the calling process belongs to, as its scope resolves it (never an environment variable it could clear)."""
    monkeypatch.setattr(messaging, "asking_run", lambda: run_id)


async def _ask(criticality: str = "destructive") -> str:
    return await tools_agents.ask_owner("Supprimer la base preprod ?", OPTIONS, 1, criticality, "irréversible")


@pytest.mark.asyncio
async def test_the_coordinator_posts_its_question_and_keeps_working(monkeypatch):
    register_run("main-1", name="main", provider_session_id="vendor-main")
    _asking(monkeypatch, "main-1")

    said = await _ask()

    asked = reg.get_run("main-1").asking
    assert said.startswith("Asked the owner") and "Do not wait" in said
    assert [question.question for question in asked] == ["Supprimer la base preprod ?"] and asked[0].recommended == 1
    assert reg.get_run("main-1").status == "running"  # nothing paused it


@pytest.mark.asyncio
@pytest.mark.parametrize(("criticality", "refusal"), [
    pytest.param("style", "Only these reasons reach the owner", id="not-critical"),
    pytest.param("", "Only these reasons reach the owner", id="no-reason"),
])
async def test_a_question_without_a_critical_reason_is_refused(monkeypatch, criticality, refusal):
    register_run("main-1", name="main")
    _asking(monkeypatch, "main-1")

    said = await _ask(criticality)

    assert said.startswith("ERROR") and refusal in said and "decide yourself" in said
    assert reg.get_run("main-1").asking == ()


@pytest.mark.asyncio
async def test_a_sub_agent_is_sent_to_its_coordinator(monkeypatch):
    register_run("main-1", name="main")
    register_run("child-1", name="backend-engineer", parent_run_id="main-1")
    _asking(monkeypatch, "child-1")  # its process scope (tests/account/test_caller_scope.py), whatever its environment says

    said = await _ask()

    assert said.startswith("ERROR") and "only the coordinator" in said and "main-1" in said and "keep working" in said
    assert reg.get_run("child-1").asking == () and reg.get_run("main-1").asking == ()


@pytest.mark.asyncio
async def test_the_owners_own_conversation_asks_him_in_its_answer(monkeypatch):
    _asking(monkeypatch, None)
    assert "ask him in your answer" in await _ask()


@pytest.mark.asyncio
async def test_past_the_open_cap_it_decides_itself(monkeypatch):
    register_run("main-1", name="main")
    _asking(monkeypatch, "main-1")
    for _ in range(OPEN_MAX):
        assert (await _ask()).startswith("Asked")

    assert "decide this one yourself" in await _ask()
    assert len(reg.get_run("main-1").asking) == OPEN_MAX


@pytest.mark.parametrize(("message_id", "closes"), [pytest.param("answer", True, id="his-answer"), pytest.param(None, False, id="another-message")])
def test_his_message_under_the_answer_id_closes_the_question_and_reaches_the_run(monkeypatch, message_id, closes):
    install_provider(monkeypatch, _Resumable())
    use_policy(monkeypatch, messaging, agents={"main": "fresh-model"}, reasoning={"main": "high"}, providers={"fake": True})
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    register_run("main-1", name="main", provider="fake", agent="main", provider_session_id="vendor-main")
    question = OwnerQuestion(question="Publier le site ?", options=OPTIONS, recommended=0, criticality="public", reason="public")
    reg.ask_owner("main-1", question)
    text = question.answer_text(QuestionAnswer(option=1))

    delivery = messaging.deliver_message("main-1", text, sender=reg.PERSON_SENDER, message_id=question.answer_id if message_id else None)
    again = messaging.deliver_message("main-1", text, sender=reg.PERSON_SENDER, message_id=question.answer_id) if closes else None

    assert delivery.state == "queued" and len(agent_queue.items("main-1")) == 1  # his answer reaches the run like any message of his
    assert (reg.get_run("main-1").asking == ()) is closes
    if again is not None:  # a retried tap is the same message: queued once
        assert again.repeated and len(agent_queue.items("main-1")) == 1
