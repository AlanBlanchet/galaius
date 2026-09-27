"""`interact login` / `interact logout`: this computer joins (or leaves) an Interact account.

The CLI asks the server for a short code, the signed-in owner allows this computer on the server's
/link page, and the CLI collects what the approval issued (`interact_core.DeviceLoginIssued`): the
machine token — saved where `interact machine connect` reads it, then kept connected by a systemd
user service — and a read-only workspace key the CLI's `agents` / `workflows` commands use. Nothing
on this computer is reachable by a workflow until its owner names a folder (`interact machine
file-roots`). Whether agents may run here, and the folders the web may start them in, is asked
once right after the approval (`AgentChoice`; off and none unless said)."""

import getpass
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from importlib.metadata import version
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from interact_core import DeviceLoginIssued, DeviceLoginStart, DeviceLoginStarted, DeviceTokenRefusal, MachineSummary, ReleaseInfo
from pydantic import BaseModel, ConfigDict, TypeAdapter

from interact import USER_AGENT, __version__
from interact.agents.catalog import AgentCatalog
from interact.agents.catalog_connection import CatalogConnection
from interact.cli.prompts import PromptMode
from interact.server_prompts import ServerPrompts
from interact.machines import MachineConfig, MachineRunner
from interact.prompt_secret import read_prompt_token


class LoginError(Exception):
    """Something the person can act on, said in their words."""


