"""Messages to a run between its turns, delivered by the long-lived launcher holding the run's next
child (the PC's runner) instead of a dispatcher process.

The dispatcher path (`deliver_message`) starts a Python process (~1.3 s), reads the policy from the
server cold (~1.3 s) and starts `claude --resume` (~3 s) before the model reads the message: a
follow-up from the web waited ~10 s for its first words (owner PC, 2026-10-08). Here the runner
queues the message exactly as that path does (`queue_message`), claims it, and hands it to a resumed
child started when the run's previous turn ended (`FollowUpWarm`, one per run for the runs whose turn
ended last), or to one it starts at once. What it does not deliver itself (the run is working,
another message is ahead, a fenced run, a CLI that cannot start ahead, any failure) goes to the
dispatcher as before. A runner that stops loses nothing and sends nothing twice: at its next start
(`recover`) a message its child never read goes back in the queue, one it read is settled from the
run's stream, and the dispatcher does either.
"""

import asyncio
import logging
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import BinaryIO
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from galaius.agents import agent_queue
from galaius.agents import registry as reg
from galaius.agents.events import stream_lines
from galaius.agents.messaging import Delivery, policy_for_continuation, queue_message, start_dispatcher
from galaius.agents.providers import provider_for
from galaius.agents.run import ResumeLaunch, mirror_while_alive, record_turn_stderr
from galaius.agents.warm import WarmStart, end_waiting

log = logging.getLogger(__name__)


class FollowUpWarm(WarmStart):
    """One resumed child per run, for the runs whose turn ended last, each kept `hold` seconds after
    that turn ended (a follow-up later than that starts its child when it arrives)."""

    capacity: int = 2
    hold: float | None = 1800.0
    #: Started as soon as the turn ends: no other CLI is starting then, and a reply read in seconds
    #: is followed up in seconds.
    delay: float = 0.0


