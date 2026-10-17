"""Who a run works for, computed from the run records and written into what it reads.

Every turn opens with where its run stands (`card`), and every message it reads with who wrote it and what that writer
is to it (`heading`): the message's kind is inferred from the records (`relation`), never chosen by its sender. Owner
2026-10-17: « it seems that everything is not clear enough for the agents » — two runs handing back to each other for
good, reports read as asks and acknowledged (each acknowledgement a turn), verdicts relayed by hand, a hand-off's new
run unknown to the runs still writing to the old one."""

import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent, Relation, ending_of
from galaius.inbox_hook import HEADING, OPERATOR

#: Share of its context window past which a run is not handed a new task (`spawn_line.Spawn._misfit`): a new run of its
#: role started under its parent takes over from it (`succeed`).
CONTEXT_NEAR_FULL = 0.8
#: Compactions after which a run hands its work to a fresh run of its role: a run of its role it spawns from then on
#: continues it (`continues`), whatever client spawned it.
HANDOFF_COMPACTIONS = 2
#: The other runs at work a card names, newest first.
CARD_RUNS_MAX = 8
#: The longest first line a card quotes.
QUOTE_MAX = 100
#: Successors followed before a chain is read as ended (a record edited by hand into a loop).
_CHAIN_MAX = 32
#: The line a turn card (`card`) opens with.
CARD_HEADING = "[Galaius: where you stand, from the run records]"
#: A line opening on a failed verdict, bare or after a label of up to four words ending on a colon or a dash (« Verdict:
#: FAIL », « Review round 2 — FAIL », « STATE: BLOCKED »): a report whose first or last line (its closing stamp) is one
#: still needs its reader (`needs_answer`). « C2 PASS (was FAIL) » is none.
#: Linear on any line: a label word opens on a letter or digit and holds a dash only between word characters, so no
#: run of dashes splits several ways (2026-10-17 review: « "-" * 2000 » took 24 s on a wider pattern).
_LABEL_WORD = r"\w[\w#.]*(?:-[\w#.]+)*"
_FAILED = re.compile(rf"\W*(?:{_LABEL_WORD}(?:\s+{_LABEL_WORD}){{0,3}}\s*[:—–-])?[\s*_]*(?:BLOCKED|FAIL(?:-AT-CAP)?|REJECTED)\b")
#: Where an agent's own words start in a message it sent (`registry.AgentRun.handoff_header`).
_CONTENT = "\n[Agent content]\n"
#: What a run at work is doing, as its record says (a queued message alone is not work a card names).
_AT_WORK = frozenset({"starting", "running", "waiting"})
#: Ended runs whose session can take a next turn (every ending but a run cancelled before it started): a spawn of its
#: role hands it the task (`spawn_line.Spawn._misfit`).
REUSABLE: frozenset[str] = reg.TERMINAL_STATUSES - {"cancelled"}


@dataclass
class _Read:
    """The records one decision read (`one_read`): each run by id, and each parent's children once read."""

    runs: dict[str, reg.AgentRun | None] = field(default_factory=dict)
    children: dict[str, list[reg.AgentRun]] | None = None


_READ: ContextVar[_Read | None] = ContextVar("relations_read", default=None)


@contextmanager
def one_read() -> Iterator[None]:
    """Read each run record once for what runs inside (a decision, a card): one run's relations climb the same records
    hundreds of times (2026-10-17: 656 reads for one card)."""
    if _READ.get() is not None:
        yield
        return
    token = _READ.set(_Read())
    try:
        yield
    finally:
        _READ.reset(token)


def _get(run_id: str) -> reg.AgentRun | None:
    if (read := _READ.get()) is None:
        return reg.get_run(run_id)
    if run_id not in read.runs:
        read.runs[run_id] = reg.get_run(run_id)
    return read.runs[run_id]


def _children(run_id: str) -> list[reg.AgentRun]:
    """The runs `run_id` spawned, as stored, oldest first; the run folder read once per decision (`one_read`)."""
    read = _READ.get()
    index = read.children if read is not None else None
    if index is None:
        index = {}
        for stored in sorted(reg._stored_records(), key=lambda stored: stored.started_at or 0.0):
            if stored.parent_run_id:
                index.setdefault(stored.parent_run_id, []).append(stored)
        if read is not None:
            read.children = index
    return index.get(run_id, [])


