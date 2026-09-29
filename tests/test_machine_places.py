"""Levels from the web, as the PC answers signed requests (`interact.machine_places.PlaceDesk`):
narrowing applies at once, widening waits on the PC until its owner confirms it there (no passkey
here), every change lands in the PC's own log with its digest, browsing needs the PC's
opt-in, and a review can be read or dropped from the web, never accepted."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from interact_core import MACHINE_AGENT_REQUESTS
from interact.machine_places import PlaceDesk
from interact.machines import MachineConfig, MachineRunner
from interact.place_reviews import PlaceReviews

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


def test_a_signed_web_widening_waits_on_the_pc_until_its_owner_approves_it(runner: MachineRunner, logged: list[dict]) -> None:
    answer = _ask(runner, "place_level", path="notes", level="read")
    assert answer["error"] is None and answer["change"]["level"] == "read" and answer["change"]["previous"] == "hidden"
    assert runner.load().places == {"docs": "write"}  # the server's signature alone never widens
    assert [entry["method"] for entry in logged] == ["web-request"] and logged[0]["digest"] == answer["change"]["digest"]
    [applied] = PlaceDesk(runner).approve(None, confirm=lambda change: True)
    assert applied.path == "notes" and runner.load().places == {"docs": "write", "notes": "read"} and runner.load().pending_places == ()
    assert logged[-1]["method"] == "pc-confirm" and logged[-1]["digest"] == answer["change"]["digest"]


def test_a_web_narrowing_applies_at_once(runner: MachineRunner, logged: list[dict]) -> None:
    answer = _ask(runner, "place_level", path="docs/reports", level="see")
    assert answer["change"] is None and runner.load().places == {"docs": "write", "docs/reports": "see"}
    assert logged[-1]["method"] == "web-narrow" and len(logged[-1]["digest"]) == 64


def test_a_pending_widening_is_withdrawn_from_the_web(runner: MachineRunner, logged: list[dict]) -> None:
    change = _ask(runner, "place_level", path="photos", level="write")["change"]
    answer = _ask(runner, "place_cancel", change_id=change["id"])
    assert answer["places"]["pending"] == [] and runner.load().places == {"docs": "write"}
    assert PlaceDesk(runner).approve(None, confirm=lambda change: True) == []


def test_approval_rechecks_the_folder_as_it_is_now(runner: MachineRunner, home: Path, logged: list[dict]) -> None:
    _ask(runner, "place_level", path="photos", level="read")
    (home / "photos").rmdir()
    (home / "photos").symlink_to(home / "docs")
    assert PlaceDesk(runner).approve(None, confirm=lambda change: True) == []
    assert "photos" not in runner.load().places and runner.load().pending_places == ()
    assert logged[-1]["method"] == "pc-refused"


def test_the_pc_owner_declining_leaves_the_widening_pending(runner: MachineRunner, logged: list[dict]) -> None:
    _ask(runner, "place_level", path="notes", level="write")
    assert PlaceDesk(runner).approve(None, confirm=lambda change: False) == []
    assert len(runner.load().pending_places) == 1 and "notes" not in runner.load().places


def test_browsing_needs_the_pcs_own_opt_in(runner: MachineRunner, logged: list[dict]) -> None:
    assert "interact machine browse on" in _ask(runner, "place_browse")["error"]
    runner.update(lambda config: config.model_copy(update={"browse": True}))
    names = {entry["name"]: entry["level"] for entry in _ask(runner, "place_browse")["browse"]}
    assert names == {"docs": "write", "notes": "hidden", "photos": "hidden"}


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
    old ones. A widening waits on the PC and stops nothing."""
    from interact.agents import registry as reg
    from interact.fence import FenceSpec
    spec = FenceSpec(working_directory=home, levels_file=runner.config_path, start=home / "docs", state=tmp_path / "state")
    fenced, loose = reg.AgentRun(run_id="fenced", provider="claude", name="a", fence=spec), reg.AgentRun(run_id="loose", provider="claude", name="b")
    stopped: list[str] = []
    monkeypatch.setattr(reg, "running_runs", lambda: [fenced, loose])
    monkeypatch.setattr(reg, "stop", lambda run_id, **_: stopped.append(run_id) or True)
    _ask(runner, "place_level", path="notes", level="read")
    assert stopped == []
    _ask(runner, "place_level", path="docs", level="read")
    assert stopped == ["fenced"] and logged[-1]["op"] == "stopped" and logged[-1]["runs"] == ["fenced"]


def test_confirming_without_asking_names_the_widening(runner: MachineRunner, logged: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """`--yes` alone would also confirm widenings queued after the owner last looked."""
    from interact.cli.machine_command import machine_approve
    _ask(runner, "place_level", path="notes", level="read")
    monkeypatch.setattr(MachineRunner, "default_config_path", staticmethod(lambda: runner.config_path))
    with pytest.raises(SystemExit, match="name the widening"):
        machine_approve(None, yes=True)
    assert "notes" not in runner.load().places
