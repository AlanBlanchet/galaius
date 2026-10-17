"""What each message is to the run reading it, and where that run stands, computed from the run records
(`galaius.agents.relations`). Owner 2026-10-17: « Even with your changes, it seems that everything is not clear enough
for the agents »: a report read as an order to acknowledge, a third party's note read as the boss's."""

import asyncio
import os
import uuid

import pytest

from galaius.agents import messaging, relations
from galaius.agents import registry as reg
from galaius.agents.spawn_line import Spawn
from galaius.agents.events import AgentEvent, Relation
from galaius.inbox_hook import HEADING, OPERATOR
from tests.support import register_run

NAMES = ("lead", "eng", "coordinator", "stranger", "rev", "old", "new", "helper")
IDS = {name: str(uuid.UUID(int=n << 96)) for n, name in enumerate(NAMES, start=1)}  # distinct first 8: a tag
ROLES = {"lead": "main", "eng": "backend-engineer", "coordinator": "main", "stranger": "tester", "rev": "code-reviewer",
         "old": "librarian", "new": "librarian", "helper": "tester"}


def _run(name: str, *, parent: str | None = None, working: bool = False, task: str | None = None, **rest) -> reg.AgentRun:
    run = register_run(IDS[name], agent=ROLES[name], name=ROLES[name], task=task or f"{name} task\nsecond line",
                       parent_run_id=IDS[parent] if parent else None, pid=os.getpid() if working else None, **rest)
    if not working:
        reg._merge_record_locked(run.run_id, {"status": "done", "finished_at": 50.0})
    return reg.get_run(run.run_id)


def _asked(name: str, by: str, text: str = "status?") -> None:
    with reg.record_lock(IDS[name]):
        reg.note_asker_locked(IDS[name], IDS[by])
    reg.record_message_event(from_run=IDS[by], to_run=IDS[name], text=text)


def _tag(name: str) -> str:
    return f"{ROLES[name]}-{IDS[name][:8]}"


@pytest.fixture
def team() -> dict[str, reg.AgentRun]:
    """lead spawned eng; coordinator (another conversation's lead) asked eng too; stranger asked nothing of anyone."""
    _run("lead", working=True)
    _run("eng", parent="lead", working=True)
    _run("coordinator", working=True)
    _run("stranger", working=True)
    _asked("eng", "coordinator", "and row I?")
    return {name: reg.get_run(IDS[name]) for name in ("lead", "eng", "coordinator", "stranger")}


@pytest.mark.parametrize("sender, recipient, text, kind", [
    pytest.param("eng", "lead", "DONE: row H live.", Relation.REPORT, id="child-to-parent-is-a-report"),
    pytest.param("lead", "eng", "also check the footer", Relation.ASKER, id="parent-to-child-is-who-it-works-for"),
    pytest.param("eng", "coordinator", "Row I live.", Relation.REPORT, id="reused-instance-to-its-other-asker-is-a-report"),
    pytest.param("coordinator", "eng", "and row J?", Relation.ASKER, id="other-asker-to-reused-instance-is-who-it-works-for"),
    pytest.param("stranger", "eng", "fyi the build is red", Relation.OTHER, id="third-party-is-no-one-it-works-for"),
    pytest.param("eng", "lead", "Shall I merge it?", Relation.REPORT_ASKING, id="report-ending-on-a-question-needs-an-answer"),
    pytest.param("eng", "lead", "BLOCKED: credential\nthe deploy key is missing", Relation.REPORT_ASKING, id="blocked-report-needs-an-answer"),
    pytest.param("eng", "lead", "Rows:\nrow H DONE\nSTATE: FAIL-AT-CAP (row I)", Relation.REPORT_ASKING, id="failed-stamp-needs-an-answer"),
])
def test_the_kind_of_a_message_comes_from_who_works_for_whom(team, sender, recipient, text, kind):
    assert relations.relation(team[sender], team[recipient], text) is kind


