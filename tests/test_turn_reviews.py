"""A coordinator's answer to the person gets its reflection from the runtime, once, when the answer ended with its agents
back, waking no one; what the answer still owes is on the coordinator's next turn, the durable rows go to the prompts'
owner (`galaius.agents.turn_reviews`; 2026-10-21: the step lived only as prompt text and was skipped on nearly every turn)."""

import json
import time
import uuid

import pytest

from galaius.agents import handback, relations, turn_reviews
from galaius.agents import registry as reg
from galaius.config.settings import TurnReview, TurnReviews
from tests.support import register_run

MAIN = str(uuid.UUID(int=21 << 96))
REFLECTION = str(uuid.UUID(int=22 << 96))
REFLECTION_ONLY = TurnReviews(reviews=TurnReviews().reviews[:1])
REPORT = "Done right: …\n## DURABLE CHANGE\n- D1 hook: refuse kill by pattern\n## REDO\n- R1 rerun the visual check"


def _ended(run_id: str, *, report: str = "", **rest) -> None:
    if reg.get_run(run_id) is None:
        register_run(run_id, **rest)
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": report})))
    reg.finish(run_id, exit_code=0, first=0)
    reg._merge_record_locked(run_id, {"status": "done", "finished_at": time.time()})


@pytest.fixture
def asked(monkeypatch) -> list[dict[str, str]]:
    """Each spawn the watcher asks, its reflection taken by run REFLECTION (started under MAIN)."""
    calls: list[dict[str, str]] = []

    def spawn(argv, **_):
        tail = argv[argv.index("spawn") + 1:]
        calls.append(dict(zip(tail[::2], tail[1::2])))
        if reg.get_run(REFLECTION) is None:
            register_run(REFLECTION, agent="reflection", name="reflection", parent_run_id=MAIN)
        return turn_reviews.subprocess.CompletedProcess(argv, 0, f"{REFLECTION}\n", "")

    monkeypatch.setattr(turn_reviews.subprocess, "run", spawn)
    return calls


@pytest.mark.parametrize("opened", ["his-message", "his-words-started-it", "an-agents-message"])
def test_a_coordinators_answer_to_the_person_is_reflected_on_once_waking_no_one(asked, opened):
    watch = turn_reviews.TurnReviewWatch(started=time.time() - 1)
    register_run(MAIN, agent="main", name="main", asked_by=(str(uuid.uuid4()),) if opened != "his-words-started-it" else ())
    if opened != "his-words-started-it":
        reg.record_message_event(from_run=reg.PERSON_SENDER if opened == "his-message" else str(uuid.uuid4()), to_run=MAIN, text="ajoute le mode sombre")
    _ended(MAIN)
    watch.tick(REFLECTION_ONLY)
    watch.tick(REFLECTION_ONLY)   # the next look: already asked
    if opened == "an-agents-message":   # no answer to the person
        assert asked == []
        return
    [spawn] = asked
    assert (spawn["--agent"], spawn["--parent-run-id"]) == ("reflection", MAIN)
    assert str(reg.events_path(MAIN)) in spawn["--task"] and "DURABLE CHANGE" in spawn["--task"] and "REDO" in spawn["--task"]
    _ended(REFLECTION, report=REPORT)
    assert MAIN not in relations.owed(reg.get_run(REFLECTION)) and handback.hand_back(REFLECTION) == []   # wakes no one


def test_the_open_rows_reach_the_coordinators_next_card_and_the_durable_ones_the_librarian(asked):
    """He replies while the reflection still runs: the turn his reply starts shows nothing yet; the first card drawn after
    the reflection ended shows its open rows, once."""
    watch = turn_reviews.TurnReviewWatch(started=time.time() - 1)
    register_run(MAIN, agent="main", name="main", asked_by=(str(uuid.uuid4()),))
    reg.record_message_event(from_run=reg.PERSON_SENDER, to_run=MAIN, text="ajoute le mode sombre")
    _ended(MAIN)
    watch.tick(REFLECTION_ONLY)                            # reflection asked, at work
    assert "R1" not in relations.card(reg.get_run(MAIN))       # his reply starts a turn meanwhile
    _ended(REFLECTION, report=REPORT)
    watch.tick(REFLECTION_ONLY)
    assert [(spawn["--agent"], spawn["--parent-run-id"]) for spawn in asked] == [("reflection", MAIN), ("librarian", MAIN)]
    assert "D1 hook: refuse kill by pattern" in asked[-1]["--task"] and "R1" not in asked[-1]["--task"]
    _ended(MAIN)                                               # that turn ends after the reflection did
    card = relations.card(reg.get_run(MAIN))                   # the next one
    assert "R1 rerun the visual check" in card and "D1" not in card
    assert "R1 rerun" not in relations.card(reg.get_run(MAIN))  # never again


@pytest.mark.parametrize(("earlier", "said", "frustrated"), [
    ((), "I got and error for exceeding characters... why are you putting these shit limits everywhere ?...", True),
    (("add a dark mode",), "I asked for dark mode yesterday, dark mode please", True),                             # raised again
    (("Garde le panneau des agents ouvert",), "Le panneau des agents se ferme encore, garde-le ouvert", True),         # in French too
    ((), "Ça ne marche toujours pas", True),
    ((), "Encore raté !! Regarde le panneau !", True),
    ((), "if a != b and c != d: return ![img](a.png) ![img](b.png)", False),                                       # code, never « ! »
    (("Ajoute le mode sombre au panneau des agents",), "Now export the invoices as CSV", False),
    (("build the agents panel with a list of runs",), "the agents panel looks good, now add filters to the runs list", False),  # a follow-up
    (("translate the invoice email to French",), "now translate the invoice email to German too", False),
])
def test_an_answer_to_a_frustrated_message_also_gets_its_frustration_analysis(asked, earlier, said, frustrated):
    register_run(MAIN, agent="main", name="main", asked_by=(str(uuid.uuid4()),))
    for text in earlier:
        reg.record_message_event(from_run=reg.PERSON_SENDER, to_run=MAIN, text=text)
    watch = turn_reviews.TurnReviewWatch(started=time.time())
    time.sleep(0.01)
    reg.record_message_event(from_run=reg.PERSON_SENDER, to_run=MAIN, text=said)
    _ended(MAIN)
    watch.tick(TurnReviews())
    watch.tick(TurnReviews())
    assert [spawn["--agent"] for spawn in asked] == ["reflection", *(["frustration-analyzer"] if frustrated else [])]
    if frustrated:
        assert "frustration" in asked[-1]["--task"] and "DURABLE CHANGE" in asked[-1]["--task"]


def test_a_signal_is_configuration():
    custom = TurnReviews(reviews=(TurnReview(role="frustration-analyzer", ask="{signal}",
                                              signal={"patterns": [r"\bfeu\b"], "exclamations": None}),))
    assert custom.reviews[0].signal.matched("au feu") == "« feu »" and custom.reviews[0].signal.matched("Ça marche !!!") is None