def outgrown(run: reg.AgentRun) -> bool:
    """It compacted `HANDOFF_COMPACTIONS` times, or its context is near full (`context_full`): its work goes to a new run."""
    return run.compactions >= HANDOFF_COMPACTIONS or context_full(run)


@one_read()
@one_read()
def continues(run: reg.AgentRun) -> bool:
    """Whether `run` continues its parent's work: its parent handed it over (`succeed`), or `run` is of its parent's role
    and its parent, outgrown (`outgrown`), is no longer at work (`_hands_off`) — how a compacted run hands off, in one run
    or several over time, `--continues` named or not. A parent still at work keeps its helpers. 2026-10-17: such
    hand-offs went unrecorded; each continuation's boss read as its own predecessor."""
    parent = _get(run.parent_run_id) if run.parent_run_id else None
    return parent is not None and (parent.successor == run.run_id or (parent.agent == run.agent and _hands_off(parent)))


@one_read()
def continuations(run_id: str) -> list[str]:
    """The runs continuing run `run_id`'s work (`continues`), oldest first."""
    return [child.run_id for child in _children(run_id) if continues(child)]


def _hands_off(run: reg.AgentRun) -> bool:
    """`run` outgrew its context (`outgrown`) and ended with no successor recorded: the runs of its role it spawned
    continue it."""
    return run.successor is None and outgrown(run) and not _working(run)


def _successor(run: reg.AgentRun) -> str | None:
    """The run that took over from `run`: the one recorded (`succeed`), else the newest run continuing it (`continues`)."""
    if not _hands_off(run):
        return run.successor
    return next((child.run_id for child in reversed(_children(run.run_id)) if child.agent == run.agent), None)


@one_read()
def chain(run_id: str) -> tuple[str, ...]:
    """`run_id` and each run that took over from the one before it (`_successor`), oldest first."""
    ids = [run_id]
    while len(ids) < _CHAIN_MAX and (run := _get(ids[-1])) is not None and (heir := _successor(run)) and heir not in ids:
        ids.append(heir)
    return tuple(ids)


@one_read()
def live(run_id: str) -> str:
    """The run at the end of `run_id`'s chain (`chain`): the one a report, a hand-back or a message for it reaches."""
    return chain(run_id)[-1]


