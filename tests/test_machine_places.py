"""Levels from the web, as the PC answers signed requests (`galaius.machine_places.PlaceDesk`):
a change applies at once, a widening never past the home folder's insides, hidden names or
credential stores; every change lands in the PC's own log with its digest; browsing lists folder
names inside the home folder, file names only with the PC's opt-in; a review can be read or
dropped from the web, never accepted."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from galaius_core import MACHINE_AGENT_REQUESTS
from galaius.machines import MachineConfig, MachineRunner
from galaius.place_reviews import PlaceReviews

pytestmark = pytest.mark.usefixtures("directory_backend")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    for folder in ("docs/reports", "notes", "photos"):
        (home / folder).mkdir(parents=True)
    (home / "notes" / "todo.txt").write_text("buy milk")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    entries: list[dict] = []
    monkeypatch.setattr(MachineRunner, "audit", staticmethod(lambda log, entry: entries.append({"log": log, **entry})))
    return entries


@pytest.fixture
def runner(home: Path, tmp_path: Path) -> MachineRunner:
    runner = MachineRunner(config_path=tmp_path / "config" / "machine.json")
    runner.reviews = PlaceReviews(root=tmp_path / "reviews")
    runner.save(MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="read_only",
                              working_directory=home, places={"docs": "write"}))
    return runner


def _ask(runner: MachineRunner, op: str, **fields) -> dict:
    """`op` as the server sends it: signed with the machine's key; the PC's answer."""
    config = runner.load()
    unsigned = MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(config.machine_id)}, "workspace_id": str(config.workspace_id), "op": op,
                                                      "initiator_account": str(uuid4()), "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                                                      "signature": "0" * 64, **fields}).model_dump(mode="json", exclude={"signature"})
    key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
    signed = {**unsigned, "signature": hmac.new(key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()}

    class Socket:
        sent: list[dict] = []

        async def send(self, text: str) -> None:
            self.sent.append(json.loads(text)["result"])

    socket = Socket()
    asyncio.run(runner._answer_agent_request(socket, config, signed))
    return socket.sent[-1]


@pytest.mark.parametrize(("path", "refused"), [("notes", None), ("photos/id_rsa", "credential stores"), ("pictures", "is a link")])
def test_a_signed_web_widening_applies_at_once_never_on_a_link_or_a_credential_store(runner: MachineRunner, home: Path, logged: list[dict], path: str, refused: str | None) -> None:
    (home / "pictures").symlink_to(home / "photos")
    answer = _ask(runner, "place_level", path=path, level="read")
    if refused is None:
        assert answer["error"] is None and answer["change"] is None and runner.load().places == {"docs": "write", "notes": "read"}
        assert logged[-1]["method"] == "web-widen" and logged[-1]["digest"] == answer["digest"]
    else:
        place_log = [entry for entry in logged if entry["log"] == "places.log"]
        assert refused in answer["error"] and runner.load().places == {"docs": "write"} and place_log[-1]["method"] == "web-refused"


def test_the_home_folder_itself_never_opens_from_the_web(runner: MachineRunner, home: Path, logged: list[dict]) -> None:
    """A working directory above the home folder: the home folder and its siblings stay closed to the web."""
    runner.update(lambda config: config.model_copy(update={"working_directory": home.parent}))
    for path in (home.name, "elsewhere"):
        (home.parent / path).mkdir(exist_ok=True)
        assert "only on this PC" in _ask(runner, "place_level", path=path, level="read")["error"]
    assert _ask(runner, "place_level", path=f"{home.name}/notes", level="read")["error"] is None


def test_a_web_narrowing_applies_at_once(runner: MachineRunner, logged: list[dict]) -> None:
    answer = _ask(runner, "place_level", path="docs/reports", level="see")
    assert answer["change"] is None and runner.load().places == {"docs": "write", "docs/reports": "see"}
    assert logged[-1]["method"] == "web-narrow" and len(logged[-1]["digest"]) == 64


def test_browsing_lists_folder_names_and_file_names_only_with_the_pcs_opt_in(runner: MachineRunner, logged: list[dict]) -> None:
    assert {entry["name"]: entry["kind"] for entry in _ask(runner, "place_browse")["browse"]} == {"docs": "folder", "notes": "folder", "photos": "folder"}
    runner.update(lambda config: config.model_copy(update={"browse": True}))
    assert {entry["name"] for entry in _ask(runner, "place_browse", path="notes")["browse"]} == {"todo.txt"}


def test_a_review_is_read_and_dropped_from_the_web_never_accepted(runner: MachineRunner, home: Path, logged: list[dict]) -> None:
    review_id = runner.reviews.stage_file("notes", home / "notes", ("todo.txt",), b"buy oat milk", origin="workflow", run_id=uuid4())
    [listed] = _ask(runner, "place_reviews")["reviews"]
    assert listed["state"] == "ready" and listed["files"] == [{"path": "todo.txt", "change": "changed", "size": 12}]
    read = _ask(runner, "place_review", review_id=str(review_id))
    assert "+buy oat milk" in read["lines"]
    with pytest.raises(ValidationError):
        MACHINE_AGENT_REQUESTS.validate_python({"op": "place_accept"})
    _ask(runner, "place_discard", review_id=str(review_id))
    assert runner.reviews.list() == () and (home / "notes" / "todo.txt").read_text() == "buy milk"


def test_a_machine_file_from_before_levels_keeps_its_folders_as_sandboxes(runner: MachineRunner) -> None:
    values = json.loads(runner.config_path.read_text())
    values.pop("places")
    values["file_roots"] = ["notes"]
    runner.config_path.write_text(json.dumps(values))
    assert runner.load().places == {"notes": "sandbox"}


def test_a_narrowing_stops_every_fenced_agent_turn_running_now(runner: MachineRunner, home: Path, logged: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Its next turn is built from the narrowed levels; the turn running now is not left with the
    old ones. A widening stops nothing."""
    from galaius.agents import registry as reg
    from galaius.fence import FenceSpec
    spec = FenceSpec(working_directory=home, levels_file=runner.config_path, start=home / "docs", state=tmp_path / "state")
    fenced, loose = reg.AgentRun(run_id="fenced", provider="claude", name="a", fence=spec), reg.AgentRun(run_id="loose", provider="claude", name="b")
    stopped: list[str] = []
    monkeypatch.setattr(reg, "running_runs", lambda: [fenced, loose])
    monkeypatch.setattr(reg, "stop", lambda run_id, **_: stopped.append(run_id) or True)
    _ask(runner, "place_level", path="notes", level="read")
    assert stopped == []
    _ask(runner, "place_level", path="docs", level="read")
    assert stopped == ["fenced"] and logged[-1]["op"] == "stopped" and logged[-1]["runs"] == ["fenced"]