class UserService(BaseModel):
    """The systemd user unit that keeps `interact machine connect` running (Linux only today)."""

    model_config = ConfigDict(frozen=True)
    name: str = "interact-machine.service"

    @property
    def path(self) -> Path:
        return MachineRunner.default_config_path().parent.parent / "systemd" / "user" / self.name

    @staticmethod
    def executable() -> Path:
        """This install's own `interact` (the service must run the same build as this CLI)."""
        beside = Path(sys.executable).with_name("interact")
        found = beside if beside.is_file() else Path(shutil.which("interact") or "")
        if not found.is_file():
            raise LoginError("cannot find the interact program to start at boot; install it with the line from your Interact page")
        return found.absolute()

    def unit(self) -> str:
        """Nothing the server sent goes in here: a unit line is a command line."""
        program = str(self.executable()).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        return (
            "[Unit]\n"
            "Description=Interact: this computer as a machine of your account\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\n"
            f'ExecStart="{program}" machine connect\n'
            # A revoked computer exits 0 and stays stopped; a crash or lost network restarts it.
            "Restart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    @staticmethod
    def _systemctl(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=30)

    def install(self) -> str | None:
        """Write, enable and start the unit; the reason it could not, else None."""
        if shutil.which("systemctl") is None:
            return "this computer has no systemd"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.unit(), encoding="utf-8")
        for arguments in (("daemon-reload",), ("enable", "--now", self.name)):
            done = self._systemctl(*arguments)
            if done.returncode != 0:
                return (done.stderr or done.stdout).strip() or f"systemctl --user {' '.join(arguments)} failed"
        return None

    @staticmethod
    def linger() -> bool:
        """Keep it running after this person signs out of the desktop; some systems refuse."""
        if shutil.which("loginctl") is None:
            return False
        return subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True, text=True, timeout=30).returncode == 0

    def remove(self) -> None:
        if shutil.which("systemctl") is not None and self.path.exists():
            self._systemctl("disable", "--now", self.name)
        self.path.unlink(missing_ok=True)
        if shutil.which("systemctl") is not None:
            self._systemctl("daemon-reload")


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
        """`server`, else the one remembered (by the server's installer or an earlier login), else
        asked once in the terminal; a bare host means https."""
        if server is None:
            try:
                remembered = cls.parsed(cls.remembered_path().read_text(encoding="utf-8"))
            except (FileNotFoundError, LoginError):
                remembered = None
            account = None if remembered is None else remembered.public()
            return account if account is not None else cls.parsed(cls.asked())
        return cls.parsed(server)

    @staticmethod
    def asked() -> str:
        if not sys.stdin.isatty():
            raise LoginError("which Interact server? Pass --server https://…")
        try:
            return input("Interact server address (e.g. https://interact.example.com): ")
        except (EOFError, KeyboardInterrupt):
            raise LoginError("no server given") from None

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
            raise LoginError(f"{server} is not an Interact server address (https://…)")
        return cls(server=f"{parts.scheme}://{parts.netloc}")

    @property
    def key_path(self) -> Path:
        return CatalogConnection.path().parent / "credentials" / f"{urlsplit(self.server).netloc.replace(':', '_')}.key"

    def remember(self) -> None:
        """The next `interact login` on this computer needs no address."""
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
            done.append(f"agents not synced ({error or type(error).__name__}; then run: interact agents sync)")
        try:
            PromptMode.installed(ServerPrompts(connection=connection))
            done.append("prompts installed")
        except (OSError, ValueError, httpx.HTTPError) as error:
            done.append(f"prompts not synced ({error or type(error).__name__}; then run: interact prompts sync)")
        return "Synced: " + ", ".join(done)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.server, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(15, connect=10), trust_env=False, follow_redirects=False)

    # ---- sign in ---------------------------------------------------------------------------

    def skew(self, http: httpx.Client) -> str | None:
        """What differs between this computer's Interact and the server's, said once."""
        try:
            server = ReleaseInfo.model_validate_json(http.get("/v1/version").content)
        except (httpx.HTTPError, ValueError):
            return None
        mine = version("interact-core")
        return None if server.core_version == mine else f"this computer runs interact-core {mine}, the server {server.core_version}: run the install line from your Interact page again"

    def start(self, http: httpx.Client, runs: bool) -> DeviceLoginStarted:
        name = socket.gethostname().split(".")[0] or "computer"
        system = self.platforms.get(platform.system())
        if system is None:
            raise LoginError(f"{platform.system()} is not supported yet")
        request = DeviceLoginStart(client_name=name, platform=system, client_version=__version__, runs=runs)
        answer = http.post("/v1/device/authorizations", json=request.model_dump(mode="json"))
        if answer.status_code == 429:
            raise LoginError("too many sign-in attempts from this network; wait a minute and try again")
        if answer.status_code != 201:
            raise LoginError(f"the server refused to start a sign-in (HTTP {answer.status_code})")
        return DeviceLoginStarted.model_validate_json(answer.content)

    def wait(self, http: httpx.Client, started: DeviceLoginStarted) -> DeviceLoginIssued:
        interval, deadline = started.interval, time.monotonic() + min(self.patience, started.expires_in + 30)
        while time.monotonic() < deadline:
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
                raise LoginError("the code expired before it was approved; run `interact login` again")
            elif error != "authorization_pending":
                raise LoginError(f"the server answered HTTP {answer.status_code} while waiting")
        raise LoginError("nobody approved this computer in time; run `interact login` again")

    def save(self, issued: DeviceLoginIssued) -> None:
        """The key for the CLI (0600), the CLI's connection, and the machine credential."""
        self.key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(issued.api_key.secret.get_secret_value() + "\n")
        os.chmod(self.key_path, 0o600)
        CatalogConnection(endpoint=self.server, workspace_id=issued.workspace.id, auth_mode="token", token_file=self.key_path.absolute()).save()
        MachineRunner().save(MachineConfig(
            server_url=self.server, workspace_id=issued.workspace.id, machine_id=issued.machine.id, token=issued.machine_token,
            permission_ceiling="read_only", working_directory=Path.home(), file_roots=(), script_roots=(), run_agents=False,
        ))

    def online(self, http: httpx.Client, issued: DeviceLoginIssued, within: float = 30) -> bool:
        headers = {"Authorization": f"Bearer {issued.api_key.secret.get_secret_value()}"}
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                machines = TypeAdapter(tuple[MachineSummary, ...]).validate_json(http.get(f"/v1/workspaces/{issued.workspace.id}/machines", headers=headers).content)
                if any(item.id == issued.machine.id and item.state == "online" for item in machines):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(2)
        return False

    def revoke(self, http: httpx.Client, secret: str) -> dict[str, object]:
        answer = http.post("/v1/device/logout", headers={"Authorization": f"Bearer {secret}"})
        if answer.status_code == 401:
            return {}  # already revoked on the web: nothing left to revoke
        if answer.status_code != 200:
            raise LoginError(f"the server did not sign this computer out (HTTP {answer.status_code}); nothing was removed here")
        return answer.json()


