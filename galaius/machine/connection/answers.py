"""Trait: answers to the server's file, data, settings and agent requests."""

import asyncio
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from pydantic import ValidationError
from galaius_core import PcFeature, MACHINE_AGENT_REQUESTS, MachineAgentSettingsUpdate, AgentRefusal, MachineAgentAnswer, MachineAgentRequest, MachineDataAnswer, MachineDataRequest, MachineFileQuery, MachineFileQueryResult
from galaius.machine.places.places import PlaceDesk
from galaius_core.wire import fitted
from galaius.agents import registry as reg
from galaius_core import AgentRefused

from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.faults import ANSWERED_REFUSALS, said_of
from galaius.machine.connection.shared import Shared
from galaius.machine.connection.files import MachineDataFiles, MachineFiles

logger = logging.getLogger(__package__)


class Answers(Shared):
    """What the runner answers when the server asks about this machine."""

    #: Agent actions already accepted (id -> expiry): each is taken once (`_answer_agent_request`).
    _agent_requests: dict[UUID, datetime]
    #: The answer of each accepted action the server may ask again (`idempotent_feature`), by its id,
    #: for its `idempotent_seconds`; past a restart a message's queue item or transcript entry tells instead.
    _agent_answers: dict[UUID, asyncio.Future[MachineAgentAnswer]]

    async def _answer_file_query(self, socket, config: MachineConfig, payload: object) -> None:
        """What a person picking a script sees of this machine: checked like a command (this
        machine, this workspace, signed, not expired), answered from the owner's file roots only."""
        try:
            query = MachineFileQuery.model_validate(payload)
        except ValidationError:
            logger.warning("malformed file query dropped")
            return
        try:
            if query.machine.id != config.machine_id or query.workspace_id != config.workspace_id:
                raise PermissionError("file query targets another machine")
            self._verify_signed(config, query, "file query")
            listing = await asyncio.to_thread(MachineFiles(config=self._current_config(config), area="scripts").listing, query.path)
            logger.info("file query %s: %s", query.path or "(roots)", listing.kind)
            answer = MachineFileQueryResult(query_id=query.id, listing=listing)
        except (PermissionError, OSError, ValueError) as error:
            answer = MachineFileQueryResult(query_id=query.id, error=fitted(MachineFileQueryResult, "error", said_of(error, query.path, doing="listing_files").said))
        await socket.send(json.dumps({"type": "file_listing", "result": answer.model_dump(mode="json")}))

    async def _answer_data_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The Data screen reading this machine's FILE ROOTS: checked like a command (this machine,
        this workspace, signed, not expired), answered beneath the named root only."""
        try:
            request = MachineDataRequest.model_validate(payload)
        except ValidationError:
            logger.warning("malformed data request dropped")
            return
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("data request targets another machine")
            self._verify_signed(config, request, "data request")
            answer = await asyncio.to_thread(MachineDataFiles(config=self._current_config(config)).answer, request)
            logger.info("data %s %s/%s", request.op, request.root or "(roots)", request.path)
        except (PermissionError, OSError, ValueError) as error:
            answer = MachineDataAnswer(request_id=request.id, error=fitted(MachineDataAnswer, "error", said_of(error, request.path, doing="reading_data").said))
        await socket.send(json.dumps({"type": "data_answer", "result": answer.model_dump(mode="json")}))

    async def _apply_web_settings(self, socket, config: MachineConfig, payload: object) -> None:
        """The owner's web page changing this PC's agent settings (`MachineAgentSettingsUpdate`):
        signed with this machine's key, for this machine, newer than the last applied, built from
        this PC's current revision, and only while `remote_settings` is on - checked against what
        the machine file holds NOW. Applied or not, the PC answers its state and logs it here."""
        try:
            update = MachineAgentSettingsUpdate.model_validate(payload)
        except ValidationError:
            logger.warning("malformed agent settings dropped")
            return
        before = self._current_config(config)
        try:
            if update.machine.id != config.machine_id or update.workspace_id != config.workspace_id:
                raise PermissionError("agent settings target another machine")
            if not hmac.compare_digest(self.signature(config.token.get_secret_value(), update), update.signature):
                raise PermissionError("agent settings signature is invalid")
            def apply(current: MachineConfig) -> MachineConfig:
                self._same_enrollment(current, config)
                return current.with_web_settings(update)
            after = await asyncio.to_thread(self.update, apply)
            if created := await asyncio.to_thread(after.create_agent_roots):
                logger.info("agent folders created for the web: %s", ", ".join(created))
            self._settings_detail, self._settings_refused = "", 0
            was, now = before.agent_settings()[0].model_dump(mode="json"), after.agent_settings()[0].model_dump(mode="json")
            changed = {key: [was[key], now[key]] for key in was if was[key] != now[key]}
            # Agents switched off from the web stop the ones the web started here, as a kill switch must.
            stopped, failed = await asyncio.to_thread(self.web_runs.stop_live) if before.run_agents and not after.run_agents else (0, 0)
            logger.info("agent settings version %s from the web applied: %s%s", update.version, ", ".join(sorted(changed)) or "no change",
                        f"; running agents stopped: {stopped}" + (f", {failed} could not be" if failed else "") if stopped or failed else "")
            error = None
        except PermissionError as refusal:
            self._settings_detail, self._settings_refused = str(refusal), update.version
            changed, error = {}, str(refusal)
            logger.warning("agent settings version %s refused: %s", update.version, refusal)
        self.audit("agents.log", {"op": "settings", "version": update.version, "changed_by": str(update.changed_by), "changed_at": update.changed_at.isoformat(), "changed": changed, "error": error})
        state = self._current_config(config).agent_state(self._settings_detail, self._settings_refused)
        await socket.send(json.dumps({"type": "agent_settings_state", "state": state.model_dump(mode="json")}))

    async def _answer_agent_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The owner driving agents here from the web (`MachineAgents`): checked like a command (this
        machine, this workspace, signed, not expired; an action's id accepted once), answered from
        the owner's CURRENT settings on this machine."""
        try:
            request: MachineAgentRequest = MACHINE_AGENT_REQUESTS.validate_python(payload)
        except ValidationError:
            logger.warning("malformed agent request dropped")
            return
        answered: asyncio.Future[MachineAgentAnswer] | None = None
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("agent request targets another machine")
            self._verify_signed(config, request, "agent request")
            if request.action:
                now = datetime.now(UTC)
                self._agent_requests = {key: until for key, until in self._agent_requests.items() if until > now}
                self._agent_answers = {key: answer for key, answer in self._agent_answers.items() if key in self._agent_requests}
                if (first := self._agent_answers.get(request.id)) is not None:
                    # Asked again (its answer never reached the server): the first one's answer, once it is known.
                    try:
                        answer = await asyncio.wait_for(asyncio.shield(first), request.seconds)
                    except TimeoutError:
                        raise AgentRefused("under_way", "its first delivery is still under way on this computer") from None
                    except asyncio.CancelledError:
                        if not first.cancelled() or ((task := asyncio.current_task()) is not None and task.cancelling()):
                            raise
                    else:
                        if not (isinstance(answer.refusal, AgentRefusal) and answer.refusal.pending):
                            await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))
                            return
                        # The first answered while its act still worked here: this one delivers it again, which answers that act.
                        self._agent_answers.pop(request.id, None)
                        self._agent_requests.pop(request.id, None)
                    # The first stopped unanswered (`_release`): this one delivers it; whatever it had queued is found, never queued again.
                if request.id in self._agent_requests:
                    raise PermissionError("agent request was already used")
                self._agent_requests[request.id] = request.expires_at if request.idempotent_feature is None else max(request.expires_at, now + timedelta(seconds=request.idempotent_seconds))
                if request.idempotent_feature is not None:
                    answered = self._agent_answers[request.id] = asyncio.get_running_loop().create_future()
                    if (task := asyncio.current_task()) is not None:
                        task.add_done_callback(lambda _, key=request.id, answer=answered: self._release(key, answer))
            current = self._current_config(config)
            if request.feature == PcFeature.SCREEN:
                answer = await self._screen(current).answer(request, lambda message: socket.send(json.dumps(message)))
                await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))
                return
            if request.feature == PcFeature.PLACES:
                answer = await asyncio.to_thread(PlaceDesk(self).answer, request)
                await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))
                return
            self._log_ring.secrets = self._secrets(current)
            agents = self._agents(current)
            answer = await agents.answer(request)
            # Reading (the page polls every few seconds) stays out of the owner's log; actions go in.
            run_id = answer.run_id or getattr(request, "run_id", None)
            placed = next((item for item in agents.runs.read() if item.run_id == run_id), None) if run_id else None
            recorded = reg.get_run(str(run_id)) if run_id else None
            folder = "/".join(filter(None, (getattr(request, "root", "") or (placed.root if placed else ""), getattr(request, "path", "") or (placed.path if placed else ""))))
            said = " ".join(f"{key}={value}" for key, value in (("role", getattr(request, "role", None) or (recorded.agent if recorded else None)), ("folder", folder),
                                                                 ("run", str(run_id)[:8] if run_id else None), ("provider", getattr(request, "provider", None) if request.op == "provider" else None),
                                                                 ("answer", answer.detail if request.op in {"answer", "send", "provider"} else None)) if value)
            logger.log(logging.INFO if request.action else logging.DEBUG, "agent %s %s", request.op, said)
        except ANSWERED_REFUSALS as error:
            answer = MachineAgentAnswer(request_id=request.id, error=fitted(MachineAgentAnswer, "error", (said := said_of(error, doing="driving_agent")).said), refusal=said.refusal)
            if self.own_fault(error, said) and request.action:
                logger.error("agent %s failed: %s", request.op, answer.error, exc_info=error)
                self._report_fault(config, request.op, answer.error or "", error)
            else:
                logger.warning("agent %s refused: %s", request.op, answer.error)
        except Exception as error:  # noqa: BLE001 — the asker is never left with no answer at all
            # Whatever this was, the server is waiting on an answer to THIS request: without one it
            # cannot tell a refusal from a frozen PC and tells him the PC « n'a pas confirmé le
            # lancement » minutes later (2026-10-23 16:29, his new conversation: `admission.Paused`
            # derived from bare `Exception`, so the only trace was this runner's own log line).
            answer = MachineAgentAnswer(request_id=request.id, error=fitted(MachineAgentAnswer, "error", (said := said_of(error, doing="driving_agent")).said), refusal=said.refusal)
            logger.error("agent %s failed: %s", request.op, answer.error, exc_info=error)
            if request.action:
                self._report_fault(config, request.op, answer.error or "", error)
        if request.action:
            # The whole brief / message stays HERE, in the owner's local log; the server keeps a digest.
            asked = request.model_dump(mode="json", exclude={"type", "id", "machine", "workspace_id", "expires_at", "signature"})
            self.audit("agents.log", {**asked, **({"started_run_id": str(answer.run_id)} if answer.run_id and "run_id" not in asked else {}), "error": answer.error})
        if answered is not None:
            answered.set_result(answer)
        await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))