@pytest.mark.parametrize("sender, relation, reports, expected", [
    pytest.param("eng", Relation.REPORT, (), f"[Galaius: report from backend-engineer (run {IDS['eng'][:8]}) on what you asked it. Read it; no reply.]", id="report"),
    pytest.param("eng", Relation.REPORT_ASKING, (), f"[Galaius: report from backend-engineer (run {IDS['eng'][:8]}) on what you asked it. It needs your answer: answer it in this turn.]", id="report-asking"),
    pytest.param("coordinator", Relation.ASKER, (), f"[Galaius: message from main (run {IDS['coordinator'][:8]}), who you work for. Answer it in this turn.]", id="asker"),
    pytest.param("stranger", Relation.OTHER, (), f"[Galaius: message from tester (run {IDS['stranger'][:8]}). You don't work for it: reply only if it asks a question; your report still goes to main-{IDS['lead'][:8]}, main-{IDS['coordinator'][:8]}.]", id="other"),
])
def test_each_kind_opens_the_message_with_its_heading(team, sender, relation, reports, expected):
    recipient = team["eng"] if sender != "eng" else team["lead"]
    event = AgentEvent(kind="message", from_run=IDS[sender], to_run=recipient.run_id, text="x", relation=relation, reports=reports)
    assert relations.heading(event, recipient, mid_turn=False) == expected


def test_a_hand_back_started_turn_is_headed_as_the_reports_it_carries(team):
    _run("rev", parent="lead")
    lead = reg.get_run(IDS["lead"])
    for name, said in (("eng", "DONE: row H live."), ("rev", "Verdict: PASS.")):
        reg.record_message_event(from_run=IDS[name], to_run=IDS["lead"], text=said)
    event = AgentEvent(kind="message", from_run=IDS["rev"], to_run=IDS["lead"], text="…", reports=(IDS["eng"], IDS["rev"]))
    assert relations.heading(event, lead, mid_turn=False) == (
        f"[Galaius: reports from backend-engineer (run {IDS['eng'][:8]}), "
        f"code-reviewer (run {IDS['rev'][:8]}) on what you asked them. Read them; no reply.]")


def test_the_persons_message_keeps_its_own_heading_mid_turn_and_none_at_a_turn_start(team):
    event = AgentEvent(kind="message", from_run=reg.PERSON_SENDER, to_run=IDS["eng"], text="hi")
    assert relations.heading(event, team["eng"], mid_turn=True) == HEADING.format(who=OPERATOR)
    assert relations.heading(event, team["eng"], mid_turn=False) is None


def test_the_card_says_whom_the_run_works_for_what_is_at_work_and_where_its_report_goes(team):
    _run("helper", parent="eng", working=True)  # eng's own agent
    _run("rev", parent="lead")  # idle, reusable
    card = relations.card(reg.get_run(IDS["eng"])).splitlines()
    assert card == [
        relations.CARD_HEADING,
        f"You are {_tag('eng')}. You work for: {_tag('lead')} — « eng task ».",
        f"Also asked by: {_tag('coordinator')} — « and row I? ».",
        f"Your agents still at work: {_tag('helper')}.",
        f"Idle, to reuse: {_tag('rev')}.",
        f"When this turn ends your final message goes to {_tag('lead')}, {_tag('coordinator')} by itself: never send it, never ack a report.",
    ]


def test_a_card_lists_the_conversations_other_runs_at_work_and_a_top_run_reports_to_the_person(team):
    _run("helper", parent="eng", working=True)
    card = relations.card(reg.get_run(IDS["lead"])).splitlines()
    assert card[1] == f"You are {_tag('lead')}. You work for: the person who launched you."
    assert card[2] == f"Your agents still at work: {_tag('eng')}."
    assert card[3] == f"At work in this conversation: {_tag('helper')} — « helper task »."
    assert card[-1].startswith("When this turn ends your final message goes to the person who launched you by itself")


def _spawned(name: str, *, parent: str | None, **spawn) -> dict:
    """Start run `name` as `Spawn.start` does, its runner recording what it was handed; what that was."""
    handed = {}

    async def runner(provider, task, *, run_id, **rest):
        handed.update(rest)
        register_run(run_id, agent=ROLES[name], name=ROLES[name], task=task, parent_run_id=parent, pid=os.getpid(),
                     asked_by=rest["asked_by"], inherited_asks=rest["inherited_asks"])

    asyncio.run(Spawn(task=f"{name} task", cwd=".", agent=ROLES[name], parent_run_id=parent, **spawn).start(IDS[name], runner))
    return handed


