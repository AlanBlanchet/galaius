"""`galaius login` / `galaius logout`: this computer joins (or leaves) a Galaius account.

The CLI asks the server for a short code, the signed-in owner allows this computer on the server's
/link page (opened in the browser), and the CLI collects what the approval issued
(`galaius_core.DeviceLoginIssued`): the machine token — saved where `galaius machine connect` reads
it, then kept connected by this computer's background service (`MACHINE_SERVICE`) — and a
read-only workspace key the CLI's `agents` / `workflows` commands use. Nothing is ever asked in the
terminal: every choice (agents, their folders, folder levels, the name) is made on the computer's
page on the web, the safe defaults holding until then (agents off, every folder hidden). The
installers run `galaius login --detach`: the browser opens on the approval, a detached process
finishes the sign-in and starts the service, and the installer is done."""

import asyncio
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal
from urllib.parse import urlsplit

import httpx
import websockets
from galaius_core import MACHINE_TOKEN_PATTERN, DeviceLoginIssued, DeviceLoginStart, DeviceLoginStarted, DeviceTokenRefusal, MachineSummary, ReleaseInfo
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter

from galaius import USER_AGENT, __version__
from galaius.agents.catalog import AgentCatalog
from galaius.agents.catalog_connection import CatalogConnection
from galaius.cli.prompts import PromptMode
from galaius.server_prompts import ServerPrompts
from galaius.machine_service import MACHINE_SERVICE, ServiceUnavailable
from galaius.machines import MachineConfig, MachineRunner
from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES
from galaius.processes import process_started
from galaius.windowless import console_python


class LoginError(Exception):
    """Something the person can act on, said in their words."""