class AgentChoice(BaseModel):
    """Whether agents may run on this computer, and the folders the web may start them in (names
    under the machine's working directory, checked by `MachineConfig.usable_agent_roots`): asked
    once by `interact login`, or given as flags; off and none unless said."""

    model_config = ConfigDict(frozen=True)
    run_agents: bool = False
    folders: tuple[str, ...] = ()
    rules: ClassVar[str] = ("a folder must be strictly below {base}, not hidden (.name), not a symlink, not Interact's own folder, "
                            "and not inside or around a folder shared with workflows")

    @staticmethod
    def joining() -> MachineConfig:
        """The machine `AccountLogin.save` writes, as far as the folder rules read it (no server yet)."""
        return MachineConfig.model_construct(working_directory=Path.home(), file_roots=(), script_roots=())

    @classmethod
    def given(cls, agents: bool | None, folders: Iterable[str]) -> "AgentChoice | None":
        """From `--agents/--no-agents` and `--agent-folder`, checked before any sign-in starts; None
        when neither was given. A folder alone means agents on."""
        if agents is None and not folders:
            return None
        if agents is False and folders:
            raise LoginError("--no-agents and --agent-folder contradict each other: pick one")
        machine = cls.joining()
        choice, refused = cls(run_agents=agents is not False, folders=cls.named(folders, machine.working_directory)).checked(machine)
        if refused:
            raise LoginError(f"cannot let agents start in {', '.join(refused)}: {cls.rules.format(base=machine.working_directory)}")
        return choice

    @classmethod
    def asked(cls, machine: MachineConfig) -> "AgentChoice":
        """Two questions in the terminal; refused folders are said with the rules and asked once more,
        then left out."""
        if not _confirmed("Let agents run on this computer from the web? [y/N] "):
            return cls()
        base = machine.working_directory
        print(f"Working directory: {base}")
        for attempt in range(2):
            answer = _answered(f"Which folders may they start in? (names under {base}, comma-separated; Enter = none) ")
            choice, refused = cls(run_agents=True, folders=cls.named(answer.split(","), base)).checked(machine)
            if not refused:
                return choice
            print(f"Refused: {', '.join(refused)} ({cls.rules.format(base=base)}).")
        print(f"Left out: {', '.join(refused)}.")
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
        return runner.update(lambda current: current.model_copy(update={"run_agents": self.run_agents, "agent_roots": self.checked(current)[0].folders}))

    @staticmethod
    def described(config: MachineConfig) -> str:
        """The result line, and how to change it later."""
        if not config.run_agents:
            return "Agents: off here. To allow them:  interact machine agents on  then  interact machine agent-roots <folder…>"
        folders = list(config.agent_roots_by_name())
        if not folders:
            return "Agents: on here, no folder to start them in from the web yet. To add one:  interact machine agent-roots <folder…>"
        return (f"Agents: on here; the web can start them in {', '.join(folders)} (under {config.working_directory}) or any folder beneath. "
                "To change:  interact machine agent-roots <folder…>  (\"\" clears) or  interact machine agents off")


def _existing_machine() -> MachineConfig | None:
    try:
        return MachineRunner().load()
    except FileNotFoundError:
        return None


def _shown(value: str) -> str:
    """Server text as one plain terminal line: no escape sequence or line break reaches the screen."""
    return "".join(character if character.isprintable() else "?" for character in value)


