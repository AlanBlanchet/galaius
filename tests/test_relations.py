"""What each message is to the run reading it, and where that run stands, computed from the run records
(`galaius.agents.relations`). Owner 2026-10-17: « Even with your changes, it seems that everything is not clear enough
for the agents »: a report read as an order to acknowledge, a third party's note read as the boss's."""

import asyncio
import re
import os
import time
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


def _placed(name: str) -> str:
    """`name`'s run as the turn card lists it: with its project."""
    return f"{_tag(name)} (project: {relations.project_label(relations.project(IDS[name]))})"


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
        f"You are {_placed('eng')}. You work for: {_placed('lead')} — « eng task ».",
        f"Also asked by: {_placed('coordinator')} — « and row I? ».",
        f"Your agents still at work: {_placed('helper')}.",
        f"Idle, to reuse: {_placed('rev')}.",
        f"When this turn ends your final message goes to {_placed('lead')}, {_placed('coordinator')} by itself: never send it, never ack a report.",
    ]


def test_a_card_lists_the_conversations_other_runs_at_work_and_a_top_run_reports_to_the_person(team):
    _run("helper", parent="eng", working=True)
    card = relations.card(reg.get_run(IDS["lead"])).splitlines()
    assert card[1] == f"You are {_placed('lead')}. You work for: the person who launched you."
    assert card[2] == f"Your agents still at work: {_placed('eng')}."
    assert card[3] == f"At work in this conversation: {_placed('helper')} — « helper task »."
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
    reg._merge_record_locked(IDS["old"], {"compactions": 1, "compacted_to": 185_000, "context_tokens": 190_000, "context_window": 200_000})
    _asked("old", "coordinator", "fix rule 4")
    _asked("old", "stranger", "fix rule 5")
    reg.record_message_event(from_run=IDS["old"], to_run=IDS["stranger"], text="Rule 5 fixed.", reports=(IDS["old"],))
    _run("helper", parent="old", working=True)

    _spawned("new", parent=IDS["lead"])  # still near full after a compaction, the old librarian is taken over without --continues

    new = reg.get_run(IDS["new"])
    assert relations.live(IDS["old"]) == IDS["new"] and not relations.succeed(IDS["old"], IDS["eng"])
    assert relations.owed(new) == (IDS["lead"], IDS["coordinator"])  # never the stranger, answered before
    assert relations.owed(reg.get_run(IDS["old"])) == ()  # its asks moved: each asker hears once
    assert relations.report_to(reg.get_run(IDS["helper"])) == _tag("new")
    assert relations.relation(reg.get_run(IDS["helper"]), new, "DONE") is Relation.REPORT
    assert f"Your agents still at work: {_placed('helper')}." in relations.card(new)
    assert _tag("old") not in relations.card(reg.get_run(IDS["eng"]))  # a run taken over is never offered for reuse
    assert messaging.queue_message(IDS["old"], "note to self", sender=IDS["new"]).state == "error"


@pytest.mark.parametrize("parent", ["lead", None], ids=["child-of-lead", "launched-by-the-person"])
def test_a_run_handing_off_its_own_work_leaves_its_successor_working_for_its_own_boss(team, parent):
    _run("old", parent=parent, working=True)
    _asked("old", "coordinator", "fix rule 4")

    _spawned("new", parent=IDS["old"], continues=IDS["old"])

    new = reg.get_run(IDS["new"])
    boss = _placed("lead") if parent else "the person who launched you"
    card = relations.card(new).splitlines()
    assert card[1] == f"You are {_placed('new')}. You work for: {boss}{' — « old task »' if parent else ''}."  # what the boss asked
    assert card[-1].startswith(f"When this turn ends your final message goes to {', '.join(filter(None, (parent and _placed('lead'), _placed('coordinator'))))} by itself")
    assert IDS["new"] not in {relations.live(asker) for asker in relations.owed(new)}
    assert relations.owed(reg.get_run(IDS["old"])) == ()  # the boss hears from the successor only