class AccountLogin(BaseModel):
    """One server this computer signs in to; every path it writes is derived here."""

    model_config = ConfigDict(frozen=True)
    server: str
    #: The poll gives up after this long (the server's own code lifetime is shorter).
    patience: ClassVar[float] = 15 * 60
    platforms: ClassVar[dict[str, Literal["linux", "macos", "windows"]]] = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}

    @staticmethod
    def remembered_path() -> Path:
        """Written by the server's install script."""
        return MachineRunner.default_config_path().parent / "login-server"

    @classmethod
    def at(cls, server: str | None) -> "AccountLogin":
        """`server`, else the one remembered (by the server's installer or an earlier login); a
        bare host means https."""
        if server is None:
            try:
                remembered = cls.parsed(cls.remembered_path().read_text(encoding="utf-8"))
            except (FileNotFoundError, LoginError):
                remembered = None
            account = None if remembered is None else remembered.public()
            if account is None:
                raise LoginError("which Galaius server? Pass --server https://… (the install line from your Galaius page sets it)")
            return account
        return cls.parsed(server)

    def public(self) -> "AccountLogin | None":
        """A remembered server as another computer reaches it: an address on this computer only (an
        SSH tunnel's 127.0.0.1) is swapped for the public address it names, and dropped when it does
        not answer; a public address is kept as is."""
        if urlsplit(self.server).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return self
        try:
            with self.client() as http:
                named = http.get("/v1/install").json().get("server")
                public = None if not named else self.parsed(named)
                if public is not None and urlsplit(public.server).hostname not in {"127.0.0.1", "localhost", "::1"} and public.answers():
                    return public
                return self if http.get("/v1/version").status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError, LoginError):
            return None

    def answers(self) -> bool:
        try:
            with self.client() as http:
                return http.get("/v1/version").status_code == 200
        except httpx.HTTPError:
            return False

    @classmethod
    def parsed(cls, server: str) -> "AccountLogin":
        server = server.strip()
        if "://" not in server:
            server = f"https://{server}"
        parts = urlsplit(server)
        loopback = parts.hostname in {"127.0.0.1", "localhost", "::1"}
        if parts.scheme not in ({"https", "http"} if loopback else {"https"}) or not parts.hostname or parts.username or parts.path not in {"", "/"}:
            raise LoginError(f"{server} is not a Galaius server address (https://…)")
        return cls(server=f"{parts.scheme}://{parts.netloc}")

    @property
    def key_path(self) -> Path:
        """The CLI's own read key from the sign-in. It acts for nobody (the server's rule): what this
        PC's agents read (catalog, tools, prompts) goes through its link, which acts for its owner."""
        return CatalogConnection.path().parent / "credentials" / f"{urlsplit(self.server).netloc.replace(':', '_')}.key"

    def key(self) -> str | None:
        """That key, None when this computer holds none (never signed in here, or already out)."""
        try:
            return PRIVATE_FILES.read_secret(self.key_path)
        except (OSError, ValueError):
            return None

    def remember(self) -> None:
        """The next `galaius login` on this computer needs no address."""
        path = self.remembered_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(self.server + "\n", encoding="utf-8")

    @staticmethod
    def synced(connection: CatalogConnection) -> str:
        """The account's agent catalog and prompts, installed here as `agents sync` and
        `prompts sync` do: what was synced, or why not (the computer stays connected either way)."""
        done = []
        try:
            catalog = AgentCatalog.refresh(connection)
            catalog.connection.save()
            done.append(f"{len(catalog.snapshot.agents)} agents")
        except (OSError, ValueError, httpx.HTTPError) as error:
            done.append(f"agents not synced ({error or type(error).__name__}; then run: galaius agents sync)")
        try:
            PromptMode.installed(ServerPrompts(connection=connection))
            done.append("prompts installed")
        except (OSError, ValueError, httpx.HTTPError) as error:
            done.append(f"prompts not synced ({error or type(error).__name__}; then run: galaius prompts sync)")
        return "Synced: " + ", ".join(done)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.server, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(15, connect=10), trust_env=False, follow_redirects=False)

    # ---- sign in ---------------------------------------------------------------------------

    def skew(self, http: httpx.Client) -> str | None:
        """What differs between this computer's Galaius and the server's, said once."""
        try:
            server = ReleaseInfo.model_validate_json(http.get("/v1/version").content)
        except (httpx.HTTPError, ValueError):
            return None
        mine = version("galaius-core")
        return None if server.core_version == mine else f"this computer runs galaius-core {mine}, the server {server.core_version}: run the install line from your Galaius page again"

    def start(self, http: httpx.Client, runs: bool, replaces: SecretStr | None = None) -> DeviceLoginStarted:
        """A sign-in code; `replaces` (`UnreadableMachine.token_for`) proves this computer is the PC it enrolled
        before, so its owner's approval replaces that entry instead of adding a second one."""
        name = socket.gethostname().split(".")[0] or "computer"
        system = self.platforms.get(platform.system())
        if system is None:
            raise LoginError(f"{platform.system()} is not supported yet")
        request = DeviceLoginStart(client_name=name, platform=system, client_version=__version__, runs=runs, replaces=replaces)
        answer = http.post("/v1/device/authorizations", json=request.revealed())
        if answer.status_code == 429:
            raise LoginError("too many sign-in attempts from this network; wait a minute and try again")
        if answer.status_code != 201:
            raise LoginError(f"the server refused to start a sign-in (HTTP {answer.status_code})")
        return DeviceLoginStarted.model_validate_json(answer.content)

    def begun(self, http: httpx.Client, runs: bool, open_browser: bool, replaces: SecretStr | None = None) -> tuple[DeviceLoginStarted, bool]:
        """A sign-in code started, and whether its approval page opened in this computer's browser."""
        if (skew := self.skew(http)) is not None:
            print(f"Note: {skew}.", file=sys.stderr)
        started = self.start(http, runs, replaces)
        return started, open_browser and self.opened(started.verification_uri_complete)

    @staticmethod
    def opened(url: str) -> bool:
        """`url` opened in this computer's browser, when it has one to open it."""
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform in {"darwin", "win32"}):
            return False
        try:
            return webbrowser.open(url)
        except webbrowser.Error:
            return False

    def wait(self, http: httpx.Client, started: DeviceLoginStarted, current: Callable[[], bool] = lambda: True) -> DeviceLoginIssued:
        """The approval collected; `current` false = a newer sign-in took over this one (it stops)."""
        interval, deadline = started.interval, time.monotonic() + min(self.patience, started.expires_in + 30)
        while time.monotonic() < deadline:
            if not current():
                raise LoginError("a newer install of this computer took over this sign-in")
            time.sleep(interval)
            try:
                answer = http.post("/v1/device/token", json={"device_code": started.device_code.get_secret_value()})
            except httpx.TransportError:
                continue  # the server is restarting or the network blinked: ask again next turn
            if answer.status_code == 200:
                return DeviceLoginIssued.model_validate_json(answer.content)
            if answer.status_code >= 500:
                continue  # the server is restarting (a deploy) or its proxy blinked: ask again next turn
            try:
                error = DeviceTokenRefusal.model_validate_json(answer.content).error if answer.status_code == 400 else None
            except ValueError:
                error = None
            if error == "slow_down":
                interval += 5
            elif error == "access_denied":
                raise LoginError("the sign-in was refused on the web page")
            elif error in {"expired_token", "invalid_grant"}:
                raise LoginError("the code expired before it was approved; run `galaius login` again")
            elif error != "authorization_pending":
                raise LoginError(f"the server answered HTTP {answer.status_code} while waiting")
        raise LoginError("nobody approved this computer in time; run `galaius login` again")

    def save(self, issued: DeviceLoginIssued) -> None:
        """The key for the CLI and the machine credential (private files, sealed at rest where the
        system can: `PRIVATE_FILES`), and the CLI's connection."""
        PRIVATE_FILES.write_secret(self.key_path, issued.api_key.secret.get_secret_value())
        MachineRunner().save(MachineConfig(
            server_url=self.server, workspace_id=issued.workspace.id, machine_id=issued.machine.id, token=issued.machine_token,
            permission_ceiling="read_only", working_directory=Path.home(), script_roots=(), run_agents=False,
        ))
        CatalogConnection(endpoint=self.server, workspace_id=issued.workspace.id, auth_mode="machine").save()

    def page(self, http: httpx.Client, machine: MachineConfig) -> str:
        """This computer's page in the web app, where its settings live: the app is where the
        server serves it (`/v1/install` origin, e.g. under /plateform), else at the server's root."""
        try:
            app = str(http.get("/v1/install").json()["origin"]).rstrip("/")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            app = self.server
        return f"{app}/#data?computer={machine.machine_id.hex}"

    #: How long `galaius login` waits for the server to see this computer online (its service
    #: connects within seconds; a first start on a slow PC compiles its Python first).
    online_within: ClassVar[float] = 60

    def online(self, http: httpx.Client, machine: MachineConfig, key: str) -> bool:
        """Whether the server sees `machine` online within `online_within`, read with the account
        key this login holds."""
        headers = {"Authorization": f"Bearer {key}"}
        deadline = time.monotonic() + self.online_within
        while time.monotonic() < deadline:
            try:
                machines = TypeAdapter(tuple[MachineSummary, ...]).validate_json(http.get(f"/v1/workspaces/{machine.workspace_id}/machines", headers=headers).content)
                if any(item.id == machine.machine_id and item.state == "online" for item in machines):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(2)
        return False

    def brought_online(self, http: httpx.Client, machine: MachineConfig, key: str | None) -> bool:
        """This computer's background service (re)started on this build and seen online, else why
        not: said here and sent to its page on the web (`MachineProblem`), so whoever looks there
        reads the reason too. Without an account `key` (its CLI signed out) only started."""
        try:
            MACHINE_SERVICE.install()
        except ServiceUnavailable as refused:
            print(f"Could not start it in the background ({refused}). Keep it connected with:  galaius machine connect", file=sys.stderr)
            MachineRunner.report_problem(machine, "service_unavailable", str(refused), MACHINE_SERVICE.own_last_words())
            if ready := self.report_ready(http, machine):
                print(ready, file=sys.stderr)
            return False
        print("Starting its background connection…", flush=True)
        if key is None:
            print("Started; whether it is online was not checked (this computer's sign-in key could not be read). Check:  galaius machine service status", file=sys.stderr)
            return False
        if self.online(http, machine, key):
            return True
        running = MACHINE_SERVICE.running()
        said = MACHINE_SERVICE.last_words()
        MachineRunner.report_problem(machine, "channel_unreachable" if running else "service_stopped", said, MACHINE_SERVICE.own_last_words())
        state = "runs but has not reached the server" if running else "is not running"
        print(f"Not online after {self.online_within:g} s: its background service {state}." + (f" Its last words:\n{said}" if said else "")
              + f"\nThe reason shows on its page too. Log: {MACHINE_SERVICE.logs}. Try again with:  galaius machine service restart", file=sys.stderr)
        if ready := self.report_ready(http, machine):
            print(ready, file=sys.stderr)
        return False

    def report_ready(self, http: httpx.Client, machine: MachineConfig) -> str | None:
        """The one line a failed install says about its error report, when its question reached the
        server: it waits, unsent, for an answer on the PC's page (nothing is asked here: owner
        2026-10-08 « After install, everything should be done from the web »)."""
        return f"An error report is ready, not sent: answer on this computer's page: {self.page(http, machine)}" if MachineRunner.error_reports().asked() else None

    def connected_name(self, machine: MachineConfig) -> str:
        """This computer's name among the server's machines (asked as the CLI is signed in), its id
        when the server cannot be asked; LoginError when the server answers it no longer has it."""
        if not self.key_path.is_file():
            return f"machine {machine.machine_id}"
        connection = CatalogConnection(endpoint=self.server, workspace_id=machine.workspace_id, auth_mode="token", token_file=self.key_path.absolute())
        try:
            with connection.connect() as http:
                payload = connection.authenticate(http).request(http, "GET", f"/v1/workspaces/{machine.workspace_id}/machines")
            machines = TypeAdapter(tuple[MachineSummary, ...]).validate_json(payload)
        except (OSError, ValueError, httpx.HTTPError):  # a refused or broken catalog answer is a ValueError
            return f"machine {machine.machine_id}"
        found = next((item for item in machines if item.id == machine.machine_id and item.state != "revoked"), None)
        if found is None:
            raise LoginError(f"this computer was removed from the machines of {self.server}; run `galaius login` again: it signs in as a new one")
        return found.name

    def holds(self, machine: MachineConfig) -> bool:
        """Whether this server still holds `machine`'s enrollment, asked with the machine's own token
        on a read that changes nothing (the workspaces its prompts come from; the channel is not
        opened for it, which would show the PC online). Only the server's own refusal of that token
        says no (401 `authentication_failed`): removed on the web, revoked, or never this server's (an
        older install's leftover; also a cloud instance's bootstrap token before its first
        connection, which it then replaces by a new sign-in). Anything else (offline, a server
        restarting, a proxy's own 401) is no verdict: held."""
        try:
            with self.client() as http:
                answer = http.get("/v1/machine/prompt-workspaces", headers=machine.authorization)
            return answer.status_code != 401 or answer.json().get("code") != "authentication_failed"
        except (httpx.HTTPError, ValueError, AttributeError):
            return True

    def revoke(self, http: httpx.Client, secret: str) -> dict[str, object]:
        answer = http.post("/v1/device/logout", headers={"Authorization": f"Bearer {secret}"})
        if answer.status_code == 401:
            return {}  # already revoked on the web: nothing left to revoke
        if answer.status_code != 200:
            raise LoginError(f"the server did not sign this computer out (HTTP {answer.status_code}); nothing was removed here")
        return answer.json()


