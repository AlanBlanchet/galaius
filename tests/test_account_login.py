"""`galaius login`: which server it signs in at, asking nothing; the installers' detached sign-in."""

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from galaius import account_login
from galaius.account_login import AccountLogin, LoginError
from galaius_core import DeviceLoginStarted
from galaius.agents.catalog_connection import CatalogConnection
from galaius.machine_service import MACHINE_SERVICE
from galaius.machines import MachineRunner

PUBLIC = "https://galaius.example.org"
TUNNEL = "http://127.0.0.1:8817"


@pytest.mark.parametrize("remembered, answering, expected", [
    (TUNNEL, {TUNNEL, PUBLIC}, PUBLIC),  # tunnel up: the public address it names is used
    (TUNNEL, {TUNNEL}, TUNNEL),          # public unreachable from here: the tunnel still works
    (TUNNEL, set(), None),               # tunnel down: never the target
    (PUBLIC, set(), PUBLIC),             # a public address is kept as remembered
])
def test_remembered_server(monkeypatch: pytest.MonkeyPatch, remembered: str, answering: set[str], expected: str | None) -> None:
    def client(self: AccountLogin) -> httpx.Client:
        def answer(request: httpx.Request) -> httpx.Response:
            if self.server not in answering:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"server": PUBLIC} if request.url.path == "/v1/install" else {})
        return httpx.Client(base_url=self.server, transport=httpx.MockTransport(answer))

    monkeypatch.setattr(AccountLogin, "client", client)
    chosen = AccountLogin.parsed(remembered).public()
    assert (chosen.server if chosen else None) == expected


# ---- a joining computer: nothing asked, flags still apply ------------------------------------

