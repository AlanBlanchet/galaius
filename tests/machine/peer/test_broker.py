"""The local endpoint: one JSON line in, one out, bounded, never granting; and the link's failure budget."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from galaius_core import PeerFleetCall, PeerMessage, PeerReply, PcFleet, RunRef
from galaius.machine.peer import broker as broker_module
from galaius.machine.peer.broker import PeerBroker
from galaius.machine.peer.link import PeerLink, PeerLinkDown


class Socket:
    def __init__(self, link: PeerLink, answer=None) -> None:
        self.link, self.sent, self.answer = link, [], answer

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        if self.answer is not None:
            self.link.resolve({"request_id": message["envelope"]["request_id"], **self.answer})


async def ask(path: Path, body: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(body)
    await writer.drain()
    answer = await reader.readline()
    writer.close()
    return json.loads(answer)


def run(link: PeerLink, path: Path, body: bytes) -> dict:
    async def scenario() -> dict:
        broker = PeerBroker(link, path)
        serving = asyncio.create_task(broker.serve())
        while not path.exists():
            await asyncio.sleep(0.01)
        try:
            return await ask(path, body)
        finally:
            serving.cancel()
    return asyncio.run(scenario())


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "peer.sock"


def test_a_call_reaches_the_server_with_the_claim_unverified_and_its_reply_comes_back(path: Path) -> None:
    link = PeerLink()
    socket = Socket(link, {"answer": PcFleet().model_dump(mode="json")})
    link.attach(socket)
    claim = RunRef(run_id=uuid4(), conversation_id=uuid4())
    answer = run(link, path, json.dumps({"call": PeerFleetCall().model_dump(mode="json"), "by": claim.model_dump(mode="json")}).encode() + b"\n")
    assert PeerReply.model_validate({"request_id": uuid4(), **answer["reply"]}).answer.kind == "peer_fleet"
    sent = socket.sent[0]["envelope"]["call"]
    assert sent["by"]["run_id"] == str(claim.run_id) and sent["by_verified"] is False         # a test process is in no run: the name is a claim
    assert oct(path.stat().st_mode & 0o777) == "0o600" if path.exists() else True


@pytest.mark.parametrize("body", [b"not json\n", b'{"call": {"kind": "peer_start"}}\n', b'{"call": {"kind": "nope"}}\n', b"\n"], ids=["text", "short-start", "unknown-kind", "empty"])
def test_a_malformed_call_is_refused_and_the_broker_stays_up(path: Path, body: bytes) -> None:
    link = PeerLink()
    link.attach(Socket(link))
    assert run(link, path, body)["refusal"]["code"] == "invalid_request"


def test_a_frame_over_the_cap_is_refused(path: Path) -> None:
    link = PeerLink()
    link.attach(Socket(link))
    big = json.dumps({"call": PeerMessage(key="k", machine=uuid4(), run_id=uuid4(), text="t").model_dump(mode="json"), "pad": "x" * 70000}).encode() + b"\n"
    assert run(link, path, big)["refusal"]["code"] == "invalid_request"


def test_a_closed_link_answers_at_once_and_a_silent_server_after_two_tries_with_the_same_key(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(PeerLink(), path, json.dumps({"call": PeerFleetCall().model_dump(mode="json")}).encode() + b"\n")["refusal"]["code"] == "pc_unreachable"
    monkeypatch.setattr("galaius.machine.peer.link.PEER_ANSWER_SECONDS", 0.05)
    silent = PeerLink()
    socket = Socket(silent)
    silent.attach(socket)
    start = {"kind": "peer_message", "key": "retry-key", "machine": str(uuid4()), "run_id": str(uuid4()), "text": "hi"}
    assert run(silent, path.with_name("b.sock"), json.dumps({"call": start}).encode() + b"\n")["refusal"]["code"] == "pc_slow"
    assert [message["envelope"]["call"]["key"] for message in socket.sent] == ["retry-key", "retry-key"]


def test_a_process_inside_a_fenced_run_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    class Run:
        fence = object()
    monkeypatch.setattr(broker_module.Caller, "scope_run", classmethod(lambda cls, pid: str(uuid4())))
    monkeypatch.setattr(broker_module.reg, "get_run", lambda run: Run())
    verified, refusal = PeerBroker.resolved(1, None)
    assert not verified and refusal["refusal"]["code"] == "permission_denied"
    monkeypatch.setattr(broker_module.reg, "get_run", lambda run: type("Plain", (), {"fence": None})())
    claim = RunRef(run_id=uuid4(), conversation_id=uuid4())
    assert PeerBroker.resolved(1, claim) == (False, None)                                   # inside another run than the one it names: a claim
    monkeypatch.setattr(broker_module.Caller, "scope_run", classmethod(lambda cls, pid: str(claim.run_id)))
    assert PeerBroker.resolved(1, claim) == (True, None)                                    # inside that very run: verified


def test_the_client_asks_the_runner_through_its_socket_and_a_runner_not_running_says_so(path: Path) -> None:
    from galaius.machine.peer.client import PeerClient, PeerUnavailable

    link = PeerLink()
    link.attach(Socket(link, {"answer": PcFleet().model_dump(mode="json")}))

    async def scenario() -> None:
        serving = asyncio.create_task(PeerBroker(link, path).serve())
        while not path.exists():
            await asyncio.sleep(0.01)
        try:
            assert (await asyncio.to_thread(PeerClient(path).fleet)).kind == "peer_fleet"
        finally:
            serving.cancel()
    asyncio.run(scenario())
    with pytest.raises(PeerUnavailable, match="not running"):
        PeerClient(path.with_name("absent.sock")).fleet()


def test_the_mcp_tools_place_events_send_and_stop_through_the_runner_and_say_a_refusal_plainly(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_spawn / agent_events / agent_send / agent_stop with `machine`: the sentences an agent reads, from a runner answering for another PC."""
    from galaius_core import PcState, PeerProjects, PeerProject, PeerStarted, PeerSent, PeerWindow, MachineRunEvents
    from galaius.server.tools import agents as tools

    other, project, run = uuid4(), uuid4(), uuid4()
    answers = {
        "peer_fleet": PcFleet(pcs=(PcState(id=uuid4(), name="here", this=True, state="online"), PcState(id=other, name="far", state="online", dispatchable=True, places_free=2, places_total=3))),
        "peer_projects": PeerProjects(projects=(PeerProject(id=project, name="Demo"),)),
        "peer_start": PeerStarted(dispatch_id=uuid4(), run_id=run, machine_id=other, machine_name="far", conversation_id=uuid4(), permission="workspace_write"),
        "peer_message": PeerSent(message_id=uuid4()),
        "peer_events": PeerWindow(events=MachineRunEvents(run_id=run)),
    }

    class Runner:
        def __init__(self, link: PeerLink) -> None:
            self.link, self.calls = link, []

        async def send(self, text: str) -> None:
            envelope = json.loads(text)["envelope"]
            kind = envelope["call"]["kind"]
            self.calls.append(envelope["call"])
            if kind == "peer_stop":
                self.link.resolve({"request_id": envelope["request_id"], "refusal": {"code": "not_found", "message": "no such run among yours"}})
            else:
                self.link.resolve({"request_id": envelope["request_id"], "answer": answers[kind].model_dump(mode="json")})
    link = PeerLink()
    runner = Runner(link)
    link.attach(runner)
    from galaius.machine.peer.client import PeerClient
    monkeypatch.setattr(PeerClient, "__init__", lambda self, p=None: setattr(self, "path", path))

    async def scenario() -> list[str]:
        serving = asyncio.create_task(PeerBroker(link, path).serve())
        while not path.exists():
            await asyncio.sleep(0.01)
        try:
            return [await asyncio.to_thread(tools._peer_spawn, "auto", "demo", "judge x", "tester", None, None, None, None),
                    await asyncio.to_thread(tools._peer_spawn, "far", None, "judge x", "tester", None, None, None, None),
                    await asyncio.to_thread(tools._peer_send, "far", str(run), "hello"),
                    await asyncio.to_thread(tools._peer_events, "far", str(run), 5),
                    await asyncio.to_thread(tools._peer_stop, "far", str(run)),
                    await asyncio.to_thread(tools._peer_send, "nowhere", str(run), "hello")]
        finally:
            serving.cancel()
    placed, no_project, sent, events, stopped, unknown = asyncio.run(scenario())
    assert placed.startswith(f"Started on another PC — run_id={run}") and "far" in placed
    assert no_project.startswith("ERROR: machine needs project") and "far" in sent and events.endswith("no events yet")
    assert stopped.startswith("ERROR: no such run among yours (not_found)") and unknown.startswith("ERROR: no other PC named 'nowhere'")
    assert [call["kind"] for call in runner.calls if call["kind"] == "peer_start"] == ["peer_start"]