class AgentChoice(BaseModel):
    """Whether agents may run on this computer, and the folders the web may start them in (names
    under the machine's working directory, checked by `MachineConfig.usable_agent_roots`): given as
    `galaius login` flags over the machine's current settings (`of`: off and none on a joining
    computer); otherwise set on its page on the web. The fields share `MachineConfig`'s names."""

    model_config = ConfigDict(frozen=True)
    run_agents: bool = False
    folders: tuple[str, ...] = ()
    continue_conversations: bool = False
    answer_approvals: bool = False
    #: The yes / no settings beside the folders, by field, each its own `--<field>` / `--no-<field>` flag.
    opt_ins: ClassVar[dict[str, str]] = {
        "continue_conversations": "the web continues your editor (Claude Code) conversations here, as a copy",
        "answer_approvals": "the web answers the approvals a session asks for",
    }
    rules: ClassVar[str] = ("a folder must be strictly below {base}, not hidden (.name), not a symlink, not Galaius's own folder, "
                            "and not inside or around a folder shared with workflows")

    @staticmethod
    def joining() -> MachineConfig:
        """The machine `AccountLogin.save` writes, as far as the folder rules and `of` read it (no server yet)."""
        return MachineConfig.model_construct(working_directory=Path.home(), script_roots=(), run_agents=False)

    @classmethod
    def of(cls, machine: MachineConfig) -> "AgentChoice":
        """The machine's current settings: the defaults every question and flag starts from."""
        return cls(run_agents=machine.run_agents, folders=machine.agent_roots, **{field: getattr(machine, field) for field in cls.opt_ins})

    @classmethod
    def given(cls, agents: bool | None, folders: Iterable[str], opt_ins: Mapping[str, bool | None], machine: MachineConfig) -> "AgentChoice | None":
        """From `--agents/--no-agents`, `--agent-folder` and the `opt_ins` flags over `machine`'s
        current settings, checked before anything is saved; None when none was given.
        Each flag changes only what it names; a folder or a yes alone means agents on."""
        folders, opt_ins = tuple(folders), {field: answer for field, answer in opt_ins.items() if answer is not None}
        if agents is None and not folders and not opt_ins:
            return None
        if agents is False and (folders or any(opt_ins.values())):
            raise LoginError("--no-agents contradicts --agent-folder, --continue-conversations and --answer-approvals: pick one")
        current = cls.of(machine)
        on = False if agents is False else bool(agents or folders or any(opt_ins.values())) or current.run_agents
        choice = current.model_copy(update={"run_agents": on, **opt_ins})
        if folders:
            choice, refused = choice.model_copy(update={"folders": cls.named(folders, machine.working_directory)}).checked(machine)
            if refused:
                raise LoginError(f"cannot let agents start in {', '.join(refused)}: {cls.rules.format(base=machine.working_directory)}")
        return choice

    @staticmethod
    def named(folders: Iterable[str], base: Path) -> tuple[str, ...]:
        """Typed names as the machine stores them: blanks dropped, `~` and absolute paths below
        `base` made relative, each once."""
        names: dict[str, None] = {}
        for folder in (folder.strip() for folder in folders):
            if not folder:
                continue
            path = Path(folder).expanduser()
            if path.is_absolute() and base.resolve() in path.resolve().parents:
                path = path.resolve().relative_to(base.resolve())
            names[path.as_posix()] = None
        return tuple(names)

    def checked(self, machine: MachineConfig) -> tuple["AgentChoice", tuple[str, ...]]:
        """(this choice keeping only the usable folders, by their stored name; the names refused)."""
        usable, refused = machine.model_copy(update={"agent_roots": self.folders}).usable_agent_roots()
        base = machine.working_directory.resolve()
        return self.model_copy(update={"folders": tuple(root.relative_to(base).as_posix() for root in usable)}), refused

    def applied(self, runner: MachineRunner) -> MachineConfig:
        """Saved on this machine, re-checked against its file under the runner's lock."""
        return runner.update(lambda current: current.model_copy(update={**self.model_dump(exclude={"folders"}), "agent_roots": self.checked(current)[0].folders}))

    @staticmethod
    def described(config: MachineConfig) -> str:
        """The result line, and how to change it later."""
        if not config.run_agents:
            return "Agents: off here. Turn them on from its page on the web (or here:  galaius machine agents on)."
        folders = list(config.agent_roots_by_name())
        extras = (f"\nEditor conversations continued from the web: {'on' if config.continue_conversations else 'off'}; approvals answered from the web: "
                  f"{'on' if config.answer_approvals else 'off'}. To change:  galaius machine agents on --continue on|off --approvals on|off")
        if not folders:
            return "Agents: on here, no folder to start them in from the web yet. To add one:  galaius machine agent-roots <folder…>" + extras
        return (f"Agents: on here; the web can start them in {', '.join(folders)} (under {config.working_directory}) or any folder beneath. "
                "To change:  galaius machine agent-roots <folder…>  (\"\" clears) or  galaius machine agents off" + extras)