@pytest.fixture
def joining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh computer: its home with two folders, a real machine file under it, the server faked."""
    home = tmp_path / "home"
    for folder in ("dev", "work", ".secret"):
        (home / folder).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(CatalogConnection, "path", classmethod(lambda cls: home / ".config" / "galaius" / "catalog.json"))
    issued = SimpleNamespace(workspace=SimpleNamespace(id=uuid4(), name="My workspace"), approved_by="owner", machine=SimpleNamespace(id=uuid4(), name="pc2"),
                             machine_token=SecretStr(uuid4().hex * 2), **{"api_key": SimpleNamespace(**{"secret": SecretStr(uuid4().hex * 2)})})
    for name, value in {"skew": lambda self, http: None, "start": lambda self, http, runs: SimpleNamespace(verification_uri_complete="https://x/link", user_code="ABCD-EFGH", expires_in=600),
                        "wait": lambda self, http, started, current=None: issued, "online": lambda self, http, machine, key: True, "revoke": lambda self, http, key: {}}.items():
        monkeypatch.setattr(account_login.AccountLogin, name, value)
    monkeypatch.setattr(account_login.AccountLogin, "synced", staticmethod(lambda connection: "Synced: nothing"))
    monkeypatch.setattr(account_login.AccountLogin, "page", lambda self, http, machine: f"{self.server}/plateform/#data?computer={machine.machine_id.hex}")
    monkeypatch.setattr(type(MACHINE_SERVICE), "install", lambda self: None)
    monkeypatch.setattr(type(MACHINE_SERVICE), "after_logout", lambda self: True)
    return home


@pytest.mark.parametrize("tty, flags, saved, said", [
    # (run_agents, agent_roots, continue_conversations, answer_approvals)
    (True, {}, (False, [], False, False), "Agents: off here. Turn them on from its page"),   # a terminal: still nothing asked
    (True, {"agent_folders": ("work", "~/dev/")}, (True, ["work", "dev"], False, False), "start them in work, dev"),  # flags set them ahead
    (True, {"agent_opt_ins": {"answer_approvals": True, "continue_conversations": None}}, (True, [], False, True), "approvals answered from the web: on"),
    (True, {"agents": False}, (False, [], False, False), "Agents: off here"),
    (False, {"agents": True, "agent_opt_ins": {"continue_conversations": True}}, (True, [], True, False), "continued from the web: on"),
])
def test_a_joining_computer_is_asked_nothing_after_the_approval(joining: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                                 tty: bool, flags: dict, saved: tuple, said: str) -> None:
    """« It's just to install … everything should be done from the web »: once approved, the
    service starts and nothing is asked here; flags given ahead still set the agent settings."""
    events: list[str] = []
    monkeypatch.setattr(type(MACHINE_SERVICE), "install", lambda self: events.append("service"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail(f"asked: {question}"))
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=False, **flags)
    machine = MachineRunner().load()
    assert (machine.run_agents, list(machine.agent_roots), machine.continue_conversations, machine.answer_approvals) == saved
    out = capsys.readouterr().out
    assert events == ["service"] and said in out and f"#data?computer={machine.machine_id.hex}" in out


@pytest.mark.parametrize("flags, error", [
    ({"agent_folders": (".secret",)}, "cannot let agents start in .secret"),
    ({"agents": False, "agent_folders": ("dev",)}, "contradict"),
    ({"agents": False, "agent_opt_ins": {"continue_conversations": True}}, "contradict"),
])
def test_bad_agent_flags_refused_before_sign_in(joining: Path, flags: dict, error: str) -> None:
    with pytest.raises(LoginError, match=error):
        account_login.login("https://galaius.example.org", allow_runs=False, open_browser=False, **flags)
    assert not MachineRunner.default_config_path().exists()


@pytest.mark.parametrize("refused, running, code", [
    ("Windows refused to register the logon task (Access is denied.)", None, "service_unavailable"),
    (None, False, "service_stopped"),       # set up, but its program is not running
    (None, True, "channel_unreachable"),    # running, never reached the server
])
def test_not_online_says_why_here_and_on_its_page(joining: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                   refused: str | None, running: bool | None, code: str) -> None:
    def install(self) -> None:
        if refused:
            raise account_login.ServiceUnavailable(refused)
    reported: list[tuple[str, str]] = []
    monkeypatch.setattr(type(MACHINE_SERVICE), "install", install)
    monkeypatch.setattr(type(MACHINE_SERVICE), "running", lambda self: bool(running))
    monkeypatch.setattr(type(MACHINE_SERVICE), "last_words", lambda self, lines=3: "Task Scheduler: last result 0x1\nTraceback: boom")
    monkeypatch.setattr(account_login.AccountLogin, "online", lambda self, http, machine, key: False)
    monkeypatch.setattr(MachineRunner, "report_problem", classmethod(lambda cls, machine, said, detail="", *rest: reported.append((said, detail)) or True))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(account_login.MachineErrorReports, "asked", lambda self: True)  # its question reached the server (test_machines)
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=False)
    said = capsys.readouterr()
    assert [value for value, _ in reported] == [code] and "Connected:" not in said.out
    assert (refused or "Traceback: boom") in said.err and (refused or "0x1") in reported[0][1]
    # The installer asks nothing: one line says the report waits for an answer on the PC's page.
    assert "An error report is ready, not sent" in said.err and "#data?computer=" in said.err


def test_login_flags_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr("galaius.cli.login_command.sign_in", lambda server, **options: seen.update(options))
    from galaius.cli.app import app
    with pytest.raises(SystemExit, match="0"):
        app(["login", "--server", "x.org", "--agent-folder", "dev", "--agent-folder", "work", "--no-agents",
             "--continue-conversations", "--no-answer-approvals"])
    assert (seen["agents"], seen["agent_folders"], seen["agent_opt_ins"]) == (False, ("dev", "work"), {"continue_conversations": True, "answer_approvals": False})


# ---- already connected: only the agent questions, the current settings as defaults ----------

@pytest.fixture
def connected(joining: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`joining`, signed in once with agents on in dev and conversations continued; a second sign-in
    would fail loudly, and the server lists this computer as pc2."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    account_login.login(PUBLIC, allow_runs=False, open_browser=False, agent_folders=("dev",), agent_opt_ins={"continue_conversations": True})
    machine = MachineRunner().load()

    def refused(*_: object) -> None:
        raise AssertionError("signed in again")

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/workspaces/{machine.workspace_id}/machines" and request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json=[{"id": str(machine.machine_id), "name": "pc2", "state": "online"}])

    connect = CatalogConnection.connect
    monkeypatch.setattr(AccountLogin, "start", refused)
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, transport=None: connect(self, transport=httpx.MockTransport(answer)))
    return joining


@pytest.mark.parametrize("remote, tty, flags, saved", [
    # (run_agents, agent_roots, continue_conversations, answer_approvals); remote: the PC takes its web page's settings
    (False, True, {}, (True, ["dev"], True, False)),                                   # web control off, a terminal: still nothing asked
    (True, True, {}, (True, ["dev"], True, False)),
    (True, False, {"agent_opt_ins": {"answer_approvals": True}}, (True, ["dev"], True, True)),  # a flag changes only what it names
    (True, False, {"agents": False}, (False, ["dev"], True, False)),
])
def test_connected_login_asks_nothing_and_restarts_the_service(connected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                               remote: bool, tty: bool, flags: dict, saved: tuple) -> None:
    """Re-running the installer on a connected PC is all an older one needs: its service restarts
    on the new build, and nothing is asked whatever the terminal or its settings."""
    MachineRunner().update(lambda current: current.model_copy(update={"remote_settings": remote}))
    capsys.readouterr()
    calls: list[str] = []
    for action in ("install", "stop", "start"):
        monkeypatch.setattr(type(account_login.MACHINE_SERVICE), action, lambda self, action=action: calls.append(action))
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail(f"asked: {question}"))
    account_login.login(None, allow_runs=False, open_browser=False, **flags)
    machine = MachineRunner().load()
    assert (machine.run_agents, list(machine.agent_roots), machine.continue_conversations, machine.answer_approvals) == saved
    assert calls == ["stop", "install"]
    out = capsys.readouterr().out
    assert f"already connected to {PUBLIC} as pc2." in out and "Online: its background service restarted" in out and "Agents: " in out


@pytest.mark.parametrize("verdict, refusal", [("accepted", None), ("refused", "does not know it"), ("unreachable", "does not answer")])
def test_connected_elsewhere_moves_only_to_a_server_that_holds_this_computer(connected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                                               verdict: str, refusal: str | None) -> None:
    """The server moved with its data: the computer follows the new address as the same machine,
    proven by the new server's channel taking its own token; a server that refuses it changes nothing."""
    moved_to = "https://new.example.org"
    before = MachineRunner.default_config_path().read_bytes()
    token = MachineRunner().load().token.get_secret_value()
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(account_login, "_channel_accepts", lambda server, offered: asked.append((server, offered)) or verdict)
    calls: list[str] = []
    for action in ("install", "stop", "start"):
        monkeypatch.setattr(type(account_login.MACHINE_SERVICE), action, lambda self, action=action: calls.append(action))
    if refusal is not None:
        with pytest.raises(LoginError, match=refusal):
            account_login.login(moved_to, allow_runs=False, open_browser=False)
        assert MachineRunner.default_config_path().read_bytes() == before and calls == []
        return
    account_login.login(moved_to, allow_runs=False, open_browser=False)
    machine = MachineRunner().load()
    assert asked == [(moved_to, token)] and machine.server_url == moved_to and machine.token.get_secret_value() == token
    assert CatalogConnection.load().endpoint == moved_to and AccountLogin.remembered_path().read_text().strip() == moved_to
    assert calls == ["stop", "install"] and f"now connects to {moved_to} (was {PUBLIC})" in capsys.readouterr().out


