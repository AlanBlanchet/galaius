"""What keeps `galaius machine connect` running on this computer after `galaius login`, one per
operating system (`MACHINE_SERVICE`), started, stopped and read by `galaius machine service`:

- Linux: a systemd user unit (`galaius-machine.service`), restarted on failure by systemd.
- Windows: a Scheduled Task of this user, started at their logon with their own rights (no
  administrator, no stored password: an interactive-token task) running `pythonw -m galaius
  machine service run` (no console window). Task Scheduler restarts a task only when it fails to
  START, so `run` itself restarts the connection after a crash, and holds every program it starts
  in a job that closes with it: stopping the task stops the scripts and agents it started.
- macOS: a launchd agent of this user (`~/Library/LaunchAgents/ai.galaius.machine.plist`), loaded
  at their login, restarted by launchd after a crash.
- Anywhere else: no background service, said as such; `galaius machine connect` in a terminal
  keeps it connected.

Each says its own last words (`last_words`): what `galaius login` shows, and sends to the
computer's page, when the service does not bring the computer online."""

import asyncio
import getpass
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import ClassVar
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict

from galaius.error_reports import MachineErrorReports
from galaius.machines import JsonLines, MachineRunner
from galaius.private_files import WindowsPrivateFiles
from galaius.upgrade.quiet import UpgradeReady
from galaius.windowless import windowless_process

if sys.platform == "win32":
    import pythoncom
    import pywintypes
    import win32api
    import win32con
    import win32job
    import win32security
    import win32com.client


class ServiceUnavailable(Exception):
    """This computer cannot keep the connection running in the background; the words say why."""


class MachineService(BaseModel):
    """The background service keeping the machine connection running, as this system has one: set
    up by `galaius login`, removed by `galaius logout`, read and controlled by `galaius machine
    service`; `run` is the restarting connection itself."""

    model_config = ConfigDict(frozen=True)
    #: A crash restarts the connection after this long.
    restart_seconds: ClassVar[float] = 10

    @property
    def logs(self) -> str:
        """Where to read what it did, in this operating system's words."""
        return str(self.log_path())

    @staticmethod
    def executable() -> Path:
        """This install's own `galaius` (the service must run the same build as this CLI)."""
        beside = Path(sys.executable).with_name("galaius")
        found = beside if beside.is_file() else Path(shutil.which("galaius") or "")
        if not found.is_file():
            raise ServiceUnavailable("cannot find the galaius program to start at logon; install it with the line from your Galaius page")
        return found.absolute()

    def install(self) -> None:
        """Set up and started; ServiceUnavailable says why it could not."""
        raise NotImplementedError

    def remove(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def running(self) -> bool:
        raise NotImplementedError

    def installed(self) -> bool:
        raise NotImplementedError

    def after_logout(self) -> bool:
        """Whether it keeps running once this person signs out of the desktop."""
        return False

    def last_words(self, lines: int = 3) -> str:
        """The end of what it last wrote (its log), to say why it is not running or not connected
        (`lines` of it: 3 said in a terminal, more for an error report)."""
        return self._tail(self.log_path(), lines)

    def own_last_words(self) -> tuple[str, ...]:
        """What galaius itself last wrote in its log (`MachineErrorReports.own_lines`): an error report's lines."""
        return MachineErrorReports.own_lines(self.last_words(MachineErrorReports.LOG_LINES))

    @staticmethod
    def log_path() -> Path:
        """Where the service writes what it does when no service manager keeps its output."""
        return MachineRunner.default_config_path().parent / "machine-service.log"

    @staticmethod
    def _tail(path: Path, lines: int = 3) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join([line for line in text.splitlines() if line.strip()][-lines:])

    def run(self) -> None:
        """The connection in this process, restarted after a crash, until the server revokes this
        computer (then it returns and stays stopped) or the settings forbid it (PermissionError)."""
        printer = logging.StreamHandler(sys.stderr)
        printer.setFormatter(JsonLines())
        package = logging.getLogger("galaius")
        package.addHandler(printer)
        package.setLevel(logging.INFO)
        runner = MachineRunner()
        try:
            config = runner.load()
        except (OSError, ValueError) as error:
            raise ServiceUnavailable(f"cannot read this computer's machine settings ({error}); run  galaius login") from None
        while True:
            try:
                asyncio.run(runner.connect(config))
                return
            except PermissionError as error:
                raise ServiceUnavailable(f"connection stopped: {error}") from None
            except UpgradeReady:
                raise
            except Exception as error:  # noqa: BLE001 - a crash restarts the connection, never the service
                package.exception("machine connection crashed: %s; restarting in %s s", error, self.restart_seconds)
                time.sleep(self.restart_seconds)


class SystemdMachineService(MachineService):
    """Linux: the systemd user unit, restarted by systemd itself (`Restart=on-failure`)."""

    name: ClassVar[str] = "galaius-machine.service"

    @property
    def path(self) -> Path:
        return MachineRunner.default_config_path().parent.parent / "systemd" / "user" / self.name

    @property
    def logs(self) -> str:
        return "journalctl --user -u galaius-machine"

    def unit(self) -> str:
        """Nothing the server sent goes in here: a unit line is a command line."""
        program = str(self.executable()).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        return (
            "[Unit]\n"
            "Description=Galaius: this computer as a machine of your account\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\n"
            f'ExecStart="{program}" machine connect\n'
            # A revoked computer exits 0 and stays stopped; a crash or lost network restarts it.
            f"Restart=on-failure\nRestartSec={self.restart_seconds:g}\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess:
        if shutil.which("systemctl") is None:
            raise ServiceUnavailable("this computer has no systemd: keep it connected with  galaius machine connect  in a terminal")
        done = subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=30)
        if done.returncode != 0 and arguments[0] not in {"is-active", "disable"}:
            raise ServiceUnavailable((done.stderr or done.stdout).strip() or f"systemctl --user {' '.join(arguments)} failed")
        return done

    def install(self) -> None:
        unit = self.unit()
        self._systemctl("--version")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(unit, encoding="utf-8")
        self._systemctl("daemon-reload")
        self._systemctl("enable", "--now", self.name)

    def remove(self) -> None:
        if shutil.which("systemctl") is not None and self.path.exists():
            self._systemctl("disable", "--now", self.name)
        self.path.unlink(missing_ok=True)
        if shutil.which("systemctl") is not None:
            self._systemctl("daemon-reload")

    def start(self) -> None:
        self._systemctl("start", self.name)

    def stop(self) -> None:
        self._systemctl("stop", self.name)

    def running(self) -> bool:
        return self._systemctl("is-active", "--quiet", self.name).returncode == 0

    def installed(self) -> bool:
        return self.path.exists()

    def last_words(self, lines: int = 3) -> str:
        done = subprocess.run(["journalctl", "--user", "-u", self.name, "-n", str(lines), "-o", "cat", "--no-pager"], capture_output=True, text=True, timeout=15) if shutil.which("journalctl") else None
        return done.stdout.strip() if done is not None and done.returncode == 0 else ""

    def after_logout(self) -> bool:
        """Lingering keeps it running after sign-out; some systems refuse."""
        if shutil.which("loginctl") is None:
            return False
        return subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True, text=True, timeout=30).returncode == 0


