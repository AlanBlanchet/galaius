"""Trait: watches over runs, usage, hand-backs and silences."""

import asyncio
from datetime import UTC, datetime
import websockets
from galaius.agents.turns.handback import EndWatch, SilenceWatch
from galaius.agents.turns.reviews import TurnReviewWatch
from galaius.config.settings import Config
from galaius.agents.limits.subscription_watch import SubscriptionWatch
from galaius.agents import registry as reg
import logging

from galaius.machine.connection import logs
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import EnrollmentChanged
from galaius.machine.connection.runs import Runs

logger = logging.getLogger(__package__)


class Watches(Runs):
    """Follow-up of the runs this machine hosts."""

    #: How often the usage lines of runs pinned to a subscription are read (caps applied) and moved
    #: usage goes to the server (`SubscriptionWatch`).
    subscription_seconds = 5
    #: How often this PC's run records are settled against their processes (`registry.settle_gone`).
    settle_seconds = 60
    #: How often this PC looks which runs ended, to hand their reports back (`handback.EndWatch`): one look reads only
    #: the records that moved (~0.15 s for ~5,000); at `settle_seconds` a finished agent's report waited up to a minute
    #: before its coordinator could answer the person.
    handback_seconds = 10
    #: How often, while connected, each login here is asked its usage by its own CLI (`SubscriptionWatch.ask`: 1-2 s,
    #: about 1 s of CPU, no model turn each).
    usage_ask_seconds = 120
    #: What watches this PC's logins' usage.
    _subscriptions: SubscriptionWatch

    async def _follow_up(self, config: MachineConfig) -> None:
        """At start: the messages to web runs a former runner left queued go to their dispatchers,
        and the runs whose turn ended last get their next turn started ahead (`FollowUps`)."""
        try:
            agents = await asyncio.to_thread(self._agents, self._current_config(config))
            if agents.run_agents:
                await agents.follow_up()
        except Exception:  # its own failure never stops the runner: a follow-up then starts as before
            logger.exception("queued follow-ups not recovered")

    async def _resume_paused(self, config: MachineConfig) -> None:
        """Every `settle_seconds`, while agents run here: the runs a cut stopped are resumed once what stopped them
        reopened (`MachineAgents.resume_paused`). Local and independent of the server, like `_settle_runs`: web-started
        and terminal-started runs alike."""
        while True:
            try:
                agents = await asyncio.to_thread(self._agents, self._current_config(config))
                if agents.run_agents:
                    await agents.resume_paused()
            except EnrollmentChanged:
                pass  # the connection loop reconnects with the new enrollment; the next pass reads it
            except Exception:  # one failed pass never stops the next
                logger.exception("resuming paused runs failed")
            await asyncio.sleep(self.settle_seconds)

    async def _watch_subscriptions(self, config: MachineConfig) -> None:
        """Every `subscription_seconds`, the usage lines runs pinned to a subscription appended
        (`SubscriptionWatch.heard`): local, like `_settle_runs`, so a cap holds while offline too. The
        log ring's masked values follow the subscriptions signed in since (their tokens)."""
        while True:
            try:
                self._log_ring.secrets = await asyncio.to_thread(logs.secrets, self._current_config(config), self._spent_tokens)
                await asyncio.to_thread(self._subscriptions.heard)
            except Exception:  # one failed pass never stops the next
                logger.exception("reading subscription usage failed")
            await asyncio.sleep(self.subscription_seconds)

    async def _ask_usage(self) -> None:
        """Every `usage_ask_seconds` while connected, and at once after a sign-in here ends signed in
        (`AgentPrograms.signed_in`), each login's usage as its CLI says (`SubscriptionWatch.ask`; reported within
        `subscription_seconds`); a failed pass is tried again next time."""
        wanted, loop = asyncio.Event(), asyncio.get_running_loop()

        def signed_in() -> None:  # the sign-in's own thread; a connection already gone asks at its next one
            try:
                loop.call_soon_threadsafe(wanted.set)
            except RuntimeError:
                pass
        self.programs.signed_in_listeners.append(signed_in)
        try:
            while True:
                try:
                    await self._subscriptions.ask(self.programs.environment)
                except Exception:  # never the connection's end: the next pass asks again
                    logger.exception("asking the logins' usage failed")
                try:
                    await asyncio.wait_for(wanted.wait(), self.usage_ask_seconds)
                except TimeoutError:
                    pass
                wanted.clear()
        finally:  # this connection's own: the next one listens for itself
            self.programs.signed_in_listeners.remove(signed_in)

    async def _report_usage(self, socket) -> None:
        """Every `subscription_seconds` while connected, the subscriptions whose usage moved enough
        (`SubscriptionWatch.report`), marked reported once sent; a pass that fails is tried again next time."""
        while True:
            await asyncio.sleep(self.subscription_seconds)
            try:
                if (report := await asyncio.to_thread(self._subscriptions.report, datetime.now(UTC))) is not None:
                    await socket.send(report.model_dump_json())
                    await asyncio.to_thread(self._subscriptions.sent, report, datetime.now(UTC))
            except websockets.ConnectionClosed:
                raise
            except Exception:  # never the connection's end: the next pass reports it
                logger.exception("reporting subscription usage failed")

    async def _settle_runs(self) -> None:
        """Every `settle_seconds`, mark ended the runs whose process is gone (`registry.settle_gone`)."""
        while True:
            try:
                for run in await asyncio.to_thread(reg.settle_gone):
                    cause = run.interruption.describe() if run.interruption else ""
                    logger.info("run %s settled %s %s", run.run_id[:8], run.status, cause)
            except Exception:  # one unreadable record never stops the sweep
                logger.exception("settling run records failed")
            await asyncio.sleep(self.settle_seconds)

    async def _hand_back(self) -> None:
        """Every `handback_seconds`, resume each parent whose last pending agent ended since (`handback.EndWatch`)."""
        watch = await asyncio.to_thread(EndWatch)
        while True:
            await asyncio.sleep(self.handback_seconds)
            try:
                for delivery in await asyncio.to_thread(watch.tick):
                    logger.info("run %s handed its agents' reports back: %s", delivery.run_id[:8], delivery.state)
            except Exception:  # one unreadable record never stops the watch
                logger.exception("handing agents' reports back failed")

    async def _review_turns(self) -> None:
        """Every `settle_seconds`, ask the reviews each coordinator's answer to the person that ended owes, and hand each
        review's durable rows to the prompts' owner (`turn_reviews.TurnReviewWatch`)."""
        watch = TurnReviewWatch()
        while True:
            await asyncio.sleep(self.settle_seconds)
            try:
                for ask in await asyncio.to_thread(watch.tick, Config().agent_turn_reviews):
                    logger.info("asked %s about run %s", ask.role, ask.parent[:8])
            except Exception:  # one unreadable record never stops the watch
                logger.exception("asking for turn reviews failed")

    async def _tell_silences(self) -> None:
        """Every `settle_seconds`, tell the run that asked each run silent too long at work, once (`handback.SilenceWatch`):
        this service runs whenever this computer runs agents, where a dispatcher leaves when it has nothing to send."""
        watch = SilenceWatch()
        while True:
            await asyncio.sleep(self.settle_seconds)
            try:
                for delivery in await asyncio.to_thread(watch.tick, Config()):
                    logger.info("run %s told of a silent run it asked: %s", delivery.run_id[:8], delivery.state)
            except Exception:  # one unreadable record never stops the watch
                logger.exception("telling of silent runs failed")