class UnreadableMachine(LoginError):
    """The machine file is there but is not one this galaius can read (an older install's format, a
    value sealed by another Windows user): `set_aside` keeps it so this computer can sign in again."""

    def __init__(self, path: Path, error: Exception) -> None:
        super().__init__(f"this computer's machine file cannot be read ({type(error).__name__})")
        self.path = path
        self.kind = type(error).__name__
        self.proof = self._proof(path)

    @staticmethod
    def _proof(path: Path) -> tuple[AccountLogin, SecretStr] | None:
        """(its server, its machine token) when the file still holds both readably (read before `set_aside`)."""
        try:
            values = json.loads(PRIVATE_FILES.read_text(path))
            token = PRIVATE_FILES.unseal(values["token"])
            if not re.fullmatch(MACHINE_TOKEN_PATTERN, token):
                return None
            return AccountLogin.parsed(values["server_url"]), SecretStr(token)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, LoginError):
            return None

    def token_for(self, account: AccountLogin) -> SecretStr | None:
        """The proof a fresh sign-in on `account`'s server sends (`DeviceLoginStart.replaces`) so its owner's
        approval replaces the PC this file enrolled; None for another server (never sent there)."""
        return self.proof[1] if self.proof is not None and self.proof[0] == account else None

    def set_aside(self) -> Path:
        aside = self.path.with_name(self.path.name + ".unreadable")
        try:
            PRIVATE_FILES.replace(self.path, aside)
        except OSError:
            raise LoginError(f"{self} nor moved aside: remove {self.path}, then run it again") from None
        return aside