class WindowsMachineService(MachineService):
    """Windows: a Scheduled Task of this user, at their logon, through the Task Scheduler's own API.
    Its programs (the runner, the scripts and agents it starts) are held in a job barred from the
    desktop's shared state: no clipboard, no other program's windows, no desktop switching, no
    display or system settings (the task runs inside the signed-in session, unlike a Linux unit)."""

    #: What the job's programs may not touch (JOB_OBJECT_UILIMIT_*): other programs' windows
    #: (HANDLES), the clipboard, global atoms, desktops, display / system settings, sign-out.
    ui_limits: ClassVar[tuple[str, ...]] = ("HANDLES", "READCLIPBOARD", "WRITECLIPBOARD", "GLOBALATOMS", "DESKTOP", "DISPLAYSETTINGS", "SYSTEMPARAMETERS", "EXITWINDOWS")
    #: Task Scheduler's TASK_STATE values this reads (running, ready to start, switched off).
    states: ClassVar[dict[int, str]] = {1: "disabled", 3: "ready", 4: "running"}

    @property
    def task(self) -> str:
        """Task names are shared by every user of the computer: this user's SID keeps theirs apart."""
        return f"Galaius machine {win32security.ConvertSidToStringSid(WindowsPrivateFiles.user())}"

    @staticmethod
    def executable() -> Path:
        """`pythonw.exe` of this install's own environment: the same build as this CLI, no console window."""
        found = Path(sys.executable).with_name("pythonw.exe")
        if not found.is_file():
            raise ServiceUnavailable(f"{found} is missing: reinstall galaius with the line from your Galaius page")
        return found.absolute()

    @staticmethod
    def user() -> str:
        """DOMAIN\\name of this user: the task starts at THEIR logon only (an any-user trigger needs an administrator)."""
        return win32api.GetUserNameEx(win32con.NameSamCompatible)

    def definition(self) -> str:
        """The task, as Task Scheduler's XML: nothing the server sent goes in here."""
        user, program = escape(self.user()), escape(str(self.executable()))
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <RegistrationInfo><Description>Galaius: this computer as a machine of your account</Description></RegistrationInfo>\n"
            f"  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>\n"
            f'  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\n"
            "    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>\n"
            "  </Settings>\n"
            f'  <Actions Context="Author"><Exec><Command>{program}</Command><Arguments>-m galaius machine service run</Arguments></Exec></Actions>\n'
            "</Task>\n"
        )

    @staticmethod
    def _folder():
        pythoncom.CoInitialize()
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        return scheduler.GetFolder("\\")

    def _task(self):
        try:
            return self._folder().GetTask(self.task)
        except pywintypes.com_error:
            return None

    def install(self) -> None:
        try:
            # TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3: this user's own rights, no password kept.
            self._folder().RegisterTask(self.task, self.definition(), 6, None, None, 3)
        except pywintypes.com_error as error:
            raise ServiceUnavailable(f"Windows refused to register the logon task ({error.excepinfo[2] if error.excepinfo else error})") from None
        self.start()

    def remove(self) -> None:
        if self._task() is None:
            return
        self.stop()
        self._folder().DeleteTask(self.task, 0)

    def start(self) -> None:
        task = self._task()
        if task is None:
            raise ServiceUnavailable("this computer is not connected: run  galaius login")
        if self.running():
            return
        task.Run(None)

    def stop(self) -> None:
        task = self._task()
        if task is not None and self.running():
            task.Stop(0)

    def running(self) -> bool:
        task = self._task()
        return task is not None and self.states.get(task.State) == "running"

    def installed(self) -> bool:
        return self._task() is not None

    def last_words(self, lines: int = 3) -> str:
        task = self._task()
        result = f"Task Scheduler: last result 0x{task.LastTaskResult & 0xFFFFFFFF:x}" if task is not None else "Task Scheduler: no task"
        return "\n".join(filter(None, (result, super().last_words(lines))))

    def run(self) -> None:
        """What the task starts: its output in `log_path` first (no console window: an early
        failure must still be readable), its programs held in a job that closes with it, then the
        restarting connection."""
        if windowless_process():  # pythonw, or a worker in a hidden console: its output is seen only in the log
            path = self.log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 5 << 20:
                path.replace(path.with_suffix(".log.1"))
            sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process
        try:
            job = self.held_children()
            try:
                super().run()
            finally:
                job.Close()  # the last handle: every program still in the job ends with it
        except BaseException:
            traceback.print_exc()
            raise

    @staticmethod
    def held_children():
        """A job holding this process: every program it starts closes with it (stopping the task
        stops them) once the returned handle closes, i.e. when this process ends."""
        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        restrictions = 0
        for limit in WindowsMachineService.ui_limits:
            restrictions |= getattr(win32job, f"JOB_OBJECT_UILIMIT_{limit}")
        win32job.SetInformationJobObject(job, win32job.JobObjectBasicUIRestrictions, {"UIRestrictionsClass": restrictions})
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
        return job