def _confirmed(question: str) -> bool:
    """Yes only for an explicit y; no terminal, Ctrl-D or Ctrl-C answer no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _answered(question: str) -> str:
    """The typed line; Ctrl-D or Ctrl-C answer nothing."""
    try:
        return input(question)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def login(server: str | None, *, allow_runs: bool, yes: bool, open_browser: bool, agents: bool | None = None, agent_folders: tuple[str, ...] = ()) -> None:
    """`agents` / `agent_folders` answer the agents question ahead (scripts, the install line); unsaid
    and in a terminal without `yes`, it is asked; else agents stay off."""
    try:
        _login(server, allow_runs=allow_runs, yes=yes, open_browser=open_browser, agents=AgentChoice.given(agents, agent_folders))
    except httpx.HTTPError as error:
        raise LoginError(f"cannot reach the server ({type(error).__name__}); check the address and your network, then run it again") from None


def _login(server: str | None, *, allow_runs: bool, yes: bool, open_browser: bool, agents: AgentChoice | None) -> None:
    account = AccountLogin.at(server)
    existing = _existing_machine()
    if existing is not None:
        raise LoginError(f"this computer is already connected (machine {existing.machine_id} on {existing.server_url}); run `interact logout` first")
    with account.client() as http:
        if (skew := account.skew(http)) is not None:
            print(f"Note: {skew}.", file=sys.stderr)
        started = account.start(http, allow_runs)
        print(f"To connect this computer, open this page and allow it:\n\n    {started.verification_uri_complete}\n")
        print(f"Check the page shows the code  {started.user_code}  (expires in {started.expires_in // 60} min).")
        if open_browser and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform == "darwin"):
            webbrowser.open(started.verification_uri_complete)
        print("Waiting for approval…", flush=True)
        issued = account.wait(http, started)
        workspace, approver = _shown(issued.workspace.name), _shown(issued.approved_by)
        print(f"\nApproved by {approver} for the workspace “{workspace}”.")
        if not yes and not _confirmed(f"Connect this computer to “{workspace}”? [y/N] "):
            account.revoke(http, issued.api_key.secret.get_secret_value())
            raise LoginError("not connected; the approval was withdrawn" if sys.stdin.isatty() else "confirm in a terminal, or pass --yes (the approval was withdrawn)")
        account.save(issued)
        account.remember()
        runner = MachineRunner()
        if agents is None:
            agents = AgentChoice.asked(runner.load()) if sys.stdin.isatty() and not yes else AgentChoice()
        machine = agents.applied(runner)
        synced = account.synced(CatalogConnection.load())
        service = UserService()
        refused = service.install()
        if refused is not None:
            print(f"Could not start it in the background ({refused}). Keep it connected with:  interact machine connect", file=sys.stderr)
        elif not account.online(http, issued):
            print("Started, but the server does not see it online yet. Check:  systemctl --user status interact-machine", file=sys.stderr)
        else:
            print(f"Connected: {issued.machine.name} is now a machine in {workspace}")
            print(synced)
            if not service.linger():
                print("It runs while you are signed in to this computer.")
        print("Workflows can reach no folder here yet. To share one:  interact machine file-roots <folder under your home>")
        print(AgentChoice.described(machine))


def logout() -> None:
    connection = CatalogConnection.load()
    machine = _existing_machine()
    if connection is None or connection.token_file is None:
        raise LoginError("this computer is not signed in")
    account = AccountLogin.at(connection.endpoint)
    try:
        key = read_prompt_token(connection.token_file)
    except ValueError:
        key = None  # already gone: nothing to revoke from here
    try:
        with account.client() as http:
            revoked = account.revoke(http, key) if key is not None else {}
    except httpx.HTTPError as error:
        raise LoginError(f"cannot reach {account.server} ({type(error).__name__}); nothing was removed here, run it again") from None
    UserService().remove()
    if machine is not None and machine.server_url == account.server:
        MachineRunner.default_config_path().unlink(missing_ok=True)
    connection.token_file.unlink(missing_ok=True)
    CatalogConnection.path().unlink(missing_ok=True)
    print(f"Signed out: this computer left {account.server}" + (" and was removed from its machines." if revoked.get("machine") else "."))