def _existing_machine() -> MachineConfig | None:
    """This computer's machine, None when it has none; `UnreadableMachine` when its file is not one;
    a file not private to this user is refused as it was (its token may have been read)."""
    path = MachineRunner.default_config_path()
    try:
        return MachineRunner().load()
    except FileNotFoundError:
        return None
    except PermissionError:
        raise LoginError(f"this computer's machine file {path} is not private to this user, so its token may have been read: "
                         "remove that file (and the PC on its page on the web), then run the install line again") from None
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise UnreadableMachine(path, error) from None


class SaidHere(BaseModel):
    """What `galaius login` says about this computer's earlier connection before signing it in again, in the
    language of where it shows: an installer's output is French (it ends on `INSTALLED`), a terminal's English."""

    model_config = ConfigDict(frozen=True)
    #: `{machine}`, `{server}`: the server refused this computer's token (`_held`).
    not_held: str
    #: `{kind}`, `{aside}`: the machine file did not read (`UnreadableMachine`), kept aside.
    unreadable: str


#: By `detach` (the installers).
SAID_HERE: dict[bool, SaidHere] = {
    False: SaidHere(not_held="This computer's connection (machine {machine}) is no longer known to {server} (removed on the web, or left by an earlier install): it signs in again.",
                    unreadable="This computer's machine file cannot be read ({kind}); kept as {aside}, this computer signs in again."),
    True: SaidHere(not_held="La connexion de cet ordinateur (machine {machine}) n'est plus connue de {server} (retiré sur le web, ou laissé par une installation précédente) : il se reconnecte.",
                   unreadable="Le fichier de connexion de cet ordinateur est illisible ({kind}) ; gardé sous {aside}, il se reconnecte."),
}


def _held(account: AccountLogin, existing: MachineConfig | None, said: SaidHere) -> MachineConfig | None:
    """`existing`, unless `account` is its server and refuses its token (`AccountLogin.holds`): then
    the computer signs in again as a new one, so the install line never reopens the page of a PC
    that can never connect. Nothing is deleted before that sign-in lands (its approval rewrites the
    files and restarts the service); only the background service, which can no longer connect, stops."""
    if existing is None or AccountLogin.parsed(existing.server_url) != account or account.holds(existing):
        return existing
    print(said.not_held.format(machine=existing.machine_id, server=account.server), flush=True)
    try:
        MACHINE_SERVICE.stop()
    except ServiceUnavailable:
        pass  # nothing running here to stop
    return None


def _shown(value: str) -> str:
    """Server text as one plain terminal line: no escape sequence or line break reaches the screen."""
    return "".join(character if character.isprintable() else "?" for character in value)