class LaunchdMachineService(MachineService):
    """macOS: a launchd agent of this user, loaded at their login (`RunAtLoad`), restarted by
    launchd when the connection crashes (`KeepAlive` unless it exits cleanly, as a revoked computer
    does); its output in `log_path`."""

    label: ClassVar[str] = "ai.galaius.machine"

    @property
    def path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def definition(self) -> bytes:
        """The agent, as launchd's property list: nothing the server sent goes in here."""
        log = str(self.log_path())
        return plistlib.dumps({
            "Label": self.label, "ProgramArguments": [str(self.executable()), "machine", "connect"],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": int(self.restart_seconds),
            "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log,
        })

    def _launchctl(self, *arguments: str, tolerated: bool = False) -> subprocess.CompletedProcess:
        done = subprocess.run(["launchctl", *arguments], capture_output=True, text=True, timeout=30)
        if done.returncode != 0 and not tolerated:
            raise ServiceUnavailable((done.stderr or done.stdout).strip() or f"launchctl {' '.join(arguments)} failed")
        return done

    def install(self) -> None:
        definition = self.definition()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path().parent.mkdir(parents=True, exist_ok=True)
        self._launchctl("bootout", f"{self.domain}/{self.label}", tolerated=True)
        self.path.write_bytes(definition)
        self._launchctl("bootstrap", self.domain, str(self.path))

    def remove(self) -> None:
        self._launchctl("bootout", f"{self.domain}/{self.label}", tolerated=True)
        self.path.unlink(missing_ok=True)

    def start(self) -> None:
        if not self.installed():
            raise ServiceUnavailable("this computer is not connected: run  galaius login")
        if self._launchctl("print", f"{self.domain}/{self.label}", tolerated=True).returncode != 0:
            self._launchctl("bootstrap", self.domain, str(self.path))
        self._launchctl("kickstart", f"{self.domain}/{self.label}")

    def stop(self) -> None:
        """Unloaded until the next login or `start` (launchd would restart a killed one)."""
        self._launchctl("bootout", f"{self.domain}/{self.label}", tolerated=True)

    def running(self) -> bool:
        done = self._launchctl("print", f"{self.domain}/{self.label}", tolerated=True)
        return done.returncode == 0 and "state = running" in done.stdout

    def installed(self) -> bool:
        return self.path.exists()


class TerminalMachineService(MachineService):
    """No background service on this system: the connection runs in a terminal."""

    @property
    def logs(self) -> str:
        return "the terminal running  galaius machine connect"

    def install(self) -> None:
        raise ServiceUnavailable(f"{sys.platform} has no Galaius background service: keep it connected with  galaius machine connect  in a terminal")

    def remove(self) -> None:
        return None

    def start(self) -> None:
        self.install()

    def stop(self) -> None:
        self.install()

    def running(self) -> bool:
        return False

    def installed(self) -> bool:
        return False


MACHINE_SERVICE: MachineService = (
    WindowsMachineService() if sys.platform == "win32" else SystemdMachineService() if sys.platform.startswith("linux")
    else LaunchdMachineService() if sys.platform == "darwin" else TerminalMachineService())