class Ready(BaseModel):
    """A follow-up's turn ready to receive its message: how it was started, and its child."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    launch: ResumeLaunch
    process: asyncio.subprocess.Process


class Claimed(BaseModel):
    """A queued message this launcher delivers: its queue item, its text, the turn it begins."""

    model_config = ConfigDict(frozen=True)

    item: agent_queue.QueueItem
    text: str
    lifecycle_token: str


class FollowUps(BaseModel):
    """The next turns of the runs this launcher started, each child started before its message.
    Lives on one event loop (the runner's agent loop): every method but `recover` and `recent` runs there."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    warm: WarmStart = Field(default_factory=FollowUpWarm)

    _tasks: set[asyncio.Task] = PrivateAttr(default_factory=set)
    #: Each child's error output, kept (redacted) only when its turn fails; gone with the child.
    _stderr: WeakKeyDictionary[asyncio.subprocess.Process, BinaryIO] = PrivateAttr(default_factory=WeakKeyDictionary)

    async def send(self, run_id: str, text: str, *, environment: Mapping[str, str], message_id: str | None = None) -> Delivery:
        """`text` from the person to run `run_id`, queued durably, then started here when the run
        is between turns, else by its dispatcher; the delivery says which queue item carries it.
        The turn's child is claimed (or started) while the message is being queued, and once it is
        queued the hand-over finishes even when the caller stops waiting for it. A message already
        queued under `message_id` is left to whoever took it then (`queue_message`)."""
        run = await asyncio.to_thread(reg.get_run, run_id)
        readying = asyncio.create_task(self._child(run, environment)) if run is not None and self._resumable(run) and not run.process_running() else None
        try:
            delivery = await asyncio.to_thread(queue_message, run_id, text, sender="operator", environment=environment, message_id=message_id)
        except BaseException:
            if (child := await self._ready(readying)) is not None:
                end_waiting(child.process)
            raise
        child = await self._ready(readying)
        if delivery.state != "queued" or delivery.repeated:
            if child is not None:
                end_waiting(child.process)
            return delivery
        handing = asyncio.create_task(self._deliver(delivery, child, environment))
        self._keep(handing, run_id, environment)
        return await asyncio.shield(handing)

    def follow(self, run_id: str, process: asyncio.subprocess.Process, *, environment: Mapping[str, str]) -> None:
        """Once run `run_id`'s turn (`process`, started elsewhere) has ended, keep its next turn's child started."""
        async def ended() -> None:
            await process.wait()
            await self.ready(run_id, environment=environment)
        self._keep(asyncio.create_task(ended()))

    async def ready(self, run_id: str, *, environment: Mapping[str, str]) -> None:
        """Once a turn of run `run_id` ended: a message still waiting (sent while it worked, and not
        handed to that turn) opens the next turn now, never left waiting for a dispatcher that may
        not run; else keep its next turn started ahead, when it can be (`_resumable`), while no
        other turn of it begins (another sender's message, through its dispatcher)."""
        run = await asyncio.to_thread(reg.get_run, run_id)
        if run is None:
            return
        if (waiting := await asyncio.to_thread(self._waiting, run_id)) is not None:
            child = None
            if self._resumable(run):
                try:
                    child = await self._child(run, environment)
                except (OSError, ValueError, RuntimeError) as error:  # its dispatcher starts the turn instead
                    log.warning("no child for the follow-up waiting on %s: %s", run_id[:8], error)
            if (delivery := await self._deliver(waiting, child, environment)).state == "error":
                log.warning("follow-up waiting on %s not delivered: %s", run_id[:8], delivery.text)
            return
        if not self._resumable(run):
            return
        try:
            launch = await asyncio.to_thread(self._launch, run, environment)
        except (OSError, ValueError, RuntimeError) as error:
            log.info("no follow-up child for %s: %s", run_id[:8], error)
            return

        def still() -> bool:
            current = reg.get_run(run_id)
            return current is not None and current.lifecycle_token == run.lifecycle_token and not current.process_running()

        self.warm.prepare(run_id, lambda: launch.key(reg.get_run(run_id) or run), lambda _: self._started(launch), run_id=run_id, still=still)

    def recover(self, runs: Iterable[reg.AgentRun], *, environment: Mapping[str, str]) -> tuple[str, ...]:
        """At the launcher's start, for every run among `runs` with a message still queued or in
        flight: one handed to a child that ended without a word goes back in the queue (that child
        ended with the launcher's pipe, or before reading its command line), unless a dispatcher of
        the run still runs (it settles its own); the run's dispatcher then delivers what is queued
        and settles what was read from the run's stream. The runs recovered."""
        recovered = []
        for run in runs:
            if not agent_queue.path(run.run_id).exists() or not any(item.state in {"pending", "running"} for item in agent_queue.items(run.run_id)):
                continue
            with reg.record_lock(run.run_id):
                current = reg.get_run(run.run_id)
                if current is not None and not current.process_running() and not agent_queue.dispatcher_running_locked(run.run_id):
                    for item in agent_queue.items_locked(run.run_id):
                        if item.state == "running" and not self._read(current, item):
                            agent_queue.release_locked(run.run_id, item.id)
            if (refused := start_dispatcher(run.run_id, environment)) is not None:
                log.warning("queued follow-up to %s: %s", run.run_id[:8], refused)
            recovered.append(run.run_id)
        return tuple(recovered)

    def recent(self, runs: Iterable[reg.AgentRun]) -> tuple[str, ...]:
        """Among `runs`, the ones whose next turn is worth starting ahead now: between turns, ended
        less than `hold` ago, the `capacity` latest."""
        now, hold = time.time(), self.warm.hold or float("inf")
        ended = [run for run in runs if run.finished_at is not None and now - run.finished_at < hold and not run.process_running() and self._resumable(run)]
        return tuple(run.run_id for run in sorted(ended, key=lambda run: run.finished_at or 0.0, reverse=True)[:self.warm.capacity])

    def close(self) -> None:
        self.warm.close()

    @staticmethod
    def _waiting(run_id: str) -> Delivery | None:
        """The first message queued for `run_id` while it is between turns and no dispatcher of it
        runs (one that does delivers it itself), as its delivery."""
        with reg.record_lock(run_id):
            run = reg.get_run(run_id)
            if run is None or run.working() or agent_queue.dispatcher_running_locked(run_id):
                return None
            item = agent_queue.first_active_locked(run_id)
            return Delivery.queued(run, item) if item is not None and item.state == "pending" else None

    @staticmethod
    def _resumable(run: reg.AgentRun) -> bool:
        """Its next turn can start before its message: a named role on a CLI that starts ahead, unfenced."""
        try:
            return bool(run.agent) and run.fence is None and provider_for(run.provider).starts_ahead
        except ValueError:
            return False

    @staticmethod
    def _read(run: reg.AgentRun, item: agent_queue.QueueItem) -> bool:
        """Whether the child `item` was handed to said anything after it (or `item` has no anchor)."""
        if item.raw_index is None:
            return True
        try:
            lines = stream_lines(reg.raw_events_path(run.run_id).read_text(encoding="utf-8", errors="replace"))[item.raw_index:]
        except OSError:
            return True  # nothing tells: the dispatcher's settling decides, as for any attempt
        provider = provider_for(run.provider)
        return any(provider.took_message(line) for line in lines)

    @staticmethod
    def _launch(run: reg.AgentRun, environment: Mapping[str, str]) -> ResumeLaunch:
        """The next turn of `run` under the current policy, as its dispatcher would start it."""
        provider = provider_for(run.provider)
        _, criterion, model, reasoning = policy_for_continuation(run, provider, environment)
        return ResumeLaunch.of(provider, run, run.provider_session_id or run.run_id, model=model, criterion=criterion, reasoning=reasoning, environment=environment)

    async def _child(self, run: reg.AgentRun, environment: Mapping[str, str]) -> Ready:
        """The next turn of `run` and its child: the one started ahead when it is still fresh, else one started now."""
        launch = await asyncio.to_thread(self._launch, run, environment)
        held = self.warm.claim(run.run_id, await asyncio.to_thread(launch.key, run))
        self.warm.forget(run.run_id)  # none is started for a turn about to begin
        log.info("follow-up to %s: %s", run.run_id[:8], "its child started ahead" if held is not None else "no fresh child held, one starts now")
        return Ready(launch=launch, process=held.process if held is not None else await self._started(launch))

    @staticmethod
    async def _ready(readying: asyncio.Task[Ready] | None) -> Ready | None:
        if readying is None:
            return None
        try:
            return await readying
        except Exception as error:  # the dispatcher starts the turn instead, or says why not in the run
            log.warning("no child for a follow-up: %s", error, exc_info=not isinstance(error, (OSError, ValueError, RuntimeError)))
            return None

    async def _deliver(self, delivery: Delivery, child: Ready | None, environment: Mapping[str, str]) -> Delivery:
        try:
            started = child is not None and await self._start(delivery, child, environment)
        except Exception as error:  # queued already: whatever stopped it here, its dispatcher delivers it or says why in the run
            log.warning("follow-up to %s left to its dispatcher: %s", delivery.run_id[:8], error, exc_info=not isinstance(error, (OSError, ValueError, RuntimeError)))
            started = False
        if started or (refused := await asyncio.to_thread(start_dispatcher, delivery.run_id, environment)) is None:
            return delivery
        return Delivery(state="error", text=f"ERROR: {refused}", run_id=delivery.run_id)

    async def _start(self, delivery: Delivery, child: Ready, environment: Mapping[str, str]) -> bool:
        """Start the turn carrying `delivery` on `child`; False (the child ended) when it is the dispatcher's to deliver."""
        run_id, launch, process = delivery.run_id, child.launch, child.process
        claimed = await asyncio.to_thread(self._claim, delivery, process.pid, launch)
        if claimed is None:
            end_waiting(process)
            return False
        try:
            await launch.hand(process, claimed.text)
        except BaseException:  # it never got the message whole: ended, and the message back in the queue
            end_waiting(process)
            await asyncio.to_thread(self._release, run_id, claimed.item.id)
            raise
        self._keep(asyncio.create_task(self._turn(run_id, claimed, process, environment)), run_id, environment)
        return True

    async def _started(self, launch: ResumeLaunch) -> asyncio.subprocess.Process:
        stderr = tempfile.TemporaryFile()
        process = await launch.start_ahead(stderr)
        self._stderr[process] = stderr
        return process

    def _keep(self, task: asyncio.Task, run_id: str | None = None, environment: Mapping[str, str] | None = None) -> None:
        """Hold `task` to its end. One serving run `run_id` that fails hands the run to its
        dispatcher: a message its child never read goes back in the queue, the rest is settled."""
        self._tasks.add(task)

        def ended(task: asyncio.Task) -> None:
            self._tasks.discard(task)
            if task.cancelled() or (error := task.exception()) is None:
                return
            log.error("follow-up to %s failed: %s", (run_id or "?")[:8], error, exc_info=error)
            if run_id is not None and environment is not None:
                self._keep(asyncio.create_task(asyncio.to_thread(self._recover_one, run_id, environment)))

        task.add_done_callback(ended)

    def _recover_one(self, run_id: str, environment: Mapping[str, str]) -> None:
        if (run := reg.get_run(run_id)) is not None:
            self.recover([run], environment=environment)

    @staticmethod
    def _claim(delivery: Delivery, pid: int, launch: ResumeLaunch) -> Claimed | None:
        """Under the run's lock: claim the delivery's item when the run is between turns and nothing
        is queued ahead of it, and begin the turn of process `pid`."""
        run_id = delivery.run_id
        with reg.record_lock(run_id):
            run = reg.get_run(run_id)
            if run is None or run.working():
                return None
            item = agent_queue.claim_locked(run_id, delivery.queue_id or "")
            if item is None:
                return None
            message = reg.message_for(run_id, item.message_id)
            current = reg.begin_turn_locked(run_id, pid=pid, model=launch.model, requested_criterion=launch.criterion, reasoning=launch.reasoning) if message is not None else None
            if message is None or current is None or current.lifecycle_token is None:
                agent_queue.release_locked(run_id, item.id)  # its dispatcher fails it as it fails any such item
                return None
            return Claimed(item=item, text=message.text, lifecycle_token=current.lifecycle_token)

    @staticmethod
    def _release(run_id: str, item_id: str) -> None:
        with reg.record_lock(run_id):
            agent_queue.release_locked(run_id, item_id)

    async def _turn(self, run_id: str, claimed: Claimed, process: asyncio.subprocess.Process, environment: Mapping[str, str]) -> None:
        """Watch the turn to its end as the dispatcher would (its stream mirrored, its ending and its
        item's outcome recorded), then start the run's next child."""
        mirror = asyncio.create_task(mirror_while_alive(run_id, lambda: process.returncode is None))
        code = await process.wait()
        await mirror
        await asyncio.to_thread(self._settle, run_id, claimed, process.pid, code, self._stderr.pop(process, None))
        await self.ready(run_id, environment=environment)

    @staticmethod
    def _settle(run_id: str, claimed: Claimed, pid: int, code: int, stderr: BinaryIO | None) -> None:
        if stderr is not None:
            if code:
                record_turn_stderr(run_id, stderr)
            stderr.close()
        reg.finish(run_id, exit_code=code, expected_pid=pid, expected_lifecycle_token=claimed.lifecycle_token)
        agent_queue.settle(run_id, claimed.item, code)