def test_a_successor_owes_only_what_its_predecessor_still_owed_and_reads_what_is_sent_to_it(team):
    _run("old", parent="lead")
    reg._merge_record_locked(IDS["old"], {"context_tokens": 190_000, "context_window": 200_000})
    _asked("old", "coordinator", "fix rule 4")
    _asked("old", "stranger", "fix rule 5")
    reg.record_message_event(from_run=IDS["old"], to_run=IDS["stranger"], text="Rule 5 fixed.", reports=(IDS["old"],))
    _run("helper", parent="old", working=True)

    _spawned("new", parent=IDS["lead"])  # its context full, the old librarian is taken over without --continues

    new = reg.get_run(IDS["new"])
    assert relations.live(IDS["old"]) == IDS["new"] and not relations.succeed(IDS["old"], IDS["eng"])
    assert relations.owed(new) == (IDS["lead"], IDS["coordinator"])  # never the stranger, answered before
    assert relations.owed(reg.get_run(IDS["old"])) == ()  # its asks moved: each asker hears once
    assert relations.report_to(reg.get_run(IDS["helper"])) == _tag("new")
    assert relations.relation(reg.get_run(IDS["helper"]), new, "DONE") is Relation.REPORT
    assert f"Your agents still at work: {_tag('helper')}." in relations.card(new)
    assert _tag("old") not in relations.card(reg.get_run(IDS["eng"]))  # a run taken over is never offered for reuse
    assert messaging.queue_message(IDS["old"], "note to self", sender=IDS["new"]).state == "error"


@pytest.mark.parametrize("parent", ["lead", None], ids=["child-of-lead", "launched-by-the-person"])
def test_a_run_handing_off_its_own_work_leaves_its_successor_working_for_its_own_boss(team, parent):
    _run("old", parent=parent, working=True)
    _asked("old", "coordinator", "fix rule 4")

    _spawned("new", parent=IDS["old"], continues=IDS["old"])

    new = reg.get_run(IDS["new"])
    boss = _tag("lead") if parent else "the person who launched you"
    card = relations.card(new).splitlines()
    assert card[1] == f"You are {_tag('new')}. You work for: {boss}{' — « old task »' if parent else ''}."  # what the boss asked
    assert card[-1].startswith(f"When this turn ends your final message goes to {', '.join(filter(None, (parent and _tag('lead'), _tag('coordinator'))))} by itself")
    assert IDS["new"] not in {relations.live(asker) for asker in relations.owed(new)}
    assert relations.owed(reg.get_run(IDS["old"])) == ()  # the boss hears from the successor only


def test_a_critic_spawned_for_a_maker_starts_knowing_and_owing_both(team):
    handed = _spawned("rev", parent=IDS["lead"], for_run=IDS["eng"])  # `galaius agents spawn --for eng`

    rev = reg.get_run(IDS["rev"])
    assert handed["asked_by"] == (IDS["eng"],)  # recorded as it starts: the card its brief carries names the maker
    assert relations.owed(rev) == (IDS["lead"], IDS["eng"])
    assert f"Also asked by: {_tag('eng')} — « rev task »." in relations.card(rev)
    assert relations.relation(rev, team["eng"], "Verdict: FAIL\nrow 2") is Relation.REPORT_ASKING
    assert relations.relation(rev, team["eng"], "C1 FAIL fixed\nC2 PASS (was FAIL)") is Relation.REPORT
    for verdict in ("Review round 2 — FAIL", "Round 2 verdict: FAIL", "Verdict for be866616: FAIL"):
        assert relations.relation(rev, team["eng"], f"{verdict}\nrow 2") is Relation.REPORT_ASKING, verdict


@pytest.mark.parametrize("named, refused", [
    pytest.param({"continues": "caller"}, False, id="its-own-work"),
    pytest.param({"continues": "idle-peer"}, False, id="an-ended-run-of-its-role-in-its-conversation"),
    pytest.param({"continues": "coordinator"}, True, id="another-conversations-run"),
    pytest.param({"for_run": "eng"}, False, id="for-a-run-of-its-conversation"),
    pytest.param({"for_run": "coordinator"}, False, id="for-a-run-that-asked-it"),
    pytest.param({"for_run": "stranger"}, True, id="for-a-stranger"),
])
def test_a_run_names_only_runs_that_are_its_own_to_route(team, named, refused):
    """An agent steered by injected text must not reroute another conversation's reports (2026-10-17 review)."""
    _run("old", parent="lead")  # an ended librarian of lead's conversation
    with reg.record_lock(IDS["lead"]):
        reg.note_asker_locked(IDS["lead"], IDS["coordinator"])
    ids = IDS | {"caller": IDS["lead"], "idle-peer": IDS["old"]}
    spawn = Spawn(task="t", cwd=".", agent="librarian", parent_run_id=IDS["lead"], **{key: ids[value] for key, value in named.items()})
    assert (spawn.naming_refusal() is not None) is refused
