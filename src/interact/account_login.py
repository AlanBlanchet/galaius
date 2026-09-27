"""`interact login` / `interact logout`: this computer joins (or leaves) an Interact account.

The CLI asks the server for a short code, the signed-in owner allows this computer on the server's
/link page, and the CLI collects what the approval issued (`interact_core.DeviceLoginIssued`): the
machine token — saved where `interact machine connect` reads it, then kept connected by a systemd
user service — and a read-only workspace key the CLI's `agents` / `workflows` commands use. Nothing
on this computer is reachable by a workflow until its owner names a folder (`interact machine
file-roots`)."""

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
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from interact_core import DeviceLoginIssued, DeviceLoginStart, DeviceLoginStarted, ReleaseInfo
from pydantic import BaseModel, ConfigDict

from interact import USER_AGENT, __version__
from interact.agents.catalog_connection import CatalogConnection
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
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "systemd" / "user" / self.name

    @staticmethod
    def executable() -> Path:
        """This install's own `interact` (the service must run the same build as this CLI)."""
        beside = Path(sys.executable).with_name("interact")
        found = beside if beside.is_file() else Path(shutil.which("interact") or "")
        if not found.is_file():
            raise LoginError("cannot find the interact program to start at boot; install it with the line from your Interact page")
        return found.absolute()

    def unit(self, workspace: str) -> str:
        return (
            "[Unit]\n"
            f"Description=Interact: this computer as a machine of {workspace}\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={self.executable()} machine connect\n"
            # A revoked computer exits 0 and stays stopped; a crash or lost network restarts it.
            "Restart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    @staticmethod
    def _systemctl(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=30)

    def install(self, workspace: str) -> str | None:
        """Write, enable and start the unit; the reason it could not, else None."""
        if shutil.which("systemctl") is None:
            return "this computer has no systemd"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.unit(workspace), encoding="utf-8")
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
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "interact" / "login-server"

    @classmethod
    def at(cls, server: str | None) -> "AccountLogin":
        """`server`, else the one the installer remembered."""
        if server is None:
            try:
                server = cls.remembered_path().read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                raise LoginError("which Interact server? Run the install line from your Interact page, or pass --server https://…") from None
        parts = urlsplit(server.strip())
        loopback = parts.hostname in {"127.0.0.1", "localhost", "::1"}
        if parts.scheme not in ({"https", "http"} if loopback else {"https"}) or not parts.hostname or parts.username or parts.path not in {"", "/"}:
            raise LoginError(f"{server} is not an Interact server address (https://…)")
        return cls(server=f"{parts.scheme}://{parts.netloc}")

    @property
    def key_path(self) -> Path:
        return CatalogConnection.path().parent / "credentials" / f"{urlsplit(self.server).netloc.replace(':', '_')}.key"

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
        request = DeviceLoginStart(client_name=name, platform=system, client_version=__version__)
        answer = http.post("/v1/device/authorizations" + ("?runs=1" if runs else ""), json=request.model_dump(mode="json"))
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
            error = answer.json().get("error") if answer.headers.get("content-type", "").startswith("application/json") else None
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
                machines = http.get(f"/v1/workspaces/{issued.workspace.id}/machines", headers=headers).json()
                if any(item.get("id") == str(issued.machine.id) and item.get("state") == "online" for item in machines):
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


def _existing_machine() -> MachineConfig | None:
    try:
        return MachineRunner().load()
    except FileNotFoundError:
        return None


def login(server: str | None, *, allow_runs: bool, yes: bool, open_browser: bool) -> None:
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
        print(f"\nApproved by {issued.approved_by} for the company “{issued.workspace.name}”.")
        if not yes:
            if not sys.stdin.isatty():
                account.revoke(http, issued.api_key.secret.get_secret_value())
                raise LoginError("confirm in a terminal, or pass --yes")
            if input("Connect this computer to it? [y/N] ").strip().lower() not in {"y", "yes"}:
                account.revoke(http, issued.api_key.secret.get_secret_value())
                raise LoginError("not connected; the approval was withdrawn")
        account.save(issued)
        service = UserService()
        refused = service.install(issued.workspace.name)
        if refused is not None:
            print(f"Could not start it in the background ({refused}). Keep it connected with:  interact machine connect", file=sys.stderr)
        elif not account.online(http, issued):
            print("Started, but the server does not see it online yet. Check:  systemctl --user status interact-machine", file=sys.stderr)
        else:
            print(f"Connected: {issued.machine.name} is now a machine in {issued.workspace.name}")
            if not service.linger():
                print("It runs while you are signed in to this computer.")
        print("Workflows can reach no folder here yet. To share one:  interact machine file-roots <folder under your home>")
        print("Agent steps are off here. To allow them:  interact machine agents on")


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
    with account.client() as http:
        revoked = account.revoke(http, key) if key is not None else {}
    UserService().remove()
    if machine is not None and machine.server_url == account.server:
        MachineRunner.default_config_path().unlink(missing_ok=True)
    connection.token_file.unlink(missing_ok=True)
    CatalogConnection.path().unlink(missing_ok=True)
    print(f"Signed out: this computer left {account.server}" + (" and was removed from its machines." if revoked.get("machine") else "."))