def login(server: str | None, *, allow_runs: bool, open_browser: bool, agents: bool | None = None, agent_folders: tuple[str, ...] = (),
          agent_opt_ins: Mapping[str, bool | None] = MappingProxyType({}), detach: bool = False, resume: bool = False) -> None:
    """Asks nothing. `agents` / `agent_folders` / `agent_opt_ins` (by `AgentChoice.opt_ins` field)
    set the agent settings ahead (scripts); unsaid, a joining computer keeps agents off (set later
    on its page on the web) and a connected one keeps its own. Already connected to this server
    (the one remembered when `server` is None): nothing is signed in again, the service restarts on
    this build — unless that server refuses its token (`_held`) or its file is unreadable, then it
    signs in as a new computer; connected to another server: moved there when that server holds this computer's
    enrollment (`_moved`), else refused. `detach` (the installers): the sign-in is handed to a
    background process (`_handed_off`) and this returns at once; `resume` is that process."""
    said, unreadable = SAID_HERE[detach], None
    try:
        existing = _existing_machine()
    except UnreadableMachine as error:
        print(said.unreadable.format(kind=error.kind, aside=error.set_aside()), file=sys.stderr)
        existing, unreadable = None, error
    try:
        account = AccountLogin.parsed(existing.server_url) if existing is not None and server is None else AccountLogin.at(server)
        replaces = None if unreadable is None else unreadable.token_for(account)
        if not resume:
            existing = _held(account, existing, said)
        if detach:
            _handed_off(account, existing, allow_runs=allow_runs, open_browser=open_browser, replaces=replaces,
                        agents=AgentChoice.given(agents, agent_folders, agent_opt_ins, existing or AgentChoice.joining()))
        elif resume:
            _resumed(account)
        elif existing is None:
            with account.client() as http:
                started, _ = account.begun(http, allow_runs, open_browser, replaces)
                print(f"To connect this computer, open this page and allow it:\n\n    {started.verification_uri_complete}\n")
                print(f"Check the page shows the code  {started.user_code}  (expires in {started.expires_in // 60} min); if it asks for a number, pick  {started.match}.")
                _joined(account, http, started, agents=AgentChoice.given(agents, agent_folders, agent_opt_ins, AgentChoice.joining()))
        elif AccountLogin.parsed(existing.server_url) != account:
            _moved(account, existing, agents=AgentChoice.given(agents, agent_folders, agent_opt_ins, existing))
        else:
            _reconfigured(account, existing, agents=AgentChoice.given(agents, agent_folders, agent_opt_ins, existing))
    except httpx.HTTPError as error:
        raise LoginError(f"cannot reach the server ({type(error).__name__}); check the address and your network, then run it again") from None


#: The one line an installer ends with: everything after it happens in the browser.
INSTALLED = "Installé : continuez dans votre navigateur"


class HandedOff(BaseModel):
    """What `galaius login --detach` hands its detached waiter on stdin (never on its command line):
    the sign-in it started (None: a connected computer, only restarted) and the settings given
    ahead, with the mark naming it the computer's one pending sign-in (`Pending`)."""

    model_config = ConfigDict(frozen=True)
    started: DeviceLoginStarted | None = None
    agents: AgentChoice | None = None
    mark: str = ""

    def payload(self) -> bytes:
        return json.dumps({**self.model_dump(mode="json"), "started": self.started.revealed() if self.started is not None else None}).encode()


class Pending(BaseModel):
    """This computer's one sign-in waiting for its approval (`login.wait`, private): a second
    install line run meanwhile reopens the same page instead of starting another code, so whichever
    tab is approved, its waiter collects it. The waiter refreshes the file at each poll; one that
    stopped (the computer restarted, the code expired) leaves it stale and a new sign-in starts."""

    model_config = ConfigDict(frozen=True)
    mark: str
    link: str
    #: The number the approval page asks for when the browser is on another network (`DeviceLoginStarted.match`).
    match: str
    expires_at: datetime
    #: The waiter's process (`process_started` names it for good), written by the waiter itself
    #: (`claim`): a page whose waiter is gone (stopped by an installer replacing Galaius, the
    #: computer restarted) is never reopened, whatever the file's age.
    pid: int | None = None
    started: int | None = None
    #: A waiter polls every few seconds: a file older than this has none behind it any more.
    alive_within: ClassVar[timedelta] = timedelta(seconds=30)

    @staticmethod
    def path() -> Path:
        return UserPaths.config() / "login.wait"

    @classmethod
    def read(cls) -> "Pending | None":
        """The pending sign-in a waiter still serves, else None."""
        try:
            pending = cls.model_validate_json(PRIVATE_FILES.read_text(cls.path()))
            touched = datetime.fromtimestamp(cls.path().stat().st_mtime, UTC)
        except (OSError, ValueError):
            return None
        now = datetime.now(UTC)
        alive = pending.pid is not None and pending.started is not None and process_started(pending.pid) == pending.started
        return pending if alive and pending.expires_at > now + timedelta(minutes=1) and now - touched < cls.alive_within else None

    def write(self) -> None:
        """Written stale: only its waiter's own refresh (`current`) makes it live, so a waiter that
        never started (or died starting, e.g. a second install replacing its files) is never reused."""
        PRIVATE_FILES.write_text(self.path(), self.model_dump_json())
        os.utime(self.path(), (0, 0))

    def claim(self) -> None:
        """The waiter (this process) names itself in its sign-in's file, still marked as its own. Not
        locked: a second install writing its own sign-in in the same milliseconds may be overwritten,
        and its waiter then stops (its page asks for a new sign-in at the next install line)."""
        try:
            pending = Pending.model_validate_json(PRIVATE_FILES.read_text(self.path()))
        except (OSError, ValueError):
            return
        if pending.mark == self.mark:
            PRIVATE_FILES.write_text(self.path(), pending.model_copy(update={"pid": os.getpid(), "started": process_started(os.getpid())}).model_dump_json())

    def current(self) -> bool:
        """Still this computer's pending sign-in (its file refreshed: the waiter is alive)."""
        try:
            if Pending.model_validate_json(PRIVATE_FILES.read_text(self.path())).mark != self.mark:
                return False
            os.utime(self.path())
            return True
        except (OSError, ValueError):
            return False


