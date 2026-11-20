"""Trait: what the runner does on a timer: prompts, policy, heartbeat, presence, reports."""

import asyncio
import json
import platform
import time
import sys
from uuid import UUID
import httpx
from galaius import USER_AGENT, __version__
from galaius.prompts import mirror as prompt_mirror
from galaius.prompts import projection as prompt_projection
from galaius.agents.catalog.connection import CatalogConnection
from galaius.cli.prompts import PromptMode
from galaius.account.tls import server_tls
from galaius.machine.tray.launch import TrayLauncher
from galaius.agents.run import load_policy
from galaius.agents import registry as reg
from galaius.host.console_watch import ConsoleWatch
from galaius.upgrade.release import BuildIdentity
from galaius.machine.peer.seal import INSTANCE
from galaius.machine.presence import PresenceWatch
import logging

from galaius.machine.connection import inventory, problems
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import Paused, Woke
from galaius.machine.connection.runs import Runs

logger = logging.getLogger(__package__)


class Upkeep(Runs):
    """Periodic work of a connected machine."""

    heartbeat_seconds = 3
    #: A wait that lasted this much longer than asked, by the wall clock or the system's (Windows counts a sleep, Linux
    #: and macOS do not), means the computer slept: connect again at once rather than wait ~35 s for a ping to fail.
    woke_after_seconds = 10.0
    #: How often the prompt check looks again whether it is due (`prompt_sync_seconds`), and a subscription signed in since is installed.
    prompt_tick_seconds = 5
    #: How often this PC checks whether the prompts it runs changed (a company granted or withdrew
    #: it, a prompt or agent was saved); a check is one 304 per workspace when nothing moved.
    prompt_sync_seconds = 300
    #: How often this PC renews the agent catalog and tool list a start reads (a 304 each while
    #: nothing changed): a start reads them from memory instead of waiting on the server.
    policy_renew_seconds = 15
    #: How often this PC looks whether its owner accepted an error report it prepared (only while one waits).
    deliver_seconds = 60
    #: How often the tray icon's presence is looked at (written only when it changed), and its run count read again.
    presence_seconds = 1.0
    presence_runs_seconds = 5.0
    #: How often the tray icon is looked for, and started again when it ended (`TrayLauncher.ensure`).
    tray_seconds = 60.0
    #: What the tray icon shows (`_present`), and the presence lock this runner holds once it took it.
    presence: PresenceWatch
    _presence_lock: int | None

    async def _sync_prompts(self) -> None:
        """Keep this PC's agents on the prompts of the workspace it runs, and its ~/galaius copies
        current, without anyone running `galaius prompts sync` (`prompt_mirror.sync`); a subscription
        signed in since the last install gets it within `prompt_tick_seconds`. A failed check is
        logged and retried next round; it never touches the machine channel."""
        seen: dict[UUID, str] = {}
        waited, failed = self.prompt_sync_seconds, False
        while True:
            due = waited >= self.prompt_sync_seconds
            if due:
                waited, failed = 0, False
            try:
                connection = CatalogConnection.load()
                if connection is not None and connection.auth_mode == "machine" and (
                        (due and await asyncio.to_thread(prompt_mirror.changed, connection, seen))
                        or (not failed and await asyncio.to_thread(prompt_projection.unprojected_subscriptions))):
                    report = await asyncio.to_thread(prompt_mirror.sync, connection, prompt_mirror.mirror_root(), PromptMode.installed)
                    logger.info("prompts synced: %s", "; ".join(report.notes))
            except prompt_projection.NoMainAgent:
                seen.clear()
                failed = True  # asked again next full round, once a main agent may be chosen
                logger.info("no agent instructions to install yet: this workspace has no main agent chosen")
            except (OSError, ValueError, httpx.HTTPError) as error:
                seen.clear()
                failed = True  # a failed sync waits for the next full round
                logger.warning("prompt sync failed: %s", error)
            await asyncio.sleep(self.prompt_tick_seconds)
            waited += self.prompt_tick_seconds

    async def _renew_policy(self) -> None:
        """Keep what a start reads first (`load_policy`: the agent catalog, the tool list) renewed in
        the background, every `policy_renew_seconds`. A failed renewal is logged; starts keep the last
        renewed read for up to `CatalogConnection.RENEWALS_REUSED` periods, then read the server themselves."""
        CatalogConnection.renew_every(self.policy_renew_seconds)
        while True:
            try:
                await asyncio.to_thread(self._renewed_policy)
            except Exception:  # one failed renewal never stops the next; starts then read the server themselves
                logger.exception("agent catalog not renewed")
            await asyncio.sleep(self.policy_renew_seconds)

    @staticmethod
    def _renewed_policy() -> None:
        with CatalogConnection.renewing():
            if (notice := load_policy().ignored_notice()) is not None:
                logger.warning(notice)

    async def _heartbeat(self, socket, config: MachineConfig) -> None:
        """A beat every `heartbeat_seconds`, each followed by a ping whose answer is what the tray icon calls
        connected (`PresenceWatch.hear`); leaves the connection when the computer slept (`Woke`) or the owner
        paused it (`Paused`)."""
        while True:
            monotonic_from, wall_from = time.monotonic(), time.time()
            await asyncio.sleep(self.heartbeat_seconds)
            if self.slept(monotonic_from, wall_from, self.heartbeat_seconds):
                raise Woke(f"this computer slept about {time.time() - wall_from:.0f} s")
            if self.presence.files.paused:
                raise Paused
            await socket.send(json.dumps({"type": "heartbeat", "peer": {"instance": str(INSTANCE)}, **await asyncio.to_thread(self._beat, config)}))
            (await socket.ping()).add_done_callback(self._ponged)

    def _ponged(self, answer: asyncio.Future) -> None:
        if not answer.cancelled() and answer.exception() is None:
            self.presence.hear()

    async def _present(self, config: MachineConfig) -> None:
        """What the tray icon shows (`galaius.machine.presence`), written whenever it changed by the runner holding the
        presence lock; another (an older release still leaving) holds it until it exits, and this one waits for it."""
        while self._presence_lock is None:
            if (held := await asyncio.to_thread(self.presence.files.hold)) is None:
                await asyncio.sleep(self.presence_seconds)
            self._presence_lock = held
        # The page the last connection found, until this one asks (seconds when the server is away): the first look
        # replaces whatever the last process left, never shown as this one's.
        page = await asyncio.to_thread(lambda: self.presence.files.read().page)
        asked = asyncio.create_task(asyncio.to_thread(self._page, config))
        pc, runs, read_at, written, iconed = platform.node().split(".")[0], 0, float("-inf"), None, time.monotonic()
        try:
            while True:
                if asked is not None and asked.done():
                    if asked.exception() is None:
                        page = asked.result() or page
                    else:
                        logger.warning("this computer's page not read: %s", asked.exception())
                    asked = None
                if sys.platform != "win32" and time.monotonic() - iconed >= self.tray_seconds:
                    # An icon that ended (a crash) comes back; on Windows this process sits in a job barred from the
                    # taskbar, so its icon is started by the service only (`WindowsMachineService.run`).
                    iconed = time.monotonic()
                    await asyncio.to_thread(TrayLauncher.ensure)
                if time.monotonic() - read_at >= self.presence_runs_seconds:
                    runs, read_at = await asyncio.to_thread(self._live_runs, runs), time.monotonic()
                shown = self.presence.presence(runs=runs, update_waiting=self._quiet is not None and self._quiet.waiting(), pc=pc, page=page, version=__version__)
                if shown != written:
                    try:
                        await asyncio.to_thread(self.presence.files.write, shown)
                        written = shown
                    except OSError as error:  # the next look writes it
                        logger.warning("tray presence not written: %s", error)
                await asyncio.sleep(self.presence_seconds)
        finally:
            if asked is not None:
                asked.cancel()
            if written is not None and self.presence.link == "refused":  # said before the process ends: only a sign-in brings it back
                self.presence.files.write(written.model_copy(update={"link": "refused"}))

    @staticmethod
    def _page(config: MachineConfig) -> str:
        with httpx.Client(verify=server_tls(), timeout=10, headers={"User-Agent": USER_AGENT}) as http:
            return config.page(http)

    @staticmethod
    def _live_runs(before: int) -> int:
        """Agent runs alive on this computer now (every galaius run, from the web or an editor); `before` when unreadable."""
        try:
            return len(reg.running_runs())
        except (OSError, ValueError) as error:
            logger.warning("runs unreadable for the tray: %s", error)
            return before

    def _beat(self, connected: MachineConfig) -> dict[str, object]:
        """What every hello and heartbeat says: this PC's runtimes, resources, its file roots and
        agent settings as its machine file says NOW (a change made here reaches the server with the
        next beat), whether work of its own is going on (`busy`), the build it runs (`build`: none
        from a source checkout), so its server knows which release each PC is on, and the console windows
        its service hid lately (`console_windows`, `galaius.host.console_watch`)."""
        current = self._current_config(connected)
        build = BuildIdentity.installed()
        return {"build": build.model_dump(mode="json") if build is not None else None,
                "console_windows": [shown.model_dump(mode="json") for shown in ConsoleWatch.recent()[-5:]], "runtimes": inventory.runtimes(current), "accelerators": inventory.accelerators(), "functions": inventory.functions(), "resources": inventory.resources(current.working_directory),
                "file_roots": current.reported_file_roots(), "agent_settings": current.agent_state(self._settings_detail, self._settings_refused).model_dump(mode="json"),
                "busy": self.busy()}

    async def _deliver_reports(self, config: MachineConfig) -> None:
        """Every `deliver_seconds`, the report its owner accepted on the PC's page goes up (`deliver`)."""
        while True:
            try:
                if await asyncio.to_thread(problems.drafts().deliver, config):
                    logger.info("error report sent: its owner accepted it on this computer's page")
            except Exception:  # one failed look never stops the next
                logger.exception("delivering an error report failed")
            await asyncio.sleep(self.deliver_seconds)

    def slept(self, monotonic_from: float, wall_from: float, asked: float) -> bool:
        """Whether a wait of `asked` seconds begun at (`monotonic_from`, `wall_from`) took long enough that this
        computer slept meanwhile (`woke_after_seconds`)."""
        return max(time.monotonic() - monotonic_from, time.time() - wall_from) - asked > self.woke_after_seconds
