"""His message taken back (owner 2026-11-04: « i just said that exact thing to the wrong coordinator in the wrong project...
And i wanted to stop / remove that message or do something, but i couldn't »): still waiting → never read; read by the
turn at work → that turn and the agents it started on it stop; once read, the run's next turn is told, once; asked
again → the same outcome, nothing done twice (`messaging.retract`)."""

import os
import time

import pytest
from galaius_core import StopActor

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.catalog import relations
from tests.support.agents import setup_queue

HIM = StopActor(kind="person", name="Alan")
WRONG = "deploy the billing change to prod now"


def _sent(text: str = WRONG) -> str:
    """His message to `r1`, queued (not yet read): its message id."""
    delivery = messaging.deliver_message("r1", text, sender=reg.PERSON_SENDER)
    assert delivery.state != "error", delivery.text
    return next(item.message_id for item in agent_queue.items("r1") if item.id == delivery.queue_id)


def _reading(monkeypatch) -> list[int]:
    """`r1`'s turn claims what waits and is at work on it now (its process this one, never killed: `_terminate` noted)."""
    killed: list[int] = []
    monkeypatch.setattr(reg, "_terminate", killed.append)
    with reg.record_lock("r1"):
        assert agent_queue.claim_next_locked("r1") is not None
        reg._merge_record_locked("r1", {"status": "running", "pid": os.getpid(), "pid_started": None, "finished_at": None})
    return killed


def _child(run_id: str, started_at: float) -> None:
    reg.register(run_id=run_id, pid=os.getpid(), provider="queue-fake", name=run_id, task="t", agent="tester",
                 parent_run_id="r1", provider_session_id=f"vendor-{run_id}")
    reg._merge_record_locked(run_id, {"started_at": started_at, "status": "running", "pid_started": None})


def test_taken_back_while_waiting_is_never_read(monkeypatch):
    provider = setup_queue(monkeypatch)
    message = _sent()

    assert messaging.retract("r1", message, by=HIM) == "unread"
    agent_queue.dispatch("r1")

    assert provider.messages == []
    assert [item.state for item in agent_queue.items("r1")] == ["cancelled"]
    assert reg.get_run("r1").held_at is None   # nothing was stopped: the run is not held
    assert relations._his_words(reg.get_run("r1")) == "t"   # his recalled words never hold it


def test_taken_back_while_read_stops_the_turn_and_its_agents_and_the_next_turn_is_told_once(monkeypatch):
    provider = setup_queue(monkeypatch)
    _child("before", time.time() - 60)   # at work on an earlier ask: kept
    message = _sent()
    killed = _reading(monkeypatch)
    _child("on-it", time.time() + 1)   # started on the withdrawn message

    assert messaging.retract("r1", message, by=HIM) == "stopped"

    run = reg.get_run("r1")
    assert run.status == "stopped" and run.held_at is not None   # held as by his stop: nothing restarts it by itself
    assert reg.get_run("on-it").status == "stopped" and reg.get_run("before").status == "running"
    assert len(killed) == 2

    assert messaging.deliver_message("r1", "sorry, wrong conversation: carry on", sender=reg.PERSON_SENDER).state != "error"
    agent_queue.dispatch("r1")
    told = provider.messages[-1]
    assert f"« {WRONG} »" in told and "do not act on it" in told and "the turn working on it was stopped" in told
    assert told.index(WRONG) < told.index("carry on")

    assert messaging.deliver_message("r1", "next", sender=reg.PERSON_SENDER).state != "error"
    agent_queue.dispatch("r1")
    assert WRONG not in provider.messages[-1]   # told once


def test_taking_back_the_message_a_turn_opened_on_keeps_his_later_words_queued(monkeypatch):
    """His follow-up written behind it is never dropped: only that turn stops, the run is not held, the follow-up goes next
    (code-reviewer 74d9e07b B2: a whole-run stop cancelled it and held the run)."""
    provider = setup_queue(monkeypatch)
    message = _sent()
    _reading(monkeypatch)
    _sent("and the footer too")

    assert messaging.retract("r1", message, by=HIM) == "stopped"
    assert [item.state for item in agent_queue.items("r1")] == ["cancelled", "pending"] and reg.get_run("r1").held_at is None
    reg._merge_record_locked("r1", {"pid": 999999})   # its process gone
    agent_queue.dispatch("r1")
    assert "and the footer too" in provider.messages[-1] and f"« {WRONG} »" in provider.messages[-1]


def test_a_message_read_mid_turn_never_stops_the_turn_at_work_on_an_earlier_ask(monkeypatch):
    """Read by a turn at work on another ask (`inject`): that turn goes on, told at its next step (B2)."""
    setup_queue(monkeypatch)
    _sent("the earlier ask")
    killed = _reading(monkeypatch)
    message = _sent()
    assert any(WRONG in text for text in agent_queue.inject("r1"))   # read at its next step

    assert messaging.retract("r1", message, by=HIM) == "after"
    assert killed == [] and reg.get_run("r1").status == "running"
    told = agent_queue.inject("r1")   # its next step, nothing else pending
    assert told and f"« {WRONG} »" in told[0] and agent_queue.inject("r1") == []


def test_taken_back_after_its_turn_ended_tells_the_next_turn(monkeypatch):
    provider = setup_queue(monkeypatch)
    message = _sent()
    agent_queue.dispatch("r1")   # read and answered
    assert len(provider.messages) == 1

    assert messaging.retract("r1", message, by=HIM) == "after"
    assert reg.get_run("r1").held_at is None   # no turn to stop

    assert messaging.deliver_message("r1", "next", sender=reg.PERSON_SENDER).state != "error"
    agent_queue.dispatch("r1")
    assert f"« {WRONG} »" in provider.messages[-1] and "was stopped" not in provider.messages[-1]


@pytest.mark.parametrize("reading", [False, True], ids=["waiting", "read"])
def test_taken_back_twice_answers_the_same_and_stops_nothing_again(monkeypatch, reading):
    setup_queue(monkeypatch)
    message = _sent()
    if reading:
        _reading(monkeypatch)
    first = messaging.retract("r1", message, by=HIM)
    stops: list[str] = []
    monkeypatch.setattr(reg, "stop", lambda run_id, **_: stops.append(run_id) or True)

    assert messaging.retract("r1", message, by=HIM) == first and stops == []


def test_a_message_that_never_reached_the_run_is_unknown(monkeypatch):
    setup_queue(monkeypatch)
    assert messaging.retract("r1", "f" * 32, by=HIM) == "unknown"
    assert messaging.retract("no-such-run", "f" * 32, by=HIM) == "unknown"