def _handed_off(account: AccountLogin, existing: MachineConfig | None, *, allow_runs: bool, open_browser: bool, agents: AgentChoice | None,
                replaces: SecretStr | None = None) -> None:
    """The installers' sign-in, nothing asked and nothing waited for here. A new computer: its
    pending sign-in's page reopens if a waiter still serves one; else a code is started, its page
    opened in the browser, and a detached `galaius login --resume` (`HandedOff` on its stdin, its
    output in `login.log`) waits for the approval, saves the credentials and starts the background
    service. A connected one: a server it cannot move to is refused here; its page opens in the
    browser and its restart is detached the same way."""
    if existing is not None:
        if AccountLogin.parsed(existing.server_url) != account and _channel_accepts(account.server, existing.token.get_secret_value()) != "accepted":
            raise LoginError(f"this computer is connected to {existing.server_url} and {account.server} does not know it; run `galaius logout` first")
        if agents is not None:
            agents.applied(MachineRunner())
        _detached(account, HandedOff())
        with account.client() as http:
            link, match = account.page(http, existing), ""
    elif (pending := Pending.read()) is not None:
        link, match = pending.link, pending.match
    else:
        with account.client() as http:
            started, _ = account.begun(http, allow_runs, open_browser=False, replaces=replaces)
        pending = Pending(mark=secrets.token_hex(16), link=started.verification_uri_complete, match=started.match,
                          expires_at=datetime.now(UTC) + timedelta(seconds=started.expires_in))
        pending.write()
        _detached(account, HandedOff(started=started, agents=agents, mark=pending.mark))
        link, match = pending.link, pending.match
    print(INSTALLED)
    if not (open_browser and account.opened(link)):
        print(f"Ouvrez cette page : {link}")
    if match:  # approved from another network (a copied link), the page asks for it
        print(f"Si la page demande un numéro, choisissez : {match}")


def _detached(account: AccountLogin, handed: HandedOff) -> None:
    """`galaius login --resume` in its own session, outliving this process and its terminal, its
    output appended to `login.log` (private to this user), `handed` on its stdin."""
    log = UserPaths.config() / "login.log"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [console_python(), "-m", "galaius", "login", "--resume", "--server", account.server]
    # Windows: no console, its own group, out of the window's job when Windows lets it (else inside it:
    # closing the window may end it, said in the log). Elsewhere: its own session.
    base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    attempts = [base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base] if sys.platform == "win32" else [0]
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as output:
        if sys.platform != "win32":
            os.fchmod(output.fileno(), 0o600)
        for attempt, flags in enumerate(attempts, 1):
            try:
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=output, close_fds=True,
                                         creationflags=flags, start_new_session=sys.platform != "win32")
                break
            except OSError as error:
                if attempt == len(attempts):
                    raise LoginError(f"the background sign-in did not start ({error}); see {log}") from None
                output.write(b"The window's job forbids leaving it: the sign-in waits inside it (closing the window may end it).\n")
                output.flush()
    try:
        child.stdin.write(handed.payload())
        child.stdin.close()
    except OSError:
        raise LoginError(f"the background sign-in did not start; see {log}") from None


def _resumed(account: AccountLogin) -> None:
    """The detached waiter: what `_handed_off` handed over on stdin, finished."""
    try:
        handed = HandedOff.model_validate_json(sys.stdin.buffer.read())
    except ValueError as error:
        raise LoginError(f"--resume takes what `galaius login --detach` hands over ({error})") from None
    existing = _existing_machine()
    if handed.started is None:
        if existing is None:
            raise LoginError("--resume restarts a connected computer, and this one is not connected")
        if AccountLogin.parsed(existing.server_url) != account:
            _moved(account, existing, agents=None)
        else:
            _reconfigured(account, existing, agents=None)
        return
    pending = Pending(mark=handed.mark, link=handed.started.verification_uri_complete, match=handed.started.match, expires_at=datetime.now(UTC))
    pending.claim()
    with account.client() as http:
        _joined(account, http, handed.started, agents=handed.agents, current=pending.current)
    Pending.path().unlink(missing_ok=True)


def _moved(account: AccountLogin, existing: MachineConfig, *, agents: AgentChoice | None) -> None:
    """The server moved (its data with it): this computer stays the SAME machine and only follows the
    address. Proof the new address holds this enrollment: its machine channel opens with this
    computer's own token, which every other server refuses before accepting. Then the address is
    rewritten (machine file, the CLI's connection, the remembered server) and the service restarted
    on it. A server that does not know this computer changes nothing here."""
    match _channel_accepts(account.server, existing.token.get_secret_value()):
        case "refused":
            raise LoginError(f"this computer is already connected (machine {existing.machine_id} on {existing.server_url}) and "
                             f"{account.server} does not know it; run `galaius logout` first to sign in there as a new computer")
        case "unreachable":
            raise LoginError(f"{account.server} does not answer as a Galaius server yet; nothing changed here, run it again once it does")
    machine = MachineRunner().update(lambda current: current.model_copy(update={"server_url": account.server}))
    connection = CatalogConnection.load()
    if connection is not None and connection.endpoint.rstrip("/") == existing.server_url.rstrip("/"):
        connection.model_copy(update={"endpoint": account.server}).save()
    account.remember()
    print(f"Moved: this computer now connects to {account.server} (was {existing.server_url}).")
    _reconfigured(account, machine, agents=agents)


