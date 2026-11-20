"""`agents search`, `read`, `send`, `retract`, `compact` and `resume`: find a conversation and say something into a run's."""

import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, TextIO
from cyclopts import Parameter
from galaius_core import MachineRefusal, RefusedLine, refusal_of, IMAGE_TYPES, Coded
from galaius.agents import messaging
from galaius.agents import agent_queue
from galaius.agents.catalog import relations
from galaius.agents import registry as reg
from galaius.agents.conversations import ConversationAnswers
from galaius.agents.spawn_line import SpawnLine
from galaius.cli.commands.agents.app import agents_app


def said_refused(sentence: str, refusal: MachineRefusal | None, *, file: TextIO | None = None) -> None:
    """A refusal said to whoever runs the command: its `ERROR:` line, then, typed, its reason as one `RefusedLine`."""
    print(f"ERROR: {sentence}" if not sentence.startswith("ERROR: ") else sentence, file=file)
    if refusal is not None:
        print(RefusedLine(refusal=refusal).model_dump_json(), file=file)


@agents_app.command(name="search")
def agents_search(query: str, limit: int = 20, project: str | None = None, since: str | None = None, every_project: bool = False) -> None:
    """Search the conversations this computer holds (galaius's runs and each agent CLI's saved
    sessions) for every word of QUERY, as the `conversation_search` tool does, in the same scope."""

    answers = ConversationAnswers(environment=dict(os.environ), cwd=os.getcwd(), read_call="galaius agents read {id} --around {line}")
    print(answers.search(query, limit, project, since, every_project))


@agents_app.command(name="read")
def agents_read(conversation: str, around: int | None = None, limit: int = 50, every_project: bool = False) -> None:
    """Read one conversation `agents search` found, LIMIT lines around line AROUND (else its last), as
    the `conversation_read` tool does."""

    print(ConversationAnswers(environment=dict(os.environ), cwd=os.getcwd()).read(conversation, around, limit, every_project))


@agents_app.command(name="retract")
def agents_retract(run_id: str, message_id: str) -> None:
    """Take back message MESSAGE_ID its person sent run RUN_ID: never read when it still waits; its turn stopped when an
    agent is reading it; the run's next turn told. Prints `Withdrawn: <unread|stopped|after|unknown>.` Only the person
    takes a message back: an agent asking is refused."""

    by = reg.caller_stop_actor(os.environ)
    if by.kind == "agent":
        print("ERROR: only the person who sent a message takes it back.", file=sys.stderr)
        raise SystemExit(1)
    print(f"Withdrawn: {messaging.retract(run_id, message_id, by=by)}.")


@agents_app.command(name="send")
def agents_send(run_id: str, message: str, *, fyi: Annotated[bool, Parameter(name="--fyi")] = False,
                cross_project: Annotated[bool, Parameter(name="--cross-project")] = False,
                image_paths: Annotated[list[Path] | None, Parameter(name="--image")] = None,
                message_id: Annotated[str | None, Parameter(name="--message-id")] = None) -> None:
    """Send a message to a running agent — it answers with its full context intact.

    How the VS Code panel lets you join the conversation instead of only watching: the agent
    resumes its own session, so it remembers what it already did and its reply lands in the same
    transcript the panel shows.

    --fyi: a one-way note (a report, an ack, a heads-up): it reads it, and its turns never report back to you for it.
    --cross-project: the person's deliberate act, at his own terminal (never from an agent run's process): a run working
    for another project than the sending run's is sent to anyway (`relations.foreign_project` refuses it otherwise).
    --image PATH (repeatable): an image the message shows, kept with the run it reaches; the agent sees it (an image
    input when its CLI takes one, else a line naming its file). MESSAGE may then be "".
    --message-id ID: the one id the message keeps end to end (the sender's own, e.g. a web outbox row): its transcript
    entry is recorded under it, and the same id sent again records and enqueues nothing.
    """
    if message_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", message_id):
        print("ERROR: --message-id takes 1 to 160 letters, digits, '.', '_', ':' or '-'.")
        raise SystemExit(1)


    if reg.resolve_run_id(run_id) is None and (waiting := SpawnLine.here().describe(run_id)) is not None:
        print(f"ERROR: {run_id} has not started ({waiting}); send once it runs.")
        raise SystemExit(1)
    if (refused := messaging.send_refusal(run_id, cross_project=cross_project)) is not None:
        print(f"ERROR: {refused}")
        raise SystemExit(1)
    try:   # kept with the run the message reaches (`relations.live`), which opens them
        media = reg.RunMedia.keep_files(relations.live(target), image_paths) if image_paths and (target := reg.resolve_run_id(run_id)) else ()
    except (OSError, ValueError) as error:
        print(f"ERROR: --image takes an image file ({', '.join(kind.removeprefix('image/').upper() for _, kind, _ in IMAGE_TYPES)}): {error}")
        raise SystemExit(1)
    try:
        # A person's send waits a bounded time for the run (`messaging.PERSON_PATIENCE`) and watches its turn start without
        # its lock: the web runs this command and gives it 60 s, past which it tells him « send timed out » with no reason
        # (2026-10-26: his run's record lock was wedged since 09:35).
        delivery = messaging.deliver_message(run_id, message, message_id=message_id, turn=agent_queue.MessageTurn(fyi=fyi, media=media))
        refused = messaging.wait_for_start(delivery)
    except reg.RecordBusy as busy:
        print(f"ERROR: {busy}; the message was not recorded, send it again.")
        raise SystemExit(1) from None
    if refused is not None:
        print(f"ERROR: the resumed turn did not start: {refused}")
        raise SystemExit(1)
    waiting = SpawnLine.here().waits(delivery.run_id) if delivery.state == "queued" else None
    if delivery.state == "error":
        said_refused(delivery.text, delivery.refusal)
        raise SystemExit(1)
    print(f"{delivery.text} Its turn: {waiting}" if waiting else delivery.text)