def test_a_retry_keeps_its_key_inside_the_window_and_a_different_request_or_an_hour_later_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from galaius.machine.peer.client import PeerClient

    client = PeerClient(Path("/nonexistent/peer.sock"))
    monkeypatch.setattr(PeerClient, "asker", staticmethod(lambda: None))
    machine, project = uuid4(), uuid4()
    monkeypatch.setattr("galaius.machine.peer.client.time.time", lambda: 1_000_000.0)
    first = client.key(machine, project, "tester", "", "judge x")
    assert client.key(machine, project, "tester", "", "judge x") == first and client.key(machine, project, "tester", "", "judge y") != first
    monkeypatch.setattr("galaius.machine.peer.client.time.time", lambda: 1_000_000.0 + 3600)
    assert client.key(machine, project, "tester", "", "judge x") != first


def test_a_placed_run_named_by_the_shared_mcp_server_has_no_peer_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    placed_run = uuid4()
    entries = type("Entries", (), {"entry": lambda self, run: type("Entry", (), {"placed_by": uuid4()})() if run == placed_run else None})()
    monkeypatch.setattr(broker_module.WebRuns, "in_folder", classmethod(lambda cls, folder: entries))
    monkeypatch.setattr(broker_module.Caller, "scope_run", classmethod(lambda cls, pid: None))      # the MCP server itself is in no run's scope
    verified, refusal = PeerBroker.resolved(1, RunRef(run_id=placed_run, conversation_id=uuid4()))
    assert not verified and refusal["refusal"]["code"] == "permission_denied"
    assert PeerBroker.resolved(1, RunRef(run_id=uuid4(), conversation_id=uuid4())) == (False, None)
