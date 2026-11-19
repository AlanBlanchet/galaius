"""Trait: what the runner does on a timer: prompts, policy, heartbeat, presence, reports."""

import asyncio
import json
import platform
import time
import sys
from uuid import UUID
import httpx
import websockets
from galaius_core import MachineProblem, MachineProblemCode
from galaius import USER_AGENT, __version__
from galaius.prompts import mirror as prompt_mirror
from galaius.prompts import projection as prompt_projection
from galaius.agents.catalog.connection import CatalogConnection
from galaius.cli.prompts import PromptMode
from galaius.account.tls import server_tls
from galaius.diagnostics.error_reports import MachineErrorReports
from galaius.machine.tray.launch import TrayLauncher
from galaius.diagnostics.redaction import redact
from galaius_core.wire import readable
from galaius.agents.run import load_policy
from galaius.agents import registry as reg
from galaius.host.console_watch import ConsoleWatch
from galaius.upgrade.release import BuildIdentity
from galaius.machine.peer.seal import INSTANCE
import logging

from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import Paused, Woke

logger = logging.getLogger(__package__)


class Upkeep:
    """Periodic work of a connected machine."""

    async def _sync_prompts(self) -> None:
        """Keep this PC's agents on the prompts of the workspace it runs, and its ~/galaius copies
        current, without anyone running `galaius prompts sync` (`prompt_mirror.sync`); a subscription
        signed in since the last install gets it within `subscription_seconds`. A failed check is
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
            await asyncio.sleep(self.subscription_seconds)
            waited += self.subscription_seconds

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
                "console_windows": [shown.model_dump(mode="json") for shown in ConsoleWatch.recent()[-5:]], "runtimes": self._runtimes(current), "accelerators": self._accelerators(), "functions": self._functions(), "resources": self._resources(current.working_directory),
                "file_roots": current.reported_file_roots(), "agent_settings": current.agent_state(self._settings_detail, self._settings_refused).model_dump(mode="json"),
                "busy": self.busy()}

    @staticmethod
    def _channel_url(server_url: str) -> str:
        return MachineConfig.endpoint_on(server_url, "/v1/machine-channel", socket=True)

    @staticmethod
    def _channel(endpoint: str, token: str, open_timeout: float = 10, close_timeout: float = 3, ping_interval: float | None = 20, max_size: int = 1024 * 1024) -> websockets.connect:
        """The machine channel at `endpoint`, trusting what every HTTP call to the server trusts (`server_tls`)."""
        return websockets.connect(endpoint, additional_headers={"Authorization": f"Bearer {token}"}, user_agent_header=USER_AGENT,
                                  ssl=server_tls() if endpoint.startswith("wss:") else None, open_timeout=open_timeout,
                                  close_timeout=close_timeout, ping_interval=ping_interval, max_size=max_size)

    @classmethod
    def report_problem(cls, config: MachineConfig, code: MachineProblemCode, detail: str = "", lines: tuple[str, ...] = (), secrets: tuple[str, ...] = ()) -> bool:
        """Tells its server why this computer is not connected (`MachineProblem`, shown on its page):
        over HTTPS with its machine token, which still answers when the channel does not. Best
        effort, credentials masked: False when the server did not take it. The same problem also
        prepares an error report from `lines` (what led to it) and asks its owner on that page
        whether to send it (`error_reports`): nothing of the lines leaves before his yes."""
        # What a person can read of `detail` and no more: a service log's end is this program's own text as
        # often as a sentence (« KeyError: 'token' »), and its page shows these words as this computer's own
        # (visual-critic 2026-10-15). The whole text stays in this computer's log and in `lines`, which goes
        # nowhere before its owner says so.
        problem = MachineProblem(code=code, detail=readable(redact(detail, (config.token.get_secret_value(),)).strip())[-500:])
        # The question's line is galaius's own last line, never `detail`: on an install it is the service
        # log's raw end, which a program the PC ran may have written (threat model S1). Never raises.
        cls.error_reports().prepare(config, code, lines[-1] if lines else code, lines, secrets)
        try:
            response = httpx.post(config.endpoint("/v1/machine/problem"), json=problem.model_dump(mode="json", exclude_none=True), headers=config.authorization, timeout=10)
        except httpx.HTTPError:
            return False
        return response.status_code == 204

    @classmethod
    def error_reports(cls) -> MachineErrorReports:
        """This computer's error report drafts, beside its machine file."""
        return MachineErrorReports(folder=cls.default_config_path().parent / "error-reports")

    async def _deliver_reports(self, config: MachineConfig) -> None:
        """Every `deliver_seconds`, the report its owner accepted on the PC's page goes up (`deliver`)."""
        while True:
            try:
                if await asyncio.to_thread(self.error_reports().deliver, config):
                    logger.info("error report sent: its owner accepted it on this computer's page")
            except Exception:  # one failed look never stops the next
                logger.exception("delivering an error report failed")
            await asyncio.sleep(self.deliver_seconds)