def _channel_accepts(server: str, token: str, timeout: float = 15) -> Literal["accepted", "refused", "unreachable"]:
    """What `server`'s machine channel says to `token`: a Galaius server refuses an unknown token
    (403) before accepting the WebSocket; anything else (no route, no answer) is not a verdict. The
    probe closes at once and sends nothing."""
    async def probe() -> Literal["accepted", "refused", "unreachable"]:
        try:
            async with websockets.connect(MachineRunner._channel_url(server), additional_headers={"Authorization": f"Bearer {token}"},
                                          user_agent_header=USER_AGENT, open_timeout=timeout, close_timeout=3):
                return "accepted"
        except websockets.InvalidStatus as error:
            return "refused" if error.response.status_code == 403 else "unreachable"
        except (OSError, TimeoutError, websockets.InvalidHandshake, websockets.InvalidURI):
            return "unreachable"
    return asyncio.run(probe())


def _reconfigured(account: AccountLogin, existing: MachineConfig, *, agents: AgentChoice | None) -> None:
    """This computer stays connected as it is: agent settings the flags name change (the rest live
    on its page on the web), and its background service restarts on THIS build, so re-running the
    installer is all an older computer ever needs to take every later update by itself."""
    print(f"This computer is already connected to {account.server} as {_shown(account.connected_name(existing))}.")
    machine = existing if agents is None else agents.applied(MachineRunner())
    print(AgentChoice.described(machine))
    if machine.remote_settings:
        with account.client() as http:
            print(f"Its agent settings can also be changed on its page: {account.page(http, machine)}")
    try:
        MACHINE_SERVICE.stop()
    except ServiceUnavailable:
        pass  # not set up here yet: `brought_online` sets it up
    with account.client() as http:
        if account.brought_online(http, machine, account.key()):
            print("Online: its background service restarted on this galaius build.")


def _joined(account: AccountLogin, http: httpx.Client, started: DeviceLoginStarted, *, agents: AgentChoice | None, current: Callable[[], bool] = lambda: True) -> None:
    """The approval awaited, then this computer saved, online (or told why) and synced. The
    approval on the web is the consent, and every choice after it is made on the PC's page
    (`remote_settings` is on for a new PC); flags given ahead still apply, else agents stay off."""
    print(f"Waiting for approval at {started.verification_uri_complete} …", flush=True)
    issued = account.wait(http, started, current)
    workspace, approver = _shown(issued.workspace.name), _shown(issued.approved_by)
    print(f"\nApproved by {approver} for the workspace “{workspace}”.")
    account.save(issued)
    account.remember()
    machine = (agents or AgentChoice()).applied(MachineRunner())
    if account.brought_online(http, machine, issued.api_key.secret.get_secret_value()):
        print(f"Connected: {issued.machine.name} is now a machine in {workspace}")
        if not MACHINE_SERVICE.after_logout():
            print("It runs while you are signed in to this computer; it starts again at your next sign-in.")
        print(f"Its settings (agents, folders) are on its page: {account.page(http, machine)}")
    print(account.synced(CatalogConnection.load()))
    print(AgentChoice.described(machine))


def logout() -> None:
    unreadable = None
    try:
        machine = _existing_machine()
    except UnreadableMachine as error:
        machine, unreadable = None, error
    if machine is None and unreadable is None:
        raise LoginError("this computer is not signed in")
    # An unreadable file: the server it signed in at, where its CLI key still signs it out.
    account = AccountLogin.at(None if machine is None else machine.server_url)
    key = account.key()  # None: already gone, nothing to revoke from here
    if key is None and account.key_path.exists():
        print(f"This computer's sign-in key could not be read ({account.key_path}): it is removed here but stays valid on the server until removed there.", file=sys.stderr)
    try:
        with account.client() as http:
            revoked = account.revoke(http, key) if key is not None else {}
    except httpx.HTTPError as error:
        raise LoginError(f"cannot reach {account.server} ({type(error).__name__}); nothing was removed here, run it again") from None
    if unreadable is not None:
        print(f"{unreadable}; kept as {unreadable.set_aside()}.", file=sys.stderr)
    try:
        MACHINE_SERVICE.remove()
    except ServiceUnavailable as refused:
        # Already signed out on the server: the rest is removed here all the same.
        print(f"The background service could not be removed ({refused}); it can no longer connect.", file=sys.stderr)
    MachineRunner.default_config_path().unlink(missing_ok=True)
    account.key_path.unlink(missing_ok=True)
    CatalogConnection.path().unlink(missing_ok=True)
    print(f"Signed out: this computer left {account.server}" + (" and was removed from its machines." if revoked.get("machine") else "."))