def test_logout_finishes_where_the_service_cannot_be_removed(connected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A computer whose service manager does not answer (no systemd bus) still signs out and
    drops its credentials: the server already revoked it, so nothing can connect any more."""
    def refused(self) -> None:
        raise account_login.ServiceUnavailable("Failed to connect to bus: No such file or directory")
    monkeypatch.setattr(type(MACHINE_SERVICE), "remove", refused)
    monkeypatch.setattr(AccountLogin, "revoke", lambda self, http, key: {"machine": True})
    account_login.logout()
    said = capsys.readouterr()
    assert not MachineRunner.default_config_path().exists() and CatalogConnection.load() is None
    assert "Failed to connect to bus" in said.err and "Signed out" in said.out


@pytest.mark.parametrize("answer, expected", [
    (httpx.Response(200, json={"origin": "https://galaius.example.org/plateform", "server": "https://galaius.example.org"}), "https://galaius.example.org/plateform/#data?computer="),
    (httpx.Response(404), "https://galaius.example.org/#data?computer="),   # an older server: its root
])
def test_the_page_named_is_where_the_server_serves_the_app(answer: httpx.Response, expected: str) -> None:
    account = AccountLogin.at("https://galaius.example.org")
    machine = SimpleNamespace(machine_id=uuid4())
    with httpx.Client(base_url=account.server, transport=httpx.MockTransport(lambda request: answer)) as http:
        assert account.page(http, machine) == expected + machine.machine_id.hex


def _started(code: str = "BCDF-GHJK") -> DeviceLoginStarted:
    return DeviceLoginStarted(device_code="d" * 40, user_code=code, verification_uri="https://x/plateform/link",
                              verification_uri_complete=f"https://x/plateform/link?code={code}", expires_in=600, interval=5)


@pytest.mark.parametrize("opened", [True, False])
def test_detach_opens_the_approval_hands_the_wait_over_and_says_one_line(joining: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], opened: bool) -> None:
    """The install line: the code is started, the waiting handed to a detached `galaius login
    --resume` (its sign-in on stdin, never on its command line), the page opened, and the installer
    ends on « Installé : continuez dans votre navigateur » (plus the link where no browser opened)."""
    monkeypatch.setattr(AccountLogin, "begun", lambda self, http, runs, open_browser: (_started(), False))
    monkeypatch.setattr(AccountLogin, "opened", staticmethod(lambda url: opened))
    handed: list[account_login.HandedOff] = []
    monkeypatch.setattr(account_login, "_detached", lambda account, value: handed.append(value))
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail(f"asked: {question}"))
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=True, detach=True, agent_folders=("dev",))
    [handoff] = handed
    received = account_login.HandedOff.model_validate_json(handoff.payload())
    assert received.started.device_code.get_secret_value() == "d" * 40 and received.agents.folders == ("dev",)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Installé : continuez dans votre navigateur" and (len(out) == 1) == opened
    assert not MachineRunner.default_config_path().exists()  # saved only by the detached process, once approved
    assert account_login.PRIVATE_FILES.read_text(account_login.Pending.path())  # the pending page (with its code) is private: readable only as such


def test_a_second_install_line_reopens_the_same_page_while_its_waiter_lives(joining: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A newbie who sees nothing happen runs the line again: same page, no second code, so
    whichever tab is approved, the one waiter collects it; once that waiter is gone, a new sign-in."""
    codes = iter(["BCDF-GHJK", "LMNP-QRST"])
    monkeypatch.setattr(AccountLogin, "begun", lambda self, http, runs, open_browser: (_started(next(codes)), False))
    pages: list[str] = []
    monkeypatch.setattr(AccountLogin, "opened", staticmethod(lambda url: pages.append(url) or True))
    handed: list[account_login.HandedOff] = []
    monkeypatch.setattr(account_login, "_detached", lambda account, value: handed.append(value))
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=True, detach=True)
    first = account_login.Pending.read  # its waiter has not refreshed it yet: not to be reused
    assert first() is None
    account_login.Pending.model_validate_json(account_login.Pending.path().read_text()).current()  # the waiter's first poll
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=True, detach=True)
    assert len(handed) == 1 and pages == ["https://x/plateform/link?code=BCDF-GHJK"] * 2
    stale = (datetime.now() - timedelta(minutes=2)).timestamp()
    os.utime(account_login.Pending.path(), (stale, stale))  # its waiter stopped refreshing it
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=True, detach=True)
    assert len(handed) == 2 and pages[-1].endswith("LMNP-QRST")


