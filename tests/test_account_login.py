"""`interact login`: which server it signs in at, and the agents question it asks once."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from interact import account_login
from interact.account_login import AccountLogin, LoginError
from interact.agents.catalog_connection import CatalogConnection
from interact.machine_service import MACHINE_SERVICE
from interact.machines import MachineRunner

PUBLIC = "https://interact.example.org"
TUNNEL = "http://127.0.0.1:8817"


@pytest.mark.parametrize("remembered, answering, expected", [
    (TUNNEL, {TUNNEL, PUBLIC}, PUBLIC),  # tunnel up: the public address it names is used
    (TUNNEL, {TUNNEL}, TUNNEL),          # public unreachable from here: the tunnel still works
    (TUNNEL, set(), None),               # tunnel down: never the target, asked instead
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


# ---- the agents question, right after the approval ------------------------------------------

@pytest.fixture
def joining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh computer: its home with two folders, a real machine file under it, the server faked."""
    home = tmp_path / "home"
    for folder in ("dev", "work", ".secret"):
        (home / folder).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(CatalogConnection, "path", classmethod(lambda cls: home / ".config" / "interact" / "catalog.json"))
    issued = SimpleNamespace(workspace=SimpleNamespace(id=uuid4(), name="My workspace"), approved_by="owner", machine=SimpleNamespace(id=uuid4(), name="pc2"),
                             machine_token=SecretStr(uuid4().hex * 2), **{"api_key": SimpleNamespace(**{"secret": SecretStr(uuid4().hex * 2)})})
    for name, value in {"skew": lambda self, http: None, "start": lambda self, http, runs: SimpleNamespace(verification_uri_complete="https://x/link", user_code="ABCD-EFGH", expires_in=600),
                        "wait": lambda self, http, started: issued, "online": lambda self, http, issued: True, "revoke": lambda self, http, key: {}}.items():
        monkeypatch.setattr(account_login.AccountLogin, name, value)
    monkeypatch.setattr(account_login.AccountLogin, "synced", staticmethod(lambda connection: "Synced: nothing"))
    monkeypatch.setattr(type(MACHINE_SERVICE), "install", lambda self: None)
    monkeypatch.setattr(type(MACHINE_SERVICE), "after_logout", lambda self: True)
    return home


@pytest.mark.parametrize("tty, answers, flags, saved, said", [
    # (run_agents, agent_roots, continue_conversations, answer_approvals)
    (True, ["y", "y", "dev, ~/work/", "y", "n"], {}, (True, ["dev", "work"], True, False), "start them in dev, work"),  # asked; ~ path made relative
    (True, ["y", ""], {}, (False, [], False, False), "Agents: off here"),                              # Enter keeps the default No; nothing more asked
    (True, ["y", "y", "", "", ""], {}, (True, [], False, False), "no folder to start them in"),         # yes, Enter = no folder, opt-ins default No
    (True, ["y", "y", "dev,.secret,../x", "dev,nope/../work", "", "y"], {}, (True, ["dev", "work"], False, True), "Refused: .secret, ../x"),  # re-asked once
    (True, ["y", "y", ".secret", ".secret", "n", "n"], {}, (True, [], False, False), "Left out: .secret"),  # refused twice: left out
    (True, ["y"], {"agent_folders": ("work",)}, (True, ["work"], False, False), "start them in work"),  # a flag answers ahead, never asked
    (True, ["y"], {"agent_opt_ins": {"answer_approvals": True, "continue_conversations": None}}, (True, [], False, True), "approvals answered from the web: on"),
    (True, ["y"], {"agents": False}, (False, [], False, False), "Agents: off here"),
    (False, [], {"yes": True}, (False, [], False, False), "Agents: off here"),                         # no terminal: defaults, never blocks
    (False, [], {"yes": True, "agents": True, "agent_opt_ins": {"continue_conversations": True}}, (True, [], True, False), "continued from the web: on"),
])
def test_agents_asked_once_at_login(joining: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                    tty: bool, answers: list[str], flags: dict, saved: tuple, said: str) -> None:
    replies = iter(answers)
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda question: next(replies))
    account_login.login("https://interact.example.org", allow_runs=False, open_browser=False, **{"yes": False, **flags})
    machine = MachineRunner().load()
    assert (machine.run_agents, list(machine.agent_roots), machine.continue_conversations, machine.answer_approvals) == saved
    assert next(replies, None) is None  # every scripted answer was asked for, no more
    assert said in capsys.readouterr().out


