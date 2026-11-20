"""The machine's link to the server: connect, serve, receive."""

import asyncio
import getpass
import hmac
import json
import logging
import os
import random
import time
import sys
import traceback
from pathlib import Path
from uuid import UUID
import websockets
from pydantic import SecretStr, ValidationError
from galaius_core import PcFeature, PcFeatures, AGENT_IMAGES_WIRE, PROJECTS_ROOT, MachineCommand, MachineTokenSwap
from galaius.agents.limits.subscription_watch import SubscriptionWatch
from galaius.host.processes import share_malloc_arenas, shell_path
from galaius.account.address import ServerAddress
from galaius.machine.agents import AgentSpawns, LogRing
from galaius.machine.presence import PresenceFiles, PresenceWatch
from galaius.machine.programs import AgentPrograms
from galaius.machine.projects import relocate
from galaius.machine.projects.workspaces import MachineWorkspaces, WorkspaceJobs
from galaius.agents.run import load_policy
from galaius.agents.turns.followup import FollowUps
from galaius.agents.limits.warm import WarmStart
from galaius.agents.turns.session_stops import SETTINGS_OK, SessionStops
from galaius.agents import registry as reg
from galaius.desktop.screen.trust import ScreenEnd
from galaius.workflows.functions import PermissionLevel
from galaius.upgrade.quiet import QuietPoint, UpgradeReady
from galaius.upgrade.store import RuntimeStore
from galaius.isolation.fence import available
from galaius.machine.peer.broker import PeerBroker
from galaius.machine.peer.link import PeerLink
from galaius.machine.peer.seal import PeerSeal
from galaius.machine.peer.switch import PeerSwitch
from galaius.machine.places.levels import BrowseBudget
from galaius.machine.places.place_reviews import PlaceReviews

from galaius.machine.connection import inventory, logs, problems, transport
from galaius.machine.connection.answers import Answers
from galaius.machine.connection.commands import Commands
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import EnrollmentChanged, Paused, TokenSwapped, Woke
from galaius.machine.connection.logs import JsonLines

from galaius.machine.connection.script import ScriptRuntime
from galaius.machine.connection.upkeep import Upkeep
from galaius.machine.connection.watches import Watches

logger = logging.getLogger(__package__)


