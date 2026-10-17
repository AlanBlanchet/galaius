"""Who a run works for, computed from the run records and written into what it reads.

Every turn opens with where its run stands (`card`), and every message it reads with who wrote it and what that writer
is to it (`heading`): the message's kind is inferred from the records (`relation`), never chosen by its sender. Owner
2026-10-17: « it seems that everything is not clear enough for the agents » — two runs handing back to each other for
good, reports read as asks and acknowledged (each acknowledgement a turn), verdicts relayed by hand, a hand-off's new
run unknown to the runs still writing to the old one."""

import re
from collections.abc import Sequence

from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent, Relation, ending_of
from galaius.inbox_hook import HEADING, OPERATOR

#: Share of its context window past which a run is not handed a new task (`spawn_line.Spawn._misfit`): a new run of its
#: role started under its parent takes over from it (`succeed`).
CONTEXT_NEAR_FULL = 0.8
#: The other runs at work a card names, newest first.
CARD_RUNS_MAX = 8
#: The longest first line a card quotes.
QUOTE_MAX = 100
#: Successors followed before a chain is read as ended (a record edited by hand into a loop).
_CHAIN_MAX = 32
#: The line a turn card (`card`) opens with.
CARD_HEADING = "[Galaius: where you stand, from the run records]"
#: A line opening on a failed verdict, bare or after a short label (« Verdict: FAIL », « STATE: BLOCKED »): a report whose
#: first or last line (its closing stamp) is one still needs its reader (`needs_answer`).
_FAILED = re.compile(r"^\W*(?:\w+(?: \w+)?\W*:\W*)?(?:BLOCKED|FAIL(?:-AT-CAP)?|REJECTED)\b")
#: Where an agent's own words start in a message it sent (`registry.AgentRun.handoff_header`).
_CONTENT = "\n[Agent content]\n"
#: What a run at work is doing, as its record says (a queued message alone is not work a card names).
_AT_WORK = frozenset({"starting", "running", "waiting"})
#: Ended runs whose session can take a next turn (every ending but a run cancelled before it started): a spawn of its
#: role hands it the task (`spawn_line.Spawn._misfit`).
REUSABLE: frozenset[str] = reg.TERMINAL_STATUSES - {"cancelled"}


def chain(run_id: str) -> tuple[str, ...]:
    """`run_id` and each run that took over from the one before it (`AgentRun.successor`), oldest first."""
    ids = [run_id]
    while len(ids) < _CHAIN_MAX and (run := reg.get_run(ids[-1])) is not None and run.successor and run.successor not in ids:
        ids.append(run.successor)
    return tuple(ids)


def live(run_id: str) -> str:
    """The run at the end of `run_id`'s chain (`chain`): the one a report, a hand-back or a message for it reaches."""
    return chain(run_id)[-1]


def inherited(predecessor: reg.AgentRun, *, parent: str | None) -> dict[str, float]:
    """What a run started under `parent` to take over from `predecessor` owes from its start (`AgentRun.inherited_asks`):
    each ask the predecessor still owed (`open_asks`), at its own time. Never one its new parent asks anyway (the task it
    starts on), nor the predecessor itself (a run handing off its own work: `boss`)."""
    return {asker: at or predecessor.started_at or 0.0 for asker, at in open_asks(predecessor).items()
            if asker not in (parent, predecessor.run_id)}


def succeed(predecessor_id: str, successor_id: str) -> bool:
    """Record run `successor_id` as taking over from run `predecessor_id` (a hand-off, a run whose context filled), the
    successor started owing what it inherited (`inherited`): what is handed back or sent to the predecessor reaches the
    successor (`live`), and the predecessor is never resumed again. False when it had a successor already, or is the
    same run."""
    if predecessor_id == successor_id:
        return False
    with reg.record_lock(predecessor_id):
        predecessor = reg.get_run(predecessor_id)
        if predecessor is None or predecessor.successor is not None:
            return False
        reg._merge_record_locked(predecessor_id, {"successor": successor_id})
    return True


def boss(run: reg.AgentRun) -> str | None:
    """The run `run` works for first: its parent at the end of its chain (`live`); for a run its own parent handed its
    work to (`--continues` itself), that parent's own; None for a run the person launched."""
    parent, seen = run.parent_run_id, {run.run_id}
    while parent and parent not in seen and live(parent) == run.run_id:
        seen.add(parent)
        parent = above.parent_run_id if (above := reg.get_run(parent)) is not None else None
    return live(parent) if parent and parent not in seen else None


def content(text: str) -> str:
    """The sender's own words in a recorded message: past its provenance header when it carries one."""
    return text.split(_CONTENT, 1)[1] if text.startswith("[Interact agent provenance]") and _CONTENT in text else text


def needs_answer(text: str) -> bool:
    """A report still needing its reader: it opens on a failed verdict, or its closing line names one or asks."""
    lines = [line.strip() for line in content(text).splitlines() if line.strip()]
    return bool(lines) and (bool(_FAILED.match(lines[0])) or bool(_FAILED.match(lines[-1])) or lines[-1].endswith("?"))