@agents_app.command(name="compact")
def agents_compact(run_id: str) -> None:
    """Compact an agent's conversation: its CLI replaces what was said so far with its own summary,
    freeing its context. A turn of its own: a run at work compacts once its turn ends."""

    delivery = messaging.deliver_message(run_id, "", turn=agent_queue.CompactionTurn())
    if (refused := messaging.wait_for_start(delivery)) is not None:
        print(f"ERROR: the compaction did not start: {refused}")
        raise SystemExit(1)
    print(delivery.text)
    if delivery.state == "error":
        raise SystemExit(1)


@agents_app.command(name="resume")
def agents_resume(
    run_id: list[str] | None = None, *, all_cut: bool = False, hours: float = 24.0,
) -> None:
    """Pick up agents whose run was CUT — a usage limit, an OOM kill, a reboot, a closed laptop.

    With no argument it only LISTS them (a cut run is one whose process is gone with nothing having
    recorded how it ended). `--all-cut` resumes every one it lists; naming ids resumes those.

    Resuming also drops the quota refusals remembered under an exhausted window: they were the
    reason half of these runs stopped, and they would refuse the same models again.
    """

    # Only a LISTING pays for the whole store: deriving every run re-reads every transcript, which
    # took over a minute on a real history and made a named resume time out (visual-critic r2).
    cut = [] if run_id else reg.interrupted_runs(within_hours=hours)
    targets = list(run_id or ()) or ([run.run_id for run in cut] if all_cut else [])
    if not targets:
        if not cut:
            print(f"Nothing cut in the last {hours:g} h.")
            return
        for run in cut:
            stopped = run.finished_at or run.started_at
            print(f"  {run.run_id[:8]}  {run.agent or run.name:<20} stopped {(time.time() - stopped) / 60:.0f} min ago")
        print(f"\nResume them with  galaius agents resume --all-cut  (or by id).")
        return
    refused = False
    for identity in targets:
        # A run at one of his subscription caps is refused before anything is queued, typed (`said_refused`) for the
        # caller to word with its reset time.
        if (run := reg.get_run(reg.resolve_run_id(identity) or identity)) is not None:
            try:
                run.prepare_continue()
            except Coded as refusal:
                said_refused(f"{identity[:8]}: {refusal}", refusal_of(refusal), file=sys.stderr)
                refused = True
                continue
        delivery = messaging.deliver_message(identity, reg.RESUME_BRIEF)
        if delivery.state == "error":
            said_refused(f"{identity[:8]}: {delivery.text.removeprefix('ERROR: ')}", delivery.refusal, file=sys.stderr)
        else:
            print(f"{identity[:8]}: {delivery.text}")
        refused = refused or delivery.state == "error"
    if refused:
        raise SystemExit(1)