@pytest.mark.parametrize("gauge, outgrown", [
    pytest.param({"compactions": 1, "compacted_to": 170_000, "context_tokens": 175_000}, True, id="its-compaction-left-it-near-full"),
    pytest.param({"compactions": 1, "compacted_to": 15_000, "context_tokens": 170_000}, False, id="grown-back-after-a-compaction"),
    pytest.param({"compactions": 3, "compacted_to": 20_000, "context_tokens": 60_000}, False, id="compacted-often-yet-room-left"),
    pytest.param({"compactions": 0, "context_tokens": 190_000}, False, id="near-full-not-yet-compacted"),
])
def test_a_run_hands_its_work_off_only_when_still_near_full_after_a_compaction(gauge, outgrown):
    assert relations.outgrown(reg.AgentRun(run_id="r", provider="claude", name="r", cwd=".", context_window=200_000, **gauge)) is outgrown


@pytest.mark.parametrize("helper", [False, True], ids=["takes-over-from-its-outgrown-parent", "helper-keeps-reporting-to-it"])
def test_a_run_of_its_role_an_outgrown_run_spawns_takes_over_from_it_unless_a_helper(team, helper):
    _run("old", parent="lead", working=True)
    reg._merge_record_locked(IDS["old"], {"compactions": 1, "compacted_to": 170_000, "context_tokens": 170_000, "context_window": 200_000})

    _spawned("new", parent=IDS["old"], helper=helper)
    reg._merge_record_locked(IDS["old"], {"status": "done", "finished_at": 60.0, "pid": None})  # its turn ended, waiting

    new = reg.get_run(IDS["new"])
    assert relations.live(IDS["old"]) == (IDS["old"] if helper else IDS["new"])  # decided at the spawn, never after
    assert relations.owed(new) == ((IDS["old"],) if helper else (IDS["lead"],))
    assert relations.boss(new) == (IDS["old"] if helper else IDS["lead"])


def test_a_critic_spawned_for_a_maker_starts_knowing_and_owing_both(team):
    handed = _spawned("rev", parent=IDS["lead"], for_run=IDS["eng"])  # `galaius agents spawn --for eng`

    rev = reg.get_run(IDS["rev"])
    assert handed["asked_by"] == (IDS["eng"],)  # recorded as it starts: the card its brief carries names the maker
    assert relations.owed(rev) == (IDS["lead"], IDS["eng"])
    assert f"Also asked by: {_placed('eng')} — « rev task »." in relations.card(rev)
    assert relations.relation(rev, team["eng"], "Verdict: FAIL\nrow 2") is Relation.REPORT_ASKING
    assert relations.relation(rev, team["eng"], "C1 FAIL fixed\nC2 PASS (was FAIL)") is Relation.REPORT


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


@pytest.mark.parametrize("line, failed", [
    *((verdict, True) for verdict in ("BLOCKED: credential", "**Verdict: FAIL**", "Review round 2 — FAIL", "Round 2 verdict: FAIL",
                                      "Verdict for be866616: FAIL", "code-reviewer verdict: REJECTED", "STATE: FAIL-AT-CAP")),
    *((other, False) for other in ("C2 PASS (was FAIL)", "Verdict: PASS", "Fixed the FAIL rows", "-" * 5000, "#." * 5000, "—" * 5000)),
])
def test_a_report_needs_an_answer_when_a_line_opens_on_a_failed_verdict_read_in_linear_time(line, failed):
    started = time.perf_counter()
    assert relations.needs_answer(f"Done.\n{line}") is failed
    assert time.perf_counter() - started < 0.1  # a run of dashes once held the recipient's queue lock for seconds