@pytest.mark.parametrize("flags, error", [
    ({"agent_folders": (".secret",)}, "cannot let agents start in .secret"),
    ({"agents": False, "agent_folders": ("dev",)}, "contradict"),
    ({"agents": False, "agent_opt_ins": {"continue_conversations": True}}, "contradict"),
])
def test_bad_agent_flags_refused_before_sign_in(joining: Path, flags: dict, error: str) -> None:
    with pytest.raises(LoginError, match=error):
        account_login.login("https://interact.example.org", allow_runs=False, yes=True, open_browser=False, **flags)
    assert not MachineRunner.default_config_path().exists()


def test_login_flags_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr("interact.cli.login_command.sign_in", lambda server, **options: seen.update(options))
    from interact.cli.app import app
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
    account_login.login(PUBLIC, allow_runs=False, yes=True, open_browser=False, agent_folders=("dev",), agent_opt_ins={"continue_conversations": True})
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


@pytest.mark.parametrize("remote, tty, answers, flags, saved", [
    # (run_agents, agent_roots, continue_conversations, answer_approvals); remote: the PC takes its web page's settings
    (False, True, ["", "", "", ""], {}, (True, ["dev"], True, False)),                    # Enter keeps every current answer
    (False, True, ["", "work", "n", "y"], {}, (True, ["work"], False, True)),             # changed: folder replaced, opt-ins flipped
    (False, True, ["", "-", "", ""], {}, (True, [], True, False)),                       # - clears the folders
    (False, True, ["n"], {}, (False, ["dev"], True, False)),                             # agents off, the rest kept for later
    (True, True, [], {}, (True, ["dev"], True, False)),                                  # web control on: nothing asked, unchanged
    (True, False, [], {"agent_opt_ins": {"answer_approvals": True}}, (True, ["dev"], True, True)),  # a flag changes only what it names
    (True, False, [], {"agents": False}, (False, ["dev"], True, False)),
    (True, False, [], {}, (True, ["dev"], True, False)),                                # no terminal, no flag: unchanged
])
def test_connected_login_asks_nothing_web_control_covers_and_restarts_the_service(connected: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                                                    remote: bool, tty: bool, answers: list[str], flags: dict, saved: tuple) -> None:
    """Re-running the installer on a connected PC is all an older one needs: its service restarts
    on the new build, and settings its web page holds are not asked again."""
    MachineRunner().update(lambda current: current.model_copy(update={"remote_settings": remote}))
    capsys.readouterr()
    replies = iter(answers)
    calls: list[str] = []
    for action in ("install", "stop", "start"):
        monkeypatch.setattr(type(account_login.MACHINE_SERVICE), action, lambda self, action=action: calls.append(action))
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda question: next(replies))
    account_login.login(None, allow_runs=False, open_browser=False, **{"yes": False, **flags})
    machine = MachineRunner().load()
    assert (machine.run_agents, list(machine.agent_roots), machine.continue_conversations, machine.answer_approvals) == saved
    assert next(replies, None) is None and calls == ["install", "stop", "start"]
    out = capsys.readouterr().out
    assert f"already connected to {PUBLIC} as pc2." in out and "Background service restarted" in out and "Agents: " in out


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
            account_login.login(moved_to, allow_runs=False, yes=True, open_browser=False)
        assert MachineRunner.default_config_path().read_bytes() == before and calls == []
        return
    account_login.login(moved_to, allow_runs=False, yes=True, open_browser=False)
    machine = MachineRunner().load()
    assert asked == [(moved_to, token)] and machine.server_url == moved_to and machine.token.get_secret_value() == token
    assert CatalogConnection.load().endpoint == moved_to and AccountLogin.remembered_path().read_text().strip() == moved_to
    assert calls == ["install", "stop", "start"] and f"now connects to {moved_to} (was {PUBLIC})" in capsys.readouterr().out