def answers(sender: reg.AgentRun, recipient: reg.AgentRun) -> bool:
    """Whether a message from `sender` answers what `recipient` asked it: `recipient` is a run it works for (or took over
    from one, `live`), and when each asked the other, `recipient`'s ask is the one still owed (`owed`)."""
    if recipient.run_id not in {live(asker) for asker in sender.askers}:
        return False
    return sender.run_id not in recipient.askers or recipient.run_id in {live(asker) for asker in owed(sender)}


def relation(sender: reg.AgentRun, recipient: reg.AgentRun, text: str, *, reports: Sequence[str] = ()) -> Relation:
    """What `sender` is to `recipient` for a message saying `text` (a hand-back: carrying the final words of `reports`),
    from their records as they are now."""
    if answers(sender, recipient):
        asking = any(needs_answer(final_words(run_id)) for run_id in reports) if reports else needs_answer(text)
        return Relation.REPORT_ASKING if asking else Relation.REPORT
    return Relation.ASKER if sender.run_id in recipient.askers else Relation.OTHER


def final_words(run_id: str) -> str:
    """What run `run_id` last said: its ending's text, else its newest text."""
    ending = ending_of(events := reg.read_events(run_id), run_id)
    said = ending.text if ending is not None else next((event.text for event in reversed(events) if event.kind == "text"), "")
    return (said or "").strip()


def owed(run: reg.AgentRun) -> tuple[str, ...]:
    """The runs its ended turn reports to: each run it answers (`askers`) whose latest ask no hand-back has carried its
    report on yet (`_reported`). Its parent asked its task when it started, and asks again with each message it or the
    person sends; a run of its `asked_by` asked from its start (a spawn `for_run` it, a hand-off) and again with each message
    it sends. An automatic resume, the person's « Continuer », any hand-back (`reports`) or a one-way note (`fyi`) is no
    ask. An ask with no time stays owed. 2026-10-17: a report sent to a third run made it an asker for good, each later turn of the
    reporter woke it with an empty report (one coordinator: 9 such runs; a librarian: 4 empty turns; four runs looping
    every 3 min); a reused instance's every turn also woke the run that had spawned it for an earlier job."""
    return tuple(open_asks(run))


def open_asks(run: reg.AgentRun) -> dict[str, float | None]:
    """Each ask `owed` counts, with its time: never one of a run `run` took over itself (a hand-off to itself)."""
    return {asker: at for asker, at in _asks(run).items()
            if live(asker) != run.run_id and (at is None or not _reported(run.run_id, to=asker, since=at))}


def _asks(run: reg.AgentRun) -> dict[str, float | None]:
    """Each run it answers that asked it something, with when it last did (`owed`)."""
    # Each run it works for asked it from its start (a spawn `--for` a maker), or when it asked the run this one took over
    # from (`inherited`); a message it sent later moves that to the message.
    start = run.started_at or None
    asked: dict[str, float | None] = {run.parent_run_id: start} if run.parent_run_id else {}
    asked |= {asker: run.inherited_asks.get(asker, start) for asker in run.asked_by}
    for event in reg.read_messages(run.run_id):
        if event.to_run != run.run_id or event.fyi or event.reports:
            continue
        said_by = event.for_run or event.from_run
        if said_by == reg.PERSON_SENDER and event.text != reg.RESUME_BRIEF and run.parent_run_id:
            said_by = run.parent_run_id  # the person's words to it: its parent hears the outcome
        if said_by in run.askers:
            asked[said_by] = event.at
    return asked


def _reported(run_id: str, *, to: str, since: float) -> bool:
    """A hand-back to run `to`, or to a run that took over from it (`chain`), carried `run_id`'s report at or after `since`."""
    return any(event.to_run == target and run_id in event.reports and (event.at or 0.0) >= since
               for target in chain(to) for event in reg.read_messages(target))


def _last_ask(run: reg.AgentRun, asker: str) -> str:
    """The first line of what `asker` last asked `run`: its task when `asker` sent it nothing (its parent, a run it was
    spawned for or took over the asks of)."""
    said = next((event.text for event in reversed(reg.read_messages(run.run_id))
                 if event.to_run == run.run_id and not event.fyi and not event.reports and (event.for_run or event.from_run) == asker), None)
    return _first_line(content(said) if said is not None else run.task)


def _first_line(text: str) -> str:
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return line if len(line) <= QUOTE_MAX else f"{line[:QUOTE_MAX - 1]}…"


def tag(run_id: str) -> str:
    """A run as a card names it: `<role>-<run8>`, its id alone when it has no record."""
    run = reg.get_run(run_id)
    return run_id[:8] if run is None else _tag(run)


def _tag(run: reg.AgentRun) -> str:
    return f"{run.agent or run.name}-{run.run_id[:8]}"