@pytest.fixture
def two_projects(team, tmp_path, monkeypatch):
    """lead coordinates repository `site`, coordinator repository `shop`; eng, lead's child, works in a scratch folder;
    stranger coordinates from a `.worktrees/` slice of `site`. This process descends from no run (the person's terminal)."""
    folders = {name: tmp_path / name for name in ("site", "shop", "scratch")} | {"slice": tmp_path / "site" / ".worktrees" / "fix"}
    for name in ("site", "shop"):
        (folders[name] / ".git").mkdir(parents=True)
    folders["scratch"].mkdir()
    folders["slice"].mkdir(parents=True)
    (folders["slice"] / ".git").write_text("gitdir: ../../.git/worktrees/fix\n")
    for run, folder in (("lead", "site"), ("coordinator", "shop"), ("eng", "scratch"), ("stranger", "slice")):
        reg._merge_record_locked(IDS[run], {"cwd": str(folders[folder])})
    monkeypatch.setenv("GALAIUS_RUN_ID", IDS["lead"])
    monkeypatch.setattr(reg, "ancestor_run", lambda: None)
    return folders


def _send_cli(capsys, **flags) -> str:
    from galaius.cli.app_commands import agents_send
    with pytest.raises(SystemExit):
        agents_send(IDS["coordinator"], "take it all", **flags)
    return capsys.readouterr().out


def _from_an_agents_process(monkeypatch, variable: str | None = None) -> None:
    """This process descends from lead's run, its `GALAIUS_RUN_ID` cleared (`env -u`) or set to `variable`."""
    monkeypatch.delenv("GALAIUS_RUN_ID") if variable is None else monkeypatch.setenv("GALAIUS_RUN_ID", variable)
    monkeypatch.setattr(reg, "ancestor_run", lambda: reg.get_run(IDS["lead"]))


@pytest.mark.parametrize("entry", ["foreign_project", "succeed", "spawn-continues", "terminal-continues", "cli-send", "mcp-send",
                                   "cleared-variable-send", "agent-passes-the-override", "forged-parent-continues",
                                   "bogus-run-id-send"])
def test_a_run_never_messages_or_hands_off_to_another_projects_run(two_projects, entry, capsys, monkeypatch):
    """Owner 2026-10-19: one project's coordinator handed its work to another project's coordinator (same web session,
    separate launch trees). Each entry refuses, naming both projects; nothing is recorded or queued."""
    from galaius.server import tools_agents
    heard = len(reg.read_events(IDS["coordinator"]))
    assert relations.foreign_project(IDS["eng"], IDS["lead"]) is None  # a child in another folder is its coordinator's project
    assert relations.foreign_project(IDS["stranger"], IDS["lead"]) is None  # a slice of the repository is the repository
    said = {
        "foreign_project": lambda: relations.foreign_project(IDS["lead"], IDS["coordinator"]),
        "succeed": lambda: str(pytest.raises(relations.ForeignProject, relations.succeed, IDS["coordinator"], IDS["lead"]).value),
        "spawn-continues": lambda: Spawn(task="t", cwd=".", agent="main", parent_run_id=IDS["lead"], continues=IDS["coordinator"]).naming_refusal(),
        "terminal-continues": lambda: Spawn(task="t", cwd=str(two_projects["site"]), agent="main", continues=IDS["coordinator"]).naming_refusal(),
        "cli-send": lambda: _send_cli(capsys),
        "mcp-send": lambda: asyncio.run(tools_agents.agent_send(IDS["coordinator"], "take it all")),
        "cleared-variable-send": lambda: _from_an_agents_process(monkeypatch) or _send_cli(capsys),
        "agent-passes-the-override": lambda: _from_an_agents_process(monkeypatch) or _send_cli(capsys, cross_project=True),
        "bogus-run-id-send": lambda: _from_an_agents_process(monkeypatch, "not-a-run") or _send_cli(capsys),
        "forged-parent-continues": lambda: _from_an_agents_process(monkeypatch) or Spawn(
            task="t", cwd=".", agent="main", parent_run_id=IDS["coordinator"], continues=IDS["coordinator"]).naming_refusal(),
    }[entry]()
    assert "the person's own act" in said if entry == "agent-passes-the-override" else "project shop" in said and "project site" in said, said
    assert reg.get_run(IDS["lead"]).successor is None and reg.get_run(IDS["coordinator"]).successor is None
    assert len(reg.read_events(IDS["coordinator"])) == heard
    if entry == "succeed":  # the person's deliberate act
        assert relations.succeed(IDS["coordinator"], IDS["lead"], cross_project=True)
    if entry == "terminal-continues":
        assert Spawn(task="t", cwd=str(two_projects["site"]), agent="main", continues=IDS["coordinator"], cross_project=True).naming_refusal() is None