def inherited(predecessor: reg.AgentRun, *, parent: str | None) -> dict[str, float]:
    """What a run started under `parent` to take over from `predecessor` owes from its start (`AgentRun.inherited_asks`):
    each ask the predecessor still owed (`open_asks`), at its own time. Never one its new parent asks anyway (the task it
    starts on), nor the predecessor itself (a run handing off its own work: `boss`)."""
    return {asker: at or predecessor.started_at or 0.0 for asker, at in _open(predecessor).items()
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


@one_read()
def boss(run: reg.AgentRun) -> str | None:
    """The run `run` works for first: the parent of the first run of its lineage (`lineage`) at the end of its chain
    (`live`); None for a run the person launched."""
    return _working_for(run)[0]


def _working_for(run: reg.AgentRun) -> tuple[str | None, reg.AgentRun]:
    """`boss`, and the run of `run`'s lineage that boss asked: `run` itself, or the first run whose work it continues."""
    first, line = _climb(run)
    return first, line[-1]


@one_read()
def lineage(run: reg.AgentRun) -> tuple[reg.AgentRun, ...]:
    """`run` and each run whose work it continues (`continues`), nearest first."""
    return _climb(run)[1]


def _climb(run: reg.AgentRun) -> tuple[str | None, tuple[reg.AgentRun, ...]]:
    line = [run]
    while len(line) < _CHAIN_MAX and continues(line[-1]) and (above := _get(line[-1].parent_run_id or "")) is not None \
            and above.run_id not in {link.run_id for link in line}:
        line.append(above)
    parent = line[-1].parent_run_id
    return (live(parent) if parent and parent not in {link.run_id for link in line} else None), tuple(line)


def _commands(run_id: str, other: reg.AgentRun) -> bool:
    """Whether run `run_id` is `other`'s boss or above it (`boss`): the launch tree outranks who wrote first. 2026-10-17:
    a continuation's status note made it its coordinator's asker; the coordinator's next order then read as a report and
    its every hand-back waited on the coordinator, never ending."""
    seen = {other.run_id}
    while (up := boss(other)) is not None and up not in seen and len(seen) < _CHAIN_MAX:
        if up == run_id:
            return True
        seen.add(up)
        if (other := _get(up)) is None:
            return False
    return False


def content(text: str) -> str:
    """The sender's own words in a recorded message: past its provenance header when it carries one."""
    return text.split(_CONTENT, 1)[1] if text.startswith("[Interact agent provenance]") and _CONTENT in text else text


def needs_answer(text: str) -> bool:
    """A report still needing its reader: it opens on a failed verdict, or its closing line names one or asks."""
    lines = [line.strip() for line in content(text).splitlines() if line.strip()]
    return bool(lines) and (_failed(lines[0]) or _failed(lines[-1]) or lines[-1].endswith("?"))


def _failed(line: str) -> bool:
    """Whether `line` opens on a failed verdict (`_FAILED`); a verdict opens its line, so only its start is read."""
    return _FAILED.match(line[:QUOTE_MAX]) is not None


@one_read()
def works_for(run: reg.AgentRun) -> frozenset[str]:
    """The runs `run` answers, at the end of their chains (`live`): its boss and every other run that asked it (`_asks`)."""
    return frozenset(live(asker) for asker in _asks(run)) - {run.run_id}


def answers(sender: reg.AgentRun, recipient: reg.AgentRun) -> bool:
    """Whether a message from `sender` answers what `recipient` asked it: `sender` works for `recipient` (`works_for`),
    and when each works for the other, `recipient`'s ask is the one still owed (`owed`)."""
    if recipient.run_id not in works_for(sender):
        return False
    return sender.run_id not in works_for(recipient) or recipient.run_id in {live(asker) for asker in owed(sender)}


@one_read()
def relation(sender: reg.AgentRun, recipient: reg.AgentRun, text: str, *, reports: Sequence[str] = ()) -> Relation:
    """What `sender` is to `recipient` for a message saying `text` (a hand-back: carrying the final words of `reports`),
    from their records as they are now."""
    if answers(sender, recipient):
        asking = any(needs_answer(final_words(run_id)) for run_id in reports) if reports else needs_answer(text)
        return Relation.REPORT_ASKING if asking else Relation.REPORT
    return Relation.ASKER if sender.run_id in works_for(recipient) else Relation.OTHER


def final_words(run_id: str) -> str:
    """What run `run_id` last said: its ending's text, else its newest text."""
    ending = ending_of(events := reg.read_events(run_id), run_id)
    said = ending.text if ending is not None else next((event.text for event in reversed(events) if event.kind == "text"), "")
    return (said or "").strip()


@one_read()
def owed(run: reg.AgentRun) -> tuple[str, ...]:
    """The runs its ended turn reports to: each run it answers (`askers`) whose latest ask no hand-back has carried its
    report on yet (`_reported`). Its parent asked its task when it started, and asks again with each message it or the
    person sends; a run of its `asked_by` asked from its start (a spawn `for_run` it, a hand-off) and again with each message
    it sends. An automatic resume, the person's « Continuer », any hand-back (`reports`) or a one-way note (`fyi`) is no
    ask. An ask with no time stays owed. 2026-10-17: a report sent to a third run made it an asker for good, each later turn of the
    reporter woke it with an empty report (one coordinator: 9 such runs; a librarian: 4 empty turns; four runs looping
    every 3 min); a reused instance's every turn also woke the run that had spawned it for an earlier job."""
    return tuple(open_asks(run))


@one_read()
def open_asks(run: reg.AgentRun) -> dict[str, float | None]:
    """Each ask `owed` counts, with its time: none once another run took over (they moved to it, `inherited`)."""
    return {} if live(run.run_id) != run.run_id else _open(run)


def _open(run: reg.AgentRun) -> dict[str, float | None]:
    """Each ask of `run` no report has answered yet (`_reported`); never one of a run `run` took over itself."""
    return {asker: at for asker, at in _asks(run).items()
            if live(asker) != run.run_id and (at is None or not _reported(run.run_id, to=asker, since=at))}


def _asks(run: reg.AgentRun) -> dict[str, float | None]:
    """Each run it answers that asked it something, with when it last did (`owed`): first its boss (`boss`, for a run
    continuing its parent's work the boss of its lineage); never a run of its own lineage (`lineage`), nor one it is the
    boss of (`_commands`)."""
    # Each run it works for asked it from its start (a spawn `--for` a maker), or when it asked the run this one took over
    # from (`inherited`); a message it sent later moves that to the message.
    start = run.started_at or None
    top = _working_for(run)[1].parent_run_id
    asked: dict[str, float | None] = {top: start} if top else {}
    asked |= {asker: run.inherited_asks.get(asker, start) for asker in run.asked_by if asker not in asked}
    reached = {live(asker): asker for asker in asked}
    for event in reg.read_messages(run.run_id):
        if event.to_run != run.run_id or event.fyi or event.reports:
            continue
        said_by = event.for_run or event.from_run
        if said_by == reg.PERSON_SENDER and event.text != reg.RESUME_BRIEF:
            said_by = boss(run)  # the person's words to it: its boss hears the outcome
        if (asker := said_by if said_by in asked else reached.get(live(said_by or ""))) is not None:
            asked[asker] = event.at
    own = {link.run_id for link in lineage(run)}
    return {asker: at for asker, at in asked.items()
            if asker not in own and ((other := _get(asker)) is None or not _commands(run.run_id, other))}


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
    run = _get(run_id)
    return run_id[:8] if run is None else _tag(run)


def _tag(run: reg.AgentRun) -> str:
    return f"{run.agent or run.name}-{run.run_id[:8]}"


def _signature(run: reg.AgentRun) -> str:
    """A run as a heading names it: `<role> (run <8>)`."""
    return f"{run.agent or run.name} (run {run.run_id[:8]})"


def _working(run: reg.AgentRun) -> bool:
    return run.status in _AT_WORK and run.process_running()


@one_read()
def report_to(run: reg.AgentRun) -> str:
    """Whom `run`'s final message reaches by itself: each run it owes its report (`owed`, at the end of its chain), the
    person for a run nobody launched; "" when every ask was answered."""
    if (due := owed(run)):
        return ", ".join(dict.fromkeys(tag(live(asker)) for asker in due))
    return "" if boss(run) else OPERATOR


@one_read()
def card(run: reg.AgentRun) -> str:
    """Where `run` stands as a turn of it starts: whom it works for and on what, its agents and the conversation's still at
    work, the idle ones of a role it can hand a task to, and where its final message goes."""
    first, asked = _working_for(run)
    works_for = OPERATOR if first is None else f"{tag(first)} — « {_last_ask(asked, asked.parent_run_id or first)} »"
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
    return (run.status in REUSABLE and live(run.run_id) == run.run_id and run.replaced_by is None and not context_full(run)
            and not _working(run) and reg.model_spoke_in(run.run_id))


@one_read()
def heading(event: AgentEvent, run: reg.AgentRun, *, mid_turn: bool) -> str | None:
    """The line a message to `run` opens with: who wrote it and what that writer is to `run` (`relation`). The person's
    words and the launcher's own open a turn bare (they ARE its prompt) and carry `inbox_hook.HEADING` mid-turn."""
    origin = _get(event.from_run) if event.from_run and event.from_run != reg.PERSON_SENDER else None
    if origin is None:
        who = OPERATOR if event.from_run in (None, reg.PERSON_SENDER) else f"Galaius on this computer ({event.from_run})"
        return HEADING.format(who=who) if mid_turn else None
    kind = event.relation or relation(origin, run, event.text, reports=event.reports)
    if kind in (Relation.REPORT, Relation.REPORT_ASKING):
        reporters = [reporter for reporter in (_get(run_id) for run_id in event.reports) if reporter is not None] or [origin]
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