class MachineRunner(Answers, Commands, Upkeep, Watches):
    reconnect_seconds = (1, 2, 5, 10, 20)
    #: How many refusals of its token in a row stop the connection for good (the service is not restarted after a clean stop): one answer from a server
    #: mid-deploy or a proxy is not a revoked PC, so it asks again after the usual waits (about 8 s in all) before it believes it.
    refusal_tries = 4
    #: After the server said it restarts (close 1012) or a gateway answered for it mid-deploy (HTTP 502/503/504): tried
    #: again every `restart_retry_seconds`, plus up to a second at random so its computers do not all knock at once, for
    #: `restart_window_seconds`; then `reconnect_seconds`. One PC over 14 days (2026-11-14): 185 closes 1012, 16 gaps
    #: over 20 s (most 139 s) while the backoff had grown to 20 s by the time the server was back.
    restart_retry_seconds = 2.0
    restart_window_seconds = 180.0
    #: Failed tries in a row (about 10 s with `reconnect_seconds`) before its page is told why.
    report_after_failures = 3

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or self.default_config_path()
        #: What the last failed channel try met, and the last crash its page was told of (each said once).
        self._last_failure = ""
        self._reported_crash = ""
        self._sequences = {}
        self._workers = {}
        #: File queries being answered (held so the loop does not drop them mid-answer).
        self._queries: set[asyncio.Task] = set()
        self._screen_desk = None
        self._agent_requests = {}
        self._agent_answers = {}
        self._sessions = None
        #: Commands being executed now (0 or 1: one worker runs them in order).
        self._executing = 0
        self._quiet = QuietPoint.current()
        #: The server closed the connection to restart (1012): ask for a release check once it is back.
        self._server_restarted = False
        self._log_ring = LogRing()
        logging.getLogger("galaius").addHandler(self._log_ring)
        self._runs_unreadable = None
        self._runs_working = None
        #: Tokens this runner held before a swap (`take_token_swap`): still masked in every kept line.
        self._spent_tokens: tuple[str, ...] = ()
        self._settings_detail, self._settings_refused = "", 0
        self._workspace_jobs = WorkspaceJobs(path=self.config_path.with_name("machine-workspaces.json"))
        self.presence = PresenceWatch(files=PresenceFiles(folder=self.config_path.parent))
        self._presence_lock = None
        #: Staged writes waiting for the owner's review, and the web's whole-PC browse budget.
        self.reviews = PlaceReviews.default()
        self._subscriptions = SubscriptionWatch()
        self.browse_budget = BrowseBudget()
        #: The agent child started ahead of the next web start (`WarmStart`), and of the next turn of
        #: the runs whose turn ended last (`FollowUps`).
        self.warm = WarmStart()
        self.followups = FollowUps()
        #: Cross-PC dispatch: this runner's calls to its server on the live channel, the seal of its last accepted hello, and the local endpoint
        #: its CLI and MCP server ask through (`galaius.machine.peer`).
        self.peer_link = PeerLink()
        self.peer_seal = PeerSeal(self.config_path.with_name("peer-seal"))
        self.peer_broker = PeerBroker(self.peer_link, PeerBroker.socket_path(self.config_path))
        #: The agent programs the owner installs from the web, and their sign-in (`AgentPrograms`).
        self.programs = AgentPrograms(environment=inventory.system_environment(), log_path=self.config_path.with_name("agent-programs.log"),
                                      active=lambda name: load_policy().provider_active(name), usage=self._subscriptions.login_usage)

    async def connect(self, config: MachineConfig) -> None:
        # Never through an address only this computer has: a tunnel down after a reboot kept this
        # PC offline for six minutes while its server answered all along (2026-10-23).
        config = await asyncio.to_thread(self.public_enrollment, config)
        # Exact secrets masked in every kept log line from the first one on (`LogRing`).
        self._log_ring.secrets = logs.secrets(config, self._spent_tokens)
        self._workspace_jobs.settle_interrupted()
        # Projects an earlier Galaius kept (~/.galaius/projects, galaius/projets), moved to ~/galaius/projects before any request names one.
        moved, left = await asyncio.to_thread(self.move_projects, config, True)
        for line in (*(f"moved {move.source} -> {move.target}" for move in moved.moves), *(f"kept {kept.folder}: {kept.why}" for kept in moved.kept), *left):
            logger.info("projects folder: %s", line)
        # Before anything reads which cut runs come back by themselves (`EndWatch`, `_resume_paused`): from when they do.
        await asyncio.to_thread(reg.cut_resume_since)
        # The hook that lets the web stop his editor sessions, in place before he opens the next one: Claude Code reads
        # its hooks when a session starts (`SessionStops`). Settings that are not JSON are left as they are.
        if (hooked := await asyncio.to_thread(SessionStops().install)) not in SETTINGS_OK:
            logger.warning("the web cannot stop an editor session on this computer: its Claude Code settings are %s", hooked)
        # Local and independent of the server: a run killed while the PC is offline is settled too.
        settler = asyncio.create_task(self._settle_runs())
        handing = asyncio.create_task(self._hand_back())
        silences = asyncio.create_task(self._tell_silences())
        reviewing = asyncio.create_task(self._review_turns())
        reports = asyncio.create_task(self._deliver_reports(config))
        followed = asyncio.create_task(self._follow_up(config))
        resumed = asyncio.create_task(self._resume_paused(config))
        watched = asyncio.create_task(self._watch_subscriptions(config))
        presenting = asyncio.create_task(self._present(config))
        brokering = asyncio.create_task(self.peer_broker.serve())
        swapped = False
        try:
            await self._connect(config)
        except TokenSwapped:
            swapped = True
        except (PermissionError, UpgradeReady):
            raise  # stopped on purpose, or leaving for a newer build: not a crash
        except Exception as error:
            # Whatever restarts it (systemd, launchd, the Windows task's loop), its page says why, once per cause.
            said = f"{type(error).__name__}: {error}"
            if said != self._reported_crash:
                self._reported_crash = said
                lines = (*self._log_ring.lines, *traceback.format_exception(error))
                await asyncio.to_thread(problems.report, config, "crashed", said, lines, self._log_ring.secrets)
            raise
        finally:
            settler.cancel()
            reports.cancel()
            followed.cancel()
            resumed.cancel()
            watched.cancel()
            handing.cancel()
            silences.cancel()
            reviewing.cancel()
            presenting.cancel()
            brokering.cancel()
            await asyncio.gather(settler, handing, silences, reviewing, reports, followed, resumed, watched, presenting, brokering, return_exceptions=True)
            for ahead in (self.warm, self.followups):  # a child started ahead never outlives its runner; ended on the loop it lives on
                AgentSpawns.loop().call_soon_threadsafe(ahead.close)
        if swapped:
            await self.connect(self.load())

    def public_enrollment(self, config: MachineConfig) -> MachineConfig:
        """`config` with the address every computer reaches its server at (`ServerAddress.reachable`).

        An enrollment made through an SSH tunnel holds that tunnel's `127.0.0.1`, which exists only
        while the tunnel is up: after a reboot the link retried it for minutes though the server was
        reachable the whole time. The swap is the same on every computer, needs no setting, and is
        WRITTEN BACK once the server itself named the public address — so the next boot starts on it
        — while an address only remembered here carries this run alone (the tunnel may be the only
        way in to a server that is not published)."""
        address = ServerAddress.parsed(config.server_url)
        if not address.local:
            return config
        named = address.public()  # the server's own answer, already proven to answer (`public`)
        remembered = ServerAddress.remembered()
        public = named if named is not None else remembered
        if public is None or public.local or public.server == address.server:
            return config
        logger.info("enrolled through %s, which only this computer has; connecting to %s",
                    config.server_url, public.server, extra={"machine_id": config.machine_id})
        connecting = config.model_copy(update={"server_url": public.server})
        if named is None:
            return connecting
        try:
            return self.update(lambda current: current.model_copy(update={"server_url": public.server}))
        except (OSError, ValidationError):  # an unwritable file never stops the link: this run connects anyway
            logger.warning("could not write %s back to the machine file", public.server, extra={"machine_id": config.machine_id})
            return connecting


    async def _connect(self, config: MachineConfig) -> None:
        delay_index, restarting_since = 0, None
        #: Refusals of the token in a row: the connection stops only for a refusal that repeats (`refusal_tries`), never for one answer.
        refusals = 0
        while True:
            self.presence.link = "reconnecting"
            if self.presence.files.paused:
                logger.info("paused from the tray icon; connecting again once resumed", extra={"machine_id": config.machine_id})
                while self.presence.files.paused:
                    await asyncio.sleep(self.presence_seconds)
                delay_index, restarting_since = 0, None
            if delay_index == self.report_after_failures:
                # Still not in after these tries: its page says why (the server forgets it once it is in).
                await asyncio.to_thread(problems.report, config, "channel_unreachable", self._last_failure, tuple(self._log_ring.lines), self._log_ring.secrets)
            endpoint = transport.channel_url(config.server_url)
            try:
                async with transport.channel(
                    endpoint,
                    config.token.get_secret_value(),
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=15,
                    # A message to an agent may show images (`PcFeature.AGENT_IMAGES`): the largest request carrying them.
                    max_size=AGENT_IMAGES_WIRE,
                ) as socket:
                    delay_index, restarting_since, refusals = 0, None, 0
                    self.presence.connected()
                    self._reported_crash = ""  # in again: a later crash, even the same one, is news to its page
                    logger.info("connected to %s", endpoint, extra={"machine_id": config.machine_id, "workspace_id": config.workspace_id})
                    if self._server_restarted:
                        self._server_restarted = False
                        RuntimeStore.default().request_check()
                    can_fence = (await asyncio.to_thread(available))[0]       # `available()` is (ready, why): the server reads a bool
                    await socket.send(json.dumps({"type": "hello", "features": self.features(), "peer": self.peer_seal.hello(), "peer_can_fence": can_fence,
                                                  **await asyncio.to_thread(self._beat, config)}))
                    if await self._serve(socket, config):
                        self.presence.link = "refused"
                        return
            except EnrollmentChanged:
                config = self.load()
                logger.warning("machine file changed its enrollment; reconnecting with it", extra={"machine_id": config.machine_id})
                continue
            except Paused:
                continue
            except Woke as woke:
                logger.info("%s; connecting again now", woke, extra={"machine_id": config.machine_id})
                continue
            except PermissionError:
                raise
            except websockets.ConnectionClosed as error:
                if error.code in {4401, 4403}:
                    refusals += 1
                    if refusals >= self.refusal_tries:
                        self.presence.link = "refused"
                        logger.error("machine token was revoked or rejected; connection stopped", extra={"machine_id": config.machine_id})
                        print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                        return
                    logger.warning("machine token refused (%d of %d): asking again before stopping", refusals, self.refusal_tries, extra={"machine_id": config.machine_id})
                self._last_failure = f"the connection closed (code {error.code})"
                logger.warning("connection closed (code %s)", error.code, extra={"machine_id": config.machine_id})
                # The server restarted: a deploy may have brought a release, checked once it answers again.
                self._server_restarted = self._server_restarted or error.code == 1012
                restarting_since = restarting_since or (time.monotonic() if error.code == 1012 else None)
            except websockets.InvalidStatus as error:
                # Refused at the handshake: a revoked or unknown token answers 401/403 before the
                # socket opens (the 4401/4403 close codes above never arrive). Stop, like a close.
                if error.response.status_code in {401, 403}:
                    refusals += 1
                    if refusals >= self.refusal_tries:
                        self.presence.link = "refused"
                        logger.error("machine token was refused (HTTP %s); connection stopped", error.response.status_code, extra={"machine_id": config.machine_id})
                        print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                        return
                    logger.warning("machine token refused (HTTP %s, %d of %d): asking again before stopping", error.response.status_code, refusals, self.refusal_tries,
                                   extra={"machine_id": config.machine_id})
                self._last_failure = f"the server refused the connection: HTTP {error.response.status_code}"
                restarting_since = restarting_since or (time.monotonic() if error.response.status_code in {502, 503, 504} else None)
                logger.warning("server refused the connection: HTTP %s", error.response.status_code, extra={"machine_id": config.machine_id})
            except (OSError, TimeoutError, websockets.InvalidHandshake) as error:
                # A server mid-deploy answers the upgrade with no or a non-101 response: retry, never exit.
                self._last_failure = f"{endpoint} unreachable: {error or type(error).__name__}"
                logger.warning("server unreachable: %s", error or type(error).__name__, extra={"machine_id": config.machine_id})
            delay = self.retry_delay(delay_index, restarting_since)
            logger.info("reconnecting in %.1f s", delay, extra={"machine_id": config.machine_id})
            await self._wait(delay)
            delay_index += 1

    def retry_delay(self, failures: int, restarting_since: float | None, now: float | None = None) -> float:
        """How long before the next try, after `failures` failed ones in a row; `restarting_since`: when the server
        last said it restarts (`restart_retry_seconds`)."""
        if restarting_since is not None and (time.monotonic() if now is None else now) - restarting_since < self.restart_window_seconds:
            return self.restart_retry_seconds + random.uniform(0, 1)
        return self.reconnect_seconds[min(failures, len(self.reconnect_seconds) - 1)]

    async def _wait(self, seconds: float) -> None:
        """`seconds`, cut short when the computer slept meanwhile: after a sleep the wait is over."""
        started = time.monotonic()
        while (left := seconds - (time.monotonic() - started)) > 0:
            step, monotonic_from, wall_from = min(left, 1.0), time.monotonic(), time.time()
            await asyncio.sleep(step)
            if self.slept(monotonic_from, wall_from, step):
                return

    async def _serve(self, socket, config: MachineConfig) -> bool:
        # Only the receiver consumes the websocket; only the worker executes commands.
        # Ending a connection drops queued work but joins the active command before reconnecting:
        # cancelling to_thread would leave its subprocess running beside the next command.
        commands: asyncio.Queue[MachineCommand | None] = asyncio.Queue(maxsize=32)
        self.peer_link.attach(socket)
        receiver = asyncio.create_task(self._receive(socket, config, commands))
        worker = asyncio.create_task(self._command_worker(socket, config, commands))
        heartbeat = asyncio.create_task(self._heartbeat(socket, config))
        usage = asyncio.create_task(self._report_usage(socket))
        asked = asyncio.create_task(self._ask_usage())
        upgrade = asyncio.create_task(self._leave_when_quiet(commands))
        prompts = asyncio.create_task(self._sync_prompts())
        policy = asyncio.create_task(self._renew_policy())
        try:
            done, _ = await asyncio.wait((receiver, worker, heartbeat, usage, upgrade), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return receiver in done and receiver.result()
        finally:
            self.peer_link.detach()
            receiver.cancel()
            heartbeat.cancel()
            usage.cancel()
            asked.cancel()
            upgrade.cancel()
            prompts.cancel()
            policy.cancel()
            while not commands.empty():
                commands.get_nowait()
            commands.put_nowait(None)
            for query in self._queries:
                query.cancel()
            await asyncio.gather(receiver, heartbeat, usage, asked, upgrade, prompts, policy, *self._queries, return_exceptions=True)
            if self._screen_desk is not None:  # its frames rode this connection: the viewer opens it again
                await self._screen_desk.close_all(ScreenEnd("link_lost", "the link to the server dropped"))
            await asyncio.shield(worker)

    async def _command_worker(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> None:
        while (command := await commands.get()) is not None:
            self._executing += 1
            try:
                await self._execute(socket, config, command)
            finally:
                self._executing -= 1

    async def _leave_when_quiet(self, commands: asyncio.Queue[MachineCommand | None]) -> None:
        """Supervised: once another runtime is active and no command, query, agent request or web
        session turn is in flight, raise `UpgradeReady` (looked at on this loop, so nothing starts
        between the look and the connection closing)."""
        if self._quiet is None:
            await asyncio.Event().wait()
        await self._quiet.watch(lambda: not commands.empty() or bool(self._executing) or bool(self._queries) or (self._sessions is not None and self._sessions.busy) or MachineWorkspaces.busy()
                                or (self._screen_desk is not None and self._screen_desk.live is not None), every=1)

    async def _receive(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> bool:
        async for payload in socket:
            self.presence.hear()
            response = json.loads(payload)
            if response.get("type") == "command":
                # A full queue holds this read, as the one-at-a-time loop did: never an exit.
                await commands.put(MachineCommand.model_validate(response["command"]))
            elif response.get("type") in {"file_query", "data_request"}:
                answer = (self._answer_file_query(socket, config, response["query"]) if response["type"] == "file_query"
                          else self._answer_data_request(socket, config, response["request"]))
                task = asyncio.create_task(answer)
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "agent_settings":
                task = asyncio.create_task(self._apply_web_settings(socket, config, response.get("settings")))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "agent_request":
                task = asyncio.create_task(self._answer_agent_request(socket, config, response["request"]))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "screen_input" and self._screen_desk is not None:
                self._screen_desk.take_raw(response)
            elif response.get("type") == "screen_close" and self._screen_desk is not None:
                task = asyncio.create_task(self._screen_desk.close(str(response.get("session", "")), str(response.get("why") or "the viewer left")[:400]))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "token_swap":
                try:
                    swap = MachineTokenSwap.model_validate(response)
                except ValidationError as error:
                    raise PermissionError("the server's token swap is malformed; the token is unchanged") from error
                self.take_token_swap(config, swap)
            elif response.get("type") == "peer_sealed":
                self.peer_seal.keep(response)
            elif response.get("type") == "peer_switch":
                PeerSwitch.on(True) if response.get("peer_runs") is True else PeerSwitch.off()
            elif response.get("type") == "peer_reply":
                self.peer_link.resolve(response.get("reply"))
            elif response.get("type") == "revoked":
                return True
        return False

    def take_token_swap(self, config: MachineConfig, swap: MachineTokenSwap) -> None:
        """A one-time bootstrap token (a cloud instance's: its user_data holds it) is swapped by the
        server on its first connection (`MachineTokenSwap`, signed with the current token's key).
        Saved under the machine file's lock while that file still holds the current token, then
        `TokenSwapped`. A swap for another machine or not signed so stops the runner
        (`PermissionError`), its token unchanged."""
        if swap.machine.id != config.machine_id or not hmac.compare_digest(self.signature(config.token.get_secret_value(), swap), swap.signature):
            raise PermissionError("the server's token swap is invalid; the token is unchanged")
        token = swap.token

        def swapped(current: MachineConfig) -> MachineConfig:
            self._same_enrollment(current, config)
            return current.model_copy(update={"token": SecretStr(token)})

        self.update(swapped)
        self._spent_tokens = (*self._spent_tokens, config.token.get_secret_value())
        self._log_ring.secrets = (*self._log_ring.secrets, token)
        logger.info("bootstrap token swapped; connecting with the new one", extra={"machine_id": config.machine_id})
        raise TokenSwapped

    def _query_finished(self, task: asyncio.Task) -> None:
        self._queries.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("machine query failed", exc_info=error)

    @classmethod
    def features(cls) -> list[str]:
        """What this runner's hello names (`PcFeatures`): every `PcFeature` of the galaius-core it ships with (a held sign-in
        only where one runs here, `AgentPrograms.HELD_SIGN_IN`), and each Script language it runs here."""
        features = frozenset(PcFeature) if AgentPrograms.HELD_SIGN_IN else frozenset(PcFeature) - {PcFeature.HELD_SIGN_IN}
        # A runner names the peer features it really has: the broker needs a POSIX socket; a Windows PC is a target of peer runs, never a source.
        features = features if PeerBroker.available else features - {PcFeature.PEER_BROKER}
        return PcFeatures(features, frozenset(ScriptRuntime.languages(inventory.safe_environment().get("PATH")))).said()


    def move_projects(self, config: MachineConfig, apply: bool) -> tuple[relocate.MovePlan, list[str]]:
        """The projects an earlier Galaius put here, moved to the projects folder with this PC's records naming them
        (`relocate`); with `apply` False only planned. (the plan, what was left undone)."""
        done = relocate.plan(config.working_directory, config.agent_roots)
        if not apply or not (done.moves or done.drops):
            return done, []

        def rehome(root: str, names: frozenset[str]) -> None:
            self.web_runs.rehome(root, names, PROJECTS_ROOT), self._workspace_jobs.rehome(root, names, PROJECTS_ROOT)
        return done, relocate.apply(done, rehome, lambda roots: self.update(lambda current: current.model_copy(
            update={"agent_roots": tuple(root for root in current.agent_roots if root not in roots)})))


async def connect_command(server_url: str | None, workspace_id: UUID | None, machine_id: UUID | None, token: str | None, permission_ceiling: PermissionLevel, working_directory: Path | None, serve: bool) -> None:
    if serve:  # before the runner: its programs take their PATH from it once, when it is built (`inventory.system_environment`)
        os.environ["PATH"] = shell_path(os.environ.get("PATH", ""))
        # Before its first worker thread: a thread per call each kept its own C arena, 36 of them held 722 of the
        # service's 1,367 MB after 3 h (2026-11-09).
        share_malloc_arenas()
    runner = MachineRunner()
    try:
        if server_url is None and workspace_id is None and machine_id is None and token is None:
            config = runner.load()
        else:
            if server_url is None or workspace_id is None or machine_id is None:
                raise ValueError("setup needs server URL, workspace ID, and machine ID")
            secret = token or getpass.getpass("Machine token: ")
            config = MachineConfig(server_url=server_url, workspace_id=workspace_id, machine_id=machine_id, token=secret, permission_ceiling=permission_ceiling, working_directory=working_directory or Path.cwd())
            runner.save(config)
        if not serve:
            print(f"Saved machine configuration to {runner.config_path}; enable the galaius-machine user service to keep it connected.")
            return
        # The service's journal gets one JSON object per line, the same fields the server stores.
        printer = logging.StreamHandler(sys.stderr)
        printer.setFormatter(JsonLines())
        package = logging.getLogger("galaius")
        package.addHandler(printer)
        package.setLevel(logging.INFO)
        await runner.connect(config)
    except (OSError, PermissionError, ValidationError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