def test_resume_finishes_what_detach_handed_over_and_a_newer_sign_in_takes_over(joining: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = account_login.Pending(mark="mine", link="https://x/link?code=BCDF-GHJK", expires_at=datetime.now(UTC) + timedelta(minutes=9))
    pending.write()
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(account_login.HandedOff(started=_started(), mark="mine").payload()), isatty=lambda: False))
    seen: list[bool] = []
    issued = AccountLogin.wait
    monkeypatch.setattr(AccountLogin, "wait", lambda self, http, value, current: seen.append(current()) or issued(self, http, value))
    account_login.login("https://galaius.example.org", allow_runs=False, open_browser=False, resume=True)
    assert seen == [True] and MachineRunner().load().run_agents is False and not account_login.Pending.path().exists()
    account_login.Pending(mark="a later one", link="x", expires_at=datetime.now(UTC)).write()
    assert not pending.current()


def test_a_superseded_waiter_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_login.time, "sleep", lambda seconds: None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("polled after being superseded"))) as http, pytest.raises(LoginError, match="newer install"):
        AccountLogin.at("https://galaius.example.org").wait(http, _started(), lambda: False)


def test_detach_on_a_connected_computer_opens_its_page_and_restarts_it_detached(connected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    handed: list[account_login.HandedOff] = []
    pages: list[str] = []
    monkeypatch.setattr(account_login, "_detached", lambda account, value: handed.append(value))
    monkeypatch.setattr(AccountLogin, "opened", staticmethod(lambda url: pages.append(url) or True))
    capsys.readouterr()
    account_login.login(None, allow_runs=False, open_browser=True, detach=True)
    assert [value.started for value in handed] == [None] and pages and "#data?computer=" in pages[0]
    assert capsys.readouterr().out.splitlines() == ["Installé : continuez dans votre navigateur"]


def test_detach_is_a_real_detached_process_with_a_private_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its own session (outlives the installer and its terminal), its output in login.log (0600), the hand-off on stdin."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    seen: dict = {}

    class Child:
        def __init__(self, argv, **options):
            seen.update(options, argv=argv)
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: seen.setdefault("payload", self.stdin.getvalue())

    monkeypatch.setattr(account_login.subprocess, "Popen", Child)
    account_login._detached(AccountLogin.at("https://galaius.example.org"), account_login.HandedOff(started=_started(), mark="m"))
    assert seen["argv"][1:] == ["-m", "galaius", "login", "--resume", "--server", "https://galaius.example.org"]
    assert b"d" * 40 in seen["payload"] and b"d" * 40 not in " ".join(seen["argv"]).encode()
    if sys.platform == "win32":
        assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW and seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["start_new_session"] is True and (tmp_path / "galaius" / "login.log").stat().st_mode & 0o777 == 0o600