@pytest.mark.parametrize("folder, refused", [
    pytest.param("site", False, id="its-own-repository"),
    pytest.param("slice", False, id="a-slice-of-its-repository"),
    pytest.param("scratch", False, id="a-folder-in-no-repository"),
    pytest.param("shop", True, id="another-projects-repository"),
    pytest.param("shop-worktree", True, id="a-linked-worktree-of-another-project-lying-elsewhere"),
    pytest.param("override", False, id="the-persons-own-cross-project-act"),
])
def test_a_conversation_works_for_the_project_its_first_coordinator_started_in(two_projects, tmp_path, folder, refused):
    """Owner 2026-10-19: one project's coordinator talked with another project's, which had nothing to do with it. A
    run of lead's conversation (repository `site`) is never started in another project's repository: not `shop`, nor a
    worktree of it added outside it; a scratch folder belongs to no project."""
    worktree = tmp_path / "shop-wt" / "ui"
    (two_projects["shop"] / ".git" / "worktrees" / "ui").mkdir(parents=True)
    (two_projects["shop"] / ".git" / "worktrees" / "ui" / "commondir").write_text("../..\n")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {two_projects['shop'] / '.git' / 'worktrees' / 'ui'}\n")
    assert relations.folder_project(str(worktree)) == two_projects["shop"] and relations.folder_project(str(two_projects["slice"])) == two_projects["site"]
    cwd = {"shop-worktree": worktree, "override": two_projects["shop"]}.get(folder) or two_projects[folder]
    said = Spawn(task="t", cwd=str(cwd), agent="librarian", parent_run_id=IDS["lead"], cross_project=folder == "override").naming_refusal()
    assert (said is not None) is refused
    assert not refused or f"project shop, and this conversation works for project site" in said


@pytest.mark.parametrize("asker, agent, continues, refused", [
    pytest.param("eng", "main", None, True, id="a-maker-starts-a-coordinator-in-its-conversation"),
    pytest.param("lead", "main", "lead", False, id="a-coordinator-hands-its-own-work-on"),
    pytest.param("eng", "librarian", None, False, id="a-maker-starts-another-role"),
    pytest.param(None, "main", None, False, id="the-persons-own-spawn"),
])
def test_a_coordinator_never_starts_inside_an_agents_conversation(two_projects, monkeypatch, asker, agent, continues, refused):
    """Owner 2026-10-27 « Why is there another Coordinator in your list of agents ? »: makers probing the product spawned
    `main` in scratch folders as their own children; each joined his conversation's agent list."""
    monkeypatch.setattr(messaging, "asking_run", lambda: asker and IDS[asker])
    said = Spawn(task="t", cwd=str(two_projects["scratch"]), agent=agent, parent_run_id=IDS[asker or "lead"],
                 continues=continues and IDS[continues]).naming_refusal()
    assert (said is not None) is refused, said
    assert not refused or "GALAIUS_AGENTS_DIR" in said


def test_the_turn_card_names_the_project_of_each_run_it_lists(two_projects):
    card = relations.card(reg.get_run(IDS["eng"]))
    site = relations.project_label(two_projects["site"])
    assert f"You are {_tag('eng')} (project: {site}). You work for: {_tag('lead')} (project: {site})" in card
    unread = reg.get_run(IDS["eng"]).model_copy(update={"run_id": str(uuid.uuid4())})   # a first turn: no record to read back yet
    assert relations.card(unread).splitlines()[1].startswith(f"You are {ROLES['eng']}-{unread.run_id[:8]} (project: {site}).")
    tags = re.findall(r"\b[a-z][a-z-]*-[0-9a-f]{8}\b", card)
    assert len(tags) >= 3 and len(tags) == len(re.findall(r"\b[a-z][a-z-]*-[0-9a-f]{8} \(project: ", card)), card   # each run it lists