def _signature(run: reg.AgentRun) -> str:
    """A run as a heading names it: `<role> (run <8>)`."""
    return f"{run.agent or run.name} (run {run.run_id[:8]})"


def _working(run: reg.AgentRun) -> bool:
    return run.status in _AT_WORK and run.process_running()


def report_to(run: reg.AgentRun) -> str:
    """Whom `run`'s final message reaches by itself: each run it owes its report (`owed`, at the end of its chain), the
    person for a run nobody launched; "" when every ask was answered."""
    if (due := owed(run)):
        return ", ".join(dict.fromkeys(tag(live(asker)) for asker in due))
    return "" if boss(run) else OPERATOR


def card(run: reg.AgentRun) -> str:
    """Where `run` stands as a turn of it starts: whom it works for and on what, its agents and the conversation's still at
    work, the idle ones of a role it can hand a task to, and where its final message goes."""
    first = boss(run)
    works_for = OPERATOR if first is None else f"{tag(first)} — « {_last_ask(run, run.parent_run_id or first)} »"
    lines = [f"{CARD_HEADING}\nYou are {_tag(run)}. You work for: {works_for}."]
    also = [asker for asker in owed(run) if asker != run.parent_run_id and live(asker) != first]
    if also:
        lines.append("Also asked by: " + "; ".join(f"{tag(live(asker))} — « {_last_ask(run, asker)} »" for asker in also) + ".")
    conversation = [other for other in reg.stored_tree(reg.conversation_of(run.parent_run_id or run.run_id)) if other.run_id != run.run_id]
    mine = [other for other in conversation if _working(other) and (live(other.parent_run_id or '') == run.run_id or run.run_id in {live(asker) for asker in other.asked_by})]
    lines.append("Your agents still at work: " + (", ".join(_tag(other) for other in mine) or "none") + ".")
    others = [other for other in conversation if _working(other) and other not in mine and other.run_id not in run.askers][:CARD_RUNS_MAX]
    if others:
        lines.append("At work in this conversation: " + "; ".join(f"{_tag(other)} — « {_first_line(other.task)} »" for other in others) + ".")
    idle = {other.agent: other for other in reversed(conversation) if other.agent and _reusable(other)}
    if idle:
        lines.append("Idle, to reuse: " + ", ".join(_tag(other) for other in sorted(idle.values(), key=lambda other: other.agent or "")) + ".")
    goes_to = report_to(run)
    lines.append(f"When this turn ends your final message goes to {goes_to} by itself: never send it, never ack a report." if goes_to
                 else "Every ask of you is answered: when this turn ends your final message goes to nobody; never ack a report.")
    return "\n".join(lines)


def context_full(run: reg.AgentRun) -> bool:
    """Its conversation holds `CONTEXT_NEAR_FULL` of its context window or more, as its CLI last reported."""
    return bool(run.context_window) and (run.context_tokens or 0) >= CONTEXT_NEAR_FULL * (run.context_window or 0)


def _reusable(run: reg.AgentRun) -> bool:
    """An ended run a spawn of its role would hand its task to (`spawn_line.Spawn._misfit`, all but its checks against the
    spawn's own revision, provider and permissions)."""
    return (run.status in REUSABLE and run.successor is None and run.replaced_by is None and not context_full(run)
            and not _working(run) and reg.model_spoke_in(run.run_id))


def heading(event: AgentEvent, run: reg.AgentRun, *, mid_turn: bool) -> str | None:
    """The line a message to `run` opens with: who wrote it and what that writer is to `run` (`relation`). The person's
    words and the launcher's own open a turn bare (they ARE its prompt) and carry `inbox_hook.HEADING` mid-turn."""
    origin = reg.get_run(event.from_run) if event.from_run and event.from_run != reg.PERSON_SENDER else None
    if origin is None:
        who = OPERATOR if event.from_run in (None, reg.PERSON_SENDER) else f"Galaius on this computer ({event.from_run})"
        return HEADING.format(who=who) if mid_turn else None
    kind = event.relation or relation(origin, run, event.text, reports=event.reports)
    if kind in (Relation.REPORT, Relation.REPORT_ASKING):
        reporters = [reporter for reporter in (reg.get_run(run_id) for run_id in event.reports) if reporter is not None] or [origin]
        names = ", ".join(_signature(reporter) for reporter in reporters)
        one = len(reporters) == 1
        said = f"report from {names} on what you asked it" if one else f"reports from {names} on what you asked them"
        if kind is Relation.REPORT:
            return f"[Galaius: {said}. {'Read it' if one else 'Read them'}; no reply.]"
        return f"[Galaius: {said}. {'It needs' if one else 'One needs'} your answer: answer it in this turn.]"
    if kind is Relation.ASKER:
        return f"[Galaius: message from {_signature(origin)}, who you work for. Answer it in this turn.]"
    return (f"[Galaius: message from {_signature(origin)}. You don't work for it: reply only if it asks a question; "
            f"your report still goes to {report_to(run) or (tag(first) if (first := boss(run)) else OPERATOR)}.]")
